"""Unit tests for rate limiting and retry/backoff flow wiring (P2 / §5.1-5.2)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from pg_mcp.config.settings import ResilienceConfig, ValidationConfig
from pg_mcp.models.errors import (
    LLMResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    RateLimitExceededError,
)
from pg_mcp.models.query import QueryRequest, ReturnType, ValidationResult
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.orchestrator import QueryOrchestrator
from pg_mcp.services.sql_generator import GenerationResult


def _gen(sql: str = "SELECT 1;", tokens: int = 10) -> GenerationResult:
    return GenerationResult(sql=sql, tokens_used=tokens, model="m", latency_ms=1.0)


def _valid_detail() -> ValidationResult:
    return ValidationResult(
        is_valid=True,
        is_select=True,
        allows_data_modification=False,
        uses_blocked_functions=[],
        error_message=None,
    )


@pytest.fixture(autouse=True)
def _reset_metrics():
    """Isolate the metrics singleton between tests."""
    MetricsCollector.reset()
    yield
    MetricsCollector.reset()


def _make_orchestrator(
    generator=None,
    validator=None,
    rate_limiter=None,
    metrics=None,
    max_retries: int = 2,
) -> QueryOrchestrator:
    """Orchestrator with mocked services and configurable limiter/metrics."""
    gen = generator if generator is not None else AsyncMock()
    if not isinstance(gen.generate.return_value, GenerationResult):
        gen.generate.return_value = _gen()

    val = validator if validator is not None else MagicMock()
    val.validate_or_raise.return_value = None
    val.validate_detail.return_value = _valid_detail()

    return QueryOrchestrator(
        sql_generator=gen,
        sql_validator=val,
        sql_executor=AsyncMock(),
        result_validator=MagicMock(),
        schema_cache=MagicMock(),
        pools={"db": MagicMock()},
        resilience_config=ResilienceConfig(
            max_retries=max_retries,
            retry_delay=0.1,
            rate_limit_acquire_timeout=0.1,  # keep rejection tests fast
        ),
        validation_config=ValidationConfig(enabled=False),
        rate_limiter=rate_limiter,
        metrics=metrics,
    )


class TestQueryRateLimiting:
    """Query-slot behavior on execute_query (§5.1)."""

    @pytest.mark.asyncio
    async def test_query_rejected_when_slots_exhausted(self) -> None:
        """Exhausted query slots yield a rate_limit_exceeded response."""
        rl = MultiRateLimiter(query_limit=1, llm_limit=1)
        assert await rl.query_limiter.acquire()  # hold the only slot

        metrics = MetricsCollector()
        orch = _make_orchestrator(rate_limiter=rl, metrics=metrics)
        response = await orch.execute_query(QueryRequest(question="q", database="db"))

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "rate_limit_exceeded"

        # Narrow-acquire rejection is counted for observability
        value = metrics.query_requests.labels(
            status="rate_limit_exceeded", database="db"
        )._value.get()
        assert value == 1

        rl.query_limiter.release()

    @pytest.mark.asyncio
    async def test_query_slot_released_after_success(self) -> None:
        """The query slot is released once the pipeline finishes."""
        rl = MultiRateLimiter(query_limit=1, llm_limit=1)
        orch = _make_orchestrator(rate_limiter=rl)

        request = QueryRequest(question="q", database="db", return_type=ReturnType.SQL)
        response = await orch.execute_query(request)

        assert response.success is True
        # release() decrements the counter via a scheduled task; let it run
        await asyncio.sleep(0)
        assert rl.query_limiter.active_count == 0


class TestLLMSlot:
    """LLM-slot behavior in the generation loop (§5.1)."""

    @pytest.mark.asyncio
    async def test_llm_slot_exhaustion_raises_rate_limit(self) -> None:
        """No free LLM slot raises RateLimitExceededError without LLM calls."""
        rl = MultiRateLimiter(query_limit=5, llm_limit=1)
        assert await rl.llm_limiter.acquire()  # exhaust LLM slots

        gen = AsyncMock()
        gen.generate.return_value = _gen()
        orch = _make_orchestrator(generator=gen, rate_limiter=rl, max_retries=2)

        with pytest.raises(RateLimitExceededError):
            await orch._generate_sql_with_retry(question="q", schema=MagicMock(), request_id="r")

        # The failure happened before any LLM call: budget untouched
        assert gen.generate.call_count == 0

        rl.llm_limiter.release()


class TestRetryBackoffFlow:
    """Retry classification and backoff behavior (§5.2)."""

    @pytest.mark.asyncio
    async def test_transient_error_retries_with_backoff(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Timeout errors sleep (backoff) between attempts, then succeed."""
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("pg_mcp.services.orchestrator.asyncio.sleep", fake_sleep)

        gen = AsyncMock()
        gen.generate.side_effect = [LLMTimeoutError("timeout"), _gen("SELECT 1;")]
        orch = _make_orchestrator(generator=gen, max_retries=2)

        sql, _validation, _tokens = await orch._generate_sql_with_retry(
            question="q", schema=MagicMock(), request_id="r"
        )

        assert sql == "SELECT 1;"
        assert _tokens == 10
        assert gen.generate.call_count == 2
        assert len(sleeps) == 1
        assert 0 < sleeps[0] < 1.0  # tiny base + jitter stays small

    @pytest.mark.asyncio
    async def test_response_error_routes_to_feedback_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LLMResponseError retries via feedback, not silent backoff."""

        async def fake_sleep(seconds: float) -> None:
            pass

        monkeypatch.setattr("pg_mcp.services.orchestrator.asyncio.sleep", fake_sleep)

        gen = AsyncMock()
        gen.generate.side_effect = [
            LLMResponseError("empty content"),
            _gen("SELECT 2;"),
        ]
        orch = _make_orchestrator(generator=gen, max_retries=2)

        sql, _validation, _tokens = await orch._generate_sql_with_retry(
            question="q", schema=MagicMock(), request_id="r"
        )

        assert sql == "SELECT 2;"
        assert gen.generate.call_count == 2
        second_call = gen.generate.call_args_list[1]
        assert "Response unparseable" in second_call.kwargs["error_feedback"]
        assert second_call.kwargs["previous_attempt"] is None

    @pytest.mark.asyncio
    async def test_auth_failure_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-retryable LLMUnavailableError raises after a single attempt."""

        async def fake_sleep(seconds: float) -> None:
            raise AssertionError("auth failure must not backoff-retry")

        monkeypatch.setattr("pg_mcp.services.orchestrator.asyncio.sleep", fake_sleep)

        gen = AsyncMock()
        gen.generate.side_effect = LLMUnavailableError("bad key", retryable=False)
        orch = _make_orchestrator(generator=gen, max_retries=3)

        with pytest.raises(LLMUnavailableError):
            await orch._generate_sql_with_retry(question="q", schema=MagicMock(), request_id="r")

        assert gen.generate.call_count == 1
        assert orch.circuit_breaker.failure_count == 1

    @pytest.mark.asyncio
    async def test_rate_limit_429_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """retryable=True (429) LLMUnavailableError backs off and retries."""

        async def fake_sleep(seconds: float) -> None:
            pass

        monkeypatch.setattr("pg_mcp.services.orchestrator.asyncio.sleep", fake_sleep)

        gen = AsyncMock()
        gen.generate.side_effect = [
            LLMUnavailableError("429 too many requests", retryable=True),
            _gen("SELECT 3;"),
        ]
        orch = _make_orchestrator(generator=gen, max_retries=2)

        sql, _validation, _tokens = await orch._generate_sql_with_retry(
            question="q", schema=MagicMock(), request_id="r"
        )

        assert sql == "SELECT 3;"
        assert gen.generate.call_count == 2
        assert orch.circuit_breaker.failure_count == 0

    @pytest.mark.asyncio
    async def test_shared_retry_budget_across_error_types(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Transient errors and feedback retries share one max_retries budget."""

        async def fake_sleep(seconds: float) -> None:
            pass

        monkeypatch.setattr("pg_mcp.services.orchestrator.asyncio.sleep", fake_sleep)

        gen = AsyncMock()
        gen.generate.side_effect = [
            LLMTimeoutError("timeout"),  # backoff retry
            LLMResponseError("unparseable"),  # feedback retry (2nd of budget)
            LLMTimeoutError("timeout"),  # 3rd failure -> budget (2) exhausted
        ]
        orch = _make_orchestrator(generator=gen, max_retries=2)

        with pytest.raises(LLMTimeoutError):
            await orch._generate_sql_with_retry(question="q", schema=MagicMock(), request_id="r")

        # max_retries=2 -> at most 3 total LLM calls
        assert gen.generate.call_count == 3

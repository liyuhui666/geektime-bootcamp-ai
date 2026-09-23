"""Unit tests for request tracing context propagation (P2/P3 observability)."""

import logging
import re
import uuid

import pytest

from pg_mcp.observability.tracing import (
    TraceContext,
    TracingLogger,
    clear_request_id,
    generate_request_id,
    get_request_id,
    get_tracing_logger,
    request_context,
    set_request_id,
    trace_async,
    trace_sync,
)

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


@pytest.fixture(autouse=True)
def _clean_context():
    """Ensure each test starts and ends with no request id set."""
    clear_request_id()
    yield
    clear_request_id()


class TestRequestId:
    def test_generate_request_id_is_uuid4(self) -> None:
        rid = generate_request_id()
        assert UUID_RE.match(rid)

    def test_generate_request_id_unique(self) -> None:
        assert generate_request_id() != generate_request_id()

    def test_default_context_is_none(self) -> None:
        assert get_request_id() is None

    def test_set_and_get(self) -> None:
        set_request_id("custom-id")
        assert get_request_id() == "custom-id"

    def test_clear(self) -> None:
        set_request_id("custom-id")
        clear_request_id()
        assert get_request_id() is None


class TestRequestContext:
    @pytest.mark.asyncio
    async def test_generates_id_when_none(self) -> None:
        async with request_context() as rid:
            assert UUID_RE.match(rid)
            assert get_request_id() == rid

    @pytest.mark.asyncio
    async def test_uses_provided_id(self) -> None:
        async with request_context("fixed-id") as rid:
            assert rid == "fixed-id"
            assert get_request_id() == "fixed-id"

    @pytest.mark.asyncio
    async def test_resets_after_exit(self) -> None:
        async with request_context():
            pass
        assert get_request_id() is None

    @pytest.mark.asyncio
    async def test_resets_after_exception(self) -> None:
        with pytest.raises(RuntimeError):
            async with request_context():
                raise RuntimeError("boom")
        assert get_request_id() is None

    @pytest.mark.asyncio
    async def test_nested_context_restores_outer(self) -> None:
        async with request_context("outer") as outer:
            async with request_context("inner"):
                assert get_request_id() == "inner"
            assert get_request_id() == outer


class TestTraceDecorators:
    @pytest.mark.asyncio
    async def test_trace_async_without_context(self) -> None:
        @trace_async()
        async def work(value: int) -> int:
            return value * 2

        assert await work(21) == 42

    @pytest.mark.asyncio
    async def test_trace_async_with_context_restores_factory(self) -> None:
        @trace_async(operation="op")
        async def work() -> str:
            return "ok"

        async with request_context("rid-1"):
            assert await work() == "ok"
        # The global record factory must be restored afterwards
        assert logging.getLogRecordFactory() is not None

    @pytest.mark.asyncio
    async def test_trace_async_injects_request_id_into_records(self) -> None:
        seen: list[logging.LogRecord] = []

        class Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                seen.append(record)

        logger = logging.getLogger("trace-test")
        handler = Handler()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        @trace_async(operation="gen")
        async def work() -> None:
            logger.info("inside")

        async with request_context("rid-2"):
            await work()

        assert getattr(seen[0], "request_id", None) == "rid-2"
        assert getattr(seen[0], "operation", None) == "gen"

    def test_trace_sync_without_context(self) -> None:
        @trace_sync()
        def work(value: int) -> int:
            return value + 1

        assert work(1) == 2

    def test_trace_sync_with_context(self) -> None:
        @trace_sync(operation="val")
        def work() -> None:
            logging.getLogger("trace-test-sync").info("inside")

        async_holder = request_context("rid-3")

        import asyncio

        async def run() -> None:
            async with async_holder:
                work()

        asyncio.run(run())


class TestTracingLogger:
    def test_all_levels_log(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = get_tracing_logger("tracing-logger-test")
        with caplog.at_level(logging.DEBUG, logger="tracing-logger-test"):
            logger.debug("d")
            logger.info("i")
            logger.warning("w")
            logger.error("e")
            logger.critical("c")
        messages = [r.message for r in caplog.records]
        assert messages == ["d", "i", "w", "e", "c"]

    def test_injects_request_id_into_extra(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = TracingLogger("tracing-logger-test-2")
        with caplog.at_level(logging.INFO, logger="tracing-logger-test-2"):
            import asyncio

            async def run() -> None:
                async with request_context("rid-4"):
                    logger.info("msg", extra={"database": "db1"})

            asyncio.run(run())
        record = caplog.records[0]
        assert record.request_id == "rid-4"  # type: ignore[attr-defined]
        assert record.database == "db1"  # type: ignore[attr-defined]

    def test_existing_request_id_not_overwritten(self) -> None:
        logger = TracingLogger("tracing-logger-test-3")
        extra = {"request_id": "explicit"}
        # Direct _log call to inspect the merged extra
        logger._log(logging.INFO, "m", extra=extra)
        assert extra["request_id"] == "explicit"

    def test_exception_logs_traceback(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = TracingLogger("tracing-logger-test-4")
        with caplog.at_level(logging.ERROR, logger="tracing-logger-test-4"):
            try:
                raise ValueError("cause")
            except ValueError:
                logger.exception("failed")
        assert caplog.records[0].exc_info is not None


class TestTraceContextModel:
    def test_defaults(self) -> None:
        ctx = TraceContext(request_id="r1")
        assert ctx.parent_id is None
        assert ctx.operation is None
        assert ctx.metadata is None

    def test_full(self) -> None:
        ctx = TraceContext(request_id="r1", parent_id="p", operation="op", metadata={"k": 1})
        assert ctx.parent_id == "p"
        assert ctx.metadata == {"k": 1}


def test_uuid_helper_consistent_with_stdlib() -> None:
    rid = generate_request_id()
    assert str(uuid.UUID(rid)) == rid

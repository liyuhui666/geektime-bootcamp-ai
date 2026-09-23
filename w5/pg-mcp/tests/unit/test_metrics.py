"""Unit tests for the Prometheus metrics collector (P2 / §5.3)."""

import pytest
from prometheus_client import REGISTRY

from pg_mcp.observability.metrics import MetricsCollector


@pytest.fixture(autouse=True)
def _reset_metrics():
    """Isolate the metrics singleton between tests."""
    MetricsCollector.reset()
    yield
    MetricsCollector.reset()


def _counter_value(counter, **labels) -> float:
    return counter.labels(**labels)._value.get()


class TestSingleton:
    def test_same_instance_returned(self) -> None:
        assert MetricsCollector() is MetricsCollector()

    def test_reset_returns_same_instance_with_fresh_values(self) -> None:
        metrics = MetricsCollector()
        metrics.increment_query_request("success", "db1")

        MetricsCollector.reset()

        assert MetricsCollector() is metrics
        assert _counter_value(metrics.query_requests, status="success", database="db1") == 0


class TestQueryMetrics:
    def test_query_request_labels(self) -> None:
        metrics = MetricsCollector()
        metrics.increment_query_request("success", "db1")
        metrics.increment_query_request("error", "db1")
        metrics.increment_query_request("success", "db1")

        assert _counter_value(metrics.query_requests, status="success", database="db1") == 2
        assert _counter_value(metrics.query_requests, status="error", database="db1") == 1

    def test_query_duration_observed(self) -> None:
        metrics = MetricsCollector()
        metrics.query_duration.observe(0.7)

        samples = metrics.query_duration.collect()[0].samples
        count = [s for s in samples if s.name.endswith("_count")]
        assert count[0].value == 1


class TestLLMMetrics:
    def test_llm_call_operation_and_status_labels(self) -> None:
        metrics = MetricsCollector()
        metrics.increment_llm_call("generation")
        metrics.increment_llm_call("generation", status="error")
        metrics.increment_llm_call("validation")

        assert _counter_value(metrics.llm_calls, operation="generation", status="success") == 1
        assert _counter_value(metrics.llm_calls, operation="generation", status="error") == 1
        assert _counter_value(metrics.llm_calls, operation="validation", status="success") == 1

    def test_llm_latency_label(self) -> None:
        metrics = MetricsCollector()
        metrics.observe_llm_latency("generation", 1.2)

        count = [
            s
            for s in metrics.llm_latency.labels(operation="generation").collect()[0].samples
            if s.name.endswith("_count")
        ]
        assert count[0].value == 1

    def test_llm_tokens_accumulate(self) -> None:
        metrics = MetricsCollector()
        metrics.increment_llm_tokens("generation", 120)
        metrics.increment_llm_tokens("generation", 30)

        assert _counter_value(metrics.llm_tokens_used, operation="generation") == 150


class TestResilienceMetrics:
    def test_sql_validation_failure_reasons(self) -> None:
        metrics = MetricsCollector()
        metrics.increment_sql_validation_failure("security")
        metrics.increment_sql_validation_failure("parse")
        metrics.increment_sql_validation_failure("parse")

        assert _counter_value(metrics.sql_validation_failures, reason="security") == 1
        assert _counter_value(metrics.sql_validation_failures, reason="parse") == 2

    def test_sql_generation_duration_attempt_label(self) -> None:
        metrics = MetricsCollector()
        metrics.observe_sql_generation_duration(0, 0.5)
        metrics.observe_sql_generation_duration(1, 0.8)

        count0 = [
            s
            for s in metrics.sql_generation_duration.labels(attempt="0").collect()[0].samples
            if s.name.endswith("_count")
        ]
        count1 = [
            s
            for s in metrics.sql_generation_duration.labels(attempt="1").collect()[0].samples
            if s.name.endswith("_count")
        ]
        assert count0[0].value == 1
        assert count1[0].value == 1

    def test_database_error_sqlstate_class(self) -> None:
        metrics = MetricsCollector()
        metrics.increment_database_error("db1", "28")
        metrics.increment_database_error("db1", "42")
        metrics.increment_database_error("db1", "28")

        assert _counter_value(metrics.database_errors, database="db1", sqlstate_class="28") == 2
        assert _counter_value(metrics.database_errors, database="db1", sqlstate_class="42") == 1

    def test_circuit_breaker_state_mapping(self) -> None:
        metrics = MetricsCollector()

        metrics.set_circuit_breaker_state("closed")
        assert metrics.circuit_breaker_state._value.get() == 0

        metrics.set_circuit_breaker_state("half_open")
        assert metrics.circuit_breaker_state._value.get() == 1

        metrics.set_circuit_breaker_state("open")
        assert metrics.circuit_breaker_state._value.get() == 2

    def test_circuit_breaker_state_unknown_defaults_closed(self) -> None:
        metrics = MetricsCollector()
        metrics.set_circuit_breaker_state("bogus")

        assert metrics.circuit_breaker_state._value.get() == 0

    def test_rate_limiter_active_labels(self) -> None:
        metrics = MetricsCollector()
        metrics.set_rate_limiter_active("queries", 3)
        metrics.set_rate_limiter_active("llm", 1)

        assert metrics.rate_limiter_active.labels(type="queries")._value.get() == 3
        assert metrics.rate_limiter_active.labels(type="llm")._value.get() == 1


class TestRegistration:
    def test_all_metrics_registered_in_default_registry(self) -> None:
        """Every collector is visible through the shared Prometheus registry."""
        metrics = MetricsCollector()
        registered = {m.name for m in REGISTRY.collect()}
        for collector in (
            metrics.query_requests,
            metrics.query_duration,
            metrics.llm_calls,
            metrics.llm_latency,
            metrics.llm_tokens_used,
            metrics.sql_rejected,
            metrics.sql_generation_duration,
            metrics.sql_validation_failures,
            metrics.database_errors,
            metrics.circuit_breaker_state,
            metrics.rate_limiter_active,
            metrics.db_connections_active,
            metrics.db_query_duration,
            metrics.schema_cache_age,
        ):
            assert collector.collect()[0].name in registered

    def test_reinit_after_reset_has_no_registry_collision(self) -> None:
        metrics = MetricsCollector()
        metrics.increment_query_request("success", "db1")
        metrics.reset_all_metrics()

        # Re-increment after reset: values fresh, no DuplicatedTimeseries error
        metrics.increment_query_request("success", "db1")
        assert _counter_value(metrics.query_requests, status="success", database="db1") == 1

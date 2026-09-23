"""Unit tests for DatabaseManager runtime construction (P3 / §4.3)."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import pg_mcp.db.runtime as runtime_module
from pg_mcp.config.policy import EffectivePolicy
from pg_mcp.config.settings import MultiDatabaseConfig, OpenAIConfig, Settings
from pg_mcp.db.runtime import DatabaseManager
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_validator import SQLValidator


def make_settings(databases_json: str) -> Settings:
    return Settings(
        openai=OpenAIConfig(api_key="sk-test-key"),
        multidb=MultiDatabaseConfig(databases_json=databases_json),  # type: ignore[arg-type]
    )


def make_pool_mock() -> MagicMock:
    pool = MagicMock()
    pool.close = AsyncMock()
    return pool


@pytest.fixture
def patch_create_pool(monkeypatch: pytest.MonkeyPatch) -> list[MagicMock]:
    """Stub create_pool; returns the list of pools handed out in call order."""
    pools: list[MagicMock] = []

    async def fake_create_pool(config: Any) -> MagicMock:
        pool = make_pool_mock()
        pools.append(pool)
        return pool

    monkeypatch.setattr(runtime_module, "create_pool", fake_create_pool)
    return pools


TWO_DB_JSON = json.dumps(
    [
        {"connection": {"name": "db1"}},
        {
            "connection": {"name": "db2"},
            "security": {"blocked_tables": ["secrets"]},
        },
    ]
)


class TestDatabaseManagerBuild:
    """Runtime construction, policy wiring and fail-fast behavior."""

    @pytest.mark.asyncio
    async def test_builds_runtime_per_database(self, patch_create_pool: list[MagicMock]) -> None:
        runtimes = await DatabaseManager.build(make_settings(TWO_DB_JSON))

        assert set(runtimes.keys()) == {"db1", "db2"}
        assert len(patch_create_pool) == 2

    @pytest.mark.asyncio
    async def test_policy_follows_database(self, patch_create_pool) -> None:
        """Each runtime's validator/executor share that database's policy."""
        runtimes = await DatabaseManager.build(make_settings(TWO_DB_JSON))

        rt1, rt2 = runtimes["db1"], runtimes["db2"]

        # db1: pure global policy; db2: override replaces table blocklist
        assert rt1.policy.blocked_tables == frozenset()
        assert rt2.policy.blocked_tables == frozenset({"secrets"})

        assert isinstance(rt1.validator, SQLValidator)
        assert isinstance(rt1.executor, SQLExecutor)
        assert rt1.validator.policy is rt1.policy
        assert rt1.executor.policy is rt1.policy
        assert rt2.validator.policy is rt2.policy
        assert rt2.executor.policy is rt2.policy
        assert rt2.executor.database_name == "db2"

    @pytest.mark.asyncio
    async def test_executor_gets_pool(self, patch_create_pool) -> None:
        runtimes = await DatabaseManager.build(make_settings(TWO_DB_JSON))
        assert runtimes["db1"].executor.pool is patch_create_pool[0]
        assert runtimes["db1"].pool is patch_create_pool[0]

    @pytest.mark.asyncio
    async def test_single_database_mode_builds_one_runtime(self, patch_create_pool) -> None:
        settings = make_settings("   ")  # blank -> single-database mode
        runtimes = await DatabaseManager.build(settings)
        assert len(runtimes) == 1

    @pytest.mark.asyncio
    async def test_fail_fast_on_connection_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failing pool creation aborts startup (exception propagates)."""

        async def failing_create_pool(config: Any) -> None:
            raise RuntimeError("connection refused")

        monkeypatch.setattr(runtime_module, "create_pool", failing_create_pool)

        with pytest.raises(RuntimeError, match="connection refused"):
            await DatabaseManager.build(make_settings(TWO_DB_JSON))

    @pytest.mark.asyncio
    async def test_metrics_handed_to_executor(self, patch_create_pool) -> None:
        from pg_mcp.observability.metrics import MetricsCollector

        MetricsCollector.reset()
        metrics = MetricsCollector()
        runtimes = await DatabaseManager.build(make_settings(TWO_DB_JSON), metrics=metrics)
        assert runtimes["db1"].executor.metrics is metrics
        MetricsCollector.reset()

    def test_policy_merge_single_db_matches_global(self) -> None:
        """Sanity: single-db mode entry has no override -> global policy."""
        settings = make_settings(TWO_DB_JSON)
        policy = EffectivePolicy.merge(settings.security, settings.databases[0].security)
        assert policy.blocked_tables == frozenset()

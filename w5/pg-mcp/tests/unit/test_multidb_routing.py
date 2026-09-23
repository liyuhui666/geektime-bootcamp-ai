"""Multi-database routing integration tests (P3 / §7.2).

Verifies the key multi-database security property end-to-end through
execute_query: policy follows the database. The same generated SQL is
accepted by one database and rejected by another, because each request is
validated under the routed database's EffectivePolicy.

Real SQLValidator instances are used (with different policies); LLM and
execution are mocked.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from pg_mcp.config.policy import EffectivePolicy
from pg_mcp.config.settings import (
    DatabaseSecurityOverride,
    ResilienceConfig,
    SecurityConfig,
    ValidationConfig,
)
from pg_mcp.db.runtime import DatabaseRuntime
from pg_mcp.models.query import QueryRequest, ReturnType
from pg_mcp.services.orchestrator import QueryOrchestrator
from pg_mcp.services.sql_generator import GenerationResult
from pg_mcp.services.sql_validator import SQLValidator


def _gen(sql: str = "SELECT * FROM secrets") -> GenerationResult:
    return GenerationResult(sql=sql, tokens_used=10, model="m", latency_ms=1.0)


def _runtime(name: str, override: DatabaseSecurityOverride | None) -> DatabaseRuntime:
    policy = EffectivePolicy.merge(SecurityConfig(), override)
    pool = MagicMock()
    executor = AsyncMock()
    executor.execute.return_value = ([{"id": 1}], 1)
    return DatabaseRuntime(
        name=name,
        pool=pool,
        executor=executor,
        validator=SQLValidator(policy=policy),
        policy=policy,
    )


def _orchestrator(default_database: str | None = None) -> QueryOrchestrator:
    """db1 has no restrictions; db2 blocks the table 'secrets'."""
    runtimes = {
        "db1": _runtime("db1", None),
        "db2": _runtime("db2", DatabaseSecurityOverride(blocked_tables=["secrets"])),
    }
    schema_cache = MagicMock()
    schema_cache.get.return_value = MagicMock()
    generator = AsyncMock()
    generator.generate.return_value = _gen()

    return QueryOrchestrator(
        runtimes=runtimes,
        sql_generator=generator,
        result_validator=MagicMock(),
        schema_cache=schema_cache,
        resilience_config=ResilienceConfig(retry_delay=0.1),
        validation_config=ValidationConfig(enabled=False),
        default_database=default_database,
    )


@pytest.mark.asyncio
async def test_same_sql_allowed_on_unrestricted_db() -> None:
    orch = _orchestrator()
    response = await orch.execute_query(
        QueryRequest(question="show secrets", database="db1", return_type=ReturnType.SQL)
    )
    assert response.success is True


@pytest.mark.asyncio
async def test_same_sql_rejected_on_restricted_db() -> None:
    """db2's blocklist applies: identical SQL is a security violation there."""
    orch = _orchestrator()
    response = await orch.execute_query(
        QueryRequest(question="show secrets", database="db2", return_type=ReturnType.SQL)
    )
    assert response.success is False
    assert response.error is not None
    assert response.error.code == "security_violation"


@pytest.mark.asyncio
async def test_default_database_routes_to_unrestricted_db() -> None:
    """No explicit database + default_database=db1 -> db1's policy applies."""
    orch = _orchestrator(default_database="db1")
    response = await orch.execute_query(
        QueryRequest(question="show secrets", return_type=ReturnType.SQL)
    )
    assert response.success is True


@pytest.mark.asyncio
async def test_default_database_routes_to_restricted_db() -> None:
    """Routing to db2 by default enforces db2's policy, not the global one."""
    orch = _orchestrator(default_database="db2")
    response = await orch.execute_query(
        QueryRequest(question="show secrets", return_type=ReturnType.SQL)
    )
    assert response.success is False
    assert response.error is not None
    assert response.error.code == "security_violation"


@pytest.mark.asyncio
async def test_execution_uses_routed_runtime_executor() -> None:
    """The executor actually invoked belongs to the routed database."""
    orch = _orchestrator()
    response = await orch.execute_query(
        QueryRequest(question="count users", database="db2", return_type=ReturnType.RESULT)
    )

    # Swap in permissive SQL that both DBs accept; assert db2's executor ran.
    orch.sql_generator.generate.return_value = _gen("SELECT count(*) FROM users")
    response = await orch.execute_query(
        QueryRequest(question="count users", database="db2", return_type=ReturnType.RESULT)
    )
    assert response.success is True
    assert response.data is not None
    orch.runtimes["db2"].executor.execute.assert_awaited_once()
    orch.runtimes["db1"].executor.execute.assert_not_awaited()

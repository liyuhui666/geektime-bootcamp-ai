"""Unit tests for the MCP query tool request handling (server layer).

Covers the tool-level guard paths; the orchestrator itself is mocked.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import pg_mcp.server as server_module
from pg_mcp.models.query import QueryResponse
from pg_mcp.observability.tracing import clear_request_id
from pg_mcp.server import query


@pytest.fixture(autouse=True)
def _restore_globals():
    """Restore server globals and tracing context after each test."""
    yield
    server_module._orchestrator = None
    clear_request_id()


def success_response(**overrides: Any) -> QueryResponse:
    payload: dict[str, Any] = {
        "success": True,
        "generated_sql": "SELECT 1;",
        "data": None,
        "error": None,
        "confidence": 0,
        "tokens_used": 5,
    }
    payload.update(overrides)
    return QueryResponse(**payload)


class TestQueryToolGuards:
    @pytest.mark.asyncio
    async def test_not_initialized(self) -> None:
        server_module._orchestrator = None
        result = await query(question="q")
        assert result["success"] is False
        assert result["error"]["code"] == "SERVER_NOT_INITIALIZED"

    @pytest.mark.asyncio
    async def test_invalid_return_type(self) -> None:
        server_module._orchestrator = MagicMock()
        result = await query(question="q", return_type="bogus")
        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_PARAMETER"
        assert "bogus" in result["error"]["message"]

    @pytest.mark.asyncio
    async def test_invalid_request(self) -> None:
        server_module._orchestrator = MagicMock()
        result = await query(question="")  # empty question fails QueryRequest
        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_REQUEST"

    @pytest.mark.asyncio
    async def test_success_returns_response_dict(self) -> None:
        orch = MagicMock()
        orch.execute_query = AsyncMock(return_value=success_response())
        server_module._orchestrator = orch

        result = await query(question="count users", database="db1", return_type="sql")

        assert result["success"] is True
        assert result["generated_sql"] == "SELECT 1;"
        assert result["tokens_used"] == 5
        request = orch.execute_query.await_args.args[0]
        assert request.question == "count users"
        assert request.database == "db1"

    @pytest.mark.asyncio
    async def test_unexpected_exception_becomes_internal_error(self) -> None:
        orch = MagicMock()
        orch.execute_query = AsyncMock(side_effect=RuntimeError("boom"))
        server_module._orchestrator = orch

        result = await query(question="q")
        assert result["success"] is False
        assert result["error"]["code"] == "INTERNAL_ERROR"
        assert result["error"]["details"]["error_type"] == "RuntimeError"


class TestSettingsHelpers:
    def test_environment_flags(self) -> None:
        from pg_mcp.config.settings import OpenAIConfig, Settings

        settings = Settings(openai=OpenAIConfig(api_key="sk-test-key"), environment="production")
        assert settings.is_production is True
        assert settings.is_development is False

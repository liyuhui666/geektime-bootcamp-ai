"""Unit tests for connection pool lifecycle management (P3)."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import pg_mcp.db.pool as pool_module
from pg_mcp.config.settings import DatabaseConfig
from pg_mcp.db.pool import close_pools, create_pool, create_pools


def make_config(name: str = "db1") -> DatabaseConfig:
    return DatabaseConfig(name=name, host="localhost")


def make_pool_mock() -> MagicMock:
    pool = MagicMock()
    pool.close = AsyncMock()
    pool.terminate = MagicMock()
    return pool


class TestCreatePool:
    @pytest.mark.asyncio
    async def test_passes_config_parameters(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        async def fake_create_pool(**kwargs: Any) -> MagicMock:
            captured.update(kwargs)
            return make_pool_mock()

        monkeypatch.setattr(pool_module.asyncpg, "create_pool", fake_create_pool)

        config = DatabaseConfig(
            name="appdb",
            host="h",
            port=5433,
            user="u",
            password="p",
            min_pool_size=2,
            max_pool_size=9,
            pool_timeout=7.0,
            command_timeout=11.0,
        )
        pool = await create_pool(config)

        assert pool is not None
        assert captured["database"] == "appdb"
        assert captured["host"] == "h"
        assert captured["port"] == 5433
        assert captured["user"] == "u"
        assert captured["min_size"] == 2
        assert captured["max_size"] == 9
        assert captured["timeout"] == 7.0
        assert captured["command_timeout"] == 11.0

    @pytest.mark.asyncio
    async def test_none_pool_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_create_pool(**kwargs: Any) -> None:
            return None

        monkeypatch.setattr(pool_module.asyncpg, "create_pool", fake_create_pool)

        with pytest.raises(RuntimeError, match="Failed to create connection pool"):
            await create_pool(make_config())


class TestCreatePools:
    @pytest.mark.asyncio
    async def test_creates_pool_per_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pools_by_name = {"db1": make_pool_mock(), "db2": make_pool_mock()}

        async def fake_create_pool(**kwargs: Any) -> MagicMock:
            return pools_by_name[kwargs["database"]]

        monkeypatch.setattr(pool_module.asyncpg, "create_pool", fake_create_pool)

        pools = await create_pools([make_config("db1"), make_config("db2")])
        assert pools == pools_by_name


class TestClosePools:
    @pytest.mark.asyncio
    async def test_graceful_close(self) -> None:
        pool = make_pool_mock()
        await close_pools({"db1": pool}, timeout=1.0)
        pool.close.assert_awaited_once()
        pool.terminate.assert_not_called()

    @pytest.mark.asyncio
    async def test_timeout_forces_terminate(self) -> None:
        pool = make_pool_mock()

        async def slow_close() -> None:
            import asyncio

            await asyncio.sleep(5)

        pool.close = slow_close
        # wait_for needs the coroutine; patch close to raise TimeoutError instead
        pool2 = make_pool_mock()
        pool2.close = AsyncMock(side_effect=TimeoutError)

        await close_pools({"slow": pool, "timeout": pool2}, timeout=0.01)
        pool2.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_error_forces_terminate_and_continues(self) -> None:
        failing = make_pool_mock()
        failing.close = AsyncMock(side_effect=RuntimeError("boom"))
        healthy = make_pool_mock()

        await close_pools({"bad": failing, "good": healthy}, timeout=1.0)

        failing.terminate.assert_called_once()
        healthy.close.assert_awaited_once()
        healthy.terminate.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_pools_no_error(self) -> None:
        await close_pools({}, timeout=1.0)

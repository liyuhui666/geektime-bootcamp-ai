"""Per-database runtime: everything a request needs when routed to one database.

A DatabaseRuntime bundles the connection pool, the SQL executor, and the SQL
validator — each constructed with that database's EffectivePolicy. Policy
follows the database: a request routed to database B is validated and executed
entirely under B's policy, so per-database blocklists cannot be bypassed by
switching databases mid-request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pg_mcp.config.policy import EffectivePolicy
from pg_mcp.db.pool import create_pool

if TYPE_CHECKING:
    # Imported lazily at runtime inside DatabaseManager.build: importing the
    # services package here would be circular (services.orchestrator imports
    # DatabaseRuntime from this module).
    from asyncpg import Pool

    from pg_mcp.config.settings import Settings
    from pg_mcp.observability.metrics import MetricsCollector
    from pg_mcp.services.sql_executor import SQLExecutor
    from pg_mcp.services.sql_validator import SQLValidator

logger = logging.getLogger(__name__)


@dataclass
class DatabaseRuntime:
    """All runtime components for one database.

    Attributes:
        name: Database name (the routing key).
        pool: asyncpg connection pool.
        executor: SQL executor bound to this database's policy.
        validator: SQL validator bound to this database's policy.
        policy: The merged effective policy for this database.
    """

    name: str
    pool: Pool
    executor: SQLExecutor
    validator: SQLValidator
    policy: EffectivePolicy


class DatabaseManager:
    """Builds DatabaseRuntime objects from resolved settings."""

    @staticmethod
    async def build(
        settings: Settings,
        metrics: MetricsCollector | None = None,
    ) -> dict[str, DatabaseRuntime]:
        """Build runtimes for every configured database.

        Construction is fail-fast: if any database's pool cannot be created,
        the exception propagates and the server refuses to start.

        Args:
            settings: Application settings (databases list already resolved).
            metrics: Optional metrics collector handed to each executor.

        Returns:
            dict[str, DatabaseRuntime]: Runtimes keyed by database name.

        Raises:
            asyncpg.PostgresError: If connecting to any database fails.
            RuntimeError: If a pool could not be created.
        """
        runtimes: dict[str, DatabaseRuntime] = {}

        # Local imports: see TYPE_CHECKING note above (import-cycle avoidance).
        from pg_mcp.services.sql_executor import SQLExecutor
        from pg_mcp.services.sql_validator import SQLValidator

        for entry in settings.databases:
            conn = entry.connection
            policy = EffectivePolicy.merge(settings.security, entry.security)

            logger.info(
                f"Connecting to database '{conn.name}'",
                extra={
                    "dsn": conn.safe_dsn,
                    "min_size": conn.min_pool_size,
                    "max_size": conn.max_pool_size,
                },
            )
            pool = await create_pool(conn)

            validator = SQLValidator(policy=policy)
            executor = SQLExecutor(
                pool=pool,
                policy=policy,
                database_name=conn.name,
                metrics=metrics,
            )
            runtimes[conn.name] = DatabaseRuntime(
                name=conn.name,
                pool=pool,
                executor=executor,
                validator=validator,
                policy=policy,
            )
            logger.info(f"Database '{conn.name}' ready (policy merged)")

        return runtimes

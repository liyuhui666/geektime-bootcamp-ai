"""FastMCP server for PostgreSQL natural language query interface.

This module implements the MCP server using FastMCP, exposing the query
functionality as an MCP tool. It includes complete lifespan management for
initializing and cleaning up all components.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import Settings
from pg_mcp.db.pool import close_pools
from pg_mcp.db.runtime import DatabaseManager, DatabaseRuntime
from pg_mcp.models.query import QueryRequest, QueryResponse, ReturnType
from pg_mcp.observability.logging import configure_logging, get_logger
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.tracing import request_context
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.orchestrator import QueryOrchestrator
from pg_mcp.services.result_validator import ResultValidator
from pg_mcp.services.sql_generator import SQLGenerator

logger = get_logger(__name__)

# Global state for lifespan management
_settings: Settings | None = None
_runtimes: dict[str, DatabaseRuntime] = {}
_schema_cache: SchemaCache | None = None
_orchestrator: QueryOrchestrator | None = None
_metrics: MetricsCollector | None = None
_rate_limiter: MultiRateLimiter | None = None
# Whether the heavy init below already ran in this process. HTTP mode runs
# it once at app startup; per-session lifespan entries then become no-ops.
_lifespan_initialized = False


@asynccontextmanager
async def lifespan(_app: FastMCP) -> AsyncIterator[None]:  # type: ignore[type-arg]
    """Lifespan context manager for server initialization and cleanup.

    This function manages the complete lifecycle of the MCP server:

    Startup:
        1. Load configuration from Settings
        2. Configure logging
        3. Create database connection pools
        4. Load schema cache for all databases
        5. Initialize metrics collector
        6. Create service components (generators, validators, executors)
        7. Initialize resilience components (circuit breaker, rate limiter)
        8. Create query orchestrator
        9. Start metrics HTTP server (optional)

    Shutdown:
        1. Stop schema auto-refresh (if enabled)
        2. Close all database connection pools
        3. Stop metrics HTTP server (if running)

    Yields:
        None

    Example:
        >>> async with lifespan():
        ...     # Server is running with all components initialized
        ...     pass
    """
    global _settings, _runtimes, _schema_cache, _orchestrator, _metrics
    global _rate_limiter, _lifespan_initialized

    # Background schema loaders; cancelled on shutdown if still running.
    schema_load_tasks: list[asyncio.Task[None]] = []

    # The official SDK's HTTP transport enters this lifespan per client
    # session (lowlevel Server.run), not once per process. All init below
    # is process-global (settings, pools, orchestrator), so only the first
    # entry does the work; later sessions reuse it. Doing it again would
    # stack another pool per reconnect until PostgreSQL hits max_connections.
    if _lifespan_initialized:
        logger.info("Reusing already-initialized server components")
        yield
        return

    logger.info("Starting PostgreSQL MCP Server initialization...")

    try:
        # 1. Load Settings
        logger.info("Loading configuration...")
        _settings = Settings()

        # 2. Configure logging
        logger.info("Configuring logging...")
        configure_logging(
            level=_settings.observability.log_level,
            log_format=_settings.observability.log_format,
            enable_sensitive_filter=True,
        )

        logger.info(
            "Configuration loaded",
            extra={
                "environment": _settings.environment,
                "log_level": _settings.observability.log_level,
                "databases": [entry.connection.name for entry in _settings.databases],
            },
        )

        # 3. Build per-database runtimes (pools + policy-bound validators and
        # executors). Fail-fast: any connection error aborts startup.
        logger.info("Creating database runtimes...")
        _runtimes = await DatabaseManager.build(_settings)

        # 4. Load Schema cache in the background. Introspection can take a
        # while on wide databases; blocking startup on it delays the MCP
        # initialize handshake past clients' request timeouts (e.g. 60s).
        # The orchestrator falls back to on-demand loading if a query
        # arrives before the background load finishes.
        logger.info("Initializing schema cache...")
        _schema_cache = SchemaCache(_settings.cache)

        async def _load_schema_in_background(rt: DatabaseRuntime) -> None:
            logger.info(f"Loading schema for database '{rt.name}' (background)...")
            try:
                schema = await _schema_cache.load(rt.name, rt.pool)
                logger.info(
                    f"Schema loaded for '{rt.name}'",
                    extra={
                        "tables": len(schema.tables),
                    },
                )
            except Exception as e:
                # Startup continues; the orchestrator retries on demand and
                # surfaces a clean error if the database is unreachable.
                logger.error(f"Background schema load failed for '{rt.name}': {e!s}")

        for rt in _runtimes.values():
            schema_load_tasks.append(asyncio.create_task(_load_schema_in_background(rt)))

        # Optional: Start schema auto-refresh
        # Disabled by default to avoid unnecessary background tasks
        # Uncomment to enable:
        # if _settings.cache.enabled:
        #     logger.info("Starting schema auto-refresh...")
        #     await _schema_cache.start_auto_refresh(
        #         interval_minutes=60,  # Refresh every hour
        #         pools={name: rt.pool for name, rt in _runtimes.items()},
        #     )

        # 5. Initialize metrics collector
        logger.info("Initializing metrics collector...")
        _metrics = MetricsCollector()

        # Start metrics HTTP server if enabled
        if _settings.observability.metrics_enabled:
            from prometheus_client import start_http_server

            start_http_server(_settings.observability.metrics_port)
            logger.info(f"Metrics server started on port {_settings.observability.metrics_port}")

        # 6. Create LLM-backed service components (shared across databases)
        logger.info("Initializing service components...")

        # SQL Generator
        sql_generator = SQLGenerator(_settings.openai)

        # Result Validator
        result_validator = ResultValidator(
            openai_config=_settings.openai,
            validation_config=_settings.validation,
        )

        # 7. Initialize resilience components
        logger.info("Initializing resilience components...")

        # Rate limiter: query slots cover the whole pipeline; LLM slots cover
        # both LLM call sites (generation + result validation). The circuit
        # breaker lives inside the orchestrator.
        _rate_limiter = MultiRateLimiter(
            query_limit=_settings.resilience.rate_limit_query,
            llm_limit=_settings.resilience.rate_limit_llm,
        )

        # 8. Create QueryOrchestrator
        logger.info("Creating query orchestrator...")
        _orchestrator = QueryOrchestrator(
            runtimes=_runtimes,
            sql_generator=sql_generator,
            result_validator=result_validator,
            schema_cache=_schema_cache,
            resilience_config=_settings.resilience,
            validation_config=_settings.validation,
            rate_limiter=_rate_limiter,
            metrics=_metrics,
            default_database=_settings.multidb.default_database,
        )

        logger.info("PostgreSQL MCP Server initialization complete!")
        logger.info(
            "Server ready to accept requests",
            extra={
                "databases": list(_runtimes.keys()),
                "default_database": _settings.multidb.default_database,
                "cache_enabled": _settings.cache.enabled,
                "metrics_enabled": _settings.observability.metrics_enabled,
            },
        )

        # Yield to run the server. _lifespan_initialized marks that the
        # first (owning) session completed setup; if THIS session crashes,
        # later sessions must still be able to re-run full init, so the
        # flag is cleared on the way out.
        _lifespan_initialized = True

        yield

    finally:
        # Full teardown when the owning session exits (transport shutting
        # down) or crashes - see "Cleaning up crashed session" in logs.
        # Clearing the flag lets a later session re-run full init.
        _lifespan_initialized = False

        # Cancel schema loads still running in the background (they hold
        # pool connections that are about to be closed).
        if schema_load_tasks:
            for task in schema_load_tasks:
                task.cancel()
            await asyncio.gather(*schema_load_tasks, return_exceptions=True)

        # Stop schema auto-refresh with timeout
        if _schema_cache is not None:
            try:
                await asyncio.wait_for(_schema_cache.stop_auto_refresh(), timeout=3.0)
                logger.info("Schema auto-refresh stopped")
            except TimeoutError:
                logger.warning("Schema auto-refresh stop timed out")
            except Exception as e:
                logger.warning(f"Error stopping schema auto-refresh: {e!s}")

        # Close database connection pools with timeout
        if _runtimes:
            try:
                # Use 5 second timeout for graceful shutdown
                await close_pools({name: rt.pool for name, rt in _runtimes.items()}, timeout=5.0)
                logger.info("Database connection pools closed")
            except Exception as e:
                logger.error(f"Error closing connection pools: {e!s}")

        logger.info("PostgreSQL MCP Server shutdown complete")


# Create FastMCP server instance with lifespan.
#
# Host-header note: FastMCP auto-enables DNS-rebinding protection only when
# constructed with host in ("127.0.0.1", "localhost", "::1") - the default
# host. The allowed_hosts list is then fixed to those loopback names, so any
# request whose Host header carries a real IP or hostname (e.g. a remote
# client hitting http://10.x.x.x:8000/mcp) is rejected with 421 before auth
# even runs. We bind 0.0.0.0 for remote access at the uvicorn layer, so we
# explicitly disable that protection here; remote abuse is handled by the
# bearer-token middleware in __main__ instead.
mcp = FastMCP(
    "pg-mcp",
    lifespan=lifespan,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
async def query(
    question: str,
    database: str | None = None,
    return_type: str = "result",
) -> dict[str, Any]:
    """Execute a natural language query against PostgreSQL database.

    This tool converts natural language questions into SQL queries and executes
    them against the specified PostgreSQL database. It includes comprehensive
    security validation, result verification, and error handling.

    Args:
        question: Natural language description of the query.
            Examples:
                - "How many users registered in the last 30 days?"
                - "Show me the top 10 products by revenue"
                - "What is the average order value by country?"

        database: Target database name (optional if only one database is configured).
            If not specified and only one database is available, it will be
            automatically selected.

        return_type: Type of result to return.
            Options:
                - "sql": Return only the generated SQL query without executing it
                - "result": Execute the query and return results (default)

    Returns:
        dict: Query response containing:
            - success (bool): Whether the query succeeded
            - generated_sql (str): The generated SQL query
            - data (dict): Query results if executed (columns, rows, row_count, etc.)
            - error (dict): Error information if query failed
            - confidence (int): Confidence score (0-100) for result quality
            - tokens_used (int): Number of LLM tokens consumed

    Examples:
        >>> # Get query results
        >>> result = await query(
        ...     question="How many active users are there?",
        ...     return_type="result"
        ... )
        >>> print(result["data"]["rows"])

        >>> # Get SQL only
        >>> result = await query(
        ...     question="Count all products",
        ...     return_type="sql"
        ... )
        >>> print(result["generated_sql"])

    Raises:
        This function does not raise exceptions. All errors are captured and
        returned in the response with success=False and error details.

    Security:
        - Only SELECT queries are allowed (no INSERT, UPDATE, DELETE, DROP, etc.)
        - Dangerous PostgreSQL functions are blocked (pg_sleep, file operations, etc.)
        - Query execution timeout is enforced
        - Row count limits prevent memory exhaustion
        - All queries run in read-only transactions
    """
    global _orchestrator

    if _orchestrator is None:
        return {
            "success": False,
            "error": {
                "code": "SERVER_NOT_INITIALIZED",
                "message": "Server not initialized properly",
                "details": None,
            },
        }

    # Validate return_type
    if return_type not in ("sql", "result"):
        return {
            "success": False,
            "error": {
                "code": "INVALID_PARAMETER",
                "message": f"Invalid return_type: '{return_type}'. Must be 'sql' or 'result'.",
                "details": {"return_type": return_type},
            },
        }

    # Build request
    try:
        request = QueryRequest(
            question=question,
            database=database,
            return_type=ReturnType(return_type),
        )
    except Exception as e:
        return {
            "success": False,
            "error": {
                "code": "INVALID_REQUEST",
                "message": f"Invalid request parameters: {e!s}",
                "details": {"error": str(e)},
            },
        }

    # Execute query through orchestrator. The tracing context assigns the
    # request id; the orchestrator reuses it for all downstream log lines.
    try:
        async with request_context() as request_id:
            logger.info(
                "Query tool invoked",
                extra={"request_id": request_id, "question": question[:100]},
            )
            response: QueryResponse = await _orchestrator.execute_query(request)
        # to_dict always includes tokens_used (0 when no LLM call succeeded)
        return response.to_dict()
    except Exception as e:
        logger.exception("Unexpected error in query tool")
        return {
            "success": False,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": f"Internal server error: {e!s}",
                "details": {"error_type": type(e).__name__},
            },
            "tokens_used": 0,
        }


if __name__ == "__main__":
    """Run the server when executed directly."""
    import anyio

    anyio.run(mcp.run_stdio_async)

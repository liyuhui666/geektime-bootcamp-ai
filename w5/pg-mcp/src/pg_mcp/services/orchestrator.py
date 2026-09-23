"""Query orchestrator for coordinating the complete query flow.

This module provides the QueryOrchestrator class that coordinates all components
of the query processing pipeline: SQL generation, validation, execution, and result
validation. It implements retry logic, error handling, and request tracking.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import ResilienceConfig, ValidationConfig
from pg_mcp.db.runtime import DatabaseRuntime
from pg_mcp.models.errors import (
    DatabaseError,
    ErrorCode,
    LLMError,
    LLMResponseError,
    PgMcpError,
    QuestionTooLongError,
    RateLimitExceededError,
    SchemaLoadError,
    SecurityViolationError,
    SQLParseError,
)
from pg_mcp.models.query import (
    ErrorDetail,
    QueryRequest,
    QueryResponse,
    QueryResult,
    ReturnType,
    ValidationResult,
)
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.tracing import get_request_id
from pg_mcp.resilience.backoff import backoff_delay
from pg_mcp.resilience.circuit_breaker import CircuitBreaker
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.result_validator import ResultValidator
from pg_mcp.services.sql_generator import SQLGenerator
from pg_mcp.services.sql_validator import SQLValidator

logger = logging.getLogger(__name__)


class QueryOrchestrator:
    """Orchestrates the complete query processing pipeline.

    This class coordinates SQL generation, validation, execution, and result
    validation. It implements retry logic with error feedback, circuit breaker
    pattern for fault tolerance, and comprehensive error handling.

    Per-database components (validator, executor, pool) come from the
    DatabaseRuntime the request is routed to, so validation and execution
    always run under the target database's effective policy.

    Example:
        >>> orchestrator = QueryOrchestrator(
        ...     runtimes=runtimes,
        ...     sql_generator=generator,
        ...     result_validator=result_validator,
        ...     schema_cache=cache,
        ...     resilience_config=resilience_config,
        ...     validation_config=validation_config,
        ... )
        >>> response = await orchestrator.execute_query(QueryRequest(
        ...     question="How many users?",
        ...     database="mydb"
        ... ))
    """

    def __init__(
        self,
        runtimes: dict[str, DatabaseRuntime],
        sql_generator: SQLGenerator,
        result_validator: ResultValidator,
        schema_cache: SchemaCache,
        resilience_config: ResilienceConfig,
        validation_config: ValidationConfig,
        rate_limiter: MultiRateLimiter | None = None,
        metrics: MetricsCollector | None = None,
        default_database: str | None = None,
    ) -> None:
        """Initialize query orchestrator.

        Args:
            runtimes: Per-database runtimes keyed by database name (pool,
                executor, validator, effective policy).
            sql_generator: SQL generation service.
            result_validator: Result validation service.
            schema_cache: Schema cache instance.
            resilience_config: Resilience configuration for retries, circuit
                breaker, and rate limiter timeouts.
            validation_config: Validation configuration including thresholds.
            rate_limiter: Optional concurrency limiter. When provided, query
                slots cover the whole pipeline and LLM slots cover the two
                LLM call sites; None disables limiting (tests, embedding).
            metrics: Optional metrics collector (None-safe for tests).
            default_database: Database used when a request does not specify
                one and multiple databases are configured.
        """
        self.runtimes = runtimes
        self.sql_generator = sql_generator
        self.result_validator = result_validator
        self.schema_cache = schema_cache
        self.resilience_config = resilience_config
        self.validation_config = validation_config
        self.rate_limiter = rate_limiter
        self.metrics = metrics
        self._default_database = default_database
        self._acquire_timeout = resilience_config.rate_limit_acquire_timeout

        # Create circuit breaker for LLM calls
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=resilience_config.circuit_breaker_threshold,
            recovery_timeout=resilience_config.circuit_breaker_timeout,
        )

    def _sync_breaker_metric(self) -> None:
        """Publish the current circuit breaker state to the gauge (if wired)."""
        if self.metrics is not None:
            self.metrics.set_circuit_breaker_state(str(self.circuit_breaker.state))

    def _error_response(
        self,
        code: ErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> QueryResponse:
        """Build a failed QueryResponse with the given error code."""
        return QueryResponse(
            success=False,
            generated_sql=None,
            validation=None,
            data=None,
            error=ErrorDetail(code=code.value, message=message, details=details),
            confidence=0,
            tokens_used=None,
        )

    async def execute_query(self, request: QueryRequest) -> QueryResponse:
        """Execute complete query flow, guarded by the query rate limiter.

        The limiter uses the narrow acquire() bool contract rather than a
        broad ``except TimeoutError``: the wrapped pipeline itself raises
        TimeoutError internally (asyncio.wait_for, executor), which a broad
        except would misreport as rate limiting.

        Args:
            request: Query request containing question and parameters.

        Returns:
            QueryResponse: Complete response with SQL, results, or error information.
        """
        limiter = self.rate_limiter
        if limiter is None:
            return await self._execute_query_impl(request)

        if not await limiter.query_limiter.acquire(timeout=self._acquire_timeout):
            if self.metrics is not None:
                self.metrics.increment_query_request("rate_limit_exceeded", request.database or "-")
            return self._error_response(
                code=ErrorCode.RATE_LIMIT_EXCEEDED,
                message="Too many concurrent queries, please retry later",
                details={"retry_after_seconds": self._acquire_timeout, "limiter": "queries"},
            )

        try:
            if self.metrics is not None:
                self.metrics.set_rate_limiter_active("queries", limiter.query_limiter.active_count)
            return await self._execute_query_impl(request)
        finally:
            limiter.query_limiter.release()
            if self.metrics is not None:
                self.metrics.set_rate_limiter_active("queries", limiter.query_limiter.active_count)

    async def _execute_query_impl(self, request: QueryRequest) -> QueryResponse:
        """Run the full pipeline (schema → generate → validate → execute).

        This method orchestrates the entire pipeline:
        1. Generate request_id for tracking
        2. Resolve and validate database name
        3. Load schema from cache
        4. Generate and validate SQL with retry logic
        5. Execute SQL (if return_type == RESULT)
        6. Validate results (optional)
        7. Return structured response

        Emits query_requests (per exit status/database) and query_duration.

        Args:
            request: Query request containing question and parameters.

        Returns:
            QueryResponse: Complete response with SQL, results, or error information.
        """
        # Reuse the upstream request id from the tracing context when present
        request_id = get_request_id() or str(uuid.uuid4())
        logger.info(
            "Starting query execution",
            extra={"request_id": request_id, "question": request.question[:100]},
        )

        status = ErrorCode.INTERNAL_ERROR.value
        database = request.database or "-"
        started = time.perf_counter()

        try:
            # Step 0: Enforce configured question length. QueryRequest's own
            # max_length covers the model-level bound; this honors a lower
            # VALIDATION_MAX_QUESTION_LENGTH override from settings.
            if len(request.question) > self.validation_config.max_question_length:
                raise QuestionTooLongError(
                    message=(
                        f"Question length {len(request.question)} exceeds maximum of "
                        f"{self.validation_config.max_question_length} characters"
                    ),
                    details={
                        "length": len(request.question),
                        "max_length": self.validation_config.max_question_length,
                    },
                )

            # Step 1: Resolve database name and its runtime
            database_name = self._resolve_database(request.database)
            database = database_name
            runtime = self.runtimes[database_name]
            logger.debug(
                "Resolved database",
                extra={"request_id": request_id, "database": database_name},
            )

            # Step 2: Get schema from cache
            schema = self.schema_cache.get(database_name)
            if schema is None:
                # Schema not in cache, load it via the runtime's pool
                try:
                    schema = await self.schema_cache.load(database_name, runtime.pool)
                except Exception as e:
                    raise SchemaLoadError(
                        message=f"Failed to load schema for database '{database_name}': {e!s}",
                        details={"database": database_name, "error": str(e)},
                    ) from e

            logger.debug(
                "Schema loaded",
                extra={
                    "request_id": request_id,
                    "database": database_name,
                    "tables": len(schema.tables),
                },
            )

            # Step 3: Generate and validate SQL with retry logic
            # (validation runs under this database's policy)
            generated_sql, validation_result, tokens_used = await self._generate_sql_with_retry(
                question=request.question,
                schema=schema,
                request_id=request_id,
                validator=runtime.validator,
            )
            # Step 4: If return_type is SQL, return early
            if request.return_type == ReturnType.SQL:
                logger.info(
                    "Returning SQL only",
                    extra={"request_id": request_id, "sql_length": len(generated_sql)},
                )
                status = ErrorCode.SUCCESS.value
                return QueryResponse(
                    success=True,
                    generated_sql=generated_sql,
                    validation=validation_result,
                    data=None,
                    error=None,
                    confidence=100,
                    tokens_used=tokens_used,
                )

            # Step 5: Execute SQL (under this database's policy)
            logger.debug("Executing SQL", extra={"request_id": request_id})
            start_time = self._get_current_time_ms()

            results, total_count = await runtime.executor.execute(generated_sql)

            execution_time_ms = self._get_current_time_ms() - start_time
            logger.info(
                "SQL executed successfully",
                extra={
                    "request_id": request_id,
                    "row_count": total_count,
                    "execution_time_ms": execution_time_ms,
                },
            )

            # Step 6: Validate results (non-blocking, failures don't fail the request)
            result_confidence = await self._validate_results_safely(
                question=request.question,
                sql=generated_sql,
                results=results,
                row_count=total_count,
                request_id=request_id,
            )

            # Step 7: Build successful response
            query_result = QueryResult(
                columns=list(results[0].keys()) if results else [],
                rows=results,
                row_count=len(results),  # Limited row count (after max_rows applied)
                execution_time_ms=execution_time_ms,
            )

            return QueryResponse(
                success=True,
                generated_sql=generated_sql,
                validation=validation_result,
                data=query_result,
                error=None,
                confidence=result_confidence,
                tokens_used=tokens_used,
            )

        except PgMcpError as e:
            # Handle known application errors
            status = e.code.value
            logger.warning(
                "Query execution failed with known error",
                extra={
                    "request_id": request_id,
                    "error_code": e.code,
                    "error_message": str(e),
                },
            )
            return self._error_response(code=e.code, message=e.message, details=e.details)
        except Exception as e:
            # Handle unexpected errors
            status = ErrorCode.INTERNAL_ERROR.value
            logger.exception(
                "Query execution failed with unexpected error",
                extra={"request_id": request_id},
            )
            return self._error_response(
                code=ErrorCode.INTERNAL_ERROR,
                message=f"Internal server error: {e!s}",
                details={"error_type": type(e).__name__},
            )
        finally:
            # Emit per-exit request metrics: status is the error code (or
            # "success"); labels never contain question/SQL text (PII).
            if self.metrics is not None:
                self.metrics.increment_query_request(status, database)
                self.metrics.query_duration.observe(time.perf_counter() - started)

    def _resolve_database(self, database: str | None) -> str:
        """Resolve database name from request, default, or auto-select.

        Resolution order: explicit request.database (must exist) ->
        auto-select when exactly one database is configured ->
        configured default_database -> error listing available databases.

        Args:
            database: Database name from request (optional).

        Returns:
            str: Resolved database name.

        Raises:
            DatabaseError: If database is invalid or cannot be resolved.

        Example:
            >>> name = orchestrator._resolve_database("mydb")  # Validates "mydb" exists
            >>> name = orchestrator._resolve_database(None)  # Auto-selects if only one DB
        """
        if database is not None:
            # Validate specified database exists
            if database not in self.runtimes:
                raise DatabaseError(
                    message=f"Database '{database}' not found",
                    details={
                        "requested_database": database,
                        "available_databases": list(self.runtimes.keys()),
                    },
                )
            return database

        # Auto-select if only one database available
        available_dbs = list(self.runtimes.keys())
        if len(available_dbs) == 0:
            raise DatabaseError(
                message="No databases configured",
                details={},
            )
        if len(available_dbs) == 1:
            return available_dbs[0]

        # Multiple databases: fall back to the configured default, else reject
        if self._default_database and self._default_database in self.runtimes:
            return self._default_database

        raise DatabaseError(
            message="Multiple databases available, please specify which to query",
            details={"available_databases": available_dbs},
        )

    @asynccontextmanager
    async def _llm_slot(self) -> AsyncIterator[None]:
        """Acquire an LLM concurrency slot.

        Slot timeout raises RateLimitExceededError (a PgMcpError) so the top
        level maps it to a rate_limit_exceeded response. Using for_llm()
        directly would leak a raw TimeoutError that the generation loop's
        ``except LLMError`` cannot catch.

        Yields:
            None while holding the slot.
        """
        if self.rate_limiter is None:
            yield
            return

        if not await self.rate_limiter.llm_limiter.acquire(timeout=self._acquire_timeout):
            raise RateLimitExceededError(
                message="LLM concurrency limit exceeded",
                details={"retry_after_seconds": self._acquire_timeout, "limiter": "llm"},
            )
        try:
            if self.metrics is not None:
                self.metrics.set_rate_limiter_active(
                    "llm", self.rate_limiter.llm_limiter.active_count
                )
            yield
        finally:
            self.rate_limiter.llm_limiter.release()
            if self.metrics is not None:
                self.metrics.set_rate_limiter_active(
                    "llm", self.rate_limiter.llm_limiter.active_count
                )

    async def _generate_sql_with_retry(
        self,
        question: str,
        schema: Any,
        request_id: str,
        validator: SQLValidator,
    ) -> tuple[str, ValidationResult, int]:
        """Generate and validate SQL with retry logic.

        Both failure classes share one retry budget (max_retries), so a run
        never exceeds max_retries + 1 LLM calls:

        - Transient API errors (timeout / network / rate-limit 429) retry
          after exponential backoff (`backoff_delay`).
        - Content-level failures (LLMResponseError: empty/unparseable
          response) retry through the feedback channel — at temperature 0 a
          blind retry would reproduce the same output.
        - Auth failures (LLMUnavailableError with retryable=False) fail fast.

        Args:
            question: User's natural language question.
            schema: Database schema for context.
            request_id: Request ID for tracking.
            validator: The target database's SQL validator (its effective
                policy decides what SQL is acceptable).

        Returns:
            tuple: (generated_sql, validation_result, tokens_used) where
                tokens_used aggregates usage across all generation attempts.

        Raises:
            RateLimitExceededError: If no LLM slot became available (does not
                consume the retry budget).
            LLMError: If circuit breaker is open or generation fails finally.
            SecurityViolationError: If SQL fails validation after all retries.
            SQLParseError: If SQL cannot be parsed.
        """
        # Check circuit breaker
        if not self.circuit_breaker.allow_request():
            self._sync_breaker_metric()
            raise LLMError(
                message="SQL generation service is temporarily unavailable (circuit breaker open)",
                details={
                    "circuit_state": self.circuit_breaker.state,
                    "failure_count": self.circuit_breaker.failure_count,
                },
            )

        previous_sql: str | None = None
        error_feedback: str | None = None
        max_retries = self.resilience_config.max_retries
        retry_delay = self.resilience_config.retry_delay
        backoff_factor = self.resilience_config.backoff_factor
        tokens_used = 0

        def _backoff(attempt: int) -> float:
            return backoff_delay(retry_delay, backoff_factor, attempt)

        for attempt in range(max_retries + 1):
            call_started = time.perf_counter()
            try:
                logger.debug(
                    "Generating SQL",
                    extra={
                        "request_id": request_id,
                        "attempt": attempt + 1,
                        "max_retries": max_retries + 1,
                    },
                )

                async with self._llm_slot():
                    result = await self.sql_generator.generate(
                        question=question,
                        schema=schema,
                        previous_attempt=previous_sql,
                        error_feedback=error_feedback,
                    )

                if self.metrics is not None:
                    self.metrics.increment_llm_call("generation", "success")
                    self.metrics.observe_sql_generation_duration(
                        attempt, time.perf_counter() - call_started
                    )
                    self.metrics.increment_llm_tokens("generation", result.tokens_used)

            except RateLimitExceededError:
                # Top level maps this to a rate_limit_exceeded response;
                # do not consume the retry budget.
                raise
            except LLMResponseError as e:
                if self.metrics is not None:
                    self.metrics.increment_llm_call("generation", "error")
                    self.metrics.observe_sql_generation_duration(
                        attempt, time.perf_counter() - call_started
                    )
                if attempt < max_retries:
                    # Content-level failure: route through feedback channel.
                    logger.warning(
                        "LLM response unparseable, retrying with feedback",
                        extra={"request_id": request_id, "attempt": attempt + 1, "error": str(e)},
                    )
                    previous_sql, error_feedback = None, f"Response unparseable: {e.message}"
                    await asyncio.sleep(_backoff(attempt))
                    continue
                self.circuit_breaker.record_failure()
                self._sync_breaker_metric()
                raise
            except LLMError as e:
                if self.metrics is not None:
                    self.metrics.increment_llm_call("generation", "error")
                    self.metrics.observe_sql_generation_duration(
                        attempt, time.perf_counter() - call_started
                    )
                # LLMUnavailableError carries retryable=False for auth-type
                # failures; plain LLMError/timeout are transient by default.
                retryable = getattr(e, "retryable", True)
                if not retryable or attempt >= max_retries:
                    self.circuit_breaker.record_failure()
                    self._sync_breaker_metric()
                    logger.error(
                        "SQL generation failed (no further retry)",
                        extra={"request_id": request_id, "attempt": attempt + 1, "error": str(e)},
                    )
                    raise
                logger.warning(
                    "Transient LLM error, retrying with backoff",
                    extra={"request_id": request_id, "attempt": attempt + 1, "error": str(e)},
                )
                await asyncio.sleep(_backoff(attempt))
                continue
            except Exception as e:
                # Unexpected error during generation
                self.circuit_breaker.record_failure()
                self._sync_breaker_metric()
                logger.exception(
                    "Unexpected error during SQL generation",
                    extra={"request_id": request_id},
                )
                raise LLMError(
                    message=f"SQL generation failed unexpectedly: {e!s}",
                    details={"error_type": type(e).__name__},
                ) from e

            generated_sql = result.sql
            tokens_used += result.tokens_used

            logger.debug(
                "SQL generated",
                extra={
                    "request_id": request_id,
                    "sql_length": len(generated_sql),
                },
            )

            # Validate SQL
            try:
                validator.validate_or_raise(generated_sql)
            except (SecurityViolationError, SQLParseError) as validation_error:
                if self.metrics is not None:
                    reason = (
                        "security"
                        if isinstance(validation_error, SecurityViolationError)
                        else "parse"
                    )
                    self.metrics.increment_sql_validation_failure(reason)
                if attempt < max_retries:
                    # Retry with feedback (shared retry budget)
                    logger.warning(
                        "SQL validation failed, retrying with feedback",
                        extra={
                            "request_id": request_id,
                            "attempt": attempt + 1,
                            "error": str(validation_error),
                        },
                    )
                    previous_sql = generated_sql
                    error_feedback = str(validation_error)
                    await asyncio.sleep(_backoff(attempt))
                    continue
                # Out of retries, record failure and raise
                self.circuit_breaker.record_failure()
                self._sync_breaker_metric()
                logger.error(
                    "SQL validation failed after all retries",
                    extra={
                        "request_id": request_id,
                        "attempts": attempt + 1,
                        "error": str(validation_error),
                    },
                )
                raise

            # Validation successful
            self.circuit_breaker.record_success()
            self._sync_breaker_metric()
            logger.info(
                "SQL generated and validated successfully",
                extra={
                    "request_id": request_id,
                    "attempts": attempt + 1,
                    "tokens_used": tokens_used,
                },
            )

            # Build validation result from the validator so the response
            # reflects what was actually checked (blocked functions, etc.)
            validation_result = validator.validate_detail(generated_sql)

            return generated_sql, validation_result, tokens_used

        # Should not reach here, but just in case
        self.circuit_breaker.record_failure()
        self._sync_breaker_metric()
        raise LLMError(
            message="SQL generation failed after all retry attempts",
            details={"max_retries": max_retries},
        )

    async def _validate_results_safely(
        self,
        question: str,
        sql: str,
        results: list[dict[str, Any]],
        row_count: int,
        request_id: str,
    ) -> int:
        """Validate query results with error handling (non-blocking).

        This method attempts to validate results using LLM, but failures
        don't cause the overall query to fail. Returns a confidence score.

        Args:
            question: User's original question.
            sql: Generated SQL query.
            results: Query results.
            row_count: Total row count.
            request_id: Request ID for tracking.

        Returns:
            int: Confidence score (0-100). Returns 100 if validation disabled/fails.

        Example:
            >>> confidence = await orchestrator._validate_results_safely(
            ...     question="Count users",
            ...     sql="SELECT COUNT(*) FROM users",
            ...     results=[{"count": 42}],
            ...     row_count=1,
            ...     request_id="123",
            ... )
        """
        if not self.validation_config.enabled:
            return 100

        try:
            logger.debug(
                "Validating results",
                extra={"request_id": request_id},
            )

            call_started = time.perf_counter()
            async with self._llm_slot():
                validation_result = await self.result_validator.validate(
                    question=question,
                    sql=sql,
                    results=results,
                    row_count=row_count,
                )

            if self.metrics is not None:
                self.metrics.increment_llm_call("validation", "success")
                self.metrics.llm_latency.labels(operation="validation").observe(
                    time.perf_counter() - call_started
                )
                self.metrics.increment_llm_tokens("validation", validation_result.tokens_used)

            logger.info(
                "Result validation completed",
                extra={
                    "request_id": request_id,
                    "confidence": validation_result.confidence,
                    "is_acceptable": validation_result.is_acceptable,
                },
            )

            return validation_result.confidence

        except RateLimitExceededError:
            # Concurrency limit: treat like any other non-blocking validation
            # failure — the query result itself is unaffected.
            logger.warning(
                "Result validation skipped: LLM concurrency limit",
                extra={"request_id": request_id},
            )
            return 100
        except Exception as e:
            # Log but don't fail the query
            logger.warning(
                "Result validation failed, continuing with default confidence",
                extra={
                    "request_id": request_id,
                    "error": str(e),
                },
            )
            return 100  # Default to high confidence if validation fails

    @staticmethod
    def _get_current_time_ms() -> float:
        """Get monotonic timestamp in milliseconds.

        Uses time.perf_counter instead of time.time so duration measurements
        are immune to system clock adjustments (NTP resyncs, DST, manual changes).
        The absolute value is meaningless; only differences are consumed.

        Returns:
            float: Monotonic timestamp in milliseconds.
        """
        return time.perf_counter() * 1000

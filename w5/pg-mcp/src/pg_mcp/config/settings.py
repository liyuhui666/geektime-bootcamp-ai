"""Configuration management for PostgreSQL MCP Server.

This module defines all configuration settings using Pydantic for validation
and type safety. Configuration is loaded from environment variables with
sensible defaults.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class DatabaseConfig(BaseSettings):
    """PostgreSQL database connection configuration."""

    model_config = SettingsConfigDict(env_prefix="DATABASE_")

    host: str = Field(default="localhost", description="Database host")
    port: int = Field(default=5432, ge=1, le=65535, description="Database port")
    name: str = Field(default="postgres", description="Database name")
    user: str = Field(default="postgres", description="Database user")
    password: str = Field(default="", description="Database password")

    # Connection pool settings
    min_pool_size: int = Field(default=5, ge=1, le=100, description="Minimum pool size")
    max_pool_size: int = Field(default=20, ge=1, le=100, description="Maximum pool size")
    pool_timeout: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Pool acquire timeout in seconds"
    )
    command_timeout: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Command execution timeout in seconds"
    )

    @property
    def dsn(self) -> str:
        """Build PostgreSQL DSN connection string."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"

    @property
    def safe_dsn(self) -> str:
        """Build DSN with masked password for logging."""
        return f"postgresql://{self.user}:***@{self.host}:{self.port}/{self.name}"


class OpenAIConfig(BaseSettings):
    """OpenAI API configuration."""

    model_config = SettingsConfigDict(env_prefix="OPENAI_")

    api_key: SecretStr = Field(default=SecretStr(""), description="OpenAI API key")
    base_url: str | None = Field(
        default=None,
        description="Optional OpenAI-compatible API base URL (e.g. internal LLM gateway)",
    )
    model: str = Field(default="gpt-4o-mini", description="Model to use for SQL generation")
    max_tokens: int = Field(
        default=2000, ge=100, le=32768, description="Maximum tokens in response"
    )
    temperature: float = Field(
        default=0.0, ge=0.0, le=2.0, description="Temperature for response randomness"
    )
    timeout: float = Field(
        default=30.0, ge=5.0, le=120.0, description="API request timeout in seconds"
    )

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, v: SecretStr) -> SecretStr:
        """Validate API key is not empty and has correct format."""
        api_key_str = v.get_secret_value()
        if not api_key_str or not api_key_str.strip():
            raise ValueError("OpenAI API key must not be empty")
        if not api_key_str.startswith("sk-"):
            raise ValueError("OpenAI API key must start with 'sk-'")
        return v


class SecurityConfig(BaseSettings):
    """Security and access control configuration.

    Note: this server is read-only by hard constraint (see SQLValidator).
    There is deliberately no "allow write operations" switch.
    """

    model_config = SettingsConfigDict(env_prefix="SECURITY_")

    # NoDecode: pydantic-settings would otherwise JSON-decode list-typed env
    # values, so the documented comma-separated format in .env.example would
    # crash startup. The before-validator below handles both formats.
    blocked_functions: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "pg_sleep",
            "pg_read_file",
            "pg_write_file",
            "lo_import",
            "lo_export",
        ],
        description="List of blocked PostgreSQL functions",
    )
    blocked_tables: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description='Blocked table names; bare ("internal") or schema-qualified ("audit.logs")',
    )
    blocked_columns: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description='Blocked column names; bare ("password") or table-qualified ("users.password")',
    )
    allow_explain: bool = Field(
        default=False, description="Allow EXPLAIN statements (plan only, no execution)"
    )
    allow_explain_analyze: bool = Field(
        default=False,
        description="Allow EXPLAIN ANALYZE (actually executes the statement; keep off by default)",
    )
    block_system_catalogs: bool = Field(
        default=False,
        description=(
            "Reject pg_catalog/information_schema references. Off by default: the "
            "README example queries rely on information_schema metadata reads."
        ),
    )
    max_rows: int = Field(default=10000, ge=1, le=100000, description="Maximum rows to return")
    max_execution_time: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Maximum query execution time in seconds"
    )
    readonly_role: str | None = Field(
        default=None, description="PostgreSQL role to switch to for read-only access"
    )
    safe_search_path: str = Field(
        default="public", description="Safe search_path to set during query execution"
    )

    @field_validator("blocked_functions", "blocked_tables", "blocked_columns", mode="before")
    @classmethod
    def parse_string_list(cls, v: str | list[str]) -> list[str]:
        """Parse comma-separated string or list."""
        if isinstance(v, str):
            return [f.strip() for f in v.split(",") if f.strip()]
        return v


class ValidationConfig(BaseSettings):
    """Query validation configuration."""

    model_config = SettingsConfigDict(env_prefix="VALIDATION_")

    max_question_length: int = Field(
        default=10000, ge=1, le=50000, description="Maximum question length in characters"
    )

    # Result validation settings
    enabled: bool = Field(default=True, description="Enable result validation using LLM")
    sample_rows: int = Field(
        default=5, ge=1, le=100, description="Number of sample rows to include in validation"
    )
    timeout_seconds: float = Field(
        default=10.0, ge=1.0, le=60.0, description="Result validation timeout in seconds"
    )
    confidence_threshold: int = Field(
        default=70, ge=0, le=100, description="Minimum confidence for acceptable results"
    )


class CacheConfig(BaseSettings):
    """Schema cache configuration."""

    model_config = SettingsConfigDict(env_prefix="CACHE_")

    schema_ttl: int = Field(
        default=3600, ge=60, le=86400, description="Schema cache TTL in seconds"
    )
    max_size: int = Field(default=100, ge=1, le=1000, description="Maximum cache entries")
    enabled: bool = Field(default=True, description="Enable schema caching")


class ResilienceConfig(BaseSettings):
    """Resilience and fault tolerance configuration."""

    model_config = SettingsConfigDict(env_prefix="RESILIENCE_")

    max_retries: int = Field(default=3, ge=0, le=10, description="Maximum retry attempts")
    retry_delay: float = Field(
        default=1.0, ge=0.1, le=10.0, description="Initial retry delay in seconds"
    )
    backoff_factor: float = Field(
        default=2.0, ge=1.0, le=10.0, description="Exponential backoff factor"
    )
    circuit_breaker_threshold: int = Field(
        default=5, ge=1, le=100, description="Failures before circuit opens"
    )
    circuit_breaker_timeout: float = Field(
        default=60.0, ge=10.0, le=300.0, description="Circuit breaker timeout in seconds"
    )

    # Concurrency rate limiting (wired into the orchestrator request path)
    rate_limit_query: int = Field(
        default=10, ge=1, le=1000, description="Maximum concurrent queries"
    )
    rate_limit_llm: int = Field(
        default=5, ge=1, le=1000, description="Maximum concurrent LLM API calls"
    )
    rate_limit_acquire_timeout: float = Field(
        default=5.0,
        ge=0.1,
        le=60.0,
        description="Seconds to wait for a rate limiter slot before rejecting",
    )


class ObservabilityConfig(BaseSettings):
    """Observability and monitoring configuration."""

    model_config = SettingsConfigDict(env_prefix="OBSERVABILITY_")

    metrics_enabled: bool = Field(default=True, description="Enable Prometheus metrics")
    metrics_port: int = Field(
        default=9090, ge=1024, le=65535, description="Metrics HTTP server port"
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description="Logging level"
    )
    log_format: Literal["json", "text"] = Field(default="json", description="Log format")


class MultiDatabaseConfig(BaseSettings):
    """Multi-database mode configuration (env prefix MULTIDB_)."""

    model_config = SettingsConfigDict(env_prefix="MULTIDB_")

    databases_json: str | None = Field(
        default=None,
        description=(
            "JSON array of database entries, e.g. "
            '[{"connection":{"name":"db1","host":"localhost"}},'
            '{"connection":{"name":"db2"},"security":{"blocked_tables":["secrets"]}}]. '
            "When set, DATABASE_* single-database variables are ignored."
        ),
    )
    default_database: str | None = Field(
        default=None,
        description=(
            "Database used when a request does not specify one. In single-database "
            "mode it is auto-selected; in multi-database mode without this setting "
            "an unspecified request is rejected."
        ),
    )


class DatabaseSecurityOverride(BaseModel):
    """Per-database security policy overrides; None fields fall back to global.

    See EffectivePolicy.merge for the exact merge semantics.
    """

    blocked_tables: list[str] | None = None
    blocked_columns: list[str] | None = None
    allow_explain: bool | None = None
    allow_explain_analyze: bool | None = None
    readonly_role: str | None = None
    safe_search_path: str | None = None


class DatabaseConnectionParams(BaseModel):
    """Connection parameters for one database entry.

    Deliberately a plain BaseModel, NOT a BaseSettings subclass: nested
    BaseSettings fields silently fall back to reading DATABASE_* environment
    variables when a JSON entry omits a field, which would mix the two
    configuration modes. JSON entries must be self-contained; missing fields
    take the same defaults as DatabaseConfig.
    """

    host: str = Field(default="localhost", description="Database host")
    port: int = Field(default=5432, ge=1, le=65535, description="Database port")
    # Required (no default): a JSON entry without a name must fail validation,
    # not silently key the runtime as "".
    name: str = Field(min_length=1, description="Database name")
    user: str = Field(default="postgres", description="Database user")
    password: str = Field(default="", description="Database password")
    min_pool_size: int = Field(default=5, ge=1, le=100, description="Minimum pool size")
    max_pool_size: int = Field(default=20, ge=1, le=100, description="Maximum pool size")
    pool_timeout: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Pool acquire timeout in seconds"
    )
    command_timeout: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Command execution timeout in seconds"
    )

    @property
    def dsn(self) -> str:
        """Build PostgreSQL DSN connection string."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"

    @property
    def safe_dsn(self) -> str:
        """Build DSN with masked password for logging."""
        return f"postgresql://{self.user}:***@{self.host}:{self.port}/{self.name}"


class DatabaseEntry(BaseModel):
    """One DATABASES_JSON array element: connection config + optional policy override."""

    connection: DatabaseConnectionParams
    security: DatabaseSecurityOverride | None = None


class Settings(BaseSettings):
    """Main application settings aggregating all config sections."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["development", "staging", "production"] = Field(
        default="development", description="Application environment"
    )

    # Nested configurations
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    resilience: ResilienceConfig = Field(default_factory=ResilienceConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    multidb: MultiDatabaseConfig = Field(default_factory=MultiDatabaseConfig)

    # Resolved database entries, populated by model_post_init (fail-fast).
    databases: list[DatabaseEntry] = Field(default_factory=list, exclude=True)

    def model_post_init(self, __context: Any) -> None:
        """Resolve the database entry list; any inconsistency aborts startup.

        Rules (design §4.1):
        1. MULTIDB_DATABASES_JSON unset -> single-database mode: the DATABASE_*
           config becomes the sole entry; behavior identical to v0.2.
        2. Set -> parse the JSON array; duplicate database names are an error;
           DATABASE_* variables are ignored (warning logged).
        3. MULTIDB_DEFAULT_DATABASE must name one of the entries.
        """
        import json as _json
        import logging as _logging

        _logger = _logging.getLogger(__name__)

        raw = self.multidb.databases_json
        if raw is None or not raw.strip():
            self.databases = [
                DatabaseEntry(
                    connection=DatabaseConnectionParams(
                        host=self.database.host,
                        port=self.database.port,
                        name=self.database.name,
                        user=self.database.user,
                        password=self.database.password,
                        min_pool_size=self.database.min_pool_size,
                        max_pool_size=self.database.max_pool_size,
                        pool_timeout=self.database.pool_timeout,
                        command_timeout=self.database.command_timeout,
                    )
                )
            ]
            if self.multidb.default_database is not None and (
                self.multidb.default_database != self.database.name
            ):
                raise ValueError(
                    f"MULTIDB_DEFAULT_DATABASE '{self.multidb.default_database}' does not "
                    f"match the single configured database '{self.database.name}'"
                )
            return

        try:
            parsed = _json.loads(raw)
        except _json.JSONDecodeError as e:
            raise ValueError(f"MULTIDB_DATABASES_JSON is not valid JSON: {e}") from e
        if not isinstance(parsed, list) or not parsed:
            raise ValueError("MULTIDB_DATABASES_JSON must be a non-empty JSON array")

        try:
            self.databases = [DatabaseEntry.model_validate(item) for item in parsed]
        except ValidationError as e:
            raise ValueError(f"Invalid MULTIDB_DATABASES_JSON entry: {e}") from e

        names = [entry.connection.name for entry in self.databases]
        if duplicates := {n for n in names if names.count(n) > 1}:
            raise ValueError(
                f"Duplicate database names in MULTIDB_DATABASES_JSON: {sorted(duplicates)}"
            )

        _logger.warning(
            "MULTIDB_DATABASES_JSON is set; DATABASE_* single-database variables are ignored"
        )

        if self.multidb.default_database is not None and self.multidb.default_database not in names:
            raise ValueError(
                f"MULTIDB_DEFAULT_DATABASE '{self.multidb.default_database}' is not one of "
                f"the configured databases: {names}"
            )

    @property
    def is_production(self) -> bool:
        """Check if running in production environment."""
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        """Check if running in development environment."""
        return self.environment == "development"


# Global settings instance
_settings: Settings | None = None


def get_settings() -> Settings:
    """Get or create global settings instance.

    Returns:
        Settings: The global settings instance.
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Reset global settings instance. Useful for testing."""
    global _settings
    _settings = None

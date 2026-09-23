"""Effective security policy: global SecurityConfig merged with per-database overrides.

Each database can tighten (or relax, via explicit configuration) parts of the
global security posture. The final policy a request is validated against is an
EffectivePolicy — the immutable result of merging the global SecurityConfig
with one database's DatabaseSecurityOverride.
"""

from dataclasses import dataclass

from pg_mcp.config.settings import DatabaseSecurityOverride, SecurityConfig


@dataclass(frozen=True)
class EffectivePolicy:
    """Global SecurityConfig merged with per-database overrides.

    Attributes:
        blocked_functions: Lowercase function names rejected during validation.
        blocked_tables: Lowercase table names; bare names ("internal") or
            schema-qualified ("audit.logs").
        blocked_columns: Lowercase column names; bare ("password") or
            table-qualified ("users.password").
        allow_explain: Whether plain EXPLAIN is permitted.
        allow_explain_analyze: Whether EXPLAIN ANALYZE (which actually runs
            the statement) is permitted.
        block_system_catalogs: Whether pg_catalog/information_schema references
            are rejected.
        readonly_role: Role to SET before executing (None = no switch).
        safe_search_path: search_path set on the execution session.
        max_rows: Row cap applied by the executor.
        max_execution_time: Statement timeout in seconds.
    """

    blocked_functions: frozenset[str]
    blocked_tables: frozenset[str]
    blocked_columns: frozenset[str]
    allow_explain: bool
    allow_explain_analyze: bool
    block_system_catalogs: bool
    readonly_role: str | None
    safe_search_path: str
    max_rows: int
    max_execution_time: float

    @classmethod
    def merge(
        cls,
        base: SecurityConfig,
        override: DatabaseSecurityOverride | None,
    ) -> "EffectivePolicy":
        """Merge a global SecurityConfig with an optional per-database override.

        None fields on the override fall back to the global value; set fields
        replace it. blocked_functions, max_rows and max_execution_time are
        global-only (not overridable per database).

        Args:
            base: Global security configuration.
            override: Per-database overrides, or None for pure global policy.

        Returns:
            EffectivePolicy: The immutable merged policy.
        """
        if override is None:
            override = DatabaseSecurityOverride()

        tables = (
            override.blocked_tables if override.blocked_tables is not None else base.blocked_tables
        )
        columns = (
            override.blocked_columns
            if override.blocked_columns is not None
            else base.blocked_columns
        )

        return cls(
            blocked_functions=frozenset(f.lower() for f in base.blocked_functions),
            blocked_tables=frozenset(t.lower() for t in tables),
            blocked_columns=frozenset(c.lower() for c in columns),
            allow_explain=(
                override.allow_explain if override.allow_explain is not None else base.allow_explain
            ),
            allow_explain_analyze=(
                override.allow_explain_analyze
                if override.allow_explain_analyze is not None
                else base.allow_explain_analyze
            ),
            block_system_catalogs=base.block_system_catalogs,
            readonly_role=(
                override.readonly_role if override.readonly_role is not None else base.readonly_role
            ),
            safe_search_path=(
                override.safe_search_path
                if override.safe_search_path is not None
                else base.safe_search_path
            ),
            max_rows=base.max_rows,
            max_execution_time=base.max_execution_time,
        )

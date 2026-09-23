"""Unit tests for EffectivePolicy merge semantics (P3 / §4.2)."""

import dataclasses

import pytest

from pg_mcp.config.policy import EffectivePolicy
from pg_mcp.config.settings import DatabaseSecurityOverride, SecurityConfig


class TestEffectivePolicyMerge:
    """Test global SecurityConfig + per-database override merging."""

    def test_none_override_yields_pure_global_policy(self) -> None:
        """merge with None override reproduces the global SecurityConfig."""
        base = SecurityConfig(
            blocked_functions=["pg_sleep", "lo_import"],
            blocked_tables=["secrets", "Audit.Logs"],
            blocked_columns=["Password"],
            allow_explain=True,
            max_rows=500,
            max_execution_time=15.0,
            readonly_role="reader",
            safe_search_path="app,public",
        )
        policy = EffectivePolicy.merge(base, None)

        assert policy.blocked_functions == frozenset({"pg_sleep", "lo_import"})
        assert policy.blocked_tables == frozenset({"secrets", "audit.logs"})
        assert policy.blocked_columns == frozenset({"password"})
        assert policy.allow_explain is True
        assert policy.allow_explain_analyze is False
        assert policy.block_system_catalogs is False
        assert policy.readonly_role == "reader"
        assert policy.safe_search_path == "app,public"
        assert policy.max_rows == 500
        assert policy.max_execution_time == 15.0

    def test_empty_override_falls_back_to_global(self) -> None:
        """An override with all-None fields behaves like no override."""
        base = SecurityConfig(allow_explain=True)
        policy = EffectivePolicy.merge(base, DatabaseSecurityOverride())

        assert policy.blocked_tables == frozenset()
        assert policy.allow_explain is True
        assert policy.readonly_role is None

    def test_override_replaces_set_fields(self) -> None:
        """Set override fields replace the global value."""
        base = SecurityConfig(blocked_tables=["global_secret"])
        override = DatabaseSecurityOverride(
            blocked_tables=["db1_secret", "Audit.Events"],
            allow_explain=True,
            readonly_role="db1_reader",
            safe_search_path="db1_schema",
        )
        policy = EffectivePolicy.merge(base, override)

        # Replace, not union: db1 does not inherit global blocklists
        assert policy.blocked_tables == frozenset({"db1_secret", "audit.events"})
        assert policy.allow_explain is True
        assert policy.readonly_role == "db1_reader"
        assert policy.safe_search_path == "db1_schema"

    def test_partial_override_keeps_unset_globals(self) -> None:
        """None fields on the override fall back to the global config."""
        base = SecurityConfig(
            blocked_columns=["ssn"],
            allow_explain_analyze=True,
            readonly_role="global_reader",
        )
        override = DatabaseSecurityOverride(blocked_tables=["local_only"])
        policy = EffectivePolicy.merge(base, override)

        assert policy.blocked_tables == frozenset({"local_only"})
        assert policy.blocked_columns == frozenset({"ssn"})
        assert policy.allow_explain_analyze is True
        assert policy.readonly_role == "global_reader"

    def test_global_only_fields_ignore_override(self) -> None:
        """blocked_functions, max_rows and max_execution_time are global-only."""
        base = SecurityConfig(max_rows=1234, max_execution_time=7.5)
        # DatabaseSecurityOverride has no fields for these; merged policy must
        # always carry the global values.
        override = DatabaseSecurityOverride(blocked_tables=["x"])
        policy = EffectivePolicy.merge(base, override)

        assert policy.max_rows == 1234
        assert policy.max_execution_time == 7.5
        assert (
            policy.blocked_functions
            == EffectivePolicy.merge(SecurityConfig(), None).blocked_functions
        )

    def test_frozen_policy(self) -> None:
        """EffectivePolicy is immutable."""
        policy = EffectivePolicy.merge(SecurityConfig(), None)
        with pytest.raises(dataclasses.FrozenInstanceError):
            policy.allow_explain = True  # type: ignore[misc]

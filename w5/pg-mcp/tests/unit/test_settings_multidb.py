"""Unit tests for multi-database configuration parsing (P3 / §4.1).

model_post_init must resolve settings.databases fail-fast: any inconsistency
aborts startup instead of surfacing at first request.
"""

import json
import logging

import pytest
from pydantic import ValidationError

from pg_mcp.config.settings import (
    MultiDatabaseConfig,
    OpenAIConfig,
    Settings,
)


def make_settings(**multidb_kwargs: object) -> Settings:
    """Build a Settings with a valid OpenAI key and explicit multidb config."""
    return Settings(
        openai=OpenAIConfig(api_key="sk-test-key"),
        multidb=MultiDatabaseConfig(**multidb_kwargs),  # type: ignore[arg-type]
    )


TWO_DB_JSON = json.dumps(
    [
        {"connection": {"name": "db1", "host": "h1"}},
        {
            "connection": {"name": "db2", "host": "h2"},
            "security": {"blocked_tables": ["secrets"], "allow_explain": True},
        },
    ]
)


class TestSingleDatabaseMode:
    """DATABASE_* variables still drive configuration by default."""

    def test_default_single_database_entry(self) -> None:
        settings = make_settings()
        assert len(settings.databases) == 1
        entry = settings.databases[0]
        assert entry.connection.name == settings.database.name
        assert entry.security is None

    def test_entry_mirrors_database_config(self) -> None:
        settings = Settings(
            openai=OpenAIConfig(api_key="sk-test-key"),
            database={"host": "dbhost", "name": "appdb"},  # type: ignore[arg-type]
        )
        entry = settings.databases[0].connection
        assert entry.host == "dbhost"
        assert entry.name == "appdb"

    def test_default_database_mismatch_rejected(self) -> None:
        with pytest.raises(ValidationError, match="does not match"):
            make_settings(default_database="other")


class TestMultiDatabaseJsonMode:
    """MULTIDB_DATABASES_JSON switches to multi-database mode."""

    def test_json_entries_parsed(self) -> None:
        settings = make_settings(databases_json=TWO_DB_JSON)
        assert [e.connection.name for e in settings.databases] == ["db1", "db2"]
        assert settings.databases[0].connection.host == "h1"

    def test_security_override_parsed(self) -> None:
        settings = make_settings(databases_json=TWO_DB_JSON)
        sec = settings.databases[1].security
        assert sec is not None
        assert sec.blocked_tables == ["secrets"]
        assert sec.allow_explain is True

    def test_database_vars_ignored_warning_logged(self, caplog) -> None:
        settings = make_settings(databases_json=TWO_DB_JSON)
        # DATABASE_* single-database values must NOT leak into JSON mode
        assert settings.databases[0].connection.name == "db1"
        with caplog.at_level(logging.WARNING, logger="pg_mcp.config.settings"):
            make_settings(databases_json=TWO_DB_JSON)
        assert any("ignored" in r.message for r in caplog.records)

    def test_default_database_valid(self) -> None:
        settings = make_settings(databases_json=TWO_DB_JSON, default_database="db2")
        assert settings.databases[1].connection.name == "db2"


class TestMultiDatabaseFailFast:
    """Malformed configuration aborts startup with a clear error."""

    def test_invalid_json_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not valid JSON"):
            make_settings(databases_json="{not json")

    def test_non_array_rejected(self) -> None:
        with pytest.raises(ValidationError, match="non-empty JSON array"):
            make_settings(databases_json='{"connection": {"name": "db1"}}')

    def test_empty_array_rejected(self) -> None:
        with pytest.raises(ValidationError, match="non-empty JSON array"):
            make_settings(databases_json="[]")

    def test_entry_missing_name_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Invalid MULTIDB_DATABASES_JSON"):
            make_settings(databases_json='[{"connection": {"host": "h1"}}]')

    def test_duplicate_names_rejected(self) -> None:
        dup = json.dumps(
            [
                {"connection": {"name": "db1"}},
                {"connection": {"name": "db1", "host": "h2"}},
            ]
        )
        with pytest.raises(ValidationError, match="Duplicate database names"):
            make_settings(databases_json=dup)

    def test_unknown_default_database_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not one of"):
            make_settings(databases_json=TWO_DB_JSON, default_database="ghost")

    def test_blank_json_string_falls_back_to_single_db(self) -> None:
        """Whitespace-only JSON is treated as unset."""
        settings = make_settings(databases_json="   ")
        assert len(settings.databases) == 1

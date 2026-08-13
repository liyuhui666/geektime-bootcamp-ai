"""Unit tests for the export service, exporters, and filename utilities."""

import csv
import io
import json
from datetime import date, datetime, timezone

import pytest

from app.models.schemas import QueryColumn, QueryResult
from app.services.export import (
    CsvExporter,
    ExporterRegistry,
    ExportService,
    JsonExporter,
    NdJsonExporter,
    _normalize,
    export_service,
)
from app.utils.filename import slugify, utc_timestamp


def _make_result(
    rows: list[dict],
    columns: list[tuple[str, str]] | None = None,
    sql: str = "SELECT * FROM t",
    row_count: int | None = None,
) -> QueryResult:
    """Build a QueryResult for testing."""
    if columns is None:
        keys: list[str] = []
        for row in rows:
            for k in row:
                if k not in keys:
                    keys.append(k)
        columns = [(k, "text") for k in keys]
    cols = [QueryColumn(name=name, dataType=data_type) for name, data_type in columns]
    return QueryResult(
        columns=cols,
        rows=rows,
        rowCount=row_count if row_count is not None else len(rows),
        executionTimeMs=12,
        sql=sql,
    )


class TestNormalize:
    """Tests for the _normalize value serializer (FEATURE_EXPORT.md §5.2)."""

    def test_none_becomes_empty(self):
        assert _normalize(None) == ""

    def test_bool_true_before_int(self):
        # bool must be checked before int, else True -> "1"
        assert _normalize(True) == "true"
        assert _normalize(False) == "false"

    def test_int_and_float(self):
        assert _normalize(42) == "42"
        assert _normalize(3.5) == "3.5"

    def test_datetime_iso(self):
        dt = datetime(2026, 8, 13, 10, 0, 0, tzinfo=timezone.utc)
        assert _normalize(dt) == dt.isoformat()
        d = date(2026, 8, 13)
        assert _normalize(d) == "2026-08-13"

    def test_dict_and_list_json(self):
        assert _normalize({"a": 1}) == '{"a": 1}'
        assert _normalize([1, 2, 3]) == "[1, 2, 3]"

    def test_string_passthrough(self):
        assert _normalize("hello") == "hello"


class TestCsvExporter:
    """Tests for CSV output: BOM, headers, type normalization."""

    def test_has_utf8_bom(self):
        result = _make_result([{"id": 1}], columns=[("id", "integer")])
        payload = CsvExporter().export(result)
        assert payload.startswith(b"\xef\xbb\xbf")

    def test_headers_and_values(self):
        result = _make_result(
            [{"id": 1, "name": "alice"}],
            columns=[("id", "integer"), ("name", "text")],
        )
        payload = CsvExporter().export(result)
        text = payload.decode("utf-8-sig")  # strip BOM
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        assert reader.fieldnames == ["id", "name"]
        assert rows[0]["id"] == "1"
        assert rows[0]["name"] == "alice"

    def test_normalizes_special_types(self):
        result = _make_result(
            [{"active": True, "count": None, "meta": {"k": "v"}}],
            columns=[("active", "boolean"), ("count", "integer"), ("meta", "json")],
        )
        payload = CsvExporter().export(result)
        text = payload.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        assert rows[0]["active"] == "true"
        assert rows[0]["count"] == ""
        assert json.loads(rows[0]["meta"]) == {"k": "v"}

    def test_missing_column_safe(self):
        # Row missing a column key should not crash
        result = _make_result([{"id": 1}], columns=[("id", "integer"), ("name", "text")])
        payload = CsvExporter().export(result)
        assert b"id" in payload

    def test_chinese_with_bom(self):
        result = _make_result([{"name": "张三"}], columns=[("name", "text")])
        payload = CsvExporter().export(result)
        assert "张三".encode() in payload


class TestJsonExporter:
    """Tests for JSON document and array styles."""

    def test_document_style_structure(self):
        result = _make_result(
            [{"id": 1, "name": "alice"}],
            columns=[("id", "integer"), ("name", "text")],
        )
        payload = JsonExporter().export(result)
        doc = json.loads(payload)
        assert doc["rowCount"] == 1
        assert doc["sql"] == "SELECT * FROM t"
        assert {"name": "id", "dataType": "integer"} in doc["columns"]
        assert doc["rows"] == [{"id": 1, "name": "alice"}]

    def test_array_style(self):
        result = _make_result([{"id": 1}, {"id": 2}], columns=[("id", "integer")])
        payload = JsonExporter().export(result, options={"json_style": "array"})
        data = json.loads(payload)
        assert data == [{"id": 1}, {"id": 2}]

    def test_datetime_serialized(self):
        dt = datetime(2026, 8, 13, 10, 0, 0, tzinfo=timezone.utc)
        result = _make_result([{"ts": dt}], columns=[("ts", "timestamp")])
        payload = JsonExporter().export(result)
        doc = json.loads(payload)
        # datetime not natively JSON-serializable -> default=str kicks in
        assert "2026" in doc["rows"][0]["ts"]


class TestNdJsonExporter:
    """Tests for NDJSON: one JSON object per line."""

    def test_each_line_is_json(self):
        result = _make_result([{"id": 1}, {"id": 2}], columns=[("id", "integer")])
        payload = NdJsonExporter().export(result)
        lines = [ln for ln in payload.decode("utf-8").splitlines() if ln]
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"id": 1}
        assert json.loads(lines[1]) == {"id": 2}

    def test_empty_result(self):
        result = _make_result([], columns=[("id", "integer")], row_count=0)
        payload = NdJsonExporter().export(result)
        assert payload == b""


class TestExporterRegistry:
    """Tests for the registry (OCP: formats dynamic, not hardcoded)."""

    def test_supported_formats_contains_builtins(self):
        formats = export_service.supported_formats()
        assert set(["csv", "json", "ndjson"]).issubset(set(formats))

    def test_get_unknown_format_raises(self):
        registry = ExporterRegistry()
        with pytest.raises(KeyError):
            registry.get("xml")


class TestExportService:
    """Tests for the facade: returns (payload, content_type, ext)."""

    def test_export_csv_returns_triple(self):
        result = _make_result([{"id": 1}], columns=[("id", "integer")])
        payload, content_type, ext = export_service.export(result, "csv")
        assert ext == "csv"
        assert content_type == "text/csv; charset=utf-8"
        assert payload.startswith(b"\xef\xbb\xbf")

    def test_export_unknown_format_raises_valueerror(self):
        result = _make_result([{"id": 1}], columns=[("id", "integer")])
        with pytest.raises(ValueError):
            export_service.export(result, "xml")

    def test_custom_registry_isolates_formats(self):
        registry = ExporterRegistry()
        registry.register(CsvExporter())
        service = ExportService(registry)
        assert service.supported_formats() == ["csv"]

    def test_json_options_threaded(self):
        result = _make_result([{"id": 1}], columns=[("id", "integer")])
        payload, _, _ = export_service.export(result, "json", options={"json_style": "array"})
        assert json.loads(payload) == [{"id": 1}]


class TestFilenameUtils:
    """Tests for slugify / utc_timestamp (security: path traversal)."""

    def test_slugify_strips_special_chars(self):
        assert slugify("my-db") == "my-db"
        assert slugify("My DB (prod)") == "my-db-prod"

    def test_slugify_blocks_traversal(self):
        # No path separators survive
        assert "/" not in slugify("../etc/passwd")
        assert "\\" not in slugify("..\\etc\\passwd")
        assert slugify("../etc/passwd") == "etc-passwd"

    def test_slugify_empty_fallback(self):
        assert slugify("!!!") == "db"
        assert slugify("") == "db"

    def test_utc_timestamp_format(self):
        ts = utc_timestamp()
        # YYYYMMDDTHHMMSSZ
        assert len(ts) == 16
        assert ts.endswith("Z")
        assert "T" in ts

    def test_utc_timestamp_deterministic(self):
        fixed = datetime(2026, 8, 13, 10, 15, 30, tzinfo=timezone.utc)
        assert utc_timestamp(fixed) == "20260813T101530Z"

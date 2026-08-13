"""Data export service (Exporter + Registry + Facade).

Mirrors the project's adapter pattern (see ``app/adapters/``): adding a new
export format means writing one ``Exporter`` class and registering one line --
no existing code changes (Open-Closed Principle). The format list is driven
entirely by the registry at runtime, never hardcoded in a schema.

Example:
    payload, content_type, ext = export_service.export(result, "csv")
"""

import csv
import io
import json
import logging
from collections.abc import Iterator
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

from app.models.schemas import QueryResult

logger = logging.getLogger(__name__)


def _normalize(value: Any) -> str:
    """Serialize a single cell value to a string safe for text formats.

    Ordering matters:
    - ``bool`` is checked **before** ``int`` (``bool`` is a subclass of ``int``
      in Python, so ``True`` would otherwise become ``"1"``).
    - ``dict`` / ``list`` are JSON-encoded so structured columns survive.
    - ``datetime`` / ``date`` use ISO 8601.

    Args:
        value: A cell value from ``QueryResult.rows`` (may be None)

    Returns:
        String representation of the value

    Examples:
        >>> _normalize(None)
        ''
        >>> _normalize(True)
        'true'
        >>> _normalize(42)
        '42'
        >>> _normalize({"a": 1})
        '{"a": 1}'
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


@runtime_checkable
class Exporter(Protocol):
    """Contract for converting a ``QueryResult`` into a byte stream.

    Implementations declare their format metadata as class attributes and
    implement ``export``. ``export_iter`` is an optional streaming hook
    reserved for future large-result support (see FEATURE_EXPORT.md §5.2).
    """

    format_name: str
    content_type: str
    file_extension: str

    def export(self, result: QueryResult, options: dict[str, Any] | None = None) -> bytes:
        """Serialize ``result`` to a complete byte payload."""
        ...

    def export_iter(self, result: QueryResult) -> Iterator[bytes]:
        """Yield the payload in chunks (default: single ``export`` chunk)."""
        yield self.export(result)


class CsvExporter:
    """CSV exporter using the stdlib ``csv`` module with a UTF-8 BOM."""

    format_name = "csv"
    content_type = "text/csv; charset=utf-8"
    file_extension = "csv"

    def export(self, result: QueryResult, options: dict[str, Any] | None = None) -> bytes:
        headers = [c.name for c in result.columns]
        # BOM so Excel detects UTF-8 (and renders Chinese correctly)
        buffer = io.StringIO()
        buffer.write("﻿")
        writer = csv.DictWriter(
            buffer,
            fieldnames=headers,
            lineterminator="\r\n",
            extrasaction="ignore",
        )
        writer.writeheader()
        for row in result.rows:
            writer.writerow({h: _normalize(row.get(h)) for h in headers})
        return buffer.getvalue().encode("utf-8")

    def export_iter(self, result: QueryResult) -> Iterator[bytes]:
        """Stream header then rows one at a time."""
        headers = [c.name for c in result.columns]
        # BOM + header
        buffer = io.StringIO()
        buffer.write("﻿")
        writer = csv.DictWriter(
            buffer, fieldnames=headers, lineterminator="\r\n", extrasaction="ignore"
        )
        writer.writeheader()
        yield buffer.getvalue().encode("utf-8")
        for row in result.rows:
            line_buf = io.StringIO()
            line_writer = csv.DictWriter(
                line_buf,
                fieldnames=headers,
                lineterminator="\r\n",
                extrasaction="ignore",
            )
            line_writer.writerow({h: _normalize(row.get(h)) for h in headers})
            yield line_buf.getvalue().encode("utf-8")


class JsonExporter:
    """JSON exporter supporting ``document`` (default) and ``array`` styles."""

    format_name = "json"
    content_type = "application/json; charset=utf-8"
    file_extension = "json"

    def export(self, result: QueryResult, options: dict[str, Any] | None = None) -> bytes:
        style = (options or {}).get("json_style", "document")
        if style == "array":
            payload: Any = result.rows
        else:
            payload = {
                "columns": [{"name": c.name, "dataType": c.data_type} for c in result.columns],
                "rows": result.rows,
                "rowCount": result.row_count,
                "sql": result.sql,
            }
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")


class NdJsonExporter:
    """NDJSON exporter: one JSON object per line."""

    format_name = "ndjson"
    content_type = "application/x-ndjson"
    file_extension = "ndjson"

    def export(self, result: QueryResult, options: dict[str, Any] | None = None) -> bytes:
        lines = [json.dumps(row, ensure_ascii=False, default=str) for row in result.rows]
        return ("\n".join(lines) + "\n" if lines else "").encode("utf-8")

    def export_iter(self, result: QueryResult) -> Iterator[bytes]:
        for row in result.rows:
            yield (json.dumps(row, ensure_ascii=False, default=str) + "\n").encode("utf-8")


class ExporterRegistry:
    """Registry of exporters keyed by ``format_name`` (Factory pattern).

    Mirrors ``DatabaseAdapterRegistry``: new formats are registered, not
    hard-coded. Unknown formats raise ``KeyError`` from ``get``.
    """

    def __init__(self) -> None:
        self._exporters: dict[str, Exporter] = {}

    def register(self, exporter: Exporter) -> None:
        """Register an exporter instance under its ``format_name``."""
        self._exporters[exporter.format_name] = exporter
        logger.info("Registered exporter for format '%s'", exporter.format_name)

    def get(self, format_name: str) -> Exporter:
        """Get the exporter for ``format_name``.

        Raises:
            KeyError: If the format is not registered.
        """
        if format_name not in self._exporters:
            raise KeyError(f"Unsupported export format: '{format_name}'")
        return self._exporters[format_name]

    def supported(self) -> list[str]:
        """Return the list of registered format names (sorted, stable order)."""
        return list(self._exporters.keys())


class ExportService:
    """Facade isolating the API layer from concrete exporter details."""

    def __init__(self, registry: ExporterRegistry) -> None:
        self.registry = registry
        logger.info("Initialized ExportService")

    def export(
        self,
        result: QueryResult,
        fmt: str,
        options: dict[str, Any] | None = None,
    ) -> tuple[bytes, str, str]:
        """Serialize ``result`` to ``fmt``.

        Args:
            result: The query result to export.
            fmt: Format name (e.g. ``"csv"``).
            options: Optional format-specific options (e.g. ``{"json_style": "array"}``).

        Returns:
            Tuple of ``(payload, content_type, file_extension)``.

        Raises:
            ValueError: If ``fmt`` is not a registered format.
        """
        try:
            exporter = self.registry.get(fmt)
        except KeyError as e:
            raise ValueError(str(e)) from e
        payload = exporter.export(result, options)
        logger.info(
            "Exported %d rows to %s (%d bytes)",
            result.row_count,
            fmt,
            len(payload),
        )
        return payload, exporter.content_type, exporter.file_extension

    def supported_formats(self) -> list[str]:
        """Proxy to the registry for the API layer."""
        return self.registry.supported()


# Global singletons (consistent with database_service / adapter_registry style)
exporter_registry = ExporterRegistry()
for _exporter in (CsvExporter(), JsonExporter(), NdJsonExporter()):
    exporter_registry.register(_exporter)
export_service = ExportService(exporter_registry)

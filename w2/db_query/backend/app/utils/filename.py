"""Filename utilities for safe, timestamped export file names."""

import re
from datetime import datetime, timezone


def slugify(value: str) -> str:
    """Normalize a string into a filename-safe slug.

    Keeps only ``[a-z0-9-]`` so the result is safe to embed in a
    ``Content-Disposition`` header (no path separators, no quotes, no
    traversal characters). Empty input yields ``"db"`` so we never emit an
    empty segment.

    Args:
        value: Raw string (typically a database connection name)

    Returns:
        Lowercase slug containing only ``a-z0-9-``

    Examples:
        >>> slugify("my-postgres")
        'my-postgres'
        >>> slugify("My DB (prod)")
        'my-db-prod'
        >>> slugify("../etc/passwd")
        'etc-passwd'
    """
    lowered = value.lower()
    # Replace any run of non-alphanumeric characters with a single hyphen
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return slug or "db"


def utc_timestamp(now: datetime | None = None) -> str:
    """Return a filename-safe UTC timestamp (compact ISO 8601).

    Format ``YYYYMMDDTHHMMSSZ`` is lexicographically sortable and free of
    characters that need escaping in a filename or HTTP header.

    Args:
        now: Optional datetime to format; defaults to current UTC time

    Returns:
        Compact UTC timestamp string ending with ``Z``

    Examples:
        >>> utc_timestamp(datetime(2026, 8, 13, 10, 15, 30, tzinfo=timezone.utc))
        '20260813T101530Z'
    """
    ts = now if now is not None else datetime.now(timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

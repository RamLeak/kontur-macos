"""Small deterministic helpers shared by backend domain modules."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def iso_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


def bounded_text(value: str, maximum: int, field: str = "value") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{field} exceeds {maximum} bytes")
    return value


def bounded_json(value: Any, maximum: int, field: str = "value") -> Any:
    encoded = canonical_json(value).encode("utf-8")
    if len(encoded) > maximum:
        raise ValueError(f"{field} exceeds {maximum} bytes")
    return value


def normalized_answer(value: str | None) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", "", value).strip().casefold()


def list_cursor(items: list[Any], limit: int, cursor: str | None) -> tuple[list[Any], str | None]:
    """Apply a deliberately opaque integer offset cursor to an in-memory list."""

    try:
        offset = int(cursor) if cursor else 0
    except ValueError:
        offset = 0
    limit = max(1, min(limit, 100))
    page = items[offset : offset + limit]
    next_cursor = str(offset + limit) if offset + limit < len(items) else None
    return page, next_cursor

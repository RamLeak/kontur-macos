"""Stable, filter-bound keyset pagination for the source catalog."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from sqlalchemy import Integer, and_, case, cast, func, or_, select
from sqlalchemy.orm import Session

from .models import Task, TaskRevision
from .source_catalog import SourceError
from .util import sha256_json


def _sort_keys(sort: str) -> list[Any]:
    # Decimal IDs keep their original spelling. Length + digits gives natural
    # numeric order without integer overflow or losing leading zeroes. Legacy
    # non-decimal IDs remain deterministic, after numeric IDs of the same type.
    numeric = (Task.external_id != "") & ~Task.external_id.bool_op("GLOB")("*[^0-9]*")
    digits = func.ltrim(Task.external_id, "0")
    tie = [Task.line_number, case((numeric, 0), else_=1),
           case((numeric, func.length(digits)), else_=0),
           case((numeric, digits), else_=Task.external_id), Task.external_id, Task.id]
    if sort == "number":
        # The supplied snapshot records the exact upstream sequence per type.
        # Filters retain those ranks; old imports without a rank keep the
        # legacy natural numeric order. JSON type checks reject fake strings.
        order = TaskRevision.source_metadata["kege_source_order"].as_integer()
        ranked = (
            (TaskRevision.source_metadata["kege_schema"].as_string() == "kege-current-bank/2.0")
            & (func.json_type(TaskRevision.source_metadata, "$.kege_source_order") == "integer")
            & order.between(0, 10_000)
        )
        return [Task.line_number, case((ranked, 0), else_=1),
                case((ranked, order), else_=0), *tie[1:]]
    # All keys sort ascending. Negation makes years/publication times descend
    # while unknown values sort last; final source/task IDs break every tie.
    freshness = [-func.coalesce(TaskRevision.source_year, 0),
                 -cast(func.coalesce(func.strftime("%s", TaskRevision.source_created_at), "0"), Integer)]
    if sort == "newest":
        return [*freshness, *tie]
    ranks = {"easy": 0, "medium": 1, "hard": 2}
    if sort == "difficulty_desc":
        ranks = {name: 2 - rank for name, rank in ranks.items()}
    return [case(ranks, value=TaskRevision.difficulty, else_=3), *freshness, *tie]


def paginate_tasks(
    session: Session, query_stmt: Any, *, sort: str, scope: dict[str, Any], limit: int, cursor: str | None
) -> tuple[list[Any], str | None, int]:
    keys = _sort_keys(sort)
    fingerprint = sha256_json({"sort": sort, **scope})
    values: list[Any] | None = None
    if cursor:
        try:
            if len(cursor) > 2048:
                raise ValueError
            encoded_cursor = cursor.encode("ascii")
            padding = b"=" * (-len(encoded_cursor) % 4)
            raw = base64.b64decode(encoded_cursor + padding, altchars=b"-_", validate=True)
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict) or set(parsed) != {"v", "scope", "keys"}:
                raise ValueError
            if parsed["v"] != 4 or isinstance(parsed["v"], bool) or parsed["scope"] != fingerprint:
                raise ValueError
            values = parsed["keys"]
            if not isinstance(values, list) or len(values) != len(keys):
                raise ValueError
            for index, value in enumerate(values):
                if index >= len(keys) - 3:
                    if not isinstance(value, str) or len(value) > 255:
                        raise ValueError
                elif isinstance(value, bool) or not isinstance(value, int) or abs(value) > 10_000_000_000:
                    raise ValueError
        except (ValueError, TypeError, UnicodeError, binascii.Error, json.JSONDecodeError) as exc:
            raise SourceError("TASK_CURSOR_INVALID", "catalog filters or sort changed; reload the first page") from exc
    total = int(session.scalar(select(func.count()).select_from(query_stmt.subquery())) or 0)
    if values is not None:
        query_stmt = query_stmt.where(or_(*(
            and_(*(keys[previous] == values[previous] for previous in range(index)), key > values[index])
            for index, key in enumerate(keys)
        )))
    result = list(session.execute(query_stmt.add_columns(*keys).order_by(*keys).limit(limit + 1)).all())
    page = result[:limit]
    next_cursor = None
    if len(result) > limit and page:
        encoded = json.dumps({"v": 4, "scope": fingerprint, "keys": list(page[-1][2:])},
                             ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        next_cursor = base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")
    return [(row[0], row[1]) for row in page], next_cursor, total

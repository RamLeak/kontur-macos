"""Owner-only draft sync and durable bounded invalidation polling."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, delete, func, select, text
from sqlalchemy.orm import Session

from .ai_gateway import has_active_sealed_exam
from .api_common import api_error, ensure_owner_row, get_db, owner_session
from .auth import OwnerPrincipal
from .models import Conversation, OwnerSyncChange, OwnerSyncRecord, OwnerSyncWrite, SolveSession, TaskRevision, utcnow
from .util import iso_datetime

router = APIRouter(tags=["sync"])
Db = Annotated[Session, Depends(get_db)]
Owner = Annotated[OwnerPrincipal, Depends(owner_session)]
ExpectedOwner = Annotated[str | None, Header(alias="X-Sync-Owner", max_length=128)]
Kind = Literal["practice_answer", "composer", "preferences", "catalog_filters"]


class SyncContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Kind
    solve_session_id: str | None = Field(default=None, max_length=36)
    conversation_id: str | None = Field(default=None, max_length=36)
    task_id: str | None = Field(default=None, max_length=36)
    task_revision_id: str | None = Field(default=None, max_length=36)
    content_hash: str | None = Field(default=None, max_length=64)


class SyncWrite(SyncContext):
    base_version: int = Field(ge=0, le=2**53 - 1)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    value: dict[str, Any]


class SharedPreferences(BaseModel):
    """Deliberately excludes device layout/cache and assistant authority."""

    model_config = ConfigDict(extra="forbid", strict=True)
    theme: Literal["system", "light", "dark"] | None = None
    reducedMotion: bool | None = None
    textSize: Literal["standard", "large"] | None = None
    defaultTaskType: int | None = Field(default=None, ge=1, le=27)
    restoreWorkspace: bool | None = None
    editorTheme: Literal["interface", "light", "charcoal"] | None = None
    editorFontSize: Literal[12, 14, 16] | None = None
    editorWrap: bool | None = None
    editorTabSize: Literal[2, 4, 8] | None = None
    editorLineNumbers: bool | None = None
    boardBackground: Literal["blank", "grid", "dots"] | None = None


class CatalogFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    unsolvedOnly: bool | None = None
    taskType: int | None = Field(default=None, ge=1, le=27)


def _hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _context(data: SyncContext, session: Session, owner_id: str) -> dict[str, Any]:
    fields = set(SyncContext.model_fields)
    context = SyncContext.model_validate(data.model_dump(include=fields)).model_dump()
    if data.kind in ("preferences", "catalog_filters"):
        if any(value is not None for key, value in context.items() if key != "kind"):
            api_error(422, "SYNC_CONTEXT_INVALID", "Account settings cannot carry task context")
        return context
    # Draft content must not become an alternate surface around sealed-exam policy.
    if has_active_sealed_exam(session, owner_id):
        api_error(403, "EXAM_SEALED", "Practice and composer drafts are unavailable during a sealed exam")
    solve = None
    if data.solve_session_id:
        solve = session.scalar(
            select(SolveSession).where(
                SolveSession.id == data.solve_session_id,
                SolveSession.owner_id == owner_id,
            )
        )
        if solve is None:
            api_error(404, "NOT_FOUND", "Solve session not found")
        revision = session.get(TaskRevision, solve.task_revision_id)
        if revision is None or (
            data.task_id != revision.task_id
            or data.task_revision_id != revision.id
            or data.content_hash != revision.content_hash
        ):
            api_error(422, "REVISION_INVALID", "Draft must match the solve session task, revision and content hash")
    elif any((data.task_id, data.task_revision_id, data.content_hash)):
        api_error(422, "SYNC_CONTEXT_INVALID", "Task context requires a solve session")
    if data.kind == "practice_answer":
        if solve is None or data.conversation_id is not None:
            api_error(422, "SYNC_CONTEXT_INVALID", "Practice answer requires only a revision-bound solve session")
    else:
        conversation = session.scalar(
            select(Conversation).where(
                Conversation.id == data.conversation_id,
                Conversation.owner_id == owner_id,
                Conversation.deleted_at.is_(None),
            )
        )
        if conversation is None:
            api_error(404, "NOT_FOUND", "Conversation not found")
        if solve is not None and solve.conversation_id != conversation.id:
            api_error(422, "SYNC_CONTEXT_INVALID", "Conversation does not belong to this solve session")
    return context


def _value(data: SyncWrite) -> dict[str, Any]:
    if len(json.dumps(data.value, ensure_ascii=False).encode()) > 32768:
        api_error(413, "SYNC_VALUE_TOO_LARGE", "Sync value exceeds 32 KiB")
    if data.kind in ("practice_answer", "composer"):
        field = "answer" if data.kind == "practice_answer" else "text"
        if set(data.value) != {field} or not isinstance(data.value[field], str) or len(data.value[field]) > 16000:
            api_error(422, "SYNC_VALUE_INVALID", f"Draft requires only a bounded {field} string")
        return data.value
    schema = SharedPreferences if data.kind == "preferences" else CatalogFilters
    from pydantic import ValidationError

    try:
        return schema.model_validate(data.value).model_dump(exclude_unset=True)
    except ValidationError:
        api_error(422, "SYNC_VALUE_INVALID", "Unknown or invalid shared preference/filter")


def _view(record: OwnerSyncRecord | None, context: dict[str, Any]) -> dict[str, Any]:
    return {
        "context": context,
        "version": record.version if record else 0,
        "value": record.value if record else None,
        "updated_at": iso_datetime(record.updated_at) if record else None,
    }


def _check_expected_owner(principal: OwnerPrincipal, expected_owner: str | None) -> None:
    # An expectation is never authentication or an owner selector. It only
    # refuses stale work after another tab changes the authenticated cookie.
    if expected_owner is not None and expected_owner != principal.owner_id:
        api_error(401, "OWNER_SESSION_CHANGED", "Authenticated owner changed; reload the account session")


@router.get("/sync/records")
def get_record(
    session: Db, principal: Owner, context: Annotated[SyncContext, Query()], expected_owner: ExpectedOwner = None
) -> dict[str, Any]:
    _check_expected_owner(principal, expected_owner)
    bound = _context(context, session, principal.owner_id)
    record = session.get(OwnerSyncRecord, _hash([principal.owner_id, bound]))
    return _view(record, bound)


@router.put("/sync/records")
def put_record(data: SyncWrite, session: Db, principal: Owner, expected_owner: ExpectedOwner = None) -> dict[str, Any]:
    _check_expected_owner(principal, expected_owner)
    # SQLite is the single supported writer. Serialize before reads, including
    # reservation/retry checks; simultaneous base-version writes cannot both win.
    session.execute(text("BEGIN IMMEDIATE"))
    context = _context(data, session, principal.owner_id)
    value = _value(data)
    request_hash = _hash([context, data.base_version, value])
    # Successful replay is guaranteed for at most 7 days and the latest 4096
    # writes per owner. Expired requests still pass the base-version check;
    # an old acknowledged mutation can never silently be reapplied.
    session.execute(
        delete(OwnerSyncWrite).where(
            OwnerSyncWrite.owner_id == principal.owner_id,
            OwnerSyncWrite.created_at < utcnow() - timedelta(days=7),
        )
    )
    previous = session.scalar(
        select(OwnerSyncWrite).where(
            OwnerSyncWrite.owner_id == principal.owner_id,
            OwnerSyncWrite.idempotency_key == data.idempotency_key,
        )
    )
    if previous:
        if previous.request_hash != request_hash:
            api_error(409, "IDEMPOTENCY_CONFLICT", "Retry key was already used for a different sync write")
        return previous.response
    record_id = _hash([principal.owner_id, context])
    record = session.get(OwnerSyncRecord, record_id)
    version = record.version if record else 0
    if data.base_version != version:
        api_error(409, "VERSION_CONFLICT", "Draft changed on another client", {"current": _view(record, context)})
    ensure_owner_row(session, principal.owner_id)
    if record is None:
        record = OwnerSyncRecord(
            id=record_id,
            owner_id=principal.owner_id,
            kind=data.kind,
            context=context,
            value=value,
            version=1,
            updated_at=utcnow(),
        )
        session.add(record)
    else:
        record.value = value
        record.version += 1
        record.updated_at = utcnow()
    response = _view(record, context)
    retained = (
        select(OwnerSyncWrite.id)
        .where(OwnerSyncWrite.owner_id == principal.owner_id)
        .order_by(
            OwnerSyncWrite.created_at.desc(),
            OwnerSyncWrite.id.desc(),
        )
        .limit(4095)
    )
    session.execute(
        delete(OwnerSyncWrite).where(
            OwnerSyncWrite.owner_id == principal.owner_id,
            OwnerSyncWrite.id.not_in(retained),
        )
    )
    session.add(
        OwnerSyncWrite(
            owner_id=principal.owner_id,
            idempotency_key=data.idempotency_key,
            request_hash=request_hash,
            response=response,
        )
    )
    session.commit()
    return response


@router.get("/sync/changes")
def get_changes(
    session: Db,
    principal: Owner,
    after: int = Query(default=0, ge=0, le=2**53 - 1),
    limit: int = Query(default=100, ge=1, le=200),
    expected_owner: ExpectedOwner = None,
) -> dict[str, Any]:
    _check_expected_owner(principal, expected_owner)
    rows = list(
        session.scalars(
            select(OwnerSyncChange)
            .where(
                OwnerSyncChange.owner_id == principal.owner_id,
                OwnerSyncChange.sequence > after,
            )
            .order_by(OwnerSyncChange.sequence)
            .limit(limit + 1)
        )
    )
    page = rows[:limit]
    latest = (
        session.scalar(
            select(func.max(OwnerSyncChange.sequence)).where(
                OwnerSyncChange.owner_id == principal.owner_id,
            )
        )
        or 0
    )
    oldest = (
        session.scalar(
            select(func.min(OwnerSyncChange.sequence)).where(
                OwnerSyncChange.owner_id == principal.owner_id,
            )
        )
        or 0
    )
    return {
        "cursor": page[-1].sequence if page else min(after, latest),
        "latest_cursor": latest,
        "reset_required": bool(after > latest or (after and oldest > after)),
        "has_more": len(rows) > limit,
        "changes": [{"revision": row.sequence, "topic": row.topic} for row in page],
    }


def install_change_triggers(connection: Connection) -> None:
    """Bounded SQL identifiers only; writes and invalidations share a transaction.

    Watch authoritative aggregates, including bulk SQL mutations from old APIs.
    No payloads, grading flags or client identifiers enter this notification log.
    """
    connection.exec_driver_sql(
        "CREATE TRIGGER IF NOT EXISTS sync_changes_retention AFTER INSERT ON owner_sync_changes "
        "BEGIN DELETE FROM owner_sync_changes WHERE owner_id=NEW.owner_id AND sequence < "
        "(SELECT sequence FROM owner_sync_changes WHERE owner_id=NEW.owner_id "
        "ORDER BY sequence DESC LIMIT 1 OFFSET 999); END"
    )
    tables = {
        "owner_sync_records": "sync_records",
        "solve_sessions": "solve_sessions",
        "attempts": "progress",
        "code_documents": "code",
        "board_documents": "board",
        "annotations": "annotations",
        "bookmarks": "bookmarks",
        "projects": "projects",
        "conversations": "conversations",
        "messages": "messages",
        "exam_sessions": "exams",
        "exam_answers": "exams",
        "exam_workspaces": "exams",
        "learning_events": "progress",
    }
    for table, topic in tables.items():
        for operation in ("INSERT", "UPDATE", "DELETE"):
            reference = "OLD" if operation == "DELETE" else "NEW"
            owner = f"{reference}.owner_id"
            if table == "exam_answers":
                owner = (
                    "(SELECT exam_sessions.owner_id FROM exam_items JOIN exam_sessions "
                    "ON exam_sessions.id=exam_items.exam_session_id "
                    f"WHERE exam_items.id={reference}.exam_item_id)"
                )
            connection.exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS sync_{table}_{operation.lower()} AFTER {operation} ON {table} "
                f"BEGIN INSERT INTO owner_sync_changes(owner_id, topic) VALUES ({owner}, '{topic}'); END"
            )

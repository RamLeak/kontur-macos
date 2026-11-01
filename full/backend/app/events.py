"""Transactional append-only learning event store."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import LearningEvent, new_id, utcnow
from .util import bounded_json, sha256_json


class EventConflict(ValueError):
    """The same idempotency key was reused with a different event."""

    code = "IDEMPOTENCY_CONFLICT"


def append_event(
    session: Session,
    *,
    owner_id: str,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    policy_version: str = "1",
    occurred_at: datetime | None = None,
) -> LearningEvent:
    """Insert or return an event inside the caller's transaction.

    Operational state and this event must be committed by the same session and
    transaction. The sequence is an SQLite autoincrement primary key, giving one
    ordered writer-visible sequence without a separate mutable counter row.
    """

    safe_payload = bounded_json(payload or {}, 200_000, "event payload")
    payload_hash = sha256_json(safe_payload)
    key = idempotency_key or f"event:{new_id()}"
    existing = session.scalar(
        select(LearningEvent).where(
            LearningEvent.owner_id == owner_id,
            LearningEvent.idempotency_key == key,
        )
    )
    if existing is not None:
        if existing.event_type != event_type or existing.payload_hash != payload_hash:
            raise EventConflict(f"idempotency key already describes another event: {key}")
        return existing

    event = LearningEvent(
        event_id=new_id(),
        owner_id=owner_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        occurred_at=occurred_at or utcnow(),
        recorded_at=utcnow(),
        schema_version="v1",
        policy_version=policy_version,
        idempotency_key=key,
        payload=safe_payload,
        payload_hash=payload_hash,
    )
    session.add(event)
    session.flush()
    return event

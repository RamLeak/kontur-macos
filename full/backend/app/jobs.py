"""SQLite-backed idempotent job leases for the single-owner worker."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from .models import Job, new_id, utcnow
from .util import bounded_json


def enqueue_job(
    session: Session,
    *,
    kind: str,
    payload: dict[str, Any],
    idempotency_key: str,
) -> Job:
    existing = session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
    if existing is not None:
        return existing
    job = Job(
        id=new_id(),
        kind=kind,
        status="queued",
        payload=bounded_json(payload, 200_000, "job payload"),
        idempotency_key=idempotency_key,
        attempts=0,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(job)
    session.flush()
    return job


def claim_job(session: Session, *, worker_id: str, lease_seconds: int = 60) -> Job | None:
    now = utcnow()
    candidate_id = session.scalar(
        select(Job.id)
        .where(
            or_(
                Job.status == "queued",
                (Job.status == "running") & (Job.lease_expires_at < now),
            )
        )
        .order_by(Job.created_at, Job.id)
        .limit(1)
    )
    if candidate_id is None:
        return None

    # The conditional UPDATE is the claim gate.  Two workers may observe the
    # same candidate, but only one can transition it from an eligible state;
    # the other receives no RETURNING row and must retry later.  This avoids a
    # select-then-mutate double claim without relying on an application mutex.
    claimed_id = session.scalar(
        update(Job)
        .where(
            and_(
                Job.id == candidate_id,
                or_(
                    Job.status == "queued",
                    (Job.status == "running") & (Job.lease_expires_at < now),
                ),
            )
        )
        .values(
            status="running",
            lease_owner=worker_id,
            lease_expires_at=now + timedelta(seconds=max(1, lease_seconds)),
            attempts=Job.attempts + 1,
            updated_at=now,
        )
        .returning(Job.id)
    )
    if claimed_id is None:
        return None
    job = session.get(Job, claimed_id)
    if job is None:
        return None
    session.flush()
    return job


def complete_job(session: Session, job: Job, *, worker_id: str) -> None:
    if job.status != "running" or job.lease_owner != worker_id:
        raise ValueError("job lease is not owned by this worker")
    job.status = "succeeded"
    job.lease_owner = None
    job.lease_expires_at = None
    job.updated_at = utcnow()
    session.flush()


def fail_job(session: Session, job: Job, *, worker_id: str, error: str) -> None:
    if job.status != "running" or job.lease_owner != worker_id:
        raise ValueError("job lease is not owned by this worker")
    job.status = "failed"
    job.last_error = error[:2000]
    job.lease_owner = None
    job.lease_expires_at = None
    job.updated_at = utcnow()
    session.flush()

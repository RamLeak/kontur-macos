"""Durable, resumable and metadata-only КЕГЭ catalog synchronization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session

from .assets import AssetError
from .config import Settings
from .events import EventConflict
from .models import SourceSyncItem, SourceSyncJob, utcnow
from .source_catalog import (
    SOURCE_NAME,
    SourceError,
    ensure_source_policy,
    scoped_sync_key,
)
from .source_transport import BoundedKegeClient, SourceFetchError, admit_remote_task
from .util import sha256_json

CATALOG_MAX_ITEMS = 1000
CATALOG_PARSER_VERSION = "kompege-json-observed-2026-09-03"
CATALOG_LEASE_SECONDS = 60


@dataclass(frozen=True, slots=True)
class SyncJobReservation:
    job: SourceSyncJob
    duplicate: bool


def _request_hash(canonical_urls: list[str], target_exam_year: int | None) -> str:
    return sha256_json(
        {
            "schema_version": 1,
            "source": SOURCE_NAME,
            "parser_version": CATALOG_PARSER_VERSION,
            "urls": canonical_urls,
            "target_exam_year": target_exam_year,
        }
    )


def reserve_catalog_job(
    session: Session,
    *,
    owner_id: str,
    settings: Settings,
    canonical_urls: list[str],
    idempotency_key: str,
    target_exam_year: int | None = None,
) -> SyncJobReservation:
    """Persist a bounded catalog manifest before any upstream request."""

    if not 1 <= len(canonical_urls) <= CATALOG_MAX_ITEMS:
        raise SourceError("SOURCE_BATCH_INVALID", "catalog batch must contain 1 to 1000 URLs")
    if settings.source_mode != "sync" or not settings.source_network_enabled:
        raise SourceError("SOURCE_DISABLED", "КЕГЭ catalog sync is disabled")
    if target_exam_year is not None and not 2000 <= target_exam_year <= 2100:
        raise SourceError("YEAR_INVALID", "target_exam_year must be between 2000 and 2100")
    policy = ensure_source_policy(session, settings)
    if policy.permission_status not in {"private_authorized", "public_authorized"}:
        raise SourceError("SOURCE_RIGHTS_REQUIRED", "source policy has not authorized learner visibility")
    request_hash = _request_hash(canonical_urls, target_exam_year)
    existing = session.scalar(
        select(SourceSyncJob).where(
            SourceSyncJob.owner_id == owner_id,
            SourceSyncJob.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.request_hash != request_hash:
            raise SourceError("IDEMPOTENCY_CONFLICT", "idempotency key already describes another catalog manifest")
        return SyncJobReservation(existing, True)

    job = SourceSyncJob(
        owner_id=owner_id,
        source_policy_id=policy.id,
        mode="catalog-sync",
        request_hash=request_hash,
        manifest_hash=sha256_json({"urls": canonical_urls, "target_exam_year": target_exam_year}),
        parser_version=CATALOG_PARSER_VERSION,
        status="queued",
        max_items=len(canonical_urls),
        target_exam_year=target_exam_year,
        cursor_index=0,
        total_items=len(canonical_urls),
        counters={"tasks_seen": 0, "tasks_admitted": 0, "revisions_created": 0, "failures": 0},
        failures=[],
        idempotency_key=idempotency_key,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(job)
    session.flush()
    session.add_all(
        [
            SourceSyncItem(
                job_id=job.id,
                ordinal=ordinal,
                canonical_url=canonical,
                status="pending",
                attempts=0,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            for ordinal, canonical in enumerate(canonical_urls)
        ]
    )
    session.flush()
    return SyncJobReservation(job, False)


def _claim_item(session: Session, job_id: str, worker_id: str, *, retry_failed: bool = False) -> SourceSyncItem | None:
    now = utcnow()
    stale_before = now - timedelta(seconds=CATALOG_LEASE_SECONDS)
    session.execute(
        update(SourceSyncItem)
        .where(
            SourceSyncItem.job_id == job_id,
            SourceSyncItem.status == "fetching",
            SourceSyncItem.updated_at < stale_before,
        )
        .values(status="pending", last_error="worker lease expired", updated_at=now)
    )
    statuses = ["pending", "failed"] if retry_failed else ["pending"]
    candidate_id = session.scalar(
        select(SourceSyncItem.id)
        .where(
            SourceSyncItem.job_id == job_id,
            SourceSyncItem.status.in_(statuses),
        )
        .order_by(SourceSyncItem.ordinal, SourceSyncItem.id)
        .limit(1)
    )
    if candidate_id is None:
        return None
    claimed_id = session.scalar(
        update(SourceSyncItem)
        .where(
            and_(
                SourceSyncItem.id == candidate_id,
                SourceSyncItem.status.in_(statuses),
            )
        )
        .values(
            status="fetching",
            attempts=SourceSyncItem.attempts + 1,
            last_error=None,
            updated_at=now,
        )
        .returning(SourceSyncItem.id)
    )
    if claimed_id is None:
        return None
    item = session.get(SourceSyncItem, claimed_id)
    if item is None:
        return None
    job = session.get(SourceSyncJob, job_id)
    if job is not None:
        job.status = "running"
        job.lease_owner = worker_id
        job.lease_expires_at = now + timedelta(seconds=CATALOG_LEASE_SECONDS)
        job.updated_at = now
    session.flush()
    return item


def _failure(exc: Exception) -> dict[str, str]:
    code = str(getattr(exc, "code", "SOURCE_IMPORT_FAILED"))
    message = str(getattr(exc, "message", "source item admission failed"))[:500]
    return {"code": code, "message": message}


def _finish_job(session: Session, job_id: str) -> None:
    job = session.get(SourceSyncJob, job_id)
    if job is None:
        return
    pending = session.scalar(
        select(SourceSyncItem.id)
        .where(
            SourceSyncItem.job_id == job_id,
            SourceSyncItem.status.in_(["pending", "fetching"]),
        )
        .limit(1)
    )
    if pending is not None:
        job.status = "paused"
    else:
        failures = job.failures if isinstance(job.failures, list) else []
        job.status = "partial" if failures else "succeeded"
        job.lease_owner = None
        job.lease_expires_at = None
    job.updated_at = utcnow()
    session.flush()


def process_catalog_job(
    session: Session,
    *,
    job_id: str,
    owner_id: str,
    settings: Settings,
    client: BoundedKegeClient,
    worker_id: str = "api-source-sync",
    work_limit: int = CATALOG_MAX_ITEMS,
    retry_failed: bool = False,
) -> SourceSyncJob:
    """Process at most ``work_limit`` items with one transaction per item.

    The caller must commit the reservation before invoking this function. Every
    claim/result transition is committed independently, so an interrupted
    request can resume from the durable item cursor without re-fetching success.
    """

    if work_limit < 1 or work_limit > CATALOG_MAX_ITEMS:
        raise SourceError("SOURCE_BATCH_INVALID", "work_limit must be between 1 and 1000")
    processed = 0
    while processed < work_limit:
        # A worker owns transaction boundaries for this job. This keeps each
        # checkpoint durable even if the process dies during the next request.
        if session.in_transaction():
            session.commit()
        with session.begin():
            job = session.scalar(
                select(SourceSyncJob).where(
                    SourceSyncJob.id == job_id,
                    SourceSyncJob.owner_id == owner_id,
                )
            )
            if job is None:
                raise SourceError("NOT_FOUND", "source sync job not found")
            if job.status in {"succeeded", "partial"} and not retry_failed:
                break
            item = _claim_item(session, job.id, worker_id, retry_failed=retry_failed)
        if item is None:
            if session.in_transaction():
                session.commit()
            with session.begin():
                _finish_job(session, job_id)
            break
        item_id = item.id

        try:
            with session.begin():
                task, revision, _assets, duplicate = admit_remote_task(
                    session,
                    owner_id=owner_id,
                    settings=settings,
                    source_url=item.canonical_url,
                    idempotency_key=(
                        f"catalog:{job_id}:{item.ordinal}:{scoped_sync_key(owner_id, item.canonical_url)[:12]}"
                    ),
                    client=client,
                    target_exam_year=job.target_exam_year,
                )
                item = session.get(SourceSyncItem, item.id)
                job = session.get(SourceSyncJob, job_id)
                if item is None or job is None:
                    raise SourceError("SOURCE_STATE_INVALID", "source sync checkpoint disappeared")
                item.status = "succeeded"
                item.task_id = task.id
                item.revision_id = revision.id
                item.response_hash = revision.content_hash
                item.last_error = None
                job.cursor_index = max(job.cursor_index, item.ordinal + 1)
                counters = dict(job.counters or {})
                counters["tasks_seen"] = int(counters.get("tasks_seen", 0)) + 1
                counters["tasks_admitted"] = int(counters.get("tasks_admitted", 0)) + 1
                if not duplicate:
                    counters["revisions_created"] = int(counters.get("revisions_created", 0)) + 1
                # A successful retry resolves the prior item-level failure;
                # terminal job state and counters must reflect current durable
                # item status rather than historical transient attempts.
                failures = [
                    failure
                    for failure in list(job.failures or [])
                    if not isinstance(failure, dict) or failure.get("ordinal") != item.ordinal
                ]
                job.failures = failures
                counters["failures"] = len(failures)
                job.counters = counters
                job.updated_at = utcnow()
        except (SourceError, SourceFetchError, AssetError, EventConflict, ValueError) as exc:
            with session.begin():
                item = session.get(SourceSyncItem, item_id)
                job = session.get(SourceSyncJob, job_id)
                if item is not None:
                    item.status = "failed"
                    item.last_error = _failure(exc)["message"]
                    item.updated_at = utcnow()
                if job is not None:
                    failures = list(job.failures or [])
                    failures.append(
                        {
                            "ordinal": item.ordinal if item else -1,
                            "url": item.canonical_url if item else "",
                            **_failure(exc),
                        }
                    )
                    job.failures = failures[:1000]
                    counters = dict(job.counters or {})
                    counters["tasks_seen"] = int(counters.get("tasks_seen", 0)) + 1
                    counters["failures"] = int(counters.get("failures", 0)) + 1
                    job.counters = counters
                    job.cursor_index = max(job.cursor_index, (item.ordinal + 1) if item else job.cursor_index)
                    job.updated_at = utcnow()
        processed += 1

    if session.in_transaction():
        session.commit()
    with session.begin():
        _finish_job(session, job_id)
    job = session.get(SourceSyncJob, job_id)
    if job is None:
        raise SourceError("NOT_FOUND", "source sync job not found")
    return job


def sync_job_items(session: Session, job_id: str) -> list[SourceSyncItem]:
    return list(
        session.scalars(select(SourceSyncItem).where(SourceSyncItem.job_id == job_id).order_by(SourceSyncItem.ordinal))
    )

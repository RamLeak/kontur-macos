"""Atomic collector for the recent, difficulty-balanced private KЕГЭ catalog."""

from __future__ import annotations

import argparse
import json
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .assets import AssetError
from .auth import ensure_owner
from .catalog_policy import (
    FALLBACK_THRESHOLD,
    FALLBACK_YEAR,
    POLICY_VERSION,
    PRIMARY_YEARS,
    TARGET_PER_CELL,
    CatalogPolicyError,
    CatalogSelection,
    select_recent_catalog,
)
from .config import Settings
from .db import init_schema, make_engine, make_session_factory
from .events import EventConflict, append_event
from .models import CatalogEntry, SourceSyncItem, SourceSyncJob, utcnow
from .source_catalog import SourceError, canonical_source_url, ensure_source_policy, import_fixture
from .source_discovery import _eligible_number_tasks, _mapping_failure
from .source_transport import BoundedKegeClient, SourceFetchError, _normalise_number_payload
from .util import bounded_text, sha256_json

RECENT_CATALOG_MODE = "recent-catalog"
RECENT_CATALOG_PARSER_VERSION = "kompege-recent-catalog-v3"
NUMBER_API_TEMPLATE = "https://kompege.ru/api/v1/task/number/{number}"
NUMBER_COUNT = 27
MAX_RECORDS_PER_NUMBER = 1_000
MAX_SELECTED_TASKS = NUMBER_COUNT * 3 * TARGET_PER_CELL
LEASE_SECONDS = 180


def _number_urls() -> list[str]:
    urls: list[str] = []
    for line in range(1, NUMBER_COUNT + 1):
        rendered = NUMBER_API_TEMPLATE.format(number=line)
        canonical = canonical_source_url(rendered, api_endpoint=True)
        if canonical != rendered:
            raise SourceError("SOURCE_PATH_BLOCKED", "number endpoint rendered outside the approved path")
        urls.append(canonical)
    return urls


def _request_hash(urls: list[str]) -> str:
    return sha256_json(
        {
            "schema_version": 1,
            "source": "kompege",
            "mode": RECENT_CATALOG_MODE,
            "parser_version": RECENT_CATALOG_PARSER_VERSION,
            "urls": urls,
            "policy_version": POLICY_VERSION,
            "primary_years": list(PRIMARY_YEARS),
            "fallback_year": FALLBACK_YEAR,
            "fallback_threshold": FALLBACK_THRESHOLD,
            "target_per_cell": TARGET_PER_CELL,
        }
    )


def _base_counters() -> dict[str, Any]:
    return {
        "published": False,
        "policy_version": POLICY_VERSION,
        "primary_years": list(PRIMARY_YEARS),
        "fallback_year": FALLBACK_YEAR,
        "fallback_threshold": FALLBACK_THRESHOLD,
        "target_per_cell": TARGET_PER_CELL,
        "numbers_requested": 0,
        "numbers_completed": 0,
        "tasks_seen": 0,
        "tasks_eligible": 0,
        "tasks_selected": 0,
        "total_selected": 0,
        "quota_met": False,
        "attachment_bytes_fetched": 0,
        "cells": [],
        "exclusions": {},
    }


def _failure(exc: Exception) -> dict[str, str]:
    return {
        "code": str(getattr(exc, "code", "SOURCE_CATALOG_FAILED"))[:100],
        "message": str(getattr(exc, "message", str(exc)))[:500],
    }


def _append_failure(job: SourceSyncJob, failure: dict[str, str]) -> None:
    failures = list(job.failures or [])
    failures.append(failure)
    job.failures = failures[-1_000:]


def _clear_lease(job: SourceSyncJob) -> None:
    job.lease_owner = None
    job.lease_expires_at = None


def _load_job(session: Session, job_id: str, owner_id: str) -> SourceSyncJob:
    job = session.scalar(
        select(SourceSyncJob).where(SourceSyncJob.id == job_id, SourceSyncJob.owner_id == owner_id)
    )
    if job is None:
        raise SourceError("NOT_FOUND", "recent catalog job not found")
    return job


def _reserve_job(
    session: Session,
    *,
    owner_id: str,
    settings: Settings,
    idempotency_key: str,
) -> tuple[SourceSyncJob, bool]:
    if settings.source_mode != "sync" or not settings.source_network_enabled:
        raise SourceError("SOURCE_DISABLED", "recent KЕГЭ catalog sync is disabled")
    policy = ensure_source_policy(session, settings)
    if policy.permission_status not in {"private_authorized", "public_authorized"}:
        raise SourceError("SOURCE_RIGHTS_REQUIRED", "source policy has not authorized learner visibility")
    key = bounded_text(idempotency_key, 255, "Idempotency-Key")
    urls = _number_urls()
    request_hash = _request_hash(urls)
    existing = session.scalar(
        select(SourceSyncJob).where(
            SourceSyncJob.owner_id == owner_id,
            SourceSyncJob.idempotency_key == key,
        )
    )
    if existing is not None:
        if existing.request_hash != request_hash:
            raise SourceError("IDEMPOTENCY_CONFLICT", "idempotency key describes another source request")
        return existing, True

    job = SourceSyncJob(
        owner_id=owner_id,
        source_policy_id=policy.id,
        mode=RECENT_CATALOG_MODE,
        request_hash=request_hash,
        manifest_hash=request_hash,
        parser_version=RECENT_CATALOG_PARSER_VERSION,
        status="queued",
        max_items=MAX_SELECTED_TASKS,
        target_exam_year=2027,
        cursor_index=0,
        total_items=NUMBER_COUNT,
        counters=_base_counters(),
        failures=[],
        idempotency_key=key,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(job)
    session.flush()
    session.add_all(
        [
            SourceSyncItem(
                job_id=job.id,
                ordinal=index,
                canonical_url=url,
                status="pending",
                attempts=0,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            for index, url in enumerate(urls)
        ]
    )
    session.flush()
    return job, False


def _prepare_run(
    session: Session,
    *,
    owner_id: str,
    settings: Settings,
    idempotency_key: str,
    resume: bool,
    worker_id: str,
) -> tuple[SourceSyncJob, bool]:
    with session.begin():
        job, duplicate = _reserve_job(
            session,
            owner_id=owner_id,
            settings=settings,
            idempotency_key=idempotency_key,
        )
    if duplicate and job.status in {"succeeded", "partial"} and bool((job.counters or {}).get("published")):
        return job, True
    if duplicate and not resume:
        raise SourceError("SOURCE_RESUME_REQUIRED", "unfinished recent catalog job requires --resume")

    with session.begin():
        job = _load_job(session, job.id, owner_id)
        if (
            job.status == "running"
            and job.lease_expires_at is not None
            and job.lease_expires_at > utcnow()
            and job.lease_owner != worker_id
        ):
            raise SourceError("SOURCE_JOB_BUSY", "recent catalog job is leased by another worker")
        for item in session.scalars(
            select(SourceSyncItem).where(SourceSyncItem.job_id == job.id).order_by(SourceSyncItem.ordinal)
        ):
            item.status = "pending"
            item.response_hash = None
            item.last_error = None
            item.updated_at = utcnow()
        job.status = "queued"
        job.cursor_index = 0
        job.counters = _base_counters()
        job.failures = []
        _clear_lease(job)
        job.updated_at = utcnow()
    return job, False


def _fetch_all_numbers(
    session: Session,
    *,
    job_id: str,
    owner_id: str,
    client: Any,
    worker_id: str,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for ordinal in range(NUMBER_COUNT):
        with session.begin():
            job = _load_job(session, job_id, owner_id)
            item = session.scalar(
                select(SourceSyncItem).where(
                    SourceSyncItem.job_id == job.id,
                    SourceSyncItem.ordinal == ordinal,
                )
            )
            if item is None:
                raise SourceError("SOURCE_NUMBER_INCOMPLETE", "catalog job is missing a number checkpoint")
            item.status = "fetching"
            item.attempts += 1
            item.last_error = None
            item.updated_at = utcnow()
            job.status = "running"
            job.lease_owner = worker_id
            job.lease_expires_at = utcnow() + timedelta(seconds=LEASE_SECONDS)
            job.updated_at = utcnow()
            url = item.canonical_url
            item_id = item.id
        try:
            for request_attempt in range(3):
                try:
                    raw = client.fetch_json_array(url, api_endpoint=True)
                    break
                except SourceFetchError as exc:
                    if exc.code != "SOURCE_FETCH_FAILED" or request_attempt == 2:
                        raise
                    # Retry only transient transport failures. The production
                    # client spaces requests; validation and policy failures
                    # are never retried. Each additional attempt is durable.
                    with session.begin():
                        retry_item = session.get(SourceSyncItem, item_id)
                        if retry_item is not None:
                            retry_item.attempts += 1
            if not isinstance(raw, list):
                raise SourceFetchError("SOURCE_RESPONSE_INVALID", "number endpoint response must be an array")
            normalized = _normalise_number_payload(raw, url, max_tasks=MAX_RECORDS_PER_NUMBER)
            eligible, gate_failures = _eligible_number_tasks(raw, normalized)
            normalization_failures = [_mapping_failure(value) for value in normalized.failures]
            endpoint_failures = [*normalization_failures, *gate_failures]
        except (SourceFetchError, SourceError, ValueError) as exc:
            with session.begin():
                job = _load_job(session, job_id, owner_id)
                stored_item = session.get(SourceSyncItem, item_id)
                if stored_item is not None:
                    stored_item.status = "failed"
                    stored_item.last_error = _failure(exc)["message"]
                    stored_item.updated_at = utcnow()
                counters = dict(job.counters or {})
                counters["numbers_requested"] = int(counters.get("numbers_requested", 0)) + 1
                job.counters = counters
                job.updated_at = utcnow()
            raise

        candidates.extend(eligible)
        with session.begin():
            job = _load_job(session, job_id, owner_id)
            stored_item = session.get(SourceSyncItem, item_id)
            if stored_item is not None:
                stored_item.status = "succeeded"
                stored_item.response_hash = sha256_json(
                    {
                        "url": url,
                        "records": [sha256_json(value) for value in eligible],
                    }
                )
                stored_item.updated_at = utcnow()
            counters = dict(job.counters or {})
            counters["numbers_requested"] = int(counters.get("numbers_requested", 0)) + 1
            counters["numbers_completed"] = int(counters.get("numbers_completed", 0)) + 1
            counters["tasks_seen"] = int(counters.get("tasks_seen", 0)) + len(raw)
            counters["tasks_eligible"] = int(counters.get("tasks_eligible", 0)) + len(eligible)
            job.counters = counters
            for failure in endpoint_failures:
                _append_failure(job, failure)
            job.cursor_index = ordinal + 1
            job.lease_expires_at = utcnow() + timedelta(seconds=LEASE_SECONDS)
            job.updated_at = utcnow()
    return candidates


def _mark_failed(session: Session, *, job_id: str, owner_id: str, exc: Exception) -> None:
    if session.in_transaction():
        session.rollback()
    with session.begin():
        job = _load_job(session, job_id, owner_id)
        job.status = "failed"
        counters = dict(job.counters or {})
        counters["published"] = False
        counters["attachment_bytes_fetched"] = 0
        job.counters = counters
        _append_failure(job, _failure(exc))
        _clear_lease(job)
        job.updated_at = utcnow()


def _publish(
    session: Session,
    *,
    job_id: str,
    owner_id: str,
    settings: Settings,
    selection: CatalogSelection,
) -> SourceSyncJob:
    if selection.total_selected == 0:
        raise SourceError("SOURCE_CATALOG_EMPTY", "recent source catalog contains no eligible tasks")
    manifest_hash = sha256_json(
        {
            "policy_version": POLICY_VERSION,
            "selected": [sha256_json(item) for item in selection.selected],
            "cells": selection.cells,
        }
    )
    with session.begin():
        job = _load_job(session, job_id, owner_id)
        checkpoints = list(
            session.scalars(
                select(SourceSyncItem).where(SourceSyncItem.job_id == job.id).order_by(SourceSyncItem.ordinal)
            )
        )
        if len(checkpoints) != NUMBER_COUNT or any(item.status != "succeeded" for item in checkpoints):
            raise SourceError("SOURCE_NUMBER_INCOMPLETE", "all 27 number endpoints must succeed before publication")
        policy = ensure_source_policy(session, settings)
        revisions_created = 0
        duplicate_revisions = 0
        for payload in selection.selected:
            source_id = str(payload["source_id"])
            _task, revision, _assets, duplicate = import_fixture(
                session,
                owner_id=owner_id,
                settings=settings,
                payload=payload,
                idempotency_key=f"{job.id}:catalog:{source_id}",
                admission_mode="remote",
                admitted_rights_status=policy.permission_status,
                parser_version=RECENT_CATALOG_PARSER_VERSION,
                target_exam_year=2027,
            )
            session.add(CatalogEntry(job_id=job.id, revision_id=revision.id))
            revisions_created += 0 if duplicate else 1
            duplicate_revisions += 1 if duplicate else 0

        counters = dict(job.counters or {})
        counters.update(
            {
                "published": True,
                "policy_version": POLICY_VERSION,
                "primary_years": list(PRIMARY_YEARS),
                "fallback_year": FALLBACK_YEAR,
                "fallback_threshold": FALLBACK_THRESHOLD,
                "target_per_cell": TARGET_PER_CELL,
                "tasks_selected": selection.total_selected,
                "total_selected": selection.total_selected,
                "revisions_created": revisions_created,
                "duplicate_revisions": duplicate_revisions,
                "quota_met": selection.quota_met,
                "attachment_bytes_fetched": 0,
                "cells": selection.cells,
                "exclusions": selection.exclusions,
            }
        )
        job.counters = counters
        job.manifest_hash = manifest_hash
        job.status = "succeeded" if selection.quota_met else "partial"
        job.cursor_index = NUMBER_COUNT
        if not selection.quota_met:
            _append_failure(
                job,
                {
                    "code": "SOURCE_QUOTA_UNREACHED",
                    "message": "official recent source does not provide 50 eligible tasks in every cell",
                },
            )
        _clear_lease(job)
        job.updated_at = utcnow()
        append_event(
            session,
            owner_id=owner_id,
            aggregate_type="source_sync_job",
            aggregate_id=job.id,
            event_type="recent_catalog_published",
            payload={
                "manifest_hash": manifest_hash,
                "total_selected": selection.total_selected,
                "quota_met": selection.quota_met,
                "policy_version": POLICY_VERSION,
            },
            idempotency_key=f"recent-catalog:{job.id}:published",
        )
    return job


def run_recent_catalog(
    session: Session,
    *,
    owner_id: str,
    settings: Settings,
    client: Any,
    idempotency_key: str,
    resume: bool = False,
    worker_id: str = "recent-catalog-cli",
) -> SourceSyncJob:
    """Fetch all number endpoints and atomically publish the selected edition."""

    job, replayed = _prepare_run(
        session,
        owner_id=owner_id,
        settings=settings,
        idempotency_key=idempotency_key,
        resume=resume,
        worker_id=worker_id,
    )
    if replayed:
        return job
    try:
        candidates = _fetch_all_numbers(
            session,
            job_id=job.id,
            owner_id=owner_id,
            client=client,
            worker_id=worker_id,
        )
        selection = select_recent_catalog(candidates)
        return _publish(
            session,
            job_id=job.id,
            owner_id=owner_id,
            settings=settings,
            selection=selection,
        )
    except Exception as exc:
        _mark_failed(session, job_id=job.id, owner_id=owner_id, exc=exc)
        raise


def _summary(job: SourceSyncJob) -> dict[str, Any]:
    counters = dict(job.counters or {})
    raw_cells = counters.get("cells")
    cells: list[Any] = raw_cells if isinstance(raw_cells, list) else []
    shortage_cells = 0
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        shortfall = cell.get("shortfall")
        if isinstance(shortfall, int) and not isinstance(shortfall, bool) and shortfall > 0:
            shortage_cells += 1
    return {
        "job_id": job.id,
        "status": job.status,
        "published": bool(counters.get("published")),
        "total_selected": int(counters.get("total_selected", 0)),
        "quota_met": bool(counters.get("quota_met")),
        "shortage_cells": shortage_cells,
        "attachment_bytes_fetched": int(counters.get("attachment_bytes_fetched", 0)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import the reviewed recent KЕГЭ difficulty catalog")
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    engine = make_engine(settings.database_url)
    init_schema(engine)
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            with session.begin():
                ensure_owner(session, settings.owner_id)
            client = BoundedKegeClient(settings, min_request_interval_seconds=2.0, timeout_seconds=30.0)
            job = run_recent_catalog(
                session,
                owner_id=settings.owner_id,
                settings=settings,
                client=client,
                idempotency_key=args.idempotency_key,
                resume=args.resume,
            )
            print(json.dumps(_summary(job), ensure_ascii=False, sort_keys=True))
        return 0 if job.status in {"succeeded", "partial"} else 2
    except (SourceError, SourceFetchError, CatalogPolicyError, AssetError, EventConflict, ValueError) as exc:
        print(json.dumps(_failure(exc), ensure_ascii=False, sort_keys=True))
        return 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())

"""Polite, resumable discovery of a large КЕГЭ variant catalog.

The command uses only the configured random-variant JSON endpoint.  It never
calls an attachment URL: every linked file remains a metadata-only Asset until
an explicit learner download or runner-use preflight requests it.
"""

from __future__ import annotations

import argparse
import json
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session

from .auth import ensure_owner
from .config import Settings
from .db import init_schema, make_engine, make_session_factory
from .events import EventConflict
from .models import SourceSyncItem, SourceSyncJob, Task, TaskRevision, utcnow
from .source_catalog import (
    SOURCE_NAME,
    SourceError,
    canonical_source_url,
    ensure_source_policy,
    import_fixture,
)
from .source_transport import (
    BoundedKegeClient,
    SourceFetchError,
    _normalise_number_payload,
    _normalise_variant_payload,
)
from .util import bounded_text, sha256_json

DISCOVERY_PARSER_VERSION = "kompege-variant-discovery-v1"
NUMBER_DISCOVERY_PARSER_VERSION = "kompege-number-discovery-v2"
DISCOVERY_LEASE_SECONDS = 60
DEFAULT_TARGET_EXAM_YEAR = 2027
DEFAULT_MAX_UNIQUE_TASKS = 1000
DEFAULT_MAX_VARIANTS = 1000
DEFAULT_PLATEAU_LIMIT = 3
NUMBER_LINE_COUNT = 27
NUMBER_API_TEMPLATE = "https://kompege.ru/api/v1/task/number/{number}"
NUMBER_REQUIRED_ASSET_LINES = frozenset({3, 9, 17, 18, 22, 23, 24, 26, 27})


def _validate_year(value: int) -> None:
    if isinstance(value, bool) or not 2000 <= value <= 2100:
        raise SourceError("YEAR_INVALID", "target_exam_year must be between 2000 and 2100")


def _discovery_hash(
    variant_url: str,
    *,
    target_exam_year: int,
    max_unique_tasks: int,
    max_variants: int,
    plateau_limit: int,
) -> str:
    return sha256_json(
        {
            "schema_version": 1,
            "source": SOURCE_NAME,
            "mode": "variant-discovery",
            "parser_version": DISCOVERY_PARSER_VERSION,
            "variant_url": variant_url,
            "target_exam_year": target_exam_year,
            "max_unique_tasks": max_unique_tasks,
            "max_variants": max_variants,
            "plateau_limit": plateau_limit,
        }
    )


def reserve_variant_discovery_job(
    session: Session,
    *,
    owner_id: str,
    settings: Settings,
    idempotency_key: str,
    target_exam_year: int = DEFAULT_TARGET_EXAM_YEAR,
    max_unique_tasks: int = DEFAULT_MAX_UNIQUE_TASKS,
    max_variants: int = DEFAULT_MAX_VARIANTS,
    plateau_limit: int = DEFAULT_PLATEAU_LIMIT,
) -> tuple[SourceSyncJob, bool]:
    """Reserve the durable discovery stream before its first network call."""

    _validate_year(target_exam_year)
    if not 1 <= max_unique_tasks <= DEFAULT_MAX_UNIQUE_TASKS:
        raise SourceError("SOURCE_BATCH_INVALID", "max_unique_tasks must be between 1 and 1000")
    if not 1 <= max_variants <= 10_000:
        raise SourceError("SOURCE_BATCH_INVALID", "max_variants must be between 1 and 10000")
    if not 1 <= plateau_limit <= 100:
        raise SourceError("SOURCE_BATCH_INVALID", "plateau_limit must be between 1 and 100")
    if settings.source_mode != "sync" or not settings.source_network_enabled:
        raise SourceError("SOURCE_DISABLED", "КЕГЭ variant discovery is disabled")
    configured_url = settings.source_variant_api_url
    if not configured_url:
        raise SourceError("SOURCE_API_UNVERIFIED", "variant endpoint is not owner-configured")
    variant_url = canonical_source_url(configured_url, api_endpoint=True)
    policy = ensure_source_policy(session, settings)
    if policy.permission_status not in {"private_authorized", "public_authorized"}:
        raise SourceError("SOURCE_RIGHTS_REQUIRED", "source policy has not authorized learner visibility")
    key = bounded_text(idempotency_key, 255, "Idempotency-Key")
    request_hash = _discovery_hash(
        variant_url,
        target_exam_year=target_exam_year,
        max_unique_tasks=max_unique_tasks,
        max_variants=max_variants,
        plateau_limit=plateau_limit,
    )
    existing = session.scalar(
        select(SourceSyncJob).where(
            SourceSyncJob.owner_id == owner_id,
            SourceSyncJob.idempotency_key == key,
        )
    )
    if existing is not None:
        if existing.request_hash != request_hash:
            raise SourceError("IDEMPOTENCY_CONFLICT", "idempotency key already describes another discovery request")
        return existing, True

    job = SourceSyncJob(
        owner_id=owner_id,
        source_policy_id=policy.id,
        mode="variant-discovery",
        request_hash=request_hash,
        manifest_hash=request_hash,
        parser_version=DISCOVERY_PARSER_VERSION,
        status="queued",
        max_items=max_variants,
        target_exam_year=target_exam_year,
        cursor_index=0,
        total_items=max_unique_tasks,
        counters={
            "variants_requested": 0,
            "tasks_seen": 0,
            "tasks_admitted": 0,
            "unique_tasks": 0,
            "target_unique_tasks": max_unique_tasks,
            "variant_attempt_limit": max_variants,
            "plateau_limit": plateau_limit,
            "plateau_streak": 0,
            "failures": 0,
            "attachment_bytes_fetched": 0,
            "seen_source_ids": [],
        },
        failures=[],
        idempotency_key=key,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(job)
    session.flush()
    session.add(
        SourceSyncItem(
            job_id=job.id,
            ordinal=0,
            canonical_url=variant_url,
            status="pending",
            attempts=0,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
    )
    session.flush()
    return job, False


def _failure(exc: Exception, *, source_id: str | None = None) -> dict[str, str]:
    result = {
        "code": str(getattr(exc, "code", "SOURCE_IMPORT_FAILED")),
        "message": str(getattr(exc, "message", "source discovery failed"))[:500],
    }
    if source_id is not None:
        result["source_id"] = source_id
    return result


def _load_discovery_job(session: Session, job_id: str, owner_id: str) -> SourceSyncJob:
    job = session.scalar(select(SourceSyncJob).where(SourceSyncJob.id == job_id, SourceSyncJob.owner_id == owner_id))
    if job is None:
        raise SourceError("NOT_FOUND", "source discovery job not found")
    return job


def _clear_lease(job: SourceSyncJob) -> None:
    job.lease_owner = None
    job.lease_expires_at = None


def _append_failure(job: SourceSyncJob, failure: dict[str, str]) -> None:
    failures = list(job.failures or [])
    failures.append(failure)
    job.failures = failures[-1000:]
    counters = dict(job.counters or {})
    counters["failures"] = len(job.failures)
    job.counters = counters


def run_variant_discovery(
    session: Session,
    *,
    owner_id: str,
    settings: Settings,
    client: BoundedKegeClient,
    idempotency_key: str,
    target_exam_year: int = DEFAULT_TARGET_EXAM_YEAR,
    max_unique_tasks: int = DEFAULT_MAX_UNIQUE_TASKS,
    max_variants: int = DEFAULT_MAX_VARIANTS,
    plateau_limit: int = DEFAULT_PLATEAU_LIMIT,
    work_limit: int = DEFAULT_MAX_VARIANTS,
    resume: bool = False,
    worker_id: str = "variant-discovery-cli",
) -> SourceSyncJob:
    """Run bounded variant requests, checkpointing after each response.

    A repeated idempotency key resumes a queued/paused job without issuing a
    second request for any already-admitted task. A terminal plateau is only
    resumed when the caller explicitly passes ``resume=True``.
    """

    if not 1 <= work_limit <= 10_000:
        raise SourceError("SOURCE_BATCH_INVALID", "work_limit must be between 1 and 10000")
    job, duplicate = reserve_variant_discovery_job(
        session,
        owner_id=owner_id,
        settings=settings,
        idempotency_key=idempotency_key,
        target_exam_year=target_exam_year,
        max_unique_tasks=max_unique_tasks,
        max_variants=max_variants,
        plateau_limit=plateau_limit,
    )
    if session.in_transaction():
        session.commit()

    if duplicate:
        with session.begin():
            job = _load_discovery_job(session, job.id, owner_id)
            now = utcnow()
            if job.status == "succeeded":
                return job
            if job.status == "partial" and not resume:
                return job
            if (
                job.status == "running"
                and job.lease_expires_at is not None
                and job.lease_expires_at > now
                and job.lease_owner != worker_id
            ):
                raise SourceError("SOURCE_JOB_BUSY", "source discovery job is leased by another worker")
            if job.status == "partial" and resume:
                counters = dict(job.counters or {})
                if int(counters.get("variants_requested", 0)) >= int(counters.get("variant_attempt_limit", 0)):
                    return job
                counters["plateau_streak"] = 0
                job.counters = counters
                job.failures = [
                    failure
                    for failure in list(job.failures or [])
                    if not isinstance(failure, dict) or failure.get("code") != "SOURCE_PLATEAU"
                ]
                job.status = "queued"
            if job.status == "running" and (job.lease_expires_at is None or job.lease_expires_at <= now):
                job.status = "queued"
                _clear_lease(job)
            job.updated_at = now

    processed = 0
    while processed < work_limit:
        if session.in_transaction():
            session.commit()
        with session.begin():
            job = _load_discovery_job(session, job.id, owner_id)
            counters = dict(job.counters or {})
            unique_count = int(counters.get("unique_tasks", 0))
            attempts = int(counters.get("variants_requested", 0))
            current_plateau = int(counters.get("plateau_streak", 0))
            if job.status in {"succeeded", "partial"}:
                break
            if unique_count >= max_unique_tasks:
                job.status = "succeeded"
                _clear_lease(job)
                job.updated_at = utcnow()
                break
            if attempts >= max_variants:
                job.status = "partial"
                _append_failure(job, {"code": "SOURCE_ATTEMPT_LIMIT", "message": "variant attempt limit reached"})
                _clear_lease(job)
                job.updated_at = utcnow()
                break
            if current_plateau >= plateau_limit:
                job.status = "partial"
                _append_failure(
                    job, {"code": "SOURCE_PLATEAU", "message": "variant discovery reached a duplicate plateau"}
                )
                _clear_lease(job)
                job.updated_at = utcnow()
                break
            item = session.scalar(
                select(SourceSyncItem).where(SourceSyncItem.job_id == job.id, SourceSyncItem.ordinal == 0)
            )
            if item is None:
                raise SourceError("SOURCE_STATE_INVALID", "variant discovery checkpoint is missing")
            job.status = "running"
            job.lease_owner = worker_id
            job.lease_expires_at = utcnow() + timedelta(seconds=DISCOVERY_LEASE_SECONDS)
            item.status = "fetching"
            item.attempts = int(item.attempts or 0) + 1
            item.last_error = None
            item.updated_at = utcnow()
            job.updated_at = utcnow()
            variant_url = item.canonical_url

        try:
            raw = client.fetch_json(variant_url, api_endpoint=True)
            normalized = _normalise_variant_payload(raw, variant_url, max_tasks=27)
        except (SourceFetchError, SourceError, ValueError) as exc:
            with session.begin():
                job = _load_discovery_job(session, job.id, owner_id)
                item = session.scalar(
                    select(SourceSyncItem).where(SourceSyncItem.job_id == job.id, SourceSyncItem.ordinal == 0)
                )
                counters = dict(job.counters or {})
                counters["variants_requested"] = int(counters.get("variants_requested", 0)) + 1
                job.counters = counters
                _append_failure(job, _failure(exc))
                job.status = "paused"
                _clear_lease(job)
                if item is not None:
                    item.status = "pending"
                    item.last_error = _failure(exc)["message"]
                    item.updated_at = utcnow()
                job.updated_at = utcnow()
            processed += 1
            continue

        with session.begin():
            job = _load_discovery_job(session, job.id, owner_id)
            policy = ensure_source_policy(session, settings)
            counters = dict(job.counters or {})
            seen = {str(item) for item in counters.get("seen_source_ids", []) if isinstance(item, str)}
            normalization_failures = list(getattr(normalized, "failures", []))
            for failure in normalization_failures:
                _append_failure(job, failure)
            new_unique = 0
            admitted = 0
            for payload in normalized:
                source_id = str(payload["source_id"])
                # A random variant may contain more new tasks than the
                # remaining target. Admit only the exact remainder so the
                # durable unique counter can never overshoot 1,000.
                if source_id not in seen and len(seen) >= max_unique_tasks:
                    continue
                try:
                    with session.begin_nested():
                        _task, _revision, _assets, _duplicate = import_fixture(
                            session,
                            owner_id=owner_id,
                            settings=settings,
                            payload=payload,
                            idempotency_key=f"{idempotency_key}:task:{source_id}",
                            admission_mode="remote",
                            admitted_rights_status=policy.permission_status,
                            parser_version=DISCOVERY_PARSER_VERSION,
                            target_exam_year=target_exam_year,
                        )
                    admitted += 1
                    if source_id not in seen:
                        seen.add(source_id)
                        new_unique += 1
                except (SourceError, EventConflict, ValueError) as exc:
                    _append_failure(job, _failure(exc, source_id=source_id))
            counters["variants_requested"] = int(counters.get("variants_requested", 0)) + 1
            counters["tasks_seen"] = int(counters.get("tasks_seen", 0)) + len(normalized) + len(normalization_failures)
            counters["tasks_admitted"] = int(counters.get("tasks_admitted", 0)) + admitted
            counters["seen_source_ids"] = sorted(seen)
            counters["unique_tasks"] = len(seen)
            counters["plateau_streak"] = int(counters.get("plateau_streak", 0)) + 1 if new_unique == 0 else 0
            counters["attachment_bytes_fetched"] = 0
            counters["failures"] = len(job.failures or [])
            job.counters = counters
            job.cursor_index = len(seen)
            item = session.scalar(
                select(SourceSyncItem).where(SourceSyncItem.job_id == job.id, SourceSyncItem.ordinal == 0)
            )
            if item is not None:
                item.status = "succeeded" if len(seen) >= max_unique_tasks else "pending"
                item.updated_at = utcnow()
            if len(seen) >= max_unique_tasks:
                job.status = "succeeded"
            elif int(counters["plateau_streak"]) >= plateau_limit:
                job.status = "partial"
                _append_failure(
                    job, {"code": "SOURCE_PLATEAU", "message": "variant discovery reached a duplicate plateau"}
                )
            elif int(counters["variants_requested"]) >= max_variants:
                job.status = "partial"
                _append_failure(job, {"code": "SOURCE_ATTEMPT_LIMIT", "message": "variant attempt limit reached"})
            else:
                job.status = "paused"
            _clear_lease(job)
            job.updated_at = utcnow()
        processed += 1

    if session.in_transaction():
        session.commit()
    with session.begin():
        job = _load_discovery_job(session, job.id, owner_id)
        if job.status == "running":
            job.status = "paused"
            _clear_lease(job)
            job.updated_at = utcnow()
    return job


def _number_urls(number_api_template: str) -> list[str]:
    """Render and revalidate the one supported number-catalog template.

    The template is intentionally a CLI/operator input, not a browser input.
    Keeping the accepted form exact prevents a seemingly harmless ``{id}``
    task endpoint from being repurposed as a bulk crawler.
    """

    if not isinstance(number_api_template, str) or number_api_template.strip() != NUMBER_API_TEMPLATE:
        raise SourceError(
            "SOURCE_API_UNVERIFIED",
            "number_api_template must be the reviewed КЕГЭ number endpoint template",
        )
    urls: list[str] = []
    for line in range(1, NUMBER_LINE_COUNT + 1):
        rendered = number_api_template.format(number=line)
        canonical = canonical_source_url(rendered, api_endpoint=True)
        expected = f"https://kompege.ru/api/v1/task/number/{line}"
        if canonical != expected:
            raise SourceError("SOURCE_PATH_BLOCKED", "number endpoint rendered outside the approved path")
        urls.append(canonical)
    return urls


def _number_discovery_hash(urls: list[str], *, target_exam_year: int, max_unique_tasks: int) -> str:
    return sha256_json(
        {
            "schema_version": 1,
            "source": SOURCE_NAME,
            "mode": "number-discovery",
            "parser_version": NUMBER_DISCOVERY_PARSER_VERSION,
            "urls": urls,
            "target_exam_year": target_exam_year,
            "max_unique_tasks": max_unique_tasks,
        }
    )


def _answer_is_usable(value: Any) -> bool:
    """Require a bounded scalar answer before admitting a scorable task."""

    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return isinstance(value, (int, float))


def _current_number_catalog(session: Session, *, target_exam_year: int) -> tuple[set[str], dict[int, int]]:
    """Read only current, learner-visible, scorable KЕГЭ revisions."""

    rows = session.execute(
        select(Task.external_id, TaskRevision.line_number, TaskRevision.answer)
        .join(TaskRevision, TaskRevision.id == Task.current_revision_id)
        .where(
            Task.source_name == SOURCE_NAME,
            TaskRevision.target_exam_year == target_exam_year,
            TaskRevision.rights_status.in_(["private_authorized", "public_authorized"]),
        )
    ).all()
    source_ids: set[str] = set()
    line_counts: dict[int, int] = {}
    for source_id, line_number, answer in rows:
        if not _answer_is_usable(answer):
            continue
        source_key = str(source_id)
        source_ids.add(source_key)
        line = int(line_number)
        line_counts[line] = line_counts.get(line, 0) + 1
    return source_ids, line_counts


def _line_targets(max_unique_tasks: int) -> dict[int, int]:
    base, remainder = divmod(max_unique_tasks, NUMBER_LINE_COUNT)
    return {line: base + (1 if line <= remainder else 0) for line in range(1, NUMBER_LINE_COUNT + 1)}


def reserve_number_discovery_job(
    session: Session,
    *,
    owner_id: str,
    settings: Settings,
    idempotency_key: str,
    number_api_template: str,
    target_exam_year: int = DEFAULT_TARGET_EXAM_YEAR,
    max_unique_tasks: int = DEFAULT_MAX_UNIQUE_TASKS,
) -> tuple[SourceSyncJob, bool]:
    """Reserve a durable 27-line number discovery before upstream I/O."""

    _validate_year(target_exam_year)
    if isinstance(max_unique_tasks, bool) or not 1 <= max_unique_tasks <= DEFAULT_MAX_UNIQUE_TASKS:
        raise SourceError("SOURCE_BATCH_INVALID", "max_unique_tasks must be between 1 and 1000")
    urls = _number_urls(number_api_template)
    if settings.source_mode != "sync" or not settings.source_network_enabled:
        raise SourceError("SOURCE_DISABLED", "КЕГЭ number discovery is disabled")
    policy = ensure_source_policy(session, settings)
    if policy.permission_status not in {"private_authorized", "public_authorized"}:
        raise SourceError("SOURCE_RIGHTS_REQUIRED", "source policy has not authorized learner visibility")
    key = bounded_text(idempotency_key, 255, "Idempotency-Key")
    request_hash = _number_discovery_hash(urls, target_exam_year=target_exam_year, max_unique_tasks=max_unique_tasks)
    existing = session.scalar(
        select(SourceSyncJob).where(
            SourceSyncJob.owner_id == owner_id,
            SourceSyncJob.idempotency_key == key,
        )
    )
    if existing is not None:
        if existing.request_hash != request_hash:
            raise SourceError("IDEMPOTENCY_CONFLICT", "idempotency key already describes another number catalog")
        return existing, True

    baseline_ids, baseline_lines = _current_number_catalog(session, target_exam_year=target_exam_year)
    targets = _line_targets(max_unique_tasks)
    job = SourceSyncJob(
        owner_id=owner_id,
        source_policy_id=policy.id,
        mode="number-discovery",
        request_hash=request_hash,
        manifest_hash=request_hash,
        parser_version=NUMBER_DISCOVERY_PARSER_VERSION,
        status="queued",
        max_items=DEFAULT_MAX_UNIQUE_TASKS,
        target_exam_year=target_exam_year,
        cursor_index=0,
        total_items=NUMBER_LINE_COUNT,
        counters={
            "numbers_requested": 0,
            "numbers_completed": 0,
            "lines_visited": 0,
            "tasks_seen": 0,
            "tasks_eligible": 0,
            "tasks_skipped_hidden": 0,
            "tasks_skipped_unscorable": 0,
            "tasks_admitted": 0,
            "revisions_created": 0,
            "baseline_existing": len(baseline_ids),
            "newly_admitted": 0,
            "current_total": len(baseline_ids),
            "target_unique_tasks": max_unique_tasks,
            "attachment_bytes_fetched": 0,
            "line_targets": {str(line): count for line, count in targets.items()},
            "line_counts": {str(line): count for line, count in baseline_lines.items()},
            "seen_source_ids": sorted(baseline_ids),
            "selection_mode": "fair-round-robin",
        },
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
                ordinal=ordinal,
                canonical_url=url,
                status="pending",
                attempts=0,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            for ordinal, url in enumerate(urls)
        ]
    )
    session.flush()
    return job, False


def _claim_number_item(session: Session, job_id: str, worker_id: str) -> SourceSyncItem | None:
    """Atomically claim the next number endpoint and reclaim stale leases."""

    now = utcnow()
    stale_before = now - timedelta(seconds=DISCOVERY_LEASE_SECONDS)
    session.execute(
        update(SourceSyncItem)
        .where(
            SourceSyncItem.job_id == job_id,
            SourceSyncItem.status == "fetching",
            SourceSyncItem.updated_at < stale_before,
        )
        .values(status="pending", last_error="worker lease expired", updated_at=now)
    )
    candidate_id = session.scalar(
        select(SourceSyncItem.id)
        .where(SourceSyncItem.job_id == job_id, SourceSyncItem.status == "pending")
        .order_by(SourceSyncItem.ordinal, SourceSyncItem.id)
        .limit(1)
    )
    if candidate_id is None:
        return None
    claimed_id = session.scalar(
        update(SourceSyncItem)
        .where(and_(SourceSyncItem.id == candidate_id, SourceSyncItem.status == "pending"))
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
    job = session.get(SourceSyncJob, job_id)
    if item is None or job is None:
        return None
    job.status = "running"
    job.lease_owner = worker_id
    job.lease_expires_at = now + timedelta(seconds=DISCOVERY_LEASE_SECONDS)
    job.updated_at = now
    session.flush()
    return item


def _fetch_number_array(client: Any, url: str) -> list[Any]:
    """Use the explicit array protocol, with a compatibility read-only fallback."""

    fetch_array = getattr(client, "fetch_json_array", None)
    if callable(fetch_array):
        raw = fetch_array(url, api_endpoint=True)
    else:
        fetch_json = getattr(client, "fetch_json", None)
        if not callable(fetch_json):
            raise SourceFetchError("SOURCE_FETCH_PROTOCOL", "source client lacks the JSON array protocol")
        raw = fetch_json(url, api_endpoint=True)
    if not isinstance(raw, list):
        raise SourceFetchError("SOURCE_RESPONSE_INVALID", "number endpoint response must be a JSON array")
    return raw


def _raw_number_source_ids(raw: list[Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in raw:
        candidate = item.get("task") if isinstance(item, dict) and isinstance(item.get("task"), dict) else item
        if not isinstance(candidate, dict):
            continue
        source_id = candidate.get("taskId")
        if source_id is None:
            source_id = candidate.get("external_id")
        if source_id is None:
            source_id = candidate.get("id")
        if source_id is not None:
            result[str(source_id)] = candidate
        sub_tasks = candidate.get("subTask")
        if not isinstance(sub_tasks, list):
            continue
        for index, child in enumerate(sub_tasks[:4]):
            if not isinstance(child, dict):
                continue
            child_id = child.get("taskId")
            if child_id is None:
                child_id = child.get("external_id")
            if child_id is None:
                child_id = child.get("id")
            if child_id is None and source_id is not None:
                child_id = f"{source_id}:sub:{index}"
            if child_id is not None:
                result[str(child_id)] = child
    return result


def _eligible_number_tasks(
    raw: list[Any], normalized: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Apply hidden/scorable gates after safe normalization, without logging data."""

    raw_by_id = _raw_number_source_ids(raw)
    eligible: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for item in normalized:
        source_id = str(item["source_id"])
        source = raw_by_id.get(source_id, {})
        if source.get("hide") is True:
            failures.append(
                {"code": "SOURCE_HIDDEN", "message": "hidden source record excluded", "source_id": source_id}
            )
            continue
        if not _answer_is_usable(item.get("answer")):
            failures.append(
                {"code": "SOURCE_UNSCORABLE", "message": "source record has no scalar answer", "source_id": source_id}
            )
            continue
        eligible.append(item)
    return eligible, failures


def _has_number_assets(item: dict[str, Any]) -> bool:
    assets = item.get("assets")
    if isinstance(assets, list) and assets:
        return True
    subtasks = item.get("subtasks")
    if not isinstance(subtasks, list):
        return False
    return any(
        isinstance(subtask, dict) and isinstance(subtask.get("assets"), list) and subtask["assets"]
        for subtask in subtasks
    )


def _select_number_tasks(
    batches: dict[int, list[dict[str, Any]]],
    *,
    seen_source_ids: set[str],
    line_counts: dict[int, int],
    max_unique_tasks: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Select a deterministic, fair, exact-size subset from all 27 lines."""

    by_id: dict[str, tuple[str, dict[str, Any]]] = {}
    failures: list[dict[str, str]] = []
    for line in sorted(batches):
        for item in batches[line]:
            source_id = str(item["source_id"])
            fingerprint = sha256_json(item)
            previous = by_id.get(source_id)
            if previous is not None:
                if previous[0] != fingerprint:
                    failures.append(
                        {
                            "code": "SOURCE_CONFLICTING_DUPLICATE",
                            "message": "duplicate source record has conflicting normalized content",
                            "source_id": source_id,
                        }
                    )
                continue
            by_id[source_id] = (fingerprint, item)

    grouped: dict[int, list[dict[str, Any]]] = {}
    for _fingerprint, item in by_id.values():
        source_id = str(item["source_id"])
        if source_id in seen_source_ids:
            continue
        line = int(item["line_number"])
        grouped.setdefault(line, []).append(item)

    def ordered(line: int) -> list[dict[str, Any]]:
        return sorted(
            grouped.get(line, []),
            key=lambda item: (
                0 if line in NUMBER_REQUIRED_ASSET_LINES and _has_number_assets(item) else 1,
                str(item["source_id"]),
            ),
        )

    remaining = max_unique_tasks - len(seen_source_ids)
    if remaining <= 0:
        return [], failures
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    selected_counts: dict[int, int] = {}

    available_lines = [line for line in sorted(grouped) if ordered(line)]
    # Coverage is intentionally performed before quota filling.  Required
    # attachment lines get the first candidate with metadata assets.
    available_lines.sort(
        key=lambda line: (
            0 if line in NUMBER_REQUIRED_ASSET_LINES and any(_has_number_assets(item) for item in ordered(line)) else 1,
            line,
        )
    )
    for line in available_lines:
        if len(selected) >= remaining or line_counts.get(line, 0) > 0:
            continue
        candidate = ordered(line)[0]
        selected.append(candidate)
        selected_ids.add(str(candidate["source_id"]))
        selected_counts[line] = selected_counts.get(line, 0) + 1

    targets = _line_targets(max_unique_tasks)
    for line in range(1, NUMBER_LINE_COUNT + 1):
        deficit = max(0, targets[line] - line_counts.get(line, 0) - selected_counts.get(line, 0))
        for candidate in ordered(line):
            if deficit <= 0 or len(selected) >= remaining:
                break
            source_id = str(candidate["source_id"])
            if source_id in selected_ids:
                continue
            selected.append(candidate)
            selected_ids.add(source_id)
            selected_counts[line] = selected_counts.get(line, 0) + 1
            deficit -= 1

    leftovers = [
        item for line in sorted(grouped) for item in ordered(line) if str(item["source_id"]) not in selected_ids
    ]
    leftovers.sort(
        key=lambda item: (
            0 if int(item["line_number"]) in NUMBER_REQUIRED_ASSET_LINES and _has_number_assets(item) else 1,
            int(item["line_number"]),
            str(item["source_id"]),
        )
    )
    for candidate in leftovers:
        if len(selected) >= remaining:
            break
        selected.append(candidate)
        selected_ids.add(str(candidate["source_id"]))
    return selected, failures


def _mapping_failure(value: Any) -> dict[str, str]:
    result = {
        "code": str(value.get("code", "SOURCE_TASK_INCOMPLETE"))[:100]
        if isinstance(value, dict)
        else "SOURCE_TASK_INCOMPLETE",
        "message": str(value.get("message", "source task could not be normalized"))[:500]
        if isinstance(value, dict)
        else "source task could not be normalized",
    }
    if isinstance(value, dict):
        for key in ("source_id", "index"):
            if value.get(key) is not None:
                result[key] = str(value[key])[:255]
    return result


def run_number_discovery(
    session: Session,
    *,
    owner_id: str,
    settings: Settings,
    client: Any,
    idempotency_key: str,
    number_api_template: str,
    target_exam_year: int = DEFAULT_TARGET_EXAM_YEAR,
    max_unique_tasks: int = DEFAULT_MAX_UNIQUE_TASKS,
    resume: bool = False,
    worker_id: str = "number-discovery-cli",
) -> SourceSyncJob:
    """Visit all 27 number routes, then admit a fair metadata-only subset."""

    job, duplicate = reserve_number_discovery_job(
        session,
        owner_id=owner_id,
        settings=settings,
        idempotency_key=idempotency_key,
        number_api_template=number_api_template,
        target_exam_year=target_exam_year,
        max_unique_tasks=max_unique_tasks,
    )
    if session.in_transaction():
        session.commit()
    job_id = job.id

    with session.begin():
        job = _load_discovery_job(session, job_id, owner_id)
        if duplicate and job.status == "succeeded":
            return job
        baseline_ids, baseline_lines = _current_number_catalog(session, target_exam_year=target_exam_year)
        counters = dict(job.counters or {})
        baseline_count = int(counters.get("baseline_existing", len(baseline_ids)))
        counters.update(
            {
                "baseline_existing": baseline_count,
                "current_total": len(baseline_ids),
                "seen_source_ids": sorted(baseline_ids),
                "line_counts": {str(line): count for line, count in baseline_lines.items()},
                "attachment_bytes_fetched": 0,
            }
        )
        job.counters = counters
        if len(baseline_ids) > max_unique_tasks:
            job.status = "partial"
            _append_failure(
                job,
                {
                    "code": "SOURCE_BASELINE_OVER_CAP",
                    "message": "existing catalog is already above the requested cap",
                },
            )
            _clear_lease(job)
            return job
        if duplicate and job.status == "partial" and not resume:
            return job
        if (
            duplicate
            and job.status == "running"
            and job.lease_expires_at is not None
            and job.lease_expires_at > utcnow()
            and job.lease_owner != worker_id
        ):
            raise SourceError("SOURCE_JOB_BUSY", "source number discovery job is leased by another worker")
        if duplicate and job.status == "partial" and resume:
            session.query(SourceSyncItem).filter(SourceSyncItem.job_id == job.id).update(
                {
                    "status": "pending",
                    "task_id": None,
                    "revision_id": None,
                    "response_hash": None,
                    "last_error": None,
                    "updated_at": utcnow(),
                },
                synchronize_session=False,
            )
            job.failures = []
            counters["numbers_requested"] = 0
            counters["numbers_completed"] = 0
            counters["lines_visited"] = 0
            counters["tasks_seen"] = 0
            counters["tasks_eligible"] = 0
            counters["tasks_skipped_hidden"] = 0
            counters["tasks_skipped_unscorable"] = 0
            counters["failures"] = 0
            job.counters = counters
        if duplicate and job.status == "running" and (job.lease_expires_at is None or job.lease_expires_at <= utcnow()):
            _clear_lease(job)
            job.status = "queued"
        job.updated_at = utcnow()

    batches: dict[int, list[dict[str, Any]]] = {}
    visited = 0
    while visited < NUMBER_LINE_COUNT:
        with session.begin():
            job = _load_discovery_job(session, job_id, owner_id)
            item = _claim_number_item(session, job.id, worker_id)
            if item is None:
                break
            ordinal = item.ordinal
            url = item.canonical_url
        try:
            raw = _fetch_number_array(client, url)
            normalized = _normalise_number_payload(raw, url, max_tasks=1_000)
            eligible, gate_failures = _eligible_number_tasks(raw, normalized)
            normalization_failures = list(getattr(normalized, "failures", []))
            failures = [_mapping_failure(value) for value in normalization_failures] + gate_failures
        except (SourceFetchError, SourceError, ValueError) as exc:
            raw = []
            eligible = []
            failures = [_failure(exc)]
            with session.begin():
                job = _load_discovery_job(session, job_id, owner_id)
                item = session.get(SourceSyncItem, item.id)
                if item is not None:
                    item.status = "failed"
                    item.last_error = failures[-1]["message"]
                    item.updated_at = utcnow()
                counters = dict(job.counters or {})
                counters["numbers_requested"] = int(counters.get("numbers_requested", 0)) + 1
                counters["lines_visited"] = int(counters.get("lines_visited", 0)) + 1
                counters["failures"] = int(counters.get("failures", 0)) + len(failures)
                job.counters = counters
                for failure in failures:
                    _append_failure(job, failure)
                job.updated_at = utcnow()
            visited += 1
            continue

        batches[ordinal + 1] = eligible
        with session.begin():
            job = _load_discovery_job(session, job_id, owner_id)
            item = session.get(SourceSyncItem, item.id)
            if item is not None:
                item.status = "succeeded"
                item.response_hash = sha256_json(
                    {
                        "url": url,
                        "source_ids": sorted(str(value["source_id"]) for value in eligible),
                    }
                )
                item.updated_at = utcnow()
            counters = dict(job.counters or {})
            counters["numbers_requested"] = int(counters.get("numbers_requested", 0)) + 1
            counters["numbers_completed"] = int(counters.get("numbers_completed", 0)) + 1
            counters["lines_visited"] = int(counters.get("lines_visited", 0)) + 1
            counters["tasks_seen"] = int(counters.get("tasks_seen", 0)) + len(raw)
            counters["tasks_eligible"] = int(counters.get("tasks_eligible", 0)) + len(eligible)
            counters["tasks_skipped_hidden"] = int(counters.get("tasks_skipped_hidden", 0)) + sum(
                1 for value in failures if value.get("code") == "SOURCE_HIDDEN"
            )
            counters["tasks_skipped_unscorable"] = int(counters.get("tasks_skipped_unscorable", 0)) + sum(
                1 for value in failures if value.get("code") == "SOURCE_UNSCORABLE"
            )
            job.counters = counters
            for failure in failures:
                _append_failure(job, failure)
            job.updated_at = utcnow()
        visited += 1

    if visited < NUMBER_LINE_COUNT:
        with session.begin():
            job = _load_discovery_job(session, job_id, owner_id)
            job.status = "partial"
            _append_failure(job, {"code": "SOURCE_NUMBER_INCOMPLETE", "message": "not all 27 lines were visited"})
            _clear_lease(job)
            job.updated_at = utcnow()
        return job

    with session.begin():
        job = _load_discovery_job(session, job_id, owner_id)
        baseline_ids, baseline_lines = _current_number_catalog(session, target_exam_year=target_exam_year)
        selected, selection_failures = _select_number_tasks(
            batches,
            seen_source_ids=baseline_ids,
            line_counts=baseline_lines,
            max_unique_tasks=max_unique_tasks,
        )
        for failure in selection_failures:
            _append_failure(job, failure)
        policy = ensure_source_policy(session, settings)
        admitted = 0
        revisions_created = 0
        seen = set(baseline_ids)
        for payload in selected:
            source_id = str(payload["source_id"])
            try:
                with session.begin_nested():
                    _task, _revision, _assets, duplicate_import = import_fixture(
                        session,
                        owner_id=owner_id,
                        settings=settings,
                        payload=payload,
                        idempotency_key=f"{job.id}:number:{source_id}",
                        admission_mode="remote",
                        admitted_rights_status=policy.permission_status,
                        parser_version=NUMBER_DISCOVERY_PARSER_VERSION,
                        target_exam_year=target_exam_year,
                    )
                admitted += 1
                if not duplicate_import:
                    revisions_created += 1
                seen.add(source_id)
            except (SourceError, EventConflict, ValueError) as exc:
                _append_failure(job, _failure(exc, source_id=source_id))
        final_ids, final_lines = _current_number_catalog(session, target_exam_year=target_exam_year)
        counters = dict(job.counters or {})
        counters["tasks_admitted"] = int(counters.get("tasks_admitted", 0)) + admitted
        counters["revisions_created"] = int(counters.get("revisions_created", 0)) + revisions_created
        counters["seen_source_ids"] = sorted(final_ids)
        counters["current_total"] = len(final_ids)
        counters["line_counts"] = {str(line): count for line, count in final_lines.items()}
        missing_lines = [line for line in range(1, NUMBER_LINE_COUNT + 1) if final_lines.get(line, 0) == 0]
        counters["missing_lines"] = missing_lines
        counters["newly_admitted"] = max(0, len(final_ids) - int(counters.get("baseline_existing", 0)))
        counters["attachment_bytes_fetched"] = 0
        counters["failures"] = len(job.failures or [])
        job.counters = counters
        job.cursor_index = NUMBER_LINE_COUNT
        coverage_complete = max_unique_tasks < NUMBER_LINE_COUNT or not missing_lines
        if (
            int(counters.get("numbers_completed", 0)) == NUMBER_LINE_COUNT
            and len(final_ids) == max_unique_tasks
            and coverage_complete
        ):
            job.status = "succeeded"
        else:
            job.status = "partial"
            if len(final_ids) != max_unique_tasks:
                _append_failure(
                    job,
                    {
                        "code": "SOURCE_TARGET_UNREACHED",
                        "message": "27-line discovery did not provide the exact requested number of unique tasks",
                    },
                )
            if not coverage_complete:
                _append_failure(
                    job,
                    {
                        "code": "SOURCE_LINE_COVERAGE_INCOMPLETE",
                        "message": "catalog discovery did not provide at least one task for every line 1..27",
                    },
                )
        _clear_lease(job)
        job.updated_at = utcnow()
    return job


def _summary(job: SourceSyncJob) -> dict[str, Any]:
    counters = dict(job.counters or {})
    counters.pop("seen_source_ids", None)
    return {"status": job.status, "counters": counters}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Polite KЕГЭ catalog discovery")
    parser.add_argument("--mode", choices=("random", "number"), default="random")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--number-api-template")
    parser.add_argument("--target-exam-year", type=int, default=DEFAULT_TARGET_EXAM_YEAR)
    parser.add_argument("--max-unique-tasks", type=int, default=DEFAULT_MAX_UNIQUE_TASKS)
    parser.add_argument("--max-variants", type=int, default=DEFAULT_MAX_VARIANTS)
    parser.add_argument("--plateau-limit", type=int, default=DEFAULT_PLATEAU_LIMIT)
    parser.add_argument("--work-limit", type=int, default=DEFAULT_MAX_VARIANTS)
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
            client = BoundedKegeClient(settings, min_request_interval_seconds=2.0)
            if args.mode == "number":
                if not args.number_api_template:
                    raise SourceError(
                        "SOURCE_API_TEMPLATE_REQUIRED",
                        "--number-api-template is required for number mode",
                    )
                job = run_number_discovery(
                    session,
                    owner_id=settings.owner_id,
                    settings=settings,
                    client=client,
                    idempotency_key=args.idempotency_key or "number-discovery-2027",
                    number_api_template=args.number_api_template,
                    target_exam_year=args.target_exam_year,
                    max_unique_tasks=args.max_unique_tasks,
                    resume=args.resume,
                )
            else:
                job = run_variant_discovery(
                    session,
                    owner_id=settings.owner_id,
                    settings=settings,
                    client=client,
                    idempotency_key=args.idempotency_key or "variant-discovery-2027",
                    target_exam_year=args.target_exam_year,
                    max_unique_tasks=args.max_unique_tasks,
                    max_variants=args.max_variants,
                    plateau_limit=args.plateau_limit,
                    work_limit=args.work_limit,
                    resume=args.resume,
                )
            print(json.dumps(_summary(job), ensure_ascii=False, sort_keys=True))
    except (SourceError, SourceFetchError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "blocked", "code": str(getattr(exc, "code", "SOURCE_DISCOVERY_FAILED"))},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

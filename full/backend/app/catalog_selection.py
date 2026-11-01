"""Read-side access to atomically published, owner-scoped catalog editions."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import CatalogEntry, SourceSyncJob
from .util import iso_datetime

RECENT_CATALOG_MODE = "recent-catalog"


def latest_catalog_job(session: Session, owner_id: str) -> SourceSyncJob | None:
    return session.scalar(
        select(SourceSyncJob)
        .where(
            SourceSyncJob.owner_id == owner_id,
            SourceSyncJob.mode == RECENT_CATALOG_MODE,
            SourceSyncJob.status.in_(("succeeded", "partial")),
            SourceSyncJob.counters["published"].as_boolean().is_(True),
        )
        .order_by(SourceSyncJob.updated_at.desc(), SourceSyncJob.id.desc())
        .limit(1)
    )


def catalog_revision_ids(job_id: str) -> Any:
    return select(CatalogEntry.revision_id).where(CatalogEntry.job_id == job_id)


def coverage_view(session: Session, owner_id: str) -> dict[str, Any]:
    job = latest_catalog_job(session, owner_id)
    if job is None:
        return {"published": False, "policy_version": None, "primary_years": [2025, 2026, 2027],
                "fallback_year": 2024, "fallback_threshold": 30, "target_per_cell": 50,
                "total_selected": 0, "quota_met": False, "updated_at": None, "cells": []}
    report = job.counters
    keys = ("policy_version", "primary_years", "fallback_year", "fallback_threshold", "target_per_cell",
            "total_selected", "quota_met", "cells")
    return {**{key: report.get(key) for key in keys}, "published": True, "updated_at": iso_datetime(job.updated_at)}

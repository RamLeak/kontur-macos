"""API dependencies, stable errors and response serializers."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any, NoReturn

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .auth import OwnerPrincipal, ensure_owner, require_owner
from .models import (
    Annotation,
    Attempt,
    BoardDocument,
    BoardVersion,
    CodeDocument,
    CodeSample,
    CodeSampleVersion,
    CodeVersion,
    Conversation,
    ExamAnswer,
    ExamItem,
    ExamReport,
    ExamSession,
    Message,
    Project,
    ReviewItem,
    TaskBookmark,
    TaskRevision,
)
from .source_identity import display_source_id
from .util import iso_datetime


def get_db(request: Request) -> Generator[Session, None, None]:
    session = request.app.state.session_factory()
    session.info["settings"] = request.app.state.settings
    session.info["runner_client"] = getattr(request.app.state, "runner_client", None)
    session.info["codex_client"] = getattr(request.app.state, "codex_client", None)
    session.info["source_client"] = getattr(request.app.state, "source_client", None)
    try:
        yield session
    finally:
        session.close()


def owner_session(
    principal: OwnerPrincipal = Depends(require_owner),
) -> OwnerPrincipal:
    # The dependency performs no write; the first transactional endpoint call
    # ensures the owner row before writing any scoped aggregate.
    return principal


def api_error(status_code: int, code: str, message: str, details: dict[str, Any] | None = None) -> NoReturn:
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "details": details or {}},
    )


def require_found(value: Any, *, resource: str = "resource") -> Any:
    if value is None:
        api_error(404, "NOT_FOUND", f"{resource} not found")
    return value


def ensure_owner_row(session: Session, owner_id: str) -> None:
    ensure_owner(session, owner_id)


def asset_view(asset: Any) -> dict[str, Any]:
    return {
        "id": asset.id,
        "filename": asset.filename,
        "media_type": asset.media_type,
        "detected_type": asset.detected_type,
        "size_bytes": asset.size_bytes,
        "content_hash": asset.content_hash,
        "cache_state": asset.cache_state,
        "rights_status": asset.rights_status,
        "canonical_url": asset.canonical_url,
    }


def revision_view(
    revision: TaskRevision,
    assets: list[Any] | None = None,
    *,
    include_solution: bool = True,
) -> dict[str, Any]:
    view = {
        "id": revision.id,
        "task_id": revision.task_id,
        "revision_number": revision.revision_number,
        "source_url": revision.source_url,
        "source_name": "kompege",
        "source_id": display_source_id(revision),
        "line_number": revision.line_number,
        "exam_year": revision.exam_year,
        # Preparation target is an owner-selected catalog label, not a claim
        # that the upstream record is officially authored for that year.
        "target_exam_year": revision.target_exam_year,
        "source_year": revision.source_year,
        "source_year_basis": revision.source_year_basis,
        "source_created_at": iso_datetime(revision.source_created_at),
        "source_difficulty": revision.source_difficulty,
        "difficulty": revision.difficulty,
        "source_label": revision.source_label,
        "source_metadata": revision.source_metadata,
        "statement": revision.statement,
        "statement_latex": revision.statement_latex,
        "table": revision.table_manifest,
        "content_hash": revision.content_hash,
        "retrieved_at": iso_datetime(revision.retrieved_at),
        "parser_version": revision.parser_version,
        "rights_status": revision.rights_status,
        "assets": [asset_view(asset) for asset in assets or []],
    }
    # Answer keys and solution text stay server-side for exam scoring and tutor
    # context. They are never included in task/list/session payloads; a sealed
    # exam therefore cannot be bypassed by opening the same task elsewhere.
    return view


def task_view(
    task: Any,
    revision: TaskRevision,
    assets: list[Any] | None = None,
    *,
    include_solution: bool = True,
) -> dict[str, Any]:
    return {
        "id": task.id,
        "source_name": task.source_name,
        "external_id": display_source_id(revision) if task.external_id == revision.source_id else task.external_id,
        "line_number": task.line_number,
        "current_revision_id": task.current_revision_id,
        "revision": revision_view(revision, assets, include_solution=include_solution),
    }


def bookmark_view(bookmark: TaskBookmark) -> dict[str, Any]:
    return {
        "task_id": bookmark.task_id,
        "task_revision_id": bookmark.task_revision_id,
        "created_at": iso_datetime(bookmark.created_at),
    }


def project_view(project: Project) -> dict[str, Any]:
    return {
        "id": project.id,
        "name": project.name,
        "pinned": project.pinned,
        "archived": project.archived,
        "order_index": project.order_index,
        "created_at": iso_datetime(project.created_at),
        "updated_at": iso_datetime(project.updated_at),
    }


def conversation_view(conversation: Conversation) -> dict[str, Any]:
    return {
        "id": conversation.id,
        "project_id": conversation.project_id,
        "title": conversation.title,
        "pinned": conversation.pinned,
        "archived": conversation.archived,
        "last_message_at": iso_datetime(conversation.last_message_at),
        "created_at": iso_datetime(conversation.created_at),
    }


def message_view(message: Message) -> dict[str, Any]:
    return {
        "id": message.id,
        "conversation_id": message.conversation_id,
        "role": message.role,
        "content": message.content,
        "content_hash": message.content_hash,
        "metadata": message.metadata_json,
        "created_at": iso_datetime(message.created_at),
    }


def attempt_view(attempt: Attempt) -> dict[str, Any]:
    return {
        "id": attempt.id,
        "solve_session_id": attempt.solve_session_id,
        "status": attempt.status,
        "started_at": iso_datetime(attempt.started_at),
        "completed_at": iso_datetime(attempt.completed_at),
        "user_answer": attempt.user_answer,
        "verified_correct": attempt.verified_correct,
        "verification_method": attempt.verification_method,
        "independent": attempt.independent,
        "hint_count": attempt.hint_count,
        "error_type": attempt.error_type,
        "notes": attempt.notes,
    }


def code_view(document: CodeDocument, version: CodeVersion | None) -> dict[str, Any]:
    return {
        "document_id": document.id,
        "solve_session_id": document.solve_session_id,
        "language": document.language,
        "version": document.current_version,
        "content": version.content if version else "",
        "content_hash": version.content_hash if version else None,
        "updated_at": iso_datetime(document.updated_at),
    }


def code_version_view(version: CodeVersion) -> dict[str, Any]:
    return {
        "id": version.id,
        "document_id": version.document_id,
        "version": version.version,
        "content": version.content,
        "content_hash": version.content_hash,
        "created_at": iso_datetime(version.created_at),
    }


def board_view(document: BoardDocument, version: BoardVersion | None) -> dict[str, Any]:
    return {
        "document_id": document.id,
        "solve_session_id": document.solve_session_id,
        "version": document.current_version,
        "snapshot": version.snapshot_json if version else {},
        "payload_hash": version.payload_hash if version else None,
        "size_bytes": version.size_bytes if version else 0,
        "updated_at": iso_datetime(document.updated_at),
    }


def annotation_view(annotation: Annotation) -> dict[str, Any]:
    return {
        "id": annotation.id,
        "solve_session_id": annotation.solve_session_id,
        "kind": annotation.kind,
        "payload": annotation.payload_json,
        "anchor": annotation.anchor_json,
        "source_revision_hash": annotation.source_revision_hash,
        "anchor_status": annotation.anchor_status,
        "created_at": iso_datetime(annotation.created_at),
        "updated_at": iso_datetime(annotation.updated_at),
    }


def sample_view(sample: CodeSample, version: CodeSampleVersion | None) -> dict[str, Any]:
    return {
        "id": sample.id,
        "title": sample.title,
        "language": sample.language,
        "usage_note": sample.usage_note,
        "task_lines": sample.task_lines,
        "tags": sample.tags,
        "enabled": sample.enabled,
        "version": sample.current_version,
        "content": version.content if version else "",
        "content_hash": version.content_hash if version else None,
        "created_at": iso_datetime(sample.created_at),
        "updated_at": iso_datetime(sample.updated_at),
    }


def error_view(
    error: Any,
    *,
    evidence: list[dict[str, Any]] | None = None,
    task_revision_id: str | None = None,
    solve_session_id: str | None = None,
    exam_item_id: str | None = None,
) -> dict[str, Any]:
    reason = str(getattr(error, "reason", "") or "").strip()
    if not reason:
        description = str(getattr(error, "description", "") or "").strip()
        reason = description or f"Observed error: {error.error_type}"
    return {
        "id": error.id,
        "attempt_id": error.attempt_id,
        "task_revision_id": task_revision_id,
        "solve_session_id": solve_session_id,
        "exam_item_id": exam_item_id,
        "status": error.status,
        "error_type": error.error_type,
        "description": error.description,
        "reason": reason,
        "prevention_note": error.prevention_note,
        "source": error.source,
        "task_line": getattr(error, "task_line", None),
        "policy_version": getattr(error, "policy_version", None),
        "evidence_ids": [error.attempt_id] if error.attempt_id else [],
        "evidence": evidence or ([{"kind": "attempt", "id": error.attempt_id}] if error.attempt_id else []),
        "created_at": iso_datetime(error.created_at),
        "updated_at": iso_datetime(error.updated_at),
    }


def review_view(review: ReviewItem) -> dict[str, Any]:
    reason = str(getattr(review, "reason", "") or "").strip()
    if not reason:
        codes = ", ".join(str(code) for code in (review.reason_codes or []))
        reason = f"Review evidence: {codes}" if codes else "Review item has recorded evidence"
    return {
        "id": review.id,
        "skill_id": review.skill_id,
        "error_pattern_id": review.error_pattern_id,
        "due_at": iso_datetime(review.due_at),
        "interval_days": review.interval_days,
        "reason_codes": review.reason_codes,
        "reason": reason,
        "evidence_ids": review.evidence_ids,
        "evidence": [{"kind": "error_event", "id": item} for item in review.evidence_ids],
        "task_line": getattr(review, "task_line", None),
        "policy_version": getattr(review, "policy_version", None),
        "status": review.status,
        "completed_at": iso_datetime(review.completed_at),
    }


def exam_view(
    session: ExamSession,
    items: list[tuple[ExamItem, TaskRevision]],
    *,
    assets_by_revision: dict[str, list[Any]] | None = None,
    duration_minutes: int | None = None,
    deadline_at: Any = None,
) -> dict[str, Any]:
    assets_by_revision = assets_by_revision or {}

    def item_view(item: ExamItem, revision: TaskRevision) -> dict[str, Any]:
        view: dict[str, Any] = {
            "id": item.id,
            "position": item.position,
            "task_revision_id": revision.id,
            "line_number": revision.line_number,
            "statement": revision.statement,
            "difficulty": revision.difficulty,
            "source_id": display_source_id(revision),
            # Keep the sealed boundary explicit for clients and legacy checks;
            # the actual answer key never crosses this view.
            "answer": None,
        }
        assets = assets_by_revision.get(revision.id, [])
        if assets:
            view["assets"] = [asset_view(asset) for asset in assets]
        return view

    return {
        "id": session.id,
        "blueprint_id": session.blueprint_id,
        "seed": session.seed,
        "rng_algorithm": session.rng_algorithm,
        "rng_version": session.rng_version,
        "scoring_version": session.scoring_version,
        "pool_manifest_hash": session.pool_manifest_hash,
        "ordered_revision_ids": session.ordered_revision_ids,
        "lifecycle": session.lifecycle,
        "sealed": session.sealed,
        # ``legacy`` marks sessions created before explicit strict/study mode;
        # callers must not infer AI/pause availability from missing data.
        "mode": session.mode or "legacy",
        "started_at": iso_datetime(session.started_at),
        "duration_minutes": duration_minutes,
        "deadline_at": iso_datetime(deadline_at),
        "completed_at": iso_datetime(session.completed_at),
        "items": [item_view(item, revision) for item, revision in items],
    }


def answer_view(answer: ExamAnswer | None) -> dict[str, Any] | None:
    if answer is None:
        return None
    return {"id": answer.id, "content": answer.content, "saved_at": iso_datetime(answer.saved_at)}


def report_view(report: ExamReport) -> dict[str, Any]:
    return {
        "id": report.id,
        "exam_session_id": report.exam_session_id,
        "scoring_version": report.scoring_version,
        "report": report.report_json,
        "report_hash": report.report_hash,
        "created_at": iso_datetime(report.created_at),
    }

"""Deterministic КЕГЭ-only exam simulations with sealed server enforcement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .catalog_selection import catalog_revision_ids, latest_catalog_job
from .events import append_event
from .exam_scoring import PRACTICE_SCORING_VERSION, SCALE_2026, points_for_answer, score_scale
from .learning import record_mastery_evidence, task_skill_for_revision
from .models import (
    ExamAnswer,
    ExamBlueprint,
    ExamCodeRun,
    ExamItem,
    ExamReport,
    ExamSession,
    ExamWorkspace,
    Task,
    TaskAsset,
    TaskRevision,
    new_id,
    utcnow,
)
from .source_identity import display_source_id
from .util import normalized_answer, sha256_json

RNG_ALGORITHM = "sha256-sort"
RNG_VERSION = "v1"
SCORING_VERSION = "score-v1"


class ExamError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ExamAssembly:
    exam: ExamSession
    items: list[ExamItem]


@dataclass(frozen=True, slots=True)
class SelectionConstraints:
    source_name: str
    catalog: str
    rights_statuses: tuple[str, ...]
    require_assets_by_line: frozenset[int]
    excluded_revision_ids: frozenset[str]
    include_revision_ids: frozenset[str]
    parser_versions: tuple[str, ...] | None
    require_answer: bool
    difficulty: str | None
    source_year: int | None


def exam_timing(session: Session, exam: ExamSession) -> tuple[int | None, datetime | None]:
    """Return the immutable timing derived from the exam blueprint.

    The session stores its start instant; the official duration is part of the
    versioned blueprint constraints.  Invalid or absent custom timing remains
    an untimed exam, while the official 2027 blueprint always carries a valid
    bounded duration.
    """

    blueprint = session.get(ExamBlueprint, exam.blueprint_id)
    constraints = blueprint.constraints if blueprint is not None else None
    exam_constraints = constraints.get("exam") if isinstance(constraints, dict) else None
    raw_duration = exam_constraints.get("duration_minutes") if isinstance(exam_constraints, dict) else None
    if isinstance(raw_duration, bool) or not isinstance(raw_duration, int) or not 0 < raw_duration <= 24 * 60:
        return None, None
    # A prepared session has a DB compatibility timestamp, but its timer is
    # deliberately absent until the explicit /start transition.
    if exam.lifecycle == "ready":
        return raw_duration, None
    return raw_duration, exam.started_at + timedelta(minutes=raw_duration, seconds=exam.paused_seconds or 0)


def elapsed_seconds(exam: ExamSession, *, now: datetime | None = None) -> int:
    if exam.lifecycle == "ready":
        return 0
    end = exam.completed_at or exam.paused_at or now or utcnow()
    return max(0, int((end - exam.started_at).total_seconds() - (exam.paused_seconds or 0)))


def set_exam_paused(session: Session, *, owner_id: str, exam: ExamSession, paused: bool) -> None:
    if exam.owner_id != owner_id:
        raise ExamError("OWNER_SCOPE", "exam does not belong to owner")
    if exam.mode == "strict":
        raise ExamError("EXAM_PAUSE_FORBIDDEN", "строгий вариант нельзя поставить на паузу")
    desired = "paused" if paused else "active"
    if exam.lifecycle == desired:
        return
    if exam.lifecycle not in {"active", "paused"} or (not exam.sealed and exam.mode != "study"):
        raise ExamError("EXAM_NOT_ACTIVE", "Завершённый вариант нельзя продолжить.")
    now = utcnow()
    if paused:
        _duration, deadline = exam_timing(session, exam)
        if deadline is not None and now >= deadline:
            raise ExamError("EXAM_TIME_EXPIRED", "Время истекло. Завершите вариант, чтобы увидеть результат.")
        exam.paused_at = now
    else:
        if exam.paused_at is None:
            raise ExamError("EXAM_TIMING_INVALID", "Состояние таймера повреждено.")
        exam.paused_seconds = (exam.paused_seconds or 0) + max(0, (now - exam.paused_at).total_seconds())
        exam.paused_at = None
    exam.lifecycle = desired
    session.flush()
    append_event(
        session,
        owner_id=owner_id,
        aggregate_type="exam_session",
        aggregate_id=exam.id,
        event_type="exam_paused" if paused else "exam_resumed",
        payload={"elapsed_seconds": elapsed_seconds(exam, now=now)},
        idempotency_key=f"exam:{exam.id}:{desired}:{new_id()}",
    )


def _bounded_string_list(value: Any, *, field: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ExamError("BLUEPRINT_INVALID", f"{field} must be a bounded list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 255:
            raise ExamError("BLUEPRINT_INVALID", f"{field} contains an invalid value")
        result.append(item)
    return tuple(dict.fromkeys(result))


def _selection_constraints(value: dict[str, Any]) -> SelectionConstraints:
    raw = value.get("selection", {})
    if not isinstance(raw, dict):
        raise ExamError("BLUEPRINT_INVALID", "constraints.selection must be an object")
    allowed = {
        "source_name",
        "catalog",
        "rights_statuses",
        "require_assets_by_line",
        "exclude_revision_ids",
        "include_revision_ids",
        "parser_versions",
        "require_answer",
        "difficulty",
        "source_year",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ExamError("BLUEPRINT_INVALID", f"unsupported selection constraint: {unknown[0]}")

    source_name = raw.get("source_name", "kompege")
    if source_name != "kompege":
        raise ExamError("BLUEPRINT_INVALID", "exam source must be kompege")

    catalog = raw.get("catalog", "published")
    if not isinstance(catalog, str) or catalog not in {"published", "all"}:
        raise ExamError("BLUEPRINT_INVALID", "catalog must be published or all")

    raw_rights = raw.get("rights_statuses", ["private_authorized", "public_authorized"])
    rights = _bounded_string_list(raw_rights, field="rights_statuses", maximum=2)
    allowed_rights = {"private_authorized", "public_authorized"}
    if not rights or not set(rights).issubset(allowed_rights):
        raise ExamError("BLUEPRINT_INVALID", "rights_statuses must contain only authorized states")

    raw_asset_lines = raw.get("require_assets_by_line", [])
    if not isinstance(raw_asset_lines, list) or len(raw_asset_lines) > 27:
        raise ExamError("BLUEPRINT_INVALID", "require_assets_by_line must be a bounded list")
    asset_lines: set[int] = set()
    for raw_line in raw_asset_lines:
        if isinstance(raw_line, bool):
            raise ExamError("BLUEPRINT_INVALID", "require_assets_by_line contains an invalid line")
        try:
            line = int(raw_line)
        except (TypeError, ValueError) as exc:
            raise ExamError("BLUEPRINT_INVALID", "require_assets_by_line contains an invalid line") from exc
        if not 1 <= line <= 27:
            raise ExamError("BLUEPRINT_INVALID", "require_assets_by_line contains an invalid line")
        asset_lines.add(line)

    excluded = frozenset(
        _bounded_string_list(raw.get("exclude_revision_ids", []), field="exclude_revision_ids", maximum=10_000)
    )
    included = frozenset(
        _bounded_string_list(raw.get("include_revision_ids", []), field="include_revision_ids", maximum=10_000)
    )
    if included and excluded.intersection(included):
        raise ExamError("BLUEPRINT_INVALID", "include_revision_ids and exclude_revision_ids overlap")
    parser_versions: tuple[str, ...] | None = None
    if "parser_versions" in raw:
        parser_versions = _bounded_string_list(raw["parser_versions"], field="parser_versions", maximum=32)
        if not parser_versions:
            raise ExamError("BLUEPRINT_INVALID", "parser_versions cannot be empty")
    require_answer = raw.get("require_answer", False)
    if not isinstance(require_answer, bool):
        raise ExamError("BLUEPRINT_INVALID", "require_answer must be boolean")
    difficulty = raw.get("difficulty")
    if difficulty is not None and (
        not isinstance(difficulty, str) or difficulty not in {"basic", "easy", "medium", "hard"}
    ):
        raise ExamError("BLUEPRINT_INVALID", "difficulty must be easy, medium or hard")
    source_year = raw.get("source_year")
    if source_year is not None and (
        isinstance(source_year, bool) or not isinstance(source_year, int) or not 2000 <= source_year <= 2100
    ):
        raise ExamError("BLUEPRINT_INVALID", "source_year is outside allowed bounds")
    return SelectionConstraints(
        source_name=source_name,
        catalog=catalog,
        rights_statuses=rights,
        require_assets_by_line=frozenset(asset_lines),
        excluded_revision_ids=excluded,
        include_revision_ids=included,
        parser_versions=parser_versions,
        require_answer=require_answer,
        difficulty=difficulty,
        source_year=source_year,
    )


def _line_requirements(value: dict[str, Any]) -> dict[int, int]:
    result: dict[int, int] = {}
    for raw_line, raw_count in value.items():
        try:
            line = int(raw_line)
            count = int(raw_count)
        except (TypeError, ValueError) as exc:
            raise ExamError("BLUEPRINT_INVALID", "required_lines must map integer lines to counts") from exc
        if not 1 <= line <= 27 or count < 1 or count > 100:
            raise ExamError("BLUEPRINT_INVALID", "required line/count is outside allowed bounds")
        result[line] = count
    if not result:
        raise ExamError("BLUEPRINT_INVALID", "required_lines cannot be empty")
    return result


def active_sealed_exam(session: Session, owner_id: str) -> bool:
    return (
        session.scalar(
            select(ExamSession.id).where(
                ExamSession.owner_id == owner_id,
                ExamSession.lifecycle.in_(("active", "paused")),
                ExamSession.sealed.is_(True),
            )
        )
        is not None
    )


def assemble_exam(
    session: Session,
    *,
    owner_id: str,
    blueprint: ExamBlueprint,
    seed: int,
    mode: str = "legacy",
    lifecycle: str = "active",
    prepare_kind: str | None = None,
    prepare_options: dict[str, Any] | None = None,
    difficulty_variant: bool = True,
) -> ExamAssembly:
    if mode not in {"legacy", "strict", "study"}:
        raise ExamError("EXAM_MODE_INVALID", "режим варианта должен быть strict или study")
    if lifecycle not in {"active", "ready"}:
        raise ExamError("EXAM_LIFECYCLE_INVALID", "exam lifecycle must be active or ready")
    if lifecycle == "ready" and not prepare_kind:
        raise ExamError("EXAM_PREPARE_INVALID", "ready sessions require a preparation kind")
    if blueprint.owner_id != owner_id:
        raise ExamError("OWNER_SCOPE", "exam blueprint does not belong to owner")
    if lifecycle == "active" and active_sealed_exam(session, owner_id):
        raise ExamError("EXAM_SEALED", "another sealed exam is already active")
    requirements = _line_requirements(blueprint.required_lines)
    if not isinstance(blueprint.constraints, dict):
        raise ExamError("BLUEPRINT_INVALID", "constraints must be an object")
    selection_policy = _selection_constraints(blueprint.constraints)
    if selection_policy.difficulty and difficulty_variant and requirements != {line: 1 for line in range(1, 28)}:
        raise ExamError("BLUEPRINT_INVALID", "Difficulty variants require exactly one task per line 1–27")
    constraints_hash = sha256_json(blueprint.constraints)
    selected: list[TaskRevision] = []
    pool_manifest: dict[str, list[str]] = {}
    ranked_pool: dict[int, list[TaskRevision]] = {}
    catalog_job = latest_catalog_job(session, owner_id) if selection_policy.catalog == "published" else None
    for line, count in sorted(requirements.items()):
        predicates: list[Any] = [
            Task.source_name == selection_policy.source_name,
            TaskRevision.line_number == line,
            TaskRevision.rights_status.in_(selection_policy.rights_statuses),
        ]
        if catalog_job is not None and not selection_policy.include_revision_ids:
            predicates.append(TaskRevision.id.in_(catalog_revision_ids(catalog_job.id)))
        elif not selection_policy.include_revision_ids:
            predicates.append(Task.current_revision_id == TaskRevision.id)
        if blueprint.exam_year is not None:
            # A blueprint year describes the learner's preparation target.
            # ``exam_year`` on a revision is only observed upstream
            # provenance and must not be relabelled or used for assembly.
            predicates.append(TaskRevision.target_exam_year == blueprint.exam_year)
        if selection_policy.source_year is not None:
            predicates.extend(
                [
                    TaskRevision.source_year == selection_policy.source_year,
                    TaskRevision.source_year_basis.is_not(None),
                ]
            )
        if selection_policy.include_revision_ids:
            predicates.append(TaskRevision.id.in_(selection_policy.include_revision_ids))
        if selection_policy.excluded_revision_ids:
            predicates.append(TaskRevision.id.not_in(selection_policy.excluded_revision_ids))
        if selection_policy.parser_versions is not None:
            predicates.append(TaskRevision.parser_version.in_(selection_policy.parser_versions))
        if selection_policy.require_answer:
            predicates.extend([TaskRevision.answer.is_not(None), TaskRevision.answer != ""])
        if line in selection_policy.require_assets_by_line:
            predicates.append(select(TaskAsset.id).where(TaskAsset.revision_id == TaskRevision.id).exists())
        revisions = list(
            session.scalars(
                select(TaskRevision)
                .join(Task, Task.id == TaskRevision.task_id)
                .where(*predicates)
                .order_by(TaskRevision.id)
            )
        )
        # A snapshot can retain the original question while declaring that
        # required materials could not enter the trusted asset boundary. Such
        # questions remain in the catalogue but cannot become exam items.
        revisions = [
            r for r in revisions
            if not (isinstance(r.source_metadata, dict)
                    and r.source_metadata.get("kege_schema") == "kege-current-bank/2.0"
                    and r.source_metadata.get("kege_unavailable_resources"))
        ]
        if selection_policy.difficulty and difficulty_variant:
            # Legacy difficulty variants retain their published-catalog fallback
            # policy and 80% target coverage requirement.
            revisions = [
                r
                for r in revisions
                if r.source_year in {2024, 2025, 2026, 2027} and r.difficulty in {"easy", "medium", "hard"}
            ]
            if catalog_job is None:
                recent_counts = {
                    level: sum(r.source_year != 2024 and r.difficulty == level for r in revisions)
                    for level in ("easy", "medium", "hard")
                }
                revisions = [
                    r for r in revisions if r.source_year != 2024 or recent_counts[r.difficulty or "easy"] < 30
                ]
        elif selection_policy.difficulty:
            target = "easy" if selection_policy.difficulty == "basic" else selection_policy.difficulty
            revisions = [r for r in revisions if r.difficulty == target]
        if len(revisions) < count:
            raise ExamError(
                "EXAM_POOL_TOO_SMALL", f"Для номера {line} не хватает заданий КЕГЭ с ответом и нужными файлами."
            )
        ids = [revision.id for revision in revisions]
        pool_manifest[str(line)] = ids
        ranked = sorted(
            revisions,
            key=lambda revision: (sha256_json({"seed": seed, "line": line, "revision_id": revision.id}), revision.id),
        )
        ranked_pool[line] = ranked
        selected.extend(ranked[:count])
    if selection_policy.difficulty and difficulty_variant:
        # Variant UI calls this level "basic"; source revisions canonically use
        # "easy". Keep upstream classification intact at this boundary.
        target = "easy" if selection_policy.difficulty == "basic" else selection_policy.difficulty
        target_lines = [line for line, pool in ranked_pool.items() if any(r.difficulty == target for r in pool)]
        needed = ceil(len(requirements) * 0.8)
        if len(target_lines) < needed:
            raise ExamError(
                "EXAM_DIFFICULTY_POOL_TOO_SMALL",
                f"Эта сложность доступна только для {len(target_lines)} из 27 номеров; нужно минимум {needed}. "
                "Попробуйте другую сложность — задания не будут подменены.",
            )
        mandatory = [line for line in target_lines if all(r.difficulty == target for r in ranked_pool[line])]
        optional = sorted(
            (line for line in target_lines if line not in mandatory),
            key=lambda line: sha256_json({"seed": seed, "target_line": line}),
        )
        preferred = set(mandatory + optional[: max(0, needed - len(mandatory))])
        selected = []
        for line, pool in sorted(ranked_pool.items()):
            matching = [r for r in pool if (r.difficulty == target) == (line in preferred)]
            selected.append((matching or pool)[0])
    pool_hash = sha256_json(
        {
            "blueprint_id": blueprint.id,
            "blueprint_version": blueprint.version,
            "target_exam_year": blueprint.exam_year,
            "requirements": requirements,
            "constraints_hash": constraints_hash,
            "pool": pool_manifest,
        }
    )
    ordered_revision_ids = [revision.id for revision in selected]
    exam = ExamSession(
        id=new_id(),
        owner_id=owner_id,
        blueprint_id=blueprint.id,
        seed=seed,
        rng_algorithm=RNG_ALGORITHM,
        rng_version=RNG_VERSION,
        scoring_version=PRACTICE_SCORING_VERSION if selection_policy.difficulty else SCORING_VERSION,
        pool_manifest_hash=pool_hash,
        ordered_revision_ids=ordered_revision_ids,
        lifecycle=lifecycle,
        # Legacy requests retain the pre-mode sealed behavior. Explicit study
        # sessions are the only new unsealed/pause-capable mode.
        sealed=mode != "study",
        mode=mode,
        prepare_kind=prepare_kind,
        prepare_options=prepare_options,
        prepared_at=utcnow() if lifecycle == "ready" else None,
        started_at=utcnow(),
    )
    session.add(exam)
    session.flush()
    items: list[ExamItem] = []
    for position, revision in enumerate(selected):
        item = ExamItem(
            id=new_id(),
            exam_session_id=exam.id,
            position=position,
            task_revision_id=revision.id,
        )
        session.add(item)
        items.append(item)
    session.flush()
    append_event(
        session,
        owner_id=owner_id,
        aggregate_type="exam_session",
        aggregate_id=exam.id,
        event_type="exam_prepared" if lifecycle == "ready" else "exam_simulation_started",
        payload={
            "blueprint_id": blueprint.id,
            "blueprint_version": blueprint.version,
            "target_exam_year": blueprint.exam_year,
            "seed": seed,
            "ordered_revision_ids": ordered_revision_ids,
            "pool_manifest_hash": pool_hash,
            "constraints_hash": constraints_hash,
            "rng_algorithm": RNG_ALGORITHM,
            "rng_version": RNG_VERSION,
            "scoring_version": exam.scoring_version,
            "mode": exam.mode,
            "lifecycle": lifecycle,
            "prepare_kind": prepare_kind,
        },
        idempotency_key=f"exam:{exam.id}:{'prepared' if lifecycle == 'ready' else 'started'}",
    )
    return ExamAssembly(exam=exam, items=items)


def save_answer(session: Session, *, owner_id: str, exam: ExamSession, item: ExamItem, content: str) -> ExamAnswer:
    if exam.owner_id != owner_id:
        raise ExamError("OWNER_SCOPE", "exam does not belong to owner")
    if item.exam_session_id != exam.id:
        raise ExamError("OWNER_SCOPE", "exam item does not belong to exam")
    if exam.lifecycle != "active":
        raise ExamError("EXAM_NOT_ACTIVE", "exam answers are immutable after submission")
    _duration_minutes, deadline_at = exam_timing(session, exam)
    if deadline_at is not None and utcnow() >= deadline_at:
        raise ExamError("EXAM_TIME_EXPIRED", "exam deadline has passed; answers are immutable")
    existing = session.scalar(select(ExamAnswer).where(ExamAnswer.exam_item_id == item.id))
    if existing is None:
        existing = ExamAnswer(exam_item_id=item.id, content=content, saved_at=utcnow())
        session.add(existing)
    else:
        existing.content = content
        existing.saved_at = utcnow()
    session.flush()
    append_event(
        session,
        owner_id=owner_id,
        aggregate_type="exam_item",
        aggregate_id=item.id,
        event_type="exam_answer_saved",
        payload={"content_hash": sha256_json(content)},
        idempotency_key=f"exam-answer:{item.id}:{sha256_json(content)}",
    )
    return existing


def submit_exam(session: Session, *, owner_id: str, exam: ExamSession) -> ExamReport:
    if exam.owner_id != owner_id:
        raise ExamError("OWNER_SCOPE", "exam does not belong to owner")
    if exam.lifecycle == "submitted":
        if active_sealed_exam(session, owner_id):
            raise ExamError("EXAM_SEALED", "Разборы доступны после завершения текущего варианта.")
        report = session.scalar(select(ExamReport).where(ExamReport.exam_session_id == exam.id))
        if report is None:
            raise ExamError("REPORT_MISSING", "submitted exam report is missing")
        return report
    if exam.lifecycle not in {"active", "paused"}:
        raise ExamError("EXAM_NOT_ACTIVE", "exam cannot be submitted")
    rows = list(
        session.execute(
            select(ExamItem, TaskRevision, ExamAnswer)
            .join(TaskRevision, TaskRevision.id == ExamItem.task_revision_id)
            .outerjoin(ExamAnswer, ExamAnswer.exam_item_id == ExamItem.id)
            .where(ExamItem.exam_session_id == exam.id)
            .order_by(ExamItem.position)
        )
    )
    result_items: list[dict[str, Any]] = []
    correct_count = 0
    primary_score = 0
    max_primary_score = 0
    practice_scoring = exam.scoring_version == PRACTICE_SCORING_VERSION
    for item, revision, answer in rows:
        submitted = answer.content if answer is not None else ""
        correct = bool(
            revision.answer is not None and normalized_answer(submitted) == normalized_answer(revision.answer)
        )
        points, maximum = (
            points_for_answer(revision.line_number, submitted, revision.answer)
            if practice_scoring
            else (int(correct), 1)
        )
        correct = points == maximum
        primary_score += points
        max_primary_score += maximum
        if correct:
            correct_count += 1
        result_items.append(
            {
                "item_id": item.id,
                "position": item.position,
                "task_revision_id": revision.id,
                "line_number": revision.line_number,
                "difficulty": revision.difficulty,
                "source_id": display_source_id(revision),
                "status": "correct"
                if correct
                else ("partial" if points else ("unanswered" if not submitted.strip() else "incorrect")),
                "submitted_answer": submitted,
                "expected_answer": revision.answer,
                "points": points,
                "max_points": maximum,
            }
        )
        for mapping in task_skill_for_revision(session, revision.id):
            record_mastery_evidence(
                session,
                owner_id=owner_id,
                skill_id=mapping.skill_id,
                evidence_type="exam_attempt",
                value={"correct": correct, "independent": True, "hint_count": 0, "exam_id": exam.id},
            )
    duration_minutes, _deadline = exam_timing(session, exam)
    time_spent = elapsed_seconds(exam)
    if duration_minutes:
        time_spent = min(time_spent, duration_minutes * 60)
    workspace_rows = list(
        session.scalars(
            select(ExamWorkspace)
            .where(ExamWorkspace.owner_id == owner_id, ExamWorkspace.exam_session_id == exam.id)
            .order_by(ExamWorkspace.exam_item_id, ExamWorkspace.id)
        )
    )
    workspaces_by_item = {workspace.exam_item_id: workspace for workspace in workspace_rows}
    code_run_rows = list(
        session.scalars(
            select(ExamCodeRun)
            .where(ExamCodeRun.owner_id == owner_id, ExamCodeRun.exam_session_id == exam.id)
            .order_by(ExamCodeRun.exam_item_id, ExamCodeRun.created_at, ExamCodeRun.id)
        )
    )
    code_runs_by_item: dict[str, list[dict[str, Any]]] = {}
    for run in code_run_rows:
        code_runs_by_item.setdefault(run.exam_item_id, []).append(
            {
                "id": run.id,
                "workspace_version": run.workspace_version,
                "status": run.status,
                "executor_status": run.executor_status,
                "stdout": run.stdout,
                "stderr": run.stderr,
                "exit_code": run.exit_code,
                "timed_out": run.timed_out,
                "duration_ms": run.duration_ms,
                "created_at": run.created_at.isoformat() + "Z",
                "completed_at": run.completed_at.isoformat() + "Z" if run.completed_at else None,
            }
        )
    workspace_snapshots = {
        item.id: {
            "code": workspaces_by_item[item.id].code_content if item.id in workspaces_by_item else "",
            "code_version": workspaces_by_item[item.id].code_version if item.id in workspaces_by_item else 0,
            "board": workspaces_by_item[item.id].board_snapshot if item.id in workspaces_by_item else {},
            "board_version": workspaces_by_item[item.id].board_version if item.id in workspaces_by_item else 0,
            "code_runs": code_runs_by_item.get(item.id, []),
        }
        for item, _revision, _answer in rows
    }
    report_payload = {
        "exam_session_id": exam.id,
        "scoring_version": exam.scoring_version,
        "mode": exam.mode or "legacy",
        "correct_count": correct_count,
        "total_count": len(rows),
        "items": result_items,
        "primary_score": primary_score,
        "max_primary_score": max_primary_score,
        "test_score": SCALE_2026[primary_score] if practice_scoring and max_primary_score == 29 else None,
        "score_scale": score_scale(),
        "practice_only": True,
        "elapsed_seconds": time_spent,
        # This snapshot is copied before the session becomes submitted so
        # later correction work cannot mutate the historical attempt.
        "workspaces": workspace_snapshots,
    }
    report = ExamReport(
        id=new_id(),
        exam_session_id=exam.id,
        scoring_version=exam.scoring_version,
        report_json=report_payload,
        report_hash=sha256_json(report_payload),
        created_at=utcnow(),
    )
    session.add(report)
    exam.lifecycle = "submitted"
    exam.sealed = True
    exam.completed_at = utcnow()
    session.flush()
    append_event(
        session,
        owner_id=owner_id,
        aggregate_type="exam_session",
        aggregate_id=exam.id,
        event_type="exam_simulation_submitted",
        payload={"report_hash": report.report_hash, "correct_count": correct_count},
        idempotency_key=f"exam:{exam.id}:submitted",
    )
    return report

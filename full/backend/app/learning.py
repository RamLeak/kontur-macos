"""Evidence-first learning projections and deterministic review planning."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from .events import append_event
from .exam_scoring import PRACTICE_SCORING_VERSION
from .models import (
    Attempt,
    DailyPlan,
    ErrorEvent,
    ErrorPattern,
    ExamReport,
    ExamSession,
    LearningEvent,
    MasteryEvidence,
    MasteryState,
    ReviewItem,
    Skill,
    SolveSession,
    TaskRevision,
    TaskSkill,
    utcnow,
)
from .util import sha256_json

TAXONOMY_VERSION = "v1"
MASTERY_ALGORITHM_VERSION = "mastery-v1"
DAILY_PLAN_VERSION = "daily-plan-v1"

SKILL_TITLES: dict[int, str] = {
    1: "Графы, таблицы и информационные модели",
    2: "Таблицы истинности и логические функции",
    3: "Фильтрация и агрегация таблиц",
    4: "Префиксные коды и условие Фано",
    5: "Исполнение простых алгоритмов",
    6: "Циклы, условия и трассировка программ",
    7: "Количество информации и передача данных",
    8: "Комбинаторика и перебор слов",
    9: "Обработка электронных таблиц",
    10: "Поиск в текстах и анализ документов",
    11: "Кодирование и объём информации",
    12: "Строковый редактор и системы переписывания",
    13: "IP-сети и маски адресов",
    14: "Системы счисления и ограничения цифр",
    15: "Логика предикатов и поиск параметров",
    16: "Рекурсия и рекурсивные функции",
    17: "Обработка последовательностей и файлов",
    18: "Табличная оптимизация и динамические пути",
    19: "Игры: выигрышный ход",
    20: "Игры: стратегия и глубина",
    21: "Игры: проигрышные и выигрышные позиции",
    22: "Параллельные процессы и графы зависимостей",
    23: "Программы-исполнители и динамический подсчёт",
    24: "Алгоритмы текстовых файлов",
    25: "Делители, маски и поиск целых чисел",
    26: "Сортировка, записи и эффективная обработка файлов",
    27: "Оптимизационные алгоритмы и большие входы",
}


def skill_id_for_line(line_number: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"ege-informatics-skill:{TAXONOMY_VERSION}:{line_number}"))


def seed_taxonomy(session: Session) -> None:
    for line_number, title in SKILL_TITLES.items():
        skill_id = skill_id_for_line(line_number)
        skill = session.get(Skill, skill_id)
        if skill is None:
            session.add(
                Skill(
                    id=skill_id,
                    code=f"informatics.line.{line_number}",
                    line_number=line_number,
                    title=title,
                    taxonomy_version=TAXONOMY_VERSION,
                    active=True,
                )
            )
        elif skill.title != title or not skill.active:
            skill.title = title
            skill.active = True
    session.flush()


def record_mastery_evidence(
    session: Session,
    *,
    owner_id: str,
    skill_id: str,
    evidence_type: str,
    value: dict[str, Any],
    attempt_id: str | None = None,
    policy_version: str = MASTERY_ALGORITHM_VERSION,
) -> MasteryEvidence:
    evidence = MasteryEvidence(
        owner_id=owner_id,
        skill_id=skill_id,
        attempt_id=attempt_id,
        evidence_type=evidence_type,
        value_json=value,
        policy_version=policy_version,
        created_at=utcnow(),
    )
    session.add(evidence)
    session.flush()
    # Evidence and its audit event share the caller's transaction.  No
    # projection is allowed to treat an evidence row as durable before this
    # append succeeds.
    append_event(
        session,
        owner_id=owner_id,
        aggregate_type="mastery_evidence",
        aggregate_id=evidence.id,
        event_type="mastery_evidence_recorded",
        payload={
            "skill_id": skill_id,
            "evidence_type": evidence_type,
            "attempt_id": attempt_id,
            "value": value,
            "policy_version": policy_version,
        },
        idempotency_key=f"mastery-evidence:{evidence.id}",
    )
    return evidence


def proven_practice_attempt_ids(
    session: Session,
    *,
    owner_id: str,
    solve_ids: set[str] | None = None,
) -> set[str]:
    """Return attempts backed by an immutable server answer-check event.

    A browser can preserve the exact scoring method string, so the method on
    ``Attempt`` is only a label.  The append-only ``practice_answer_checked``
    event, its owner, solve-session binding and result are the authority used
    by projections.  Historical attempt/evidence rows remain untouched when
    they do not have this provenance.
    """

    candidates: dict[str, tuple[str, bool]] = {}
    events = session.scalars(
        select(LearningEvent).where(
            LearningEvent.owner_id == owner_id,
            LearningEvent.aggregate_type == "answer_check",
            LearningEvent.event_type == "practice_answer_checked",
        )
    )
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if payload.get("scoring_version") != PRACTICE_SCORING_VERSION:
            continue
        request = payload.get("request")
        result = payload.get("result")
        if not isinstance(request, dict) or not isinstance(result, dict):
            continue
        attempt_id = result.get("attempt_id")
        candidate_solve_id = request.get("solve_session_id")
        correct = result.get("correct")
        status = result.get("status")
        if (
            not isinstance(attempt_id, str)
            or not isinstance(candidate_solve_id, str)
            or not isinstance(correct, bool)
            or status != ("correct" if correct else "incorrect")
            or event.aggregate_id != attempt_id
        ):
            continue
        candidates[attempt_id] = (candidate_solve_id, correct)
    if not candidates:
        return set()

    query = (
        select(Attempt, SolveSession)
        .join(SolveSession, SolveSession.id == Attempt.solve_session_id)
        .join(TaskRevision, TaskRevision.id == SolveSession.task_revision_id)
        .where(
            Attempt.owner_id == owner_id,
            SolveSession.owner_id == owner_id,
            Attempt.id.in_(list(candidates)),
        )
    )
    if solve_ids is not None:
        query = query.where(Attempt.solve_session_id.in_(solve_ids))
    proven: set[str] = set()
    for attempt, solve in session.execute(query):
        candidate = candidates.get(attempt.id)
        if candidate is None or candidate[0] != solve.id:
            continue
        if solve.status.casefold() in {"exam", "exam_active", "exam_submitted", "sealed_exam"}:
            continue
        expected_correct = candidate[1]
        expected_status = "solved" if expected_correct else "failed"
        if (
            attempt.status == expected_status
            and attempt.verified_correct is expected_correct
            and attempt.verification_method == f"server:{PRACTICE_SCORING_VERSION}"
        ):
            proven.add(attempt.id)
    return proven


def proven_exam_report_ids(session: Session, *, owner_id: str) -> set[str]:
    """Return submitted exam reports with matching immutable audit events."""

    reports = session.execute(
        select(ExamReport, ExamSession)
        .join(ExamSession, ExamSession.id == ExamReport.exam_session_id)
        .where(
            ExamSession.owner_id == owner_id,
            ExamSession.lifecycle == "submitted",
        )
    ).all()
    if not reports:
        return set()
    events = session.scalars(
        select(LearningEvent).where(
            LearningEvent.owner_id == owner_id,
            LearningEvent.aggregate_type == "exam_session",
            LearningEvent.event_type == "exam_simulation_submitted",
        )
    )
    event_hashes: dict[str, set[str]] = {}
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        report_hash = payload.get("report_hash")
        if isinstance(report_hash, str):
            event_hashes.setdefault(event.aggregate_id, set()).add(report_hash)
    trusted: set[str] = set()
    for report, exam in reports:
        report_json = report.report_json if isinstance(report.report_json, dict) else {}
        if (
            report_json.get("exam_session_id") == exam.id
            and report.report_hash == sha256_json(report_json)
            and report.report_hash in event_hashes.get(exam.id, set())
        ):
            trusted.add(exam.id)
    return trusted


def _mastery_for_values(values: list[dict[str, Any]]) -> tuple[str, int, int, float]:
    evidence_count = len(values)
    independent_successes = sum(
        1 for value in values if bool(value.get("correct")) and bool(value.get("independent", True))
    )
    hint_total = sum(max(0, int(value.get("hint_count", 0) or 0)) for value in values)
    hint_dependency = round(hint_total / evidence_count, 4) if evidence_count else 0.0
    if independent_successes >= 2 and hint_dependency <= 0.25:
        status = "mastered"
    elif independent_successes >= 1:
        status = "developing"
    else:
        status = "insufficient_evidence"
    return status, evidence_count, independent_successes, hint_dependency


def rebuild_mastery(session: Session, *, owner_id: str) -> dict[str, Any]:
    """Rebuild owner mastery rows from immutable evidence only."""

    seed_taxonomy(session)
    skills = list(session.scalars(select(Skill).where(Skill.active.is_(True)).order_by(Skill.line_number)))
    proven_attempts = proven_practice_attempt_ids(session, owner_id=owner_id)
    proven_exams = proven_exam_report_ids(session, owner_id=owner_id)
    output: list[dict[str, Any]] = []
    generation = str(uuid5(NAMESPACE_URL, f"mastery-generation:{owner_id}:{MASTERY_ALGORITHM_VERSION}"))
    for skill in skills:
        evidence = list(
            session.scalars(
                select(MasteryEvidence)
                .where(MasteryEvidence.owner_id == owner_id, MasteryEvidence.skill_id == skill.id)
                .order_by(MasteryEvidence.created_at, MasteryEvidence.id)
            )
        )
        values = []
        for item in evidence:
            value = item.value_json if isinstance(item.value_json, dict) else {}
            if item.evidence_type == "attempt" and item.attempt_id in proven_attempts:
                values.append(value)
            elif item.evidence_type == "exam_attempt" and value.get("exam_id") in proven_exams:
                values.append(value)
        status, evidence_count, independent_successes, hint_dependency = _mastery_for_values(values)
        state = session.scalar(
            select(MasteryState).where(
                MasteryState.owner_id == owner_id,
                MasteryState.skill_id == skill.id,
            )
        )
        if state is None:
            state = MasteryState(owner_id=owner_id, skill_id=skill.id)
            session.add(state)
        state.status = status
        state.evidence_count = evidence_count
        state.independent_successes = independent_successes
        state.hint_dependency = hint_dependency
        state.algorithm_version = MASTERY_ALGORITHM_VERSION
        state.generation = generation
        state.updated_at = utcnow()
        output.append(
            {
                "skill_id": skill.id,
                "line_number": skill.line_number,
                "title": skill.title,
                "status": status,
                "evidence_count": evidence_count,
                "independent_successes": independent_successes,
                "hint_dependency": hint_dependency if evidence_count else None,
            }
        )
    session.flush()
    output_hash = sha256_json(output)
    return {
        "algorithm_version": MASTERY_ALGORITHM_VERSION,
        "generation": generation,
        "projection_hash": output_hash,
        "items": output,
    }


def task_skill_for_revision(session: Session, revision_id: str) -> list[TaskSkill]:
    return list(
        session.scalars(
            select(TaskSkill).where(
                TaskSkill.revision_id == revision_id,
                TaskSkill.mapping_status == "confirmed",
                TaskSkill.taxonomy_version == TAXONOMY_VERSION,
            )
        )
    )


def create_review_for_error(
    session: Session,
    *,
    owner_id: str,
    error: ErrorEvent,
    reason_codes: list[str] | None = None,
) -> ReviewItem:
    marker = f"error:{error.id}"
    existing = next(
        (
            item
            for item in session.scalars(select(ReviewItem).where(ReviewItem.owner_id == owner_id))
            if marker in item.reason_codes
        ),
        None,
    )
    if existing is not None:
        return existing
    pattern = session.scalar(
        select(ErrorPattern).where(
            ErrorPattern.owner_id == owner_id,
            ErrorPattern.normalized_type == error.error_type,
        )
    )
    if pattern is None:
        pattern = ErrorPattern(
            owner_id=owner_id,
            normalized_type=error.error_type,
            count=0,
            last_at=utcnow(),
        )
        session.add(pattern)
        session.flush()
    pattern.count += 1
    pattern.last_at = utcnow()
    task_line = error.task_line
    if task_line is None and error.attempt_id:
        task_line = session.scalar(
            select(TaskRevision.line_number)
            .join(SolveSession, SolveSession.task_revision_id == TaskRevision.id)
            .join(Attempt, Attempt.solve_session_id == SolveSession.id)
            .where(Attempt.id == error.attempt_id, Attempt.owner_id == owner_id)
        )
    if task_line is not None:
        error.task_line = int(task_line)
    reason = error.reason.strip() if error.reason else ""
    if not reason:
        reason = error.description.strip() if error.description else ""
    if not reason:
        reason = f"Подтверждённая ошибка типа «{error.error_type}»"
    error.reason = reason
    item = ReviewItem(
        owner_id=owner_id,
        skill_id=None,
        error_pattern_id=pattern.id,
        due_at=utcnow() + timedelta(days=1),
        interval_days=1,
        reason_codes=sorted(set((reason_codes or []) + ["confirmed_error", marker])),
        reason=reason,
        evidence_ids=[error.id],
        task_line=task_line,
        policy_version=MASTERY_ALGORITHM_VERSION,
        status="due",
        created_at=utcnow(),
    )
    session.add(item)
    session.flush()
    return item


def complete_review(session: Session, *, owner_id: str, review: ReviewItem, success: bool) -> None:
    if review.owner_id != owner_id:
        raise ValueError("review does not belong to owner")
    # A successful review is not terminal: it is scheduled for its next
    # interval and becomes due again only when that timestamp is reached.
    review.status = "scheduled" if success else "due"
    review.completed_at = utcnow() if success else None
    if success:
        review.interval_days = min(365, max(1, review.interval_days * 2))
        review.due_at = utcnow() + timedelta(days=review.interval_days)
    else:
        review.interval_days = 1
        review.due_at = utcnow() + timedelta(days=1)
    append_event(
        session,
        owner_id=owner_id,
        aggregate_type="review_item",
        aggregate_id=review.id,
        event_type="review_completed",
        payload={"success": success, "interval_days": review.interval_days},
        idempotency_key=f"review:{review.id}:{review.completed_at or review.due_at}",
    )


def build_daily_plan(session: Session, *, owner_id: str, plan_date: str) -> DailyPlan:
    due = list(
        session.scalars(
            select(ReviewItem)
            .where(
                ReviewItem.owner_id == owner_id,
                ReviewItem.status == "due",
                ReviewItem.due_at <= utcnow(),
            )
            .order_by(ReviewItem.due_at, ReviewItem.id)
            .limit(20)
        )
    )
    items = [
        {
            "review_item_id": item.id,
            "reason_codes": item.reason_codes,
            "evidence_ids": item.evidence_ids,
        }
        for item in due
    ]
    reason = "due_reviews_first" if items else "insufficient_due_evidence"
    plan = session.scalar(select(DailyPlan).where(DailyPlan.owner_id == owner_id, DailyPlan.plan_date == plan_date))
    if plan is None:
        plan = DailyPlan(owner_id=owner_id, plan_date=plan_date)
        session.add(plan)
    plan.algorithm_version = DAILY_PLAN_VERSION
    plan.items = items
    plan.reason = reason
    plan.created_at = utcnow()
    session.flush()
    return plan

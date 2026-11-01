"""Owner-scoped difficulty variant templates, reusing the exam lifecycle."""

from __future__ import annotations

from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.orm import Session

from .events import append_event
from .exam_scoring import FIPI_SHA256, PRACTICE_SCORING_VERSION
from .models import ExamBlueprint
from .official_blueprints import EGE_2027_TEMPLATE

DIFFICULTY_NAMES = {"basic": "Базовый", "medium": "Средний", "hard": "Сложный"}


def difficulty_blueprint(session: Session, *, owner_id: str, difficulty: str) -> ExamBlueprint:
    template_id = f"practice-2027-difficulty-{difficulty}-v1"
    existing = session.scalar(
        select(ExamBlueprint).where(
            ExamBlueprint.owner_id == owner_id,
            ExamBlueprint.constraints["reference"]["template_id"].as_string() == template_id,
        )
    )
    if existing is not None:
        return existing
    constraints = deepcopy(EGE_2027_TEMPLATE["constraints"])
    constraints["selection"]["difficulty"] = difficulty
    constraints["exam"].update({"scoring_version": PRACTICE_SCORING_VERSION, "target_difficulty_count": 22})
    constraints["reference"].update(
        {"template_id": template_id, "verified_at": "2026-09-06", "source_sha256": FIPI_SHA256}
    )
    blueprint = ExamBlueprint(
        owner_id=owner_id,
        name=f"{DIFFICULTY_NAMES[difficulty]} вариант",
        exam_year=2027,
        required_lines={str(line): 1 for line in range(1, 28)},
        constraints=constraints,
        version=1,
    )
    session.add(blueprint)
    session.flush()
    append_event(
        session,
        owner_id=owner_id,
        aggregate_type="exam_blueprint",
        aggregate_id=blueprint.id,
        event_type="difficulty_blueprint_created",
        payload={"template_id": template_id},
        idempotency_key=f"blueprint-template:{template_id}",
    )
    return blueprint

"""Versioned official-reference exam templates.

The 2027 material is still a FIPI project as of 2026-09-03. The template is
therefore deliberately labelled provisional and can be instantiated without
silently mutating an owner's existing blueprint.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .api_common import api_error, ensure_owner_row, get_db, owner_session
from .auth import OwnerPrincipal
from .events import append_event
from .models import ExamBlueprint, utcnow

TEMPLATE_ID = "fipi-informatics-2027-project-2026-08-21"
FIPI_ARCHIVE_URL = "https://doc.fipi.ru/ege/demoversii-specifikacii-kodifikatory/2027/inf_11_2027.zip"
FIPI_ARCHIVE_SHA256 = "1cd38aab1ed2ec408948cecc71257c1a1894f76ef4974a6f07925e58c641f6d9"

EGE_2027_TEMPLATE: dict[str, Any] = {
    "template_id": TEMPLATE_ID,
    "name": "ЕГЭ по информатике 2027 — проект ФИПИ",
    "exam_year": 2027,
    "required_lines": {str(line): 1 for line in range(1, 28)},
    "constraints": {
        "selection": {
            "source_name": "kompege",
            "rights_statuses": ["private_authorized", "public_authorized"],
            "require_assets_by_line": [3, 9, 17, 18, 22, 23, 24, 26, 27],
            "require_answer": True,
        },
        "exam": {"total_tasks": 27, "duration_minutes": 235},
        "reference": {
            "template_id": TEMPLATE_ID,
            "status": "provisional",
            "publisher": "ФИПИ",
            "published_document_date": "2026-08-21",
            "verified_at": "2026-09-03",
            "review_after": "2026-10-01",
            "source_url": FIPI_ARCHIVE_URL,
            "source_sha256": FIPI_ARCHIVE_SHA256,
            "note": "Проект КИМ 2027; требуется повторная проверка после утверждения ФИПИ.",
        },
    },
}


def _template_view() -> dict[str, Any]:
    return deepcopy(EGE_2027_TEMPLATE)


def _blueprint_view(blueprint: ExamBlueprint) -> dict[str, Any]:
    return {
        "id": blueprint.id,
        "name": blueprint.name,
        "exam_year": blueprint.exam_year,
        "required_lines": blueprint.required_lines,
        "constraints": blueprint.constraints,
        "version": blueprint.version,
        "created_at": blueprint.created_at.isoformat() + "Z",
    }


router = APIRouter()


@router.get("/exam-blueprint-templates/{exam_year}")
def get_template(
    exam_year: int,
    _session: Annotated[Session, Depends(get_db)],
    _principal: Annotated[OwnerPrincipal, Depends(owner_session)],
) -> dict[str, Any]:
    if exam_year != 2027:
        api_error(404, "NOT_FOUND", "exam blueprint template not found")
    return _template_view()


@router.post("/exam-blueprint-templates/{exam_year}/instantiate", status_code=201)
def instantiate_template(
    exam_year: int,
    session: Annotated[Session, Depends(get_db)],
    principal: Annotated[OwnerPrincipal, Depends(owner_session)],
) -> dict[str, Any]:
    if exam_year != 2027:
        api_error(404, "NOT_FOUND", "exam blueprint template not found")
    template = _template_view()
    with session.begin():
        ensure_owner_row(session, principal.owner_id)
        candidates = list(
            session.scalars(
                select(ExamBlueprint)
                .where(ExamBlueprint.owner_id == principal.owner_id, ExamBlueprint.exam_year == 2027)
                .order_by(ExamBlueprint.created_at, ExamBlueprint.id)
            )
        )
        blueprint = next(
            (
                item
                for item in candidates
                if isinstance(item.constraints, dict)
                and isinstance(item.constraints.get("reference"), dict)
                and item.constraints["reference"].get("template_id") == TEMPLATE_ID
            ),
            None,
        )
        duplicate = blueprint is not None
        if blueprint is None:
            blueprint = ExamBlueprint(
                owner_id=principal.owner_id,
                name=template["name"],
                exam_year=template["exam_year"],
                required_lines=template["required_lines"],
                constraints=template["constraints"],
                version=1,
                created_at=utcnow(),
            )
            session.add(blueprint)
            session.flush()
            append_event(
                session,
                owner_id=principal.owner_id,
                aggregate_type="exam_blueprint",
                aggregate_id=blueprint.id,
                event_type="official_exam_blueprint_instantiated",
                payload={"template_id": TEMPLATE_ID, "exam_year": 2027, "status": "provisional"},
                idempotency_key=f"blueprint-template:{TEMPLATE_ID}",
            )
    return {"blueprint": _blueprint_view(blueprint), "duplicate": duplicate}

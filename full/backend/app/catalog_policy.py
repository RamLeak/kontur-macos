"""Pure selection policy for the recent, difficulty-balanced KЕГЭ catalog."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .util import sha256_json

POLICY_VERSION = "recent-difficulty-v1"
DIFFICULTY_MAPPING_VERSION = "kompege-ui-difficulty-v1"
YEAR_EVIDENCE_VERSION = "kompege-year-evidence-v1"
PRIMARY_YEARS = (2025, 2026, 2027)
FALLBACK_YEAR = 2024
FALLBACK_THRESHOLD = 30
TARGET_PER_CELL = 50
DIFFICULTIES = ("easy", "medium", "hard")
DIFFICULTY_BY_SOURCE = {0: "easy", 1: "medium", 2: "hard"}

_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")


class CatalogPolicyError(ValueError):
    """Source records cannot be selected without guessing their identity."""


@dataclass(frozen=True, slots=True)
class CatalogSelection:
    selected: list[dict[str, Any]]
    cells: list[dict[str, Any]]
    exclusions: dict[str, int]
    total_selected: int
    quota_met: bool


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC).isoformat()


def _explicit_year(record: dict[str, Any]) -> int | None:
    raw = record.get("examYear")
    if raw is None:
        raw = record.get("exam_year")
    if raw is None:
        raw = record.get("year")
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if 2000 <= value <= 2100 else None


def _label(record: dict[str, Any]) -> str | None:
    raw = record.get("comment")
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return value[:2000] if value else None


def normalize_source_metadata(
    record: dict[str, Any], *, inherited: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Normalize only evidenced year/difficulty fields from one source record.

    A child in a grouped 19–21 task may inherit fields that the child record
    does not carry. Every inherited field is recorded explicitly in bounded
    metadata; retrieval time and the learner target year are never evidence.
    """

    inherited = inherited or {}
    inherited_fields: list[str] = []

    raw_difficulty = record.get("difficulty")
    if raw_difficulty is None:
        raw_difficulty = inherited.get("source_difficulty")
        if raw_difficulty is not None:
            inherited_fields.append("difficulty")
    if isinstance(raw_difficulty, bool) or raw_difficulty not in DIFFICULTY_BY_SOURCE:
        source_difficulty = None
        difficulty = None
    else:
        source_difficulty = int(raw_difficulty)
        difficulty = DIFFICULTY_BY_SOURCE[source_difficulty]

    label = _label(record)
    label_inherited = False
    if label is None and isinstance(inherited.get("source_label"), str):
        label = str(inherited["source_label"])
        label_inherited = True
        inherited_fields.append("source_label")
    label_years = {int(value) for value in _YEAR_RE.findall(label or "") if 2000 <= int(value) <= 2100}
    label_year = next(iter(label_years)) if len(label_years) == 1 else None
    explicit_year = _explicit_year(record)

    local_created_value = record.get("createdAt") or record.get("created_at")
    created_at = _timestamp(local_created_value)
    if created_at is None and isinstance(inherited.get("source_created_at"), str):
        created_at = str(inherited["source_created_at"])
        inherited_fields.append("source_created_at")

    conflict = explicit_year is not None and label_year is not None and explicit_year != label_year
    ambiguous_label = len(label_years) > 1
    if conflict or ambiguous_label:
        source_year = None
        source_year_basis = None
    elif explicit_year is not None:
        source_year = explicit_year
        source_year_basis = "source-explicit-year"
    elif label_year is not None:
        source_year = label_year
        source_year_basis = "source-label-year"
        if label_inherited:
            inherited_fields.append("source_year")
    elif local_created_value is not None:
        source_year = datetime.fromisoformat(created_at).year if created_at is not None else None
        source_year_basis = "source-created-at" if source_year is not None else None
    else:
        inherited_year = inherited.get("source_year")
        inherited_basis = inherited.get("source_year_basis")
        if isinstance(inherited_year, int) and isinstance(inherited_basis, str):
            source_year = inherited_year
            source_year_basis = inherited_basis
            inherited_fields.append("source_year")
        elif created_at is not None:
            source_year = datetime.fromisoformat(created_at).year
            source_year_basis = "source-created-at"
        else:
            source_year = None
            source_year_basis = None

    updated_at = _timestamp(record.get("updatedAt") or record.get("updated_at"))
    evidence: dict[str, Any] = {
        "difficulty_mapping_version": DIFFICULTY_MAPPING_VERSION,
        "year_evidence_version": YEAR_EVIDENCE_VERSION,
    }
    if inherited_fields:
        evidence["inherited_fields"] = sorted(set(inherited_fields))
    if conflict:
        evidence["year_conflict"] = True
    if ambiguous_label:
        evidence["year_label_ambiguous"] = True
    if updated_at is not None:
        evidence["source_updated_at"] = updated_at

    return {
        "source_year": source_year,
        "source_year_basis": source_year_basis,
        "source_created_at": created_at,
        "source_difficulty": source_difficulty,
        "difficulty": difficulty,
        "source_label": label,
        "source_metadata": evidence,
    }


def _newest_first(item: dict[str, Any]) -> tuple[int, float, str]:
    created = item.get("source_created_at")
    timestamp = 0.0
    if isinstance(created, str):
        try:
            timestamp = datetime.fromisoformat(created).timestamp()
        except ValueError:
            timestamp = 0.0
    return (-int(item["source_year"]), -timestamp, str(item["source_id"]))


def select_recent_catalog(candidates: list[dict[str, Any]]) -> CatalogSelection:
    """Apply the frozen 27×3 quota and the conditional 2024 fallback."""

    unique: dict[str, tuple[str, dict[str, Any]]] = {}
    exclusions = {
        "duplicate": 0,
        "conflict": 0,
        "unknown_year": 0,
        "old_year": 0,
        "unknown_difficulty": 0,
    }
    for item in candidates:
        source_id = str(item.get("source_id") or "")
        if not source_id:
            raise CatalogPolicyError("normalized source record has no source_id")
        fingerprint = sha256_json(item)
        previous = unique.get(source_id)
        if previous is not None:
            if previous[0] != fingerprint:
                exclusions["conflict"] += 1
                raise CatalogPolicyError(f"conflicting normalized source record: {source_id}")
            exclusions["duplicate"] += 1
            continue
        unique[source_id] = (fingerprint, item)

    grouped: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for _fingerprint, item in unique.values():
        raw_line = item.get("line_number")
        if isinstance(raw_line, bool) or not isinstance(raw_line, (int, str)):
            raise CatalogPolicyError("normalized source record has an invalid line")
        try:
            line = int(raw_line)
        except (TypeError, ValueError) as exc:
            raise CatalogPolicyError("normalized source record has an invalid line") from exc
        if not 1 <= line <= 27:
            raise CatalogPolicyError("normalized source record has a line outside 1..27")
        difficulty = item.get("difficulty")
        if difficulty not in DIFFICULTIES:
            exclusions["unknown_difficulty"] += 1
            continue
        year = item.get("source_year")
        if isinstance(year, bool) or not isinstance(year, int):
            exclusions["unknown_year"] += 1
            continue
        if year not in {*PRIMARY_YEARS, FALLBACK_YEAR}:
            exclusions["old_year"] += 1
            continue
        grouped.setdefault((line, str(difficulty)), []).append(item)

    selected: list[dict[str, Any]] = []
    cells: list[dict[str, Any]] = []
    for line in range(1, 28):
        for difficulty in DIFFICULTIES:
            available = sorted(grouped.get((line, difficulty), []), key=_newest_first)
            primary = [item for item in available if item["source_year"] in PRIMARY_YEARS]
            fallback = [item for item in available if item["source_year"] == FALLBACK_YEAR]
            fallback_allowed = len(primary) < FALLBACK_THRESHOLD
            selected_primary = primary[:TARGET_PER_CELL]
            places = TARGET_PER_CELL - len(selected_primary)
            selected_fallback = fallback[:places] if fallback_allowed and places > 0 else []
            chosen = [*selected_primary, *selected_fallback]
            selected.extend(chosen)
            cells.append(
                {
                    "line_number": line,
                    "difficulty": difficulty,
                    "primary_available": len(primary),
                    "fallback_available": len(fallback),
                    "fallback_allowed": fallback_allowed,
                    "selected_primary": len(selected_primary),
                    "selected_fallback": len(selected_fallback),
                    "total": len(chosen),
                    "shortfall": TARGET_PER_CELL - len(chosen),
                }
            )

    return CatalogSelection(
        selected=selected,
        cells=cells,
        exclusions=exclusions,
        total_selected=len(selected),
        quota_met=all(cell["shortfall"] == 0 for cell in cells),
    )

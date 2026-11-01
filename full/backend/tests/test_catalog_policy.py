from __future__ import annotations

from backend.app.catalog_policy import normalize_source_metadata, select_recent_catalog
from backend.app.source_transport import _normalise_number_payload


def candidate(source_id: int, *, year: int | None, level: str | None) -> dict[str, object]:
    return {
        "source_id": str(source_id),
        "line_number": 1,
        "source_year": year,
        "source_created_at": f"{year}-06-01T10:00:00+00:00" if year is not None else None,
        "difficulty": level,
    }


def test_source_label_year_is_distinct_from_publication_year() -> None:
    result = normalize_source_metadata(
        {
            "comment": "Демоверсия 2027",
            "difficulty": 0,
            "createdAt": "2026-08-30T08:01:45.319Z",
        }
    )
    assert result["source_year"] == 2027
    assert result["source_year_basis"] == "source-label-year"
    assert result["source_created_at"].startswith("2026-08-30")
    assert result["source_difficulty"] == 0
    assert result["difficulty"] == "easy"


def test_ambiguous_or_conflicting_year_and_unknown_difficulty_are_not_guessed() -> None:
    ambiguous = normalize_source_metadata(
        {"comment": "Материалы 2025 и 2026", "difficulty": 9, "createdAt": "2026-01-01T00:00:00Z"}
    )
    assert ambiguous["source_year"] is None
    assert ambiguous["difficulty"] is None
    assert ambiguous["source_metadata"]["year_label_ambiguous"] is True

    conflict = normalize_source_metadata(
        {"year": 2025, "comment": "Демоверсия 2027", "difficulty": 1}
    )
    assert conflict["source_year"] is None
    assert conflict["source_metadata"]["year_conflict"] is True


def test_grouped_children_record_inherited_year_and_difficulty() -> None:
    normalized = _normalise_number_payload(
        [
            {
                "taskId": 1900,
                "number": 19,
                "text": "parent",
                "key": "1",
                "comment": "Демоверсия 2027",
                "difficulty": 2,
                "createdAt": "2026-08-30T08:01:45Z",
                "subTask": [{"number": 20, "text": "child", "key": "2"}],
            }
        ],
        "https://kompege.ru/api/v1/task/number/19",
    )
    child = next(item for item in normalized if item["line_number"] == 20)
    assert child["source_year"] == 2027
    assert child["difficulty"] == "hard"
    assert {"difficulty", "source_label", "source_created_at", "source_year"} <= set(
        child["source_metadata"]["inherited_fields"]
    )


def test_selection_never_uses_2023_or_unknown_metadata() -> None:
    result = select_recent_catalog(
        [
            candidate(1, year=2027, level="easy"),
            candidate(2, year=2025, level="easy"),
            candidate(3, year=2024, level="easy"),
            candidate(4, year=2023, level="easy"),
            candidate(5, year=None, level="easy"),
            candidate(6, year=2026, level=None),
        ]
    )
    assert [item["source_id"] for item in result.selected] == ["1", "2", "3"]
    assert result.exclusions["old_year"] == 1
    assert result.exclusions["unknown_year"] == 1
    assert result.exclusions["unknown_difficulty"] == 1

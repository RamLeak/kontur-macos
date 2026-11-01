"""Versioned practice scoring; official 2026 conversion, provisional 2027 KIM."""

from __future__ import annotations

import re
from typing import Any

from .util import normalized_answer

PRACTICE_SCORING_VERSION = "ege-practice-2027-v1"
SCALE_SOURCE = (
    "https://obrnadzor.gov.ru/wp-content/uploads/2026/04/tabliczy-sootvetstviya-pervichnyh-i-testovyh-ballov-ege.pdf"
)
FIPI_SOURCE = "https://doc.fipi.ru/ege/demoversii-specifikacii-kodifikatory/2027/inf_11_2027.zip"
FIPI_SHA256 = "cf7c8bbec5dd345cd054dc7dd41c8322891077e76175b767de0b80cc4dc2bbff"
# Table 6, p. 4: primary scores 0..29, Informatics column. No interpolation.
SCALE_2026 = (
    0,
    7,
    14,
    20,
    27,
    34,
    40,
    43,
    46,
    48,
    51,
    54,
    56,
    59,
    62,
    64,
    67,
    70,
    72,
    75,
    78,
    80,
    83,
    85,
    88,
    90,
    93,
    95,
    98,
    100,
)


def score_scale() -> dict[str, Any]:
    return {
        "year": 2026,
        "status": "official_2026",
        "source_url": SCALE_SOURCE,
        "verified_at": "2026-09-06",
        "values": list(SCALE_2026),
        "note": "Официальная шкала ЕГЭ-2026. Шкала 2027 ещё не подтверждена. "
        "Балл тренировочного варианта не является прогнозом результата ЕГЭ.",
    }


def _numeric_cells(value: str) -> list[str] | None:
    parts = re.split(r"[\s;,]+", value.strip())
    if not parts or not all(re.fullmatch(r"-?\d+", part) for part in parts):
        return None
    return parts


def practice_answer_text(value: str) -> str:
    """Some source keys contain literal line separators; never mutate provenance."""
    return value.replace(r"\r\n", "\n").replace(r"\n", "\n").replace(r"\r", "\n")


def points_for_answer(line: int, submitted: str, expected: str | None) -> tuple[int, int]:
    maximum = 2 if line in {26, 27} else 1
    if not expected or not submitted.strip():
        return 0, maximum
    submitted, expected = practice_answer_text(submitted), practice_answer_text(expected)
    actual_cells, expected_cells = _numeric_cells(submitted), _numeric_cells(expected)
    if expected_cells is not None and len(expected_cells) > 1:
        exact = actual_cells == expected_cells
    else:
        exact = normalized_answer(submitted) == normalized_answer(expected)
    if exact:
        return maximum, maximum
    # FIPI 2027 demo, pp. 27–28: two correct numbers swapped OR exactly one
    # correct number in its correct cell earns one point. Extra cells do not.
    if maximum == 2 and expected_cells is not None and len(expected_cells) == 2:
        if actual_cells is not None and 1 <= len(actual_cells) <= 2:
            if actual_cells == expected_cells[::-1] or any(
                a == e for a, e in zip(actual_cells, expected_cells, strict=False)
            ):
                return 1, maximum
    return 0, maximum

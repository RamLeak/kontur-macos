import pytest
from backend.app.exam_scoring import FIPI_SHA256, points_for_answer
from backend.app.official_blueprints import EGE_2027_TEMPLATE


@pytest.mark.parametrize("line,expected,answer,points", [
    (25, r"44 55\n66 77", "44 55\n66 77", 1),
    (25, r"44 55\n66 77", "44556677", 0),
    (26, r"12\n34", "12 34", 2),
    (26, r"12\n34", "12", 1),
    (27, r"12 34\n56 78", "12 34\n56 78", 2),
    (27, r"12 34\n56 78", "12 34", 0),
])
def test_practice_answers_accept_source_line_separators_without_merging_cells(line, expected, answer, points):
    assert points_for_answer(line, answer, expected) == (points, 2 if line in (26, 27) else 1)


def test_new_reference_does_not_rewrite_historical_official_snapshot():
    assert FIPI_SHA256 == "cf7c8bbec5dd345cd054dc7dd41c8322891077e76175b767de0b80cc4dc2bbff"
    assert EGE_2027_TEMPLATE["constraints"]["reference"]["source_sha256"] == (
        "1cd38aab1ed2ec408948cecc71257c1a1894f76ef4974a6f07925e58c641f6d9"
    )

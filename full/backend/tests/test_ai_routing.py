from __future__ import annotations

import pytest
from backend.app.ai_gateway import MODEL_POLICY_VERSION, AiError, route_model


@pytest.mark.parametrize(
    ("model", "effort"),
    tuple(
        (f"gpt-5.6-{model}", effort)
        for model in ("luna", "terra", "sol")
        for effort in ("low", "medium", "high", "xhigh")
    ),
)
def test_manual_route_is_never_replaced(model: str, effort: str) -> None:
    route = route_model(
        model_selection=model,
        effort=effort,
        line_number=27,
        tutor_mode="full_solution",
    )

    assert route.requested_model == model
    assert route.requested_effort == effort
    assert route.effective_model == model
    assert route.effective_effort == effort
    assert route.reason == "manual override"
    assert route.policy_version == MODEL_POLICY_VERSION


@pytest.mark.parametrize("line_number", (24, 26, 27))
def test_auto_routes_complex_informatics_lines_to_sol(line_number: int) -> None:
    route = route_model(
        model_selection="auto",
        effort=None,
        line_number=line_number,
        tutor_mode="hint",
    )

    assert (route.effective_model, route.effective_effort) == ("gpt-5.6-sol", "high")


@pytest.mark.parametrize("line_number", (19, 20, 21))
@pytest.mark.parametrize("tutor_mode", ("compare", "full_solution"))
def test_auto_routes_deep_group_analysis_to_sol(line_number: int, tutor_mode: str) -> None:
    route = route_model(
        model_selection="auto",
        effort=None,
        line_number=line_number,
        tutor_mode=tutor_mode,
    )

    assert (route.effective_model, route.effective_effort) == ("gpt-5.6-sol", "high")


@pytest.mark.parametrize("line_number", range(1, 9))
@pytest.mark.parametrize("tutor_mode", ("hint", "socratic"))
def test_auto_routes_introductory_hints_to_luna(line_number: int, tutor_mode: str) -> None:
    route = route_model(
        model_selection="auto",
        effort=None,
        line_number=line_number,
        tutor_mode=tutor_mode,
    )

    assert (route.effective_model, route.effective_effort) == ("gpt-5.6-luna", "medium")


@pytest.mark.parametrize(
    ("line_number", "tutor_mode"),
    ((9, "hint"), (18, "full_solution"), (22, "compare"), (None, "debug")),
)
def test_auto_routes_remaining_requests_to_terra(line_number: int | None, tutor_mode: str) -> None:
    route = route_model(
        model_selection="auto",
        effort=None,
        line_number=line_number,
        tutor_mode=tutor_mode,
    )

    assert (route.effective_model, route.effective_effort) == (
        "gpt-5.6-terra",
        "medium",
    )


@pytest.mark.parametrize(
    ("model", "effort"),
    (
        ("gpt-5.6-sol", "max"),
        ("gpt-5.6-terra", "none"),
        ("gpt-5.6-luna", "unknown"),
        ("gpt-5.6-unknown", "high"),
    ),
)
def test_unknown_manual_pair_fails_closed(model: str, effort: str) -> None:
    with pytest.raises(AiError, match="not allowlisted") as captured:
        route_model(
            model_selection=model,
            effort=effort,
            line_number=1,
            tutor_mode="hint",
        )

    assert captured.value.code == "MODEL_ROUTE_UNAVAILABLE"

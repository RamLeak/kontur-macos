"""Exam browser runs retain the same data-only backup boundaries as practice."""

from urllib.parse import quote

import pytest
from test_offline_state import get, put

from offline_state import StateValidationError, validate_client_state

PREFIX = "kontur:device-python-runs:v1:"
SCOPE = "exam:exam-1:session-2:item:item-3:browser"


def state(scope, runs=None):
    return {"version": 1, "entries": {PREFIX + quote(scope, safe="-_.!~*'()"): [] if runs is None else runs}}


@pytest.mark.parametrize("scope", [SCOPE, "practice:session-1:browser"])
def test_actual_browser_scopes_are_admitted(scope):
    value = state(scope)
    assert validate_client_state(value) == value


@pytest.mark.parametrize(
    "scope",
    [
        "exam:exam-1:item:item-3:browser",
        "exam:exam-1:session-2:item:item-3:server",
        "exam:exam-1:session-2:extra:item:item-3:browser",
        "exam:exam-1::item:item-3:browser",
        "exam:exam-1:constructor:item:item-3:browser",
        "exam:exam-1:session-2:item:../item:browser",
        "exam:" + "x" * 161 + ":session-2:item:item-3:browser",
    ],
)
def test_unrelated_or_malformed_exam_scopes_are_rejected(scope):
    with pytest.raises(StateValidationError):
        validate_client_state(state(scope))


def test_noncanonical_encoding_and_existing_run_count_bound_still_rejected():
    value = state(SCOPE)
    encoded = next(iter(value["entries"]))
    value["entries"] = {encoded.replace("%3A", "%3a", 1): []}
    with pytest.raises(StateValidationError):
        validate_client_state(value)
    with pytest.raises(StateValidationError):
        validate_client_state(state(SCOPE, [{} for _ in range(21)]))


def test_exam_runs_persist_in_full_backup_between_initialized_profiles(seed_factory, offline_factory, tmp_path):
    bundle, _ = seed_factory()
    source = offline_factory(bundle, tmp_path / "exam-history-source")
    target = offline_factory(bundle, tmp_path / "exam-history-target")
    runs = [
        {
            "id": "exam-run-1", "status": "completed", "stdout": "42\n", "stderr": "",
            "duration_ms": 1, "exit_code": 0, "created_at": "2026-01-01T00:00:00.000Z",
        }
    ]
    entries = state(SCOPE, runs)["entries"]
    assert put(source, 0, entries)["status"] == 200
    assert source.flush()["committed"] is True
    archive = tmp_path / "exam-history.kontur"
    receipt = source.transfer.export_bundle(archive)
    target.transfer.preview(archive)
    target.transfer.import_bundle(archive, receipt["contentSha256"])
    assert get(target)["value"]["entries"] == entries

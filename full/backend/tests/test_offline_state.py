"""Typed UI-state persistence, original defaults, and optimistic rollback."""

from __future__ import annotations

import base64
import json

import pytest

from offline_state import MAX_BYTES, StateValidationError, validate_client_state


def value(entries):
    return {"version": 1, "entries": entries}


def get(application):
    response = application.request("GET", "/api/v1/local/client-state")
    assert response["status"] == 200
    return json.loads(base64.b64decode(response["bodyBase64"]))


def put(application, version, entries):
    return application.request(
        "PUT",
        "/api/v1/local/client-state",
        json.dumps({"expectedVersion": version, "value": value(entries)}, ensure_ascii=False, separators=(",", ":")),
    )


def test_original_macos_redo_and_false_exam_flag_roundtrip():
    entries = {
        "kontur:shortcut-profile:macos:v1": {"board.redo": "Cmd+Shift+KeyZ", "board.undo": "Shift+Cmd+KeyZ"},
        "kontur:exam-ui:v1:exam-1": {"itemId": None, "flags": {"item-1": False}, "surfaces": {"item-1": "code"}},
    }
    assert validate_client_state(value(entries)) == value(entries)


@pytest.mark.parametrize(
    "entries",
    [
        {"auth-token": "forbidden"},
        {"kontur:workspace-split:v3": float("nan")},
        {"kontur:shortcut-profile:windows:v1": {"board.undo": "Ctrl+Ctrl+KeyZ"}},
        {"kontur:shortcut-profile:macos:v1": {"board.undo": "Cmd+KeyQ"}},
        {"kontur:exam-ui:v1:exam-1": {"itemId": None, "flags": {"item-1": 1}, "surfaces": {}}},
        {"kontur:device-python-runs:v1:practice%3asession-1%3Abrowser": []},
    ],
)
def test_invalid_or_nonallowlisted_values_are_rejected(entries):
    with pytest.raises(StateValidationError):
        validate_client_state(value(entries))


def test_state_save_stale_invalid_and_restart(seed_factory, offline_factory, tmp_path):
    bundle, _receipt = seed_factory()
    profile = tmp_path / "state-profile"
    application = offline_factory(bundle, profile)
    assert get(application) == {"version": 0, "value": value({})}
    entries = {"kontur:interface-preferences:v1": {"theme": "dark"}, "kontur:workspace-split:v3": 52.5}
    assert put(application, 0, entries)["status"] == 200
    saved = get(application)
    assert saved == {"version": 1, "value": value(entries)}
    assert put(application, 0, {})["status"] == 409
    assert put(application, 1, {"auth-token": "x"})["status"] == 422
    assert get(application) == saved
    assert application.flush()["committed"] is True
    application.close()
    assert get(offline_factory(bundle, profile)) == saved


def test_4mib_original_code_history_persists_without_truncation(seed_factory, offline_factory, tmp_path):
    bundle, _receipt = seed_factory()
    profile = tmp_path / "history-profile"
    application = offline_factory(bundle, profile)
    runs = [
        {
            "id": f"run-{index}",
            "status": "completed",
            "stdout": "é" * 110000,
            "stderr": "",
            "duration_ms": 1,
            "exit_code": 0,
            "created_at": "2026-10-29T00:00:00.000Z",
        }
        for index in range(20)
    ]
    entries = {"kontur:device-python-runs:v1:practice%3Asession-1%3Abrowser": runs}
    size = len(json.dumps(value(entries), ensure_ascii=False).encode("utf-8"))
    assert 4 * 1024**2 < size < MAX_BYTES
    assert put(application, 0, entries)["status"] == 200
    assert get(application) == {"version": 1, "value": value(entries)}
    application.flush()
    application.close()
    assert get(offline_factory(bundle, profile)) == {"version": 1, "value": value(entries)}

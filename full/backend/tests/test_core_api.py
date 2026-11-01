from __future__ import annotations

import base64
from pathlib import Path

import pytest
from backend.app.assets import AssetError, detect_type
from backend.app.auth import issue_session_token
from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.models import LearningEvent, SourceSnapshot, TaskRevision
from backend.app.source_catalog import sanitize_statement
from fastapi.testclient import TestClient
from sqlalchemy import func, select


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        asset_root=tmp_path / "assets",
        owner_session_secret="test-owner-secret",
        source_mode="fixture",
        source_permission_status="private_authorized",
        testing=True,
        max_context_chars=4_000,
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        test_client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        yield test_client


def fixture_payload(
    source_id: str = "task-1", line: int = 1, statement: str = "<p>Find <b>x</b>.</p><script>alert(1)</script>"
) -> dict[str, object]:
    return {
        "source_url": f"https://kompege.ru/task?id={source_id}",
        "source_id": source_id,
        "line_number": line,
        "exam_year": 2026,
        "statement": statement,
        "answer": "42",
        "rights_status": "private_authorized",
        "assets": [
            {
                "url": "https://kompege.ru/files/input.txt",
                "filename": "input.txt",
                "media_type": "text/plain",
                "content_base64": base64.b64encode(b"42\n").decode(),
            }
        ],
    }


def test_default_auth_fails_closed() -> None:
    app = create_app(Settings(database_url="sqlite+pysqlite:///:memory:"))
    with TestClient(app) as client:
        response = client.get("/api/v1/capabilities")
    assert response.status_code == 503
    assert response.json()["code"] == "AUTH_UNAVAILABLE"
    assert "detail" not in response.json()


def test_fixture_import_is_idempotent_and_statement_is_inert(client: TestClient) -> None:
    payload = fixture_payload()
    first = client.post("/api/v1/source/import-fixture", json=payload, headers={"Idempotency-Key": "fixture-1"})
    second = client.post("/api/v1/source/import-fixture", json=payload, headers={"Idempotency-Key": "fixture-1"})
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["duplicate"] is True
    task = first.json()["task"]
    assert "<script>" not in task["revision"]["statement"]
    assert task["revision"]["statement"] == "Find x."
    session = client.app.state.session_factory()
    try:
        assert session.scalar(select(func.count(TaskRevision.id))) == 1
        assert session.scalar(select(func.count(SourceSnapshot.id))) == 1
        assert session.scalar(select(func.count(LearningEvent.sequence))) >= 1
    finally:
        session.close()


def test_source_url_and_duplicate_filename_are_rejected(client: TestClient) -> None:
    bad = fixture_payload("bad")
    bad["source_url"] = "https://evil.example/task?id=bad"
    response = client.post("/api/v1/source/import-fixture", json=bad)
    assert response.status_code == 422
    assert response.json()["code"] == "SOURCE_HOST_BLOCKED"

    duplicate = fixture_payload("duplicate")
    duplicate["assets"] = [
        {"url": "https://kompege.ru/files/a.txt", "filename": "a?.txt", "content_base64": "YQ=="},
        {"url": "https://kompege.ru/files/b.txt", "filename": "a*.txt", "content_base64": "Yg=="},
    ]
    response = client.post("/api/v1/source/import-fixture", json=duplicate)
    assert response.status_code == 422
    assert response.json()["code"] == "ASSET_FILENAME_DUPLICATE"


def test_asset_type_detection_uses_binary_signatures_not_filename_extensions() -> None:
    assert detect_type(b"\x89PNG\r\n\x1a\nbody", "image.png", "image/png") == "image/png"
    assert detect_type(b"GIF89abody", "image.gif", "image/gif") == "image/gif"
    assert detect_type(b"\xff\xd8\xffbody", "image.jpg", "image/jpeg") == "image/jpeg"
    assert detect_type(b"not-an-image", "image.png", None) == "text/plain"
    with pytest.raises(AssetError, match="does not match"):
        detect_type(b"plain text", "image.png", "image/png")


def test_code_storage_and_board_optimistic_versions(client: TestClient) -> None:
    imported = client.post(
        "/api/v1/source/import-fixture", json=fixture_payload("code-task"), headers={"Idempotency-Key": "code-task"}
    ).json()
    revision_id = imported["task"]["revision"]["id"]
    conversation = client.post("/api/v1/conversations", json={"title": "Practice"}).json()
    solve = client.post(
        "/api/v1/solve-sessions", json={"task_revision_id": revision_id, "conversation_id": conversation["id"]}
    ).json()
    solve_id = solve["id"]
    saved_code = client.put(
        f"/api/v1/solve-sessions/{solve_id}/code",
        json={"content": "print(42)", "version": 0},
    )
    assert saved_code.status_code == 200
    stale = client.put(f"/api/v1/solve-sessions/{solve_id}/code", json={"content": "print(43)", "version": 0})
    assert stale.status_code == 409
    assert stale.json()["code"] == "VERSION_CONFLICT"
    board = client.put(f"/api/v1/solve-sessions/{solve_id}/board", json={"snapshot": {"strokes": []}, "version": 0})
    assert board.status_code == 200
    stale_run = client.post(
        f"/api/v1/solve-sessions/{solve_id}/code-runs",
        json={"content": "print(43)", "version": saved_code.json()["version"]},
    )
    assert stale_run.status_code == 409
    assert stale_run.json()["code"] == "VERSION_CONFLICT"
    run = client.post(
        f"/api/v1/solve-sessions/{solve_id}/code-runs",
        json={"content": saved_code.json()["content"], "version": saved_code.json()["version"]},
    )
    assert run.status_code == 503
    assert run.json()["code"] == "RUNNER_UNAVAILABLE"


def test_active_sealed_exam_blocks_context_and_samples(client: TestClient) -> None:
    imported = client.post(
        "/api/v1/source/import-fixture", json=fixture_payload("exam-task"), headers={"Idempotency-Key": "exam-task"}
    ).json()
    revision_id = imported["task"]["revision"]["id"]
    solve = client.post("/api/v1/solve-sessions", json={"task_revision_id": revision_id}).json()
    conversation = client.post("/api/v1/conversations", json={"title": "Exam"}).json()
    sample = client.post("/api/v1/code-samples", json={"title": "Style", "content": "print(1)"}).json()
    cached_preview_payload = {
        "conversation_id": conversation["id"],
        "message": "cached preview before exam",
        "selected_sample_ids": [sample["id"]],
    }
    cached_preview = client.post(
        "/api/v1/assistant/context-preview",
        json=cached_preview_payload,
        headers={"Idempotency-Key": "sealed-cache-preview"},
    )
    assert cached_preview.status_code == 200, cached_preview.text
    blueprint = client.post("/api/v1/exam-blueprints", json={"name": "Mini", "required_lines": {"1": 1}}).json()
    exam = client.post("/api/v1/exams", json={"blueprint_id": blueprint["id"], "seed": 7})
    assert exam.status_code == 201
    solve_view = client.get(f"/api/v1/solve-sessions/{solve['id']}")
    assert solve_view.status_code == 200
    assert solve_view.json()["sealed_exam"] is True
    assert "answer" not in solve_view.json()["task"]["revision"]
    assert "explanation" not in solve_view.json()["task"]["revision"]
    task_view = client.get(f"/api/v1/tasks/{imported['task']['id']}")
    assert task_view.status_code == 200
    assert "answer" not in task_view.json()["revision"]
    assert "explanation" not in task_view.json()["revision"]
    revision_list = client.get(f"/api/v1/tasks/{imported['task']['id']}/revisions")
    assert revision_list.status_code == 200
    assert all("answer" not in item and "explanation" not in item for item in revision_list.json()["items"])
    samples = client.get("/api/v1/code-samples")
    assert samples.status_code == 423
    assert samples.json()["code"] == "EXAM_SEALED"
    context = client.post(
        "/api/v1/assistant/context-preview",
        json={"conversation_id": conversation["id"], "message": "hint", "selected_sample_ids": [sample["id"]]},
    )
    assert context.status_code == 423
    assert context.json()["code"] == "EXAM_SEALED"
    cached_replay = client.post(
        "/api/v1/assistant/context-preview",
        json=cached_preview_payload,
        headers={"Idempotency-Key": "sealed-cache-preview"},
    )
    assert cached_replay.status_code == 423
    assert cached_replay.json()["code"] == "EXAM_SEALED"
    run_replay = client.post(
        "/api/v1/assistant/runs",
        json=cached_preview_payload,
        headers={"Idempotency-Key": "sealed-cache-preview"},
    )
    assert run_replay.status_code == 423
    assert run_replay.json()["code"] == "EXAM_SEALED"
    comparison = client.post(
        "/api/v1/solution-comparisons",
        json={"conversation_id": conversation["id"], "message": "compare"},
    )
    assert comparison.status_code == 423
    assert comparison.json()["code"] == "EXAM_SEALED"
    for response in (
        client.post("/api/v1/code-samples", json={"title": "Blocked", "content": "print(2)"}),
        client.patch(f"/api/v1/code-samples/{sample['id']}", json={"content": "print(2)"}),
        client.post(f"/api/v1/code-samples/{sample['id']}/versions", json={"content": "print(3)"}),
        client.delete(f"/api/v1/code-samples/{sample['id']}"),
    ):
        assert response.status_code == 423
        assert response.json()["code"] == "EXAM_SEALED"
    assert exam.json()["ordered_revision_ids"] == [revision_id]


def test_explicit_loopback_dev_mode_requires_exact_origin() -> None:
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:", owner_dev_mode=True, owner_dev_origin="http://localhost:3000"
    )
    app = create_app(settings)
    with TestClient(app, base_url="http://localhost:3000", client=("127.0.0.1", 8000)) as client:
        allowed = client.get("/api/v1/capabilities", headers={"Origin": "http://localhost:3000"})
        denied = client.get("/api/v1/capabilities", headers={"Origin": "http://evil.example"})
    assert allowed.status_code == 200
    assert denied.status_code == 503


def test_statement_sanitizer_removes_active_markup() -> None:
    text, digest = sanitize_statement("<svg/onload=alert(1)>x</svg><style>bad</style><p>ok</p>", 1000)
    assert text == "x\nok"
    assert len(digest) == 64

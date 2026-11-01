from __future__ import annotations

import base64
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from backend.app.auth import issue_session_token
from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.models import ExamSession
from fastapi.testclient import TestClient
from sqlalchemy import select


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        asset_root=tmp_path / "assets",
        owner_session_secret="test-owner-secret",
        source_mode="fixture",
        source_permission_status="private_authorized",
        testing=True,
    )
    with TestClient(create_app(settings)) as test_client:
        test_client.headers["X-Owner-Session"] = issue_session_token("owner", "test-owner-secret")
        yield test_client


def _task(
    source_id: str,
    *,
    observed_year: int | None,
    target_year: int = 2027,
    with_asset: bool = False,
    statement: str | None = None,
) -> dict[str, object]:
    assets: list[dict[str, object]] = []
    if with_asset:
        assets.append(
            {
                "url": f"https://kompege.ru/files/{source_id}.txt",
                "filename": f"{source_id}.txt",
                "media_type": "text/plain",
                "content_base64": base64.b64encode(b"1\n").decode("ascii"),
            }
        )
    return {
        "source_url": f"https://kompege.ru/task?id={source_id}",
        "source_id": source_id,
        "line_number": 1,
        "exam_year": observed_year,
        "target_exam_year": target_year,
        "statement": statement or f"Task {source_id}",
        "answer": "1",
        "rights_status": "private_authorized",
        "assets": assets,
    }


def _import(client: TestClient, payload: dict[str, object], key: str) -> dict[str, object]:
    response = client.post("/api/v1/source/import-fixture", json=payload, headers={"Idempotency-Key": key})
    assert response.status_code == 201
    return response.json()["task"]


def _blueprint(client: TestClient, *, count: int, constraints: dict[str, object] | None = None) -> str:
    response = client.post(
        "/api/v1/exam-blueprints",
        json={
            "name": "EGE 2027 test",
            "exam_year": 2027,
            "required_lines": {"1": count},
            "constraints": constraints or {},
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_exam_target_year_never_borrows_revisions_from_another_target(client: TestClient) -> None:
    _import(client, _task("target-2026", observed_year=2027, target_year=2026), "target-2026")
    _import(client, _task("target-2027", observed_year=2026, target_year=2027), "target-2027")
    blueprint_id = _blueprint(client, count=2)

    response = client.post("/api/v1/exams", json={"blueprint_id": blueprint_id, "seed": 17})

    assert response.status_code == 422
    assert response.json()["code"] == "EXAM_POOL_TOO_SMALL"


def test_exam_target_year_is_independent_from_observed_source_year(client: TestClient) -> None:
    _import(
        client,
        _task("observed-2026-target-2027", observed_year=2026, statement="Eligible for preparation"),
        "observed-2026-target-2027",
    )
    _import(
        client,
        _task("observed-2027-target-2026", observed_year=2027, target_year=2026, statement="Wrong target"),
        "observed-2027-target-2026",
    )
    blueprint_id = _blueprint(client, count=1)

    response = client.post("/api/v1/exams", json={"blueprint_id": blueprint_id, "seed": 170})

    assert response.status_code == 201, response.text
    assert [item["statement"] for item in response.json()["items"]] == ["Eligible for preparation"]


def test_exam_selection_honours_required_asset_lines(client: TestClient) -> None:
    _import(client, _task("plain-2027", observed_year=None), "plain-2027")
    _import(client, _task("file-2027", observed_year=None, with_asset=True), "file-2027")
    blueprint_id = _blueprint(
        client,
        count=2,
        constraints={"selection": {"require_assets_by_line": [1]}},
    )

    response = client.post("/api/v1/exams", json={"blueprint_id": blueprint_id, "seed": 18})

    assert response.status_code == 422
    assert response.json()["code"] == "EXAM_POOL_TOO_SMALL"


def test_exam_item_exposes_attachment_metadata_and_server_timing_without_solution(client: TestClient) -> None:
    _import(client, _task("timed-file-2027", observed_year=None, with_asset=True), "timed-file-2027")
    blueprint_id = _blueprint(client, count=1, constraints={"exam": {"duration_minutes": 120}})

    started = client.post("/api/v1/exams", json={"blueprint_id": blueprint_id, "seed": 180})

    assert started.status_code == 201, started.text
    payload = started.json()
    item = payload["items"][0]
    assert item["position"] == 0
    assert item["answer"] is None
    assert item["assets"][0]["filename"] == "timed-file-2027.txt"
    assert set(item["assets"][0]) == {
        "id",
        "filename",
        "media_type",
        "detected_type",
        "size_bytes",
        "content_hash",
        "cache_state",
        "rights_status",
        "canonical_url",
    }
    assert "content" not in item["assets"][0]
    assert payload["duration_minutes"] == 120
    started_at = datetime.fromisoformat(payload["started_at"].replace("Z", "+00:00"))
    deadline_at = datetime.fromisoformat(payload["deadline_at"].replace("Z", "+00:00"))
    assert deadline_at - started_at == timedelta(minutes=120)

    active = client.get("/api/v1/exams/active")
    assert active.status_code == 200, active.text
    active_payload = active.json()["active"]
    assert active_payload["started_at"] == payload["started_at"]
    assert active_payload["deadline_at"] == payload["deadline_at"]
    assert active_payload["items"][0]["assets"][0]["filename"] == "timed-file-2027.txt"
    assert active_payload["items"][0]["answer"] is None


def test_exam_deadline_rejects_answer_changes_but_allows_one_idempotent_submit(client: TestClient) -> None:
    _import(client, _task("expired-2027", observed_year=None), "expired-2027")
    blueprint_id = _blueprint(client, count=1, constraints={"exam": {"duration_minutes": 120}})

    started = client.post("/api/v1/exams", json={"blueprint_id": blueprint_id, "seed": 181})
    assert started.status_code == 201, started.text
    exam = started.json()
    item_id = exam["items"][0]["id"]

    saved = client.put(
        f"/api/v1/exams/{exam['id']}/answers/{item_id}",
        json={"content": "1"},
    )
    assert saved.status_code == 200, saved.text

    db = client.app.state.session_factory()
    try:
        with db.begin():
            persisted = db.scalar(select(ExamSession).where(ExamSession.id == exam["id"]))
            assert persisted is not None
            persisted.started_at = datetime(2020, 1, 1)
    finally:
        db.close()

    expired = client.put(
        f"/api/v1/exams/{exam['id']}/answers/{item_id}",
        json={"content": "2"},
    )
    assert expired.status_code == 423
    assert expired.json()["code"] == "EXAM_TIME_EXPIRED"

    active = client.get("/api/v1/exams/active")
    assert active.status_code == 200, active.text
    assert active.json()["active"]["answers"][item_id]["content"] == "1"

    submitted = client.post(f"/api/v1/exams/{exam['id']}/submit")
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["report"]["correct_count"] == 1

    repeated = client.post(f"/api/v1/exams/{exam['id']}/submit")
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["id"] == submitted.json()["id"]
    assert repeated.json()["report_hash"] == submitted.json()["report_hash"]


def test_exam_pool_uses_only_current_task_revision(client: TestClient) -> None:
    _import(client, _task("revised-2027", observed_year=None, statement="First"), "revision-first")
    _import(client, _task("revised-2027", observed_year=None, statement="Second"), "revision-second")
    blueprint_id = _blueprint(client, count=2)

    response = client.post("/api/v1/exams", json={"blueprint_id": blueprint_id, "seed": 19})

    assert response.status_code == 422
    assert response.json()["code"] == "EXAM_POOL_TOO_SMALL"


def test_official_2027_template_is_explicitly_provisional_and_idempotent(client: TestClient) -> None:
    template_response = client.get("/api/v1/exam-blueprint-templates/2027")
    assert template_response.status_code == 200
    template = template_response.json()
    assert template["exam_year"] == 2027
    assert template["required_lines"] == {str(line): 1 for line in range(1, 28)}
    assert template["constraints"]["reference"]["status"] == "provisional"
    assert template["constraints"]["exam"]["duration_minutes"] == 235

    first = client.post("/api/v1/exam-blueprint-templates/2027/instantiate")
    second = client.post("/api/v1/exam-blueprint-templates/2027/instantiate")
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["blueprint"]["id"] == second.json()["blueprint"]["id"]
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True

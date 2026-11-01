from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from backend.app.auth import issue_session_token
from backend.app.config import Settings
from backend.app.db import CURRENT_SCHEMA_VERSION, init_schema, make_engine
from backend.app.main import create_app
from backend.app.models import Asset, TaskAsset, TaskRevision
from fastapi.testclient import TestClient
from sqlalchemy import select, text

OWNER_SECRET = "test-owner-secret"


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        asset_root=tmp_path / "assets",
        owner_session_secret=OWNER_SECRET,
        source_mode="fixture",
        source_permission_status="private_authorized",
        testing=True,
    )
    with TestClient(create_app(settings)) as test_client:
        _auth(test_client, "owner-a")
        yield test_client


def _auth(client: TestClient, owner_id: str) -> None:
    client.headers["X-Owner-Session"] = issue_session_token(owner_id, OWNER_SECRET)


def _task(
    source_id: str,
    *,
    line: int = 1,
    answer: str = "42",
    with_asset: bool = False,
    metadata_only: bool = False,
) -> dict[str, object]:
    assets: list[dict[str, object]] = []
    if with_asset:
        asset: dict[str, object] = {
            "url": f"https://kompege.ru/files/{source_id}.txt",
            "filename": f"{source_id}.txt",
            "media_type": "text/plain",
        }
        if metadata_only:
            asset["content_hash"] = "a" * 64
        else:
            asset["content_base64"] = base64.b64encode(f"content-{source_id}\n".encode()).decode("ascii")
        assets.append(asset)
    return {
        "source_url": f"https://kompege.ru/task?id={source_id}",
        "source_id": source_id,
        "line_number": line,
        "exam_year": None,
        "target_exam_year": 2027,
        "statement": f"TEST_ONLY task {source_id}",
        "answer": answer,
        "rights_status": "private_authorized",
        "assets": assets,
    }


def _import(client: TestClient, source_id: str, **kwargs: object) -> dict[str, Any]:
    response = client.post(
        "/api/v1/source/import-fixture",
        json=_task(source_id, **kwargs),
        headers={"Idempotency-Key": f"import-{source_id}"},
    )
    assert response.status_code == 201, response.text
    return response.json()["task"]


def _prepare(
    client: TestClient,
    *,
    key: str,
    kind: str = "thematic",
    mode: str = "strict",
    required_lines: dict[str, int] | None = None,
    **kwargs: object,
):
    body = {
        "kind": kind,
        "mode": mode,
        "required_lines": required_lines or {"1": 1},
        "difficulty": "any",
        "duration_minutes": 45,
        "exclude_solved": False,
        "exclude_recent": False,
        "source_year": None,
        "include_corrected_errors": False,
        **kwargs,
    }
    return client.post("/api/v1/exams/prepare", json=body, headers={"Idempotency-Key": key})


def test_ready_preview_has_no_timer_and_start_is_idempotent(client: TestClient) -> None:
    _import(client, "ready-1")
    first = _prepare(client, key="prepare-ready")
    assert first.status_code == 201, first.text
    preview = first.json()
    assert preview["lifecycle"] == "ready"
    assert preview["started_at"] is None
    assert preview["deadline_at"] is None
    assert preview["remaining_seconds"] is None
    assert preview["answers"] == {}
    assert preview["critical_files_ready"] is True
    assert client.get("/api/v1/exams/active").json()["active"] is None

    replay = _prepare(client, key="prepare-ready")
    assert replay.status_code == 201
    replay_payload = replay.json()
    assert replay_payload["id"] == preview["id"]
    assert replay_payload["lifecycle"] == "ready"
    assert replay_payload["items"] == preview["items"]
    assert len(client.get("/api/v1/exams").json()["items"]) == 1

    started = client.post(
        f"/api/v1/exams/{preview['id']}/start",
        headers={"Idempotency-Key": "start-ready"},
    )
    assert started.status_code == 200, started.text
    active = started.json()
    assert active["lifecycle"] == "active"
    assert active["started_at"] is not None
    assert active["deadline_at"] is not None
    assert active["remaining_seconds"] == 45 * 60

    start_replay = client.post(
        f"/api/v1/exams/{preview['id']}/start",
        headers={"Idempotency-Key": "start-ready"},
    )
    assert start_replay.status_code == 200
    start_replay_payload = start_replay.json()
    assert start_replay_payload["id"] == active["id"]
    assert start_replay_payload["lifecycle"] == "active"
    assert start_replay_payload["items"] == active["items"]


def test_prepare_is_owner_scoped_and_strict_start_respects_sealed_gate(client: TestClient) -> None:
    _import(client, "owner-a-task")
    prepared = _prepare(client, key="owner-a-prepare")
    assert prepared.status_code == 201
    exam_id = prepared.json()["id"]

    _auth(client, "owner-b")
    hidden = client.get(f"/api/v1/exams/{exam_id}")
    assert hidden.status_code == 404
    foreign_start = client.post(f"/api/v1/exams/{exam_id}/start", headers={"Idempotency-Key": "foreign-start"})
    assert foreign_start.status_code == 404

    _auth(client, "owner-a")
    active_blueprint = client.post(
        "/api/v1/exam-blueprints",
        json={"name": "active", "required_lines": {"1": 1}},
    ).json()
    active = client.post(
        "/api/v1/exams",
        json={"blueprint_id": active_blueprint["id"], "seed": 11},
    )
    assert active.status_code == 201
    blocked = client.post(f"/api/v1/exams/{exam_id}/start", headers={"Idempotency-Key": "sealed-start"})
    assert blocked.status_code == 423
    assert blocked.json()["code"] == "EXAM_SEALED"


def test_critical_asset_is_rechecked_before_start(client: TestClient) -> None:
    imported = _import(client, "metadata-file", with_asset=True, metadata_only=True)
    prepared = _prepare(client, key="prepare-metadata")
    assert prepared.status_code == 201, prepared.text
    preview = prepared.json()
    assert preview["critical_files_ready"] is False
    assert preview["missing_critical_files"][0]["reason"] == "not_ready"

    blocked = client.post(
        f"/api/v1/exams/{preview['id']}/start",
        headers={"Idempotency-Key": "start-metadata"},
    )
    assert blocked.status_code == 422
    assert blocked.json()["code"] == "EXAM_CRITICAL_ASSET_UNREADY"

    session = client.app.state.session_factory()
    try:
        with session.begin():
            revision = session.get(TaskRevision, imported["revision"]["id"])
            assert revision is not None
            asset = session.scalar(
                select(Asset)
                .join_from(Asset, __import__("backend.app.models", fromlist=["TaskAsset"]).TaskAsset)
                .where(Asset.content_hash == "a" * 64)
            )
            assert asset is not None
            asset.cache_state = "ready"
            asset.hash_verified = True
    finally:
        session.close()

    ready_again = client.get(f"/api/v1/exams/{preview['id']}")
    assert ready_again.status_code == 200
    assert ready_again.json()["critical_files_ready"] is True
    started = client.post(
        f"/api/v1/exams/{preview['id']}/start",
        headers={"Idempotency-Key": "start-metadata"},
    )
    assert started.status_code == 200, started.text


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("content_hash", "b" * 64, "hash_changed"),
        ("expected_content_hash", "c" * 64, "source_hash_changed"),
        ("rights_status", "review_required", "rights_not_authorized"),
    ],
)
def test_prepare_start_rechecks_each_critical_asset_integrity_branch(
    client: TestClient, field: str, value: str, reason: str
) -> None:
    source_id = f"critical-{reason}"
    imported = _import(client, source_id, with_asset=True)
    prepared = _prepare(client, key=f"prepare-{reason}")
    assert prepared.status_code == 201, prepared.text
    preview = prepared.json()
    session = client.app.state.session_factory()
    try:
        with session.begin():
            asset = session.scalar(
                select(Asset)
                .join_from(Asset, TaskAsset)
                .where(TaskAsset.revision_id == imported["revision"]["id"])
            )
            assert asset is not None
            setattr(asset, field, value)
    finally:
        session.close()

    blocked = client.post(
        f"/api/v1/exams/{preview['id']}/start",
        headers={"Idempotency-Key": f"start-{reason}"},
    )
    assert blocked.status_code == 422, blocked.text
    assert blocked.json()["code"] == "EXAM_CRITICAL_ASSET_UNREADY"
    refreshed = client.get(f"/api/v1/exams/{preview['id']}")
    assert refreshed.status_code == 200
    assert refreshed.json()["missing_critical_files"][0]["reason"] == reason


def test_server_filters_solved_revisions_and_rejects_shortage_without_fallback(client: TestClient) -> None:
    first = _import(client, "solved-task")
    _import(client, "available-task")
    solve = client.post("/api/v1/solve-sessions", json={"task_revision_id": first["revision"]["id"]})
    assert solve.status_code == 201
    attempt = client.post(
        f"/api/v1/solve-sessions/{solve.json()['id']}/check-answer",
        json={"answer": "42"},
        headers={"Idempotency-Key": "solved-attempt"},
    )
    assert attempt.status_code == 200, attempt.text
    assert attempt.json()["status"] == "correct"

    prepared = _prepare(client, key="exclude-solved", exclude_solved=True)
    assert prepared.status_code == 201, prepared.text
    assert prepared.json()["items"][0]["source_id"] == "available-task"

    _auth(client, "owner-b")
    only = _import(client, "only-solved")
    only_solve = client.post("/api/v1/solve-sessions", json={"task_revision_id": only["revision"]["id"]})
    assert only_solve.status_code == 201
    only_attempt = client.post(
        f"/api/v1/solve-sessions/{only_solve.json()['id']}/attempts",
        json={
            "status": "solved",
            "user_answer": "42",
            "verified_correct": True,
            "independent": True,
            "hint_count": 0,
        },
        headers={"Idempotency-Key": "only-solved-attempt"},
    )
    assert only_attempt.status_code == 201
    only_solved = _prepare(
        client,
        key="only-solved-excluded",
        required_lines={"2": 1},
        exclude_solved=True,
    )
    assert only_solved.status_code == 422
    assert only_solved.json()["code"] == "EXAM_POOL_TOO_SMALL"


def test_errors_mode_uses_confirmed_owner_errors_and_corrected_toggle(client: TestClient) -> None:
    imported = _import(client, "error-task")
    solve = client.post("/api/v1/solve-sessions", json={"task_revision_id": imported["revision"]["id"]}).json()
    wrong = client.post(
        f"/api/v1/solve-sessions/{solve['id']}/check-answer",
        json={"answer": "wrong"},
        headers={"Idempotency-Key": "wrong-answer"},
    )
    assert wrong.status_code == 200, wrong.text
    error = client.post(
        "/api/v1/errors",
        json={
            "attempt_id": wrong.json()["attempt_id"],
            "error_type": "answer-mismatch",
            "status": "confirmed",
            "description": "TEST_ONLY confirmed wrong answer",
        },
        headers={"Idempotency-Key": "confirm-error"},
    )
    assert error.status_code == 201, error.text

    selected = _prepare(client, key="errors-open", kind="errors")
    assert selected.status_code == 201, selected.text
    assert selected.json()["items"][0]["source_id"] == "error-task"

    correct = client.post(
        f"/api/v1/solve-sessions/{solve['id']}/check-answer",
        json={"answer": "42"},
        headers={"Idempotency-Key": "correct-answer"},
    )
    assert correct.status_code == 200, correct.text
    excluded = _prepare(client, key="errors-exclude-corrected", kind="errors")
    assert excluded.status_code == 422
    assert excluded.json()["code"] == "EXAM_ERROR_POOL_EMPTY"

    included = _prepare(
        client,
        key="errors-include-corrected",
        kind="errors",
        include_corrected_errors=True,
    )
    assert included.status_code == 201, included.text


def test_schema_v8_to_v9_adds_prepare_fields_without_losing_rows(tmp_path: Path) -> None:
    database = tmp_path / "exam-v8.sqlite"
    engine = make_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    try:
        init_schema(engine)
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM schema_migrations WHERE version > 8"))
            for column in ("prepare_kind", "prepare_options", "prepared_at"):
                connection.exec_driver_sql(f"ALTER TABLE exam_sessions DROP COLUMN {column}")
        init_schema(engine)
        init_schema(engine)
        with engine.connect() as connection:
            version = connection.execute(text("SELECT MAX(version) FROM schema_migrations")).scalar_one()
            columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info('exam_sessions')")}
        assert version == CURRENT_SCHEMA_VERSION == 13
        assert {"prepare_kind", "prepare_options", "prepared_at"} <= columns
    finally:
        engine.dispose()

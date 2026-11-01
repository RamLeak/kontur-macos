from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from backend.app.auth import issue_session_token
from backend.app.config import Settings
from backend.app.db import CURRENT_SCHEMA_VERSION, init_schema, make_engine, make_session_factory
from backend.app.main import create_app
from backend.app.models import ExamAnswer, ExamBlueprint, ExamItem, ExamSession, ExamWorkspace, Task, TaskRevision, User
from fastapi.testclient import TestClient
from sqlalchemy import text


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
        test_client.headers["X-Owner-Session"] = issue_session_token("owner", settings.owner_session_secret)
        yield test_client


def _auth(client: TestClient, owner_id: str) -> None:
    settings = client.app.state.settings
    client.headers["X-Owner-Session"] = issue_session_token(owner_id, settings.owner_session_secret)


def _import_task(client: TestClient, source_id: str = "exam-workspace-task") -> dict[str, Any]:
    response = client.post(
        "/api/v1/source/import-fixture",
        json={
            "source_url": f"https://kompege.ru/task?id={source_id}",
            "source_id": source_id,
            "line_number": 1,
            "statement": "Synthetic exam workspace task.",
            "answer": "42",
            "rights_status": "private_authorized",
        },
        headers={"Idempotency-Key": f"fixture-{source_id}"},
    )
    assert response.status_code == 201, response.text
    return response.json()["task"]


def _blueprint(client: TestClient, *, duration_minutes: int = 60) -> str:
    response = client.post(
        "/api/v1/exam-blueprints",
        json={
            "name": "Synthetic workspace exam",
            "required_lines": {"1": 1},
            "constraints": {"exam": {"duration_minutes": duration_minutes}},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _start(client: TestClient, blueprint_id: str, *, mode: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"blueprint_id": blueprint_id, "seed": 11}
    if mode is not None:
        body["mode"] = mode
    response = client.post("/api/v1/exams", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _error(response: Any, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["code"] == code


def test_explicit_modes_and_legacy_pause_compatibility(client: TestClient) -> None:
    _import_task(client)
    blueprint_id = _blueprint(client)

    strict = _start(client, blueprint_id, mode="strict")
    assert strict["mode"] == "strict"
    assert strict["sealed"] is True
    _error(client.post(f"/api/v1/exams/{strict['id']}/pause"), 423, "EXAM_PAUSE_FORBIDDEN")
    assert client.post(f"/api/v1/exams/{strict['id']}/submit").status_code == 200

    study = _start(client, blueprint_id, mode="study")
    assert study["mode"] == "study"
    assert study["sealed"] is False
    assert client.get("/api/v1/exams/active").json()["active"]["id"] == study["id"]
    assert client.post(f"/api/v1/exams/{study['id']}/pause").status_code == 200
    assert client.post(f"/api/v1/exams/{study['id']}/resume").status_code == 200
    assert client.post(f"/api/v1/exams/{study['id']}/submit").status_code == 200

    legacy = _start(client, blueprint_id)
    assert legacy["mode"] == "legacy"
    assert legacy["sealed"] is True
    assert client.post(f"/api/v1/exams/{legacy['id']}/pause").status_code == 200


def test_exam_workspace_is_owner_scoped_and_has_independent_versions(client: TestClient) -> None:
    _import_task(client, "owner-workspace-task")
    exam = _start(client, _blueprint(client), mode="strict")
    item_id = exam["items"][0]["id"]
    endpoint = f"/api/v1/exams/{exam['id']}/items/{item_id}/workspace"

    initial = client.get(endpoint)
    assert initial.status_code == 200, initial.text
    assert initial.json()["read_only"] is False
    assert initial.json()["code"]["version"] == 0
    assert initial.json()["board"]["version"] == 0

    saved_code = client.put(f"{endpoint}/code", json={"content": "print(42)", "version": 0})
    assert saved_code.status_code == 200, saved_code.text
    assert saved_code.json()["code"]["content"] == "print(42)"
    assert saved_code.json()["code"]["version"] == 1
    saved_board = client.put(
        f"{endpoint}/board",
        json={"snapshot": {"strokes": [{"points": [{"x": 1, "y": 2}]}]}, "version": 0},
    )
    assert saved_board.status_code == 200, saved_board.text
    assert saved_board.json()["board"]["version"] == 1
    stale = client.put(f"{endpoint}/code", json={"content": "print(43)", "version": 0})
    _error(stale, 409, "VERSION_CONFLICT")

    _auth(client, "foreign-owner")
    _error(client.get(endpoint), 404, "NOT_FOUND")
    _error(client.put(f"{endpoint}/board", json={"snapshot": {}, "version": 0}), 404, "NOT_FOUND")


def test_exam_workspace_becomes_read_only_at_deadline_and_after_pause(client: TestClient) -> None:
    _import_task(client, "deadline-workspace-task")
    exam = _start(client, _blueprint(client, duration_minutes=1), mode="study")
    item_id = exam["items"][0]["id"]
    endpoint = f"/api/v1/exams/{exam['id']}/items/{item_id}/workspace"
    assert client.post(f"/api/v1/exams/{exam['id']}/pause").status_code == 200
    paused = client.get(endpoint)
    assert paused.status_code == 200
    assert paused.json()["read_only"] is True
    _error(client.put(f"{endpoint}/code", json={"content": "x=1", "version": 0}), 423, "EXAM_NOT_ACTIVE")
    assert client.post(f"/api/v1/exams/{exam['id']}/resume").status_code == 200

    session = client.app.state.session_factory()
    try:
        with session.begin():
            persisted = session.get(ExamSession, exam["id"])
            assert persisted is not None
            persisted.started_at = datetime(2020, 1, 1)
    finally:
        session.close()
    expired = client.get(endpoint)
    assert expired.status_code == 200
    assert expired.json()["read_only"] is True
    _error(client.put(f"{endpoint}/board", json={"snapshot": {}, "version": 0}), 423, "EXAM_TIME_EXPIRED")


def test_report_contains_immutable_exam_workspace_snapshot(client: TestClient) -> None:
    _import_task(client, "history-workspace-task")
    exam = _start(client, _blueprint(client), mode="study")
    item_id = exam["items"][0]["id"]
    endpoint = f"/api/v1/exams/{exam['id']}/items/{item_id}/workspace"
    code = client.put(f"{endpoint}/code", json={"content": "answer = 42", "version": 0})
    assert code.status_code == 200, code.text
    board = client.put(
        f"{endpoint}/board",
        json={
            "snapshot": {
                "texts": [{"x": 2, "y": 3, "text": "proof"}],
                "images": [
                    {
                        "id": "diagram-1",
                        "asset_id": "asset-1",
                        "revision_hash": "rev-hash",
                        "x": 4,
                        "y": 5,
                        "width": 120,
                        "height": 80,
                    }
                ],
            },
            "version": 0,
        },
    )
    assert board.status_code == 200, board.text
    answer = client.put(f"/api/v1/exams/{exam['id']}/answers/{item_id}", json={"content": "42"})
    assert answer.status_code == 200, answer.text

    submitted = client.post(f"/api/v1/exams/{exam['id']}/submit")
    assert submitted.status_code == 200, submitted.text
    report = submitted.json()
    assert report["report"]["mode"] == "study"
    snapshot = report["report"]["workspaces"][item_id]
    assert snapshot["code"] == "answer = 42"
    assert snapshot["code_version"] == 1
    assert snapshot["board"]["texts"][0]["text"] == "proof"
    assert snapshot["board"]["images"][0]["asset_id"] == "asset-1"
    readonly = client.get(endpoint)
    assert readonly.status_code == 200
    assert readonly.json()["read_only"] is True
    _error(client.put(f"{endpoint}/code", json={"content": "mutated", "version": 1}), 423, "EXAM_NOT_ACTIVE")
    assert client.get(f"/api/v1/exams/{exam['id']}/report").json() == report


def test_schema_v7_to_v8_preserves_legacy_exam_and_adds_workspace_table(tmp_path: Path) -> None:
    database = tmp_path / "exam-v7.sqlite"
    engine = make_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    try:
        init_schema(engine)
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM schema_migrations WHERE version > 7"))
            connection.exec_driver_sql("ALTER TABLE exam_sessions DROP COLUMN mode")
            connection.exec_driver_sql("DROP TABLE exam_workspaces")
            connection.execute(
                text("INSERT INTO users(id, role, created_at) VALUES ('legacy-v7-owner', 'owner', :created_at)"),
                {"created_at": "2026-09-06 00:00:00"},
            )
            connection.execute(
                text(
                    "INSERT INTO exam_blueprints"
                    "(id, owner_id, name, exam_year, required_lines, constraints, version, created_at)"
                    " VALUES ('legacy-v7-blueprint', 'legacy-v7-owner', 'Legacy v7', 2027, :required, :constraints, 1, "
                    ":created_at)"
                ),
                {
                    "required": json.dumps({"1": 1}),
                    "constraints": json.dumps({}),
                    "created_at": "2026-09-06 00:00:00",
                },
            )
            connection.execute(
                text(
                    "INSERT INTO exam_sessions"
                    "(id, owner_id, blueprint_id, seed, rng_algorithm, rng_version, scoring_version,"
                    " pool_manifest_hash, ordered_revision_ids, lifecycle, sealed, started_at, completed_at)"
                    " VALUES ('legacy-v7-exam', 'legacy-v7-owner', 'legacy-v7-blueprint', 7, 'sha256-sort', 'v1', "
                    "'score-v1',"
                    " 'legacy-pool', :ordered, 'active', 1, :started, NULL)"
                ),
                {"ordered": json.dumps([]), "started": "2026-09-06 00:00:00"},
            )
        init_schema(engine)
        with engine.connect() as connection:
            version = connection.execute(text("SELECT MAX(version) FROM schema_migrations")).scalar_one()
            columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info('exam_sessions')")}
            workspace_table = connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name='exam_workspaces'")
            ).scalar_one()
        assert version == CURRENT_SCHEMA_VERSION == 13
        assert "mode" in columns
        assert workspace_table == "exam_workspaces"
        with make_session_factory(engine)() as session:
            preserved = session.get(ExamSession, "legacy-v7-exam")
            assert preserved is not None
            assert preserved.mode == "legacy"
    finally:
        engine.dispose()


def test_schema_v8_to_v10_preserves_populated_exam_rows_across_restart(tmp_path: Path) -> None:
    database = tmp_path / "exam-v8-populated.sqlite"
    database_url = f"sqlite+pysqlite:///{database.as_posix()}"
    engine = make_engine(database_url)
    created_at = "2026-09-06 00:00:00"
    try:
        init_schema(engine)
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM schema_migrations WHERE version > 8"))
            connection.exec_driver_sql("DROP TABLE exam_code_runs")
            connection.exec_driver_sql("DROP TABLE exam_study_ai_runs")
            connection.exec_driver_sql("DROP TABLE exam_study_ai_context_snapshots")
            for column in ("prepare_kind", "prepare_options", "prepared_at"):
                connection.exec_driver_sql(f"ALTER TABLE exam_sessions DROP COLUMN {column}")

            connection.execute(
                text("INSERT INTO users(id, role, created_at) VALUES ('v8-owner', 'owner', :created_at)"),
                {"created_at": created_at},
            )
            connection.execute(
                text(
                    "INSERT INTO exam_blueprints"
                    "(id, owner_id, name, exam_year, required_lines, constraints, version, created_at)"
                    " VALUES ('v8-blueprint', 'v8-owner', 'Populated v8', 2027, :required, :constraints, "
                    "4, :created_at)"
                ),
                {
                    "required": json.dumps({"1": 1}),
                    "constraints": json.dumps({"duration_minutes": 60}),
                    "created_at": created_at,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO tasks"
                    "(id, source_name, external_id, line_number, current_revision_id, created_at, updated_at)"
                    " VALUES ('v8-task', 'fixture', 'v8-external', 1, NULL, :created_at, :created_at)"
                ),
                {"created_at": created_at},
            )
            connection.execute(
                text(
                    "INSERT INTO task_revisions"
                    "(id, task_id, revision_number, source_url, source_id, line_number, exam_year, target_exam_year,"
                    " source_year, source_year_basis, source_created_at, source_difficulty, difficulty, source_label,"
                    " source_metadata, statement, statement_latex, answer, explanation, content_hash, retrieved_at,"
                    " parser_version, rights_status, asset_manifest, subtask_manifest, table_manifest, created_at)"
                    " VALUES ('v8-revision', 'v8-task', 1, 'fixture://v8', 'v8-source', 1, 2027, 2027,"
                    " 2026, 'fixture', :created_at, 2, 'medium', 'v8 source', :metadata, 'Preserved task', NULL,"
                    " '42', 'fixture explanation', :content_hash, :created_at, 'fixture-v1', 'private_authorized',"
                    " :assets, :subtasks, :table_manifest, :created_at)"
                ),
                {
                    "created_at": created_at,
                    "metadata": json.dumps({"legacy": True}),
                    "content_hash": "v" * 64,
                    "assets": json.dumps([]),
                    "subtasks": json.dumps([]),
                    "table_manifest": json.dumps({}),
                },
            )
            connection.execute(
                text("UPDATE tasks SET current_revision_id = 'v8-revision' WHERE id = 'v8-task'")
            )
            connection.execute(
                text(
                    "INSERT INTO exam_sessions"
                    "(id, owner_id, blueprint_id, seed, rng_algorithm, rng_version, scoring_version, "
                    "pool_manifest_hash, ordered_revision_ids, lifecycle, sealed, mode, started_at, paused_at, "
                    "paused_seconds, completed_at)"
                    " VALUES ('v8-exam', 'v8-owner', 'v8-blueprint', 17, 'sha256-sort', 'v1', 'score-v1', 'v8-pool',"
                    " :ordered, 'paused', 1, 'legacy', :created_at, :paused_at, 17.5, NULL)"
                ),
                {"ordered": json.dumps(["v8-revision"]), "created_at": created_at, "paused_at": created_at},
            )
            connection.execute(
                text(
                    "INSERT INTO exam_items(id, exam_session_id, position, task_revision_id)"
                    " VALUES ('v8-item', 'v8-exam', 0, 'v8-revision')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO exam_answers(id, exam_item_id, content, saved_at)"
                    " VALUES ('v8-answer', 'v8-item', 'legacy answer', :created_at)"
                ),
                {"created_at": created_at},
            )
            connection.execute(
                text(
                    "INSERT INTO exam_workspaces"
                    "(id, owner_id, exam_session_id, exam_item_id, code_content, code_version, board_snapshot,"
                    " board_version, updated_at)"
                    " VALUES ('v8-workspace', 'v8-owner', 'v8-exam', 'v8-item', 'print(42)', 3, :board, 2, :created_at)"
                ),
                {
                    "board": json.dumps({"id": "v8-board", "strokes": [{"id": "stroke-1"}], "texts": [], "images": []}),
                    "created_at": created_at,
                },
            )

        init_schema(engine)
        engine.dispose()
        engine = make_engine(database_url)
        init_schema(engine)
        with engine.connect() as connection:
            version = connection.execute(text("SELECT MAX(version) FROM schema_migrations")).scalar_one()
            assert version == CURRENT_SCHEMA_VERSION == 13
            assert connection.execute(text("SELECT COUNT(*) FROM exam_code_runs")).scalar_one() == 0
            assert connection.execute(text("SELECT COUNT(*) FROM exam_study_ai_runs")).scalar_one() == 0
            assert connection.execute(text("SELECT COUNT(*) FROM exam_study_ai_context_snapshots")).scalar_one() == 0
            columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info('exam_sessions')")}
            assert {"prepare_kind", "prepare_options", "prepared_at"} <= columns

        with make_session_factory(engine)() as session:
            owner = session.get(User, "v8-owner")
            blueprint = session.get(ExamBlueprint, "v8-blueprint")
            task = session.get(Task, "v8-task")
            revision = session.get(TaskRevision, "v8-revision")
            exam = session.get(ExamSession, "v8-exam")
            item = session.get(ExamItem, "v8-item")
            answer = session.get(ExamAnswer, "v8-answer")
            workspace = session.get(ExamWorkspace, "v8-workspace")
            assert owner is not None and blueprint is not None and task is not None and revision is not None
            assert item is not None and answer is not None and workspace is not None and exam is not None
            assert blueprint.name == "Populated v8" and blueprint.version == 4
            assert task.current_revision_id == revision.id and revision.answer == "42"
            assert exam.lifecycle == "paused" and exam.sealed is True and exam.mode == "legacy"
            assert exam.paused_seconds == 17.5 and exam.paused_at is not None
            assert answer.content == "legacy answer" and item.task_revision_id == revision.id
            assert workspace.code_content == "print(42)" and workspace.code_version == 3
            assert workspace.board_version == 2 and workspace.board_snapshot["strokes"][0]["id"] == "stroke-1"
            assert exam.prepare_kind is None and exam.prepare_options is None and exam.prepared_at is None
    finally:
        engine.dispose()

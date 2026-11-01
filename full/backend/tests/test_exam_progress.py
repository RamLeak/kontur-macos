from pathlib import Path
from typing import Any

import pytest
from backend.app.auth import issue_session_token
from backend.app.config import Settings
from backend.app.db import CURRENT_SCHEMA_VERSION, init_schema, make_engine
from backend.app.exam_scoring import PRACTICE_SCORING_VERSION
from backend.app.main import create_app
from backend.app.models import ExamSession
from fastapi.testclient import TestClient
from sqlalchemy import text


@pytest.fixture()
def owner_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        asset_root=tmp_path / "assets",
        owner_session_secret="test-owner-secret",
        source_mode="fixture",
        source_permission_status="private_authorized",
        testing=True,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        client.headers["X-Owner-Session"] = issue_session_token("owner", settings.owner_session_secret)
        yield client


def _import_task(
    client: TestClient,
    source_id: str,
    *,
    statement: str = "Synthetic exam progress task.",
    line_number: int = 1,
    answer: str = "42",
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/source/import-fixture",
        json={
            "source_url": f"https://kompege.ru/task?id={source_id}",
            "source_id": source_id,
            "line_number": line_number,
            "statement": statement,
            "answer": answer,
            "rights_status": "private_authorized",
        },
        headers={"Idempotency-Key": f"exam-progress-{source_id}-{statement}"},
    )
    assert response.status_code == 201, response.text
    return response.json()["task"]


def _exact_exam(
    client: TestClient,
    revision_id: str,
    *,
    seed: int,
    mode: str | None = None,
    line_number: int = 1,
) -> dict[str, Any]:
    blueprint = client.post(
        "/api/v1/exam-blueprints",
        json={
            "name": "Exact exam progress",
            "required_lines": {str(line_number): 1},
            "constraints": {"selection": {"include_revision_ids": [revision_id]}},
        },
    )
    assert blueprint.status_code == 201, blueprint.text
    body: dict[str, Any] = {"blueprint_id": blueprint.json()["id"], "seed": seed}
    if mode is not None:
        body["mode"] = mode
    exam = client.post("/api/v1/exams", json=body)
    assert exam.status_code == 201, exam.text
    assert exam.json()["items"][0]["task_revision_id"] == revision_id
    return exam.json()


def _progress(client: TestClient, *revision_ids: str) -> dict[str, dict[str, Any]]:
    response = client.get(
        "/api/v1/task-progress",
        params=[("revision_ids", revision_id) for revision_id in revision_ids],
    )
    assert response.status_code == 200, response.text
    return {item["task_revision_id"]: item for item in response.json()}


def test_exam_progress_uses_verified_reports_and_exam_workspaces(owner_client: TestClient) -> None:
    correct_task = _import_task(owner_client, "exam-progress-correct")
    correct_exam = _exact_exam(owner_client, correct_task["revision"]["id"], seed=101)
    correct_item = correct_exam["items"][0]["id"]
    assert owner_client.put(
        f"/api/v1/exams/{correct_exam['id']}/answers/{correct_item}",
        json={"content": "42"},
    ).status_code == 200
    correct_report = owner_client.post(f"/api/v1/exams/{correct_exam['id']}/submit")
    assert correct_report.status_code == 200, correct_report.text

    incorrect_task = _import_task(owner_client, "exam-progress-incorrect")
    incorrect_exam = _exact_exam(owner_client, incorrect_task["revision"]["id"], seed=102)
    incorrect_item = incorrect_exam["items"][0]["id"]
    assert owner_client.put(
        f"/api/v1/exams/{incorrect_exam['id']}/answers/{incorrect_item}",
        json={"content": "41"},
    ).status_code == 200
    incorrect_report = owner_client.post(f"/api/v1/exams/{incorrect_exam['id']}/submit")
    assert incorrect_report.status_code == 200, incorrect_report.text

    unanswered_task = _import_task(owner_client, "exam-progress-unanswered")
    unanswered_exam = _exact_exam(owner_client, unanswered_task["revision"]["id"], seed=103)
    unanswered_report = owner_client.post(f"/api/v1/exams/{unanswered_exam['id']}/submit")
    assert unanswered_report.status_code == 200, unanswered_report.text

    versioned_first = _import_task(owner_client, "exam-progress-versioned", statement="old source revision")
    versioned_latest = _import_task(owner_client, "exam-progress-versioned", statement="new source revision")
    old_exam = _exact_exam(owner_client, versioned_first["revision"]["id"], seed=104)
    old_item = old_exam["items"][0]["id"]
    assert owner_client.put(
        f"/api/v1/exams/{old_exam['id']}/answers/{old_item}",
        json={"content": "42"},
    ).status_code == 200
    old_report = owner_client.post(f"/api/v1/exams/{old_exam['id']}/submit")
    assert old_report.status_code == 200, old_report.text

    second_correct_task = _import_task(owner_client, "exam-progress-correct-two")
    second_correct_exam = _exact_exam(owner_client, second_correct_task["revision"]["id"], seed=108)
    second_correct_item = second_correct_exam["items"][0]["id"]
    assert owner_client.put(
        f"/api/v1/exams/{second_correct_exam['id']}/answers/{second_correct_item}",
        json={"content": "42"},
    ).status_code == 200
    second_correct_report = owner_client.post(f"/api/v1/exams/{second_correct_exam['id']}/submit")
    assert second_correct_report.status_code == 200, second_correct_report.text

    started_task = _import_task(owner_client, "exam-progress-started")
    started_exam = _exact_exam(owner_client, started_task["revision"]["id"], seed=105, mode="study")
    started_item = started_exam["items"][0]["id"]
    workspace = f"/api/v1/exams/{started_exam['id']}/items/{started_item}/workspace"
    saved_code = owner_client.put(f"{workspace}/code", json={"content": "print(42)", "version": 0})
    assert saved_code.status_code == 200, saved_code.text
    saved_board = owner_client.put(
        f"{workspace}/board",
        json={"snapshot": {"strokes": [{"points": [{"x": 1, "y": 2}]}]}, "version": 0},
    )
    assert saved_board.status_code == 200, saved_board.text
    unavailable_run = owner_client.post(
        f"/api/v1/exams/{started_exam['id']}/items/{started_item}/code-runs",
        json={"version": saved_code.json()["code"]["version"]},
        headers={"Idempotency-Key": "exam-progress-unavailable-run"},
    )
    assert unavailable_run.status_code == 503, unavailable_run.text

    untouched_task = _import_task(owner_client, "exam-progress-untouched")
    _exact_exam(owner_client, untouched_task["revision"]["id"], seed=106, mode="study")
    forged_task = _import_task(owner_client, "exam-progress-forged")
    solve = owner_client.post(
        "/api/v1/solve-sessions", json={"task_revision_id": forged_task["revision"]["id"]}
    )
    assert solve.status_code == 201, solve.text
    forged = owner_client.post(
        f"/api/v1/solve-sessions/{solve.json()['id']}/attempts",
        json={
            "status": "solved",
            "verified_correct": True,
            "verification_method": "server:PRACTICE_SCORING_VERSION",
        },
        headers={"Idempotency-Key": "exam-progress-forged-attempt"},
    )
    assert forged.status_code == 422, forged.text
    assert forged.json()["code"] == "VERIFICATION_METHOD_FORBIDDEN"

    by_revision = _progress(
        owner_client,
        correct_task["revision"]["id"],
        incorrect_task["revision"]["id"],
        unanswered_task["revision"]["id"],
        versioned_first["revision"]["id"],
        versioned_latest["revision"]["id"],
        second_correct_task["revision"]["id"],
        started_task["revision"]["id"],
        untouched_task["revision"]["id"],
        forged_task["revision"]["id"],
    )
    assert by_revision[correct_task["revision"]["id"]]["status"] == "correct"
    assert by_revision[correct_task["revision"]["id"]]["solve_session_id"] is None
    assert by_revision[incorrect_task["revision"]["id"]]["status"] == "incorrect"
    assert by_revision[incorrect_task["revision"]["id"]]["solve_session_id"] is None
    assert by_revision[unanswered_task["revision"]["id"]]["status"] == "neutral"
    assert by_revision[versioned_first["revision"]["id"]]["status"] == "correct"
    assert by_revision[versioned_latest["revision"]["id"]]["status"] == "neutral"
    assert by_revision[second_correct_task["revision"]["id"]]["status"] == "correct"
    assert by_revision[started_task["revision"]["id"]]["status"] == "started"
    assert by_revision[started_task["revision"]["id"]]["solve_session_id"] is None
    assert by_revision[untouched_task["revision"]["id"]]["status"] == "neutral"
    assert by_revision[forged_task["revision"]["id"]]["status"] == "neutral"

    first_correct_page = owner_client.get(
        "/api/v1/tasks",
        params={"learning_status": "correct", "limit": 1},
    )
    assert first_correct_page.status_code == 200, first_correct_page.text
    assert first_correct_page.json()["total"] == 2
    assert len(first_correct_page.json()["items"]) == 1
    assert first_correct_page.json()["next_cursor"] is not None
    second_correct_page = owner_client.get(
        "/api/v1/tasks",
        params={
            "learning_status": "correct",
            "limit": 1,
            "cursor": first_correct_page.json()["next_cursor"],
        },
    )
    assert second_correct_page.status_code == 200, second_correct_page.text
    assert second_correct_page.json()["total"] == 2
    assert len(second_correct_page.json()["items"]) == 1
    assert second_correct_page.json()["items"][0]["id"] != first_correct_page.json()["items"][0]["id"]

    owner_client.headers["X-Owner-Session"] = issue_session_token(
        "foreign-owner", owner_client.app.state.settings.owner_session_secret
    )
    foreign = _progress(owner_client, correct_task["revision"]["id"], started_task["revision"]["id"])
    assert foreign[correct_task["revision"]["id"]]["status"] == "neutral"
    assert foreign[started_task["revision"]["id"]]["status"] == "neutral"


def test_exam_wrong_error_is_pinned_and_clears_from_errors_after_practice_correction(
    owner_client: TestClient,
) -> None:
    task = _import_task(owner_client, "exam-progress-error")
    exam = _exact_exam(owner_client, task["revision"]["id"], seed=107)
    item_id = exam["items"][0]["id"]
    assert owner_client.put(
        f"/api/v1/exams/{exam['id']}/answers/{item_id}", json={"content": "41"}
    ).status_code == 200
    submitted = owner_client.post(f"/api/v1/exams/{exam['id']}/submit")
    assert submitted.status_code == 200, submitted.text
    report_hash = submitted.json()["report_hash"]
    replayed = owner_client.post(f"/api/v1/exams/{exam['id']}/submit")
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["report_hash"] == report_hash

    errors = owner_client.get("/api/v1/errors")
    assert errors.status_code == 200, errors.text
    exam_errors = [item for item in errors.json()["items"] if item["source"] == "exam-report"]
    assert len(exam_errors) == 1
    assert exam_errors[0]["status"] == "confirmed"
    assert exam_errors[0]["error_type"] == "Неверный ответ"
    assert exam_errors[0]["task_revision_id"] == task["revision"]["id"]
    assert exam_errors[0]["exam_item_id"] == item_id
    assert exam_errors[0]["attempt_id"] is None
    assert exam_errors[0]["solve_session_id"] is None

    prepared = owner_client.post(
        "/api/v1/exams/prepare",
        json={
            "kind": "errors",
            "mode": "study",
            "required_lines": {"1": 1},
            "difficulty": "any",
            "duration_minutes": 30,
            "exclude_solved": False,
            "exclude_recent": False,
            "source_year": None,
            "include_corrected_errors": False,
        },
        headers={"Idempotency-Key": "exam-progress-errors-open"},
    )
    assert prepared.status_code == 201, prepared.text
    assert prepared.json()["items"][0]["task_revision_id"] == task["revision"]["id"]

    solve = owner_client.post("/api/v1/solve-sessions", json={"task_revision_id": task["revision"]["id"]})
    assert solve.status_code == 201, solve.text
    corrected = owner_client.post(
        f"/api/v1/solve-sessions/{solve.json()['id']}/check-answer",
        json={"answer": "42"},
        headers={"Idempotency-Key": "exam-progress-error-correction"},
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["status"] == "correct"
    corrected_progress = _progress(owner_client, task["revision"]["id"])
    assert corrected_progress[task["revision"]["id"]]["status"] == "correct"
    assert owner_client.get("/api/v1/errors").json()["items"][0]["id"] == exam_errors[0]["id"]

    excluded = owner_client.post(
        "/api/v1/exams/prepare",
        json={
            "kind": "errors",
            "mode": "study",
            "required_lines": {"1": 1},
            "difficulty": "any",
            "duration_minutes": 30,
            "exclude_solved": False,
            "exclude_recent": False,
            "source_year": None,
            "include_corrected_errors": False,
        },
        headers={"Idempotency-Key": "exam-progress-errors-excluded"},
    )
    assert excluded.status_code == 422, excluded.text
    assert excluded.json()["code"] == "EXAM_ERROR_POOL_EMPTY"

    included = owner_client.post(
        "/api/v1/exams/prepare",
        json={
            "kind": "errors",
            "mode": "study",
            "required_lines": {"1": 1},
            "difficulty": "any",
            "duration_minutes": 30,
            "exclude_solved": False,
            "exclude_recent": False,
            "source_year": None,
            "include_corrected_errors": True,
        },
        headers={"Idempotency-Key": "exam-progress-errors-included"},
    )
    assert included.status_code == 201, included.text
    assert included.json()["items"][0]["task_revision_id"] == task["revision"]["id"]

    report = owner_client.get(f"/api/v1/exams/{exam['id']}/report")
    assert report.status_code == 200, report.text
    assert report.json()["report_hash"] == report_hash


def test_partial_exam_report_is_incorrect_pinned_and_correctable(owner_client: TestClient) -> None:
    task = _import_task(
        owner_client,
        "exam-progress-partial",
        line_number=26,
        answer="1 2",
    )
    exam = _exact_exam(owner_client, task["revision"]["id"], seed=109, line_number=26)
    item_id = exam["items"][0]["id"]
    # Exact-revision blueprints use legacy scoring by default. Switch this
    # synthetic session to the real versioned practice scorer so line 26
    # produces an immutable one-point partial result.
    with owner_client.app.state.session_factory() as session:
        persisted_exam = session.get(ExamSession, exam["id"])
        assert persisted_exam is not None
        persisted_exam.scoring_version = PRACTICE_SCORING_VERSION
        session.commit()
    saved = owner_client.put(
        f"/api/v1/exams/{exam['id']}/answers/{item_id}",
        json={"content": "1"},
    )
    assert saved.status_code == 200, saved.text

    submitted = owner_client.post(f"/api/v1/exams/{exam['id']}/submit")
    assert submitted.status_code == 200, submitted.text
    report = submitted.json()["report"]
    report_item = report["items"][0]
    assert report_item["status"] == "partial"
    assert report_item["points"] == 1
    assert report_item["max_points"] == 2

    revision_id = task["revision"]["id"]
    progress = _progress(owner_client, revision_id)[revision_id]
    assert progress["status"] == "incorrect"
    assert progress["solve_session_id"] is None

    incorrect_tasks = owner_client.get(
        "/api/v1/tasks",
        params={"learning_status": "incorrect", "limit": 10},
    )
    assert incorrect_tasks.status_code == 200, incorrect_tasks.text
    assert incorrect_tasks.json()["total"] == 1
    assert [item["id"] for item in incorrect_tasks.json()["items"]] == [task["id"]]

    errors = owner_client.get("/api/v1/errors")
    assert errors.status_code == 200, errors.text
    exam_errors = [item for item in errors.json()["items"] if item["source"] == "exam-report"]
    assert len(exam_errors) == 1
    assert exam_errors[0]["error_type"] == "Неверный ответ"
    assert "частично" in exam_errors[0]["description"].casefold()
    assert exam_errors[0]["task_revision_id"] == revision_id
    assert exam_errors[0]["exam_item_id"] == item_id
    assert exam_errors[0]["attempt_id"] is None
    assert exam_errors[0]["solve_session_id"] is None

    prepared = owner_client.post(
        "/api/v1/exams/prepare",
        json={
            "kind": "errors",
            "mode": "study",
            "required_lines": {"26": 1},
            "difficulty": "any",
            "duration_minutes": 30,
            "exclude_solved": False,
            "exclude_recent": False,
            "source_year": None,
            "include_corrected_errors": False,
        },
        headers={"Idempotency-Key": "exam-progress-partial-errors-open"},
    )
    assert prepared.status_code == 201, prepared.text
    assert prepared.json()["items"][0]["task_revision_id"] == revision_id

    solve = owner_client.post("/api/v1/solve-sessions", json={"task_revision_id": revision_id})
    assert solve.status_code == 201, solve.text
    corrected = owner_client.post(
        f"/api/v1/solve-sessions/{solve.json()['id']}/check-answer",
        json={"answer": "1 2"},
        headers={"Idempotency-Key": "exam-progress-partial-correction"},
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["status"] == "correct"
    assert _progress(owner_client, revision_id)[revision_id]["status"] == "correct"

    excluded = owner_client.post(
        "/api/v1/exams/prepare",
        json={
            "kind": "errors",
            "mode": "study",
            "required_lines": {"26": 1},
            "difficulty": "any",
            "duration_minutes": 30,
            "exclude_solved": False,
            "exclude_recent": False,
            "source_year": None,
            "include_corrected_errors": False,
        },
        headers={"Idempotency-Key": "exam-progress-partial-errors-excluded"},
    )
    assert excluded.status_code == 422, excluded.text
    assert excluded.json()["code"] == "EXAM_ERROR_POOL_EMPTY"

    included = owner_client.post(
        "/api/v1/exams/prepare",
        json={
            "kind": "errors",
            "mode": "study",
            "required_lines": {"26": 1},
            "difficulty": "any",
            "duration_minutes": 30,
            "exclude_solved": False,
            "exclude_recent": False,
            "source_year": None,
            "include_corrected_errors": True,
        },
        headers={"Idempotency-Key": "exam-progress-partial-errors-included"},
    )
    assert included.status_code == 201, included.text
    assert included.json()["items"][0]["task_revision_id"] == revision_id

    historical_report = owner_client.get(f"/api/v1/exams/{exam['id']}/report")
    assert historical_report.status_code == 200, historical_report.text
    assert historical_report.json()["report"] == report


def test_v12_error_linkage_migration_preserves_legacy_error_row(tmp_path: Path) -> None:
    database = tmp_path / "exam-error-v12.sqlite"
    engine = make_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    try:
        init_schema(engine)
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO users(id, role, created_at) VALUES ('legacy-owner', 'owner', CURRENT_TIMESTAMP)")
            )
            connection.execute(
                text(
                    "INSERT INTO error_events "
                    "(id, owner_id, attempt_id, status, error_type, description, reason, prevention_note, "
                    "source, task_line, policy_version, created_at, updated_at) "
                    "VALUES ('legacy-error', 'legacy-owner', NULL, 'proposed', 'legacy', 'old', 'old', '', "
                    "'user', 1, 'error-capture-v1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
            connection.exec_driver_sql("DROP INDEX ix_error_events_owner_id")
            connection.exec_driver_sql("DROP INDEX ix_error_events_task_revision_id")
            connection.exec_driver_sql("DROP INDEX ix_error_events_exam_item_id")
            connection.exec_driver_sql("ALTER TABLE error_events RENAME TO error_events_v12")
            connection.exec_driver_sql(
                "CREATE TABLE error_events ("
                "id VARCHAR(36) NOT NULL PRIMARY KEY, "
                "owner_id VARCHAR(64) NOT NULL, "
                "attempt_id VARCHAR(36), "
                "status VARCHAR(32) NOT NULL, "
                "error_type VARCHAR(128) NOT NULL, "
                "description TEXT NOT NULL, "
                "reason TEXT NOT NULL, "
                "prevention_note TEXT NOT NULL, "
                "source VARCHAR(64) NOT NULL, "
                "task_line INTEGER, "
                "policy_version VARCHAR(32) NOT NULL, "
                "created_at DATETIME NOT NULL, "
                "updated_at DATETIME NOT NULL, "
                "FOREIGN KEY (owner_id) REFERENCES users(id), "
                "FOREIGN KEY (attempt_id) REFERENCES attempts(id))"
            )
            connection.exec_driver_sql(
                "INSERT INTO error_events "
                "SELECT id, owner_id, attempt_id, status, error_type, description, reason, prevention_note, "
                "source, task_line, policy_version, created_at, updated_at FROM error_events_v12"
            )
            connection.exec_driver_sql("DROP TABLE error_events_v12")
            connection.execute(text("DELETE FROM schema_migrations WHERE version >= 12"))

        init_schema(engine)
        with engine.connect() as connection:
            version = connection.execute(text("SELECT MAX(version) FROM schema_migrations")).scalar_one()
            columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info('error_events')").fetchall()
            }
            legacy = connection.execute(
                text("SELECT status, error_type, description FROM error_events WHERE id = 'legacy-error'")
            ).one()
            assert version == CURRENT_SCHEMA_VERSION == 13
            assert {"task_revision_id", "exam_item_id"}.issubset(columns)
            assert tuple(legacy) == ("proposed", "legacy", "old")
    finally:
        engine.dispose()


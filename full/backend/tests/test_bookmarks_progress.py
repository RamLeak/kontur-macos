from __future__ import annotations

from pathlib import Path

import pytest
from backend.app.auth import issue_session_token
from backend.app.config import Settings
from backend.app.db import CURRENT_SCHEMA_VERSION
from backend.app.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy import text


@pytest.fixture()
def owner_client(tmp_path: Path):
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
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        yield client


def _auth(client: TestClient, owner_id: str) -> None:
    settings = client.app.state.settings
    client.headers["X-Owner-Session"] = issue_session_token(owner_id, settings.owner_session_secret)


def _import_task(client: TestClient, source_id: str, *, statement: str = "Solve the task.") -> dict[str, object]:
    response = client.post(
        "/api/v1/source/import-fixture",
        json={
            "source_url": f"https://kompege.ru/task?id={source_id}",
            "source_id": source_id,
            "line_number": 1,
            "statement": statement,
            "answer": "42",
            "rights_status": "private_authorized",
        },
        headers={"Idempotency-Key": f"fixture-{source_id}-{statement}"},
    )
    assert response.status_code == 201, response.text
    return response.json()["task"]


def _solve(client: TestClient, revision_id: str) -> dict[str, object]:
    response = client.post("/api/v1/solve-sessions", json={"task_revision_id": revision_id})
    assert response.status_code == 201, response.text
    return response.json()


def test_bookmarks_are_owner_scoped_idempotent_and_revision_validated(owner_client: TestClient) -> None:
    task = _import_task(owner_client, "bookmark-task", statement="First revision.")
    task_id = task["id"]
    first_revision_id = task["revision"]["id"]
    updated = _import_task(owner_client, "bookmark-task", statement="Second revision.")
    second_revision_id = updated["revision"]["id"]
    other_task = _import_task(owner_client, "other-bookmark-task")

    mismatch = owner_client.put(
        f"/api/v1/bookmarks/{task_id}", json={"task_revision_id": other_task["revision"]["id"]}
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["code"] == "REVISION_INVALID"

    first = owner_client.put(f"/api/v1/bookmarks/{task_id}", json={"task_revision_id": first_revision_id})
    repeated = owner_client.put(f"/api/v1/bookmarks/{task_id}", json={"task_revision_id": first_revision_id})
    assert first.status_code == repeated.status_code == 200
    assert first.json() == repeated.json()

    moved = owner_client.put(f"/api/v1/bookmarks/{task_id}", json={"task_revision_id": second_revision_id})
    assert moved.status_code == 200
    assert moved.json()["task_revision_id"] == second_revision_id
    assert owner_client.get("/api/v1/bookmarks").json() == [moved.json()]

    _auth(owner_client, "foreign-owner")
    assert owner_client.get("/api/v1/bookmarks").json() == []
    assert owner_client.delete(f"/api/v1/bookmarks/{task_id}").status_code == 204
    _auth(owner_client, "owner")
    assert owner_client.get("/api/v1/bookmarks").json() == [moved.json()]


def test_progress_statuses_ignore_empty_work_and_exam_evidence(owner_client: TestClient) -> None:
    neutral_task = _import_task(owner_client, "progress-neutral")
    neutral_solve = _solve(owner_client, neutral_task["revision"]["id"])
    whitespace = owner_client.put(
        f"/api/v1/solve-sessions/{neutral_solve['id']}/code", json={"content": " \n\t ", "version": 0}
    )
    assert whitespace.status_code == 200
    empty_board_task = _import_task(owner_client, "progress-empty-board")
    empty_board_solve = _solve(owner_client, empty_board_task["revision"]["id"])
    empty_board = owner_client.put(
        f"/api/v1/solve-sessions/{empty_board_solve['id']}/board",
        json={"snapshot": {"strokes": []}, "version": 0},
    )
    assert empty_board.status_code == 200

    started_task = _import_task(owner_client, "progress-started")
    started_solve = _solve(owner_client, started_task["revision"]["id"])
    saved_code = owner_client.put(
        f"/api/v1/solve-sessions/{started_solve['id']}/code", json={"content": "print(42)", "version": 0}
    )
    assert saved_code.status_code == 200

    board_task = _import_task(owner_client, "progress-board")
    board_solve = _solve(owner_client, board_task["revision"]["id"])
    saved_board = owner_client.put(
        f"/api/v1/solve-sessions/{board_solve['id']}/board",
        json={"snapshot": {"strokes": [{"points": [{"x": 0, "y": 0}]}]}, "version": 0},
    )
    assert saved_board.status_code == 200

    annotation_task = _import_task(owner_client, "progress-annotation")
    annotation_solve = _solve(owner_client, annotation_task["revision"]["id"])
    annotation = owner_client.post(
        f"/api/v1/solve-sessions/{annotation_solve['id']}/annotations",
        json={"kind": "text", "payload": {"note": "remember"}, "anchor": {"quote": "task"}},
    )
    assert annotation.status_code == 201

    incorrect_task = _import_task(owner_client, "progress-incorrect")
    incorrect_solve = _solve(owner_client, incorrect_task["revision"]["id"])
    incorrect = owner_client.post(
        f"/api/v1/solve-sessions/{incorrect_solve['id']}/attempts",
        json={"status": "failed", "verified_correct": False, "verification_method": "owner_review"},
    )
    assert incorrect.status_code == 201
    incorrect_check = owner_client.post(
        f"/api/v1/solve-sessions/{incorrect_solve['id']}/check-answer",
        json={"answer": "41"},
        headers={"Idempotency-Key": "progress-incorrect-check"},
    )
    assert incorrect_check.status_code == 200
    assert incorrect_check.json()["status"] == "incorrect"

    correct_task = _import_task(owner_client, "progress-correct")
    correct_solve = _solve(owner_client, correct_task["revision"]["id"])
    correct = owner_client.post(
        f"/api/v1/solve-sessions/{correct_solve['id']}/attempts",
        json={"status": "solved", "verified_correct": True, "verification_method": "owner_review"},
    )
    assert correct.status_code == 201
    correct_check = owner_client.post(
        f"/api/v1/solve-sessions/{correct_solve['id']}/check-answer",
        json={"answer": "42"},
        headers={"Idempotency-Key": "progress-correct-check"},
    )
    assert correct_check.status_code == 200
    assert correct_check.json()["status"] == "correct"

    technical_task = _import_task(owner_client, "progress-technical-error")
    technical_solve = _solve(owner_client, technical_task["revision"]["id"])
    technical = owner_client.post(
        f"/api/v1/solve-sessions/{technical_solve['id']}/attempts",
        json={
            "status": "failed",
            "verified_correct": False,
            "verification_method": "runner_error",
            "error_type": "RUNNER_UNAVAILABLE",
        },
    )
    assert technical.status_code == 201

    exam_task = _import_task(owner_client, "progress-exam")
    blueprint = owner_client.post(
        "/api/v1/exam-blueprints", json={"name": "Progress exam", "required_lines": {"1": 1}}
    )
    assert blueprint.status_code == 201
    exam = owner_client.post("/api/v1/exams", json={"blueprint_id": blueprint.json()["id"], "seed": 7})
    assert exam.status_code == 201
    item_id = exam.json()["items"][0]["id"]
    saved_exam_answer = owner_client.put(f"/api/v1/exams/{exam.json()['id']}/answers/{item_id}", json={"content": "42"})
    assert saved_exam_answer.status_code == 200

    revision_ids = [
        neutral_task["revision"]["id"],
        empty_board_task["revision"]["id"],
        started_task["revision"]["id"],
        board_task["revision"]["id"],
        annotation_task["revision"]["id"],
        incorrect_task["revision"]["id"],
        correct_task["revision"]["id"],
        technical_task["revision"]["id"],
        exam_task["revision"]["id"],
    ]
    response = owner_client.get("/api/v1/task-progress", params=[("revision_ids", value) for value in revision_ids])
    assert response.status_code == 200, response.text
    by_revision = {item["task_revision_id"]: item for item in response.json()}
    assert by_revision[neutral_task["revision"]["id"]]["status"] == "neutral"
    assert by_revision[neutral_task["revision"]["id"]]["solve_session_id"] is None
    assert by_revision[empty_board_task["revision"]["id"]]["status"] == "neutral"
    assert by_revision[started_task["revision"]["id"]]["status"] == "started"
    assert by_revision[started_task["revision"]["id"]]["solve_session_id"] == started_solve["id"]
    assert by_revision[board_task["revision"]["id"]]["status"] == "started"
    assert by_revision[board_task["revision"]["id"]]["solve_session_id"] == board_solve["id"]
    assert by_revision[annotation_task["revision"]["id"]]["status"] == "started"
    assert by_revision[annotation_task["revision"]["id"]]["solve_session_id"] == annotation_solve["id"]
    assert by_revision[incorrect_task["revision"]["id"]]["status"] == "incorrect"
    assert by_revision[correct_task["revision"]["id"]]["status"] == "correct"
    assert by_revision[technical_task["revision"]["id"]]["status"] == "neutral"
    assert by_revision[exam_task["revision"]["id"]]["status"] == "neutral"

    _auth(owner_client, "foreign-owner")
    foreign = owner_client.get("/api/v1/task-progress", params=[("revision_ids", revision_ids[1])])
    assert foreign.status_code == 200
    assert foreign.json()[0]["status"] == "neutral"
    assert foreign.json()[0]["solve_session_id"] is None


def test_task_filters_apply_before_count_and_keyset(owner_client: TestClient) -> None:
    started_one = _import_task(owner_client, "filter-started-one")
    started_two = _import_task(owner_client, "filter-started-two")
    neutral = _import_task(owner_client, "filter-neutral")
    for task, content in ((started_one, "x = 1"), (started_two, "x = 2")):
        solve = _solve(owner_client, task["revision"]["id"])
        saved = owner_client.put(
            f"/api/v1/solve-sessions/{solve['id']}/code", json={"content": content, "version": 0}
        )
        assert saved.status_code == 200
    bookmarked = owner_client.put(
        f"/api/v1/bookmarks/{started_two['id']}", json={"task_revision_id": started_two["revision"]["id"]}
    )
    assert bookmarked.status_code == 200

    first = owner_client.get("/api/v1/tasks", params={"learning_status": "started", "limit": 1})
    assert first.status_code == 200, first.text
    assert first.json()["total"] == 2
    assert len(first.json()["items"]) == 1
    assert first.json()["items"][0]["id"] != neutral["id"]
    assert first.json()["next_cursor"] is not None

    second = owner_client.get(
        "/api/v1/tasks",
        params={"learning_status": "started", "limit": 1, "cursor": first.json()["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    assert second.json()["total"] == 2
    assert len(second.json()["items"]) == 1
    assert second.json()["items"][0]["id"] != first.json()["items"][0]["id"]

    bookmarks = owner_client.get("/api/v1/tasks", params={"bookmarked_only": "true"})
    assert bookmarks.status_code == 200, bookmarks.text
    assert bookmarks.json()["total"] == 1
    assert bookmarks.json()["items"][0]["id"] == started_two["id"]


def test_bookmark_schema_migrates_and_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "bookmarks.sqlite"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database.as_posix()}",
        asset_root=tmp_path / "assets",
        owner_session_secret="test-owner-secret",
        source_mode="fixture",
        source_permission_status="private_authorized",
        testing=True,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        client.headers["X-Owner-Session"] = issue_session_token("owner", settings.owner_session_secret)
        task = _import_task(client, "restart-bookmark")
        saved = client.put(
            f"/api/v1/bookmarks/{task['id']}", json={"task_revision_id": task["revision"]["id"]}
        )
        assert saved.status_code == 200
        with client.app.state.session_factory() as session:
            version = session.execute(text("SELECT MAX(version) FROM schema_migrations")).scalar_one()
            assert version == CURRENT_SCHEMA_VERSION == 13
            assert session.execute(text("SELECT COUNT(*) FROM bookmarks")).scalar_one() == 1

    restarted = create_app(settings)
    with TestClient(restarted) as client:
        client.headers["X-Owner-Session"] = issue_session_token("owner", settings.owner_session_secret)
        bookmarks = client.get("/api/v1/bookmarks")
        assert bookmarks.status_code == 200
        assert bookmarks.json()[0]["task_revision_id"] == task["revision"]["id"]

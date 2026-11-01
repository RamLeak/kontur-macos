from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from backend.app.auth import issue_session_token
from backend.app.config import Settings
from backend.app.db import CURRENT_SCHEMA_VERSION, init_schema, make_engine
from backend.app.main import create_app
from backend.app.runner_gateway import RunnerUnavailable
from fastapi.testclient import TestClient
from sqlalchemy import text


class FakeRunner:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.calls: list[dict[str, Any]] = []

    def run(self, *, code: str, inputs: list[dict[str, Any]], limits: dict[str, Any]) -> dict[str, Any]:
        if self.unavailable:
            raise RunnerUnavailable("runner capability unavailable")
        self.calls.append({"code": code, "inputs": inputs, "limits": limits})
        return {
            "status": "succeeded",
            "stdout": f"ran:{code}",
            "stderr": "",
            "exit_code": 0,
            "duration_ms": 8,
            "input_digest": "d" * 64,
            "resource": {"network": False},
        }


@pytest.fixture()
def runner_client(tmp_path: Path) -> tuple[TestClient, FakeRunner]:
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        asset_root=tmp_path / "assets",
        owner_session_secret="test-owner-secret",
        source_mode="fixture",
        source_permission_status="private_authorized",
        runner_mode="isolated",
        runner_attested=True,
        runner_base_url="http://127.0.0.1:9797",
        testing=True,
    )
    app = create_app(settings)
    fake = FakeRunner()
    app.state.runner_client = fake
    with TestClient(app) as client:
        client.headers["X-Owner-Session"] = issue_session_token("owner", settings.owner_session_secret)
        yield client, fake


def _import_task(client: TestClient, source_id: str) -> dict[str, Any]:
    response = client.post(
        "/api/v1/source/import-fixture",
        json={
            "source_url": f"https://kompege.ru/task?id={source_id}",
            "source_id": source_id,
            "line_number": 1,
            "statement": "Synthetic exam code task.",
            "answer": "42",
            "rights_status": "private_authorized",
        },
        headers={"Idempotency-Key": f"import-{source_id}"},
    )
    assert response.status_code == 201, response.text
    return response.json()["task"]


def _start_exam(client: TestClient, source_id: str, *, mode: str = "strict") -> tuple[dict[str, Any], str]:
    task = _import_task(client, source_id)
    blueprint = client.post(
        "/api/v1/exam-blueprints",
        json={"name": "Code run exam", "required_lines": {"1": 1}, "constraints": {"exam": {"duration_minutes": 60}}},
    )
    assert blueprint.status_code == 201, blueprint.text
    exam = client.post(
        "/api/v1/exams",
        json={"blueprint_id": blueprint.json()["id"], "seed": 11, "mode": mode},
    )
    assert exam.status_code == 201, exam.text
    return exam.json(), task["revision"]["id"]


def test_exam_code_run_reads_saved_version_and_replays_idempotently(
    runner_client: tuple[TestClient, FakeRunner],
) -> None:
    client, runner = runner_client
    exam, _revision_id = _start_exam(client, "exam-code-run")
    item_id = exam["items"][0]["id"]
    workspace = f"/api/v1/exams/{exam['id']}/items/{item_id}/workspace"
    saved = client.put(f"{workspace}/code", json={"content": "print(1)", "version": 0})
    assert saved.status_code == 200, saved.text

    first = client.post(
        f"/api/v1/exams/{exam['id']}/items/{item_id}/code-runs",
        json={"version": saved.json()["code"]["version"]},
        headers={"Idempotency-Key": "exam-run-1"},
    )
    assert first.status_code == 201, first.text
    payload = first.json()
    assert payload["status"] == "succeeded"
    assert payload["stdout"] == "ran:print(1)"
    assert payload["workspace_version"] == 1
    assert payload["code_hash"]
    assert payload["item_id"] == item_id
    assert len(runner.calls) == 1

    replay = client.post(
        f"/api/v1/exams/{exam['id']}/items/{item_id}/code-runs",
        json={"version": 1},
        headers={"Idempotency-Key": "exam-run-1"},
    )
    assert replay.status_code == 201
    assert replay.json() == payload
    assert len(runner.calls) == 1

    changed = client.put(f"{workspace}/code", json={"content": "print(2)", "version": 1})
    assert changed.status_code == 200, changed.text
    stale = client.post(
        f"/api/v1/exams/{exam['id']}/items/{item_id}/code-runs",
        json={"version": 1},
        headers={"Idempotency-Key": "exam-run-stale"},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "VERSION_CONFLICT"
    second = client.post(
        f"/api/v1/exams/{exam['id']}/items/{item_id}/code-runs",
        json={"version": 2},
        headers={"Idempotency-Key": "exam-run-2"},
    )
    assert second.status_code == 201, second.text
    assert second.json()["stdout"] == "ran:print(2)"
    history = client.get(f"/api/v1/exams/{exam['id']}/items/{item_id}/code-runs")
    assert history.status_code == 200
    assert [item["workspace_version"] for item in history.json()["items"]] == [2, 1]
    submitted = client.post(f"/api/v1/exams/{exam['id']}/submit")
    assert submitted.status_code == 200, submitted.text
    report_runs = submitted.json()["report"]["workspaces"][item_id]["code_runs"]
    assert [item["stdout"] for item in report_runs] == ["ran:print(1)", "ran:print(2)"]


def test_exam_code_run_is_owner_scoped_and_lifecycle_guarded(runner_client: tuple[TestClient, FakeRunner]) -> None:
    client, _runner = runner_client
    exam, _revision_id = _start_exam(client, "exam-code-owner")
    item_id = exam["items"][0]["id"]
    workspace = f"/api/v1/exams/{exam['id']}/items/{item_id}/workspace"
    saved = client.put(f"{workspace}/code", json={"content": "x=1", "version": 0})
    assert saved.status_code == 200
    _auth(client, "foreign-owner")
    hidden = client.get(f"/api/v1/exams/{exam['id']}/items/{item_id}/code-runs")
    assert hidden.status_code == 404
    hidden_run = client.post(
        f"/api/v1/exams/{exam['id']}/items/{item_id}/code-runs",
        json={"version": 1},
        headers={"Idempotency-Key": "foreign-run"},
    )
    assert hidden_run.status_code == 404

    _auth(client, "owner")
    assert client.post(f"/api/v1/exams/{exam['id']}/pause").status_code == 423


def test_exam_code_run_unavailable_is_persisted_without_fake_success(tmp_path: Path) -> None:
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        asset_root=tmp_path / "assets",
        owner_session_secret="test-owner-secret",
        source_mode="fixture",
        source_permission_status="private_authorized",
        runner_mode="isolated",
        runner_attested=True,
        runner_base_url="http://127.0.0.1:9797",
        testing=True,
    )
    app = create_app(settings)
    fake = FakeRunner(unavailable=True)
    app.state.runner_client = fake
    with TestClient(app) as client:
        client.headers["X-Owner-Session"] = issue_session_token("owner", settings.owner_session_secret)
        exam, _revision_id = _start_exam(client, "exam-code-unavailable")
        item_id = exam["items"][0]["id"]
        workspace = f"/api/v1/exams/{exam['id']}/items/{item_id}/workspace"
        saved = client.put(f"{workspace}/code", json={"content": "print('safe')", "version": 0})
        assert saved.status_code == 200
        response = client.post(
            f"/api/v1/exams/{exam['id']}/items/{item_id}/code-runs",
            json={"version": 1},
            headers={"Idempotency-Key": "unavailable-run"},
        )
        assert response.status_code == 503
        assert response.json()["code"] == "RUNNER_UNAVAILABLE"
        run_id = response.json()["details"]["run_id"]
        history = client.get(f"/api/v1/exams/{exam['id']}/items/{item_id}/code-runs").json()["items"]
        assert history[0]["id"] == run_id
        assert history[0]["status"] == "unavailable"
        assert history[0]["stdout"] == ""
        replay = client.post(
            f"/api/v1/exams/{exam['id']}/items/{item_id}/code-runs",
            json={"version": 1},
            headers={"Idempotency-Key": "unavailable-run"},
        )
        assert replay.status_code == 503
        assert replay.json()["details"]["run_id"] == run_id


def test_v10_restart_preserves_exam_code_run_row(tmp_path: Path) -> None:
    database = tmp_path / "exam-code-v10.sqlite"
    engine = make_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    try:
        init_schema(engine)
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM schema_migrations WHERE version > 8"))
            connection.exec_driver_sql("DROP TABLE exam_code_runs")
            for column in ("prepare_kind", "prepare_options", "prepared_at"):
                connection.exec_driver_sql(f"ALTER TABLE exam_sessions DROP COLUMN {column}")
        init_schema(engine)
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT MAX(version) FROM schema_migrations")).scalar_one()
                == CURRENT_SCHEMA_VERSION
                    == 13
            )
            assert connection.execute(text("SELECT COUNT(*) FROM exam_code_runs")).scalar_one() == 0
    finally:
        engine.dispose()

    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database.as_posix()}",
        asset_root=tmp_path / "assets",
        owner_session_secret="test-owner-secret",
        source_mode="fixture",
        source_permission_status="private_authorized",
        runner_mode="isolated",
        runner_attested=True,
        runner_base_url="http://127.0.0.1:9797",
        testing=True,
    )
    app = create_app(settings)
    fake = FakeRunner()
    app.state.runner_client = fake
    with TestClient(app) as client:
        client.headers["X-Owner-Session"] = issue_session_token("owner", settings.owner_session_secret)
        exam, _revision_id = _start_exam(client, "migration-code-run")
        item_id = exam["items"][0]["id"]
        workspace = f"/api/v1/exams/{exam['id']}/items/{item_id}/workspace"
        saved = client.put(f"{workspace}/code", json={"content": "print('persist')", "version": 0})
        assert saved.status_code == 200
        run = client.post(
            f"/api/v1/exams/{exam['id']}/items/{item_id}/code-runs",
            json={"version": 1},
            headers={"Idempotency-Key": "migration-run"},
        )
        assert run.status_code == 201, run.text
        run_id = run.json()["id"]

    restarted = create_app(settings)
    with TestClient(restarted) as client:
        client.headers["X-Owner-Session"] = issue_session_token("owner", settings.owner_session_secret)
        history = client.get(f"/api/v1/exams/{exam['id']}/items/{item_id}/code-runs")
        assert history.status_code == 200
        assert history.json()["items"][0]["id"] == run_id


def _auth(client: TestClient, owner_id: str) -> None:
    settings = client.app.state.settings
    client.headers["X-Owner-Session"] = issue_session_token(owner_id, settings.owner_session_secret)

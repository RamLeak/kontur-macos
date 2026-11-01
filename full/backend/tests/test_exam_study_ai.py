from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from backend.app.auth import issue_session_token
from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.models import AgentRun, ExamStudyAiContextSnapshot, ExamStudyAiRun, LearningEvent, SolveSession
from fastapi.testclient import TestClient
from sqlalchemy import select


class FakeStudyCodex:
    def __init__(self) -> None:
        self.digest = "b" * 64
        self.run_calls: list[str] = []
        self.status_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.requests: list[dict[str, Any]] = []
        self.runs: dict[str, dict[str, Any]] = {}

    def run(
        self,
        *,
        run_id: str,
        model: str,
        effort: str,
        manifest: dict[str, Any],
        sections: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.run_calls.append(run_id)
        self.requests.append(
            {"run_id": run_id, "model": model, "effort": effort, "manifest": manifest, "sections": sections}
        )
        self.runs[run_id] = {"status": "queued", "content": ""}
        return {
            "accepted": True,
            "status": "queued",
            "content": "",
            "input_digest": self.digest,
            "gateway": {
                "schema_version": 1,
                "run_id": run_id,
                "input_digest": self.digest,
                "model": model,
                "effort": effort,
            },
        }

    def status(self, run_id: str) -> dict[str, Any]:
        self.status_calls.append(run_id)
        record = self.runs[run_id]
        request = self.requests[self.run_calls.index(run_id)]
        return {
            "schema_version": 1,
            "run_id": run_id,
            "input_digest": self.digest,
            "model": request["model"],
            "effort": request["effort"],
            "status": record["status"],
            "final_output": record["content"],
            "error_code": None,
        }

    def cancel(self, run_id: str) -> dict[str, Any]:
        self.cancel_calls.append(run_id)
        self.runs[run_id]["status"] = "cancelled"
        return {
            "schema_version": 1,
            "run_id": run_id,
            "input_digest": self.digest,
            "model": self.requests[self.run_calls.index(run_id)]["model"],
            "effort": self.requests[self.run_calls.index(run_id)]["effort"],
            "status": "cancelled",
            "final_output": "",
            "error_code": "AGENT_CANCELLED",
        }


@pytest.fixture()
def study_client(tmp_path: Path) -> tuple[TestClient, FakeStudyCodex]:
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        asset_root=tmp_path / "assets",
        owner_session_secret="test-owner-secret",
        source_mode="fixture",
        source_permission_status="private_authorized",
        codex_enabled=True,
        codex_attested=True,
        codex_base_url="http://127.0.0.1:8787",
        testing=True,
    )
    app = create_app(settings)
    fake = FakeStudyCodex()
    app.state.codex_client = fake
    with TestClient(app) as client:
        client.headers["X-Owner-Session"] = issue_session_token("owner", settings.owner_session_secret)
        yield client, fake


def _study_exam(client: TestClient, source_id: str = "study-ai-task") -> tuple[dict[str, Any], str]:
    imported = client.post(
        "/api/v1/source/import-fixture",
        json={
            "source_url": f"https://kompege.ru/task?id={source_id}",
            "source_id": source_id,
            "line_number": 1,
            "statement": "Synthetic Study AI task.",
            "answer": "42",
            "explanation": "hidden explanation",
            "rights_status": "private_authorized",
        },
        headers={"Idempotency-Key": f"import-{source_id}"},
    )
    assert imported.status_code == 201, imported.text
    blueprint = client.post(
        "/api/v1/exam-blueprints",
        json={"name": "Study AI exam", "required_lines": {"1": 1}, "constraints": {"exam": {"duration_minutes": 60}}},
    )
    assert blueprint.status_code == 201, blueprint.text
    exam = client.post(
        "/api/v1/exams",
        json={"blueprint_id": blueprint.json()["id"], "seed": 31, "mode": "study"},
    )
    assert exam.status_code == 201, exam.text
    return exam.json(), imported.json()["task"]["revision"]["id"]


def _study_path(exam_id: str, item_id: str) -> str:
    return f"/api/v1/exams/{exam_id}/items/{item_id}/study-ai/runs"


def test_study_ai_freezes_server_context_replays_and_records_visible_help(
    study_client: tuple[TestClient, FakeStudyCodex],
) -> None:
    client, fake = study_client
    exam, revision_id = _study_exam(client)
    item_id = exam["items"][0]["id"]
    workspace = f"/api/v1/exams/{exam['id']}/items/{item_id}/workspace"
    saved = client.put(f"{workspace}/code", json={"content": "print(42)", "version": 0})
    assert saved.status_code == 200, saved.text
    first = client.post(
        _study_path(exam["id"], item_id),
        json={"message": "Подскажи следующий шаг", "workspace_version": 1},
        headers={"Idempotency-Key": "study-ai-1"},
    )
    assert first.status_code == 201, first.text
    payload = first.json()
    assert payload["status"] == "queued"
    assert payload["task_revision_id"] == revision_id
    assert "snapshot_json" not in payload
    assert fake.run_calls and len(fake.run_calls) == 1
    sections = fake.requests[0]["sections"]
    joined = "\n".join(section["content"] for section in sections)
    assert "hidden explanation" not in joined
    assert all("answer" not in section["source_id"].casefold() for section in sections)

    replay = client.post(
        _study_path(exam["id"], item_id),
        json={"message": "Подскажи следующий шаг", "workspace_version": 1},
        headers={"Idempotency-Key": "study-ai-1"},
    )
    assert replay.status_code == 201
    assert replay.json() == payload
    assert len(fake.run_calls) == 1
    changed = client.post(
        _study_path(exam["id"], item_id),
        json={"message": "Другой вопрос", "workspace_version": 1},
        headers={"Idempotency-Key": "study-ai-1"},
    )
    assert changed.status_code == 409
    assert changed.json()["code"] == "IDEMPOTENCY_CONFLICT"

    agent_id = fake.run_calls[0]
    fake.runs[agent_id] = {"status": "succeeded", "content": "Проверь границу цикла."}
    current = client.get(f"{_study_path(exam['id'], item_id)}/{payload['id']}")
    assert current.status_code == 200, current.text
    assert current.json()["status"] == "succeeded"
    assert current.json()["content"] == "Проверь границу цикла."

    with client.app.state.session_factory() as session:
        snapshot = session.get(ExamStudyAiContextSnapshot, payload["context_snapshot_id"])
        study_run = session.get(ExamStudyAiRun, payload["id"])
        agent = session.get(AgentRun, agent_id)
        assert snapshot is not None and study_run is not None and agent is not None
        assert study_run.agent_run_id == agent.id
        assert agent.solve_session_id is None
        assert session.query(SolveSession).count() == 0
        snapshot_text = json.dumps(snapshot.snapshot_json, ensure_ascii=False)
        assert "answer" not in snapshot_text.casefold()
        assert "hidden explanation" not in snapshot_text
        visible = session.scalar(
            select(LearningEvent).where(
                LearningEvent.event_type == "study_assist_help_visible",
                LearningEvent.aggregate_id == study_run.id,
            )
        )
        assert visible is not None


def test_study_ai_owner_sealed_pause_and_cancel_guards(study_client: tuple[TestClient, FakeStudyCodex]) -> None:
    client, fake = study_client
    exam, _revision_id = _study_exam(client, "study-ai-guards")
    item_id = exam["items"][0]["id"]
    path = _study_path(exam["id"], item_id)
    started = client.post(
        path,
        json={"message": "Explain", "workspace_version": 0},
        headers={"Idempotency-Key": "cancel-me"},
    )
    assert started.status_code == 201, started.text
    cancelled = client.post(f"{path}/{started.json()['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert fake.cancel_calls == [fake.run_calls[0]]

    client.headers["X-Owner-Session"] = issue_session_token("foreign", client.app.state.settings.owner_session_secret)
    assert client.get(f"{path}/{started.json()['id']}").status_code == 404
    client.headers["X-Owner-Session"] = issue_session_token("owner", client.app.state.settings.owner_session_secret)
    assert client.post(f"/api/v1/exams/{exam['id']}/pause").status_code == 200
    paused = client.get(f"{path}/{started.json()['id']}")
    assert paused.status_code == 423
    assert paused.json()["code"] == "EXAM_NOT_ACTIVE"

    strict_exam, _ = _study_exam(client, "study-ai-sealed")
    # A separate strict session cannot coexist as active with the study one,
    # so exercise the global sealed gate using a fresh owner/app fixture shape.
    blueprint = client.post(
        "/api/v1/exam-blueprints",
        json={"name": "Strict gate", "required_lines": {"1": 1}, "constraints": {"exam": {"duration_minutes": 60}}},
    )
    strict = client.post(
        "/api/v1/exams",
        json={"blueprint_id": blueprint.json()["id"], "seed": 32, "mode": "strict"},
    )
    assert strict.status_code == 201, strict.text
    strict_item = strict.json()["items"][0]["id"]
    sealed = client.post(
        _study_path(strict.json()["id"], strict_item),
        json={"message": "blocked", "workspace_version": 0},
        headers={"Idempotency-Key": "sealed-study"},
    )
    assert sealed.status_code == 423
    assert sealed.json()["code"] == "EXAM_SEALED"
    assert strict_exam["id"] != strict.json()["id"]


def test_study_ai_poll_and_cancel_fail_closed_after_strict_exam_starts(
    study_client: tuple[TestClient, FakeStudyCodex],
) -> None:
    client, fake = study_client
    exam, _revision_id = _study_exam(client, "study-ai-strict-after-start")
    item_id = exam["items"][0]["id"]
    path = _study_path(exam["id"], item_id)
    started = client.post(
        path,
        json={"message": "Keep this question active", "workspace_version": 0},
        headers={"Idempotency-Key": "strict-after-study"},
    )
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]
    assert fake.run_calls

    blueprint = client.post(
        "/api/v1/exam-blueprints",
        json={
            "name": "Strict after Study AI",
            "required_lines": {"1": 1},
            "constraints": {"exam": {"duration_minutes": 60}},
        },
    )
    assert blueprint.status_code == 201, blueprint.text
    strict = client.post(
        "/api/v1/exams",
        json={"blueprint_id": blueprint.json()["id"], "seed": 33, "mode": "strict"},
    )
    assert strict.status_code == 201, strict.text

    polled = client.get(f"{path}/{run_id}")
    assert polled.status_code == 423, polled.text
    assert polled.json()["code"] == "EXAM_SEALED"
    assert set(polled.json()) == {"code", "message", "details"}
    assert "content" not in polled.json()
    assert "context_snapshot_id" not in polled.json()

    cancelled = client.post(f"{path}/{run_id}/cancel")
    assert cancelled.status_code == 423, cancelled.text
    assert cancelled.json()["code"] == "EXAM_SEALED"
    assert set(cancelled.json()) == {"code", "message", "details"}
    assert "content" not in cancelled.json()
    assert "context_snapshot_id" not in cancelled.json()


def test_study_ai_unavailable_is_persisted_and_replayed(tmp_path: Path) -> None:
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
        exam, _revision_id = _study_exam(client, "study-ai-unavailable")
        item_id = exam["items"][0]["id"]
        path = _study_path(exam["id"], item_id)
        first = client.post(
            path,
            json={"message": "Help", "workspace_version": 0},
            headers={"Idempotency-Key": "unavailable-study"},
        )
        assert first.status_code == 503
        run_id = first.json()["details"]["run_id"]
        replay = client.post(
            path,
            json={"message": "Help", "workspace_version": 0},
            headers={"Idempotency-Key": "unavailable-study"},
        )
        assert replay.status_code == 503
        assert replay.json()["details"]["run_id"] == run_id
        with client.app.state.session_factory() as session:
            event = session.scalar(
                select(LearningEvent).where(
                    LearningEvent.owner_id == "owner",
                    LearningEvent.aggregate_type == "exam_study_ai_run",
                    LearningEvent.aggregate_id == run_id,
                    LearningEvent.event_type == "study_ai_unavailable",
                )
            )
            assert event is not None

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from backend.app.auth import issue_session_token
from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.models import (
    AgentEvent,
    AgentRun,
    ContextPack,
    IdempotencyReservation,
    LearningEvent,
    Message,
    Skill,
    utcnow,
)
from fastapi.testclient import TestClient
from sqlalchemy import func, select


class FakeCodex:
    def __init__(self) -> None:
        self.capability_calls = 0
        self.run_calls: list[str] = []
        self.status_calls: list[str] = []
        self.event_calls: list[tuple[str, int]] = []
        self.cancel_calls: list[str] = []
        self.run_requests: list[dict[str, Any]] = []
        self.runs: dict[str, dict[str, Any]] = {}
        self.digest = "a" * 64

    def capabilities(self) -> dict[str, Any]:
        self.capability_calls += 1
        return {"schema_version": 1, "codex": {"available": True, "routes": []}}

    def run(self, *, run_id: str, **_kwargs: Any) -> dict[str, Any]:
        self.run_calls.append(run_id)
        self.run_requests.append(
            {
                "run_id": run_id,
                "manifest": _kwargs["manifest"],
                "sections": _kwargs["sections"],
            }
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
                "model": "gpt-5.6-terra",
                "effort": "medium",
            },
        }

    def status(self, run_id: str) -> dict[str, Any]:
        self.status_calls.append(run_id)
        record = self.runs[run_id]
        return {
            "schema_version": 1,
            "run_id": run_id,
            "input_digest": self.digest,
            "model": "gpt-5.6-terra",
            "effort": "medium",
            "status": record["status"],
            "final_output": record["content"],
            "error_code": None,
        }

    def events(self, run_id: str, *, after: int = 0) -> dict[str, Any]:
        self.event_calls.append((run_id, after))
        events = [
            {"sequence": 1, "type": "queued", "created_at": "2026-09-03T00:00:00+00:00"},
            {"sequence": 2, "type": "running", "created_at": "2026-09-03T00:00:01+00:00"},
        ]
        return {"schema_version": 1, "run_id": run_id, "events": [item for item in events if item["sequence"] > after]}

    def cancel(self, run_id: str) -> dict[str, Any]:
        self.cancel_calls.append(run_id)
        self.runs[run_id]["status"] = "cancelled"
        return {
            "schema_version": 1,
            "run_id": run_id,
            "input_digest": self.digest,
            "model": "gpt-5.6-terra",
            "effort": "medium",
            "status": "cancelled",
            "final_output": "",
            "error_code": "AGENT_CANCELLED",
        }


class FakeRunner:
    def __init__(self) -> None:
        self.capability_calls = 0

    def capabilities(self) -> dict[str, Any]:
        self.capability_calls += 1
        return {"schema_version": 1, "runner": {"available": True}}


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
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        yield client


@pytest.fixture()
def codex_client(tmp_path: Path) -> tuple[TestClient, FakeCodex]:
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
    fake = FakeCodex()
    app.state.codex_client = fake
    with TestClient(app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        yield client, fake


def test_assistant_status_events_cancel_and_shared_idempotency(codex_client: tuple[TestClient, FakeCodex]) -> None:
    client, fake = codex_client
    conversation = client.post("/api/v1/conversations", json={"title": "Tutor"}).json()
    payload = {"conversation_id": conversation["id"], "message": "Explain the loop"}

    first = client.post("/api/v1/assistant/runs", json=payload, headers={"Idempotency-Key": "assistant-1"})
    second = client.post("/api/v1/assistant/runs", json=payload, headers={"Idempotency-Key": "assistant-1"})
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    run_id = first.json()["id"]
    assert fake.run_calls == [run_id]

    changed = client.post(
        "/api/v1/assistant/runs",
        json={**payload, "message": "Explain a different loop"},
        headers={"Idempotency-Key": "assistant-1"},
    )
    assert changed.status_code == 409
    assert changed.json()["code"] == "IDEMPOTENCY_CONFLICT"

    status = client.get(f"/api/v1/assistant/runs/{run_id}")
    assert status.status_code == 200, status.text
    assert status.json()["status"] == "queued"
    assert status.json()["input_digest"] == fake.digest
    assert fake.status_calls == [run_id]

    events = client.get(f"/api/v1/assistant/runs/{run_id}/events")
    assert events.status_code == 200
    assert '"event_type":"gateway_event"' in events.text
    assert '"status":"queued"' in events.text
    assert "retry: 1000" in events.text
    assert "id: " in events.text
    assert "event: snapshot" in events.text
    assert "event: terminal" not in events.text
    assert fake.event_calls == [(run_id, 0)]

    resumed = client.get(
        f"/api/v1/assistant/runs/{run_id}/events",
        headers={"Last-Event-ID": "999"},
    )
    assert resumed.status_code == 200
    assert '"event_type"' not in resumed.text
    assert "event: snapshot" in resumed.text

    invalid_cursor = client.get(
        f"/api/v1/assistant/runs/{run_id}/events",
        headers={"Last-Event-ID": "not-a-sequence"},
    )
    assert invalid_cursor.status_code == 400
    assert invalid_cursor.json()["code"] == "EVENT_CURSOR_INVALID"

    event_count = client.app.state.session_factory()
    try:
        before = event_count.scalar(select(func.count(AgentEvent.id)).where(AgentEvent.agent_run_id == run_id))
    finally:
        event_count.close()
    repeated_status = client.get(f"/api/v1/assistant/runs/{run_id}")
    assert repeated_status.status_code == 200
    event_count = client.app.state.session_factory()
    try:
        after = event_count.scalar(select(func.count(AgentEvent.id)).where(AgentEvent.agent_run_id == run_id))
        assert after == before
        payloads = list(event_count.scalars(select(AgentEvent).where(AgentEvent.agent_run_id == run_id)))
        assert any(item.payload.get("input_digest") == fake.digest for item in payloads)
    finally:
        event_count.close()

    fake.runs[run_id]["status"] = "succeeded"
    fake.runs[run_id]["content"] = "Completed"
    terminal_events = client.get(f"/api/v1/assistant/runs/{run_id}/events")
    assert terminal_events.status_code == 200
    assert "event: terminal" in terminal_events.text
    assert '"status":"succeeded"' in terminal_events.text

    cancel_first = client.post(
        "/api/v1/assistant/runs",
        json={"conversation_id": conversation["id"], "message": "Cancel me"},
        headers={"Idempotency-Key": "assistant-cancel"},
    )
    cancel_id = cancel_first.json()["id"]
    cancelled = client.post(f"/api/v1/assistant/runs/{cancel_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert fake.cancel_calls == [cancel_id]

    db = client.app.state.session_factory()
    try:
        assert db.scalar(select(func.count(Message.id)).where(Message.owner_id == "owner")) == 2
        assert db.scalar(select(func.count(AgentRun.id)).where(AgentRun.owner_id == "owner")) == 2
        assert db.scalar(
            select(func.count(LearningEvent.sequence)).where(
                LearningEvent.owner_id == "owner", LearningEvent.aggregate_type == "agent_run"
            )
        ) >= 3
    finally:
        db.close()


def test_assistant_preview_is_retrievable_and_shares_message_idempotency(
    codex_client: tuple[TestClient, FakeCodex],
) -> None:
    client, fake = codex_client
    conversation = client.post("/api/v1/conversations", json={"title": "Preview"}).json()
    payload = {"conversation_id": conversation["id"], "message": "Show the next hint"}
    first = client.post("/api/v1/assistant/context-preview", json=payload, headers={"Idempotency-Key": "shared-1"})
    second = client.post("/api/v1/assistant/context-preview", json=payload, headers={"Idempotency-Key": "shared-1"})
    assert first.status_code == second.status_code == 200
    assert first.json()["context_pack_id"] == second.json()["context_pack_id"]
    assert first.json()["run_id"] == second.json()["run_id"]
    assert first.json()["sections"] == second.json()["sections"]

    run = client.post("/api/v1/assistant/runs", json=payload, headers={"Idempotency-Key": "shared-1"})
    assert run.status_code == 201
    run_view = run.json()
    assert run_view["id"] == first.json()["run_id"]
    assert run_view["context_pack_id"] == first.json()["context_pack_id"]
    assert fake.run_calls == [run_view["id"]]
    assert fake.run_requests == [
        {
            "run_id": first.json()["run_id"],
            "manifest": first.json()["manifest"],
            "sections": first.json()["sections"],
        }
    ]

    repeated_run = client.post("/api/v1/assistant/runs", json=payload, headers={"Idempotency-Key": "shared-1"})
    assert repeated_run.status_code == 201
    assert repeated_run.json()["id"] == run_view["id"]
    assert fake.run_calls == [run_view["id"]]

    db = client.app.state.session_factory()
    try:
        assert db.scalar(select(func.count(Message.id)).where(Message.owner_id == "owner")) == 1
        assert db.scalar(select(func.count(AgentRun.id)).where(AgentRun.owner_id == "owner")) == 1
        assert db.scalar(select(func.count(ContextPack.id)).where(ContextPack.owner_id == "owner")) == 1
        assert db.scalar(select(func.count(AgentEvent.id)).where(AgentEvent.agent_run_id == run_view["id"])) == 1
    finally:
        db.close()

    # The idempotent message remains discoverable even after more than the old
    # 500-message window has been filled with newer user messages.
    db = client.app.state.session_factory()
    try:
        with db.begin():
            db.add_all(
                Message(
                    owner_id="owner",
                    conversation_id=conversation["id"],
                    role="user",
                    content=f"newer message {index}",
                    content_hash="b" * 64,
                    metadata_json={},
                    created_at=utcnow(),
                )
                for index in range(501)
            )
    finally:
        db.close()
    old_preview = client.post(
        "/api/v1/assistant/context-preview",
        json=payload,
        headers={"Idempotency-Key": "shared-1"},
    )
    assert old_preview.status_code == 200
    assert old_preview.json()["context_pack_id"] == first.json()["context_pack_id"]


def test_cancel_during_sealed_exam_returns_no_cached_assistant_content(
    codex_client: tuple[TestClient, FakeCodex],
) -> None:
    client, _fake = codex_client
    conversation = client.post("/api/v1/conversations", json={"title": "Sealed cancellation"}).json()
    run = client.post(
        "/api/v1/assistant/runs",
        json={"conversation_id": conversation["id"], "message": "Prepare a hint"},
        headers={"Idempotency-Key": "sealed-cancel-run"},
    )
    assert run.status_code == 201, run.text
    imported = client.post(
        "/api/v1/source/import-fixture",
        json={
            "source_url": "https://kompege.ru/task?id=sealed-cancel-task",
            "source_id": "sealed-cancel-task",
            "line_number": 1,
            "statement": "Sealed cancellation task",
            "answer": "42",
            "rights_status": "private_authorized",
        },
        headers={"Idempotency-Key": "sealed-cancel-task"},
    )
    assert imported.status_code == 201, imported.text
    blueprint = client.post(
        "/api/v1/exam-blueprints", json={"name": "Sealed cancellation exam", "required_lines": {"1": 1}}
    )
    assert blueprint.status_code == 201, blueprint.text
    exam = client.post("/api/v1/exams", json={"blueprint_id": blueprint.json()["id"], "seed": 29})
    assert exam.status_code == 201, exam.text

    cancelled = client.post(f"/api/v1/assistant/runs/{run.json()['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert set(cancelled.json()) == {"id", "status"}
    assert cancelled.json()["status"] == "cancelled"


def test_parallel_preview_and_run_have_one_server_side_reservation(tmp_path: Path) -> None:
    database_path = tmp_path / "parallel-assistant.sqlite"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_path.as_posix()}",
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
    fake = FakeCodex()
    app.state.codex_client = fake

    with TestClient(app) as client:
        auth = {"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)}
        conversation = client.post("/api/v1/conversations", json={"title": "Parallel"}, headers=auth).json()
        payload = {"conversation_id": conversation["id"], "message": "One concurrent request"}
        barrier = threading.Barrier(2)

        def request(path: str) -> Any:
            barrier.wait(timeout=10)
            headers = {**auth, "Idempotency-Key": "parallel-1"}
            return client.post(path, json=payload, headers=headers)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(request, "/api/v1/assistant/context-preview"),
                executor.submit(request, "/api/v1/assistant/runs"),
            ]
            responses = [future.result(timeout=30) for future in futures]

        assert sorted(response.status_code for response in responses) == [200, 201]
        preview = next(response.json() for response in responses if response.status_code == 200)
        run = next(response.json() for response in responses if response.status_code == 201)
        assert run["id"] == preview["run_id"]
        assert run["context_pack_id"] == preview["context_pack_id"]
        assert fake.run_calls == [run["id"]]
        assert fake.run_requests == [
            {"run_id": preview["run_id"], "manifest": preview["manifest"], "sections": preview["sections"]}
        ]

        db = app.state.session_factory()
        try:
            assert db.scalar(select(func.count(Message.id)).where(Message.owner_id == "owner")) == 1
            assert db.scalar(select(func.count(ContextPack.id)).where(ContextPack.owner_id == "owner")) == 1
            assert db.scalar(select(func.count(AgentRun.id)).where(AgentRun.owner_id == "owner")) == 1
            assert db.scalar(
                select(func.count(IdempotencyReservation.id)).where(IdempotencyReservation.owner_id == "owner")
            ) == 1
        finally:
            db.close()

        changed = client.post(
            "/api/v1/assistant/runs",
            json={**payload, "message": "Different request"},
            headers={**auth, "Idempotency-Key": "parallel-1"},
        )
        assert changed.status_code == 409
        assert changed.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_solution_comparison_reuses_assistant_security_and_idempotency(
    codex_client: tuple[TestClient, FakeCodex],
) -> None:
    client, fake = codex_client
    conversation = client.post("/api/v1/conversations", json={"title": "Compare"}).json()
    payload = {
        "conversation_id": conversation["id"],
        "message": "Compare my solution with the reference approach",
        "model_selection": "gpt-5.6-terra",
        "effort": "medium",
    }

    first = client.post(
        "/api/v1/solution-comparisons",
        json=payload,
        headers={"Idempotency-Key": "comparison-1"},
    )
    repeated = client.post(
        "/api/v1/solution-comparisons",
        json=payload,
        headers={"Idempotency-Key": "comparison-1"},
    )

    assert first.status_code == repeated.status_code == 201
    assert first.json()["id"] == repeated.json()["id"]
    assert fake.run_calls == [first.json()["id"]]
    assert fake.run_requests[0]["manifest"]["tutor_mode"] == "compare"


def test_capabilities_probes_attached_services_per_request_without_startup_call(tmp_path: Path) -> None:
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        asset_root=tmp_path / "assets",
        owner_session_secret="test-owner-secret",
        source_mode="fixture",
        source_permission_status="private_authorized",
        codex_enabled=True,
        codex_attested=True,
        codex_base_url="http://127.0.0.1:8787",
        runner_mode="isolated",
        runner_attested=True,
        runner_base_url="http://127.0.0.1:9797",
        testing=True,
    )
    app = create_app(settings)
    fake_codex = FakeCodex()
    fake_runner = FakeRunner()
    app.state.codex_client = fake_codex
    app.state.runner_client = fake_runner
    assert fake_runner.capability_calls == 0
    assert fake_codex.capability_calls == 0
    with TestClient(app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        assert fake_runner.capability_calls == 0
        response = client.get("/api/v1/capabilities")
        assert response.status_code == 200
        assert response.json()["runner"]["available"] is True
        assert response.json()["codex"]["available"] is True
        assert fake_runner.capability_calls == 1
        assert fake_codex.capability_calls == 1
        assert len(fake_codex.status_calls) == 0
        assert client.get("/api/v1/capabilities").status_code == 200
        assert fake_runner.capability_calls == 2
        assert fake_codex.capability_calls == 2


@pytest.mark.parametrize(
    "advertisement",
    [None, {}, {"available": "true"}, {"available": None}, {"available": False}],
)
def test_capability_probe_requires_explicit_service_availability(
    tmp_path: Path, advertisement: dict[str, Any] | None
) -> None:
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

    class MalformedCodex:
        def capabilities(self) -> dict[str, Any]:
            response: dict[str, Any] = {"schema_version": 1}
            if advertisement is not None:
                response["codex"] = advertisement
            return response

    app.state.codex_client = MalformedCodex()
    with TestClient(app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        response = client.get("/api/v1/capabilities")
    assert response.status_code == 200
    assert response.json()["codex"]["available"] is False


def test_capability_probe_does_not_expose_adapter_exception(tmp_path: Path) -> None:
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

    class ExplodingCodex:
        def capabilities(self) -> dict[str, Any]:
            raise RuntimeError("internal-url=http://127.0.0.1:8787 bearer-secret=hidden")

    app.state.codex_client = ExplodingCodex()
    with TestClient(app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        response = client.get("/api/v1/capabilities")
    assert response.status_code == 200
    body = response.json()["codex"]
    assert body["available"] is False
    assert body["reason"] == "codex capability probe failed"
    assert "hidden" not in response.text


def test_source_capabilities_reflect_task_and_variant_paths_independently(tmp_path: Path) -> None:
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        asset_root=tmp_path / "assets",
        owner_session_secret="test-owner-secret",
        source_mode="sync",
        source_network_enabled=True,
        source_permission_status="private_authorized",
        source_variant_api_url="https://kompege.ru/api/v1/variant/random",
        testing=True,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        response = client.get("/api/v1/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert body["source_sync"]["available"] is True
    assert body["source_sync"]["task_import"]["available"] is False
    assert body["source_sync"]["variant_sync"]["available"] is True
    assert body["source_variant_sync"]["available"] is True
    assert "variant/random" not in response.text


def test_solve_error_review_and_analytics_contracts(owner_client: TestClient) -> None:
    imported = owner_client.post(
        "/api/v1/source/import-fixture",
        json={
            "source_url": "https://kompege.ru/task?id=round2",
            "source_id": "round2",
            "line_number": 24,
            "statement": "Process the file.",
            "rights_status": "private_authorized",
        },
        headers={"Idempotency-Key": "round2-source"},
    ).json()
    solve = owner_client.post(
        "/api/v1/solve-sessions", json={"task_revision_id": imported["task"]["revision"]["id"]}
    ).json()
    solve_view = owner_client.get(f"/api/v1/solve-sessions/{solve['id']}")
    assert solve_view.status_code == 200
    assert solve_view.json()["task"]["id"] == imported["task"]["id"]
    assert solve_view.json()["task"]["revision"]["id"] == imported["task"]["revision"]["id"]
    assert solve_view.json()["sealed_exam"] is False

    attempt = owner_client.post(
        f"/api/v1/solve-sessions/{solve['id']}/attempts",
        json={"status": "failed", "error_type": "file_boundary"},
        headers={"Idempotency-Key": "round2-attempt"},
    ).json()
    error = owner_client.post(
        "/api/v1/errors",
        json={"attempt_id": attempt["id"], "error_type": "file_boundary", "status": "confirmed"},
    )
    assert error.status_code == 201
    assert error.json()["error"]["task_line"] == 24
    assert error.json()["error"]["reason"]
    assert error.json()["error"]["policy_version"] == "error-capture-v1"
    assert error.json()["error"]["evidence"]
    assert error.json()["review"]["task_line"] == 24
    assert error.json()["review"]["reason"]
    assert error.json()["review"]["policy_version"] == "mastery-v1"
    assert error.json()["review"]["evidence_ids"] == [error.json()["error"]["id"]]

    metrics = owner_client.post("/api/v1/analytics/rebuild")
    assert metrics.status_code == 200
    values = metrics.json()["metrics"]
    assert values["solved_attempt_count"] == 0
    assert values["correct_count"] == 0
    assert values["independent_success_count"] == 0
    assert values["accuracy"] is None
    assert values["correct_rate"] is None
    assert values["independent_success_rate"] is None
    assert values["skill_coverage_count"] == 0
    assert values["skill_coverage_rate"] == 0.0
    assert "semantics" in values

    mastery = owner_client.get("/api/v1/mastery")
    assert mastery.status_code == 200
    rows_without_evidence = [item for item in mastery.json()["items"] if item["evidence_count"] == 0]
    assert rows_without_evidence
    assert all(item["hint_dependency"] is None for item in rows_without_evidence)


def test_analytics_reports_missing_denominators_as_null(
    owner_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("backend.app.learning.seed_taxonomy", lambda _session: None)
    db = owner_client.app.state.session_factory()
    try:
        with db.begin():
            for skill in db.scalars(select(Skill)):
                skill.active = False
    finally:
        db.close()

    response = owner_client.post("/api/v1/analytics/rebuild")
    assert response.status_code == 200
    values = response.json()["metrics"]
    assert values["attempt_count"] == 0
    assert values["active_skill_count"] == 0
    assert values["accuracy"] is None
    assert values["correct_rate"] is None
    assert values["independent_success_rate"] is None
    assert values["skill_coverage_rate"] is None


def test_active_exam_rehydrates_owner_answers_and_clears_after_submit(owner_client: TestClient) -> None:
    assert owner_client.get("/api/v1/exams/active").json() == {"active": None}

    imported = owner_client.post(
        "/api/v1/source/import-fixture",
        json={
            "source_url": "https://kompege.ru/task?id=active-exam",
            "source_id": "active-exam",
            "line_number": 1,
            "statement": "Count the values.",
            "answer": "42",
            "explanation": "This correct solution must never be exposed by the active endpoint.",
            "rights_status": "private_authorized",
        },
        headers={"Idempotency-Key": "active-exam-source"},
    )
    assert imported.status_code == 201
    blueprint = owner_client.post(
        "/api/v1/exam-blueprints",
        json={"name": "Active recovery", "required_lines": {"1": 1}},
    )
    assert blueprint.status_code == 201
    started = owner_client.post(
        "/api/v1/exams",
        json={"blueprint_id": blueprint.json()["id"], "seed": 7},
    )
    assert started.status_code == 201
    exam = started.json()
    item_id = exam["items"][0]["id"]

    saved = owner_client.put(
        f"/api/v1/exams/{exam['id']}/answers/{item_id}",
        json={"content": "learner response"},
    )
    assert saved.status_code == 200

    active_response = owner_client.get("/api/v1/exams/active")
    assert active_response.status_code == 200
    active = active_response.json()["active"]
    assert active["id"] == exam["id"]
    assert active["sealed"] is True
    assert active["lifecycle"] == "active"
    assert active["answers"][item_id]["content"] == "learner response"
    assert set(active["items"][0]) == {
        "id",
        "position",
        "task_revision_id",
        "line_number",
        "difficulty",
        "source_id",
        "statement",
        "answer",
    }
    assert active["items"][0]["answer"] is None

    owner_client.headers["X-Owner-Session"] = issue_session_token("foreign", "test-owner-secret")
    foreign = owner_client.get("/api/v1/exams/active")
    assert foreign.status_code == 200
    assert foreign.json() == {"active": None}

    owner_client.headers["X-Owner-Session"] = issue_session_token("owner", "test-owner-secret")
    submitted = owner_client.post(f"/api/v1/exams/{exam['id']}/submit")
    assert submitted.status_code == 200
    assert owner_client.get("/api/v1/exams/active").json() == {"active": None}

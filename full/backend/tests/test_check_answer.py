from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from backend.app.auth import issue_session_token
from backend.app.config import Settings
from backend.app.exam_scoring import PRACTICE_SCORING_VERSION
from backend.app.learning import skill_id_for_line
from backend.app.main import create_app
from backend.app.models import AgentRun, Attempt, LearningEvent, MasteryEvidence
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
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        test_client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        yield test_client


def _auth(client: TestClient, owner_id: str) -> None:
    settings = client.app.state.settings
    client.headers["X-Owner-Session"] = issue_session_token(owner_id, settings.owner_session_secret)


def _import_task(client: TestClient, source_id: str, *, answer: str | None = "42") -> dict[str, Any]:
    response = client.post(
        "/api/v1/source/import-fixture",
        json={
            "source_url": f"https://kompege.ru/task?id={source_id}",
            "source_id": source_id,
            "line_number": 1,
            "statement": f"Synthetic check-answer task {source_id}.",
            "answer": answer,
            "rights_status": "private_authorized",
        },
        headers={"Idempotency-Key": f"fixture-{source_id}-{answer}"},
    )
    assert response.status_code == 201, response.text
    return response.json()["task"]


def _solve(client: TestClient, revision_id: str) -> dict[str, Any]:
    response = client.post("/api/v1/solve-sessions", json={"task_revision_id": revision_id})
    assert response.status_code == 201, response.text
    return response.json()


def _assert_error(response: Any, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["code"] == code


def test_check_answer_uses_authoritative_key_and_replays_idempotently(client: TestClient) -> None:
    task = _import_task(client, "check-correct")
    solve = _solve(client, task["revision"]["id"])
    endpoint = f"/api/v1/solve-sessions/{solve['id']}/check-answer"

    correct = client.post(endpoint, json={"answer": "42"}, headers={"Idempotency-Key": "check-correct-1"})
    assert correct.status_code == 200, correct.text
    result = correct.json()
    assert set(result) == {"attempt_id", "status", "correct", "reason"}
    assert result["attempt_id"]
    assert result["status"] == "correct"
    assert result["correct"] is True
    assert result["reason"] is None
    # UUIDs can contain arbitrary digits; only learner-facing values must omit the key.
    assert "42" not in str({key: value for key, value in result.items() if key != "attempt_id"})

    replay = client.post(endpoint, json={"answer": "42"}, headers={"Idempotency-Key": "check-correct-1"})
    assert replay.status_code == 200
    assert replay.json() == result
    conflict = client.post(endpoint, json={"answer": "41"}, headers={"Idempotency-Key": "check-correct-1"})
    _assert_error(conflict, 409, "IDEMPOTENCY_CONFLICT")

    session = client.app.state.session_factory()
    try:
        attempt = session.scalar(select(Attempt).where(Attempt.id == result["attempt_id"]))
        assert attempt is not None
        assert attempt.status == "solved"
        assert attempt.verified_correct is True
        assert attempt.verification_method == f"server:{PRACTICE_SCORING_VERSION}"
        assert session.scalar(select(func.count(Attempt.id))) == 1
        assert session.scalar(select(func.count(MasteryEvidence.id))) == 1
        checked = session.scalar(
            select(LearningEvent).where(
                LearningEvent.event_type == "practice_answer_checked",
                LearningEvent.aggregate_id == result["attempt_id"],
            )
        )
        assert checked is not None
        assert checked.payload["result"] == result
        assert checked.payload["request"]["answer"] == "42"
    finally:
        session.close()

    immutable = client.patch(
        f"/api/v1/solve-sessions/{solve['id']}/attempts/{result['attempt_id']}",
        json={"verified_correct": False, "status": "failed"},
    )
    _assert_error(immutable, 409, "ATTEMPT_IMMUTABLE")

    wrong = client.post(
        endpoint,
        json={"answer": "41"},
        headers={"Idempotency-Key": "check-wrong-1"},
    )
    assert wrong.status_code == 200, wrong.text
    assert wrong.json()["status"] == "incorrect"
    assert wrong.json()["correct"] is False
    assert wrong.json()["reason"] == "answer_mismatch"


def test_legacy_attempt_verdict_is_self_reported_and_server_method_is_forbidden(client: TestClient) -> None:
    task = _import_task(client, "legacy-verdict-boundary")
    solve = _solve(client, task["revision"]["id"])
    forged = client.post(
        f"/api/v1/solve-sessions/{solve['id']}/attempts",
        json={"status": "solved", "verified_correct": True, "verification_method": "server:forged"},
    )
    _assert_error(forged, 422, "VERIFICATION_METHOD_FORBIDDEN")

    recorded = client.post(
        f"/api/v1/solve-sessions/{solve['id']}/attempts",
        json={"status": "solved", "verified_correct": True, "verification_method": "owner_review"},
        headers={"Idempotency-Key": "legacy-self-reported"},
    )
    assert recorded.status_code == 201, recorded.text
    assert recorded.json()["status"] == "self_reported"
    assert recorded.json()["verified_correct"] is None
    assert recorded.json()["verification_method"] == "self_reported"

    patched = client.patch(
        f"/api/v1/solve-sessions/{solve['id']}/attempts/{recorded.json()['id']}",
        json={"status": "solved", "verified_correct": True, "verification_method": "owner_review"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["status"] == "self_reported"
    assert patched.json()["verified_correct"] is None
    assert patched.json()["verification_method"] == "self_reported"

    session = client.app.state.session_factory()
    try:
        assert session.scalar(select(func.count(MasteryEvidence.id))) == 0
    finally:
        session.close()
    progress = client.get("/api/v1/task-progress", params=[("revision_ids", task["revision"]["id"])])
    assert progress.status_code == 200, progress.text
    assert progress.json()[0]["status"] == "neutral"


def test_exact_server_label_without_answer_check_provenance_stays_historical(client: TestClient) -> None:
    task = _import_task(client, "historical-forged-server-label")
    solve = _solve(client, task["revision"]["id"])
    session = client.app.state.session_factory()
    try:
        with session.begin():
            forged = Attempt(
                owner_id="owner",
                solve_session_id=solve["id"],
                status="solved",
                user_answer="42",
                verified_correct=True,
                verification_method=f"server:{PRACTICE_SCORING_VERSION}",
            )
            session.add(forged)
            session.flush()
            evidence = MasteryEvidence(
                owner_id="owner",
                skill_id=skill_id_for_line(1),
                attempt_id=forged.id,
                evidence_type="attempt",
                value_json={"correct": True, "independent": True, "hint_count": 0, "status": "solved"},
            )
            session.add(evidence)
    finally:
        session.close()

    progress = client.get("/api/v1/task-progress", params=[("revision_ids", task["revision"]["id"])])
    assert progress.status_code == 200
    assert progress.json()[0]["status"] == "neutral"
    metrics = client.post("/api/v1/analytics/rebuild")
    assert metrics.status_code == 200
    assert metrics.json()["metrics"]["attempt_count"] == 0
    mastery = client.get("/api/v1/mastery")
    line_one = next(item for item in mastery.json()["items"] if item["line_number"] == 1)
    assert line_one["evidence_count"] == 0

    session = client.app.state.session_factory()
    try:
        assert session.scalar(select(func.count(Attempt.id))) == 1
        assert session.scalar(select(func.count(MasteryEvidence.id))) == 1
    finally:
        session.close()


def test_check_answer_without_authoritative_key_is_unavailable(client: TestClient) -> None:
    task = _import_task(client, "check-unsupported", answer=None)
    solve = _solve(client, task["revision"]["id"])
    response = client.post(
        f"/api/v1/solve-sessions/{solve['id']}/check-answer",
        json={"answer": "42"},
        headers={"Idempotency-Key": "check-unsupported-1"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "attempt_id": None,
        "status": "unavailable",
        "correct": None,
        "reason": "answer_key_unavailable",
    }
    attempts = client.get(f"/api/v1/solve-sessions/{solve['id']}/attempts")
    assert attempts.status_code == 200
    assert attempts.json()["items"] == []


def test_wrong_check_creates_stable_error_history_and_correct_keeps_it(client: TestClient) -> None:
    task = _import_task(client, "check-error-history")
    solve = _solve(client, task["revision"]["id"])
    endpoint = f"/api/v1/solve-sessions/{solve['id']}/check-answer"

    wrong = client.post(endpoint, json={"answer": "41"}, headers={"Idempotency-Key": "error-history-wrong"})
    assert wrong.status_code == 200, wrong.text
    assert wrong.json()["status"] == "incorrect"

    errors = client.get("/api/v1/errors")
    assert errors.status_code == 200
    answer_check_errors = [
        item for item in errors.json()["items"] if item["attempt_id"] == wrong.json()["attempt_id"]
    ]
    assert len(answer_check_errors) == 1
    assert answer_check_errors[0]["error_type"] == "Неверный ответ"
    assert answer_check_errors[0]["source"] == "answer-check"
    assert answer_check_errors[0]["description"] == "Ответ не совпал с серверной проверкой."
    assert answer_check_errors[0]["task_revision_id"] == task["revision"]["id"]
    assert answer_check_errors[0]["solve_session_id"] == solve["id"]

    correct = client.post(endpoint, json={"answer": "42"}, headers={"Idempotency-Key": "error-history-correct"})
    assert correct.status_code == 200, correct.text
    assert correct.json()["status"] == "correct"
    after = client.get("/api/v1/errors")
    assert after.status_code == 200
    after_items = [item for item in after.json()["items"] if item["attempt_id"] == wrong.json()["attempt_id"]]
    assert after_items == answer_check_errors


def test_completed_assistant_run_marks_practice_attempt_assisted(client: TestClient) -> None:
    task = _import_task(client, "check-assisted")
    conversation = client.post("/api/v1/conversations", json={"title": "TEST_ONLY assistance"})
    assert conversation.status_code == 201, conversation.text
    conversation_id = conversation.json()["id"]
    solve = client.post(
        "/api/v1/solve-sessions",
        json={"task_revision_id": task["revision"]["id"], "conversation_id": conversation_id},
    )
    assert solve.status_code == 201, solve.text
    solve_payload = solve.json()

    session = client.app.state.session_factory()
    try:
        with session.begin():
            session.add(
                AgentRun(
                    owner_id="owner",
                    conversation_id=conversation_id,
                    solve_session_id=solve_payload["id"],
                    requested_model="TEST_ONLY",
                    requested_effort="minimal",
                    tutor_mode="hint",
                    policy_version="test",
                    status="succeeded",
                    content="A persisted assistant hint.",
                )
            )
    finally:
        session.close()

    checked = client.post(
        f"/api/v1/solve-sessions/{solve_payload['id']}/check-answer",
        json={"answer": "42"},
        headers={"Idempotency-Key": "check-assisted"},
    )
    assert checked.status_code == 200, checked.text
    assert checked.json()["status"] == "correct"
    attempts = client.get(f"/api/v1/solve-sessions/{solve_payload['id']}/attempts")
    assert attempts.status_code == 200
    attempt = attempts.json()["items"][0]
    assert attempt["independent"] is False
    assert attempt["hint_count"] == 1


def test_check_answer_is_owner_and_revision_scoped(client: TestClient) -> None:
    first = _import_task(client, "check-revision", answer="42")
    first_solve = _solve(client, first["revision"]["id"])
    second = client.post(
        "/api/v1/source/import-fixture",
        json={
            "source_url": "https://kompege.ru/task?id=check-revision",
            "source_id": "check-revision",
            "line_number": 1,
            "statement": "A later authoritative revision.",
            "answer": "43",
            "rights_status": "private_authorized",
        },
        headers={"Idempotency-Key": "fixture-check-revision-v2"},
    )
    assert second.status_code == 201, second.text
    assert second.json()["task"]["revision"]["id"] != first["revision"]["id"]

    _auth(client, "foreign-owner")
    foreign = client.post(
        f"/api/v1/solve-sessions/{first_solve['id']}/check-answer",
        json={"answer": "42"},
        headers={"Idempotency-Key": "foreign-check"},
    )
    _assert_error(foreign, 404, "NOT_FOUND")

    _auth(client, "owner")
    first_result = client.post(
        f"/api/v1/solve-sessions/{first_solve['id']}/check-answer",
        json={"answer": "42"},
        headers={"Idempotency-Key": "owner-revision-check"},
    )
    assert first_result.status_code == 200, first_result.text
    assert first_result.json()["status"] == "correct"


def test_check_answer_is_blocked_during_owner_sealed_exam(client: TestClient) -> None:
    task = _import_task(client, "check-sealed")
    solve = _solve(client, task["revision"]["id"])
    blueprint = client.post(
        "/api/v1/exam-blueprints",
        json={"name": "Check-answer sealed test", "required_lines": {"1": 1}},
    )
    assert blueprint.status_code == 201, blueprint.text
    exam = client.post("/api/v1/exams", json={"blueprint_id": blueprint.json()["id"], "seed": 11})
    assert exam.status_code == 201, exam.text

    response = client.post(
        f"/api/v1/solve-sessions/{solve['id']}/check-answer",
        json={"answer": "42"},
        headers={"Idempotency-Key": "check-sealed-1"},
    )
    _assert_error(response, 423, "EXAM_SEALED")
    attempts = client.get(f"/api/v1/solve-sessions/{solve['id']}/attempts")
    assert attempts.status_code == 200
    assert attempts.json()["items"] == []

"""Actual private API and SQLite; synthetic native callbacks, never a model."""
from __future__ import annotations

import base64
import json

from sqlalchemy import select
from test_local_codex import Native

from local_codex import LocalCodexClient


def decoded(response):
    return json.loads(base64.b64decode(response["bodyBase64"]))


class CheckingNative(Native):
    checking = True
    unavailable = False

    def __call__(self, operation, payload):
        result = super().__call__(operation, payload)
        if operation == "codex_status":
            if self.unavailable:
                result.update(state="unavailable", error={
                    "code": "codex_unsafe_configuration", "message": "SYNTHETIC_DETAIL_DO_NOT_FORWARD",
                })
            elif self.checking:
                result["state"] = "checking"
        return result


def setup(seed_factory, offline_factory):
    bundle, _ = seed_factory()
    native = CheckingNative()
    application = offline_factory(bundle, gateway=native)
    application.ai_provider.codex_client = LocalCodexClient(
        native, status_wait_seconds=0.005, status_poll_seconds=0.001,
    )
    conversation = decoded(application.request("POST", "/api/v1/conversations", '{"title":"Synthetic retry"}'))
    body = json.dumps({"conversation_id": conversation["id"], "message": "Synthetic tutor question"})
    return application, native, body


def test_checking_retry_reuses_original_message_and_context_without_failed_run(seed_factory, offline_factory):
    from app.models import AgentRun, ContextPack, IdempotencyReservation, Message

    application, native, body = setup(seed_factory, offline_factory)
    headers = {"Idempotency-Key": "synthetic-checking-retry"}
    first = application.request("POST", "/api/v1/assistant/runs", body, headers)
    assert first["status"] == 503
    error = decoded(first)
    assert error["details"]["retryable"] is True
    assert error["details"]["native_code"] == "codex_unavailable"
    assert "status" not in error["details"]
    assert native.starts == []
    with application.app.state.session_factory() as session:
        assert list(session.scalars(select(AgentRun))) == []
        assert len(list(session.scalars(select(Message).where(Message.role == "user")))) == 1
        assert len(list(session.scalars(select(ContextPack)))) == 1
        reservation = session.scalar(select(IdempotencyReservation))
        assert reservation.agent_run_id is None
        context_id = reservation.context_pack_id

    native.checking = False
    second = application.request("POST", "/api/v1/assistant/runs", body, headers)
    assert second["status"] == 201, decoded(second)
    run = decoded(second)
    assert len(native.starts) == 1
    with application.app.state.session_factory() as session:
        assert len(list(session.scalars(select(AgentRun)))) == 1
        assert len(list(session.scalars(select(Message).where(Message.role == "user")))) == 1
        assert len(list(session.scalars(select(ContextPack)))) == 1
        reservation = session.scalar(select(IdempotencyReservation))
        assert reservation.agent_run_id == run["id"]
        assert reservation.context_pack_id == context_id
    native.state, native.text = "completed", "Synthetic answer"
    assert application.request("GET", f'/api/v1/assistant/runs/{run["id"]}')["status"] == 200


def test_checking_without_idempotency_cleans_only_unstarted_run_and_context(seed_factory, offline_factory):
    from app.models import AgentRun, ContextPack, Message

    application, native, body = setup(seed_factory, offline_factory)
    response = application.request("POST", "/api/v1/assistant/runs", body)
    assert response["status"] == 503
    assert decoded(response)["details"]["retryable"] is True
    assert native.starts == []
    with application.app.state.session_factory() as session:
        assert list(session.scalars(select(AgentRun))) == []
        assert list(session.scalars(select(ContextPack))) == []
        assert len(list(session.scalars(select(Message).where(Message.role == "user")))) == 1


def test_known_preaccept_failure_has_safe_reason_and_terminal_retry_signal(seed_factory, offline_factory):
    application, native, body = setup(seed_factory, offline_factory)
    native.unavailable = True
    response = application.request("POST", "/api/v1/assistant/runs", body, {"Idempotency-Key": "synthetic-unavailable"})
    assert response["status"] == 503
    error = decoded(response)
    assert error["details"]["native_code"] == "codex_unsafe_configuration"
    assert error["details"]["status"] == "failed"
    assert "SYNTHETIC_DETAIL_DO_NOT_FORWARD" not in json.dumps(error)
    assert native.starts == []

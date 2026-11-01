"""Synthetic API regressions for safe offline provider metadata."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from backend.app.api import _provider_route_view
from backend.app.auth import issue_session_token
from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.models import AgentRun
from fastapi.testclient import TestClient
from sqlalchemy import select

SAFE_PROVIDER = {
    "id": "ollama-local",
    "kind": "ollama",
    "model": "llama3",
    "effort": "default",
}


class SyntheticProviderGateway:
    def __init__(self) -> None:
        self.digest = "a" * 64
        self.provider_info: dict[str, str] | None = dict(SAFE_PROVIDER)
        self.status_digest: str | None = None
        self.status_model: str | None = None
        self.status_provider_info: dict[str, str] | None | object = ...
        self._model = ""
        self._effort = ""
        self.run_id = ""

    def capabilities(self) -> dict[str, Any]:
        return {"schema_version": 1, "codex": {"available": True, "routes": []}}

    def run(self, *, run_id: str, model: str, effort: str, **_kwargs: Any) -> dict[str, Any]:
        self.run_id = run_id
        self._model = model
        self._effort = effort
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
                "provider_info": self.provider_info,
            },
        }

    def status(self, run_id: str) -> dict[str, Any]:
        provider_info = self.provider_info if self.status_provider_info is ... else self.status_provider_info
        result: dict[str, Any] = {
            "schema_version": 1,
            "run_id": run_id,
            "input_digest": self.status_digest or self.digest,
            "model": self.status_model or self._model,
            "effort": self._effort,
            "status": "queued",
            "final_output": "",
            "error_code": None,
        }
        if provider_info is not None:
            result["provider_info"] = provider_info
        return result


@pytest.fixture()
def provider_client(tmp_path: Path) -> tuple[TestClient, SyntheticProviderGateway]:
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
    gateway = SyntheticProviderGateway()
    app.state.codex_client = gateway
    with TestClient(app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        yield client, gateway


def _create_run(client: TestClient) -> tuple[str, dict[str, Any]]:
    conversation = client.post("/api/v1/conversations", json={"title": "Synthetic metadata"}).json()
    response = client.post(
        "/api/v1/assistant/runs",
        json={"conversation_id": conversation["id"], "message": "Explain this synthetic route"},
    )
    assert response.status_code == 201, response.text
    value = response.json()
    return value["id"], value


def test_valid_provider_metadata_round_trips_without_replacing_original_route(provider_client) -> None:
    client, _gateway = provider_client
    run_id, created = _create_run(client)

    assert created["provider_info"] == SAFE_PROVIDER
    assert set(created["provider_info"]) == {"id", "kind", "model", "effort"}
    with client.app.state.session_factory() as session:
        stored = session.scalar(select(AgentRun).where(AgentRun.id == run_id))
        assert stored is not None
        stored_reason = stored.route_reason
        stored_model = stored.effective_model
        original_reason, stored_info = _provider_route_view(stored_reason)
    assert stored_info == SAFE_PROVIDER
    assert original_reason == created["route_reason"]
    assert stored_reason != created["route_reason"]
    assert created["input_digest"] == "a" * 64
    assert stored_model == created["effective_model"]


def test_omitted_provider_info_preserves_existing_metadata(provider_client) -> None:
    client, gateway = provider_client
    run_id, created = _create_run(client)
    gateway.status_provider_info = None

    response = client.get(f"/api/v1/assistant/runs/{run_id}")
    assert response.status_code == 200, response.text
    current = response.json()
    assert current["provider_info"] == SAFE_PROVIDER
    assert current["route_reason"] == created["route_reason"]


def test_changed_provider_identity_is_rejected_for_existing_run(provider_client) -> None:
    client, gateway = provider_client
    run_id, created = _create_run(client)
    gateway.status_provider_info = {**SAFE_PROVIDER, "model": "different-local-model"}

    response = client.get(f"/api/v1/assistant/runs/{run_id}")
    assert response.status_code == 200, response.text
    current = response.json()
    assert current["status"] == "unavailable"
    assert current["error_code"] == "AGENT_PROTOCOL_ERROR"
    assert current["provider_info"] == SAFE_PROVIDER
    assert current["route_reason"] == created["route_reason"]


@pytest.mark.parametrize("bad_field", ["digest", "model"])
def test_bad_gateway_status_cannot_change_existing_route_or_provider_info(provider_client, bad_field: str) -> None:
    client, gateway = provider_client
    run_id, created = _create_run(client)
    if bad_field == "digest":
        gateway.status_digest = "b" * 64
    else:
        gateway.status_model = "unexpected-gateway-model"

    response = client.get(f"/api/v1/assistant/runs/{run_id}")
    assert response.status_code == 200, response.text
    current = response.json()
    assert current["status"] == "unavailable"
    assert current["error_code"] == "AGENT_PROTOCOL_ERROR"
    assert current["provider_info"] == SAFE_PROVIDER
    assert current["route_reason"] == created["route_reason"]


def test_provider_metadata_requires_only_safe_identity_fields_and_no_secret_surface(provider_client) -> None:
    client, _gateway = provider_client
    _run_id, created = _create_run(client)

    info = created["provider_info"]
    assert info == SAFE_PROVIDER
    assert "api_key" not in info
    assert "endpoint" not in info
    assert all(isinstance(value, str) and value for value in info.values())

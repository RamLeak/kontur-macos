"""Damaged optional AI settings must not make the offline bank unavailable."""
from __future__ import annotations

import base64
import json

import pytest

from offline_ai_settings import LocalAISettingsError, LocalAISettingsStore


@pytest.mark.parametrize("selected", [[], {}, 123, "missing-provider"])
def test_invalid_selected_provider_disables_only_ai(seed_factory, offline_factory, tmp_path, selected):
    bundle, _ = seed_factory()
    profile = tmp_path / "profile"
    target = profile / "private-ai" / "settings.json"
    target.parent.mkdir(parents=True)
    original = json.dumps({"schemaVersion": 1, "selectedKind": selected, "profiles": []}).encode()
    target.write_bytes(original)
    with pytest.raises(LocalAISettingsError):
        LocalAISettingsStore(path=target)
    application = offline_factory(bundle, profile)
    assert application.request("GET", "/api/v1/tasks?limit=1")["status"] == 200
    settings = json.loads(base64.b64decode(application.request("GET", "/api/v1/local/ai")["bodyBase64"]))
    assert settings["selectedKind"] is None
    assert settings["profiles"] == []
    assert target.read_bytes() == original


@pytest.mark.parametrize("field", ["kind", "effort", "endpoint", "model", "opencodeProvider"])
def test_invalid_profile_scalars_are_classified_as_optional_config_error(tmp_path, field):
    target = tmp_path / "settings.json"
    record = {"kind": "ollama", "model": "synthetic-model", field: []}
    target.write_text(
        json.dumps({"schemaVersion": 1, "selectedKind": "ollama", "profiles": [record]}), encoding="utf-8"
    )
    with pytest.raises(LocalAISettingsError):
        LocalAISettingsStore(path=target)


def test_failed_http_worker_start_leaves_no_phantom_run(monkeypatch):
    import threading
    import time

    from test_offline_ai import make_context

    from app.codex_gateway import CodexUnavailable
    from offline_ai import HTTPResponse, OfflineAIClient, ProviderConfig
    from offline_ai_adapter import OfflineAIProviderAdapter

    settings = LocalAISettingsStore()
    settings.upsert(ProviderConfig("synthetic-ollama", "ollama", "synthetic-model", endpoint="http://127.0.0.1:11434"))
    calls = []

    def transport(request):
        calls.append(request)
        return HTTPResponse(200, {"content-type": "application/x-ndjson"}, (b'{"response":"ok","done":true}\n',))

    adapter = OfflineAIProviderAdapter(settings=settings, http_client=OfflineAIClient(transport))
    with monkeypatch.context() as patch:
        def fail_start(_thread):
            raise RuntimeError("synthetic thread allocation failure")

        patch.setattr(threading.Thread, "start", fail_start)
        manifest, sections = make_context("failed-start")
        with pytest.raises(CodexUnavailable):
            adapter.run(manifest=manifest, sections=sections, model="gpt-5.6-sol", effort="high", run_id="failed-start")
    assert calls == []
    adapter.quiesce(timeout=0.1)
    manifest, sections = make_context("next-start")
    adapter.run(manifest=manifest, sections=sections, model="gpt-5.6-sol", effort="high", run_id="next-start")
    deadline = time.monotonic() + 2
    while adapter.status("next-start")["status"] in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.001)
    assert adapter.status("next-start")["status"] == "succeeded"
    assert len(calls) == 1
    adapter.quiesce()


def test_opencode_model_provider_prefix_cannot_rebind_an_existing_key():
    from test_offline_ai import FakeProtector

    from offline_ai import ProviderConfig
    from offline_ai_secrets import SecretStore

    # In-memory protector/store; no key file, network or model is involved.
    class MemorySecrets(SecretStore):
        def __init__(self):
            self.values = {}
            self._protector = FakeProtector()

        def get(self, reference):
            return self.values[reference]

        def put(self, reference, secret):
            self.values[reference] = secret

        def delete(self, reference):
            self.values.pop(reference, None)

        def has(self, reference):
            return reference in self.values

    secrets = MemorySecrets()
    settings = LocalAISettingsStore(secret_store=secrets)
    old = ProviderConfig("opencode", "opencode_cli", "openai/synthetic-model", allow_remote=True)
    new = ProviderConfig("opencode", "opencode_cli", "anthropic/synthetic-model", allow_remote=True)
    settings.upsert(old, api_key="fake-openai-key", selected=True)
    with pytest.raises(LocalAISettingsError):
        settings.upsert(new, selected=True)
    chosen, key = settings.capture()
    assert chosen.model == old.model and key == "fake-openai-key"
    settings.upsert(new, clear_api_key=True, selected=True)
    chosen, key = settings.capture()
    assert chosen.model == new.model and key is None


def test_tls_control_socket_uses_raw_shutdown_handle_not_ssl_dup(monkeypatch):
    import offline_ai

    class TLS:
        def fileno(self):
            return 42

        def dup(self):
            raise NotImplementedError("SSLSocket cannot be duplicated")

    class Control:
        pass

    calls = []
    control = Control()
    with monkeypatch.context() as patch:
        patch.setattr(offline_ai.ssl, "SSLSocket", TLS)
        patch.setattr(offline_ai.socket, "dup", lambda descriptor: calls.append(descriptor) or 43)
        patch.setattr(offline_ai.socket, "socket", lambda *, fileno: control if fileno == 43 else None)
        assert offline_ai._duplicate_control_socket(TLS()) is control
    assert calls == [42]

    # A runtime which cannot duplicate a handle keeps the original TLS socket;
    # its explicit shutdown still controls cancellation and the wall watchdog.
    with monkeypatch.context() as patch:
        patch.setattr(offline_ai.ssl, "SSLSocket", TLS)

        def unavailable(_descriptor):
            raise OSError("synthetic duplicate failure")

        patch.setattr(offline_ai.socket, "dup", unavailable)
        assert offline_ai._duplicate_control_socket(TLS()) is None

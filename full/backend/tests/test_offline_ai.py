"""Synthetic contract tests for the selectable offline AI seam.

No CLI, model, network, existing profile, auth file, or environment secret is
read by these tests.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.codex_gateway import CodexUnavailable
from app.util import canonical_json
from offline_ai import (
    ALLOWED_ROUTES,
    TUTOR_SYSTEM_POLICY,
    AICancelled,
    AIConfigError,
    AIProtocolError,
    AITimeout,
    AIToolEvent,
    HTTPResponse,
    OfflineAIClient,
    ProviderConfig,
    ProviderRegistry,
    _request_digest,
    _wire_prompt,
    build_tutor_packet,
)
from offline_ai_adapter import OfflineAIProviderAdapter
from offline_ai_gateway import OPEN_CODE_GATEWAY_OPERATIONS, OpenCodeGatewayClient
from offline_ai_opencode import OpenCodeEventDecoder, build_opencode_request, decode_opencode_events
from offline_ai_secrets import SecretStore, SecretStoreError
from offline_ai_settings import LocalAISettingsError, LocalAISettingsStore, install_local_ai_routes


def make_context(run_id: str = "run-1") -> tuple[dict[str, Any], list[dict[str, str]]]:
    sections = [
        {"trust_label": "SYSTEM_POLICY", "source_id": "policy-v1", "content": "Only provide a learning hint."},
        {"trust_label": "USER_REQUEST", "source_id": "question-v1", "content": "Синтетический учебный вопрос."},
    ]
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "resolved_model": "gpt-5.6-sol",
        "resolved_effort": "high",
        "tutor_mode": "hint",
        "code_samples": [],
        "sections": [
            {
                "trust_label": section["trust_label"],
                "source_id": section["source_id"],
                "character_count": len(section["content"]),
                "sha256": hashlib.sha256(section["content"].encode("utf-8")).hexdigest(),
            }
            for section in sections
        ],
    }
    return manifest, sections


class SyntheticTransport:
    def __init__(self, chunks: list[bytes], *, content_type: str = "application/x-ndjson", status: int = 200) -> None:
        self.chunks = chunks
        self.content_type = content_type
        self.status = status
        self.calls: list[Any] = []

    def __call__(self, request: Any) -> HTTPResponse:
        self.calls.append(request)
        return HTTPResponse(self.status, {"content-type": self.content_type}, list(self.chunks))


def packet():
    manifest, sections = make_context()
    return build_tutor_packet(
        manifest=manifest,
        sections=sections,
        model="gpt-5.6-sol",
        effort="high",
        run_id="run-1",
    )


def test_packet_preserves_original_context_envelope_hashes_and_route() -> None:
    result = packet()
    parsed = json.loads(result.rendered_prompt)
    assert result.rendered_prompt == canonical_json(parsed)
    assert parsed["format"] == "ege-context-envelope-v1"
    assert parsed["trusted_metadata"]["section_count"] == 2
    expected_prompt = _wire_prompt(
        [
            {"trust_label": section["trust_label"], "source_id": section["source_id"], "content": section["content"]}
            for section in result.sections
        ]
    )
    assert result.rendered_prompt == expected_prompt
    assert result.model == "gpt-5.6-sol" and result.effort == "high"
    assert result.prompt_sha256 == hashlib.sha256(expected_prompt.encode("utf-8")).hexdigest()
    assert result.input_digest == _request_digest(
        run_id="run-1",
        manifest_hash=result.manifest_sha256,
        prompt_hash=result.prompt_sha256,
        model="gpt-5.6-sol",
        effort="high",
        tutor_mode="hint",
        wall_seconds=120,
        inactivity_seconds=30,
    )


def test_settings_validation_is_local_only_and_requires_explicit_remote_https() -> None:
    transport = SyntheticTransport([])
    client = OfflineAIClient(transport)
    local = ProviderConfig("ollama-1", "ollama", "llama3", endpoint="http://127.0.0.1:11434/")
    assert client.probe(local) == {
        "available": True,
        "configured": True,
        "network": False,
        "reason": "validated_without_network",
    }
    assert transport.calls == []
    with pytest.raises(AIConfigError):
        ProviderConfig("remote", "openai_compatible", "model", endpoint="http://example.test/v1")
    with pytest.raises(AIConfigError):
        ProviderConfig("remote", "openai_compatible", "model", endpoint="https://example.test/v1")
    remote = ProviderConfig(
        "remote-ok",
        "openai_compatible",
        "model",
        endpoint="https://example.test/v1",
        allow_remote=True,
        api_key_ref="remote-key",
    )
    assert client.probe(remote)["network"] is False


def test_http_send_is_text_only_bounded_and_keeps_original_route() -> None:
    transport = SyntheticTransport(
        [
            b'{"message":{"role":"assistant","content":"First "},"done":false}\n',
            b'{"message":{"role":"assistant","content":"hint."},"done":true}\n',
        ]
    )
    client = OfflineAIClient(transport)
    config = ProviderConfig("ollama-1", "ollama", "llama3", endpoint="http://localhost:11434")
    result = client.request(packet=packet(), provider=config)
    assert result.content == "First hint."
    assert result.model == "gpt-5.6-sol" and result.effort == "high"
    assert result.provider_model == "llama3"
    assert result.to_dict()["provider_info"] == {
        "id": "ollama-1",
        "kind": "ollama",
        "model": "llama3",
        "effort": "default",
    }
    request = transport.calls[0]
    assert request.url == "http://localhost:11434/api/chat"
    body = json.loads(request.body)
    assert body == {
        "messages": [
            {"content": TUTOR_SYSTEM_POLICY, "role": "system"},
            {"content": packet().rendered_prompt, "role": "user"},
        ],
        "model": "llama3",
        "stream": True,
    }
    assert "Authorization" not in request.headers
    assert "SYNTHETIC" not in json.dumps(result.to_dict())


def test_probe_network_uses_metadata_only_without_student_context() -> None:
    transport = SyntheticTransport([b'{"models":[]}'])
    client = OfflineAIClient(transport)
    config = ProviderConfig("lm", "lm_studio", "local-model", endpoint="http://127.0.0.1:1234/v1")
    result = client.probe(config, network=True)
    assert result["network"] is True
    assert transport.calls[0].method == "GET"
    assert transport.calls[0].body == b""


def test_tool_events_are_rejected_and_cancel_before_send_is_safe() -> None:
    transport = SyntheticTransport([b'{"message":{"tool_calls":[{"id":"x"}]}}\n'])
    client = OfflineAIClient(transport)
    with pytest.raises(AIToolEvent):
        client.request(
            packet=packet(),
            provider=ProviderConfig("ollama", "ollama", "m", endpoint="http://127.0.0.1:11434"),
        )
    event = threading.Event()
    event.set()
    transport.calls.clear()
    with pytest.raises(AICancelled):
        client.request(
            packet=packet(),
            provider=ProviderConfig("ollama", "ollama", "m", endpoint="http://127.0.0.1:11434"),
            cancel_event=event,
        )
    assert transport.calls == []


def test_registry_switches_without_secret_or_file_access() -> None:
    registry = ProviderRegistry(
        [
            ProviderConfig("codex", "codex_cli", "gpt-5.6-sol", effort="high"),
            ProviderConfig("ollama", "ollama", "llama3", endpoint="http://127.0.0.1:11434"),
        ]
    )
    assert registry.current().id == "codex"
    assert registry.select("ollama").id == "ollama"
    registry.register(ProviderConfig("ollama-new", "ollama", "llama3.1", endpoint="http://127.0.0.1:11434"))
    assert tuple(item.id for item in registry.values()) == ("codex", "ollama-new")
    public = registry.public()
    assert all("SYNTHETIC" not in json.dumps(item) for item in public["providers"])


def test_opencode_spec_is_typed_native_request_and_event_decoder_is_tool_fail_closed() -> None:
    config = ProviderConfig("oc", "opencode_cli", "openai/gpt-5.6-sol", effort="high")
    spec = build_opencode_request(packet(), config)
    assert spec.provider_model == "openai/gpt-5.6-sol"
    assert spec.rendered_prompt == packet().rendered_prompt
    native = spec.to_native_job()
    assert native["textOnly"] is True
    assert set(native) == {
        "runId",
        "renderedPrompt",
        "providerModel",
        "providerId",
        "endpoint",
        "apiKey",
        "effort",
        "tutorMode",
        "manifestSha256",
        "promptSha256",
        "inputDigest",
        "textOnly",
    }
    assert not any(key in native for key in ("argv", "environment", "cwd", "executable"))
    decoder = OpenCodeEventDecoder()
    decoder.feed('{"type":"session.started"}')
    decoder.feed('{"type":"session.text.delta","properties":{"delta":"answer"}}')
    assert decoder.finish() == "answer"
    with pytest.raises(AIToolEvent):
        decode_opencode_events(['{"type":"session.tool.called","properties":{"name":"read"}}'])


def test_opencode_gateway_payload_is_typed_and_records_real_provider_model() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    state = {"started": False, "polled": False}

    def gateway(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((operation, payload))
        if operation == "opencode_status":
            return {"state": "ready", "models": ["openai/gpt-5.6-sol"], "gatewayProtocol": 1}
        if operation == "opencode_start":
            state["started"] = True
            assert set(payload) == {
                "runId",
                "renderedPrompt",
                "providerModel",
                "providerId",
                "endpoint",
                "apiKey",
                "effort",
                "tutorMode",
                "manifestSha256",
                "promptSha256",
                "inputDigest",
                "textOnly",
            }
            assert payload["textOnly"] is True
            assert payload["apiKey"] == "SYNTHETIC_KEY"
            return {
                "requestId": "native-1",
                "state": "starting",
                "text": "",
                "model": {"mode": "manual", "id": "openai/gpt-5.6-sol"},
                "effort": "high",
            }
        assert operation == "opencode_poll" and payload == {"requestId": "native-1"}
        state["polled"] = True
        return {
            "requestId": "native-1",
            "state": "completed",
            "text": "OpenCode answer",
            "model": {"mode": "manual", "id": "openai/gpt-5.6-sol"},
            "effort": "high",
        }

    client = OpenCodeGatewayClient(gateway)
    started = client.run(
        manifest=make_context()[0],
        sections=make_context()[1],
        model="gpt-5.6-sol",
        effort="high",
        run_id="run-1",
        provider_model="openai/gpt-5.6-sol",
        api_key="SYNTHETIC_KEY",
    )
    assert started["provider_model"] == "openai/gpt-5.6-sol"
    assert started["provider_info"] == {
        "id": "opencode-native",
        "kind": "opencode_cli",
        "model": "openai/gpt-5.6-sol",
        "effort": "high",
    }
    assert started["model"] == "gpt-5.6-sol"
    finished = client.status("run-1")
    assert finished["status"] == "succeeded" and finished["final_output"] == "OpenCode answer"
    assert finished["provider_info"] == started["provider_info"]
    local_events = client.events("run-1")
    assert local_events["schema_version"] == 1
    assert local_events["run_id"] == "run-1"
    assert local_events["next_after"] == len(local_events["events"])
    assert [item["type"] for item in local_events["events"]] == ["queued", "queued", "succeeded"]
    assert all(set(item) == {"sequence", "type", "created_at"} for item in local_events["events"])
    assert set(operation for operation, _payload in calls) <= {
        "opencode_status",
        "opencode_start",
        "opencode_poll",
    }
    assert "opencode_events" not in [operation for operation, _payload in calls]
    assert client.events("run-1", after=local_events["next_after"])["events"] == []
    future_cursor = client.events("run-1", after=99)
    assert future_cursor["events"] == [] and future_cursor["next_after"] == 99
    with pytest.raises(AIProtocolError):
        client.events("run-1", after=-1)
    with pytest.raises(AIProtocolError):
        client.events("run-1", after=True)
    assert state == {"started": True, "polled": True}


def test_opencode_gateway_exposes_exactly_four_native_operations() -> None:
    assert OPEN_CODE_GATEWAY_OPERATIONS == frozenset(
        {"opencode_status", "opencode_start", "opencode_poll", "opencode_cancel"}
    )


def test_provider_adapter_keeps_original_capabilities_and_runs_http_as_accepted_lifecycle() -> None:
    transport = SyntheticTransport(
        [b'{"choices":[{"delta":{"content":"bounded"}}]}\n'],
    )
    settings = LocalAISettingsStore()
    profile = ProviderConfig("ollama-1", "ollama", "llama3", endpoint="http://127.0.0.1:11434")
    settings.upsert(profile)
    adapter = OfflineAIProviderAdapter(settings=settings, http_client=OfflineAIClient(transport))
    accepted = adapter.run(
        manifest=make_context("adapter-run")[0],
        sections=make_context("adapter-run")[1],
        model="gpt-5.6-sol",
        effort="high",
        run_id="adapter-run",
    )
    assert accepted["accepted"] is True
    assert accepted["gateway"]["provider_info"] == {
        "id": "ollama-1",
        "kind": "ollama",
        "model": "llama3",
        "effort": "default",
    }
    capabilities = adapter.capabilities()
    assert capabilities["schema_version"] == 1
    assert capabilities["codex"]["available"] is True
    assert {tuple((item["model"], item["effort"])) for item in capabilities["codex"]["routes"]} == ALLOWED_ROUTES
    assert capabilities["provider"]["kind"] == "ollama"
    deadline = time.monotonic() + 2
    status = adapter.status("adapter-run")
    while status["status"] not in {"succeeded", "failed", "timed_out", "cancelled", "unavailable"}:
        if time.monotonic() >= deadline:
            raise AssertionError("synthetic HTTP worker did not finish")
        time.sleep(0.01)
        status = adapter.status("adapter-run")
    assert status["status"] == "succeeded"
    assert status["final_output"] == "bounded"
    assert status["provider_info"]["model"] == "llama3"
    assert adapter.events("adapter-run")["schema_version"] == 1
    adapter.quiesce()


class _BlockingChunks:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.closed = threading.Event()

    def __iter__(self) -> _BlockingChunks:
        return self

    def __next__(self) -> bytes:
        self.started.set()
        if not self.closed.wait(2):
            raise AssertionError("synthetic stream was not cancelled")
        raise StopIteration


def test_provider_adapter_cancel_closes_active_http_response_and_quiesces() -> None:
    chunks = _BlockingChunks()

    def transport(_request: Any) -> HTTPResponse:
        return HTTPResponse(200, {"content-type": "application/x-ndjson"}, chunks, close=chunks.closed.set)

    settings = LocalAISettingsStore()
    settings.upsert(ProviderConfig("ollama-1", "ollama", "llama3", endpoint="http://127.0.0.1:11434"))
    adapter = OfflineAIProviderAdapter(settings=settings, http_client=OfflineAIClient(transport))
    adapter.run(
        manifest=make_context("cancel-run")[0],
        sections=make_context("cancel-run")[1],
        model="gpt-5.6-sol",
        effort="high",
        run_id="cancel-run",
    )
    assert chunks.started.wait(1)
    cancelled = adapter.cancel("cancel-run")
    assert isinstance(cancelled, dict) and cancelled["status"] == "cancelling"
    assert chunks.closed.is_set()
    deadline = time.monotonic() + 2
    status = adapter.status("cancel-run")
    while status["status"] not in {"cancelled", "failed", "timed_out", "unavailable"}:
        if time.monotonic() >= deadline:
            raise AssertionError("synthetic HTTP cancellation did not settle")
        time.sleep(0.01)
        status = adapter.status("cancel-run")
    assert status["status"] == "cancelled"
    adapter.quiesce()


class _UncooperativeChunks:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def __iter__(self) -> _UncooperativeChunks:
        return self

    def __next__(self) -> bytes:
        self.started.set()
        if self.release.wait(2):
            raise StopIteration
        raise AssertionError("synthetic worker was not released")


def test_provider_adapter_keeps_cancelling_worker_active_until_worker_exit() -> None:
    chunks = _UncooperativeChunks()

    def transport(_request: Any) -> HTTPResponse:
        # Deliberately ignore close: the adapter must not discard this live
        # worker merely because cancellation was requested.
        return HTTPResponse(200, {"content-type": "application/x-ndjson"}, chunks)

    settings = LocalAISettingsStore()
    profile = ProviderConfig("ollama-1", "ollama", "llama3", endpoint="http://127.0.0.1:11434")
    settings.upsert(profile)
    adapter = OfflineAIProviderAdapter(settings=settings, http_client=OfflineAIClient(transport))
    adapter.run(
        manifest=make_context("live-cancel")[0],
        sections=make_context("live-cancel")[1],
        model="gpt-5.6-sol",
        effort="high",
        run_id="live-cancel",
    )
    assert chunks.started.wait(1)
    snapshot = adapter.cancel("live-cancel")
    assert isinstance(snapshot, dict) and snapshot["status"] == "cancelling"
    with pytest.raises(CodexUnavailable):
        adapter.run(
            manifest=make_context("replacement")[0],
            sections=make_context("replacement")[1],
            model="gpt-5.6-sol",
            effort="high",
            run_id="replacement",
        )
    chunks.release.set()
    deadline = time.monotonic() + 2
    status = adapter.status("live-cancel")
    while status["status"] not in {"cancelled", "failed", "timed_out", "unavailable"}:
        if time.monotonic() >= deadline:
            raise AssertionError("cancelling worker did not settle")
        time.sleep(0.01)
        status = adapter.status("live-cancel")
    assert status["status"] == "cancelled"
    adapter.quiesce()


class _HangingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, mode: str, *, delay_seconds: float = 0.0) -> None:
        super().__init__(("127.0.0.1", 0), _HangingHTTPHandler)
        self.mode = mode
        self.delay_seconds = delay_seconds
        self.request_started = threading.Event()
        self.headers_sent = threading.Event()
        self.release = threading.Event()


class _HangingHTTPHandler(BaseHTTPRequestHandler):
    server: _HangingHTTPServer

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.server.request_started.set()
        if self.server.mode == "headers":
            self.server.release.wait(5)
            return
        if self.server.mode == "slow_headers":
            for byte in b"HTTP/1.1 200 OK\r\nX-Drip: ":
                try:
                    self.wfile.write(bytes((byte,)))
                    self.wfile.flush()
                except OSError:
                    return
                time.sleep(0.05)
            for _ in range(200):
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                except OSError:
                    return
                time.sleep(0.05)
            return
        if self.server.mode == "slow_trailer":
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.write(b"2\r\n{}\r\n")
                self.wfile.flush()
                self.wfile.write(b"0\r\nX-Drip: ")
                self.wfile.flush()
            except OSError:
                return
            for _ in range(200):
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                except OSError:
                    return
                time.sleep(0.05)
            return
        body = b'{"choices":[{"delta":{"content":"delayed"}}]}\n'
        incomplete_body = self.server.mode in {
            "body",
            "connection_close_body",
            "connection_close_body_delayed",
        }
        if self.server.mode == "delayed_headers":
            time.sleep(self.server.delay_seconds)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Content-Length", str(128 if incomplete_body else len(body)))
        if self.server.mode in {"connection_close_body", "connection_close_body_delayed"}:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.server.mode == "delayed_headers":
            self.wfile.write(body)
            self.wfile.flush()
            return
        if self.server.mode == "delayed_body":
            time.sleep(self.server.delay_seconds)
            self.wfile.write(body)
            self.wfile.flush()
            return
        if self.server.mode == "connection_close_body_delayed":
            self.server.headers_sent.set()
            time.sleep(self.server.delay_seconds)
        self.wfile.write(b'{"choices":[{"delta":{"content":"partial"}}]}\n')
        self.wfile.flush()
        self.server.release.wait(5)


@pytest.mark.parametrize("mode", ["headers", "body", "connection_close_body_delayed"])
def test_default_http_transport_cancel_interrupts_loopback_header_or_body_read(mode: str) -> None:
    server = _HangingHTTPServer(mode, delay_seconds=1.3)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}"
        client = OfflineAIClient()
        config = ProviderConfig("loopback", "openai_compatible_local", "synthetic", endpoint=endpoint)
        errors: list[BaseException] = []

        def send() -> None:
            try:
                client.request(packet=packet(), provider=config)
            except BaseException as exc:  # noqa: BLE001 - assert the stable cancellation class below
                errors.append(exc)

        worker = threading.Thread(target=send, daemon=True)
        worker.start()
        assert server.request_started.wait(2)
        if mode == "connection_close_body_delayed":
            assert server.headers_sent.wait(2)
            time.sleep(0.1)
        started_cancel = time.monotonic()
        assert client.cancel("run-1") is True
        worker.join(2)
        assert not worker.is_alive()
        assert time.monotonic() - started_cancel < 1.5
        assert len(errors) == 1 and isinstance(errors[0], AICancelled)
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        serving.join(2)


@pytest.mark.parametrize("mode", ["slow_headers", "slow_trailer"])
def test_default_http_transport_absolute_wall_timeout_handles_continuous_drips(mode: str) -> None:
    server = _HangingHTTPServer(mode)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}"
        client = OfflineAIClient()
        config = ProviderConfig(
            "loopback",
            "openai_compatible_local",
            "synthetic",
            endpoint=endpoint,
            timeout_seconds=0.8,
            inactivity_seconds=5,
        )
        started = time.monotonic()
        with pytest.raises(AITimeout):
            client.request(packet=packet(), provider=config)
        assert time.monotonic() - started < 2
        assert server.request_started.is_set()
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        serving.join(2)


@pytest.mark.parametrize("mode", ["delayed_headers", "delayed_body"])
def test_default_http_transport_allows_configured_delayed_headers_and_body(mode: str) -> None:
    server = _HangingHTTPServer(mode, delay_seconds=1.3)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}"
        client = OfflineAIClient()
        config = ProviderConfig(
            "loopback",
            "openai_compatible_local",
            "synthetic",
            endpoint=endpoint,
            timeout_seconds=10,
            inactivity_seconds=5,
        )
        started = time.monotonic()
        result = client.request(packet=packet(), provider=config)
        assert result.content == "delayed"
        assert time.monotonic() - started >= 1.1
        assert server.request_started.is_set()
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        serving.join(2)


class FakeProtector:
    def protect(self, value: bytes) -> bytes:
        return b"ENC:" + value[::-1]

    def unprotect(self, value: bytes) -> bytes:
        assert value.startswith(b"ENC:")
        return value[4:][::-1]


class _BlockingSecretStore(SecretStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path, protector=FakeProtector())
        self.block_next_get = False
        self.get_started = threading.Event()
        self.release_get = threading.Event()

    def get(self, reference: str) -> str:
        value = super().get(reference)
        if self.block_next_get:
            self.block_next_get = False
            self.get_started.set()
            if not self.release_get.wait(2):
                raise AssertionError("synthetic key capture was not released")
        return value


class _RaceSettingsStore(LocalAISettingsStore):
    def __init__(self, *, secret_store: SecretStore) -> None:
        super().__init__(secret_store=secret_store)
        self.switch_attempted = threading.Event()

    def upsert(self, profile: ProviderConfig, **kwargs: Any) -> ProviderConfig:
        if profile.endpoint == "https://two.example/v1":
            self.switch_attempted.set()
        return super().upsert(profile, **kwargs)


def test_secret_store_uses_encrypted_file_and_never_accepts_forbidden_paths(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "ai-secrets.dpapi", protector=FakeProtector())
    store.put("openai", "SYNTHETIC_API_KEY")
    assert store.get("openai") == "SYNTHETIC_API_KEY"
    assert store.references() == ("openai",)
    assert b"SYNTHETIC_API_KEY" not in store.path.read_bytes()
    with pytest.raises(SecretStoreError):
        SecretStore(tmp_path / "export" / "secrets.dpapi", protector=FakeProtector())
    with pytest.raises(SecretStoreError):
        SecretStore(tmp_path / "secrets.json", protector=FakeProtector())


def test_adapter_captures_profile_and_key_as_one_settings_generation(tmp_path: Path) -> None:
    secret_store = _BlockingSecretStore(tmp_path / "ai-secrets.dpapi")
    settings = _RaceSettingsStore(secret_store=secret_store)
    old_profile = ProviderConfig(
        "remote",
        "openai_compatible",
        "old-model",
        endpoint="https://one.example/v1",
        allow_remote=True,
    )
    new_profile = ProviderConfig(
        "remote",
        "openai_compatible",
        "new-model",
        endpoint="https://two.example/v1",
        allow_remote=True,
    )
    settings.upsert(old_profile, api_key="OLD_KEY", selected=True)
    secret_store.block_next_get = True
    transport = SyntheticTransport([b'{"choices":[{"delta":{"content":"ok"}}]}\n'])
    adapter = OfflineAIProviderAdapter(settings=settings, http_client=OfflineAIClient(transport))
    errors: list[BaseException] = []

    def send() -> None:
        try:
            adapter.run(
                manifest=make_context("generation-race")[0],
                sections=make_context("generation-race")[1],
                model="gpt-5.6-sol",
                effort="high",
                run_id="generation-race",
            )
        except BaseException as exc:  # noqa: BLE001 - assert no race failure below
            errors.append(exc)

    sender = threading.Thread(target=send, daemon=True)
    sender.start()
    assert secret_store.get_started.wait(2)

    def switch() -> None:
        settings.upsert(new_profile, api_key="NEW_KEY", selected=True)

    switcher = threading.Thread(target=switch, daemon=True)
    switcher.start()
    assert settings.switch_attempted.wait(2)
    secret_store.release_get.set()
    sender.join(2)
    switcher.join(2)
    assert not sender.is_alive() and not switcher.is_alive()
    assert errors == []
    assert len(transport.calls) == 1
    request = transport.calls[0]
    assert request.url == "https://one.example/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer OLD_KEY"
    adapter.quiesce()


def test_settings_preserve_key_only_for_same_provider_identity_and_support_explicit_clear(tmp_path: Path) -> None:
    secret_store = SecretStore(tmp_path / "ai-secrets.dpapi", protector=FakeProtector())
    settings = LocalAISettingsStore(secret_store=secret_store, path=tmp_path / "ai-settings.json")
    first = ProviderConfig(
        "remote",
        "openai_compatible",
        "remote-model",
        endpoint="https://one.example/v1",
        allow_remote=True,
    )
    settings.upsert(first, api_key="SYNTHETIC_API_KEY", selected=True)
    assert settings.public()["profiles"][0]["hasApiKey"] is True
    same_identity = ProviderConfig(
        "remote",
        "openai_compatible",
        "changed-model",
        endpoint="https://one.example/v1",
        allow_remote=True,
    )
    settings.upsert(same_identity)
    assert secret_store.get("ai-openai_compatible") == "SYNTHETIC_API_KEY"
    changed_endpoint = ProviderConfig(
        "remote",
        "openai_compatible",
        "changed-model",
        endpoint="https://two.example/v1",
        allow_remote=True,
    )
    with pytest.raises(LocalAISettingsError):
        settings.upsert(changed_endpoint)
    assert secret_store.get("ai-openai_compatible") == "SYNTHETIC_API_KEY"
    settings.upsert(changed_endpoint, clear_api_key=True)
    assert settings.public()["profiles"][0]["hasApiKey"] is False
    with pytest.raises(SecretStoreError):
        secret_store.get("ai-openai_compatible")


def test_settings_validation_never_echoes_api_key_or_input_body() -> None:
    app = FastAPI()
    install_local_ai_routes(app, store=LocalAISettingsStore(), client=OfflineAIClient(SyntheticTransport([])))
    response = TestClient(app).put(
        "/api/v1/local/ai",
        json={"kind": "ollama", "model": 42, "apiKey": "SYNTHETIC_API_KEY", "unexpected": "SYNTHETIC_BODY"},
    )
    assert response.status_code == 422
    assert "SYNTHETIC_API_KEY" not in response.text
    assert "SYNTHETIC_BODY" not in response.text

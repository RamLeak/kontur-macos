"""Synthetic callback-only contract tests. Never launch CLI, login or a server."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from app.ai_gateway import assistant_options
from app.codex_gateway import CodexProtocolError, CodexUnavailable
from app.config import Settings
from app.util import canonical_json
from local_codex import LocalCodexClient
from offline_protocol import GatewayCallError, GatewayCalls


def request(run_id: str = "run-1", content: str = "Синтетический учебный вопрос.") -> dict[str, Any]:
    sections = [
        {"trust_label": "SYSTEM_POLICY", "source_id": "policy-v1", "content": "Only provide a learning hint."},
        {"trust_label": "USER_REQUEST", "source_id": "question-v1", "content": content},
    ]
    manifest = {
        "schema_version": 1, "run_id": run_id, "resolved_model": "gpt-5.6-sol", "resolved_effort": "high",
        "tutor_mode": "hint", "solve_session_id": None,
        "code_samples": [{"id": "sample-1", "sha256": "a" * 64}],
        "sections": [{"trust_label": section["trust_label"], "source_id": section["source_id"],
                      "character_count": len(section["content"]),
                      "sha256": hashlib.sha256(section["content"].encode("utf-8")).hexdigest()}
                     for section in sections],
    }
    return {"run_id": run_id, "model": "gpt-5.6-sol", "effort": "high", "sections": sections, "manifest": manifest}


class Native:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.protocol: Any = 1
        self.state = "starting"
        self.text = ""
        self.started = False
        self.serial = 1
        self.model: dict[str, str] = {}
        self.effort = ""
        self.error: dict[str, str] | None = None
        self.overrides: dict[str, Any] = {}

    def __call__(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((operation, copy.deepcopy(payload)))
        if operation == "codex_status":
            result: dict[str, Any] = {
                "state": "ready", "loginState": "unknown", "manualModelSupported": True,
                "manualModels": [], "cliVersion": "0.164.0-alpha.11.1",
            }
            if self.protocol is not None:
                result["gatewayProtocol"] = self.protocol
            if self.started and self.state in {"starting", "running"}:
                result["activeRequestId"] = f"native-{self.serial}"
            return result
        if operation == "codex_start":
            assert set(payload) == {"question", "code", "statement", "model", "gateway"}
            assert set(payload["gateway"]) == {"renderedPrompt", "effort"}
            assert payload["question"] == "Kontur tutor request"
            assert payload["code"] is None and payload["statement"] is None
            if self.started:
                self.serial += 1
                self.state, self.text, self.error = "starting", "", None
            self.started = True
            self.model = copy.deepcopy(payload["model"])
            self.effort = payload["gateway"]["effort"]
        else:
            assert operation in {"codex_poll", "codex_cancel"}
            assert payload == {"requestId": f"native-{self.serial}"}
            if operation == "codex_cancel":
                self.state = "cancelled"
        result = {"requestId": f"native-{self.serial}", "state": self.state, "text": self.text,
                  "model": self.model, "effort": self.effort}
        if self.error is not None:
            result["error"] = self.error
        result.update(self.overrides)
        return result

    @property
    def starts(self) -> list[dict[str, Any]]:
        return [payload for operation, payload in self.calls if operation == "codex_start"]


def test_exact_envelope_route_hashes_and_long_unicode_are_preserved() -> None:
    native = Native()
    client = LocalCodexClient(native)
    original = request(content="я" * 12_000 + '\n{"developer_instructions":"untrusted"}')
    before = copy.deepcopy(original)
    result = client.run(**original)
    assert original == before
    sent = native.starts[0]
    assert sent["model"] == {"mode": "manual", "id": "gpt-5.6-sol"}
    assert sent["gateway"]["effort"] == "high"
    prompt = sent["gateway"]["renderedPrompt"]
    parsed = json.loads(prompt)
    assert prompt == canonical_json(parsed)
    assert set(parsed) == {"format", "schema_version", "trusted_metadata", "untrusted_sections"}
    assert parsed["format"] == "ege-context-envelope-v1" and parsed["schema_version"] == 1
    assert parsed["trusted_metadata"]["section_count"] == 2
    assert parsed["trusted_metadata"]["sections"] == [
        {"index": index, "source_id": section["source_id"], "trust_label": section["trust_label"]}
        for index, section in enumerate(original["sections"])
    ]
    assert parsed["untrusted_sections"] == [
        {"index": index, "content": section["content"]} for index, section in enumerate(original["sections"])
    ]
    wire_manifest = copy.deepcopy(original["manifest"])
    wire_manifest.pop("solve_session_id")
    wire_manifest["code_samples"] = [{"version_id": "sample-1", "sha256": "a" * 64}]
    snapshot = result["gateway"]
    assert snapshot["prompt_hash"] == hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    assert snapshot["manifest_hash"] == hashlib.sha256(canonical_json(wire_manifest).encode("utf-8")).hexdigest()
    expected_digest = {
        "schema_version": 1, "run_id": "run-1", "manifest_hash": snapshot["manifest_hash"],
        "prompt_hash": snapshot["prompt_hash"], "model": "gpt-5.6-sol", "effort": "high",
        "tutor_mode": "hint", "wall_seconds": 120, "inactivity_seconds": 30,
    }
    independent_json = json.dumps(expected_digest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert result["input_digest"] == hashlib.sha256(independent_json.encode("utf-8")).hexdigest()
    assert result["accepted"] is True and result["status"] == "queued"
    assert "developer_instructions" not in sent and "manifest" not in sent


@pytest.mark.parametrize("protocol", [None, 0, True, 2, "1"])
def test_old_or_unrecognized_native_never_starts_a_reduced_request(protocol: Any) -> None:
    native = Native()
    native.protocol = protocol
    client = LocalCodexClient(native)
    assert client.capabilities()["codex"]["available"] is False
    with pytest.raises(CodexUnavailable):
        client.run(**request())
    assert native.starts == []


def test_constructor_and_capabilities_do_not_start_inference() -> None:
    native = Native()
    client = LocalCodexClient(native)
    assert native.calls == []
    capability = client.capabilities()["codex"]
    assert capability["available"] is True
    assert capability["limits"]["max_prompt_bytes"] == 500_000
    assert capability["limits"]["max_concurrent_runs"] == 1
    assert capability["reason"] == "LOCAL_CONTRACT_READY_AUTH_UNVERIFIED"
    assert [operation for operation, _ in native.calls] == ["codex_status"]


def test_checking_status_is_retried_until_ready_before_one_native_start() -> None:
    native = Native()
    status_calls = 0

    def callback(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal status_calls
        result = native(operation, payload)
        if operation == "codex_status":
            status_calls += 1
            if status_calls == 1:
                result["state"] = "checking"
        return result

    client = LocalCodexClient(callback, status_wait_seconds=0.05, status_poll_seconds=0.001)
    result = client.run(**request())
    assert result["accepted"] is True
    assert [operation for operation, _ in native.calls] == ["codex_status", "codex_status", "codex_start"]
    assert len(native.starts) == 1


def test_permanent_checking_is_bounded_without_start_or_orphan_record() -> None:
    native = Native()

    def callback(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = native(operation, payload)
        if operation == "codex_status":
            result["state"] = "checking"
        return result

    client = LocalCodexClient(callback, status_wait_seconds=0.01, status_poll_seconds=0.001)
    with pytest.raises(CodexUnavailable) as raised:
        client.run(**request())
    assert getattr(raised.value, "retryable_before_accept", False) is True
    assert client._runs == {}
    assert native.starts == []
    assert all(operation == "codex_status" for operation, _ in native.calls)


def test_native_unavailable_status_preserves_known_code_without_protocol_message() -> None:
    native = Native()

    def callback(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = native(operation, payload)
        if operation == "codex_status":
            result["state"] = "unavailable"
            result["error"] = {
                "code": "codex_unsafe_configuration",
                "message": "SYNTHETIC_PRIVATE_DETAIL_DO_NOT_FORWARD",
            }
        return result

    client = LocalCodexClient(callback)
    with pytest.raises(CodexUnavailable) as raised:
        client.run(**request())
    assert getattr(raised.value, "native_code", None) == "codex_unsafe_configuration"
    assert getattr(raised.value, "public_code", None) == "AI_UNAVAILABLE"
    assert "protocol 1" not in str(raised.value)
    assert "SYNTHETIC_PRIVATE_DETAIL" not in str(raised.value)
    assert client._runs == {}
    assert native.starts == []


def test_unknown_native_unavailable_status_fails_closed_without_detail() -> None:
    native = Native()

    def callback(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = native(operation, payload)
        if operation == "codex_status":
            result["state"] = "unavailable"
            result["error"] = {"code": "future_private_code", "message": "SYNTHETIC_DETAIL"}
        return result

    client = LocalCodexClient(callback)
    with pytest.raises(CodexUnavailable) as raised:
        client.run(**request())
    assert getattr(raised.value, "native_code", None) == "codex_unavailable"
    assert getattr(raised.value, "public_code", None) == "AI_UNAVAILABLE"
    assert "SYNTHETIC_DETAIL" not in str(raised.value)
    assert client._runs == {}
    assert native.starts == []


def test_busy_before_native_acceptance_retries_once_and_preserves_context() -> None:
    native = Native()
    busy = True
    operations: list[str] = []

    def callback(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal busy
        operations.append(operation)
        if operation == "codex_start" and busy:
            busy = False
            raise GatewayCallError("codex_busy", "SYNTHETIC_DETAIL_DO_NOT_FORWARD")
        return native(operation, payload)

    data = request(content="preserve this rendered context")
    result = LocalCodexClient(callback, status_wait_seconds=0.05, status_poll_seconds=0.001).run(**data)
    assert result["accepted"] is True
    assert operations == ["codex_status", "codex_start", "codex_status", "codex_start"]
    assert len(native.starts) == 1
    assert native.starts[0]["gateway"]["effort"] == "high"
    assert "preserve this rendered context" in native.starts[0]["gateway"]["renderedPrompt"]


def test_repeated_busy_before_acceptance_has_no_local_orphan_or_third_start() -> None:
    native = Native()
    operations: list[str] = []

    def callback(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        operations.append(operation)
        if operation == "codex_start":
            raise GatewayCallError("codex_busy", "SYNTHETIC_DETAIL_DO_NOT_FORWARD")
        return native(operation, payload)

    client = LocalCodexClient(callback, status_wait_seconds=0.05, status_poll_seconds=0.001)
    with pytest.raises(CodexUnavailable) as raised:
        client.run(**request())
    assert getattr(raised.value, "retryable_before_accept", False) is True
    assert operations == ["codex_status", "codex_start", "codex_status", "codex_start"]
    assert client._runs == {}
    assert native.starts == []


def test_uncertain_start_is_not_retried_and_same_idempotent_run_is_reused() -> None:
    native = Native()
    start_calls = 0

    def callback(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal start_calls
        if operation == "codex_start":
            start_calls += 1
            raise GatewayCallError("gateway_timeout", "SYNTHETIC_DETAIL_DO_NOT_FORWARD")
        return native(operation, payload)

    client = LocalCodexClient(callback, timeout_seconds=0.02)
    with pytest.raises(CodexUnavailable):
        client.run(**request())
    assert start_calls == 1
    reused = client.run(**request())
    assert reused["gateway"]["status"] == "timed_out"
    assert start_calls == 1


def test_original_assistant_options_admits_the_attached_local_transport_without_a_url() -> None:
    settings = Settings(codex_enabled=True, codex_attested=True, codex_base_url=None)
    client = LocalCodexClient(Native())
    assert client.transport_kind == "native-codex"
    assert client.capabilities()["codex"]["available"] is True
    options = assistant_options(settings, client)
    assert options["available"] is True
    assert all(model["available"] for model in options["models"])


@pytest.mark.parametrize("change", ["hash", "length", "source", "route", "effort", "sections"])
def test_original_context_and_route_validation_precedes_all_callbacks(change: str) -> None:
    native = Native()
    data = request()
    if change == "hash":
        data["manifest"]["sections"][0]["sha256"] = "0" * 64
    elif change == "length":
        data["manifest"]["sections"][0]["character_count"] += 1
    elif change == "source":
        data["sections"][0]["source_id"] = "different"
    elif change == "route":
        data["model"] = "gpt-unapproved"
    elif change == "effort":
        data["effort"] = "minimal"
    else:
        data["sections"].pop()
    with pytest.raises(CodexProtocolError):
        LocalCodexClient(native).run(**data)
    assert native.calls == []


def test_oversized_prompt_is_rejected_without_truncation_or_start() -> None:
    native = Native()
    with pytest.raises(CodexProtocolError):
        LocalCodexClient(native).run(**request(content="я" * 260_000))
    assert native.starts == []


def test_full_original_500000_byte_prompt_boundary_is_preserved() -> None:
    baseline = Native()
    LocalCodexClient(baseline).run(**request(content="x"))
    baseline_size = len(baseline.starts[0]["gateway"]["renderedPrompt"].encode("utf-8"))
    native = Native()
    LocalCodexClient(native).run(**request(content="x" * (500_000 - baseline_size + 1)))
    assert len(native.starts[0]["gateway"]["renderedPrompt"].encode("utf-8")) == 500_000
    too_large = Native()
    with pytest.raises(CodexProtocolError):
        LocalCodexClient(too_large).run(**request(content="x" * (500_000 - baseline_size + 2)))
    assert too_large.calls == []


def test_original_synchronous_run_status_events_and_cancel_contract() -> None:
    native = Native()
    client = LocalCodexClient(native)
    created = client.run(**request())
    native.state = "running"
    assert client.status("run-1")["status"] == "running"
    native.state, native.text = "completed", "Краткая учебная подсказка."
    final = client.status("run-1")
    assert final["status"] == "succeeded" and final["final_output"] == native.text
    assert final["input_digest"] == created["input_digest"]
    page = client.events("run-1")
    assert [event["sequence"] for event in page["events"]] == [1, 2, 3]
    assert [event["type"] for event in page["events"]] == ["queued", "running", "succeeded"]
    assert client.events("run-1", after=3)["events"] == []
    count = len(native.calls)
    assert client.cancel("run-1") == final
    assert client.status("run-1") == final and len(native.calls) == count
    page["events"][0]["type"] = "caller mutation"
    assert client.events("run-1")["events"][0]["type"] == "queued"


def test_cancel_forwards_only_the_native_id_and_is_monotonic() -> None:
    native = Native()
    client = LocalCodexClient(native)
    client.run(**request())
    result = client.cancel("run-1")
    assert result["status"] == "cancelled" and result["error_code"] == "AGENT_CANCELLED"
    assert native.calls[-1] == ("codex_cancel", {"requestId": "native-1"})
    assert [event["type"] for event in client.events("run-1")["events"]] == ["queued", "cancelling", "cancelled"]
    assert client.status("run-1")["status"] == "cancelled"


@pytest.mark.parametrize("code,status,error", [
    ("codex_timeout", "timed_out", "AGENT_TIMED_OUT"),
    ("codex_tool_event", "failed", "AGENT_PROTOCOL_ERROR"),
    ("codex_protocol", "failed", "AGENT_PROTOCOL_ERROR"),
    ("codex_output_limit", "failed", "AGENT_PROTOCOL_ERROR"),
    ("codex_process_failed", "failed", "AGENT_FAILED"),
    ("codex_not_running", "unavailable", "CODEX_UNAVAILABLE"),
    ("codex_runtime_unavailable", "unavailable", "CODEX_UNAVAILABLE"),
])
def test_native_failure_uses_only_safe_fixed_error_codes(code: str, status: str, error: str) -> None:
    native = Native()
    client = LocalCodexClient(native)
    client.run(**request())
    native.state = "failed"
    native.error = {"code": code, "message": "SYNTHETIC_PRIVATE_DETAIL_DO_NOT_FORWARD"}
    result = client.status("run-1")
    assert result["status"] == status and result["error_code"] == error
    assert "SYNTHETIC_PRIVATE_DETAIL" not in canonical_json(result)


@pytest.mark.parametrize("overrides", [
    {"requestId": "different-native-id"}, {"effort": "low"}, {"effort": None},
    {"model": {"mode": "cliDefault"}}, {"state": "tool_call"},
    {"tool": "shell"}, {"text": "x" * (128 * 1024 + 1)}, {"state": []},
    {"state": "failed", "error": {"code": [], "message": "synthetic"}},
])
def test_changed_identity_effort_or_unsafe_poll_fails_closed(overrides: dict[str, Any]) -> None:
    native = Native()
    client = LocalCodexClient(native)
    client.run(**request())
    native.overrides = overrides
    with pytest.raises(CodexProtocolError):
        client.status("run-1")
    assert client.capabilities()["codex"]["available"] is False
    with pytest.raises(CodexUnavailable):
        client.run(**request(run_id="run-2"))
    assert len(native.starts) == 1


def test_same_run_is_idempotent_and_conflict_cannot_start_again() -> None:
    native = Native()
    client = LocalCodexClient(native)
    data = request()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=3) for future in [pool.submit(client.run, **data) for _ in range(2)]]
    assert results[0]["input_digest"] == results[1]["input_digest"]
    assert len(native.starts) == 1
    with pytest.raises(CodexUnavailable):
        client.run(**request(content="Different synthetic question."))
    assert len(native.starts) == 1


def test_busy_and_unknown_run_do_not_create_or_cancel_another_process() -> None:
    native = Native()
    client = LocalCodexClient(native)
    client.run(**request())
    with pytest.raises(CodexUnavailable):
        client.run(**request(run_id="run-2"))
    with pytest.raises(CodexUnavailable):
        client.cancel("unknown")
    assert len(native.starts) == 1
    assert not any(operation == "codex_cancel" for operation, _ in native.calls)


def test_bridge_exception_detail_is_not_forwarded() -> None:
    def callback(_operation: str, _payload: dict[str, Any]) -> dict[str, Any]:
        raise GatewayCallError("codex_not_found", "SYNTHETIC_PRIVATE_DETAIL_DO_NOT_FORWARD")
    with pytest.raises(CodexUnavailable) as raised:
        LocalCodexClient(callback).capabilities()
    assert "SYNTHETIC_PRIVATE_DETAIL" not in str(raised.value)


def test_completed_native_result_is_collected_before_a_new_send_replaces_it() -> None:
    native = Native()
    client = LocalCodexClient(native)
    client.run(**request())
    native.state, native.text = "completed", "First synthetic final answer."
    assert client.run(**request(run_id="run-2"))["accepted"] is True
    assert client.status("run-1")["final_output"] == "First synthetic final answer."
    operations = [operation for operation, _ in native.calls]
    assert operations.index("codex_poll") < len(operations) - 1
    assert len(native.starts) == 2


def test_last_request_error_does_not_override_native_ready_capability() -> None:
    native = Native()
    def callback(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = native(operation, payload)
        if operation == "codex_status":
            result["error"] = {"code": "codex_process_failed", "message": "Previous request failed."}
        return result
    client = LocalCodexClient(callback)
    assert client.capabilities()["codex"]["available"] is True
    assert client.run(**request())["accepted"] is True


def test_real_phase1_gateway_call_envelope_is_compatible_without_stdio_or_processes() -> None:
    native = Native()
    frames: list[dict[str, Any]] = []
    def emit(frame: dict[str, Any]) -> None:
        frames.append(frame)
        calls.resolve({"op": "gateway_result", "callId": frame["callId"], "ok": True,
                       "result": native(frame["operation"], frame["payload"])})
    calls = GatewayCalls(emit, timeout=0.5)
    client = LocalCodexClient(calls.call)
    assert client.run(**request())["accepted"] is True
    native.state, native.text = "completed", "Synthetic bridged answer."
    assert client.status("run-1")["final_output"] == native.text
    assert all(frame["event"] == "gateway" for frame in frames)
    assert len({frame["callId"] for frame in frames}) == len(frames)


def test_bridge_start_timeout_is_ambiguous_and_cannot_be_retried_silently() -> None:
    native = Native()
    def callback(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation == "codex_start":
            raise GatewayCallError("gateway_timeout", "SYNTHETIC_DETAIL_NOT_FOR_OUTPUT")
        return native(operation, payload)
    client = LocalCodexClient(callback)
    with pytest.raises(CodexUnavailable):
        client.run(**request())
    assert client.capabilities()["codex"]["available"] is False
    with pytest.raises(CodexUnavailable):
        client.run(**request(run_id="run-2"))


def test_terminal_record_eviction_never_polls_or_cancels_a_new_native_run_as_the_old_one() -> None:
    native = Native()
    client = LocalCodexClient(native, max_records=1)
    client.run(**request())
    native.state, native.text = "completed", "First synthetic final."
    assert client.run(**request(run_id="run-2"))["status"] == "queued"
    assert native.serial == 2
    previous_calls = len(native.calls)
    with pytest.raises(CodexUnavailable):
        client.status("run-1")
    assert len(native.calls) == previous_calls
    assert client.cancel("run-2")["status"] == "cancelled"
    assert native.calls[-1] == ("codex_cancel", {"requestId": "native-2"})


def test_async_callback_uses_its_existing_reader_loop_from_a_worker() -> None:
    async def exercise() -> None:
        native = Native()
        async def callback(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
            await asyncio.sleep(0)
            return native(operation, payload)
        client = LocalCodexClient(callback)
        result = await asyncio.to_thread(client.run, **request())
        assert result["accepted"] is True and len(native.starts) == 1
        with pytest.raises(CodexUnavailable, match="worker"):
            client.capabilities()
    asyncio.run(exercise())


def test_async_start_timeout_blocks_further_starts() -> None:
    async def exercise() -> None:
        native = Native()
        async def callback(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
            if operation == "codex_start":
                await asyncio.Event().wait()
            return native(operation, payload)
        client = LocalCodexClient(callback, timeout_seconds=0.02)
        with pytest.raises(CodexUnavailable):
            await asyncio.to_thread(client.run, **request())
        assert (await asyncio.to_thread(client.capabilities))["codex"]["available"] is False
        with pytest.raises(CodexUnavailable):
            await asyncio.to_thread(client.run, **request(run_id="run-2"))
        await asyncio.sleep(0)
    asyncio.run(exercise())


def test_loop_caller_fails_before_waiting_on_a_worker_lock() -> None:
    async def exercise() -> None:
        native = Native()
        entered, release = asyncio.Event(), asyncio.Event()
        async def callback(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
            if operation == "codex_status":
                entered.set()
                await release.wait()
            return native(operation, payload)
        client = LocalCodexClient(callback)
        worker = asyncio.create_task(asyncio.to_thread(client.run, **request()))
        await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            with pytest.raises(CodexUnavailable, match="worker"):
                client.capabilities()
        finally:
            release.set()
        assert (await asyncio.wait_for(worker, timeout=2))["accepted"] is True
    asyncio.run(exercise())


@pytest.mark.parametrize("field,value", [("state", []), ("loginState", {}), ("manualModels", [123])])
def test_malformed_native_status_is_a_safe_protocol_error(field: str, value: Any) -> None:
    native = Native()
    def callback(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = native(operation, payload)
        result[field] = value
        return result
    with pytest.raises(CodexProtocolError):
        LocalCodexClient(callback).capabilities()
    assert native.starts == []

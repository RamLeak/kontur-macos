"""Synthetic transport tests: no socket, subprocess, model or real profile."""

import asyncio
import base64
import io
import json
import time

import pytest

from offline_app import ASGIAdapter
from offline_bridge import Dispatcher, read_frame, safe_error
from offline_protocol import (
    MAX_BODY_BYTES,
    BridgeError,
    GatewayCalls,
    JsonlWriter,
    quiesce_gateway,
    validate_headers,
    validate_request,
)


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Synthetic operation did not finish")


def frames(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


@pytest.mark.parametrize(
    "path",
    [
        "https://example.test/api/v1/a",
        "/api/v1/../a",
        "/api/v1/%2e%2e/a",
        "/api/v1/a#x",
        "/api/v1/a\\b",
        "/api/v1/a?owner_id=other",
    ],
)
def test_invalid_private_paths_are_rejected(path):
    with pytest.raises(BridgeError):
        validate_request("GET", path)


def test_owner_and_auth_headers_cannot_be_selected_by_browser():
    for headers in (
        {"Authorization": "x"},
        {"Cookie": "x"},
        {"X-Sync-Owner": "other"},
        {"Idempotency-Key": "a", "idempotency-key": "b"},
    ):
        with pytest.raises(BridgeError):
            validate_headers(headers)
    assert validate_headers({"Idempotency-Key": "retry-1", "X-Sync-Owner": "owner"}) == [
        (b"idempotency-key", b"retry-1"),
        (b"x-sync-owner", b"owner"),
    ]
    with pytest.raises(BridgeError):
        validate_request("POST", "/api/v1/projects", '{"owner_id":"other"}')


@pytest.mark.parametrize("data", [b"[]\n", b'{"id":NaN}\n', b'{"id":1}', b"\xff\n"])
def test_malformed_frames_fail_closed(data):
    with pytest.raises(BridgeError):
        read_frame(io.BytesIO(data))


def test_asgi_roundtrip_preserves_binary_headers_and_private_context():
    marker = object()
    observed = {}

    async def app(scope, receive, send):
        observed.update(scope)
        assert scope["kontur.private_process"] is marker
        observed["request"] = await receive()
        await send(
            {
                "type": "http.response.start",
                "status": 201,
                "headers": [(b"content-type", b"application/octet-stream"), (b"set-cookie", b"forbidden")],
            }
        )
        await send({"type": "http.response.body", "body": b"\x00\xffbinary"})

    result = asyncio.run(
        ASGIAdapter(app, marker).request(
            "POST", "/api/v1/test?q=1", '{"text":"данные"}', {"Idempotency-Key": "retry-1"}
        )
    )
    assert result["status"] == 201
    assert base64.b64decode(result["bodyBase64"]) == b"\x00\xffbinary"
    assert ("set-cookie", "forbidden") not in result["headers"]
    assert (b"idempotency-key", b"retry-1") in observed["headers"]
    assert json.loads(observed["request"]["body"]) == {"text": "данные"}


def test_stream_response_is_not_disconnected_before_last_chunk():
    async def app(scope, receive, send):
        await receive()
        disconnected = asyncio.create_task(receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"event: one\n", "more_body": True})
        await asyncio.sleep(0)
        assert not disconnected.done()
        await send({"type": "http.response.body", "body": b"data: two\n\n", "more_body": False})
        assert (await disconnected)["type"] == "http.disconnect"

    result = asyncio.run(ASGIAdapter(app, object()).request("GET", "/api/v1/events"))
    assert base64.b64decode(result["bodyBase64"]) == b"event: one\ndata: two\n\n"


def test_reverse_reply_stays_live_while_flush_drains_inflight_request():
    stream = io.BytesIO()
    writer = JsonlWriter(stream)
    gateway = GatewayCalls(writer.write, timeout=2)

    class Application:
        def request(self, *_args):
            result = gateway.call("codex_status", {})
            assert result == {"available": False}
            return {"status": 200, "headers": [], "bodyBase64": ""}

        def flush(self):
            return {"committed": True, "pendingMutations": 0}

    dispatcher = Dispatcher(Application(), writer, gateway)
    dispatcher.submit({"id": 1, "op": "request", "method": "GET", "path": "/api/v1/test"})
    wait_for(lambda: any(item.get("event") == "gateway" for item in frames(stream)))
    event = next(item for item in frames(stream) if item.get("event") == "gateway")
    dispatcher.submit({"id": 2, "op": "flush"})
    dispatcher.submit({"id": 3, "op": "request", "method": "GET", "path": "/api/v1/test"})
    dispatcher.submit({"op": "gateway_result", "callId": event["callId"], "ok": True, "result": {"available": False}})
    wait_for(lambda: len([item for item in frames(stream) if "id" in item]) == 3)
    responses = {item["id"]: item for item in frames(stream) if "id" in item}
    assert responses[1]["ok"] is True
    assert responses[2]["result"] == {"committed": True, "pendingMutations": 0}
    assert responses[3]["error"]["code"] == "backend_busy"
    dispatcher.close()


def test_safe_errors_never_echo_raw_exception_content():
    assert "PRIVATE" not in json.dumps(safe_error(RuntimeError("PRIVATE DATA")))


def test_request_bound_includes_original_large_history_and_rejects_nonfinite():
    assert (
        validate_request("PUT", "/api/v1/local/client-state", json.dumps({"history": "a" * (4 * 1024**2)}))[0] == "PUT"
    )
    with pytest.raises(BridgeError) as too_large:
        validate_request("PUT", "/api/v1/local/client-state", json.dumps({"history": "a" * MAX_BODY_BYTES}))
    assert too_large.value.code == "body_too_large"
    with pytest.raises(BridgeError):
        validate_request("PUT", "/api/v1/local/client-state", '{"value":NaN}')


@pytest.mark.parametrize("cancel_state", ["running", "cancelled"])
def test_flush_cancels_orphaned_native_start_and_requires_terminal_state(cancel_state):
    calls = []
    active = "orphaned-native"

    def call(operation, payload):
        nonlocal active
        calls.append(operation)
        if operation == "codex_status":
            return {"gatewayProtocol": 1, "state": "ready", "activeRequestId": active}
        assert payload == {"requestId": "orphaned-native"}
        if cancel_state == "cancelled":
            active = None
        return {"requestId": "orphaned-native", "state": cancel_state}

    if cancel_state == "cancelled":
        quiesce_gateway(call)
        assert calls == ["codex_status", "codex_cancel", "codex_status"]
    else:
        with pytest.raises(BridgeError) as pending:
            quiesce_gateway(call, timeout=0.04)
        assert pending.value.code == "flush_pending_ai"


def test_native_cancel_acknowledgement_waits_for_poll_terminal():
    calls = []

    def call(operation, _payload):
        calls.append(operation)
        if operation == "codex_status":
            return {
                "gatewayProtocol": 1,
                "state": "ready",
                "activeRequestId": "async-native" if len(calls) == 1 else None,
            }
        return {"requestId": "async-native", "state": "running" if operation == "codex_cancel" else "cancelled"}

    quiesce_gateway(call)
    assert calls == ["codex_status", "codex_cancel", "codex_poll", "codex_status"]

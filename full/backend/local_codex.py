"""Original synchronous gateway API over an application-owned native callback.

No process, network, profile or database access lives here. Native gateway
protocol 1 is required; the legacy question/code/statement DTO fails closed.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.codex_gateway import (
    ALLOWED_ROUTES,
    CodexProtocolError,
    CodexResponse,
    CodexUnavailable,
    HttpCodexClient,
    _request_digest,
)
from app.util import canonical_json
from offline_protocol import GatewayCallError

GatewayCall = Callable[[str, dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]]
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_TERMINAL = frozenset({"succeeded", "failed", "timed_out", "cancelled", "unavailable"})
_NATIVE_STATES = frozenset({"starting", "running", "completed", "cancelled", "failed"})
_NATIVE_ERRORS = frozenset({
    "codex_not_found", "codex_incompatible", "codex_unsafe_configuration", "codex_request_invalid",
    "codex_busy", "codex_not_running", "codex_cancelled", "codex_timeout", "codex_output_limit",
    "codex_tool_event", "codex_protocol", "codex_process_failed", "codex_login_failed",
    "codex_cancel_failed", "codex_spawn_failed", "codex_runtime_unavailable", "codex_unavailable",
})
_PROMPT_LIMIT = 500_000
_FINAL_LIMIT = 128 * 1024
_STATUS_WAIT_SECONDS = 5.0
_STATUS_POLL_SECONDS = 0.05
_NATIVE_API_CODES = {
    "codex_timeout": "AGENT_TIMED_OUT",
    "codex_protocol": "AGENT_PROTOCOL_ERROR",
    "codex_tool_event": "AGENT_PROTOCOL_ERROR",
    "codex_output_limit": "AGENT_PROTOCOL_ERROR",
    "codex_process_failed": "AGENT_FAILED",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _require_id(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise CodexProtocolError("Local Codex identifier is invalid")
    return value


def _require_worker() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise CodexUnavailable("Synchronous Local Codex requires a request worker")


class _NativeFailure(CodexUnavailable):
    def __init__(self, code: str, *, retryable_before_accept: bool = False) -> None:
        self.native_code = code if code in _NATIVE_ERRORS else "codex_unavailable"
        self.public_code = _NATIVE_API_CODES.get(self.native_code, "AI_UNAVAILABLE")
        self.retryable_before_accept = retryable_before_accept
        super().__init__("Local Codex callback failed")


@dataclass(slots=True)
class _Run:
    run_id: str
    input_digest: str
    manifest_hash: str
    prompt_hash: str
    model: str
    effort: str
    tutor_mode: str
    native_id: str | None = None
    status: str = "queued"
    error_code: str | None = None
    final_output: str = ""
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    completed_at: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def event(self, event_type: str) -> None:
        # Only lifecycle transitions are synthesized, never reasoning/tool data.
        if len(self.events) >= 512:
            raise CodexProtocolError("Local Codex event limit exceeded")
        self.events.append({"sequence": len(self.events) + 1, "type": event_type, "created_at": _now()})

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": 1, "run_id": self.run_id, "input_digest": self.input_digest,
            "manifest_hash": self.manifest_hash, "prompt_hash": self.prompt_hash,
            "model": self.model, "effort": self.effort, "tutor_mode": self.tutor_mode,
            "status": self.status, "error_code": self.error_code, "final_output": self.final_output,
            "created_at": self.created_at, "started_at": self.started_at,
            "completed_at": self.completed_at, "event_count": len(self.events),
        }


class LocalCodexClient(HttpCodexClient):
    """Keep original validation/results; replace only its private transport seam.

    Sync FastAPI handlers call this on workers. An asynchronous callback needs
    its live reader loop, captured at construction or supplied explicitly.
    """

    transport_kind = "native-codex"

    def __init__(
        self,
        gateway_call: GatewayCall,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        timeout_seconds: float = 20.0,
        max_response_bytes: int = 1_000_000,
        max_records: int = 128,
        status_wait_seconds: float = _STATUS_WAIT_SECONDS,
        status_poll_seconds: float = _STATUS_POLL_SECONDS,
    ) -> None:
        if not callable(gateway_call):
            raise ValueError("gateway_call must be callable")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("timeout_seconds must be finite")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be between zero and 60")
        for name, value in (("status_wait_seconds", status_wait_seconds), ("status_poll_seconds", status_poll_seconds)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be finite")
            if not math.isfinite(value) or not 0 < value <= 60:
                raise ValueError(f"{name} must be between zero and 60")
        if type(max_records) is not int or not 1 <= max_records <= 128:
            raise ValueError("max_records must be between 1 and 128")
        if type(max_response_bytes) is not int or not 1024 <= max_response_bytes <= 2_000_000:
            raise ValueError("max_response_bytes is invalid")
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                pass
        # Do not call the HTTP constructor: no URL, credential, transport or
        # environment-derived Settings is needed at this local boundary.
        self._gateway_call = gateway_call
        self._loop = loop
        self.timeout_seconds = float(timeout_seconds)
        self.status_wait_seconds = float(status_wait_seconds)
        self.status_poll_seconds = float(status_poll_seconds)
        self.max_response_bytes = max_response_bytes
        self._max_records = max_records
        self._lock = threading.RLock()
        self._runs: OrderedDict[str, _Run] = OrderedDict()
        self._uncertain_start = False

    def _call(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        _require_worker()
        future = None
        try:
            result = self._gateway_call(operation, payload)
            if inspect.isawaitable(result):
                if self._loop is None or not self._loop.is_running():
                    if inspect.iscoroutine(result):
                        result.close()
                    raise _NativeFailure("codex_unavailable")
                awaitable_result = result

                async def receive() -> dict[str, Any]:
                    return await awaitable_result

                future = asyncio.run_coroutine_threadsafe(receive(), self._loop)
                result = future.result(timeout=self.timeout_seconds)
            if not isinstance(result, dict):
                raise CodexProtocolError("Local Codex result must be an object")
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded) > self.max_response_bytes:
                raise CodexProtocolError("Local Codex response limit exceeded")
            return json.loads(encoded)
        except GatewayCallError as exc:
            # A lost bridge reply can hide a successful native start. Do not
            # issue another start until the owning process is restarted.
            if operation == "codex_start" and exc.code not in _NATIVE_ERRORS:
                self._uncertain_start = True
            code = "codex_timeout" if exc.code == "gateway_timeout" else exc.code
            retryable = operation == "codex_start" and code == "codex_busy"
            raise _NativeFailure(code, retryable_before_accept=retryable) from None
        except (CodexProtocolError, _NativeFailure):
            if operation == "codex_start":
                self._uncertain_start = True
            raise
        except (FutureTimeoutError, TimeoutError):
            if future is not None:
                future.cancel()
            if operation == "codex_start":
                self._uncertain_start = True
            raise _NativeFailure("codex_timeout") from None
        except Exception:
            if operation == "codex_start":
                self._uncertain_start = True
            raise _NativeFailure("codex_unavailable") from None

    def _native_status(self) -> dict[str, Any]:
        allowed = {"state", "cliVersion", "loginState", "manualModelSupported", "manualModels",
                   "activeRequestId", "error", "gatewayProtocol"}
        deadline = time.monotonic() + self.status_wait_seconds
        while True:
            value = self._call("codex_status", {})
            if (set(value) - allowed or not isinstance(value.get("state"), str)
                    or value["state"] not in {"checking", "ready", "unavailable"}):
                raise CodexProtocolError("Local Codex status is invalid")
            if (not isinstance(value.get("loginState"), str)
                    or value["loginState"] not in {"unknown", "inProgress", "required", "ready"}):
                raise CodexProtocolError("Local Codex login status is invalid")
            if type(value.get("manualModelSupported")) is not bool or not isinstance(value.get("manualModels"), list):
                raise CodexProtocolError("Local Codex model status is invalid")
            if len(value["manualModels"]) > 128 or any(
                not isinstance(item, str) or len(item) > 128 or "\0" in item for item in value["manualModels"]
            ):
                raise CodexProtocolError("Local Codex model status exceeds limits")
            if value.get("activeRequestId") is not None:
                _require_id(value["activeRequestId"])
            if value["state"] != "checking":
                return value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return value
            time.sleep(min(self.status_poll_seconds, remaining))

    @staticmethod
    def _native_status_error_code(value: dict[str, Any]) -> str:
        error = value.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        return code if isinstance(code, str) and code in _NATIVE_ERRORS else "codex_unavailable"

    @staticmethod
    def _gateway_ready(value: dict[str, Any]) -> bool:
        return (value["state"] == "ready" and value["manualModelSupported"]
                and type(value.get("gatewayProtocol")) is int and value["gatewayProtocol"] == 1)

    def _response(self, status: int, value: dict[str, Any]) -> CodexResponse:
        body = canonical_json(value).encode("utf-8")
        if len(body) > self.max_response_bytes:
            raise CodexProtocolError("Local gateway response exceeds limits")
        return CodexResponse(status, {"Content-Type": "application/json", "Cache-Control": "no-store"}, body)

    def _capabilities(self) -> CodexResponse:
        native = self._native_status()
        ready = self._gateway_ready(native) and not self._uncertain_start
        reason = "LOCAL_CONTRACT_READY_AUTH_UNVERIFIED" if ready else "NATIVE_GATEWAY_PROTOCOL_REQUIRED"
        if native["state"] == "unavailable":
            reason = "NATIVE_CODEX_UNAVAILABLE"
        return self._response(200, {"schema_version": 1, "codex": {
            "status": "ready" if ready else "unavailable", "available": ready,
            "reason": reason,
            "executable": None, "cli_contract": "native-codex-gateway-v1",
            "routes": [{"model": model, "effort": effort} for model, effort in sorted(ALLOWED_ROUTES)],
            "limits": {"max_prompt_bytes": _PROMPT_LIMIT, "max_wall_seconds": 120,
                       "max_inactivity_seconds": 30, "max_stream_bytes": 512 * 1024,
                       "max_events": 512, "max_final_bytes": _FINAL_LIMIT, "max_concurrent_runs": 1},
        }})

    def _start_native(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._call("codex_start", request)
        except _NativeFailure as exc:
            if exc.native_code != "codex_busy" or not exc.retryable_before_accept or self._uncertain_start:
                raise
            native = self._native_status()
            if native["state"] == "checking":
                raise _NativeFailure("codex_unavailable", retryable_before_accept=True) from None
            if native["state"] == "unavailable":
                raise _NativeFailure(self._native_status_error_code(native)) from None
            if native.get("activeRequestId"):
                raise _NativeFailure("codex_busy", retryable_before_accept=True) from None
            if not self._gateway_ready(native):
                raise _NativeFailure("codex_unavailable") from None
            return self._call("codex_start", request)

    def _transition(self, run: _Run, state: str, error_code: str | None = None) -> None:
        if run.status in _TERMINAL or run.status == state:
            return
        run.status = state
        run.error_code = error_code
        if state == "running":
            run.started_at = run.started_at or _now()
        if state in _TERMINAL:
            run.completed_at = _now()
        run.event(state)

    def _apply(self, run: _Run, value: dict[str, Any]) -> None:
        allowed = {"requestId", "state", "text", "model", "error", "effort"}
        if (set(value) - allowed or not isinstance(value.get("state"), str)
                or value["state"] not in _NATIVE_STATES):
            raise CodexProtocolError("Local Codex lifecycle is invalid")
        native_id = _require_id(value.get("requestId"))
        if run.native_id is not None and run.native_id != native_id:
            raise CodexProtocolError("Local Codex request identity changed")
        if value.get("model") != {"mode": "manual", "id": run.model} or value.get("effort") != run.effort:
            raise CodexProtocolError("Local Codex route changed")
        text = value.get("text")
        if not isinstance(text, str) or "\0" in text or len(text.encode("utf-8")) > _FINAL_LIMIT:
            raise CodexProtocolError("Local Codex text is invalid")
        error = value.get("error")
        if error is not None and (not isinstance(error, dict) or set(error) != {"code", "message"}
                                  or not isinstance(error.get("code"), str)
                                  or error.get("code") not in _NATIVE_ERRORS
                                  or not isinstance(error.get("message"), str)):
            raise CodexProtocolError("Local Codex error is invalid")
        state = value["state"]
        if state in {"starting", "running", "completed"} and error is not None:
            raise CodexProtocolError("Local Codex lifecycle error is inconsistent")
        run.native_id = native_id
        if state == "completed":
            run.final_output = text
            self._transition(run, "succeeded")
        elif state == "cancelled":
            self._transition(run, "cancelled", "AGENT_CANCELLED")
        elif state == "failed":
            if not isinstance(error, dict):
                raise CodexProtocolError("Local Codex failure lacks a safe code")
            self._fail(run, error["code"])
        elif run.status != "cancelling":
            self._transition(run, "queued" if state == "starting" else "running")

    def _fail(self, run: _Run, code: str) -> None:
        if code == "codex_timeout":
            self._transition(run, "timed_out", "AGENT_TIMED_OUT")
        elif code == "codex_cancelled":
            self._transition(run, "cancelled", "AGENT_CANCELLED")
        elif code in {"codex_protocol", "codex_tool_event", "codex_output_limit", "codex_cancel_failed",
                      "codex_request_invalid"}:
            if code == "codex_cancel_failed":
                self._uncertain_start = True
            self._transition(run, "failed", "AGENT_PROTOCOL_ERROR")
        elif code == "codex_process_failed":
            self._transition(run, "failed", "AGENT_FAILED")
        else:
            self._transition(run, "unavailable", "CODEX_UNAVAILABLE")

    def _create(self, payload: dict[str, Any]) -> CodexResponse:
        run_id = _require_id(payload.get("run_id"))
        digest = _request_digest(
            run_id=run_id, manifest_hash=payload["manifest_sha256"], prompt_hash=payload["prompt_sha256"],
            model=payload["model"], effort=payload["effort"], tutor_mode=payload["tutor_mode"],
            wall_seconds=120, inactivity_seconds=30,
        )
        previous = self._runs.get(run_id)
        if previous is not None:
            if previous.input_digest != digest:
                return self._response(409, {"schema_version": 1, "code": "IDEMPOTENCY_CONFLICT"})
            return self._response(200, previous.snapshot())
        if self._uncertain_start:
            raise CodexUnavailable("Local Codex startup is unconfirmed; restart is required")
        native = self._native_status()
        if native["state"] == "checking":
            raise _NativeFailure("codex_unavailable", retryable_before_accept=True)
        if native["state"] == "unavailable":
            raise _NativeFailure(self._native_status_error_code(native))
        if not self._gateway_ready(native):
            raise CodexUnavailable("Native gateway protocol 1 is required to preserve context and effort")
        if len(payload["rendered_prompt"].encode("utf-8")) > _PROMPT_LIMIT:
            raise CodexProtocolError("Context exceeds the native gateway prompt limit")
        pending = [run for run in self._runs.values() if run.status not in _TERMINAL]
        if pending:
            # Native retains only its latest request. Collect an earlier final
            # result before a new explicit Send is allowed to replace that slot.
            for active in pending:
                self._request("GET", f"/v1/runs/{active.run_id}")
            native = self._native_status()
            if native["state"] == "unavailable":
                raise _NativeFailure(self._native_status_error_code(native))
            if native["state"] == "checking":
                raise _NativeFailure("codex_unavailable", retryable_before_accept=True)
            if not self._gateway_ready(native) or self._uncertain_start:
                raise CodexUnavailable("Local Codex is unavailable after reconciliation")
        if native.get("activeRequestId") or any(run.status not in _TERMINAL for run in self._runs.values()):
            raise CodexUnavailable("Local Codex already has an active request")
        if len(self._runs) >= self._max_records:
            oldest = next((key for key, run in self._runs.items() if run.status in _TERMINAL), None)
            if oldest is None:
                raise CodexUnavailable("Local Codex record limit reached")
            self._runs.pop(oldest)
        run = _Run(run_id, digest, payload["manifest_sha256"], payload["prompt_sha256"],
                   payload["model"], payload["effort"], payload["tutor_mode"])
        run.event("queued")
        self._runs[run_id] = run
        request = {"question": "Kontur tutor request", "code": None, "statement": None,
                   "model": {"mode": "manual", "id": run.model},
                   "gateway": {"renderedPrompt": payload["rendered_prompt"], "effort": run.effort}}
        try:
            self._apply(run, self._start_native(request))
        except _NativeFailure as exc:
            if exc.retryable_before_accept:
                self._runs.pop(run_id, None)
            else:
                self._fail(run, exc.native_code)
            raise
        except CodexProtocolError:
            self._uncertain_start = True
            self._fail(run, "codex_protocol")
            raise
        return self._response(202, run.snapshot())

    def _request(self, method: str, path: str, body: bytes = b"") -> CodexResponse:
        # A reader loop must not wait for a lock held by a worker awaiting that
        # same loop's callback. Reject before entering the shared state lock.
        _require_worker()
        with self._lock:
            if method == "GET" and path == "/v1/capabilities":
                return self._capabilities()
            if method == "POST" and path == "/v1/runs":
                try:
                    payload = json.loads(body.decode("utf-8"))
                except (UnicodeError, ValueError):
                    raise CodexProtocolError("Local gateway request is invalid") from None
                if not isinstance(payload, dict):
                    raise CodexProtocolError("Local gateway request must be an object")
                return self._create(payload)
            match = re.fullmatch(r"/v1/runs/([A-Za-z0-9_.:-]+)(?:(/cancel)|/events\?after=(\d+))?", path)
            if not match:
                raise CodexProtocolError("Local gateway operation is invalid")
            run = self._runs.get(_require_id(match[1]))
            if run is None:
                raise CodexUnavailable("Local Codex run is not available")
            if method == "GET" and match[3] is not None:
                after = int(match[3])
                events = [dict(item) for item in run.events if item["sequence"] > after]
                return self._response(200, {"schema_version": 1, "run_id": run.run_id,
                                           "events": events, "next_after": max(after, len(run.events))})
            if (method, bool(match[2])) not in {("GET", False), ("POST", True)}:
                raise CodexProtocolError("Local gateway method is invalid")
            if run.status not in _TERMINAL:
                if run.native_id is None:
                    raise CodexProtocolError("Local Codex run has no native identity")
                operation = "codex_cancel" if match[2] else "codex_poll"
                if match[2]:
                    self._transition(run, "cancelling")
                try:
                    self._apply(run, self._call(operation, {"requestId": run.native_id}))
                except _NativeFailure as exc:
                    self._fail(run, exc.native_code)
                except CodexProtocolError:
                    self._uncertain_start = True
                    self._fail(run, "codex_protocol")
                    raise
            return self._response(200, run.snapshot())

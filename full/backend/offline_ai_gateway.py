"""Typed reverse-gateway adapter for the root-owned OpenCode native job.

This adapter mirrors the existing synchronous ``LocalCodexClient`` seam while
keeping process controls in native code.  Python emits only fixed
``opencode_*`` operations with a validated tutor packet; it cannot pass a
binary path, argv, environment, cwd, shell command, or tool request.
"""

from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from offline_ai import (
    MAX_OUTPUT_BYTES,
    AIProtocolError,
    AIUnavailable,
    ProviderConfig,
    TutorDomainPacket,
    build_tutor_packet,
)
from offline_ai_opencode import OpenCodeRequestSpec, build_opencode_request

OPEN_CODE_GATEWAY_OPERATIONS = frozenset(
    {"opencode_status", "opencode_start", "opencode_poll", "opencode_cancel"}
)
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MODEL_RE = re.compile(r"[^\x00-\x1f\x7f\s]{1,512}\Z")
_STATES = frozenset({"starting", "running", "completed", "cancelled", "failed"})
_TERMINAL = frozenset({"succeeded", "failed", "timed_out", "cancelled", "unavailable"})
_ERROR_CODES = frozenset(
    {
        "opencode_not_found",
        "opencode_incompatible",
        "opencode_unsafe_configuration",
        "opencode_request_invalid",
        "opencode_busy",
        "opencode_not_running",
        "opencode_cancelled",
        "opencode_timeout",
        "opencode_output_limit",
        "opencode_tool_event",
        "opencode_protocol",
        "opencode_process_failed",
        "opencode_cancel_failed",
        "opencode_spawn_failed",
        "opencode_unavailable",
    }
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise AIProtocolError(f"OpenCode {field} is invalid")
    return value


def _model(value: Any) -> str:
    if not isinstance(value, str) or not _MODEL_RE.fullmatch(value) or "/" not in value:
        raise AIProtocolError("OpenCode provider model is invalid")
    return value


def _safe_text(value: Any) -> str:
    if not isinstance(value, str) or "\x00" in value or len(value.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise AIProtocolError("OpenCode text exceeds the byte limit")
    return value


def _start_payload(request: OpenCodeRequestSpec) -> dict[str, Any]:
    """Return the only payload admitted to ``opencode_start``."""

    payload = request.to_native_job()
    if set(payload) != {
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
    }:
        raise AIProtocolError("OpenCode request payload is invalid")
    if payload["textOnly"] is not True:
        raise AIProtocolError("OpenCode text-only mode is required")
    _id(payload["runId"], "run id")
    _model(payload["providerModel"])
    _id(payload["providerId"], "provider id")
    if payload["endpoint"] is not None and (
        not isinstance(payload["endpoint"], str)
        or "\x00" in payload["endpoint"]
        or len(payload["endpoint"].encode("utf-8")) > 2_048
    ):
        raise AIProtocolError("OpenCode endpoint is invalid")
    if payload["apiKey"] is not None and (
        not isinstance(payload["apiKey"], str)
        or not payload["apiKey"]
        or "\x00" in payload["apiKey"]
        or any(ord(char) < 32 or ord(char) == 127 for char in payload["apiKey"])
        or len(payload["apiKey"].encode("utf-8")) > 4_096
    ):
        raise AIProtocolError("OpenCode API key is invalid")
    if payload["effort"] not in {"low", "medium", "high", "xhigh"}:
        raise AIProtocolError("OpenCode effort is invalid")
    if not isinstance(payload["renderedPrompt"], str) or not payload["renderedPrompt"]:
        raise AIProtocolError("OpenCode prompt is invalid")
    for key in ("manifestSha256", "promptSha256", "inputDigest"):
        value = payload[key]
        if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise AIProtocolError("OpenCode context digest is invalid")
    return payload


@dataclass(slots=True)
class _Run:
    run_id: str
    packet: TutorDomainPacket
    provider_model: str
    provider_id: str
    provider_kind: str
    native_id: str | None = None
    status: str = "queued"
    error_code: str | None = None
    final_output: str = ""
    created_at: str = field(default_factory=_now)
    completed_at: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def event(self, event_type: str) -> None:
        if len(self.events) >= 512:
            raise AIProtocolError("OpenCode event limit exceeded")
        self.events.append({"sequence": len(self.events) + 1, "type": event_type, "created_at": _now()})

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "status": self.status,
            "error_code": self.error_code,
            "final_output": self.final_output,
            "model": self.packet.model,
            "effort": self.packet.effort,
            "provider_model": self.provider_model,
            "provider_info": {
                "id": self.provider_id,
                "kind": self.provider_kind,
                "model": self.provider_model,
                "effort": self.packet.effort,
            },
            "manifest_hash": self.packet.manifest_sha256,
            "prompt_hash": self.packet.prompt_sha256,
            "input_digest": self.packet.input_digest,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "event_count": len(self.events),
        }


class OpenCodeGatewayClient:
    """LocalCodexClient-compatible adapter over fixed reverse-gateway calls."""

    transport_kind = "native-opencode"

    def __init__(
        self,
        gateway_call: Callable[[str, dict[str, Any]], dict[str, Any]],
        *,
        max_records: int = 128,
    ) -> None:
        if not callable(gateway_call):
            raise ValueError("gateway_call must be callable")
        if type(max_records) is not int or not 1 <= max_records <= 128:
            raise ValueError("max_records must be between 1 and 128")
        self._gateway_call = gateway_call
        self._max_records = max_records
        self._runs: OrderedDict[str, _Run] = OrderedDict()
        self._lock = threading.RLock()
        self._uncertain_start = False

    def _call(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation not in OPEN_CODE_GATEWAY_OPERATIONS or not isinstance(payload, dict):
            raise AIProtocolError("OpenCode gateway operation is invalid")
        try:
            result = self._gateway_call(operation, payload)
        except Exception as exc:
            raise AIUnavailable("OpenCode native gateway is unavailable") from exc
        if not isinstance(result, dict):
            raise AIProtocolError("OpenCode native gateway result is invalid")
        try:
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded) > 2_000_000:
                raise AIProtocolError("OpenCode native gateway result exceeds the byte limit")
            return json.loads(encoded)
        except (TypeError, ValueError, RecursionError) as exc:
            raise AIProtocolError("OpenCode native gateway result is invalid") from exc

    @staticmethod
    def _status_value(value: dict[str, Any]) -> dict[str, Any]:
        allowed = {"state", "cliVersion", "activeRequestId", "error", "gatewayProtocol", "models"}
        if set(value) - allowed or value.get("state") not in {"checking", "ready", "unavailable"}:
            raise AIProtocolError("OpenCode native status is invalid")
        if value.get("activeRequestId") is not None:
            _id(value["activeRequestId"], "request id")
        if value.get("cliVersion") is not None and not isinstance(value["cliVersion"], str):
            raise AIProtocolError("OpenCode CLI version is invalid")
        if value.get("gatewayProtocol") is not None and type(value["gatewayProtocol"]) is not int:
            raise AIProtocolError("OpenCode gateway protocol is invalid")
        models = value.get("models", [])
        if not isinstance(models, list) or len(models) > 256 or any(not isinstance(item, str) for item in models):
            raise AIProtocolError("OpenCode model list is invalid")
        return value

    @staticmethod
    def _native_value(value: dict[str, Any], run: _Run) -> dict[str, Any]:
        allowed = {"requestId", "state", "text", "model", "effort", "error"}
        if set(value) - allowed or value.get("state") not in _STATES:
            raise AIProtocolError("OpenCode lifecycle is invalid")
        native_id = _id(value.get("requestId"), "request id")
        if run.native_id is not None and run.native_id != native_id:
            raise AIProtocolError("OpenCode request identity changed")
        expected_model = {"mode": "manual", "id": run.provider_model}
        if value.get("model") != expected_model or value.get("effort") != run.packet.effort:
            raise AIProtocolError("OpenCode route changed")
        text = _safe_text(value.get("text", ""))
        error = value.get("error")
        if error is not None:
            if (
                not isinstance(error, dict)
                or set(error) != {"code", "message"}
                or error.get("code") not in _ERROR_CODES
                or not isinstance(error.get("message"), str)
            ):
                raise AIProtocolError("OpenCode error is invalid")
        run.native_id = native_id
        state = value["state"]
        if state == "completed":
            run.final_output = text
            run.status = "succeeded"
            run.completed_at = _now()
            run.event("succeeded")
        elif state == "cancelled":
            run.status = "cancelled"
            run.error_code = "AGENT_CANCELLED"
            run.completed_at = _now()
            run.event("cancelled")
        elif state == "failed":
            code = error["code"] if isinstance(error, dict) else "opencode_protocol"
            if code == "opencode_timeout":
                run.status, run.error_code = "timed_out", "AGENT_TIMED_OUT"
            elif code == "opencode_process_failed":
                run.status, run.error_code = "failed", "AGENT_FAILED"
            elif code in {"opencode_unavailable", "opencode_not_found", "opencode_incompatible"}:
                run.status, run.error_code = "unavailable", "OPENCODE_UNAVAILABLE"
            else:
                run.status, run.error_code = "failed", "AGENT_PROTOCOL_ERROR"
            run.completed_at = _now()
            run.event(run.status)
        elif run.status not in _TERMINAL:
            run.status = "queued" if state == "starting" else "running"
            run.event(run.status)
        return run.snapshot()

    def capabilities(self) -> dict[str, Any]:
        native = self._status_value(self._call("opencode_status", {}))
        ready = native["state"] == "ready" and not self._uncertain_start
        return {
            "schema_version": 1,
            "opencode": {
                "status": "ready" if ready else "unavailable",
                "available": ready,
                "reason": "NATIVE_OPENCODE_GATEWAY_READY" if ready else "NATIVE_OPENCODE_GATEWAY_REQUIRED",
                "transport": self.transport_kind,
                "models": list(native.get("models", [])),
                "limits": {
                    "max_prompt_bytes": 500_000,
                    "max_wall_seconds": 120,
                    "max_inactivity_seconds": 30,
                    "max_output_bytes": MAX_OUTPUT_BYTES,
                    "max_events": 512,
                    "max_concurrent_runs": 1,
                },
            },
        }

    def run(
        self,
        *,
        manifest: Mapping[str, Any],
        sections: Sequence[Mapping[str, Any]],
        model: str,
        effort: str,
        run_id: str,
        provider_model: str | None = None,
        provider: ProviderConfig | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        packet = build_tutor_packet(
            manifest=manifest,
            sections=sections,
            model=model,
            effort=effort,
            run_id=run_id,
        )
        if provider is not None:
            normalized_provider = provider
            if normalized_provider.kind != "opencode_cli":
                raise AIProtocolError("provider is not an OpenCode CLI profile")
            provider_model = normalized_provider.model
            if "/" not in provider_model and normalized_provider.opencode_provider:
                provider_model = f"{normalized_provider.opencode_provider}/{provider_model}"
        if provider_model is None:
            raise AIProtocolError("OpenCode provider model is required")
        provider_model = _model(provider_model)
        with self._lock:
            previous = self._runs.get(run_id)
            if previous is not None:
                if previous.packet.input_digest != packet.input_digest or previous.provider_model != provider_model:
                    raise AIProtocolError("OpenCode request idempotency conflict")
                return {"accepted": True, **previous.snapshot()}
            if self._uncertain_start or any(item.status not in _TERMINAL for item in self._runs.values()):
                raise AIUnavailable("OpenCode already has an active request")
            if len(self._runs) >= self._max_records:
                oldest = next((key for key, item in self._runs.items() if item.status in _TERMINAL), None)
                if oldest is None:
                    raise AIUnavailable("OpenCode record limit reached")
                self._runs.pop(oldest)
            config = provider or ProviderConfig("opencode-native", "opencode_cli", provider_model, effort=effort)
            request = build_opencode_request(packet, config, api_key=api_key)
            run = _Run(
                run_id,
                packet,
                provider_model,
                provider_id=config.id,
                provider_kind=config.kind,
            )
            run.event("queued")
            self._runs[run_id] = run
            try:
                result = self._call("opencode_start", _start_payload(request))
                snapshot = self._native_value(result, run)
            except Exception:
                self._uncertain_start = True
                raise
            return {"accepted": True, **snapshot}

    def request(
        self,
        packet: TutorDomainPacket,
        *,
        provider_model: str | None = None,
        provider: ProviderConfig | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        return self.run(
            manifest=packet.manifest,
            sections=packet.sections,
            model=packet.model,
            effort=packet.effort,
            run_id=packet.run_id,
            provider_model=provider_model,
            provider=provider,
            api_key=api_key,
        )

    def status(self, run_id: str) -> dict[str, Any]:
        run_id = _id(run_id, "run id")
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise AIUnavailable("OpenCode run is not available")
            if run.status not in _TERMINAL:
                if run.native_id is None:
                    raise AIProtocolError("OpenCode run has no native identity")
                self._native_value(self._call("opencode_poll", {"requestId": run.native_id}), run)
            return run.snapshot()

    def events(self, run_id: str, *, after: int = 0) -> dict[str, Any]:
        run_id = _id(run_id, "run id")
        if type(after) is not int or after < 0:
            raise AIProtocolError("OpenCode event cursor is invalid")
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise AIUnavailable("OpenCode run is not available")
            # The native bridge is intentionally limited to status/start/poll/
            # cancel.  Lifecycle events are synthesized from this bounded,
            # sanitized local record, just like the original LocalCodex seam;
            # raw provider/OpenCode event bodies never cross into Python.
            events = [dict(item) for item in run.events if item["sequence"] > after]
            return {
                "schema_version": 1,
                "run_id": run_id,
                "events": events,
                "next_after": max(after, len(run.events)),
            }

    def cancel(self, run_id: str) -> dict[str, Any]:
        run_id = _id(run_id, "run id")
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise AIUnavailable("OpenCode run is not available")
            if run.status not in _TERMINAL:
                if run.native_id is None:
                    raise AIProtocolError("OpenCode run has no native identity")
                self._native_value(self._call("opencode_cancel", {"requestId": run.native_id}), run)
            return run.snapshot()


__all__ = [
    "OPEN_CODE_GATEWAY_OPERATIONS",
    "OpenCodeGatewayClient",
]

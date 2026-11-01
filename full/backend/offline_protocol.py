"""Data-only JSONL transport primitives; imports no application/configuration."""

from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any, BinaryIO
from urllib.parse import parse_qsl, unquote, urlsplit

# A measured 50,084,031-byte attachment needs a bounded base64 JSONL frame.
MAX_FRAME_BYTES = 96 * 1024 * 1024
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 64_000_000
METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
GATEWAY_OPERATIONS = frozenset({
    "codex_status", "codex_start", "codex_poll", "codex_cancel",
    "opencode_status", "opencode_start", "opencode_poll", "opencode_cancel",
})
FORBIDDEN_INPUT_KEYS = frozenset(
    {
        "owner_id",
        "ownerId",
        "authorization",
        "profile",
        "database_url",
        "asset_root",
        "path",
        "file_path",
        "filePath",
        "filesystem_path",
        "storage_key",
    }
)
ALLOWED_HEADERS = frozenset({"content-type", "idempotency-key", "x-sync-owner"})


class BridgeError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class GatewayCallError(BridgeError):
    pass


def invalid_request() -> BridgeError:
    return BridgeError("invalid_request", "Некорректный локальный запрос.")


def validate_headers(headers: Any, owner_id: str = "owner") -> list[tuple[bytes, bytes]]:
    if headers is None:
        return []
    if not isinstance(headers, dict) or len(headers) > 3:
        raise invalid_request()
    result = []
    seen: set[str] = set()
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise invalid_request()
        name = key.lower()
        if (
            name not in ALLOWED_HEADERS
            or name in seen
            or len(value.encode("utf-8")) > 1024
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise invalid_request()
        if name == "x-sync-owner" and value != owner_id:
            raise BridgeError("owner_mismatch", "Контекст локального владельца изменился.")
        try:
            result.append((name.encode("ascii"), value.encode("latin-1")))
        except UnicodeEncodeError:
            raise invalid_request() from None
        seen.add(name)
    return result


def validate_request(method: Any, path: Any, body: Any = None) -> tuple[str, str, str]:
    if (
        not isinstance(method, str)
        or method not in METHODS
        or not isinstance(path, str)
        or len(path.encode("utf-8")) > 8192
    ):
        raise invalid_request()
    if not path.startswith("/api/v1/") or any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise invalid_request()
    parts = urlsplit(path)
    decoded = unquote(parts.path)
    if parts.scheme or parts.netloc or parts.fragment or "\\" in path or "\\" in decoded:
        raise invalid_request()
    if any(part in {".", ".."} for part in decoded.split("/")) or any(ord(c) < 32 for c in decoded):
        raise invalid_request()
    if any(key in FORBIDDEN_INPUT_KEYS for key, _ in parse_qsl(parts.query)):
        raise invalid_request()
    if body is None:
        body = ""
    if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise BridgeError("body_too_large", "Локальный запрос превышает допустимый размер.")
    if body:
        try:
            value = json.loads(body, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("nonfinite")))
        except (ValueError, RecursionError):
            raise invalid_request() from None
        if isinstance(value, dict) and any(key in value for key in FORBIDDEN_INPUT_KEYS):
            raise invalid_request()
    return method, path, body


def encoded_response(status: int, headers: list[tuple[str, str]], body: bytes) -> dict[str, Any]:
    if len(body) > MAX_RESPONSE_BYTES:
        raise BridgeError("response_too_large", "Ответ превышает допустимый размер.")
    return {"status": status, "headers": headers, "bodyBase64": base64.b64encode(body).decode("ascii")}


class JsonlWriter:
    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream
        self.lock = threading.Lock()

    def write(self, value: dict[str, Any]) -> None:
        try:
            data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, RecursionError):
            raise BridgeError("protocol_error", "Не удалось сформировать локальный ответ.") from None
        if len(data) > MAX_FRAME_BYTES:
            raise BridgeError("response_too_large", "Ответ превышает допустимый размер.")
        with self.lock:
            self.stream.write(data + b"\n")
            self.stream.flush()


class GatewayCalls:
    """Reader resolves calls while request workers wait; no worker reads stdin."""

    def __init__(self, emit: Callable[[dict[str, Any]], None], timeout: float = 120.0) -> None:
        self.emit = emit
        self.timeout = timeout
        self.condition = threading.Condition()
        self.pending: dict[str, dict[str, Any] | None] = {}
        self.closed = False

    def call(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation not in GATEWAY_OPERATIONS or not isinstance(payload, dict):
            raise GatewayCallError("gateway_protocol", "Некорректный запрос к локальному ИИ.")
        call_id = uuid.uuid4().hex
        with self.condition:
            if self.closed or len(self.pending) >= 16:
                raise GatewayCallError("gateway_unavailable", "Локальный ИИ сейчас недоступен.")
            self.pending[call_id] = None
        try:
            self.emit({"event": "gateway", "callId": call_id, "operation": operation, "payload": payload})
            deadline = time.monotonic() + self.timeout
            with self.condition:
                while not self.closed and self.pending.get(call_id) is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise GatewayCallError("gateway_timeout", "Локальный ИИ не ответил вовремя.")
                    self.condition.wait(remaining)
                result = self.pending.get(call_id)
            if self.closed or not result:
                raise GatewayCallError("gateway_unavailable", "Локальный ИИ сейчас недоступен.")
            if result.get("ok") is not True:
                error = result.get("error")
                if (
                    isinstance(error, dict)
                    and isinstance(error.get("code"), str)
                    and isinstance(error.get("message"), str)
                    and len(error["code"]) <= 64
                    and len(error["message"]) <= 1024
                ):
                    raise GatewayCallError(error["code"], error["message"])
                raise GatewayCallError("gateway_unavailable", "Локальный ИИ сейчас недоступен.")
            if not isinstance(result.get("result"), dict):
                raise GatewayCallError("gateway_protocol", "Некорректный ответ локального ИИ.")
            return result["result"]
        finally:
            with self.condition:
                self.pending.pop(call_id, None)

    def resolve(self, frame: dict[str, Any]) -> None:
        call_id = frame.get("callId")
        with self.condition:
            if not isinstance(call_id, str) or call_id not in self.pending or self.pending[call_id] is not None:
                raise invalid_request()
            if not isinstance(frame.get("ok"), bool):
                raise invalid_request()
            self.pending[call_id] = frame
            self.condition.notify_all()

    def close(self) -> None:
        with self.condition:
            self.closed = True
            self.condition.notify_all()

    def quiesce(self) -> None:
        """Cancel even an orphaned native start whose Python reply was lost."""
        quiesce_gateway(self.call)


def quiesce_gateway(call: Callable[[str, dict[str, Any]], dict[str, Any]], *, timeout: float = 30.0) -> None:
    def active_request() -> str | None:
        status = call("codex_status", {})
        if (
            type(status.get("gatewayProtocol")) is not int
            or status["gatewayProtocol"] != 1
            or status.get("state") not in {"checking", "ready", "unavailable"}
        ):
            raise BridgeError("flush_pending_ai", "Не удалось подтвердить завершение ИИ.")
        request_id = status.get("activeRequestId")
        if request_id is not None and (not isinstance(request_id, str) or not request_id or len(request_id) > 256):
            raise BridgeError("flush_pending_ai", "Не удалось подтвердить завершение ИИ.")
        return request_id

    request_id = active_request()
    if request_id is None:
        return
    deadline = time.monotonic() + timeout
    result = call("codex_cancel", {"requestId": request_id})
    while True:
        if result.get("requestId") != request_id or result.get("state") not in {
            "starting",
            "running",
            "completed",
            "cancelled",
            "failed",
        }:
            raise BridgeError("flush_pending_ai", "Не удалось подтвердить завершение ИИ.")
        if result["state"] in {"completed", "cancelled", "failed"} and active_request() is None:
            return
        if time.monotonic() >= deadline:
            raise BridgeError("flush_pending_ai", "Запрос ИИ ещё завершается.")
        time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        result = call("codex_poll", {"requestId": request_id})

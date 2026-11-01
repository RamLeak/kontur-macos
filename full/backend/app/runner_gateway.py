"""Fail-closed boundary to the isolated ``runnerd`` HTTP service.

The monolith never executes submitted code and never opens the runner's
database.  ``HttpRunnerClient`` is deliberately usable only as an injected
adapter; the capability gate still requires an owner-reviewed hostile-container
attestation before the application can call it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import Settings


class RunnerUnavailable(RuntimeError):
    code = "RUNNER_UNAVAILABLE"


class RunnerClient(Protocol):
    def capabilities(self) -> dict[str, Any]: ...

    def run(self, *, code: str, inputs: list[dict[str, Any]], limits: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class RunnerStatus:
    available: bool
    reason: str
    mode: str


@dataclass(frozen=True, slots=True)
class RunnerResponse:
    status: int
    headers: dict[str, str]
    body: bytes


RunnerTransport = Callable[[str, str, dict[str, str], bytes, float, int], RunnerResponse]


def _safe_internal_base_url(raw: str) -> str:
    try:
        parsed = urlsplit(raw.strip())
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError as exc:
        raise RunnerUnavailable("runner URL is invalid") from exc
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise RunnerUnavailable("runner URL must be an authenticated-free HTTP(S) service URL")
    try:
        address = ipaddress.ip_address(host)
        safe_host = address.is_loopback or address.is_private or address.is_link_local
    except ValueError:
        safe_host = host.casefold() in {"localhost", "runnerd"}
    if not safe_host:
        raise RunnerUnavailable("runner URL must target an internal host")
    normalized_host = host.lower()
    netloc = normalized_host if port is None else f"{normalized_host}:{port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, netloc, path, "", ""))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _default_transport(
    url: str,
    method: str,
    headers: dict[str, str],
    body: bytes,
    timeout: float,
    max_bytes: int,
) -> RunnerResponse:
    request = Request(url, data=body if method == "POST" else None, headers=headers, method=method)
    opener = build_opener(_NoRedirect)
    try:
        response = opener.open(request, timeout=timeout)
        status = int(response.status)
        response_headers = {str(key): str(value) for key, value in response.headers.items()}
        payload = response.read(max_bytes + 1)
    except HTTPError as exc:
        status = int(exc.code)
        response_headers = {str(key): str(value) for key, value in exc.headers.items()}
        payload = exc.read(max_bytes + 1)
    except (OSError, URLError, TimeoutError) as exc:
        raise RunnerUnavailable("bounded runner request failed") from exc
    if len(payload) > max_bytes:
        raise RunnerUnavailable("runner response exceeds configured byte limit")
    return RunnerResponse(status=status, headers=response_headers, body=payload)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _input_bytes(item: dict[str, Any]) -> bytes:
    value = item.get("content")
    if isinstance(value, bytes):
        return value
    encoded = item.get("content_base64")
    if isinstance(encoded, str):
        try:
            return base64.b64decode(encoded.encode("ascii"), validate=True)
        except (ValueError, UnicodeError, binascii.Error) as exc:
            raise RunnerUnavailable("runner input has invalid base64") from exc
    raise RunnerUnavailable("runner input bytes are missing")


def _validate_file_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or not name
        or name.startswith("/")
        or "\\" in name
        or any(part in {"", ".", ".."} for part in name.split("/"))
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
        or len(name.encode("utf-8")) > 240
    ):
        raise RunnerUnavailable("runner input filename is unsafe")


def _input_digest(*, code: str, stdin: str, language: str, files: list[dict[str, str]], limits: dict[str, int]) -> str:
    canonical = {
        "schema_version": 1,
        "language": language,
        "code_sha256": _sha256(code.encode("utf-8")),
        "stdin_sha256": _sha256(stdin.encode("utf-8")),
        "files": sorted(files, key=lambda item: item["name"].encode("utf-8")),
        "limits": {
            "wall_time_seconds": limits["wall_time_seconds"],
            "output_bytes": limits["output_bytes"],
        },
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256(encoded)


class HttpRunnerClient:
    """Bounded runnerd adapter with an injectable transport for tests."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: RunnerTransport | None = None,
        timeout_seconds: float = 20.0,
        max_response_bytes: int = 512_000,
    ) -> None:
        if not settings.runner_base_url:
            raise RunnerUnavailable("runner service URL is not configured")
        self.base_url = _safe_internal_base_url(settings.runner_base_url)
        self.shared_secret = settings.runner_shared_secret
        self.transport = transport or _default_transport
        self.timeout_seconds = max(1.0, min(timeout_seconds, 60.0))
        self.max_response_bytes = max(1024, min(max_response_bytes, 2_000_000))

    def _request(self, method: str, path: str, body: bytes = b"") -> RunnerResponse:
        if not path.startswith("/") or ".." in path.split("/"):
            raise RunnerUnavailable("runner path is invalid")
        headers = {"Accept": "application/json", "Cache-Control": "no-store"}
        if method == "POST":
            headers["Content-Type"] = "application/json"
        if self.shared_secret:
            headers["Authorization"] = f"Bearer {self.shared_secret}"
        return self.transport(
            f"{self.base_url}{path}", method, headers, body, self.timeout_seconds, self.max_response_bytes
        )

    def capabilities(self) -> dict[str, Any]:
        response = self._request("GET", "/v1/capabilities")
        data = self._decode(response)
        if response.status != 200 or data.get("schema_version") != 1 or isinstance(data.get("schema_version"), bool):
            raise RunnerUnavailable("runner capabilities response is invalid")
        return data

    def run(self, *, code: str, inputs: list[dict[str, Any]], limits: dict[str, Any]) -> dict[str, Any]:
        # ``execute`` passes only v1 limits; copy before reading so the caller's
        # dictionary is never mutated by this adapter.
        requested = dict(limits)
        language = str(requested.pop("language", "python"))
        stdin = str(requested.pop("stdin", ""))
        wall = requested.get("wall_time_seconds", 5)
        output = requested.get("output_bytes", 64_000)
        if isinstance(wall, bool) or not isinstance(wall, int) or wall < 1:
            raise RunnerUnavailable("runner wall limit is invalid")
        if isinstance(output, bool) or not isinstance(output, int) or output < 1024:
            raise RunnerUnavailable("runner output limit is invalid")
        files: list[dict[str, str]] = []
        encoded_files: list[dict[str, str]] = []
        names: set[str] = set()
        for item in inputs:
            name = item.get("name")
            if not isinstance(name, str):
                raise RunnerUnavailable("runner input filename is missing")
            _validate_file_name(name)
            key = name.casefold()
            if key in names:
                raise RunnerUnavailable("runner input filenames must be unique")
            names.add(key)
            content = _input_bytes(item)
            digest = _sha256(content)
            supplied = item.get("sha256")
            if supplied is not None and supplied != digest:
                raise RunnerUnavailable(f"runner input digest mismatch for {name}")
            file_ref = {"name": name, "sha256": digest}
            files.append(file_ref)
            encoded_files.append({**file_ref, "content_base64": base64.b64encode(content).decode("ascii")})
        safe_limits = {"wall_time_seconds": wall, "output_bytes": output}
        expected_digest = _input_digest(code=code, stdin=stdin, language=language, files=files, limits=safe_limits)
        payload = {
            "schema_version": 1,
            "language": language,
            "code": code,
            "stdin": stdin,
            "files": encoded_files,
            "limits": safe_limits,
        }
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        response = self._request("POST", "/v1/runs", body)
        data = self._decode(response)
        if response.status != 200:
            raise RunnerUnavailable(str(data.get("message") or "runner rejected request"))
        if (
            data.get("schema_version") != 1
            or isinstance(data.get("schema_version"), bool)
            or data.get("input_digest") != expected_digest
        ):
            raise RunnerUnavailable("runner response failed protocol digest validation")
        return data

    @staticmethod
    def _decode(response: RunnerResponse) -> dict[str, Any]:
        try:
            value = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RunnerUnavailable("runner response is not valid JSON") from exc
        if not isinstance(value, dict):
            raise RunnerUnavailable("runner response must be an object")
        return value


def runner_status(settings: Settings, client: RunnerClient | None) -> RunnerStatus:
    if client is None:
        return RunnerStatus(False, "no runner service is attached", settings.runner_mode)
    if settings.runner_mode != "isolated" or not settings.runner_attested:
        return RunnerStatus(False, "hostile-container gate has not been attested", settings.runner_mode)
    return RunnerStatus(True, "attested runner service attached", settings.runner_mode)


def execute(
    settings: Settings,
    client: RunnerClient | None,
    *,
    code: str,
    inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    status = runner_status(settings, client)
    if not status.available or client is None:
        raise RunnerUnavailable(status.reason)
    limits = {
        "wall_time_seconds": 10,
        "output_bytes": settings.max_run_output_bytes,
        "network": False,
        "read_only_inputs": True,
    }
    try:
        return client.run(code=code, inputs=inputs, limits=limits)
    except RunnerUnavailable:
        raise
    except (OSError, TimeoutError, ValueError) as exc:
        raise RunnerUnavailable("runner adapter failed closed") from exc

"""Production browser-session, CSRF, trusted-proxy and quota boundary."""

from __future__ import annotations

import ipaddress
import math
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Annotated, NoReturn
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from .auth import OwnerPrincipal, issue_session_token, require_owner, verify_session_token
from .config import Settings

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _http_error(status: int, code: str, message: str, *, headers: dict[str, str] | None = None) -> NoReturn:
    raise HTTPException(
        status_code=status,
        detail={"code": code, "message": message, "details": {}},
        headers=headers,
    )


@dataclass(frozen=True, slots=True)
class _Origin:
    value: str
    host: str


def _loopback_origin(value: str | None) -> _Origin | None:
    if not value or any(ord(char) < 32 or char.isspace() for char in value):
        return None
    parsed = urlsplit(value)
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    host = hostname.casefold()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback:
        return None
    host_for_url = f"[{host}]" if ":" in host and not host.startswith("[") else host
    canonical = f"{parsed.scheme.casefold()}://{host_for_url}"
    if port is not None:
        canonical += f":{port}"
    return _Origin(value=canonical, host=host)


def _referer_origin(value: str | None) -> _Origin | None:
    if not value or any(ord(char) < 32 or char.isspace() for char in value):
        return None
    parsed = urlsplit(value)
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    host = hostname.casefold()
    host_for_url = f"[{host}]" if ":" in host and not host.startswith("[") else host
    canonical = f"{parsed.scheme.casefold()}://{host_for_url}"
    if port is not None:
        canonical += f":{port}"
    return _Origin(value=canonical, host=host)


def _production_origin(value: str | None) -> _Origin | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    host = parsed.netloc.casefold()
    return _Origin(value=f"https://{host}", host=host)


class OwnerRateLimiter:
    """Small in-process fixed-window quota; deployment keeps one backend writer."""

    def __init__(self) -> None:
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, *, owner_id: str, bucket: str, limit: int, window_seconds: int) -> int | None:
        now = time.monotonic()
        cutoff = now - window_seconds
        key = (owner_id, bucket)
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                return max(1, math.ceil(window_seconds - (now - events[0])))
            events.append(now)
        return None


class SecurityGuard:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.rate_limiter = OwnerRateLimiter()

    @property
    def production(self) -> bool:
        return (
            self.settings.security_mode == "production"
            and not self.settings.testing
            and not self.settings.owner_dev_mode
        )

    def _runtime_config(self) -> tuple[_Origin, tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]]:
        if self.settings.security_mode not in {"production", "development", "testing"}:
            _http_error(503, "SECURITY_CONFIG_UNAVAILABLE", "Security mode is invalid")
        # The browser double-submit helper intentionally reads one fixed,
        # non-secret cookie name. A server-only rename would silently break all
        # mutations, so production rejects that inconsistent configuration.
        if self.settings.owner_csrf_cookie != "ege_csrf":
            _http_error(503, "SECURITY_CONFIG_UNAVAILABLE", "CSRF cookie name is incompatible")
        secret = self.settings.owner_session_secret
        if not secret or len(secret.encode("utf-8")) < 32:
            _http_error(503, "SECURITY_CONFIG_UNAVAILABLE", "Owner session secret is missing or too short")
        origin = _production_origin(self.settings.owner_public_origin)
        if origin is None:
            _http_error(503, "SECURITY_CONFIG_UNAVAILABLE", "A single HTTPS owner origin is required")
        try:
            networks = tuple(ipaddress.ip_network(value, strict=True) for value in self.settings.trusted_proxy_cidrs)
        except ValueError:
            _http_error(503, "SECURITY_CONFIG_UNAVAILABLE", "Trusted proxy CIDR configuration is invalid")
        if not networks:
            _http_error(503, "SECURITY_CONFIG_UNAVAILABLE", "At least one trusted proxy CIDR is required")
        return origin, networks

    def validate_header_source(self, request: Request) -> None:
        if not self.production:
            return
        _, networks = self._runtime_config()
        peer = request.client.host if request.client else ""
        try:
            address = ipaddress.ip_address(peer)
        except ValueError:
            _http_error(401, "TRUSTED_PROXY_REQUIRED", "Owner header requires the trusted proxy boundary")
        if not any(address in network for network in networks):
            _http_error(401, "TRUSTED_PROXY_REQUIRED", "Owner header requires the trusted proxy boundary")

    def validate_dev_request(
        self,
        request: Request,
        *,
        bootstrap: bool = False,
        origin_error_status: int = 403,
    ) -> None:
        """Enforce the explicit loopback-only browser boundary for local dev."""

        if not self.settings.owner_dev_mode:
            _http_error(503, "SECURITY_CONFIG_UNAVAILABLE", "Local development owner mode is disabled")
        origin = _loopback_origin(self.settings.owner_dev_origin)
        if origin is None:
            _http_error(503, "SECURITY_CONFIG_UNAVAILABLE", "A configured loopback owner origin is required")

        peer = request.client.host if request.client else ""
        try:
            peer_loopback = ipaddress.ip_address(peer).is_loopback
        except ValueError:
            peer_loopback = peer.casefold() == "localhost"
        if not peer_loopback:
            _http_error(401, "TRUSTED_PROXY_REQUIRED", "Local development owner mode requires a loopback peer")

        supplied_origin = request.headers.get("Origin")
        if supplied_origin is None and request.method == "GET" and not bootstrap:
            referer = request.headers.get("Referer")
            referer_origin = _referer_origin(referer)
            if referer_origin is not None and referer_origin.value == origin.value:
                return
            _http_error(origin_error_status, "ORIGIN_REJECTED", "GET requires the configured owner Origin or Referer")
        if supplied_origin is None:
            _http_error(origin_error_status, "ORIGIN_REJECTED", "Request origin is required")
        supplied = _loopback_origin(supplied_origin)
        if supplied is None or supplied.value != origin.value:
            _http_error(origin_error_status, "ORIGIN_REJECTED", "Request origin is not the configured owner origin")


    def _validate_browser_boundary(self, request: Request, *, origin: _Origin) -> None:
        supplied_origin = request.headers.get("Origin", "").rstrip("/").casefold()
        if supplied_origin != origin.value:
            _http_error(403, "ORIGIN_REJECTED", "Request origin is not the configured owner origin")
        supplied_host = request.headers.get("Host", "").casefold()
        if supplied_host != origin.host:
            _http_error(403, "HOST_REJECTED", "Request host is not the configured owner host")

    def _bucket(self, request: Request) -> tuple[str, int]:
        path = request.url.path
        if path.endswith("/events"):
            return "stream", self.settings.rate_stream_limit
        if (
            path.startswith("/api/v1/source/")
            or path.startswith("/api/v1/assets/")
            or path.endswith("/code-runs")
            or path.startswith("/api/v1/assistant/runs")
            or path == "/api/v1/auth/session"
        ):
            return "sensitive", self.settings.rate_sensitive_limit
        if request.method in UNSAFE_METHODS:
            return "mutation", self.settings.rate_mutation_limit
        return "read", self.settings.rate_read_limit

    def authorize(self, request: Request, *, owner_id: str, csrf_exempt: bool = False) -> None:
        if not self.production:
            return
        origin, _ = self._runtime_config()
        browser_bound = request.method in UNSAFE_METHODS or request.url.path.endswith("/events")
        if browser_bound:
            self._validate_browser_boundary(request, origin=origin)
        if request.method in UNSAFE_METHODS and not csrf_exempt:
            cookie = request.cookies.get(self.settings.owner_csrf_cookie, "")
            header = request.headers.get("X-CSRF-Token", "")
            if len(cookie) < 32 or not secrets.compare_digest(cookie, header):
                _http_error(403, "CSRF_REQUIRED", "A valid CSRF token is required")
        bucket, limit = self._bucket(request)
        retry_after = self.rate_limiter.check(
            owner_id=owner_id,
            bucket=bucket,
            limit=limit,
            window_seconds=self.settings.rate_window_seconds,
        )
        if retry_after is not None:
            _http_error(
                429,
                "RATE_LIMITED",
                "Owner request quota exceeded",
                headers={"Retry-After": str(retry_after)},
            )


router = APIRouter()


@router.post("/auth/session", status_code=204)
def bootstrap_session(request: Request) -> Response:
    """Exchange a trusted-proxy header for a bounded browser cookie pair."""

    settings: Settings = request.app.state.settings
    guard: SecurityGuard = request.app.state.security_guard
    if settings.owner_dev_mode:
        guard.validate_dev_request(request, bootstrap=True)
        response = Response(status_code=204)
        response.headers["Cache-Control"] = "no-store"
        return response

    secret = settings.owner_session_secret
    header_token = request.headers.get("X-Owner-Session")
    if not secret:
        _http_error(503, "AUTH_UNAVAILABLE", "Owner authentication is not configured")
    if not header_token:
        _http_error(401, "OWNER_AUTH_REQUIRED", "Trusted owner identity is required")
    guard.validate_header_source(request)
    owner_id = verify_session_token(header_token, secret)
    if owner_id is None:
        _http_error(401, "OWNER_AUTH_REQUIRED", "Trusted owner identity is invalid")
    guard.authorize(request, owner_id=owner_id, csrf_exempt=True)

    expires_at = int(time.time()) + settings.owner_session_ttl_seconds
    session_token = issue_session_token(owner_id, secret, expires_at=expires_at)
    csrf_token = secrets.token_urlsafe(32)
    secure = guard.production
    response = Response(status_code=204)
    response.set_cookie(
        settings.owner_session_cookie,
        session_token,
        max_age=settings.owner_session_ttl_seconds,
        path="/",
        secure=secure,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        settings.owner_csrf_cookie,
        csrf_token,
        max_age=settings.owner_session_ttl_seconds,
        path="/",
        secure=secure,
        httponly=False,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/auth/session")
def session_status(principal: Annotated[OwnerPrincipal, Depends(require_owner)]) -> dict[str, object]:
    return {"authenticated": True, "owner_id": principal.owner_id}


@router.delete("/auth/session", status_code=204)
def clear_session(request: Request, _principal: Annotated[OwnerPrincipal, Depends(require_owner)]) -> Response:
    settings: Settings = request.app.state.settings
    guard: SecurityGuard = request.app.state.security_guard
    response = Response(status_code=204)
    response.delete_cookie(
        settings.owner_session_cookie,
        path="/",
        secure=guard.production,
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        settings.owner_csrf_cookie,
        path="/",
        secure=guard.production,
        httponly=False,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"
    return response

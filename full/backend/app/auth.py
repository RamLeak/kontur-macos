"""Owner-only authentication boundary.

The initial application has one owner, but every protected query still receives an
authenticated owner id. The session token is an HMAC envelope; the secret is read
only by the runtime process from injected configuration and is never persisted.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from .models import User


class AuthUnavailable(RuntimeError):
    """Raised when owner authentication has not been configured."""


@dataclass(frozen=True, slots=True)
class OwnerPrincipal:
    owner_id: str
    role: str = "owner"


def _encoded_owner(owner_id: str) -> str:
    if not owner_id or len(owner_id) > 128 or "." in owner_id or any(ord(char) < 32 for char in owner_id):
        raise ValueError("owner_id must be a non-empty bounded identifier without dots or controls")
    encoded = base64.urlsafe_b64encode(owner_id.encode("utf-8")).decode("ascii").rstrip("=")
    return encoded


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()


def issue_session_token(owner_id: str, secret: str, *, expires_at: int | None = None) -> str:
    """Issue a signed owner token.

    Header tokens may retain the original two-part format so a reverse proxy can
    inject a stable credential. Browser cookies are issued as expiring v2
    tokens by the session bootstrap endpoint.
    """

    encoded = _encoded_owner(owner_id)
    if expires_at is None:
        return f"{encoded}.{_sign(encoded, secret)}"
    if expires_at < 1:
        raise ValueError("expires_at must be a positive Unix timestamp")
    payload = f"v2.{encoded}.{expires_at}"
    return f"{payload}.{_sign(payload, secret)}"


def verify_session_token(token: str, secret: str, *, now: int | None = None) -> str | None:
    try:
        parts = token.split(".")
        if len(parts) == 2:
            encoded, signature = parts
            payload = encoded
        elif len(parts) == 4 and parts[0] == "v2":
            _, encoded, raw_expiry, signature = parts
            expiry = int(raw_expiry)
            if expiry <= (int(time.time()) if now is None else now):
                return None
            payload = f"v2.{encoded}.{raw_expiry}"
        else:
            return None
        expected = _sign(payload, secret)
        if not hmac.compare_digest(signature, expected):
            return None
        padded = encoded + "=" * (-len(encoded) % 4)
        owner_id = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        if _encoded_owner(owner_id) != encoded:
            return None
    except (ValueError, UnicodeError, binascii.Error):
        return None
    return owner_id or None


def ensure_owner(session: Session, owner_id: str) -> User:
    user = session.get(User, owner_id)
    if user is None:
        user = User(id=owner_id, role="owner")
        session.add(user)
        session.flush()
    return user


def require_owner(request: Request) -> OwnerPrincipal:
    settings: Any = request.app.state.settings
    secret = settings.owner_session_secret
    header_token = request.headers.get("X-Owner-Session")
    cookie_token = request.cookies.get(settings.owner_session_cookie)
    security_guard: Any = getattr(request.app.state, "security_guard", None)
    if settings.owner_dev_mode:
        validate_dev_request = getattr(security_guard, "validate_dev_request", None)
        if not callable(validate_dev_request):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "SECURITY_CONFIG_UNAVAILABLE",
                    "message": "Local development owner boundary is unavailable",
                    "details": {},
                },
            )
        validate_dev_request(request, origin_error_status=403 if secret else 503)
    if header_token and security_guard is not None:
        security_guard.validate_header_source(request)
    if header_token and cookie_token:
        header_owner = verify_session_token(header_token, secret) if secret else None
        cookie_owner = verify_session_token(cookie_token, secret) if secret else None
        if header_owner is None or cookie_owner is None or header_owner != cookie_owner:
            raise HTTPException(
                status_code=401,
                detail={"code": "AUTH_AMBIGUOUS", "message": "Conflicting owner credentials", "details": {}},
            )
    token = header_token or cookie_token
    if cookie_token and not header_token and not cookie_token.startswith("v2.") and not settings.testing:
        token = None
    owner_id = verify_session_token(token or "", secret) if secret else None
    if owner_id is not None:
        if security_guard is not None:
            security_guard.authorize(request, owner_id=owner_id)
        return OwnerPrincipal(owner_id=owner_id)

    # Explicit local-dev escape hatch is validated above by the security guard;
    # it never runs unless the request came from the configured loopback origin.
    if settings.owner_dev_mode and not token:
        return OwnerPrincipal(owner_id=settings.owner_id)
    if not secret:
        raise HTTPException(
            status_code=503,
            detail={"code": "AUTH_UNAVAILABLE", "message": "Owner authentication is not configured", "details": {}},
        )
    raise HTTPException(
        status_code=401,
        detail={"code": "OWNER_AUTH_REQUIRED", "message": "Valid owner session required", "details": {}},
    )

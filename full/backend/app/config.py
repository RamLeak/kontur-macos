"""Runtime configuration for the backend.

External boundaries are intentionally disabled unless an owner explicitly enables
and attests them. The module does not load dotenv files or print secret values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None:
        return default
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    return values


@dataclass(frozen=True, slots=True)
class Settings:
    """Configuration with fail-closed defaults."""

    database_url: str = "sqlite:///backend/data/app.sqlite"
    asset_root: Path = Path("backend/data/assets")
    owner_session_secret: str | None = None
    owner_id: str = "owner"
    owner_dev_mode: bool = False
    owner_dev_origin: str | None = None
    # Production is the safe default. Tests and the explicit loopback-only
    # development mode bypass the browser security layer without weakening a
    # deployed process by accident.
    security_mode: str = "production"
    owner_public_origin: str | None = None
    trusted_proxy_cidrs: tuple[str, ...] = ("127.0.0.1/32", "::1/128")
    owner_session_cookie: str = "ege_owner_session"
    owner_csrf_cookie: str = "ege_csrf"
    owner_session_ttl_seconds: int = 43_200
    rate_window_seconds: int = 60
    rate_read_limit: int = 600
    rate_mutation_limit: int = 120
    rate_sensitive_limit: int = 30
    rate_stream_limit: int = 8
    source_mode: str = "disabled"
    source_network_enabled: bool = False
    source_permission_status: str = "review_required"
    # The task JSON endpoint is intentionally unset by default: the public
    # canonical task page is known, while an API path requires an owner-approved
    # smoke/contract review before any production sync can use it.
    source_api_template: str | None = None
    # The observed random-variant endpoint is independently gated.  Supplying
    # this value records the owner's explicit review of that endpoint; an
    # arbitrary request-body URL can never enable variant sync.
    source_variant_api_url: str | None = None
    codex_enabled: bool = False
    codex_attested: bool = False
    codex_base_url: str | None = None
    codex_shared_secret: str | None = None
    runner_mode: str = "disabled"
    runner_attested: bool = False
    runner_base_url: str | None = None
    runner_shared_secret: str | None = None
    max_board_bytes: int = 2_000_000
    max_code_bytes: int = 200_000
    max_message_bytes: int = 100_000
    max_asset_bytes: int = 10_000_000
    # Aggregate on-disk asset cache quota. It is separate from the per-asset
    # limit and is enforced transactionally before lazy promotion.
    max_asset_cache_bytes: int = 1_000_000_000
    max_run_output_bytes: int = 64 * 1024
    max_context_chars: int = 80_000
    testing: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        """Load non-secret configuration values from the process environment.

        ``EGE_OWNER_SESSION_SECRET`` is read only by the runtime authenticator;
        this function never logs it. Missing authentication material keeps all
        owner-protected routes unavailable.
        """

        defaults = cls()
        database_url = os.getenv("EGE_DATABASE_URL", defaults.database_url)
        asset_root = Path(os.getenv("EGE_ASSET_ROOT", str(defaults.asset_root)))
        secret = os.getenv("EGE_OWNER_SESSION_SECRET") or None
        return cls(
            database_url=database_url,
            asset_root=asset_root,
            owner_session_secret=secret,
            owner_id=os.getenv("EGE_OWNER_ID", defaults.owner_id),
            owner_dev_mode=_bool_env("EGE_OWNER_DEV_MODE"),
            owner_dev_origin=os.getenv("EGE_OWNER_DEV_ORIGIN") or None,
            security_mode=os.getenv("EGE_SECURITY_MODE", defaults.security_mode).strip().lower(),
            owner_public_origin=os.getenv("EGE_OWNER_PUBLIC_ORIGIN") or None,
            trusted_proxy_cidrs=_csv_env("EGE_TRUSTED_PROXY_CIDRS", defaults.trusted_proxy_cidrs),
            owner_session_cookie=os.getenv("EGE_OWNER_SESSION_COOKIE", defaults.owner_session_cookie),
            owner_csrf_cookie=os.getenv("EGE_OWNER_CSRF_COOKIE", defaults.owner_csrf_cookie),
            owner_session_ttl_seconds=_int_env(
                "EGE_OWNER_SESSION_TTL_SECONDS",
                defaults.owner_session_ttl_seconds,
                minimum=300,
                maximum=604_800,
            ),
            rate_window_seconds=_int_env(
                "EGE_RATE_WINDOW_SECONDS", defaults.rate_window_seconds, minimum=1, maximum=3_600
            ),
            rate_read_limit=_int_env("EGE_RATE_READ_LIMIT", defaults.rate_read_limit, minimum=1, maximum=100_000),
            rate_mutation_limit=_int_env(
                "EGE_RATE_MUTATION_LIMIT", defaults.rate_mutation_limit, minimum=1, maximum=100_000
            ),
            rate_sensitive_limit=_int_env(
                "EGE_RATE_SENSITIVE_LIMIT", defaults.rate_sensitive_limit, minimum=1, maximum=10_000
            ),
            rate_stream_limit=_int_env("EGE_RATE_STREAM_LIMIT", defaults.rate_stream_limit, minimum=1, maximum=1_000),
            source_mode=os.getenv("EGE_SOURCE_MODE", defaults.source_mode),
            source_network_enabled=_bool_env("EGE_SOURCE_NETWORK_ENABLED"),
            source_permission_status=os.getenv("EGE_SOURCE_PERMISSION_STATUS", defaults.source_permission_status),
            source_api_template=os.getenv("EGE_SOURCE_API_TEMPLATE") or None,
            source_variant_api_url=os.getenv("EGE_SOURCE_VARIANT_API_URL") or None,
            max_asset_cache_bytes=_int_env(
                "EGE_MAX_ASSET_CACHE_BYTES",
                defaults.max_asset_cache_bytes,
                minimum=defaults.max_asset_bytes,
                maximum=10_000_000_000,
            ),
            codex_enabled=_bool_env("EGE_CODEX_ENABLED"),
            codex_attested=_bool_env("EGE_CODEX_ATTESTED"),
            codex_base_url=os.getenv("EGE_CODEX_BASE_URL") or None,
            codex_shared_secret=os.getenv("EGE_CODEX_SHARED_SECRET") or None,
            runner_mode=os.getenv("EGE_RUNNER_MODE", defaults.runner_mode),
            runner_attested=_bool_env("EGE_RUNNER_ATTESTED"),
            runner_base_url=os.getenv("EGE_RUNNER_BASE_URL") or None,
            runner_shared_secret=os.getenv("EGE_RUNNER_SHARED_SECRET") or None,
            testing=_bool_env("EGE_TESTING"),
        )

    def for_testing(self, *, database_url: str = "sqlite+pysqlite:///:memory:") -> Settings:
        """Return a deterministic test configuration without external access."""

        return replace(
            self,
            database_url=database_url,
            owner_session_secret="test-only-owner-secret",
            asset_root=Path(".test-assets"),
            source_mode="fixture",
            source_network_enabled=False,
            source_permission_status="private_authorized",
            codex_enabled=False,
            codex_attested=False,
            runner_mode="mock",
            runner_attested=False,
            security_mode="testing",
            testing=True,
        )

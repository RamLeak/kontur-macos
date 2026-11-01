"""Private settings seam for selectable offline AI providers.

The store intentionally keeps one non-secret profile per provider kind.  API
keys are written only through an injected :class:`SecretStore`; public views
contain a boolean ``hasApiKey`` and never the key or its opaque storage ref.
Route installation is explicit so the root application can mount it under its
existing private local API/ACL and CSRF dependencies.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from app.codex_gateway import CodexProtocolError, CodexUnavailable
from offline_ai import (
    AIConfigError,
    OfflineAIError,
    ProviderConfig,
    validate_provider_config,
)
from offline_ai_secrets import SecretStore, SecretStoreError

PROFILE_KINDS = (
    "codex_cli",
    "opencode_cli",
    "ollama",
    "lm_studio",
    "openai_compatible_local",
    "openai_compatible",
)
ProfileKind = Literal[
    "codex_cli",
    "opencode_cli",
    "ollama",
    "lm_studio",
    "openai_compatible_local",
    "openai_compatible",
]

_DEFAULT_ENDPOINTS = {
    "ollama": "http://127.0.0.1:11434",
    "lm_studio": "http://127.0.0.1:1234/v1",
}
_PUBLIC_KEYS = {
    "schemaVersion",
    "selectedKind",
    "profiles",
}
MAX_SETTINGS_BYTES = 128 * 1024


class LocalAISettingsError(ValueError):
    code = "AI_SETTINGS_INVALID"


class _SafeLocalAIRoute(APIRoute):
    """Keep FastAPI validation details from echoing secret-bearing bodies."""

    def get_route_handler(self) -> Callable[..., Any]:
        handler = super().get_route_handler()

        async def safe_handler(request: Any) -> Any:
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "AI_SETTINGS_INVALID", "message": "invalid AI settings payload"},
                ) from None

        return safe_handler


def _settings_path(path: Path | str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or "\x00" in str(candidate):
        raise LocalAISettingsError("AI settings path must be absolute")
    resolved = Path(os.path.abspath(candidate))
    components = {part.casefold() for part in resolved.parts}
    if components & {".kontur", "export", "sqlite", "clientstate"}:
        raise LocalAISettingsError("AI settings path is not allowed")
    if resolved.suffix.casefold() in {".kontur", ".sqlite", ".sqlite3", ".db"}:
        raise LocalAISettingsError("AI settings path is not allowed")
    return resolved


@dataclass(slots=True)
class LocalAISettingsStore:
    """One selected, non-secret profile per provider kind."""

    secret_store: SecretStore | None = None
    path: Path | None = None
    _profiles: dict[str, ProviderConfig] = field(default_factory=dict, init=False, repr=False)
    _selected_kind: str | None = field(default=None, init=False, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        self._profiles: dict[str, ProviderConfig] = {}
        self._selected_kind: str | None = None
        if self.path is not None:
            self.path = _settings_path(self.path)
            self.load()

    @staticmethod
    def _secret_ref(kind: str) -> str:
        return f"ai-{kind}"

    def _normalize(self, profile: ProviderConfig) -> ProviderConfig:
        if profile.kind not in PROFILE_KINDS:
            raise LocalAISettingsError("provider kind is unsupported")
        return validate_provider_config(profile)

    @staticmethod
    def _provider_identity(profile: ProviderConfig) -> tuple[str | None, str | None]:
        prefix = profile.model.split("/", 1)[0] if profile.kind == "opencode_cli" and "/" in profile.model else None
        return profile.opencode_provider, prefix

    def upsert(
        self,
        profile: ProviderConfig,
        *,
        api_key: str | None = None,
        clear_api_key: bool = False,
        selected: bool | None = None,
    ) -> ProviderConfig:
        """Validate and atomically update one profile.

        A blank UI key means "keep the existing key".  Endpoint/provider
        identity changes require either a replacement key or explicit
        ``clear_api_key`` so a credential cannot silently target a new host.
        """

        with self._lock:
            normalized = self._normalize(profile)
            previous = self._profiles.get(normalized.kind)
            if api_key == "":
                api_key = None
            if clear_api_key and api_key is not None:
                raise LocalAISettingsError("replace or clear the API key, not both")
            identity_changed = bool(
                previous is not None
                and (
                    previous.endpoint != normalized.endpoint
                    or self._provider_identity(previous) != self._provider_identity(normalized)
                )
            )
            if (
                identity_changed
                and previous is not None
                and previous.api_key_ref
                and api_key is None
                and not clear_api_key
            ):
                raise LocalAISettingsError("API key must be replaced or cleared when provider identity changes")

            old_profiles = dict(self._profiles)
            old_selected = self._selected_kind
            old_secret: str | None = None
            old_secret_present = False
            key_ref = None if clear_api_key else normalized.api_key_ref
            if key_ref is None and previous is not None and not clear_api_key:
                key_ref = previous.api_key_ref
            secret_changed = False
            if previous is not None and previous.api_key_ref and self.secret_store is not None:
                try:
                    old_secret = self.secret_store.get(previous.api_key_ref)
                    old_secret_present = True
                except SecretStoreError:
                    pass
            if api_key is not None:
                if normalized.kind == "codex_cli":
                    raise LocalAISettingsError("Codex CLI credentials are managed by its native login flow")
                if normalized.kind in {"ollama", "lm_studio", "openai_compatible_local"}:
                    raise LocalAISettingsError("local provider does not accept an API key")
                if self.secret_store is None:
                    raise LocalAISettingsError("API key storage is unavailable")
                key_ref = key_ref or self._secret_ref(normalized.kind)
                try:
                    self.secret_store.put(key_ref, api_key)
                except SecretStoreError as exc:
                    raise LocalAISettingsError("API key could not be stored") from exc
                secret_changed = True
            elif clear_api_key and previous is not None and previous.api_key_ref and self.secret_store is not None:
                try:
                    self.secret_store.delete(previous.api_key_ref)
                except SecretStoreError as exc:
                    raise LocalAISettingsError("API key could not be cleared") from exc
                secret_changed = True

            if key_ref != normalized.api_key_ref:
                normalized = replace(normalized, api_key_ref=key_ref)
            self._profiles[normalized.kind] = normalized
            if selected is True or self._selected_kind is None:
                self._selected_kind = normalized.kind
            elif selected is False and self._selected_kind == normalized.kind:
                self._selected_kind = next(iter(self._profiles), None)
            try:
                self.save()
            except Exception:
                self._profiles = old_profiles
                self._selected_kind = old_selected
                if secret_changed and self.secret_store is not None:
                    try:
                        if (
                            old_secret_present
                            and previous is not None
                            and previous.api_key_ref
                            and old_secret is not None
                        ):
                            self.secret_store.put(previous.api_key_ref, old_secret)
                        else:
                            rollback_ref = previous.api_key_ref if previous is not None else key_ref
                            if rollback_ref:
                                self.secret_store.delete(rollback_ref)
                    except SecretStoreError:
                        pass
                raise
            return normalized

    def select(self, kind: str) -> ProviderConfig:
        with self._lock:
            if kind not in PROFILE_KINDS or kind not in self._profiles:
                raise LocalAISettingsError("provider profile is not configured")
            previous = self._selected_kind
            self._selected_kind = kind
            try:
                self.save()
            except Exception:
                self._selected_kind = previous
                raise
            return self._profiles[kind]

    def selected(self) -> ProviderConfig | None:
        with self._lock:
            return self._profiles.get(self._selected_kind) if self._selected_kind else None

    def capture(self, profile: ProviderConfig | None = None) -> tuple[ProviderConfig | None, str | None]:
        """Capture one profile and its key while the settings snapshot is locked.

        A send must never read a profile, yield to a concurrent settings save,
        and then read a key from the next profile generation.  When an
        explicit profile is stale relative to the stored kind, the current
        stored profile wins so endpoint/model and key remain one generation.
        """

        with self._lock:
            chosen = profile
            if chosen is None:
                chosen = self._profiles.get(self._selected_kind) if self._selected_kind else None
            else:
                stored = self._profiles.get(chosen.kind)
                if stored is not None and stored != chosen:
                    chosen = stored
            if chosen is None or not chosen.api_key_ref:
                return chosen, None
            if self.secret_store is None:
                raise LocalAISettingsError("provider API key is unavailable")
            try:
                return chosen, self.secret_store.get(chosen.api_key_ref)
            except SecretStoreError as exc:
                raise LocalAISettingsError("provider API key is unavailable") from exc

    def get(self, kind: str) -> ProviderConfig:
        with self._lock:
            if kind not in PROFILE_KINDS or kind not in self._profiles:
                raise LocalAISettingsError("provider profile is not configured")
            return self._profiles[kind]

    def profiles(self) -> tuple[ProviderConfig, ...]:
        with self._lock:
            return tuple(self._profiles.values())

    def _has_key(self, profile: ProviderConfig) -> bool:
        if not profile.api_key_ref or self.secret_store is None:
            return False
        try:
            return self.secret_store.has(profile.api_key_ref)
        except SecretStoreError:
            return False

    def public(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schemaVersion": 1,
                "selectedKind": self._selected_kind,
                "profiles": [
                    {
                        "id": profile.id,
                        "kind": profile.kind,
                        "label": profile.label or profile.kind,
                        "model": profile.model,
                        "effort": profile.effort,
                        "endpoint": profile.endpoint,
                        "opencodeProvider": profile.opencode_provider,
                        "allowRemote": profile.allow_remote,
                        "enabled": profile.enabled,
                        "hasApiKey": self._has_key(profile),
                    }
                    for profile in self._profiles.values()
                ],
            }

    def load(self) -> None:
        with self._lock:
            if self.path is None or not self.path.exists():
                return
            try:
                with self.path.open("rb") as source:
                    raw = source.read(MAX_SETTINGS_BYTES + 1)
                if len(raw) > MAX_SETTINGS_BYTES:
                    raise LocalAISettingsError("AI settings file is too large")
                value = json.loads(raw.decode("utf-8"))
            except LocalAISettingsError:
                raise
            except (OSError, UnicodeError, ValueError, RecursionError) as exc:
                raise LocalAISettingsError("AI settings could not be opened") from exc
            if (
                not isinstance(value, Mapping)
                or set(value) - _PUBLIC_KEYS
                or type(value.get("schemaVersion")) is not int
                or value.get("schemaVersion") != 1
            ):
                raise LocalAISettingsError("AI settings format is invalid")
            profiles = value.get("profiles")
            if not isinstance(profiles, list) or len(profiles) > len(PROFILE_KINDS):
                raise LocalAISettingsError("AI settings profiles are invalid")
            loaded: dict[str, ProviderConfig] = {}
            for item in profiles:
                if not isinstance(item, Mapping):
                    raise LocalAISettingsError("AI settings profile is invalid")
                allowed = {
                    "id", "kind", "label", "model", "effort", "endpoint", "opencodeProvider",
                    "allowRemote", "enabled", "hasApiKey",
                }
                if set(item) - allowed:
                    raise LocalAISettingsError("AI settings profile is invalid")
                if any(
                    item.get(key) is not None and not isinstance(item[key], str)
                    for key in ("id", "kind", "label", "model", "effort", "endpoint", "opencodeProvider")
                ):
                    raise LocalAISettingsError("AI settings profile fields are invalid")
                kind = item.get("kind")
                if kind not in PROFILE_KINDS:
                    raise LocalAISettingsError("AI settings provider kind is invalid")
                if kind in loaded:
                    raise LocalAISettingsError("AI settings contains duplicate provider kinds")
                if type(item.get("hasApiKey", False)) is not bool:
                    raise LocalAISettingsError("AI settings key marker is invalid")
                if type(item.get("allowRemote", False)) is not bool or type(item.get("enabled", True)) is not bool:
                    raise LocalAISettingsError("AI settings provider flags are invalid")
                endpoint = item.get("endpoint") or _DEFAULT_ENDPOINTS.get(kind)
                model = item.get("model")
                if not isinstance(model, str):
                    raise LocalAISettingsError("AI settings model is invalid")
                try:
                    profile = ProviderConfig(
                        id=item.get("id") or f"local-{kind}",
                        kind=kind,
                        model=model,
                        effort=item.get("effort", "medium"),
                        endpoint=endpoint,
                        opencode_provider=item.get("opencodeProvider"),
                        api_key_ref=self._secret_ref(kind) if item.get("hasApiKey") else None,
                        allow_remote=item.get("allowRemote", False),
                        enabled=item.get("enabled", True),
                        label=item.get("label"),
                    )
                    loaded[kind] = self._normalize(profile)
                except (AIConfigError, ValueError) as exc:
                    raise LocalAISettingsError("AI settings profile is invalid") from exc
            selected = value.get("selectedKind")
            if (selected is not None and not isinstance(selected, str)) or (
                selected not in loaded if isinstance(selected, str) else bool(loaded)
            ):
                raise LocalAISettingsError("AI settings selected provider is invalid")
            self._profiles = loaded
            self._selected_kind = selected

    def save(self) -> None:
        with self._lock:
            if self.path is None:
                return
            payload = json.dumps(self.public(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(payload) > MAX_SETTINGS_BYTES:
                raise LocalAISettingsError("AI settings file is too large")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            except Exception:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                raise LocalAISettingsError("AI settings could not be saved") from None


class LocalAIProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    kind: ProfileKind
    model: str
    effort: Literal["low", "medium", "high", "xhigh"] = "medium"
    endpoint: str | None = None
    opencode_provider: str | None = Field(default=None, alias="opencodeProvider")
    allow_remote: bool = Field(default=False, alias="allowRemote")
    enabled: bool = True
    label: str | None = None
    api_key: str | None = Field(default=None, alias="apiKey", repr=False)
    clear_api_key: bool = Field(default=False, alias="clearApiKey")
    selected: bool | None = None


class LocalAIProfileSelectIn(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    kind: ProfileKind | None = None
    network: bool = False


def install_local_ai_routes(
    target: FastAPI | APIRouter,
    *,
    store: LocalAISettingsStore,
    client: Any,
    owner_dependency: Callable[..., Any] | None = None,
    write_dependency: Callable[..., Any] | None = None,
    prefix: str = "/api/v1/local/ai",
) -> APIRouter:
    """Install GET/PUT settings and POST metadata-only connectivity check.

    ``owner_dependency`` and ``write_dependency`` are injected by the root
    private API.  The helper never weakens that ACL or creates a second global
    RPC surface.
    """

    router = APIRouter(prefix=prefix, tags=["local-ai"], route_class=_SafeLocalAIRoute)
    read_dependencies = [Depends(owner_dependency)] if owner_dependency is not None else []
    write_dependencies = list(read_dependencies)
    if write_dependency is not None:
        write_dependencies.append(Depends(write_dependency))

    @router.get("", dependencies=read_dependencies)
    def get_local_ai_settings() -> dict[str, Any]:
        return store.public()

    @router.put("", dependencies=write_dependencies)
    def put_local_ai_settings(payload: LocalAIProfileIn) -> dict[str, Any]:
        endpoint = payload.endpoint or _DEFAULT_ENDPOINTS.get(payload.kind)
        try:
            profile = ProviderConfig(
                id=f"local-{payload.kind}",
                kind=payload.kind,
                model=payload.model,
                effort=payload.effort,
                endpoint=endpoint,
                opencode_provider=payload.opencode_provider or None,
                allow_remote=payload.allow_remote,
                enabled=payload.enabled,
                label=payload.label,
            )
            store.upsert(
                profile,
                api_key=payload.api_key,
                clear_api_key=payload.clear_api_key,
                selected=payload.selected,
            )
        except (AIConfigError, LocalAISettingsError) as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": getattr(exc, "code", "AI_SETTINGS_INVALID"), "message": str(exc)},
            ) from exc
        return store.public()

    @router.post("/check", dependencies=read_dependencies)
    def check_local_ai(payload: LocalAIProfileSelectIn | None = None) -> dict[str, Any]:
        requested = payload or LocalAIProfileSelectIn()
        try:
            profile = store.selected() if requested.kind is None else store.get(requested.kind)
        except LocalAISettingsError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": getattr(exc, "code", "AI_SETTINGS_INVALID"), "message": str(exc)},
            ) from exc
        if profile is None:
            return {"available": False, "configured": False, "network": False, "reason": "no_profile"}
        try:
            result = client.probe(profile, network=requested.network, secret_store=store.secret_store)
        except (CodexProtocolError, CodexUnavailable):
            raise HTTPException(
                status_code=503,
                detail={"code": "AI_UNAVAILABLE", "message": "provider check failed"},
            ) from None
        except (AIConfigError, OfflineAIError) as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": getattr(exc, "code", "AI_SETTINGS_INVALID"), "message": "provider check failed"},
            ) from None
        return {"profile": profile.kind, "result": result, "settings": store.public()}

    if isinstance(target, FastAPI):
        target.include_router(router)
    elif isinstance(target, APIRouter):
        target.include_router(router)
    else:
        raise TypeError("target must be FastAPI or APIRouter")
    return router


# Root integration uses this shorter seam name; both names install the exact
# same private router and require the caller's existing ACL dependencies.
install_settings_routes = install_local_ai_routes


__all__ = [
    "LocalAIProfileIn",
    "LocalAIProfileSelectIn",
    "LocalAISettingsError",
    "LocalAISettingsStore",
    "PROFILE_KINDS",
    "install_local_ai_routes",
    "install_settings_routes",
]

"""Provider router compatible with the existing LocalCodexClient seam.

The adapter keeps the application's original accepted/status/events/cancel
lifecycle while routing one explicit Send to the selected provider.  HTTP
providers run in one bounded background worker so the request handler returns
an accepted snapshot immediately.  OpenCode process control remains in the
root-owned native reverse gateway; this module never starts a process.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.codex_gateway import CodexProtocolError, CodexUnavailable
from offline_ai import (
    ALLOWED_ROUTES,
    MAX_OUTPUT_BYTES,
    MAX_PROMPT_BYTES,
    AICancelled,
    AIConfigError,
    AIProtocolError,
    AITimeout,
    AIUnavailable,
    OfflineAIClient,
    OfflineAIError,
    ProviderConfig,
    TutorDomainPacket,
    build_tutor_packet,
)
from offline_ai_gateway import OpenCodeGatewayClient
from offline_ai_settings import LocalAISettingsStore

_TERMINAL = frozenset({"succeeded", "failed", "timed_out", "cancelled", "unavailable"})
_ACTIVE = frozenset({"queued", "running", "cancelling"})
_MAX_RUNS = 128


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class _HTTPRun:
    run_id: str
    packet: TutorDomainPacket = field(repr=False)
    provider: ProviderConfig
    api_key: str | None = field(default=None, repr=False)
    status: str = "queued"
    error_code: str | None = None
    final_output: str = field(default="", repr=False)
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    completed_at: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)
    finished: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def provider_model(self) -> str:
        return self.provider.model

    def event(self, event_type: str) -> None:
        if len(self.events) >= 512:
            raise AIProtocolError("AI event limit exceeded")
        self.events.append({"sequence": len(self.events) + 1, "type": event_type, "created_at": _now()})

    def transition(self, status: str, error_code: str | None = None) -> None:
        if self.status in _TERMINAL or self.status == status:
            return
        self.status = status
        self.error_code = error_code
        if status == "running":
            self.started_at = self.started_at or _now()
        if status in _TERMINAL:
            self.completed_at = self.completed_at or _now()
        self.event(status)

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "input_digest": self.packet.input_digest,
            "manifest_hash": self.packet.manifest_sha256,
            "prompt_hash": self.packet.prompt_sha256,
            "model": self.packet.model,
            "effort": self.packet.effort,
            "tutor_mode": self.packet.tutor_mode,
            "status": self.status,
            "error_code": self.error_code,
            "final_output": self.final_output,
            "provider_model": self.provider_model,
            "provider_kind": self.provider.kind,
            "provider_info": {
                "id": self.provider.id,
                "kind": self.provider.kind,
                "model": self.provider_model,
                "effort": "default",
            },
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "event_count": len(self.events),
        }


class OfflineAIProviderAdapter:
    """Route one explicit Send to Codex, OpenCode, or bounded HTTP local AI.

    The public methods intentionally match the original ``LocalCodexClient``
    seam.  The ``codex`` capability object is kept at the original top level
    for existing application gates; ``provider`` carries the actual selected
    provider without relabelling it as Sol/Codex.
    """

    # ``app.ai_gateway.codex_status`` recognizes this attached local seam.
    # The actual selected backend remains visible in capabilities/provider_info.
    transport_kind = "native-codex"

    def __init__(
        self,
        *,
        settings: LocalAISettingsStore,
        codex_client: Any | None = None,
        opencode_client: OpenCodeGatewayClient | None = None,
        http_client: OfflineAIClient | None = None,
    ) -> None:
        self.settings = settings
        self.codex_client = codex_client
        self.opencode_client = opencode_client
        self.http_client = http_client or OfflineAIClient()
        self._lock = threading.RLock()
        self._run_kinds: dict[str, str] = {}
        self._active_run_ids: set[str] = set()
        self._http_runs: OrderedDict[str, _HTTPRun] = OrderedDict()

    @staticmethod
    def _compat_error(exc: Exception) -> Exception:
        if isinstance(exc, (CodexUnavailable, CodexProtocolError)):
            return exc
        if isinstance(exc, (AIConfigError, AIProtocolError)):
            return CodexProtocolError("AI provider request failed protocol validation")
        if isinstance(exc, OfflineAIError):
            return CodexUnavailable("AI provider is unavailable")
        return CodexUnavailable("AI provider is unavailable")

    def _profile(self, profile: ProviderConfig | None = None) -> ProviderConfig:
        chosen = profile or self.settings.selected()
        if chosen is None:
            raise AIUnavailable("no AI provider is selected")
        if not chosen.enabled:
            raise AIUnavailable("selected AI provider is disabled")
        return chosen

    def _profile_with_key(self, profile: ProviderConfig | None = None) -> tuple[ProviderConfig, str | None]:
        chosen, key = self.settings.capture(profile)
        if chosen is None:
            raise AIUnavailable("no AI provider is selected")
        if not chosen.enabled:
            raise AIUnavailable("selected AI provider is disabled")
        return chosen, key

    @staticmethod
    def _actual_model(profile: ProviderConfig, result: Mapping[str, Any] | None = None) -> str:
        candidate = result.get("provider_model") if result is not None else None
        model = candidate if isinstance(candidate, str) and candidate else profile.model
        if profile.kind == "opencode_cli" and "/" not in model and profile.opencode_provider:
            model = f"{profile.opencode_provider}/{model}"
        return model

    @staticmethod
    def _compat_codex(*, available: bool, reason: str) -> dict[str, Any]:
        return {
            "status": "ready" if available else "unavailable",
            "available": available,
            "reason": reason,
            "executable": None,
            "cli_contract": "native-codex-gateway-v1",
            "routes": [{"model": model, "effort": effort} for model, effort in sorted(ALLOWED_ROUTES)],
            "limits": {
                "max_prompt_bytes": MAX_PROMPT_BYTES,
                "max_wall_seconds": 120,
                "max_inactivity_seconds": 30,
                "max_stream_bytes": 512 * 1024,
                "max_events": 512,
                "max_final_bytes": MAX_OUTPUT_BYTES,
                "max_concurrent_runs": 1,
            },
        }

    def capabilities(self) -> dict[str, Any]:
        profile = self.settings.selected()
        result: dict[str, Any] = {
            "schema_version": 1,
            "selected": profile.kind if profile is not None else None,
            "settings": self.settings.public(),
            "providers": [
                {"kind": item.kind, "model": item.model, "effort": item.effort, "enabled": item.enabled}
                for item in self.settings.profiles()
            ],
        }
        if profile is None:
            result["codex"] = self._compat_codex(available=False, reason="no_profile")
            result["provider"] = {"available": False, "reason": "no_profile"}
            result["available"] = False
            result["reason"] = "no_profile"
            return result

        backend: dict[str, Any] | None = None
        available = False
        reason = "provider_client_not_attached"
        try:
            if profile.kind == "codex_cli":
                if self.codex_client is not None:
                    backend = self.codex_client.capabilities()
                    available = bool(backend.get("codex", {}).get("available")) if isinstance(backend, dict) else False
                    reason = "codex_backend_ready" if available else "codex_backend_unavailable"
            elif profile.kind == "opencode_cli":
                if self.opencode_client is not None:
                    backend = self.opencode_client.capabilities()
                    available = (
                        bool(backend.get("opencode", {}).get("available"))
                        if isinstance(backend, dict)
                        else False
                    )
                    reason = "opencode_backend_ready" if available else "opencode_backend_unavailable"
            else:
                # Configuration validation is local; reachability belongs to
                # the explicit POST /check action.
                backend = self.http_client.capabilities()
                available = True
                reason = "validated_without_network"
        except Exception:
            # Capability probes never disclose provider URLs, headers, or
            # exception bodies to the browser.
            backend = None
            available = False
            reason = "provider_capability_probe_failed"

        result["codex"] = self._compat_codex(available=available, reason=reason)
        result["provider"] = {
            "id": profile.id,
            "kind": profile.kind,
            "model": self._actual_model(profile),
            "effort": profile.effort if profile.kind == "opencode_cli" else "default",
            "available": available,
            "reason": reason,
            "backend": backend,
        }
        result["backend"] = backend
        result["available"] = available
        result["reason"] = reason
        return result

    def probe(
        self,
        profile: ProviderConfig | None = None,
        *,
        network: bool = False,
        secret_store: Any = None,
    ) -> dict[str, Any]:
        try:
            chosen = self._profile(profile)
            if chosen.kind == "codex_cli":
                if self.codex_client is None:
                    raise AIUnavailable("Codex client is not attached")
                return self.codex_client.capabilities()
            if chosen.kind == "opencode_cli":
                if self.opencode_client is None:
                    raise AIUnavailable("OpenCode client is not attached")
                return self.opencode_client.capabilities()
            return self.http_client.probe(
                chosen,
                network=network,
                secret_store=secret_store if secret_store is not None else self.settings.secret_store,
            )
        except Exception as exc:
            mapped = self._compat_error(exc)
            raise mapped from None

    @staticmethod
    def _actual_effort(result: Mapping[str, Any], requested_effort: str) -> str:
        provider_info = result.get("provider_info")
        if isinstance(provider_info, Mapping):
            value = provider_info.get("effort")
            if isinstance(value, str) and value:
                return value
        value = result.get("effort")
        return value if isinstance(value, str) and value else "default"

    def _annotate(self, result: dict[str, Any], profile: ProviderConfig, requested_effort: str) -> dict[str, Any]:
        actual_model = self._actual_model(profile, result)
        actual_effort = self._actual_effort(result, requested_effort)
        annotated = {
            **result,
            "content": result.get("final_output") if isinstance(result.get("final_output"), str) else "",
            "provider_kind": profile.kind,
            "provider_label": profile.label or profile.kind,
            "provider_model": actual_model,
            "provider_info": {
                "id": profile.id,
                "kind": profile.kind,
                "model": actual_model,
                "effort": actual_effort,
            },
        }
        gateway = annotated.get("gateway")
        if isinstance(gateway, dict):
            annotated["gateway"] = {
                **gateway,
                "provider_info": {
                    "id": profile.id,
                    "kind": profile.kind,
                    "model": actual_model,
                    "effort": actual_effort,
                },
            }
        elif profile.kind != "codex_cli":
            # App lifecycle code already understands this snapshot shape and
            # records provider_info without changing the original route.
            annotated["gateway"] = {
                key: value
                for key, value in annotated.items()
                if key
                in {
                    "schema_version",
                    "run_id",
                    "input_digest",
                    "manifest_hash",
                    "prompt_hash",
                    "model",
                    "effort",
                    "tutor_mode",
                    "status",
                    "error_code",
                    "final_output",
                    "provider_model",
                    "provider_kind",
                    "provider_info",
                    "created_at",
                    "started_at",
                    "completed_at",
                    "event_count",
                }
            }
        return annotated

    @staticmethod
    def _same_http_identity(run: _HTTPRun, packet: TutorDomainPacket, profile: ProviderConfig) -> bool:
        return run.packet.input_digest == packet.input_digest and run.provider == profile

    def _mark_active(self, run_id: str, status: Any) -> None:
        if isinstance(status, str) and status in _ACTIVE:
            self._active_run_ids.add(run_id)
        else:
            self._active_run_ids.discard(run_id)

    def _start_http(
        self,
        *,
        manifest: Mapping[str, Any],
        sections: Sequence[Mapping[str, Any]],
        model: str,
        effort: str,
        run_id: str,
        profile: ProviderConfig,
        api_key: str | None,
        cancel_event: threading.Event | None,
    ) -> dict[str, Any]:
        packet = build_tutor_packet(
            manifest=manifest,
            sections=sections,
            model=model,
            effort=effort,
            run_id=run_id,
        )
        if cancel_event is not None and cancel_event.is_set():
            raise AICancelled("AI request cancelled")
        with self._lock:
            existing_kind = self._run_kinds.get(run_id)
            if existing_kind is not None and existing_kind != "http":
                raise AIProtocolError("AI run idempotency conflict")
            previous = self._http_runs.get(run_id)
            if previous is not None:
                if not self._same_http_identity(previous, packet, profile):
                    raise AIProtocolError("AI run idempotency conflict")
                return {"accepted": True, **previous.snapshot()}
            if any(item != run_id and item in self._active_run_ids for item in self._run_kinds):
                raise AIUnavailable("AI already has an active request")
            if len(self._http_runs) >= _MAX_RUNS:
                oldest = next(
                    (
                        key_id
                        for key_id, item in self._http_runs.items()
                        if item.status in _TERMINAL and item.finished.is_set()
                    ),
                    None,
                )
                if oldest is None:
                    raise AIUnavailable("AI record limit reached")
                self._http_runs.pop(oldest)
                self._run_kinds.pop(oldest, None)
            run = _HTTPRun(run_id=run_id, packet=packet, provider=profile, api_key=api_key)
            if cancel_event is not None:
                run.cancel_event = cancel_event
            run.event("queued")
            self._http_runs[run_id] = run
            self._run_kinds[run_id] = "http"
            self._active_run_ids.add(run_id)
            try:
                worker = threading.Thread(target=self._http_worker, args=(run,), name="kontur-ai-http", daemon=True)
                run.thread = worker
                worker.start()
            except Exception:
                run.api_key = None
                run.thread = None
                run.finished.set()
                self._active_run_ids.discard(run_id)
                self._http_runs.pop(run_id, None)
                self._run_kinds.pop(run_id, None)
                raise AIUnavailable("AI worker could not start") from None
            snapshot = run.snapshot()
        return {"accepted": True, **snapshot, "gateway": {**snapshot}}

    def _http_worker(self, run: _HTTPRun) -> None:
        try:
            with self._lock:
                if run.status in _TERMINAL:
                    return
                if run.cancel_event.is_set() or run.status == "cancelling":
                    run.transition("cancelled", "AGENT_CANCELLED")
                    return
                run.transition("running")
            try:
                result = self.http_client.request(
                    packet=run.packet,
                    provider=run.provider,
                    api_key=run.api_key,
                    cancel_event=run.cancel_event,
                )
                if (
                    result.manifest_sha256 != run.packet.manifest_sha256
                    or result.prompt_sha256 != run.packet.prompt_sha256
                    or not isinstance(result.content, str)
                    or len(result.content.encode("utf-8")) > MAX_OUTPUT_BYTES
                ):
                    raise AIProtocolError("AI provider response identity is invalid")
            except Exception as exc:
                with self._lock:
                    if run.status in _TERMINAL:
                        return
                    if run.cancel_event.is_set() or isinstance(exc, AICancelled):
                        run.transition("cancelled", "AGENT_CANCELLED")
                    elif isinstance(exc, AITimeout):
                        run.transition("timed_out", "AGENT_TIMED_OUT")
                    elif isinstance(exc, (AIProtocolError, AIConfigError)):
                        run.transition("failed", "AGENT_PROTOCOL_ERROR")
                    elif isinstance(exc, AIUnavailable):
                        run.transition("unavailable", "CODEX_UNAVAILABLE")
                    else:
                        run.transition("unavailable", "CODEX_UNAVAILABLE")
                return
            with self._lock:
                if run.status in _TERMINAL:
                    return
                if run.cancel_event.is_set() or run.status == "cancelling":
                    run.transition("cancelled", "AGENT_CANCELLED")
                    return
                run.final_output = result.content
                run.transition("succeeded")
        finally:
            with self._lock:
                # A worker can only leave this method after the provider
                # transport has returned or raised.  Keep the run active until
                # this point so a new Send cannot evict/replace live work.
                self._active_run_ids.discard(run.run_id)
                run.api_key = None
                run.finished.set()

    def run(
        self,
        *,
        manifest: Mapping[str, Any],
        sections: Sequence[Mapping[str, Any]],
        model: str,
        effort: str,
        run_id: str,
        provider: ProviderConfig | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        try:
            chosen, captured_key = self._profile_with_key(provider)
            if chosen.kind == "codex_cli":
                if self.codex_client is None:
                    raise AIUnavailable("Codex client is not attached")
                # Preserve the original Codex response and model semantics;
                # actual provider metadata is only added for non-Codex routes.
                with self._lock:
                    existing_kind = self._run_kinds.get(run_id)
                    if existing_kind is not None and existing_kind != "codex_cli":
                        raise AIProtocolError("AI run idempotency conflict")
                    if existing_kind is None and self._active_run_ids:
                        raise AIUnavailable("AI already has an active request")
                    self._run_kinds[run_id] = "codex_cli"
                result = self.codex_client.run(
                    manifest=manifest,
                    sections=list(sections),
                    model=model,
                    effort=effort,
                    run_id=run_id,
                )
                with self._lock:
                    self._mark_active(run_id, result.get("status") if isinstance(result, dict) else None)
                return result
            if chosen.kind == "opencode_cli":
                if self.opencode_client is None:
                    raise AIUnavailable("OpenCode client is not attached")
                with self._lock:
                    existing_kind = self._run_kinds.get(run_id)
                    if existing_kind is not None and existing_kind != "opencode_cli":
                        raise AIProtocolError("AI run idempotency conflict")
                    if existing_kind is None and self._active_run_ids:
                        raise AIUnavailable("AI already has an active request")
                    self._run_kinds[run_id] = "opencode_cli"
                result = self.opencode_client.run(
                    manifest=manifest,
                    sections=list(sections),
                    model=model,
                    effort=effort,
                    run_id=run_id,
                    provider=chosen,
                    api_key=captured_key,
                )
                with self._lock:
                    self._mark_active(run_id, result.get("status") if isinstance(result, dict) else None)
                return self._annotate(result, chosen, effort)
            return self._annotate(
                self._start_http(
                    manifest=manifest,
                    sections=sections,
                    model=model,
                    effort=effort,
                    run_id=run_id,
                    profile=chosen,
                    api_key=captured_key,
                    cancel_event=cancel_event,
                ),
                chosen,
                effort,
            )
        except (CodexUnavailable, CodexProtocolError):
            raise
        except Exception as exc:
            raise self._compat_error(exc) from None

    def request(
        self,
        packet: TutorDomainPacket,
        *,
        provider: ProviderConfig | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        return self.run(
            manifest=packet.manifest,
            sections=packet.sections,
            model=packet.model,
            effort=packet.effort,
            run_id=packet.run_id,
            provider=provider,
            cancel_event=cancel_event,
        )

    def status(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            kind = self._run_kinds.get(run_id)
            if kind == "http":
                run = self._http_runs.get(run_id)
                if run is None:
                    raise CodexUnavailable("AI run is not available")
                return run.snapshot()
        try:
            if kind == "codex_cli":
                if self.codex_client is None:
                    raise AIUnavailable("Codex client is not attached")
                result = self.codex_client.status(run_id)
                with self._lock:
                    self._mark_active(run_id, result.get("status") if isinstance(result, dict) else None)
                return result
            if kind == "opencode_cli":
                if self.opencode_client is None:
                    raise AIUnavailable("OpenCode client is not attached")
                result = self.opencode_client.status(run_id)
                with self._lock:
                    self._mark_active(run_id, result.get("status") if isinstance(result, dict) else None)
                return result
            raise AIUnavailable("AI run is not available")
        except (CodexUnavailable, CodexProtocolError):
            raise
        except Exception as exc:
            raise self._compat_error(exc) from None

    def events(self, run_id: str, *, after: int = 0) -> dict[str, Any]:
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise CodexProtocolError("AI event cursor is invalid")
        with self._lock:
            kind = self._run_kinds.get(run_id)
            if kind == "http":
                run = self._http_runs.get(run_id)
                if run is None:
                    raise CodexUnavailable("AI run is not available")
                events = [dict(item) for item in run.events if item["sequence"] > after]
                return {
                    "schema_version": 1,
                    "run_id": run_id,
                    "events": events,
                    "next_after": max(after, len(run.events)),
                }
        try:
            if kind == "codex_cli":
                if self.codex_client is None:
                    raise AIUnavailable("Codex client is not attached")
                return self.codex_client.events(run_id, after=after)
            if kind == "opencode_cli":
                if self.opencode_client is None:
                    raise AIUnavailable("OpenCode client is not attached")
                return self.opencode_client.events(run_id, after=after)
            raise AIUnavailable("AI run is not available")
        except (CodexUnavailable, CodexProtocolError):
            raise
        except Exception as exc:
            raise self._compat_error(exc) from None

    def cancel(self, run_id: str) -> dict[str, Any] | bool:
        should_cancel = False
        with self._lock:
            kind = self._run_kinds.get(run_id)
            if kind == "http":
                run = self._http_runs.get(run_id)
                if run is None:
                    raise CodexUnavailable("AI run is not available")
                if run.status not in _TERMINAL:
                    run.cancel_event.set()
                    run.transition("cancelling")
                    should_cancel = True
                snapshot = run.snapshot()
            else:
                snapshot = None
        if kind == "http":
            if should_cancel:
                self.http_client.cancel(run_id)
            assert snapshot is not None
            return snapshot
        try:
            if kind == "codex_cli":
                if self.codex_client is None:
                    raise AIUnavailable("Codex client is not attached")
                result = self.codex_client.cancel(run_id)
                with self._lock:
                    self._mark_active(run_id, result.get("status") if isinstance(result, dict) else None)
                return result
            if kind == "opencode_cli":
                if self.opencode_client is None:
                    raise AIUnavailable("OpenCode client is not attached")
                result = self.opencode_client.cancel(run_id)
                with self._lock:
                    self._mark_active(run_id, result.get("status") if isinstance(result, dict) else None)
                return result
            raise AIUnavailable("AI run is not available")
        except (CodexUnavailable, CodexProtocolError):
            raise
        except Exception as exc:
            raise self._compat_error(exc) from None

    def quiesce(self, timeout: float = 60.0) -> None:
        """Cancel and join adapter-owned work before profile/storage teardown."""

        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be positive")
        with self._lock:
            http_runs = [
                run
                for run in self._http_runs.values()
                if run.status in _ACTIVE or (run.thread is not None and run.thread.is_alive())
            ]
            opencode_ids = [
                run_id
                for run_id, kind in self._run_kinds.items()
                if kind == "opencode_cli" and run_id in self._active_run_ids
            ]
            for run in http_runs:
                if run.status not in _TERMINAL:
                    run.cancel_event.set()
                    run.transition("cancelling")
        for run in http_runs:
            try:
                self.http_client.cancel(run.run_id)
            except Exception:
                pass
        if self.opencode_client is not None:
            for run_id in opencode_ids:
                try:
                    result = self.opencode_client.cancel(run_id)
                    with self._lock:
                        self._mark_active(run_id, result.get("status") if isinstance(result, dict) else None)
                except Exception:
                    # A live unknown run is not silently ignored by the final
                    # active-id check below.
                    pass
        deadline = time.monotonic() + float(timeout)
        for run in http_runs:
            worker = run.thread
            if worker is not None:
                worker.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            if any(run.thread is not None and run.thread.is_alive() for run in http_runs):
                raise CodexUnavailable("AI request cancellation did not finish")
            if any(run_id in self._active_run_ids for run_id in opencode_ids):
                raise CodexUnavailable("OpenCode request cancellation did not finish")


__all__ = ["OfflineAIProviderAdapter"]

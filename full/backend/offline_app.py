"""Explicit local bootstrap and in-process ASGI; never opens a listening socket."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from offline_protocol import (
    MAX_RESPONSE_BYTES,
    BridgeError,
    encoded_response,
    quiesce_gateway,
    validate_headers,
    validate_request,
)

OWNER_ID = "owner"


class ASGIAdapter:
    def __init__(self, app: Any, marker: object) -> None:
        self.app = app
        self.marker = marker
        self.background: set[asyncio.Task[Any]] = set()
        self.background_failed = False

    async def request(
        self, method: str, path: str, body: str = "", headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        method, path, body = validate_request(method, path, body)
        supplied_headers = validate_headers(headers, OWNER_ID)
        parts = urlsplit(path)
        if parts.path == "/api/v1/auth/session" and method in {"POST", "DELETE"}:
            # Authentication is the inherited private process context. No cookie
            # is issued to, read from or required from the browser.
            return encoded_response(204, [("cache-control", "no-store")], b"")
        received = False
        complete = asyncio.Event()
        status = 500
        response_headers: list[tuple[str, str]] = []
        chunks: list[bytes] = []
        total = 0

        async def receive() -> dict[str, Any]:
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": body.encode("utf-8"), "more_body": False}
            await complete.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            nonlocal status, response_headers, total
            if message["type"] == "http.response.start":
                status = int(message["status"])
                response_headers = [
                    (k.decode("latin-1"), v.decode("latin-1"))
                    for k, v in message.get("headers", [])
                    if k.lower() not in {b"set-cookie", b"authorization"}
                ]
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise BridgeError("response_too_large", "Ответ превышает допустимый размер.")
                chunks.append(chunk)
                if not message.get("more_body", False):
                    complete.set()

        request_headers = [(b"host", b"offline.local")]
        if not any(key == b"content-type" for key, _ in supplied_headers):
            request_headers.append((b"content-type", b"application/json"))
        request_headers.extend(supplied_headers)
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": unquote(parts.path),
            "raw_path": parts.path.encode("utf-8"),
            "query_string": parts.query.encode("utf-8"),
            "root_path": "",
            "headers": request_headers,
            "client": ("127.0.0.1", 0),
            "server": ("offline.local", 0),
            "kontur.private_process": self.marker,
        }
        task = asyncio.create_task(self.app(scope, receive, send))
        self.background.add(task)

        def finished(value: asyncio.Task[Any]) -> None:
            self.background.discard(value)
            if not value.cancelled() and value.exception() is not None:
                self.background_failed = True

        task.add_done_callback(finished)
        waiting = asyncio.create_task(complete.wait())
        try:
            await asyncio.wait({task, waiting}, return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                await task
            if not complete.is_set():
                raise BridgeError("asgi_protocol", "Локальный обработчик не завершил ответ.")
        finally:
            waiting.cancel()
        data = b"".join(chunks)
        if parts.path == "/api/v1/capabilities" and status == 200:
            value = json.loads(data)
            value["auth"] = {"available": True, "mode": "local-process"}
            data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            response_headers = [(k, v) for k, v in response_headers if k.lower() != "content-length"]
            response_headers.append(("content-length", str(len(data))))
        return encoded_response(status, response_headers, data)

    async def drain(self, timeout: float = 120.0) -> None:
        pending = set(self.background)
        if pending:
            _done, remaining = await asyncio.wait(pending, timeout=timeout)
            if remaining:
                raise BridgeError("flush_pending_mutations", "Дождитесь завершения фонового сохранения.")
        if self.background_failed:
            raise BridgeError(
                "background_failed", "Фоновая операция завершилась с ошибкой. Сохранение не подтверждено."
            )


class QuiescentCodex:
    """Fence new background starts while original cancellation/persistence drains."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.condition = threading.Condition()
        self.suspended = False
        self.starting = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)

    def run(self, *args: Any, **kwargs: Any) -> Any:
        from app.codex_gateway import CodexUnavailable

        with self.condition:
            if self.suspended:
                raise CodexUnavailable("Local requests are being committed")
            self.starting += 1
        try:
            return self.client.run(*args, **kwargs)
        finally:
            with self.condition:
                self.starting -= 1
                self.condition.notify_all()

    def suspend(self) -> None:
        import time

        deadline = time.monotonic() + 60
        with self.condition:
            self.suspended = True
            while self.starting:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BridgeError("flush_pending_ai", "Запрос ИИ ещё завершается.")
                self.condition.wait(remaining)

    def resume(self) -> None:
        with self.condition:
            self.suspended = False


class OfflineApplication:
    def __init__(
        self,
        profile: Path,
        bundle: Path,
        gateway_call: Callable[[str, dict[str, Any]], dict[str, Any]],
        legacy_store: Path | None = None,
        gateway_quiesce: Callable[[], None] | None = None,
    ) -> None:
        self.profile = profile.resolve()
        self.bundle = bundle.resolve()
        self.gateway_call = gateway_call
        self.gateway_quiesce = gateway_quiesce or (lambda: quiesce_gateway(gateway_call))
        self.legacy_store = legacy_store
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever, name="kontur-asgi", daemon=True)
        self._loop_thread.start()
        self.app: Any = None
        self.adapter: ASGIAdapter | None = None
        self.client: Any = None
        self.transfer: Any = None

    def _await(self, coroutine: Any, timeout: float = 180.0) -> Any:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(timeout=timeout)

    def start(self) -> None:
        self._await(self._start())

    async def _start(self) -> None:
        from fastapi import HTTPException, Request
        from sqlalchemy import event

        from app.auth import OwnerPrincipal, ensure_owner, require_owner
        from app.config import Settings
        from app.main import create_app
        from app.security import SecurityGuard
        from local_codex import LocalCodexClient
        from offline_ai import ProviderConfig
        from offline_ai_adapter import OfflineAIProviderAdapter
        from offline_ai_gateway import OpenCodeGatewayClient
        from offline_ai_secrets import SecretStore
        from offline_ai_settings import LocalAISettingsError, LocalAISettingsStore, install_local_ai_routes
        from offline_catalog import load_catalog_seed
        from offline_state import install_client_state_routes
        from offline_transfer import TransferManager

        self.profile.mkdir(parents=True, exist_ok=True)
        asset_root = self.profile / "assets"
        asset_root.mkdir(exist_ok=True)
        settings = Settings(
            database_url="sqlite:///" + (self.profile / "app.sqlite3").as_posix(),
            asset_root=asset_root,
            owner_id=OWNER_ID,
            owner_dev_mode=False,
            testing=False,
            security_mode="production",
            source_mode="manual",
            source_network_enabled=False,
            source_permission_status="private_authorized",
            codex_enabled=True,
            codex_attested=True,
            codex_base_url=None,
            runner_mode="disabled",
            runner_attested=False,
            max_asset_bytes=64_000_000,
            max_asset_cache_bytes=2 * 1024**3,
        )
        self.app = create_app(settings)

        @event.listens_for(self.app.state.engine, "connect")
        def durable_connection(connection: Any, _record: Any) -> None:
            connection.execute("PRAGMA synchronous=FULL")

        marker = object()

        class PrivateProcessGuard(SecurityGuard):
            def authorize(guard_self: Any, request: Any, *, owner_id: str, csrf_exempt: bool = False) -> None:
                if request.scope.get("kontur.private_process") is not marker or owner_id != OWNER_ID:
                    raise HTTPException(
                        403,
                        {"code": "LOCAL_CONTEXT_REQUIRED", "message": "Private local context required", "details": {}},
                    )
                bucket, limit = guard_self._bucket(request)
                retry = guard_self.rate_limiter.check(
                    owner_id=OWNER_ID, bucket=bucket, limit=limit, window_seconds=settings.rate_window_seconds
                )
                if retry is not None:
                    raise HTTPException(
                        429,
                        {"code": "RATE_LIMITED", "message": "Local operation rate limit reached", "details": {}},
                        headers={"Retry-After": str(retry)},
                    )

        guard = PrivateProcessGuard(settings)

        def local_owner(request: Any) -> Any:
            guard.authorize(request, owner_id=OWNER_ID)
            return OwnerPrincipal(owner_id=OWNER_ID)

        local_owner.__annotations__ = {"request": Request, "return": OwnerPrincipal}
        self.app.dependency_overrides[require_owner] = local_owner
        self.app.state.security_guard = guard
        private_ai = self.profile / "private-ai"
        ai_settings_path = private_ai / "settings.json"
        secrets = SecretStore(private_ai / "provider-keys.dpapi")
        try:
            ai_settings = LocalAISettingsStore(secret_store=secrets, path=ai_settings_path)
        except (LocalAISettingsError, ValueError):
            # A damaged optional connection must not hide the task bank. Keep
            # its original file until an explicit settings Save repairs it.
            ai_settings = LocalAISettingsStore(secret_store=secrets)
            ai_settings.path = ai_settings_path
        else:
            if ai_settings.selected() is None and not ai_settings_path.exists():
                ai_settings.upsert(ProviderConfig("local-codex_cli", "codex_cli", "auto"), selected=True)
        self.ai_provider = OfflineAIProviderAdapter(
            settings=ai_settings,
            codex_client=LocalCodexClient(self.gateway_call),
            opencode_client=OpenCodeGatewayClient(self.gateway_call),
        )
        self.client = QuiescentCodex(self.ai_provider)
        self.app.state.codex_client = self.client
        from app.api_common import owner_session

        install_local_ai_routes(self.app, store=ai_settings, client=self.ai_provider, owner_dependency=owner_session)
        install_client_state_routes(self.app)
        self.adapter = ASGIAdapter(self.app, marker)
        self._lifespan = self.app.router.lifespan_context(self.app)
        await self._lifespan.__aenter__()
        with self.app.state.session_factory.begin() as session:
            ensure_owner(session, OWNER_ID)
        load_catalog_seed(
            self.app.state.engine, self.app.state.session_factory, settings, self.bundle, self.profile, OWNER_ID
        )
        self.transfer = TransferManager(
            self.app.state.engine, self.app.state.session_factory, settings, self.profile, owner_id=OWNER_ID
        )
        if self.legacy_store is not None:
            self.transfer.migrate_mini_store(self.legacy_store)

    def request(self, method: str, path: str, body: str = "", headers: dict[str, str] | None = None) -> dict[str, Any]:
        if self.adapter is None:
            raise BridgeError("backend_not_ready", "Локальная база ещё не готова.")
        return self._await(self.adapter.request(method, path, body, headers))

    def flush(self) -> dict[str, Any]:
        """Fence starts, persist original cancellations, drain writes, checkpoint."""
        if self.app is None or self.adapter is None or self.client is None:
            raise BridgeError("backend_not_ready", "Локальная база ещё не готова.")
        from sqlalchemy import select

        from app.ai_gateway import record_disabled_run
        from app.api import _AGENT_ACTIVE_STATUSES, _agent_event_once
        from app.models import AgentRun, ExamStudyAiRun

        try:
            self.client.suspend()
            self._await(self.adapter.drain(), timeout=125)
            # Native cancellation is asynchronous. Wait for its terminal result
            # before persisting original API snapshots, including orphan starts.
            self.gateway_quiesce()
            with self.app.state.session_factory() as session:
                study = list(
                    session.execute(
                        select(
                            ExamStudyAiRun.id,
                            ExamStudyAiRun.exam_session_id,
                            ExamStudyAiRun.exam_item_id,
                            ExamStudyAiRun.agent_run_id,
                        )
                        .join(AgentRun, AgentRun.id == ExamStudyAiRun.agent_run_id)
                        .where(
                            ExamStudyAiRun.owner_id == OWNER_ID,
                            AgentRun.owner_id == OWNER_ID,
                            AgentRun.status.in_(_AGENT_ACTIVE_STATUSES),
                        )
                    )
                )
                study_agent_ids = {row.agent_run_id for row in study}
                active = list(
                    session.scalars(
                        select(AgentRun.id).where(
                            AgentRun.owner_id == OWNER_ID, AgentRun.status.in_(_AGENT_ACTIVE_STATUSES)
                        )
                    )
                )
            paths = [
                f"/api/v1/exams/{row.exam_session_id}/items/{row.exam_item_id}/study-ai/runs/{row.id}/cancel"
                for row in study
            ]
            paths.extend(
                f"/api/v1/assistant/runs/{run_id}/cancel" for run_id in active if run_id not in study_agent_ids
            )
            for path in paths:
                self.request("POST", path)
            quiesce = getattr(self.client, "quiesce", None)
            if callable(quiesce):
                quiesce()
            self._await(self.adapter.drain(), timeout=125)
            with self.app.state.session_factory.begin() as session:
                interrupted = session.scalars(
                    select(AgentRun).where(AgentRun.owner_id == OWNER_ID, AgentRun.status.in_(_AGENT_ACTIVE_STATUSES))
                ).all()
                for run in interrupted:
                    # A restarted child has no in-memory gateway mapping. Native
                    # is confirmed idle; retain history as an explicit failure,
                    # never manufacture a successful answer or grading event.
                    record_disabled_run(session, owner_id=OWNER_ID, run=run, code="LOCAL_RUNTIME_INTERRUPTED")
                    _agent_event_once(
                        session,
                        run=run,
                        event_type="completed",
                        payload={"status": run.status, "error_code": run.error_code},
                        gateway_event_key="offline-quiescence",
                    )
            with self.app.state.session_factory() as session:
                if (
                    session.scalar(
                        select(AgentRun.id)
                        .where(AgentRun.owner_id == OWNER_ID, AgentRun.status.in_(_AGENT_ACTIVE_STATUSES))
                        .limit(1)
                    )
                    is not None
                ):
                    raise BridgeError("flush_pending_ai", "Запрос ИИ ещё завершается.")
            with self.app.state.engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA synchronous=FULL")
                checkpoint = connection.exec_driver_sql("PRAGMA wal_checkpoint(FULL)").fetchone()
            if checkpoint is None or int(checkpoint[0]) != 0 or int(checkpoint[1]) != int(checkpoint[2]):
                raise BridgeError("flush_pending_mutations", "Дождитесь завершения сохранения.")
            return {"committed": True, "pendingMutations": 0}
        finally:
            self.client.resume()

    def close(self) -> None:
        try:
            if self.app is not None and hasattr(self, "_lifespan"):
                self._await(self._lifespan.__aexit__(None, None, None), timeout=30)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop_thread.join(timeout=5)
            self._loop.close()

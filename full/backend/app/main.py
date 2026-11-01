"""FastAPI application factory for the private EGE workspace."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from .api import router
from .codex_gateway import CodexUnavailable, HttpCodexClient
from .config import Settings
from .db import init_schema, make_engine, make_session_factory
from .device_assets import router as device_assets_router
from .learning import seed_taxonomy
from .official_blueprints import router as official_blueprints_router
from .runner_gateway import HttpRunnerClient, RunnerUnavailable
from .security import SecurityGuard
from .security import router as security_router
from .sync_records import router as sync_records_router


def _error_content(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"code": code, "message": message, "details": details or {}}


def _http_exception_content(exc: HTTPException) -> dict[str, Any]:
    if isinstance(exc.detail, dict) and {"code", "message", "details"}.issubset(exc.detail):
        return {
            "code": str(exc.detail["code"]),
            "message": str(exc.detail["message"]),
            "details": exc.detail.get("details") or {},
        }
    return _error_content("HTTP_ERROR", str(exc.detail), {})


def create_app(settings: Settings | None = None) -> FastAPI:
    current = settings or Settings.from_env()
    engine = make_engine(current.database_url)
    session_factory = make_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_schema(engine)
        with session_factory.begin() as session:
            seed_taxonomy(session)
        yield
        engine.dispose()

    app = FastAPI(
        title="EGE Informatics Workspace API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = current
    app.state.security_guard = SecurityGuard(current)
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.runner_client = None
    if current.runner_base_url:
        try:
            app.state.runner_client = HttpRunnerClient(current)
        except RunnerUnavailable:
            # Configuration errors keep the capability explicitly unavailable;
            # no startup probe or network call is performed here.
            app.state.runner_client = None
    app.state.codex_client = None
    if current.codex_base_url and current.codex_enabled and current.codex_attested:
        try:
            # The adapter is attached only after the deployment-level gate is
            # enabled.  No capability probe or network request happens during
            # import/startup; the first request remains bounded and fail-closed.
            app.state.codex_client = HttpCodexClient(current)
        except CodexUnavailable:
            app.state.codex_client = None
    app.state.source_client = None

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=_http_exception_content(exc), headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_error_content("VALIDATION_ERROR", "Request validation failed", {"errors": exc.errors()}),
        )

    @app.exception_handler(IntegrityError)
    async def integrity_exception_handler(_request: Request, _exc: IntegrityError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=_error_content("IDEMPOTENCY_CONFLICT", "Database constraint rejected the operation", {}),
        )

    @app.get("/health", include_in_schema=False)
    @app.get("/api/v1/health", include_in_schema=True)
    def health() -> dict[str, Any]:
        return {"status": "ok", "database": "configured"}

    app.include_router(security_router, prefix="/api/v1")
    app.include_router(official_blueprints_router, prefix="/api/v1")
    app.include_router(router, prefix="/api/v1")
    app.include_router(device_assets_router, prefix="/api/v1")
    app.include_router(sync_records_router, prefix="/api/v1")
    return app


# The local child imports this factory with explicit Settings. Do not construct
# an environment-configured global application in the packaged offline runtime.

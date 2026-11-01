from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from backend.app.auth import issue_session_token
from backend.app.config import Settings
from backend.app.main import create_app


def test_two_independent_frontend_stores_against_real_http_api(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    node = shutil.which("node")
    if not node or not (root / "frontend/node_modules/typescript").is_dir():
        pytest.skip("Node/TypeScript required for desktop frontend-store integration")
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{(tmp_path / 'TEST_ONLY-http.sqlite').as_posix()}",
        asset_root=tmp_path / "assets",
        owner_session_secret="TEST_ONLY-http-secret",
        testing=True,
        source_mode="fixture",
        source_permission_status="private_authorized",
    )
    app = create_app(settings)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "The synthetic loopback API did not become ready."
            config = {
                "url": f"http://127.0.0.1:{port}",
                "token": issue_session_token("TEST_ONLY-owner", settings.owner_session_secret),
                "foreignToken": issue_session_token("TEST_ONLY-foreign", settings.owner_session_secret),
                "cookieName": settings.owner_session_cookie,
                "foreignCookie": issue_session_token(
                    "TEST_ONLY-foreign", settings.owner_session_secret, expires_at=int(time.time()) + 1800
                ),
            }
            result = subprocess.run(
                [node, str(root / "frontend/tests/owner-sync-real-api.integration.mjs")],
                input=json.dumps(config),
                capture_output=True,
                text=True,
                timeout=40,
                cwd=root / "frontend",
                check=False,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert "real HTTP API" in result.stdout and "PASS" in result.stdout
        finally:
            server.should_exit = True
            thread.join(timeout=10)

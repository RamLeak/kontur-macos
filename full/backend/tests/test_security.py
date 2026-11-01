from __future__ import annotations

from pathlib import Path

from backend.app.auth import issue_session_token, verify_session_token
from backend.app.config import Settings
from backend.app.main import create_app
from fastapi.testclient import TestClient

SECRET = "owner-session-secret-with-at-least-32-bytes"
ORIGIN = "https://ege.example"


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "asset_root": tmp_path / "assets",
        "owner_session_secret": SECRET,
        "security_mode": "production",
        "owner_public_origin": ORIGIN,
        "trusted_proxy_cidrs": ("127.0.0.1/32",),
        "rate_window_seconds": 60,
        "rate_read_limit": 20,
        "rate_mutation_limit": 20,
        "rate_sensitive_limit": 20,
        "rate_stream_limit": 2,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _bootstrap(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/session",
        headers={
            "Origin": ORIGIN,
            "X-Owner-Session": issue_session_token("owner", SECRET),
        },
    )
    assert response.status_code == 204


def test_v2_session_tokens_expire_and_reject_tampering() -> None:
    token = issue_session_token("owner", SECRET, expires_at=1_800)
    assert verify_session_token(token, SECRET, now=1_799) == "owner"
    assert verify_session_token(token, SECRET, now=1_800) is None
    assert verify_session_token(token + "x", SECRET, now=1_799) is None


def test_production_bootstrap_issues_hardened_cookie_pair(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 51000)) as client:
        response = client.post(
            "/api/v1/auth/session",
            headers={
                "Origin": ORIGIN,
                "X-Owner-Session": issue_session_token("owner", SECRET),
            },
        )
        assert response.status_code == 204
        cookies = response.headers.get_list("set-cookie")
        assert any("ege_owner_session=" in value and "HttpOnly" in value for value in cookies)
        assert any("ege_csrf=" in value and "HttpOnly" not in value for value in cookies)
        assert all("Secure" in value and "SameSite=strict" in value for value in cookies)
        assert response.headers["cache-control"] == "no-store"

        authenticated = client.get("/api/v1/capabilities")
        assert authenticated.status_code == 200


def test_production_rejects_untrusted_proxy_header(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app, base_url=ORIGIN, client=("198.51.100.9", 51000)) as client:
        response = client.get(
            "/api/v1/capabilities",
            headers={"X-Owner-Session": issue_session_token("owner", SECRET)},
        )
    assert response.status_code == 401
    assert response.json()["code"] == "TRUSTED_PROXY_REQUIRED"


def test_unsafe_request_requires_exact_origin_host_and_csrf(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 51000)) as client:
        _bootstrap(client)
        missing_csrf = client.post("/api/v1/projects", json={"name": "Plan"}, headers={"Origin": ORIGIN})
        assert missing_csrf.status_code == 403
        assert missing_csrf.json()["code"] == "CSRF_REQUIRED"

        csrf = client.cookies.get("ege_csrf")
        assert csrf
        bad_origin = client.post(
            "/api/v1/projects",
            json={"name": "Plan"},
            headers={"Origin": "https://evil.example", "X-CSRF-Token": csrf},
        )
        assert bad_origin.status_code == 403
        assert bad_origin.json()["code"] == "ORIGIN_REJECTED"

        bad_host = client.post(
            "/api/v1/projects",
            json={"name": "Plan"},
            headers={"Origin": ORIGIN, "Host": "other.example", "X-CSRF-Token": csrf},
        )
        assert bad_host.status_code == 403
        assert bad_host.json()["code"] == "HOST_REJECTED"

        accepted = client.post(
            "/api/v1/projects",
            json={"name": "Plan"},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        assert accepted.status_code == 201


def test_owner_rate_limit_returns_stable_429(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, rate_mutation_limit=1))
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 51000)) as client:
        _bootstrap(client)
        csrf = client.cookies.get("ege_csrf")
        assert csrf
        headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
        first = client.post("/api/v1/projects", json={"name": "One"}, headers=headers)
        second = client.post("/api/v1/projects", json={"name": "Two"}, headers=headers)
    assert first.status_code == 201
    assert second.status_code == 429
    assert second.json()["code"] == "RATE_LIMITED"
    assert int(second.headers["retry-after"]) >= 1


def test_logout_requires_csrf_and_clears_session(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 51000)) as client:
        _bootstrap(client)
        denied = client.delete("/api/v1/auth/session", headers={"Origin": ORIGIN})
        assert denied.status_code == 403

        csrf = client.cookies.get("ege_csrf")
        assert csrf
        cleared = client.delete(
            "/api/v1/auth/session",
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        assert cleared.status_code == 204
        unauthenticated = client.get("/api/v1/capabilities")
        assert unauthenticated.status_code == 401


def test_event_stream_handshake_requires_exact_origin(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 51000)) as client:
        _bootstrap(client)
        response = client.get(
            "/api/v1/assistant/runs/missing/events",
            headers={"Origin": "https://evil.example"},
        )
    assert response.status_code == 403
    assert response.json()["code"] == "ORIGIN_REJECTED"


def test_production_config_fails_closed_for_weak_secret(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, owner_session_secret="short"))
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 51000)) as client:
        response = client.get(
            "/api/v1/capabilities",
            headers={"X-Owner-Session": issue_session_token("owner", "short")},
        )
    assert response.status_code == 503
    assert response.json()["code"] == "SECURITY_CONFIG_UNAVAILABLE"


def test_production_rejects_csrf_cookie_name_the_browser_contract_cannot_read(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, owner_csrf_cookie="custom_csrf"))
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 51000)) as client:
        response = client.post(
            "/api/v1/auth/session",
            headers={
                "Origin": ORIGIN,
                "X-Owner-Session": issue_session_token("owner", SECRET),
            },
        )
    assert response.status_code == 503
    assert response.json()["code"] == "SECURITY_CONFIG_UNAVAILABLE"


def test_loopback_dev_bootstrap_is_cookie_free_and_authorizes_browser_requests(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            database_url="sqlite+pysqlite:///:memory:",
            owner_dev_mode=True,
            owner_dev_origin="http://localhost:3000",
        )
    )
    with TestClient(app, base_url="http://localhost:3000", client=("127.0.0.1", 51000)) as client:
        bootstrapped = client.post("/api/v1/auth/session", headers={"Origin": "http://localhost:3000"})
        assert bootstrapped.status_code == 204
        assert bootstrapped.headers["cache-control"] == "no-store"
        assert bootstrapped.headers.get("set-cookie") is None

        safe_get = client.get("/api/v1/capabilities", headers={"Referer": "http://localhost:3000/exams"})
        assert safe_get.status_code == 200

        unsafe = client.post(
            "/api/v1/projects",
            json={"name": "Local dev"},
            headers={"Origin": "http://localhost:3000"},
        )
        assert unsafe.status_code == 201


def test_loopback_dev_rejects_foreign_origin_and_unsafe_referer_only(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            database_url="sqlite+pysqlite:///:memory:",
            owner_dev_mode=True,
            owner_dev_origin="http://localhost:3000",
        )
    )
    with TestClient(app, base_url="http://localhost:3000", client=("127.0.0.1", 51000)) as client:
        foreign_get = client.get("/api/v1/capabilities", headers={"Origin": "http://evil.example"})
        assert foreign_get.status_code == 503
        assert foreign_get.json()["code"] == "ORIGIN_REJECTED"

        unsafe_referer_only = client.post(
            "/api/v1/projects",
            json={"name": "Should be rejected"},
            headers={"Referer": "http://localhost:3000/exams"},
        )
        assert unsafe_referer_only.status_code == 503
        assert unsafe_referer_only.json()["code"] == "ORIGIN_REJECTED"


def test_loopback_dev_origin_and_peer_configuration_fail_closed(tmp_path: Path) -> None:
    invalid_origins = ("https://evil.example", "http://localhost:not-a-port")
    for configured_origin in invalid_origins:
        app = create_app(
            Settings(
                database_url="sqlite+pysqlite:///:memory:",
                owner_dev_mode=True,
                owner_dev_origin=configured_origin,
            )
        )
        with TestClient(app, base_url="http://localhost:3000", client=("127.0.0.1", 51000)) as client:
            response = client.post("/api/v1/auth/session", headers={"Origin": "http://localhost:3000"})
        assert response.status_code == 503
        assert response.json()["code"] == "SECURITY_CONFIG_UNAVAILABLE"

    app = create_app(
        Settings(
            database_url="sqlite+pysqlite:///:memory:",
            owner_dev_mode=True,
            owner_dev_origin="http://localhost:3000",
        )
    )
    with TestClient(app, base_url="http://localhost:3000", client=("198.51.100.9", 51000)) as client:
        response = client.post("/api/v1/auth/session", headers={"Origin": "http://localhost:3000"})
    assert response.status_code == 401
    assert response.json()["code"] == "TRUSTED_PROXY_REQUIRED"

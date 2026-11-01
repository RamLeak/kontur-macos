from __future__ import annotations

import hashlib
from pathlib import Path

from backend.app import api
from backend.app.auth import issue_session_token
from backend.app.main import create_app
from backend.app.models import Asset, ExamSession
from backend.tests.test_source_lazy_sync import FakeSourceClient, _client, _settings
from backend.tests.test_type9_file_inputs import _import_csv
from sqlalchemy import select


def test_device_original_and_txt_never_write_server_cache(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    fake = FakeSourceClient()
    app.state.source_client = fake
    with _client(settings, app) as client:
        assert client.get("/api/v1/assets/missing/device").status_code == 401
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        asset_id = _import_csv(client, fake)
        original = client.get(f"/api/v1/assets/{asset_id}/device")
        assert original.status_code == 200, original.text
        assert original.content == b"1;2;3\n4;5;6\n"
        assert original.headers["x-content-hash"] == hashlib.sha256(original.content).hexdigest()
        assert original.headers["cache-control"] == "no-store"
        text = client.get(f"/api/v1/assets/{asset_id}/device-text")
        assert text.status_code == 200, text.text
        import base64
        assert base64.b64decode(text.json()["files"][0]["content_base64"]) == b"1\t2\t3\n4\t5\t6\n"
        with app.state.session_factory() as session:
            asset = session.get(Asset, asset_id)
            assert asset is not None and asset.storage_key is None
            assert asset.cache_state == "metadata_only" and not asset.hash_verified
        assert list(settings.asset_root.rglob("*")) == []


def test_device_rejects_changed_source_and_wrong_table_scope(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    fake = FakeSourceClient()
    app.state.source_client = fake
    with _client(settings, app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        asset_id = _import_csv(client, fake)
        fake.bytes_by_url["https://kompege.ru/files/test-only.csv"] = b"changed"
        assert client.get(f"/api/v1/assets/{asset_id}/device").status_code == 503
        assert client.get(f"/api/v1/assets/{asset_id}/device-text").status_code == 503
        assert client.get("/api/v1/assets/not-found/device").status_code == 404


def test_device_text_checks_type_before_fetching_source_bytes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    fake = FakeSourceClient()
    app.state.source_client = fake
    with _client(settings, app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        asset_id = _import_csv(client, fake, line=8)
        response = client.get(f"/api/v1/assets/{asset_id}/device-text")
        assert response.status_code == 404, response.text
        assert fake.byte_calls == []
        with app.state.session_factory() as session:
            asset = session.get(Asset, asset_id)
            assert asset is not None and asset.expected_content_hash is not None
            assert asset.fetched_at is None


def test_device_metadata_exposes_verified_legacy_ready_digest(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    fake = FakeSourceClient()
    app.state.source_client = fake
    with _client(settings, app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        asset_id = _import_csv(client, fake)
        with app.state.session_factory() as session:
            asset = session.get(Asset, asset_id)
            assert asset is not None
            digest = asset.content_hash
            asset.expected_content_hash = None
            asset.cache_state = "ready"
            asset.hash_verified = True
            session.commit()

        metadata = client.get(f"/api/v1/assets/{asset_id}/device-metadata")
        assert metadata.status_code == 200, metadata.text
        assert metadata.json()["pinned_sha256"] == digest
        assert metadata.json()["pinned"] is True


def test_oversized_device_delivery_does_not_pin_observed_digest(tmp_path: Path) -> None:
    from dataclasses import replace

    settings = replace(_settings(tmp_path), max_asset_bytes=4, max_asset_cache_bytes=4)
    app = create_app(settings)
    fake = FakeSourceClient()
    source_url = "https://kompege.ru/api/v1/task/1011"
    asset_url = "https://kompege.ru/files/unknown-large.txt"
    fake.payloads[source_url] = {
        "taskId": 1011,
        "number": 1,
        "text": "TEST_ONLY oversized unknown source",
        "files": [{"url": "/files/unknown-large.txt", "name": "unknown-large.txt", "media_type": "text/plain"}],
    }
    fake.bytes_by_url[asset_url] = b"12345"
    app.state.source_client = fake

    with _client(settings, app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        imported = client.post(
            "/api/v1/source/import-url",
            json={"source_url": "https://kompege.ru/task?id=1011"},
            headers={"Idempotency-Key": "TEST_ONLY-oversized-device"},
        )
        assert imported.status_code == 201, imported.text
        asset_id = imported.json()["task"]["revision"]["assets"][0]["id"]
        delivered = client.get(f"/api/v1/assets/{asset_id}/device")
        assert delivered.status_code == 503
        assert delivered.json()["code"] == "ASSET_TOO_LARGE"
        with app.state.session_factory() as session:
            asset = session.get(Asset, asset_id)
            assert asset is not None
            assert asset.expected_content_hash is None
            assert asset.fetched_at is None
            assert asset.cache_state == "metadata_only"
        assert list(settings.asset_root.rglob("*")) == []


def test_device_unknown_source_pins_first_digest_without_server_cache(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    fake = FakeSourceClient()
    app.state.source_client = fake
    source_url = "https://kompege.ru/api/v1/task/1010"
    asset_url = "https://kompege.ru/files/unknown.txt"
    payload = {
        "taskId": 1010,
        "number": 1,
        "text": "TEST_ONLY unknown source digest",
        "files": [{"url": "/files/unknown.txt", "name": "unknown.txt", "media_type": "text/plain"}],
    }
    first_bytes = b"first ephemeral bytes\n"
    fake.payloads[source_url] = payload
    fake.bytes_by_url[asset_url] = first_bytes

    with _client(settings, app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        imported = client.post(
            "/api/v1/source/import-url",
            json={"source_url": "https://kompege.ru/task?id=1010"},
            headers={"Idempotency-Key": "TEST_ONLY-unknown-device"},
        )
        assert imported.status_code == 201, imported.text
        asset_id = imported.json()["task"]["revision"]["assets"][0]["id"]
        before = client.get(f"/api/v1/assets/{asset_id}/device-metadata")
        assert before.status_code == 200, before.text
        assert before.json()["pinned_sha256"] is None
        assert before.json()["pinned"] is False
        assert fake.byte_calls == []

        delivered = client.get(f"/api/v1/assets/{asset_id}/device")
        assert delivered.status_code == 200, delivered.text
        digest = hashlib.sha256(first_bytes).hexdigest()
        assert delivered.content == first_bytes
        assert delivered.headers["x-content-hash"] == digest

        with app.state.session_factory() as session:
            asset = session.get(Asset, asset_id)
            assert asset is not None
            assert asset.expected_content_hash == digest
            assert asset.content_hash != digest  # metadata placeholder is preserved
            assert asset.cache_state == "metadata_only"
            assert not asset.hash_verified
            assert asset.storage_key is None
            assert asset.fetched_at is not None

            old_manifest_exam = ExamSession(prepare_options={"critical_assets": [{
                "revision_id": imported.json()["task"]["revision"]["id"],
                "asset_id": asset.id,
                "filename": asset.filename,
                "content_hash": asset.content_hash,
                "expected_content_hash": None,
            }]})
            assert api._missing_critical_files(session, old_manifest_exam)[0]["reason"] == "source_hash_changed"

            pinned_manifest_exam = ExamSession(prepare_options={"critical_assets": [{
                "revision_id": imported.json()["task"]["revision"]["id"],
                "asset_id": asset.id,
                "filename": asset.filename,
                "content_hash": asset.content_hash,
                "expected_content_hash": digest,
            }]})
            assert api._missing_critical_files(session, pinned_manifest_exam) == []
        assert list(settings.asset_root.rglob("*")) == []

        after = client.get(f"/api/v1/assets/{asset_id}/device-metadata")
        assert after.status_code == 200
        assert after.json()["pinned_sha256"] == digest
        assert after.json()["pinned"] is True

        refreshed = client.post(
            "/api/v1/source/import-url",
            json={"source_url": "https://kompege.ru/task?id=1010"},
            headers={"Idempotency-Key": "TEST_ONLY-unknown-device-refresh"},
        )
        assert refreshed.status_code == 201, refreshed.text
        assert refreshed.json()["task"]["revision"]["id"] == imported.json()["task"]["revision"]["id"]
        assert refreshed.json()["task"]["revision"]["assets"][0]["id"] == asset_id
        with app.state.session_factory() as session:
            assert [row.id for row in session.scalars(select(Asset)).all()] == [asset_id]

        # The source-bank relation is intentionally shared; the endpoint must
        # not trust or require a browser-supplied owner id for an authorized
        # asset.
        client.headers["X-Owner-Session"] = issue_session_token("owner-b", settings.owner_session_secret)
        foreign_metadata = client.get(f"/api/v1/assets/{asset_id}/device-metadata")
        assert foreign_metadata.status_code == 200
        foreign_delivery = client.get(f"/api/v1/assets/{asset_id}/device")
        assert foreign_delivery.status_code == 200
        assert foreign_delivery.content == first_bytes

        fake.bytes_by_url[asset_url] = b"changed after first pin\n"
        changed = client.get(f"/api/v1/assets/{asset_id}/device")
        assert changed.status_code == 503, changed.text
        assert changed.json()["code"] == "ASSET_HASH_MISMATCH"
        with app.state.session_factory() as session:
            asset = session.get(Asset, asset_id)
            assert asset is not None and asset.expected_content_hash == digest


def test_prepared_exam_reconciles_browser_pin_before_start_without_server_cache(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    fake = FakeSourceClient()
    source_url = "https://kompege.ru/api/v1/task/1012"
    asset_url = "https://kompege.ru/files/prepared-unknown.txt"
    source_bytes = b"prepared browser bytes\n"
    fake.payloads[source_url] = {
        "taskId": 1012,
        "number": 1,
        "text": "TEST_ONLY prepared unknown source",
        "answer": "42",
        "files": [{"url": "/files/prepared-unknown.txt", "name": "prepared-unknown.txt", "media_type": "text/plain"}],
    }
    fake.bytes_by_url[asset_url] = source_bytes
    app.state.source_client = fake

    with _client(settings, app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        imported = client.post(
            "/api/v1/source/import-url",
            json={"source_url": "https://kompege.ru/task?id=1012"},
            headers={"Idempotency-Key": "TEST_ONLY-prepared-unknown"},
        )
        assert imported.status_code == 201, imported.text
        asset_id = imported.json()["task"]["revision"]["assets"][0]["id"]
        prepared = client.post(
            "/api/v1/exams/prepare",
            json={
                "kind": "thematic",
                "mode": "strict",
                "required_lines": {"1": 1},
                "difficulty": "any",
                "duration_minutes": 45,
                "exclude_solved": False,
                "exclude_recent": False,
                "source_year": None,
                "include_corrected_errors": False,
            },
            headers={"Idempotency-Key": "TEST_ONLY-prepared-exam"},
        )
        assert prepared.status_code == 201, prepared.text
        exam_id = prepared.json()["id"]
        assert prepared.json()["critical_files_ready"] is False

        before = client.post(f"/api/v1/exams/{exam_id}/assets-ready")
        assert before.status_code == 200, before.text
        assert before.json()["critical_files_ready"] is False
        assert before.json()["missing_critical_files"][0]["reason"] == "not_ready"

        delivered = client.get(f"/api/v1/assets/{asset_id}/device")
        assert delivered.status_code == 200, delivered.text
        assert delivered.content == source_bytes
        ready = client.post(f"/api/v1/exams/{exam_id}/assets-ready")
        assert ready.status_code == 200, ready.text
        assert ready.json()["critical_files_ready"] is True
        assert client.post(
            f"/api/v1/exams/{exam_id}/start",
            headers={"Idempotency-Key": "TEST_ONLY-prepared-start"},
        ).status_code == 200

        blocked_mutation = client.post(
            f"/api/v1/exams/{exam_id}/assets-ready",
            headers={"Idempotency-Key": "TEST_ONLY-after-start"},
        )
        assert blocked_mutation.status_code == 422
        assert blocked_mutation.json()["code"] == "EXAM_PREPARATION_IMMUTABLE"
        with app.state.session_factory() as session:
            asset = session.get(Asset, asset_id)
            assert asset is not None and asset.storage_key is None
            assert asset.expected_content_hash == hashlib.sha256(source_bytes).hexdigest()


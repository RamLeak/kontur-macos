from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from backend.app.auth import issue_session_token
from backend.app.config import Settings
from backend.app.db import init_schema
from backend.app.main import create_app
from backend.app.models import Asset, SourceSyncItem, SourceSyncJob, TaskRevision, utcnow
from backend.app.source_catalog import sanitize_statement
from backend.app.source_discovery import run_variant_discovery
from backend.app.source_sync import process_catalog_job, reserve_catalog_job
from backend.app.source_transport import _normalise_remote_record, _normalise_variant_payload
from fastapi.testclient import TestClient
from sqlalchemy import func, select


class FakeSourceClient:
    def __init__(self) -> None:
        self.json_calls: list[tuple[str, bool]] = []
        self.byte_calls: list[str] = []
        self.payloads: dict[str, dict[str, object]] = {}
        self.bytes_by_url: dict[str, bytes] = {}

    def fetch_json(self, url: str, *, api_endpoint: bool = False) -> dict[str, object]:
        self.json_calls.append((url, api_endpoint))
        return self.payloads[url]

    def fetch_bytes(
        self,
        url: str,
        *,
        filename: str = "attachment.txt",
        declared_media_type: str | None = None,
        expected_hash: str | None = None,
    ) -> bytes:
        del filename, declared_media_type, expected_hash
        self.byte_calls.append(url)
        return self.bytes_by_url[url]


def _client(settings: Settings, app: object) -> TestClient:
    return TestClient(app)  # type: ignore[arg-type]


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        asset_root=tmp_path / "assets",
        owner_session_secret="test-owner-secret",
        source_mode="sync",
        source_network_enabled=True,
        source_permission_status="private_authorized",
        source_api_template="https://kompege.ru/api/v1/task/{id}",
        source_variant_api_url="https://kompege.ru/api/v1/variant/random",
        testing=True,
    )


def test_variant_replay_does_not_call_source_transport_again(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    fake = FakeSourceClient()
    variant_url = "https://kompege.ru/api/v1/variant/random"
    fake.payloads[variant_url] = {
        "hideAnswer": True,
        "kim": "2027",
        "realMode": False,
        "tasks": [
            {"id": "upstream-a", "taskId": 1001, "number": 1, "text": "one"},
            {"id": "upstream-b", "taskId": 1002, "number": 2, "text": "two"},
        ],
    }
    app.state.source_client = fake
    with _client(settings, app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        first = client.post(
            "/api/v1/source/sync-variants",
            json={"max_tasks": 2, "target_exam_year": 2027},
            headers={"Idempotency-Key": "variant-once"},
        )
        second = client.post(
            "/api/v1/source/sync-variants",
            json={"max_tasks": 2, "target_exam_year": 2027},
            headers={"Idempotency-Key": "variant-once"},
        )
    assert first.status_code == second.status_code == 201
    assert second.json()["duplicate"] is True
    assert second.json()["target_exam_year"] == 2027
    assert len(fake.json_calls) == 1


def test_variant_discovery_is_resumable_targeted_and_never_fetches_bytes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    init_schema(app.state.engine)
    fake = FakeSourceClient()
    variant_url = "https://kompege.ru/api/v1/variant/random"
    fake.payloads[variant_url] = {
        "hideAnswer": True,
        "kim": "2027",
        "realMode": False,
        "tasks": [
            {"id": "upstream-a", "taskId": 2001, "number": 1, "text": "one", "key": "secret-one"},
            {"id": "upstream-b", "taskId": 2002, "number": 2, "text": "two", "key": "secret-two"},
        ],
    }
    app.state.source_client = fake
    session = app.state.session_factory()
    try:
        with session.begin():
            from backend.app.api_common import ensure_owner_row

            ensure_owner_row(session, "owner")
        first = run_variant_discovery(
            session,
            owner_id="owner",
            settings=settings,
            client=fake,  # type: ignore[arg-type]
            idempotency_key="discover-2027",
            target_exam_year=2027,
            max_unique_tasks=2,
            max_variants=10,
            work_limit=3,
        )
        replay = run_variant_discovery(
            session,
            owner_id="owner",
            settings=settings,
            client=fake,  # type: ignore[arg-type]
            idempotency_key="discover-2027",
            target_exam_year=2027,
            max_unique_tasks=2,
            max_variants=10,
            work_limit=3,
        )
        assert first.id == replay.id
        assert first.status == replay.status == "succeeded"
        assert first.counters["unique_tasks"] == 2
        assert first.counters["attachment_bytes_fetched"] == 0
        assert fake.json_calls == [(variant_url, True)]
        assert fake.byte_calls == []
        revisions = list(session.scalars(select(TaskRevision).order_by(TaskRevision.source_id)))
        assert [revision.target_exam_year for revision in revisions] == [2027, 2027]
        assert all(revision.exam_year is None for revision in revisions)
        assert [revision.answer for revision in revisions] == ["secret-one", "secret-two"]
    finally:
        session.close()


def test_variant_discovery_does_not_overshoot_exact_target(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    init_schema(app.state.engine)
    fake = FakeSourceClient()
    variant_url = "https://kompege.ru/api/v1/variant/random"
    fake.payloads[variant_url] = {
        "tasks": [
            {"taskId": 3001, "number": 1, "text": "first"},
            {"taskId": 3002, "number": 2, "text": "second"},
        ]
    }
    session = app.state.session_factory()
    try:
        with session.begin():
            from backend.app.api_common import ensure_owner_row

            ensure_owner_row(session, "owner")
        job = run_variant_discovery(
            session,
            owner_id="owner",
            settings=settings,
            client=fake,  # type: ignore[arg-type]
            idempotency_key="discover-exact-one",
            max_unique_tasks=1,
            max_variants=5,
            work_limit=5,
        )
        assert job.status == "succeeded"
        assert job.counters["unique_tasks"] == 1
        assert session.scalar(select(func.count(TaskRevision.id))) == 1
        assert fake.byte_calls == []
    finally:
        session.close()


def test_catalog_sync_is_durable_and_metadata_only(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    fake = FakeSourceClient()
    for task_id in (1001, 1002):
        url = f"https://kompege.ru/api/v1/task/{task_id}"
        fake.payloads[url] = {
            "id": f"upstream-{task_id}",
            "taskId": task_id,
            "number": task_id - 1000,
            "text": "<p>task</p><img src='/images/plot.png'><script>bad()</script>",
            "files": [{"url": "/files/data.txt", "name": "data.txt", "media_type": "text/plain"}],
        }
    app.state.source_client = fake
    urls = ["https://kompege.ru/task?id=1001", "https://kompege.ru/task?id=1002"]
    with _client(settings, app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        first = client.post(
            "/api/v1/source/sync",
            json={"source_urls": urls},
            headers={"Idempotency-Key": "catalog-once"},
        )
        replay = client.post(
            "/api/v1/source/sync",
            json={"source_urls": urls},
            headers={"Idempotency-Key": "catalog-once"},
        )
        assert first.status_code == replay.status_code == 201, first.text
        assert first.json()["total"] == 2
        assert replay.json()["job_id"] == first.json()["job_id"]
        assert len(fake.json_calls) == 2
        assert fake.byte_calls == []
        assert not list((tmp_path / "assets").rglob("*"))
        session = app.state.session_factory()
        try:
            assets = list(session.scalars(select(Asset)))
            assert assets
            assert all(asset.cache_state == "metadata_only" for asset in assets)
            job = session.scalar(select(SourceSyncJob).where(SourceSyncJob.id == first.json()["job_id"]))
            assert job is not None and job.status == "succeeded"
            assert session.scalar(select(func.count(SourceSyncItem.id)).where(SourceSyncItem.job_id == job.id)) == 2
        finally:
            session.close()


def test_catalog_worker_reclaims_stale_fetching_item(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    init_schema(app.state.engine)
    fake = FakeSourceClient()
    source_url = "https://kompege.ru/task?id=1099"
    fake.payloads["https://kompege.ru/api/v1/task/1099"] = {
        "taskId": 1099,
        "number": 9,
        "text": "reclaim",
    }
    session = app.state.session_factory()
    try:
        from backend.app.api_common import ensure_owner_row

        with session.begin():
            ensure_owner_row(session, "owner")
            reservation = reserve_catalog_job(
                session,
                owner_id="owner",
                settings=settings,
                canonical_urls=[source_url],
                idempotency_key="stale-job",
                target_exam_year=2027,
            )
        with session.begin():
            item = session.scalar(select(SourceSyncItem).where(SourceSyncItem.job_id == reservation.job.id))
            assert item is not None
            item.status = "fetching"
            item.updated_at = utcnow() - timedelta(seconds=120)
        job = process_catalog_job(
            session,
            job_id=reservation.job.id,
            owner_id="owner",
            settings=settings,
            client=fake,  # type: ignore[arg-type]
            work_limit=1,
        )
        assert job.status == "succeeded"
        assert fake.json_calls == [("https://kompege.ru/api/v1/task/1099", True)]
        item = session.scalar(select(SourceSyncItem).where(SourceSyncItem.job_id == reservation.job.id))
        assert item is not None and item.status == "succeeded" and item.attempts == 1
    finally:
        session.close()


def test_task_pagination_contract_keeps_total_and_target_year(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    fake = FakeSourceClient()
    for task_id in (1101, 1102, 1103):
        url = f"https://kompege.ru/api/v1/task/{task_id}"
        fake.payloads[url] = {"taskId": task_id, "number": task_id - 1100, "text": f"task {task_id}"}
    app.state.source_client = fake
    with _client(settings, app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        imported = client.post(
            "/api/v1/source/sync",
            json={
                "source_urls": [
                    "https://kompege.ru/task?id=1101",
                    "https://kompege.ru/task?id=1102",
                    "https://kompege.ru/task?id=1103",
                ],
                "target_exam_year": 2027,
            },
            headers={"Idempotency-Key": "catalog-target-2027"},
        )
        assert imported.status_code == 201, imported.text
        first = client.get("/api/v1/tasks?target_exam_year=2027&limit=1")
        assert first.status_code == 200, first.text
        first_body = first.json()
        assert first_body["total"] == 3
        assert first_body["items"][0]["revision"]["target_exam_year"] == 2027
        assert first_body["items"][0]["revision"]["exam_year"] is None
        second = client.get(f"/api/v1/tasks?target_exam_year=2027&limit=1&cursor={first_body['next_cursor']}")
        assert second.status_code == 200, second.text
        assert second.json()["total"] == 3


def test_source_health_counts_current_visible_target_revisions_only(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    fake = FakeSourceClient()
    task_url = "https://kompege.ru/api/v1/task/1107"
    fake.payloads[task_url] = {"taskId": 1107, "number": 7, "text": "first", "year": 2026}
    app.state.source_client = fake
    with _client(settings, app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        first = client.post(
            "/api/v1/source/import-url",
            json={"source_url": "https://kompege.ru/task?id=1107", "target_exam_year": 2027},
            headers={"Idempotency-Key": "health-history-a"},
        )
        assert first.status_code == 201, first.text
        fake.payloads[task_url] = {"taskId": 1107, "number": 7, "text": "second", "year": 2026}
        second = client.post(
            "/api/v1/source/import-url",
            json={"source_url": "https://kompege.ru/task?id=1107", "target_exam_year": 2027},
            headers={"Idempotency-Key": "health-history-b"},
        )
        assert second.status_code == 201, second.text
        health = client.get("/api/v1/source/health?target_exam_year=2027")
        assert health.status_code == 200, health.text
        body = health.json()
        assert body["target_exam_year"] == 2027
        assert body["current_unique"] == body["total"] == 1
        assert body["coverage"]["7"] == 1
        other_target = client.get("/api/v1/source/health?target_exam_year=2026")
        assert other_target.status_code == 200, other_target.text
        assert other_target.json()["current_unique"] == 0


def test_prepare_asset_promotes_once_and_strict_protocol_rejects_url_only(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    fake = FakeSourceClient()
    task_url = "https://kompege.ru/api/v1/task/1004"
    asset_url = "https://kompege.ru/files/prepare.txt"
    fake.payloads[task_url] = {
        "taskId": 1004,
        "number": 4,
        "text": "prepare",
        "files": [
            {
                "url": "/files/prepare.txt",
                "name": "prepare.txt",
                "media_type": "text/plain",
                "sha256": hashlib.sha256(b"ready\n").hexdigest(),
            }
        ],
    }
    fake.bytes_by_url[asset_url] = b"ready\n"
    app.state.source_client = fake
    with _client(settings, app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        imported = client.post(
            "/api/v1/source/import-url",
            json={"source_url": "https://kompege.ru/task?id=1004"},
            headers={"Idempotency-Key": "prepare-asset"},
        )
        assert imported.status_code == 201, imported.text
        asset_id = imported.json()["task"]["revision"]["assets"][0]["id"]
        prepared = client.post(f"/api/v1/assets/{asset_id}/prepare", json={})
        replay = client.post(f"/api/v1/assets/{asset_id}/prepare", json={})
        assert prepared.status_code == replay.status_code == 200
        assert prepared.json()["cache_state"] == replay.json()["cache_state"] == "ready"
        assert len(fake.byte_calls) == 1

        fake.payloads["https://kompege.ru/api/v1/task/1005"] = {
            "taskId": 1005,
            "number": 5,
            "text": "strict protocol",
            "files": [
                {
                    "url": "/files/prepare.txt",
                    "name": "strict.txt",
                    "media_type": "text/plain",
                    "sha256": hashlib.sha256(b"unexpected").hexdigest(),
                }
            ],
        }
        second_import = client.post(
            "/api/v1/source/import-url",
            json={"source_url": "https://kompege.ru/task?id=1005"},
            headers={"Idempotency-Key": "strict-asset"},
        )
        assert second_import.status_code == 201, second_import.text
        strict_asset_id = second_import.json()["task"]["revision"]["assets"][0]["id"]

        class UrlOnlyClient:
            def fetch_bytes(self, url: str) -> bytes:
                fake.byte_calls.append(url)
                return b"unexpected"

        app.state.source_client = UrlOnlyClient()
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        rejected = client.post(f"/api/v1/assets/{strict_asset_id}/prepare", json={})
        assert rejected.status_code == 503
        assert rejected.json()["code"] == "ASSET_FETCH_PROTOCOL"


def test_lazy_promotion_preserves_asset_identity_across_new_sync_key(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    fake = FakeSourceClient()
    task_url = "https://kompege.ru/api/v1/task/1007"
    asset_url = "https://kompege.ru/files/stable.txt"
    fake.payloads[task_url] = {
        "taskId": 1007,
        "number": 7,
        "text": "stable attachment",
        "files": [{"url": "/files/stable.txt", "name": "stable.txt", "media_type": "text/plain"}],
    }
    fake.bytes_by_url[asset_url] = b"stable\n"
    app.state.source_client = fake
    with _client(settings, app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        first = client.post(
            "/api/v1/source/import-url",
            json={"source_url": "https://kompege.ru/task?id=1007"},
            headers={"Idempotency-Key": "stable-key-a"},
        )
        assert first.status_code == 201, first.text
        asset_id = first.json()["task"]["revision"]["assets"][0]["id"]
        revision_id = first.json()["task"]["revision"]["id"]
        prepared = client.post(f"/api/v1/assets/{asset_id}/prepare", json={})
        assert prepared.status_code == 200, prepared.text

        session = client.app.state.session_factory()
        try:
            promoted = session.get(Asset, asset_id)
            assert promoted is not None
            assert promoted.cache_state == "ready"
            assert promoted.hash_verified is True
            assert promoted.expected_content_hash is None
            promoted_hash = promoted.content_hash
        finally:
            session.close()

        second = client.post(
            "/api/v1/source/import-url",
            json={"source_url": "https://kompege.ru/task?id=1007"},
            headers={"Idempotency-Key": "stable-key-b"},
        )
        assert second.status_code == 201, second.text
        assert second.json()["task"]["revision"]["id"] == revision_id
        assert second.json()["task"]["revision"]["assets"][0]["id"] == asset_id
        replay = client.post(
            "/api/v1/source/import-url",
            json={"source_url": "https://kompege.ru/task?id=1007"},
            headers={"Idempotency-Key": "stable-key-b"},
        )
        assert replay.status_code == 201, replay.text
        assert replay.json()["duplicate"] is True

        assert fake.json_calls == [(task_url, True), (task_url, True)]
        assert fake.byte_calls == [asset_url]
        session = app.state.session_factory()
        try:
            assert session.scalar(select(func.count(Asset.id))) == 1
            assert session.scalar(select(func.count(TaskRevision.id))) == 1
            promoted = session.scalar(select(Asset).where(Asset.id == asset_id))
            assert promoted is not None and promoted.content_hash == promoted_hash
        finally:
            session.close()


def test_asset_cache_quota_fails_before_promoting_bytes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings = replace(settings, max_asset_cache_bytes=1)
    app = create_app(settings)
    fake = FakeSourceClient()
    task_url = "https://kompege.ru/api/v1/task/1006"
    asset_url = "https://kompege.ru/files/quota.txt"
    fake.payloads[task_url] = {
        "taskId": 1006,
        "number": 6,
        "text": "quota",
        "files": [
            {
                "url": "/files/quota.txt",
                "name": "quota.txt",
                "media_type": "text/plain",
                "sha256": hashlib.sha256(b"quota\n").hexdigest(),
            }
        ],
    }
    fake.bytes_by_url[asset_url] = b"quota\n"
    app.state.source_client = fake
    with _client(settings, app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        imported = client.post(
            "/api/v1/source/import-url",
            json={"source_url": "https://kompege.ru/task?id=1006"},
            headers={"Idempotency-Key": "quota-asset"},
        )
        assert imported.status_code == 201, imported.text
        asset_id = imported.json()["task"]["revision"]["assets"][0]["id"]
        prepared = client.post(f"/api/v1/assets/{asset_id}/prepare", json={})
        assert prepared.status_code == 503
        assert prepared.json()["code"] == "ASSET_CACHE_QUOTA"
        assert fake.byte_calls == [asset_url]
        assert not list((tmp_path / "assets").rglob("*"))
        session = app.state.session_factory()
        try:
            asset = session.get(Asset, asset_id)
            assert asset is not None and asset.cache_state == "failed"
        finally:
            session.close()


def test_safe_statement_preserves_image_table_and_list_structure() -> None:
    statement, _raw_hash = sanitize_statement(
        "<table><tr><td>A</td><th>B</th></tr></table><ul><li>one</li><li>two</li></ul><img src='/images/a.png'>",
        10_000,
    )
    assert "A\tB" in statement
    assert "• one" in statement and "• two" in statement
    assert "[Изображение 1]" in statement


def test_lazy_asset_fetch_is_cached_and_wrong_hash_is_quarantined(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    fake = FakeSourceClient()
    task_url = "https://kompege.ru/api/v1/task/1003"
    asset_url = "https://kompege.ru/files/data.txt"
    expected = hashlib.sha256(b"expected").hexdigest()
    fake.payloads[task_url] = {
        "id": "upstream-1003",
        "taskId": 1003,
        "number": 3,
        "text": "task",
        "files": [
            {
                "url": "/files/data.txt",
                "name": "data.txt",
                "media_type": "text/plain",
                "sha256": expected,
            }
        ],
    }
    fake.bytes_by_url[asset_url] = b"wrong"
    app.state.source_client = fake
    with _client(settings, app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        imported = client.post(
            "/api/v1/source/import-url",
            json={"source_url": "https://kompege.ru/task?id=1003"},
            headers={"Idempotency-Key": "lazy-asset"},
        )
        assert imported.status_code == 201, imported.text
        asset_id = imported.json()["task"]["revision"]["assets"][0]["id"]
        download = client.get(f"/api/v1/assets/{asset_id}/download")
        assert download.status_code == 503
        assert download.json()["code"] == "ASSET_HASH_MISMATCH"
        again = client.get(f"/api/v1/assets/{asset_id}/download")
        assert again.status_code == 503
        assert len(fake.byte_calls) == 1
        session = app.state.session_factory()
        try:
            asset = session.get(Asset, asset_id)
            assert asset is not None and asset.cache_state == "quarantined"
            assert asset.hash_verified is False
        finally:
            session.close()


def test_subtask_normalization_preserves_group_provenance() -> None:
    normalized = _normalise_remote_record(
        {
            "id": "upstream-19",
            "taskId": 1900,
            "number": 19,
            "text": "parent",
            "subTask": [
                {"id": "upstream-20", "number": 20, "text": "<b>child</b><script>x</script>"},
                {"number": 21, "text": "child two"},
            ],
        },
        "https://kompege.ru/task?id=1900",
    )
    assert [item["line_number"] for item in normalized["subtasks"]] == [20, 21]
    assert normalized["subtasks"][0]["group_external_id"] == "1900"
    assert normalized["subtasks"][0]["upstream_record_id"] == "upstream-20"
    assert "<script>" not in normalized["subtasks"][0]["statement"]


def test_variant_skips_external_and_data_images_with_explicit_failures() -> None:
    normalized = _normalise_variant_payload(
        {
            "hideAnswer": True,
            "kim": "2027",
            "realMode": False,
            "tasks": [
                {"taskId": 2001, "number": 1, "text": "valid"},
                {"taskId": 2002, "number": 2, "text": '<p>external</p><img src="https://evil.example/a.png">'},
                {"taskId": 2003, "number": 3, "text": '<p>inline</p><img src="data:image/png;base64,AAAA">'},
            ],
        },
        "https://kompege.ru/api/v1/variant/random",
    )
    assert [item["source_id"] for item in normalized] == ["2001"]
    assert len(normalized.failures) == 2
    assert {failure["source_id"] for failure in normalized.failures} == {"2002", "2003"}
    assert all(failure["code"] == "SOURCE_IMAGE_UNSUPPORTED" for failure in normalized.failures)

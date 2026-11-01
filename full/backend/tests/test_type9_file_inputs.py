from __future__ import annotations

import hashlib
from pathlib import Path

from backend.app.auth import issue_session_token
from backend.app.main import create_app
from backend.app.models import Asset
from backend.app.task_file_inputs import code_inputs_for_assets
from backend.tests.test_source_lazy_sync import FakeSourceClient, _client, _settings


def _import_csv(client, fake, *, line=9, data=b"1;2;3\n4;5;6\n"):
    fake.payloads["https://kompege.ru/api/v1/task/1009"] = {
        "taskId": 1009, "number": line, "text": "TEST_ONLY count rows",
        "files": [{"url": "/files/test-only.csv", "name": "test-only.csv", "media_type": "text/csv",
                   "sha256": hashlib.sha256(data).hexdigest()}],
    }
    fake.bytes_by_url["https://kompege.ru/files/test-only.csv"] = data
    response = client.post("/api/v1/source/import-url", json={"source_url": "https://kompege.ru/task?id=1009"},
                           headers={"Idempotency-Key": "TEST_ONLY-type9"})
    assert response.status_code == 201, response.text
    return response.json()["task"]["revision"]["assets"][0]["id"]


def test_lazy_original_then_txt_download_and_replay(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    fake = FakeSourceClient()
    app.state.source_client = fake
    with _client(settings, app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        asset_id = _import_csv(client, fake)
        assert fake.byte_calls == []
        first = client.post(f"/api/v1/assets/{asset_id}/prepare-text")
        assert first.status_code == 200, first.text
        item = first.json()["files"][0]
        assert item["filename"] == "test-only_csv.txt"
        expected = b"1\t2\t3\n4\t5\t6\n"
        assert item["content_hash"] == hashlib.sha256(expected).hexdigest()
        assert item["source_sha256"] == hashlib.sha256(b"1;2;3\n4;5;6\n").hexdigest()
        second = client.post(f"/api/v1/assets/{asset_id}/prepare-text")
        assert second.json() == first.json()
        downloaded = client.get(f"/api/v1/assets/{asset_id}/text")
        assert downloaded.status_code == 200
        assert downloaded.content == expected
        assert downloaded.headers["cache-control"] == "no-store"
        assert downloaded.headers["x-source-hash"] == item["source_sha256"]
        original = client.get(f"/api/v1/assets/{asset_id}/download")
        assert original.content == b"1;2;3\n4;5;6\n"
        assert len(fake.byte_calls) == 1
        with app.state.session_factory() as session:
            asset = session.get(Asset, asset_id)
            assert asset is not None
            manifest, inputs, error = code_inputs_for_assets([asset], line_number=9, settings=settings)
            assert error is None
            assert inputs == [{"name": "test-only_csv.txt", "content": expected, "sha256": item["content_hash"]}]
            assert manifest[0]["source_sha256"] == item["source_sha256"]
            assert manifest[0]["converter_version"]
            other_manifest, other_inputs, other_error = code_inputs_for_assets(
                [asset], line_number=24, settings=settings,
            )
            assert other_error is None
            assert other_inputs[0]["name"] == "test-only.csv"
            assert other_manifest[0]["content_hash"] == item["source_sha256"]
        assert client.get(f"/api/v1/assets/{asset_id}/text?sheet=-1").status_code == 404
        assert client.get(f"/api/v1/assets/{asset_id}/text?sheet=1").status_code == 404


def test_conversion_scope_and_auth_are_enforced(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    fake = FakeSourceClient()
    app.state.source_client = fake
    with _client(settings, app) as client:
        assert client.post("/api/v1/assets/missing/prepare-text").status_code == 401
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        asset_id = _import_csv(client, fake, line=24)
        assert client.post(f"/api/v1/assets/{asset_id}/prepare-text").status_code == 404
        assert fake.byte_calls == []


def test_bad_original_digest_is_never_converted(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    fake = FakeSourceClient()
    app.state.source_client = fake
    with _client(settings, app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        asset_id = _import_csv(client, fake)
        fake.bytes_by_url["https://kompege.ru/files/test-only.csv"] = b"wrong source\n"
        response = client.post(f"/api/v1/assets/{asset_id}/prepare-text")
        assert response.status_code == 503
        assert response.json()["code"] == "ASSET_HASH_MISMATCH"
        with app.state.session_factory() as session:
            asset = session.get(Asset, asset_id)
            assert asset is not None and not asset.hash_verified
            assert asset.cache_state == "quarantined"


def test_csv_type_does_not_depend_on_windows_mime_registry(monkeypatch) -> None:
    from backend.app.assets import detect_type
    monkeypatch.setattr("backend.app.assets.mimetypes.guess_type", lambda name: ("application/vnd.ms-excel", None))
    assert detect_type(b"1,2,3\n", "TEST_ONLY.csv", "text/csv") == "text/csv"


def test_derived_names_reject_casefold_collisions(tmp_path: Path) -> None:
    from backend.app.assets import AssetStore
    settings = _settings(tmp_path)
    store = AssetStore(settings)
    files = [store.store_bytes(canonical_url=f"https://kompege.ru/files/{name}", filename=name,
                               data=b"1,2,3\n", declared_media_type="text/csv", rights_status="private_authorized")
             for name in ("A.csv", "a.csv")]
    _manifest, inputs, error = code_inputs_for_assets(files, line_number=9, settings=settings)
    assert inputs == []
    assert error == "task input filenames collide"


def test_exam_conversion_version_is_pinned_and_checked(tmp_path: Path, monkeypatch) -> None:
    from backend.app import api
    from backend.app.models import ExamItem, ExamSession, TaskRevision
    from sqlalchemy import select
    settings = _settings(tmp_path)
    app = create_app(settings)
    fake = FakeSourceClient()
    app.state.source_client = fake
    with _client(settings, app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        asset_id = _import_csv(client, fake)
        assert client.post(f"/api/v1/assets/{asset_id}/prepare-text").status_code == 200
        with app.state.session_factory() as session:
            revision = session.scalar(select(TaskRevision))
            assert revision is not None
            item = ExamItem(task_revision_id=revision.id, position=0)
            frozen = api._critical_asset_manifest(session, [(item, revision)])
            assert frozen[0]["converter_version"] == api.CONVERTER_VERSION
            exam = ExamSession(prepare_options={"critical_assets": frozen})
            _manifest, inputs, error = api._exam_code_run_inputs(
                session, revision=revision, settings=settings, exam=exam,
            )
            assert error is None and inputs[0]["name"] == "test-only_csv.txt"
            assert not api._missing_critical_files(session, exam)
            monkeypatch.setattr(api, "CONVERTER_VERSION", "TEST_ONLY-future-version")
            assert api._missing_critical_files(session, exam)[0]["reason"] == "conversion_version_changed"
            _manifest, inputs, error = api._exam_code_run_inputs(
                session, revision=revision, settings=settings, exam=exam,
            )
            assert inputs == [] and error is not None


def test_alternative_formats_of_same_task_have_distinct_python_paths(tmp_path: Path) -> None:
    from io import BytesIO

    from backend.app.assets import AssetStore
    from openpyxl import Workbook
    settings = _settings(tmp_path)
    workbook = Workbook()
    workbook.active.append([1, 2, 3])
    buffer = BytesIO()
    workbook.save(buffer)
    originals = [("9.csv", b"1,2,3\n", "text/csv"),
                 ("9.xlsx", buffer.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")]
    assets = [AssetStore(settings).store_bytes(canonical_url=f"https://kompege.ru/files/{name}",
              filename=name, data=data, declared_media_type=media, rights_status="private_authorized")
              for name, data, media in originals]
    manifest, inputs, error = code_inputs_for_assets(assets, line_number=9, settings=settings)
    assert error is None
    assert [item["name"] for item in inputs] == ["9_csv.txt", "9_xlsx.txt"]
    assert all(item["content"] == b"1\t2\t3\n" for item in inputs)
    assert all(item["converter_version"] == "type9-tsv-v3" for item in manifest)

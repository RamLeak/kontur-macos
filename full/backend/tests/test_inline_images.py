from __future__ import annotations

import base64
import json

import pytest
from backend.app.api_common import ensure_owner_row
from backend.app.auth import issue_session_token
from backend.app.config import Settings
from backend.app.inline_images import MAX_INLINE_BYTES, decode_inline_image, parse_inline_image
from backend.app.main import create_app
from backend.app.models import Asset
from backend.app.source_catalog import SourceError, canonical_asset_reference, import_fixture
from backend.app.source_transport import BoundedKegeClient, SourceFetchError, SourceResponse, _normalise_remote_record
from fastapi.testclient import TestClient

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)
ENDPOINT = "https://kompege.ru/api/v1/task/number/1"


def record(data=PNG):
    encoded = base64.b64encode(data).decode("ascii")
    return {"taskId": 31498, "number": 1, "text": f'<p>Graph</p><img src="data:image/png;base64,{encoded}">',
            "key": "42", "createdAt": "2026-08-01T00:00:00Z", "difficulty": 0}


@pytest.mark.parametrize("image_kind", ["inline", "linked"])
def test_embedded_image_is_metadata_until_viewed_and_is_then_cached(tmp_path, image_kind):
    settings = Settings(database_url="sqlite+pysqlite:///:memory:", asset_root=tmp_path / "assets",
                        source_mode="sync", source_network_enabled=True, source_permission_status="private_authorized",
                        owner_session_secret="test-only", testing=True)
    calls = []
    source_record = record()
    expected_url = ENDPOINT
    if image_kind == "linked":
        source_record["text"] = '<p>Graph</p><img src="/images/graph.png">'
        expected_url = "https://kompege.ru/images/graph.png"

    def transport(url, headers, maximum, timeout):
        calls.append(url)
        assert url == expected_url
        body = json.dumps([source_record]).encode() if image_kind == "inline" else PNG
        return SourceResponse(200, url, {}, body)

    app = create_app(settings)
    app.state.source_client = BoundedKegeClient(settings, transport=transport, resolver=lambda *_: ["8.8.8.8"])
    payload = _normalise_remote_record(source_record, "https://kompege.ru/task?id=31498")
    assert "base64" not in json.dumps(payload)
    assert "[Изображение 1]" in payload["statement"]
    with TestClient(app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", "test-only")})
        with app.state.session_factory() as session, session.begin():
            ensure_owner_row(session, "owner")
            _, _, assets, _ = import_fixture(session, owner_id="owner", settings=settings, payload=payload,
                                             idempotency_key="inline-one", admission_mode="remote",
                                             admitted_rights_status="private_authorized")
            asset_id = assets[0].id
            assert assets[0].cache_state == "metadata_only" and assets[0].size_bytes == 0
        assert calls == [] and not list(tmp_path.rglob("*.png"))
        prepared = client.post(f"/api/v1/assets/{asset_id}/prepare")
        assert prepared.status_code == 200, prepared.text
        image = client.get(f"/api/v1/assets/{asset_id}/download")
        assert image.status_code == 200 and image.content == PNG
        assert image.headers["content-disposition"].startswith("inline;")
        assert image.headers["cache-control"] == "no-store"
        assert image.headers["x-content-type-options"] == "nosniff"
        assert calls == [expected_url]
        with app.state.session_factory() as session:
            cached = session.get(Asset, asset_id)
            assert cached.hash_verified and cached.size_bytes == len(PNG)


def test_inline_images_do_not_consume_statement_text_budget():
    payload = _normalise_remote_record(record(PNG + b"\x00" * 100_000), "https://kompege.ru/task?id=31498")
    assert len(payload["statement"]) < 100
    assert "base64" not in json.dumps(payload)


@pytest.mark.parametrize("value", [
    "data:image/svg+xml;base64,PHN2Zz4=", "data:text/html;base64,PGgxPg==",
    "data:image/png;base64,AAAA", "data:image/png;base64,%%%",
    "data:image/png;base64," + "A" * (MAX_INLINE_BYTES * 2),
], ids=["svg", "html", "signature", "encoding", "size"])
def test_unsupported_embedded_content_is_rejected(value):
    with pytest.raises(ValueError):
        decode_inline_image(value)


def test_locator_cannot_change_host_path_index_or_hash():
    locator = _normalise_remote_record(record(), "https://kompege.ru/task?id=31498")["assets"][0]["url"]
    assert parse_inline_image(locator).endpoint == ENDPOINT
    assert canonical_asset_reference(locator) == locator
    for bad in [locator.replace("kompege.ru", "evil.example"), locator.replace("/number/1", "/number/28"),
                locator.replace(":0:", ":20:"), locator + "?x=1"]:
        with pytest.raises(SourceError):
            canonical_asset_reference(bad)


def test_changed_image_is_not_served_as_the_pinned_revision():
    payload = _normalise_remote_record(record(), "https://kompege.ru/task?id=31498")
    asset = payload["assets"][0]
    settings = Settings(source_mode="sync", source_network_enabled=True)

    def transport(url, *_):
        return SourceResponse(200, url, {}, json.dumps([record(PNG + b"changed")]).encode())

    client = BoundedKegeClient(settings, transport=transport, resolver=lambda *_: ["8.8.8.8"])
    with pytest.raises(SourceFetchError, match="differs from pinned source"):
        client.fetch_bytes(asset["url"], filename=asset["filename"], declared_media_type=asset["media_type"])

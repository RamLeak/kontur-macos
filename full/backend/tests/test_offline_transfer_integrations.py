"""Full backups against initialized applications and admitted asset boundaries."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3

import pytest
from test_offline_transfer import _manager

from app.models import Asset, SourcePolicy, User
from offline_transfer import MAX_FULL_ASSET_BYTES, TransferError


def decoded(response):
    assert response["status"] == 200
    return json.loads(base64.b64decode(response["bodyBase64"]))


def test_full_backup_between_initialized_profiles_retains_local_bookkeeping(seed_factory, offline_factory, tmp_path):
    bundle, _receipt = seed_factory()
    source = offline_factory(bundle, tmp_path / "source-profile")
    target_profile = tmp_path / "target-profile"
    target = offline_factory(bundle, target_profile)
    entries = {"kontur:interface-preferences:v1": {"theme": "dark"}}
    body = json.dumps({"expectedVersion": 0, "value": {"version": 1, "entries": entries}})
    assert source.request("PUT", "/api/v1/local/client-state", body)["status"] == 200
    assert (
        source.request("POST", "/api/v1/conversations", '{"title":"Synthetic restored conversation"}')["status"] == 201
    )
    with target.app.state.session_factory() as session:
        local_created = session.get(User, "owner").created_at
        local_policy_id = session.query(SourcePolicy).one().id
    with source.app.state.session_factory() as session:
        assert session.query(SourcePolicy).one().id != local_policy_id
        assert session.get(User, "owner").created_at != local_created
    archive = tmp_path / "initialized.kontur"
    source.flush()
    export = source.transfer.export_bundle(archive)
    preview = target.transfer.preview(archive)
    assert preview["conflictCount"] == 0
    target.flush()
    result = target.transfer.import_bundle(archive, export["contentSha256"])
    assert result["imported"]["conversations"] == 1
    with target.app.state.session_factory() as session:
        assert session.get(User, "owner").created_at == local_created
        assert session.query(SourcePolicy).one().id == local_policy_id
    expected = {"version": 1, "value": {"version": 1, "entries": entries}}
    assert decoded(target.request("GET", "/api/v1/local/client-state")) == expected
    assert target.transfer.import_bundle(archive, export["contentSha256"])["imported"] == {}
    target.flush()
    target.close()
    reopened = offline_factory(bundle, target_profile)
    assert decoded(reopened.request("GET", "/api/v1/local/client-state")) == expected
    assert decoded(reopened.request("GET", "/api/v1/tasks"))["total"] == 2
    assert base64.b64decode(reopened.request("GET", "/api/v1/assets/a-1/download")["bodyBase64"]) == b"1 2 3\n"
    # A later target edit is user state, and must still conflict atomically.
    changed = json.dumps({"expectedVersion": 1, "value": {"version": 1, "entries": {}}})
    assert reopened.request("PUT", "/api/v1/local/client-state", changed)["status"] == 200
    with pytest.raises(TransferError) as conflict:
        reopened.transfer.import_bundle(archive, export["contentSha256"])
    assert conflict.value.code == "record_conflict"
    assert decoded(reopened.request("GET", "/api/v1/local/client-state"))["version"] == 2


def ready_archive(tmp_path, payload):
    source, engine, factory = _manager(tmp_path, "asset-source")
    checksum = hashlib.sha256(payload).hexdigest()
    key = "ab/asset.bin"
    target = source.settings.asset_root / key
    target.parent.mkdir()
    target.write_bytes(payload)
    try:
        with factory.begin() as session:
            session.add(User(id="owner", role="owner"))
            session.add(
                Asset(
                    id="ready-asset",
                    source_name="own",
                    canonical_url="https://example.invalid/synthetic",
                    storage_key=key,
                    filename="asset.bin",
                    media_type="application/octet-stream",
                    detected_type="application/octet-stream",
                    size_bytes=len(payload),
                    content_hash=checksum,
                    expected_content_hash=checksum,
                    cache_state="ready",
                    rights_status="private_authorized",
                    hash_verified=True,
                )
            )
        archive = tmp_path / "ready.kontur"
        source.export_bundle(archive)
        return archive
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE transfer_assets SET bytes=NULL",
        "UPDATE transfer_assets SET storage_key='other/asset.bin'",
        "UPDATE transfer_assets SET filename='different.bin'",
        "UPDATE assets SET size_bytes=size_bytes+1",
    ],
)
def test_incoherent_ready_asset_rejected_before_backup_or_mutation(tmp_path, mutation):
    archive = ready_archive(tmp_path, b"synthetic attachment")
    with sqlite3.connect(archive) as connection:
        connection.execute(mutation)
    target, engine, factory = _manager(tmp_path, "asset-target")
    try:
        with pytest.raises(TransferError) as rejected:
            target.import_bundle(archive, hashlib.sha256(archive.read_bytes()).hexdigest())
        assert rejected.value.code == "invalid_bundle"
        assert not (target.profile / "backups").exists()
        assert not any(target.settings.asset_root.iterdir())
        with factory() as session:
            assert session.query(User).count() == session.query(Asset).count() == 0
    finally:
        engine.dispose()


def test_full_backup_roundtrips_maximum_admitted_attachment(tmp_path):
    payload = b"x" * MAX_FULL_ASSET_BYTES
    archive = ready_archive(tmp_path, payload)
    target, engine, factory = _manager(tmp_path, "large-asset-target")
    try:
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        target.preview(archive)
        target.import_bundle(archive, digest)
        with factory() as session:
            asset = session.get(Asset, "ready-asset")
            assert asset.size_bytes == 64_000_000
            assert (target.settings.asset_root / asset.storage_key).read_bytes() == payload
    finally:
        engine.dispose()


def test_full_backup_rejects_attachment_above_admitted_limit(tmp_path):
    with pytest.raises(TransferError) as oversized:
        ready_archive(tmp_path, b"x" * (MAX_FULL_ASSET_BYTES + 1))
    assert oversized.value.code == "limit_exceeded"
    assert not (tmp_path / "ready.kontur").exists()


def test_archive_cannot_replace_fixed_owner_role(tmp_path):
    archive = ready_archive(tmp_path, b"synthetic attachment")
    with sqlite3.connect(archive) as connection:
        connection.execute("UPDATE users SET role='admin'")
    target, engine, factory = _manager(tmp_path, "role-target")
    try:
        with pytest.raises(TransferError) as rejected:
            target.import_bundle(archive, hashlib.sha256(archive.read_bytes()).hexdigest())
        assert rejected.value.code == "foreign_owner"
        assert not (target.profile / "backups").exists()
        with factory() as session:
            assert session.query(User).count() == 0
    finally:
        engine.dispose()

"""A full archive above the legacy container cap stays bounded in Python RAM."""
from __future__ import annotations

import gc
import hashlib
import json
import tracemalloc
from pathlib import Path

import pytest
from test_offline_transfer import _manager

import offline_transfer
from app.db import make_engine, make_session_factory
from app.models import Asset, User

ASSET_COUNT = 80
PAYLOAD_BYTES = 4 * 1024 * 1024
PEAK_BYTES = 32 * 1024 * 1024
STORAGE_KEY = "ab/shared-large.bin"


def _digest_file(path):
    checksum = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def _peak(operation):
    gc.collect()
    tracemalloc.start()
    try:
        result = operation()
        return result, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_full_archive_above_legacy_container_cap_roundtrips_without_body_pool(tmp_path, monkeypatch):
    source, source_engine, factory = _manager(tmp_path, "large-source")
    target, target_engine, _target_factory = _manager(tmp_path, "large-target")
    reopened_engine = None
    payload = b"x" * PAYLOAD_BYTES
    checksum = hashlib.sha256(payload).hexdigest()
    source_file = source.settings.asset_root / STORAGE_KEY
    source_file.parent.mkdir()
    source_file.write_bytes(payload)
    del payload
    with factory.begin() as session:
        session.add(User(id="owner", role="owner"))
        session.add_all([
            Asset(
                id=f"large-{index:03}", source_name="own", canonical_url=f"https://example.invalid/large/{index}",
                storage_key=STORAGE_KEY, filename="shared-large.bin", media_type="application/octet-stream",
                detected_type="application/octet-stream", size_bytes=PAYLOAD_BYTES, content_hash=checksum,
                expected_content_hash=checksum, cache_state="ready", rights_status="private_authorized",
                hash_verified=True,
            )
            for index in range(ASSET_COUNT)
        ])

    archive = tmp_path / "above-legacy-limit.kontur"
    original_read = Path.read_bytes

    def no_archive_read(path):
        if path.suffix == ".kontur" or path.name.startswith(".kontur-private-"):
            pytest.fail("Full archives and their private copies must be streamed")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", no_archive_read)
    try:
        exported, export_peak = _peak(lambda: source.export_bundle(archive))
        archive_bytes = archive.stat().st_size
        assert archive_bytes > 256 * 1024 * 1024
        assert archive_bytes > ASSET_COUNT * PAYLOAD_BYTES
        assert export_peak < PEAK_BYTES
        assert _digest_file(archive) == exported["contentSha256"]

        preview, preview_peak = _peak(lambda: target.preview(archive))
        assert preview_peak < PEAK_BYTES
        assert preview["counts"]["assets"] == ASSET_COUNT
        assert preview["contentSha256"] == exported["contentSha256"]

        published_assets = []
        publish = offline_transfer._publish_noclobber

        def count_asset_publish(temporary, destination):
            publish(temporary, destination)
            if destination.is_relative_to(target.settings.asset_root):
                published_assets.append(destination)

        with monkeypatch.context() as import_patch:
            import_patch.setattr(offline_transfer, "_publish_noclobber", count_asset_publish)
            imported, import_peak = _peak(lambda: target.import_bundle(archive, exported["contentSha256"]))
        assert import_peak < PEAK_BYTES
        assert imported["contentSha256"] == exported["contentSha256"]
        assert imported["imported"]["assets"] == ASSET_COUNT
        target_file = target.settings.asset_root / STORAGE_KEY
        assert published_assets == [target_file]
        assert [path for path in target.settings.asset_root.rglob("*") if path.is_file()] == [target_file]
        assert target_file.stat().st_size == PAYLOAD_BYTES
        assert _digest_file(target_file) == checksum

        target_engine.dispose()
        reopened_engine = make_engine(target.settings.database_url)
        reopened_factory = make_session_factory(reopened_engine)
        with reopened_factory() as session:
            assets = session.query(Asset).all()
            assert {asset.id for asset in assets} == {f"large-{index:03}" for index in range(ASSET_COUNT)}
            assert all(
                asset.storage_key == STORAGE_KEY and asset.size_bytes == PAYLOAD_BYTES
                and asset.content_hash == checksum and asset.expected_content_hash == checksum
                and asset.cache_state == "ready" and asset.hash_verified is True
                for asset in assets
            )
        assert _digest_file(target_file) == checksum
        print("KONTUR_LARGE_TRANSFER_METADATA " + json.dumps({
            "archiveBytes": archive_bytes, "assetRows": ASSET_COUNT, "uniqueAssetFiles": len(published_assets),
            "exportPeakBytes": export_peak, "previewPeakBytes": preview_peak, "importPeakBytes": import_peak,
            "reopenedRowsAndHashVerified": True,
        }, sort_keys=True))
    finally:
        if reopened_engine is not None:
            reopened_engine.dispose()
        target_engine.dispose()
        source_engine.dispose()

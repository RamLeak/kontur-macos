"""Full transfers keep attachment bytes on disk and pin one immutable image."""
from __future__ import annotations

import hashlib
import tracemalloc
from pathlib import Path

import pytest
from test_offline_transfer import _manager

import offline_transfer
from app.models import Asset, User
from offline_transfer import TransferError


def _populated(tmp_path):
    manager, engine, factory = _manager(tmp_path, "stream-source")
    payload = b"synthetic attachment\n" * (4 * 1024 * 1024 // 21)
    checksum = hashlib.sha256(payload).hexdigest()
    key = "ab/shared.bin"
    target = manager.settings.asset_root / key
    target.parent.mkdir()
    target.write_bytes(payload)
    with factory.begin() as session:
        session.add(User(id="owner", role="owner"))
        for index in range(6):
            session.add(Asset(
                id=f"stream-{index}", source_name="own", canonical_url=f"https://example.invalid/{index}",
                storage_key=key, filename="shared.bin", media_type="application/octet-stream",
                detected_type="application/octet-stream", size_bytes=len(payload), content_hash=checksum,
                expected_content_hash=checksum, cache_state="ready", rights_status="private_authorized",
                hash_verified=True,
            ))
    return manager, engine, len(payload), checksum


def _peak(operation):
    tracemalloc.start()
    try:
        value = operation()
        return value, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_full_export_preview_import_do_not_materialize_container_or_all_bodies(tmp_path, monkeypatch):
    source, source_engine, size, checksum = _populated(tmp_path)
    target, target_engine, factory = _manager(tmp_path, "stream-target")
    archive = tmp_path / "streaming.kontur"
    original_read = Path.read_bytes

    def no_container_read(path):
        if path.suffix == ".kontur":
            pytest.fail("Full containers must be streamed")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", no_container_read)
    try:
        exported, export_peak = _peak(lambda: source.export_bundle(archive))
        preview, preview_peak = _peak(lambda: target.preview(archive))
        imported, import_peak = _peak(lambda: target.import_bundle(archive, exported["contentSha256"]))
        # Six 4-MiB BLOBs fit the wire limits but must not coexist in Python RAM.
        assert archive.stat().st_size > size * 6
        assert max(export_peak, preview_peak, import_peak) < 16 * 1024 * 1024
        assert preview["contentSha256"] == exported["contentSha256"] == imported["contentSha256"]
        assert imported["imported"]["assets"] == 6
        with factory() as session:
            assert session.query(Asset).count() == 6
        assert hashlib.sha256((target.settings.asset_root / "ab/shared.bin").read_bytes()).hexdigest() == checksum
    finally:
        source_engine.dispose()
        target_engine.dispose()


def test_streaming_keeps_cumulative_limits_and_cleans_failed_private_copy(tmp_path, monkeypatch):
    source, source_engine, size, _checksum = _populated(tmp_path)
    target, target_engine, factory = _manager(tmp_path, "limited-target")
    archive = tmp_path / "limited.kontur"
    private_root = tmp_path / "private"
    private_root.mkdir()
    monkeypatch.setattr(offline_transfer.tempfile, "tempdir", str(private_root))
    try:
        monkeypatch.setattr(offline_transfer, "MAX_FULL_TOTAL_ASSET_BYTES", size * 5)
        with pytest.raises(TransferError) as export_error:
            source.export_bundle(archive)
        assert export_error.value.code == "limit_exceeded"
        assert not archive.exists()
        monkeypatch.setattr(offline_transfer, "MAX_FULL_TOTAL_ASSET_BYTES", size * 6)
        source.export_bundle(archive)
        monkeypatch.setattr(offline_transfer, "MAX_FULL_TOTAL_ASSET_BYTES", size * 5)
        with pytest.raises(TransferError) as preview_error:
            target.preview(archive)
        assert preview_error.value.code == "limit_exceeded"
        assert list(private_root.iterdir()) == []
        with factory() as session:
            assert session.query(Asset).count() == 0
        assert list(target.settings.asset_root.rglob("*.bin")) == []
    finally:
        source_engine.dispose()
        target_engine.dispose()


def test_source_changed_after_preview_is_rejected_before_import(tmp_path):
    source, source_engine, _size, _checksum = _populated(tmp_path)
    target, target_engine, factory = _manager(tmp_path, "changed-target")
    archive = tmp_path / "changed.kontur"
    try:
        exported = source.export_bundle(archive)
        target.preview(archive)
        with archive.open("ab") as output:
            output.write(b"changed after preview")
        with pytest.raises(TransferError) as error:
            target.import_bundle(archive, exported["contentSha256"])
        assert error.value.code == "import_changed"
        with factory() as session:
            assert session.query(Asset).count() == 0
        assert list(target.settings.asset_root.rglob("*.bin")) == []
    finally:
        source_engine.dispose()
        target_engine.dispose()


def test_asset_growth_after_fstat_is_rejected_with_bounded_read(tmp_path, monkeypatch):
    source, engine, size, _checksum = _populated(tmp_path)
    asset_path = source.settings.asset_root / "ab/shared.bin"
    original_open = Path.open
    monkeypatch.setattr(offline_transfer, "MAX_FULL_ASSET_BYTES", size)
    requested = []

    class GrowingFile:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def fileno(self):
            return self.handle.fileno()

        def read(self, count=-1):
            requested.append(count)
            assert 0 <= count <= size + 1, "Asset reads must remain bounded after fstat"
            with original_open(asset_path, "ab") as output:
                output.write(b"growth after metadata check")
            return self.handle.read(count)

    def opening(path, mode="r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        return GrowingFile(handle) if path == asset_path and mode == "rb" else handle

    monkeypatch.setattr(Path, "open", opening)
    try:
        with pytest.raises(TransferError) as error:
            source.export_bundle(tmp_path / "growing.kontur")
        assert error.value.code == "limit_exceeded"
        assert requested == [size + 1]
        assert not (tmp_path / "growing.kontur").exists()
    finally:
        engine.dispose()


def test_failed_asset_copy_removes_only_new_files_and_keeps_database_unchanged(tmp_path, monkeypatch):
    source, source_engine, _size, _checksum = _populated(tmp_path)
    target, target_engine, factory = _manager(tmp_path, "rollback-target")
    second = source.settings.asset_root / "ac/other.bin"
    second.parent.mkdir()
    second.write_bytes((source.settings.asset_root / "ab/shared.bin").read_bytes())
    with source.session_factory.begin() as session:
        session.get(Asset, "stream-1").storage_key = "ac/other.bin"
    archive = tmp_path / "rollback.kontur"
    sentinel = target.settings.asset_root / "untouched.txt"
    sentinel.write_bytes(b"existing synthetic file")
    original_copy = offline_transfer._AssetWrite.copy_to
    calls = []

    def interrupted_copy(item, output):
        calls.append(item.path)
        if len(calls) == 2:
            output.write(b"partial synthetic body")
            raise OSError("synthetic disk write failure")
        original_copy(item, output)

    try:
        exported = source.export_bundle(archive)
        target.preview(archive)
        monkeypatch.setattr(offline_transfer._AssetWrite, "copy_to", interrupted_copy)
        with pytest.raises(TransferError) as error:
            target.import_bundle(archive, exported["contentSha256"])
        assert error.value.code == "import_failed"
        assert len(calls) == 2
        assert {path.relative_to(target.settings.asset_root).as_posix()
                for path in target.settings.asset_root.rglob("*") if path.is_file()} == {"untouched.txt"}
        assert sentinel.read_bytes() == b"existing synthetic file"
        with factory() as session:
            assert session.query(Asset).count() == 0
    finally:
        source_engine.dispose()
        target_engine.dispose()

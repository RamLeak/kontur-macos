"""Early validation errors close private SQLite cursors before file cleanup."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from test_offline_transfer import _manager
from test_offline_transfer_streaming import _populated

import offline_transfer
from app.models import Asset
from offline_transfer import TransferError


@pytest.mark.parametrize(
    "failure",
    ["blob_checksum", "blob_type", "model_type", "asset_total", "table_total", "row_count", "full_value"],
)
def test_preview_early_failure_closes_private_cursors_without_mutation(tmp_path, monkeypatch, failure):
    source, source_engine, size, _checksum = _populated(tmp_path)
    target, target_engine, _factory = _manager(tmp_path, "cleanup-target")
    private_root = tmp_path / "private"
    private_root.mkdir()
    monkeypatch.setattr(offline_transfer.tempfile, "tempdir", str(private_root))
    archive = tmp_path / "early-invalid.kontur"
    try:
        source.export_bundle(archive)
        mutations = {
            "blob_checksum": ("UPDATE transfer_assets SET bytes=? WHERE asset_id=?", (b"corrupt", "stream-0")),
            "blob_type": ("UPDATE transfer_assets SET bytes=? WHERE asset_id=?", ("not-a-blob", "stream-0")),
            "model_type": ("UPDATE assets SET size_bytes=? WHERE id=?", ("not-an-integer", "stream-0")),
        }
        if failure in mutations:
            statement, parameters = mutations[failure]
            with closing(sqlite3.connect(archive)) as connection:
                connection.execute(statement, parameters)
                connection.commit()
        elif failure == "asset_total":
            monkeypatch.setattr(offline_transfer, "MAX_FULL_TOTAL_ASSET_BYTES", size)
        elif failure == "table_total":
            monkeypatch.setattr(offline_transfer, "MAX_FULL_TABLE_BYTES", 1)
        elif failure == "row_count":
            monkeypatch.setattr(offline_transfer, "MAX_ROWS_PER_TABLE", 1)
        elif failure == "full_value":
            monkeypatch.setattr(offline_transfer, "MAX_FULL_VALUE_BYTES", 1)

        # Keep the exception traceback alive while checking cleanup. On Windows,
        # an unclosed cursor in that traceback retains the private file handle.
        with pytest.raises(TransferError) as rejected:
            target.preview(archive)
        if failure == "blob_checksum":
            assert rejected.value.code == "checksum_mismatch"
        assert list(private_root.iterdir()) == []
        with target.session_factory() as session:
            assert session.query(Asset).count() == 0
        assert list(target.settings.asset_root.iterdir()) == []
        assert not (Path(target.profile) / "backups").exists()
    finally:
        target_engine.dispose()
        source_engine.dispose()

"""Legacy archive warnings must fit the native Settings preview contract."""

import sqlite3

from test_offline_transfer import _legacy_bundle, _manager

from app.models import OwnerSyncRecord


def test_many_unknown_attempts_preview_and_import_without_repeated_warnings(tmp_path):
    manager, engine, factory = _manager(tmp_path, "many-legacy-warnings")
    try:
        archive = tmp_path / "many-legacy.kontur"
        _legacy_bundle(archive)
        with sqlite3.connect(archive) as connection:
            connection.executemany(
                "INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?)",
                [
                    (f"unknown-{index}", "absent-source", "absent-revision", "wrong", "manual", 0, "", "")
                    for index in range(1001)
                ],
            )
        preview = manager.preview(archive)
        assert preview["counts"]["attempts"] == 1004
        assert preview["warnings"] == ["legacy_unknown_revision", "legacy_unknown_document_ledger_only"]
        result = manager.import_bundle(archive, preview["contentSha256"])
        assert result["warnings"] == preview["warnings"]
        with factory() as session:
            assert session.query(OwnerSyncRecord).filter(OwnerSyncRecord.kind == "imported_attempt").count() == 1004
    finally:
        engine.dispose()

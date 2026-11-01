"""Synthetic, local-only transfer tests.

These tests create temporary SQLite databases and asset roots.  They never
read a user profile, live database, credentials, or network resource.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from app.config import Settings
from app.db import init_schema, make_engine, make_session_factory
from app.models import (
    Asset,
    Attempt,
    BoardDocument,
    BoardVersion,
    CodeDocument,
    CodeVersion,
    LearningEvent,
    OwnerSyncRecord,
    SolveSession,
    Task,
    TaskRevision,
    User,
    utcnow,
)
from app.util import sha256_bytes
from offline_state import RECORD_ID
from offline_transfer import LOCAL_APPLICATION_ID, TransferError, TransferManager

LEGACY_DDL = """
CREATE TABLE metadata (key TEXT NOT NULL PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE assets (id TEXT NOT NULL PRIMARY KEY,name TEXT NOT NULL,mime TEXT NOT NULL,
bytes BLOB NOT NULL,sha256 TEXT NOT NULL);
CREATE TABLE tasks (source_id TEXT NOT NULL,revision TEXT NOT NULL,type_number INTEGER NOT NULL,
title TEXT NOT NULL,statement TEXT NOT NULL,blocks_json TEXT,asset_ids_json TEXT NOT NULL,
PRIMARY KEY(source_id,revision));
CREATE TABLE task_heads (source_id TEXT NOT NULL PRIMARY KEY,revision TEXT NOT NULL);
CREATE TABLE attempts (id TEXT NOT NULL PRIMARY KEY,task_source_id TEXT NOT NULL,task_revision TEXT,
verdict TEXT NOT NULL,source TEXT NOT NULL,created_at INTEGER NOT NULL,code TEXT NOT NULL,answer TEXT NOT NULL);
CREATE TABLE documents (id TEXT NOT NULL PRIMARY KEY,task_source_id TEXT,task_revision TEXT,title TEXT NOT NULL,
type_number INTEGER,code TEXT NOT NULL,statement TEXT NOT NULL,board_json TEXT NOT NULL,
revision INTEGER NOT NULL,updated_at INTEGER NOT NULL);
CREATE TABLE preferences (key TEXT NOT NULL PRIMARY KEY,value_json TEXT NOT NULL);
"""


def _client_state_value() -> dict[str, object]:
    history = [
        {
            "id": f"run-{index}",
            "status": "completed",
            "stdout": "é" * 100_000,
            "stderr": "",
            "duration_ms": 1,
            "exit_code": 0,
            "created_at": "2026-01-01T00:00:00.000Z",
        }
        for index in range(20)
    ]
    return {
        "version": 1,
        "entries": {"kontur:device-python-runs:v1:practice%3Asession-1%3Abrowser": history},
    }


def _manager(root: Path, name: str, owner: str = "owner") -> tuple[TransferManager, object, object]:
    # Keep the SQLAlchemy URL relative: the copied app bootstrap derives a
    # parent path from ``str(engine.url)`` and Windows URL quoting would turn
    # an absolute drive letter into a literal ``C%3A`` directory.
    database = Path(os.path.relpath(root / f"{name}.sqlite3", Path.cwd()))
    assets = root / f"{name}-assets"
    assets.mkdir()
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database.as_posix()}",
        asset_root=assets,
        owner_id=owner,
        testing=True,
    )
    engine = make_engine(settings.database_url)
    init_schema(engine)
    factory = make_session_factory(engine)
    return TransferManager(engine, factory, settings, root / f"{name}-profile", owner), engine, factory


def _legacy_bundle(
    path: Path,
    *,
    local: bool = False,
    include_own: bool = False,
    task_source_id: str = "source-1",
    task_revision: str = "r1",
) -> bytes:
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA application_id={LOCAL_APPLICATION_ID if local else 0x4B4F4E54}")
    connection.execute("PRAGMA user_version=1")
    connection.executescript(LEGACY_DDL)
    connection.executemany(
        "INSERT INTO metadata(key,value) VALUES (?,?)",
        [("format", "kontur-local-v1" if local else "kontur-transfer-v1"), ("schemaVersion", "1")],
    )
    content = b"synthetic asset"
    connection.execute(
        "INSERT INTO assets(id,name,mime,bytes,sha256) VALUES (?,?,?,?,?)",
        ("asset-1", "fixture.txt", "text/plain", content, hashlib.sha256(content).hexdigest()),
    )
    connection.execute(
        "INSERT INTO tasks(source_id,revision,type_number,title,statement,blocks_json,asset_ids_json) "
        "VALUES (?,?,?,?,?,?,?)",
        (task_source_id, task_revision, 1, "Synthetic", "Solve", None, '["asset-1"]'),
    )
    connection.execute(
        "INSERT INTO task_heads(source_id,revision) VALUES (?,?)",
        (task_source_id, task_revision),
    )
    connection.execute(
        "INSERT INTO attempts(id,task_source_id,task_revision,verdict,source,created_at,code,answer) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("attempt-known", task_source_id, task_revision, "correct", "manual", 0, "print(1)", "1"),
    )
    connection.execute(
        "INSERT INTO attempts(id,task_source_id,task_revision,verdict,source,created_at,code,answer) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("attempt-unknown", task_source_id, None, "correct", "manual", 0, "legacy", "1"),
    )
    connection.execute(
        "INSERT INTO attempts(id,task_source_id,task_revision,verdict,source,created_at,code,answer) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("attempt-unknown-pair", "missing-source", "missing-revision", "correct", "manual", 0, "legacy", "1"),
    )
    connection.execute(
        "INSERT INTO documents(id,task_source_id,task_revision,title,type_number,code,statement,"
        "board_json,revision,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("doc-1", task_source_id, task_revision, "Draft", 1, "x=1", "Solve", '{"nodes":[1]}', 1, 0),
    )
    connection.execute(
        "INSERT INTO documents(id,task_source_id,task_revision,title,type_number,code,statement,"
        "board_json,revision,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "doc-unknown",
            "missing-source",
            "missing-revision",
            "Unknown draft",
            1,
            "x=2",
            "Solve",
            '{"nodes":[2]}',
            1,
            0,
        ),
    )
    if include_own:
        connection.executemany(
            "INSERT INTO documents(id,task_source_id,task_revision,title,type_number,code,statement,"
            "board_json,revision,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                ("doc-own-1", None, None, "Own draft 1", None, "x=own1", "Own statement 1", '{"nodes":[3]}', 1, 0),
                ("doc-own-2", None, None, "Own draft 2", None, "x=own2", "Own statement 2", '{"nodes":[4]}', 2, 0),
            ],
        )
    connection.execute("INSERT INTO preferences(key,value_json) VALUES ('theme','\"dark\"')")
    if local:
        connection.execute(
            "CREATE TABLE import_receipts (source_id TEXT NOT NULL,fingerprint TEXT NOT NULL,local_id TEXT NOT NULL,"
            "PRIMARY KEY(source_id,fingerprint))"
        )
    connection.commit()
    connection.close()
    return path.read_bytes()


def test_full_roundtrip_preserves_models_and_asset_bytes(tmp_path: Path) -> None:
    manager, engine, factory = _manager(tmp_path, "source")
    try:
        now = utcnow()
        asset_bytes = b"cached synthetic bytes"
        asset_path = Path(manager.settings.asset_root) / "ab" / "asset.txt"
        asset_path.parent.mkdir(parents=True)
        asset_path.write_bytes(asset_bytes)
        session = factory()
        session.add(User(id="owner", role="owner", created_at=now))
        task = Task(
            id="task-1",
            source_name="kompege",
            external_id="source-1",
            line_number=1,
            created_at=now,
            updated_at=now,
        )
        session.add(task)
        session.flush()
        revision = TaskRevision(
            id="revision-1",
            task_id=task.id,
            revision_number=1,
            source_url="synthetic://task-1",
            source_id="source-1",
            line_number=1,
            statement="Synthetic task",
            content_hash="a" * 64,
            retrieved_at=now,
            created_at=now,
        )
        session.add(revision)
        session.flush()
        task.current_revision_id = revision.id
        session.add(
            Asset(
                id="asset-1",
                source_name="kompege",
                canonical_url="synthetic://asset-1",
                storage_key="ab/asset.txt",
                filename="asset.txt",
                media_type="text/plain",
                detected_type="text/plain",
                size_bytes=len(asset_bytes),
                content_hash=sha256_bytes(asset_bytes),
                expected_content_hash=sha256_bytes(asset_bytes),
                cache_state="ready",
                rights_status="private_authorized",
                hash_verified=True,
                created_at=now,
            )
        )
        session.add(
            OwnerSyncRecord(
                id=RECORD_ID,
                owner_id="owner",
                kind="offline_client_state",
                context={"version": 1},
                value=_client_state_value(),
                version=1,
                updated_at=now,
            )
        )
        session.commit()
        session.close()

        archive = tmp_path / "full.kontur"
        exported = manager.export_bundle(archive)
        assert exported["format"] == "kontur-full-v1"
        preview = manager.preview(archive)
        assert preview["counts"]["tasks"] == 1
        assert preview["counts"]["learning_events"] == 0

        target, target_engine, target_factory = _manager(tmp_path, "target")
        try:
            result = target.import_bundle(archive, exported["contentSha256"])
            assert result["imported"]["tasks"] == 1
            session = target_factory()
            assert session.get(Task, "task-1").current_revision_id == "revision-1"
            assert session.get(Asset, "asset-1").content_hash == sha256_bytes(asset_bytes)
            state = session.get(OwnerSyncRecord, RECORD_ID).value
            assert (
                len(state["entries"]["kontur:device-python-runs:v1:practice%3Asession-1%3Abrowser"][0]["stdout"])
                == 100_000
            )
            session.close()
            assert (Path(target.settings.asset_root) / "ab" / "asset.txt").read_bytes() == asset_bytes
        finally:
            target_engine.dispose()
    finally:
        engine.dispose()


def test_legacy_import_retains_unknown_revision_without_learning_event(tmp_path: Path) -> None:
    manager, engine, factory = _manager(tmp_path, "legacy")
    try:
        archive = tmp_path / "legacy.kontur"
        _legacy_bundle(archive)
        preview = manager.preview(archive)
        result = manager.import_bundle(archive, preview["contentSha256"])
        assert result["format"] == "kontur-transfer-v1"
        session = factory()
        try:
            assert session.query(Task).count() == 1
            assert session.query(TaskRevision).count() == 1
            assert session.query(Attempt).count() == 1
            assert session.query(LearningEvent).count() == 0
            unknown = session.query(OwnerSyncRecord).filter(OwnerSyncRecord.kind == "imported_attempt").all()
            assert any(row.value["id"] == "attempt-unknown" and row.value["taskRevision"] is None for row in unknown)
            assert any(row.value["id"] == "attempt-unknown-pair" for row in unknown)
            documents = session.query(OwnerSyncRecord).filter(OwnerSyncRecord.kind == "imported_document").all()
            assert any(row.value["id"] == "doc-unknown" for row in documents)
        finally:
            session.close()
        second = manager.import_bundle(archive, manager.preview(archive)["contentSha256"])
        assert second["duplicateRecords"] > 0
    finally:
        engine.dispose()


def test_invalid_client_state_rejected_before_backup(tmp_path: Path) -> None:
    source, source_engine, source_factory = _manager(tmp_path, "invalid-state-source")
    try:
        session = source_factory()
        session.add(User(id="owner", role="owner", created_at=utcnow()))
        session.flush()
        session.add(
            OwnerSyncRecord(
                id=RECORD_ID,
                owner_id="owner",
                kind="offline_client_state",
                context={"version": 1},
                value=_client_state_value(),
                version=1,
                updated_at=utcnow(),
            )
        )
        session.commit()
        session.close()
        archive = tmp_path / "invalid-state.kontur"
        source.export_bundle(archive)
    finally:
        source_engine.dispose()

    connection = sqlite3.connect(archive)
    connection.execute(
        "UPDATE owner_sync_records SET value=? WHERE id=?",
        (json.dumps({"version": 1, "entries": {"unknown": {}}}), RECORD_ID),
    )
    connection.commit()
    connection.close()

    target, target_engine, target_factory = _manager(tmp_path, "invalid-state-target")
    try:
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        with pytest.raises(TransferError) as raised:
            target.import_bundle(archive, digest)
        assert raised.value.code == "invalid_bundle"
        assert not (tmp_path / "invalid-state-target-profile" / "backups").exists()
        session = target_factory()
        try:
            assert session.query(User).count() == 0
        finally:
            session.close()
    finally:
        target_engine.dispose()


def test_legacy_exact_seed_match_reuses_catalog_revision(tmp_path: Path) -> None:
    manager, engine, factory = _manager(tmp_path, "seed-match")
    try:
        source_id = "kompege:123"
        current_hash = "b" * 64
        session = factory()
        task = Task(
            id="seed-task",
            source_name="kompege",
            external_id="123",
            line_number=1,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        session.add(task)
        session.flush()
        old_revision = TaskRevision(
            id="seed-old-revision",
            task_id=task.id,
            revision_number=1,
            source_url="synthetic://old",
            source_id=source_id,
            line_number=1,
            statement="old",
            content_hash="a" * 64,
            retrieved_at=utcnow(),
            rights_status="private_authorized",
            created_at=utcnow(),
        )
        current_revision = TaskRevision(
            id="seed-current-revision",
            task_id=task.id,
            revision_number=2,
            source_url="synthetic://current",
            source_id=source_id,
            line_number=1,
            statement="current",
            content_hash=current_hash,
            retrieved_at=utcnow(),
            rights_status="private_authorized",
            created_at=utcnow(),
        )
        session.add_all([old_revision, current_revision])
        session.flush()
        task.current_revision_id = old_revision.id
        session.commit()
        session.close()

        archive = tmp_path / "seed-match.kontur"
        _legacy_bundle(archive, task_source_id=source_id, task_revision=current_hash)
        preview = manager.preview(archive)
        manager.import_bundle(archive, preview["contentSha256"])
        session = factory()
        try:
            assert session.query(Task).filter(Task.source_name == "kontur-transfer-v1").count() == 0
            stored_task = session.get(Task, task.id)
            assert stored_task.current_revision_id == old_revision.id
            assert session.query(TaskRevision).filter(TaskRevision.id == current_revision.id).count() == 1
            assert {solve.task_revision_id for solve in session.query(SolveSession).all()} == {current_revision.id}
        finally:
            session.close()
    finally:
        engine.dispose()


def test_mini_own_documents_reopen_as_private_tasks(tmp_path: Path) -> None:
    manager, engine, factory = _manager(tmp_path, "own-mini")
    try:
        source = tmp_path / "own-mini-transfer.sqlite3"
        _legacy_bundle(source, local=True, include_own=True)
        migrated = manager.migrate_mini_store(source)
        assert migrated["format"] == "kontur-local-v1"
        session = factory()
        try:
            own_tasks = session.query(Task).filter(Task.source_name == "own").all()
            assert len(own_tasks) == 2
            own_revisions = [session.get(TaskRevision, task.current_revision_id) for task in own_tasks]
            assert all(revision is not None for revision in own_revisions)
            assert all(revision.rights_status == "private_authorized" for revision in own_revisions if revision)
            assert all(
                revision.task_id == task.id for task, revision in zip(own_tasks, own_revisions, strict=True) if revision
            )
            assert {version.content for version in session.query(CodeVersion).all()} >= {"x=own1", "x=own2"}
            assert {version.snapshot_json["nodes"][0] for version in session.query(BoardVersion).all()} >= {3, 4}
            assert session.query(SolveSession).count() == 4
            assert session.query(CodeDocument).count() == 4
            assert session.query(BoardDocument).count() == 3
        finally:
            session.close()
        repeated = manager.migrate_mini_store(source)
        assert repeated["warnings"] == ["already_migrated"]
    finally:
        engine.dispose()


def test_mini_store_migration_is_digest_idempotent_and_source_unchanged(tmp_path: Path) -> None:
    manager, engine, factory = _manager(tmp_path, "mini-target")
    try:
        source = tmp_path / "mini.sqlite3"
        original = _legacy_bundle(source, local=True)
        digest = hashlib.sha256(original).hexdigest()
        migrated = manager.migrate_mini_store(source)
        assert migrated["format"] == "kontur-local-v1"
        assert migrated["sourceBackupPath"]
        assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
        repeated = manager.migrate_mini_store(source)
        assert repeated["warnings"] == ["already_migrated"]
        session = factory()
        try:
            assert session.query(Task).count() == 1
        finally:
            session.close()
    finally:
        engine.dispose()


def test_changed_bundle_digest_is_rejected_before_backup(tmp_path: Path) -> None:
    manager, engine, factory = _manager(tmp_path, "digest")
    del factory
    try:
        archive = tmp_path / "legacy.kontur"
        _legacy_bundle(archive)
        preview = manager.preview(archive)
        archive.write_bytes(archive.read_bytes() + b"x")
        with pytest.raises(TransferError) as raised:
            manager.import_bundle(archive, preview["contentSha256"])
        assert raised.value.code == "import_changed"
        assert not (tmp_path / "digest-profile" / "backups").exists()
    finally:
        engine.dispose()

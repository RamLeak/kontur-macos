"""Real synthetic SQLite container checks; never imports the legacy server."""

from copy import deepcopy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest.mock import patch
import uuid

from exporter.kontur_transfer import Bundle, ExportError, write_bundle


def fixture_bundle() -> Bundle:
    task = {"sourceId": "synthetic:task:1", "revision": "revision-current", "typeNumber": 17,
            "title": "Синтетическое задание", "statement": "Synthetic condition only",
            "blocks": {"synthetic": True}, "assetIds": ["asset-hello"]}
    previous = {**task, "revision": "revision-old", "statement": "Previous synthetic condition"}
    return Bundle(
        tasks=[task, previous], task_heads={"synthetic:task:1": "revision-current"},
        attempts=[
            {"id": "event-1", "taskSourceId": "synthetic:task:1", "taskRevision": "revision-old",
             "verdict": "correct", "source": "imported", "createdAt": 1000, "code": "print(1)", "answer": "1"},
            {"id": "event-current-1", "taskSourceId": "synthetic:task:1", "taskRevision": "revision-current",
             "verdict": "correct", "source": "imported", "createdAt": 1500, "code": "print(2)", "answer": "2"},
            {"id": "event-2", "taskSourceId": "synthetic:task:1", "taskRevision": "revision-current",
             "verdict": "error", "source": "imported", "createdAt": 2000, "code": "raise RuntimeError()", "answer": ""},
            {"id": "event-unknown", "taskSourceId": "synthetic:task:1", "taskRevision": None,
             "verdict": "correct", "source": "imported", "createdAt": 0, "code": "", "answer": "legacy"},
        ],
        documents=[{"id": "document-1", "taskSourceId": "synthetic:task:1", "taskRevision": "revision-current",
                    "title": "Черновик", "typeNumber": 17, "code": "'); DROP TABLE tasks; --",
                    "statement": "Synthetic saved condition", "board": {"version": 1, "strokes": [[0, 1]]},
                    "revision": 3, "updatedAt": 2000}],
        assets=[{"id": "asset-hello", "name": "example.txt", "mime": "text/plain", "bytes": b"hello",
                 "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"}],
        preferences={"selectedDocumentId": "document-1", "kontur.legacy12.bookmarks": ["synthetic:task:1"]},
    )


class TransferWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.qa_root = Path(__file__).resolve().parent
        self.root = self.qa_root / ("exporter_tmp_" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(self.clean_test_directory)

    def clean_test_directory(self) -> None:
        self.root.resolve().relative_to(self.qa_root)
        shutil.rmtree(self.root)

    def test_roundtrip_exact_schema_history_assets_and_plain_code(self) -> None:
        target = self.root / "roundtrip.kontur"
        result = write_bundle(target, fixture_bundle(), created_at=1234)
        self.assertEqual(target.read_bytes()[:16], b"SQLite format 3\x00")
        self.assertEqual(result.content_sha256, hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertEqual((result.task_count, result.attempt_count, result.document_count, result.asset_count), (2, 4, 1, 1))
        with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)) as connection:
            self.assertEqual(connection.execute("PRAGMA application_id").fetchone(), (0x4B4F4E54,))
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone(), (1,))
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone(), ("ok",))
            self.assertEqual(dict(connection.execute("SELECT key,value FROM metadata")), {
                "format": "kontur-transfer-v1", "schemaVersion": "1", "producer": "kontur-legacy12-exporter/1", "createdAt": "1234"})
            objects = connection.execute("SELECT type,name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_autoindex_%'").fetchall()
            self.assertEqual(set(objects), {("table", name) for name in ("metadata", "assets", "tasks", "task_heads", "attempts", "documents", "preferences")})
            columns = connection.execute("PRAGMA table_info(attempts)").fetchall()
            self.assertEqual([(row[1], row[2], row[3], row[5]) for row in columns], [
                ("id", "TEXT", 1, 1), ("task_source_id", "TEXT", 1, 0), ("task_revision", "TEXT", 0, 0),
                ("verdict", "TEXT", 1, 0), ("source", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0),
                ("code", "TEXT", 1, 0), ("answer", "TEXT", 1, 0)])
            self.assertEqual(connection.execute("SELECT id,task_revision,verdict FROM attempts ORDER BY created_at,id").fetchall(), [
                ("event-unknown", None, "correct"), ("event-1", "revision-old", "correct"),
                ("event-current-1", "revision-current", "correct"), ("event-2", "revision-current", "error")])
            self.assertEqual(connection.execute("SELECT bytes,typeof(bytes),sha256 FROM assets").fetchone(), (
                b"hello", "blob", "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"))
            code, board, revision = connection.execute("SELECT code,board_json,revision FROM documents").fetchone()
            self.assertEqual(code, "'); DROP TABLE tasks; --")
            self.assertEqual(json.loads(board), {"version": 1, "strokes": [[0, 1]]})
            self.assertEqual(revision, 3)

    def test_existing_destination_is_unchanged(self) -> None:
        target = self.root / "existing.kontur"
        target.write_bytes(b"synthetic existing data")
        with self.assertRaises(ExportError) as raised:
            write_bundle(target, fixture_bundle())
        self.assertEqual(raised.exception.code, "destination_exists")
        self.assertEqual(target.read_bytes(), b"synthetic existing data")
        self.assertEqual(list(self.root.iterdir()), [target])

    def test_invalid_data_has_no_partial_file(self) -> None:
        mutations = (
            lambda bundle: bundle.assets[0].update(sha256="0" * 64),
            lambda bundle: bundle.tasks[0].update(typeNumber=True),
            lambda bundle: bundle.tasks[0].update(sourceId="😀" * 65),
            lambda bundle: bundle.tasks[0].update(assetIds=["missing"]),
            lambda bundle: bundle.attempts[0].update(createdAt=1.0),
            lambda bundle: bundle.attempts[-1].update(taskSourceId="missing"),
            lambda bundle: bundle.documents[0].update(board=[]),
            lambda bundle: bundle.documents[0].update(code="x" * (2 * 1024 * 1024 + 1)),
            lambda bundle: bundle.preferences.update(selectedDocumentId="missing"),
            lambda bundle: bundle.attempts.append(deepcopy(bundle.attempts[0])),
            lambda bundle: bundle.task_heads.clear(),
            lambda bundle: bundle.preferences.update({"kontur.legacy12.transferWarnings": None}),
            lambda bundle: bundle.preferences.update({"kontur.legacy12.transferWarnings": ["valid_code", 1]}),
            lambda bundle: bundle.preferences.update({"kontur.legacy12.transferWarnings": [""]}),
        )
        for index, mutate in enumerate(mutations):
            bundle = fixture_bundle()
            mutate(bundle)
            with self.subTest(index=index), self.assertRaises(ExportError):
                write_bundle(self.root / "invalid.kontur", bundle)
            self.assertEqual(list(self.root.iterdir()), [])

    def test_failed_publication_cleans_sqlite_temp_and_retry_works(self) -> None:
        target = self.root / "retry.kontur"
        with patch("exporter.kontur_transfer._publish_new", side_effect=OSError("synthetic disk error")):
            with self.assertRaises(ExportError) as raised:
                write_bundle(target, fixture_bundle())
        self.assertEqual(raised.exception.code, "export_failed")
        self.assertNotIn("synthetic disk", str(raised.exception))
        self.assertEqual(list(self.root.iterdir()), [])
        write_bundle(target, fixture_bundle())
        self.assertTrue(target.is_file())

    def test_competing_destination_is_never_overwritten(self) -> None:
        from exporter.kontur_transfer import _publish_new
        target = self.root / "race.kontur"
        def competing_writer(source: Path, destination: Path) -> None:
            destination.write_bytes(b"competing synthetic file")
            _publish_new(source, destination)
        with patch("exporter.kontur_transfer._publish_new", side_effect=competing_writer):
            with self.assertRaises(ExportError) as raised:
                write_bundle(target, fixture_bundle())
        self.assertEqual(raised.exception.code, "destination_exists")
        self.assertEqual(target.read_bytes(), b"competing synthetic file")
        self.assertEqual(list(self.root.iterdir()), [target])

    def test_same_logical_export_is_reproducible(self) -> None:
        first = write_bundle(self.root / "first.kontur", fixture_bundle(), created_at=1)
        second_bundle = fixture_bundle()
        second_bundle.tasks.reverse()
        second_bundle.attempts.reverse()
        second = write_bundle(self.root / "second.kontur", second_bundle, created_at=1)
        self.assertEqual(first.content_sha256, second.content_sha256)

    def test_future_warning_codes_are_valid_bounded_data(self) -> None:
        bundle = fixture_bundle()
        bundle.preferences["kontur.legacy12.transferWarnings"] = ["future_unknown_code"]
        write_bundle(self.root / "future-warning.kontur", bundle)

    def test_javascript_date_boundary_is_separate_from_document_revision(self) -> None:
        boundary = 8_640_000_000_000_000
        bundle = fixture_bundle()
        bundle.attempts[0]["createdAt"] = boundary
        bundle.documents[0]["updatedAt"] = boundary
        bundle.documents[0]["revision"] = 9_007_199_254_740_991
        write_bundle(self.root / "date-boundary.kontur", bundle, created_at=boundary)
        for field in ("attempt", "document", "metadata"):
            bundle = fixture_bundle()
            if field == "attempt": bundle.attempts[0]["createdAt"] = boundary + 1
            if field == "document": bundle.documents[0]["updatedAt"] = boundary + 1
            with self.subTest(field=field), self.assertRaises(ExportError):
                write_bundle(self.root / (field + ".kontur"), bundle, created_at=boundary + 1 if field == "metadata" else 1)


if __name__ == "__main__":
    unittest.main()

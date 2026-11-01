"""Synthetic-only integration tests. Never discover or open a runtime database."""

import hashlib
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import export_catalog as exporter


OWNED = Path(__file__).resolve().parent
SENTINEL = "PRIVATE_OWNER_SESSION_CONFIG_SENTINEL_7c19"
STAMP = "2026-10-29 00:00:00.000000"
FIXTURE_DDL = """
CREATE TABLE tasks (
 id VARCHAR(36) PRIMARY KEY, source_name VARCHAR(64), external_id VARCHAR(255),
 line_number INTEGER, current_revision_id VARCHAR(36), created_at DATETIME,
 updated_at DATETIME, owner_secret TEXT);
CREATE TABLE task_revisions (
 id VARCHAR(36) PRIMARY KEY, task_id VARCHAR(36), revision_number INTEGER,
 source_url TEXT, source_id VARCHAR(255), line_number INTEGER, exam_year INTEGER,
 target_exam_year INTEGER, source_year INTEGER, source_year_basis VARCHAR(64),
 source_created_at DATETIME, source_difficulty INTEGER, difficulty VARCHAR(16),
 source_label TEXT, source_metadata JSON, statement TEXT, statement_latex TEXT,
 answer TEXT, explanation TEXT, content_hash VARCHAR(64), retrieved_at DATETIME,
 parser_version VARCHAR(64), rights_status VARCHAR(32), asset_manifest JSON,
 subtask_manifest JSON, table_manifest JSON, created_at DATETIME, owner_secret TEXT);
CREATE TABLE assets (
 id VARCHAR(36) PRIMARY KEY, source_name VARCHAR(64), canonical_url TEXT,
 storage_key TEXT, filename VARCHAR(255), media_type VARCHAR(255),
 detected_type VARCHAR(64), size_bytes INTEGER, content_hash VARCHAR(64),
 expected_content_hash VARCHAR(64), cache_state VARCHAR(32), rights_status VARCHAR(32),
 hash_verified BOOLEAN, fetched_at DATETIME, created_at DATETIME, owner_secret TEXT);
CREATE TABLE task_assets (
 id VARCHAR(36) PRIMARY KEY, revision_id VARCHAR(36), asset_id VARCHAR(36),
 runner_filename VARCHAR(255), position INTEGER, owner_secret TEXT);
CREATE TABLE catalog_entries (
 id VARCHAR(36) PRIMARY KEY, job_id VARCHAR(36), revision_id VARCHAR(36), owner_secret TEXT);
CREATE TABLE users (id TEXT, secret TEXT);
CREATE TABLE messages (id TEXT, content TEXT);
CREATE TABLE config (id TEXT, secret TEXT);
CREATE TABLE schema_migrations (version INTEGER, secret TEXT);
"""


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="synthetic-", dir=OWNED)
        self.root = Path(self.directory.name).resolve()
        self.addCleanup(self.cleanup_owned)
        self.database = self.root / "synthetic.sqlite3"
        self.cache = self.root / "synthetic-cache"
        self.cache.mkdir()
        self.output = self.root / "catalog.sqlite3"
        self.connection = sqlite3.connect(self.database)
        self.addCleanup(self.connection.close)
        self.connection.executescript(FIXTURE_DDL)
        self.asset = self.add_asset("a-1", b"1 2 3\n")
        for table in ("users", "messages", "config", "schema_migrations"):
            self.connection.execute("INSERT INTO " + table + " VALUES (?,?)", ("12", SENTINEL))
        self.insert("tasks", {"id": "t-1", "source_name": "kompege", "external_id": "123",
            "line_number": 17, "current_revision_id": "r-2", "created_at": STAMP,
            "updated_at": STAMP, "owner_secret": SENTINEL})
        self.insert("tasks", {"id": "t-other", "source_name": "not-kompege", "external_id": "secret",
            "line_number": 17, "current_revision_id": "r-other", "created_at": STAMP,
            "updated_at": STAMP, "owner_secret": SENTINEL})
        self.revision("r-1", 1, "private_authorized")
        self.revision("r-2", 2, "public_authorized")
        self.revision("r-blocked", 3, "blocked", answer=SENTINEL)
        self.revision("r-other", 1, "public_authorized", task="t-other", answer=SENTINEL)
        for number in (1, 2):
            self.insert("task_assets", {"id": "l-" + str(number), "revision_id": "r-" + str(number),
                "asset_id": "a-1", "runner_filename": "17.txt", "position": 0, "owner_secret": SENTINEL})
        for identifier in ("r-1", "r-blocked"):
            self.insert("catalog_entries", {"id": "ce-" + identifier, "job_id": SENTINEL,
                "revision_id": identifier, "owner_secret": SENTINEL})
        self.connection.commit()

    def cleanup_owned(self):
        # Verify the resolved recursive-cleanup target is our exclusive test child.
        self.assertEqual(self.root.parent, OWNED)
        self.assertTrue(self.root.name.startswith("synthetic-"))
        self.directory.cleanup()

    def insert(self, table, row):
        self.connection.execute("INSERT INTO " + table + " (" + ",".join(row) + ") VALUES (" +
                                ",".join("?" for _ in row) + ")", tuple(row.values()))

    def add_asset(self, identifier, data, *, filename="17.txt", url="https://kompege.ru/files/17.txt"):
        digest = hashlib.sha256(data).hexdigest()
        key = digest[:2] + "/" + digest + ".txt"
        target = self.cache / key
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(data)
        row = {"id": identifier, "source_name": "kompege", "canonical_url": url, "storage_key": key,
            "filename": filename, "media_type": "text/plain", "detected_type": "text/plain",
            "size_bytes": len(data), "content_hash": digest, "expected_content_hash": None,
            "cache_state": "ready", "rights_status": "private_authorized", "hash_verified": 1,
            "fetched_at": STAMP, "created_at": STAMP, "owner_secret": SENTINEL}
        self.insert("assets", row)
        return row

    def revision(self, identifier, number, rights, *, task="t-1", answer="42"):
        manifest = [{"asset_id": "a-1", "url": self.asset["canonical_url"], "filename": "17.txt",
                     "content_hash": self.asset["content_hash"], "size_bytes": 6,
                     "media_type": "text/plain", "role": "file"}]
        self.insert("task_revisions", {"id": identifier, "task_id": task, "revision_number": number,
            "source_url": "https://kompege.ru/task?id=123", "source_id": "123", "line_number": 17,
            "exam_year": 2026, "target_exam_year": 2027, "source_year": 2026,
            "source_year_basis": "source-explicit-year", "source_created_at": STAMP,
            "source_difficulty": 2, "difficulty": "hard", "source_label": "Реальное поле источника",
            "source_metadata": encode({"kege_schema": "kege-current-bank/2.0", "title": "Задача"}),
            "statement": "Синтетика: найти число α", "statement_latex": r"x^2+\alpha",
            "answer": answer, "explanation": "Синтетическое объяснение", "content_hash": "a" * 64,
            "retrieved_at": STAMP, "parser_version": "synthetic-v1", "rights_status": rights,
            "asset_manifest": encode(manifest), "subtask_manifest": "[]",
            "table_manifest": encode({"rows": [[1, 2]], "columns": ["A", "B"]}),
            "created_at": STAMP, "owner_secret": SENTINEL})

    def update_asset(self, **values):
        self.connection.execute("UPDATE assets SET " + ",".join(key + "=?" for key in values) +
                                " WHERE id='a-1'", tuple(values.values()))
        self.connection.commit()

    def update_revision(self, field, value, identifier="r-2"):
        self.connection.execute("UPDATE task_revisions SET " + field + "=? WHERE id=?", (value, identifier))
        self.connection.commit()

    def export(self):
        self.connection.commit()
        return exporter.export_catalog(self.database, self.cache, self.output)

    def metadata(self):
        with closing(sqlite3.connect(self.output)) as connection:
            return {key: json.loads(value) for key, value in connection.execute("SELECT key,value FROM catalog_metadata")}

    def records(self, table):
        with closing(sqlite3.connect(self.output)) as connection:
            return {key: json.loads(value) for key, value in connection.execute("SELECT id,record_json FROM " + table)}

    def no_temporary(self):
        self.assertFalse(list(self.root.glob(".catalog-seed-*")))

    def test_complete_faithful_records_and_privacy(self):
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        receipt = self.export()
        self.assertEqual(receipt["exit"], 0)
        self.assertTrue(receipt["complete"])
        self.assertEqual(receipt["counts"]["deniedRevisions"], 1)
        self.assertEqual(receipt["counts"]["otherSourceRevisions"], 1)
        self.assertEqual(receipt["sourceReadColumns"]["catalog_entries"], ["revision_id"])
        self.assertEqual(set(receipt["sourceReadColumns"]), {"tasks", "task_revisions", "assets", "task_assets", "catalog_entries"})
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).hexdigest())
        revisions = self.records("catalog_revisions")
        self.assertEqual(set(revisions), {"r-1", "r-2"})
        self.assertEqual(revisions["r-1"]["rights_status"], "private_authorized")
        for field, expected in {"answer": "42", "explanation": "Синтетическое объяснение",
            "statement_latex": r"x^2+\alpha", "source_year": 2026, "difficulty": "hard",
            "source_metadata": {"kege_schema": "kege-current-bank/2.0", "title": "Задача"}}.items():
            self.assertEqual(revisions["r-2"][field], expected)
        self.assertNotIn("storage_key", self.records("catalog_assets")["a-1"])
        self.assertNotIn(SENTINEL.encode(), self.output.read_bytes())
        self.assertNotIn(b"owner_secret", self.output.read_bytes())
        self.no_temporary()

    def test_independent_contract_hashes_and_exact_seven_tables(self):
        receipt = self.export()
        metadata = self.metadata()
        with closing(sqlite3.connect(self.output)) as connection:
            self.assertEqual(connection.execute("PRAGMA application_id").fetchone()[0], 0x4B435331)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            self.assertEqual(tables, {"catalog_metadata", "catalog_tasks", "catalog_revisions", "catalog_assets", "catalog_links", "catalog_selection", "catalog_asset_bytes"})
            for table in ("catalog_tasks", "catalog_revisions", "catalog_assets", "catalog_links"):
                lines = b"".join((encode([key, json.loads(value)]) + "\n").encode() for key, value in connection.execute("SELECT id,record_json FROM " + table + " ORDER BY id"))
                self.assertEqual(hashlib.sha256(lines).hexdigest(), metadata["collectionsSha256"][table])
            selection = list(connection.execute("SELECT revision_id FROM catalog_selection ORDER BY revision_id"))
            self.assertEqual(selection, [("r-1",), ("r-2",)])
            lines = b"".join((encode([key]) + "\n").encode() for (key,) in selection)
            self.assertEqual(hashlib.sha256(lines).hexdigest(), metadata["collectionsSha256"]["catalog_selection"])
            rows = list(connection.execute("SELECT asset_id,sha256,bytes FROM catalog_asset_bytes ORDER BY asset_id"))
            self.assertEqual(rows[0][2], b"1 2 3\n")
            for _, digest, data in rows:
                self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
            lines = b"".join((encode([key, digest, len(data)]) + "\n").encode() for key, digest, data in rows)
            self.assertEqual(hashlib.sha256(lines).hexdigest(), metadata["collectionsSha256"]["catalog_asset_bytes"])
        expected = metadata.pop("manifestSha256")
        self.assertEqual(hashlib.sha256(encode(metadata).encode()).hexdigest(), expected)
        self.assertEqual(hashlib.sha256(self.output.read_bytes()).hexdigest(), receipt["fileSha256"])

    def test_missing_file_is_incomplete(self):
        (self.cache / self.asset["storage_key"]).unlink()
        result = self.export()
        self.assertEqual(result["exit"], 2)
        self.assertEqual(result["counts"]["unavailableAssets"], 1)
        self.assertEqual(result["issueCounts"], {"asset_missing_or_unreadable": 1})
        self.assertFalse(self.metadata()["complete"])

    def test_metadata_only_no_network_completion(self):
        self.update_asset(cache_state="metadata_only", hash_verified=0, storage_key=None)
        result = self.export()
        self.assertEqual(result["issueCounts"], {"asset_not_ready": 1})
        self.assertEqual(result["counts"]["assetBytesRows"], 0)

    def test_malicious_storage_paths_never_read_external_bytes(self):
        outside = self.root / "outside.txt"
        outside.write_text(SENTINEL, encoding="utf-8")
        digest = self.asset["content_hash"]
        for key in ("../outside.txt", str(outside), "C:/outside.txt", "\\\\host\\share\\file", "ab\\" + digest,
                    "00/" + digest + ".txt", digest[:2] + "/" + "0" * 64 + ".txt", ".env"):
            with self.subTest(key=key):
                self.update_asset(storage_key=key)
                result = self.export()
                self.assertEqual(result["issueCounts"], {"asset_path_invalid": 1})
                self.assertNotIn(SENTINEL.encode(), self.output.read_bytes())
                self.output.unlink()
        self.assertEqual(outside.read_text(encoding="utf-8"), SENTINEL)

    def test_hash_mismatch_is_incomplete(self):
        (self.cache / self.asset["storage_key"]).write_bytes(b"9 9 9\n")
        self.assertEqual(self.export()["issueCounts"], {"asset_hash_mismatch": 1})

    def test_expected_hash_mismatch_is_incomplete(self):
        self.update_asset(expected_content_hash="b" * 64)
        self.assertEqual(self.export()["issueCounts"], {"asset_expected_hash_mismatch": 1})

    def test_size_mismatch_is_incomplete(self):
        self.update_asset(size_bytes=7)
        self.assertEqual(self.export()["issueCounts"], {"asset_size_mismatch": 1})

    def test_oversize_is_rejected_before_content_read(self):
        self.update_asset(size_bytes=20_000_001)
        with patch.object(exporter.os, "read", side_effect=AssertionError("must not read oversize asset")):
            result = self.export()
        self.assertEqual(result["issueCounts"], {"asset_too_large": 1})

    def test_unverified_or_quarantined_is_incomplete(self):
        self.update_asset(hash_verified=0)
        self.assertEqual(self.export()["issueCounts"], {"asset_hash_unverified": 1})
        self.output.unlink()
        self.update_asset(hash_verified=1, cache_state="quarantined")
        self.assertEqual(self.export()["issueCounts"], {"asset_not_ready": 1})

    def test_missing_asset_row_reports_each_revision_reference(self):
        self.connection.execute("DELETE FROM assets WHERE id='a-1'")
        result = self.export()
        self.assertEqual(result["exit"], 2)
        self.assertEqual(result["issueCounts"], {"asset_row_missing_or_not_authorized": 2})
        self.assertEqual(result["counts"]["unresolvedAssetReferences"], 2)

    def test_subtask_only_registered_asset_preserved_without_invented_link(self):
        asset = self.add_asset("a-2", b"child", filename="child.txt", url="https://kompege.ru/files/child.txt")
        self.update_revision("subtask_manifest", encode([{"source_id": "child-123", "line_number": 19,
            "statement": "Child", "assets": [{"url": asset["canonical_url"], "filename": "child.txt", "expected_hash": asset["content_hash"]}]}]))
        result = self.export()
        self.assertEqual(result["exit"], 0)
        self.assertEqual(result["counts"]["assets"], 2)
        self.assertEqual(result["counts"]["assetBytesRows"], 2)
        self.assertEqual(result["counts"]["links"], 2)

    def test_ambiguous_subtask_asset_not_guessed(self):
        self.add_asset("a-2", b"child", filename="child.txt", url="https://kompege.ru/files/child.txt")
        self.add_asset("a-3", b"other", filename="child.txt", url="https://kompege.ru/files/child.txt")
        self.update_revision("subtask_manifest", encode([{"source_id": "child", "assets": [
            {"url": "https://kompege.ru/files/child.txt", "filename": "child.txt"}]}]))
        result = self.export()
        self.assertEqual(result["issueCounts"], {"manifest_asset_missing_or_ambiguous": 1})

    def test_two_missing_manifest_resources_counted_separately(self):
        self.update_revision("subtask_manifest", encode([{"source_id": "child", "assets": [
            {"url": "https://kompege.ru/files/a.txt", "filename": "a.txt"},
            {"url": "https://kompege.ru/files/b.txt", "filename": "b.txt"}]}]))
        self.assertEqual(self.export()["issueCounts"], {"manifest_asset_missing_or_ambiguous": 2})

    def test_original_manifest_pin_cannot_silently_change_bytes(self):
        self.update_revision("asset_manifest", encode([{"asset_id": "a-1", "size_bytes": 6, "content_hash": "b" * 64}]))
        result = self.export()
        self.assertFalse(result["complete"])
        self.assertEqual(result["issueCounts"], {"manifest_asset_revision_mismatch": 1})

    def test_known_upstream_unavailable_resource_blocks_completeness(self):
        self.update_revision("source_metadata", encode({"kege_unavailable_resources": [{"kind": "image"}]}))
        self.assertEqual(self.export()["issueCounts"], {"upstream_resource_unavailable": 1})

    def test_nested_internal_or_owner_json_key_fails_without_artifact(self):
        for field, content in (("source_metadata", {"nested": {"ownerID": SENTINEL}}),
                               ("asset_manifest", [{"storageKey": SENTINEL}])):
            with self.subTest(field=field):
                self.update_revision(field, encode(content))
                with self.assertRaisesRegex(exporter.SeedError, "catalog_metadata_disallowed_key"):
                    self.export()
                self.assertFalse(self.output.exists())
                self.no_temporary()
                if field == "source_metadata":
                    self.update_revision(field, "{}")

    def test_nullable_json_null_and_sql_null_preserve_the_same_logical_value(self):
        for stored in ("null", None):
            with self.subTest(storage=stored):
                self.update_revision("table_manifest", stored)
                self.assertTrue(self.export()["complete"])
                self.assertIsNone(self.records("catalog_revisions")["r-2"]["table_manifest"])
                self.output.unlink()
                self.no_temporary()

    def test_required_json_null_still_rejects_without_publishing(self):
        for field in ("source_metadata", "asset_manifest", "subtask_manifest"):
            with self.subTest(field=field):
                self.update_revision(field, "null")
                with self.assertRaisesRegex(exporter.SeedError, "catalog_json_shape_invalid"):
                    self.export()
                self.assertFalse(self.output.exists())
                self.no_temporary()
                self.update_revision(field, "{}" if field == "source_metadata" else "[]")

    def test_review_sensitive_key_variants_fail_closed_at_every_depth(self):
        keys = ("storage_path", "path", "database_path", "owner_email", "credential", "secret_key", "profile_id",
                "StoragePath", "DATABASE_PATH", "OWNER.EMAIL", "credential-cache", "SecretKey", "profileID",
                "prefix_owner_email_suffix", "private_storage_location", "customerCredential", "credentialBackup",
                "rootpath", "pathToFile", "cacheRoot", "assetRoot", "credential_value", "o.w.n.e.r_e.m.a.i.l",
                "s_t_o_r_a_g_e_p_a_t_h", "ＳＴＯＲＡＧＥ＿ＰＡＴＨ", "OwnerID", "auth.context", "accessTokenValue")
        for key in keys:
            with self.subTest(key=key):
                self.update_revision("source_metadata", encode({"evidence": [{"nested": {key: SENTINEL}}]}))
                try:
                    with self.assertRaisesRegex(exporter.SeedError, "catalog_metadata_disallowed_key"):
                        self.export()
                    self.assertFalse(self.output.exists())
                    self.no_temporary()
                finally:
                    # Own synthetic output only, so failed subcases cannot mask the next one.
                    self.output.unlink(missing_ok=True)

    def test_review_public_metadata_is_preserved_without_silent_redaction(self):
        metadata = {"source_author": "Synthetic author", "upstream_record_id": "123", "source_year": 2026,
                    "year_evidence_version": "v1", "difficulty_mapping_version": "v1", "kege_schema": "v2",
                    "kege_source_order": 12, "secondary_tags": ["games", "numbers"], "raw_statement_hash": "f" * 64,
                    "evidence": [{"title": "path, profile and secret are ordinary text values"}]}
        self.update_revision("source_metadata", encode(metadata))
        self.assertTrue(self.export()["complete"])
        self.assertEqual(self.records("catalog_revisions")["r-2"]["source_metadata"], metadata)

    def test_review_key_material_families_and_compact_variants_fail_closed(self):
        keys = ("oauth", "jwt", "encryption_key", "key_material", "key", "OAuth", "JWT",
                "ENCRYPTION_KEY", "KeyMaterial", "k.e.y", "O-Auth", "j.w.t", "encryptionkey",
                "keymaterial", "encryption.key", "privateKeyMaterial", "key_material_backup",
                "currentOAuth", "jwtValue", "ｅｎｃｒｙｐｔｉｏｎ＿ｋｅｙ", "ＫＥＹ")
        for key in keys:
            with self.subTest(key=key):
                self.update_revision("source_metadata", encode({"evidence": [{key: SENTINEL}]}))
                try:
                    with self.assertRaisesRegex(exporter.SeedError, "catalog_metadata_disallowed_key"):
                        self.export()
                    self.assertFalse(self.output.exists())
                    self.no_temporary()
                finally:
                    self.output.unlink(missing_ok=True)

    def test_review_keyboard_monkey_and_actual_kege_metadata_fields_are_preserved(self):
        metadata = {"keyboard": {"shortcuts": ["Ctrl+S"]}, "monkey": "public synthetic label",
                    "kege_record_id": "123", "kege_question_number": 17, "kege_source_order": 12,
                    "kege_schema": "kege-current-bank/2.0", "primary_kind": "number", "title": "Задача",
                    "secondary_tags": ["games", "numbers"], "kege_declared_difficulty": 2,
                    "kege_parent_record_id": "100", "kege_unavailable_resources": []}
        self.update_revision("source_metadata", encode(metadata))
        self.assertTrue(self.export()["complete"])
        self.assertEqual(self.records("catalog_revisions")["r-2"]["source_metadata"], metadata)

    def test_review_null_and_orphan_catalog_rows_fail_closed(self):
        mutations = (
            "UPDATE task_revisions SET rights_status=NULL WHERE id='r-blocked'",
            "UPDATE tasks SET source_name=NULL WHERE id='t-other'",
            "UPDATE task_revisions SET task_id='missing-task' WHERE id='r-blocked'",
            "UPDATE task_revisions SET task_id=NULL WHERE id='r-blocked'",
            "UPDATE task_assets SET revision_id='missing-revision' WHERE id='l-2'",
            "UPDATE catalog_entries SET revision_id='missing-revision' WHERE id='ce-r-blocked'",
            "UPDATE catalog_entries SET revision_id=NULL WHERE id='ce-r-blocked'",
        )
        baseline = "\n".join(self.connection.iterdump())
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.connection.execute(mutation)
                self.connection.commit()
                try:
                    with self.assertRaisesRegex(exporter.SeedError, "catalog_source_rows_invalid"):
                        self.export()
                    self.assertFalse(self.output.exists())
                    self.no_temporary()
                finally:
                    self.output.unlink(missing_ok=True)
                    self.connection.close()
                    self.database.unlink()
                    self.connection = sqlite3.connect(self.database)
                    self.addCleanup(self.connection.close)
                    self.connection.executescript(baseline)

    def test_review_resolve_runtime_error_is_safe_at_each_path_boundary(self):
        original = Path.resolve
        for target, expected in ((self.database, "catalog_source_read_failed"),
                                 (self.cache, "catalog_export_failed"),
                                 (self.output.parent, "catalog_export_failed")):
            with self.subTest(target=target.name):
                def resolving(path, *args, **kwargs):
                    if path == target:
                        raise RuntimeError(SENTINEL + " " + str(target))
                    return original(path, *args, **kwargs)
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(Path, "resolve", resolving), redirect_stdout(stdout), redirect_stderr(stderr):
                    status = exporter.main(["--database", str(self.database), "--asset-root", str(self.cache),
                                            "--output", str(self.output)])
                self.assertEqual(status, 1)
                receipt = json.loads(stdout.getvalue())
                self.assertEqual(receipt["error"], expected)
                self.assertFalse(receipt["complete"])
                self.assertEqual(stderr.getvalue(), "")
                self.assertNotIn(SENTINEL, stdout.getvalue())
                self.assertNotIn(str(self.root), stdout.getvalue())
                self.assertFalse(self.output.exists())

    def test_review_main_suppresses_unexpected_runtime_exception_diagnostics(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(exporter, "export_catalog", side_effect=RuntimeError(SENTINEL + str(self.root))), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            status = exporter.main(["--database", str(self.database), "--asset-root", str(self.cache),
                                    "--output", str(self.output)])
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(stdout.getvalue())["error"], "catalog_export_failed")
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn(SENTINEL, stdout.getvalue())
        self.assertNotIn(str(self.root), stdout.getvalue())

    def test_source_authorizer_rejects_owner_column_table_writes_and_extensions(self):
        with exporter.source_snapshot(self.database) as source:
            for statement in ("SELECT secret FROM users", "SELECT owner_secret FROM tasks",
                "SELECT job_id FROM catalog_entries", "UPDATE tasks SET external_id='x'",
                "PRAGMA query_only=OFF", "ATTACH ':memory:' AS other", "SELECT load_extension('untrusted')"):
                with self.subTest(sql=statement), self.assertRaises(sqlite3.DatabaseError):
                    source.connection.execute(statement).fetchall()

    def test_source_view_or_trigger_rejected(self):
        self.connection.execute("CREATE TRIGGER bad AFTER UPDATE ON tasks BEGIN SELECT 1; END")
        self.connection.commit()
        with self.assertRaisesRegex(exporter.SeedError, "catalog_schema_objects_invalid"):
            self.export()
        self.assertFalse(self.output.exists())
        self.connection.execute("DROP TRIGGER bad")
        self.connection.execute("ALTER TABLE tasks RENAME TO secret_tasks")
        self.connection.execute("CREATE VIEW tasks AS SELECT * FROM secret_tasks")
        self.connection.commit()
        with self.assertRaisesRegex(exporter.SeedError, "catalog_schema_objects_invalid"):
            self.export()

    def test_live_wal_committed_data_visible_without_immutable(self):
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("UPDATE task_revisions SET answer='314' WHERE id='r-2'")
        self.connection.commit()
        self.assertTrue(Path(str(self.database) + "-wal").exists())
        self.export()
        self.assertEqual(self.records("catalog_revisions")["r-2"]["answer"], "314")

    def test_read_transaction_keeps_snapshot_during_concurrent_wal_commit(self):
        self.connection.execute("PRAGMA journal_mode=WAL")
        with exporter.source_snapshot(self.database) as source:
            first = source.connection.execute("SELECT current_revision_id FROM tasks WHERE id='t-1'").fetchone()[0]
            self.connection.execute("UPDATE tasks SET current_revision_id='r-1' WHERE id='t-1'")
            self.connection.commit()
            second = source.connection.execute("SELECT current_revision_id FROM tasks WHERE id='t-1'").fetchone()[0]
        self.assertEqual((first, second), ("r-2", "r-2"))

    def test_unexported_current_pointer_cleared_without_denied_row(self):
        self.connection.execute("UPDATE tasks SET current_revision_id='r-blocked' WHERE id='t-1'")
        result = self.export()
        self.assertEqual(result["counts"]["suppressedCurrentPointers"], 1)
        self.assertIsNone(self.records("catalog_tasks")["t-1"]["current_revision_id"])
        self.assertNotIn("r-blocked", self.records("catalog_revisions"))

    def test_output_existing_never_overwritten(self):
        self.output.write_bytes(b"existing artifact")
        with self.assertRaisesRegex(exporter.SeedError, "catalog_output_exists"):
            self.export()
        self.assertEqual(self.output.read_bytes(), b"existing artifact")
        self.no_temporary()

    def test_fatal_asset_budget_or_deadline_publishes_nothing(self):
        for name, value, error in (("MAX_TOTAL_ASSET_BYTES", 5, "catalog_asset_budget_exceeded"),
                                  ("MAX_EXPORT_SECONDS", -1, "catalog_deadline_exceeded")):
            with self.subTest(name=name), patch.object(exporter, name, value):
                with self.assertRaisesRegex(exporter.SeedError, error):
                    self.export()
                self.assertFalse(self.output.exists())
                self.no_temporary()

    def test_empty_catalog_is_incomplete(self):
        self.connection.execute("UPDATE task_revisions SET rights_status='blocked'")
        result = self.export()
        self.assertEqual(result["exit"], 2)
        self.assertEqual(result["counts"]["tasks"], 0)
        self.assertEqual(result["issueCounts"], {"catalog_empty": 1})

    def test_output_inside_cache_rejected(self):
        with self.assertRaisesRegex(exporter.SeedError, "catalog_output_inside_cache"):
            exporter.export_catalog(self.database, self.cache, self.cache / "seed.sqlite3")

    def test_cli_safe_receipt_no_paths_or_raw_exception(self):
        process = subprocess.run([sys.executable, "-B", str(OWNED / "export_catalog.py"), "--database", str(self.database),
            "--asset-root", str(self.cache), "--output", str(self.output)], capture_output=True, text=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue(json.loads(process.stdout)["complete"])
        self.assertEqual(process.stderr, "")
        self.assertNotIn(str(self.root), process.stdout)
        self.assertNotIn(SENTINEL, process.stdout)
        process = subprocess.run([sys.executable, "-B", str(OWNED / "export_catalog.py"), "--database", str(self.database),
            "--asset-root", str(self.cache), "--output", str(self.output)], capture_output=True, text=True, timeout=15)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(json.loads(process.stdout)["error"], "catalog_output_exists")
        self.assertEqual(process.stderr, "")

    def test_source_sql_looking_statement_stays_inert_data(self):
        statement = "SELECT load_extension('evil'); DROP TABLE users; # not executable"
        self.update_revision("statement", statement)
        self.assertTrue(self.export()["complete"])
        self.assertEqual(self.records("catalog_revisions")["r-2"]["statement"], statement)
        self.assertEqual(self.connection.execute("SELECT secret FROM users").fetchone()[0], SENTINEL)

    def test_cache_symlink_is_not_followed(self):
        path = self.cache / self.asset["storage_key"]
        path.unlink()
        outside = self.root / "outside.txt"
        outside.write_bytes(b"1 2 3\n")
        try:
            path.symlink_to(outside)
        except OSError:
            self.skipTest("Windows symlink privilege unavailable")
        self.assertEqual(self.export()["issueCounts"], {"asset_path_invalid": 1})


if __name__ == "__main__":
    unittest.main()

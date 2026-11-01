"""Synthetic-only checks for the derived seed builder.

The tests create a tiny v1 container locally.  They never discover, open, or
print the reviewed real seed and never contact an upstream service.
"""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import tracemalloc
import unittest
import warnings
from unittest.mock import patch

import finalize_catalog as finalizer


OWNED = Path(__file__).resolve().parent
STAMP = "2026-10-30T00:00:00+00:00"
V1_DDL = """
CREATE TABLE catalog_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE catalog_tasks (id TEXT PRIMARY KEY, record_json TEXT NOT NULL);
CREATE TABLE catalog_revisions (id TEXT PRIMARY KEY, record_json TEXT NOT NULL);
CREATE TABLE catalog_assets (id TEXT PRIMARY KEY, record_json TEXT NOT NULL);
CREATE TABLE catalog_links (id TEXT PRIMARY KEY, record_json TEXT NOT NULL);
CREATE TABLE catalog_selection (revision_id TEXT PRIMARY KEY);
CREATE TABLE catalog_asset_bytes (asset_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, bytes BLOB NOT NULL);
"""


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(value).hexdigest()


class FinalizerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="seed-finalizer-", dir=OWNED)
        self.root = Path(self.directory.name)
        self.input = self.root / "catalog-v1.sqlite3"
        self.snapshot_root = self.root / "snapshot"
        self.snapshot_root.mkdir()
        self.snapshot_manifest = self.root / "snapshot.json"
        self.output = self.root / "catalog-v2.sqlite3"
        self.cached = b"already cached\n"
        self.promoted = b"captured by bounded snapshot\n"
        self.cache_id = "asset-cached"
        self.promoted_id = "asset-promoted"
        self.warning_revision = "revision-warning"
        self._create_v1()
        self.source_hash = digest(self.input.read_bytes())
        self._create_snapshot()
        self.input_before = self.input.read_bytes()

    def tearDown(self):
        self.assertFalse(list(self.root.glob(".catalog-seed-v2-*")))
        self.assertFalse(list(self.root.glob(".catalog-assets-*")))
        self.directory.cleanup()

    def _create_v1(self):
        cached_hash = digest(self.cached)
        promoted_hash = digest(self.promoted)
        tasks = {
            "task-1": {"id": "task-1", "source_name": "kompege", "external_id": "synthetic-1",
                       "line_number": 1, "current_revision_id": self.warning_revision,
                       "created_at": STAMP, "updated_at": STAMP},
        }
        revisions = {
            self.warning_revision: {
                "id": self.warning_revision, "task_id": "task-1", "revision_number": 1,
                "source_metadata": {"kege_unavailable_resources": [{"kind": "image"}]},
                "rights_status": "private_authorized", "content_hash": "a" * 64,
                "asset_manifest": [], "subtask_manifest": [], "table_manifest": None,
            },
        }
        assets = {
            self.cache_id: {
                "id": self.cache_id, "source_name": "kompege", "canonical_url": "https://kompege.test/cached",
                "filename": "cached.txt", "media_type": "text/plain", "detected_type": "text/plain",
                "size_bytes": len(self.cached), "content_hash": cached_hash, "expected_content_hash": None,
                "cache_state": "ready", "rights_status": "private_authorized", "hash_verified": True,
                "fetched_at": STAMP, "created_at": STAMP,
            },
            self.promoted_id: {
                "id": self.promoted_id, "source_name": "kompege", "canonical_url": "https://kompege.test/promoted",
                "filename": "promoted.bin", "media_type": "application/octet-stream", "detected_type": None,
                "size_bytes": 0, "content_hash": promoted_hash, "expected_content_hash": promoted_hash,
                "cache_state": "metadata_only", "rights_status": "private_authorized", "hash_verified": False,
                "fetched_at": None, "created_at": STAMP,
            },
        }
        links = {
            "link-cached": {"id": "link-cached", "revision_id": self.warning_revision,
                            "asset_id": self.cache_id, "runner_filename": "cached.txt", "position": 0},
            "link-promoted": {"id": "link-promoted", "revision_id": self.warning_revision,
                              "asset_id": self.promoted_id, "runner_filename": "promoted.bin", "position": 1},
        }
        selection = [self.warning_revision]
        connection = sqlite3.connect(self.input)
        connection.executescript(V1_DDL)
        connection.execute("PRAGMA application_id=0x4B435331")
        connection.execute("PRAGMA user_version=1")
        for table, records in (("catalog_tasks", tasks), ("catalog_revisions", revisions),
                               ("catalog_assets", assets), ("catalog_links", links)):
            for identifier, record in sorted(records.items()):
                connection.execute(f"INSERT INTO {table}(id,record_json) VALUES (?,?)", (identifier, encode(record)))
        connection.executemany("INSERT INTO catalog_selection(revision_id) VALUES (?)", [(item,) for item in selection])
        connection.execute("INSERT INTO catalog_asset_bytes(asset_id,sha256,bytes) VALUES (?,?,?)",
                           (self.cache_id, cached_hash, self.cached))

        collections = {}
        for table, records in (("catalog_tasks", tasks), ("catalog_revisions", revisions),
                               ("catalog_assets", assets), ("catalog_links", links)):
            collections[table] = digest(b"".join(
                (encode([identifier, record]) + "\n").encode("utf-8")
                for identifier, record in sorted(records.items())
            ))
        collections["catalog_selection"] = digest((encode([selection[0]]) + "\n").encode("utf-8"))
        collections["catalog_asset_bytes"] = digest(
            (encode([self.cache_id, cached_hash, len(self.cached)]) + "\n").encode("utf-8")
        )
        metadata = {
            "format": finalizer.FORMAT_V1, "schemaVersion": 1, "sourceSchema": 12,
            "producerSha256": "b" * 64, "createdAt": STAMP,
            "scope": {"sourceName": "kompege", "rights": ["private_authorized", "public_authorized"]},
            "limits": {"assetBytes": finalizer.MAX_ASSET_BYTES, "totalAssetBytes": finalizer.MAX_TOTAL_ASSET_BYTES},
            "counts": {"tasks": 1, "revisions": 1, "assets": 2, "links": 2, "selection": 1,
                       "assetBytesRows": 1, "assetBytesTotal": len(self.cached), "unavailableAssets": 1,
                       "unresolvedAssetReferences": 0, "suppressedCurrentPointers": 0,
                       "deniedRevisions": 0, "otherSourceRevisions": 0},
            "complete": False,
            # Deliberately omit the warning example to model v1's capped sample.
            "issues": [{"code": "asset_not_ready", "assetId": self.promoted_id}],
            "issueCounts": {"asset_not_ready": 1, "upstream_resource_unavailable": 1},
            "collectionsSha256": collections,
        }
        metadata["manifestSha256"] = digest(encode(metadata).encode("utf-8"))
        for key, value in sorted(metadata.items()):
            connection.execute("INSERT INTO catalog_metadata(key,value) VALUES (?,?)", (key, encode(value)))
        connection.commit()
        connection.close()

    def _create_snapshot(self):
        relative = "bytes/promoted.bin"
        target = self.snapshot_root / "bytes"
        target.mkdir()
        (target / "promoted.bin").write_bytes(self.promoted)
        manifest = {
            "format": finalizer.SNAPSHOT_FORMAT,
            "sourceSeedSha256": self.source_hash,
            "capturedAt": STAMP,
            "assets": [{
                "assetId": self.promoted_id, "relativePath": relative,
                "expectedSha256": digest(self.promoted), "sizeBytes": len(self.promoted),
                "detectedType": "application/octet-stream", "fetchedAt": STAMP,
            }],
            "counts": {"assets": 1, "bytes": len(self.promoted)},
        }
        manifest["manifestSha256"] = digest(encode(manifest).encode("utf-8"))
        self.snapshot_manifest.write_text(encode(manifest), encoding="utf-8")

    def _derive(self):
        return finalizer.derive_catalog(
            self.input, self.snapshot_manifest, self.snapshot_root, self.output,
            expected_source_sha256=None,
        )

    def _add_large_missing_snapshot(self, *, count=4, size=2 * 1024 * 1024):
        large_entries = []
        with closing(sqlite3.connect(self.input)) as connection:
            for index in range(count):
                asset_id = f"asset-large-{index}"
                data = bytes([65 + index]) * size
                checksum = digest(data)
                record = {
                    "id": asset_id, "source_name": "kompege",
                    "canonical_url": f"https://kompege.test/{asset_id}",
                    "filename": f"{asset_id}.bin", "media_type": "application/octet-stream",
                    "detected_type": None, "size_bytes": 0, "content_hash": checksum,
                    "expected_content_hash": checksum, "cache_state": "metadata_only",
                    "rights_status": "private_authorized", "hash_verified": False,
                    "fetched_at": None, "created_at": STAMP,
                }
                connection.execute(
                    "INSERT INTO catalog_assets(id,record_json) VALUES (?,?)",
                    (asset_id, encode(record)),
                )
                relative = f"large/{asset_id}.bin"
                target = self.snapshot_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                large_entries.append({
                    "assetId": asset_id, "relativePath": relative,
                    "expectedSha256": checksum, "sizeBytes": size,
                    "detectedType": "application/octet-stream", "fetchedAt": STAMP,
                })
            assets = {
                asset_id: json.loads(raw)
                for asset_id, raw in connection.execute(
                    "SELECT id,record_json FROM catalog_assets ORDER BY id"
                )
            }
            metadata = self._metadata(self.input)
            metadata["counts"]["assets"] = len(assets)
            metadata["counts"]["unavailableAssets"] = len(assets) - 1
            metadata["issueCounts"]["asset_not_ready"] = len(assets) - 1
            metadata["collectionsSha256"]["catalog_assets"] = digest(b"".join(
                (encode([asset_id, row]) + "\n").encode("utf-8")
                for asset_id, row in sorted(assets.items())
            ))
            metadata["manifestSha256"] = digest(
                encode({key: value for key, value in metadata.items() if key != "manifestSha256"}).encode("utf-8")
            )
            for key, value in metadata.items():
                connection.execute(
                    "UPDATE catalog_metadata SET value=? WHERE key=?",
                    (encode(value), key),
                )
            connection.commit()
        self.source_hash = digest(self.input.read_bytes())
        promoted_entry = {
            "assetId": self.promoted_id, "relativePath": "bytes/promoted.bin",
            "expectedSha256": digest(self.promoted), "sizeBytes": len(self.promoted),
            "detectedType": "application/octet-stream", "fetchedAt": STAMP,
        }
        entries = [promoted_entry, *large_entries]
        manifest = {
            "format": finalizer.SNAPSHOT_FORMAT, "sourceSeedSha256": self.source_hash,
            "capturedAt": STAMP, "assets": entries,
            "counts": {"assets": len(entries), "bytes": sum(item["sizeBytes"] for item in entries)},
        }
        manifest["manifestSha256"] = digest(encode(manifest).encode("utf-8"))
        self.snapshot_manifest.write_text(encode(manifest), encoding="utf-8")

    def _make_sentinel_source(self):
        """Turn the metadata-only fixture asset into a proven legacy sentinel."""
        with closing(sqlite3.connect(self.input)) as connection:
            rows = dict(connection.execute("SELECT id,record_json FROM catalog_assets"))
            record = json.loads(rows[self.promoted_id])
            sentinel = digest(("metadata:" + record["canonical_url"] + ":" + record["filename"]).encode("utf-8"))
            record.update({
                "content_hash": sentinel, "expected_content_hash": sentinel, "size_bytes": 0,
                "detected_type": None, "cache_state": "metadata_only", "hash_verified": False,
                "fetched_at": None,
            })
            connection.execute("UPDATE catalog_assets SET record_json=? WHERE id=?", (encode(record), self.promoted_id))
            assets = {
                asset_id: json.loads(raw)
                for asset_id, raw in connection.execute("SELECT id,record_json FROM catalog_assets ORDER BY id")
            }
            assets_digest = digest(b"".join(
                (encode([asset_id, row]) + "\n").encode("utf-8")
                for asset_id, row in sorted(assets.items())
            ))
            metadata = self._metadata(self.input)
            metadata["collectionsSha256"]["catalog_assets"] = assets_digest
            metadata["manifestSha256"] = digest(encode({k: v for k, v in metadata.items() if k != "manifestSha256"}).encode("utf-8"))
            for key, value in metadata.items():
                connection.execute("UPDATE catalog_metadata SET value=? WHERE key=?", (encode(value), key))
            connection.commit()
        self.source_hash = digest(self.input.read_bytes())
        return sentinel

    def _write_worker_snapshot(self, sentinel: str, *, partial: bool = False, genuine_mismatch: bool = False):
        assets_root = self.snapshot_root / "assets"
        assets_root.mkdir(exist_ok=True)
        payloads = [(self.cache_id, self.cached, "text/plain"), (self.promoted_id, self.promoted, "application/octet-stream")]
        entries = []
        for asset_id, data, detected in payloads:
            actual = digest(data)
            target = assets_root / actual[:2]
            target.mkdir(parents=True, exist_ok=True)
            (target / actual).write_bytes(data)
            expected = None
            if asset_id != self.promoted_id:
                expected = None
            entries.append({
                "url": "https://kompege.test/" + asset_id,
                "assetIds": [asset_id], "filenames": [asset_id + ".bin"], "mediaTypes": [detected],
                "expectedContentHash": expected, "status": "downloaded",
                "path": f"assets/{actual[:2]}/{actual}", "sha256": actual, "sizeBytes": len(data),
                "detectedType": detected, "source": "network",
            })
        normalization_fingerprint = {
            "canonicalUrl": "https://kompege.test/promoted",
            "filename": "promoted.bin",
            "cacheState": "metadata_only",
            "sizeBytes": 0,
            "contentHash": sentinel if not genuine_mismatch else "f" * 64,
            "expectedContentHash": sentinel,
            "hashVerified": False,
            "detectedType": None,
        }
        normalization = {
            "entries": [{
                "assetId": self.promoted_id,
                "canonicalUrl": "https://kompege.test/promoted",
                "filename": "promoted.bin",
                "originalExpectedContentHash": sentinel,
                "normalizedExpectedContentHash": None,
                "fingerprint": normalization_fingerprint,
            }],
        }
        if partial:
            del normalization["entries"][0]["fingerprint"]["hashVerified"]
        receipt = {
            "format": finalizer.SNAPSHOT_FORMAT, "seedSha256": self.source_hash, "createdAt": STAMP,
            "legacyHashSentinelNormalization": normalization,
        }
        self.snapshot_manifest.write_text(encode({"format": finalizer.SNAPSHOT_FORMAT, "entries": entries}) + "\n", encoding="utf-8")
        (self.snapshot_manifest.parent / "receipt.json").write_text(encode(receipt) + "\n", encoding="utf-8")

    @staticmethod
    def _metadata(path):
        with closing(sqlite3.connect(path)) as connection:
            return {key: json.loads(value) for key, value in connection.execute("SELECT key,value FROM catalog_metadata")}

    @staticmethod
    def _rows(path, table):
        with closing(sqlite3.connect(path)) as connection:
            if table == "catalog_selection":
                return list(connection.execute("SELECT revision_id FROM catalog_selection ORDER BY revision_id"))
            if table == "catalog_asset_bytes":
                return list(connection.execute("SELECT asset_id,sha256,bytes FROM catalog_asset_bytes ORDER BY asset_id"))
            return list(connection.execute(f"SELECT id,record_json FROM {table} ORDER BY id"))

    def test_promotes_all_bytes_preserves_rows_and_reconstructs_source_warning(self):
        before = {table: self._rows(self.input, table) for table in
                  ("catalog_tasks", "catalog_revisions", "catalog_links", "catalog_selection")}
        original_metadata = self._metadata(self.input)
        receipt = self._derive()
        self.assertEqual(receipt["format"], finalizer.FORMAT_V2)
        self.assertEqual(receipt["exit"], 2)
        self.assertFalse(receipt["complete"])
        self.assertTrue(receipt["offlineReady"])
        metadata = self._metadata(self.output)
        self.assertEqual(metadata["format"], finalizer.FORMAT_V2)
        self.assertEqual(metadata["schemaVersion"], 2)
        self.assertEqual(metadata["sourceSchema"], 12)
        self.assertTrue(metadata["offlineReady"])
        self.assertEqual(metadata["sourceSeedSha256"], self.source_hash)
        self.assertFalse(metadata["complete"])
        self.assertEqual(metadata["issueCounts"], {"upstream_resource_unavailable": 1})
        self.assertEqual(metadata["issues"], [{"code": "upstream_resource_unavailable", "revisionId": self.warning_revision}])
        self.assertEqual(metadata["counts"]["assetBytesRows"], 2)
        self.assertEqual(metadata["counts"]["unavailableAssets"], 0)
        for table, rows in before.items():
            self.assertEqual(self._rows(self.output, table), rows)
        assets = {key: json.loads(raw) for key, raw in self._rows(self.output, "catalog_assets")}
        self.assertEqual(assets[self.cache_id]["content_hash"], digest(self.cached))
        self.assertEqual(assets[self.promoted_id]["content_hash"], digest(self.promoted))
        self.assertEqual(assets[self.promoted_id]["expected_content_hash"], digest(self.promoted))
        self.assertEqual(assets[self.promoted_id]["detected_type"], "application/octet-stream")
        self.assertEqual(assets[self.promoted_id]["size_bytes"], len(self.promoted))
        self.assertEqual(assets[self.promoted_id]["cache_state"], "ready")
        self.assertTrue(assets[self.promoted_id]["hash_verified"])
        bytes_rows = self._rows(self.output, "catalog_asset_bytes")
        self.assertEqual({asset_id for asset_id, _, _ in bytes_rows}, {self.cache_id, self.promoted_id})
        for asset_id, expected, data in bytes_rows:
            self.assertEqual(digest(data), expected)
            self.assertEqual(data, self.cached if asset_id == self.cache_id else self.promoted)
        # Four unchanged collections retain their v1 hashes; assets and bytes are rewritten.
        for table in ("catalog_tasks", "catalog_revisions", "catalog_links", "catalog_selection"):
            self.assertEqual(metadata["collectionsSha256"][table], original_metadata["collectionsSha256"][table])
        self.assertNotEqual(metadata["collectionsSha256"]["catalog_asset_bytes"], original_metadata["collectionsSha256"]["catalog_asset_bytes"])
        self.assertEqual(metadata["manifestSha256"], digest(encode({k: v for k, v in metadata.items() if k != "manifestSha256"}).encode()))
        self.assertEqual(digest(self.output.read_bytes()), receipt["fileSha256"])
        self.assertEqual(self.input.read_bytes(), self.input_before)

    def test_output_is_no_overwrite(self):
        first = self._derive()
        before = self.output.read_bytes()
        with self.assertRaisesRegex(finalizer.FinalizerError, "catalog_output_exists"):
            self._derive()
        self.assertEqual(self.output.read_bytes(), before)
        self.assertEqual(first["fileSha256"], digest(before))

    def test_accepts_snapshot_worker_grouped_manifest_and_receipt(self):
        cached_digest = digest(self.cached)
        promoted_digest = digest(self.promoted)
        cached_path = self.snapshot_root / "assets" / cached_digest[:2]
        promoted_path = self.snapshot_root / "assets" / promoted_digest[:2]
        cached_path.mkdir(parents=True)
        promoted_path.mkdir(parents=True, exist_ok=True)
        (cached_path / cached_digest).write_bytes(self.cached)
        (promoted_path / promoted_digest).write_bytes(self.promoted)
        worker_manifest = {
            "format": finalizer.SNAPSHOT_FORMAT,
            "entries": [
                {"url": "https://kompege.test/cached", "assetIds": [self.cache_id], "filenames": ["cached.txt"],
                 "mediaTypes": ["text/plain"], "expectedContentHash": None, "status": "seed_ready",
                 "path": f"assets/{cached_digest[:2]}/{cached_digest}", "sha256": cached_digest,
                 "sizeBytes": len(self.cached), "detectedType": "text/plain", "source": "seed"},
                {"url": "https://kompege.test/promoted", "assetIds": [self.promoted_id], "filenames": ["promoted.bin"],
                 "mediaTypes": ["application/octet-stream"], "expectedContentHash": promoted_digest, "status": "downloaded",
                 "path": f"assets/{promoted_digest[:2]}/{promoted_digest}", "sha256": promoted_digest,
                 "sizeBytes": len(self.promoted), "detectedType": "application/octet-stream", "source": "network"},
            ],
        }
        receipt = {"format": finalizer.SNAPSHOT_FORMAT, "seedSha256": self.source_hash, "createdAt": STAMP}
        self.snapshot_manifest.write_text(encode(worker_manifest) + "\n", encoding="utf-8")
        (self.snapshot_manifest.parent / "receipt.json").write_text(encode(receipt) + "\n", encoding="utf-8")
        result = self._derive()
        self.assertEqual(result["issueCounts"], {"upstream_resource_unavailable": 1})
        self.assertTrue(self.output.exists())

    def test_snapshot_bytes_are_staged_before_source_path_changes(self):
        reader = finalizer._read_snapshot_file

        def mutate_source_after_read(*args, **kwargs):
            result = reader(*args, **kwargs)
            (self.snapshot_root / "bytes" / "promoted.bin").write_bytes(b"changed after validation")
            return result

        with patch.object(finalizer, "_read_snapshot_file", side_effect=mutate_source_after_read):
            receipt = self._derive()

        self.assertEqual(receipt["exit"], 2)
        bytes_rows = self._rows(self.output, "catalog_asset_bytes")
        promoted = {asset_id: data for asset_id, _sha256, data in bytes_rows}[self.promoted_id]
        self.assertEqual(promoted, self.promoted)

    def test_large_snapshot_assets_are_not_materialized_as_a_body_pool(self):
        self._add_large_missing_snapshot()
        total_snapshot_bytes = 4 * 2 * 1024 * 1024
        tracemalloc.start()
        try:
            receipt = self._derive()
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertEqual(receipt["counts"]["assetBytesRows"], 6)
        self.assertLess(peak, total_snapshot_bytes // 2)
        with closing(sqlite3.connect(self.output)) as connection:
            rows = list(connection.execute(
                "SELECT asset_id,length(bytes) FROM catalog_asset_bytes ORDER BY asset_id"
            ))
        self.assertEqual(len(rows), 6)
        self.assertEqual({asset_id for asset_id, _size in rows}, {
            self.cache_id, self.promoted_id,
            "asset-large-0", "asset-large-1", "asset-large-2", "asset-large-3",
        })
        self.assertEqual(
            {size for asset_id, size in rows if asset_id.startswith("asset-large-")},
            {2 * 1024 * 1024},
        )

    def test_sqlite_write_failure_closes_streams_and_staging(self):
        original_copy = finalizer._copy_sqlite_snapshot
        opened = []

        def copy_with_write_failure(input_path, temporary, expected_hash):
            original_copy(input_path, temporary, expected_hash)
            with closing(sqlite3.connect(temporary)) as connection:
                connection.executescript(
                    """
                    CREATE TRIGGER injected_snapshot_write_failure
                    BEFORE INSERT ON catalog_asset_bytes
                    BEGIN SELECT RAISE(ABORT, 'injected snapshot write failure'); END;
                    """
                )
                connection.commit()

        real_fdopen = finalizer.os.fdopen

        def track_fdopen(*args, **kwargs):
            handle = real_fdopen(*args, **kwargs)
            opened.append(handle)
            return handle

        with patch.object(finalizer, "_copy_sqlite_snapshot", side_effect=copy_with_write_failure):
            with patch.object(finalizer.os, "fdopen", side_effect=track_fdopen):
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always", ResourceWarning)
                    with self.assertRaisesRegex(finalizer.FinalizerError, "catalog_publish_failed"):
                        self._derive()

        self.assertTrue(opened)
        self.assertTrue(all(handle.closed for handle in opened))
        self.assertFalse([item for item in caught if issubclass(item.category, ResourceWarning)])
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob(".catalog-seed-v2-*")))
        self.assertFalse(list(self.root.glob(".catalog-assets-*")))

    def test_snapshot_staging_open_failure_closes_source_stream(self):
        staging_root = self.root / "staging"
        staging_root.mkdir()
        opened = []
        calls = 0
        real_fdopen = finalizer.os.fdopen

        def fail_staging_fdopen(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected staging open failure")
            handle = real_fdopen(*args, **kwargs)
            opened.append(handle)
            return handle

        entry = {
            "relativePath": "bytes/promoted.bin",
            "expectedSha256": digest(self.promoted),
            "sizeBytes": len(self.promoted),
        }
        with patch.object(finalizer.os, "fdopen", side_effect=fail_staging_fdopen):
            with self.assertRaisesRegex(finalizer.FinalizerError, "snapshot_file_invalid"):
                finalizer._read_snapshot_file(self.snapshot_root, entry, staging_root=staging_root)

        self.assertEqual(calls, 2)
        self.assertTrue(opened)
        self.assertTrue(all(handle.closed for handle in opened))
        self.assertFalse(list(staging_root.glob(".snapshot-asset-*")))

    def test_expected_hash_mismatch_fails_without_copy(self):
        manifest = json.loads(self.snapshot_manifest.read_text(encoding="utf-8"))
        manifest["assets"][0]["expectedSha256"] = "f" * 64
        manifest.pop("manifestSha256", None)
        self.snapshot_manifest.write_text(encode(manifest), encoding="utf-8")
        with self.assertRaisesRegex(finalizer.FinalizerError, "snapshot_expected_hash_mismatch"):
            self._derive()
        self.assertFalse(self.output.exists())

    def test_missing_snapshot_file_fails_without_copy(self):
        (self.snapshot_root / "bytes" / "promoted.bin").unlink()
        with self.assertRaisesRegex(finalizer.FinalizerError, "snapshot_file_invalid"):
            self._derive()
        self.assertFalse(self.output.exists())

    def test_unknown_snapshot_asset_fails_without_copy(self):
        manifest = json.loads(self.snapshot_manifest.read_text(encoding="utf-8"))
        manifest["assets"][0]["assetId"] = "not-in-seed"
        manifest.pop("manifestSha256", None)
        self.snapshot_manifest.write_text(encode(manifest), encoding="utf-8")
        with self.assertRaisesRegex(finalizer.FinalizerError, "snapshot_asset_set_invalid"):
            self._derive()
        self.assertFalse(self.output.exists())

    def test_proven_metadata_hash_sentinel_clears_expected_only(self):
        sentinel = self._make_sentinel_source()
        self._write_worker_snapshot(sentinel)
        result = self._derive()
        self.assertEqual(result["exit"], 2)
        metadata = self._metadata(self.output)
        self.assertNotIn("legacyHashSentinelNormalization", metadata)
        self.assertNotIn("normalization", metadata)
        assets = {key: json.loads(raw) for key, raw in self._rows(self.output, "catalog_assets")}
        promoted = assets[self.promoted_id]
        self.assertIsNone(promoted["expected_content_hash"])
        self.assertEqual(promoted["content_hash"], digest(self.promoted))
        self.assertEqual(promoted["size_bytes"], len(self.promoted))
        self.assertEqual(promoted["detected_type"], "application/octet-stream")
        self.assertEqual(promoted["cache_state"], "ready")
        self.assertTrue(promoted["hash_verified"])

    def test_partial_metadata_hash_sentinel_fingerprint_fails_without_copy(self):
        sentinel = self._make_sentinel_source()
        self._write_worker_snapshot(sentinel, partial=True)
        with self.assertRaisesRegex(finalizer.FinalizerError, "snapshot_normalization_invalid"):
            self._derive()
        self.assertFalse(self.output.exists())

    def test_genuine_hash_mismatch_cannot_use_sentinel_normalization(self):
        sentinel = self._make_sentinel_source()
        self._write_worker_snapshot(sentinel, genuine_mismatch=True)
        with self.assertRaisesRegex(finalizer.FinalizerError, "snapshot_normalization_invalid"):
            self._derive()
        self.assertFalse(self.output.exists())

    def test_original_collection_hash_mismatch_fails_without_copy(self):
        with closing(sqlite3.connect(self.input)) as connection:
            connection.execute("UPDATE catalog_tasks SET record_json=? WHERE id=?", (encode({"id": "task-1", "source_name": "kompege", "changed": True}), "task-1"))
            connection.commit()
        with self.assertRaisesRegex(finalizer.FinalizerError, "catalog_collection_hash_invalid"):
            self._derive()
        self.assertFalse(self.output.exists())

    def test_unknown_original_issue_fails_without_copy(self):
        metadata = self._metadata(self.input)
        metadata["issueCounts"]["unknown_source_issue"] = 1
        metadata["manifestSha256"] = digest(encode({k: v for k, v in metadata.items() if k != "manifestSha256"}).encode())
        with closing(sqlite3.connect(self.input)) as connection:
            for key, value in metadata.items():
                connection.execute("UPDATE catalog_metadata SET value=? WHERE key=?", (encode(value), key))
            connection.commit()
        with self.assertRaisesRegex(finalizer.FinalizerError, "catalog_issue_unknown"):
            self._derive()
        self.assertFalse(self.output.exists())

    def test_rights_change_fails_without_copy(self):
        assets = {key: json.loads(raw) for key, raw in self._rows(self.input, "catalog_assets")}
        assets[self.promoted_id]["rights_status"] = "denied"
        with closing(sqlite3.connect(self.input)) as connection:
            connection.execute("UPDATE catalog_assets SET record_json=? WHERE id=?", (encode(assets[self.promoted_id]), self.promoted_id))
            connection.commit()
        with self.assertRaisesRegex(finalizer.FinalizerError, "catalog_rights_invalid"):
            self._derive()
        self.assertFalse(self.output.exists())

    def _rewrite_sentinel_source_record(self, *, changes=None, remove=()):
        """Keep collection proofs valid so strict fingerprint validation is exercised."""
        with closing(sqlite3.connect(self.input)) as connection:
            record = json.loads(connection.execute(
                "SELECT record_json FROM catalog_assets WHERE id=?", (self.promoted_id,),
            ).fetchone()[0])
            record.update(changes or {})
            for key in remove:
                record.pop(key)
            connection.execute(
                "UPDATE catalog_assets SET record_json=? WHERE id=?", (encode(record), self.promoted_id),
            )
            assets = {
                asset_id: json.loads(raw)
                for asset_id, raw in connection.execute("SELECT id,record_json FROM catalog_assets ORDER BY id")
            }
            metadata = self._metadata(self.input)
            metadata["collectionsSha256"]["catalog_assets"] = digest(b"".join(
                (encode([asset_id, value]) + "\n").encode("utf-8")
                for asset_id, value in sorted(assets.items())
            ))
            metadata["manifestSha256"] = digest(encode({
                key: value for key, value in metadata.items() if key != "manifestSha256"
            }).encode("utf-8"))
            for key, value in metadata.items():
                connection.execute("UPDATE catalog_metadata SET value=? WHERE key=?", (encode(value), key))
            connection.commit()
        self.source_hash = digest(self.input.read_bytes())

    def test_metadata_sentinel_rejects_boolean_source_size(self):
        sentinel = self._make_sentinel_source()
        self._rewrite_sentinel_source_record(changes={"size_bytes": False})
        self._write_worker_snapshot(sentinel)
        with self.assertRaises(finalizer.FinalizerError):
            self._derive()
        self.assertFalse(self.output.exists())
        self.assertEqual(digest(self.input.read_bytes()), self.source_hash)

    def test_metadata_sentinel_requires_present_null_source_detected_type(self):
        sentinel = self._make_sentinel_source()
        self._rewrite_sentinel_source_record(remove=("detected_type",))
        self._write_worker_snapshot(sentinel)
        with self.assertRaisesRegex(finalizer.FinalizerError, "snapshot_normalization_invalid"):
            self._derive()
        self.assertFalse(self.output.exists())
        self.assertEqual(digest(self.input.read_bytes()), self.source_hash)

    def test_metadata_sentinel_rejects_boolean_receipt_size(self):
        sentinel = self._make_sentinel_source()
        self._write_worker_snapshot(sentinel)
        receipt_path = self.snapshot_manifest.parent / "receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["legacyHashSentinelNormalization"]["entries"][0]["fingerprint"]["sizeBytes"] = False
        receipt_path.write_text(encode(receipt) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(finalizer.FinalizerError, "snapshot_normalization_invalid"):
            self._derive()
        self.assertFalse(self.output.exists())

    def test_metadata_sentinel_rejects_conflicting_evidence_aliases(self):
        sentinel = self._make_sentinel_source()
        for mutation in ("size_alias", "fingerprint_alias", "expected_hash_alias"):
            with self.subTest(mutation=mutation):
                self._write_worker_snapshot(sentinel)
                receipt_path = self.snapshot_manifest.parent / "receipt.json"
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                evidence = receipt["legacyHashSentinelNormalization"]["entries"][0]
                if mutation == "size_alias":
                    evidence["fingerprint"]["size_bytes"] = False
                elif mutation == "fingerprint_alias":
                    evidence["originalFingerprint"] = {**evidence["fingerprint"], "sizeBytes": 1}
                else:
                    evidence["originalExpectedSha256"] = "f" * 64
                receipt_path.write_text(encode(receipt) + "\n", encoding="utf-8")
                with self.assertRaisesRegex(finalizer.FinalizerError, "snapshot_normalization_invalid"):
                    self._derive()
                self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()

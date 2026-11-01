from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
downloader = importlib.import_module("download_assets")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record(
    asset_id: str,
    url: str,
    *,
    filename: str,
    media_type: str,
    expected: str | None,
    state: str = "metadata_only",
    ready: bool = False,
    size: int = 0,
) -> dict[str, object]:
    return {
        "id": asset_id,
        "source_name": "kompege",
        "canonical_url": url,
        "filename": filename,
        "media_type": media_type,
        "detected_type": media_type if ready else None,
        "size_bytes": size,
        "content_hash": expected or "0" * 64,
        "expected_content_hash": expected,
        "cache_state": state,
        "rights_status": "public_authorized",
        "hash_verified": ready,
    }


class FakeTransport:
    def __init__(self, responses: dict[str, list[downloader.SourceResponse | Exception]]):
        self.responses = {key: list(value) for key, value in responses.items()}
        self.calls: list[str] = []

    def __call__(self, url: str, _headers: dict[str, str], _maximum: int, _timeout: float) -> downloader.SourceResponse:
        self.calls.append(url)
        queue = self.responses[url]
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _response(url: str, data: bytes, *, status: int = 200, content_type: str = "text/plain") -> downloader.SourceResponse:
    return downloader.SourceResponse(status, url, {"Content-Type": content_type}, data)


class SyntheticSeed:
    def __init__(self, root: Path, records: list[dict[str, object]], byte_rows: list[tuple[str, bytes]], *, upstream: int = 0):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "catalog.incomplete.sqlite3"
        connection = sqlite3.connect(self.path)
        connection.executescript(
            """
            CREATE TABLE catalog_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE catalog_tasks (id TEXT PRIMARY KEY);
            CREATE TABLE catalog_revisions (id TEXT PRIMARY KEY);
            CREATE TABLE catalog_assets (id TEXT PRIMARY KEY, record_json TEXT NOT NULL);
            CREATE TABLE catalog_links (id TEXT PRIMARY KEY);
            CREATE TABLE catalog_selection (revision_id TEXT PRIMARY KEY);
            CREATE TABLE catalog_asset_bytes (asset_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, bytes BLOB NOT NULL);
            """
        )
        metadata = {
            "format": "kontur-catalog-seed-v1",
            "schemaVersion": 1,
            "complete": False,
            "issueCounts": {"upstream_resource_unavailable": upstream} if upstream else {},
        }
        connection.execute("INSERT INTO catalog_metadata VALUES (?,?)", ("format", json.dumps(metadata["format"])))
        connection.execute("INSERT INTO catalog_metadata VALUES (?,?)", ("schemaVersion", "1"))
        connection.execute("INSERT INTO catalog_metadata VALUES (?,?)", ("complete", "false"))
        connection.execute("INSERT INTO catalog_metadata VALUES (?,?)", ("issueCounts", json.dumps(metadata["issueCounts"])))
        for record in records:
            connection.execute(
                "INSERT INTO catalog_assets VALUES (?,?)",
                (record["id"], json.dumps(record, sort_keys=True, separators=(",", ":"))),
            )
        for asset_id, data in byte_rows:
            connection.execute(
                "INSERT INTO catalog_asset_bytes VALUES (?,?,?)",
                (asset_id, _digest(data), data),
            )
        connection.commit()
        connection.close()


class SnapshotTests(unittest.TestCase):
    def _run(
        self,
        root: Path,
        records: list[dict[str, object]],
        byte_rows: list[tuple[str, bytes]],
        transport: FakeTransport | None = None,
        resolver=None,
        *,
        upstream: int = 0,
    ) -> dict[str, object]:
        seed = SyntheticSeed(root, records, byte_rows, upstream=upstream)
        return downloader.run_snapshot(
            seed.path,
            root / "private-output",
            transport=transport,
            resolver=resolver,
            workers=3,
            min_interval_seconds=0,
        )

    def test_seed_ready_bytes_are_copied_and_resume_is_hash_verified(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            ready_data = b"\x89PNG\r\n\x1a\nseed-image"
            fetched_data = b"downloaded text\n"
            ready_hash = _digest(ready_data)
            fetched_hash = _digest(fetched_data)
            ready_url = "https://kompege.ru/images/seed.png"
            fetched_url = "https://kompege.ru/files/fetched.txt"
            records = [
                _record("a-ready", ready_url, filename="seed.png", media_type="image/png", expected=ready_hash, ready=True, state="ready", size=len(ready_data)),
                _record("a-fetch", fetched_url, filename="fetched.txt", media_type="text/plain", expected=fetched_hash),
            ]
            transport = FakeTransport({fetched_url: [_response(fetched_url, fetched_data)]})
            # Construct the seed before taking the immutable-input digest.
            seed = SyntheticSeed(root, records, [("a-ready", ready_data)])
            before = _digest(seed.path.read_bytes())
            first = downloader.run_snapshot(
                seed.path,
                root / "private-output",
                transport=transport,
                resolver=lambda _host, _port: ["93.184.216.34"],
                min_interval_seconds=0,
            )
            self.assertTrue(first["complete"])
            self.assertEqual(first["counts"]["seedReady"], 1)
            self.assertEqual(first["counts"]["downloaded"], 1)
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(before, _digest(seed.path.read_bytes()))
            second = downloader.run_snapshot(
                seed.path,
                root / "private-output",
                transport=transport,
                resolver=lambda _host, _port: ["93.184.216.34"],
                min_interval_seconds=0,
            )
            self.assertTrue(second["complete"])
            self.assertEqual(second["counts"]["resumed"], 1)
            self.assertEqual(len(transport.calls), 1)
            receipt = json.loads((root / "private-output" / "receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["format"], downloader.FORMAT)
            self.assertTrue(all(item["sha256"] for item in receipt["entries"] if item["status"] != "failed"))

    def test_transient_retry_is_once_and_content_type_is_checked(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            url = "https://kompege.ru/files/retry.txt"
            data = b"retry body\n"
            record = _record("a-retry", url, filename="retry.txt", media_type="text/plain", expected=_digest(data))
            transport = FakeTransport({url: [_response(url, b"", status=503), _response(url, data)]})
            result = self._run(root, [record], [], transport, lambda _host, _port: ["93.184.216.34"])
            self.assertTrue(result["complete"])
            self.assertEqual(len(transport.calls), 2)

            bad_url = "https://kompege.ru/files/bad-type.txt"
            bad_record = _record("a-type", bad_url, filename="bad-type.txt", media_type="text/plain", expected=_digest(data))
            bad_transport = FakeTransport({bad_url: [_response(bad_url, data, content_type="image/png")]})
            bad = self._run(root / "bad", [bad_record], [], bad_transport, lambda _host, _port: ["93.184.216.34"])
            self.assertFalse(bad["complete"])
            self.assertEqual(bad["issueCounts"], {"response_content_type_mismatch": 1})
            self.assertEqual(len(bad_transport.calls), 1)

    def test_private_dns_and_unsafe_redirect_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            private_url = "https://kompege.ru/files/private.txt"
            private_record = _record("a-private", private_url, filename="private.txt", media_type="text/plain", expected=_digest(b"x"))
            transport = FakeTransport({private_url: [_response(private_url, b"x")]})
            result = self._run(root, [private_record], [], transport, lambda _host, _port: ["10.0.0.1"])
            self.assertFalse(result["complete"])
            self.assertEqual(result["issueCounts"], {"private_target": 1})
            self.assertEqual(transport.calls, [])

            dns_transport = FakeTransport({private_url: [_response(private_url, b"x")]})
            dns_result = self._run(
                root / "dns",
                [private_record],
                [],
                dns_transport,
                lambda _host, _port: (_ for _ in ()).throw(OSError("synthetic DNS failure")),
            )
            self.assertEqual(dns_result["issueCounts"], {"dns_failed": 1})
            self.assertEqual(dns_transport.calls, [])

            redirect_url = "https://kompege.ru/files/redirect.txt"
            redirect_record = _record("a-redirect", redirect_url, filename="redirect.txt", media_type="text/plain", expected=_digest(b"x"))
            redirect_transport = FakeTransport({
                redirect_url: [downloader.SourceResponse(302, redirect_url, {"Location": "https://example.com/x"}, b"")],
            })
            redirected = self._run(root / "redirect", [redirect_record], [], redirect_transport, lambda _host, _port: ["93.184.216.34"])
            self.assertFalse(redirected["complete"])
            self.assertEqual(redirected["issueCounts"], {"unsafe_redirect": 1})

    def test_expected_hash_mismatch_is_not_cached_and_upstream_marker_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            url = "https://kompege.ru/files/wrong.txt"
            body = b"not the pinned bytes"
            record = _record("a-wrong", url, filename="wrong.txt", media_type="text/plain", expected=_digest(b"expected"))
            transport = FakeTransport({url: [_response(url, body)]})
            result = self._run(root, [record], [], transport, lambda _host, _port: ["93.184.216.34"], upstream=30)
            self.assertFalse(result["complete"])
            self.assertEqual(result["upstreamResourceUnavailable"], 30)
            self.assertEqual(result["issueCounts"], {"asset_hash_mismatch": 1})
            assets = list((root / "private-output" / "assets").rglob("*")) if (root / "private-output" / "assets").exists() else []
            self.assertEqual([path for path in assets if path.is_file()], [])

    def test_allowlist_rejects_non_public_file_urls(self) -> None:
        for value in (
            "http://kompege.ru/files/x.txt",
            "https://example.com/files/x.txt",
            "https://kompege.ru/task/x",
            "https://kompege.ru/files/../secret.txt",
            "https://user:pass@kompege.ru/files/x.txt",
            "https://kompege.ru/files/x.txt#fragment",
        ):
            with self.subTest(value=value):
                with self.assertRaises(downloader.SnapshotError):
                    downloader.canonical_asset_url(value)


if __name__ == "__main__":
    unittest.main()

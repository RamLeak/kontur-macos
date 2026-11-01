"""Synthetic receipt/finalizer and pinned-transport boundary checks."""

import gc
import importlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
import weakref
from contextlib import closing
from http.client import HTTPResponse
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import download_assets as downloader
from test_download_assets import FakeTransport, SyntheticSeed, _digest, _record, _response

FINALIZER_ROOT = Path(__file__).resolve().parent.parent / "catalog-seed-finalizer-20261030"
sys.path.insert(0, str(FINALIZER_ROOT))
finalizer = importlib.import_module("finalize_catalog")
fixtures = importlib.import_module("test_finalize_catalog")


class NormalizationTests(unittest.TestCase):
    def test_ready_bytes_cannot_bypass_another_rows_genuine_expected_hash(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            root = Path(temporary)
            url = "https://kompege.ru/files/shared.txt"
            body = b"unpinned ready bytes\n"
            records = [
                _record("asset-ready", url, filename="ready.txt", media_type="text/plain", expected=None,
                        ready=True, state="ready", size=len(body)),
                _record("asset-pinned", url, filename="pinned.txt", media_type="text/plain",
                        expected=_digest(b"genuine expected bytes\n")),
            ]
            seed = SyntheticSeed(root, records, [("asset-ready", body)])
            result = downloader.run_snapshot(seed.path, root / "output")
            self.assertFalse(result["complete"])
            self.assertEqual(result["issueCounts"], {"asset_hash_mismatch": 1})

    def test_same_url_metadata_fingerprints_normalize_with_original_evidence(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            root = Path(temporary)
            url = "https://kompege.ru/files/shared.txt"
            body = b"the real attachment\n"
            records = []
            for index, state in enumerate(("metadata_only", "failed")):
                filename = f"original-{index}.txt"
                sentinel = _digest(f"metadata:{url}:{filename}".encode())
                records.append(_record(f"asset-{index}", url, filename=filename, media_type="text/plain",
                                       expected=sentinel, state=state))
            seed = SyntheticSeed(root, records, [])
            before = _digest(seed.path.read_bytes())
            transport = FakeTransport({url: [_response(url, body)]})
            result = downloader.run_snapshot(seed.path, root / "output", transport=transport,
                                             resolver=lambda *_args: ["93.184.216.34"])
            self.assertTrue(result["complete"])
            self.assertEqual(transport.calls, [url])
            self.assertEqual(result["entries"][0]["sha256"], _digest(body))
            self.assertIsNone(result["entries"][0]["expectedContentHash"])
            evidence = result["legacyHashSentinelNormalization"]["entries"]
            self.assertEqual(len(evidence), 2)
            for original, item in zip(records, evidence, strict=True):
                self.assertEqual(item["originalExpectedContentHash"], original["expected_content_hash"])
                self.assertEqual(item["fingerprint"]["contentHash"], original["content_hash"])
                self.assertEqual(item["fingerprint"]["filename"], original["filename"])
                self.assertIsNone(item["normalizedExpectedContentHash"])
            self.assertEqual(_digest(seed.path.read_bytes()), before)

    def test_near_sentinels_keep_expected_hash_enforced(self):
        changes = (
            {"content_hash": "a" * 64}, {"size_bytes": 1}, {"hash_verified": True},
            {"hash_verified": 0}, {"detected_type": "text/plain"}, {"cache_state": "ready"},
            {"expected_content_hash": "b" * 64},
        )
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            for index, mutation in enumerate(changes):
                with self.subTest(mutation=mutation):
                    root = Path(temporary) / str(index)
                    url = "https://kompege.ru/files/strict.txt"
                    sentinel = _digest(f"metadata:{url}:strict.txt".encode())
                    record = _record("asset-strict", url, filename="strict.txt", media_type="text/plain",
                                     expected=sentinel)
                    record.update(mutation)
                    seed = SyntheticSeed(root, [record], [])
                    result = downloader.run_snapshot(
                        seed.path, root / "output", transport=FakeTransport({url: [_response(url, b"different bytes")]}),
                        resolver=lambda *_args: ["93.184.216.34"],
                    )
                    self.assertFalse(result["complete"])
                    self.assertEqual(result["issueCounts"], {"asset_hash_mismatch": 1})
                    self.assertEqual(result["legacyHashSentinelNormalization"]["entries"], [])

    def test_actual_downloader_receipt_builds_v2_and_preserves_warning_and_domain_rows(self):
        fixture = fixtures.FinalizerTests(methodName="runTest")
        fixture.setUp()
        try:
            with closing(sqlite3.connect(fixture.input)) as connection:
                for identifier, raw in list(connection.execute("SELECT id,record_json FROM catalog_assets")):
                    record = json.loads(raw)
                    record["canonical_url"] = "https://kompege.ru/files/" + record["filename"]
                    connection.execute("UPDATE catalog_assets SET record_json=? WHERE id=?",
                                       (fixtures.encode(record), identifier))
                connection.commit()
            # Recompute the source collection hashes after the synthetic URL change.
            fixture._make_sentinel_source()
            before = _digest(fixture.input.read_bytes())
            preserved = {table: fixture._rows(fixture.input, table) for table in
                         ("catalog_tasks", "catalog_revisions", "catalog_links", "catalog_selection")}
            url = "https://kompege.ru/files/promoted.bin"
            output = fixture.root / "downloader-output"
            result = downloader.run_snapshot(
                fixture.input, output, transport=FakeTransport({url: [_response(url, fixture.promoted)]}),
                resolver=lambda *_args: ["93.184.216.34"],
            )
            # The historical upstream warning remains; admitted bytes are all available.
            self.assertFalse(result["complete"])
            self.assertEqual(result["counts"]["unavailable"], 0)
            finalizer.derive_catalog(fixture.input, output / "manifest.json", output, fixture.output,
                                     expected_source_sha256=before)
            metadata = fixture._metadata(fixture.output)
            self.assertEqual(metadata["schemaVersion"], 2)
            self.assertTrue(metadata["offlineReady"])
            self.assertFalse(metadata["complete"])
            self.assertEqual(metadata["issueCounts"], {"upstream_resource_unavailable": 1})
            self.assertIn(fixture.warning_revision, fixtures.encode(metadata["issues"]))
            for table, rows in preserved.items():
                self.assertEqual(fixture._rows(fixture.output, table), rows)
            self.assertEqual(_digest(fixture.input.read_bytes()), before)
        finally:
            fixture.tearDown()


class PinnedTransportTests(unittest.TestCase):
    def test_receipt_drive_and_control_paths_fail_before_filesystem_access(self):
        output = Path(__file__).resolve().parent / "synthetic-output"
        for value in ("assets/D:/escape", "assets/file:stream", "assets/line\nname"):
            with self.subTest(value=value):
                with patch.object(Path, "is_symlink", side_effect=AssertionError("Unexpected filesystem access")):
                    with self.assertRaises(downloader.SnapshotError):
                        downloader._safe_relative_path(output, value)

    def test_http_content_length_must_be_complete_without_reading_extra_bytes(self):
        class BufferedSocket:
            def makefile(self, _mode):
                return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\ntruncated\n")

        response = HTTPResponse(BufferedSocket())
        response.begin()
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(downloader, "_PinnedHTTPSConnection", return_value=connection):
            with self.assertRaisesRegex(downloader.TransportFailure, "incomplete_response") as failure:
                downloader._default_transport("https://kompege.ru/files/file.txt", {}, 20, 1,
                                              addresses=("93.184.216.34",))
        self.assertTrue(failure.exception.transient)
        connection.close.assert_called_once()

    def test_empty_userinfo_is_also_rejected(self):
        with self.assertRaises(downloader.SnapshotError):
            downloader.canonical_asset_url("https://@kompege.ru/files/file.txt")

    def test_http_reader_does_not_probe_past_the_remaining_budget(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.getheaders.return_value = [("Content-Type", "text/plain")]
        response.read.return_value = b"abc"
        response.length = 0
        response.isclosed.return_value = False  # A larger or unframed body remains.
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(downloader, "_PinnedHTTPSConnection", return_value=connection):
            with self.assertRaisesRegex(downloader.TransportFailure, "total_asset_budget_exceeded"):
                downloader._default_transport("https://kompege.ru/files/file.txt", {}, 3, 1,
                                              addresses=("93.184.216.34",))
        response.read.assert_called_once_with(3)
        connection.close.assert_called_once()

    def test_fake_transport_without_fake_resolver_cannot_start_real_dns(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            root = Path(temporary)
            url = "https://kompege.ru/files/file.txt"
            seed = SyntheticSeed(root, [_record("asset-1", url, filename="file.txt", media_type="text/plain",
                                               expected=None)], [])
            with patch.object(downloader, "_default_resolver", return_value=["93.184.216.34"]) as resolver:
                result = downloader.run_snapshot(seed.path, root / "output",
                                                 transport=FakeTransport({url: [_response(url, b"data")]}))
            resolver.assert_not_called()
            self.assertEqual(result["issueCounts"], {"network_not_authorized": 1})

    def _connection(self, raw_peer, tls_peer):
        raw = Mock()
        raw.getpeername.return_value = (raw_peer, 443)
        secured = Mock()
        secured.getpeername.return_value = (tls_peer, 443)
        context = Mock()
        context.wrap_socket.return_value = secured
        with patch.object(downloader.ssl, "create_default_context", return_value=context):
            connection = downloader._PinnedHTTPSConnection(("93.184.216.34",), 1.0)
        return connection, raw, secured, context

    def test_numeric_connection_and_both_peers_are_checked_before_http(self):
        connection, raw, secured, context = self._connection("93.184.216.34", "93.184.216.34")
        with patch.object(downloader.socket, "socket", return_value=raw), \
             patch.object(downloader.socket, "getaddrinfo") as resolver:
            connection.connect()
        raw.connect.assert_called_once_with(("93.184.216.34", 443))
        context.wrap_socket.assert_called_once_with(raw, server_hostname="kompege.ru")
        resolver.assert_not_called()
        self.assertIs(connection.sock, secured)
        connection.close()

    def test_wrong_raw_or_tls_peer_closes_connection(self):
        for raw_peer, tls_peer in (("127.0.0.1", "93.184.216.34"), ("93.184.216.34", "10.0.0.1")):
            with self.subTest(raw_peer=raw_peer, tls_peer=tls_peer):
                connection, raw, secured, context = self._connection(raw_peer, tls_peer)
                with patch.object(downloader.socket, "socket", return_value=raw):
                    with self.assertRaisesRegex(downloader.SnapshotError, "peer_mismatch"):
                        connection.connect()
                raw.close.assert_called_once()
                if raw_peer != "93.184.216.34":
                    context.wrap_socket.assert_not_called()
                else:
                    secured.close.assert_called_once()


class RejectedBodyTests(unittest.TestCase):
    def test_pinned_generic_row_cannot_bypass_another_rows_specific_mime(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            root = Path(temporary)
            url = "https://kompege.ru/files/file.bin"
            body = b"%PDF-1.7\nattachment"
            records = [
                _record("asset-pinned", url, filename="file.bin", media_type="application/octet-stream",
                        expected=_digest(body)),
                _record("asset-image", url, filename="image.png", media_type="image/png", expected=None),
            ]
            seed = SyntheticSeed(root, records, [])
            result = downloader.run_snapshot(seed.path, root / "output",
                                             transport=FakeTransport({url: [_response(url, body,
                                                                                     content_type="application/pdf")]}),
                                             resolver=lambda *_args: ["93.184.216.34"])
            self.assertEqual(result["issueCounts"], {"asset_type_mismatch": 1})

    def test_failed_response_bodies_are_released_before_receipt_write(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            root = Path(temporary)
            responses = []
            records = [_record(f"asset-{i}", f"https://kompege.ru/files/{i}.txt", filename=f"{i}.txt",
                               media_type="text/plain", expected="a" * 64) for i in range(4)]
            seed = SyntheticSeed(root, records, [])

            def transport(url, _headers, _maximum, _timeout):
                response = _response(url, b"x" * 1_000_000)
                responses.append(weakref.ref(response))
                return response

            write_json = downloader._write_json_atomically

            def check_receipt(path, payload):
                if path.name == "receipt.json":
                    gc.collect()
                    self.assertEqual(sum(reference() is not None for reference in responses), 0)
                write_json(path, payload)

            with patch.object(downloader, "_write_json_atomically", side_effect=check_receipt):
                result = downloader.run_snapshot(seed.path, root / "output", transport=transport,
                                                 resolver=lambda *_args: ["93.184.216.34"], workers=1)
            self.assertEqual(result["issueCounts"], {"asset_hash_mismatch": 4})

    def test_redirect_controls_and_traversal_are_rejected_before_urljoin(self):
        locations = ("/files/../files/end.txt", "\thttps://kompege.ru/files/end.txt",
                     "https://kompege.ru/fi\nles/end.txt")
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            for index, location in enumerate(locations):
                with self.subTest(location=location):
                    root = Path(temporary) / str(index)
                    url = "https://kompege.ru/files/start.txt"
                    end = "https://kompege.ru/files/end.txt"
                    seed = SyntheticSeed(root, [_record("asset-1", url, filename="start.txt",
                                                       media_type="text/plain", expected=None)], [])
                    transport = FakeTransport({
                        url: [downloader.SourceResponse(302, url, {"Location": location}, b"")],
                        end: [_response(end, b"body")],
                    })
                    result = downloader.run_snapshot(seed.path, root / "output", transport=transport,
                                                     resolver=lambda *_args: ["93.184.216.34"])
                    self.assertEqual(result["issueCounts"], {"unsafe_redirect": 1})
                    self.assertEqual(transport.calls, [url])

    def test_html_after_long_whitespace_or_utf16_is_rejected(self):
        bodies = (b" " * 8192 + b"<html>error</html>", "<html>error</html>".encode("utf-16"))
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            for index, body in enumerate(bodies):
                with self.subTest(encoding=index):
                    root = Path(temporary) / str(index)
                    url = "https://kompege.ru/files/file.bin"
                    seed = SyntheticSeed(root, [_record("asset-1", url, filename="file.bin",
                                                       media_type="application/octet-stream", expected=None)], [])
                    result = downloader.run_snapshot(
                        seed.path, root / "output", transport=FakeTransport({url: [_response(url, body)]}),
                        resolver=lambda *_args: ["93.184.216.34"],
                    )
                    self.assertEqual(result["issueCounts"], {"asset_html_blocked": 1})

    def test_changed_resume_bytes_cannot_reuse_the_old_digest(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            root = Path(temporary)
            url = "https://kompege.ru/files/file.txt"
            seed = SyntheticSeed(root, [_record("asset-1", url, filename="file.txt", media_type="text/plain",
                                               expected=None)], [])
            output = root / "output"
            downloader.run_snapshot(seed.path, output, transport=FakeTransport({url: [_response(url, b"original\n")]}),
                                    resolver=lambda *_args: ["93.184.216.34"])
            check = downloader._safe_existing_asset

            def replace_after_check(path, *args):
                valid = check(path, *args)
                if valid:
                    path.write_bytes(b"modified\n")
                return valid

            with patch.object(downloader, "_safe_existing_asset", side_effect=replace_after_check):
                result = downloader.run_snapshot(seed.path, output)
            self.assertFalse(result["complete"])
            self.assertEqual(result["counts"]["resumed"], 0)


if __name__ == "__main__":
    unittest.main()

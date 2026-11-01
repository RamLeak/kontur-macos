from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import download_assets as downloader
from test_download_assets import (
    FakeTransport,
    SyntheticSeed,
    _digest,
    _record,
    _response,
)

ROOT = Path(__file__).resolve().parent
PUBLIC_IP = "93.184.216.34"


def _run_snapshot(
    seed: SyntheticSeed,
    output: Path,
    *,
    transport,
    resolver,
    **kwargs,
) -> dict[str, object]:
    return downloader.run_snapshot(
        seed.path,
        output,
        transport=transport,
        resolver=resolver,
        workers=1,
        min_interval_seconds=0,
        **kwargs,
    )


class BudgetTransport:
    def __init__(self, responses: dict[str, downloader.SourceResponse]):
        self.responses = dict(responses)
        self.calls: list[tuple[str, int]] = []

    def __call__(self, url: str, _headers: dict[str, str], maximum: int, _timeout: float) -> downloader.SourceResponse:
        self.calls.append((url, maximum))
        return self.responses[url]


class DownloaderSecurityTests(unittest.TestCase):
    def test_default_network_requires_opt_in_before_resolver_or_transport(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            url = "https://kompege.ru/files/network-gate.txt"
            body = b"network gate\n"
            record = _record(
                "a-network-gate",
                url,
                filename="network-gate.txt",
                media_type="text/plain",
                expected=_digest(body),
            )
            seed = SyntheticSeed(root / "seed", [record], [])
            transport_calls: list[str] = []
            resolver_calls: list[tuple[str, int]] = []

            def fake_transport(
                request_url: str,
                _headers: dict[str, str],
                _maximum: int,
                _timeout: float,
            ) -> downloader.SourceResponse:
                transport_calls.append(request_url)
                return _response(request_url, body)

            def fake_resolver(host: str, port: int) -> list[str]:
                resolver_calls.append((host, port))
                return [PUBLIC_IP]

            with (
                mock.patch.object(downloader, "_default_transport", side_effect=fake_transport),
                mock.patch.object(downloader, "_default_resolver", side_effect=fake_resolver),
            ):
                result = downloader.run_snapshot(
                    seed.path,
                    root / "private-output",
                    workers=1,
                    min_interval_seconds=0,
                    allow_network=False,
                )

            self.assertFalse(result["complete"])
            self.assertEqual(result["issueCounts"], {"network_not_authorized": 1})
            self.assertEqual(transport_calls, [])
            self.assertEqual(resolver_calls, [])

    def test_canonical_url_rejects_encoded_traversal_slashes_controls_and_bad_ports(self) -> None:
        values = (
            "https://kompege.ru/files/%2e%2e/secret.txt",
            "https://kompege.ru/files/%252e%252e/secret.txt",
            "https://kompege.ru/files/foo%2f..%2fsecret.txt",
            "https://kompege.ru/files/foo%5c..%5csecret.txt",
            "https://kompege.ru/files/foo\\..\\secret.txt",
            "https://kompege.ru/files/foo\x00bar.txt",
            "https://kompege.ru/files/foo%0abar.txt",
            "https://kompege.ru:bad/files/x.txt",
            "https://kompege.ru:65536/files/x.txt",
        )
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(downloader.SnapshotError):
                    downloader.canonical_asset_url(value)

    def test_dns_is_revalidated_before_each_transient_retry(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            url = "https://kompege.ru/files/retry-dns.txt"
            body = b"retry after dns check\n"
            record = _record(
                "a-retry-dns",
                url,
                filename="retry-dns.txt",
                media_type="text/plain",
                expected=_digest(body),
            )
            transport = FakeTransport(
                {
                    url: [
                        downloader.TransportFailure("network_error", transient=True),
                        _response(url, body),
                    ]
                }
            )
            resolver_calls: list[tuple[str, int]] = []

            def resolver(host: str, port: int) -> list[str]:
                resolver_calls.append((host, port))
                return [PUBLIC_IP]

            seed = SyntheticSeed(root / "seed", [record], [])
            result = _run_snapshot(seed, root / "private-output", transport=transport, resolver=resolver)

            self.assertTrue(result["complete"])
            self.assertEqual(transport.calls, [url, url])
            self.assertEqual(len(resolver_calls), 2)

    def test_private_resolution_on_retry_blocks_the_second_request(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            url = "https://kompege.ru/files/retry-private.txt"
            body = b"must not reach private retry\n"
            record = _record(
                "a-retry-private",
                url,
                filename="retry-private.txt",
                media_type="text/plain",
                expected=_digest(body),
            )
            transport = FakeTransport(
                {
                    url: [
                        downloader.TransportFailure("network_error", transient=True),
                        _response(url, body),
                    ]
                }
            )
            resolver_results = iter(([PUBLIC_IP], ["10.0.0.1"]))
            resolver_calls: list[tuple[str, int]] = []

            def resolver(host: str, port: int) -> list[str]:
                resolver_calls.append((host, port))
                return next(resolver_results)

            seed = SyntheticSeed(root / "seed", [record], [])
            result = _run_snapshot(seed, root / "private-output", transport=transport, resolver=resolver)

            self.assertFalse(result["complete"])
            self.assertEqual(result["issueCounts"], {"private_target": 1})
            self.assertEqual(transport.calls, [url])
            self.assertEqual(len(resolver_calls), 2)

    def test_transient_dns_failure_retries_once(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            url = "https://kompege.ru/files/dns-retry.txt"
            body = b"retry after transient dns\n"
            record = _record(
                "a-dns-retry",
                url,
                filename="dns-retry.txt",
                media_type="text/plain",
                expected=_digest(body),
            )
            transport = FakeTransport({url: [_response(url, body)]})
            resolver_results = iter(
                (
                    downloader.TransportFailure("dns_failed", transient=True),
                    [PUBLIC_IP],
                )
            )
            resolver_calls: list[tuple[str, int]] = []

            def resolver(host: str, port: int) -> list[str]:
                resolver_calls.append((host, port))
                result = next(resolver_results)
                if isinstance(result, Exception):
                    raise result
                return result

            seed = SyntheticSeed(root / "seed", [record], [])
            result = _run_snapshot(seed, root / "private-output", transport=transport, resolver=resolver)

            self.assertTrue(result["complete"])
            self.assertEqual(transport.calls, [url])
            self.assertEqual(len(resolver_calls), 2)

    def test_dns_is_revalidated_before_a_redirect_request(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            start_url = "https://kompege.ru/files/redirect-dns-start.txt"
            target_url = "https://kompege.ru/files/redirect-dns-target.txt"
            body = b"redirected after dns check\n"
            record = _record(
                "a-redirect-dns",
                start_url,
                filename="redirect-dns.txt",
                media_type="text/plain",
                expected=_digest(body),
            )
            transport = FakeTransport(
                {
                    start_url: [
                        downloader.SourceResponse(
                            302,
                            start_url,
                            {"Location": target_url},
                            b"",
                        )
                    ],
                    target_url: [_response(target_url, body)],
                }
            )
            resolver_calls: list[tuple[str, int]] = []

            def resolver(host: str, port: int) -> list[str]:
                resolver_calls.append((host, port))
                return [PUBLIC_IP]

            seed = SyntheticSeed(root / "seed", [record], [])
            result = _run_snapshot(seed, root / "private-output", transport=transport, resolver=resolver)

            self.assertTrue(result["complete"])
            self.assertEqual(transport.calls, [start_url, target_url])
            self.assertEqual(len(resolver_calls), 2)

    def test_html_body_is_blocked_even_when_declared_text_or_octet_stream(self) -> None:
        html = b"<html><body>sign-in page</body></html>\n"
        cases = (
            ("text/plain", "text/plain", "html-text.txt"),
            ("application/octet-stream", "application/octet-stream", "html-octets.bin"),
        )
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            for index, (media_type, response_type, filename) in enumerate(cases):
                with self.subTest(media_type=media_type):
                    url = f"https://kompege.ru/files/{filename}"
                    record = _record(
                        f"a-html-{index}",
                        url,
                        filename=filename,
                        media_type=media_type,
                        expected=None,
                    )
                    transport = FakeTransport(
                        {url: [_response(url, html, content_type=response_type)]}
                    )
                    seed = SyntheticSeed(root / f"seed-{index}", [record], [])
                    result = _run_snapshot(
                        seed,
                        root / f"private-output-{index}",
                        transport=transport,
                        resolver=lambda _host, _port: [PUBLIC_IP],
                    )

                    self.assertFalse(result["complete"])
                    self.assertEqual(result["issueCounts"], {"asset_html_blocked": 1})
                    self.assertEqual(result["counts"]["downloaded"], 0)

    def test_resume_requires_the_same_seed_hash_even_when_url_has_no_expected_hash(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            url = "https://kompege.ru/files/resume-seed.txt"
            body = b"resume identity\n"
            first_record = _record(
                "a-resume",
                url,
                filename="resume-seed.txt",
                media_type="text/plain",
                expected=None,
            )
            second_record = _record(
                "a-resume",
                url,
                filename="resume-seed-renamed.txt",
                media_type="text/plain",
                expected=None,
            )
            first_seed = SyntheticSeed(root / "seed-a", [first_record], [])
            second_seed = SyntheticSeed(root / "seed-b", [second_record], [])
            transport = FakeTransport(
                {
                    url: [_response(url, body), _response(url, body)],
                }
            )
            def resolver(_host, _port):
                return [PUBLIC_IP]
            output = root / "private-output"

            first = _run_snapshot(first_seed, output, transport=transport, resolver=resolver)
            second = _run_snapshot(second_seed, output, transport=transport, resolver=resolver)

            self.assertNotEqual(first["seedSha256"], second["seedSha256"])
            self.assertEqual(first["counts"]["downloaded"], 1)
            self.assertEqual(second["counts"]["resumed"], 0)
            self.assertEqual(second["counts"]["downloaded"], 1)
            self.assertEqual(len(transport.calls), 2)

    def test_resume_requires_matching_asset_ids(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            url = "https://kompege.ru/files/resume-assets.txt"
            body = b"asset id identity\n"
            record = _record(
                "a-resume-id",
                url,
                filename="resume-assets.txt",
                media_type="text/plain",
                expected=None,
            )
            seed = SyntheticSeed(root / "seed", [record], [])
            transport = FakeTransport(
                {
                    url: [_response(url, body), _response(url, body)],
                }
            )
            def resolver(_host, _port):
                return [PUBLIC_IP]
            output = root / "private-output"

            first = _run_snapshot(seed, output, transport=transport, resolver=resolver)
            receipt_path = output / "receipt.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["entries"][0]["assetIds"] = ["different-asset-id"]
            receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
            second = _run_snapshot(seed, output, transport=transport, resolver=resolver)

            self.assertEqual(first["seedSha256"], second["seedSha256"])
            self.assertEqual(second["counts"]["resumed"], 0)
            self.assertEqual(second["counts"]["downloaded"], 1)
            self.assertEqual(len(transport.calls), 2)

    def test_budget_bounds_transport_and_stops_after_exhaustion(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            records = []
            responses = {}
            for index in range(3):
                body = (f"{index}abc").encode("ascii")
                filename = f"budget-{index}.txt"
                url = f"https://kompege.ru/files/{filename}"
                records.append(
                    _record(
                        f"a-budget-{index}",
                        url,
                        filename=filename,
                        media_type="text/plain",
                        expected=None,
                    )
                )
                responses[url] = _response(url, body)
            transport = BudgetTransport(responses)
            seed = SyntheticSeed(root / "seed", records, [])
            budget = 8
            result = _run_snapshot(
                seed,
                root / "private-output",
                transport=transport,
                resolver=lambda _host, _port: [PUBLIC_IP],
                max_total_bytes=budget,
            )

            self.assertFalse(result["complete"])
            self.assertEqual(result["issueCounts"], {"total_asset_budget_exceeded": 1})
            self.assertEqual(result["counts"]["downloaded"], 2)
            self.assertEqual(result["counts"]["bytes"], budget)
            self.assertEqual(len(transport.calls), 2)
            self.assertLessEqual(transport.calls[0][1], budget)
            self.assertLessEqual(transport.calls[1][1], budget - 4)

    def test_pinned_expected_sha_remains_a_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            url = "https://kompege.ru/files/pinned-sha.txt"
            record = _record(
                "a-pinned-sha",
                url,
                filename="pinned-sha.txt",
                media_type="text/plain",
                expected=_digest(b"expected bytes\n"),
            )
            transport = FakeTransport({url: [_response(url, b"wrong bytes\n")]})
            seed = SyntheticSeed(root / "seed", [record], [])
            result = _run_snapshot(
                seed,
                root / "private-output",
                transport=transport,
                resolver=lambda _host, _port: [PUBLIC_IP],
            )

            self.assertFalse(result["complete"])
            self.assertEqual(result["issueCounts"], {"asset_hash_mismatch": 1})
            self.assertEqual(result["counts"]["downloaded"], 0)


if __name__ == "__main__":
    unittest.main()

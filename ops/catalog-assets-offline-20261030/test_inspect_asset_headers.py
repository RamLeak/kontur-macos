from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import inspect_asset_headers as inspector


PUBLIC_IP = "93.184.216.34"


class FakeResponse:
    def __init__(self, status: int, headers: list[tuple[str, str]]):
        self.status = status
        self._headers = headers
        self.read_calls = 0
        self.closed = False

    def getheaders(self) -> list[tuple[str, str]]:
        return list(self._headers)

    def read(self, *_args, **_kwargs):
        self.read_calls += 1
        raise AssertionError("HEAD inspector must never read a response body")

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, factory: "FakeConnectionFactory", addresses: tuple[str, ...], timeout: float):
        self.factory = factory
        self.addresses = addresses
        self.timeout = timeout
        self.response: FakeResponse | None = None
        self.closed = False

    def request(self, method: str, target: str, *, headers: dict[str, str]) -> None:
        if method != "HEAD":
            raise AssertionError(f"unexpected HTTP method: {method}")
        self.factory.requests.append((method, target, dict(headers), self.addresses, self.timeout))
        queue = self.factory.responses[target]
        self.response = queue.pop(0)
        self.factory.seen_responses.append(self.response)

    def getresponse(self) -> FakeResponse:
        assert self.response is not None
        return self.response

    def close(self) -> None:
        self.closed = True


class FakeConnectionFactory:
    def __init__(self, responses: dict[str, list[FakeResponse]]):
        self.responses = {target: list(queue) for target, queue in responses.items()}
        self.requests: list[tuple[str, str, dict[str, str], tuple[str, ...], float]] = []
        self.connections: list[FakeConnection] = []
        self.seen_responses: list[FakeResponse] = []

    def __call__(self, addresses: tuple[str, ...], timeout: float) -> FakeConnection:
        connection = FakeConnection(self, addresses, timeout)
        self.connections.append(connection)
        return connection


class HeaderInspectorTests(unittest.TestCase):
    def _receipt(self, root: Path, urls: list[str], *, issue: str = "asset_too_large") -> Path:
        root.mkdir(parents=True, exist_ok=True)
        receipt = {
            "format": inspector.SNAPSHOT_FORMAT,
            "seedSha256": inspector.EXPECTED_SEED_SHA256,
            "createdAt": "2026-10-30T12:00:00+00:00",
            "entries": [
                {"url": url, "assetIds": [f"asset-{index}"], "status": "failed", "issue": issue}
                for index, url in enumerate(urls)
            ],
        }
        path = root / "receipt.json"
        path.write_text(json.dumps(receipt), encoding="utf-8")
        return path

    def _three_urls(self) -> list[str]:
        return [
            "https://kompege.ru/files/oversized-a.bin",
            "https://kompege.ru/images/oversized-b.jpg?download=1",
            "https://kompege.ru/files/oversized-c.txt",
        ]

    def _responses(self, urls: list[str], *, length: int = inspector.MAX_ASSET_BYTES + 1) -> dict[str, list[FakeResponse]]:
        return {
            inspector._target_for_head(url): [
                FakeResponse(200, [("Content-Length", str(length)), ("Content-Type", "application/octet-stream")])
            ]
            for url in urls
        }

    def test_head_only_records_bounded_metadata_and_rejects_body_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            urls = self._three_urls()
            receipt = self._receipt(root, urls)
            output = root / "headers.json"
            factory = FakeConnectionFactory(self._responses(urls))
            resolver_calls: list[tuple[str, int]] = []

            def resolver(host: str, port: int) -> list[str]:
                resolver_calls.append((host, port))
                return [PUBLIC_IP]

            result = inspector.inspect_receipt(
                receipt,
                output,
                allow_network=True,
                resolver=resolver,
                connection_factory=factory,
            )

            self.assertTrue(result["complete"])
            self.assertEqual(result["counts"], {"entries": 3, "responses": 3, "failed": 0})
            self.assertEqual(len(factory.requests), 3)
            self.assertEqual(resolver_calls, [("kompege.ru", 443)] * 3)
            for method, _target, headers, addresses, timeout in factory.requests:
                self.assertEqual(method, "HEAD")
                self.assertEqual(addresses, (PUBLIC_IP,))
                self.assertEqual(timeout, inspector.HEAD_TIMEOUT_SECONDS)
                self.assertEqual(set(headers), {"Accept", "User-Agent"})
                self.assertNotIn("Cookie", headers)
                self.assertNotIn("Authorization", headers)
            self.assertEqual([response.read_calls for response in factory.seen_responses], [0] * 3)
            self.assertTrue(all(response.closed for response in factory.seen_responses))
            self.assertEqual([item["contentLength"] for item in result["entries"]], [inspector.MAX_ASSET_BYTES + 1] * 3)
            self.assertTrue(all(item["receiptIssue"] == "asset_too_large" for item in result["entries"]))
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), result)

    def test_issue_allowlist_accepts_one_and_eight_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            one_url = ["https://kompege.ru/files/failed-one.bin"]
            one_receipt = self._receipt(root / "one", one_url, issue="http_error")
            one_output = root / "one" / "headers.json"
            one_factory = FakeConnectionFactory(self._responses(one_url))
            one = inspector.inspect_receipt(
                one_receipt,
                one_output,
                allow_network=True,
                issues={"http_error"},
                resolver=lambda _host, _port: [PUBLIC_IP],
                connection_factory=one_factory,
            )
            self.assertEqual(one["counts"]["entries"], 1)
            self.assertEqual(one["entries"][0]["receiptIssue"], "http_error")

            eight_urls = [f"https://kompege.ru/files/failed-{index}.bin" for index in range(8)]
            eight_receipt = self._receipt(root / "eight", eight_urls, issue="network_error")
            eight_output = root / "eight" / "headers.json"
            eight_factory = FakeConnectionFactory(self._responses(eight_urls))
            eight = inspector.inspect_receipt(
                eight_receipt,
                eight_output,
                allow_network=True,
                issues="asset_too_large,http_error,network_error,incomplete_response,transient_http",
                resolver=lambda _host, _port: [PUBLIC_IP],
                connection_factory=eight_factory,
            )
            self.assertEqual(eight["counts"]["entries"], 8)
            self.assertEqual(eight["limits"]["workers"], 3)
            self.assertEqual(len(eight_factory.requests), 8)

    def test_issue_allowlist_rejects_zero_over_eight_and_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            zero_receipt = self._receipt(root / "zero", [])
            with self.assertRaisesRegex(inspector.InspectorError, "receipt_entry_count_invalid"):
                inspector.inspect_receipt(zero_receipt, root / "zero" / "headers.json", allow_network=True)

            nine_urls = [f"https://kompege.ru/files/failed-{index}.bin" for index in range(9)]
            nine_receipt = self._receipt(root / "nine", nine_urls)
            with self.assertRaisesRegex(inspector.InspectorError, "receipt_entry_count_invalid"):
                inspector.inspect_receipt(nine_receipt, root / "nine" / "headers.json", allow_network=True)

            with self.assertRaisesRegex(inspector.InspectorError, "issue_allowlist_invalid"):
                inspector.inspect_receipt(
                    zero_receipt,
                    root / "unknown" / "headers.json",
                    allow_network=True,
                    issues="asset_too_large,not_allowlisted",
                )

    def test_transient_head_retries_with_fresh_dns_and_never_follows_redirect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            urls = self._three_urls()
            receipt = self._receipt(root, urls)
            output = root / "headers.json"
            responses = self._responses(urls)
            retry_target = inspector._target_for_head(urls[0])
            responses[retry_target] = [
                FakeResponse(503, []),
                FakeResponse(200, [("Content-Length", "20000001"), ("Content-Type", "application/octet-stream")]),
            ]
            redirect_target = inspector._target_for_head(urls[1])
            responses[redirect_target] = [
                FakeResponse(302, [("Location", "https://example.com/private")]),
            ]
            factory = FakeConnectionFactory(responses)
            resolver_calls: list[tuple[str, int]] = []

            def resolver(host: str, port: int) -> list[str]:
                resolver_calls.append((host, port))
                return [PUBLIC_IP]

            result = inspector.inspect_receipt(
                receipt,
                output,
                allow_network=True,
                resolver=resolver,
                connection_factory=factory,
            )

            self.assertFalse(result["complete"])
            self.assertEqual(result["issueCounts"], {"redirect_not_followed": 1})
            self.assertEqual(result["entries"][0]["attempts"], 2)
            self.assertEqual(result["entries"][0]["status"], 200)
            self.assertEqual(result["entries"][1]["inspectionIssue"], "redirect_not_followed")
            self.assertEqual(result["entries"][1]["status"], 302)
            self.assertEqual(len(factory.requests), 4)
            self.assertEqual(len(resolver_calls), 4)

    def test_conflicting_content_length_is_fixed_issue_without_body_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            urls = self._three_urls()
            receipt = self._receipt(root, urls)
            output = root / "headers.json"
            responses = self._responses(urls)
            responses[inspector._target_for_head(urls[0])] = [
                FakeResponse(200, [("Content-Length", "12"), ("content-length", "13")]),
            ]
            factory = FakeConnectionFactory(responses)
            result = inspector.inspect_receipt(
                receipt,
                output,
                allow_network=True,
                resolver=lambda _host, _port: [PUBLIC_IP],
                connection_factory=factory,
            )

            self.assertEqual(result["entries"][0]["inspectionIssue"], "content_length_invalid")
            self.assertEqual(result["entries"][0]["status"], 200)
            self.assertEqual(result["issueCounts"], {"content_length_invalid": 1})
            self.assertEqual(len(factory.requests), 3)

    def test_network_flag_is_required_before_dns_or_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = self._receipt(root, self._three_urls())
            output = root / "headers.json"
            resolver_calls = 0

            def resolver(_host: str, _port: int) -> list[str]:
                nonlocal resolver_calls
                resolver_calls += 1
                raise AssertionError("DNS must not run without --allow-network")

            with self.assertRaisesRegex(inspector.InspectorError, "network_not_authorized"):
                inspector.inspect_receipt(receipt, output, resolver=resolver)
            self.assertEqual(resolver_calls, 0)
            self.assertFalse(output.exists())

    def test_private_dns_fails_before_connection_and_unsafe_url_before_dns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            urls = self._three_urls()
            receipt = self._receipt(root, urls)
            output = root / "private.json"
            factory = FakeConnectionFactory(self._responses(urls))
            result = inspector.inspect_receipt(
                receipt,
                output,
                allow_network=True,
                resolver=lambda _host, _port: ["10.0.0.1"],
                connection_factory=factory,
            )
            self.assertEqual(result["issueCounts"], {"private_target": 3})
            self.assertEqual(factory.requests, [])

            unsafe_receipt = self._receipt(root, ["https://example.com/files/a.bin", *urls[1:]])
            with self.assertRaisesRegex(inspector.InspectorError, "asset_host_blocked"):
                inspector.inspect_receipt(
                    unsafe_receipt,
                    root / "unsafe.json",
                    allow_network=True,
                    resolver=lambda *_args: (_ for _ in ()).throw(AssertionError("DNS must not run")),
                    connection_factory=factory,
                )
            self.assertFalse((root / "unsafe.json").exists())

    def test_cli_requires_explicit_allow_network_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = self._receipt(root, self._three_urls())
            self.assertEqual(
                inspector.main(["--receipt", str(receipt), "--outputJSON", str(root / "headers.json")]),
                1,
            )


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Inspect failed attachment headers without downloading attachment bodies.

The input is a downloader receipt produced from the reviewed v1 seed.  Only
receipt entries carrying an explicitly allowlisted failure issue are selected.
The inspector sends at most one HEAD request per selected URL, retries one
transient failure, and never follows a redirect or reads a response body.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
import json
import os
from http.client import HTTPException
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# ``-I`` intentionally omits the script directory from ``sys.path``.  This is
# the explicitly reviewed sibling module, not an ambient import location.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from download_assets import (
    FORMAT as SNAPSHOT_FORMAT,
    MAX_ASSET_BYTES,
    MAX_RECEIPT_BYTES,
    TRANSIENT_HTTP,
    SnapshotError,
    TransportFailure,
    _PinnedHTTPSConnection,
    _default_resolver,
    _is_inline_url,
    _public_addresses,
    canonical_asset_url,
)


OUTPUT_FORMAT = "kontur-asset-header-inspection-v1"
EXPECTED_SEED_SHA256 = "a18b55186b42e6d11142349f92bbb83ff34aff356d2758067d9f26e53392b051"
ALLOWED_ISSUES = frozenset({
    "asset_too_large", "http_error", "network_error", "incomplete_response", "transient_http",
})
DEFAULT_ISSUES = frozenset({"asset_too_large"})
MAX_URLS = 8
MAX_ENTRIES = MAX_URLS
MAX_WORKERS = 3
HEAD_TIMEOUT_SECONDS = 15.0
MAX_RETRIES = 1
MAX_HEADER_COUNT = 128
MAX_HEADER_VALUE_LENGTH = 8 * 1024
MAX_SAFE_HEADER_INTEGER = 2**63 - 1
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:\-]{0,254}\Z")
DECIMAL_RE = re.compile(r"[0-9]+\Z")

Resolver = Callable[[str, int], list[str]]
ConnectionFactory = Callable[[tuple[str, ...], float], Any]


class InspectorError(Exception):
    """Fixed safe error code for CLI and receipt-validation failures."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_issues(value: str | Iterable[str] | None) -> frozenset[str]:
    if value is None:
        return DEFAULT_ISSUES
    if isinstance(value, str):
        values = value.split(",")
    else:
        values = list(value)
    if any(not isinstance(item, str) for item in values):
        raise InspectorError("issue_allowlist_invalid")
    normalized = [item.strip() for item in values]
    if not normalized or any(not item or item not in ALLOWED_ISSUES for item in normalized):
        raise InspectorError("issue_allowlist_invalid")
    selected = frozenset(normalized)
    if not selected:
        raise InspectorError("issue_allowlist_invalid")
    return selected


def _read_receipt(path: Path, issue_allowlist: frozenset[str] = DEFAULT_ISSUES) -> list[dict[str, Any]]:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RECEIPT_BYTES:
            raise InspectorError("receipt_invalid")
        with path.open("rb") as handle:
            encoded = handle.read(MAX_RECEIPT_BYTES + 1)
        if len(encoded) > MAX_RECEIPT_BYTES:
            raise InspectorError("receipt_invalid")
        raw = json.loads(encoded)
    except InspectorError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise InspectorError("receipt_invalid") from None
    if (
        not isinstance(raw, dict)
        or raw.get("format") != SNAPSHOT_FORMAT
        or raw.get("seedSha256") != EXPECTED_SEED_SHA256
        or not isinstance(raw.get("entries"), list)
    ):
        raise InspectorError("receipt_invalid")

    selected: list[dict[str, Any]] = []
    for item in raw["entries"]:
        issue = item.get("issue") if isinstance(item, dict) else None
        if not isinstance(issue, str) or issue not in issue_allowlist:
            continue
        asset_ids = item.get("assetIds")
        if (
            not isinstance(asset_ids, list)
            or not asset_ids
            or any(not isinstance(asset_id, str) or ID_RE.fullmatch(asset_id) is None for asset_id in asset_ids)
        ):
            raise InspectorError("receipt_entry_invalid")
        if len(asset_ids) != len(set(asset_ids)):
            raise InspectorError("receipt_entry_invalid")
        try:
            url = canonical_asset_url(item.get("url"))
        except SnapshotError as exc:
            raise InspectorError(exc.code) from None
        except (TypeError, ValueError):
            raise InspectorError("receipt_url_invalid") from None
        if _is_inline_url(url):
            raise InspectorError("inline_bytes_missing")
        selected.append({
            "assetIds": sorted(asset_ids),
            "url": url,
            "receiptIssue": issue,
        })

    if not 0 < len(selected) <= MAX_URLS:
        raise InspectorError("receipt_entry_count_invalid")
    urls = [item["url"] for item in selected]
    if len(set(urls)) != len(urls):
        raise InspectorError("receipt_duplicate_url")
    return selected


def _target_for_head(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit(("", "", parsed.path, parsed.query, ""))


def _header_pairs(response: Any) -> list[tuple[str, str]]:
    try:
        pairs = response.getheaders()
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise InspectorError("headers_invalid") from exc
    if isinstance(pairs, dict):
        pairs = list(pairs.items())
    if not isinstance(pairs, Iterable):
        raise InspectorError("headers_invalid")
    result: list[tuple[str, str]] = []
    try:
        for index, pair in enumerate(pairs):
            if index >= MAX_HEADER_COUNT or not isinstance(pair, (tuple, list)) or len(pair) != 2:
                raise InspectorError("headers_invalid")
            name, value = pair
            if (
                not isinstance(name, str)
                or not name
                or len(name) > 256
                or not isinstance(value, str)
                or len(value) > MAX_HEADER_VALUE_LENGTH
            ):
                raise InspectorError("headers_invalid")
            result.append((name, value))
    except InspectorError:
        raise
    except (TypeError, ValueError):
        raise InspectorError("headers_invalid") from None
    return result


def _header_values(pairs: Iterable[tuple[str, str]], name: str) -> list[str]:
    wanted = name.casefold()
    return [value for header, value in pairs if header.casefold() == wanted]


def _content_length(pairs: Iterable[tuple[str, str]]) -> int | None:
    values = _header_values(pairs, "content-length")
    if not values:
        return None
    parsed: list[int] = []
    for value in values:
        pieces = value.split(",")
        if not pieces:
            raise InspectorError("content_length_invalid")
        for piece in pieces:
            token = piece.strip()
            if not DECIMAL_RE.fullmatch(token) or len(token) > 20:
                raise InspectorError("content_length_invalid")
            number = int(token)
            if number > MAX_SAFE_HEADER_INTEGER:
                raise InspectorError("content_length_invalid")
            parsed.append(number)
    if not parsed or len(set(parsed)) != 1:
        raise InspectorError("content_length_invalid")
    return parsed[0]


def _content_type(pairs: Iterable[tuple[str, str]]) -> str | None:
    values = _header_values(pairs, "content-type")
    if not values:
        return None
    normalized = [value.split(";", 1)[0].strip().lower() for value in values]
    if any(not value or len(value) > 255 for value in normalized) or len(set(normalized)) != 1:
        raise InspectorError("content_type_invalid")
    return normalized[0]


def _close_safely(value: Any) -> None:
    close = getattr(value, "close", None)
    if callable(close):
        try:
            close()
        except (OSError, HTTPException):
            pass


def _head_one(
    item: dict[str, Any],
    *,
    resolver: Resolver,
    connection_factory: ConnectionFactory,
) -> dict[str, Any]:
    url = item["url"]
    parsed = urlsplit(url)
    target = _target_for_head(url)
    headers = {
        "Accept": "*/*",
        "User-Agent": "EGE-Informatics-Offline-Asset-Headers/1",
    }
    last_status: int | None = None
    last_length: int | None = None
    last_type: str | None = None
    attempts = 0
    inspection_issue: str | None = None

    while attempts <= MAX_RETRIES:
        attempts += 1
        connection = None
        response = None
        try:
            # Resolver and public-address validation deliberately happen for
            # every retry.  The numeric result is passed to the pinned TLS
            # connection; no hostname resolver or ambient proxy is involved.
            addresses = _public_addresses(resolver(parsed.hostname or "", 443))
            connection = connection_factory(addresses, HEAD_TIMEOUT_SECONDS)
            connection.request("HEAD", target, headers=headers)
            response = connection.getresponse()
            status = response.status
            if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
                raise InspectorError("status_invalid")
            last_status = status
            pairs = _header_pairs(response)
            content_length = _content_length(pairs)
            content_type = _content_type(pairs)
            last_status, last_length, last_type = status, content_length, content_type
            if status in TRANSIENT_HTTP:
                if attempts <= MAX_RETRIES:
                    continue
                inspection_issue = "transient_http"
                break
            if status in {301, 302, 303, 307, 308}:
                # Location is intentionally not consumed or followed.  The
                # status remains in the report as evidence of the fixed stop.
                inspection_issue = "redirect_not_followed"
                break
            if status != 200:
                inspection_issue = "http_error"
                break
            if content_length is None:
                inspection_issue = "content_length_missing"
            break
        except InspectorError as exc:
            inspection_issue = exc.code
            break
        except SnapshotError as exc:
            inspection_issue = exc.code
            break
        except TransportFailure as exc:
            if exc.transient and attempts <= MAX_RETRIES:
                continue
            inspection_issue = exc.code
            break
        except (OSError, HTTPException, TimeoutError):
            if attempts <= MAX_RETRIES:
                continue
            inspection_issue = "network_error"
            break
        except Exception:
            # Never expose transport/library details in the JSON report.
            inspection_issue = "network_error"
            break
        finally:
            _close_safely(response)
            _close_safely(connection)

    result: dict[str, Any] = {
        "assetIds": item["assetIds"],
        "url": url,
        "receiptIssue": item["receiptIssue"],
        "status": last_status,
        "contentLength": last_length,
        "contentType": last_type,
        "attempts": attempts,
        "checkedAt": _utc_now(),
    }
    if inspection_issue is not None:
        result["inspectionIssue"] = inspection_issue
    return result


def _write_new_json(path: Path, payload: dict[str, Any]) -> None:
    if path.exists() or path.is_symlink() or not path.parent.is_dir():
        raise InspectorError("output_invalid")
    encoded = (_canonical(payload) + "\n").encode("utf-8")
    fd: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise InspectorError("output_exists") from None
    except (OSError, ValueError):
        raise InspectorError("output_invalid") from None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def inspect_receipt(
    receipt_path: str | Path,
    output_json: str | Path,
    *,
    allow_network: bool = False,
    issues: str | Iterable[str] | None = None,
    resolver: Resolver | None = None,
    connection_factory: ConnectionFactory | None = None,
) -> dict[str, Any]:
    """HEAD-check allowlisted failed entries in a reviewed receipt."""

    if allow_network is not True:
        raise InspectorError("network_not_authorized")
    issue_allowlist = _parse_issues(issues)
    selected = _read_receipt(Path(receipt_path), issue_allowlist)
    resolver = resolver or _default_resolver
    connection_factory = connection_factory or _PinnedHTTPSConnection
    observations = [
        _head_one(item, resolver=resolver, connection_factory=connection_factory)
        for item in selected
    ]
    issue_counts = Counter(
        item["inspectionIssue"] for item in observations if item.get("inspectionIssue") is not None
    )
    result = {
        "format": OUTPUT_FORMAT,
        "createdAt": _utc_now(),
        "seedSha256": EXPECTED_SEED_SHA256,
        "issues": sorted(issue_allowlist),
        "limits": {
            "entries": MAX_URLS,
            "workers": MAX_WORKERS,
            "timeoutSeconds": HEAD_TIMEOUT_SECONDS,
            "maxRetries": MAX_RETRIES,
        },
        "counts": {
            "entries": len(observations),
            "responses": sum(item["status"] is not None for item in observations),
            "failed": sum(item.get("inspectionIssue") is not None for item in observations),
        },
        "issueCounts": dict(sorted(issue_counts.items())),
        "complete": not issue_counts,
        "entries": observations,
    }
    _write_new_json(Path(output_json), result)
    return result


run_inspection = inspect_receipt
inspect_asset_headers = inspect_receipt


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise InspectorError("arguments_invalid")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("--allow-network", action="store_true", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--issues", default="asset_too_large")
    parser.add_argument(
        "--outputJSON", "--output-json", "--output", dest="output_json", type=Path, required=True
    )
    try:
        args = parser.parse_args(argv)
        result = inspect_receipt(args.receipt, args.output_json, allow_network=args.allow_network, issues=args.issues)
    except InspectorError as exc:
        print(_canonical({"format": OUTPUT_FORMAT, "status": "ERROR", "error": exc.code}))
        return 1
    print(_canonical({"format": OUTPUT_FORMAT, "status": "COMPLETE" if result["complete"] else "INCOMPLETE", "counts": result["counts"], "issueCounts": result["issueCounts"]}))
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

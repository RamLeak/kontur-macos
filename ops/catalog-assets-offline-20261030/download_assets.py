#!/usr/bin/env python3
"""Bounded offline snapshot of admitted catalog attachment bytes.

The input is the sanitized v1 catalog seed.  This utility deliberately does
not import the application, execute SQL from the input, or inspect task text.
It copies already embedded bytes from ``catalog_asset_bytes`` and fetches only
missing, admitted, direct public asset URLs.  All network collaborators are
injectable so the test suite remains fully synthetic.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import mimetypes
import os
import posixpath
import re
import socket
import sqlite3
import ssl
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.client import HTTPException, HTTPSConnection
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import URLError
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

FORMAT = "kontur-asset-snapshot-v1"
SOURCE_NAME = "kompege"
SOURCE_HOST = "kompege.ru"
ALLOWED_HOSTS = frozenset({"kompege.ru", "www.kompege.ru"})
ADMITTED_RIGHTS = frozenset({"private_authorized", "public_authorized"})
# HEAD verified genuine text attachments up to 50,084,031 bytes.
MAX_ASSET_BYTES = 64_000_000
MAX_TOTAL_ASSET_BYTES = 2 * 1024**3
# The default stays 2 GiB. A larger reviewed run must explicitly name its
# combined existing-byte/network budget; no body is allocated from this value.
MAX_EXPLICIT_TOTAL_BYTES = 8 * 1024**3
MAX_URL_LENGTH = 2_048
MAX_RECEIPT_BYTES = 16 * 1024**2
MAX_REDIRECTS = 3
MAX_RETRIES = 1
MAX_WORKERS = 3
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MIN_INTERVAL_SECONDS = 0.10
TRANSIENT_HTTP = frozenset({408, 429, 500, 502, 503, 504})
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:\-]{0,254}\Z")
INLINE_RE = re.compile(
    r"https://kompege\.ru/api/v1/task/number/([1-9]|1[0-9]|2[0-7])"
    r"#inline-image-v1=([0-9]{1,18}):([0-9]|1[0-9]):([a-f0-9]{64})\.(png|jpg|gif)\Z"
)

SEED_TABLES = {
    "catalog_metadata",
    "catalog_tasks",
    "catalog_revisions",
    "catalog_assets",
    "catalog_links",
    "catalog_selection",
    "catalog_asset_bytes",
}
SEED_COLUMNS = {
    "catalog_metadata": {"key", "value"},
    "catalog_assets": {
        "id",
        "record_json",
    },
    "catalog_asset_bytes": {"asset_id", "sha256", "bytes"},
}


class SnapshotError(Exception):
    """Fixed safe error code; values and paths never escape this exception."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class TransportFailure(Exception):
    """Internal transport failure with a fixed code and transient marker."""

    def __init__(self, code: str, *, transient: bool):
        self.code = code
        self.transient = transient
        super().__init__(code)


@dataclass(frozen=True)
class SourceResponse:
    status: int
    url: str
    headers: dict[str, str]
    body: bytes


Transport = Callable[[str, dict[str, str], int, float], SourceResponse]
Resolver = Callable[[str, int], list[str]]


@dataclass(frozen=True)
class AssetRecord:
    asset_id: str
    url: str
    filename: str
    media_type: str
    expected_hash: str | None
    cache_state: str
    hash_verified: bool
    content_hash: str | None
    size_bytes: int
    rights_status: str
    source_name: str
    normalization: dict[str, Any] | None = None


@dataclass(frozen=True)
class SeedBytes:
    asset_id: str
    sha256: str
    size_bytes: int


@dataclass
class UrlEntry:
    url: str
    records: list[AssetRecord] = field(default_factory=list)
    status: str = "pending"
    issue: str | None = None
    relative_path: str | None = None
    sha256: str | None = None
    size_bytes: int = 0
    detected_type: str | None = None
    response_content_type: str | None = None
    source: str | None = None

    @property
    def asset_ids(self) -> list[str]:
        return sorted(record.asset_id for record in self.records)

    @property
    def expected_hash(self) -> str | None:
        values = {record.expected_hash for record in self.records if record.expected_hash}
        return next(iter(values)) if len(values) == 1 else None

    @property
    def expected_hashes(self) -> list[str]:
        return sorted({record.expected_hash for record in self.records if record.expected_hash})

    @property
    def filename(self) -> str:
        return self.records[0].filename if self.records else "attachment.bin"

    @property
    def media_types(self) -> list[str]:
        return sorted({record.media_type for record in self.records if record.media_type})

    @property
    def validation_record(self) -> AssetRecord:
        # A duplicate URL may have one legacy generic MIME row and one row
        # carrying the pinned digest. Prefer the pinned/specific row for the
        # single fetch; generic octet-stream is not evidence against a more
        # precise admitted declaration.
        return next((record for record in self.records if record.expected_hash), self.records[0])

    def manifest_record(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "url": self.url,
            "assetIds": self.asset_ids,
            "filenames": sorted({record.filename for record in self.records}),
            "mediaTypes": self.media_types,
            "expectedContentHash": self.expected_hash,
            "status": self.status,
        }
        if len(self.expected_hashes) > 1:
            result["expectedContentHashes"] = self.expected_hashes
        if self.relative_path is not None:
            result["path"] = self.relative_path
        if self.sha256 is not None:
            result["sha256"] = self.sha256
            result["sizeBytes"] = self.size_bytes
        if self.detected_type is not None:
            result["detectedType"] = self.detected_type
        if self.response_content_type is not None:
            result["responseContentType"] = self.response_content_type
        if self.source is not None:
            result["source"] = self.source
        if self.issue is not None:
            result["issue"] = self.issue
        return result


@dataclass(frozen=True)
class SeedData:
    assets: list[AssetRecord]
    bytes_by_asset: dict[str, SeedBytes]
    metadata: dict[str, Any]
    file_sha256: str


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path, *, maximum: int | None = None) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if maximum is not None and total > maximum:
                raise SnapshotError("asset_too_large")
            digest.update(chunk)
    return digest.hexdigest(), total


def _require_hash(value: object, code: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise SnapshotError(code)
    return value


def _safe_filename(value: object) -> str:
    if not isinstance(value, str) or len(value) > 255:
        raise SnapshotError("asset_filename_invalid")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip(".")
    if not cleaned or cleaned in {".", ".."}:
        raise SnapshotError("asset_filename_invalid")
    return cleaned[:255]


def _normalise_media_type(value: object) -> str:
    if value is None:
        return "application/octet-stream"
    if not isinstance(value, str) or len(value) > 255:
        raise SnapshotError("asset_media_type_invalid")
    return value.split(";", 1)[0].strip().lower() or "application/octet-stream"


def _legacy_hash_normalization(record: dict[str, Any]) -> dict[str, Any] | None:
    """Recognize only the old importer's exact immutable metadata fingerprint."""
    url = record.get("canonical_url")
    filename = record.get("filename")
    expected = record.get("expected_content_hash")
    if not isinstance(url, str) or not isinstance(filename, str) or not isinstance(expected, str):
        return None
    sentinel = _sha256_bytes(f"metadata:{url}:{filename}".encode())
    if (
        record.get("source_name") != SOURCE_NAME or record.get("rights_status") not in ADMITTED_RIGHTS
        or expected != sentinel or record.get("content_hash") != sentinel
        or type(record.get("size_bytes")) is not int or record["size_bytes"] != 0
        or record.get("hash_verified") is not False or "detected_type" not in record
        or record["detected_type"] is not None or record.get("cache_state") not in {"metadata_only", "failed"}
    ):
        return None
    return {
        "assetId": record["id"],
        "canonicalUrl": url,
        "filename": filename,
        "originalExpectedContentHash": expected,
        "normalizedExpectedContentHash": None,
        "fingerprint": {
            "canonicalUrl": url,
            "filename": filename,
            "cacheState": record["cache_state"],
            "sizeBytes": 0,
            "contentHash": record["content_hash"],
            "expectedContentHash": expected,
            "hashVerified": False,
            "detectedType": None,
        },
    }


def _validate_path_encoding(path: str) -> None:
    decoded_path = path
    for _ in range(8):
        if "\\" in decoded_path or any(ord(character) < 32 or ord(character) == 127 for character in decoded_path):
            raise SnapshotError("asset_path_invalid")
        if any(part in {".", ".."} for part in decoded_path.split("/")):
            raise SnapshotError("asset_path_invalid")
        try:
            decoded = unquote(decoded_path, errors="strict")
        except UnicodeError as exc:
            raise SnapshotError("asset_path_invalid") from exc
        if decoded == decoded_path:
            break
        # Encoded separators change the path structure seen by the server.
        if decoded.count("/") != decoded_path.count("/"):
            raise SnapshotError("asset_path_invalid")
        decoded_path = decoded
    else:
        raise SnapshotError("asset_path_invalid")


def _validate_raw_url(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_URL_LENGTH:
        raise SnapshotError("asset_url_invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in value) or "\\" in value:
        raise SnapshotError("asset_path_invalid")
    return value


def canonical_asset_url(raw_url: object) -> str:
    """Canonicalize a direct public attachment or a pinned inline locator."""
    raw_url = _validate_raw_url(raw_url)
    if INLINE_RE.fullmatch(raw_url.strip()):
        return raw_url.strip()
    if "#" in raw_url:
        raise SnapshotError("asset_url_fragment_blocked")
    try:
        parsed = urlsplit(raw_url.strip())
        port = parsed.port
    except ValueError as exc:
        raise SnapshotError("asset_url_invalid") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https" or host not in ALLOWED_HOSTS:
        raise SnapshotError("asset_host_blocked")
    if parsed.username is not None or parsed.password is not None or port is not None or ":" in parsed.netloc:
        raise SnapshotError("asset_url_credentials_or_port")
    _validate_path_encoding(parsed.path)
    path = posixpath.normpath(parsed.path or "/")
    if not path.startswith("/") or ".." in path.split("/"):
        raise SnapshotError("asset_path_invalid")
    if not ((path.startswith("/files/") and path != "/files/") or (path.startswith("/images/") and path != "/images/")):
        raise SnapshotError("asset_path_blocked")
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit(("https", SOURCE_HOST, path, query, ""))


def _is_inline_url(url: str) -> bool:
    return INLINE_RE.fullmatch(url) is not None


def _detect_type(data: bytes, filename: str, declared: str | None) -> str:
    declared_type = (declared or "").split(";", 1)[0].strip().lower()
    encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    prefix = data.decode(encoding, errors="replace").lstrip()[:8192].lower()
    if declared_type in {"text/html", "application/xhtml+xml"} or re.search(
        r"<\s*(?:!doctype\s+html\b|html\b|head\b|body\b|script\b)", prefix
    ):
        raise SnapshotError("asset_html_blocked")
    if data.startswith(b"%PDF-"):
        detected = "application/pdf"
    elif data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        detected = "application/zip"
    elif data.startswith(b"\xd0\xcf\x11\xe0"):
        detected = "application/x-ole-storage"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = "image/png"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        detected = "image/gif"
    elif data.startswith(b"\xff\xd8\xff"):
        detected = "image/jpeg"
    else:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            detected = "application/octet-stream"
        else:
            guessed = "text/csv" if Path(filename).suffix.lower() == ".csv" else mimetypes.guess_type(filename)[0]
            detected = guessed if guessed and guessed.startswith("text/") else "text/plain"
    if declared_type and declared_type != "application/octet-stream":
        zip_family = detected == "application/zip" and (
            declared_type == "application/zip" or declared_type.startswith("application/vnd.")
        )
        ole_family = detected == "application/x-ole-storage" and (
            declared_type == "application/x-ole-storage" or declared_type.startswith("application/vnd.")
        )
        if declared_type != detected and not zip_family and not ole_family:
            raise SnapshotError("asset_type_mismatch")
    return detected


def _response_content_type(headers: dict[str, str]) -> str | None:
    for key, value in headers.items():
        if key.casefold() == "content-type":
            if not isinstance(value, str):
                return None
            value = value.split(";", 1)[0].strip().lower()
            return value or None
    return None


def _compatible_response_type(response_type: str, detected: str) -> bool:
    if response_type in {"text/html", "application/xhtml+xml"}:
        return False
    if response_type in {"", "application/octet-stream"}:
        return True
    if response_type == detected:
        return True
    if detected == "application/zip" and response_type.startswith("application/vnd."):
        return True
    if detected == "application/x-ole-storage" and response_type.startswith("application/vnd."):
        return True
    return response_type.startswith("text/") and detected.startswith("text/")


def _validate_bytes(
    data: bytes,
    record: AssetRecord,
    *,
    response_content_type: str | None,
) -> tuple[str, str]:
    if not isinstance(data, bytes) or len(data) == 0:
        raise SnapshotError("asset_empty")
    if len(data) > MAX_ASSET_BYTES:
        raise SnapshotError("asset_too_large")
    detected = _detect_type(data, record.filename, record.media_type)
    if response_content_type and not _compatible_response_type(response_content_type, detected):
        raise SnapshotError("response_content_type_mismatch")
    digest = _sha256_bytes(data)
    if record.expected_hash is not None and digest != record.expected_hash:
        raise SnapshotError("asset_hash_mismatch")
    return digest, detected


def _validate_entry_bytes(data: bytes, entry: UrlEntry, content_type: str | None) -> tuple[str, str]:
    # A URL shared by multiple rows must satisfy every pinned digest and MIME
    # declaration, regardless of which row originally supplied its bytes.
    result = None
    for record in entry.records:
        checked = _validate_bytes(data, record, response_content_type=content_type)
        if result is None or record.media_type != "application/octet-stream":
            result = checked
    if result is None:
        raise SnapshotError("seed_asset_record_invalid")
    return result


def _set_readonly_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA query_only=ON")
    try:
        connection.execute("PRAGMA trusted_schema=OFF")
    except sqlite3.DatabaseError as exc:
        raise SnapshotError("seed_schema_invalid") from exc
    try:
        connection.enable_load_extension(False)
    except (AttributeError, sqlite3.DatabaseError):
        pass


def _validate_seed_schema(connection: sqlite3.Connection) -> None:
    try:
        objects = list(
            connection.execute(
                "SELECT type,name FROM sqlite_master "
                "WHERE type IN ('table','view','trigger') ORDER BY type,name"
            )
        )
    except sqlite3.DatabaseError as exc:
        raise SnapshotError("seed_schema_invalid") from exc
    tables = {name for kind, name in objects if kind == "table"}
    if tables != SEED_TABLES or any(kind != "table" for kind, _ in objects):
        raise SnapshotError("seed_schema_invalid")
    for table, expected in SEED_COLUMNS.items():
        try:
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        except sqlite3.DatabaseError as exc:
            raise SnapshotError("seed_schema_invalid") from exc
        if columns != expected:
            raise SnapshotError("seed_schema_invalid")


def _read_seed(path: Path) -> SeedData:
    try:
        # Do not expand user-profile/environment paths: the operator supplies
        # the explicit seed path and this tool must not inspect ambient profile
        # state.
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SnapshotError("seed_unavailable") from exc
    if not resolved.is_file():
        raise SnapshotError("seed_unavailable")
    try:
        file_sha256, _ = _sha256_file(resolved)
    except OSError as exc:
        raise SnapshotError("seed_unavailable") from exc
    try:
        uri = resolved.as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    except (OSError, sqlite3.DatabaseError) as exc:
        raise SnapshotError("seed_open_failed") from exc
    try:
        _set_readonly_connection(connection)
        connection.execute("BEGIN")
        _validate_seed_schema(connection)
        metadata: dict[str, Any] = {}
        for key, raw_value in connection.execute("SELECT key,value FROM catalog_metadata ORDER BY key"):
            if not isinstance(key, str) or not isinstance(raw_value, str):
                raise SnapshotError("seed_metadata_invalid")
            try:
                metadata[key] = json.loads(raw_value)
            except (TypeError, json.JSONDecodeError) as exc:
                raise SnapshotError("seed_metadata_invalid") from exc
        if metadata.get("format") != "kontur-catalog-seed-v1" or metadata.get("schemaVersion") != 1:
            raise SnapshotError("seed_metadata_invalid")
        assets: list[AssetRecord] = []
        for asset_id, raw_record in connection.execute(
            "SELECT id,record_json FROM catalog_assets ORDER BY id"
        ):
            if not isinstance(asset_id, str) or ID_RE.fullmatch(asset_id) is None or not isinstance(raw_record, str):
                raise SnapshotError("seed_asset_record_invalid")
            try:
                record = json.loads(raw_record)
            except (TypeError, json.JSONDecodeError) as exc:
                raise SnapshotError("seed_asset_record_invalid") from exc
            if not isinstance(record, dict) or record.get("id") != asset_id:
                raise SnapshotError("seed_asset_record_invalid")
            expected = record.get("expected_content_hash")
            if expected is not None:
                expected = _require_hash(expected, "seed_asset_hash_invalid")
            content_hash = record.get("content_hash")
            if content_hash is not None and (not isinstance(content_hash, str) or len(content_hash) > 128):
                raise SnapshotError("seed_asset_hash_invalid")
            size_bytes = record.get("size_bytes", 0)
            if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0:
                raise SnapshotError("seed_asset_size_invalid")
            normalization = _legacy_hash_normalization(record)
            if normalization is not None:
                expected = None
            assets.append(
                AssetRecord(
                    asset_id=asset_id,
                    url=canonical_asset_url(record.get("canonical_url")),
                    filename=_safe_filename(record.get("filename")),
                    media_type=_normalise_media_type(record.get("media_type")),
                    expected_hash=expected,
                    cache_state=record.get("cache_state") if isinstance(record.get("cache_state"), str) else "",
                    hash_verified=record.get("hash_verified") is True,
                    content_hash=content_hash,
                    size_bytes=size_bytes,
                    rights_status=record.get("rights_status") if isinstance(record.get("rights_status"), str) else "",
                    source_name=record.get("source_name") if isinstance(record.get("source_name"), str) else "",
                    normalization=normalization,
                )
            )
        bytes_by_asset: dict[str, SeedBytes] = {}
        for asset_id, raw_sha, raw_length in connection.execute(
            "SELECT asset_id,sha256,length(bytes) FROM catalog_asset_bytes ORDER BY asset_id"
        ):
            if not isinstance(asset_id, str) or asset_id in bytes_by_asset:
                raise SnapshotError("seed_bytes_invalid")
            digest = _require_hash(raw_sha, "seed_bytes_invalid")
            if not isinstance(raw_length, int) or raw_length < 0 or raw_length > MAX_ASSET_BYTES:
                raise SnapshotError("seed_bytes_invalid")
            # Keep only metadata here; validate/copy one bounded BLOB at a time.
            bytes_by_asset[asset_id] = SeedBytes(asset_id, digest, raw_length)
        return SeedData(assets=assets, bytes_by_asset=bytes_by_asset, metadata=metadata, file_sha256=file_sha256)
    except sqlite3.DatabaseError as exc:
        raise SnapshotError("seed_read_failed") from exc
    finally:
        try:
            connection.rollback()
        except sqlite3.DatabaseError:
            pass
        connection.close()


def _default_resolver(host: str, port: int) -> list[str]:
    try:
        values = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise TransportFailure("dns_failed", transient=True) from exc
    return [str(item[4][0]) for item in values]


def _public_addresses(addresses: Iterable[str]) -> tuple[str, ...]:
    checked: list[str] = []
    for raw_address in addresses:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as exc:
            raise SnapshotError("dns_invalid") from exc
        if not address.is_global:
            raise SnapshotError("private_target")
        value = str(address)
        if value not in checked:
            checked.append(value)
    if not checked:
        raise TransportFailure("dns_failed", transient=True)
    return tuple(checked)


class _PinnedHTTPSConnection(HTTPSConnection):
    """Connect to a vetted numeric address, with the original TLS hostname."""

    def __init__(self, addresses: tuple[str, ...], timeout: float):
        super().__init__(SOURCE_HOST, 443, timeout=timeout, context=ssl.create_default_context())
        self.addresses = _public_addresses(addresses)

    def _check_peer(self, connection: socket.socket, selected: str) -> None:
        try:
            peer = str(ipaddress.ip_address(connection.getpeername()[0]))
        except (OSError, ValueError) as exc:
            raise SnapshotError("peer_invalid") from exc
        if peer != selected or peer not in self.addresses or not ipaddress.ip_address(peer).is_global:
            raise SnapshotError("peer_mismatch")

    def connect(self) -> None:
        selected = self.addresses[0]
        family = socket.AF_INET6 if ":" in selected else socket.AF_INET
        raw = socket.socket(family, socket.SOCK_STREAM)
        try:
            raw.settimeout(self.timeout)
            raw.connect((selected, 443))
            self._check_peer(raw, selected)
            secured = self._context.wrap_socket(raw, server_hostname=SOURCE_HOST)
            try:
                self._check_peer(secured, selected)
            except Exception:
                secured.close()
                raise
            self.sock = secured
        except Exception:
            raw.close()
            raise


def _default_transport(
    url: str, headers: dict[str, str], maximum: int, timeout: float, *, addresses: tuple[str, ...]
) -> SourceResponse:
    # HTTPSConnection uses neither ambient proxies nor cookie/auth handlers.
    # The TCP connection uses the same DNS result that passed validation.
    connection = _PinnedHTTPSConnection(addresses, timeout)
    parsed = urlsplit(url)
    target = urlunsplit(("", "", parsed.path, parsed.query, ""))
    try:
        connection.request("GET", target, headers=headers)
        with connection.getresponse() as response:
            response_headers = {str(key): str(value) for key, value in response.getheaders()}
            # Error and redirect bodies are not attachment data and are never retained.
            body = response.read(maximum) if response.status == 200 else b""
            if response.status == 200 and len(body) == maximum and not response.isclosed():
                code = "asset_too_large" if maximum == MAX_ASSET_BYTES else "total_asset_budget_exceeded"
                raise TransportFailure(code, transient=False)
            if response.status == 200 and response.length is not None and response.length > 0:
                raise TransportFailure("incomplete_response", transient=True)
            return SourceResponse(response.status, url, response_headers, body)
    except ssl.SSLCertVerificationError as exc:
        raise TransportFailure("tls_verification_failed", transient=False) from exc
    except (OSError, HTTPException) as exc:
        raise TransportFailure("network_error", transient=True) from exc
    finally:
        connection.close()


class _RateLimiter:
    def __init__(self, interval: float):
        self.interval = max(0.0, interval)
        self.lock = threading.Lock()
        self.last_at = 0.0

    def wait(self) -> None:
        if self.interval <= 0:
            return
        with self.lock:
            remaining = self.interval - (time.monotonic() - self.last_at)
            if remaining > 0:
                time.sleep(remaining)
            self.last_at = time.monotonic()


class _ByteBudget:
    """Reserve before a request so concurrent bodies share one hard ceiling."""

    def __init__(self, limit: int):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= MAX_EXPLICIT_TOTAL_BYTES:
            raise SnapshotError("total_asset_budget_invalid")
        self.limit = limit
        self.used = 0
        self.reserved = 0
        self.network_bytes = 0
        self.paths: set[str] = set()
        self.condition = threading.Condition()

    def admit_existing(self, path: str, size: int) -> None:
        with self.condition:
            if path in self.paths:
                return
            if self.used + size > self.limit:
                raise SnapshotError("total_asset_budget_exceeded")
            self.used += size
            self.paths.add(path)

    def reserve(self) -> int:
        with self.condition:
            while True:
                available = self.limit - self.used - self.reserved
                if available > 0:
                    allowance = min(MAX_ASSET_BYTES, available)
                    self.reserved += allowance
                    return allowance
                if self.reserved == 0:
                    raise SnapshotError("total_asset_budget_exceeded")
                self.condition.wait()

    def settle(self, allowance: int, consumed: int) -> None:
        with self.condition:
            self.reserved -= allowance
            self.used += consumed
            self.network_bytes += consumed
            self.condition.notify_all()


class AssetDownloader:
    """Bounded direct-file fetcher with injectable DNS and transport."""

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        resolver: Resolver | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
        max_redirects: int = MAX_REDIRECTS,
        allow_network: bool = False,
        budget: _ByteBudget | None = None,
    ):
        self.transport = transport
        self.resolver = resolver or _default_resolver
        self.requires_network = transport is None or resolver is None
        self.allow_network = allow_network is True
        self.budget = budget or _ByteBudget(MAX_TOTAL_ASSET_BYTES)
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        # Keep a small positive floor in production/CLI use; tests can still
        # inject transport without contacting the network.
        self.rate_limiter = _RateLimiter(max(0.01, float(min_interval_seconds)))
        self.max_redirects = max(0, min(int(max_redirects), MAX_REDIRECTS))

    def _reject_private_resolution(self, url: str) -> tuple[str, ...]:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        try:
            addresses = self.resolver(host, parsed.port or 443)
        except OSError as exc:
            raise TransportFailure("dns_failed", transient=True) from exc
        return _public_addresses(addresses)

    def fetch(self, entry: UrlEntry) -> tuple[bytes, str, str, str | None]:
        if _is_inline_url(entry.url):
            raise SnapshotError("inline_bytes_missing")
        if self.requires_network and not self.allow_network:
            raise SnapshotError("network_not_authorized")
        current = entry.url
        for hop in range(self.max_redirects + 1):
            current = canonical_asset_url(current)
            for attempt in range(MAX_RETRIES + 1):
                self.rate_limiter.wait()
                try:
                    addresses = self._reject_private_resolution(current)
                    allowance = self.budget.reserve()
                    # A failed/interrupted request may have consumed its full
                    # allowance. Charge conservatively; never reset on retry.
                    consumed = allowance
                    try:
                        headers = {"Accept": "*/*", "User-Agent": "EGE-Informatics-Offline-Snapshot/1"}
                        if self.transport is None:
                            response = _default_transport(
                                current, headers, allowance, self.timeout_seconds, addresses=addresses
                            )
                        else:
                            response = self.transport(current, headers, allowance, self.timeout_seconds)
                        consumed = min(len(response.body), allowance)
                        if len(response.body) > allowance:
                            code = "asset_too_large" if allowance == MAX_ASSET_BYTES else "total_asset_budget_exceeded"
                            raise SnapshotError(code)
                    finally:
                        self.budget.settle(allowance, consumed)
                except TransportFailure as exc:
                    if exc.transient and attempt < MAX_RETRIES:
                        continue
                    raise SnapshotError(exc.code) from exc
                except (OSError, URLError, TimeoutError) as exc:
                    if attempt < MAX_RETRIES:
                        continue
                    raise SnapshotError("network_error") from exc
                if response.url != current:
                    raise SnapshotError("unsafe_redirect")
                if response.status in {301, 302, 303, 307, 308}:
                    location = next(
                        (value for key, value in response.headers.items() if key.casefold() == "location"),
                        "",
                    )
                    if not location or hop >= self.max_redirects:
                        raise SnapshotError("redirect_blocked")
                    try:
                        location = _validate_raw_url(location)
                        if location != location.strip():
                            raise SnapshotError("asset_url_invalid")
                        _validate_path_encoding(urlsplit(location).path)
                        current = canonical_asset_url(urljoin(current, location))
                    except (SnapshotError, ValueError) as exc:
                        raise SnapshotError("unsafe_redirect") from exc
                    break
                if response.status in TRANSIENT_HTTP:
                    if attempt < MAX_RETRIES:
                        continue
                    raise SnapshotError("transient_http")
                if response.status != 200:
                    raise SnapshotError("http_error")
                if len(response.body) > MAX_ASSET_BYTES:
                    raise SnapshotError("asset_too_large")
                content_type = _response_content_type(response.headers)
                digest, detected = _validate_entry_bytes(response.body, entry, content_type)
                return response.body, digest, detected, content_type
            else:
                raise SnapshotError("redirect_blocked")
            if hop == self.max_redirects:
                raise SnapshotError("redirect_blocked")
            continue
        raise SnapshotError("redirect_blocked")


def _safe_relative_path(output: Path, raw: object) -> Path:
    if (
        not isinstance(raw, str) or not raw or "\\" in raw or ":" in raw
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
    ):
        raise SnapshotError("receipt_path_invalid")
    candidate = PurePosixPath(raw)
    if candidate.is_absolute() or ".." in candidate.parts or candidate.parts[0:1] != ("assets",):
        raise SnapshotError("receipt_path_invalid")
    path = output
    try:
        # Walk from the validated root so an unchecked parent junction cannot
        # redirect a deeper filesystem probe. The finite parts also avoid a
        # Windows drive-root ancestor loop on malformed receipt paths.
        for part in candidate.parts:
            path = path / part
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise SnapshotError("receipt_path_invalid")
        resolved = path.resolve(strict=False)
        resolved.relative_to(output.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise SnapshotError("receipt_path_invalid") from exc
    return path


def _safe_existing_asset(path: Path, output: Path, expected_digest: str, expected_size: int) -> bool:
    try:
        if not path.is_file() or path.is_symlink() or os.path.realpath(path) != str(path.resolve()):
            return False
        path.resolve().relative_to(output.resolve())
        digest, size = _sha256_file(path, maximum=MAX_ASSET_BYTES)
    except (OSError, RuntimeError, SnapshotError, ValueError):
        return False
    return digest == expected_digest and size == expected_size


def _asset_relative_path(digest: str) -> str:
    return f"assets/{digest[:2]}/{digest}"


def _write_bytes_atomically(output: Path, relative_path: str, data: bytes) -> None:
    destination = _safe_relative_path(output, relative_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.parent.chmod(0o700)
    except OSError:
        pass
    if destination.exists() and _safe_existing_asset(destination, output, _sha256_bytes(data), len(data)):
        return
    try:
        fd, temporary_name = tempfile.mkstemp(prefix=".asset-", dir=str(destination.parent))
        temporary = Path(temporary_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, destination)
    finally:
        if "temporary" in locals() and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _write_json_atomically(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".receipt-", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _load_receipt(output: Path, seed_sha256: str) -> dict[str, dict[str, Any]]:
    path = output / "receipt.json"
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        if path.stat().st_size > MAX_RECEIPT_BYTES:
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict) or raw.get("format") != FORMAT or raw.get("seedSha256") != seed_sha256:
        return {}
    result: dict[str, dict[str, Any]] = {}
    entries = raw.get("entries")
    if not isinstance(entries, list):
        return {}
    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        if item.get("status") not in {"seed_ready", "downloaded", "resumed"}:
            continue
        digest = item.get("sha256")
        size = item.get("sizeBytes")
        relative = item.get("path")
        if (
            not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None
            or isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_ASSET_BYTES
        ):
            continue
        try:
            _safe_relative_path(output, relative)
        except SnapshotError:
            continue
        if item["url"] in result:
            return {}
        result[item["url"]] = item
    return result


def _safe_output_dir(seed: Path, output: Path) -> Path:
    try:
        seed_resolved = seed.resolve(strict=True)
        output_resolved = output.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise SnapshotError("output_invalid") from exc
    if output.exists() and output.is_symlink():
        raise SnapshotError("output_invalid")
    if output_resolved == seed_resolved or output_resolved == seed_resolved.parent:
        raise SnapshotError("output_must_be_separate")
    if output_resolved.exists() and not output_resolved.is_dir():
        raise SnapshotError("output_invalid")
    output_resolved.mkdir(parents=True, exist_ok=True)
    try:
        output_resolved.chmod(0o700)
    except OSError:
        pass
    return output_resolved


class SnapshotBuilder:
    def __init__(
        self,
        seed_path: Path,
        output_dir: Path,
        *,
        transport: Transport | None = None,
        resolver: Resolver | None = None,
        workers: int = MAX_WORKERS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
        allow_network: bool = False,
        max_total_bytes: int = MAX_TOTAL_ASSET_BYTES,
    ):
        self.seed_path = Path(seed_path)
        self.output_dir = Path(output_dir)
        if workers < 1 or workers > MAX_WORKERS:
            raise SnapshotError("worker_limit")
        self.workers = workers
        self.budget = _ByteBudget(max_total_bytes)
        self.downloader = AssetDownloader(
            transport=transport,
            resolver=resolver,
            timeout_seconds=timeout_seconds,
            min_interval_seconds=min_interval_seconds,
            allow_network=allow_network,
            budget=self.budget,
        )

    def _make_entries(self, assets: Iterable[AssetRecord]) -> tuple[list[UrlEntry], Counter[str]]:
        grouped: dict[str, UrlEntry] = {}
        issues: Counter[str] = Counter()
        for record in assets:
            if record.source_name != SOURCE_NAME or record.rights_status not in ADMITTED_RIGHTS:
                issues["asset_not_admitted"] += 1
                continue
            entry = grouped.setdefault(record.url, UrlEntry(record.url))
            entry.records.append(record)
        for entry in grouped.values():
            expected = {record.expected_hash for record in entry.records if record.expected_hash}
            media_types = {record.media_type for record in entry.records}
            specific_media_types = {media_type for media_type in media_types if media_type != "application/octet-stream"}
            if len(expected) > 1 or len(specific_media_types) > 1:
                entry.status = "failed"
                entry.issue = "asset_url_conflict"
                issues[entry.issue] += 1
        return sorted(grouped.values(), key=lambda item: item.url), issues

    def _apply_seed_bytes(
        self,
        entries: list[UrlEntry],
        seed: SeedData,
        issues: Counter[str],
    ) -> int:
        by_id = {record.asset_id: record for record in seed.assets}
        by_url = {entry.url: entry for entry in entries}
        copied = 0
        for asset_id, seed_bytes in seed.bytes_by_asset.items():
            record = by_id.get(asset_id)
            if record is None:
                issues["orphan_seed_bytes"] += 1
                continue
            if record.rights_status not in ADMITTED_RIGHTS or record.source_name != SOURCE_NAME:
                issues["seed_bytes_not_admitted"] += 1
                continue
            entry = by_url.get(record.url)
            if entry is None or entry.issue:
                continue
            try:
                connection = sqlite3.connect(self.seed_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
                try:
                    _set_readonly_connection(connection)
                    row = connection.execute(
                        "SELECT bytes FROM catalog_asset_bytes WHERE asset_id=? AND length(bytes)=?",
                        (asset_id, seed_bytes.size_bytes),
                    ).fetchone()
                finally:
                    connection.close()
                if row is None or not isinstance(row[0], bytes) or len(row[0]) != seed_bytes.size_bytes:
                    raise SnapshotError("seed_bytes_invalid")
                data = row[0]
                if _sha256_bytes(data) != seed_bytes.sha256:
                    raise SnapshotError("seed_bytes_hash_mismatch")
                digest, detected = _validate_entry_bytes(data, entry, None)
            except (OSError, sqlite3.DatabaseError):
                entry.status = "failed"
                entry.issue = "seed_read_failed"
                issues[entry.issue] += 1
                continue
            except SnapshotError as exc:
                entry.status = "failed"
                entry.issue = exc.code
                issues[exc.code] += 1
                continue
            if record.cache_state != "ready" or not record.hash_verified:
                issues["seed_bytes_state_invalid"] += 1
                continue
            if record.size_bytes not in {0, len(data)}:
                entry.status = "failed"
                entry.issue = "seed_bytes_size_mismatch"
                issues[entry.issue] += 1
                continue
            if entry.status not in {"pending", "seed_ready"}:
                continue
            if entry.sha256 is not None and entry.sha256 != digest:
                entry.status = "failed"
                entry.issue = "seed_bytes_conflict"
                issues[entry.issue] += 1
                continue
            relative_path = _asset_relative_path(digest)
            try:
                self.budget.admit_existing(relative_path, len(data))
                _write_bytes_atomically(self.output_dir, relative_path, data)
            except SnapshotError as exc:
                entry.status = "failed"
                entry.issue = exc.code
                issues[entry.issue] += 1
                continue
            except OSError:
                entry.status = "failed"
                entry.issue = "output_write_failed"
                issues[entry.issue] += 1
                continue
            if entry.status == "pending":
                copied += 1
            entry.status = "seed_ready"
            entry.relative_path = relative_path
            entry.sha256 = digest
            entry.size_bytes = len(data)
            entry.detected_type = detected
            entry.response_content_type = None
            entry.source = "seed"
        return copied

    def _apply_resume(
        self,
        entries: list[UrlEntry],
        receipt: dict[str, dict[str, Any]],
        issues: Counter[str],
    ) -> int:
        resumed = 0
        for entry in entries:
            if entry.status != "pending":
                continue
            old = receipt.get(entry.url)
            if old is None or old.get("assetIds") != entry.asset_ids:
                continue
            expected = entry.expected_hash
            if expected and old.get("sha256") != expected:
                continue
            try:
                path = _safe_relative_path(self.output_dir, old.get("path"))
            except SnapshotError:
                issues["receipt_path_invalid"] += 1
                continue
            digest = old.get("sha256")
            size = old.get("sizeBytes")
            if SHA256_RE.fullmatch(digest or "") is None or not isinstance(size, int) or size <= 0:
                issues["receipt_invalid"] += 1
                continue
            if not _safe_existing_asset(path, self.output_dir, digest, size):
                continue
            try:
                with path.open("rb") as handle:
                    data = handle.read(MAX_ASSET_BYTES + 1)
                actual_digest, detected = _validate_entry_bytes(
                    data, entry, old.get("responseContentType")
                    if isinstance(old.get("responseContentType"), str) else None,
                )
                if actual_digest != digest or len(data) != size:
                    continue
                self.budget.admit_existing(old["path"], size)
            except (OSError, SnapshotError):
                continue
            entry.status = "resumed"
            entry.relative_path = str(PurePosixPath(old["path"]))
            entry.sha256 = digest
            entry.size_bytes = size
            entry.detected_type = detected
            entry.response_content_type = (
                old.get("responseContentType") if isinstance(old.get("responseContentType"), str) else None
            )
            entry.source = "receipt"
            resumed += 1
        return resumed

    def _download_one(self, entry: UrlEntry) -> UrlEntry:
        try:
            data, digest, detected, content_type = self.downloader.fetch(entry)
            relative_path = _asset_relative_path(digest)
            try:
                _write_bytes_atomically(self.output_dir, relative_path, data)
            except (OSError, SnapshotError) as exc:
                raise SnapshotError("output_write_failed") from exc
            entry.status = "downloaded"
            entry.relative_path = relative_path
            entry.sha256 = digest
            entry.size_bytes = len(data)
            entry.detected_type = detected
            entry.response_content_type = content_type
            entry.source = "network"
        except SnapshotError as exc:
            entry.status = "failed"
            entry.issue = exc.code
        except Exception:
            entry.status = "failed"
            entry.issue = "download_failed"
        # Failures also return only metadata: exception tracebacks retained by
        # Futures would otherwise hold every rejected attachment body in RAM.
        return entry

    def run(self) -> dict[str, Any]:
        seed = _read_seed(self.seed_path)
        output = _safe_output_dir(self.seed_path, self.output_dir)
        # Keep paths stable after the safety check; no later code opens the seed
        # for writing and no input-provided path is ever interpreted as SQL.
        self.output_dir = output
        entries, issues = self._make_entries(seed.assets)
        receipt = _load_receipt(output, seed.file_sha256)
        seed_ready = self._apply_seed_bytes(entries, seed, issues)
        resumed = self._apply_resume(entries, receipt, issues)

        pending = [entry for entry in entries if entry.status == "pending"]
        downloaded = 0
        if pending:
            with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="asset") as pool:
                future_map = {pool.submit(self._download_one, entry): entry for entry in pending}
                for future in as_completed(future_map):
                    entry = future_map.pop(future)
                    try:
                        future.result()
                    except SnapshotError as exc:
                        entry.status = "failed"
                        entry.issue = exc.code
                        issues[exc.code] += 1
                    except Exception:
                        entry.status = "failed"
                        entry.issue = "download_failed"
                        issues[entry.issue] += 1
                    else:
                        if entry.status == "downloaded":
                            downloaded += 1
                        elif entry.issue:
                            issues[entry.issue] += 1

        total_bytes = 0
        seen_paths: set[str] = set()
        for entry in entries:
            if entry.status in {"seed_ready", "resumed", "downloaded"} and entry.relative_path:
                if entry.relative_path not in seen_paths:
                    seen_paths.add(entry.relative_path)
                    total_bytes += entry.size_bytes
        if total_bytes > self.budget.limit:
            issues["total_asset_budget_exceeded"] += 1
        for entry in entries:
            if entry.status == "pending":
                entry.status = "failed"
                entry.issue = "asset_unavailable"
                issues[entry.issue] += 1

        upstream_issue_counts = seed.metadata.get("issueCounts")
        if not isinstance(upstream_issue_counts, dict):
            upstream_issue_counts = {}
        upstream_unavailable = upstream_issue_counts.get("upstream_resource_unavailable", 0)
        if not isinstance(upstream_unavailable, int) or upstream_unavailable < 0:
            upstream_unavailable = 0
        success_count = sum(entry.status in {"seed_ready", "downloaded", "resumed"} for entry in entries)
        unavailable_count = len(entries) - success_count
        complete = unavailable_count == 0 and not issues and upstream_unavailable == 0
        payload_entries = [entry.manifest_record() for entry in entries]
        result = {
            "format": FORMAT,
            "createdAt": _utc_now(),
            "seedSha256": seed.file_sha256,
            "seedComplete": seed.metadata.get("complete") is True,
            "upstreamResourceUnavailable": upstream_unavailable,
            "limits": {
                "assetBytes": MAX_ASSET_BYTES,
                "totalAssetBytes": self.budget.limit,
                "workers": self.workers,
                "timeoutSeconds": self.downloader.timeout_seconds,
                "maxRetries": MAX_RETRIES,
                "maxRedirects": self.downloader.max_redirects,
            },
            "counts": {
                "assets": len(seed.assets),
                "admittedAssets": sum(record.rights_status in ADMITTED_RIGHTS and record.source_name == SOURCE_NAME for record in seed.assets),
                "uniqueUrls": len(entries),
                "seedReady": seed_ready,
                "resumed": resumed,
                "downloaded": downloaded,
                "available": success_count,
                "unavailable": unavailable_count,
                "bytes": total_bytes,
                "networkBytesCharged": self.budget.network_bytes,
            },
            "issueCounts": dict(sorted(issues.items())),
            "complete": complete,
            "entries": payload_entries,
            "legacyHashSentinelNormalization": {
                "entries": [record.normalization for record in seed.assets if record.normalization is not None]
            },
        }
        if _sha256_file(self.seed_path)[0] != seed.file_sha256:
            raise SnapshotError("seed_changed")
        _write_json_atomically(output / "manifest.json", {"format": FORMAT, "entries": payload_entries})
        _write_json_atomically(output / "receipt.json", result)
        return result


def run_snapshot(
    seed_path: str | Path,
    output_dir: str | Path,
    *,
    transport: Transport | None = None,
    resolver: Resolver | None = None,
    workers: int = MAX_WORKERS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
    allow_network: bool = False,
    max_total_bytes: int = MAX_TOTAL_ASSET_BYTES,
) -> dict[str, Any]:
    """Run one bounded snapshot; injectable collaborators are test-only seams."""

    return SnapshotBuilder(
        Path(seed_path),
        Path(output_dir),
        transport=transport,
        resolver=resolver,
        workers=workers,
        timeout_seconds=timeout_seconds,
        min_interval_seconds=min_interval_seconds,
        allow_network=allow_network,
        max_total_bytes=max_total_bytes,
    ).run()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a bounded offline asset snapshot from a sanitized seed.")
    parser.add_argument("--seed", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=MAX_WORKERS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--min-interval", type=float, default=DEFAULT_MIN_INTERVAL_SECONDS)
    parser.add_argument("--allow-network", action="store_true", help="Explicitly authorize public attachment HTTPS requests.")
    parser.add_argument(
        "--max-total-bytes", type=int, default=MAX_TOTAL_ASSET_BYTES,
        help="Explicit combined existing-byte/network ceiling (default 2 GiB; at most 8 GiB).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        result = run_snapshot(
            args.seed,
            args.output,
            workers=args.workers,
            timeout_seconds=args.timeout,
            min_interval_seconds=args.min_interval,
            allow_network=args.allow_network,
            max_total_bytes=args.max_total_bytes,
        )
    except SnapshotError as exc:
        print(_canonical({"format": FORMAT, "status": "ERROR", "error": exc.code}))
        return 1
    except (OSError, sqlite3.DatabaseError):
        print(_canonical({"format": FORMAT, "status": "ERROR", "error": "snapshot_failed"}))
        return 1
    print(
        _canonical(
            {
                "format": FORMAT,
                "status": "COMPLETE" if result["complete"] else "INCOMPLETE",
                "counts": result["counts"],
                "issueCounts": result["issueCounts"],
                "complete": result["complete"],
            }
        )
    )
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

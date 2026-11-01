#!/usr/bin/env python3
"""Build a derived catalog seed from the reviewed v1 seed and an asset snapshot.

This utility is intentionally stdlib-only.  The v1 input is opened read-only
and is never modified.  The only write target is a new sibling SQLite copy;
the snapshot is a bounded, local byte source and is never fetched from a URL.
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile
from typing import Any


FORMAT_V1 = "kontur-catalog-seed-v1"
FORMAT_V2 = "kontur-catalog-seed-v2"
APPLICATION_ID = 0x4B435331
SQLITE_USER_VERSION = 1
RIGHTS = {"private_authorized", "public_authorized"}
SOURCE_NAME = "kompege"
DEFAULT_SOURCE_SHA256 = "a18b55186b42e6d11142349f92bbb83ff34aff356d2758067d9f26e53392b051"
MAX_ASSET_BYTES = 64_000_000
MAX_TOTAL_ASSET_BYTES = 3 * 1024**3
MAX_RECORD_BYTES = 4 * 1024**2
MAX_METADATA_VALUE_BYTES = 4 * 1024**2
MAX_JSON_DEPTH = 24
MAX_JSON_NODES = 100_000
HASH = re.compile(r"[0-9a-f]{64}\Z")

TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "catalog_metadata": ("key", "value"),
    "catalog_tasks": ("id", "record_json"),
    "catalog_revisions": ("id", "record_json"),
    "catalog_assets": ("id", "record_json"),
    "catalog_links": ("id", "record_json"),
    "catalog_selection": ("revision_id",),
    "catalog_asset_bytes": ("asset_id", "sha256", "bytes"),
}
COLLECTION_TABLES = ("catalog_tasks", "catalog_revisions", "catalog_assets", "catalog_links")
HASH_TABLES = (*COLLECTION_TABLES, "catalog_selection", "catalog_asset_bytes")
COUNT_KEYS = {
    "tasks", "revisions", "assets", "links", "selection", "assetBytesRows", "assetBytesTotal",
    "unavailableAssets", "unresolvedAssetReferences", "suppressedCurrentPointers", "deniedRevisions",
    "otherSourceRevisions",
}
V1_METADATA_KEYS = {
    "format", "schemaVersion", "sourceSchema", "producerSha256", "createdAt",
    "scope", "limits", "counts", "complete", "issues", "issueCounts",
    "collectionsSha256", "manifestSha256",
}
ALLOWED_ISSUES = {
    "asset_changed", "asset_expected_hash_mismatch", "asset_hash_invalid",
    "asset_hash_mismatch", "asset_hash_unverified", "asset_missing_or_unreadable",
    "asset_not_ready", "asset_path_invalid", "asset_size_mismatch", "asset_too_large",
    "asset_row_missing_or_not_authorized", "asset_not_regular",
    "catalog_empty", "catalog_selection_empty", "manifest_asset_missing_or_ambiguous",
    "manifest_asset_revision_mismatch", "manifest_asset_unresolved",
    "upstream_resource_unavailable",
}
SNAPSHOT_FORMAT = "kontur-asset-snapshot-v1"
SNAPSHOT_TOP_LEVEL_KEYS = {"format", "sourceSeedSha256", "capturedAt", "assets", "counts", "manifestSha256"}
SNAPSHOT_ENTRY_KEYS = {
    "assetId", "relativePath", "expectedSha256", "sha256", "sizeBytes", "detectedType", "fetchedAt",
}
WORKER_MANIFEST_KEYS = {"format", "entries"}
WORKER_ENTRY_KEYS = {
    "url", "assetIds", "filenames", "mediaTypes", "expectedContentHash", "status", "path", "sha256",
    "sizeBytes", "detectedType", "responseContentType", "source", "issue", "expectedContentHashes",
}
NORMALIZATION_BLOCK_KEYS = (
    "legacyHashSentinelNormalization", "normalization", "normalizations", "normalizationReceipt",
    "assetNormalization", "assetNormalizations", "normalizedAssets",
)
NORMALIZATION_ENTRY_KEYS = {
    "assetId", "asset_id", "canonicalUrl", "canonical_url", "filename",
    "originalExpectedContentHash", "originalExpectedSha256", "original_expected_content_hash",
    "normalizedExpectedContentHash", "normalizedExpectedSha256", "normalized_expected_content_hash",
    "fingerprint", "originalFingerprint", "normalizationFingerprint", "reason", "kind",
}
FINGERPRINT_KEYS = {
    "canonicalUrl", "canonical_url", "filename", "cacheState", "cache_state", "sizeBytes", "size_bytes",
    "contentHash", "content_hash", "expectedContentHash", "expected_content_hash",
    "hashVerified", "hash_verified", "detectedType", "detected_type",
}


class FinalizerError(Exception):
    """Fixed public error code; messages never contain paths or source data."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _canonical_bytes(value: Any) -> bytes:
    return canonical(value).encode("utf-8")


def _line(value: Any) -> bytes:
    return _canonical_bytes(value) + b"\n"


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except (OSError, ValueError):
        raise FinalizerError("source_seed_unreadable") from None
    return digest.hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FinalizerError("catalog_json_duplicate_key")
        result[key] = value
    return result


def _decode(raw: Any, *, max_bytes: int = MAX_RECORD_BYTES) -> Any:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > max_bytes:
        raise FinalizerError("catalog_json_invalid")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, TypeError, RecursionError, UnicodeError, FinalizerError):
        raise FinalizerError("catalog_json_invalid") from None
    stack = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > MAX_JSON_DEPTH or nodes > MAX_JSON_NODES:
            raise FinalizerError("catalog_json_limit_exceeded")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise FinalizerError("catalog_json_invalid")
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, float) and (item != item or item in (float("inf"), float("-inf"))):
            raise FinalizerError("catalog_json_invalid")
    if canonical(value) != raw:
        raise FinalizerError("catalog_json_noncanonical")
    return value


def _require_hash(value: Any, code: str = "catalog_hash_invalid") -> str:
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise FinalizerError(code)
    return value


def _require_nonempty_text(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or len(value.encode("utf-8")) > 2 * 1024**2:
        raise FinalizerError(code)
    return value


def _validate_sqlite_layout(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        raise FinalizerError("catalog_schema_invalid")
    if connection.execute("PRAGMA user_version").fetchone()[0] != SQLITE_USER_VERSION:
        raise FinalizerError("catalog_schema_invalid")
    try:
        if connection.execute("PRAGMA integrity_check(1)").fetchone()[0] != "ok":
            raise FinalizerError("catalog_integrity_invalid")
    except sqlite3.Error:
        raise FinalizerError("catalog_integrity_invalid") from None

    found: set[str] = set()
    for kind, name, table, sql in connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"
    ):
        if kind == "index" and sql is None and str(name).startswith("sqlite_autoindex_"):
            continue
        if kind != "table" or name not in TABLE_COLUMNS or table != name or not isinstance(sql, str):
            raise FinalizerError("catalog_schema_invalid")
        if name in found:
            raise FinalizerError("catalog_schema_invalid")
        found.add(name)
    if found != set(TABLE_COLUMNS):
        raise FinalizerError("catalog_schema_invalid")
    for table, expected in TABLE_COLUMNS.items():
        try:
            info = list(connection.execute(f"PRAGMA table_xinfo({table})"))
        except sqlite3.Error:
            raise FinalizerError("catalog_schema_invalid") from None
        if len(info) != len(expected) or [row[1] for row in info] != list(expected) or any(row[6] for row in info):
            raise FinalizerError("catalog_schema_invalid")


def _open_v1(path: Path) -> sqlite3.Connection:
    try:
        if path.is_symlink():
            raise FinalizerError("source_seed_unreadable")
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise FinalizerError("source_seed_unreadable")
        connection = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True, timeout=3)
        connection.row_factory = sqlite3.Row
        try:
            connection.enable_load_extension(False)
        except (AttributeError, sqlite3.NotSupportedError):
            pass
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA busy_timeout=3000")
        _validate_sqlite_layout(connection)
        return connection
    except FinalizerError:
        raise
    except (OSError, sqlite3.Error, ValueError):
        raise FinalizerError("source_seed_unreadable") from None


def _validate_metadata(metadata: dict[str, Any]) -> None:
    if set(metadata) != V1_METADATA_KEYS:
        raise FinalizerError("catalog_metadata_invalid")
    if (
        metadata["format"] != FORMAT_V1
        or type(metadata["schemaVersion"]) is not int
        or metadata["schemaVersion"] != 1
        or type(metadata["sourceSchema"]) is not int
        or metadata["sourceSchema"] != 12
    ):
        raise FinalizerError("catalog_metadata_invalid")
    if not isinstance(metadata["complete"], bool):
        raise FinalizerError("catalog_metadata_invalid")
    _require_hash(metadata["producerSha256"], "catalog_metadata_invalid")
    _require_hash(metadata["manifestSha256"], "catalog_metadata_invalid")
    if not isinstance(metadata["createdAt"], str) or not metadata["createdAt"]:
        raise FinalizerError("catalog_metadata_invalid")
    if not isinstance(metadata["scope"], dict) or not isinstance(metadata["limits"], dict):
        raise FinalizerError("catalog_metadata_invalid")
    if not isinstance(metadata["counts"], dict) or set(metadata["counts"]) != COUNT_KEYS or not isinstance(metadata["issues"], list):
        raise FinalizerError("catalog_metadata_invalid")
    if not isinstance(metadata["issueCounts"], dict):
        raise FinalizerError("catalog_metadata_invalid")
    if not isinstance(metadata["collectionsSha256"], dict) or set(metadata["collectionsSha256"]) != set(HASH_TABLES):
        raise FinalizerError("catalog_metadata_invalid")
    for digest in metadata["collectionsSha256"].values():
        _require_hash(digest, "catalog_metadata_invalid")
    if _hash_bytes(_canonical_bytes({key: value for key, value in metadata.items() if key != "manifestSha256"})) != metadata["manifestSha256"]:
        raise FinalizerError("catalog_manifest_hash_invalid")


def _read_metadata(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = list(connection.execute("SELECT key,value FROM catalog_metadata ORDER BY key"))
    if len(rows) != len(V1_METADATA_KEYS):
        raise FinalizerError("catalog_metadata_invalid")
    metadata: dict[str, Any] = {}
    for key, raw in rows:
        if not isinstance(key, str) or key in metadata:
            raise FinalizerError("catalog_metadata_invalid")
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_METADATA_VALUE_BYTES:
            raise FinalizerError("catalog_metadata_invalid")
        metadata[key] = _decode(raw, max_bytes=MAX_METADATA_VALUE_BYTES)
    _validate_metadata(metadata)
    return metadata


def _read_records(connection: sqlite3.Connection, table: str) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    records: dict[str, dict[str, Any]] = OrderedDict()
    raw_records: dict[str, str] = {}
    for identifier, raw in connection.execute(f"SELECT id,record_json FROM {table} ORDER BY id"):
        if not isinstance(identifier, str) or not identifier or not isinstance(raw, str):
            raise FinalizerError("catalog_record_invalid")
        record = _decode(raw)
        if not isinstance(record, dict) or record.get("id") != identifier or identifier in records:
            raise FinalizerError("catalog_record_invalid")
        records[identifier] = record
        raw_records[identifier] = raw
    return records, raw_records


def _validate_references(
    tasks: dict[str, dict[str, Any]],
    revisions: dict[str, dict[str, Any]],
    assets: dict[str, dict[str, Any]],
    links: dict[str, dict[str, Any]],
    selection: list[str],
) -> None:
    for task in tasks.values():
        if task.get("source_name") != SOURCE_NAME:
            raise FinalizerError("catalog_source_invalid")
        current = task.get("current_revision_id")
        if current is not None and current not in revisions:
            raise FinalizerError("catalog_reference_invalid")
    for revision in revisions.values():
        if revision.get("task_id") not in tasks or revision.get("rights_status") not in RIGHTS:
            raise FinalizerError("catalog_rights_invalid")
        source_metadata = revision.get("source_metadata")
        if not isinstance(source_metadata, dict):
            raise FinalizerError("catalog_record_invalid")
        unavailable = source_metadata.get("kege_unavailable_resources", [])
        if not isinstance(unavailable, list):
            raise FinalizerError("catalog_source_warning_invalid")
    for asset in assets.values():
        if asset.get("rights_status") not in RIGHTS:
            raise FinalizerError("catalog_rights_invalid")
        if (
            asset.get("source_name") != SOURCE_NAME
            or "storage_key" in asset
            or not isinstance(asset.get("content_hash"), str)
            or not HASH.fullmatch(asset["content_hash"])
            or asset.get("expected_content_hash") is not None
            and not HASH.fullmatch(asset["expected_content_hash"])
        ):
            raise FinalizerError("catalog_asset_invalid")
        if type(asset.get("size_bytes")) is not int or not 0 <= asset["size_bytes"] <= MAX_ASSET_BYTES:
            raise FinalizerError("catalog_asset_invalid")
        if not isinstance(asset.get("hash_verified"), bool):
            raise FinalizerError("catalog_asset_invalid")
        if not isinstance(asset.get("cache_state"), str):
            raise FinalizerError("catalog_asset_invalid")
    for link in links.values():
        if link.get("revision_id") not in revisions or link.get("asset_id") not in assets:
            raise FinalizerError("catalog_reference_invalid")
    if len(selection) != len(set(selection)) or any(item not in revisions for item in selection):
        raise FinalizerError("catalog_selection_invalid")


def _read_selection(connection: sqlite3.Connection) -> tuple[list[str], str]:
    values = [row[0] for row in connection.execute("SELECT revision_id FROM catalog_selection ORDER BY revision_id")]
    if any(not isinstance(value, str) for value in values):
        raise FinalizerError("catalog_selection_invalid")
    digest = hashlib.sha256()
    for value in values:
        digest.update(_line([value]))
    return values, digest.hexdigest()


def _read_original_bytes(connection: sqlite3.Connection, assets: dict[str, dict[str, Any]]) -> tuple[dict[str, tuple[str, int]], int, str]:
    metadata_by_asset: dict[str, tuple[str, int]] = {}
    total = 0
    digest = hashlib.sha256()
    for asset_id, expected, data in connection.execute(
        "SELECT asset_id,sha256,bytes FROM catalog_asset_bytes ORDER BY asset_id"
    ):
        if asset_id in metadata_by_asset or asset_id not in assets or not isinstance(data, bytes):
            raise FinalizerError("catalog_asset_bytes_invalid")
        _require_hash(expected, "catalog_asset_bytes_invalid")
        if len(data) > MAX_ASSET_BYTES or _hash_bytes(data) != expected:
            raise FinalizerError("catalog_asset_bytes_invalid")
        total += len(data)
        if total > MAX_TOTAL_ASSET_BYTES:
            raise FinalizerError("catalog_asset_budget_exceeded")
        asset = assets[asset_id]
        if (
            asset.get("content_hash") != expected
            or asset.get("size_bytes") != len(data)
            or asset.get("cache_state") != "ready"
            or asset.get("hash_verified") is not True
            or asset.get("expected_content_hash") not in {None, expected}
            or not isinstance(asset.get("detected_type"), str)
            or not asset["detected_type"]
        ):
            raise FinalizerError("catalog_asset_bytes_invalid")
        metadata_by_asset[asset_id] = (expected, len(data))
        digest.update(_line([asset_id, expected, len(data)]))
    return metadata_by_asset, total, digest.hexdigest()


def _validate_issue_metadata(
    metadata: dict[str, Any],
    revisions: dict[str, dict[str, Any]],
    assets: dict[str, dict[str, Any]],
    missing_asset_ids: set[str],
) -> list[str]:
    counts = metadata["counts"]
    issue_counts = metadata["issueCounts"]
    if any(not isinstance(key, str) or type(value) is not int or value < 0 for key, value in issue_counts.items()):
        raise FinalizerError("catalog_issue_metadata_invalid")
    for code in issue_counts:
        if code not in ALLOWED_ISSUES:
            raise FinalizerError("catalog_issue_unknown")
    expected_missing = counts.get("unavailableAssets")
    if type(expected_missing) is not int or expected_missing != len(missing_asset_ids):
        raise FinalizerError("catalog_counts_invalid")
    if issue_counts.get("asset_not_ready", 0) != len(missing_asset_ids):
        raise FinalizerError("catalog_issue_metadata_invalid")
    forbidden_codes = {
        code for code, amount in issue_counts.items()
        if amount and code not in {"asset_not_ready", "upstream_resource_unavailable"}
    }
    if forbidden_codes:
        raise FinalizerError("catalog_unresolved_asset")

    examples = metadata["issues"]
    if len(examples) > 1000:
        raise FinalizerError("catalog_issue_metadata_invalid")
    for item in examples:
        if not isinstance(item, dict) or set(item) - {"code", "assetId", "revisionId", "referenceSha256"}:
            raise FinalizerError("catalog_issue_metadata_invalid")
        code = item.get("code")
        if code not in ALLOWED_ISSUES:
            raise FinalizerError("catalog_issue_unknown")
        if "assetId" in item and item["assetId"] not in assets:
            raise FinalizerError("catalog_issue_metadata_invalid")
        if "revisionId" in item and item["revisionId"] not in revisions:
            raise FinalizerError("catalog_issue_metadata_invalid")
        if "referenceSha256" in item:
            _require_hash(item["referenceSha256"], "catalog_issue_metadata_invalid")
        if code == "upstream_resource_unavailable" and "revisionId" not in item:
            raise FinalizerError("catalog_issue_metadata_invalid")
        if code == "asset_not_ready" and "assetId" not in item:
            raise FinalizerError("catalog_issue_metadata_invalid")
        if code == "asset_not_ready" and item.get("assetId") not in missing_asset_ids:
            raise FinalizerError("catalog_issue_metadata_invalid")

    warning_ids = sorted(
        revision_id
        for revision_id, revision in revisions.items()
        if revision["source_metadata"].get("kege_unavailable_resources")
    )
    if issue_counts.get("upstream_resource_unavailable", 0) != len(warning_ids):
        raise FinalizerError("catalog_source_warning_invalid")
    sampled_warning_ids = {
        item["revisionId"]
        for item in examples
        if item.get("code") == "upstream_resource_unavailable"
    }
    if not sampled_warning_ids <= set(warning_ids):
        raise FinalizerError("catalog_source_warning_invalid")
    if counts.get("unresolvedAssetReferences", 0) != 0:
        raise FinalizerError("catalog_unresolved_asset")
    for code in ("asset_row_missing_or_not_authorized", "manifest_asset_unresolved", "manifest_asset_missing_or_ambiguous"):
        if issue_counts.get(code, 0):
            raise FinalizerError("catalog_unresolved_asset")
    return warning_ids


def _validate_v1_counts(
    metadata: dict[str, Any],
    tasks: dict[str, dict[str, Any]],
    revisions: dict[str, dict[str, Any]],
    assets: dict[str, dict[str, Any]],
    links: dict[str, dict[str, Any]],
    selection: list[str],
    original_bytes: dict[str, tuple[str, int]],
    original_byte_total: int,
) -> None:
    counts = metadata["counts"]
    expected = {
        "tasks": len(tasks), "revisions": len(revisions), "assets": len(assets), "links": len(links),
        "selection": len(selection), "assetBytesRows": len(original_bytes),
        "assetBytesTotal": original_byte_total, "unavailableAssets": len(assets) - len(original_bytes),
    }
    for key, value in expected.items():
        if counts.get(key) != value:
            raise FinalizerError("catalog_counts_invalid")
    for key in ("deniedRevisions", "otherSourceRevisions", "unresolvedAssetReferences", "suppressedCurrentPointers"):
        if type(counts.get(key)) is not int or counts[key] < 0:
            raise FinalizerError("catalog_counts_invalid")


def _read_v1(path: Path, expected_source_sha256: str | None) -> dict[str, Any]:
    source_hash = _hash_file(path)
    if expected_source_sha256 is not None and source_hash != expected_source_sha256:
        raise FinalizerError("source_seed_hash_mismatch")
    connection = _open_v1(path)
    try:
        metadata = _read_metadata(connection)
        tasks, raw_tasks = _read_records(connection, "catalog_tasks")
        revisions, raw_revisions = _read_records(connection, "catalog_revisions")
        assets, raw_assets = _read_records(connection, "catalog_assets")
        links, raw_links = _read_records(connection, "catalog_links")
        selection, selection_hash = _read_selection(connection)
        original_bytes, byte_total, bytes_hash = _read_original_bytes(connection, assets)
        _validate_references(tasks, revisions, assets, links, selection)
        if selection_hash != metadata["collectionsSha256"]["catalog_selection"]:
            raise FinalizerError("catalog_collection_hash_invalid")
        if bytes_hash != metadata["collectionsSha256"]["catalog_asset_bytes"]:
            raise FinalizerError("catalog_collection_hash_invalid")
        for table, records, raw_records in (
            ("catalog_tasks", tasks, raw_tasks),
            ("catalog_revisions", revisions, raw_revisions),
            ("catalog_assets", assets, raw_assets),
            ("catalog_links", links, raw_links),
        ):
            digest = hashlib.sha256()
            for identifier in sorted(records):
                digest.update(_line([identifier, records[identifier]]))
            if digest.hexdigest() != metadata["collectionsSha256"][table]:
                raise FinalizerError("catalog_collection_hash_invalid")
            # The old producer writes canonical JSON. Keep the raw text so tests
            # can assert that task/revision/link rows are byte-for-byte unchanged.
            if any(canonical(records[identifier]) != raw_records[identifier] for identifier in records):
                raise FinalizerError("catalog_record_noncanonical")
        _validate_v1_counts(metadata, tasks, revisions, assets, links, selection, original_bytes, byte_total)
        missing_asset_ids = set(assets) - set(original_bytes)
        warnings = _validate_issue_metadata(metadata, revisions, assets, missing_asset_ids)
        # A concurrent source write must never be mistaken for a reviewed seed.
        if _hash_file(path) != source_hash:
            raise FinalizerError("source_seed_changed")
        return {
            "sourceHash": source_hash,
            "metadata": metadata,
            "tasks": tasks,
            "revisions": revisions,
            "assets": assets,
            "links": links,
            "selection": selection,
            "originalBytes": original_bytes,
            "originalByteTotal": byte_total,
            "warnings": warnings,
        }
    finally:
        connection.close()


def _validate_snapshot_manifest(raw: Any, source_hash: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(raw, dict) or set(raw) - SNAPSHOT_TOP_LEVEL_KEYS:
        raise FinalizerError("snapshot_manifest_invalid")
    if raw.get("format") != SNAPSHOT_FORMAT or raw.get("sourceSeedSha256") != source_hash:
        raise FinalizerError("snapshot_manifest_invalid")
    captured_at = raw.get("capturedAt")
    if not isinstance(captured_at, str) or not captured_at:
        raise FinalizerError("snapshot_manifest_invalid")
    assets = raw.get("assets")
    if not isinstance(assets, list):
        raise FinalizerError("snapshot_manifest_invalid")
    if "manifestSha256" in raw:
        _require_hash(raw["manifestSha256"], "snapshot_manifest_invalid")
        if _hash_bytes(_canonical_bytes({key: value for key, value in raw.items() if key != "manifestSha256"})) != raw["manifestSha256"]:
            raise FinalizerError("snapshot_manifest_hash_invalid")
    if "counts" in raw and not isinstance(raw["counts"], dict):
        raise FinalizerError("snapshot_manifest_invalid")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in assets:
        if not isinstance(entry, dict) or set(entry) - SNAPSHOT_ENTRY_KEYS or set(entry) - {"assetId", "relativePath", "expectedSha256", "sizeBytes", "detectedType", "fetchedAt", "sha256"}:
            raise FinalizerError("snapshot_manifest_invalid")
        asset_id = entry.get("assetId")
        if not isinstance(asset_id, str) or not asset_id or asset_id in seen:
            raise FinalizerError("snapshot_manifest_invalid")
        seen.add(asset_id)
        expected = _require_hash(entry.get("expectedSha256"), "snapshot_manifest_invalid")
        if "sha256" in entry and entry["sha256"] != expected:
            raise FinalizerError("snapshot_manifest_invalid")
        relative = entry.get("relativePath")
        if (
            not isinstance(relative, str) or not relative or "\\" in relative or relative.startswith("/")
            or re.match(r"^[A-Za-z]:", relative) or any(part in {"", ".", ".."} for part in relative.split("/"))
        ):
            raise FinalizerError("snapshot_manifest_invalid")
        size = entry.get("sizeBytes")
        if type(size) is not int or not 0 <= size <= MAX_ASSET_BYTES:
            raise FinalizerError("snapshot_manifest_invalid")
        detected = entry.get("detectedType")
        _require_nonempty_text(detected, "snapshot_manifest_invalid")
        fetched_at = entry.get("fetchedAt")
        if not isinstance(fetched_at, str) or not fetched_at:
            raise FinalizerError("snapshot_manifest_invalid")
        entries.append({
            "assetId": asset_id,
            "relativePath": relative,
            "expectedSha256": expected,
            "sizeBytes": size,
            "detectedType": detected,
            "fetchedAt": fetched_at,
        })
    return raw, entries


def _metadata_hash_sentinel(asset: dict[str, Any]) -> str | None:
    canonical_url = asset.get("canonical_url")
    filename = asset.get("filename")
    if not isinstance(canonical_url, str) or not isinstance(filename, str):
        return None
    return _hash_bytes(("metadata:" + canonical_url + ":" + filename).encode("utf-8"))


def _is_metadata_hash_sentinel(asset: dict[str, Any]) -> bool:
    sentinel = _metadata_hash_sentinel(asset)
    return bool(
        sentinel
        and asset.get("expected_content_hash") == sentinel
        and asset.get("content_hash") == sentinel
        and type(asset.get("size_bytes")) is int
        and asset.get("size_bytes") == 0
        and asset.get("hash_verified") is False
        and "detected_type" in asset and asset["detected_type"] is None
        and asset.get("cache_state") in {"metadata_only", "failed"}
    )


def _first_field(record: dict[str, Any], *names: str) -> Any:
    present = [record[name] for name in names if name in record]
    if present and any(canonical(value) != canonical(present[0]) for value in present[1:]):
        raise FinalizerError("snapshot_normalization_invalid")
    return present[0] if present else None


def _normalization_entries_from_block(block: Any) -> list[Any]:
    if isinstance(block, list):
        return block
    if isinstance(block, dict):
        if "entries" in block:
            if not isinstance(block["entries"], list):
                raise FinalizerError("snapshot_normalization_invalid")
            return block["entries"]
        # A keyed map is accepted only when each value is an evidence object;
        # the key is carried into assetId below.
        result = []
        for asset_id, value in block.items():
            if not isinstance(asset_id, str) or not isinstance(value, dict):
                raise FinalizerError("snapshot_normalization_invalid")
            item = dict(value)
            item.setdefault("assetId", asset_id)
            result.append(item)
        return result
    raise FinalizerError("snapshot_normalization_invalid")


def _parse_normalization_evidence(receipt: dict[str, Any], known_asset_ids: set[str]) -> dict[str, dict[str, Any]]:
    """Read separate receipt evidence for the narrowly-scoped legacy sentinel fix."""
    raw_entries: list[Any] = []
    for key in NORMALIZATION_BLOCK_KEYS:
        if key in receipt:
            raw_entries.extend(_normalization_entries_from_block(receipt[key]))
    # Some worker revisions attach evidence to the successful per-URL receipt
    # entries while keeping the normalization payload separate from manifest.
    ordinary_entries = receipt.get("entries")
    if isinstance(ordinary_entries, list):
        for item in ordinary_entries:
            if isinstance(item, dict) and any(
                key in item for key in (
                    "originalExpectedContentHash", "originalExpectedSha256", "original_expected_content_hash",
                    "normalizedExpectedContentHash", "normalizedExpectedSha256", "normalizationFingerprint",
                )
            ):
                raw_entries.append(item)
    result: dict[str, dict[str, Any]] = {}
    for item in raw_entries:
        if not isinstance(item, dict) or set(item) - NORMALIZATION_ENTRY_KEYS:
            raise FinalizerError("snapshot_normalization_invalid")
        asset_id = _first_field(item, "assetId", "asset_id")
        if not isinstance(asset_id, str) or asset_id not in known_asset_ids or asset_id in result:
            raise FinalizerError("snapshot_normalization_invalid")
        original_expected = _first_field(
            item, "originalExpectedContentHash", "originalExpectedSha256", "original_expected_content_hash",
        )
        normalized_expected = _first_field(
            item, "normalizedExpectedContentHash", "normalizedExpectedSha256", "normalized_expected_content_hash",
        )
        fingerprint = _first_field(item, "fingerprint", "originalFingerprint", "normalizationFingerprint")
        if not isinstance(fingerprint, dict) or set(fingerprint) - FINGERPRINT_KEYS:
            raise FinalizerError("snapshot_normalization_invalid")
        for aliases in (
            ("canonicalUrl", "canonical_url"), ("filename",), ("cacheState", "cache_state"),
            ("sizeBytes", "size_bytes"), ("contentHash", "content_hash"),
            ("expectedContentHash", "expected_content_hash"), ("hashVerified", "hash_verified"),
            ("detectedType", "detected_type"),
        ):
            if not any(alias in fingerprint for alias in aliases):
                raise FinalizerError("snapshot_normalization_invalid")
        result[asset_id] = {
            "assetId": asset_id,
            "canonicalUrl": _first_field(item, "canonicalUrl", "canonical_url"),
            "filename": item.get("filename"),
            "originalExpectedContentHash": original_expected,
            "normalizedExpectedContentHash": normalized_expected,
            "fingerprint": fingerprint,
        }
    return result


def _apply_normalization_evidence(
    assets: dict[str, dict[str, Any]], evidence: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    effective = {asset_id: dict(asset) for asset_id, asset in assets.items()}
    cleared: set[str] = set()
    for asset_id, item in evidence.items():
        asset = assets[asset_id]
        if not _is_metadata_hash_sentinel(asset):
            # A genuine expected hash is provenance and can never be cleared by
            # a receipt normalization claim.
            raise FinalizerError("snapshot_normalization_invalid")
        fingerprint = item["fingerprint"]
        expected = asset["expected_content_hash"]
        if (
            item["canonicalUrl"] != asset.get("canonical_url")
            or item["filename"] != asset.get("filename")
            or item["originalExpectedContentHash"] != expected
            or item["normalizedExpectedContentHash"] is not None
            or _first_field(fingerprint, "canonicalUrl", "canonical_url") != asset.get("canonical_url")
            or fingerprint.get("filename") != asset.get("filename")
            or _first_field(fingerprint, "cacheState", "cache_state") != asset.get("cache_state")
            or type(_first_field(fingerprint, "sizeBytes", "size_bytes")) is not int
            or _first_field(fingerprint, "sizeBytes", "size_bytes") != 0
            or _first_field(fingerprint, "contentHash", "content_hash") != asset.get("content_hash")
            or _first_field(fingerprint, "expectedContentHash", "expected_content_hash") != expected
            or _first_field(fingerprint, "hashVerified", "hash_verified") is not False
            or _first_field(fingerprint, "detectedType", "detected_type") is not None
        ):
            raise FinalizerError("snapshot_normalization_invalid")
        effective[asset_id]["expected_content_hash"] = None
        cleared.add(asset_id)
    return effective, cleared


def _snapshot_file_path(root: Path, entry: dict[str, Any]) -> Path:
    try:
        if root.is_symlink():
            raise FinalizerError("snapshot_root_invalid")
        root = root.resolve(strict=True)
        if not root.is_dir() or root.is_symlink():
            raise FinalizerError("snapshot_root_invalid")
        relative = entry.get("relativePath")
        if (
            not isinstance(relative, str)
            or not relative
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))
        ):
            raise FinalizerError("snapshot_path_invalid")
        path = root.joinpath(*relative.split("/"))
        resolved = path.resolve(strict=True)
        if resolved != path or root not in resolved.parents:
            raise FinalizerError("snapshot_path_invalid")
        current = root
        for component in relative.split("/"):
            current = current / component
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise FinalizerError("snapshot_path_invalid")
    except FinalizerError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise FinalizerError("snapshot_file_invalid") from None
    return path


def _read_snapshot_file(
    root: Path,
    entry: dict[str, Any],
    *,
    staging_root: Path | None = None,
) -> tuple[str, int, Path | None]:
    path = _snapshot_file_path(root, entry)
    expected_size = entry["sizeBytes"]
    expected_sha256 = entry["expectedSha256"]
    source_fd: int | None = None
    staged_fd: int | None = None
    staged_path: Path | None = None
    success = False
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        source_fd = os.open(path, flags)
        opened_path = path.resolve(strict=True)
        if opened_path != path or root.resolve(strict=True) not in opened_path.parents:
            raise FinalizerError("snapshot_path_invalid")
        source_info = os.fstat(source_fd)
        if (
            not stat.S_ISREG(source_info.st_mode)
            or stat.S_ISLNK(source_info.st_mode)
            or getattr(source_info, "st_file_attributes", 0) & 0x400
            or source_info.st_size > MAX_ASSET_BYTES
        ):
            raise FinalizerError("snapshot_file_invalid")
        if staging_root is not None:
            if staging_root.is_symlink():
                raise FinalizerError("snapshot_root_invalid")
            staging_root = staging_root.resolve(strict=True)
            if not staging_root.is_dir() or staging_root.is_symlink():
                raise FinalizerError("snapshot_root_invalid")
            staged_fd, staged_name = tempfile.mkstemp(prefix=".snapshot-asset-", dir=str(staging_root))
            staged_path = Path(staged_name)

        with ExitStack() as streams:
            source = streams.enter_context(os.fdopen(source_fd, "rb", closefd=True))
            source_fd = None
            staged = None
            if staged_fd is not None:
                staged = streams.enter_context(os.fdopen(staged_fd, "wb", closefd=True))
                staged_fd = None
            digest = hashlib.sha256()
            total = 0
            while chunk := source.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_ASSET_BYTES:
                    raise FinalizerError("asset_too_large")
                digest.update(chunk)
                if staged is not None:
                    staged.write(chunk)
            if staged is not None:
                staged.flush()
                os.fsync(staged.fileno())
        actual_sha256 = digest.hexdigest()
        if total != expected_size or actual_sha256 != expected_sha256:
            raise FinalizerError("snapshot_asset_hash_mismatch")
        if staged_path is not None:
            try:
                staged_path.chmod(0o600)
            except OSError:
                pass
        success = True
        return actual_sha256, total, staged_path
    except FinalizerError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise FinalizerError("snapshot_file_invalid") from None
    finally:
        if source_fd is not None:
            try:
                os.close(source_fd)
            except OSError:
                pass
        if staged_fd is not None:
            try:
                os.close(staged_fd)
            except OSError:
                pass
        if not success and staged_path is not None:
            try:
                staged_path.unlink(missing_ok=True)
            except OSError:
                pass


def _read_snapshot_document(
    path: Path, source_hash: str, known_asset_ids: set[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], bool, dict[str, dict[str, Any]]]:
    try:
        if path.is_symlink():
            raise FinalizerError("snapshot_manifest_invalid")
        manifest_text = path.read_text(encoding="utf-8")
        manifest_raw = json.loads(manifest_text, object_pairs_hook=_pairs)
    except (OSError, UnicodeError, ValueError, FinalizerError):
        raise FinalizerError("snapshot_manifest_invalid") from None
    manifest_body = manifest_text[:-1] if manifest_text.endswith("\n") else manifest_text
    if not isinstance(manifest_raw, dict) or canonical(manifest_raw) != manifest_body:
        raise FinalizerError("snapshot_manifest_noncanonical")
    _decode(manifest_body, max_bytes=MAX_METADATA_VALUE_BYTES)
    if "assets" in manifest_raw or "sourceSeedSha256" in manifest_raw:
        manifest, entries = _validate_snapshot_manifest(manifest_raw, source_hash)
        return manifest, entries, False, {}
    if set(manifest_raw) != WORKER_MANIFEST_KEYS or manifest_raw.get("format") != SNAPSHOT_FORMAT or not isinstance(manifest_raw.get("entries"), list):
        raise FinalizerError("snapshot_manifest_invalid")
    receipt_path = path.parent / "receipt.json"
    try:
        if receipt_path.is_symlink():
            raise FinalizerError("snapshot_receipt_invalid")
        receipt_text = receipt_path.read_text(encoding="utf-8")
        receipt = json.loads(receipt_text, object_pairs_hook=_pairs)
    except (OSError, UnicodeError, ValueError, FinalizerError):
        raise FinalizerError("snapshot_receipt_invalid") from None
    if (
        not isinstance(receipt, dict)
        or receipt.get("format") != SNAPSHOT_FORMAT
        or receipt.get("seedSha256") != source_hash
        or not isinstance(receipt.get("createdAt"), str)
        or not receipt["createdAt"]
    ):
        raise FinalizerError("snapshot_receipt_invalid")
    normalization = _parse_normalization_evidence(receipt, known_asset_ids or set())
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in manifest_raw["entries"]:
        if not isinstance(item, dict) or set(item) - WORKER_ENTRY_KEYS:
            raise FinalizerError("snapshot_manifest_invalid")
        asset_ids = item.get("assetIds")
        if not isinstance(asset_ids, list) or not asset_ids or any(not isinstance(asset_id, str) for asset_id in asset_ids):
            raise FinalizerError("snapshot_manifest_invalid")
        if known_asset_ids is not None and any(asset_id not in known_asset_ids for asset_id in asset_ids):
            raise FinalizerError("snapshot_asset_set_invalid")
        status = item.get("status")
        if status not in {"seed_ready", "downloaded", "resumed"}:
            continue
        relative = item.get("path")
        expected = item.get("sha256")
        size = item.get("sizeBytes")
        detected = item.get("detectedType")
        if not isinstance(relative, str) or not isinstance(expected, str) or not HASH.fullmatch(expected):
            raise FinalizerError("snapshot_manifest_invalid")
        declared_expected = item.get("expectedContentHash")
        if declared_expected is not None and declared_expected != expected:
            raise FinalizerError("snapshot_expected_hash_mismatch")
        if type(size) is not int or not 0 <= size <= MAX_ASSET_BYTES:
            raise FinalizerError("snapshot_manifest_invalid")
        _require_nonempty_text(detected, "snapshot_manifest_invalid")
        for asset_id in asset_ids:
            if asset_id in seen:
                raise FinalizerError("snapshot_manifest_invalid")
            seen.add(asset_id)
            entries.append({
                "assetId": asset_id,
                "relativePath": relative,
                "expectedSha256": expected,
                "sizeBytes": size,
                "detectedType": detected,
                "fetchedAt": receipt["createdAt"],
            })
    manifest = {
        "format": SNAPSHOT_FORMAT,
        "sourceSeedSha256": source_hash,
        "capturedAt": receipt["createdAt"],
        "assets": entries,
        "counts": {"assets": len(entries)},
    }
    return manifest, entries, True, normalization


def _read_snapshot(
    path: Path,
    root: Path,
    source_hash: str,
    assets: dict[str, dict[str, Any]],
    original_bytes: dict[str, tuple[str, int]],
    missing: set[str],
    staging_root: Path,
) -> tuple[dict[str, Any], dict[str, tuple[dict[str, Any], Path]], dict[str, dict[str, Any]], set[str]]:
    manifest, entries, allow_extra, normalization = _read_snapshot_document(path, source_hash, set(assets))
    effective_assets, cleared = _apply_normalization_evidence(assets, normalization)
    by_id = {entry["assetId"]: entry for entry in entries}
    if not set(by_id) <= set(assets) or not missing <= set(by_id):
        raise FinalizerError("snapshot_asset_set_invalid")
    if not allow_extra and set(by_id) != missing:
        raise FinalizerError("snapshot_asset_set_invalid")
    result: dict[str, tuple[dict[str, Any], Path]] = {}
    checked_total = 0
    for asset_id in sorted(by_id):
        entry = by_id[asset_id]
        original = effective_assets[asset_id]
        if original.get("expected_content_hash") not in {None, entry["expectedSha256"]}:
            raise FinalizerError("snapshot_expected_hash_mismatch")
        actual_sha256, size, staged_path = _read_snapshot_file(
            root,
            entry,
            staging_root=staging_root if asset_id in missing else None,
        )
        checked_total += size
        if checked_total > MAX_TOTAL_ASSET_BYTES:
            raise FinalizerError("catalog_asset_budget_exceeded")
        if asset_id in missing:
            if staged_path is None:
                raise FinalizerError("snapshot_file_invalid")
            result[asset_id] = (entry, staged_path)
        elif (actual_sha256, size) != original_bytes[asset_id]:
            raise FinalizerError("snapshot_cached_bytes_mismatch")
    if manifest.get("counts"):
        counts = manifest["counts"]
        if counts.get("assets", len(entries)) != len(entries) or (
            "bytes" in counts and counts["bytes"] != checked_total
        ):
            raise FinalizerError("snapshot_counts_invalid")
    return manifest, result, effective_assets, cleared


def _publish_no_overwrite(temporary: Path, output: Path) -> None:
    if output.exists() or output.is_symlink():
        raise FinalizerError("catalog_output_exists")
    try:
        if os.name == "nt":
            # Windows rename is exclusive when the destination exists, unlike
            # replace/MoveFileEx semantics.  The destination was checked above
            # and the kernel closes the race without replacing user data.
            os.rename(temporary, output)
        else:
            os.link(temporary, output)
            temporary.unlink()
    except FileExistsError:
        raise FinalizerError("catalog_output_exists") from None
    except (OSError, ValueError):
        raise FinalizerError("catalog_publish_failed") from None


def _copy_sqlite_snapshot(input_path: Path, temporary: Path, expected_hash: str) -> None:
    """Copy the logical read-only snapshot, including any committed WAL state."""
    if _hash_file(input_path) != expected_hash:
        raise FinalizerError("source_seed_changed")
    source = _open_v1(input_path)
    destination: sqlite3.Connection | None = None
    try:
        destination = sqlite3.connect(temporary)
        source.backup(destination)
        destination.commit()
    except (sqlite3.Error, OSError, ValueError):
        raise FinalizerError("source_seed_copy_failed") from None
    finally:
        if destination is not None:
            destination.close()
        source.close()
    if _hash_file(input_path) != expected_hash:
        raise FinalizerError("source_seed_changed")


def _insert_snapshot_bytes(
    connection: sqlite3.Connection,
    asset_id: str,
    expected_sha256: str,
    expected_size: int,
    staged_path: Path,
) -> None:
    """Stream one validated staging file into SQLite without a body pool."""
    source_fd: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if staged_path.resolve(strict=True) != staged_path:
            raise FinalizerError("snapshot_asset_hash_mismatch")
        source_fd = os.open(staged_path, flags)
        info = os.fstat(source_fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400
            or info.st_size > MAX_ASSET_BYTES
            or info.st_size != expected_size
        ):
            raise FinalizerError("snapshot_asset_hash_mismatch")
        with ExitStack() as streams:
            source = streams.enter_context(os.fdopen(source_fd, "rb", closefd=True))
            source_fd = None
            digest = hashlib.sha256()
            total = 0
            if expected_size == 0:
                data = source.read(1)
                if data:
                    raise FinalizerError("snapshot_asset_hash_mismatch")
                if digest.hexdigest() != expected_sha256:
                    raise FinalizerError("snapshot_asset_hash_mismatch")
                connection.execute(
                    "INSERT INTO catalog_asset_bytes(asset_id,sha256,bytes) VALUES (?,?,?)",
                    (asset_id, expected_sha256, sqlite3.Binary(b"")),
                )
                return

            connection.execute(
                "INSERT INTO catalog_asset_bytes(asset_id,sha256,bytes) VALUES (?,?,zeroblob(?))",
                (asset_id, expected_sha256, expected_size),
            )
            row = connection.execute(
                "SELECT rowid FROM catalog_asset_bytes WHERE asset_id=?",
                (asset_id,),
            ).fetchone()
            if row is None:
                raise FinalizerError("source_seed_copy_failed")
            blob = connection.blobopen("catalog_asset_bytes", "bytes", int(row[0]), readonly=False)
            streams.callback(blob.close)
            while chunk := source.read(1024 * 1024):
                total += len(chunk)
                if total > expected_size or total > MAX_ASSET_BYTES:
                    raise FinalizerError("snapshot_asset_hash_mismatch")
                digest.update(chunk)
                blob.write(chunk)
            if total != expected_size or digest.hexdigest() != expected_sha256:
                raise FinalizerError("snapshot_asset_hash_mismatch")
    except FinalizerError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise FinalizerError("snapshot_asset_hash_mismatch") from None
    finally:
        if source_fd is not None:
            try:
                os.close(source_fd)
            except OSError:
                pass


def _derived_metadata(
    source: dict[str, Any],
    snapshot_manifest: dict[str, Any],
    byte_metadata: dict[str, tuple[str, int]],
    collection_hashes: dict[str, str],
    producer_hash: str,
) -> dict[str, Any]:
    old = source["metadata"]
    counts = dict(old["counts"])
    counts["assetBytesRows"] = len(byte_metadata)
    counts["assetBytesTotal"] = sum(size for _sha256, size in byte_metadata.values())
    counts["unavailableAssets"] = 0
    issue_counts = {"upstream_resource_unavailable": len(source["warnings"])} if source["warnings"] else {}
    issues = [{"code": "upstream_resource_unavailable", "revisionId": revision_id} for revision_id in source["warnings"]]
    limits = {**old["limits"], "assetBytes": MAX_ASSET_BYTES, "totalAssetBytes": MAX_TOTAL_ASSET_BYTES}
    metadata = {
        "format": FORMAT_V2,
        "schemaVersion": 2,
        "sourceSchema": 12,
        "producerSha256": producer_hash,
        "createdAt": snapshot_manifest["capturedAt"],
        "scope": old["scope"],
        "limits": limits,
        "counts": counts,
        "complete": not bool(issue_counts),
        "offlineReady": True,
        "sourceSeedSha256": source["sourceHash"],
        "issues": issues,
        "issueCounts": issue_counts,
        "collectionsSha256": collection_hashes,
    }
    metadata["manifestSha256"] = _hash_bytes(_canonical_bytes(metadata))
    return metadata


def derive_catalog(
    input_path: Path,
    snapshot_manifest: Path,
    snapshot_root: Path,
    output_path: Path,
    *,
    expected_source_sha256: str | None = DEFAULT_SOURCE_SHA256,
) -> dict[str, Any]:
    """Validate v1, promote every admitted asset, and publish a new v2 copy."""
    input_path = Path(input_path)
    snapshot_manifest = Path(snapshot_manifest)
    snapshot_root = Path(snapshot_root)
    output_path = Path(output_path).absolute()
    if output_path.exists() or output_path.is_symlink():
        raise FinalizerError("catalog_output_exists")
    if not output_path.parent.is_dir():
        raise FinalizerError("catalog_output_parent_invalid")
    source = _read_v1(input_path, expected_source_sha256)
    try:
        if snapshot_root.is_symlink():
            raise FinalizerError("snapshot_root_invalid")
        snapshot_root_resolved = snapshot_root.resolve(strict=True)
        if not snapshot_root_resolved.is_dir() or snapshot_root_resolved.is_symlink():
            raise FinalizerError("snapshot_root_invalid")
    except (OSError, ValueError, RuntimeError):
        raise FinalizerError("snapshot_root_invalid") from None
    missing = set(source["assets"]) - set(source["originalBytes"])
    staging_directory = tempfile.TemporaryDirectory(prefix=".catalog-assets-", dir=output_path.parent)
    staging_root = Path(staging_directory.name)
    try:
        snapshot_json, promoted, effective_assets, _cleared_expected = _read_snapshot(
            snapshot_manifest,
            snapshot_root_resolved,
            source["sourceHash"],
            source["assets"],
            source["originalBytes"],
            missing,
            staging_root,
        )
        byte_metadata = dict(source["originalBytes"])
        promoted_assets = {asset_id: dict(row) for asset_id, row in effective_assets.items()}
        for asset_id, (entry, staged_path) in promoted.items():
            row = promoted_assets[asset_id]
            row["content_hash"] = entry["expectedSha256"]
            row["size_bytes"] = entry["sizeBytes"]
            row["detected_type"] = entry["detectedType"]
            row["cache_state"] = "ready"
            row["hash_verified"] = True
            row["fetched_at"] = entry["fetchedAt"]
            byte_metadata[asset_id] = (entry["expectedSha256"], entry["sizeBytes"])
        if set(byte_metadata) != set(promoted_assets) or len(byte_metadata) != len(promoted_assets):
            raise FinalizerError("catalog_assets_incomplete")
        total_bytes = sum(size for _sha256, size in byte_metadata.values())
        if total_bytes > MAX_TOTAL_ASSET_BYTES:
            raise FinalizerError("catalog_asset_budget_exceeded")
        for asset_id, row in promoted_assets.items():
            expected_sha256, expected_size = byte_metadata[asset_id]
            if (
                row.get("content_hash") != expected_sha256
                or row.get("size_bytes") != expected_size
                or row.get("cache_state") != "ready"
                or row.get("hash_verified") is not True
                or row.get("rights_status") not in RIGHTS
                or not isinstance(row.get("detected_type"), str)
                or not row["detected_type"]
                or row.get("expected_content_hash") not in {None, row["content_hash"]}
            ):
                raise FinalizerError("catalog_asset_invalid")

        collection_hashes: dict[str, str] = {}
        for table, records in (
            ("catalog_tasks", source["tasks"]),
            ("catalog_revisions", source["revisions"]),
            ("catalog_assets", promoted_assets),
            ("catalog_links", source["links"]),
        ):
            digest = hashlib.sha256()
            for identifier in sorted(records):
                digest.update(_line([identifier, records[identifier]]))
            collection_hashes[table] = digest.hexdigest()
        digest = hashlib.sha256()
        for revision_id in sorted(source["selection"]):
            digest.update(_line([revision_id]))
        collection_hashes["catalog_selection"] = digest.hexdigest()
        digest = hashlib.sha256()
        for asset_id in sorted(byte_metadata):
            expected_sha256, expected_size = byte_metadata[asset_id]
            digest.update(_line([asset_id, expected_sha256, expected_size]))
        collection_hashes["catalog_asset_bytes"] = digest.hexdigest()
        snapshot_manifest_json = snapshot_json
        metadata = _derived_metadata(
            source, snapshot_manifest_json, byte_metadata, collection_hashes,
            _hash_file(Path(__file__).resolve()),
        )
    except BaseException:
        staging_directory.cleanup()
        raise

    temporary: Path | None = None
    connection: sqlite3.Connection | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".catalog-seed-v2-", suffix=".sqlite3", dir=output_path.parent)
        os.close(descriptor)
        temporary = Path(name)
        _copy_sqlite_snapshot(input_path.resolve(strict=True), temporary, source["sourceHash"])
        connection = sqlite3.connect(temporary)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        for asset_id in sorted(promoted_assets):
            connection.execute(
                "UPDATE catalog_assets SET record_json=? WHERE id=?",
                (canonical(promoted_assets[asset_id]), asset_id),
            )
        for asset_id in sorted(promoted):
            entry, staged_path = promoted[asset_id]
            _insert_snapshot_bytes(
                connection,
                asset_id,
                entry["expectedSha256"],
                entry["sizeBytes"],
                staged_path,
            )
        connection.execute("DELETE FROM catalog_metadata")
        for key, value in sorted(metadata.items()):
            connection.execute("INSERT INTO catalog_metadata(key,value) VALUES (?,?)", (key, canonical(value)))
        connection.commit()
        connection.close()
        connection = None
        with temporary.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        output_hash = _hash_file(temporary)
        _publish_no_overwrite(temporary, output_path)
        temporary = None
        return {
            "format": FORMAT_V2,
            "status": "COMPLETE" if metadata["complete"] else "INCOMPLETE",
            "complete": metadata["complete"],
            "offlineReady": True,
            "exit": 0 if metadata["complete"] else 2,
            "fileSha256": output_hash,
            "sourceSeedSha256": source["sourceHash"],
            "manifestSha256": metadata["manifestSha256"],
            "collectionsSha256": collection_hashes,
            "counts": metadata["counts"],
            "issueCounts": metadata["issueCounts"],
        }
    except FinalizerError:
        raise
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        raise FinalizerError("catalog_publish_failed") from None
    finally:
        if connection is not None:
            connection.close()
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        staging_directory.cleanup()


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise FinalizerError("catalog_arguments_invalid")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("--input", "--database", dest="input_path", type=Path, required=True)
    parser.add_argument("--snapshot-manifest", type=Path, required=True)
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-source-sha256", default=DEFAULT_SOURCE_SHA256)
    try:
        args = parser.parse_args(argv)
        expected = args.expected_source_sha256
        if expected is not None:
            _require_hash(expected, "catalog_arguments_invalid")
        receipt = derive_catalog(
            args.input_path, args.snapshot_manifest, args.snapshot_root, args.output,
            expected_source_sha256=expected,
        )
    except Exception as failure:
        code = failure.code if isinstance(failure, FinalizerError) else "catalog_finalizer_failed"
        print(canonical({"format": FORMAT_V2, "status": "FAILED", "complete": False, "offlineReady": False, "exit": 1, "error": code}))
        return 1
    print(canonical(receipt))
    return receipt["exit"]


if __name__ == "__main__":
    raise SystemExit(main())

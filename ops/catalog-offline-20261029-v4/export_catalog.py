#!/usr/bin/env python3
"""Export an admitted schema-12 task catalog; no network or app imports.

Only explicitly supplied paths are used. The source is read-only; the one new
SQLite output is published without overwrite. See docs/CATALOG_SEED_FORMAT.md.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile
import time
import unicodedata


FORMAT = "kontur-catalog-seed-v1"
APPLICATION_ID = 0x4B435331
RIGHTS = ("private_authorized", "public_authorized")
SOURCE_NAME = "kompege"
MAX_ASSET_BYTES = 20_000_000
MAX_TOTAL_ASSET_BYTES = 2 * 1024**3
MAX_RECORD_BYTES = 4 * 1024**2
MAX_TEXT_BYTES = 2 * 1024**2
MAX_SOURCE_TEXT_BYTES = 256 * 1024**2
MAX_SQL_SECONDS = 120
MAX_EXPORT_SECONDS = 600
ROW_LIMITS = {"tasks": 100_000, "task_revisions": 250_000,
              "assets": 100_000, "task_assets": 500_000,
              "catalog_entries": 250_000}
COLUMNS = {
    "tasks": "id source_name external_id line_number current_revision_id created_at updated_at".split(),
    "task_revisions": (
        "id task_id revision_number source_url source_id line_number exam_year "
        "target_exam_year source_year source_year_basis source_created_at "
        "source_difficulty difficulty source_label source_metadata statement "
        "statement_latex answer explanation content_hash retrieved_at parser_version "
        "rights_status asset_manifest subtask_manifest table_manifest created_at"
    ).split(),
    "assets": (
        "id source_name canonical_url storage_key filename media_type detected_type "
        "size_bytes content_hash expected_content_hash cache_state rights_status "
        "hash_verified fetched_at created_at"
    ).split(),
    "task_assets": "id revision_id asset_id runner_filename position".split(),
    "catalog_entries": ["revision_id"],
}
OUTPUT_TABLES = {"tasks": "catalog_tasks", "task_revisions": "catalog_revisions",
                 "assets": "catalog_assets", "task_assets": "catalog_links"}
DDL = """
CREATE TABLE catalog_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE catalog_tasks (id TEXT PRIMARY KEY, record_json TEXT NOT NULL);
CREATE TABLE catalog_revisions (id TEXT PRIMARY KEY, record_json TEXT NOT NULL);
CREATE TABLE catalog_assets (id TEXT PRIMARY KEY, record_json TEXT NOT NULL);
CREATE TABLE catalog_links (id TEXT PRIMARY KEY, record_json TEXT NOT NULL);
CREATE TABLE catalog_selection (revision_id TEXT PRIMARY KEY);
CREATE TABLE catalog_asset_bytes (
    asset_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, bytes BLOB NOT NULL
);
"""
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:\-]{0,254}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_KEY = re.compile(r"([0-9a-f]{2})/([0-9a-f]{64})(?:\.[a-z0-9_\-]{1,15})?\Z")
_FORBIDDEN_KEYS = frozenset({
    "storage_key", "owner", "owner_id", "user_id", "email", "password",
    "passwd", "secret", "secrets", "token", "auth", "authorization",
    "cookie", "cookies", "session", "session_id", "profile", "config",
    "api_key", "private_key", "credentials", "access_token", "refresh_token",
    "path", "storage_path", "database_path", "owner_email", "credential",
    "secret_key", "profile_id", "cache_root", "asset_root",
    "key", "oauth", "jwt", "encryption_key", "key_material",
})
_FORBIDDEN_COMPACT_KEYS = frozenset(re.sub(r"[^a-z0-9]", "", key) for key in _FORBIDDEN_KEYS)
_SENSITIVE_TOKENS = frozenset({
    "storage", "path", "paths", "filepath", "directory", "database",
    "owner", "owners", "user", "users", "email", "credential", "credentials",
    "secret", "secrets", "password", "passwords", "passwd", "token", "tokens",
    "auth", "authentication", "authorization", "cookie", "cookies", "session",
    "sessions", "profile", "profiles", "config", "configuration",
    "key", "oauth", "jwt",
})
_SENSITIVE_AFFIXES = frozenset({
    "storage", "path", "database", "owner", "credential", "profile", "password",
    "passwd", "secret", "token", "cookie", "session", "config", "email",
    "apikey", "privatekey", "accesskey", "accesstoken", "refreshtoken",
    "authorization", "authentication", "cacheroot", "assetroot", "userid",
    "oauth", "jwt", "encryptionkey", "keymaterial",
})
_NULLABLE = frozenset({
    "current_revision_id", "exam_year", "target_exam_year", "source_year",
    "source_year_basis", "source_created_at", "source_difficulty", "difficulty",
    "source_label", "statement_latex", "answer", "explanation", "table_manifest",
    "storage_key", "detected_type", "expected_content_hash", "fetched_at",
})
_INTEGER_FIELDS = frozenset({"line_number", "revision_number", "exam_year",
    "target_exam_year", "source_year", "source_difficulty", "size_bytes", "position"})
_JSON_FIELDS = {"source_metadata": dict, "asset_manifest": list,
                "subtask_manifest": list, "table_manifest": dict}


class SeedError(Exception):
    """Fixed public error code; never include paths, values or raw exceptions."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _line(value: object) -> bytes:
    return (canonical(value) + "\n").encode("utf-8")


def _check_deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise SeedError("catalog_deadline_exceeded")


def _identifier(value: object) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        raise SeedError("catalog_identifier_invalid")
    return value


def _json_pairs(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise SeedError("catalog_json_duplicate_key")
        result[key] = value
    return result


def _sensitive_json_key(key: str) -> bool:
    # NFKC also handles full-width spellings. Split camel/acronym words, then
    # discard separators for an additional prefix/suffix check. Check keys at
    # every JSON depth; source text values are deliberately not redacted.
    normalized = unicodedata.normalize("NFKC", key)
    normalized = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", normalized)
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", normalized).casefold()
    tokens = re.findall(r"[a-z0-9]+", normalized)
    compact = "".join(tokens)
    if _SENSITIVE_TOKENS.intersection(tokens) or compact in _FORBIDDEN_COMPACT_KEYS:
        return True
    if any(compact.startswith(word) or compact.endswith(word) for word in _SENSITIVE_AFFIXES):
        return True
    # Keep legitimate author/authorship metadata while rejecting compact auth
    # spellings that have no separator (authcontext, requestauth, etc.).
    return (compact.startswith("auth") and not compact.startswith("author")) or compact.endswith("auth")


def _json_value(raw: str, expected: type, *, nullable: bool = False) -> object:
    try:
        value = json.loads(raw, object_pairs_hook=_json_pairs,
                           parse_constant=lambda _v: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise SeedError("catalog_json_invalid") from None
    # SQLAlchemy JSON(none_as_null=False) writes a nullable Python None as the
    # JSON text "null". It has the same logical value as SQL NULL, and is valid
    # only when the existing source column contract explicitly permits null.
    if value is None and nullable:
        return None
    if type(value) is not expected:
        raise SeedError("catalog_json_shape_invalid")
    stack = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > 24 or nodes > 100_000:
            raise SeedError("catalog_json_limit_exceeded")
        if type(item) is dict:
            for key, child in item.items():
                if _sensitive_json_key(key):
                    raise SeedError("catalog_metadata_disallowed_key")
                stack.append((child, depth + 1))
        elif type(item) is list:
            stack.extend((child, depth + 1) for child in item)
        elif type(item) is float and not math.isfinite(item):
            raise SeedError("catalog_json_invalid")
    return value


def _validate_record(table: str, row: dict) -> dict:
    result = {}
    for field in COLUMNS[table]:
        value = row[field]
        if value is None:
            if field not in _NULLABLE:
                raise SeedError("catalog_null_invalid")
        elif field == "hash_verified":
            if type(value) is not int or value not in (0, 1):
                raise SeedError("catalog_boolean_invalid")
            value = bool(value)
        elif field in _INTEGER_FIELDS:
            if type(value) is not int or not 0 <= value <= 2**63 - 1:
                raise SeedError("catalog_integer_invalid")
            if field == "line_number" and not 1 <= value <= 27:
                raise SeedError("catalog_line_invalid")
            if field == "revision_number" and value < 1:
                raise SeedError("catalog_revision_invalid")
        elif type(value) is not str or len(value.encode("utf-8")) > MAX_TEXT_BYTES or "\x00" in value:
            raise SeedError("catalog_text_invalid")
        elif field in _JSON_FIELDS:
            value = _json_value(value, _JSON_FIELDS[field], nullable=field in _NULLABLE)
        if field in {"id", "task_id", "revision_id", "asset_id", "current_revision_id"} and value is not None:
            _identifier(value)
        result[field] = value
    if "source_name" in result and result["source_name"] != SOURCE_NAME:
        raise SeedError("catalog_source_invalid")
    if "rights_status" in result and result["rights_status"] not in RIGHTS:
        raise SeedError("catalog_rights_invalid")
    for key in ("filename", "runner_filename"):
        if key in result and (not result[key] or result[key] in {".", ".."} or any(c in result[key] for c in "/\\:\r\n")):
            raise SeedError("catalog_filename_invalid")
    if table == "task_revisions" and not _HASH.fullmatch(result["content_hash"]):
        raise SeedError("catalog_revision_hash_invalid")
    if len(canonical(result).encode("utf-8")) > MAX_RECORD_BYTES:
        raise SeedError("catalog_record_limit_exceeded")
    return result


class Source:
    def __init__(self, connection: sqlite3.Connection, deadline: float):
        self.connection = connection
        self.deadline = deadline
        self.text_bytes = 0
        self.read_columns: set[tuple[str, str]] = set()

    def authorizer(self, action: int, table: str | None, column: str | None,
                   database: str | None, _origin: str | None) -> int:
        if database not in (None, "main"):
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            if table in ("sqlite_master", "sqlite_schema") and column in {"name", "type", "tbl_name", "sql"}:
                return sqlite3.SQLITE_OK
            if table in COLUMNS and (column in COLUMNS[table] or column == ""):
                if column:  # COUNT(*) has no data column to list in the receipt.
                    self.read_columns.add((table, column))
                return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and column == "count":
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_PRAGMA and table == "table_xinfo" and column in COLUMNS:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_TRANSACTION and table in ("BEGIN", "ROLLBACK"):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def rows(self, sql: str, parameters: tuple = (), *, limit: int) -> list[dict]:
        output = []
        for row in self.connection.execute(sql, parameters):
            _check_deadline(self.deadline)
            if len(output) >= limit:
                raise SeedError("catalog_row_limit_exceeded")
            for value in row:
                if type(value) is str:
                    size = len(value.encode("utf-8"))
                    if size > MAX_TEXT_BYTES:
                        raise SeedError("catalog_text_limit_exceeded")
                    self.text_bytes += size
                elif isinstance(value, bytes):
                    raise SeedError("catalog_source_blob_rejected")
            if self.text_bytes > MAX_SOURCE_TEXT_BYTES:
                raise SeedError("catalog_source_budget_exceeded")
            output.append(dict(row))
        return output

    def schema(self) -> None:
        placeholders = ",".join("?" for _ in COLUMNS)
        objects = self.rows(
            f"SELECT name,type,tbl_name,sql FROM sqlite_schema WHERE tbl_name IN ({placeholders}) AND type IN ('table','view','trigger')",
            tuple(COLUMNS), limit=32)
        if len(objects) != len(COLUMNS):
            raise SeedError("catalog_schema_objects_invalid")
        for row in objects:
            if row["name"] not in COLUMNS or row["type"] != "table" or not row["sql"] or "VIRTUAL" in row["sql"].upper().split():
                raise SeedError("catalog_schema_objects_invalid")
            information = list(self.connection.execute(f"PRAGMA table_xinfo({row['name']})"))
            names = {entry["name"] for entry in information}
            if not set(COLUMNS[row["name"]]) <= names or any(entry["hidden"] for entry in information):
                raise SeedError("catalog_schema_columns_invalid")


@contextmanager
def source_snapshot(database: Path):
    connection = None
    try:
        database = Path(database).resolve(strict=True)
        if not database.is_file():
            raise SeedError("catalog_source_missing")
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3)
        connection.row_factory = sqlite3.Row
        connection.enable_load_extension(False)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA busy_timeout=3000")
        if not hasattr(connection, "setlimit"):
            raise SeedError("catalog_sqlite_runtime_unsupported")
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 2 * MAX_RECORD_BYTES)
        source = Source(connection, time.monotonic() + MAX_SQL_SECONDS)
        connection.set_authorizer(source.authorizer)
        connection.set_progress_handler(lambda: int(time.monotonic() > source.deadline), 1000)
        connection.execute("BEGIN")
        source.schema()
        yield source
    except SeedError:
        raise
    except (sqlite3.Error, OSError, UnicodeError, ValueError, RuntimeError):
        raise SeedError("catalog_source_read_failed") from None
    finally:
        if connection is not None:
            try:
                connection.rollback()
            finally:
                connection.close()


def _projection(table: str, alias: str) -> str:
    # Only the fixed, reviewed internal map is used to construct SQL identifiers.
    return ",".join(alias + "." + column for column in COLUMNS[table])


def _fingerprint(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _entry_fingerprint(info: os.stat_result) -> tuple:
    # On the installed Windows Python, fstat and lstat expose different ctime
    # meanings after a file rewrite (measured on the synthetic fixture). Keep
    # the full same-handle before/after check, but compare stable file identity,
    # size and mtime across these two APIs. The actual bytes are also SHA-pinned.
    value = _fingerprint(info)
    return value[:-1] if os.name == "nt" else value


def _regular(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400)


def _windows_handle_path(descriptor: int) -> Path:
    import ctypes
    import msvcrt
    from ctypes import wintypes
    function = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
    function.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    function.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = function(msvcrt.get_osfhandle(descriptor), buffer, len(buffer), 0)
    if length == 0 or length >= len(buffer):
        raise SeedError("asset_path_invalid")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def read_asset(root: Path, row: dict, deadline: float) -> bytes:
    """Read only a digest-pinned cache file; errors never expose its storage key."""
    digest = row["content_hash"]
    if type(digest) is not str or not _HASH.fullmatch(digest):
        raise SeedError("asset_hash_invalid")
    expected = row["expected_content_hash"]
    if expected is not None and (not _HASH.fullmatch(expected) or expected != digest):
        raise SeedError("asset_expected_hash_mismatch")
    if row["cache_state"] != "ready":
        raise SeedError("asset_not_ready")
    if not row["hash_verified"]:
        raise SeedError("asset_hash_unverified")
    if row["size_bytes"] > MAX_ASSET_BYTES:
        raise SeedError("asset_too_large")
    key = row["storage_key"]
    match = _KEY.fullmatch(key) if type(key) is str else None
    if not match or match[1] != digest[:2] or match[2] != digest:
        raise SeedError("asset_path_invalid")
    prefix, filename = key.split("/")
    descriptors = []
    descriptor = None
    try:
        path = root / prefix / filename
        for component in (root / prefix, path):
            info = component.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise SeedError("asset_path_invalid")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        if os.name != "nt" and os.open in os.supports_dir_fd:
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            root_fd = os.open(root, directory_flags)
            descriptors.append(root_fd)
            directory_fd = os.open(prefix, directory_flags, dir_fd=root_fd)
            descriptors.append(directory_fd)
            descriptor = os.open(filename, flags, dir_fd=directory_fd)
        else:
            descriptor = os.open(path, flags)
            if os.name == "nt":
                final = _windows_handle_path(descriptor)
                if os.path.normcase(str(final)) != os.path.normcase(str(path)):
                    raise SeedError("asset_path_invalid")
            elif path.resolve(strict=True) != path:
                raise SeedError("asset_path_invalid")
        before = os.fstat(descriptor)
        if not _regular(before):
            raise SeedError("asset_not_regular")
        if before.st_size > MAX_ASSET_BYTES:
            raise SeedError("asset_too_large")
        if before.st_size != row["size_bytes"]:
            raise SeedError("asset_size_mismatch")
        chunks = []
        remaining = before.st_size
        while remaining:
            _check_deadline(deadline)
            chunk = os.read(descriptor, min(1024**2, remaining))
            if not chunk:
                raise SeedError("asset_changed")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1) or _fingerprint(before) != _fingerprint(os.fstat(descriptor)):
            raise SeedError("asset_changed")
        # Recheck the directory entry without following links after the read.
        after = (os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
                 if descriptors else path.lstat())
        if not _regular(after) or _entry_fingerprint(before) != _entry_fingerprint(after):
            raise SeedError("asset_changed")
        data = b"".join(chunks)
        if _hash(data) != digest:
            raise SeedError("asset_hash_mismatch")
        return data
    except SeedError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise SeedError("asset_missing_or_unreadable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


class Issues:
    def __init__(self):
        self.counts: Counter = Counter()
        self.examples: list[dict] = []
        self.seen: set[tuple] = set()

    def add(self, code: str, *, asset_id: str | None = None, revision_id: str | None = None,
            reference: str | None = None):
        key = (code, asset_id, revision_id, reference)
        if key in self.seen:
            return
        self.seen.add(key)
        self.counts[code] += 1
        if len(self.seen) > 1_000_000:
            raise SeedError("catalog_issue_limit_exceeded")
        if len(self.examples) < 1000:
            example = {"code": code}
            if asset_id is not None:
                example["assetId"] = asset_id
            if revision_id is not None:
                example["revisionId"] = revision_id
            if reference is not None:
                example["referenceSha256"] = reference
            self.examples.append(example)


class Catalog:
    def __init__(self, source: Source):
        self.source = source
        self.issues = Issues()
        self.tasks: dict[str, dict] = {}
        self.revisions: dict[str, dict] = {}
        self.assets: dict[str, dict] = {}
        self.links: dict[str, dict] = {}
        self.selection: set[str] = set()
        self.suppressed_pointers = 0
        self.excluded: dict[str, int] = {}

    def _collection(self, table: str, sql: str, parameters: tuple) -> dict[str, dict]:
        rows = self.source.rows(sql, parameters, limit=ROW_LIMITS[table])
        result = {}
        for row in rows:
            row = _validate_record(table, row)
            if row["id"] in result:
                raise SeedError("catalog_duplicate_id")
            result[row["id"]] = row
        return result

    def asset(self, asset_id: str, revision_id: str) -> None:
        _identifier(asset_id)
        if asset_id in self.assets:
            return
        rows = self.source.rows(
            "SELECT " + _projection("assets", "a") +
            " FROM assets a WHERE a.id=? AND a.source_name=? AND a.rights_status IN (?,?) ORDER BY a.id",
            (asset_id, SOURCE_NAME, *RIGHTS), limit=1)
        if not rows:
            self.issues.add("asset_row_missing_or_not_authorized", asset_id=asset_id, revision_id=revision_id)
            return
        if len(self.assets) >= ROW_LIMITS["assets"]:
            raise SeedError("catalog_row_limit_exceeded")
        self.assets[asset_id] = _validate_record("assets", rows[0])

    def manifest_asset(self, entry: dict, revision_id: str) -> None:
        if type(entry) is not dict:
            raise SeedError("catalog_manifest_shape_invalid")
        reference = _hash(canonical(entry).encode("utf-8"))
        if entry.get("asset_id") is not None:
            identifier = _identifier(entry["asset_id"])
            self.asset(identifier, revision_id)
            row = self.assets.get(identifier)
            if row is not None:
                expected = entry.get("expected_hash") or entry.get("expected_content_hash")
                pinned = expected or (entry.get("content_hash") if type(entry.get("size_bytes")) is int and entry["size_bytes"] > 0 else None)
                if pinned is not None and pinned != row["content_hash"]:
                    self.issues.add("manifest_asset_revision_mismatch", asset_id=identifier,
                                    revision_id=revision_id, reference=reference)
            return
        url, filename = entry.get("url"), entry.get("filename")
        if type(url) is not str or type(filename) is not str:
            self.issues.add("manifest_asset_unresolved", revision_id=revision_id, reference=reference)
            return
        expected = entry.get("expected_hash") or entry.get("expected_content_hash")
        parameters = (url, filename, SOURCE_NAME, *RIGHTS)
        query = "SELECT a.id FROM assets a WHERE a.canonical_url=? AND a.filename=? AND a.source_name=? AND a.rights_status IN (?,?)"
        if expected:
            if type(expected) is not str or not _HASH.fullmatch(expected):
                raise SeedError("catalog_manifest_hash_invalid")
            query += " AND (a.expected_content_hash=? OR a.content_hash=?)"
            parameters += (expected, expected)
        rows = self.source.rows(query + " ORDER BY a.id LIMIT 2", parameters, limit=2)
        if len(rows) != 1:
            self.issues.add("manifest_asset_missing_or_ambiguous", revision_id=revision_id, reference=reference)
            return
        self.asset(_identifier(rows[0]["id"]), revision_id)

    def load(self) -> None:
        # SQL NULL comparisons would otherwise silently hide malformed rows.
        # Count only whitelisted columns; no owner/job or migration data is read.
        invalid_queries = (
            "SELECT count(*) AS amount FROM tasks t WHERE t.id IS NULL OR t.source_name IS NULL",
            "SELECT count(*) AS amount FROM task_revisions r LEFT JOIN tasks t ON t.id=r.task_id WHERE r.id IS NULL OR r.task_id IS NULL OR r.rights_status IS NULL OR t.id IS NULL",
            "SELECT count(*) AS amount FROM task_assets l LEFT JOIN task_revisions r ON r.id=l.revision_id WHERE l.id IS NULL OR l.revision_id IS NULL OR l.asset_id IS NULL OR r.id IS NULL",
            "SELECT count(*) AS amount FROM catalog_entries ce LEFT JOIN task_revisions r ON r.id=ce.revision_id WHERE ce.revision_id IS NULL OR r.id IS NULL",
        )
        for query in invalid_queries:
            if self.source.rows(query, limit=1)[0]["amount"]:
                raise SeedError("catalog_source_rows_invalid")
        admitted = (SOURCE_NAME, *RIGHTS)
        eligible = " FROM task_revisions r JOIN tasks t ON t.id=r.task_id WHERE t.source_name=? AND r.rights_status IN (?,?)"
        self.revisions = self._collection("task_revisions", "SELECT " + _projection("task_revisions", "r") + eligible + " ORDER BY r.id", admitted)
        self.tasks = self._collection("tasks", "SELECT " + _projection("tasks", "t") +
            " FROM tasks t WHERE t.source_name=? AND EXISTS (SELECT r.id FROM task_revisions r WHERE r.task_id=t.id AND r.rights_status IN (?,?)) ORDER BY t.id", admitted)
        for revision in self.revisions.values():
            if revision["task_id"] not in self.tasks:
                raise SeedError("catalog_task_reference_invalid")
        for task in self.tasks.values():
            current = task["current_revision_id"]
            if current in self.revisions:
                if self.revisions[current]["task_id"] != task["id"]:
                    raise SeedError("catalog_current_reference_invalid")
                self.selection.add(current)
            elif current is not None:
                task["current_revision_id"] = None
                self.suppressed_pointers += 1
        pinned = self.source.rows(
            "SELECT DISTINCT ce.revision_id FROM catalog_entries ce JOIN task_revisions r ON r.id=ce.revision_id JOIN tasks t ON t.id=r.task_id WHERE t.source_name=? AND r.rights_status IN (?,?) ORDER BY ce.revision_id",
            admitted, limit=ROW_LIMITS["catalog_entries"])
        self.selection.update(_identifier(row["revision_id"]) for row in pinned)
        if not self.selection <= self.revisions.keys() or len(self.selection) > ROW_LIMITS["catalog_entries"]:
            raise SeedError("catalog_selection_invalid")
        self.links = self._collection("task_assets", "SELECT " + _projection("task_assets", "l") +
            " FROM task_assets l JOIN task_revisions r ON r.id=l.revision_id JOIN tasks t ON t.id=r.task_id WHERE t.source_name=? AND r.rights_status IN (?,?) ORDER BY l.id", admitted)
        for link in self.links.values():
            self.asset(link["asset_id"], link["revision_id"])
        for revision in self.revisions.values():
            for entry in revision["asset_manifest"]:
                self.manifest_asset(entry, revision["id"])
            for subtask in revision["subtask_manifest"]:
                if type(subtask) is not dict or type(subtask.get("assets", [])) is not list:
                    raise SeedError("catalog_manifest_shape_invalid")
                for entry in subtask.get("assets", []):
                    self.manifest_asset(entry, revision["id"])
            unavailable = revision["source_metadata"].get("kege_unavailable_resources", [])
            if type(unavailable) is not list:
                raise SeedError("catalog_manifest_shape_invalid")
            if unavailable:
                self.issues.add("upstream_resource_unavailable", revision_id=revision["id"])
        for key, condition, parameters in (
            ("deniedRevisions", "t.source_name=? AND (r.rights_status IS NULL OR r.rights_status NOT IN (?,?))", admitted),
            ("otherSourceRevisions", "t.source_name IS NULL OR t.source_name<>?", (SOURCE_NAME,)),
        ):
            rows = self.source.rows("SELECT count(r.id) AS amount FROM task_revisions r JOIN tasks t ON t.id=r.task_id WHERE " + condition, parameters, limit=1)
            self.excluded[key] = rows[0]["amount"]
        if not self.revisions:
            self.issues.add("catalog_empty")
        elif not self.selection:
            self.issues.add("catalog_selection_empty")


def _publish_no_overwrite(temporary: Path, output: Path) -> None:
    if os.name == "nt":
        os.rename(temporary, output)
    else:
        os.link(temporary, output)
        temporary.unlink()
        descriptor = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def export_catalog(database: Path, asset_root: Path, output: Path) -> dict:
    """Return a safe receipt. complete=False is an explicit, inspectable result."""
    deadline = time.monotonic() + MAX_EXPORT_SECONDS
    temporary = None
    connection = None
    try:
        asset_root = Path(asset_root).resolve(strict=True)
        if not asset_root.is_dir():
            raise SeedError("catalog_asset_root_invalid")
        output = Path(output).absolute()
        if output.exists() or output.is_symlink():
            raise SeedError("catalog_output_exists")
        parent = output.parent.resolve(strict=True)
        output = parent / output.name
        if parent == asset_root or asset_root in parent.parents:
            raise SeedError("catalog_output_inside_cache")
        if not parent.is_dir():
            raise SeedError("catalog_output_parent_invalid")
        with source_snapshot(database) as source:
            catalog = Catalog(source)
            catalog.load()
            _check_deadline(deadline)
            descriptor, name = tempfile.mkstemp(prefix=".catalog-seed-", suffix=".sqlite3", dir=parent)
            os.close(descriptor)
            temporary = Path(name)
            connection = sqlite3.connect(temporary)
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute("PRAGMA user_version=1")
            connection.executescript(DDL)  # Constant DDL, on the new output only.
            hashes = {}
            collections = {"tasks": catalog.tasks, "task_revisions": catalog.revisions,
                           "assets": catalog.assets, "task_assets": catalog.links}
            for table, records in collections.items():
                digest = hashlib.sha256()
                for identifier, original in sorted(records.items()):
                    _check_deadline(deadline)
                    row = {key: value for key, value in original.items() if key != "storage_key"}
                    connection.execute(f"INSERT INTO {OUTPUT_TABLES[table]} (id,record_json) VALUES (?,?)", (identifier, canonical(row)))
                    digest.update(_line([identifier, row]))
                hashes[OUTPUT_TABLES[table]] = digest.hexdigest()
            digest = hashlib.sha256()
            for revision_id in sorted(catalog.selection):
                connection.execute("INSERT INTO catalog_selection (revision_id) VALUES (?)", (revision_id,))
                digest.update(_line([revision_id]))
            hashes["catalog_selection"] = digest.hexdigest()
            digest = hashlib.sha256()
            asset_bytes, byte_rows = 0, 0
            for identifier, row in sorted(catalog.assets.items()):
                _check_deadline(deadline)
                if row["size_bytes"] <= MAX_ASSET_BYTES and asset_bytes + row["size_bytes"] > MAX_TOTAL_ASSET_BYTES:
                    raise SeedError("catalog_asset_budget_exceeded")
                try:
                    data = read_asset(asset_root, row, deadline)
                except SeedError as failure:
                    if failure.code == "catalog_deadline_exceeded":
                        raise
                    catalog.issues.add(failure.code, asset_id=identifier)
                    continue
                asset_bytes += len(data)
                byte_rows += 1
                connection.execute("INSERT INTO catalog_asset_bytes (asset_id,sha256,bytes) VALUES (?,?,?)", (identifier, row["content_hash"], data))
                digest.update(_line([identifier, row["content_hash"], len(data)]))
            hashes["catalog_asset_bytes"] = digest.hexdigest()
            counts = {"tasks": len(catalog.tasks), "revisions": len(catalog.revisions),
                      "assets": len(catalog.assets), "links": len(catalog.links),
                      "selection": len(catalog.selection), "assetBytesRows": byte_rows,
                      "assetBytesTotal": asset_bytes,
                      "unavailableAssets": len(catalog.assets) - byte_rows,
                      "unresolvedAssetReferences": sum(catalog.issues.counts[code] for code in (
                          "asset_row_missing_or_not_authorized", "manifest_asset_unresolved",
                          "manifest_asset_missing_or_ambiguous")),
                      "suppressedCurrentPointers": catalog.suppressed_pointers,
                      **catalog.excluded}
            metadata = {
                "format": FORMAT, "schemaVersion": 1, "sourceSchema": 12,
                "producerSha256": _hash(Path(__file__).read_bytes()),
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "scope": {"sourceName": SOURCE_NAME, "rights": list(RIGHTS),
                          "revisions": "all-admitted-history",
                          "selection": "current-plus-admitted-catalog-entry-union",
                          "personalDeviceOnly": True},
                "limits": {"rowCounts": ROW_LIMITS, "recordBytes": MAX_RECORD_BYTES,
                           "textBytes": MAX_TEXT_BYTES, "sourceTextBytes": MAX_SOURCE_TEXT_BYTES,
                           "assetBytes": MAX_ASSET_BYTES, "totalAssetBytes": MAX_TOTAL_ASSET_BYTES,
                           "jsonDepth": 24, "jsonNodes": 100_000,
                           "sqlSeconds": MAX_SQL_SECONDS, "exportSeconds": MAX_EXPORT_SECONDS},
                "counts": counts, "complete": not bool(catalog.issues.counts),
                "issues": catalog.issues.examples, "issueCounts": dict(sorted(catalog.issues.counts.items())),
                "collectionsSha256": hashes,
            }
            metadata["manifestSha256"] = _hash(canonical(metadata).encode("utf-8"))
            for key, value in sorted(metadata.items()):
                connection.execute("INSERT INTO catalog_metadata (key,value) VALUES (?,?)", (key, canonical(value)))
            _check_deadline(deadline)
            connection.commit()
            connection.close()
            connection = None
            with temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            seed_hash = hashlib.sha256()
            with temporary.open("rb") as handle:
                while chunk := handle.read(1024**2):
                    _check_deadline(deadline)
                    seed_hash.update(chunk)
            _publish_no_overwrite(temporary, output)
            temporary = None
            return {"format": FORMAT, "status": "COMPLETE" if metadata["complete"] else "INCOMPLETE",
                    "complete": metadata["complete"], "exit": 0 if metadata["complete"] else 2,
                    "fileSha256": seed_hash.hexdigest(), "manifestSha256": metadata["manifestSha256"],
                    "collectionsSha256": hashes, "counts": counts,
                    "issueCounts": metadata["issueCounts"],
                    "sourceReadColumns": {table: sorted(column for name, column in source.read_columns if name == table)
                                          for table in COLUMNS}}
    except SeedError:
        raise
    except (OSError, sqlite3.Error, UnicodeError, ValueError, RuntimeError):
        raise SeedError("catalog_export_failed") from None
    finally:
        if connection is not None:
            connection.close()
        if temporary is not None:
            # This path comes only from our exclusive mkstemp above.
            temporary.unlink(missing_ok=True)


class SafeParser(argparse.ArgumentParser):
    def error(self, _message: str):
        raise SeedError("catalog_arguments_invalid")


def main(argv: list[str] | None = None) -> int:
    parser = SafeParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--asset-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    try:
        arguments = parser.parse_args(argv)
        receipt = export_catalog(arguments.database, arguments.asset_root, arguments.output)
    except Exception as failure:
        # The CLI is a privacy boundary: even an unexpected runtime/path error
        # must not expose raw diagnostics, a traceback or internal filesystem paths.
        code = failure.code if isinstance(failure, SeedError) else "catalog_export_failed"
        print(canonical({"format": FORMAT, "status": "FAILED", "complete": False,
                         "exit": 1, "error": code}))
        return 1
    print(canonical(receipt))
    return receipt["exit"]


if __name__ == "__main__":
    raise SystemExit(main())

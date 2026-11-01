"""Offline, data-only transfer for the full Kontur backend.

The native seven-table ``kontur-transfer-v1`` format predates the full Python
backend.  It remains a supported input, but the Python backend writes a
separate ``kontur-full-v1`` container.  A full container contains the current
SQLAlchemy application tables and a fixed asset table; it never contains an
executable SQL script.  During import the archive is copied to a private
read-only file, its fixed schema is checked, and only parameterised statements
against the local trusted schema are used.

This module intentionally has no application startup side effects.  SQLAlchemy
is imported lazily when a manager is used, which keeps the format validator
usable by small standard-library fixtures as well.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import uuid
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

# ``KONT`` is the historical transfer container, ``KONL`` is the native local
# store and ``KONF`` identifies the full Python backend backup.
TRANSFER_APPLICATION_ID = 0x4B4F4E54
LOCAL_APPLICATION_ID = 0x4B4F4E4C
FULL_APPLICATION_ID = 0x4B4F4E46

TRANSFER_FORMAT = "kontur-transfer-v1"
LOCAL_FORMAT = "kontur-local-v1"
FULL_FORMAT = "kontur-full-v1"
SCHEMA_VERSION = 1

MAX_CONTAINER_BYTES = 3 * 1024**3 + 512 * 1024**2
MAX_LEGACY_CONTAINER_BYTES = 256 * 1024 * 1024
MAX_FULL_TABLE_BYTES = 128 * 1024 * 1024
COPY_CHUNK_BYTES = 64 * 1024
MAX_ASSET_BYTES = 16 * 1024 * 1024
MAX_FULL_ASSET_BYTES = 64_000_000
MAX_TOTAL_ASSET_BYTES = 128 * 1024 * 1024
MAX_FULL_TOTAL_ASSET_BYTES = 3 * 1024**3
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_SMALL_BYTES = 64 * 1024
MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_TIMESTAMP_MS = 8_640_000_000_000_000
MAX_TABLES = 128
MAX_ROWS_PER_TABLE = 2_000_000
MAX_FULL_VALUE_BYTES = 8 * 1024 * 1024
MAX_CLIENT_STATE_RAW_BYTES = 4 * MAX_FULL_VALUE_BYTES
MAX_ASSET_PATH_BYTES = 512

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MIME_RE = re.compile(r"[A-Za-z0-9!#$&^_.+\-]+/[A-Za-z0-9!#$&^_.+\-]+\Z")
_FORMAT_HEADER = b"SQLite format 3\x00"


class TransferError(ValueError):
    """Safe, stable error returned to the native adapter."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _error(code: str, message: str) -> TransferError:
    return TransferError(code, message)


def _invalid() -> TransferError:
    return _error("invalid_bundle", "Данные не соответствуют формату переноса.")


def _limit() -> TransferError:
    return _error("limit_exceeded", "Размер данных превышает допустимый предел.")


def _conflict() -> TransferError:
    return _error("record_conflict", "Одинаковый идентификатор содержит разные данные. Импорт отменён.")


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise _invalid() from exc


def _json_loads(raw: str | bytes, maximum: int) -> Any:
    if isinstance(raw, bytes):
        if len(raw) > maximum:
            raise _limit()
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _invalid() from exc
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > maximum:
        raise _limit()
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, RecursionError) as exc:
        raise _invalid() from exc
    _check_json_depth(value)
    return value


def _check_json_depth(value: Any, depth: int = 0) -> None:
    if depth > 126:
        raise _limit()
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise _invalid()
            _check_json_depth(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _check_json_depth(child, depth + 1)
    elif value is not None and type(value) not in (str, int, float, bool):
        raise _invalid()


def _text(value: Any, maximum: int, *, nonempty: bool = False) -> str:
    if not isinstance(value, str):
        raise _invalid()
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise _invalid() from exc
    if size > maximum or (nonempty and size == 0) or "\x00" in value:
        raise _limit() if size > maximum else _invalid()
    return value


def _identifier(value: Any, maximum: int = 256) -> str:
    value = _text(value, maximum, nonempty=True)
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        raise _invalid()
    return value


def _integer(value: Any, minimum: int = 0, maximum: int = MAX_SAFE_INTEGER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _invalid()
    return value


def _timestamp(value: Any) -> int:
    return _integer(value, 0, MAX_TIMESTAMP_MS)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _quote(name: str) -> str:
    # Names originate in trusted SQLAlchemy metadata or fixed constants.  The
    # quote helper still makes all local SQL construction explicit.
    return '"' + name.replace('"', '""') + '"'


def _normalize_type(value: str) -> str:
    return " ".join(str(value).upper().split())


def _safe_relative_path(value: Any, *, maximum: int = MAX_ASSET_PATH_BYTES) -> str:
    path = _text(value, maximum, nonempty=True)
    if "\\" in path or ":" in path or path.startswith("/"):
        raise _invalid()
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise _invalid()
    parts = path.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise _invalid()
    return path


def _safe_filename(value: Any) -> str:
    filename = _text(value, 512, nonempty=True)
    if any(char in filename for char in "/\\:") or any(ord(char) < 32 or ord(char) == 127 for char in filename):
        raise _invalid()
    return filename


def _encode_value(value: Any) -> Any:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if value is None or type(value) in (str, int, float, bool):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise _invalid()
        return value
    # Raw sqlite3/SQLAlchemy driver rows normally contain only primitive
    # values.  Datetimes are accepted for custom test engines and round-trip
    # as tagged text without changing their meaning.
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    raise _invalid()


def _decode_value(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"$bytes"}:
        encoded = value["$bytes"]
        if not isinstance(encoded, str) or len(encoded) > MAX_FULL_VALUE_BYTES * 2:
            raise _invalid()
        try:
            return base64.b64decode(encoded.encode("ascii"), validate=True)
        except (ValueError, UnicodeEncodeError) as exc:
            raise _invalid() from exc
    if isinstance(value, dict) and set(value) == {"$datetime"}:
        raw = value["$datetime"]
        if not isinstance(raw, str):
            raise _invalid()
        try:
            return datetime.fromisoformat(raw).replace(tzinfo=None).isoformat(sep=" ")
        except ValueError as exc:
            raise _invalid() from exc
    return value


def _row_fingerprint(table: str, columns: Sequence[str], values: Sequence[Any]) -> str:
    encoded = [_encode_value(value) for value in values]
    return _sha256(_canonical_json({"table": table, "columns": list(columns), "values": encoded}).encode("utf-8"))


def _uuid5(prefix: str, value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"kontur:{prefix}:{value}"))


def _utc_from_ms(value: int) -> datetime:
    _timestamp(value)
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError) as exc:
        raise _invalid() from exc


@dataclass(frozen=True)
class _ColumnSpec:
    name: str
    declared_type: str
    nullable: bool
    primary_key: int
    foreign_table: str | None = None
    foreign_column: str | None = None


@dataclass
class _LegacyBundle:
    metadata: dict[str, str]
    tasks: list[dict[str, Any]]
    heads: dict[str, str]
    attempts: list[dict[str, Any]]
    documents: list[dict[str, Any]]
    assets: list[dict[str, Any]]
    preferences: dict[str, Any]
    digest: str
    local_store: bool = False


@dataclass
class _FullBundle:
    metadata: dict[str, str]
    tables: dict[str, list[tuple[Any, ...]]]
    assets: dict[str, dict[str, Any]]
    digest: str


@dataclass
class _ArchiveAsset:
    connection: sqlite3.Connection
    rowid: int
    size: int
    checksum: str

    def __len__(self) -> int:
        return self.size

    def copy_to(self, output: BinaryIO | None = None) -> None:
        digest = hashlib.sha256()
        try:
            with self.connection.blobopen("transfer_assets", "bytes", self.rowid, readonly=True) as blob:
                if len(blob) != self.size or not 0 <= self.size <= MAX_FULL_ASSET_BYTES:
                    raise _invalid()
                remaining = self.size
                while remaining:
                    chunk = blob.read(min(remaining, COPY_CHUNK_BYTES))
                    if not chunk:
                        raise _invalid()
                    digest.update(chunk)
                    if output is not None:
                        output.write(chunk)
                    remaining -= len(chunk)
        except sqlite3.Error as exc:
            raise _invalid() from exc
        if digest.hexdigest() != self.checksum:
            raise _error("checksum_mismatch", "Контрольная сумма вложения не совпадает.")


@dataclass
class _AssetWrite:
    path: Path
    data: bytes | _ArchiveAsset

    def copy_to(self, output: BinaryIO) -> None:
        if isinstance(self.data, _ArchiveAsset):
            self.data.copy_to(output)
        else:
            output.write(self.data)


@dataclass
class _FullPlan:
    rows: dict[str, list[tuple[Any, ...]]]
    asset_writes: list[_AssetWrite] = field(default_factory=list)
    duplicate_records: int = 0
    imported: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def changes(self) -> bool:
        return any(self.imported.values()) or bool(self.asset_writes)


@dataclass
class _LegacyObjectPlan:
    objects: list[Any] = field(default_factory=list)
    task_heads: dict[str, str] = field(default_factory=dict)
    asset_writes: list[_AssetWrite] = field(default_factory=list)
    imported: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    duplicate_records: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def changes(self) -> bool:
        return bool(self.objects or self.asset_writes)


_OLD_TABLE_SPECS: dict[str, tuple[_ColumnSpec, ...]] = {
    "metadata": (
        _ColumnSpec("key", "TEXT", False, 1),
        _ColumnSpec("value", "TEXT", False, 0),
    ),
    "assets": (
        _ColumnSpec("id", "TEXT", False, 1),
        _ColumnSpec("name", "TEXT", False, 0),
        _ColumnSpec("mime", "TEXT", False, 0),
        _ColumnSpec("bytes", "BLOB", False, 0),
        _ColumnSpec("sha256", "TEXT", False, 0),
    ),
    "tasks": (
        _ColumnSpec("source_id", "TEXT", False, 1),
        _ColumnSpec("revision", "TEXT", False, 2),
        _ColumnSpec("type_number", "INTEGER", False, 0),
        _ColumnSpec("title", "TEXT", False, 0),
        _ColumnSpec("statement", "TEXT", False, 0),
        _ColumnSpec("blocks_json", "TEXT", True, 0),
        _ColumnSpec("asset_ids_json", "TEXT", False, 0),
    ),
    "task_heads": (
        _ColumnSpec("source_id", "TEXT", False, 1),
        _ColumnSpec("revision", "TEXT", False, 0),
    ),
    "attempts": (
        _ColumnSpec("id", "TEXT", False, 1),
        _ColumnSpec("task_source_id", "TEXT", False, 0),
        _ColumnSpec("task_revision", "TEXT", True, 0),
        _ColumnSpec("verdict", "TEXT", False, 0),
        _ColumnSpec("source", "TEXT", False, 0),
        _ColumnSpec("created_at", "INTEGER", False, 0),
        _ColumnSpec("code", "TEXT", False, 0),
        _ColumnSpec("answer", "TEXT", False, 0),
    ),
    "documents": (
        _ColumnSpec("id", "TEXT", False, 1),
        _ColumnSpec("task_source_id", "TEXT", True, 0),
        _ColumnSpec("task_revision", "TEXT", True, 0),
        _ColumnSpec("title", "TEXT", False, 0),
        _ColumnSpec("type_number", "INTEGER", True, 0),
        _ColumnSpec("code", "TEXT", False, 0),
        _ColumnSpec("statement", "TEXT", False, 0),
        _ColumnSpec("board_json", "TEXT", False, 0),
        _ColumnSpec("revision", "INTEGER", False, 0),
        _ColumnSpec("updated_at", "INTEGER", False, 0),
    ),
    "preferences": (
        _ColumnSpec("key", "TEXT", False, 1),
        _ColumnSpec("value_json", "TEXT", False, 0),
    ),
}

_LOCAL_EXTRA_SPEC = (
    _ColumnSpec("source_id", "TEXT", False, 1),
    _ColumnSpec("fingerprint", "TEXT", False, 2),
    _ColumnSpec("local_id", "TEXT", False, 0),
)

_FULL_METADATA_DDL = "CREATE TABLE transfer_metadata (key TEXT NOT NULL PRIMARY KEY, value TEXT NOT NULL)"
_FULL_ASSETS_DDL = (
    "CREATE TABLE transfer_assets ("
    "asset_id TEXT NOT NULL PRIMARY KEY, storage_key TEXT, filename TEXT NOT NULL, "
    "media_type TEXT NOT NULL, sha256 TEXT NOT NULL, bytes BLOB)"
)
_FULL_TRANSFER_SPECS: dict[str, tuple[_ColumnSpec, ...]] = {
    "transfer_metadata": (
        _ColumnSpec("key", "TEXT", False, 1),
        _ColumnSpec("value", "TEXT", False, 0),
    ),
    "transfer_assets": (
        _ColumnSpec("asset_id", "TEXT", False, 1),
        _ColumnSpec("storage_key", "TEXT", True, 0),
        _ColumnSpec("filename", "TEXT", False, 0),
        _ColumnSpec("media_type", "TEXT", False, 0),
        _ColumnSpec("sha256", "TEXT", False, 0),
        _ColumnSpec("bytes", "BLOB", True, 0),
    ),
}


def _load_metadata() -> Any:
    try:
        import app.models  # noqa: F401
        from app.db import Base
    except ImportError as exc:
        raise _error("runtime_unavailable", "Полная схема приложения недоступна.") from exc
    return Base.metadata


def _model_specs(metadata: Any) -> dict[str, tuple[_ColumnSpec, ...]]:
    specs: dict[str, tuple[_ColumnSpec, ...]] = {}
    for table in metadata.sorted_tables:
        columns: list[_ColumnSpec] = []
        for position, column in enumerate(table.columns, start=1):
            foreign = next(iter(column.foreign_keys), None)
            columns.append(
                _ColumnSpec(
                    name=column.name,
                    declared_type=_normalize_type(str(column.type)),
                    nullable=bool(column.nullable),
                    primary_key=position if column.primary_key else 0,
                    foreign_table=foreign.column.table.name if foreign else None,
                    foreign_column=foreign.column.name if foreign else None,
                )
            )
        specs[table.name] = tuple(columns)
    return specs


def _runtime_type_ok(value: Any, declared_type: str, nullable: bool) -> bool:
    if value is None:
        return nullable
    kind = declared_type.split("(", 1)[0].strip()
    if kind in {"TEXT", "VARCHAR", "CHAR", "CLOB"}:
        return isinstance(value, str)
    if kind in {"INTEGER", "INT", "BIGINT", "SMALLINT", "BOOLEAN"}:
        return type(value) is int
    if kind in {"FLOAT", "REAL", "DOUBLE"}:
        return type(value) in (int, float)
    if kind in {"BLOB", "BYTEA"}:
        return isinstance(value, (bytes, bytearray, memoryview))
    # SQLite's JSON/NUMERIC declarations still carry a primitive runtime value;
    # app JSON is represented as TEXT by the SQLite driver.
    return type(value) in (str, int, float, bytes, bytearray, memoryview)


def _validate_table_info(connection: sqlite3.Connection, table: str, expected: Sequence[_ColumnSpec]) -> None:
    rows = connection.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
    if len(rows) != len(expected):
        raise _invalid()
    for row, spec in zip(rows, expected, strict=True):
        _cid, name, declared, not_null, _default, primary_key = row
        if name != spec.name or _normalize_type(declared) != _normalize_type(spec.declared_type):
            raise _invalid()
        if int(not_null) != int(not spec.nullable) or int(primary_key) != spec.primary_key:
            raise _invalid()
    actual_fks = sorted(
        (str(row[3]), str(row[2]), str(row[4]))
        for row in connection.execute(f"PRAGMA foreign_key_list({_quote(table)})").fetchall()
    )
    expected_fks = sorted(
        (spec.name, spec.foreign_table, spec.foreign_column) for spec in expected if spec.foreign_table is not None
    )
    if actual_fks != expected_fks:
        raise _invalid()


def _schema_objects(connection: sqlite3.Connection) -> list[tuple[str, str, str | None]]:
    return [
        (str(row[0]), str(row[1]), row[2])
        for row in connection.execute("SELECT type,name,sql FROM sqlite_schema ORDER BY name").fetchall()
        if row[1] != "sqlite_sequence" and not (row[0] == "index" and row[1].startswith("sqlite_autoindex_"))
    ]


def _validate_schema_objects(
    connection: sqlite3.Connection,
    expected_tables: Mapping[str, Sequence[_ColumnSpec]],
) -> None:
    objects = _schema_objects(connection)
    if any(kind != "table" for kind, _name, _sql in objects):
        raise _invalid()
    names = [name for _kind, name, _sql in objects]
    if len(names) != len(set(names)) or set(names) != set(expected_tables):
        raise _invalid()
    for table, specs in expected_tables.items():
        _validate_table_info(connection, table, specs)


def _configure_archive_reader(connection: sqlite3.Connection) -> None:
    try:
        connection.enable_load_extension(False)
    except (AttributeError, sqlite3.Error) as exc:
        raise _invalid() from exc
    try:
        # SQLite rejects oversized cells/rows before constructing Python values.
        # This also bounds malicious cells when the full-container cap grows.
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, max(
            MAX_CLIENT_STATE_RAW_BYTES + MAX_FULL_VALUE_BYTES, MAX_FULL_ASSET_BYTES + MAX_SMALL_BYTES,
        ))
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
    except sqlite3.Error as exc:
        raise _invalid() from exc

    # A malformed archive is never allowed to attach another database or
    # mutate itself.  Reads and integrity-check pragmas remain available.
    deny = {
        getattr(sqlite3, name)
        for name in (
            "SQLITE_ATTACH",
            "SQLITE_DETACH",
            "SQLITE_CREATE_INDEX",
            "SQLITE_CREATE_TABLE",
            "SQLITE_CREATE_TEMP_INDEX",
            "SQLITE_CREATE_TEMP_TABLE",
            "SQLITE_CREATE_TEMP_TRIGGER",
            "SQLITE_CREATE_TEMP_VIEW",
            "SQLITE_CREATE_TRIGGER",
            "SQLITE_CREATE_VIEW",
            "SQLITE_DELETE",
            "SQLITE_DROP_INDEX",
            "SQLITE_DROP_TABLE",
            "SQLITE_DROP_TEMP_INDEX",
            "SQLITE_DROP_TEMP_TABLE",
            "SQLITE_DROP_TEMP_TRIGGER",
            "SQLITE_DROP_TEMP_VIEW",
            "SQLITE_DROP_TRIGGER",
            "SQLITE_DROP_VIEW",
            "SQLITE_INSERT",
            "SQLITE_UPDATE",
            "SQLITE_ALTER_TABLE",
        )
        if hasattr(sqlite3, name)
    }

    def authorizer(action: int, _arg1: str | None, _arg2: str | None, _db: str | None, _source: str | None) -> int:
        return sqlite3.SQLITE_DENY if action in deny else sqlite3.SQLITE_OK

    try:
        connection.set_authorizer(authorizer)
    except AttributeError as exc:
        # Python 3.11+ supplies set_authorizer.  A missing API is a hardening
        # failure rather than a reason to process an untrusted archive.
        raise _invalid() from exc


def _check_header(data: bytes) -> None:
    if len(data) < 100 or not data.startswith(_FORMAT_HEADER) or data[18] != 1 or data[19] != 1:
        raise _invalid()


def _read_bounded(path: Path) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            raise _error("file_error", "Не удалось прочитать файл переноса.")
        size = path.stat().st_size
        if size > MAX_LEGACY_CONTAINER_BYTES:
            raise _limit()
        with path.open("rb") as handle:
            data = handle.read(MAX_LEGACY_CONTAINER_BYTES + 1)
    except TransferError:
        raise
    except OSError as exc:
        raise _error("file_error", "Не удалось прочитать файл переноса.") from exc
    if len(data) > MAX_LEGACY_CONTAINER_BYTES:
        raise _limit()
    _check_header(data)
    return data


def _hash_file(path: Path, maximum: int) -> str:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(COPY_CHUNK_BYTES), b""):
            total += len(chunk)
            if total > maximum:
                raise _limit()
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def _private_archive(source: Path) -> Iterator[tuple[sqlite3.Connection, Path, str]]:
    """Pin one bounded, hashed file image for validation, planning and import."""
    descriptor, raw_path = tempfile.mkstemp(prefix=".kontur-private-", suffix=".sqlite3")
    os.close(descriptor)
    private_path = Path(raw_path)
    connection = None
    try:
        if source.is_symlink():
            raise _invalid()
        digest = hashlib.sha256()
        total = 0
        with source.open("rb") as incoming, private_path.open("wb") as output:
            before = os.fstat(incoming.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise _invalid()
            if before.st_size > MAX_CONTAINER_BYTES:
                raise _limit()
            header = incoming.read(100)
            _check_header(header)
            maximum = MAX_CONTAINER_BYTES if int.from_bytes(header[68:72], "big") == FULL_APPLICATION_ID else (
                MAX_LEGACY_CONTAINER_BYTES
            )
            if before.st_size > maximum:
                raise _limit()
            digest.update(header)
            output.write(header)
            total += len(header)
            for chunk in iter(lambda: incoming.read(COPY_CHUNK_BYTES), b""):
                total += len(chunk)
                if total > maximum:
                    raise _limit()
                digest.update(chunk)
                output.write(chunk)
            after = os.fstat(incoming.fileno())
            if total != before.st_size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise _error("import_changed", "Файл изменился после предварительного просмотра. Выберите его заново.")
            output.flush()
            os.fsync(output.fileno())
        connection = sqlite3.connect(f"{private_path.as_uri()}?mode=ro", uri=True)
        _configure_archive_reader(connection)
        yield connection, private_path, digest.hexdigest()
    except TransferError:
        raise
    except sqlite3.Error as exc:
        raise _invalid() from exc
    except OSError as exc:
        raise _error("file_error", "Не удалось прочитать файл переноса.") from exc
    finally:
        if connection is not None:
            connection.close()
        private_path.unlink(missing_ok=True)


@contextmanager
def _private_sqlite(data: bytes) -> Iterator[sqlite3.Connection]:
    descriptor, raw_path = tempfile.mkstemp(prefix=".kontur-private-", suffix=".sqlite3")
    os.close(descriptor)
    path = Path(raw_path)
    try:
        with path.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise _invalid() from exc
        try:
            _configure_archive_reader(connection)
            yield connection
        finally:
            connection.close()
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _metadata_rows(connection: sqlite3.Connection, table: str) -> dict[str, str]:
    values: dict[str, str] = {}
    with closing(connection.execute(f"SELECT key,value FROM {_quote(table)} ORDER BY key")) as rows:
        for key, value in rows:
            if len(values) >= 16:
                raise _invalid()
            key = _identifier(key)
            value = _text(value, 1024 * 1024)
            if key in values:
                raise _invalid()
            values[key] = value
    return values


def _check_integrity(connection: sqlite3.Connection) -> None:
    try:
        result = connection.execute("PRAGMA integrity_check(1)").fetchall()
    except sqlite3.Error as exc:
        raise _invalid() from exc
    if result != [("ok",)]:
        raise _invalid()
    try:
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.Error as exc:
        raise _invalid() from exc
    if foreign:
        raise _invalid()


def _validate_old_metadata(values: Mapping[str, str], expected_format: str) -> None:
    if values.get("format") != expected_format or values.get("schemaVersion") != "1":
        raise _error("unsupported_format", "Версия файла .kontur не поддерживается.")
    if len(values) > 4:
        raise _invalid()
    for key, value in values.items():
        if key in {"format", "schemaVersion"}:
            continue
        if key == "producer":
            _text(value, 256, nonempty=True)
        elif key == "createdAt":
            try:
                created_at = int(value)
            except (TypeError, ValueError) as exc:
                raise _invalid() from exc
            _timestamp(created_at)
        else:
            raise _invalid()


def _read_legacy(data: bytes, *, local_store: bool = False) -> _LegacyBundle:
    digest = _sha256(data)
    with _private_sqlite(data) as connection:
        app_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        expected_app = LOCAL_APPLICATION_ID if local_store else TRANSFER_APPLICATION_ID
        expected_format = LOCAL_FORMAT if local_store else TRANSFER_FORMAT
        if app_id != expected_app or version != 1:
            raise _error("unsupported_format", "Версия файла .kontur не поддерживается.")
        specs = dict(_OLD_TABLE_SPECS)
        if local_store:
            specs["import_receipts"] = _LOCAL_EXTRA_SPEC
        _validate_schema_objects(connection, specs)
        _check_integrity(connection)
        metadata = _metadata_rows(connection, "metadata")
        _validate_old_metadata(metadata, expected_format)

        raw_assets = connection.execute("SELECT id,name,mime,bytes,sha256 FROM assets ORDER BY id").fetchall()
        assets: list[dict[str, Any]] = []
        asset_ids: set[str] = set()
        total_asset_bytes = 0
        for asset_id, name, mime, raw_bytes, checksum in raw_assets:
            asset_id = _identifier(asset_id)
            name = _safe_filename(name)
            mime = _text(mime, 128, nonempty=True)
            if _MIME_RE.fullmatch(mime) is None:
                raise _invalid()
            if not isinstance(raw_bytes, bytes) or len(raw_bytes) > MAX_ASSET_BYTES:
                raise _limit()
            if not isinstance(checksum, str) or _SHA256_RE.fullmatch(checksum) is None:
                raise _invalid()
            if _sha256(raw_bytes) != checksum:
                raise _error("checksum_mismatch", "Контрольная сумма вложения не совпадает.")
            if asset_id in asset_ids:
                raise _invalid()
            asset_ids.add(asset_id)
            total_asset_bytes += len(raw_bytes)
            if total_asset_bytes > MAX_TOTAL_ASSET_BYTES:
                raise _limit()
            assets.append({"id": asset_id, "name": name, "mime": mime, "bytes": raw_bytes, "sha256": checksum})
        if len(assets) > 10_000:
            raise _limit()

        tasks: list[dict[str, Any]] = []
        task_pairs: set[tuple[str, str]] = set()
        source_ids: set[str] = set()
        for row in connection.execute(
            "SELECT source_id,revision,type_number,title,statement,blocks_json,asset_ids_json "
            "FROM tasks ORDER BY source_id,revision"
        ).fetchall():
            source_id, revision, type_number, title, statement, blocks_raw, asset_ids_raw = row
            source_id, revision = _identifier(source_id), _identifier(revision)
            _integer(type_number, 1, 27)
            title = _text(title, 512, nonempty=True)
            statement = _text(statement, MAX_BODY_BYTES)
            blocks = None if blocks_raw is None else _json_loads(blocks_raw, MAX_BODY_BYTES)
            if blocks is not None and type(blocks) not in (dict, list):
                raise _invalid()
            references = _json_loads(asset_ids_raw, MAX_BODY_BYTES)
            if type(references) is not list or len(references) > 10_000:
                raise _invalid()
            references = [_identifier(item) for item in references]
            if len(set(references)) != len(references) or not set(references) <= asset_ids:
                raise _invalid()
            pair = (source_id, revision)
            if pair in task_pairs:
                raise _invalid()
            task_pairs.add(pair)
            source_ids.add(source_id)
            tasks.append(
                {
                    "sourceId": source_id,
                    "revision": revision,
                    "typeNumber": int(type_number),
                    "title": title,
                    "statement": statement,
                    "blocks": blocks,
                    "assetIds": references,
                }
            )
        if len(tasks) > 50_000:
            raise _limit()

        heads: dict[str, str] = {}
        for source_id, revision in connection.execute("SELECT source_id,revision FROM task_heads ORDER BY source_id"):
            source_id, revision = _identifier(source_id), _identifier(revision)
            if source_id in heads or (source_id, revision) not in task_pairs:
                raise _invalid()
            heads[source_id] = revision
        if set(heads) != source_ids:
            raise _invalid()

        attempts: list[dict[str, Any]] = []
        attempt_ids: set[str] = set()
        for row in connection.execute(
            "SELECT id,task_source_id,task_revision,verdict,source,created_at,code,answer "
            "FROM attempts ORDER BY created_at,id"
        ):
            attempt_id, source_id, revision, verdict, source, created_at, code, answer = row
            attempt_id, source_id = _identifier(attempt_id), _identifier(source_id)
            if revision is not None:
                revision = _identifier(revision)
                # A source/revision pair outside this bundle is retained in
                # the import ledger.  It must not be promoted to a synthetic
                # official task, and the planner links it only when the exact
                # pair is present in ``task_pairs``.
            if verdict not in {"correct", "wrong", "unverified", "error"} or source not in {"manual", "imported"}:
                raise _invalid()
            _timestamp(created_at)
            code, answer = _text(code, MAX_BODY_BYTES), _text(answer, MAX_SMALL_BYTES)
            if attempt_id in attempt_ids:
                raise _invalid()
            attempt_ids.add(attempt_id)
            attempts.append(
                {
                    "id": attempt_id,
                    "taskSourceId": source_id,
                    "taskRevision": revision,
                    "verdict": verdict,
                    "source": source,
                    "createdAt": int(created_at),
                    "code": code,
                    "answer": answer,
                }
            )
        if len(attempts) > 500_000:
            raise _limit()

        documents: list[dict[str, Any]] = []
        document_ids: set[str] = set()
        for row in connection.execute(
            "SELECT id,task_source_id,task_revision,title,type_number,code,statement,board_json,revision,updated_at "
            "FROM documents ORDER BY id"
        ):
            doc_id, source_id, revision, title, type_number, code, statement, board_raw, doc_revision, updated_at = row
            doc_id = _identifier(doc_id)
            if doc_id in document_ids:
                raise _invalid()
            document_ids.add(doc_id)
            if (source_id is None) != (revision is None):
                raise _invalid()
            if source_id is not None:
                source_id, revision = _identifier(source_id), _identifier(revision)
            title, code, statement = (
                _text(title, 512, nonempty=True),
                _text(code, MAX_BODY_BYTES),
                _text(statement, MAX_BODY_BYTES),
            )
            if type_number is not None:
                _integer(type_number, 1, 27)
            board = _json_loads(board_raw, MAX_BODY_BYTES)
            if type(board) is not dict:
                raise _invalid()
            _integer(doc_revision, 1, MAX_SAFE_INTEGER)
            _timestamp(updated_at)
            documents.append(
                {
                    "id": doc_id,
                    "taskSourceId": source_id,
                    "taskRevision": revision,
                    "title": title,
                    "typeNumber": None if type_number is None else int(type_number),
                    "code": code,
                    "statement": statement,
                    "board": board,
                    "revision": int(doc_revision),
                    "updatedAt": int(updated_at),
                }
            )
        if len(documents) > 10_000:
            raise _limit()

        preferences: dict[str, Any] = {}
        for key, value_raw in connection.execute("SELECT key,value_json FROM preferences ORDER BY key"):
            key = _identifier(key)
            if key in preferences:
                raise _invalid()
            value = _json_loads(value_raw, MAX_SMALL_BYTES)
            if (
                key == "selectedDocumentId"
                and value is not None
                and (not isinstance(value, str) or value not in document_ids)
            ):
                raise _invalid()
            if key == "kontur.legacy12.transferWarnings":
                if type(value) is not list or any(not isinstance(item, str) or not item for item in value):
                    raise _invalid()
            preferences[key] = value
        if len(preferences) > 100:
            raise _limit()
        if local_store:
            # Receipts are local implementation data.  They are checked as
            # part of the fixed schema but never copied into the application.
            for source_id, fingerprint, local_id in connection.execute(
                "SELECT source_id,fingerprint,local_id FROM import_receipts"
            ):
                _identifier(source_id)
                if _SHA256_RE.fullmatch(fingerprint) is None or not isinstance(local_id, str):
                    raise _invalid()
        return _LegacyBundle(metadata, tasks, heads, attempts, documents, assets, preferences, digest, local_store)


def _full_schema(connection: sqlite3.Connection, metadata: Any) -> dict[str, tuple[_ColumnSpec, ...]]:
    specs = _model_specs(metadata)
    if len(specs) > MAX_TABLES:
        raise _limit()
    specs.update(_FULL_TRANSFER_SPECS)
    _validate_schema_objects(connection, specs)
    return specs


def _full_value_bytes(value: Any, table: str, column: _ColumnSpec) -> int:
    value_limit = (
        MAX_CLIENT_STATE_RAW_BYTES
        if table == "owner_sync_records" and column.name == "value"
        else MAX_FULL_VALUE_BYTES
    )
    size = len(value.encode("utf-8")) if isinstance(value, str) else len(value) if isinstance(
        value, (bytes, bytearray, memoryview)
    ) else 32
    if size > value_limit:
        raise _limit()
    return size + 32


def _read_full(connection: sqlite3.Connection, digest: str) -> _FullBundle:
    metadata_obj = _load_metadata()
    app_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if app_id != FULL_APPLICATION_ID or version != 1:
        raise _error("unsupported_format", "Версия файла .kontur не поддерживается.")
    specs = _full_schema(connection, metadata_obj)
    _check_integrity(connection)
    metadata = _metadata_rows(connection, "transfer_metadata")
    if metadata.get("format") != FULL_FORMAT or metadata.get("schemaVersion") != "1":
        raise _error("unsupported_format", "Версия файла .kontur не поддерживается.")
    backend_version = metadata.get("backendSchemaVersion")
    if backend_version is None or not backend_version.isdigit() or int(backend_version) > _current_schema_version():
        raise _error("unsupported_format", "Версия файла .kontur не поддерживается.")
    source_owner = metadata.get("ownerId")
    if source_owner is not None:
        _identifier(source_owner, 64)
    tables: dict[str, list[tuple[Any, ...]]] = {}
    table_bytes = 0
    for table in sorted(specs):
        if table in _FULL_TRANSFER_SPECS:
            continue
        columns = specs[table]
        validated: list[tuple[Any, ...]] = []
        with closing(connection.execute(f"SELECT * FROM {_quote(table)}")) as rows:
            for row in rows:
                if len(validated) >= MAX_ROWS_PER_TABLE:
                    raise _limit()
                table_bytes += 64
                if len(row) != len(columns) or any(
                    not _runtime_type_ok(value, column.declared_type, column.nullable)
                    for value, column in zip(row, columns, strict=True)
                ):
                    raise _invalid()
                for value, _column in zip(row, columns, strict=True):
                    table_bytes += _full_value_bytes(value, table, _column)
                    if table_bytes > MAX_FULL_TABLE_BYTES:
                        raise _limit()
                validated.append(tuple(row))
        tables[table] = validated

    assets: dict[str, dict[str, Any]] = {}
    total = 0
    with closing(connection.execute(
        "SELECT asset_id,storage_key,filename,media_type,sha256,typeof(bytes),length(bytes),rowid "
        "FROM transfer_assets ORDER BY asset_id"
    )) as raw_assets:
        for asset_id, storage_key, filename, media_type, checksum, kind, size, rowid in raw_assets:
            if len(assets) >= len(tables.get("assets", [])):
                raise _invalid()
            asset_id = _identifier(asset_id, 64)
            if storage_key is not None:
                storage_key = _safe_relative_path(storage_key)
            filename = _safe_filename(filename)
            media_type = _text(media_type, 255, nonempty=True)
            checksum = _text(checksum, 64, nonempty=True)
            if _SHA256_RE.fullmatch(checksum) is None:
                raise _invalid()
            raw_bytes = None
            if kind != "null":
                if kind != "blob" or type(size) is not int or not 0 <= size <= MAX_FULL_ASSET_BYTES:
                    raise _limit()
                total += size
                if total > MAX_FULL_TOTAL_ASSET_BYTES:
                    raise _limit()
                raw_bytes = _ArchiveAsset(connection, rowid, size, checksum)
                raw_bytes.copy_to()
            assets[asset_id] = {
                "assetId": asset_id,
                "storageKey": storage_key,
                "filename": filename,
                "mediaType": media_type,
                "sha256": checksum,
                "bytes": raw_bytes,
            }
    asset_rows = {str(row[0]) for row in tables.get("assets", [])}
    if set(assets) != asset_rows:
        raise _invalid()
    return _FullBundle(metadata, tables, assets, digest)


def _current_schema_version() -> int:
    try:
        from app.schema_version import CURRENT_SCHEMA_VERSION

        return int(CURRENT_SCHEMA_VERSION)
    except (ImportError, ValueError):
        return 13


def _validate_offline_client_state(value: Any) -> None:
    """Fail closed unless the app's bounded client-state validator accepts it."""

    try:
        from offline_state import validate_client_state
    except ImportError as exc:
        raise _error("runtime_unavailable", "Проверка локального состояния недоступна.") from exc
    try:
        result = validate_client_state(value)
    except Exception as exc:
        raise _invalid() from exc
    if result is False:
        raise _invalid()


def _validate_offline_client_state_rows(
    tables: Mapping[str, Sequence[Sequence[Any]]],
    specs: Mapping[str, Sequence[_ColumnSpec]],
) -> None:
    rows = tables.get("owner_sync_records", ())
    if not rows:
        return
    columns = {column.name: index for index, column in enumerate(specs["owner_sync_records"])}
    from offline_state import MAX_SAFE, RECORD_ID

    required = {"id", "kind", "value", "context", "version"}
    if not required <= columns.keys():
        raise _invalid()
    for row in rows:
        if row[columns["kind"]] != "offline_client_state":
            continue
        revision = row[columns["version"]]
        if row[columns["id"]] != RECORD_ID or type(revision) is not int or not 0 <= revision <= MAX_SAFE:
            raise _invalid()
        if _json_loads(row[columns["context"]], MAX_SMALL_BYTES) != {"version": 1}:
            raise _invalid()
        payload = _json_loads(row[columns["value"]], MAX_CLIENT_STATE_RAW_BYTES)
        if not isinstance(payload, dict):
            raise _invalid()
        _validate_offline_client_state(payload)


def _full_rows_equal(
    table: str,
    columns: Sequence[_ColumnSpec],
    existing: Sequence[Any],
    incoming: Sequence[Any],
    *,
    remap_policy: bool = False,
) -> bool:
    """Retain equivalent process/bootstrap bookkeeping; all user data stays exact."""
    names = {column.name: index for index, column in enumerate(columns)}
    ignored: set[str] = set()
    if table == "users" and incoming[names["role"]] == "owner":
        ignored = {"created_at"}
    elif table == "source_policies":
        ignored = {"updated_at"} | ({"id"} if remap_policy else set())
    elif table == "source_sync_jobs":
        key = incoming[names["idempotency_key"]]
        if incoming[names["mode"]] == "recent-catalog" and isinstance(key, str) and key.startswith("offline-seed:"):
            ignored = {"created_at", "updated_at"}
    elif table == "owner_sync_records":
        identifier = incoming[names["id"]]
        if (
            incoming[names["kind"]] == "offline-bootstrap"
            and isinstance(identifier, str)
            and identifier.startswith("seed:")
        ):
            ignored = {"updated_at"}
    return all(
        left == right
        for column, left, right in zip(columns, existing, incoming, strict=True)
        if column.name not in ignored
    )


def _model_ddl(metadata: Any) -> list[str]:
    try:
        from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect
        from sqlalchemy.schema import CreateTable
    except ImportError as exc:
        raise _error("runtime_unavailable", "Полная схема приложения недоступна.") from exc
    dialect = sqlite_dialect()
    return [str(CreateTable(table).compile(dialect=dialect)) for table in metadata.sorted_tables]


def _prepare_destination(path: Path) -> tuple[Path, Path]:
    path = Path(path)
    try:
        if path.exists() or path.is_symlink():
            raise _error("destination_exists", "Файл уже существует. Выберите новое имя для экспорта.")
        parent = path.parent
        if not parent.is_dir():
            raise _error("file_error", "Каталог назначения недоступен.")
        descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".part", dir=parent)
        os.close(descriptor)
        return path, Path(raw)
    except TransferError:
        raise
    except OSError as exc:
        raise _error("file_error", "Не удалось подготовить файл переноса.") from exc


def _publish_noclobber(source: Path, destination: Path) -> None:
    if os.name == "nt":
        try:
            os.rename(source, destination)
        except FileExistsError as exc:
            raise _error("destination_exists", "Файл уже существует. Выберите новое имя для экспорта.") from exc
    else:
        try:
            os.link(source, destination)
            source.unlink(missing_ok=True)
        except FileExistsError as exc:
            raise _error("destination_exists", "Файл уже существует. Выберите новое имя для экспорта.") from exc
        except OSError as exc:
            raise _error("file_error", "Не удалось опубликовать файл переноса.") from exc


def _fsync_file(path: Path) -> None:
    try:
        with path.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise _error("file_error", "Не удалось надёжно сохранить файл переноса.") from exc


def _asset_root(settings: Any) -> Path:
    root = Path(getattr(settings, "asset_root", Path("assets"))).resolve()
    return root


def _asset_path(root: Path, storage_key: str) -> Path:
    try:
        candidate = (root / storage_key).resolve()
        candidate.relative_to(root)
    except (OSError, ValueError) as exc:
        raise _invalid() from exc
    return candidate


def _read_asset_file(root: Path, storage_key: str, expected_hash: str) -> bytes:
    path = _asset_path(root, storage_key)
    try:
        if path.is_symlink() or not path.is_file():
            raise _error("asset_unavailable", "Вложение не найдено в локальном хранилище.")
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise _error("asset_unavailable", "Вложение не найдено в локальном хранилище.")
            size = before.st_size
            if size > MAX_FULL_ASSET_BYTES:
                raise _limit()
            data = handle.read(size + 1)
            after = os.fstat(handle.fileno())
        if len(data) > MAX_FULL_ASSET_BYTES:
            raise _limit()
    except TransferError:
        raise
    except OSError as exc:
        raise _error("asset_unavailable", "Вложение не найдено в локальном хранилище.") from exc
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or (
        len(data) != size or _sha256(data) != expected_hash
    ):
        raise _error("checksum_mismatch", "Контрольная сумма вложения не совпадает.")
    return data


def _backup_live_database(engine: Any, profile: Path, prefix: str) -> Path:
    backup_dir = Path(profile) / "backups"
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        descriptor, raw = tempfile.mkstemp(prefix=f".{prefix}-", suffix=".sqlite3", dir=backup_dir)
        os.close(descriptor)
        backup_path = Path(raw)
        raw_connection = engine.raw_connection()
        driver = getattr(raw_connection, "driver_connection", raw_connection)
        destination = sqlite3.connect(str(backup_path))
        try:
            driver.backup(destination)
            destination.commit()
        finally:
            destination.close()
            raw_connection.close()
        _fsync_file(backup_path)
        return backup_path
    except TransferError:
        raise
    except (OSError, sqlite3.Error, AttributeError) as exc:
        try:
            backup_path.unlink(missing_ok=True)  # type: ignore[has-type]
        except (NameError, OSError):
            pass
        raise _error("backup_failed", "Не удалось создать резервную копию. Данные не импортированы.") from exc


def _backup_source_bytes(data: bytes, profile: Path, digest: str) -> Path:
    backup_dir = Path(profile) / "backups"
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        path = backup_dir / f"kontur-local-{digest[:16]}.sqlite3"
        if path.exists():
            if _sha256(path.read_bytes()) != digest:
                raise _error("backup_failed", "Не удалось создать резервную копию. Данные не импортированы.")
            return path
        descriptor, raw = tempfile.mkstemp(prefix=f".kontur-local-{digest[:16]}.", suffix=".part", dir=backup_dir)
        os.close(descriptor)
        temporary = Path(raw)
        try:
            temporary.write_bytes(data)
            _fsync_file(temporary)
            _publish_noclobber(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path
    except TransferError:
        raise
    except OSError as exc:
        raise _error("backup_failed", "Не удалось создать резервную копию. Данные не импортированы.") from exc


class TransferManager:
    """Versioned full transfer and mini-store migration manager."""

    def __init__(self, engine: Any, session_factory: Any, settings: Any, profile: Path, owner_id: str = "owner"):
        self.engine = engine
        self.session_factory = session_factory
        self.settings = settings
        self.profile = Path(profile)
        self.owner_id = _identifier(owner_id, 64)
        self._preview_digests: dict[Path, str] = {}

    @contextmanager
    def _open_bundle(
        self, path: Path, *, confirming: bool = False, expected_sha256: str | None = None,
    ) -> Iterator[tuple[str, _FullBundle | _LegacyBundle]]:
        with _private_archive(path) as (connection, private_path, digest):
            if confirming:
                self._check_import_digest(path, digest, expected_sha256)
            app_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
            if app_id == FULL_APPLICATION_ID:
                yield FULL_FORMAT, _read_full(connection, digest)
            else:
                yield self._read_bytes(_read_bounded(private_path))

    @staticmethod
    def _read_bytes(data: bytes) -> tuple[str, _FullBundle | _LegacyBundle]:
        try:
            with _private_sqlite(data) as connection:
                app_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
            if app_id == TRANSFER_APPLICATION_ID:
                return TRANSFER_FORMAT, _read_legacy(data)
            if app_id == LOCAL_APPLICATION_ID:
                return LOCAL_FORMAT, _read_legacy(data, local_store=True)
            raise _error("unsupported_format", "Версия файла .kontur не поддерживается.")
        except TransferError:
            raise
        except sqlite3.Error as exc:
            raise _invalid() from exc

    def preview(self, path: Path) -> dict[str, Any]:
        path = Path(path).resolve()
        with self._open_bundle(path) as (format_name, bundle):
            result = self._preview_bundle(format_name, bundle)
            self._preview_digests[path] = bundle.digest
            return result

    def _preview_bundle(self, format_name: str, bundle: _FullBundle | _LegacyBundle) -> dict[str, Any]:
        if format_name == FULL_FORMAT:
            assert isinstance(bundle, _FullBundle)
            plan = self._plan_full(bundle)
            return {
                "formatVersion": 1,
                "format": FULL_FORMAT,
                "contentSha256": bundle.digest,
                "counts": {table: len(rows) for table, rows in sorted(bundle.tables.items())},
                "conflictCount": 0,
                "warnings": list(dict.fromkeys(plan.warnings)),
            }
        assert isinstance(bundle, _LegacyBundle)
        legacy_plan = self._plan_legacy(bundle)
        counts = {
            "tasks": len(bundle.tasks),
            "taskHeads": len(bundle.heads),
            "attempts": len(bundle.attempts),
            "documents": len(bundle.documents),
            "assets": len(bundle.assets),
            "preferences": len(bundle.preferences),
        }
        return {
            "formatVersion": 1,
            "format": format_name,
            "contentSha256": bundle.digest,
            "counts": counts,
            "conflictCount": 0,
            "warnings": list(dict.fromkeys(legacy_plan.warnings)),
        }

    def import_bundle(self, path: Path, expected_sha256: str | None = None) -> dict[str, Any]:
        path = Path(path).resolve()
        with self._open_bundle(path, confirming=True, expected_sha256=expected_sha256) as (format_name, bundle):
            return self._import_bundle(format_name, bundle)

    def _check_import_digest(self, path: Path, digest: str, expected_sha256: str | None) -> None:
        if expected_sha256 is not None and (
            not isinstance(expected_sha256, str) or _SHA256_RE.fullmatch(expected_sha256) is None
        ):
            raise _invalid()
        cached = self._preview_digests.get(path)
        if (
            (expected_sha256 is None and cached is None)
            or (cached is not None and cached != digest)
            or (expected_sha256 is not None and expected_sha256 != digest)
        ):
            raise _error("import_changed", "Файл изменился после предварительного просмотра. Выберите его заново.")

    def _import_bundle(self, format_name: str, bundle: _FullBundle | _LegacyBundle) -> dict[str, Any]:
        digest = bundle.digest
        if format_name == FULL_FORMAT:
            assert isinstance(bundle, _FullBundle)
            plan = self._plan_full(bundle)
            if not plan.changes:
                return self._result(format_name, digest, plan.imported, plan.duplicate_records, "", plan.warnings)
            backup = _backup_live_database(self.engine, self.profile, "pre-import")
            try:
                self._apply_full(plan)
            except Exception:
                raise
            return self._result(format_name, digest, plan.imported, plan.duplicate_records, str(backup), plan.warnings)
        assert isinstance(bundle, _LegacyBundle)
        legacy_plan = self._plan_legacy(bundle)
        if not legacy_plan.changes:
            return self._result(
                format_name, digest, dict(legacy_plan.imported), legacy_plan.duplicate_records, "", legacy_plan.warnings
            )
        backup = _backup_live_database(self.engine, self.profile, "pre-import")
        self._apply_legacy(legacy_plan)
        return self._result(
            format_name,
            digest,
            dict(legacy_plan.imported),
            legacy_plan.duplicate_records,
            str(backup),
            legacy_plan.warnings,
        )

    @staticmethod
    def _result(
        format_name: str,
        digest: str,
        imported: Mapping[str, int],
        duplicate_records: int,
        backup_path: str,
        warnings: Sequence[str],
    ) -> dict[str, Any]:
        return {
            "format": format_name,
            "contentSha256": digest,
            "imported": {str(key): int(value) for key, value in sorted(imported.items()) if value},
            "duplicateRecords": int(duplicate_records),
            "backupPath": backup_path,
            "warnings": list(dict.fromkeys(warnings)),
        }

    def export_bundle(self, path: Path) -> dict[str, Any]:
        destination, temporary = _prepare_destination(Path(path))
        metadata = _load_metadata()
        specs = _model_specs(metadata)
        tables: dict[str, list[tuple[Any, ...]]] = {}
        table_bytes = 0
        try:
            with self.engine.connect() as source:
                for table in metadata.sorted_tables:
                    columns = specs[table.name]
                    actual = source.exec_driver_sql(f"PRAGMA table_info({_quote(table.name)})").fetchall()
                    if len(actual) != len(columns):
                        raise _error("schema_mismatch", "Локальная схема приложения не поддерживается.")
                    order = ",".join(_quote(column.name) for column in columns if column.primary_key)
                    sql = f"SELECT * FROM {_quote(table.name)}" + (f" ORDER BY {order}" if order else "")
                    converted: list[tuple[Any, ...]] = []
                    with source.exec_driver_sql(sql) as rows:
                        for row in rows:
                            if len(converted) >= MAX_ROWS_PER_TABLE:
                                raise _limit()
                            values = tuple(row)
                            if any(
                                not _runtime_type_ok(value, column.declared_type, column.nullable)
                                for value, column in zip(values, columns, strict=True)
                            ):
                                raise _error("schema_mismatch", "Локальная схема приложения не поддерживается.")
                            table_bytes += 64 + sum(
                                _full_value_bytes(value, table.name, column)
                                for value, column in zip(values, columns, strict=True)
                            )
                            if table_bytes > MAX_FULL_TABLE_BYTES:
                                raise _limit()
                            converted.append(values)
                    tables[table.name] = converted

            _validate_offline_client_state_rows(tables, specs)
            asset_table = tables.get("assets", [])
            asset_columns = [column.name for column in specs.get("assets", ())]
            index = {name: asset_columns.index(name) for name in asset_columns}
            root = _asset_root(self.settings)
            def asset_rows() -> Iterator[tuple[Any, ...]]:
                total_assets = 0
                for row in asset_table:
                    cache_state = row[index["cache_state"]]
                    storage_key = row[index["storage_key"]]
                    content_hash = row[index["content_hash"]]
                    if not isinstance(content_hash, str) or _SHA256_RE.fullmatch(content_hash) is None:
                        raise _error("schema_mismatch", "Локальная схема приложения не поддерживается.")
                    raw_bytes: bytes | None = None
                    if cache_state == "ready":
                        if not isinstance(storage_key, str):
                            raise _error("asset_unavailable", "Вложение не найдено в локальном хранилище.")
                        storage_key = _safe_relative_path(storage_key)
                        raw_bytes = _read_asset_file(root, storage_key, content_hash)
                        if row[index["size_bytes"]] != len(raw_bytes):
                            raise _invalid()
                        total_assets += len(raw_bytes)
                        if total_assets > MAX_FULL_TOTAL_ASSET_BYTES:
                            raise _limit()
                    filename = _safe_filename(row[index["filename"]])
                    media_type = _text(row[index["media_type"]], 255, nonempty=True)
                    yield (row[index["id"]], storage_key, filename, media_type, content_hash, raw_bytes)

            self._write_full_container(destination, temporary, metadata, specs, tables, asset_rows())
            digest = _hash_file(temporary, MAX_CONTAINER_BYTES)
            _publish_noclobber(temporary, destination)
            return {
                "format": FULL_FORMAT,
                "path": str(destination),
                "contentSha256": digest,
                "counts": {table: len(rows) for table, rows in sorted(tables.items())},
            }
        finally:
            temporary.unlink(missing_ok=True)

    def _write_full_container(
        self,
        destination: Path,
        temporary: Path,
        metadata: Any,
        specs: Mapping[str, Sequence[_ColumnSpec]],
        tables: Mapping[str, Sequence[Sequence[Any]]],
        asset_rows: Iterable[Sequence[Any]],
    ) -> None:
        del destination
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(str(temporary))
            connection.execute("PRAGMA page_size=4096")
            connection.execute(f"PRAGMA max_page_count={MAX_CONTAINER_BYTES // 4096}")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(f"PRAGMA application_id={FULL_APPLICATION_ID}")
            connection.execute("PRAGMA user_version=1")
            connection.execute(_FULL_METADATA_DDL)
            connection.execute(_FULL_ASSETS_DDL)
            for ddl in _model_ddl(metadata):
                connection.execute(ddl)
            connection.execute("BEGIN IMMEDIATE")
            owner = self.owner_id
            metadata_rows = [
                ("format", FULL_FORMAT),
                ("schemaVersion", "1"),
                ("backendSchemaVersion", str(_current_schema_version())),
                ("ownerId", owner),
            ]
            connection.executemany("INSERT INTO transfer_metadata(key,value) VALUES (?,?)", metadata_rows)
            for row in asset_rows:
                connection.execute(
                    "INSERT INTO transfer_assets(asset_id,storage_key,filename,media_type,sha256,bytes) "
                    "VALUES (?,?,?,?,?,?)",
                    tuple(row),
                )
            for table, rows in tables.items():
                columns = [column.name for column in specs[table]]
                placeholders = ",".join("?" for _ in columns)
                statement = (
                    f"INSERT INTO {_quote(table)} ({','.join(_quote(column) for column in columns)}) "
                    f"VALUES ({placeholders})"
                )
                connection.executemany(statement, [tuple(row) for row in rows])
            connection.commit()
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise _error("schema_mismatch", "Локальная схема приложения не поддерживается.")
            if connection.execute("PRAGMA integrity_check(1)").fetchall() != [("ok",)]:
                raise _error("export_failed", "Не удалось проверить файл переноса.")
            connection.close()
            connection = None
            if temporary.stat().st_size > MAX_CONTAINER_BYTES:
                raise _limit()
            _fsync_file(temporary)
        except TransferError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise _error("export_failed", "Не удалось создать файл переноса.") from exc
        finally:
            if connection is not None:
                connection.close()

    def _plan_full(self, bundle: _FullBundle) -> _FullPlan:
        metadata = _load_metadata()
        specs = _model_specs(metadata)
        _validate_offline_client_state_rows(bundle.tables, specs)
        source_owner = bundle.metadata.get("ownerId") or self.owner_id
        _identifier(source_owner, 64)
        mapped: dict[str, list[tuple[Any, ...]]] = {}
        for table, rows in bundle.tables.items():
            columns = specs[table]
            mapped_rows: list[tuple[Any, ...]] = []
            for row in rows:
                values = list(row)
                names = [column.name for column in columns]
                if table == "users":
                    user_id = values[names.index("id")]
                    if user_id != source_owner or values[names.index("role")] != "owner":
                        raise _error("foreign_owner", "Архив содержит данные другого владельца.")
                    values[names.index("id")] = self.owner_id
                if "owner_id" in names:
                    owner_index = names.index("owner_id")
                    if values[owner_index] not in {source_owner, self.owner_id}:
                        raise _error("foreign_owner", "Архив содержит данные другого владельца.")
                    values[owner_index] = self.owner_id
                mapped_rows.append(tuple(values))
            mapped[table] = mapped_rows

        duplicate_records = 0
        imported: dict[str, int] = defaultdict(int)
        # Startup creates one policy per unique source name with a fresh UUID.
        # Retain an equivalent local policy and remap only its foreign keys.
        policy_columns = specs["source_policies"]
        policy_names = {column.name: index for index, column in enumerate(policy_columns)}
        policy_ids: dict[Any, Any] = {}
        with self.engine.connect() as connection:
            local_policies = connection.exec_driver_sql("SELECT * FROM source_policies").fetchall()
            policies_by_name = {row[policy_names["name"]]: tuple(row) for row in local_policies}
            for row in mapped["source_policies"]:
                local = policies_by_name.get(row[policy_names["name"]])
                if local is not None:
                    if not _full_rows_equal("source_policies", policy_columns, local, row, remap_policy=True):
                        raise _conflict()
                    policy_ids[row[policy_names["id"]]] = local[policy_names["id"]]
        for name, rows in mapped.items():
            columns = specs[name]
            indexes = [
                index
                for index, column in enumerate(columns)
                if (name == "source_policies" and column.name == "id")
                or (column.foreign_table == "source_policies" and column.foreign_column == "id")
            ]
            if indexes:
                mapped[name] = [
                    tuple(
                        policy_ids.get(value, value) if index in indexes else value for index, value in enumerate(row)
                    )
                    for row in rows
                ]
        with self.engine.connect() as connection:
            for table in metadata.sorted_tables:
                name = table.name
                columns = specs[name]
                pk_indexes = [index for index, column in enumerate(columns) if column.primary_key]
                if not pk_indexes:
                    raise _error("schema_mismatch", "Локальная схема приложения не поддерживается.")
                local_rows = connection.exec_driver_sql(f"SELECT * FROM {_quote(name)}").fetchall()
                local_by_pk = {tuple(row[index] for index in pk_indexes): tuple(row) for row in local_rows}
                staged: dict[tuple[Any, ...], tuple[Any, ...]] = {}
                new_rows: list[tuple[Any, ...]] = []
                for row in mapped[name]:
                    key = tuple(row[index] for index in pk_indexes)
                    if key in staged and staged[key] != row:
                        raise _conflict()
                    staged[key] = row
                    existing = local_by_pk.get(key)
                    if existing is not None:
                        if not _full_rows_equal(name, columns, existing, row):
                            raise _conflict()
                        duplicate_records += 1
                    else:
                        new_rows.append(row)
                mapped[name] = new_rows
                imported[name] = len(new_rows)

        # Validate references against the merged local/archive identity set.
        identity_sets: dict[str, set[tuple[Any, ...]]] = {}
        with self.engine.connect() as connection:
            for table in metadata.sorted_tables:
                columns = specs[table.name]
                pk_indexes = [index for index, column in enumerate(columns) if column.primary_key]
                rows = connection.exec_driver_sql(f"SELECT * FROM {_quote(table.name)}").fetchall()
                identity_sets[table.name] = {tuple(row[index] for index in pk_indexes) for row in rows}
                identity_sets[table.name].update(
                    tuple(row[index] for index in pk_indexes) for row in mapped[table.name]
                )
        for table in metadata.sorted_tables:
            columns = specs[table.name]
            for row in mapped[table.name]:
                for index, column in enumerate(columns):
                    if column.foreign_table is None or row[index] is None:
                        continue
                    target_columns = specs[column.foreign_table]
                    target_index = next(
                        (
                            position
                            for position, target in enumerate(target_columns)
                            if target.name == column.foreign_column
                        ),
                        None,
                    )
                    if target_index is None:
                        raise _invalid()
                    if (row[index],) not in identity_sets[column.foreign_table]:
                        raise _invalid()

        # The transfer asset rows are checked against the application assets
        # table and turned into safe, target-root file writes.
        asset_columns = [column.name for column in specs["assets"]]
        asset_by_id = {row[asset_columns.index("id")]: row for row in bundle.tables.get("assets", [])}
        if set(asset_by_id) != set(bundle.assets):
            raise _invalid()
        root = _asset_root(self.settings)
        asset_writes: list[_AssetWrite] = []
        planned_paths: dict[Path, tuple[str, int]] = {}
        for asset_id, asset in bundle.assets.items():
            asset_row = asset_by_id.get(asset_id)
            if asset_row is None:
                raise _invalid()
            checksum = asset_row[asset_columns.index("content_hash")]
            if checksum != asset["sha256"]:
                raise _error("checksum_mismatch", "Контрольная сумма вложения не совпадает.")
            ready = asset_row[asset_columns.index("cache_state")] == "ready"
            if (
                asset_row[asset_columns.index("storage_key")] != asset["storageKey"]
                or asset_row[asset_columns.index("filename")] != asset["filename"]
                or asset_row[asset_columns.index("media_type")] != asset["mediaType"]
            ):
                raise _invalid()
            raw_bytes = asset["bytes"]
            if ready != (raw_bytes is not None):
                raise _invalid()
            if raw_bytes is None:
                continue
            if asset_row[asset_columns.index("size_bytes")] != len(raw_bytes) or asset_row[
                asset_columns.index("expected_content_hash")
            ] not in {None, checksum}:
                raise _invalid()
            storage_key = asset["storageKey"]
            storage_key = _safe_relative_path(storage_key)
            path = _asset_path(root, storage_key)
            if path.exists():
                if (
                    path.is_symlink() or not path.is_file() or path.stat().st_size != len(raw_bytes)
                    or _hash_file(path, MAX_FULL_ASSET_BYTES) != asset["sha256"]
                ):
                    raise _conflict()
            else:
                identity = (asset["sha256"], len(raw_bytes))
                if path in planned_paths:
                    if planned_paths[path] != identity:
                        raise _conflict()
                else:
                    planned_paths[path] = identity
                    asset_writes.append(_AssetWrite(path, raw_bytes))
        warnings: list[str] = []
        imported = {table: count for table, count in imported.items() if count}
        return _FullPlan(dict(mapped), asset_writes, duplicate_records, dict(imported), warnings)

    def _apply_full(self, plan: _FullPlan) -> None:
        created_files: list[Path] = []
        temporary: Path | None = None
        try:
            for item in plan.asset_writes:
                item.path.parent.mkdir(parents=True, exist_ok=True)
                descriptor, name = tempfile.mkstemp(
                    prefix=".kontur-asset-", suffix=".import-part", dir=item.path.parent,
                )
                os.close(descriptor)
                temporary = Path(name)
                with temporary.open("wb") as handle:
                    item.copy_to(handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                _publish_noclobber(temporary, item.path)
                temporary = None
                created_files.append(item.path)
            metadata = _load_metadata()
            specs = _model_specs(metadata)
            with self.engine.begin() as connection:
                connection.exec_driver_sql("PRAGMA defer_foreign_keys=ON")
                tables = list(metadata.sorted_tables)
                # Trusted application triggers allocate new change-feed sequence
                # IDs during inserts. Restore archived IDs before those triggers
                # run; deferred owner FKs remain checked at transaction commit.
                tables.sort(key=lambda table: table.name != "owner_sync_changes")
                for table in tables:
                    rows = plan.rows.get(table.name, [])
                    if not rows:
                        continue
                    columns = [column.name for column in specs[table.name]]
                    placeholders = ",".join("?" for _ in columns)
                    statement = (
                        f"INSERT INTO {_quote(table.name)} ({','.join(_quote(column) for column in columns)}) "
                        f"VALUES ({placeholders})"
                    )
                    for row in rows:
                        connection.exec_driver_sql(statement, tuple(row))
                invalid_fk = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                if invalid_fk:
                    raise _invalid()
        except TransferError:
            for path in created_files:
                path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            for path in created_files:
                path.unlink(missing_ok=True)
            raise _error("import_failed", "Не удалось импортировать файл переноса.") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _plan_legacy(self, bundle: _LegacyBundle) -> _LegacyObjectPlan:
        try:
            from app.models import (
                Asset,
                Attempt,
                BoardDocument,
                BoardVersion,
                CodeDocument,
                CodeVersion,
                OwnerSyncRecord,
                SolveSession,
                Task,
                TaskAsset,
                TaskRevision,
                User,
                utcnow,
            )
        except ImportError as exc:
            raise _error("runtime_unavailable", "Полная схема приложения недоступна.") from exc

        plan = _LegacyObjectPlan()
        local_objects: dict[tuple[type[Any], str], Any] = {}
        with self.session_factory() as session:

            def add_object(obj: Any, entity: str) -> None:
                key_column = next(column for column in obj.__table__.primary_key.columns)
                key = getattr(obj, key_column.name)
                identity = (type(obj), str(key))
                previous = local_objects.get(identity)
                if previous is not None:
                    if not _orm_equivalent(previous, obj):
                        raise _conflict()
                    plan.duplicate_records += 1
                    return
                existing = session.get(type(obj), key)
                if existing is not None:
                    if not _orm_equivalent(existing, obj):
                        raise _conflict()
                    plan.duplicate_records += 1
                    local_objects[identity] = existing
                    return
                local_objects[identity] = obj
                plan.objects.append(obj)
                plan.imported[entity] += 1

            add_object(User(id=self.owner_id, role="owner", created_at=utcnow()), "users")
            asset_id_map: dict[str, str] = {}
            root = _asset_root(self.settings)
            for asset in bundle.assets:
                local_id = _uuid5("legacy-asset", asset["id"])
                asset_id_map[asset["id"]] = local_id
                if len(asset["name"].encode("utf-8")) > 255:
                    raise _limit()
                # Keep the generated cache path short enough for Windows
                # MAX_PATH while retaining a deterministic, checksum-bound
                # identity.  The display filename remains in Asset.filename.
                storage_key = f"transfer/{asset['sha256'][:32]}-{local_id[:8]}"
                storage_key = _safe_relative_path(storage_key)
                path = _asset_path(root, storage_key)
                if path.exists():
                    if path.is_symlink() or not path.is_file() or _sha256(path.read_bytes()) != asset["sha256"]:
                        raise _conflict()
                else:
                    plan.asset_writes.append(_AssetWrite(path, asset["bytes"]))
                obj = Asset(
                    id=local_id,
                    source_name=TRANSFER_FORMAT,
                    canonical_url=f"kontur://transfer/{asset['id']}",
                    storage_key=storage_key,
                    filename=asset["name"][:255],
                    media_type=asset["mime"],
                    detected_type=asset["mime"],
                    size_bytes=len(asset["bytes"]),
                    content_hash=asset["sha256"],
                    expected_content_hash=asset["sha256"],
                    cache_state="ready",
                    rights_status="review_required",
                    fetch_attempts=0,
                    fetch_error=None,
                    hash_verified=True,
                    fetched_at=utcnow(),
                    created_at=utcnow(),
                )
                add_object(obj, "assets")

            task_map: dict[tuple[str, str], tuple[str, str]] = {}
            revisions_by_source: dict[str, list[str]] = defaultdict(list)
            for task in bundle.tasks:
                if len(task["sourceId"].encode("utf-8")) > 255:
                    raise _limit()
                task_pair = (task["sourceId"], task["revision"])
                revisions_by_source[task["sourceId"]].append(task["revision"])
                if task["sourceId"].startswith("kompege:"):
                    external_id = task["sourceId"][len("kompege:") :]
                    seeded_task = (
                        session.query(Task)
                        .filter(
                            Task.source_name == "kompege",
                            Task.external_id == external_id,
                        )
                        .one_or_none()
                    )
                    seeded_revision = None
                    if seeded_task is not None:
                        seeded_revision = (
                            session.query(TaskRevision)
                            .filter(
                                TaskRevision.task_id == seeded_task.id,
                                TaskRevision.source_id == task["sourceId"],
                                TaskRevision.content_hash == task["revision"],
                            )
                            .one_or_none()
                        )
                    if seeded_task is not None and seeded_revision is not None:
                        task_map[task_pair] = (seeded_task.id, seeded_revision.id)
                        continue
                task_id = _uuid5("legacy-task", task["sourceId"])
                revision_id = _uuid5("legacy-revision", f"{task['sourceId']}:{task['revision']}")
                task_map[task_pair] = (task_id, revision_id)
                add_object(
                    Task(
                        id=task_id,
                        source_name=TRANSFER_FORMAT,
                        external_id=task["sourceId"][:255],
                        line_number=task["typeNumber"],
                        current_revision_id=None,
                        created_at=utcnow(),
                        updated_at=utcnow(),
                    ),
                    "tasks",
                )
                manifest = [
                    {
                        "asset_id": asset_id_map[asset_id],
                        "filename": next(item["name"] for item in bundle.assets if item["id"] == asset_id),
                        "content_hash": next(item["sha256"] for item in bundle.assets if item["id"] == asset_id),
                    }
                    for asset_id in task["assetIds"]
                ]
                digest = _sha256(_canonical_json(task).encode("utf-8"))
                revision_number = sorted(revisions_by_source[task["sourceId"]]).index(task["revision"]) + 1
                add_object(
                    TaskRevision(
                        id=revision_id,
                        task_id=task_id,
                        revision_number=revision_number,
                        source_url=f"kontur://transfer/{task['sourceId']}/{task['revision']}",
                        source_id=task["sourceId"],
                        line_number=task["typeNumber"],
                        exam_year=None,
                        target_exam_year=None,
                        source_year=None,
                        source_year_basis="unproven_import",
                        source_created_at=None,
                        source_difficulty=None,
                        difficulty=None,
                        source_label=None,
                        source_metadata={
                            "format": TRANSFER_FORMAT,
                            "original_revision": task["revision"],
                            "original_title": task["title"],
                            "original_blocks": task["blocks"],
                        },
                        statement=task["statement"],
                        statement_latex=None,
                        answer=None,
                        explanation=None,
                        content_hash=digest,
                        retrieved_at=utcnow(),
                        parser_version=TRANSFER_FORMAT,
                        rights_status="review_required",
                        asset_manifest=manifest,
                        subtask_manifest=[],
                        table_manifest=None,
                        created_at=utcnow(),
                    ),
                    "taskRevisions",
                )
                for position, asset_id in enumerate(task["assetIds"]):
                    add_object(
                        TaskAsset(
                            id=_uuid5("legacy-task-asset", f"{revision_id}:{asset_id}"),
                            revision_id=revision_id,
                            asset_id=asset_id_map[asset_id],
                            runner_filename=asset_id[:255],
                            position=position,
                        ),
                        "taskAssets",
                    )

            def add_own_document_task(document: Mapping[str, Any]) -> tuple[str, str]:
                """Give an unlinked private draft the revision required by SolveSession."""

                document_id = str(document["id"])
                task_id = _uuid5("legacy-own-task", document_id)
                revision_id = _uuid5("legacy-own-revision", f"{document_id}:{document['revision']}")
                updated_at = _utc_from_ms(document["updatedAt"])
                source_id = f"own:{_sha256(document_id.encode('utf-8'))[:32]}"
                add_object(
                    Task(
                        id=task_id,
                        source_name="own",
                        external_id=_uuid5("legacy-own-external", document_id),
                        line_number=document["typeNumber"] or 0,
                        current_revision_id=None,
                        created_at=updated_at,
                        updated_at=updated_at,
                    ),
                    "ownTasks",
                )
                content_hash = _sha256(_canonical_json(document).encode("utf-8"))
                add_object(
                    TaskRevision(
                        id=revision_id,
                        task_id=task_id,
                        revision_number=document["revision"],
                        source_url=f"kontur://own/{document_id}",
                        source_id=source_id,
                        line_number=document["typeNumber"] or 0,
                        exam_year=None,
                        target_exam_year=None,
                        source_year=None,
                        source_year_basis="private_draft",
                        source_created_at=None,
                        source_difficulty=None,
                        difficulty=None,
                        source_label="Своя работа",
                        source_metadata={
                            "format": TRANSFER_FORMAT,
                            "own_work": True,
                            "original_document_id": document_id,
                            "original_revision": document["revision"],
                        },
                        statement=document["statement"],
                        statement_latex=None,
                        answer=None,
                        explanation=None,
                        content_hash=content_hash,
                        retrieved_at=updated_at,
                        parser_version=TRANSFER_FORMAT,
                        rights_status="private_authorized",
                        asset_manifest=[],
                        subtask_manifest=[],
                        table_manifest=None,
                        created_at=updated_at,
                    ),
                    "ownTaskRevisions",
                )
                task = local_objects.get((Task, task_id)) or session.get(Task, task_id)
                current = (
                    session.get(TaskRevision, task.current_revision_id) if task and task.current_revision_id else None
                )
                if task is not None and (current is None or current.revision_number <= document["revision"]):
                    plan.task_heads[task_id] = revision_id
                return task_id, revision_id

            # A legacy head can establish a current revision only when the
            # imported source has no local head.  It never rolls back a newer
            # revision already present in the application.
            for source_id, revision in bundle.heads.items():
                task_id, revision_id = task_map[(source_id, revision)]
                task = local_objects.get((Task, task_id)) or session.get(Task, task_id)
                if task is not None and task.current_revision_id is None:
                    plan.task_heads[task_id] = revision_id

            session_map: dict[str, str] = {}
            for attempt in bundle.attempts:
                key = f"attempt:{attempt['id']}"
                ledger_id = hashlib.sha256(key.encode()).hexdigest()
                value = dict(attempt)
                add_object(
                    OwnerSyncRecord(
                        id=ledger_id,
                        owner_id=self.owner_id,
                        kind="imported_attempt",
                        context={
                            "original_id": attempt["id"],
                            "task_source_id": attempt["taskSourceId"],
                            "task_revision": attempt["taskRevision"],
                        },
                        value=value,
                        version=1,
                        updated_at=utcnow(),
                    ),
                    "importLedger",
                )
                task_pair = (attempt["taskSourceId"], attempt["taskRevision"])
                if task_pair not in task_map:
                    plan.warnings.append("legacy_unknown_revision")
                    continue
                task_id, revision_id = task_map[task_pair]
                solve_id = _uuid5("legacy-solve", attempt["id"])
                session_map[attempt["id"]] = solve_id
                add_object(
                    SolveSession(
                        id=solve_id,
                        owner_id=self.owner_id,
                        task_revision_id=revision_id,
                        conversation_id=None,
                        status="imported",
                        created_at=_utc_from_ms(attempt["createdAt"]),
                        updated_at=_utc_from_ms(attempt["createdAt"]),
                    ),
                    "solveSessions",
                )
                document_id = _uuid5("legacy-code-document", attempt["id"])
                code_version_id = _uuid5("legacy-code-version", attempt["id"])
                add_object(
                    CodeDocument(
                        id=document_id,
                        owner_id=self.owner_id,
                        solve_session_id=solve_id,
                        language="python",
                        current_version_id=code_version_id,
                        current_version=1,
                        updated_at=_utc_from_ms(attempt["createdAt"]),
                    ),
                    "codeDocuments",
                )
                add_object(
                    CodeVersion(
                        id=code_version_id,
                        document_id=document_id,
                        version=1,
                        content=attempt["code"],
                        content_hash=_sha256(attempt["code"].encode("utf-8")),
                        created_at=_utc_from_ms(attempt["createdAt"]),
                    ),
                    "codeVersions",
                )
                add_object(
                    Attempt(
                        id=attempt["id"],
                        owner_id=self.owner_id,
                        solve_session_id=solve_id,
                        status="completed",
                        started_at=_utc_from_ms(attempt["createdAt"]),
                        completed_at=_utc_from_ms(attempt["createdAt"]),
                        user_answer=attempt["answer"],
                        verified_correct=None,
                        verification_method="imported_unproven",
                        independent=True,
                        hint_count=0,
                        error_type=None,
                        notes="Imported history; verdict is not a verified LearningEvent.",
                    ),
                    "attempts",
                )

            for document in bundle.documents:
                if len(document["title"].encode("utf-8")) > 255:
                    raise _limit()
                task_pair = (document["taskSourceId"], document["taskRevision"])
                linked = task_pair in task_map
                if linked:
                    task_id, revision_id = task_map[(document["taskSourceId"], document["taskRevision"])]
                elif document["taskSourceId"] is None and document["taskRevision"] is None:
                    # Native "Своя работа" documents have no catalog identity.
                    # Materialize a private task/revision solely so the normal
                    # solve-session/code/board APIs can reopen the saved draft.
                    task_id, revision_id = add_own_document_task(document)
                    linked = True
                ledger_id = hashlib.sha256(f"document:{document['id']}".encode()).hexdigest()
                add_object(
                    OwnerSyncRecord(
                        id=ledger_id,
                        owner_id=self.owner_id,
                        kind="imported_document",
                        context={
                            "original_id": document["id"],
                            "task_source_id": document["taskSourceId"],
                            "task_revision": document["taskRevision"],
                        },
                        value=dict(document),
                        version=1,
                        updated_at=_utc_from_ms(document["updatedAt"]),
                    ),
                    "importLedger",
                )
                if not linked:
                    plan.warnings.append("legacy_unknown_document_ledger_only")
                    continue
                solve_id = _uuid5("legacy-document-solve", document["id"])
                add_object(
                    SolveSession(
                        id=solve_id,
                        owner_id=self.owner_id,
                        task_revision_id=revision_id,
                        conversation_id=None,
                        status="draft",
                        created_at=_utc_from_ms(document["updatedAt"]),
                        updated_at=_utc_from_ms(document["updatedAt"]),
                    ),
                    "solveSessions",
                )
                code_document_id = _uuid5("legacy-document-code", document["id"])
                code_version_id = _uuid5("legacy-document-code-version", document["id"])
                add_object(
                    CodeDocument(
                        id=code_document_id,
                        owner_id=self.owner_id,
                        solve_session_id=solve_id,
                        language="python",
                        current_version_id=code_version_id,
                        current_version=1,
                        updated_at=_utc_from_ms(document["updatedAt"]),
                    ),
                    "codeDocuments",
                )
                add_object(
                    CodeVersion(
                        id=code_version_id,
                        document_id=code_document_id,
                        version=1,
                        content=document["code"],
                        content_hash=_sha256(document["code"].encode("utf-8")),
                        created_at=_utc_from_ms(document["updatedAt"]),
                    ),
                    "codeVersions",
                )
                board_document_id = _uuid5("legacy-document-board", document["id"])
                board_version_id = _uuid5("legacy-document-board-version", document["id"])
                board_raw = _canonical_json(document["board"])
                add_object(
                    BoardDocument(
                        id=board_document_id,
                        owner_id=self.owner_id,
                        solve_session_id=solve_id,
                        current_version_id=board_version_id,
                        current_version=1,
                        updated_at=_utc_from_ms(document["updatedAt"]),
                    ),
                    "boardDocuments",
                )
                add_object(
                    BoardVersion(
                        id=board_version_id,
                        document_id=board_document_id,
                        version=1,
                        snapshot_json=document["board"],
                        payload_hash=_sha256(board_raw.encode("utf-8")),
                        size_bytes=len(board_raw.encode("utf-8")),
                        created_at=_utc_from_ms(document["updatedAt"]),
                    ),
                    "boardVersions",
                )

            if bundle.preferences:
                ledger_id = hashlib.sha256(b"preferences").hexdigest()
                add_object(
                    OwnerSyncRecord(
                        id=ledger_id,
                        owner_id=self.owner_id,
                        kind="preferences",
                        context={"kind": "preferences"},
                        value=dict(bundle.preferences),
                        version=1,
                        updated_at=utcnow(),
                    ),
                    "preferences",
                )
        return plan

    def _apply_legacy(self, plan: _LegacyObjectPlan) -> None:
        created_files: list[Path] = []
        try:
            for item in plan.asset_writes:
                item.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = item.path.with_name(f".{item.path.name}.import-part")
                temporary.unlink(missing_ok=True)
                with temporary.open("xb") as handle:
                    item.copy_to(handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.rename(temporary, item.path)
                created_files.append(item.path)
            # Delayed current-revision assignments avoid the Task ↔
            # TaskRevision cycle while keeping the original head unchanged when
            # a newer local revision is already present.
            task_class = next(
                (obj.__class__ for obj in plan.objects if obj.__class__.__name__ == "Task"),
                None,
            )
            if task_class is None and plan.task_heads:
                from app.models import Task as task_class
            for obj in plan.objects:
                if obj.__class__.__name__ == "Task" and obj.id in plan.task_heads:
                    obj.current_revision_id = None
            with self.session_factory.begin() as session:
                # The copied models intentionally do not declare ORM
                # relationships, so SQLAlchemy's unit-of-work cannot infer
                # every FK ordering from the object graph.  Flush the small
                # dependency layers explicitly.
                order = {
                    "User": 0,
                    "Asset": 1,
                    "Task": 2,
                    "TaskRevision": 3,
                    "TaskAsset": 4,
                    "SolveSession": 5,
                    "CodeDocument": 6,
                    "BoardDocument": 6,
                    "CodeVersion": 7,
                    "BoardVersion": 7,
                    "Attempt": 8,
                    "OwnerSyncRecord": 8,
                }
                for class_name in sorted(
                    {obj.__class__.__name__ for obj in plan.objects}, key=lambda name: order.get(name, 99)
                ):
                    batch = [obj for obj in plan.objects if obj.__class__.__name__ == class_name]
                    session.add_all(batch)
                    session.flush()
                for task_id, revision_id in plan.task_heads.items():
                    task = session.get(task_class, task_id) if task_class is not None else None
                    if task is not None:
                        task.current_revision_id = revision_id
                session.flush()
        except TransferError:
            for path in created_files:
                path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            for path in created_files:
                path.unlink(missing_ok=True)
            raise _error("import_failed", "Не удалось импортировать файл переноса.") from exc

    def migrate_mini_store(self, path: Path) -> dict[str, Any]:
        source = Path(path).resolve()
        data = _read_bounded(source)
        digest = _sha256(data)
        format_name, bundle = self._read_bytes(data)
        if format_name != LOCAL_FORMAT or not isinstance(bundle, _LegacyBundle):
            raise _error("unsupported_format", "Ожидалось локальное хранилище Kontur.")
        ledger_id = hashlib.sha256(f"mini:{digest}".encode()).hexdigest()
        try:
            from app.models import OwnerSyncRecord
        except ImportError as exc:
            raise _error("runtime_unavailable", "Полная схема приложения недоступна.") from exc
        with self.session_factory() as session:
            if session.get(OwnerSyncRecord, ledger_id) is not None:
                return self._result(
                    LOCAL_FORMAT, digest, {}, len(bundle.tasks) + len(bundle.attempts), "", ["already_migrated"]
                )
        plan = self._plan_legacy(bundle)
        # The receipt is part of the same object batch as the imported rows.
        # This closes the crash window between application-row commit and a
        # later receipt write: a digest is acknowledged only when the whole
        # migration transaction has committed.
        plan.objects.append(
            OwnerSyncRecord(
                id=ledger_id,
                owner_id=self.owner_id,
                kind="import_receipt",
                context={"format": LOCAL_FORMAT, "source_sha256": digest},
                value={"source_sha256": digest, "imported": dict(plan.imported)},
                version=1,
                updated_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        plan.imported["importReceipts"] += 1
        backup = _backup_live_database(self.engine, self.profile, "pre-migration")
        source_backup: Path | None = None
        try:
            source_backup = _backup_source_bytes(data, self.profile, digest)
            self._apply_legacy(plan)
        except Exception:
            if source_backup is not None:
                source_backup.unlink(missing_ok=True)
            raise
        result = self._result(
            LOCAL_FORMAT, digest, dict(plan.imported), plan.duplicate_records, str(backup), plan.warnings
        )
        result["sourceBackupPath"] = str(source_backup) if source_backup else ""
        return result


def _orm_values(obj: Any) -> tuple[Any, ...]:
    values: list[Any] = []
    for column in obj.__table__.columns:
        value = getattr(obj, column.name)
        if isinstance(value, (dict, list)):
            value = _canonical_json(value)
        values.append(value)
    return tuple(values)


def _orm_equivalent(left: Any, right: Any) -> bool:
    """Compare imported rows without treating generated audit times as data."""

    volatile = {
        "created_at",
        "updated_at",
        "retrieved_at",
        "fetched_at",
    }
    if left.__class__.__name__ == "Task":
        # A newer local revision is deliberately not rolled back by a legacy
        # head.  The immutable task/revision payload is compared separately.
        volatile.add("current_revision_id")
    left_values: list[Any] = []
    right_values: list[Any] = []
    for column in left.__table__.columns:
        if column.name in volatile:
            continue
        left_value = getattr(left, column.name)
        right_value = getattr(right, column.name)
        if isinstance(left_value, (dict, list)):
            left_value = _canonical_json(left_value)
        if isinstance(right_value, (dict, list)):
            right_value = _canonical_json(right_value)
        left_values.append(left_value)
        right_values.append(right_value)
    return tuple(left_values) == tuple(right_values)


__all__ = [
    "FULL_APPLICATION_ID",
    "FULL_FORMAT",
    "LOCAL_APPLICATION_ID",
    "LOCAL_FORMAT",
    "MAX_CONTAINER_BYTES",
    "TRANSFER_APPLICATION_ID",
    "TRANSFER_FORMAT",
    "TransferError",
    "TransferManager",
]

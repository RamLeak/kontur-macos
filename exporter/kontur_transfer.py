"""Build data-only .kontur containers with the Python standard library.

This module must never import the server application or its database initializer.
The public asset root is fixed by the deployment adapter, never by a request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time
from typing import Any


APPLICATION_ID = 0x4B4F4E54
FORMAT = "kontur-transfer-v1"
MAX_CONTAINER_BYTES = 256 * 1024 * 1024
MAX_ASSET_BYTES = 16 * 1024 * 1024
MAX_ASSETS_BYTES = 128 * 1024 * 1024
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_ANSWER_BYTES = 64 * 1024
MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_TIMESTAMP = 8_640_000_000_000_000

# Owned by storage's docs/TRANSFER_FORMAT.md. No input-provided SQL is executed.
DDL = """
PRAGMA application_id=1263488596;
PRAGMA user_version=1;
CREATE TABLE metadata (key TEXT NOT NULL PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE assets (
  id TEXT NOT NULL PRIMARY KEY, name TEXT NOT NULL, mime TEXT NOT NULL,
  bytes BLOB NOT NULL, sha256 TEXT NOT NULL
);
CREATE TABLE tasks (
  source_id TEXT NOT NULL, revision TEXT NOT NULL, type_number INTEGER NOT NULL,
  title TEXT NOT NULL, statement TEXT NOT NULL, blocks_json TEXT,
  asset_ids_json TEXT NOT NULL, PRIMARY KEY(source_id,revision)
);
CREATE TABLE task_heads (source_id TEXT NOT NULL PRIMARY KEY, revision TEXT NOT NULL);
CREATE TABLE attempts (
  id TEXT NOT NULL PRIMARY KEY, task_source_id TEXT NOT NULL,
  task_revision TEXT, verdict TEXT NOT NULL, source TEXT NOT NULL,
  created_at INTEGER NOT NULL, code TEXT NOT NULL, answer TEXT NOT NULL
);
CREATE TABLE documents (
  id TEXT NOT NULL PRIMARY KEY, task_source_id TEXT, task_revision TEXT,
  title TEXT NOT NULL, type_number INTEGER, code TEXT NOT NULL,
  statement TEXT NOT NULL, board_json TEXT NOT NULL,
  revision INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE preferences (key TEXT NOT NULL PRIMARY KEY, value_json TEXT NOT NULL);
"""


@dataclass
class Bundle:
    """Internal camelCase records; assets use raw bytes, not base64 or paths."""

    tasks: list[dict[str, Any]] = field(default_factory=list)
    task_heads: dict[str, str] = field(default_factory=dict)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)
    assets: list[dict[str, Any]] = field(default_factory=list)
    preferences: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExportResult:
    path: str
    content_sha256: str
    task_count: int
    attempt_count: int
    document_count: int
    asset_count: int


class ExportError(ValueError):
    """A bounded, safe-to-display export failure with no underlying OS text."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _invalid() -> None:
    raise ExportError("invalid_export_data", "Данные не соответствуют формату переноса.")


def _text(value: Any, maximum: int, *, nonempty: bool = False, identifier: bool = False) -> str:
    if not isinstance(value, str):
        _invalid()
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _invalid()
    if size > maximum or (nonempty and size == 0) or "\x00" in value:
        _invalid()
    if identifier and any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        _invalid()
    return value


def _id(value: Any) -> str:
    return _text(value, 256, nonempty=True, identifier=True)


def _integer(value: Any, minimum: int = 0, maximum: int = MAX_SAFE_INTEGER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _invalid()
    return value


def _json(value: Any, maximum: int) -> str:
    def inspect(item: Any, depth: int) -> None:
        if depth > 126:
            _invalid()
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    _invalid()
                inspect(child, depth + 1)
        elif type(item) is list:
            for child in item:
                inspect(child, depth + 1)
        elif item is not None and type(item) not in (str, int, float, bool):
            _invalid()
    inspect(value, 0)
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        _invalid()
    return _text(encoded, maximum)


def _keys(record: Any, expected: str) -> None:
    if type(record) is not dict or set(record) != set(expected.split()):
        _invalid()


def _unique(rows: list[tuple], index: int = 0) -> None:
    if len({row[index] for row in rows}) != len(rows):
        _invalid()


def _validated_rows(bundle: Bundle) -> dict[str, list[tuple]]:
    if not isinstance(bundle, Bundle):
        _invalid()
    collections = (
        (bundle.tasks, 50_000, list), (bundle.task_heads, 50_000, dict),
        (bundle.attempts, 500_000, list), (bundle.documents, 10_000, list),
        (bundle.assets, 10_000, list), (bundle.preferences, 100, dict),
    )
    if any(type(value) is not expected or len(value) > limit for value, limit, expected in collections):
        _invalid()
    rows: dict[str, list[tuple]] = {name: [] for name in ("assets", "tasks", "task_heads", "attempts", "documents", "preferences")}
    asset_total = 0
    for asset in bundle.assets:
        _keys(asset, "id name mime bytes sha256")
        name = _text(asset["name"], 512, nonempty=True)
        mime = _text(asset["mime"], 128, nonempty=True)
        data = asset["bytes"]
        checksum = asset["sha256"]
        if "/" in name or "\\" in name or name in (".", ".."):
            _invalid()
        if not re.fullmatch(r"[A-Za-z0-9!#$&^_.+\-]+/[A-Za-z0-9!#$&^_.+\-]+", mime):
            _invalid()
        if type(data) is not bytes or len(data) > MAX_ASSET_BYTES:
            _invalid()
        if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum) or hashlib.sha256(data).hexdigest() != checksum:
            _invalid()
        asset_total += len(data)
        rows["assets"].append((_id(asset["id"]), name, mime, data, checksum))
    if asset_total > MAX_ASSETS_BYTES:
        _invalid()
    _unique(rows["assets"])
    asset_ids = {row[0] for row in rows["assets"]}

    for task in bundle.tasks:
        _keys(task, "sourceId revision typeNumber title statement blocks assetIds")
        blocks = task["blocks"]
        references = task["assetIds"]
        if blocks is not None and type(blocks) not in (dict, list):
            _invalid()
        if type(references) is not list or len(references) > 10_000:
            _invalid()
        references = [_id(reference) for reference in references]
        if len(set(references)) != len(references) or not set(references) <= asset_ids:
            _invalid()
        rows["tasks"].append((
            _id(task["sourceId"]), _id(task["revision"]), _integer(task["typeNumber"], 1, 27),
            _text(task["title"], 512, nonempty=True), _text(task["statement"], MAX_BODY_BYTES),
            None if blocks is None else _json(blocks, MAX_BODY_BYTES), _json(references, MAX_BODY_BYTES),
        ))
    task_pairs = {(row[0], row[1]) for row in rows["tasks"]}
    task_sources = {row[0] for row in rows["tasks"]}
    if len(task_pairs) != len(rows["tasks"]) or set(bundle.task_heads) != task_sources:
        _invalid()
    for source_id, revision in bundle.task_heads.items():
        pair = (_id(source_id), _id(revision))
        if pair not in task_pairs:
            _invalid()
        rows["task_heads"].append(pair)

    for attempt in bundle.attempts:
        _keys(attempt, "id taskSourceId taskRevision verdict source createdAt code answer")
        source_id = _id(attempt["taskSourceId"])
        revision = attempt["taskRevision"]
        if revision is not None:
            revision = _id(revision)
            if (source_id, revision) not in task_pairs:
                _invalid()
        elif source_id not in task_sources:
            _invalid()
        if attempt["verdict"] not in ("correct", "wrong", "unverified", "error") or attempt["source"] not in ("manual", "imported"):
            _invalid()
        rows["attempts"].append((
            _id(attempt["id"]), source_id, revision, attempt["verdict"], attempt["source"],
            _integer(attempt["createdAt"], 0, MAX_TIMESTAMP), _text(attempt["code"], MAX_BODY_BYTES),
            _text(attempt["answer"], MAX_ANSWER_BYTES),
        ))
    _unique(rows["attempts"])

    for document in bundle.documents:
        _keys(document, "id taskSourceId taskRevision title typeNumber code statement board revision updatedAt")
        source_id, revision = document["taskSourceId"], document["taskRevision"]
        if (source_id is None) != (revision is None):
            _invalid()
        if source_id is not None and (_id(source_id), _id(revision)) not in task_pairs:
            _invalid()
        type_number = document["typeNumber"]
        if type_number is not None:
            type_number = _integer(type_number, 1, 27)
        if type(document["board"]) is not dict:
            _invalid()
        rows["documents"].append((
            _id(document["id"]), source_id, revision, _text(document["title"], 512, nonempty=True),
            type_number, _text(document["code"], MAX_BODY_BYTES), _text(document["statement"], MAX_BODY_BYTES),
            _json(document["board"], MAX_BODY_BYTES), _integer(document["revision"], 1), _integer(document["updatedAt"], 0, MAX_TIMESTAMP),
        ))
    _unique(rows["documents"])
    document_ids = {row[0] for row in rows["documents"]}
    for key, value in bundle.preferences.items():
        _id(key)
        if key == "selectedDocumentId" and value is not None and (type(value) is not str or value not in document_ids):
            _invalid()
        if key == "kontur.legacy12.transferWarnings":
            if type(value) is not list:
                _invalid()
            for code in value:
                _id(code)
        rows["preferences"].append((key, _json(value, MAX_ANSWER_BYTES)))
    for name, values in rows.items():
        rows[name] = sorted(values, key=lambda row: (row[0], row[1]) if name == "tasks" else (row[0],))
    return rows


def _publish_new(source: Path, destination: Path) -> None:
    if os.name == "nt":
        # Windows rename fails if the target exists; unlike replace, no clobber.
        os.rename(source, destination)
    else:
        os.link(source, destination)


def write_bundle(destination: Path, bundle: Bundle, *, created_at: int | None = None) -> ExportResult:
    """Validate, fsync and atomically publish a new data-only container.

    Existing destinations are never replaced. A temporary sibling is removed on
    failure. Publication never uses an overwrite-capable replace operation.
    """
    rows = _validated_rows(bundle)
    created_at = _integer(int(time.time() * 1000) if created_at is None else created_at, 0, MAX_TIMESTAMP)
    destination = Path(destination)
    if destination.suffix.lower() != ".kontur":
        raise ExportError("invalid_destination", "Выберите новый файл с расширением .kontur.")
    temporary: Path | None = None
    connection: sqlite3.Connection | None = None
    try:
        if destination.exists() or destination.is_symlink():
            raise ExportError("destination_exists", "Файл уже существует. Выберите другое имя.")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".kontur-export-", suffix=".tmp", dir=destination.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        connection = sqlite3.connect(temporary)
        connection.enable_load_extension(False)
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA page_size=4096")
        connection.execute("PRAGMA max_page_count=65536")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(DDL)
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany("INSERT INTO metadata(key,value) VALUES (?,?)", [
            ("format", FORMAT), ("schemaVersion", "1"),
            ("producer", "kontur-legacy12-exporter/1"), ("createdAt", str(created_at)),
        ])
        connection.executemany("INSERT INTO assets(id,name,mime,bytes,sha256) VALUES (?,?,?,?,?)", rows["assets"])
        connection.executemany("INSERT INTO tasks(source_id,revision,type_number,title,statement,blocks_json,asset_ids_json) VALUES (?,?,?,?,?,?,?)", rows["tasks"])
        connection.executemany("INSERT INTO task_heads(source_id,revision) VALUES (?,?)", rows["task_heads"])
        connection.executemany("INSERT INTO attempts(id,task_source_id,task_revision,verdict,source,created_at,code,answer) VALUES (?,?,?,?,?,?,?,?)", rows["attempts"])
        connection.executemany("INSERT INTO documents(id,task_source_id,task_revision,title,type_number,code,statement,board_json,revision,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", rows["documents"])
        connection.executemany("INSERT INTO preferences(key,value_json) VALUES (?,?)", rows["preferences"])
        connection.commit()
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ExportError("export_failed", "Не удалось проверить файл переноса.")
        connection.close()
        connection = None
        if temporary.stat().st_size > MAX_CONTAINER_BYTES:
            raise ExportError("export_too_large", "Файл переноса превышает допустимый размер.")
        checksum = hashlib.sha256()
        with temporary.open("r+b") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                checksum.update(chunk)
            os.fsync(handle.fileno())
        _publish_new(temporary, destination)
        return ExportResult(
            path=str(destination), content_sha256=checksum.hexdigest(),
            task_count=len(rows["tasks"]), attempt_count=len(rows["attempts"]),
            document_count=len(rows["documents"]), asset_count=len(rows["assets"]),
        )
    except ExportError:
        raise
    except FileExistsError:
        raise ExportError("destination_exists", "Файл уже существует. Выберите другое имя.") from None
    except (OSError, sqlite3.Error):
        raise ExportError("export_failed", "Не удалось создать файл переноса.") from None
    finally:
        if connection is not None:
            connection.close()
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _is_link(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def read_public_asset(root: Path, relative_path: str, *, max_bytes: int) -> bytes:
    """Read one explicitly catalogued file from a trusted, public asset root.

    No URL fetching, path discovery, SQL or code execution occurs. Hidden files,
    traversal and links are rejected. The caller supplies a validated fixed
    transfer-size limit and a root that has no untrusted concurrent writers.
    """
    if not isinstance(relative_path, str) or not relative_path or len(relative_path) > 1024:
        raise ExportError("invalid_asset_path", "Недопустимое имя вложения.")
    if (
        any(char in relative_path for char in "\\:?#")
        or any(ord(char) < 32 or ord(char) == 127 for char in relative_path)
        or any(not part or part.startswith(".") for part in relative_path.split("/"))
    ):
        raise ExportError("invalid_asset_path", "Недопустимое имя вложения.")
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ExportError("invalid_limit", "Недопустимый лимит вложения.")

    try:
        root = Path(root)
        if _is_link(root) or not root.is_dir():
            raise ExportError("invalid_asset_root", "Каталог вложений недоступен.")
        canonical_root = root.resolve(strict=True)
        candidate = canonical_root
        for part in relative_path.split("/"):
            candidate = candidate / part
            if _is_link(candidate):
                raise ExportError("invalid_asset_path", "Ссылки на вложения не поддерживаются.")
        candidate.resolve(strict=True).relative_to(canonical_root)
        before = candidate.stat()
        if not stat.S_ISREG(before.st_mode):
            raise ExportError("invalid_asset_path", "Вложение должно быть обычным файлом.")
        if before.st_size > max_bytes:
            raise ExportError("asset_too_large", "Вложение превышает допустимый размер.")
        with candidate.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            data = handle.read(max_bytes + 1)
            after = os.fstat(handle.fileno())
        fingerprints = [
            (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
            for value in (before, opened, after, candidate.stat())
        ]
        if any(value != fingerprints[0] for value in fingerprints[1:]) or len(data) != before.st_size:
            raise ExportError("asset_changed", "Вложение изменилось во время экспорта.")
        if len(data) > max_bytes:
            raise ExportError("asset_too_large", "Вложение превышает допустимый размер.")
        return data
    except ExportError:
        raise
    except (OSError, ValueError):
        raise ExportError("asset_unavailable", "Не удалось прочитать вложение задания.") from None

"""Owner-free catalog seed validation. No network, live database or source SQL."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from offline_protocol import BridgeError

APPLICATION_ID = 0x4B435331
TABLES = {
    "catalog_metadata": ("key", "value"),
    "catalog_tasks": ("id", "record_json"),
    "catalog_revisions": ("id", "record_json"),
    "catalog_assets": ("id", "record_json"),
    "catalog_links": ("id", "record_json"),
    "catalog_selection": ("revision_id",),
    "catalog_asset_bytes": ("asset_id", "sha256", "bytes"),
}
MAX_ROWS = {
    "catalog_metadata": 32,
    "catalog_tasks": 100_000,
    "catalog_revisions": 250_000,
    "catalog_assets": 100_000,
    "catalog_links": 500_000,
    "catalog_selection": 250_000,
    "catalog_asset_bytes": 100_000,
}
MAX_ASSET_BYTES = 64_000_000
MAX_ASSET_TOTAL = 3 * 1024**3


def failure(code: str = "catalog_invalid") -> BridgeError:
    return BridgeError(code, "Полный проверенный каталог заданий недоступен. Подготовьте комплект каталога.")


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def decode(value: str) -> Any:
    def bad(_constant: str) -> None:
        raise ValueError("nonfinite")

    return json.loads(value, parse_constant=bad)


def _ddl(sql: str) -> str:
    if not isinstance(sql, str) or not sql.isascii() or len(sql) > 4096 or re.search(r"[^a-zA-Z0-9_\s(),;]", sql):
        raise failure()
    return " ".join(re.findall(r"[A-Za-z0-9_]+|[(),]", sql.lower()))


def open_seed(path: Path, *, verify_integrity: bool = True) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 3 * 1024**3:
        raise failure("catalog_missing")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        try:
            connection.enable_load_extension(False)
        except (AttributeError, sqlite3.NotSupportedError):
            pass
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA ignore_check_constraints=ON")
        expected = {}
        for table, columns in TABLES.items():
            definitions = [
                name + (" BLOB" if name == "bytes" else " TEXT") + (" PRIMARY KEY" if index == 0 else " NOT NULL")
                for index, name in enumerate(columns)
            ]
            expected[table] = _ddl(f"CREATE TABLE {table} ({','.join(definitions)})")
        found = set()
        for kind, name, table, sql in connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema LIMIT 32"):
            if kind == "index" and sql is None and name.startswith("sqlite_autoindex_") and table in TABLES:
                continue
            if kind != "table" or name != table or name not in expected or _ddl(sql) != expected[name] or name in found:
                raise failure()
            found.add(name)
        if found != set(TABLES) or connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()[0] != 14:
            raise failure()
        if verify_integrity and connection.execute("PRAGMA integrity_check(1)").fetchone()[0] != "ok":
            raise failure()
        if (
            connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
            or connection.execute("PRAGMA user_version").fetchone()[0] != 1
        ):
            raise failure()
        for table, columns in TABLES.items():
            info = list(connection.execute(f"PRAGMA table_xinfo({table})"))
            if len(info) != len(columns) or any(
                row[1] != name or row[2] != ("BLOB" if name == "bytes" else "TEXT") or row[6] != 0
                for row, name in zip(info, columns, strict=True)
            ):
                raise failure()
            if connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] > MAX_ROWS[table]:
                raise failure("catalog_limit")
        connection.execute("BEGIN")
        return connection
    except Exception:
        connection.close()
        raise


def _json_bounds(value: Any, depth: int = 0, budget: list[int] | None = None) -> None:
    if budget is None:
        budget = [100_000]
    budget[0] -= 1
    if depth > 24 or budget[0] < 0:
        raise failure("catalog_limit")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise failure()
            _json_bounds(child, depth + 1, budget)
    elif isinstance(value, list):
        for child in value:
            _json_bounds(child, depth + 1, budget)


def _validate_v2_warnings(metadata: dict[str, Any], revisions: dict[str, Any]) -> None:
    affected = set()
    for key, row in revisions.items():
        source = row.get("source_metadata")
        if not isinstance(source, dict):
            raise failure()
        markers = source.get("kege_unavailable_resources", [])
        if not isinstance(markers, list) or any(
            not isinstance(marker, dict) or marker.get("reason") != "origin_not_admitted" for marker in markers
        ):
            raise failure()
        if markers:
            affected.add(key)
    issues = metadata["issues"]
    counts = metadata["issueCounts"]
    if (
        type(metadata["complete"]) is not bool
        or metadata["complete"] != (not affected)
        or not isinstance(issues, list)
        or not isinstance(counts, dict)
        or any(type(value) is not int for value in counts.values())
        or counts != ({"upstream_resource_unavailable": len(affected)} if affected else {})
    ):
        raise failure()
    found = set()
    for issue in issues:
        if (
            not isinstance(issue, dict)
            or set(issue) != {"code", "revisionId"}
            or issue["code"] != "upstream_resource_unavailable"
            or not isinstance(issue["revisionId"], str)
            or issue["revisionId"] in found
        ):
            raise failure()
        found.add(issue["revisionId"])
    if found != affected:
        raise failure()


def validate_seed(
    connection: sqlite3.Connection, *, verify_payloads: bool = True
) -> tuple[dict[str, Any], dict[str, list[Any]]]:
    metadata = {}
    for key, raw in connection.execute("SELECT key,value FROM catalog_metadata"):
        if not isinstance(key, str) or not isinstance(raw, str) or len(raw.encode("utf-8")) > 1024**2:
            raise failure()
        value = decode(raw)
        _json_bounds(value)
        if raw.encode("utf-8") != canonical(value):
            raise failure()
        metadata[key] = value
    required = {
        "format",
        "schemaVersion",
        "sourceSchema",
        "producerSha256",
        "createdAt",
        "scope",
        "limits",
        "counts",
        "complete",
        "issues",
        "issueCounts",
        "collectionsSha256",
        "manifestSha256",
    }
    version = metadata.get("schemaVersion")
    is_v2 = type(version) is int and version == 2
    if is_v2:
        required |= {"offlineReady", "sourceSeedSha256"}
    if (
        set(metadata) != required
        or metadata["format"] != ("kontur-catalog-seed-v2" if is_v2 else "kontur-catalog-seed-v1")
        or type(version) is not int
        or version != (2 if is_v2 else 1)
        or type(metadata["sourceSchema"]) is not int
        or metadata["sourceSchema"] != 12
    ):
        raise failure()
    if is_v2:
        if (
            metadata["offlineReady"] is not True
            or not isinstance(metadata["sourceSeedSha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", metadata["sourceSeedSha256"])
        ):
            raise failure()
    elif (
        metadata["complete"] is not True
        or metadata["issues"]
        or not isinstance(metadata["issueCounts"], dict)
        or any(type(v) is not int or v != 0 for v in metadata["issueCounts"].values())
    ):
        raise failure()
    if not isinstance(metadata["collectionsSha256"], dict) or set(metadata["collectionsSha256"]) != set(TABLES) - {
        "catalog_metadata"
    }:
        raise failure()
    if (
        hashlib.sha256(canonical({k: v for k, v in metadata.items() if k != "manifestSha256"})).hexdigest()
        != metadata["manifestSha256"]
    ):
        raise failure()
    collections: dict[str, list[Any]] = {}
    text_total = 0
    for table in ("catalog_tasks", "catalog_revisions", "catalog_assets", "catalog_links"):
        rows = []
        digest = hashlib.sha256()
        for key, raw in connection.execute(f"SELECT id,record_json FROM {table} ORDER BY id"):
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 255
                or not isinstance(raw, str)
                or len(raw.encode("utf-8")) > 4 * 1024**2
            ):
                raise failure()
            record = decode(raw)
            _json_bounds(record)
            if not isinstance(record, dict) or record.get("id") != key:
                raise failure()
            for name in ("statement", "statement_latex", "answer", "explanation"):
                value = record.get(name)
                if value is not None:
                    if not isinstance(value, str) or len(value.encode("utf-8")) > 2 * 1024**2:
                        raise failure()
                    text_total += len(value.encode("utf-8"))
            if text_total > 256 * 1024**2:
                raise failure("catalog_limit")
            digest.update(canonical([key, record]) + b"\n")
            rows.append(record)
        if digest.hexdigest() != metadata["collectionsSha256"][table]:
            raise failure()
        collections[table] = rows
    digest = hashlib.sha256()
    selection = []
    for (key,) in connection.execute("SELECT revision_id FROM catalog_selection ORDER BY revision_id"):
        if not isinstance(key, str):
            raise failure()
        digest.update(canonical([key]) + b"\n")
        selection.append(key)
    if (
        digest.hexdigest() != metadata["collectionsSha256"]["catalog_selection"]
        or not selection
        or not collections["catalog_tasks"]
    ):
        raise failure()
    collections["catalog_selection"] = selection
    digest = hashlib.sha256()
    byte_lengths = {}
    total_assets = 0
    for key, expected, count in connection.execute(
        "SELECT asset_id,sha256,length(bytes) FROM catalog_asset_bytes ORDER BY asset_id"
    ):
        if (
            not isinstance(key, str)
            or not isinstance(expected, str)
            or not re.fullmatch(r"[a-f0-9]{64}", expected)
            or type(count) is not int
            or not 0 <= count <= MAX_ASSET_BYTES
        ):
            raise failure()
        total_assets += count
        if total_assets > MAX_ASSET_TOTAL:
            raise failure("catalog_limit")
        if verify_payloads:
            data = connection.execute("SELECT bytes FROM catalog_asset_bytes WHERE asset_id=?", (key,)).fetchone()[0]
            if not isinstance(data, bytes) or hashlib.sha256(data).hexdigest() != expected:
                raise failure()
        digest.update(canonical([key, expected, count]) + b"\n")
        byte_lengths[key] = (expected, count)
    if digest.hexdigest() != metadata["collectionsSha256"]["catalog_asset_bytes"]:
        raise failure()
    tasks = {row["id"]: row for row in collections["catalog_tasks"]}
    revisions = {row["id"]: row for row in collections["catalog_revisions"]}
    assets = {row["id"]: row for row in collections["catalog_assets"]}
    if is_v2:
        _validate_v2_warnings(metadata, revisions)
    if set(assets) != set(byte_lengths) or any(key not in revisions for key in selection):
        raise failure()
    if any(
        row.get("source_name") != "kompege"
        or (row.get("current_revision_id") is not None and row["current_revision_id"] not in revisions)
        for row in tasks.values()
    ):
        raise failure()
    if any(
        row.get("task_id") not in tasks or row.get("rights_status") not in {"private_authorized", "public_authorized"}
        for row in revisions.values()
    ):
        raise failure()
    for key, row in assets.items():
        expected, count = byte_lengths[key]
        if (
            row.get("source_name") != "kompege"
            or row.get("rights_status") not in {"private_authorized", "public_authorized"}
            or "storage_key" in row
            or row.get("content_hash") != expected
            or row.get("expected_content_hash") not in {None, expected}
            or row.get("size_bytes") != count
            or row.get("hash_verified") is not True
            or row.get("cache_state") != "ready"
        ):
            raise failure()
    if any(
        row.get("revision_id") not in revisions or row.get("asset_id") not in assets
        for row in collections["catalog_links"]
    ):
        raise failure()
    counts = metadata["counts"]
    actual = {
        "tasks": len(tasks),
        "revisions": len(revisions),
        "assets": len(assets),
        "links": len(collections["catalog_links"]),
        "selection": len(selection),
        "assetBytesRows": len(byte_lengths),
        "assetBytesTotal": total_assets,
        "unavailableAssets": 0,
        "unresolvedAssetReferences": 0,
    }
    if (
        not isinstance(counts, dict)
        or any(type(v) is not int or v < 0 for v in counts.values())
        or any(counts.get(k) != v for k, v in actual.items())
    ):
        raise failure()
    return metadata, collections


def _model_values(model: Any, record: dict[str, Any]) -> dict[str, Any]:
    from sqlalchemy import Boolean, DateTime, Integer, String, Text

    columns = {column.name: column for column in model.__table__.columns}
    if set(record) - set(columns):
        raise failure()
    result = dict(record)
    for name, value in result.items():
        column = columns[name]
        if value is None:
            if not column.nullable:
                raise failure()
        elif isinstance(column.type, DateTime):
            if not isinstance(value, str):
                raise failure()
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            result[name] = parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed
        elif isinstance(column.type, Boolean):
            if type(value) is not bool:
                raise failure()
        elif isinstance(column.type, Integer):
            if type(value) is not int or not -(2**53 - 1) <= value <= 2**53 - 1:
                raise failure()
        elif isinstance(column.type, (String, Text)):
            if not isinstance(value, str):
                raise failure()
    return result


def _confined_target(root: Path, relative: str) -> Path:
    target = root / relative
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if current.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise failure()
    if not target.resolve().is_relative_to(root):
        raise failure()
    return target


def _stat_stamp(stat: os.stat_result) -> list[int]:
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def _file_stamp(path: Path) -> list[int]:
    if path.is_symlink() or not path.is_file():
        raise failure()
    return _stat_stamp(path.stat())


def _read_bootstrap_cache(path: Path, seed: Path) -> dict[str, Any]:
    # A disposable performance cache, never a replacement for the seed/domain
    # manifest. Any changed file is fully verified again. Assets also verify
    # their content hash at the existing delivery boundary on every read.
    try:
        if path.is_symlink() or path.stat().st_size > 8 * 1024**2:
            return {}
        with path.open("rb") as source:
            raw = source.read(8 * 1024**2 + 1)
        if len(raw) > 8 * 1024**2:
            return {}
        value = decode(raw.decode("utf-8"))
        if (
            not isinstance(value, dict)
            or value.get("version") != 1
            or value.get("seedPath") != str(seed.resolve())
            or value.get("seedStamp") != _file_stamp(seed)
            or not isinstance(value.get("assets"), dict)
            or not isinstance(value.get("manifestSha256"), str)
        ):
            return {}
        return value
    except (OSError, ValueError, RecursionError, BridgeError):
        return {}


def _write_bootstrap_cache(
    path: Path, seed: Path, seed_stamp: list[int], manifest: str, assets: dict[str, list[int]]
) -> None:
    # Failure to cache must not invalidate an already committed bootstrap.
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        if path.is_symlink() or _file_stamp(seed) != seed_stamp:
            return
        value = {
            "version": 1, "seedPath": str(seed.resolve()), "seedStamp": seed_stamp,
            "manifestSha256": manifest, "assets": assets,
        }
        with temporary.open("xb") as output:
            output.write(canonical(value))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except (OSError, ValueError, BridgeError):
        pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def load_catalog_seed(
    engine: Any, session_factory: Any, settings: Any, bundle: Path, profile: Path, owner_id: str
) -> dict[str, Any]:
    from sqlalchemy import Table, bindparam, select

    from app.catalog_selection import RECENT_CATALOG_MODE
    from app.models import Asset, CatalogEntry, OwnerSyncRecord, SourceSyncJob, Task, TaskAsset, TaskRevision
    from app.recent_catalog import _base_counters
    from app.source_catalog import ensure_source_policy

    created_files: list[Path] = []
    connection = None
    try:
        seed = bundle / "catalog" / "catalog.sqlite3"
        if not seed.is_file() or seed.is_symlink():
            raise failure("catalog_missing")
        cache_path = profile / "catalog-bootstrap-cache.json"
        cache = _read_bootstrap_cache(cache_path, seed)
        seed_stamp = _file_stamp(seed)
        connection = open_seed(seed, verify_integrity=not bool(cache))
        metadata, collections = validate_seed(connection, verify_payloads=not bool(cache))
        manifest = metadata["manifestSha256"]
        if cache and cache["manifestSha256"] != manifest:
            raise failure()
        models: dict[str, Any] = {
            "catalog_tasks": Task,
            "catalog_revisions": TaskRevision,
            "catalog_assets": Asset,
            "catalog_links": TaskAsset,
        }
        converted = {
            table: [_model_values(model, row) for row in collections[table]] for table, model in models.items()
        }
        asset_root = Path(settings.asset_root).resolve()
        verified_assets: dict[str, list[int]] = {}
        for record in converted["catalog_assets"]:
            digest = record["content_hash"]
            relative = f"{digest[:2]}/{digest}"
            target = _confined_target(asset_root, relative)
            if target.exists():
                stamp = _file_stamp(target)
                known_stamp = verified_assets.get(digest) or cache.get("assets", {}).get(digest)
                if stamp[2] != record["size_bytes"] or (
                    stamp != known_stamp and hashlib.sha256(target.read_bytes()).hexdigest() != digest
                ):
                    raise failure()
                if _file_stamp(target) != stamp:
                    raise failure()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                _confined_target(asset_root, relative)
                raw = connection.execute(
                    "SELECT bytes FROM catalog_asset_bytes WHERE asset_id=?", (record["id"],)
                ).fetchone()[0]
                if not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != digest:
                    raise failure()
                with target.open("xb") as output:
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                created_files.append(target)
                # Windows commits last-write metadata on the final handle
                # close; take both path stamps after closing the writer.
                stamp = _file_stamp(target)
                if hashlib.sha256(target.read_bytes()).hexdigest() != digest or _file_stamp(target) != stamp:
                    raise failure()
            verified_assets[digest] = stamp
            record["storage_key"] = relative
        if _file_stamp(seed) != seed_stamp:
            raise failure()
        with session_factory.begin() as session:
            policy = ensure_source_policy(session, settings)
            ledger_id = "seed:" + manifest[:59]
            ledger = session.get(OwnerSyncRecord, ledger_id)
            if ledger is not None:
                if (
                    ledger.owner_id != owner_id
                    or ledger.kind != "offline-bootstrap"
                    or ledger.context != {"source": "catalog-seed"}
                    or ledger.value != {"manifestSha256": manifest, "counts": metadata["counts"]}
                ):
                    raise failure()
                for table, model in models.items():
                    records = converted[table]
                    found = {}
                    for offset in range(0, len(records), 400):
                        keys = [record["id"] for record in records[offset : offset + 400]]
                        found.update({row.id: row for row in session.scalars(select(model).where(model.id.in_(keys)))})
                    for record in records:
                        existing = found.get(record["id"])
                        compared = (
                            ("source_name", "external_id")
                            if table == "catalog_tasks"
                            else tuple(key for key in record if key not in {"fetched_at", "created_at"})
                        )
                        if existing is None or any(getattr(existing, key) != record[key] for key in compared):
                            raise failure()
                job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "kontur-catalog:" + manifest))
                job = session.get(SourceSyncJob, job_id)
                selected = set(session.scalars(select(CatalogEntry.revision_id).where(CatalogEntry.job_id == job_id)))
                if (
                    job is None
                    or job.owner_id != owner_id
                    or job.mode != RECENT_CATALOG_MODE
                    or job.status != "succeeded"
                    or job.manifest_hash != manifest
                    or job.counters.get("published") is not True
                    or selected != set(collections["catalog_selection"])
                ):
                    raise failure()
                _write_bootstrap_cache(cache_path, seed, seed_stamp, manifest, verified_assets)
                return {"manifestSha256": manifest, "replayed": True}
            heads = []
            for record in converted["catalog_tasks"]:
                existing = session.get(Task, record["id"])
                if existing is None:
                    heads.append(
                        {
                            "seed_task_id": record["id"],
                            "seed_revision": record.get("current_revision_id"),
                            "seed_updated": record["updated_at"],
                        }
                    )
                    session.add(Task(**{**record, "current_revision_id": None}))
                elif existing.source_name != record["source_name"] or existing.external_id != record["external_id"]:
                    raise failure()
            session.flush()
            for table in ("catalog_revisions", "catalog_assets", "catalog_links"):
                model = models[table]
                for record in converted[table]:
                    existing = session.get(model, record["id"])
                    if existing is None:
                        session.add(model(**record))
                    elif any(
                        getattr(existing, key) != value
                        for key, value in record.items()
                        if key not in {"fetched_at", "created_at"}
                    ):
                        raise failure()
                session.flush()
            if heads:
                # Complete the cyclic pointers while preserving source metadata;
                # ORM onupdate would otherwise replace updated_at with install time.
                task_table = cast(Table, Task.__table__)
                session.connection().execute(
                    task_table.update()
                    .where(task_table.c.id == bindparam("seed_task_id"))
                    .values(current_revision_id=bindparam("seed_revision"), updated_at=bindparam("seed_updated")),
                    heads,
                )
            job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "kontur-catalog:" + manifest))
            counters = _base_counters()
            counters.update(
                {
                    "published": True,
                    "total_selected": len(collections["catalog_selection"]),
                    "tasks_selected": len(collections["catalog_selection"]),
                }
            )
            session.add(
                SourceSyncJob(
                    id=job_id,
                    owner_id=owner_id,
                    source_policy_id=policy.id,
                    mode=RECENT_CATALOG_MODE,
                    request_hash=manifest,
                    manifest_hash=manifest,
                    parser_version=metadata["format"],
                    status="succeeded",
                    max_items=len(collections["catalog_selection"]),
                    total_items=len(collections["catalog_selection"]),
                    cursor_index=len(collections["catalog_selection"]),
                    counters=counters,
                    idempotency_key="offline-seed:" + manifest,
                )
            )
            session.flush()
            for revision in collections["catalog_selection"]:
                session.add(
                    CatalogEntry(
                        id=str(uuid.uuid5(uuid.NAMESPACE_URL, job_id + ":" + revision)),
                        job_id=job_id,
                        revision_id=revision,
                    )
                )
            session.add(
                OwnerSyncRecord(
                    id=ledger_id,
                    owner_id=owner_id,
                    kind="offline-bootstrap",
                    context={"source": "catalog-seed"},
                    value={"manifestSha256": manifest, "counts": metadata["counts"]},
                    version=1,
                )
            )
        _write_bootstrap_cache(cache_path, seed, seed_stamp, manifest, verified_assets)
        return {"manifestSha256": manifest, "replayed": False}
    except Exception as error:
        for target in created_files:
            target.unlink(missing_ok=True)
        if isinstance(error, BridgeError):
            raise
        raise failure() from None
    finally:
        if connection is not None:
            connection.close()

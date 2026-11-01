"""Synthetic seed admission through the real SQLite and bootstrap boundaries."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from offline_catalog import open_seed, validate_seed
from offline_protocol import BridgeError


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _metadata(connection):
    return {key: json.loads(value) for key, value in connection.execute("SELECT key,value FROM catalog_metadata")}


def _refresh(connection, metadata, *, collections=True, manifest=True):
    if collections:
        for table in ("catalog_tasks", "catalog_revisions", "catalog_assets", "catalog_links"):
            lines = (
                _encode([key, json.loads(raw)]).encode() + b"\n"
                for key, raw in connection.execute(f"SELECT id,record_json FROM {table} ORDER BY id")
            )
            metadata["collectionsSha256"][table] = hashlib.sha256(b"".join(lines)).hexdigest()
        for table, query in (
            ("catalog_selection", "SELECT revision_id FROM catalog_selection ORDER BY revision_id"),
            (
                "catalog_asset_bytes",
                "SELECT asset_id,sha256,length(bytes) FROM catalog_asset_bytes ORDER BY asset_id",
            ),
        ):
            lines = (_encode(list(row)).encode() + b"\n" for row in connection.execute(query))
            metadata["collectionsSha256"][table] = hashlib.sha256(b"".join(lines)).hexdigest()
    if manifest:
        value = {key: value for key, value in metadata.items() if key != "manifestSha256"}
        metadata["manifestSha256"] = hashlib.sha256(_encode(value).encode()).hexdigest()
    connection.execute("DELETE FROM catalog_metadata")
    connection.executemany(
        "INSERT INTO catalog_metadata VALUES (?,?)", [(key, _encode(value)) for key, value in metadata.items()]
    )
    connection.commit()


def _record(connection, table, key, change):
    raw = connection.execute(f"SELECT record_json FROM {table} WHERE id=?", (key,)).fetchone()[0]
    value = json.loads(raw)
    change(value)
    connection.execute(f"UPDATE {table} SET record_json=? WHERE id=?", (_encode(value), key))


def _warning(revision_id):
    return {"code": "upstream_resource_unavailable", "revisionId": revision_id}


def _promote(path, *, affected=("r-1", "r-2")):
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    with closing(sqlite3.connect(path)) as connection:
        metadata = _metadata(connection)
        for revision_id in affected:
            _record(
                connection,
                "catalog_revisions",
                revision_id,
                lambda row: row["source_metadata"].update(
                    # Two resources still count as one affected revision.
                    kege_unavailable_resources=[
                        {"reason": "origin_not_admitted", "url": "https://example.org/synthetic.png"},
                        {"reason": "origin_not_admitted", "url": "https://example.net/synthetic.pdf"},
                    ]
                ),
            )
        metadata.update(
            format="kontur-catalog-seed-v2",
            schemaVersion=2,
            offlineReady=True,
            sourceSeedSha256=source_hash,
            complete=not bool(affected),
            issues=[_warning(key) for key in reversed(affected)],
            issueCounts={"upstream_resource_unavailable": len(affected)} if affected else {},
        )
        _refresh(connection, metadata)


def _validate(path):
    with closing(open_seed(path)) as connection:
        return validate_seed(connection)


def test_v1_acceptance_unchanged(seed_factory):
    bundle, _ = seed_factory()
    metadata, collections = _validate(bundle / "catalog" / "catalog.sqlite3")
    assert metadata["format"] == "kontur-catalog-seed-v1"
    assert metadata["complete"] is True
    assert {row["id"] for row in collections["catalog_revisions"]} == {"r-1", "r-2"}


@pytest.mark.parametrize("affected", [(), ("r-1",), ("r-1", "r-2")])
def test_v2_accepts_only_exact_reconstructed_warnings(seed_factory, affected):
    bundle, _ = seed_factory()
    path = bundle / "catalog" / "catalog.sqlite3"
    before, original = _validate(path)
    _promote(path, affected=affected)
    metadata, records = _validate(path)
    assert metadata["offlineReady"] is True
    assert metadata["complete"] is (not bool(affected))
    assert metadata["counts"] == before["counts"]
    assert records["catalog_tasks"] == original["catalog_tasks"]
    assert records["catalog_selection"] == original["catalog_selection"]
    assert len(records["catalog_revisions"]) == len(original["catalog_revisions"])


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("format", "kontur-catalog-seed-v1"),
        ("format", "kontur-catalog-seed-v3"),
        ("schemaVersion", 1),
        ("schemaVersion", True),
        ("sourceSchema", 13),
        ("sourceSchema", True),
        ("offlineReady", False),
        ("offlineReady", 1),
        ("sourceSeedSha256", "A" * 64),
        ("sourceSeedSha256", "f" * 63),
        ("sourceSeedSha256", 0),
        ("unexpected", True),
        ("complete", True),
        ("complete", 0),
        ("issues", []),
        ("issues", [_warning("r-1")]),
        ("issues", [_warning("r-1"), _warning("r-1")]),
        ("issues", [_warning("r-1"), _warning("other")]),
        ("issues", [_warning("r-1"), {**_warning("r-2"), "extra": True}]),
        ("issues", [_warning("r-1"), {"code": "asset_not_ready", "revisionId": "r-2"}]),
        ("issueCounts", {"upstream_resource_unavailable": 1}),
        ("issueCounts", {"upstream_resource_unavailable": True}),
        ("issueCounts", {"upstream_resource_unavailable": 2, "asset_not_ready": 0}),
        ("issueCounts", {}),
    ],
)
def test_v2_rejects_metadata_forgery_with_valid_manifest(seed_factory, key, value):
    bundle, _ = seed_factory()
    path = bundle / "catalog" / "catalog.sqlite3"
    _promote(path)
    with closing(sqlite3.connect(path)) as connection:
        metadata = _metadata(connection)
        metadata[key] = value
        _refresh(connection, metadata)
    with pytest.raises(BridgeError):
        _validate(path)


@pytest.mark.parametrize("value", [False, 0, None])
def test_v2_incomplete_without_source_warning_fails(seed_factory, value):
    bundle, _ = seed_factory()
    path = bundle / "catalog" / "catalog.sqlite3"
    _promote(path, affected=())
    with closing(sqlite3.connect(path)) as connection:
        metadata = _metadata(connection)
        metadata["complete"] = value
        _refresh(connection, metadata)
    with pytest.raises(BridgeError):
        _validate(path)


@pytest.mark.parametrize("markers", [None, {}, ["origin_not_admitted"], [{"reason": "asset_not_ready"}], [{}], []])
def test_v2_warning_must_match_original_markers(seed_factory, markers):
    bundle, _ = seed_factory()
    path = bundle / "catalog" / "catalog.sqlite3"
    _promote(path)
    with closing(sqlite3.connect(path)) as connection:
        _record(
            connection,
            "catalog_revisions",
            "r-1",
            lambda row: row["source_metadata"].update(kege_unavailable_resources=markers),
        )
        _refresh(connection, _metadata(connection))
    with pytest.raises(BridgeError):
        _validate(path)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("damage", ["missing", "bytes", "not_ready", "size", "unverified", "expected_hash", "rights"])
def test_no_version_waives_admitted_asset_gates(seed_factory, version, damage):
    bundle, _ = seed_factory()
    path = bundle / "catalog" / "catalog.sqlite3"
    if version == 2:
        _promote(path)
    with closing(sqlite3.connect(path)) as connection:
        metadata = _metadata(connection)
        if damage == "missing":
            connection.execute("DELETE FROM catalog_asset_bytes")
            metadata["counts"].update(assetBytesRows=0, assetBytesTotal=0, unavailableAssets=1)
        elif damage == "bytes":
            connection.execute("UPDATE catalog_asset_bytes SET bytes=?", (b"bad bytes",))
        else:
            field, value = {
                "not_ready": ("cache_state", "metadata_only"),
                "size": ("size_bytes", 999),
                "unverified": ("hash_verified", False),
                "expected_hash": ("expected_content_hash", "0" * 64),
                "rights": ("rights_status", "denied"),
            }[damage]
            _record(connection, "catalog_assets", "a-1", lambda row: row.update({field: value}))
        _refresh(connection, metadata)
    with pytest.raises(BridgeError):
        _validate(path)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("damage", ["manifest", "collection", "count", "unresolved", "link"])
def test_no_version_waives_integrity_or_reference_gates(seed_factory, version, damage):
    bundle, _ = seed_factory()
    path = bundle / "catalog" / "catalog.sqlite3"
    if version == 2:
        _promote(path)
    with closing(sqlite3.connect(path)) as connection:
        metadata = _metadata(connection)
        if damage == "manifest":
            metadata["manifestSha256"] = "0" * 64
        elif damage == "collection":
            metadata["collectionsSha256"]["catalog_tasks"] = "0" * 64
        elif damage == "count":
            metadata["counts"]["revisions"] += 1
        elif damage == "unresolved":
            metadata["counts"]["unresolvedAssetReferences"] = 1
        elif damage == "link":
            _record(connection, "catalog_links", "l-2", lambda row: row.update(asset_id="missing"))
        _refresh(connection, metadata, collections=damage != "collection", manifest=damage != "manifest")
    with pytest.raises(BridgeError):
        _validate(path)


def test_v1_never_accepts_v2_warning_exception(seed_factory):
    bundle, _ = seed_factory()
    path = bundle / "catalog" / "catalog.sqlite3"
    _promote(path)
    with closing(sqlite3.connect(path)) as connection:
        metadata = _metadata(connection)
        metadata.update(format="kontur-catalog-seed-v1", schemaVersion=1)
        del metadata["offlineReady"]
        del metadata["sourceSeedSha256"]
        _refresh(connection, metadata)
    with pytest.raises(BridgeError):
        _validate(path)


def test_v2_bootstrap_preserves_warnings_and_original_selection(seed_factory, offline_factory):
    from sqlalchemy import select

    from app.models import CatalogEntry, SourceSyncJob, TaskRevision

    bundle, _ = seed_factory()
    _promote(bundle / "catalog" / "catalog.sqlite3")
    application = offline_factory(bundle)
    with application.app.state.session_factory() as session:
        assert set(session.scalars(select(CatalogEntry.revision_id))) == {"r-1", "r-2"}
        assert session.scalar(select(SourceSyncJob.parser_version)) == "kontur-catalog-seed-v2"
        for revision in session.scalars(select(TaskRevision)):
            assert len(revision.source_metadata["kege_unavailable_resources"]) == 2

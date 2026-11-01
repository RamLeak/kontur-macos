"""Actual producer/consumer contract on sanitized synthetic source records."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import socket
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest
from test_offline_catalog_v2 import _encode, _metadata, _record, _refresh, _warning

from offline_catalog import open_seed, validate_seed

ROOT = Path(__file__).resolve().parents[3]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


downloader = _load("_pipeline_downloader", "ops/catalog-assets-offline-20261030/download_assets.py")
finalizer = _load("_pipeline_finalizer", "ops/catalog-seed-finalizer-20261030/finalize_catalog.py")


@pytest.mark.parametrize("with_warning", [False, True])
@pytest.mark.parametrize("missing_sentinel", [False, True])
def test_actual_snapshot_finalizer_output_is_admitted_by_runtime(
    seed_factory, tmp_path, monkeypatch, with_warning, missing_sentinel
):
    bundle, _receipt = seed_factory()
    source = bundle / "catalog" / "catalog.sqlite3"
    with closing(sqlite3.connect(source)) as connection:
        metadata = _metadata(connection)
        assets = {key: json.loads(raw) for key, raw in connection.execute("SELECT id,record_json FROM catalog_assets")}
        original_bytes = dict(connection.execute("SELECT asset_id,bytes FROM catalog_asset_bytes"))
        assert assets and original_bytes
        revision_id = connection.execute("SELECT id FROM catalog_revisions ORDER BY id LIMIT 1").fetchone()[0]
        if with_warning:
            _record(
                connection,
                "catalog_revisions",
                revision_id,
                lambda row: row["source_metadata"].update(
                    kege_unavailable_resources=[
                        {"reason": "origin_not_admitted", "url": "https://example.invalid/synthetic.png"},
                        {"reason": "origin_not_admitted", "url": "https://example.invalid/synthetic.pdf"},
                    ]
                ),
            )
        issues = [_warning(revision_id)] if with_warning else []
        counts = {"upstream_resource_unavailable": 1} if with_warning else {}
        missing_id = sorted(original_bytes)[0] if missing_sentinel else None
        for asset_id, asset in assets.items():
            # Give independent fixture records distinct public-shaped URLs.
            asset["canonical_url"] = f"https://kompege.ru/files/{asset_id}.txt"
            if asset_id == missing_id:
                sentinel = hashlib.sha256(f"metadata:{asset['canonical_url']}:{asset['filename']}".encode()).hexdigest()
                asset.update(
                    expected_content_hash=sentinel,
                    content_hash=sentinel,
                    size_bytes=0,
                    hash_verified=False,
                    detected_type=None,
                    cache_state="metadata_only",
                    fetched_at=None,
                )
                connection.execute("DELETE FROM catalog_asset_bytes WHERE asset_id=?", (asset_id,))
                metadata["counts"]["assetBytesRows"] -= 1
                metadata["counts"]["assetBytesTotal"] -= len(original_bytes[asset_id])
                metadata["counts"]["unavailableAssets"] += 1
                counts["asset_not_ready"] = 1
                issues.append({"code": "asset_not_ready", "assetId": asset_id})
            connection.execute("UPDATE catalog_assets SET record_json=? WHERE id=?", (_encode(asset), asset_id))
        metadata.update(complete=not bool(issues), issues=issues, issueCounts=counts)
        _refresh(connection, metadata)
        preserved = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in ("catalog_tasks", "catalog_revisions", "catalog_links", "catalog_selection")
        }
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    requests = []

    def no_network(*_args, **_kwargs):
        raise AssertionError("Real network is forbidden in this pipeline test")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)

    def transport(url, _headers, maximum, _timeout):
        requests.append(url)
        asset_id = next(key for key, row in assets.items() if row["canonical_url"] == url)
        payload = original_bytes[asset_id]
        assert len(payload) <= maximum
        return downloader.SourceResponse(200, url, {"Content-Type": assets[asset_id]["media_type"]}, payload)

    snapshot_root = tmp_path / "snapshot"
    snapshot = downloader.run_snapshot(
        source, snapshot_root, transport=transport, resolver=lambda *_args: ["93.184.216.34"], min_interval_seconds=0
    )
    assert snapshot["counts"]["unavailable"] == 0
    assert len(requests) == int(missing_sentinel)
    assert len(snapshot["legacyHashSentinelNormalization"]["entries"]) == int(missing_sentinel)
    derived = tmp_path / "catalog-v2.sqlite3"
    finalizer.derive_catalog(
        source, snapshot_root / "manifest.json", snapshot_root, derived, expected_source_sha256=before
    )
    with closing(open_seed(derived)) as connection:
        validate_seed(connection)
        result = _metadata(connection)
        assert result["format"] == "kontur-catalog-seed-v2"
        assert result["schemaVersion"] == 2
        assert result["offlineReady"] is True
        assert result["complete"] is (not with_warning)
        assert result["sourceSeedSha256"] == before
        assert result["issues"] == ([_warning(revision_id)] if with_warning else [])
        assert result["issueCounts"] == ({"upstream_resource_unavailable": 1} if with_warning else {})
        for table, rows in preserved.items():
            assert connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() == rows
        assert dict(connection.execute("SELECT asset_id,bytes FROM catalog_asset_bytes")) == original_bytes
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before

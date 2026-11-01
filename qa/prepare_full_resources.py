"""Pin the prepared Python, current backend and verified genuine seed together.

Run with the prepared CPython and explicit public artifact/source paths. This
only writes generated resources and their manifest inside the offline project.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESOURCES = ROOT / "resources"
SOURCE_SHA256 = "a18b55186b42e6d11142349f92bbb83ff34aff356d2758067d9f26e53392b051"
PYTHON_COMPONENT_SHA256 = "f838a940feb1e6bb075b94c04e1d842c0c8b6508292cb04e3e01c850fa7fde1a"
RECOVERY_INPUT_SHA256 = "f8186e68ef396d4df762e2aad3f87bd1fdb0010e7221643fd1cbf9e74dc2886d"
SNAPSHOT_MANIFEST_SHA256 = "8dd72b992e7bceef9554eb03d15e71529acf7137d0495ca4d821b2ed73943114"
OFFICIAL_FETCH_PROOF_SHA256 = "7bf2c3b0c35d251e7f9a9354af93594b9f96ef8bfb7b6f5c64cafa2e1abf2fb9"
BACKEND_PATHS = frozenset((
    "app/__init__.py", "app/ai_gateway.py", "app/api_common.py", "app/api.py", "app/asset_fetch.py",
    "app/assets.py", "app/auth.py", "app/catalog_policy.py", "app/catalog_selection.py", "app/codex_gateway.py",
    "app/config.py", "app/db.py", "app/device_assets.py", "app/events.py", "app/exam_scoring.py", "app/exams.py",
    "app/inline_images.py", "app/jobs.py", "app/kege_current_bank.py", "app/kege_embedded_cache.py", "app/learning.py",
    "app/local_bank.py", "app/main.py", "app/math_text.py", "app/models.py", "app/official_blueprints.py",
    "app/recent_catalog.py", "app/runner_gateway.py", "app/schema_version.py", "app/schemas.py", "app/security.py",
    "app/source_catalog.py", "app/source_discovery.py", "app/source_identity.py", "app/source_sync.py",
    "app/source_transport.py", "app/spreadsheet_text.py", "app/sync_records.py", "app/task_file_inputs.py",
    "app/task_pagination.py", "app/util.py", "app/variants.py", "local_codex.py", "offline_app.py", "offline_bridge.py",
    "offline_catalog.py", "offline_protocol.py", "offline_state.py", "offline_transfer.py",
    "offline_ai.py", "offline_ai_adapter.py", "offline_ai_gateway.py", "offline_ai_opencode.py",
    "offline_ai_providers.py", "offline_ai_secrets.py", "offline_ai_settings.py",
))


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def plain_files(root: Path) -> dict[str, Path]:
    assert root.is_dir() and not root.is_symlink() and not root.is_junction()
    result = {}
    for path in root.rglob("*"):
        assert not path.is_symlink() and not path.is_junction(), "Resource links are forbidden"
        if path.is_file():
            result[path.relative_to(root).as_posix()] = path
    return result


def publish_json(path: Path, value: dict) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".resource-manifest-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-artifact-manifest", type=Path, required=True)
    parser.add_argument("--source-seed", type=Path, required=True)
    args = parser.parse_args()
    if not (sys.flags.isolated and sys.dont_write_bytecode and sys.flags.optimize == 0):
        raise RuntimeError("Use prepared python.exe -I -B without optimization")
    assert sys.version_info[:3] == (3, 12, 14)
    assert Path(sys.executable).resolve() == (RESOURCES / "python/python.exe").resolve()
    component_path = RESOURCES / "python/runtime-manifest.json"
    assert digest(component_path) == PYTHON_COMPONENT_SHA256
    component = json.loads(component_path.read_text(encoding="utf-8-sig"))
    assert digest(args.python_artifact_manifest) == component["sourceArtifactManifestSha256"]
    manifest = json.loads(args.python_artifact_manifest.read_text(encoding="utf-8-sig"))
    assert manifest["python"] == component["python"]
    assert manifest["distributions"] == component["distributions"]
    assert manifest["directProductionRequirements"] == component["directProductionRequirements"]
    expected_python = {"runtime-manifest.json"}
    for entry in component["files"]:
        relative = entry["path"]
        path = RESOURCES / "python" / relative
        path.resolve().relative_to((RESOURCES / "python").resolve())
        assert path.stat().st_size == entry["size"] and digest(path) == entry["sha256"]
        expected_python.add(relative)
    assert set(plain_files(RESOURCES / "python")) == expected_python

    assert digest(args.source_seed) == SOURCE_SHA256
    sys.path.insert(0, str(ROOT / "full/backend"))
    from offline_catalog import open_seed, validate_seed
    sys.path.insert(0, str(ROOT / "ops/catalog-assets-offline-20261030"))
    from download_assets import _legacy_hash_normalization

    catalog_path = RESOURCES / "catalog/catalog.sqlite3"
    validated_catalog_sha256 = digest(catalog_path)
    with closing(open_seed(catalog_path)) as derived:
        metadata, _collections = validate_seed(derived)
        assert metadata["schemaVersion"] == 2 and metadata["offlineReady"] is True
        assert metadata["sourceSeedSha256"] == SOURCE_SHA256
        with closing(sqlite3.connect(
            f"{args.source_seed.resolve().as_uri()}?mode=ro&immutable=1", uri=True,
        )) as source:
            # Domain rows are immutable even when attachment cache fields change.
            for table, key in (("catalog_tasks", "id"), ("catalog_revisions", "id"),
                               ("catalog_links", "id"), ("catalog_selection", "revision_id")):
                with closing(source.execute(f"SELECT * FROM {table} ORDER BY {key}")) as original, closing(
                    derived.execute(f"SELECT * FROM {table} ORDER BY {key}")
                ) as current:
                    assert all(left == right for left, right in zip(original, current, strict=True)), table
            source_metadata = {key: json.loads(value) for key, value in source.execute(
                "SELECT key,value FROM catalog_metadata"
            )}
            source_warnings = {issue["revisionId"] for issue in source_metadata["issues"]
                               if issue["code"] == "upstream_resource_unavailable"}
            assert {issue["revisionId"] for issue in metadata["issues"]} == source_warnings
            with closing(source.execute("SELECT id,record_json FROM catalog_assets ORDER BY id")) as rows:
                original_assets = {identifier: json.loads(record) for identifier, record in rows}
            current_assets = {record["id"]: record for record in _collections["catalog_assets"]}
            assert set(original_assets) == set(current_assets)
            cache_fields = {"content_hash", "expected_content_hash", "size_bytes", "cache_state",
                            "hash_verified", "detected_type", "fetched_at"}
            for identifier, original in original_assets.items():
                current = current_assets[identifier]
                assert {k: v for k, v in original.items() if k not in cache_fields} == {
                    k: v for k, v in current.items() if k not in cache_fields
                }
                if original.get("expected_content_hash") != current.get("expected_content_hash"):
                    assert current.get("expected_content_hash") is None
                    assert _legacy_hash_normalization(original) is not None
    assert digest(args.source_seed) == SOURCE_SHA256
    del _collections

    # Ship the owner's single, explicit official-file replacement with its
    # provenance. It does not establish equality to the deleted historical body.
    recovery_root = ROOT / "artifacts/catalog-official-recovery-approved-20261030"
    recovery_path = recovery_root / "recovery-input-proof.json"
    snapshot_path = recovery_root / "manifest.json"
    fetch_path = ROOT / "artifacts/fipi2027-provenance/proof.json"
    assert digest(recovery_path) == RECOVERY_INPUT_SHA256
    assert digest(snapshot_path) == SNAPSHOT_MANIFEST_SHA256
    assert digest(fetch_path) == OFFICIAL_FETCH_PROOF_SHA256
    recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
    assert recovery["ownerApproved"] is True and recovery["historicalByteIdentity"] == "unknown"
    assert recovery["sourceSeedSha256"] == SOURCE_SHA256
    assert recovery["originalLocatorSuccessfullyFetched"] is False and recovery["originalHttpStatus"] == 404
    original_id = recovery["originalAssetId"]
    current_id = recovery["currentAssetId"]
    assert recovery["originalAssetFingerprint"] == original_assets[original_id]
    assert original_assets[original_id].get("expected_content_hash") is None
    assert original_assets[current_id]["expected_content_hash"] == recovery["memberSha256"]
    assert current_assets[original_id]["content_hash"] == current_assets[current_id]["content_hash"] == recovery["memberSha256"]
    assert current_assets[original_id]["size_bytes"] == current_assets[current_id]["size_bytes"] == recovery["memberBytes"]
    assert metadata["complete"] is False and metadata["issueCounts"] == {"upstream_resource_unavailable": 30}
    provenance = {
        "format": "kontur-catalog-provenance-v1",
        "sourceSeedSha256": SOURCE_SHA256,
        "catalogSha256": validated_catalog_sha256,
        "catalogManifestSha256": metadata["manifestSha256"],
        "snapshotManifestSha256": SNAPSHOT_MANIFEST_SHA256,
        "recoveryInputProofSha256": RECOVERY_INPUT_SHA256,
        "officialFetchProofSha256": OFFICIAL_FETCH_PROOF_SHA256,
        "recovery": recovery,
        "officialFetchProof": json.loads(fetch_path.read_text(encoding="utf-8")),
        "sourceWarningRevisionIds": sorted(source_warnings),
        "counts": metadata["counts"],
        "sourceComplete": False,
        "offlineReady": True,
    }
    publish_json(RESOURCES / "catalog/recovery-provenance.json", provenance)

    backend_source = ROOT / "full/backend"
    selected = {path.relative_to(backend_source).as_posix(): path for path in (backend_source / "app").rglob("*.py")}
    selected.update({path.name: path for path in backend_source.glob("offline*.py")})
    selected["local_codex.py"] = backend_source / "local_codex.py"
    assert len(BACKEND_PATHS) == 56 and set(selected) == BACKEND_PATHS, "Backend production allowlist changed"
    backend_root = RESOURCES / "backend"
    backend_root.mkdir(exist_ok=True)
    assert set(plain_files(backend_root)) <= set(selected), "Unexpected backend resource files"
    for relative, source in selected.items():
        assert not source.is_symlink() and not source.is_junction()
        for parent in source.parents:
            assert not parent.is_symlink() and not parent.is_junction()
            if parent == backend_source:
                break
        target = backend_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        assert digest(target) == digest(source)
    assert set(plain_files(backend_root)) == set(selected)
    assert set(plain_files(RESOURCES / "catalog")) == {"catalog.sqlite3", "recovery-provenance.json"}
    expected_resources = {
        *("python/" + relative for relative in expected_python),
        *("backend/" + relative for relative in selected),
        "catalog/catalog.sqlite3",
        "catalog/recovery-provenance.json",
    }
    assert set(plain_files(RESOURCES)) == expected_resources, "Unexpected resource files"
    publish_json(ROOT / "artifacts/backend-component-manifest.json", {
        "format": "kontur-backend-component-manifest-v1", "componentOnly": True, "catalogReady": True,
        "files": [{"path": "backend/" + relative, "size": path.stat().st_size,
                   "sha256": digest(path), "source": "owned-backend-source"}
                  for relative, path in sorted(plain_files(backend_root).items())],
    })

    manifest["backend"] = {
        "status": "prepared", "source": "current full/backend production Python sources",
        "entrypoint": "resources/backend/offline_bridge.py", "testsExcluded": True,
    }
    manifest["files"] = [{
        "path": "resources/" + relative, "size": path.stat().st_size, "sha256": digest(path),
        "source": "verified-python-component" if relative.startswith("python/") else
                  "current-backend-source" if relative.startswith("backend/") else
                  "verified-derived-catalog-v2; sourceSeedSha256=" + SOURCE_SHA256 +
                  "; manifestSha256=" + metadata["manifestSha256"],
    } for relative, path in sorted(plain_files(RESOURCES).items())]
    catalog_entry = next(entry for entry in manifest["files"] if entry["path"] == "resources/catalog/catalog.sqlite3")
    assert catalog_entry["sha256"] == validated_catalog_sha256 == digest(catalog_path)
    assert digest(args.source_seed) == SOURCE_SHA256
    output_path = ROOT / "full-local-resources-manifest.json"
    publish_json(output_path, manifest)
    print(json.dumps({"files": len(manifest["files"]), "manifestSha256": digest(output_path),
                      "catalog": metadata["counts"], "offlineReady": True, "complete": metadata["complete"]}))


if __name__ == "__main__":
    main()

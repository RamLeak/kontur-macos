"""Independent synthetic checks for the disposable offline bootstrap cache."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from offline_protocol import BridgeError


def _cache(profile: Path) -> Path:
    return profile / "catalog-bootstrap-cache.json"


def _first_asset_digest(bundle: Path) -> str:
    with sqlite3.connect(bundle / "catalog" / "catalog.sqlite3") as connection:
        raw = connection.execute("SELECT record_json FROM catalog_assets ORDER BY id LIMIT 1").fetchone()[0]
    return json.loads(raw)["content_hash"]


def _start_and_close(offline_factory, bundle: Path, profile: Path):
    application = offline_factory(bundle, profile)
    application.close()


def _start_expect_error(bundle: Path, profile: Path):
    # The shared factory only tracks successful starts.  Close a deliberately
    # failed startup here as well, so its half-created lifespan/engine cannot
    # retain a Windows file handle while the synthetic fixture is removed.
    from offline_app import OfflineApplication

    application = OfflineApplication(
        profile,
        bundle,
        lambda operation, _payload: {
            "gatewayProtocol": 1,
            "state": "unavailable",
            "loginState": "unknown",
            "manualModelSupported": False,
            "manualModels": [],
        },
    )
    try:
        application.start()
    except BaseException:
        application.close()
        raise
    application.close()
    raise AssertionError("synthetic startup unexpectedly succeeded")


def _start_maybe(bundle: Path, profile: Path):
    from offline_app import OfflineApplication

    application = OfflineApplication(
        profile,
        bundle,
        lambda operation, _payload: {
            "gatewayProtocol": 1,
            "state": "unavailable",
            "loginState": "unknown",
            "manualModelSupported": False,
            "manualModels": [],
        },
    )
    try:
        application.start()
    except BaseException:
        application.close()
        raise
    return application


def test_cold_bootstrap_writes_seed_bound_cache_and_malformed_cache_falls_back(
    seed_factory, offline_factory, tmp_path
):
    bundle, _receipt = seed_factory()
    profile = tmp_path / "profile"
    _start_and_close(offline_factory, bundle, profile)

    path = _cache(profile)
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["version"] == 1
    assert value["seedPath"] == str((bundle / "catalog" / "catalog.sqlite3").resolve())
    assert len(value["seedStamp"]) == 5
    assert value["assets"]

    path.write_text("{not-json", encoding="utf-8")
    warm = offline_factory(bundle, profile)
    assert warm.request("GET", "/api/v1/tasks?limit=1")["status"] == 200
    warm.close()
    assert json.loads(path.read_text(encoding="utf-8"))["manifestSha256"] == value["manifestSha256"]


def test_forged_cache_manifest_fails_closed_instead_of_admitting_unverified_bootstrap(
    seed_factory, offline_factory, tmp_path
):
    bundle, _receipt = seed_factory()
    profile = tmp_path / "profile"
    _start_and_close(offline_factory, bundle, profile)
    path = _cache(profile)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["manifestSha256"] = "0" * 64
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(BridgeError) as error:
        _start_expect_error(bundle, profile)
    assert error.value.code == "catalog_invalid"


def test_changed_seed_cannot_take_the_warm_shortcut(seed_factory, offline_factory, tmp_path, monkeypatch):
    bundle, _receipt = seed_factory()
    profile = tmp_path / "profile"
    _start_and_close(offline_factory, bundle, profile)

    seed = bundle / "catalog" / "catalog.sqlite3"
    stamp = seed.stat()
    os.utime(seed, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 2_000_000_000))
    import offline_catalog

    calls = []
    real_open_seed = offline_catalog.open_seed

    def record_open_seed(path, *, verify_integrity=True):
        calls.append(verify_integrity)
        return real_open_seed(path, verify_integrity=verify_integrity)

    monkeypatch.setattr(offline_catalog, "open_seed", record_open_seed)
    warm = offline_factory(bundle, profile)
    warm.close()
    assert calls == [True]


def test_changed_existing_asset_is_rehashed_before_warm_bootstrap(seed_factory, offline_factory, tmp_path):
    bundle, _receipt = seed_factory()
    profile = tmp_path / "profile"
    _start_and_close(offline_factory, bundle, profile)

    digest = _first_asset_digest(bundle)
    target = profile / "assets" / digest[:2] / digest
    original = target.read_bytes()
    target.write_bytes(bytes((original[0] ^ 0xFF,)) + original[1:])
    with pytest.raises(BridgeError) as error:
        _start_expect_error(bundle, profile)
    assert error.value.code == "catalog_invalid"


def test_asset_replacement_after_hash_read_is_not_committed_as_verified(
    seed_factory, offline_factory, tmp_path, monkeypatch
):
    bundle, _receipt = seed_factory()
    profile = tmp_path / "profile"
    _start_and_close(offline_factory, bundle, profile)

    digest = _first_asset_digest(bundle)
    target = profile / "assets" / digest[:2] / digest
    original = target.read_bytes()
    stat = target.stat()
    # NTFS rounds timestamps to 100 ns; a +1 ns write can leave the exact
    # cached stamp unchanged and never reach the simulated replacement.
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert target.stat().st_mtime_ns != stat.st_mtime_ns
    real_read_bytes = Path.read_bytes
    replaced = False

    def replace_after_read(path: Path) -> bytes:
        nonlocal replaced
        data = real_read_bytes(path)
        if path.resolve() == target.resolve() and not replaced:
            replaced = True
            path.write_bytes(b"X" * len(data))
        return data

    monkeypatch.setattr(Path, "read_bytes", replace_after_read)
    try:
        application = _start_maybe(bundle, profile)
    except BridgeError as error:
        assert error.code == "catalog_invalid"
    else:
        application.close()
        pytest.fail("bootstrap completed after the verified asset was replaced during its hash read")
    assert replaced
    assert target.read_bytes() != original

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from backend.app import kege_embedded_cache
from backend.app.assets import AssetStore
from backend.app.config import Settings
from backend.app.db import init_schema, make_engine, make_session_factory
from backend.app.kege_current_bank import apply_verified_kege_current_bank, validate_kege_current_bank
from backend.app.kege_embedded_cache import cache_verified_embedded_images
from backend.app.models import Asset, TaskRevision, User
from backend.app.source_catalog import SourceError
from backend.tests.test_kege_current_bank import _PNG_B64, _PNG_BYTES, _envelope, _task
from sqlalchemy import func, select


def seed(tmp_path: Path, *, images: int = 1, remote: bool = False):
    settings = Settings(database_url=f"sqlite+pysqlite:///{(tmp_path / 'TEST_ONLY.sqlite').as_posix()}",
                        asset_root=tmp_path / "assets", source_mode="manual", source_network_enabled=False,
                        source_permission_status="private_authorized", owner_id="TEST_ONLY", testing=True)
    engine = make_engine(settings.database_url)
    init_schema(engine)
    factory = make_session_factory(engine)
    image = {"url": f"data:image/png;base64,{_PNG_B64}", "embedded": True,
             "bytes": len(_PNG_BYTES), "sha256": hashlib.sha256(_PNG_BYTES).hexdigest(),
             "status": "ok", "local_path": "NEVER-READ-source-path.png"}
    resources = [image.copy() for _ in range(images)]
    if remote:
        resources.append({"url": "https://kompege.ru/files/TEST_ONLY.png", "bytes": 7,
                          "sha256": hashlib.sha256(b"TEST_ONLY").hexdigest(),
                          "content_type": "image/png", "status": "ok"})
    bundle = validate_kege_current_bank(_envelope([_task(images=resources)]))
    with factory() as session:
        with session.begin():
            session.add(User(id="TEST_ONLY"))
        apply_verified_kege_current_bank(session, bundle, owner_id="TEST_ONLY", settings=settings,
                                         rights_status="private_authorized", target_exam_year=2027)
    return settings, factory, bundle


def test_embedded_raster_promotes_without_new_identity_and_replay_is_read_only(tmp_path: Path) -> None:
    settings, factory, bundle = seed(tmp_path)
    with factory() as session:
        asset = session.scalar(select(Asset))
        assert asset is not None
        identity, url = asset.id, asset.canonical_url
        assert asset.cache_state == "metadata_only" and not asset.hash_verified
        session.commit()
        first = cache_verified_embedded_images(session, bundle, settings=settings)
        assert first["promoted"] == 1 and first["already_ready"] == 0
        asset = session.get(Asset, identity)
        assert asset is not None and asset.canonical_url == url
        assert AssetStore(settings).read(asset) == _PNG_BYTES
        assert session.scalar(select(func.count()).select_from(TaskRevision)) == 1
        session.commit()
        replay = cache_verified_embedded_images(session, bundle, settings=settings)
        assert replay["promoted"] == 0 and replay["already_ready"] == 1
        assert session.scalar(select(func.count()).select_from(Asset)) == 1


def test_tampered_embedded_bytes_cannot_promote_admitted_asset(tmp_path: Path) -> None:
    settings, factory, bundle = seed(tmp_path)
    damaged = replace(bundle, records=(replace(bundle.records[0], embedded_asset_bytes=(b"TEST_ONLY wrong",)),))
    with factory() as session:
        with pytest.raises(SourceError) as error_asset_hash_mismatch:
            cache_verified_embedded_images(session, damaged, settings=settings)
        assert error_asset_hash_mismatch.value.code == "ASSET_HASH_MISMATCH"
        asset = session.scalar(select(Asset))
        assert asset is not None and asset.cache_state == "metadata_only"
        assert not settings.asset_root.exists()


def test_poisoned_existing_cache_file_is_preserved_and_not_marked_ready(tmp_path: Path) -> None:
    settings, factory, bundle = seed(tmp_path)
    digest = hashlib.sha256(_PNG_BYTES).hexdigest()
    destination = settings.asset_root / digest[:2] / f"{digest}.png"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"TEST_ONLY corrupt")
    with factory() as session:
        with pytest.raises(SourceError) as error_asset_cache_corrupt:
            cache_verified_embedded_images(session, bundle, settings=settings)
        assert error_asset_cache_corrupt.value.code == "ASSET_CACHE_CORRUPT"
        asset = session.scalar(select(Asset))
        assert asset is not None and asset.cache_state == "metadata_only"
    assert destination.read_bytes() == b"TEST_ONLY corrupt"


def test_quota_is_checked_before_cache_creation(tmp_path: Path) -> None:
    settings, factory, bundle = seed(tmp_path)
    with factory() as session:
        with pytest.raises(SourceError) as error_asset_cache_quota:
            cache_verified_embedded_images(session, bundle, settings=replace(settings, max_asset_cache_bytes=1))
        assert error_asset_cache_quota.value.code == "ASSET_CACHE_QUOTA"
        assert session.scalar(select(Asset)).cache_state == "metadata_only"
    assert not settings.asset_root.exists()


def test_unlinked_metadata_cannot_receive_embedded_bytes(tmp_path: Path) -> None:
    settings, factory, bundle = seed(tmp_path)
    record = bundle.records[0]
    changed = replace(bundle, records=(replace(record, source_id="9999"),))
    with factory() as session:
        with pytest.raises(SourceError) as error_asset_not_admitted:
            cache_verified_embedded_images(session, changed, settings=settings)
        assert error_asset_not_admitted.value.code == "ASSET_NOT_ADMITTED"
        assert session.scalar(select(Asset)).cache_state == "metadata_only"


def test_second_promotion_failure_rolls_back_all_database_readiness(tmp_path: Path, monkeypatch) -> None:
    settings, factory, bundle = seed(tmp_path, images=2)
    original = kege_embedded_cache._promote_metadata_asset
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SourceError("TEST_ONLY_FAILURE", "TEST_ONLY rollback")
        return original(*args, **kwargs)

    monkeypatch.setattr(kege_embedded_cache, "_promote_metadata_asset", fail_second)
    with factory() as session:
        with pytest.raises(SourceError) as error_test_only_failure:
            cache_verified_embedded_images(session, bundle, settings=settings)
        assert error_test_only_failure.value.code == "TEST_ONLY_FAILURE"
    with factory() as session:
        assets = list(session.scalars(select(Asset)))
        assert len(assets) == 2
        assert all(asset.cache_state == "metadata_only" and not asset.hash_verified for asset in assets)


@pytest.mark.parametrize("images", [0, 1])
def test_nonembedded_slots_remain_metadata_only_and_do_not_block_inline_cache(tmp_path: Path, images: int) -> None:
    settings, factory, bundle = seed(tmp_path, images=images, remote=True)
    with factory() as session:
        result = cache_verified_embedded_images(session, bundle, settings=settings)
        assert result["promoted"] == images
        assets = list(session.scalars(select(Asset)))
        assert sum(asset.cache_state == "ready" for asset in assets) == images
        remote = [asset for asset in assets if asset.canonical_url.endswith("TEST_ONLY.png")]
        assert len(remote) == 1 and remote[0].cache_state == "metadata_only"
        assert remote[0].storage_key is None and not remote[0].hash_verified


def test_unchanged_prior_parser_revision_can_reuse_verified_embedded_bytes(tmp_path: Path) -> None:
    settings, factory, bundle = seed(tmp_path)
    with factory() as session:
        revision = session.scalar(select(TaskRevision))
        assert revision is not None
        revision.parser_version = "kege-current-bank-v2.0"
        session.commit()
        assert cache_verified_embedded_images(session, bundle, settings=settings)["promoted"] == 1
        revision.statement = "TEST_ONLY unrelated stale statement"
        session.commit()
        with pytest.raises(SourceError, match="snapshot content"):
            cache_verified_embedded_images(session, bundle, settings=settings)

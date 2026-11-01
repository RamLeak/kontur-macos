"""Cache verified embedded rasters after metadata admission, with no network."""
from __future__ import annotations

from html import escape
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .assets import AssetStore
from .config import Settings
from .inline_images import parse_inline_image
from .kege_current_bank import KEGE_CURRENT_BANK_PARSER_VERSION, VerifiedKegeCurrentBank
from .models import Asset, Task, TaskAsset, TaskRevision
from .source_catalog import FixtureAsset, SourceError, _promote_metadata_asset, sanitize_statement
from .util import sha256_bytes


def cache_verified_embedded_images(
    session: Session, bundle: VerifiedKegeCurrentBank, *, settings: Settings,
) -> dict[str, int]:
    """Promote only exact, already admitted assets; retain IDs and revisions.

    The local-file admission policy remains metadata-only. This separate local
    cache step cannot create assets, import questions or read source paths.
    Database changes are atomic. Content-addressed orphan files after a failed
    transaction follow the existing AssetStore behavior and carry no readiness.
    """
    if not isinstance(bundle, VerifiedKegeCurrentBank) or not bundle.report.ok:
        raise SourceError("BUNDLE_NOT_VERIFIED", "a verified snapshot is required")
    if settings.source_mode != "manual" or settings.source_network_enabled:
        raise SourceError("SOURCE_DISABLED", "local cache requires manual mode without network")
    store = AssetStore(settings)
    transaction = session.begin_nested() if session.in_transaction() else session.begin()
    promoted = 0
    already_ready = 0
    try:
        plan: dict[str, tuple[Asset, FixtureAsset]] = {}
        for record in bundle.records:
            if len(record.assets) != len(record.embedded_asset_bytes):
                raise SourceError("ASSET_MANIFEST_INVALID", "embedded slots do not match asset metadata")
            embedded = []
            for metadata, data in zip(record.assets, record.embedded_asset_bytes, strict=True):
                locator = parse_inline_image(metadata["url"])
                if locator is None:
                    if data != b"":
                        raise SourceError("ASSET_MANIFEST_INVALID", "external metadata cannot carry embedded bytes")
                    continue
                if not isinstance(data, bytes) or not data or sha256_bytes(data) != locator.digest:
                    raise SourceError("ASSET_HASH_MISMATCH", "embedded bytes do not match their exact locator")
                embedded.append((metadata, data))
            if not embedded:
                continue
            data_by_hash = {sha256_bytes(data): data for _metadata, data in embedded}
            if any(len(data) > settings.max_asset_bytes for data in data_by_hash.values()):
                raise SourceError("ASSET_TOO_LARGE", "embedded raster exceeds local byte limit")
            revision = session.scalar(select(TaskRevision).join(Task, Task.id == TaskRevision.task_id).where(
                Task.source_name == "kompege", Task.external_id == record.source_id,
                Task.current_revision_id == TaskRevision.id, TaskRevision.source_id == record.source_id,
                TaskRevision.line_number == record.line_number,
                TaskRevision.parser_version.in_([KEGE_CURRENT_BANK_PARSER_VERSION, "kege-current-bank-v2.0"]),
            ))
            if revision is None or revision.rights_status not in {"private_authorized", "public_authorized"}:
                raise SourceError("ASSET_NOT_ADMITTED", "current authorized snapshot revision is required")
            expected_statement, _raw_hash = sanitize_statement(escape(record.statement), settings.max_message_bytes)
            # An unchanged revision may retain the prior parser version when
            # admission reuses its exact content. Never accept a different row
            # merely because its source ID and asset digest happen to match.
            if revision.statement != expected_statement or revision.source_metadata != record.source_metadata:
                raise SourceError("ASSET_NOT_ADMITTED", "snapshot content does not match the current admission")
            linked = list(session.scalars(select(Asset).join(TaskAsset, TaskAsset.asset_id == Asset.id).where(
                TaskAsset.revision_id == revision.id,
            )))
            matched_hashes: set[str] = set()
            for metadata in record.assets:
                locator = parse_inline_image(metadata["url"])
                if locator is None:
                    continue
                digest = locator.digest
                resolved_data = data_by_hash.get(digest)
                if (resolved_data is None or metadata.get("expected_hash") != digest
                        or metadata.get("content_hash") != digest):
                    raise SourceError("ASSET_HASH_MISMATCH", "embedded raster does not match its pinned locator")
                candidates = [asset for asset in linked if asset.canonical_url == metadata["url"]
                              and asset.expected_content_hash == digest
                              and asset.filename == metadata["filename"]]
                if len(candidates) != 1:
                    raise SourceError("ASSET_NOT_ADMITTED", "exact linked image metadata is required")
                asset = candidates[0]
                if asset.rights_status not in {"private_authorized", "public_authorized"}:
                    raise SourceError("ASSET_NOT_ADMITTED", "image rights are not authorized")
                if asset.media_type != locator.media_type or metadata["media_type"] != locator.media_type:
                    raise SourceError("ASSET_REJECTED", "raster type does not match admitted metadata")
                matched_hashes.add(digest)
                if asset.id not in plan:
                    plan[asset.id] = (asset, FixtureAsset(
                        canonical_url=asset.canonical_url, filename=asset.filename,
                        media_type=asset.media_type, data=resolved_data, content_hash=digest,
                        role="image", expected_hash=digest,
                    ))
            if matched_hashes != set(data_by_hash):
                raise SourceError("ASSET_NOT_ADMITTED", "unmatched embedded bytes cannot enter the cache")
        # Validate every existing file and quota before marking any row ready.
        ready_bytes = int(session.scalar(select(func.coalesce(func.sum(Asset.size_bytes), 0)).where(
            Asset.cache_state == "ready",
        )) or 0)
        for asset, fixture in plan.values():
            assert fixture.data is not None
            if asset.cache_state == "ready":
                if not asset.hash_verified or store.read(asset) != fixture.data:
                    raise SourceError("ASSET_CACHE_CORRUPT", "ready image failed integrity validation")
                already_ready += 1
                continue
            if asset.cache_state != "metadata_only":
                raise SourceError("ASSET_CACHE_STATE", "image is not an admitted metadata placeholder")
            key = f"{fixture.content_hash[:2]}/{fixture.content_hash}{Path(asset.filename).suffix.lower()[:16]}"
            destination = (store.root / key).resolve()
            if not destination.is_relative_to(store.root):
                raise SourceError("ASSET_CACHE_PATH", "image cache path escapes its configured root")
            if destination.exists() and (not destination.is_file() or destination.read_bytes() != fixture.data):
                raise SourceError("ASSET_CACHE_CORRUPT", "existing image cache file failed integrity validation")
            ready_bytes += len(fixture.data)
        if ready_bytes > settings.max_asset_cache_bytes:
            raise SourceError("ASSET_CACHE_QUOTA", "embedded rasters exceed the configured cache quota")
        for asset, fixture in plan.values():
            if asset.cache_state == "ready":
                continue
            _promote_metadata_asset(session, store, asset, fixture, max_cache_bytes=settings.max_asset_cache_bytes)
            # AssetStore preserves content-addressed files; verify actual bytes
            # again before the database transaction can commit.
            if store.read(asset) != fixture.data:
                raise SourceError("ASSET_CACHE_CORRUPT", "stored image bytes failed integrity validation")
            promoted += 1
        transaction.commit()
    except Exception:
        transaction.rollback()
        raise
    return {"promoted": promoted, "already_ready": already_ready, "embedded_bytes": bundle.report.embedded_bytes}

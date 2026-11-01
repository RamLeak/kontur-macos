"""Explicit, owner-triggered lazy fetch for КЕГЭ attachment bytes."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .assets import AssetError, AssetStore, AssetUnavailable, detect_type, safe_filename
from .config import Settings
from .events import append_event
from .models import Asset, utcnow
from .source_catalog import SourceError, canonical_asset_reference
from .source_transport import SourceFetchError
from .util import sha256_bytes

FETCH_LEASE_SECONDS = 60


class LazyAssetError(AssetUnavailable):
    """Stable fail-closed error for a missing or rejected lazy asset."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _is_quarantine_error(exc: Exception) -> bool:
    return str(getattr(exc, "code", "")) in {
        "ASSET_HASH_MISMATCH",
        "ASSET_TYPE_MISMATCH",
        "SOURCE_PRIVATE_TARGET",
        "SOURCE_DNS_INVALID",
        "SOURCE_REDIRECT_INVALID",
        "SOURCE_HOST_BLOCKED",
        "SOURCE_PATH_BLOCKED",
    }


def _fetch_result(client: Any, asset: Asset) -> tuple[bytes, str, str]:
    fetch_bytes = getattr(client, "fetch_bytes", None)
    if not callable(fetch_bytes):
        raise LazyAssetError("ASSET_FETCH_UNAVAILABLE", "attachment byte fetch is unavailable")
    try:
        value = fetch_bytes(
            asset.canonical_url,
            filename=asset.filename,
            declared_media_type=asset.media_type,
            expected_hash=asset.expected_content_hash,
        )
    except TypeError as exc:
        # Falling back to a URL-only call would silently bypass the expected
        # hash and declared-type contract (and can cause a second network
        # request).  Every production and test adapter must implement the
        # strict protocol explicitly.
        raise LazyAssetError(
            "ASSET_FETCH_PROTOCOL", "attachment adapter does not implement strict fetch_bytes protocol"
        ) from exc
    if isinstance(value, tuple) and len(value) == 3:
        data, detected, digest = value
        if not isinstance(data, bytes) or not isinstance(detected, str) or not isinstance(digest, str):
            raise LazyAssetError("ASSET_FETCH_INVALID", "attachment adapter returned an invalid result")
        actual_digest = sha256_bytes(data)
        if digest != actual_digest:
            raise LazyAssetError("ASSET_FETCH_INVALID", "attachment adapter returned an invalid digest")
        if asset.expected_content_hash and actual_digest != asset.expected_content_hash:
            raise LazyAssetError("ASSET_HASH_MISMATCH", "asset bytes do not match the expected digest")
        try:
            actual_detected = detect_type(data, safe_filename(asset.filename), asset.media_type)
        except AssetError as exc:
            raise LazyAssetError("ASSET_TYPE_MISMATCH", "asset bytes failed MIME/signature validation") from exc
        if detected != actual_detected:
            raise LazyAssetError("ASSET_FETCH_INVALID", "attachment adapter returned an invalid media type")
        return data, actual_detected, actual_digest
    if not isinstance(value, bytes):
        raise LazyAssetError("ASSET_FETCH_INVALID", "attachment adapter returned invalid bytes")
    data = value
    digest = sha256_bytes(data)
    if asset.expected_content_hash and digest != asset.expected_content_hash:
        raise LazyAssetError("ASSET_HASH_MISMATCH", "asset bytes do not match the expected digest")
    try:
        detected = detect_type(data, safe_filename(asset.filename), asset.media_type)
    except AssetError as exc:
        raise LazyAssetError("ASSET_TYPE_MISMATCH", "asset bytes failed MIME/signature validation") from exc
    return data, detected, digest


def ensure_asset_cached(
    session: Session,
    *,
    asset_id: str,
    owner_id: str,
    settings: Settings,
    client: Any,
    reason: str = "owner_action",
) -> Asset:
    """Ensure one approved asset is ready, fetching bytes at most once per claim."""

    if session.in_transaction():
        session.commit()
    with session.begin():
        asset = session.scalar(select(Asset).where(Asset.id == asset_id))
        if asset is None:
            raise LazyAssetError("NOT_FOUND", "asset not found")
        if asset.rights_status not in {"private_authorized", "public_authorized"}:
            raise LazyAssetError("SOURCE_RIGHTS_REQUIRED", "asset rights are not learner-visible")
        if asset.cache_state == "ready":
            if not asset.hash_verified:
                raise LazyAssetError("ASSET_INTEGRITY_FAILED", "cached asset is not hash-verified")
            try:
                AssetStore(settings).read(asset)
            except AssetUnavailable as exc:
                asset.cache_state = "failed"
                asset.fetch_error = "cached asset failed integrity check"
                asset.hash_verified = False
                raise LazyAssetError("ASSET_INTEGRITY_FAILED", "cached asset failed integrity check") from exc
            return asset
        if asset.cache_state == "quarantined":
            raise LazyAssetError("ASSET_QUARANTINED", "asset is quarantined")
        if asset.cache_state == "fetching" and asset.fetch_started_at is not None:
            if utcnow() - asset.fetch_started_at < timedelta(seconds=FETCH_LEASE_SECONDS):
                raise LazyAssetError("ASSET_FETCH_IN_PROGRESS", "asset fetch is already in progress")
        try:
            asset.canonical_url = canonical_asset_reference(asset.canonical_url)
        except SourceError as exc:
            asset.cache_state = "quarantined"
            asset.fetch_error = "asset URL failed source allowlist validation"
            raise LazyAssetError("SOURCE_PATH_BLOCKED", "asset URL is not an approved КЕГЭ attachment") from exc
        asset.cache_state = "fetching"
        asset.fetch_attempts = int(asset.fetch_attempts or 0) + 1
        asset.fetch_started_at = utcnow()
        asset.fetch_error = None
        session.flush()

    try:
        data, detected, digest = _fetch_result(client, asset)
        if len(data) > settings.max_asset_bytes:
            raise LazyAssetError("ASSET_TOO_LARGE", "asset exceeds configured byte limit")
        # Keep the quota check and the DB promotion in one SQLite write
        # transaction.  This serializes competing lazy fetch promotions and
        # prevents two workers from both observing free quota.
        with session.begin():
            current = session.get(Asset, asset_id)
            if current is None:
                raise LazyAssetError("NOT_FOUND", "asset disappeared during fetch")
            ready_total = int(
                session.scalar(
                    select(func.coalesce(func.sum(Asset.size_bytes), 0)).where(
                        Asset.cache_state == "ready", Asset.id != asset_id
                    )
                )
                or 0
            )
            if ready_total + len(data) > settings.max_asset_cache_bytes:
                raise LazyAssetError("ASSET_CACHE_QUOTA", "asset cache quota would be exceeded")
            stored = AssetStore(settings).store_bytes(
                canonical_url=current.canonical_url,
                filename=current.filename,
                data=data,
                declared_media_type=current.media_type,
                rights_status=current.rights_status,
            )
            if stored.content_hash != digest:
                raise LazyAssetError("ASSET_HASH_MISMATCH", "asset digest changed during local admission")
            current.storage_key = stored.storage_key
            current.media_type = stored.media_type
            current.detected_type = detected
            current.size_bytes = stored.size_bytes
            current.content_hash = digest
            # Keep ``expected_content_hash`` as upstream provenance.  A source
            # that omitted a digest must remain distinguishable from one that
            # supplied this digest; the actual bytes are already recorded in
            # ``content_hash``.  This lets a later metadata-only sync reuse the
            # same URL/filename identity after lazy promotion without silently
            # rewriting the source manifest.
            current.cache_state = "ready"
            current.hash_verified = True
            current.fetch_error = None
            current.fetch_started_at = None
            current.fetched_at = utcnow()
            append_event(
                session,
                owner_id=owner_id,
                aggregate_type="asset",
                aggregate_id=current.id,
                event_type="asset_cached",
                payload={"content_hash": digest, "reason": reason},
                idempotency_key=f"asset-cache:{current.id}:{digest}",
            )
            session.flush()
            return current
    except (LazyAssetError, SourceFetchError, AssetError, AssetUnavailable, OSError) as exc:
        quarantined = _is_quarantine_error(exc) or isinstance(exc, AssetError)
        with session.begin():
            current = session.get(Asset, asset_id)
            if current is not None:
                current.cache_state = "quarantined" if quarantined else "failed"
                current.fetch_error = str(getattr(exc, "message", "attachment fetch failed"))[:500]
                current.hash_verified = False
                current.fetch_started_at = None
        if isinstance(exc, LazyAssetError):
            raise
        code = str(getattr(exc, "code", "ASSET_FETCH_FAILED"))
        raise LazyAssetError(code, "attachment fetch failed closed") from exc

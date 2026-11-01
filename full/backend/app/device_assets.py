"""Authenticated, bounded in-memory delivery; never admits source bytes to disk."""
from __future__ import annotations

import base64
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .api_common import api_error, get_db, owner_session
from .asset_fetch import LazyAssetError, _fetch_result
from .assets import AssetStore, AssetUnavailable
from .auth import OwnerPrincipal
from .config import Settings
from .models import Asset, TaskAsset, TaskRevision, utcnow
from .source_catalog import SourceError, canonical_asset_reference
from .source_transport import BoundedKegeClient, SourceFetchError
from .spreadsheet_text import SpreadsheetTextError, convert_spreadsheet
from .util import sha256_bytes

router = APIRouter()


_VISIBLE_RIGHTS = {"private_authorized", "public_authorized"}


def _device_asset(session: Session, asset_id: str, *, line_number: int | None = None) -> Asset:
    """Resolve one learner-visible shared-bank asset without fetching bytes."""

    asset = session.get(Asset, asset_id)
    if asset is None or asset.rights_status not in _VISIBLE_RIGHTS:
        api_error(404, "NOT_FOUND", "File not found")
    link_query = (
        select(TaskRevision.id)
        .join(TaskAsset, TaskAsset.revision_id == TaskRevision.id)
        .where(
            TaskAsset.asset_id == asset_id,
            TaskRevision.rights_status.in_(_VISIBLE_RIGHTS),
        )
    )
    if line_number is not None:
        link_query = link_query.where(TaskRevision.line_number == line_number)
    linked = session.scalar(link_query.limit(1))
    if linked is None:
        api_error(404, "NOT_FOUND", "File not found")
    if asset.cache_state == "quarantined":
        api_error(503, "ASSET_QUARANTINED", "File unavailable")
    return asset


def _pin_observed_digest(session: Session, asset_id: str, digest: str) -> None:
    """Record the first successful ephemeral digest without admitting bytes.

    Source manifests may omit a digest.  In that case the first successful
    device delivery becomes the immutable source pin.  The conditional update
    is serialized under SQLite's writer lock, so concurrent first deliveries
    either agree on the same digest or fail closed without overwriting an
    existing pin.
    """

    # ``_device_asset`` and the source fetch both read through this Session,
    # which leaves a read transaction open.  Close it before taking the short
    # write transaction used for the conditional pin.
    if session.in_transaction():
        session.commit()
    with session.begin():
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        session.execute(
            update(Asset)
            .where(
                Asset.id == asset_id,
                Asset.expected_content_hash.is_(None),
                Asset.cache_state == "metadata_only",
                Asset.rights_status.in_(_VISIBLE_RIGHTS),
            )
            .values(expected_content_hash=digest, fetched_at=utcnow())
        )
        current = session.get(Asset, asset_id)
        if current is None:
            raise LazyAssetError("NOT_FOUND", "asset disappeared during delivery")
        # Refresh immediately after the conditional UPDATE so all authorization
        # and state decisions below use the row selected under BEGIN IMMEDIATE.
        session.refresh(current)
        if current.rights_status not in _VISIBLE_RIGHTS:
            raise LazyAssetError("SOURCE_RIGHTS_REQUIRED", "asset rights are no longer authorized")
        if current.cache_state not in {"metadata_only", "ready"}:
            raise LazyAssetError("ASSET_NOT_READY", "asset is no longer available for device delivery")
        if current.cache_state == "ready" and (
            not current.hash_verified or current.content_hash != digest
        ):
            raise LazyAssetError(
                "ASSET_HASH_MISMATCH",
                "asset bytes differ from the current cached content",
            )
        if current.expected_content_hash != digest:
            raise LazyAssetError(
                "ASSET_HASH_MISMATCH",
                "asset bytes differ from the first observed source digest",
            )
        # A source-provided expected hash is an integrity claim, not proof that
        # this browser delivery was observed.  Keep the row metadata-only while
        # recording the successful, hash-checked ephemeral delivery separately.
        if current.fetched_at is None:
            current.fetched_at = utcnow()


def device_bytes(
    session: Session,
    asset_id: str,
    *,
    line_number: int | None = None,
) -> tuple[Asset, bytes, str, str, Settings]:
    settings = session.info.get("settings")
    if not isinstance(settings, Settings):
        api_error(503, "CAPABILITY_CONFIG_UNAVAILABLE", "File delivery unavailable")
    asset = _device_asset(session, asset_id, line_number=line_number)
    try:
        if asset.cache_state == "ready":
            if not asset.hash_verified:
                raise LazyAssetError("ASSET_INTEGRITY_FAILED", "File integrity unavailable")
            data = AssetStore(settings).read(asset)
            detected = asset.detected_type or asset.media_type
            digest = sha256_bytes(data)
        else:
            canonical_asset_reference(asset.canonical_url)
            client = session.info.get("source_client") or BoundedKegeClient(settings)
            data, detected, digest = _fetch_result(client, asset)
        if len(data) > settings.max_asset_bytes:
            raise LazyAssetError("ASSET_TOO_LARGE", "File too large")
        if asset.expected_content_hash is not None and digest != asset.expected_content_hash:
            raise LazyAssetError("ASSET_HASH_MISMATCH", "asset bytes do not match the pinned digest")
        if asset.cache_state != "ready":
            _pin_observed_digest(session, asset.id, digest)
        return asset, data, detected, digest, settings
    except (LazyAssetError, SourceFetchError) as exc:
        api_error(503, exc.code, "Could not load file")
    except (AssetUnavailable, SourceError, OSError):
        api_error(503, "ASSET_INTEGRITY_FAILED", "Could not load file")


@router.get("/assets/{asset_id}/device")
def deliver_device_asset(asset_id: str, session: Annotated[Session, Depends(get_db)],
                         principal: Annotated[OwnerPrincipal, Depends(owner_session)]) -> Response:
    # owner_session authorizes the shared source-bank view; no client owner IDs.
    asset, data, media_type, digest, _settings = device_bytes(session, asset_id)
    return Response(data, media_type=media_type, headers={
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "X-Content-Hash": digest,
        "Content-Disposition": "inline",
    })


@router.get("/assets/{asset_id}/device-metadata")
def deliver_device_metadata(
    asset_id: str,
    response: Response,
    session: Annotated[Session, Depends(get_db)],
    principal: Annotated[OwnerPrincipal, Depends(owner_session)],
) -> dict[str, Any]:
    """Return a source pin without fetching or promoting the asset.

    ``pinned_sha256`` is intentionally ``null`` for an unknown source digest;
    callers must load the source bytes and verify the delivery header before
    using or caching them in that case.  Metadata-only rows may carry a
    non-byte placeholder in ``content_hash``; legacy ready rows expose it only
    when ``hash_verified`` proves it is the actual byte digest.
    """

    del principal
    asset = _device_asset(session, asset_id)
    response.headers.update({
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    })
    pinned_sha256 = asset.expected_content_hash
    if pinned_sha256 is None and asset.cache_state == "ready" and asset.hash_verified:
        # Legacy ready rows may predate expected_content_hash.  Their verified
        # content_hash is the trusted observed digest for browser validation.
        pinned_sha256 = asset.content_hash
    return {
        "asset_id": asset.id,
        "filename": asset.filename,
        "pinned_sha256": pinned_sha256,
        "pinned": pinned_sha256 is not None,
    }


@router.get("/assets/{asset_id}/device-text")
def deliver_device_text(asset_id: str, session: Annotated[Session, Depends(get_db)],
                        principal: Annotated[OwnerPrincipal, Depends(owner_session)]) -> dict[str, Any]:
    asset, data, _media_type, digest, settings = device_bytes(session, asset_id, line_number=9)
    try:
        texts = convert_spreadsheet(data, asset.filename, max_input_bytes=settings.max_asset_bytes,
                                   max_output_bytes=settings.max_asset_bytes)
    except SpreadsheetTextError as exc:
        api_error(503, exc.code, "Could not convert table")
    if len(texts) > 16 or sum(len(item.content) for item in texts) > 20_000_000:
        api_error(503, "SPREADSHEET_SHEET_LIMIT", "Table exceeds file limits")
    return {"source_sha256": digest, "files": [{
        "filename": item.filename, "sheet_name": item.sheet_name,
        "content_hash": sha256_bytes(item.content), "size_bytes": len(item.content),
        "content_base64": base64.b64encode(item.content).decode("ascii"),
        "converter_version": item.converter_version,
    } for item in texts]}

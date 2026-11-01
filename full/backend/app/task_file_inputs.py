"""Verified task attachments and deterministic type-9 Python inputs."""
from __future__ import annotations

from typing import Any

from .assets import AssetStore, AssetUnavailable
from .config import Settings
from .models import Asset
from .spreadsheet_text import ConvertedText, SpreadsheetTextError, convert_spreadsheet
from .util import sha256_bytes

SPREADSHEET_EXTENSIONS = {"csv", "xls", "xlsx", "ods"}


def is_spreadsheet_filename(filename: str) -> bool:
    return filename.rsplit(".", 1)[-1].lower() in SPREADSHEET_EXTENSIONS


def spreadsheet_asset_texts(asset: Asset, settings: Settings) -> list[ConvertedText]:
    if asset.rights_status not in {"private_authorized", "public_authorized"}:
        raise SpreadsheetTextError("SOURCE_RIGHTS_REQUIRED", "File access is not authorized")
    if asset.cache_state != "ready" or not asset.hash_verified:
        raise SpreadsheetTextError("ASSET_NOT_CACHED", "Prepare the original file first")
    data = AssetStore(settings).read(asset)
    texts = convert_spreadsheet(data, asset.filename, max_input_bytes=settings.max_asset_bytes,
                                max_output_bytes=settings.max_asset_bytes)
    if not texts or len(texts) > 16:
        raise SpreadsheetTextError("SPREADSHEET_SHEET_LIMIT", "Table must contain between 1 and 16 nonempty sheets")
    return texts


def code_inputs_for_assets(
    assets: list[Asset], *, line_number: int, settings: Settings,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    manifest: list[dict[str, Any]] = []
    payload: list[dict[str, Any]] = []
    names: set[str] = set()
    for asset in assets:
        base: dict[str, Any] = {
            "asset_id": asset.id, "filename": asset.filename, "content_hash": asset.content_hash,
            "cache_state": asset.cache_state, "hash_verified": asset.hash_verified,
            "rights_status": asset.rights_status,
        }
        if asset.rights_status not in {"private_authorized", "public_authorized"}:
            return manifest + [base], [], "task attachment rights are not authorized"
        if asset.cache_state != "ready" or not asset.hash_verified:
            return manifest + [base], [], "task attachment is not cached"
        try:
            if line_number == 9 and is_spreadsheet_filename(asset.filename):
                converted = spreadsheet_asset_texts(asset, settings)
                files = [(item.filename, item.content, {
                    "source_filename": asset.filename, "source_sha256": item.source_sha256,
                    "sheet_name": item.sheet_name, "converter_version": item.converter_version,
                }) for item in converted]
            else:
                files = [(asset.filename, AssetStore(settings).read(asset), {})]
        except (AssetUnavailable, OSError, SpreadsheetTextError) as exc:
            code = exc.code if isinstance(exc, SpreadsheetTextError) else "ASSET_INTEGRITY_FAILED"
            return manifest + [base], [], f"task attachment unavailable: {code}"
        for name, content, provenance in files:
            if name.casefold() in names:
                return manifest, [], "task input filenames collide"
            names.add(name.casefold())
            digest = sha256_bytes(content)
            manifest.append({**base, "filename": name, "content_hash": digest, **provenance})
            payload.append({"name": name, "content": content, "sha256": digest})
            if len(payload) > 16 or sum(len(item["content"]) for item in payload) > 20_000_000:
                return manifest, [], "task inputs exceed runner limits"
    return manifest, payload, None

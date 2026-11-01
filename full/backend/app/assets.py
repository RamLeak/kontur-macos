"""Content-addressed local asset store with explicit download semantics."""

from __future__ import annotations

import mimetypes
import os
import re
import tempfile
from pathlib import Path

from .config import Settings
from .models import Asset, new_id, utcnow
from .util import sha256_bytes


class AssetError(ValueError):
    pass


class AssetUnavailable(FileNotFoundError):
    pass


def safe_filename(name: str, fallback: str = "attachment.bin") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    cleaned = cleaned.strip(".")
    if not cleaned or cleaned in {".", ".."}:
        return fallback
    return cleaned[:255]


def detect_type(data: bytes, filename: str, declared: str | None) -> str:
    declared_type = (declared or "").split(";", 1)[0].strip().lower()
    if data.startswith(b"%PDF-"):
        detected = "application/pdf"
    elif data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        detected = "application/zip"
    elif data.startswith(b"\xd0\xcf\x11\xe0"):
        detected = "application/x-ole-storage"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = "image/png"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        detected = "image/gif"
    elif data.startswith(b"\xff\xd8\xff"):
        detected = "image/jpeg"
    else:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            # A filename is not a trustworthy binary signature. Unknown binary
            # data stays opaque even when its extension looks like an image.
            detected = "application/octet-stream"
        else:
            # Windows registry MIME guesses may classify .csv as Excel.
            # Validated UTF-8 CSV has a platform-independent text type.
            guessed = "text/csv" if Path(filename).suffix.lower() == ".csv" else mimetypes.guess_type(filename)[0]
            detected = guessed if guessed and guessed.startswith("text/") else "text/plain"

    # A declared high-risk type must agree with magic bytes. Office Open XML
    # formats are ZIP containers, so their vendor MIME types are accepted as a
    # family; generic/octet-stream means the caller made no claim.
    if declared_type and declared_type not in {"application/octet-stream", ""}:
        zip_family = detected == "application/zip" and (
            declared_type == "application/zip" or declared_type.startswith("application/vnd.")
        )
        ole_family = detected == "application/x-ole-storage" and (
            declared_type == "application/x-ole-storage" or declared_type.startswith("application/vnd.")
        )
        if declared_type != detected and not zip_family and not ole_family:
            raise AssetError("declared media type does not match content signature")
    return detected


class AssetStore:
    def __init__(self, settings: Settings):
        self.root = Path(settings.asset_root).resolve()
        self.max_bytes = settings.max_asset_bytes

    def store_bytes(
        self,
        *,
        canonical_url: str,
        filename: str,
        data: bytes,
        declared_media_type: str | None = None,
        rights_status: str = "review_required",
    ) -> Asset:
        if len(data) > self.max_bytes:
            raise AssetError("asset exceeds configured byte limit")
        safe_name = safe_filename(filename)
        digest = sha256_bytes(data)
        detected = detect_type(data, safe_name, declared_media_type)
        extension = Path(safe_name).suffix.lower()[:16]
        storage_key = f"{digest[:2]}/{digest}{extension}"
        destination = (self.root / storage_key).resolve()
        try:
            destination.relative_to(self.root)
        except ValueError as exc:
            raise AssetError("invalid storage key") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            fd, temporary = tempfile.mkstemp(prefix=".asset-", dir=destination.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

        return Asset(
            id=new_id(),
            source_name="kompege",
            canonical_url=canonical_url,
            storage_key=storage_key,
            filename=safe_name,
            # Persist the signature-derived type.  A caller's declared MIME is
            # input for validation, never the authority shown to consumers.
            media_type=detected,
            detected_type=detected,
            size_bytes=len(data),
            content_hash=digest,
            cache_state="ready",
            rights_status=rights_status,
            hash_verified=True,
            fetched_at=utcnow(),
            created_at=utcnow(),
        )

    def read(self, asset: Asset) -> bytes:
        if asset.cache_state != "ready" or not asset.storage_key:
            raise AssetUnavailable("asset is metadata-only or quarantined")
        path = (self.root / asset.storage_key).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise AssetUnavailable("asset path escapes cache") from exc
        if not path.is_file():
            raise AssetUnavailable("asset bytes are not cached")
        data = path.read_bytes()
        if len(data) > self.max_bytes or sha256_bytes(data) != asset.content_hash:
            raise AssetUnavailable("asset integrity check failed")
        return data

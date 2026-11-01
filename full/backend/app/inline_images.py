"""Bounded locators for raster images embedded in official task JSON.

The fragment identifies bytes inside a source response, not a remote image URL.
Catalog admission retains only this locator and digest; lazy use refetches the
fixed number endpoint and verifies the exact image before cache promotion.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass

from .assets import AssetError, detect_type
from .util import sha256_bytes

MAX_INLINE_BYTES = 2_000_000
_MEDIA = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif"}
_LOCATOR = re.compile(
    r"https://kompege\.ru/api/v1/task/number/([1-9]|1[0-9]|2[0-7])"
    r"#inline-image-v1=([0-9]{1,18}):([0-9]|1[0-9]):([a-f0-9]{64})\.(png|jpg|gif)"
)


@dataclass(frozen=True, slots=True)
class InlineImage:
    endpoint: str
    source_id: str
    index: int
    digest: str
    media_type: str


def parse_inline_image(url: str) -> InlineImage | None:
    if "#inline-image-v1=" not in url:
        return None
    match = _LOCATOR.fullmatch(url)
    if match is None:
        raise ValueError("invalid inline image locator")
    number, source_id, index, digest, extension = match.groups()
    return InlineImage(
        f"https://kompege.ru/api/v1/task/number/{number}",
        source_id, int(index), digest, _MEDIA[extension],
    )


def decode_inline_image(value: str) -> tuple[bytes, str, str]:
    if len(value) > (MAX_INLINE_BYTES * 4 // 3) + 128:
        raise ValueError("inline image exceeds byte limit")
    header, separator, encoded = value.partition(",")
    extension = next((ext for ext, mime in _MEDIA.items() if header == f"data:{mime};base64"), None)
    if not separator or extension is None:
        raise ValueError("only base64 PNG, JPEG and GIF are supported")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid inline image encoding") from exc
    if not data or len(data) > MAX_INLINE_BYTES:
        raise ValueError("inline image exceeds byte limit")
    try:
        detected = detect_type(data, f"image.{extension}", _MEDIA[extension])
    except AssetError as exc:
        raise ValueError("inline image signature mismatch") from exc
    return data, detected, extension


def inline_image_asset(number: int, source_id: str, index: int, value: str) -> dict[str, str]:
    data, media_type, extension = decode_inline_image(value)
    digest = sha256_bytes(data)
    locator = (
        f"https://kompege.ru/api/v1/task/number/{number}"
        f"#inline-image-v1={source_id}:{index}:{digest}.{extension}"
    )
    if parse_inline_image(locator) is None:
        raise ValueError("invalid inline image locator")
    return {
        "url": locator, "filename": f"image-{index}.{extension}", "media_type": media_type,
        "role": "image", "content_hash": digest, "expected_hash": digest,
    }

"""Bounded adapter for the reviewed ``kege-current-bank/2.0`` snapshot.

The adapter has a deliberately narrow trust boundary.  Validation reads one
explicit JSON/JSONL file, checks the snapshot's reference graph, sanitizes task
statements and retains only bounded source metadata.  It never opens a source
``local_path``, creates a network client, or imports settings from the
environment.  Applying a verified bundle delegates every record to the
existing source catalog admission seam inside one transaction.

The full source snapshot is intentionally not part of this module or the test
fixtures.  Answers and explanations remain on the verified object for the
server-side admission call; reports and errors contain counts and safe source
identifiers only.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from html import escape
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from .config import Settings
from .inline_images import decode_inline_image, inline_image_asset
from .math_text import readable_math_text
from .models import User
from .source_catalog import (
    SourceError,
    canonical_asset_reference,
    canonical_source_url,
    import_fixture,
    sanitize_statement,
)
from .util import canonical_json, sha256_bytes, sha256_json

KEGE_CURRENT_BANK_SCHEMA = "kege-current-bank/2.0"
KEGE_CURRENT_BANK_SOURCE = "kompege"
KEGE_CURRENT_BANK_PARSER_VERSION = "kege-current-bank-v2.1"
KEGE_CURRENT_BANK_ORIGIN = "https://kompege.ru"

KEGE_MAX_FILE_BYTES = 100_000_000
KEGE_MAX_RECORD_BYTES = 750_000
KEGE_MAX_RECORDS = 10_000
KEGE_MAX_TOTAL_QUESTIONS = 12_000
KEGE_MAX_STATEMENT_BYTES = 450_000
KEGE_MAX_ANSWER_BYTES = 20_000
KEGE_MAX_METADATA_BYTES = 8_000
KEGE_MAX_METADATA_DEPTH = 20
KEGE_MAX_METADATA_NODES = 1_024
KEGE_MAX_ASSETS_PER_RECORD = 20
KEGE_MAX_DECLARED_ASSET_BYTES = 100_000_000
KEGE_MAX_EXTERNAL_RESOURCES = 20
KEGE_MAX_ID_BYTES = 32
KEGE_MAX_TITLE_BYTES = 2_000
KEGE_MAX_TAG_BYTES = 255
KEGE_MAX_SNAPSHOT_FIELD_BYTES = 255

_ASCII_DECIMAL_RE = re.compile(r"[0-9]{1,32}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.IGNORECASE)
_MEDIA_TYPE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/"
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*(?:\s*;\s*[A-Za-z0-9!#$&^_.+-]+\s*=\s*[^;\s]{1,64})?$"
)

_ROOT_FIELDS = frozenset(
    {
        "answer_note",
        "answer_status",
        "classification_counts",
        "classification_note",
        "classification_review_limits",
        "classification_sha256",
        "classification_status",
        "coverage_gaps",
        "exam_types",
        "language",
        "nested_questions_note",
        "paired_pdf_sha256",
        "resource_paths",
        "counts",
        "scope",
        "scope_note",
        "source_manifest_sha256",
        "source_snapshot_completed_at",
        "source_snapshot_started_at",
        "source_tasks_sha256",
        "source_url",
        "source_snapshot_sha256",
        "tasks",
        "title",
        "varieties_catalogue_sha256",
        "schema_version",
    }
)
_TASK_FIELDS = frozenset(
    {
        "answer_html",
        "answer_table",
        "classification",
        "comment",
        "difficulty",
        "files",
        "images",
        "raw_sha256",
        "solution_link",
        "source_issues",
        "source_number",
        "source_record_sha256",
        "source_url",
        "sub_question_classification",
        "sub_tasks",
        "task_id",
        "text_html",
    }
)
_SUBTASK_FIELDS = frozenset({"key", "number", "table", "text", "video"})
_EXAM_FIELDS = frozenset(
    {
        "assignments",
        "exam_number",
        "observed_varieties",
        "question_count",
        "question_refs",
        "title",
        "unrepresented_kinds",
        "unresolved",
    }
)
_REF_FIELDS = frozenset({"task_id", "sub_task_number"})
_FILE_FIELDS = frozenset(
    {
        "attempts",
        "bytes",
        "content_type",
        "fetched_at",
        "http_status",
        "local_path",
        "name",
        "sha256",
        "status",
        "url",
    }
)
_IMAGE_FIELDS = frozenset(
    {
        "attempts",
        "bytes",
        "content_type",
        "embedded",
        "fetched_at",
        "http_status",
        "local_path",
        "sha256",
        "status",
        "url",
    }
)


class KegeCurrentBankError(ValueError):
    """A bounded operation failed without exposing task content."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class KegeCurrentBankIssue:
    """Count-only validation context."""

    code: str
    row_number: int | None = None
    source_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"code": self.code}
        if self.row_number is not None:
            result["row_number"] = self.row_number
        if self.source_id is not None:
            result["source_id"] = self.source_id
        return result


@dataclass(frozen=True, slots=True)
class KegeCurrentBankLimits:
    """Hard limits shared by file and in-memory validation."""

    max_file_bytes: int = KEGE_MAX_FILE_BYTES
    max_record_bytes: int = KEGE_MAX_RECORD_BYTES
    max_records: int = KEGE_MAX_RECORDS
    max_total_questions: int = KEGE_MAX_TOTAL_QUESTIONS
    max_statement_bytes: int = KEGE_MAX_STATEMENT_BYTES
    max_answer_bytes: int = KEGE_MAX_ANSWER_BYTES
    max_assets_per_record: int = KEGE_MAX_ASSETS_PER_RECORD
    max_declared_asset_bytes: int = KEGE_MAX_DECLARED_ASSET_BYTES
    max_embedded_bytes: int = 8_000_000

    def __post_init__(self) -> None:
        if (
            self.max_file_bytes <= 0
            or self.max_record_bytes <= 0
            or self.max_records <= 0
            or self.max_total_questions <= 0
            or self.max_statement_bytes <= 0
            or self.max_answer_bytes <= 0
            or self.max_assets_per_record < 0
            or self.max_declared_asset_bytes <= 0
            or self.max_embedded_bytes < 0
        ):
            raise ValueError("КЕГЭ snapshot limits must be positive")


@dataclass(frozen=True, slots=True)
class KegeCurrentBankReport:
    manifest_hash: str | None
    source_file_sha256: str | None
    source_file_bytes: int
    records_seen: int
    unique_records: int
    nested_records: int
    total_questions: int
    assets_seen: int
    embedded_assets: int
    embedded_bytes: int
    unavailable_resources: int
    errors: tuple[KegeCurrentBankIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> dict[str, object]:
        result: dict[str, object] = {
            "records_seen": self.records_seen,
            "unique_records": self.unique_records,
            "nested_records": self.nested_records,
            "total_questions": self.total_questions,
            "assets_seen": self.assets_seen,
            "embedded_assets": self.embedded_assets,
            "embedded_bytes": self.embedded_bytes,
            "unavailable_resources": self.unavailable_resources,
            "error_count": len(self.errors),
            "errors_by_code": dict(sorted(Counter(item.code for item in self.errors).items())),
        }
        if self.manifest_hash is not None:
            result["manifest_hash"] = self.manifest_hash
        if self.source_file_sha256 is not None:
            result["source_file_sha256"] = self.source_file_sha256
        if self.source_file_bytes:
            result["source_file_bytes"] = self.source_file_bytes
        return result


class KegeCurrentBankValidationError(KegeCurrentBankError):
    """The complete snapshot is rejected; no partial bundle is returned."""

    def __init__(self, report: KegeCurrentBankReport) -> None:
        self.report = report
        super().__init__("KEGE_CURRENT_BANK_INVALID", f"snapshot validation failed ({len(report.errors)} errors)")


@dataclass(frozen=True, slots=True)
class KegeCurrentBankRecord:
    """One validated parent or child question, retained for server admission."""

    source_id: str
    source_url: str
    line_number: int
    source_order: int
    statement: str = field(repr=False)
    statement_latex: str | None = field(default=None, repr=False)
    answer: str | None = field(default=None, repr=False)
    explanation: str | None = field(default=None, repr=False)
    source_metadata: dict[str, Any] = field(default_factory=dict, repr=False)
    source_year: int | None = None
    source_year_basis: str | None = None
    source_created_at: str | None = None
    source_difficulty: int | None = None
    difficulty: str | None = None
    source_label: str | None = None
    exam_year: int | None = None
    target_exam_year: int | None = None
    upstream_record_id: str | None = None
    table: dict[str, int] | None = None
    assets: tuple[dict[str, Any], ...] = field(default_factory=tuple, repr=False)
    record_hash: str = ""
    embedded_asset_bytes: tuple[bytes, ...] = field(default_factory=tuple, repr=False)

    def to_payload(self, *, target_exam_year: int | None = None) -> dict[str, Any]:
        """Build the narrow source_catalog payload.

        Local-file admission is metadata-only.  Embedded bytes are retained
        only in the internal verified bundle for accounting and future
        explicitly supported promotion; they are never placed in this payload.
        """

        assets = [dict(item) for item in self.assets]
        return {
            "source_id": self.source_id,
            "source_url": self.source_url,
            "line_number": self.line_number,
            "statement": self.statement,
            "statement_latex": self.statement_latex,
            "answer": self.answer,
            "explanation": self.explanation,
            "exam_year": self.exam_year,
            "target_exam_year": self.target_exam_year if target_exam_year is None else target_exam_year,
            "source_year": self.source_year,
            "source_year_basis": self.source_year_basis,
            "source_created_at": self.source_created_at,
            "source_difficulty": self.source_difficulty,
            "difficulty": self.difficulty,
            "source_label": self.source_label,
            "source_metadata": self.source_metadata,
            "upstream_record_id": self.upstream_record_id,
            "assets": assets,
            "table": self.table,
        }


@dataclass(frozen=True, slots=True)
class VerifiedKegeCurrentBank:
    """Immutable-in-practice result of complete snapshot validation."""

    schema_version: str
    source: str
    parser_version: str
    manifest_hash: str
    source_file_sha256: str | None
    source_file_bytes: int
    records: tuple[KegeCurrentBankRecord, ...]
    report: KegeCurrentBankReport
    counts: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class KegeCurrentBankApplyResult:
    """Count-only result of one atomic manifest admission."""

    manifest_hash: str
    records_seen: int
    unique_records: int
    nested_records: int
    total_questions: int
    revisions_created: int
    replayed_records: int
    assets_seen: int
    embedded_assets_metadata_only: int
    unavailable_resources: int

    def summary(self) -> dict[str, object]:
        return {
            "manifest_hash": self.manifest_hash,
            "records_seen": self.records_seen,
            "unique_records": self.unique_records,
            "nested_records": self.nested_records,
            "total_questions": self.total_questions,
            "revisions_created": self.revisions_created,
            "replayed_records": self.replayed_records,
            "assets_seen": self.assets_seen,
            "embedded_assets_metadata_only": self.embedded_assets_metadata_only,
            "unavailable_resources": self.unavailable_resources,
        }


def _issue(code: str, row_number: int | None = None, source_id: str | None = None) -> KegeCurrentBankIssue:
    return KegeCurrentBankIssue(code, row_number, source_id)


def _bounded_text(value: Any, maximum: int, code: str, *, allow_empty: bool = True) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, code
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None, code
    if size > maximum or (not allow_empty and not value.strip()):
        return None, code
    return value, None


def _literal_decimal(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str) or _ASCII_DECIMAL_RE.fullmatch(value) is None:
        return None
    if value == "" or int(value) < 0:
        return None
    return value


def _int_field(value: Any, *, minimum: int | None = None, maximum: int | None = None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if minimum is not None and value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


def _hash_field(value: Any) -> str | None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        return None
    return value.lower()


def _metadata_bounds(
    value: Any,
    *,
    max_nodes: int = KEGE_MAX_METADATA_NODES,
    max_depth: int = KEGE_MAX_METADATA_DEPTH,
) -> str | None:
    """Bound shape before canonical JSON or reparsing can recurse."""

    pending: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > max_nodes:
            return "METADATA_TOO_LARGE"
        if isinstance(current, dict):
            if depth >= max_depth and current:
                return "METADATA_TOO_DEEP"
            if len(current) > max_nodes:
                return "METADATA_TOO_LARGE"
            for key, child in current.items():
                if not isinstance(key, str):
                    return "METADATA_INVALID"
                pending.append((child, depth + 1))
        elif isinstance(current, list):
            if depth >= max_depth and current:
                return "METADATA_TOO_DEEP"
            if len(current) > max_nodes:
                return "METADATA_TOO_LARGE"
            pending.extend((child, depth + 1) for child in current)
        elif current is None or isinstance(current, (str, int, float, bool)):
            continue
        else:
            return "METADATA_INVALID"
    return None


def _copy_metadata(
    value: Any, *, maximum_bytes: int = KEGE_MAX_METADATA_BYTES
) -> tuple[dict[str, Any] | None, str | None]:
    if value is None:
        return {}, None
    if not isinstance(value, dict):
        return None, "METADATA_INVALID"
    shape_error = _metadata_bounds(value)
    if shape_error:
        return None, shape_error
    try:
        encoded = canonical_json(value).encode("utf-8")
    except (RecursionError, TypeError, ValueError, UnicodeEncodeError):
        return None, "METADATA_INVALID"
    if len(encoded) > maximum_bytes:
        return None, "METADATA_TOO_LARGE"
    try:
        detached = json.loads(encoded.decode("utf-8"))
    except (RecursionError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "METADATA_INVALID"
    if not isinstance(detached, dict):
        return None, "METADATA_INVALID"
    return detached, None


def _safe_asset_filename(value: Any, fallback: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip().replace("\\", "/").rsplit("/", 1)[-1]
    if value in {"", ".", ".."} or "\x00" in value:
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > 255:
        return None
    return value or fallback


def _media_type(value: Any, fallback: str) -> str | None:
    if value is None:
        return fallback
    if not isinstance(value, str) or len(value.encode("utf-8")) > 255:
        return None
    value = value.strip()
    return value if _MEDIA_TYPE_RE.fullmatch(value) else None


def _external_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 2_048:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password or port:
        return None
    if not parsed.path.startswith("/") or ".." in parsed.path.split("/"):
        return None
    return value


def _table_shape(value: Any) -> tuple[dict[str, int] | None, str | None]:
    if value in (None, {}):
        return None, None
    if not isinstance(value, dict) or set(value) != {"cols", "rows"}:
        return None, "TABLE_INVALID"
    cols = _int_field(value.get("cols"), minimum=0, maximum=50)
    rows = _int_field(value.get("rows"), minimum=0, maximum=100)
    if cols is None or rows is None:
        return None, "TABLE_INVALID"
    return {"cols": cols, "rows": rows}, None


def _declared_asset(
    raw: Any,
    *,
    kind: str,
    line_number: int,
    source_id: str,
    asset_index: int,
    limits: KegeCurrentBankLimits,
) -> tuple[dict[str, Any] | None, bytes | None, bool, int, dict[str, Any] | None, str | None]:
    """Normalize one asset without reading a resource path.

    The return values are metadata, optional verified embedded bytes, embedded
    flag, byte count, unavailable-resource metadata and an error code.
    """

    if not isinstance(raw, dict):
        return None, None, False, 0, None, "ASSET_INVALID"
    allowed = _FILE_FIELDS if kind == "file" else _IMAGE_FIELDS
    if set(raw) - allowed:
        return None, None, False, 0, None, "ASSET_UNKNOWN_FIELD"
    url = raw.get("url")
    max_url_bytes = 2_048
    if kind == "image" and isinstance(url, str) and url.startswith("data:"):
        max_url_bytes = 2_700_000
    if not isinstance(url, str) or len(url.encode("utf-8")) > max_url_bytes:
        return None, None, False, 0, None, "ASSET_URL_INVALID"
    declared_hash = _hash_field(raw.get("sha256"))
    if declared_hash is None:
        return None, None, False, 0, None, "ASSET_HASH_INVALID"
    declared_bytes = _int_field(raw.get("bytes"), minimum=0, maximum=limits.max_declared_asset_bytes)
    if declared_bytes is None:
        return None, None, False, 0, None, "ASSET_SIZE_INVALID"
    embedded = bool(raw.get("embedded", False)) if kind == "image" else False
    if kind == "file" and "embedded" in raw:
        return None, None, False, 0, None, "ASSET_MANIFEST_INVALID"

    media_default = "application/octet-stream" if kind == "file" else "image/png"
    media_type = _media_type(raw.get("content_type"), media_default)
    if media_type is None:
        return None, None, False, 0, None, "ASSET_MEDIA_TYPE_INVALID"
    filename_raw = raw.get("name")
    if embedded:
        filename_raw = f"image-{asset_index}.bin"
    elif kind == "image" and filename_raw is None:
        try:
            path_name = urlsplit(url).path.rsplit("/", 1)[-1]
        except ValueError:
            return None, None, False, 0, None, "ASSET_URL_INVALID"
        filename_raw = path_name or f"image-{asset_index}.bin"
    filename = _safe_asset_filename(filename_raw, f"asset-{asset_index}.bin")
    if filename is None:
        return None, None, False, 0, None, "ASSET_FILENAME_INVALID"
    role = "file" if kind == "file" else "image"

    if embedded:
        if kind != "image" or not url.startswith("data:"):
            return None, None, False, 0, None, "INLINE_IMAGE_INVALID"
        try:
            data, detected_media, extension = decode_inline_image(url)
        except (ValueError, RecursionError):
            return None, None, False, 0, None, "INLINE_IMAGE_INVALID"
        digest = sha256_bytes(data)
        if digest != declared_hash or declared_bytes != len(data):
            return None, None, False, 0, None, "INLINE_IMAGE_HASH_MISMATCH"
        try:
            canonical_locator = inline_image_asset(line_number, source_id, asset_index, url)["url"]
        except (ValueError, RecursionError):
            return None, None, False, 0, None, "INLINE_IMAGE_INVALID"
        metadata = {
            "url": canonical_locator,
            "filename": filename if filename != f"image-{asset_index}.bin" else f"image-{asset_index}.{extension}",
            "media_type": detected_media,
            "role": role,
            "content_hash": digest,
            "expected_hash": digest,
        }
        return metadata, data, True, len(data), None, None

    if url.startswith("data:"):
        return None, None, False, 0, None, "ASSET_URL_INVALID"
    try:
        parsed_url = urlsplit(url)
        host = (parsed_url.hostname or "").lower().rstrip(".")
    except ValueError:
        return None, None, False, 0, None, "ASSET_URL_INVALID"
    if host != "kompege.ru":
        external = _external_url(url)
        if external is None:
            return None, None, False, 0, None, "ASSET_URL_INVALID"
        unavailable = {
            "url": external,
            "filename": filename,
            "role": role,
            "media_type": media_type,
            "reason": "origin_not_admitted",
            "content_hash": declared_hash,
        }
        return None, None, False, 0, unavailable, None
    try:
        canonical = canonical_asset_reference(url)
    except SourceError:
        return None, None, False, 0, None, "ASSET_URL_INVALID"
    if canonical != url:
        return None, None, False, 0, None, "ASSET_URL_NONCANONICAL"
    metadata = {
        "url": canonical,
        "filename": filename,
        "media_type": media_type,
        "role": role,
        "content_hash": declared_hash,
        "expected_hash": declared_hash,
    }
    return metadata, None, False, 0, None, None


def _asset_manifest(
    task: dict[str, Any],
    *,
    line_number: int,
    source_id: str,
    limits: KegeCurrentBankLimits,
) -> tuple[
    tuple[dict[str, Any], ...], tuple[bytes, ...], tuple[dict[str, Any], ...], int, int, list[KegeCurrentBankIssue]
]:
    assets: list[dict[str, Any]] = []
    embedded_bytes: list[bytes] = []
    unavailable: list[dict[str, Any]] = []
    embedded_count = 0
    embedded_size = 0
    errors: list[KegeCurrentBankIssue] = []
    seen: set[tuple[str, str]] = set()
    for kind in ("files", "images"):
        raw_assets = task.get(kind)
        if not isinstance(raw_assets, list) or len(raw_assets) > limits.max_assets_per_record:
            errors.append(_issue("ASSETS_INVALID", source_id=source_id))
            continue
        for index, raw in enumerate(raw_assets):
            metadata, data, is_embedded, byte_count, external, error = _declared_asset(
                raw,
                kind=kind[:-1],
                line_number=line_number,
                source_id=source_id,
                asset_index=index,
                limits=limits,
            )
            if error:
                errors.append(_issue(error, source_id=source_id))
                continue
            if external is not None:
                unavailable.append(external)
                if len(unavailable) > KEGE_MAX_EXTERNAL_RESOURCES:
                    errors.append(_issue("EXTERNAL_RESOURCES_LIMIT", source_id=source_id))
                continue
            assert metadata is not None
            identity = (metadata["url"], metadata["filename"])
            if identity in seen:
                errors.append(_issue("ASSET_DUPLICATE", source_id=source_id))
                continue
            seen.add(identity)
            assets.append(metadata)
            embedded_bytes.append(data if data is not None else b"")
            if is_embedded:
                embedded_count += 1
                embedded_size += byte_count
                if embedded_size > limits.max_embedded_bytes:
                    errors.append(_issue("EMBEDDED_BYTES_LIMIT", source_id=source_id))
    return tuple(assets), tuple(embedded_bytes), tuple(unavailable), embedded_count, embedded_size, errors


def _classification_metadata(
    classification: Any,
    source_issues: Any,
    *,
    record_id: str,
    question_number: int,
    source_order: int,
    parent_record_id: str | None,
    declared_difficulty: int | None,
    unavailable_resources: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(classification, dict):
        return None, "CLASSIFICATION_INVALID"
    primary_kind, error = _bounded_text(classification.get("primary_kind"), 255, "CLASSIFICATION_INVALID")
    if error:
        return None, error
    title, error = _bounded_text(classification.get("title"), KEGE_MAX_TITLE_BYTES, "CLASSIFICATION_INVALID")
    if error:
        return None, error
    tags = classification.get("secondary_tags", [])
    if not isinstance(tags, list) or len(tags) > 50:
        return None, "CLASSIFICATION_INVALID"
    normalized_tags: list[str] = []
    for tag in tags:
        value, tag_error = _bounded_text(tag, KEGE_MAX_TAG_BYTES, "CLASSIFICATION_INVALID", allow_empty=False)
        if tag_error or value is None:
            return None, tag_error or "CLASSIFICATION_INVALID"
        normalized_tags.append(value)
    if not isinstance(source_issues, list) or len(source_issues) > 50:
        return None, "SOURCE_ISSUES_INVALID"
    copied_issues, issues_error = _copy_metadata({"items": source_issues})
    if issues_error or copied_issues is None:
        return None, issues_error or "SOURCE_ISSUES_INVALID"
    metadata: dict[str, Any] = {
        "kege_record_id": record_id,
        "kege_question_number": question_number,
        "kege_source_order": source_order,
        "kege_schema": KEGE_CURRENT_BANK_SCHEMA,
        "primary_kind": primary_kind,
        "title": title,
        "secondary_tags": normalized_tags,
        "kege_declared_difficulty": declared_difficulty,
    }
    if parent_record_id is not None:
        metadata["kege_parent_record_id"] = parent_record_id
    if unavailable_resources:
        metadata["kege_unavailable_resources"] = [dict(item) for item in unavailable_resources]
    detached, detached_error = _copy_metadata(metadata)
    return detached, detached_error


def _record_material(record: KegeCurrentBankRecord) -> dict[str, Any]:
    return {
        "source_id": record.source_id,
        "source_url": record.source_url,
        "line_number": record.line_number,
        "source_order": record.source_order,
        "statement": record.statement,
        "statement_latex": record.statement_latex,
        "answer": record.answer,
        "explanation": record.explanation,
        "exam_year": record.exam_year,
        "target_exam_year": record.target_exam_year,
        "source_year": record.source_year,
        "source_year_basis": record.source_year_basis,
        "source_created_at": record.source_created_at,
        "source_difficulty": record.source_difficulty,
        "difficulty": record.difficulty,
        "source_label": record.source_label,
        "source_metadata": record.source_metadata,
        "upstream_record_id": record.upstream_record_id,
        "table": record.table,
        "assets": [dict(item) for item in record.assets],
    }


def kege_current_record_idempotency_key(
    record_hash: str,
    *,
    parser_version: str = KEGE_CURRENT_BANK_PARSER_VERSION,
    target_exam_year: int | None = None,
) -> str:
    """Derive a stable per-question key, including owner preparation target."""

    digest = sha256_json(
        {
            "record_hash": record_hash,
            "parser_version": parser_version,
            "target_exam_year": target_exam_year,
        }
    )
    return f"{parser_version}:{digest}"


def validate_target_exam_year(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 2_000 <= value <= 2_100:
        raise KegeCurrentBankError("TARGET_YEAR_INVALID", "target exam year must be between 2000 and 2100")
    return value


def _parse_json(raw: bytes) -> Any:
    def reject_constant(token: str) -> NoReturn:
        raise ValueError(f"unsupported JSON constant {token}")

    try:
        return json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
    except RecursionError as exc:
        raise KegeCurrentBankError("JSON_TOO_DEEP", "input JSON nesting exceeds the bounded parser limit") from exc
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise KegeCurrentBankError("JSON_INVALID", "input JSON is invalid") from exc


def _read_input(path: Path, limits: KegeCurrentBankLimits) -> tuple[Any, int, str]:
    try:
        if not path.is_file():
            raise KegeCurrentBankError("INPUT_FILE_INVALID", "input must be an explicit regular file")
        size = path.stat().st_size
        if size > limits.max_file_bytes:
            raise KegeCurrentBankError("INPUT_FILE_TOO_LARGE", "input exceeds the bounded file limit")
        raw = path.read_bytes()
    except KegeCurrentBankError:
        raise
    except OSError as exc:
        raise KegeCurrentBankError("INPUT_FILE_INVALID", "input cannot be read") from exc
    if not raw:
        raise KegeCurrentBankError("INPUT_EMPTY", "input is empty")
    try:
        parsed = _parse_json(raw)
    except KegeCurrentBankError as first_error:
        # A JSONL fallback is bounded by the same file/record limits.  Do not
        # retry a deeply nested one-line JSON object as JSONL: that would hide
        # the bounded parser failure and create a noisy per-line report.
        if first_error.code == "JSON_TOO_DEEP":
            raise
        rows: list[Any] = []
        for _line_number, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            if len(line) > limits.max_record_bytes:
                raise KegeCurrentBankError("RECORD_TOO_LARGE", "JSONL row exceeds the bounded record limit") from None
            rows.append(_parse_json(line))
            if len(rows) > limits.max_records:
                raise KegeCurrentBankError(
                    "MAX_RECORDS_EXCEEDED", "JSONL row count exceeds the bounded limit"
                ) from None
        if not rows:
            raise KegeCurrentBankError("INPUT_EMPTY", "input has no JSONL rows") from None
        return rows, size, sha256_bytes(raw)
    return parsed, size, sha256_bytes(raw)


def _root_rows(
    value: Any, limits: KegeCurrentBankLimits
) -> tuple[dict[str, Any], list[Any], list[KegeCurrentBankIssue]]:
    if not isinstance(value, dict):
        return {}, [], [_issue("ROOT_INVALID")]
    errors: list[KegeCurrentBankIssue] = []
    if set(value) - _ROOT_FIELDS:
        errors.append(_issue("ROOT_UNKNOWN_FIELD"))
    for key, metadata_value in value.items():
        if key in {"tasks", "exam_types"}:
            continue
        shape_error = _metadata_bounds(metadata_value, max_nodes=100_000, max_depth=32)
        if shape_error:
            errors.append(_issue("ROOT_METADATA_INVALID"))
            break
    if value.get("schema_version") != KEGE_CURRENT_BANK_SCHEMA:
        errors.append(_issue("SCHEMA_VERSION_INVALID"))
    raw_tasks = value.get("tasks")
    if not isinstance(raw_tasks, list):
        errors.append(_issue("TASKS_INVALID"))
        raw_tasks = []
    elif len(raw_tasks) > limits.max_records:
        errors.append(_issue("MAX_RECORDS_EXCEEDED"))
    if not isinstance(value.get("exam_types"), list):
        errors.append(_issue("EXAM_TYPES_INVALID"))
    elif len(value["exam_types"]) != 27:
        errors.append(_issue("EXAM_TYPES_COUNT_INVALID"))
    source_url = value.get("source_url")
    if source_url is not None:
        try:
            if canonical_source_url(source_url) != source_url:
                errors.append(_issue("ROOT_SOURCE_URL_INVALID"))
        except (SourceError, TypeError):
            errors.append(_issue("ROOT_SOURCE_URL_INVALID"))
    for key in (
        "source_manifest_sha256",
        "source_tasks_sha256",
        "classification_sha256",
        "paired_pdf_sha256",
        "varieties_catalogue_sha256",
    ):
        if key in value and value[key] is not None and _hash_field(value[key]) is None:
            errors.append(_issue("ROOT_HASH_INVALID"))
    return value, raw_tasks[: limits.max_records], errors


def _validate_exam_types(
    root: dict[str, Any],
    *,
    limits: KegeCurrentBankLimits,
) -> tuple[
    dict[tuple[str, int | None], tuple[int, int]], dict[int, list[tuple[str, int | None]]], list[KegeCurrentBankIssue]
]:
    refs: dict[tuple[str, int | None], tuple[int, int]] = {}
    refs_by_exam: dict[int, list[tuple[str, int | None]]] = {}
    errors: list[KegeCurrentBankIssue] = []
    exam_types = root.get("exam_types")
    if not isinstance(exam_types, list):
        return refs, refs_by_exam, [_issue("EXAM_TYPES_INVALID")]
    seen_exams: set[int] = set()
    for exam_index, raw_exam in enumerate(exam_types):
        if not isinstance(raw_exam, dict) or set(raw_exam) - _EXAM_FIELDS:
            errors.append(_issue("EXAM_TYPE_INVALID", exam_index + 1))
            continue
        exam_number = _int_field(raw_exam.get("exam_number"), minimum=1, maximum=27)
        if exam_number is None or exam_number in seen_exams:
            errors.append(_issue("EXAM_NUMBER_INVALID", exam_index + 1))
            continue
        seen_exams.add(exam_number)
        for metadata_key in ("assignments", "observed_varieties", "unrepresented_kinds", "unresolved"):
            if metadata_key in raw_exam and _metadata_bounds(raw_exam[metadata_key], max_nodes=100_000, max_depth=32):
                errors.append(_issue("EXAM_METADATA_INVALID", exam_index + 1))
        raw_refs = raw_exam.get("question_refs")
        if not isinstance(raw_refs, list) or len(raw_refs) > limits.max_total_questions:
            errors.append(_issue("QUESTION_REFS_INVALID", exam_index + 1))
            continue
        if raw_exam.get("question_count") != len(raw_refs):
            errors.append(_issue("QUESTION_COUNT_INVALID", exam_index + 1))
        current_ids: list[int] = []
        parsed_refs: list[tuple[str, int | None]] = []
        for ref_index, raw_ref in enumerate(raw_refs):
            if not isinstance(raw_ref, dict) or set(raw_ref) != _REF_FIELDS:
                errors.append(_issue("QUESTION_REF_INVALID", ref_index + 1))
                continue
            literal_id = _literal_decimal(raw_ref.get("task_id"))
            sub_number = raw_ref.get("sub_task_number")
            if literal_id is None or (sub_number is not None and sub_number not in {20, 21}):
                errors.append(_issue("QUESTION_REF_INVALID", ref_index + 1, literal_id))
                continue
            numeric_id = int(literal_id)
            current_ids.append(numeric_id)
            key = (literal_id, sub_number)
            if key in refs:
                errors.append(_issue("QUESTION_REF_DUPLICATE", ref_index + 1, literal_id))
            else:
                refs[key] = (exam_number, ref_index)
                parsed_refs.append(key)
        if current_ids != sorted(current_ids, reverse=True):
            errors.append(_issue("QUESTION_ORDER_INVALID", exam_index + 1))
        refs_by_exam[exam_number] = parsed_refs
    if seen_exams != set(range(1, 28)):
        errors.append(_issue("EXAM_NUMBERS_INCOMPLETE"))
    return refs, refs_by_exam, errors


def _validate_counts(
    root: dict[str, Any], tasks: list[Any], *, parent_count: int, child_count: int, refs_count: int
) -> list[KegeCurrentBankIssue]:
    raw_counts = root.get("counts")
    if raw_counts is None:
        return []
    if not isinstance(raw_counts, dict):
        return [_issue("COUNTS_INVALID")]
    errors: list[KegeCurrentBankIssue] = []
    expected = {
        "records": len(tasks),
        "nested_questions": child_count,
        "total_questions": refs_count,
        "source_categories": len({t.get("source_number") for t in tasks if isinstance(t, dict)}),
        "exam_numbers": 27,
    }
    for key, value in expected.items():
        if key in raw_counts and raw_counts[key] != value:
            errors.append(_issue("COUNTS_MISMATCH"))
    by_source = raw_counts.get("by_source_number")
    if by_source is not None:
        if not isinstance(by_source, dict):
            errors.append(_issue("COUNTS_INVALID"))
        else:
            actual = Counter(t.get("source_number") for t in tasks if isinstance(t, dict))
            for key, value in by_source.items():
                try:
                    number = int(key)
                except (TypeError, ValueError):
                    errors.append(_issue("COUNTS_INVALID"))
                    continue
                if value != actual.get(number, 0):
                    errors.append(_issue("COUNTS_MISMATCH"))
    return errors


def _task_classification(task: dict[str, Any], child: dict[str, Any] | None) -> Any:
    if child is not None:
        parent = task.get("sub_question_classification")
        if isinstance(parent, list):
            for item in parent:
                if isinstance(item, dict) and item.get("exam_number") == child.get("number"):
                    return item
    return task.get("classification")


def _find_reference(
    refs: dict[tuple[str, int | None], tuple[int, int]],
    source_id: str,
    sub_task_number: int | None,
) -> tuple[tuple[str, int | None], tuple[int, int]] | None:
    """Match numeric reference ids while retaining the task's literal id.

    The reviewed snapshot uses integers in ``question_refs``.  Synthetic and
    future local snapshots may carry a decimal string with leading zeroes; the
    task's literal source id remains authoritative for the canonical URL and
    internal child namespace, while the reference graph still matches its
    numeric value.
    """

    exact = (source_id, sub_task_number)
    if exact in refs:
        return exact, refs[exact]
    numeric = int(source_id)
    candidates = [(key, value) for key, value in refs.items() if key[1] == sub_task_number and int(key[0]) == numeric]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _make_record(
    task: dict[str, Any],
    *,
    source_id: str,
    line_number: int,
    source_order: int,
    parent_statement: str,
    parent_assets: tuple[dict[str, Any], ...],
    parent_embedded_bytes: tuple[bytes, ...],
    parent_unavailable: tuple[dict[str, Any], ...],
    child: dict[str, Any] | None,
    limits: KegeCurrentBankLimits,
) -> tuple[KegeCurrentBankRecord | None, list[KegeCurrentBankIssue]]:
    errors: list[KegeCurrentBankIssue] = []
    task_id = _literal_decimal(task.get("task_id"))
    if task_id is None or source_id == "":
        errors.append(_issue("TASK_ID_INVALID", source_id=source_id or None))
    raw_answer = child.get("key") if child is not None else task.get("answer_html")
    answer, answer_error = _bounded_text(raw_answer, limits.max_answer_bytes, "ANSWER_INVALID")
    if answer_error:
        errors.append(_issue(answer_error, source_id=source_id))
    elif answer is not None:
        try:
            answer, _ = sanitize_statement(answer, limits.max_answer_bytes)
        except (ValueError, RecursionError):
            errors.append(_issue("ANSWER_INVALID", source_id=source_id))
    if answer == "":
        answer = None
    statement = parent_statement
    table_raw = child.get("table") if child is not None else task.get("answer_table")
    table, table_error = _table_shape(table_raw)
    if table_error:
        errors.append(_issue(table_error, source_id=source_id))
    if child is not None:
        child_text, child_error = _bounded_text(
            child.get("text"), limits.max_statement_bytes, "STATEMENT_INVALID", allow_empty=False
        )
        if child_error or child_text is None:
            errors.append(_issue(child_error or "STATEMENT_INVALID", source_id=source_id))
        else:
            try:
                child_statement, _ = sanitize_statement(child_text, limits.max_statement_bytes)
                statement = f"Условие задания 19 (контекст)\n{parent_statement}\n\n{child_statement}"
            except (ValueError, RecursionError):
                errors.append(_issue("STATEMENT_INVALID", source_id=source_id))
    private_explanation: str | None = None
    raw_source_issues = task.get("source_issues")
    if isinstance(raw_source_issues, list) and raw_source_issues:
        try:
            private_explanation = canonical_json({"source_issues": raw_source_issues})
        except (RecursionError, TypeError, ValueError, UnicodeEncodeError):
            errors.append(_issue("SOURCE_ISSUES_INVALID", source_id=source_id))
        else:
            if len(private_explanation.encode("utf-8")) > KEGE_MAX_METADATA_BYTES:
                errors.append(_issue("SOURCE_ISSUES_INVALID", source_id=source_id))
    classification = _task_classification(task, child)
    metadata, metadata_error = _classification_metadata(
        classification,
        task.get("source_issues"),
        record_id=task_id or source_id,
        question_number=line_number,
        source_order=source_order,
        parent_record_id=task_id if child is not None else None,
        declared_difficulty=task.get("difficulty") if isinstance(task.get("difficulty"), int) else None,
        unavailable_resources=parent_unavailable,
    )
    if metadata_error:
        errors.append(_issue(metadata_error, source_id=source_id))
    if errors or metadata is None:
        return None, errors
    if parent_unavailable:
        statement = f"{statement}\n\nПримечание: часть исходных материалов недоступна в текущем импорте."
    statement = readable_math_text(statement)
    difficulty_raw = task.get("difficulty")
    difficulty = {0: "easy", 1: "medium", 2: "hard", 3: "hard"}.get(
        difficulty_raw if isinstance(difficulty_raw, int) and not isinstance(difficulty_raw, bool) else -1
    )
    material = {
        "source_id": source_id,
        "source_url": f"{KEGE_CURRENT_BANK_ORIGIN}/task?id={task_id or source_id}",
        "line_number": line_number,
        "source_order": source_order,
        "statement": statement,
        "statement_latex": None,
        "answer": answer,
        "explanation": private_explanation,
        "exam_year": None,
        "target_exam_year": None,
        "source_year": None,
        "source_year_basis": None,
        "source_created_at": None,
        "source_difficulty": difficulty_raw if isinstance(difficulty_raw, int) and 0 <= difficulty_raw <= 3 else None,
        "difficulty": difficulty,
        "source_label": None,
        "source_metadata": metadata,
        "upstream_record_id": None,
        "table": table,
        "assets": [dict(asset) for asset in parent_assets],
    }
    try:
        record_hash = sha256_json(material)
    except (RecursionError, TypeError, ValueError, UnicodeEncodeError):
        return None, [_issue("RECORD_HASH_INVALID", source_id=source_id)]
    return (
        KegeCurrentBankRecord(
            source_id=source_id,
            source_url=f"{KEGE_CURRENT_BANK_ORIGIN}/task?id={task_id or source_id}",
            line_number=line_number,
            source_order=source_order,
            statement=statement,
            explanation=private_explanation,
            answer=answer,
            source_metadata=metadata,
            source_difficulty=difficulty_raw if isinstance(difficulty_raw, int) and 0 <= difficulty_raw <= 3 else None,
            difficulty=difficulty,
            table=table,
            assets=parent_assets,
            record_hash=record_hash,
            embedded_asset_bytes=parent_embedded_bytes,
        ),
        [],
    )


def _validate_snapshot(
    value: Any, *, limits: KegeCurrentBankLimits, source_file_sha256: str | None, source_file_bytes: int
) -> VerifiedKegeCurrentBank:
    root, tasks, errors = _root_rows(value, limits)
    refs, _refs_by_exam, ref_errors = _validate_exam_types(root, limits=limits)
    errors.extend(ref_errors)
    if len(tasks) > limits.max_records:
        errors.append(_issue("MAX_RECORDS_EXCEEDED"))
    records: list[KegeCurrentBankRecord] = []
    seen_task_ids: set[str] = set()
    seen_questions: set[tuple[str, int | None]] = set()
    nested_count = 0
    assets_seen = 0
    embedded_count = 0
    embedded_bytes_count = 0
    unavailable_count = 0
    task_map: dict[str, dict[str, Any]] = {}
    parent_context: dict[
        str, tuple[str, tuple[dict[str, Any], ...], tuple[bytes, ...], tuple[dict[str, Any], ...]]
    ] = {}
    for row_number, raw_task in enumerate(tasks, start=1):
        if not isinstance(raw_task, dict) or set(raw_task) - _TASK_FIELDS:
            errors.append(_issue("TASK_FIELDS_INVALID", row_number))
            continue
        try:
            encoded = canonical_json(raw_task).encode("utf-8")
        except (RecursionError, TypeError, ValueError, UnicodeEncodeError):
            errors.append(_issue("TASK_RECORD_INVALID", row_number))
            continue
        if len(encoded) > limits.max_record_bytes:
            errors.append(_issue("TASK_RECORD_TOO_LARGE", row_number))
            continue
        task_id = _literal_decimal(raw_task.get("task_id"))
        if task_id is None:
            errors.append(_issue("TASK_ID_INVALID", row_number))
            continue
        if task_id in seen_task_ids:
            errors.append(_issue("TASK_ID_DUPLICATE", row_number, task_id))
            continue
        seen_task_ids.add(task_id)
        task_map[task_id] = raw_task
        line_number = _int_field(raw_task.get("source_number"), minimum=1, maximum=27)
        if line_number is None:
            errors.append(_issue("SOURCE_NUMBER_INVALID", row_number, task_id))
            continue
        if line_number in {20, 21}:
            errors.append(_issue("INDEPENDENT_CHILD_INVALID", row_number, task_id))
            continue
        expected_url = f"{KEGE_CURRENT_BANK_ORIGIN}/task?id={task_id}"
        raw_url = raw_task.get("source_url")
        if not isinstance(raw_url, str) or raw_url != expected_url:
            errors.append(_issue("SOURCE_URL_ID_MISMATCH", row_number, task_id))
        else:
            try:
                if canonical_source_url(raw_url) != raw_url:
                    raise SourceError("SOURCE_URL_INVALID", "noncanonical")
            except (SourceError, TypeError):
                errors.append(_issue("SOURCE_URL_INVALID", row_number, task_id))
        text_html, text_error = _bounded_text(
            raw_task.get("text_html"), limits.max_statement_bytes, "STATEMENT_INVALID", allow_empty=False
        )
        if text_error or text_html is None:
            errors.append(_issue(text_error or "STATEMENT_INVALID", row_number, task_id))
            continue
        try:
            statement, _ = sanitize_statement(text_html, limits.max_statement_bytes)
        except (ValueError, RecursionError):
            errors.append(_issue("STATEMENT_INVALID", row_number, task_id))
            continue
        if not statement:
            errors.append(_issue("STATEMENT_EMPTY", row_number, task_id))
            continue
        for field_name in ("answer_html", "comment"):
            value_field, field_error = _bounded_text(
                raw_task.get(field_name),
                limits.max_answer_bytes if field_name == "answer_html" else 10_000,
                "ANSWER_INVALID" if field_name == "answer_html" else "COMMENT_INVALID",
            )
            if field_error:
                errors.append(_issue(field_error, row_number, task_id))
        difficulty = raw_task.get("difficulty")
        if _int_field(difficulty, minimum=0, maximum=3) is None:
            errors.append(_issue("DIFFICULTY_INVALID", row_number, task_id))
        assets, embedded_data, unavailable, embedded_for_task, embedded_size, asset_errors = _asset_manifest(
            raw_task, line_number=line_number, source_id=task_id, limits=limits
        )
        for issue in asset_errors:
            issue = KegeCurrentBankIssue(issue.code, row_number, task_id)
            errors.append(issue)
        assets_seen += len(assets)
        embedded_count += embedded_for_task
        embedded_bytes_count += embedded_size
        unavailable_count += len(unavailable)
        child_list = raw_task.get("sub_tasks")
        if not isinstance(child_list, list) or len(child_list) > 2:
            errors.append(_issue("SUBTASKS_INVALID", row_number, task_id))
            child_list = []
        if child_list and line_number != 19:
            errors.append(_issue("SUBTASK_PARENT_INVALID", row_number, task_id))
        child_numbers: set[int] = set()
        for child in child_list:
            if not isinstance(child, dict) or set(child) - _SUBTASK_FIELDS:
                errors.append(_issue("SUBTASK_INVALID", row_number, task_id))
                continue
            child_number = _int_field(child.get("number"), minimum=20, maximum=21)
            if child_number is None or child_number in child_numbers:
                errors.append(_issue("SUBTASK_NUMBER_INVALID", row_number, task_id))
                continue
            child_numbers.add(child_number)
            key, key_error = _bounded_text(child.get("key"), limits.max_answer_bytes, "SUBTASK_KEY_INVALID")
            child_text, child_text_error = _bounded_text(
                child.get("text"), limits.max_statement_bytes, "SUBTASK_TEXT_INVALID", allow_empty=False
            )
            if key_error or child_text_error or key is None or child_text is None:
                errors.append(_issue(key_error or child_text_error or "SUBTASK_INVALID", row_number, task_id))
            child_table, child_table_error = _table_shape(child.get("table"))
            if child_table_error:
                errors.append(_issue(child_table_error, row_number, task_id))
            if "video" in child:
                _video, video_error = _bounded_text(child.get("video"), 255, "SUBTASK_VIDEO_INVALID")
                if video_error:
                    errors.append(_issue(video_error, row_number, task_id))
        if child_list and child_numbers != {20, 21}:
            errors.append(_issue("SUBTASK_COVERAGE_INVALID", row_number, task_id))
        if child_list:
            nested_count += len(child_list)
        parent_context[task_id] = (statement, assets, embedded_data, unavailable)
    if nested_count != sum(1 for task in tasks if isinstance(task, dict) for _ in (task.get("sub_tasks") or [])):
        errors.append(_issue("SUBTASK_COUNT_INVALID"))
    # Build each parent first, then its linked children.  Every ref must map to
    # exactly one question and every task/subtask must be covered by an exam ref.
    for task_id, task in task_map.items():
        line_number = _int_field(task.get("source_number"), minimum=1, maximum=27)
        if line_number is None or task_id not in parent_context:
            continue
        parent_reference = _find_reference(refs, task_id, None)
        if parent_reference is None:
            errors.append(_issue("REFERENCE_MISSING", source_id=task_id))
            continue
        ref_key, ref = parent_reference
        exam_number, source_order = ref
        if exam_number != line_number:
            errors.append(_issue("REFERENCE_LINE_MISMATCH", source_id=task_id))
            continue
        statement, assets, embedded_data, unavailable = parent_context[task_id]
        parent, parent_errors = _make_record(
            task,
            source_id=task_id,
            line_number=line_number,
            source_order=source_order,
            parent_statement=statement,
            parent_assets=assets,
            parent_embedded_bytes=embedded_data,
            parent_unavailable=unavailable,
            child=None,
            limits=limits,
        )
        errors.extend(parent_errors)
        if parent is not None:
            records.append(parent)
            seen_questions.add(ref_key)
        for child in task.get("sub_tasks") or []:
            child_number = child.get("number") if isinstance(child, dict) else None
            child_literal = f"{task_id}~{child_number}" if child_number in {20, 21} else ""
            child_reference = _find_reference(refs, task_id, child_number) if child_number in {20, 21} else None
            if child_reference is None:
                errors.append(_issue("CHILD_REFERENCE_MISSING", source_id=task_id))
                continue
            child_ref_key, child_ref = child_reference
            child_exam, child_order = child_ref
            if child_exam != child_number:
                errors.append(_issue("CHILD_REFERENCE_LINE_MISMATCH", source_id=task_id))
                continue
            child_record, child_errors = _make_record(
                task,
                source_id=child_literal,
                line_number=child_number,
                source_order=child_order,
                parent_statement=statement,
                parent_assets=assets,
                parent_embedded_bytes=embedded_data,
                parent_unavailable=unavailable,
                child=child,
                limits=limits,
            )
            errors.extend(child_errors)
            if child_record is not None:
                records.append(child_record)
                seen_questions.add(child_ref_key)
    expected_questions = set(refs)
    if seen_questions != expected_questions:
        errors.append(_issue("REFERENCE_COVERAGE_INVALID"))
    if len(records) > limits.max_total_questions:
        errors.append(_issue("MAX_QUESTIONS_EXCEEDED"))
    errors.extend(
        _validate_counts(root, tasks, parent_count=len(task_map), child_count=nested_count, refs_count=len(refs))
    )
    manifest_hash: str | None = None
    if not errors:
        records = sorted(records, key=lambda item: (item.line_number, item.source_order, item.source_id))
        try:
            manifest_hash = sha256_json(
                {
                    "schema_version": KEGE_CURRENT_BANK_SCHEMA,
                    "parser_version": KEGE_CURRENT_BANK_PARSER_VERSION,
                    "records": [_record_material(record) for record in records],
                }
            )
        except (RecursionError, TypeError, ValueError, UnicodeEncodeError):
            errors.append(_issue("MANIFEST_INVALID"))
    report = KegeCurrentBankReport(
        manifest_hash=manifest_hash,
        source_file_sha256=source_file_sha256,
        source_file_bytes=source_file_bytes,
        records_seen=len(tasks),
        unique_records=len(task_map),
        nested_records=nested_count,
        total_questions=len(refs),
        assets_seen=assets_seen,
        embedded_assets=embedded_count,
        embedded_bytes=embedded_bytes_count,
        unavailable_resources=unavailable_count,
        errors=tuple(errors),
    )
    if not report.ok:
        raise KegeCurrentBankValidationError(report)
    assert manifest_hash is not None
    counts = root.get("counts") if isinstance(root.get("counts"), dict) else {}
    copied_counts, counts_error = _copy_metadata(counts, maximum_bytes=16_000)
    if counts_error or copied_counts is None:
        raise KegeCurrentBankValidationError(replace(report, errors=(_issue("COUNTS_INVALID"),), manifest_hash=None))
    return VerifiedKegeCurrentBank(
        schema_version=KEGE_CURRENT_BANK_SCHEMA,
        source=KEGE_CURRENT_BANK_SOURCE,
        parser_version=KEGE_CURRENT_BANK_PARSER_VERSION,
        manifest_hash=manifest_hash,
        source_file_sha256=source_file_sha256,
        source_file_bytes=source_file_bytes,
        records=tuple(records),
        report=report,
        counts=copied_counts,
    )


def validate_kege_current_bank(value: Any, *, limits: KegeCurrentBankLimits | None = None) -> VerifiedKegeCurrentBank:
    """Validate one in-memory full current-bank envelope without side effects."""

    return _validate_snapshot(
        value, limits=limits or KegeCurrentBankLimits(), source_file_sha256=None, source_file_bytes=0
    )


def load_kege_current_bank(path: str | Path, *, limits: KegeCurrentBankLimits | None = None) -> VerifiedKegeCurrentBank:
    """Load one explicit JSON/JSONL file with no DB, settings or network access."""

    effective = limits or KegeCurrentBankLimits()
    value, size, source_hash = _read_input(Path(path), effective)
    if isinstance(value, list):
        # JSONL rows are accepted as a sequence only when one row is an
        # envelope.  This keeps the full-bank reference graph mandatory while
        # allowing a one-line test envelope to use the same file API.
        if len(value) != 1:
            raise KegeCurrentBankError("JSONL_UNSUPPORTED", "current-bank JSONL must contain one envelope row")
        value = value[0]
    return _validate_snapshot(value, limits=effective, source_file_sha256=source_hash, source_file_bytes=size)


def apply_verified_kege_current_bank(
    session: Session,
    bundle: VerifiedKegeCurrentBank,
    *,
    owner_id: str,
    settings: Settings,
    rights_status: str,
    target_exam_year: int | None = None,
    assets_root: str | Path | None = None,
) -> KegeCurrentBankApplyResult:
    """Atomically admit a complete verified bundle through source_catalog."""

    if not isinstance(bundle, VerifiedKegeCurrentBank) or not bundle.report.ok:
        raise KegeCurrentBankError("BUNDLE_NOT_VERIFIED", "apply requires a verified current-bank bundle")
    if bundle.schema_version != KEGE_CURRENT_BANK_SCHEMA or bundle.parser_version != KEGE_CURRENT_BANK_PARSER_VERSION:
        raise KegeCurrentBankError("BUNDLE_VERSION_INVALID", "bundle schema or parser is unsupported")
    try:
        expected_manifest = sha256_json(
            {
                "schema_version": KEGE_CURRENT_BANK_SCHEMA,
                "parser_version": KEGE_CURRENT_BANK_PARSER_VERSION,
                "records": [_record_material(record) for record in bundle.records],
            }
        )
    except (RecursionError, TypeError, ValueError, UnicodeEncodeError) as exc:
        raise KegeCurrentBankError("BUNDLE_TAMPERED", "verified bundle manifest cannot be encoded") from exc
    if expected_manifest != bundle.manifest_hash:
        raise KegeCurrentBankError("BUNDLE_TAMPERED", "verified bundle manifest changed")
    if not isinstance(owner_id, str) or not owner_id or len(owner_id.encode("utf-8")) > 64:
        raise KegeCurrentBankError("OWNER_INVALID", "owner id is invalid")
    if rights_status not in {"private_authorized", "public_authorized"}:
        raise KegeCurrentBankError("RIGHTS_INVALID", "rights status must be explicitly authorized")
    if settings.source_mode != "manual" or settings.source_network_enabled is not False:
        raise KegeCurrentBankError("SOURCE_DISABLED", "current-bank apply requires manual mode with network disabled")
    effective_target_year = validate_target_exam_year(target_exam_year)
    if assets_root is not None:
        root = Path(assets_root)
        if root.exists() and root.is_file():
            raise KegeCurrentBankError("ASSETS_ROOT_INVALID", "assets root must be a directory path")
        settings = replace(settings, asset_root=root)
    # The actual snapshot contains statements above the conservative runtime
    # default.  Raising this bounded limit is local to this explicit adapter;
    # it does not alter Settings.from_env or any process-wide policy.
    if settings.max_message_bytes < KEGE_MAX_STATEMENT_BYTES:
        settings = replace(settings, max_message_bytes=KEGE_MAX_STATEMENT_BYTES)

    for record in bundle.records:
        try:
            expected_record_hash = sha256_json(_record_material(record))
        except (RecursionError, TypeError, ValueError, UnicodeEncodeError) as exc:
            raise KegeCurrentBankError("BUNDLE_TAMPERED", "verified record cannot be encoded") from exc
        if expected_record_hash != record.record_hash:
            raise KegeCurrentBankError("BUNDLE_TAMPERED", "verified record hash changed")

    transaction = session.begin_nested() if session.in_transaction() else session.begin()
    revisions_created = 0
    replayed_records = 0
    assets_seen = 0
    try:
        if session.get(User, owner_id) is None:
            session.add(User(id=owner_id))
            session.flush()
        for record in bundle.records:
            payload = record.to_payload(target_exam_year=effective_target_year)
            # The reviewed record is already plain text; the common catalog
            # seam accepts HTML, so escape exactly once at that boundary.
            payload["statement"] = escape(record.statement)
            _task, _revision, assets, duplicate = import_fixture(
                session,
                owner_id=owner_id,
                settings=settings,
                payload=payload,
                idempotency_key=kege_current_record_idempotency_key(
                    record.record_hash,
                    parser_version=KEGE_CURRENT_BANK_PARSER_VERSION,
                    target_exam_year=effective_target_year,
                ),
                admission_mode="local-file",
                admitted_rights_status=rights_status,
                parser_version=KEGE_CURRENT_BANK_PARSER_VERSION,
                target_exam_year=effective_target_year,
            )
            assets_seen += len(assets)
            if duplicate:
                replayed_records += 1
            else:
                revisions_created += 1
        transaction.commit()
    except Exception:
        transaction.rollback()
        raise
    return KegeCurrentBankApplyResult(
        manifest_hash=bundle.manifest_hash,
        records_seen=bundle.report.records_seen,
        unique_records=len(bundle.records),
        nested_records=bundle.report.nested_records,
        total_questions=bundle.report.total_questions,
        revisions_created=revisions_created,
        replayed_records=replayed_records,
        assets_seen=assets_seen,
        embedded_assets_metadata_only=bundle.report.embedded_assets,
        unavailable_resources=bundle.report.unavailable_resources,
    )


# Short aliases make the adapter easy to discover while preserving the more
# explicit public names used by the CLI and integration tests.
validate_bundle = validate_kege_current_bank
load_current_bank = load_kege_current_bank
apply_verified_bundle = apply_verified_kege_current_bank


__all__ = [
    "KEGE_CURRENT_BANK_PARSER_VERSION",
    "KEGE_CURRENT_BANK_SCHEMA",
    "KegeCurrentBankApplyResult",
    "KegeCurrentBankError",
    "KegeCurrentBankIssue",
    "KegeCurrentBankLimits",
    "KegeCurrentBankRecord",
    "KegeCurrentBankReport",
    "KegeCurrentBankValidationError",
    "VerifiedKegeCurrentBank",
    "apply_verified_bundle",
    "apply_verified_kege_current_bank",
    "kege_current_record_idempotency_key",
    "load_current_bank",
    "load_kege_current_bank",
    "validate_bundle",
    "validate_kege_current_bank",
    "validate_target_exam_year",
]

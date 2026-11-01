"""Bounded, offline preparation and atomic admission of a local КЕГЭ bank.

The module deliberately separates two phases:

* load_local_bank reads and validates an explicitly selected JSON or JSONL file
  without opening a database, an asset cache, or a network client.
* apply_verified_bundle accepts only the immutable result of that validation
  and delegates one record at a time to the existing source catalog importer
  inside one transaction.

Answers and explanations are kept in the verified records for server-side
storage. Reports and command-line output contain hashes and counters only.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from sqlalchemy.orm import Session

from .assets import safe_filename
from .config import Settings
from .source_catalog import (
    SourceError,
    canonical_asset_reference,
    canonical_source_url,
    import_fixture,
    sanitize_statement,
)
from .util import canonical_json, sha256_json, sha256_text

LOCAL_BANK_SCHEMA_VERSION = 1
LOCAL_BANK_SOURCE = "kompege"
LOCAL_BANK_PARSER_VERSION = "local-bank-v1"
LOCAL_BANK_ORIGIN = "https://kompege.ru"
LOCAL_BANK_MAX_SOURCE_ID_BYTES = 255
LOCAL_BANK_MAX_URL_BYTES = 2_048
LOCAL_BANK_MAX_FILENAME_BYTES = 255
LOCAL_BANK_MAX_MEDIA_TYPE_BYTES = 255
LOCAL_BANK_MAX_STATEMENT_BYTES = 100_000
LOCAL_BANK_MAX_LATEX_BYTES = 100_000
LOCAL_BANK_MAX_ANSWER_BYTES = 20_000
LOCAL_BANK_MAX_EXPLANATION_BYTES = 100_000
LOCAL_BANK_MAX_LABEL_BYTES = 2_000
LOCAL_BANK_MAX_METADATA_BYTES = 8_000
LOCAL_BANK_MAX_METADATA_DEPTH = 32
LOCAL_BANK_MAX_METADATA_NODES = 2_048
LOCAL_BANK_MAX_ASSETS_PER_RECORD = 20
LOCAL_BANK_MAX_RECORD_BYTES = 300_000
LOCAL_BANK_MAX_FILE_BYTES = 100_000_000
LOCAL_BANK_MAX_RECORDS = 10_000
LOCAL_BANK_ALLOWED_RIGHTS = frozenset({"private_authorized", "public_authorized"})
_SOURCE_ID_RE = re.compile(r"[0-9]+")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_RECORD_FIELDS = frozenset(
    {
        "source_id",
        "source_url",
        "line_number",
        "statement",
        "statement_latex",
        "answer",
        "explanation",
        "exam_year",
        "target_exam_year",
        "source_year",
        "source_year_basis",
        "source_created_at",
        "source_difficulty",
        "difficulty",
        "source_label",
        "source_metadata",
        "upstream_record_id",
        "assets",
    }
)
_ASSET_FIELDS = frozenset({"url", "canonical_url", "filename", "media_type", "role", "content_hash"})
_DIFFICULTY_BY_SOURCE = {0: "easy", 1: "medium", 2: "hard"}


class LocalBankError(ValueError):
    """A bounded local-bank operation failed without exposing record content."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class LocalBankIssue:
    """Count-only validation context for one rejected row."""

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
class LocalBankLimits:
    """Hard limits used by both JSON and JSONL validation."""

    max_file_bytes: int = LOCAL_BANK_MAX_FILE_BYTES
    max_record_bytes: int = LOCAL_BANK_MAX_RECORD_BYTES
    max_records: int = LOCAL_BANK_MAX_RECORDS
    max_assets_per_record: int = LOCAL_BANK_MAX_ASSETS_PER_RECORD

    def __post_init__(self) -> None:
        if (
            self.max_file_bytes <= 0
            or self.max_record_bytes <= 0
            or self.max_records <= 0
            or self.max_assets_per_record < 0
        ):
            raise ValueError("local bank limits must be positive")


@dataclass(frozen=True, slots=True)
class LocalBankReport:
    """Safe summary suitable for CLI/API output."""

    manifest_hash: str | None
    records_seen: int
    unique_records: int
    duplicate_records: int
    assets_seen: int
    errors: tuple[LocalBankIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> dict[str, object]:
        errors_by_code = Counter(issue.code for issue in self.errors)
        result: dict[str, object] = {
            "records_seen": self.records_seen,
            "unique_records": self.unique_records,
            "duplicate_records": self.duplicate_records,
            "assets_seen": self.assets_seen,
            "error_count": len(self.errors),
            "errors_by_code": dict(sorted(errors_by_code.items())),
        }
        if self.manifest_hash is not None:
            result["manifest_hash"] = self.manifest_hash
        return result


class LocalBankValidationError(LocalBankError):
    """The complete input is rejected; no partial bundle is returned."""

    def __init__(self, report: LocalBankReport) -> None:
        self.report = report
        super().__init__("LOCAL_BANK_INVALID", f"local bank validation failed ({len(report.errors)} errors)")


@dataclass(frozen=True, slots=True)
class LocalBankRecord:
    """Validated record retained for server-side admission."""

    source_id: str
    source_url: str
    line_number: int
    statement: str = field(repr=False)
    statement_latex: str | None = field(repr=False)
    answer: str | None = field(repr=False)
    explanation: str | None = field(repr=False)
    exam_year: int | None
    target_exam_year: int | None
    source_year: int | None
    source_year_basis: str | None
    source_created_at: str | None
    source_difficulty: int | None
    difficulty: str | None
    source_label: str | None
    source_metadata: dict[str, Any] = field(repr=False)
    upstream_record_id: str | None
    assets: tuple[dict[str, Any], ...] = field(repr=False)
    record_hash: str

    def to_payload(self) -> dict[str, Any]:
        """Return the importer payload, including server-only answer fields."""

        return {
            "source_id": self.source_id,
            "source_url": self.source_url,
            "line_number": self.line_number,
            "statement": self.statement,
            "statement_latex": self.statement_latex,
            "answer": self.answer,
            "explanation": self.explanation,
            "exam_year": self.exam_year,
            "target_exam_year": self.target_exam_year,
            "source_year": self.source_year,
            "source_year_basis": self.source_year_basis,
            "source_created_at": self.source_created_at,
            "source_difficulty": self.source_difficulty,
            "difficulty": self.difficulty,
            "source_label": self.source_label,
            "source_metadata": self.source_metadata,
            "upstream_record_id": self.upstream_record_id,
            "assets": [dict(asset) for asset in self.assets],
        }


@dataclass(frozen=True, slots=True)
class VerifiedLocalBank:
    """Immutable-in-practice bundle produced by local validation."""

    schema_version: int
    source: str
    parser_version: str
    manifest_hash: str
    records: tuple[LocalBankRecord, ...]
    report: LocalBankReport


@dataclass(frozen=True, slots=True)
class LocalBankApplyResult:
    """Safe result of one atomic manifest admission."""

    manifest_hash: str
    records_seen: int
    unique_records: int
    duplicate_records: int
    revisions_created: int
    replayed_records: int
    assets_seen: int

    def summary(self) -> dict[str, object]:
        return {
            "manifest_hash": self.manifest_hash,
            "records_seen": self.records_seen,
            "unique_records": self.unique_records,
            "duplicate_records": self.duplicate_records,
            "revisions_created": self.revisions_created,
            "replayed_records": self.replayed_records,
            "assets_seen": self.assets_seen,
        }


def _issue(code: str, row_number: int | None, source_id: str | None = None) -> LocalBankIssue:
    return LocalBankIssue(code=code, row_number=row_number, source_id=source_id)


def _bounded_string(
    value: Any,
    maximum: int,
    code: str,
    *,
    allow_empty: bool = True,
) -> tuple[str | None, str | None]:
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


def _bounded_year(value: Any) -> tuple[int | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, int) or not 2000 <= value <= 2100:
        return None, "YEAR_INVALID"
    return value, None


def _normalise_source_created_at(value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, str) or len(value) > 64:
        return None, "SOURCE_CREATED_AT_INVALID"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
    except (TypeError, ValueError):
        return None, "SOURCE_CREATED_AT_INVALID"
    return parsed.astimezone(UTC).isoformat(), None


def _metadata_bounds(value: Any) -> str | None:
    """Check metadata shape iteratively before recursive JSON operations."""

    pending: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > LOCAL_BANK_MAX_METADATA_NODES:
            return "SOURCE_METADATA_TOO_LARGE"
        if isinstance(current, dict):
            if depth >= LOCAL_BANK_MAX_METADATA_DEPTH and current:
                return "SOURCE_METADATA_TOO_DEEP"
            if len(current) > LOCAL_BANK_MAX_METADATA_NODES - nodes:
                return "SOURCE_METADATA_TOO_LARGE"
            for key, child in current.items():
                if not isinstance(key, str):
                    return "SOURCE_METADATA_INVALID"
                pending.append((child, depth + 1))
        elif isinstance(current, list):
            if depth >= LOCAL_BANK_MAX_METADATA_DEPTH and current:
                return "SOURCE_METADATA_TOO_DEEP"
            if len(current) > LOCAL_BANK_MAX_METADATA_NODES - nodes:
                return "SOURCE_METADATA_TOO_LARGE"
            pending.extend((child, depth + 1) for child in current)
        elif current is None or isinstance(current, (str, int, float, bool)):
            continue
        else:
            return "SOURCE_METADATA_INVALID"
    return None


def _copy_bounded_metadata(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    if value is None:
        return {}, None
    if not isinstance(value, dict):
        return None, "SOURCE_METADATA_INVALID"
    shape_error = _metadata_bounds(value)
    if shape_error:
        return None, shape_error
    try:
        encoded = canonical_json(value).encode("utf-8")
    except (RecursionError, TypeError, ValueError, UnicodeEncodeError):
        return None, "SOURCE_METADATA_INVALID"
    if len(encoded) > LOCAL_BANK_MAX_METADATA_BYTES:
        return None, "SOURCE_METADATA_TOO_LARGE"
    # Reparse to detach the caller's mutable input from the verified bundle.
    try:
        detached = json.loads(encoded.decode("utf-8"))
    except (RecursionError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "SOURCE_METADATA_INVALID"
    if not isinstance(detached, dict):
        return None, "SOURCE_METADATA_INVALID"
    return detached, None


def _validate_assets(
    value: Any,
    *,
    row_number: int,
    source_id: str | None,
    limits: LocalBankLimits,
) -> tuple[list[dict[str, Any]], list[LocalBankIssue]]:
    errors: list[LocalBankIssue] = []
    if value is None:
        return [], errors
    if not isinstance(value, list):
        return [], [_issue("ASSETS_INVALID", row_number, source_id)]
    if len(value) > limits.max_assets_per_record:
        return [], [_issue("ASSETS_LIMIT_EXCEEDED", row_number, source_id)]
    assets: list[dict[str, Any]] = []
    seen_filenames: set[str] = set()
    for raw_asset in value:
        if not isinstance(raw_asset, dict):
            errors.append(_issue("ASSET_INVALID", row_number, source_id))
            continue
        unknown = set(raw_asset) - _ASSET_FIELDS
        if unknown:
            errors.append(_issue("ASSET_UNKNOWN_FIELD", row_number, source_id))
            continue
        if "url" in raw_asset and "canonical_url" in raw_asset:
            errors.append(_issue("ASSET_URL_AMBIGUOUS", row_number, source_id))
            continue
        raw_url = raw_asset.get("url", raw_asset.get("canonical_url"))
        url, url_error = _bounded_string(raw_url, LOCAL_BANK_MAX_URL_BYTES, "ASSET_URL_INVALID", allow_empty=False)
        if url_error or url is None:
            errors.append(_issue(url_error or "ASSET_URL_INVALID", row_number, source_id))
            continue
        try:
            canonical_url = canonical_asset_reference(url)
        except (SourceError, ValueError):
            errors.append(_issue("ASSET_URL_INVALID", row_number, source_id))
            continue
        if canonical_url != url:
            errors.append(_issue("ASSET_URL_NOT_CANONICAL", row_number, source_id))
            continue

        raw_filename = raw_asset.get("filename", "attachment.bin")
        filename, filename_error = _bounded_string(
            raw_filename,
            LOCAL_BANK_MAX_FILENAME_BYTES,
            "ASSET_FILENAME_INVALID",
            allow_empty=False,
        )
        if filename_error or filename is None:
            errors.append(_issue(filename_error or "ASSET_FILENAME_INVALID", row_number, source_id))
            continue
        if "/" in filename or "\\" in filename or filename in {".", ".."} or ":" in filename:
            errors.append(_issue("ASSET_PATH_FORBIDDEN", row_number, source_id))
            continue
        filename = safe_filename(filename)
        filename_key = filename.casefold()
        if filename_key in seen_filenames:
            errors.append(_issue("ASSET_FILENAME_DUPLICATE", row_number, source_id))
            continue
        seen_filenames.add(filename_key)

        raw_media_type = raw_asset.get("media_type", "application/octet-stream")
        media_type, media_type_error = _bounded_string(
            raw_media_type,
            LOCAL_BANK_MAX_MEDIA_TYPE_BYTES,
            "ASSET_MEDIA_TYPE_INVALID",
            allow_empty=False,
        )
        if media_type_error or media_type is None:
            errors.append(_issue(media_type_error or "ASSET_MEDIA_TYPE_INVALID", row_number, source_id))
            continue

        role = raw_asset.get("role", "file")
        if not isinstance(role, str) or role not in {"file", "image"}:
            errors.append(_issue("ASSET_ROLE_INVALID", row_number, source_id))
            continue

        supplied_hash = raw_asset.get("content_hash")
        if supplied_hash is None:
            content_hash = sha256_text(f"metadata:{canonical_url}:{filename}")
            expected_hash: str | None = None
        elif isinstance(supplied_hash, str) and _SHA256_RE.fullmatch(supplied_hash):
            content_hash = supplied_hash
            expected_hash = supplied_hash
        else:
            errors.append(_issue("ASSET_HASH_INVALID", row_number, source_id))
            continue
        assets.append(
            {
                "url": canonical_url,
                "filename": filename,
                "media_type": media_type,
                "role": role,
                "content_hash": content_hash,
                # The existing metadata-only importer must not interpret a
                # generated placeholder as a promised byte digest.
                "expected_hash": expected_hash,
            }
        )
    return assets, errors


def _record_material(record: LocalBankRecord) -> dict[str, Any]:
    return record.to_payload()


def _validate_record(
    raw_record: Any,
    *,
    row_number: int,
    limits: LocalBankLimits,
) -> tuple[LocalBankRecord | None, list[LocalBankIssue]]:
    errors: list[LocalBankIssue] = []
    if not isinstance(raw_record, dict):
        return None, [_issue("RECORD_INVALID", row_number)]
    unknown = set(raw_record) - _RECORD_FIELDS
    if unknown:
        errors.append(_issue("RECORD_UNKNOWN_FIELD", row_number))

    raw_source_id = raw_record.get("source_id")
    source_id: str | None = None
    if not isinstance(raw_source_id, str):
        errors.append(_issue("SOURCE_ID_INVALID", row_number))
    else:
        try:
            source_id_bytes = len(raw_source_id.encode("utf-8"))
        except UnicodeEncodeError:
            source_id_bytes = LOCAL_BANK_MAX_SOURCE_ID_BYTES + 1
        if (
            source_id_bytes > LOCAL_BANK_MAX_SOURCE_ID_BYTES
            or not raw_source_id
            or _SOURCE_ID_RE.fullmatch(raw_source_id) is None
        ):
            errors.append(_issue("SOURCE_ID_INVALID", row_number))
        else:
            source_id = raw_source_id

    source_url: str | None = None
    raw_source_url = raw_record.get("source_url")
    source_url_value, source_url_error = _bounded_string(
        raw_source_url,
        LOCAL_BANK_MAX_URL_BYTES,
        "SOURCE_URL_INVALID",
        allow_empty=False,
    )
    if source_url_error or source_url_value is None:
        errors.append(_issue(source_url_error or "SOURCE_URL_INVALID", row_number, source_id))
    else:
        expected_url = f"{LOCAL_BANK_ORIGIN}/task?id={source_id}" if source_id is not None else ""
        try:
            canonical_url = canonical_source_url(source_url_value)
        except SourceError:
            canonical_url = ""
        if canonical_url != source_url_value or canonical_url != expected_url:
            errors.append(_issue("SOURCE_URL_ID_MISMATCH", row_number, source_id))
        else:
            source_url = canonical_url

    line_number = raw_record.get("line_number")
    if isinstance(line_number, bool) or not isinstance(line_number, int) or not 1 <= line_number <= 27:
        errors.append(_issue("LINE_INVALID", row_number, source_id))
        line_number_value = None
    else:
        line_number_value = line_number

    statement_value: str | None = None
    raw_statement = raw_record.get("statement")
    statement_input, statement_error = _bounded_string(
        raw_statement,
        LOCAL_BANK_MAX_STATEMENT_BYTES,
        "STATEMENT_INVALID",
        allow_empty=False,
    )
    if statement_error or statement_input is None:
        errors.append(_issue(statement_error or "STATEMENT_INVALID", row_number, source_id))
    else:
        try:
            statement_value, _raw_statement_hash = sanitize_statement(statement_input, LOCAL_BANK_MAX_STATEMENT_BYTES)
        except ValueError:
            statement_value = None
            errors.append(_issue("STATEMENT_INVALID", row_number, source_id))
        if not statement_value:
            errors.append(_issue("STATEMENT_EMPTY", row_number, source_id))

    statement_latex, statement_latex_error = _bounded_string(
        raw_record.get("statement_latex"),
        LOCAL_BANK_MAX_LATEX_BYTES,
        "STATEMENT_LATEX_INVALID",
    )
    if statement_latex_error:
        errors.append(_issue("STATEMENT_LATEX_INVALID", row_number, source_id))

    answer, answer_error = _bounded_string(raw_record.get("answer"), LOCAL_BANK_MAX_ANSWER_BYTES, "ANSWER_INVALID")
    if answer_error:
        errors.append(_issue("ANSWER_INVALID", row_number, source_id))
    explanation, explanation_error = _bounded_string(
        raw_record.get("explanation"),
        LOCAL_BANK_MAX_EXPLANATION_BYTES,
        "EXPLANATION_INVALID",
    )
    if explanation_error:
        errors.append(_issue("EXPLANATION_INVALID", row_number, source_id))

    exam_year, exam_year_error = _bounded_year(raw_record.get("exam_year"))
    if exam_year_error:
        errors.append(_issue("EXAM_YEAR_INVALID", row_number, source_id))
    target_exam_year, target_year_error = _bounded_year(raw_record.get("target_exam_year"))
    if target_year_error:
        errors.append(_issue("TARGET_YEAR_INVALID", row_number, source_id))
    source_year, source_year_error = _bounded_year(raw_record.get("source_year"))
    if source_year_error:
        errors.append(_issue("SOURCE_YEAR_INVALID", row_number, source_id))

    raw_year_basis = raw_record.get("source_year_basis")
    if raw_year_basis is not None:
        year_basis, year_basis_error = _bounded_string(
            raw_year_basis,
            64,
            "SOURCE_YEAR_BASIS_INVALID",
            allow_empty=False,
        )
        if year_basis_error:
            errors.append(_issue("SOURCE_YEAR_BASIS_INVALID", row_number, source_id))
    else:
        year_basis = None
    if source_year is not None:
        # A local declaration is kept visibly separate from official source
        # evidence, regardless of a user-supplied basis label.
        normalized_year_basis = "local-file-declared"
    else:
        normalized_year_basis = None
        if year_basis is not None:
            errors.append(_issue("SOURCE_YEAR_BASIS_INVALID", row_number, source_id))

    source_created_at, source_created_at_error = _normalise_source_created_at(raw_record.get("source_created_at"))
    if source_created_at_error:
        errors.append(_issue(source_created_at_error, row_number, source_id))

    source_difficulty = raw_record.get("source_difficulty")
    difficulty = raw_record.get("difficulty")
    if source_difficulty is None and difficulty is None:
        source_difficulty_value = None
        difficulty_value = None
    elif (
        isinstance(source_difficulty, bool)
        or not isinstance(source_difficulty, int)
        or source_difficulty not in _DIFFICULTY_BY_SOURCE
        or not isinstance(difficulty, str)
        or difficulty != _DIFFICULTY_BY_SOURCE.get(source_difficulty)
    ):
        errors.append(_issue("SOURCE_DIFFICULTY_INVALID", row_number, source_id))
        source_difficulty_value = None
        difficulty_value = None
    else:
        source_difficulty_value = source_difficulty
        difficulty_value = difficulty

    source_label, source_label_error = _bounded_string(
        raw_record.get("source_label"),
        LOCAL_BANK_MAX_LABEL_BYTES,
        "SOURCE_LABEL_INVALID",
    )
    if source_label_error:
        errors.append(_issue("SOURCE_LABEL_INVALID", row_number, source_id))

    source_metadata, metadata_error = _copy_bounded_metadata(raw_record.get("source_metadata"))
    if metadata_error or source_metadata is None:
        errors.append(_issue(metadata_error or "SOURCE_METADATA_INVALID", row_number, source_id))
        source_metadata = {}

    upstream_record_id, upstream_id_error = _bounded_string(
        raw_record.get("upstream_record_id"),
        LOCAL_BANK_MAX_SOURCE_ID_BYTES,
        "UPSTREAM_RECORD_ID_INVALID",
    )
    if upstream_id_error:
        errors.append(_issue("UPSTREAM_RECORD_ID_INVALID", row_number, source_id))

    assets, asset_errors = _validate_assets(
        raw_record.get("assets"),
        row_number=row_number,
        source_id=source_id,
        limits=limits,
    )
    errors.extend(asset_errors)

    if errors or source_id is None or source_url is None or line_number_value is None or statement_value is None:
        return None, errors

    record = LocalBankRecord(
        source_id=source_id,
        source_url=source_url,
        line_number=line_number_value,
        statement=statement_value,
        statement_latex=statement_latex,
        answer=answer,
        explanation=explanation,
        exam_year=exam_year,
        target_exam_year=target_exam_year,
        source_year=source_year,
        source_year_basis=normalized_year_basis,
        source_created_at=source_created_at,
        source_difficulty=source_difficulty_value,
        difficulty=difficulty_value,
        source_label=source_label,
        source_metadata=source_metadata,
        upstream_record_id=upstream_record_id,
        assets=tuple(assets),
        record_hash="",
    )
    record_hash = sha256_json(_record_material(record))
    return (
        LocalBankRecord(
            source_id=record.source_id,
            source_url=record.source_url,
            line_number=record.line_number,
            statement=record.statement,
            statement_latex=record.statement_latex,
            answer=record.answer,
            explanation=record.explanation,
            exam_year=record.exam_year,
            target_exam_year=record.target_exam_year,
            source_year=record.source_year,
            source_year_basis=record.source_year_basis,
            source_created_at=record.source_created_at,
            source_difficulty=record.source_difficulty,
            difficulty=record.difficulty,
            source_label=record.source_label,
            source_metadata=record.source_metadata,
            upstream_record_id=record.upstream_record_id,
            assets=record.assets,
            record_hash=record_hash,
        ),
        [],
    )


def _parse_json(value: bytes) -> Any:
    def reject_constant(token: str) -> NoReturn:
        raise ValueError(f"unsupported JSON constant {token}")

    try:
        return json.loads(value.decode("utf-8"), parse_constant=reject_constant)
    except RecursionError as exc:
        # Keep parser failures count-only at the public validation boundary.
        raise ValueError("JSON nesting exceeds the bounded parser limit") from exc


def _extract_rows(value: Any, *, limits: LocalBankLimits) -> tuple[list[Any], list[LocalBankIssue]]:
    if not isinstance(value, dict):
        return [], [_issue("ENVELOPE_INVALID", None)]
    envelope_keys = set(value)
    missing = {"schema_version", "source", "tasks"} - envelope_keys
    unknown = envelope_keys - {"schema_version", "source", "tasks"}
    errors: list[LocalBankIssue] = []
    if missing or unknown:
        errors.append(_issue("ENVELOPE_FIELDS_INVALID", None))
    if value.get("schema_version") != LOCAL_BANK_SCHEMA_VERSION or isinstance(value.get("schema_version"), bool):
        errors.append(_issue("SCHEMA_VERSION_INVALID", None))
    if value.get("source") != LOCAL_BANK_SOURCE:
        errors.append(_issue("SOURCE_INVALID", None))
    tasks = value.get("tasks")
    if not isinstance(tasks, list):
        errors.append(_issue("TASKS_INVALID", None))
        return [], errors
    if len(tasks) > limits.max_records:
        errors.append(_issue("MAX_RECORDS_EXCEEDED", None))
    return tasks[: limits.max_records], errors


def _parse_jsonl(raw_bytes: bytes, *, limits: LocalBankLimits) -> tuple[list[Any], list[LocalBankIssue]]:
    rows: list[Any] = []
    errors: list[LocalBankIssue] = []
    for line_number, raw_line in enumerate(raw_bytes.splitlines(), start=1):
        if not raw_line.strip():
            continue
        if len(raw_line) > limits.max_record_bytes:
            errors.append(_issue("RECORD_TOO_LARGE", line_number))
            continue
        try:
            rows.append(_parse_json(raw_line))
        except (RecursionError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            errors.append(_issue("JSONL_ROW_INVALID", line_number))
        if len(rows) > limits.max_records:
            errors.append(_issue("MAX_RECORDS_EXCEEDED", line_number))
            rows = rows[: limits.max_records]
            break
    if not rows and not errors:
        errors.append(_issue("INPUT_EMPTY", None))
    return rows, errors


def _read_rows(path: Path, *, limits: LocalBankLimits) -> tuple[list[Any], list[LocalBankIssue]]:
    try:
        if not path.is_file():
            return [], [_issue("INPUT_FILE_INVALID", None)]
        if path.stat().st_size > limits.max_file_bytes:
            return [], [_issue("INPUT_FILE_TOO_LARGE", None)]
        raw_bytes = path.read_bytes()
    except OSError:
        return [], [_issue("INPUT_FILE_INVALID", None)]
    if not raw_bytes:
        return [], [_issue("INPUT_EMPTY", None)]
    try:
        parsed = _parse_json(raw_bytes)
    except (RecursionError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return _parse_jsonl(raw_bytes, limits=limits)
    if isinstance(parsed, dict) and {"schema_version", "source", "tasks"} & set(parsed):
        return _extract_rows(parsed, limits=limits)
    if isinstance(parsed, dict):
        # A one-line JSONL file is valid and is also valid JSON as a single
        # object. Treat it as one row when it is not an envelope.
        return [parsed], []
    return [], [_issue("ENVELOPE_INVALID", None)]


def _manifest_hash(records: tuple[LocalBankRecord, ...]) -> str:
    return sha256_json(
        {
            "schema_version": LOCAL_BANK_SCHEMA_VERSION,
            "source": LOCAL_BANK_SOURCE,
            "parser_version": LOCAL_BANK_PARSER_VERSION,
            "records": [_record_material(record) for record in records],
        }
    )


def validate_bundle(value: Any, *, limits: LocalBankLimits | None = None) -> VerifiedLocalBank:
    """Validate an in-memory envelope using the same bounded rules as files."""

    effective_limits = limits or LocalBankLimits()
    rows, parse_errors = _extract_rows(value, limits=effective_limits)
    return _build_bundle(rows, parse_errors, limits=effective_limits)


def _build_bundle(
    rows: list[Any],
    parse_errors: list[LocalBankIssue],
    *,
    limits: LocalBankLimits,
) -> VerifiedLocalBank:
    errors = list(parse_errors)
    records_by_id: dict[str, LocalBankRecord] = {}
    duplicate_records = 0
    assets_seen = 0
    for row_number, raw_record in enumerate(rows, start=1):
        if isinstance(raw_record, dict) and "source_metadata" in raw_record:
            metadata_error = _metadata_bounds(raw_record.get("source_metadata"))
            if metadata_error:
                errors.append(_issue(metadata_error, row_number))
                continue
        try:
            encoded_record = canonical_json(raw_record).encode("utf-8")
        except RecursionError:
            errors.append(_issue("RECORD_TOO_DEEP", row_number))
            continue
        except (TypeError, ValueError, UnicodeEncodeError):
            encoded_record = b""
        if len(encoded_record) > limits.max_record_bytes:
            errors.append(_issue("RECORD_TOO_LARGE", row_number))
            continue
        record, record_errors = _validate_record(raw_record, row_number=row_number, limits=limits)
        errors.extend(record_errors)
        if record is None:
            continue
        assets_seen += len(record.assets)
        previous = records_by_id.get(record.source_id)
        if previous is None:
            records_by_id[record.source_id] = record
        elif previous.record_hash == record.record_hash:
            duplicate_records += 1
        else:
            errors.append(_issue("DUPLICATE_SOURCE_ID_CONFLICT", row_number, record.source_id))
    records = tuple(sorted(records_by_id.values(), key=lambda item: (item.line_number, item.source_id)))
    manifest_hash: str | None = None
    if not errors:
        try:
            manifest_hash = _manifest_hash(records)
        except (RecursionError, TypeError, ValueError, UnicodeEncodeError):
            errors.append(_issue("MANIFEST_INVALID", None))
    report = LocalBankReport(
        manifest_hash=manifest_hash,
        records_seen=len(rows),
        unique_records=len(records),
        duplicate_records=duplicate_records,
        assets_seen=assets_seen,
        errors=tuple(errors),
    )
    if not report.ok:
        raise LocalBankValidationError(report)
    return VerifiedLocalBank(
        schema_version=LOCAL_BANK_SCHEMA_VERSION,
        source=LOCAL_BANK_SOURCE,
        parser_version=LOCAL_BANK_PARSER_VERSION,
        manifest_hash=report.manifest_hash or "",
        records=records,
        report=report,
    )


def load_local_bank(path: str | Path, *, limits: LocalBankLimits | None = None) -> VerifiedLocalBank:
    """Read one explicit local file and return a verified bundle.

    This function performs no environment lookup, database access, asset-byte
    read, cache access, or network request.
    """

    effective_limits = limits or LocalBankLimits()
    rows, parse_errors = _read_rows(Path(path), limits=effective_limits)
    return _build_bundle(rows, parse_errors, limits=effective_limits)


def local_record_idempotency_key(
    source_hash: str,
    *,
    parser_version: str = LOCAL_BANK_PARSER_VERSION,
    target_exam_year: int | None = None,
) -> str:
    """Derive a stable per-record key without colliding on changed payloads."""

    digest = sha256_json(
        {
            "source_hash": source_hash,
            "parser_version": parser_version,
            "target_exam_year": target_exam_year,
        }
    )
    return f"{parser_version}:{digest}"


def validate_target_exam_year(value: Any) -> int | None:
    """Validate an owner-selected preparation label without changing provenance."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 2000 <= value <= 2100:
        raise LocalBankError("TARGET_YEAR_INVALID", "target_exam_year must be between 2000 and 2100")
    return value


def apply_verified_bundle(
    session: Session,
    bundle: VerifiedLocalBank,
    *,
    owner_id: str,
    settings: Settings,
    rights_status: str,
    target_exam_year: int | None = None,
) -> LocalBankApplyResult:
    """Atomically admit a previously verified bundle through source_catalog."""

    if not isinstance(bundle, VerifiedLocalBank) or not bundle.report.ok:
        raise LocalBankError("BUNDLE_NOT_VERIFIED", "apply requires a verified local bundle")
    if bundle.parser_version != LOCAL_BANK_PARSER_VERSION or bundle.source != LOCAL_BANK_SOURCE:
        raise LocalBankError("BUNDLE_VERSION_INVALID", "bundle parser or source is not supported")
    if _manifest_hash(bundle.records) != bundle.manifest_hash:
        raise LocalBankError("BUNDLE_TAMPERED", "verified bundle manifest changed")
    if not isinstance(owner_id, str) or not owner_id or len(owner_id.encode("utf-8")) > 64:
        raise LocalBankError("OWNER_INVALID", "owner_id is invalid")
    if rights_status not in LOCAL_BANK_ALLOWED_RIGHTS:
        raise LocalBankError("RIGHTS_INVALID", "rights_status must be explicitly authorized")
    if settings.source_mode != "manual" or settings.source_network_enabled is not False:
        raise LocalBankError("SOURCE_DISABLED", "local-file apply requires manual mode with network disabled")
    effective_override = validate_target_exam_year(target_exam_year)

    transaction = session.begin_nested() if session.in_transaction() else session.begin()
    revisions_created = 0
    replayed_records = 0
    assets_seen = 0
    try:
        for record in bundle.records:
            effective_target_exam_year = (
                effective_override if effective_override is not None else record.target_exam_year
            )
            _task, _revision, assets, duplicate = import_fixture(
                session,
                owner_id=owner_id,
                settings=settings,
                payload=record.to_payload(),
                idempotency_key=local_record_idempotency_key(
                    record.record_hash,
                    parser_version=LOCAL_BANK_PARSER_VERSION,
                    target_exam_year=effective_target_exam_year,
                ),
                admission_mode="local-file",
                admitted_rights_status=rights_status,
                parser_version=LOCAL_BANK_PARSER_VERSION,
                target_exam_year=effective_target_exam_year,
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
    return LocalBankApplyResult(
        manifest_hash=bundle.manifest_hash,
        records_seen=bundle.report.records_seen,
        unique_records=len(bundle.records),
        duplicate_records=bundle.report.duplicate_records,
        revisions_created=revisions_created,
        replayed_records=replayed_records,
        assets_seen=assets_seen,
    )


__all__ = [
    "LOCAL_BANK_PARSER_VERSION",
    "LOCAL_BANK_SCHEMA_VERSION",
    "LocalBankApplyResult",
    "LocalBankError",
    "LocalBankIssue",
    "LocalBankLimits",
    "LocalBankRecord",
    "LocalBankReport",
    "LocalBankValidationError",
    "VerifiedLocalBank",
    "apply_verified_bundle",
    "load_local_bank",
    "local_record_idempotency_key",
    "validate_target_exam_year",
    "validate_bundle",
]

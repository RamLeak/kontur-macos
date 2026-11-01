"""КЕГЭ-only source catalog and deterministic local fixture importer."""

from __future__ import annotations

import base64
import binascii
import posixpath
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from .assets import AssetError, AssetStore, safe_filename
from .config import Settings
from .events import append_event
from .inline_images import parse_inline_image
from .models import (
    Asset,
    SourcePolicy,
    SourceSnapshot,
    SyncRun,
    Task,
    TaskAsset,
    TaskRevision,
    new_id,
    utcnow,
)
from .util import bounded_json, bounded_text, sha256_bytes, sha256_json, sha256_text

SOURCE_NAME = "kompege"
SOURCE_ORIGIN = "https://kompege.ru"
SOURCE_POLICY_VERSION = "source-policy-v1"
PARSER_VERSION = "fixture-v1"
ALLOWED_RIGHTS = {"review_required", "private_authorized", "public_authorized", "blocked"}
ALLOWED_HOSTS = {"kompege.ru", "www.kompege.ru"}
_LOCAL_FILE_ASSET_FIELDS = frozenset(
    {"url", "canonical_url", "filename", "media_type", "role", "content_hash", "expected_hash"}
)


def scoped_sync_key(owner_id: str, idempotency_key: str) -> str:
    """Return a stable DB key that cannot collide across owners.

    ``sync_runs.idempotency_key`` had a global unique constraint in v1. Keeping
    that legacy constraint while hashing the owner/key pair provides safe
    upgrades without a destructive SQLite table rebuild. The caller-facing key
    remains in snapshot/job metadata for auditability.
    """

    return sha256_text(f"{owner_id}\x00{idempotency_key}")


def _find_sync_run(session: Session, owner_id: str, idempotency_key: str) -> SyncRun | None:
    stored_key = scoped_sync_key(owner_id, idempotency_key)
    return session.scalar(
        select(SyncRun).where(
            or_(
                and_(SyncRun.owner_id == owner_id, SyncRun.idempotency_key == stored_key),
                # v1-v3 rows used the raw key and have no owner. Do not discard
                # those rows when replaying a fixture after migration.
                and_(SyncRun.owner_id.is_(None), SyncRun.idempotency_key == idempotency_key),
                and_(SyncRun.owner_id == owner_id, SyncRun.idempotency_key == idempotency_key),
            )
        )
    )


def stored_import_result(session: Session, sync: SyncRun) -> tuple[Task, TaskRevision, list[Asset], bool] | None:
    """Rehydrate an admitted task without another upstream request."""

    counters = sync.counters if isinstance(sync.counters, dict) else {}
    task_id = counters.get("task_id")
    revision_id = counters.get("revision_id")
    if not isinstance(task_id, str) or not isinstance(revision_id, str):
        return None
    task = session.get(Task, task_id)
    revision = session.get(TaskRevision, revision_id)
    if task is None or revision is None:
        return None
    assets = list(
        session.scalars(
            select(Asset)
            .join(TaskAsset, TaskAsset.asset_id == Asset.id)
            .where(TaskAsset.revision_id == revision.id)
            .order_by(TaskAsset.position, Asset.id)
        )
    )
    return task, revision, assets, True


class SourceError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class FixtureAsset:
    canonical_url: str
    filename: str
    media_type: str
    data: bytes | None
    content_hash: str
    role: str = "file"
    expected_hash: str | None = None


class _PlainTextStatementParser(HTMLParser):
    """Turn source markup into inert text before it crosses the API boundary."""

    _BLOCKS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "dl",
        "dt",
        "dd",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0
        self._image_index = 0
        self._row_cell_count = 0

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in {"script", "style", "iframe", "object", "embed", "noscript"}:
            self._ignored_depth += 1
        elif self._ignored_depth == 0 and tag in {"sup", "sub"}:
            self.parts.append("^(" if tag == "sup" else "_(")
        elif self._ignored_depth == 0 and tag == "img":
            self._image_index += 1
            self.parts.append(f"\n[Изображение {self._image_index}]\n")
        elif self._ignored_depth == 0 and tag in {"td", "th"}:
            if self._row_cell_count:
                self.parts.append("\t")
            self._row_cell_count += 1
        elif self._ignored_depth == 0 and tag == "tr":
            self._row_cell_count = 0
            self.parts.append("\n")
        elif self._ignored_depth == 0 and tag == "li":
            self.parts.append("\n• ")
        elif self._ignored_depth == 0 and tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if self._ignored_depth != 0:
            return
        if tag == "img":
            self._image_index += 1
            self.parts.append(f"\n[Изображение {self._image_index}]\n")
        elif tag in {"td", "th"}:
            if self._row_cell_count:
                self.parts.append("\t")
            self._row_cell_count += 1
        elif tag == "tr":
            self._row_cell_count = 0
            self.parts.append("\n")
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"script", "style", "iframe", "object", "embed", "noscript"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
        elif self._ignored_depth == 0 and tag in {"sup", "sub"}:
            self.parts.append(")")
        elif self._ignored_depth == 0 and tag == "tr":
            self.parts.append("\n")
            self._row_cell_count = 0
        elif self._ignored_depth == 0 and tag == "li":
            self.parts.append("\n")
        elif self._ignored_depth == 0 and tag in {"td", "th"}:
            pass
        elif self._ignored_depth == 0 and tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            self.parts.append(data)


def normalise_table_manifest(value: Any, maximum: int = 20_000) -> dict[str, Any] | None:
    """Keep an upstream table as a small inert matrix, never as HTML."""

    if value is None:
        return None
    if isinstance(value, dict):
        # The live KЕГЭ number endpoint emits an empty object when there is no
        # separate table manifest. The complete condition remains in ``text``
        # and is sanitized by the statement parser. Treat only a truly empty
        # object as absence; unknown non-empty shapes still fail closed below.
        if not value:
            return None
        # The observed КЕГЭ envelope uses ``table`` as a dimensions object;
        # cell content remains in the HTML text and is rendered by the inert
        # parser below. Preserve only bounded numeric provenance here.
        if (
            isinstance(value.get("cols"), int)
            and not isinstance(value.get("cols"), bool)
            and isinstance(value.get("rows"), int)
            and not isinstance(value.get("rows"), bool)
        ):
            cols = int(value["cols"])
            row_count = int(value["rows"])
            if not 0 <= cols <= 50 or not 0 <= row_count <= 100:
                raise SourceError("SOURCE_SHAPE_UNSUPPORTED", "source table dimensions exceed the bounded limit")
            return {"cols": cols, "rows": row_count}
        raw_headers = value.get("headers") or value.get("columns") or []
        raw_rows = value.get("rows") or value.get("data")
        if raw_rows is None:
            raise SourceError("SOURCE_RESPONSE_INVALID", "source table object lacks rows")
    elif isinstance(value, list):
        raw_headers = []
        raw_rows = value
    else:
        raise SourceError("SOURCE_RESPONSE_INVALID", "source table must be an array or object")
    if not isinstance(raw_headers, list) or not isinstance(raw_rows, list):
        raise SourceError("SOURCE_RESPONSE_INVALID", "source table headers and rows must be arrays")
    if len(raw_headers) > 50 or len(raw_rows) > 100:
        raise SourceError("SOURCE_SHAPE_UNSUPPORTED", "source table exceeds the bounded row/column limit")

    def cell(raw: Any) -> str:
        if raw is None:
            return ""
        if isinstance(raw, (dict, list, tuple, set)):
            raise SourceError("SOURCE_RESPONSE_INVALID", "source table cell must be scalar text")
        rendered, _raw_hash = sanitize_statement(str(raw), 2_000)
        return rendered

    headers = [cell(item) for item in raw_headers]
    normalized_rows: list[list[str]] = []
    total = sum(len(item) for item in headers)
    for raw_row in raw_rows:
        if isinstance(raw_row, dict):
            raw_values = [raw_row[key] for key in sorted(raw_row)]
        elif isinstance(raw_row, list):
            raw_values = raw_row
        else:
            raise SourceError("SOURCE_RESPONSE_INVALID", "source table row must be an array or object")
        if len(raw_values) > 50:
            raise SourceError("SOURCE_SHAPE_UNSUPPORTED", "source table row exceeds the column limit")
        normalized_row = [cell(item) for item in raw_values]
        total += sum(len(item) for item in normalized_row)
        if total > maximum:
            raise SourceError("SOURCE_RESPONSE_TOO_LARGE", "source table exceeds the bounded text limit")
        normalized_rows.append(normalized_row)
    return {"headers": headers, "rows": normalized_rows} if headers else {"rows": normalized_rows}


def sanitize_statement(value: str, maximum: int) -> tuple[str, str]:
    raw = bounded_text(value, maximum, "source statement")
    parser = _PlainTextStatementParser()
    parser.feed(raw)
    parser.close()
    text = "".join(parser.parts)
    # Keep table cell separators as structure.  ``str.split()`` without an
    # explicit separator would turn tabs into spaces and make a source table
    # unreadable even though the markup itself was safely removed.
    lines = ["\t".join(" ".join(cell.split()) for cell in line.split("\t")) for line in text.splitlines()]
    sanitized = bounded_text("\n".join(line for line in lines if line), maximum, "source statement")
    return sanitized, sha256_text(raw)


def canonical_source_url(raw_url: str, *, attachment: bool = False, api_endpoint: bool = False) -> str:
    """Normalize and validate one KЕГЭ URL without performing network access."""

    if not isinstance(raw_url, str) or len(raw_url) > 2048:
        raise SourceError("SOURCE_URL_INVALID", "source_url must be a bounded string")
    try:
        parsed = urlsplit(raw_url.strip())
    except ValueError as exc:
        raise SourceError("SOURCE_URL_INVALID", "invalid source URL") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https" or host not in ALLOWED_HOSTS:
        raise SourceError("SOURCE_HOST_BLOCKED", "source URL must use the approved KЕГЭ HTTPS host")
    if parsed.username or parsed.password or parsed.port:
        raise SourceError("SOURCE_URL_INVALID", "credentials and explicit ports are not allowed")
    path = posixpath.normpath(parsed.path or "/")
    if not path.startswith("/") or ".." in path.split("/"):
        raise SourceError("SOURCE_URL_INVALID", "invalid source path")
    if attachment:
        if not (
            (path.startswith("/files/") and path != "/files/") or (path.startswith("/images/") and path != "/images/")
        ):
            raise SourceError("SOURCE_PATH_BLOCKED", "asset must be under /files/ or /images/")
    elif not (
        path == "/task"
        or path.startswith("/task/")
        or path == "/variant"
        or path.startswith("/variant/")
        or (
            api_endpoint
            and (
                path == "/api/v1/task"
                or path.startswith("/api/v1/task/")
                or path == "/api/v1/variant/random"
            )
        )
    ):
        raise SourceError("SOURCE_PATH_BLOCKED", "task URL must be a canonical КЕГЭ task or variant page")
    if api_endpoint and path.startswith("/api/v1/task/number/"):
        raw_number = path.removeprefix("/api/v1/task/number/")
        if not raw_number.isdecimal() or not 1 <= int(raw_number) <= 27:
            raise SourceError("SOURCE_PATH_BLOCKED", "task number endpoint must be in the range 1..27")
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit(("https", "kompege.ru", path, query, ""))


def source_url_allowed(raw_url: str) -> bool:
    try:
        canonical_source_url(raw_url)
    except SourceError:
        return False
    return True


def canonical_asset_reference(raw_url: str) -> str:
    """Validate either a downloadable URL or a pinned official JSON image locator."""
    try:
        if parse_inline_image(raw_url) is not None:
            return raw_url
    except ValueError as exc:
        raise SourceError("SOURCE_PATH_BLOCKED", "invalid inline image locator") from exc
    return canonical_source_url(raw_url, attachment=True)


def _decode_fixture_bytes(value: Any, maximum: int) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SourceError("ASSET_DATA_INVALID", "content_base64 must be a string")
    try:
        data = base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
        raise SourceError("ASSET_DATA_INVALID", "content_base64 is not valid base64") from exc
    if len(data) > maximum:
        raise SourceError("ASSET_TOO_LARGE", "fixture asset exceeds configured byte limit")
    return data


def _fixture_assets(raw_assets: Any, settings: Settings) -> list[FixtureAsset]:
    if raw_assets is None:
        return []
    if not isinstance(raw_assets, list) or len(raw_assets) > 20:
        raise SourceError("ASSET_MANIFEST_INVALID", "assets must be a list of at most 20 items")
    assets: list[FixtureAsset] = []
    seen_names: set[str] = set()
    for raw in raw_assets:
        if not isinstance(raw, dict):
            raise SourceError("ASSET_MANIFEST_INVALID", "each asset must be an object")
        url = canonical_asset_reference(str(raw.get("url", "")))
        filename = safe_filename(str(raw.get("filename") or raw.get("name") or "attachment.bin"))
        filename_key = filename.casefold()
        if filename_key in seen_names:
            raise SourceError("ASSET_FILENAME_DUPLICATE", "asset filenames must be unique after sanitization")
        seen_names.add(filename_key)
        media_type = str(raw.get("media_type") or "application/octet-stream")[:255]
        data = _decode_fixture_bytes(raw.get("content_base64"), settings.max_asset_bytes)
        supplied_hash = str(raw.get("content_hash") or raw.get("sha256") or "")
        expected_hash_value = raw.get("expected_hash") if "expected_hash" in raw else supplied_hash
        expected_hash = str(expected_hash_value) if expected_hash_value else None
        digest = sha256_bytes(data) if data is not None else supplied_hash
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest.lower()):
            if data is None:
                raise SourceError("ASSET_HASH_REQUIRED", "metadata-only fixture assets require content_hash")
        role = str(raw.get("role") or "file")
        if role not in {"file", "image"}:
            raise SourceError("ASSET_MANIFEST_INVALID", "asset role must be file or image")
        assets.append(
            FixtureAsset(
                url,
                filename,
                media_type,
                data,
                digest,
                role,
                # Preserve an upstream supplied digest even when fixture bytes
                # are available; the importer verifies it before promotion and
                # uses it as the source identity discriminator.
                expected_hash=expected_hash,
            )
        )
    return assets


def _validate_local_file_asset_manifest(raw_assets: Any) -> None:
    """Reject byte-bearing or path-bearing assets at the admission seam.

    ``local-file`` is a metadata-only mode.  This check intentionally runs
    before policy/record writes and remains independent of the higher-level
    local-bank validator so direct callers cannot smuggle bytes into the
    existing fixture importer.
    """

    if raw_assets is None:
        return
    if not isinstance(raw_assets, list) or len(raw_assets) > 20:
        raise SourceError("ASSET_MANIFEST_INVALID", "local-file assets must be a bounded list")
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, dict):
            raise SourceError("ASSET_MANIFEST_INVALID", "local-file assets must be objects")
        if {"content_base64", "data"} & set(raw_asset):
            raise SourceError("ASSET_DATA_INVALID", "local-file assets cannot carry bytes")
        if set(raw_asset) - _LOCAL_FILE_ASSET_FIELDS:
            raise SourceError("ASSET_MANIFEST_INVALID", "local-file assets contain unsupported fields")


def _subtask_manifest(raw_subtasks: Any, settings: Settings) -> list[dict[str, Any]]:
    """Normalize bounded 19-21-style linked subtasks without raw HTML."""

    if raw_subtasks is None:
        return []
    if not isinstance(raw_subtasks, list) or len(raw_subtasks) > 4:
        raise SourceError("SOURCE_SHAPE_UNSUPPORTED", "subTask must contain at most four linked entries")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_subtasks):
        if not isinstance(raw, dict):
            raise SourceError("SOURCE_RESPONSE_INVALID", "subTask entry is invalid")
        source_id = bounded_text(
            str(raw.get("source_id") or raw.get("taskId") or raw.get("id") or f"sub:{index}"), 255, "subtask source_id"
        )
        line_value = raw.get("line_number") or raw.get("number") or (19 + index)
        try:
            line_number = int(str(line_value))
        except (TypeError, ValueError) as exc:
            raise SourceError("SOURCE_RESPONSE_INVALID", "subTask line number is invalid") from exc
        if not 1 <= line_number <= 27:
            raise SourceError("SOURCE_RESPONSE_INVALID", "subTask line number is outside 1..27")
        statement, raw_hash = sanitize_statement(
            str(raw.get("statement") or raw.get("text") or raw.get("html") or ""),
            settings.max_message_bytes,
        )
        if not statement:
            raise SourceError("SOURCE_RESPONSE_INVALID", "subTask statement is empty")
        raw_assets = raw.get("assets") or raw.get("files") or []
        # Subtask assets are metadata-only manifest entries. They are deliberately
        # not merged into the parent TaskAsset relation until a future grouped
        # runner contract is approved.
        metadata_assets: list[dict[str, Any]] = []
        if not isinstance(raw_assets, list) or len(raw_assets) > 20:
            raise SourceError("SOURCE_RESPONSE_INVALID", "subTask asset manifest is invalid")
        for asset in raw_assets:
            if not isinstance(asset, dict):
                raise SourceError("SOURCE_RESPONSE_INVALID", "subTask asset entry is invalid")
            url = canonical_asset_reference(str(asset.get("url") or asset.get("path") or ""))
            filename = safe_filename(str(asset.get("filename") or asset.get("name") or "attachment.bin"))
            supplied_hash = str(asset.get("content_hash") or asset.get("sha256") or "")
            digest = supplied_hash or sha256_text(f"metadata:{url}:{filename}")
            if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest.lower()):
                raise SourceError("ASSET_HASH_INVALID", "subTask asset hash is invalid")
            metadata_assets.append(
                {
                    "url": url,
                    "filename": filename,
                    "media_type": str(asset.get("media_type") or "application/octet-stream")[:255],
                    "content_hash": digest,
                    "expected_hash": supplied_hash or None,
                    "role": "image" if str(asset.get("role") or "file") == "image" else "file",
                }
            )
        result.append(
            {
                "source_id": source_id,
                "upstream_record_id": str(raw.get("id")) if raw.get("id") is not None else None,
                "line_number": line_number,
                "statement": statement,
                "raw_statement_hash": raw_hash,
                "assets": metadata_assets,
                "group_index": index,
            }
        )
    return bounded_json(result, 200_000, "subtask_manifest")


def ensure_source_policy(session: Session, settings: Settings) -> SourcePolicy:
    policy = session.scalar(select(SourcePolicy).where(SourcePolicy.name == SOURCE_NAME))
    if policy is None:
        policy = SourcePolicy(
            id=new_id(),
            name=SOURCE_NAME,
            canonical_origin=SOURCE_ORIGIN,
            mode=settings.source_mode,
            permission_status=(
                settings.source_permission_status
                if settings.source_permission_status in ALLOWED_RIGHTS
                else "review_required"
            ),
            enabled_operations=[],
            policy_version=SOURCE_POLICY_VERSION,
            updated_at=utcnow(),
        )
        session.add(policy)
        session.flush()
    elif policy.mode != settings.source_mode:
        # Runtime configuration is authoritative for the admission mode, but
        # rights are deliberately not upgraded from a fixture/request payload.
        policy.mode = settings.source_mode
        policy.updated_at = utcnow()
        session.flush()
    return policy


def _asset_identity_candidates(session: Session, fixture: FixtureAsset) -> list[Asset]:
    """Find assets by immutable source identity, never by mutable cache hash.

    ``content_hash`` is the bytes hash once a metadata-only asset is promoted,
    so it cannot be the identity of a source attachment.  The source URL and
    sanitized filename are stable provenance fields.  A supplied expected hash
    is an additional version discriminator: a conflicting upstream digest gets
    a new asset instead of mutating the old one.  Rows created by an older
    implementation may have copied the actual hash into
    ``expected_content_hash`` after promotion; the ready/hash-verified
    compatibility branch below safely reuses only such rows.
    """

    candidates = list(
        session.scalars(
            select(Asset)
            .where(
                Asset.source_name == SOURCE_NAME,
                Asset.canonical_url == fixture.canonical_url,
                Asset.filename == fixture.filename,
            )
            .order_by(Asset.created_at, Asset.id)
        )
    )
    if fixture.expected_hash:
        for candidate in candidates:
            if candidate.expected_content_hash == fixture.expected_hash:
                return [candidate]
            if (
                candidate.expected_content_hash is None
                and candidate.cache_state == "ready"
                and candidate.hash_verified
                and candidate.content_hash == fixture.expected_hash
            ):
                return [candidate]
        return []

    if fixture.data is not None:
        # Inline fixture bytes are already authoritative.  Reuse only when the
        # same source identity has the same verified/source digest; a stale
        # metadata placeholder must not suppress a changed byte payload.
        return [
            candidate
            for candidate in candidates
            if candidate.content_hash == fixture.content_hash
            and (candidate.expected_content_hash is None or candidate.hash_verified)
        ][:1]

    for candidate in candidates:
        if candidate.expected_content_hash is None:
            return [candidate]
        # A device delivery may have pinned an otherwise metadata-only row
        # without promoting bytes to the server cache.  Reuse that same source
        # identity on a later metadata refresh when the immutable placeholder
        # still agrees; the observed pin is never replaced by the refresh.
        if (
            candidate.cache_state == "metadata_only"
            and candidate.fetched_at is not None
            and candidate.content_hash == fixture.content_hash
        ):
            return [candidate]
        # Backward compatibility for rows promoted before expected-hash
        # provenance was kept separate from the actual bytes hash.
        if (
            candidate.cache_state == "ready"
            and candidate.hash_verified
            and candidate.content_hash == candidate.expected_content_hash
        ):
            return [candidate]
    return []


def _pending_metadata_asset(session: Session, fixture: FixtureAsset) -> Asset | None:
    """Find one metadata placeholder eligible for byte promotion.

    A metadata-only row has a stable URL/filename identity.  A supplied
    expected digest must still agree when the placeholder recorded one; a
    generated local placeholder has no expected digest and can be promoted by
    a later verified byte-bearing admission.  Ready or failed rows are never
    selected here, so a conflicting source version keeps its own identity.
    """

    candidates = list(
        session.scalars(
            select(Asset)
            .where(
                Asset.source_name == SOURCE_NAME,
                Asset.canonical_url == fixture.canonical_url,
                Asset.filename == fixture.filename,
                Asset.cache_state == "metadata_only",
            )
            .order_by(Asset.created_at, Asset.id)
        )
    )
    for candidate in candidates:
        if candidate.expected_content_hash is None:
            return candidate
        if fixture.expected_hash and candidate.expected_content_hash == fixture.expected_hash:
            return candidate
        if fixture.expected_hash is None and candidate.expected_content_hash == fixture.content_hash:
            return candidate
    return None


def _promote_metadata_asset(
    session: Session,
    store: AssetStore,
    asset: Asset,
    fixture: FixtureAsset,
    *,
    max_cache_bytes: int,
) -> Asset:
    """Atomically promote a pending placeholder while preserving its ID."""

    data = fixture.data
    if data is None:
        return asset
    digest = sha256_bytes(data)
    if fixture.expected_hash is not None and fixture.expected_hash != digest:
        raise SourceError("ASSET_HASH_MISMATCH", "asset content does not match the source digest")
    ready_total = int(
        session.scalar(
            select(func.coalesce(func.sum(Asset.size_bytes), 0)).where(
                Asset.cache_state == "ready", Asset.id != asset.id
            )
        )
        or 0
    )
    if ready_total + len(data) > max_cache_bytes:
        raise SourceError("ASSET_CACHE_QUOTA", "asset cache quota would be exceeded")
    try:
        stored = store.store_bytes(
            canonical_url=asset.canonical_url,
            filename=asset.filename,
            data=data,
            declared_media_type=asset.media_type,
            rights_status=asset.rights_status,
        )
    except AssetError as exc:
        raise SourceError("ASSET_REJECTED", str(exc)) from exc
    if stored.content_hash != digest:
        raise SourceError("ASSET_HASH_MISMATCH", "asset digest changed during local admission")

    # Keep the source identity/version row and replace only cache state.  A
    # later transaction rollback restores these DB fields; the content
    # addressed file follows the same orphan-safe behavior as AssetStore.
    asset.storage_key = stored.storage_key
    asset.media_type = stored.media_type
    asset.detected_type = stored.detected_type
    asset.size_bytes = stored.size_bytes
    asset.content_hash = digest
    if fixture.expected_hash is not None:
        asset.expected_content_hash = fixture.expected_hash
    asset.cache_state = "ready"
    asset.hash_verified = True
    asset.fetch_error = None
    asset.fetch_started_at = None
    asset.fetched_at = utcnow()
    session.flush()
    return asset


def _asset_from_fixture(
    session: Session,
    store: AssetStore,
    fixture: FixtureAsset,
    rights_status: str,
    *,
    max_cache_bytes: int,
) -> Asset:
    existing = _asset_identity_candidates(session, fixture)
    if existing:
        candidate = existing[0]
        if fixture.data is not None and candidate.cache_state == "metadata_only":
            return _promote_metadata_asset(
                session,
                store,
                candidate,
                fixture,
                max_cache_bytes=max_cache_bytes,
            )
        return candidate
    if fixture.data is not None:
        pending = _pending_metadata_asset(session, fixture)
        if pending is not None:
            return _promote_metadata_asset(
                session,
                store,
                pending,
                fixture,
                max_cache_bytes=max_cache_bytes,
            )
    if fixture.data is None:
        asset = Asset(
            id=new_id(),
            source_name=SOURCE_NAME,
            canonical_url=fixture.canonical_url,
            storage_key=None,
            filename=fixture.filename,
            media_type=fixture.media_type,
            detected_type=None,
            size_bytes=0,
            content_hash=fixture.content_hash,
            expected_content_hash=fixture.expected_hash,
            cache_state="metadata_only",
            rights_status=rights_status,
            created_at=utcnow(),
        )
    else:
        if fixture.expected_hash is not None and fixture.expected_hash != sha256_bytes(fixture.data):
            raise SourceError("ASSET_HASH_MISMATCH", "asset content does not match the source digest")
        try:
            asset = store.store_bytes(
                canonical_url=fixture.canonical_url,
                filename=fixture.filename,
                data=fixture.data,
                declared_media_type=fixture.media_type,
                rights_status=rights_status,
            )
        except AssetError as exc:
            raise SourceError("ASSET_REJECTED", str(exc)) from exc
    session.add(asset)
    session.flush()
    return asset


def import_fixture(
    session: Session,
    *,
    owner_id: str,
    settings: Settings,
    payload: dict[str, Any],
    idempotency_key: str,
    admission_mode: str = "fixture",
    admitted_rights_status: str | None = None,
    parser_version: str = PARSER_VERSION,
    target_exam_year: int | None = None,
) -> tuple[Task, TaskRevision, list[Asset], bool]:
    """Import one admitted deterministic record inside the caller transaction.

    ``fixture`` is test-only. ``remote`` is used by the bounded source adapter
    after policy, URL and response checks; rights then come from server policy,
    never from remote JSON.
    """

    if admission_mode == "fixture" and settings.source_mode != "fixture":
        raise SourceError("SOURCE_DISABLED", "fixture import is disabled by current source policy")
    if admission_mode == "remote" and (
        settings.source_mode not in {"manual", "sync"} or not settings.source_network_enabled
    ):
        raise SourceError("SOURCE_DISABLED", "remote source access is disabled by current policy")
    if admission_mode == "local-file" and (
        settings.source_mode != "manual" or settings.source_network_enabled is not False
    ):
        raise SourceError("SOURCE_DISABLED", "local-file import requires manual mode with source network disabled")
    if admission_mode == "local-file" and admitted_rights_status not in {"private_authorized", "public_authorized"}:
        raise SourceError("SOURCE_RIGHTS_REQUIRED", "local-file import requires explicit server-authorized rights")
    if admission_mode not in {"fixture", "remote", "local-file"}:
        raise SourceError("SOURCE_MODE_INVALID", "unknown source admission mode")
    if not isinstance(payload, dict):
        raise SourceError("FIXTURE_INVALID", "fixture must be an object")
    if admission_mode == "local-file":
        # Enforce metadata-only semantics at the shared admission seam before
        # any policy/task/asset side effects.  The dedicated local-bank parser
        # remains the stricter caller, but it cannot be the sole protection.
        _validate_local_file_asset_manifest(payload.get("assets"))
    policy = ensure_source_policy(session, settings)
    if policy.mode not in {"fixture", "manual", "sync"}:
        raise SourceError("SOURCE_DISABLED", "source policy does not allow fixture mode")
    source_url = canonical_source_url(str(payload.get("source_url", "")))
    source_id = bounded_text(str(payload.get("source_id") or payload.get("external_id") or ""), 255, "source_id")
    raw_statement = str(payload.get("statement") or "")
    statement, raw_statement_hash = sanitize_statement(raw_statement, settings.max_message_bytes)
    if not source_id or not statement:
        raise SourceError("FIXTURE_INVALID", "source_id and statement are required")
    line_value = payload.get("line_number")
    try:
        if line_value is None:
            raise TypeError("line_number is missing")
        line_number = int(str(line_value))
    except (TypeError, ValueError) as exc:
        raise SourceError("LINE_INVALID", "line_number must be an integer") from exc
    if not 1 <= line_number <= 27:
        raise SourceError("LINE_INVALID", "line_number must be between 1 and 27")
    if target_exam_year is not None:
        if isinstance(target_exam_year, bool) or not 2000 <= target_exam_year <= 2100:
            raise SourceError("YEAR_INVALID", "target_exam_year must be between 2000 and 2100")
    exam_year = payload.get("exam_year")
    if exam_year is not None:
        try:
            exam_year = int(exam_year)
        except (TypeError, ValueError) as exc:
            raise SourceError("YEAR_INVALID", "exam_year must be an integer") from exc
    rights_status = (
        admitted_rights_status
        if admission_mode in {"remote", "local-file"}
        else str(payload.get("rights_status") or "review_required")
    )
    if rights_status not in ALLOWED_RIGHTS:
        raise SourceError("RIGHTS_STATUS_INVALID", "unknown rights status")
    if admission_mode == "remote" and rights_status not in {"private_authorized", "public_authorized"}:
        raise SourceError("SOURCE_RIGHTS_REQUIRED", "source policy has not authorized learner visibility")
    if admission_mode == "fixture" and not settings.testing and rights_status != "review_required":
        raise SourceError(
            "SOURCE_RIGHTS_REQUIRED",
            "fixture payload cannot upgrade rights status outside explicit test mode",
        )
    # Resolve the idempotency key before storing any attachment.  The request
    # fingerprint is independent of generated asset/revision ids, so a replay
    # with a different body cannot be mistaken for a duplicate or leave cache
    # side effects behind.
    task = session.scalar(select(Task).where(Task.source_name == SOURCE_NAME, Task.external_id == source_id))
    existing_sync = _find_sync_run(session, owner_id, idempotency_key)
    fixture_assets = _fixture_assets(payload.get("assets"), settings)
    subtasks = _subtask_manifest(payload.get("subtasks"), settings)
    table_manifest = normalise_table_manifest(payload.get("table"))
    source_fields = _revision_source_metadata(payload)
    request_fingerprint = sha256_json(
        {
            "source_url": source_url,
            "source_id": source_id,
            "line_number": line_number,
            "exam_year": exam_year,
            "target_exam_year": target_exam_year,
            "source_fields": source_fields,
            "statement": statement,
            "statement_latex": payload.get("statement_latex"),
            "answer": payload.get("answer"),
            "explanation": payload.get("explanation"),
            "subtasks": subtasks,
            "table": table_manifest,
            "assets": [
                {
                    "url": item.canonical_url,
                    "filename": item.filename,
                    "media_type": item.media_type,
                    "content_hash": item.content_hash,
                    "role": item.role,
                }
                for item in fixture_assets
            ],
        }
    )
    if existing_sync is not None:
        if existing_sync.manifest_hash != request_fingerprint:
            raise SourceError("IDEMPOTENCY_CONFLICT", "idempotency key already describes another source payload")
        stored = stored_import_result(session, existing_sync)
        if stored is not None:
            return stored
        if task is not None and task.current_revision_id:
            current = session.get(TaskRevision, task.current_revision_id)
            if current is not None:
                existing_assets = list(
                    session.scalars(
                        select(Asset)
                        .join(TaskAsset, TaskAsset.asset_id == Asset.id)
                        .where(TaskAsset.revision_id == current.id)
                        .order_by(TaskAsset.position)
                    )
                )
                return task, current, existing_assets, True

    assets: list[Asset] = [
        _asset_from_fixture(
            session,
            AssetStore(settings),
            item,
            rights_status,
            max_cache_bytes=settings.max_asset_cache_bytes,
        )
        for item in fixture_assets
    ]
    # This is the stable upstream manifest used for the immutable revision
    # fingerprint.  It deliberately excludes generated IDs, cache state,
    # promoted byte hashes and sizes; those values can change without the
    # source task changing.
    source_asset_manifest = [
        {
            "url": item.canonical_url,
            "filename": item.filename,
            "media_type": item.media_type,
            "content_hash": item.content_hash,
            "expected_content_hash": item.expected_hash,
            "role": item.role,
        }
        for item in fixture_assets
    ]
    asset_manifest = [
        {
            "asset_id": asset.id,
            "url": asset.canonical_url,
            "filename": asset.filename,
            "content_hash": asset.content_hash,
            "size_bytes": asset.size_bytes,
            "media_type": asset.media_type,
            "role": item.role,
        }
        for asset, item in zip(assets, fixture_assets, strict=True)
    ]
    revision_material = {
        "source_url": source_url,
        "source_id": source_id,
        "line_number": line_number,
        "exam_year": exam_year,
        "target_exam_year": target_exam_year,
        "source_fields": source_fields,
        "statement": statement,
        "statement_latex": payload.get("statement_latex"),
        "answer": payload.get("answer"),
        "explanation": payload.get("explanation"),
        "subtasks": subtasks,
        "table": table_manifest,
        "asset_manifest": source_asset_manifest,
    }
    content_hash = sha256_json(revision_material)
    if task is None:
        task = Task(
            id=new_id(),
            source_name=SOURCE_NAME,
            external_id=source_id,
            line_number=line_number,
            current_revision_id=None,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        session.add(task)
        session.flush()
    current = session.get(TaskRevision, task.current_revision_id) if task.current_revision_id else None
    if current is not None and current.content_hash == content_hash:
        # A fresh idempotency key still needs a durable reservation after the
        # upstream request has produced an unchanged revision.  Otherwise a
        # retry of that fresh key would fetch the source again forever.  Keep a
        # single immutable task revision/asset identity while recording this
        # deliberate admission audit row.
        sync = _find_sync_run(session, owner_id, idempotency_key)
        if sync is None:
            sync = SyncRun(
                id=new_id(),
                source_policy_id=policy.id,
                mode=admission_mode,
                manifest_hash=request_fingerprint,
                parser_version=parser_version,
                status="succeeded",
                counters={
                    "tasks_seen": 1,
                    "revisions_created": 0,
                    "assets_seen": len(assets),
                    "task_id": task.id,
                    "revision_id": current.id,
                    "source_url": source_url,
                    "source_id": source_id,
                    "target_exam_year": target_exam_year,
                    "duplicate_revision": True,
                },
                failures=[],
                idempotency_key=scoped_sync_key(owner_id, idempotency_key),
                owner_id=owner_id,
                request_hash=request_fingerprint,
                created_at=utcnow(),
            )
            session.add(sync)
            session.flush()
            session.add(
                SourceSnapshot(
                    id=new_id(),
                    source_policy_id=policy.id,
                    source_url=source_url,
                    observed_url=source_url,
                    retrieved_at=utcnow(),
                    content_hash=content_hash,
                    parser_version=parser_version,
                    status="accepted",
                    manifest_json={
                        "idempotency_key": idempotency_key,
                        "source_id": source_id,
                        "upstream_record_id": payload.get("upstream_record_id"),
                        "raw_statement_hash": raw_statement_hash,
                        "parser_version": parser_version,
                        "duplicate_revision": True,
                    },
                )
            )
            append_event(
                session,
                owner_id=owner_id,
                aggregate_type="sync_run",
                aggregate_id=sync.id,
                event_type="source_sync_completed",
                payload={"content_hash": content_hash, "status": sync.status, "duplicate": True},
                idempotency_key=f"{idempotency_key}:sync",
            )
        return task, current, assets, True

    latest_number = session.scalar(
        select(func.max(TaskRevision.revision_number)).where(TaskRevision.task_id == task.id)
    )
    revision = TaskRevision(
        id=new_id(),
        task_id=task.id,
        revision_number=(latest_number or 0) + 1,
        source_url=source_url,
        source_id=source_id,
        line_number=line_number,
        exam_year=exam_year,
        target_exam_year=target_exam_year,
        source_year=source_fields["source_year"],
        source_year_basis=source_fields["source_year_basis"],
        source_created_at=(
            datetime.fromisoformat(source_fields["source_created_at"]).astimezone(UTC).replace(tzinfo=None)
            if source_fields["source_created_at"] else None
        ),
        source_difficulty=source_fields["source_difficulty"],
        difficulty=source_fields["difficulty"],
        source_label=source_fields["source_label"],
        source_metadata=source_fields["source_metadata"],
        statement=statement,
        statement_latex=payload.get("statement_latex"),
        answer=None if payload.get("answer") is None else str(payload.get("answer")),
        explanation=None if payload.get("explanation") is None else str(payload.get("explanation")),
        content_hash=content_hash,
        retrieved_at=utcnow(),
        parser_version=parser_version,
        rights_status=rights_status,
        asset_manifest=bounded_json(asset_manifest, 200_000, "asset_manifest"),
        subtask_manifest=subtasks,
        table_manifest=table_manifest,
        created_at=utcnow(),
    )
    session.add(revision)
    session.flush()
    for position, asset in enumerate(assets):
        session.add(
            TaskAsset(
                id=new_id(),
                revision_id=revision.id,
                asset_id=asset.id,
                runner_filename=safe_filename(asset.filename),
                position=position,
            )
        )
        append_event(
            session,
            owner_id=owner_id,
            aggregate_type="asset",
            aggregate_id=asset.id,
            event_type="asset_registered",
            payload={"content_hash": asset.content_hash, "cache_state": asset.cache_state},
            idempotency_key=f"{idempotency_key}:asset:{asset.content_hash}",
        )
    # A blocked revision is retained for provenance but may not displace the last
    # learner-visible authorized revision.
    if rights_status != "blocked":
        task.current_revision_id = revision.id
        task.line_number = line_number
    task.updated_at = utcnow()
    snapshot = SourceSnapshot(
        id=new_id(),
        source_policy_id=policy.id,
        source_url=source_url,
        observed_url=source_url,
        retrieved_at=utcnow(),
        content_hash=content_hash,
        parser_version=parser_version,
        status="accepted" if rights_status != "blocked" else "blocked",
        manifest_json={
            "idempotency_key": idempotency_key,
            "source_id": source_id,
            "upstream_record_id": payload.get("upstream_record_id"),
            "raw_statement_hash": raw_statement_hash,
            "parser_version": parser_version,
        },
    )
    session.add(snapshot)
    sync = _find_sync_run(session, owner_id, idempotency_key)
    if sync is None:
        sync = SyncRun(
            id=new_id(),
            source_policy_id=policy.id,
            mode=admission_mode,
            manifest_hash=request_fingerprint,
            parser_version=parser_version,
            status="succeeded",
            counters={
                "tasks_seen": 1,
                "revisions_created": 1,
                "assets_seen": len(assets),
                "task_id": task.id,
                "revision_id": revision.id,
                "source_url": source_url,
                "source_id": source_id,
                "target_exam_year": target_exam_year,
            },
            failures=[],
            idempotency_key=scoped_sync_key(owner_id, idempotency_key),
            owner_id=owner_id,
            request_hash=request_fingerprint,
            created_at=utcnow(),
        )
        session.add(sync)
    append_event(
        session,
        owner_id=owner_id,
        aggregate_type="task",
        aggregate_id=task.id,
        event_type="task_revision_imported",
        payload={"revision_id": revision.id, "content_hash": content_hash, "duplicate": False},
        idempotency_key=f"{idempotency_key}:revision",
    )
    append_event(
        session,
        owner_id=owner_id,
        aggregate_type="sync_run",
        aggregate_id=sync.id,
        event_type="source_sync_completed",
        payload={"content_hash": content_hash, "status": sync.status},
        idempotency_key=f"{idempotency_key}:sync",
    )
    return task, revision, assets, False


def task_is_visible(revision: TaskRevision) -> bool:
    return revision.rights_status in {"private_authorized", "public_authorized"}


def _revision_source_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate normalized evidence before hashing or storing a revision."""

    year = payload.get("source_year")
    if year is not None and (isinstance(year, bool) or not isinstance(year, int) or not 2000 <= year <= 2100):
        raise SourceError("SOURCE_METADATA_INVALID", "source year is invalid")
    level = payload.get("source_difficulty")
    difficulty = payload.get("difficulty")
    mapping = {0: "easy", 1: "medium", 2: "hard", 3: "hard"}
    if level is not None and (isinstance(level, bool) or not isinstance(level, int) or level not in mapping):
        raise SourceError("SOURCE_METADATA_INVALID", "source difficulty is invalid")
    expected_difficulty = mapping[level] if isinstance(level, int) and level in mapping else None
    if difficulty != expected_difficulty:
        raise SourceError("SOURCE_METADATA_INVALID", "source difficulty mapping is inconsistent")
    created = payload.get("source_created_at")
    if created is not None:
        try:
            if not isinstance(created, str) or len(created) > 64:
                raise ValueError
            parsed = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
            created = parsed.astimezone(UTC).isoformat()
        except (ValueError, TypeError) as exc:
            raise SourceError("SOURCE_METADATA_INVALID", "source creation date is invalid") from exc
    basis = payload.get("source_year_basis")
    if basis is not None and (not isinstance(basis, str) or not basis or len(basis) > 64):
        raise SourceError("SOURCE_METADATA_INVALID", "source year basis is invalid")
    if year is not None and basis is None:
        raise SourceError("SOURCE_METADATA_INVALID", "source year needs evidence")
    label = payload.get("source_label")
    if label is not None and (not isinstance(label, str) or len(label) > 2000):
        raise SourceError("SOURCE_METADATA_INVALID", "source label is invalid")
    evidence = payload.get("source_metadata") or {}
    if not isinstance(evidence, dict):
        raise SourceError("SOURCE_METADATA_INVALID", "source evidence must be an object")
    return {"source_year": year, "source_year_basis": basis, "source_created_at": created,
            "source_difficulty": level, "difficulty": difficulty, "source_label": label,
            "source_metadata": bounded_json(evidence, 8000, "source_metadata")}

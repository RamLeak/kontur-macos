"""Bounded KЕГЭ HTTP transport and one-admission normalizer.

The transport is production-capable but never enabled by default. Tests inject a
deterministic transport callable and therefore never contact the network.
"""

from __future__ import annotations

import ipaddress
import json
import mimetypes
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from sqlalchemy import select
from sqlalchemy.orm import Session

from .assets import AssetError, detect_type, safe_filename
from .catalog_policy import normalize_source_metadata
from .config import Settings
from .events import EventConflict, append_event
from .inline_images import decode_inline_image, inline_image_asset, parse_inline_image
from .models import Asset, SyncRun, Task, TaskAsset, TaskRevision, new_id, utcnow
from .source_catalog import (
    SOURCE_ORIGIN,
    SourceError,
    _find_sync_run,
    canonical_source_url,
    ensure_source_policy,
    import_fixture,
    normalise_table_manifest,
    sanitize_statement,
    scoped_sync_key,
    stored_import_result,
)
from .util import sha256_bytes, sha256_text


class SourceFetchError(SourceError):
    pass


REMOTE_STATEMENT_MAX_BYTES = 100_000


@dataclass(frozen=True, slots=True)
class VariantAdmission:
    """Deterministic result of one bounded random-variant admission."""

    source_url: str
    manifest_hash: str
    tasks: list[tuple[Any, Any, list[Any], bool]]
    failures: list[dict[str, Any]]
    duplicate: bool


class NormalizedVariant(list[dict[str, Any]]):
    """Admissible variant tasks plus explicit per-task normalization failures.

    Keeping this as a ``list`` subclass preserves the small internal API used
    by existing callers while allowing discovery to skip one incomplete task
    and continue filling the bounded catalog.  Failures contain only bounded
    identifiers/codes; source statement, answer and markup are never logged.
    """

    def __init__(self, tasks: list[dict[str, Any]], failures: list[dict[str, Any]]) -> None:
        super().__init__(tasks)
        self.failures = failures


@dataclass(frozen=True, slots=True)
class SourceResponse:
    status: int
    url: str
    headers: dict[str, str]
    body: bytes


Transport = Callable[[str, dict[str, str], int, float], SourceResponse]
Resolver = Callable[[str, int], list[str]]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _ImageSrcParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "img":
            return
        for key, value in attrs:
            if key.casefold() == "src" and isinstance(value, str) and value:
                self.sources.append(value)
                break


def _image_assets(statement: str, *, number: int, source_id: str) -> list[dict[str, Any]]:
    parser = _ImageSrcParser()
    parser.feed(statement)
    parser.close()
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_url in enumerate(parser.sources):
        if raw_url.startswith("data:"):
            try:
                result.append(inline_image_asset(number, source_id, index, raw_url))
            except ValueError as exc:
                raise SourceFetchError(
                    "SOURCE_IMAGE_UNSUPPORTED", "task contains an unsupported embedded image"
                ) from exc
            continue
        absolute = urljoin(SOURCE_ORIGIN + "/", raw_url)
        try:
            canonical = canonical_source_url(absolute, attachment=True)
        except SourceError as exc:
            # A single hostile/external/data image makes the task incomplete,
            # not the whole random variant.  The caller records this bounded
            # failure and continues with the remaining tasks.
            raise SourceFetchError(
                "SOURCE_IMAGE_UNSUPPORTED", "task contains an image outside the approved КЕГЭ image paths"
            ) from exc
        if canonical in seen:
            continue
        seen.add(canonical)
        filename = PurePosixPath(urlsplit(canonical).path).name or f"image-{index}.bin"
        filename = safe_filename(f"image-{index}-{filename}", fallback=f"image-{index}.bin")
        result.append(
            {
                "url": canonical,
                "filename": filename,
                "media_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
                "role": "image",
                "content_hash": sha256_text(f"metadata:{canonical}:{filename}"),
                "expected_hash": None,
            }
        )
    if len(result) > 20:
        raise SourceFetchError("SOURCE_SHAPE_UNSUPPORTED", "source task contains too many image assets")
    return result


def _default_resolver(host: str, port: int) -> list[str]:
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(cast(tuple[str, int, int, str, tuple[str, int]], item)[4][0]) for item in addresses]


def _default_transport(url: str, headers: dict[str, str], maximum: int, timeout: float) -> SourceResponse:
    request = Request(url, headers=headers, method="GET")
    opener = build_opener(_NoRedirect)
    try:
        response = opener.open(request, timeout=timeout)
        status = int(response.status)
        response_url = response.geturl()
        response_headers = {str(key): str(value) for key, value in response.headers.items()}
        body = response.read(maximum + 1)
    except HTTPError as exc:
        response_url = exc.geturl() or url
        response_headers = {str(key): str(value) for key, value in exc.headers.items()}
        body = exc.read(maximum + 1)
        status = int(exc.code)
    except (OSError, URLError, TimeoutError) as exc:
        raise SourceFetchError("SOURCE_FETCH_FAILED", "bounded source request failed") from exc
    if len(body) > maximum:
        raise SourceFetchError("SOURCE_RESPONSE_TOO_LARGE", "source response exceeds byte limit")
    return SourceResponse(status=status, url=response_url, headers=response_headers, body=body)


class BoundedKegeClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: Transport | None = None,
        resolver: Resolver | None = None,
        timeout_seconds: float = 15.0,
        max_redirects: int = 4,
        min_request_interval_seconds: float = 0.0,
    ):
        self.settings = settings
        self.transport = transport or _default_transport
        self.resolver = resolver or _default_resolver
        self.timeout_seconds = timeout_seconds
        self.max_redirects = max_redirects
        self.min_request_interval_seconds = max(0.0, min_request_interval_seconds)
        self._rate_lock = threading.Lock()
        self._last_request_at = 0.0

    def _rate_limit(self) -> None:
        """Apply an optional per-client spacing bound before network I/O.

        The default remains zero for deterministic local tests. Production
        callers can inject a conservative interval; a catalog worker should
        share one client instance for the whole bounded batch.
        """

        if self.min_request_interval_seconds <= 0:
            return
        with self._rate_lock:
            now = time.monotonic()
            wait_for = self.min_request_interval_seconds - (now - self._last_request_at)
            if wait_for > 0:
                time.sleep(wait_for)
            self._last_request_at = time.monotonic()

    def _enabled(self) -> None:
        if self.settings.source_mode not in {"manual", "sync"} or not self.settings.source_network_enabled:
            raise SourceFetchError("SOURCE_DISABLED", "KЕГЭ network access is disabled")

    def _reject_private_resolution(self, url: str) -> None:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        try:
            addresses = self.resolver(host, parsed.port or 443)
        except OSError as exc:
            raise SourceFetchError("SOURCE_DNS_FAILED", "source hostname could not be resolved") from exc
        for raw_address in addresses:
            try:
                address = ipaddress.ip_address(raw_address)
            except ValueError as exc:
                raise SourceFetchError("SOURCE_DNS_INVALID", "source DNS returned an invalid address") from exc
            if not address.is_global:
                raise SourceFetchError("SOURCE_PRIVATE_TARGET", "source target resolves to a private/local address")

    def fetch(self, raw_url: str, *, attachment: bool = False, api_endpoint: bool = False) -> SourceResponse:
        self._enabled()
        current = canonical_source_url(raw_url, attachment=attachment, api_endpoint=api_endpoint)
        for hop in range(self.max_redirects + 1):
            # Canonicalization and DNS/IP checks are repeated for every redirect.
            self._reject_private_resolution(current)
            self._rate_limit()
            response = self.transport(
                current,
                {
                    "Accept": "application/json, text/html;q=0.9, */*;q=0.1",
                    "User-Agent": "EGE-Informatics-Workspace/0.1 (+owner-controlled)",
                    "Referer": "https://kompege.ru/",
                },
                self.settings.max_asset_bytes,
                self.timeout_seconds,
            )
            if response.status in {301, 302, 303, 307, 308}:
                if hop >= self.max_redirects:
                    raise SourceFetchError("SOURCE_REDIRECT_LIMIT", "source redirect limit exceeded")
                location = next((value for key, value in response.headers.items() if key.casefold() == "location"), "")
                if not location:
                    raise SourceFetchError("SOURCE_REDIRECT_INVALID", "source redirect has no location")
                current = canonical_source_url(
                    urljoin(current, location), attachment=attachment, api_endpoint=api_endpoint
                )
                continue
            if response.status != 200:
                raise SourceFetchError("SOURCE_HTTP_ERROR", f"source returned HTTP {response.status}")
            if len(response.body) > self.settings.max_asset_bytes:
                raise SourceFetchError("SOURCE_RESPONSE_TOO_LARGE", "source response exceeds byte limit")
            return response
        raise SourceFetchError("SOURCE_REDIRECT_LIMIT", "source redirect limit exceeded")

    def _fetch_json_value(self, raw_url: str, *, api_endpoint: bool = False) -> Any:
        response = self.fetch(raw_url, api_endpoint=api_endpoint)
        try:
            value = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceFetchError("SOURCE_RESPONSE_INVALID", "source response is not valid UTF-8 JSON") from exc
        return value

    def fetch_json(self, raw_url: str, *, api_endpoint: bool = False) -> dict[str, Any]:
        value = self._fetch_json_value(raw_url, api_endpoint=api_endpoint)
        if not isinstance(value, dict):
            raise SourceFetchError("SOURCE_RESPONSE_INVALID", "source response must be a JSON object")
        return value

    def fetch_json_array(self, raw_url: str, *, api_endpoint: bool = False) -> list[Any]:
        """Fetch an allowlisted JSON array for the observed number endpoint."""

        value = self._fetch_json_value(raw_url, api_endpoint=api_endpoint)
        if not isinstance(value, list):
            raise SourceFetchError("SOURCE_RESPONSE_INVALID", "source response must be a JSON array")
        return value

    def fetch_bytes(
        self,
        raw_url: str,
        *,
        filename: str,
        declared_media_type: str | None = None,
        expected_hash: str | None = None,
    ) -> tuple[bytes, str, str]:
        """Fetch one attachment only after an explicit cache/use decision.

        This method is deliberately separate from ``fetch_json`` so catalog
        admission cannot accidentally download linked files. It returns the
        bytes plus verified detected MIME and digest; callers must atomically
        persist them through ``AssetStore``.
        """

        try:
            inline = parse_inline_image(raw_url)
        except ValueError as exc:
            raise SourceFetchError("SOURCE_PATH_BLOCKED", "invalid inline image locator") from exc
        if inline is None:
            data = self.fetch(raw_url, attachment=True).body
        else:
            records = self.fetch_json_array(inline.endpoint, api_endpoint=True)
            if len(records) > 1_000:
                raise SourceFetchError("SOURCE_BATCH_INVALID", "source task array exceeds item limit")
            candidates: list[dict[str, Any]] = []
            for raw in records:
                if not isinstance(raw, dict):
                    continue
                nested_record = raw.get("task")
                record = nested_record if isinstance(nested_record, dict) else raw
                group = [record]
                children = record.get("subTask")
                if isinstance(children, list) and len(children) <= 4:
                    group.extend(child for child in children if isinstance(child, dict))
                for item in group:
                    identity = item.get("taskId") or item.get("external_id") or item.get("id")
                    if str(identity) == inline.source_id:
                        candidates.append(item)
            if len(candidates) != 1:
                raise SourceFetchError("ASSET_SOURCE_CHANGED", "embedded image source is missing or ambiguous")
            record = candidates[0]
            statement = record.get("statement") or record.get("text") or record.get("html")
            if not isinstance(statement, str):
                raise SourceFetchError("ASSET_SOURCE_CHANGED", "embedded image source text is missing")
            parser = _ImageSrcParser()
            parser.feed(statement)
            parser.close()
            try:
                data, media_type, _extension = decode_inline_image(parser.sources[inline.index])
            except (ValueError, IndexError) as exc:
                raise SourceFetchError("ASSET_SOURCE_CHANGED", "embedded image is no longer available") from exc
            if media_type != inline.media_type or sha256_bytes(data) != inline.digest:
                raise SourceFetchError("ASSET_HASH_MISMATCH", "embedded image differs from pinned source")
        digest = sha256_bytes(data)
        if expected_hash and digest != expected_hash:
            raise SourceFetchError("ASSET_HASH_MISMATCH", "asset bytes do not match the expected digest")
        safe_name = safe_filename(filename)
        try:
            detected = detect_type(data, safe_name, declared_media_type)
        except AssetError as exc:
            raise SourceFetchError("ASSET_TYPE_MISMATCH", "asset bytes failed MIME/signature validation") from exc
        return data, detected, digest


def _normalise_remote_record(
    raw: dict[str, Any], canonical_url: str, *, inherited_metadata: dict[str, Any] | None = None,
    image_number: int | None = None,
) -> dict[str, Any]:
    nested = raw.get("task")
    record: dict[str, Any] = nested if isinstance(nested, dict) else raw
    source_fields = normalize_source_metadata(record, inherited=inherited_metadata)
    # Observed (2026-09-03) response shape uses numeric ``taskId`` and
    # ``number`` plus text; the endpoint remains configurable/unverified.
    source_id = record.get("taskId") or record.get("external_id") or record.get("id")
    line = record.get("task_number") or record.get("number") or record.get("line_number")
    statement = record.get("statement") or record.get("text") or record.get("html")
    if source_id is None or line is None or not isinstance(statement, str) or not statement.strip():
        raise SourceFetchError("SOURCE_RESPONSE_INVALID", "source task response lacks required fields")
    try:
        line_number = int(line)
    except (TypeError, ValueError) as exc:
        raise SourceFetchError("SOURCE_RESPONSE_INVALID", "source task line number is invalid") from exc
    if not 1 <= line_number <= 27:
        raise SourceFetchError("SOURCE_RESPONSE_INVALID", "source task line number is outside 1..27")
    image_number = image_number or line_number
    sub_tasks = record.get("subTask")
    subtasks: list[dict[str, Any]] = []
    if sub_tasks is not None:
        if not isinstance(sub_tasks, list) or len(sub_tasks) > 4:
            raise SourceFetchError("SOURCE_SHAPE_UNSUPPORTED", "source subTask group exceeds the bounded limit")
        for index, raw_subtask in enumerate(sub_tasks):
            if not isinstance(raw_subtask, dict):
                raise SourceFetchError("SOURCE_RESPONSE_INVALID", "source subTask entry is invalid")
            subtask = dict(raw_subtask)
            if not any(subtask.get(key) is not None for key in ("taskId", "external_id", "id")):
                subtask["taskId"] = f"{source_id}:sub:{index}"
            subtask.setdefault("number", int(line_number) + index)
            subtask.setdefault("text", subtask.get("statement") or subtask.get("html") or "")
            # Prevent an accidentally recursive upstream object from escaping
            # the bounded grouped record normalizer.
            subtask["subTask"] = []
            normalized_subtask = _normalise_remote_record(
                subtask, canonical_url, inherited_metadata=source_fields, image_number=image_number
            )
            subtasks.append(
                {
                    "source_id": normalized_subtask["source_id"],
                    "upstream_record_id": normalized_subtask.get("upstream_record_id"),
                    "line_number": normalized_subtask["line_number"],
                    "statement": normalized_subtask["statement"],
                    "statement_latex": normalized_subtask.get("statement_latex"),
                    "answer": normalized_subtask.get("answer"),
                    "explanation": normalized_subtask.get("explanation"),
                    "table": normalized_subtask.get("table"),
                    "assets": normalized_subtask.get("assets", []),
                    "source_year": normalized_subtask.get("source_year"),
                    "source_year_basis": normalized_subtask.get("source_year_basis"),
                    "source_created_at": normalized_subtask.get("source_created_at"),
                    "source_difficulty": normalized_subtask.get("source_difficulty"),
                    "difficulty": normalized_subtask.get("difficulty"),
                    "source_label": normalized_subtask.get("source_label"),
                    "source_metadata": normalized_subtask.get("source_metadata", {}),
                    "group_external_id": str(source_id),
                    "group_index": index,
                }
            )
    raw_assets = record.get("files") or record.get("attachments") or []
    assets: list[dict[str, Any]] = []
    if isinstance(raw_assets, dict):
        raw_assets = list(raw_assets.values())
    if not isinstance(raw_assets, list):
        raise SourceFetchError("SOURCE_RESPONSE_INVALID", "source attachment manifest is invalid")
    for raw_asset in raw_assets[:20]:
        if not isinstance(raw_asset, dict):
            raise SourceFetchError("SOURCE_RESPONSE_INVALID", "source attachment entry is invalid")
        relative = raw_asset.get("url") or raw_asset.get("path")
        if not isinstance(relative, str):
            raise SourceFetchError("SOURCE_RESPONSE_INVALID", "source attachment URL is invalid")
        # Importer validates the final canonical URL again; urljoin handles only
        # relative paths from the already-approved KЕГЭ origin.
        absolute_url = urljoin(SOURCE_ORIGIN + "/", relative)
        filename = raw_asset.get("name") or raw_asset.get("filename") or "attachment.bin"
        supplied_hash = raw_asset.get("sha256") or raw_asset.get("content_hash")
        assets.append(
            {
                "url": absolute_url,
                "filename": filename,
                "media_type": raw_asset.get("media_type") or "application/octet-stream",
                "role": "file",
                # A source API that omits a digest gets a deterministic metadata
                # placeholder. Lazy byte admission replaces this record with a
                # content-addressed ready asset; it is never treated as bytes.
                "content_hash": supplied_hash or sha256_text(f"metadata:{absolute_url}:{filename}"),
                "expected_hash": supplied_hash or None,
            }
        )
    assets.extend(_image_assets(statement, number=image_number, source_id=str(source_id)))
    # Embedded bytes are already bounded/validated above. They do not count as
    # learner-visible statement text and must not be retained in the revision.
    statement_parser = _ImageSrcParser()
    statement_parser.feed(statement)
    for image_source in statement_parser.sources:
        if image_source.startswith("data:"):
            statement = statement.replace(image_source, "inline-image")
    try:
        safe_statement, _raw_statement_hash = sanitize_statement(statement, REMOTE_STATEMENT_MAX_BYTES)
    except ValueError as exc:
        raise SourceFetchError("SOURCE_RESPONSE_TOO_LARGE", "source task statement exceeds the bounded limit") from exc
    answer_value = record.get("answer") or record.get("correct_answer")
    # The observed КЕГЭ envelope uses ``key`` for the answer key. Keep it in
    # the immutable revision for deterministic scoring, but never treat
    # ``solve_text`` as an explanation or execute/render untrusted markup.
    if answer_value is None and record.get("key") is not None:
        key = record.get("key")
        if isinstance(key, (dict, list, tuple, set)):
            raise SourceFetchError("SOURCE_RESPONSE_INVALID", "source answer key must be bounded scalar text")
        answer_value = str(key)[:20_000]
    return {
        "source_url": canonical_url,
        "source_id": str(source_id),
        "upstream_record_id": str(record["id"]) if record.get("id") is not None else None,
        "line_number": line_number,
        "exam_year": record.get("exam_year") or record.get("year"),
        "statement": safe_statement,
        "statement_latex": record.get("statement_latex"),
        "answer": answer_value,
        "explanation": record.get("explanation") or record.get("solution"),
        "table": normalise_table_manifest(record.get("table")),
        "subtasks": subtasks,
        "assets": assets,
        **source_fields,
    }


def _normalise_task_records(
    tasks: list[Any], canonical_url: str, *, max_tasks: int, allow_empty: bool = False
) -> NormalizedVariant:
    """Normalize a bounded task array from either variant or number routes."""

    if (not tasks and not allow_empty) or len(tasks) > max_tasks:
        raise SourceFetchError("SOURCE_BATCH_INVALID", "source task array exceeds the bounded item limit")
    normalized_by_id: dict[str, tuple[str, dict[str, Any]]] = {}
    failures: list[dict[str, Any]] = []
    for index, item in enumerate(tasks):
        if not isinstance(item, dict):
            raise SourceFetchError("SOURCE_RESPONSE_INVALID", "variant task entry is invalid")
        candidate = item.get("task") if isinstance(item.get("task"), dict) else item
        if not isinstance(candidate, dict):
            raise SourceFetchError("SOURCE_RESPONSE_INVALID", "variant task entry is invalid")
        try:
            preliminary = _normalise_remote_record(candidate, canonical_url)
        except (SourceFetchError, SourceError, ValueError) as exc:
            source_hint = candidate.get("taskId") or candidate.get("external_id") or candidate.get("id")
            failure: dict[str, Any] = {
                "index": index,
                "code": str(getattr(exc, "code", "SOURCE_TASK_INCOMPLETE")),
                "message": str(getattr(exc, "message", "variant task could not be normalized"))[:500],
            }
            if source_hint is not None:
                failure["source_id"] = str(source_hint)[:255]
            failures.append(failure)
            continue
        source_id = str(preliminary["source_id"])
        task_url = canonical_source_url(f"{SOURCE_ORIGIN}/task?{urlencode({'id': source_id})}")
        normalized = dict(preliminary)
        normalized["source_url"] = task_url
        # The answer key is stored only in the immutable revision for scoring;
        # serializers decide whether the answer is currently revealable.
        fingerprint = sha256_text(json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        previous = normalized_by_id.get(source_id)
        if previous is not None and previous[0] != fingerprint:
            raise SourceFetchError("SOURCE_RESPONSE_INVALID", "variant contains conflicting duplicate task ids")
        normalized_by_id[source_id] = (fingerprint, normalized)
    ordered = sorted(
        normalized_by_id.values(),
        key=lambda pair: (int(pair[1]["line_number"]), str(pair[1]["source_id"])),
    )
    return NormalizedVariant([item for _fingerprint, item in ordered], failures)


def _normalise_variant_payload(
    raw: dict[str, Any],
    canonical_url: str,
    *,
    max_tasks: int = 27,
) -> NormalizedVariant:
    """Normalize the observed variant envelope without exposing answer keys.

    Variant records are admitted as ordinary immutable task revisions.  The
    envelope is bounded before any database or asset work, duplicate numeric
    task ids are deduplicated only when their normalized content is identical,
    and incompatible duplicates fail closed.
    """

    if not isinstance(raw, dict):
        raise SourceFetchError("SOURCE_RESPONSE_INVALID", "variant response must be a JSON object")
    for flag in ("hideAnswer", "realMode"):
        if flag in raw and not isinstance(raw[flag], bool):
            raise SourceFetchError("SOURCE_RESPONSE_INVALID", f"variant {flag} flag is invalid")
    kim = raw.get("kim")
    if kim is not None and (not isinstance(kim, str) or len(kim) > 512):
        raise SourceFetchError("SOURCE_RESPONSE_INVALID", "variant kim metadata is invalid")
    tasks = raw.get("tasks")
    if not isinstance(tasks, list) or len(tasks) > max_tasks or len(tasks) > 27:
        raise SourceFetchError("SOURCE_BATCH_INVALID", "variant tasks must contain 1 to 27 entries")
    return _normalise_task_records(tasks, canonical_url, max_tasks=max_tasks)


def _normalise_number_payload(
    raw: list[Any], canonical_url: str, *, max_tasks: int = 1_000
) -> NormalizedVariant:
    """Normalize a number page and materialize linked 19-21 tasks.

    KЕГЭ exposes lines 20 and 21 as ``subTask`` children of each line-19
    record, while the dedicated number-20/21 routes are empty.  Each child is
    therefore admitted as its own scorable revision with a stable upstream id
    and the parent statement prepended as the shared context.  The original
    grouped manifest remains on the line-19 revision as provenance.
    """

    if not isinstance(raw, list):
        raise SourceFetchError("SOURCE_RESPONSE_INVALID", "number endpoint response must be a JSON array")
    # Number routes are line-specific and a valid line may currently have no
    # published records.  Keep that as an empty successful page so fair
    # selection can distinguish an empty line from a transport/shape failure.
    grouped = _normalise_task_records(raw, canonical_url, max_tasks=max_tasks, allow_empty=True)
    expanded: list[dict[str, Any]] = []
    fingerprints: dict[str, str] = {}

    def admit(item: dict[str, Any]) -> None:
        source_id = str(item["source_id"])
        fingerprint = sha256_text(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        previous = fingerprints.get(source_id)
        if previous is not None:
            if previous != fingerprint:
                raise SourceFetchError(
                    "SOURCE_RESPONSE_INVALID", "number page contains conflicting duplicate task ids"
                )
            return
        fingerprints[source_id] = fingerprint
        expanded.append(item)

    for parent in grouped:
        admit(parent)
        for child in parent.get("subtasks", []):
            if not isinstance(child, dict):
                raise SourceFetchError("SOURCE_RESPONSE_INVALID", "normalized subTask entry is invalid")
            combined_statement, _combined_hash = sanitize_statement(
                f"{parent['statement']}\n\n{child['statement']}", REMOTE_STATEMENT_MAX_BYTES
            )
            admit(
                {
                    "source_url": parent["source_url"],
                    "source_id": str(child["source_id"]),
                    "upstream_record_id": child.get("upstream_record_id"),
                    "line_number": int(child["line_number"]),
                    "exam_year": parent.get("exam_year"),
                    "statement": combined_statement,
                    "statement_latex": child.get("statement_latex"),
                    "answer": child.get("answer"),
                    "explanation": child.get("explanation"),
                    "table": child.get("table"),
                    "subtasks": [],
                    "assets": child.get("assets", []),
                    "source_year": child.get("source_year"),
                    "source_year_basis": child.get("source_year_basis"),
                    "source_created_at": child.get("source_created_at"),
                    "source_difficulty": child.get("source_difficulty"),
                    "difficulty": child.get("difficulty"),
                    "source_label": child.get("source_label"),
                    "source_metadata": child.get("source_metadata", {}),
                }
            )
    if len(expanded) > max_tasks:
        raise SourceFetchError("SOURCE_BATCH_INVALID", "expanded number page exceeds the bounded item limit")
    expanded.sort(key=lambda item: (int(item["line_number"]), str(item["source_id"])))
    return NormalizedVariant(expanded, list(grouped.failures))


def _stored_variant_results(session: Session, sync: SyncRun) -> list[tuple[Any, Any, list[Any], bool]]:
    """Rehydrate a completed variant response without touching the network."""

    counters = sync.counters if isinstance(sync.counters, dict) else {}
    refs = counters.get("result_refs", [])
    if not isinstance(refs, list):
        return []
    results: list[tuple[Any, Any, list[Any], bool]] = []
    for raw_ref in refs:
        if not isinstance(raw_ref, dict):
            continue
        task = session.get(Task, raw_ref.get("task_id"))
        revision = session.get(TaskRevision, raw_ref.get("revision_id"))
        if task is None or revision is None:
            continue
        assets = list(
            session.scalars(
                select(Asset)
                .join(TaskAsset, TaskAsset.asset_id == Asset.id)
                .where(TaskAsset.revision_id == revision.id)
                .order_by(TaskAsset.position, Asset.id)
            )
        )
        results.append((task, revision, assets, True))
    return results


def admit_remote_variant(
    session: Session,
    *,
    owner_id: str,
    settings: Settings,
    source_url: str,
    idempotency_key: str,
    client: BoundedKegeClient,
    max_tasks: int = 27,
    target_exam_year: int | None = None,
) -> VariantAdmission:
    """Fetch and admit one owner-approved, bounded variant envelope.

    A batch has its own deterministic manifest and idempotency row while each
    task still uses the normal remote one-admission path.  Per-task savepoints
    make failures explicit in the committed manifest without leaking a
    partially-written task revision or asset.
    """

    if settings.source_mode != "sync" or not settings.source_network_enabled:
        raise SourceFetchError("SOURCE_DISABLED", "KЕГЭ variant sync is disabled")
    if target_exam_year is not None and (isinstance(target_exam_year, bool) or not 2000 <= target_exam_year <= 2100):
        raise SourceError("YEAR_INVALID", "target_exam_year must be between 2000 and 2100")
    canonical_variant = canonical_source_url(source_url, api_endpoint=True)
    policy = ensure_source_policy(session, settings)
    if policy.permission_status not in {"private_authorized", "public_authorized"}:
        raise SourceError("SOURCE_RIGHTS_REQUIRED", "source policy has not authorized learner visibility")
    request_hash = sha256_text(f"variant-sync-v1:{canonical_variant}:{max_tasks}:{target_exam_year}")
    existing_batch = _find_sync_run(session, owner_id, idempotency_key)
    if existing_batch is not None:
        if existing_batch.request_hash and existing_batch.request_hash != request_hash:
            raise SourceError("IDEMPOTENCY_CONFLICT", "idempotency key already describes another variant request")
        return VariantAdmission(
            source_url=canonical_variant,
            manifest_hash=existing_batch.manifest_hash,
            tasks=_stored_variant_results(session, existing_batch),
            failures=list(existing_batch.failures),
            duplicate=True,
        )

    raw = client.fetch_json(canonical_variant, api_endpoint=True)
    normalized = _normalise_variant_payload(raw, canonical_variant, max_tasks=max_tasks)
    task_material = [
        {
            "source_id": item["source_id"],
            "line_number": item["line_number"],
            "source_url": item["source_url"],
            "statement": item["statement"],
            "statement_latex": item.get("statement_latex"),
            "answer": item.get("answer"),
            "explanation": item.get("explanation"),
            "subtasks": item.get("subtasks", []),
            "assets": item.get("assets", []),
            "table": item.get("table"),
        }
        for item in normalized
    ]
    manifest_hash = sha256_text(
        json.dumps(
            {
                "source_url": canonical_variant,
                "parser_version": "kompege-variant-observed-2026-09-03",
                "target_exam_year": target_exam_year,
                "kim_hash": sha256_text(raw["kim"]) if isinstance(raw.get("kim"), str) else None,
                "tasks": task_material,
                "normalization_failures": list(getattr(normalized, "failures", [])),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    # The early lookup above prevents duplicate network calls. This second
    # lookup only handles a same-transaction legacy row discovered after the
    # fetch and remains owner-scoped.
    existing_batch = _find_sync_run(session, owner_id, idempotency_key)
    if existing_batch is not None and existing_batch.manifest_hash != manifest_hash:
        raise SourceError("IDEMPOTENCY_CONFLICT", "idempotency key already describes another variant manifest")

    results: list[tuple[Any, Any, list[Any], bool]] = []
    normalization_failures: list[dict[str, Any]] = list(getattr(normalized, "failures", []))
    failures: list[dict[str, Any]] = list(normalization_failures)
    for item in normalized:
        task_key = f"{idempotency_key}:task:{item['source_id']}"
        try:
            with session.begin_nested():
                result = import_fixture(
                    session,
                    owner_id=owner_id,
                    settings=settings,
                    payload=item,
                    idempotency_key=task_key,
                    admission_mode="remote",
                    admitted_rights_status=policy.permission_status,
                    parser_version="kompege-variant-observed-2026-09-03",
                    target_exam_year=target_exam_year,
                )
            results.append(result)
        except (SourceError, AssetError, EventConflict, ValueError) as exc:
            failures.append(
                {
                    "source_id": str(item["source_id"]),
                    "code": str(getattr(exc, "code", "SOURCE_IMPORT_FAILED")),
                    "message": str(getattr(exc, "message", exc)),
                }
            )

    if existing_batch is None:
        result_refs = [
            {"task_id": task.id, "revision_id": revision.id} for task, revision, _assets, _duplicate in results
        ]
        sync = SyncRun(
            id=new_id(),
            source_policy_id=policy.id,
            mode="variant-sync",
            manifest_hash=manifest_hash,
            parser_version="kompege-variant-observed-2026-09-03",
            status="partial" if failures else "succeeded",
            counters={
                "tasks_seen": len(normalized) + len(normalization_failures),
                "tasks_admitted": len(results),
                "revisions_created": sum(1 for _task, _revision, _assets, duplicate in results if not duplicate),
                "failures": len(failures),
                "result_refs": result_refs,
                "source_url": canonical_variant,
                "max_tasks": max_tasks,
                "target_exam_year": target_exam_year,
            },
            failures=failures,
            idempotency_key=scoped_sync_key(owner_id, idempotency_key),
            owner_id=owner_id,
            request_hash=request_hash,
            created_at=utcnow(),
        )
        session.add(sync)
        session.flush()
        append_event(
            session,
            owner_id=owner_id,
            aggregate_type="sync_run",
            aggregate_id=sync.id,
            event_type="source_sync_completed",
            payload={
                "manifest_hash": manifest_hash,
                "status": sync.status,
                "tasks_seen": len(normalized),
                "failures": len(failures),
            },
            idempotency_key=f"{idempotency_key}:batch",
        )
    else:
        # A replay reuses the original batch row and task-level idempotency
        # records; no new snapshot/event is emitted for already-admitted tasks.
        failures = list(existing_batch.failures)

    return VariantAdmission(
        source_url=canonical_variant,
        manifest_hash=manifest_hash,
        tasks=results,
        failures=failures,
        duplicate=existing_batch is not None,
    )


def admit_remote_task(
    session: Session,
    *,
    owner_id: str,
    settings: Settings,
    source_url: str,
    idempotency_key: str,
    client: BoundedKegeClient,
    target_exam_year: int | None = None,
) -> tuple[Any, Any, list[Any], bool]:
    """Fetch and admit exactly one task through the source policy boundary."""

    canonical_page = canonical_source_url(source_url)
    policy = ensure_source_policy(session, settings)
    if policy.permission_status not in {"private_authorized", "public_authorized"}:
        raise SourceError("SOURCE_RIGHTS_REQUIRED", "source policy has not authorized learner visibility")
    existing_sync = _find_sync_run(session, owner_id, idempotency_key)
    if existing_sync is not None:
        counters = existing_sync.counters if isinstance(existing_sync.counters, dict) else {}
        if counters.get("source_url") and counters.get("source_url") != canonical_page:
            raise SourceError("IDEMPOTENCY_CONFLICT", "idempotency key already describes another source URL")
        if counters.get("target_exam_year") != target_exam_year:
            raise SourceError("IDEMPOTENCY_CONFLICT", "idempotency key already describes another target year")
        stored = stored_import_result(session, existing_sync)
        if stored is not None:
            return stored
    # The browser-facing page is the confirmed provenance URL.  A JSON API
    # template must be explicitly owner-configured after contract review; do
    # not present an unverified endpoint as a guaranteed КЕГЭ interface.
    task_id = dict(parse_qsl(urlsplit(canonical_page).query)).get("id")
    if task_id and settings.source_api_template:
        try:
            api_url = settings.source_api_template.format(id=task_id)
        except (KeyError, ValueError, IndexError) as exc:
            raise SourceError("SOURCE_API_UNVERIFIED", "configured source API template is invalid") from exc
        raw = client.fetch_json(api_url, api_endpoint=True)
    elif task_id and not settings.source_api_template:
        raise SourceError(
            "SOURCE_API_UNVERIFIED",
            "source task API endpoint is not owner-configured and contract-reviewed",
        )
    else:
        raw = client.fetch_json(canonical_page)
    payload = _normalise_remote_record(raw, canonical_page)
    return import_fixture(
        session,
        owner_id=owner_id,
        settings=settings,
        payload=payload,
        idempotency_key=idempotency_key,
        admission_mode="remote",
        admitted_rights_status=policy.permission_status,
        parser_version="kompege-json-observed-2026-09-03",
        target_exam_year=target_exam_year,
    )

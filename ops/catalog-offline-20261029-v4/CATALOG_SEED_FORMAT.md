# Catalog seed v1

Producer: `ops/catalog-offline-20261029-v4/export_catalog.py`, Python 3.11+ stdlib.
Compatibility v4: a nullable JSON table_manifest may be stored as SQL NULL or
as SQLAlchemy JSON text `null`; both emit logical JSON null. Required object/
array fields still reject JSON null. Source IDs/hashes/metadata are unchanged.
Consumer: the full local backend's seed ingestion (owned by phase 1).
The producer is an operator utility, not an HTTP endpoint or authentication
service. Running it on a real server/database and delivering its result requires
ROOT's separate, concrete approval. This phase uses synthetic data only.

The seed is one SQLite file, installed as
`resources/catalog/catalog.sqlite3`. It contains genuine source task data,
including solutions, and is intended for the authorized user's personal device.
`private_authorized` and `public_authorized` are both admitted, matching
schema-12 `task_is_visible` and asset preparation policy. This is not a public
redistribution grant. No upstream download is performed.

## Source boundary and selection

Open the explicitly named source database with SQLite `mode=ro`,
`query_only=ON`, `trusted_schema=OFF`, disabled extension loading, and one read
transaction. Do not use `immutable=1`: committed live WAL data must be visible.
Only the five tables below may supply business data. SQLite schema metadata for
these tables is inspected for compatibility; no migration/version table is read.
The exact schema-12 columns are required; this is a structural schema-12
compatibility check, not proof of the unqueried migration history.

* `tasks`: `id, source_name, external_id, line_number, current_revision_id,
  created_at, updated_at`.
* `task_revisions`: `id, task_id, revision_number, source_url, source_id,
  line_number, exam_year, target_exam_year, source_year, source_year_basis,
  source_created_at, source_difficulty, difficulty, source_label,
  source_metadata, statement, statement_latex, answer, explanation,
  content_hash, retrieved_at, parser_version, rights_status, asset_manifest,
  subtask_manifest, table_manifest, created_at`.
* `assets`: `id, source_name, canonical_url, storage_key, filename, media_type,
  detected_type, size_bytes, content_hash, expected_content_hash, cache_state,
  rights_status, hash_verified, fetched_at, created_at`. `storage_key` is used
  only to locate a confined file and is never emitted.
* `task_assets`: `id, revision_id, asset_id, runner_filename, position`.
* `catalog_entries`: **only `revision_id`**, distinct and filtered to exported
  authorized revisions. No job ID, owner or job data is read.

Export all existing `kompege` tasks/revisions whose revision has either admitted
rights status, including their authorized history. Do not invent source rows.
`catalog_selection` is the union of their current revision IDs and authorized
revision IDs present in `catalog_entries`; it deliberately does not claim to
reconstruct any particular owner's latest catalog job. Unexported current
pointers are cleared to NULL and counted as `suppressedCurrentPointers`, so no
foreign key points to a denied revision. Other source IDs and values are kept.
Denied revisions and unrelated sources are counted, without reading their
statement/solution bodies. Only referenced, authorized `kompege` assets are read.
Malformed NULL source names/rights and orphan revision/link/selection rows fail
the entire export with `catalog_source_rows_invalid`. Fixed whitelisted COUNT
queries detect them before any catalog body export, so SQL NULL comparisons
cannot silently hide malformed rows and produce `complete=true`.

Do not read users, owners, progress, bookmarks, attempts, messages, sessions,
jobs, snapshots, config, keys, auth or profile tables/files. A SQLite authorizer
enforces the table **and column** whitelist and rejects writes, ATTACH,
extensions, unexpected functions, and source SQL objects. Extra source columns
are never selected. Views, triggers or generated/virtual selected tables fail.

Statements, LaTeX, answers, explanations, years, difficulty, source metadata and
the three manifests are preserved as data, with bounded JSON decoding. JSON
objects with recognized sensitive storage/auth/owner/config key families fail
the whole export with a fixed safe error; fields are not silently redacted. Free task text is
not executed, interpreted as a template, logged, or treated as instructions.
The key check applies at every nesting depth: Unicode NFKC and case folding,
camel/acronym splitting, separator-insensitive compact forms, sensitive tokens,
and family prefixes/suffixes. These include path/storage/database, owner/email,
credential/secret/password/key material, profile/session/token/auth/config.
For example `storage_path`, `path`, `database_path`, `owner_email`, `credential`,
`secret_key`, `profile_id`, `oauth`, `jwt`, `encryption_key`, `key_material`,
bare `key` and their casing/separator/compact variants are rejected. `key` is
an exact token/compact name, not a generic prefix/suffix: public names such as
`keyboard` and `monkey` remain allowed. Specific key-material composites remain
sensitive prefixes/suffixes.
Legitimate metadata that passes this check, including ordinary text values,
is preserved exactly rather than silently dropping fields. Generic author /
authorship metadata is allowed; authentication/authorization material is not.
This key-family check is a bounded heuristic, not a universal detector of
secrets in arbitrary text or every possible name/encoding. The actual access
boundary is the fixed table/column whitelist and the confined digest-file
allowlist. Key files, auth/config/profile files and owner tables are never read.
Task text and source metadata values are deliberately preserved as data; the
heuristic does not weaken the source/file allowlists or permit known sensitive
key families. A flagged legitimate field fails safely for review rather than
being silently removed.

## SQLite layout

`application_id = 0x4b435331` (`KCS1`), `user_version = 1`, UTF-8.
Exactly these seven application tables (implicit primary-key indexes allowed):

```sql
CREATE TABLE catalog_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE catalog_tasks (id TEXT PRIMARY KEY, record_json TEXT NOT NULL);
CREATE TABLE catalog_revisions (id TEXT PRIMARY KEY, record_json TEXT NOT NULL);
CREATE TABLE catalog_assets (id TEXT PRIMARY KEY, record_json TEXT NOT NULL);
CREATE TABLE catalog_links (id TEXT PRIMARY KEY, record_json TEXT NOT NULL);
CREATE TABLE catalog_selection (revision_id TEXT PRIMARY KEY);
CREATE TABLE catalog_asset_bytes (
    asset_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, bytes BLOB NOT NULL
);
```

`record_json` is an object with the same snake_case keys and original IDs as the
whitelist, except `catalog_assets` omits `storage_key`. SQLite JSON columns become
JSON objects/lists/null, rather than double-encoded strings. `hash_verified` is a
JSON boolean. Nullable original fields remain null. No source SQL/DDL is copied.
`catalog_links` contains only genuine `task_assets` rows; subtask relationships
remain in the original `subtask_manifest`, without invented link rows.

The consumer MUST reject `complete=false`, missing/bad metadata or hashes,
unexpected tables/columns/views/triggers, duplicate/malformed records, invalid
references, disallowed rights, missing bytes and exceeded limits before changing
its local catalog. It must validate the canonical records against its fixed
model columns, not execute incoming SQL. Seed ingestion must not overwrite
existing user work/progress. The producer does not implement that consumer.

## Assets and incompleteness

Resolve the caller's trusted cache root once. A byte file must have an existing
authorized `assets` row, state `ready`, `hash_verified=1`, and a content-addressed
storage key of the original form `<sha256[:2]>/<sha256>[.<extension>]`.
Traversal, absolute/drive/UNC paths, backslashes, hidden components, links,
reparse points, nonregular files and changed files are rejected. Linux uses
no-follow directory-relative handles; Windows checks the opened handle's final
path before reading. Each file is bounded to **20,000,000 bytes**, its exact
source size, actual SHA-256 and any supplied expected SHA-256 are checked.
No cache fetch, repair, update, deletion, fallback path or network access occurs.

Parent manifests and subtask URL/filename references must resolve to admitted
asset rows too. A missing or ambiguous subtask row is an explicit issue; no file
is guessed by URL or filename. Known upstream unavailable-resource metadata is
also an issue. Genuine metadata-only/quarantined/failed/oversized/missing/bad
files remain declared as such; their bytes are absent and `complete=false`.
An immutable manifest's supplied expected digest, or its nonempty cached-byte
digest at admission, must still match the current asset. A later cache mutation
cannot silently supply different bytes for an old revision. Metadata-only
placeholder digests with size zero are not mistaken for byte hashes.

An incomplete artifact is retained for operator inspection with exit **2**;
it must not be packaged as a ready catalog. Fatal schema/JSON/budget/output
errors before publication publish no new artifact and exit **1**. A filesystem
failure after the atomic publication can leave the already published file;
exit 1 still forbids packaging it, and the operator must inspect it before any
retry. Existing destination files are never replaced. Exit **0** requires a nonempty
catalog/selection and no asset/resource issues. `complete` describes the
authorized runtime snapshot, not coverage of the entire upstream task bank.

Hard producer bounds: 100,000 tasks, 250,000 revisions/selection entries,
100,000 assets, 500,000 links; JSON record <=4 MiB, individual source text <=2 MiB,
total source text <=256 MiB, JSON depth <=24/nodes <=100,000, per asset
20,000,000 bytes, total asset bytes <=2 GiB. The source SQL deadline is 120 s;
the whole export deadline is 600 s. Exceeding an aggregate budget is fatal.
Reports keep at most 1,000 issue examples and always retain complete per-code
issue counts. No body, URL, filename, path or raw exception appears in reports.
Path-resolution RuntimeError (including symlink-loop failures on affected
Python versions) is mapped to a fixed safe error. The CLI also catches other
unexpected ordinary exceptions at its final boundary, with exit 1, a fixed
JSON error and no traceback/raw stderr. It does not report such failures as
an empty or complete catalog.

## Metadata and hashes

Every `catalog_metadata.value` is canonical JSON (UTF-8, sorted keys,
`ensure_ascii=False`, separators `,`/`:`, no NaN). Required keys:
`format="kontur-catalog-seed-v1"`, `schemaVersion=1`, `sourceSchema=12`,
`producerSha256`, `createdAt` (UTC ISO-8601), `scope`, `limits`, `counts`,
`complete`, `issues`, `issueCounts`, `collectionsSha256`, `manifestSha256`.
Counters include the four row collections, selection, byte rows/total bytes,
denied revisions, other-source revisions, unresolved asset references and
suppressed current pointers. Issue examples contain only fixed codes, original
validated IDs and optional hashes identifying distinct manifest references.

For each collection in ID order, hash the concatenation of these canonical
JSON lines, including one LF after every line (empty collection: SHA256 of empty):

* metadata collections: `[id, <decoded record_json object>]`;
* selection: `[revision_id]`;
* asset bytes: `[asset_id, sha256, byte_length]`. The actual BLOB must independently
  hash to that SHA-256, so the digest transitively covers all bytes.

`collectionsSha256` maps the six collection table names to their lowercase
SHA-256 digests. `manifestSha256` hashes the canonical JSON object containing
all other decoded metadata key/value pairs. It excludes its own key and does
not include an LF. These are integrity checks, not signatures. The safe console
receipt additionally contains the SHA-256 of the closed final SQLite file.
An external operator must bind that file SHA to the reviewed transfer receipt.

Write to an exclusive sibling temporary file, commit/close/fsync, then publish
without overwriting an existing destination. Failure removes only that owned
temporary file. No migration, application import/startup, listener, model call,
remote command, deploy, or system configuration is part of the producer.

# Full offline transfer schema

This document is the contract for `full/backend/offline_transfer.py`.  The
writer produces a data-only SQLite file; the importer never executes SQL read
from that file.  A private read-only copy is checked before any local database
or asset-cache mutation.

## Container versions

| container | `application_id` | `user_version` | metadata format |
| --- | ---: | ---: | --- |
| legacy transfer | `0x4b4f4e54` (`KONT`) | `1` | `kontur-transfer-v1` |
| native mini store | `0x4b4f4e4c` (`KONL`) | `1` | `kontur-local-v1` |
| full Python backup | `0x4b4f4e46` (`KONF`) | `1` | `kontur-full-v1` |

All files use the ordinary SQLite rollback journal header (`read/write format
1`), have no WAL or sidecar transfer data, and are limited to 256 MiB.

## Full backup

The full format contains two fixed transfer tables and every table in the
current public SQLAlchemy `app.models.Base.metadata` (schema version 13).  The
application tables are copied with their trusted local DDL and rows; no
application table is reduced to the seven-table legacy subset.

```sql
PRAGMA application_id = 1263488582;
PRAGMA user_version = 1;

CREATE TABLE transfer_metadata (
  key TEXT NOT NULL PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE transfer_assets (
  asset_id TEXT NOT NULL PRIMARY KEY,
  storage_key TEXT,
  filename TEXT NOT NULL,
  media_type TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  bytes BLOB
);
```

Required `transfer_metadata` rows are `format=kontur-full-v1`,
`schemaVersion=1`, `backendSchemaVersion=13`, and the fixed local
`ownerId`.  Empty application tables are still present.  `transfer_assets`
contains one row for every application `assets.id`; ready assets include their
bytes, while a metadata-only asset keeps `bytes=NULL`.  A ready asset is
export-invalid when its bounded cache file is absent or its SHA-256 differs
from `assets.content_hash`.

Application table columns, declared types, nullability, primary-key order and
foreign-key targets must equal the trusted local ORM metadata.  The archive may
contain implicit SQLite auto-indexes only.  Additional tables, views,
triggers, virtual tables, or executable SQL are rejected.  Rows are read with
parameterised statements after schema validation and `integrity_check(1)`.

The full preview/result shapes are:

```json
{
  "formatVersion": 1,
  "format": "kontur-full-v1",
  "contentSha256": "<64 lowercase hex>",
  "counts": {"tasks": 0, "learning_events": 0},
  "conflictCount": 0,
  "warnings": []
}
```

```json
{
  "format": "kontur-full-v1",
  "contentSha256": "<64 lowercase hex>",
  "imported": {"tasks": 1},
  "duplicateRecords": 0,
  "backupPath": "<profile>/backups/...sqlite3",
  "warnings": []
}
```

The exporter publishes a new sibling without replacement.  The importer
requires either the stored preview digest or a native-supplied
`expected_sha256`, hashes the exact bytes used for validation, and rejects a
changed file before creating the application backup.  Rows with an existing
immutable primary key must be byte-for-byte equivalent after the fixed owner
mapping; a different body is an atomic `record_conflict`.  Exact duplicates
are skipped and counted.  Full imports preserve code, board snapshots,
preferences, attempts, exam state, learning events, assistant history and all
other application rows.

`OwnerSyncRecord` rows with `kind="offline_client_state"` retain their full
bounded JSON value (including multi-megabyte client history). The fixed record
ID is `offline-client-state-v1`, context is exactly `{"version":1}`, and revision
is a nonnegative safe integer. Importer and exporter must call the root module's
`offline_state.validate_client_state` before database or asset mutation; an
unavailable validator fails closed. Decoded UTF-8 value is bounded to 8 MiB.
The raw SQL JSON field allows 32 MiB because SQLite's SQLAlchemy serializer
escapes Unicode; this does not raise the decoded client-state limit.

The archive owner is metadata only.  On import, rows belonging to that one
owner are mapped to the process-established owner; records for another owner
are rejected.  No browser value can select an owner.

## Legacy `kontur-transfer-v1`

The seven legacy tables and their exact DDL remain importable:

```sql
CREATE TABLE metadata (key TEXT NOT NULL PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE assets (
  id TEXT NOT NULL PRIMARY KEY, name TEXT NOT NULL, mime TEXT NOT NULL,
  bytes BLOB NOT NULL, sha256 TEXT NOT NULL
);
CREATE TABLE tasks (
  source_id TEXT NOT NULL, revision TEXT NOT NULL, type_number INTEGER NOT NULL,
  title TEXT NOT NULL, statement TEXT NOT NULL, blocks_json TEXT,
  asset_ids_json TEXT NOT NULL, PRIMARY KEY(source_id,revision)
);
CREATE TABLE task_heads (source_id TEXT NOT NULL PRIMARY KEY, revision TEXT NOT NULL);
CREATE TABLE attempts (
  id TEXT NOT NULL PRIMARY KEY, task_source_id TEXT NOT NULL,
  task_revision TEXT, verdict TEXT NOT NULL, source TEXT NOT NULL,
  created_at INTEGER NOT NULL, code TEXT NOT NULL, answer TEXT NOT NULL
);
CREATE TABLE documents (
  id TEXT NOT NULL PRIMARY KEY, task_source_id TEXT, task_revision TEXT,
  title TEXT NOT NULL, type_number INTEGER, code TEXT NOT NULL,
  statement TEXT NOT NULL, board_json TEXT NOT NULL,
  revision INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE preferences (key TEXT NOT NULL PRIMARY KEY, value_json TEXT NOT NULL);
```

Legacy validation enforces the existing 256 MiB/50,000-task/500,000-attempt/
10,000-document/10,000-asset limits, 16 MiB per asset, 128 MiB combined asset
bytes, bounded JSON, safe filenames and MIME tokens, SHA-256 asset checksums,
and timestamps in `0..8_640_000_000_000_000` milliseconds.  Known task
references are linked only to the exact revision present in the bundle.  An
attempt or document whose source/revision pair is absent from the bundle is
retained with its original identifiers and body in the import ledger; it does
not create a synthetic task or revision and never affects current progress.  A
`NULL task_revision` attempt follows the same ledger-only rule.  Manual or
otherwise unproven verdicts are never promoted to a verified `LearningEvent`.

When a legacy task has `source_id="kompege:<external_id>"` and its revision
string exactly equals a local seeded `TaskRevision.content_hash` with the same
source identity, linked history reuses that seeded task/revision ID.  An
existing seeded task head is never reset by imported history; older solved
revisions remain addressable.

Legacy code and board payloads are represented in the normal code/board
version tables when a task revision is known; the complete original record is
also retained in `OwnerSyncRecord` import-ledger data.  An unlinked own-work
document (`task_source_id` and `task_revision` both `NULL`) additionally gets a
deterministic private `Task`/`TaskRevision` pair with `source_name="own"` and
`rights_status="private_authorized"`, allowing the ordinary solve-session,
code and board APIs to reopen it without making it part of the official
catalog.  Unknown provenance remains ledger-only.  Existing records with a
different body are rejected atomically; no silent overwrite occurs.

## Native mini-store migration

`migrate_mini_store(path)` accepts only a native fixed-path
`kontur-local-v1` SQLite file with `application_id=0x4b4f4e4c`, `user_version=1`,
the seven legacy tables above, and an additional local-only table:

```sql
CREATE TABLE import_receipts (
  source_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  local_id TEXT NOT NULL,
  PRIMARY KEY(source_id,fingerprint)
);
```

The source file is opened read-only, copied to a profile backup, and never
modified.  Migration is digest-idempotent: a durable import receipt is written
only in the same transaction as the application rows.  A repeated migration
returns `already_migrated` without another application backup or duplicate
rows.  Any validation, checksum, conflict, backup, asset, or transaction
failure leaves the application state unchanged and never logs user contents.

## Assets and paths

Asset bytes are copied only from the application-selected asset root.  Every
archive `storage_key` is relative, uses `/`, contains no empty/`.`/`..` path
component, drive prefix, control character, or symlink.  The importer resolves
the path beneath the target asset root and checks the bytes and SHA-256 before
promotion.  Asset files are written through a temporary sibling and removed on
failed import; no archive path is used as executable SQL or as an arbitrary
filesystem target.

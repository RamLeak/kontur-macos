# .kontur transfer v1

A dedicated SQLite data container, not the live application database and not an
SQL script. File header is `SQLite format 3\0`; `application_id=0x4b4f4e54`
(1263488596), `user_version=1`. All metadata keys/values are TEXT. Required
metadata rows: `format=kontur-transfer-v1`, `schemaVersion=1`. Optional rows:
`producer` (<=256 UTF-8 bytes), `createdAt` (decimal Unix milliseconds).
Header read/write format versions must both be 1 (rollback journal); no WAL,
external journal or sidecar data is part of this single-file format.

Exact DDL follows. Both SQL structure and columns/types/nullability/primary-key
order are validated. SQL comparison permits only whitespace/ASCII case changes
and an optional final semicolon; punctuation and token order must match. Quoted
identifiers, comments, IF NOT EXISTS, extra CHECK/DEFAULT/UNIQUE/REFERENCES clauses,
STRICT and WITHOUT ROWID are not part of v1. There are no other tables, explicit indexes, views, triggers, virtual
tables or generated/hidden columns. Implicit `sqlite_autoindex_*` entries are
permitted. Foreign references are checked independently of SQLite FK declarations.

```sql
PRAGMA application_id=1263488596;
PRAGMA user_version=1;
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
CREATE TABLE task_heads (
  source_id TEXT NOT NULL PRIMARY KEY, revision TEXT NOT NULL
);
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
CREATE TABLE preferences (
  key TEXT NOT NULL PRIMARY KEY, value_json TEXT NOT NULL
);
INSERT INTO metadata VALUES ('format','kontur-transfer-v1');
INSERT INTO metadata VALUES ('schemaVersion','1');
```

All runtime storage classes must match declared types exactly; nullable columns
allow NULL only. Integer values cannot be REAL or numeric TEXT. Text must be
valid UTF-8. All limits below count UTF-8 bytes, not characters. A file/row/value
over a limit fails validation before preview; the importer never truncates data.

| Item | Limit |
| --- | --- |
| Entire container | 256 MiB |
| Tasks (all revisions), task heads | 50,000 each |
| Attempts | 500,000 |
| Documents | 10,000 |
| Assets | 10,000; 16 MiB each; 128 MiB combined |
| Preferences | 100 rows including selection |
| Metadata | 4 rows; only the keys above |
| Opaque ID, revision, preference key | 1..256 bytes, no NUL/control characters |
| Title, asset filename | 1..512 bytes, no NUL |
| Asset MIME | 1..128 bytes, printable ASCII; one `/`, no parameters |
| Statement, code, blocks JSON, board JSON | 2 MiB each |
| Answer, preference JSON | 64 KiB each |
| Asset ID JSON array | 10,000 distinct IDs, 2 MiB JSON |
| Timestamp | integer 0..8640000000000000 (JavaScript Date range) |
| Document revision | integer 1..9007199254740991 |
| Task/document type | integer 1..27 (document may be NULL) |

Timestamp bounds apply to attempts.created_at, documents.updated_at and optional
metadata.createdAt. The upper bound is inclusive; larger safe integers still
exceed JavaScript Date's range and are rejected. Document revisions retain their
separate safe-integer upper bound 9007199254740991.

`blocks_json` is NULL or a JSON object/array. `asset_ids_json` is a JSON array of
distinct strings and every ID must resolve to assets. Asset names are display
names, never paths. SHA-256 is exactly 64 lowercase hex characters and must match
the bytes. `board_json` is an object; unknown board fields are preserved.
All JSON is parsed with serde_json's recursion limit; no invalid/deep JSON passes.

Each task source has exactly one head, referencing a stored revision. Every
attempt with a known task_revision references an existing exact task revision.
NULL task_revision preserves an original historical event whose revision is
unknown; task_source_id must still exist among tasks. Its original verdict, ID,
timestamp, code and answer remain unchanged. Such events never affect current
progress (including attemptCount/lastVerdict); UI labels their history
`Импортировано: версия неизвестна`. No synthetic historical task is created.
Every document has both task
reference columns NULL (own work) or both referencing an existing revision.
`verdict` is `correct|wrong|unverified|error`; `source` is `manual|imported`.
Events retain stable original IDs, timestamps, verdicts, code and answer. A
source/revision pair and an event ID are immutable: different bodies under an
existing identity fail the entire import with `record_conflict`.

`preferences.value_json` stores any valid bounded JSON. The reserved key
`selectedDocumentId` is a JSON string resolving to a document, or JSON `null`.
Other keys map directly to `Preferences.values`; namespaced bookmark keys are
supported without inventing a bookmark entity. A successful import fills only
missing preference keys and an empty selection, preserving local settings.

The single exception is reserved `kontur.legacy12.transferWarnings`: its value
must be an array of nonempty string codes (1..256 UTF-8 bytes, no control
characters). Unknown future codes are accepted. If present in an import, its
codes are unioned with the local array, deduplicated and sorted lexically; an
absent key leaves the local value untouched. Null, non-array values and invalid
elements in either input or local state are rejected without coercion or loss.
The merged array must still fit the existing 64 KiB preference limit; overflow
fails before backup or mutation. Warning-only changes use the same backup and
transaction as other imports. Reimporting known codes causes no redundant write.

Import reads a bounded file into memory, hashes those exact bytes, validates a
private temporary copy opened READ_ONLY without URI support. Extension loading
is disabled; trusted schema is disabled through the connection config before SQL.
The exact permitted schema SQL is checked before integrity checking so imported
CHECK expressions cannot run. ignore_check_constraints is also ON as defense.
Full `integrity_check(1)`, including primary-key index/table consistency, must
return `ok`; quick_check alone is insufficient. Columns, runtime types, limits,
JSON, references and checksums all pass
before preview or local mutation. Native token confirmation binds contentSha256;
changed bytes fail with `import_changed` before backup or transaction.

Merge retains every task revision. A newly encountered head revision becomes
current. Reimporting an already-known old revision never rolls back a newer head.
Progress is derived for the current exact revision; old solved/error history is
retained without applying it to changed content.
The latest evaluated event decides current status: correct => solved,
wrong/error => error; unverified preserves the latest evaluated status.
everSolved records any earlier correct event for the same exact revision and
does not hide a later error. lastVerdict/lastAttemptId still include unverified
history. With no evaluated event, current-revision events or a linked document
mean in_progress; otherwise not_started. Unknown-revision events are excluded.

Document identity conflicts keep both: the imported variant receives a
deterministic `doc-import-<sha256>` ID. A local receipt associates the original ID
and complete imported document fingerprint with that local ID; repeated import
does not duplicate or overwrite even if the retained copy is subsequently edited.
Receipts are local implementation data and are not extra archive tables.

After validation and merge planning, native makes a SQLite online backup into
its own `backups/` directory and flushes that completed backup. All database
changes and receipts then run in one IMMEDIATE transaction with FULL synchronous
COMMIT. Any failed transaction rolls back. Invalid input makes no data change.
An import which inserts no records or settings needs no redundant backup; its
result has an empty `backupPath`. Export writes a new complete container to a
temporary sibling, flushes it, and publishes it using native no-replace
MoveFileExW WRITE_THROUGH; choose a new destination when `destination_exists`
is returned. Before loading export bodies, fixed SQL counts and sums of UTF-8/BLOB
bytes reject over-limit local datasets. The writer also sets page_size=4096 and
max_page_count=65536 before schema/inserts, bounding the physical SQLite file to
256 MiB even when page/index overhead exceeds the payload estimate. Failure
publishes no partial destination; the temporary file is removed.

Python producers use standard sqlite3 and hashlib with parameterized inserts;
do not copy live app DB tables, auth, `.env`, sessions or executable SQL.
Fixtures and tests must be synthetic.

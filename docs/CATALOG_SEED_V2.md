# Catalog seed v2 admission contract

Status: contract for the offline consumer and derived-seed finalizer. No network
operation or genuine complete snapshot is implied by this document.

## Exact wire fields

The seven tables, column definitions, SQLite `application_id=0x4b435331`, and
SQLite `user_version=1` remain unchanged: v2 changes metadata semantics, not the
SQLite storage schema. `format` and `schemaVersion` must be paired exactly:

| Metadata | v1 | v2 |
| --- | --- | --- |
| `format` | `kontur-catalog-seed-v1` | `kontur-catalog-seed-v2` |
| `schemaVersion` | integer `1` | integer `2` |
| `sourceSchema` | integer `12` | integer `12` |
| `offlineReady` | forbidden | boolean `true` |
| `sourceSeedSha256` | forbidden | lowercase 64-character SHA-256 of the unmodified input seed file |

Retain all 13 v1 metadata keys: `format`, `schemaVersion`, `sourceSchema`,
`producerSha256`, `createdAt`, `scope`, `limits`, `counts`, `complete`, `issues`,
`issueCounts`, `collectionsSha256`, `manifestSha256`. Thus v2 has exactly 15
keys; unknown metadata keys fail. Integer fields reject JSON booleans.

## Completeness and warnings

v1 admission stays unchanged: `complete=true`, no issues and no nonzero issue
counts. It never accepts source warnings or missing admitted asset bytes.

v2 distinguishes source completeness (`complete`) from admitted-byte readiness
(`offlineReady`). Reconstruct affected revision IDs from every retained
revision's `source_metadata.kege_unavailable_resources`. The value is an array;
every nonempty marker must be an object with `reason="origin_not_admitted"`.
Other reasons or malformed metadata fail. Count affected revisions, not markers.

If there are no affected revisions, require `complete=true`, `issues=[]`,
`issueCounts={}`. Otherwise require `complete=false` and exactly:

```json
{
  "issues": [
    {"code": "upstream_resource_unavailable", "revisionId": "<affected revision ID>"}
  ],
  "issueCounts": {"upstream_resource_unavailable": 1}
}
```

The example above represents one affected revision. Include every affected
revision exactly once; order does not matter. Issue objects have exactly the two
shown keys. No unknown issue codes, extra fields, duplicates, missing or unrelated
IDs, fabricated counts, or capped examples are accepted. The finalizer must
regenerate this full list from retained revisions (the genuine candidate has 30
affected revisions). `complete=false` without warnings fails.

## Unchanged integrity and admission gates

Every admitted asset must have one byte row, ready cache state, verified hash,
correct content/expected hash and size, admitted rights/source, and no storage
key. Retain all existing row/text/JSON/byte bounds, task/revision/link/selection
checks. Reconstructed counts must match, including `unavailableAssets=0` and
`unresolvedAssetReferences=0`. Warnings never excuse missing admitted bytes,
unresolved references, hash mismatch, or `asset_not_ready`.

Retain original tasks, revisions and selection. Retain revision metadata,
including unavailable-resource markers. The source seed hash provides provenance,
not a signature or permission to remove source records. The finalizer must verify
the unmodified input hash and preserve those collections independently.

Recompute all six collection hashes using the v1 canonical JSON-line rules, then
recompute `manifestSha256` over every metadata field except itself, including the
two new fields. All metadata JSON remains canonical. The consumer independently
hashes every BLOB and reconstructs collection hashes and counts before import.

The derived v2 `limits.assetBytes` and `limits.totalAssetBytes` describe its
actual producer bounds. Other limit fields remain source-export provenance.
On 2026-10-30, public HEAD responses measured admitted text attachments up to
50,084,031 bytes. The downloaded snapshot has 2,586,107,968 logical bytes before
the owner-approved 134,074-byte official replacement. The producer/consumer
per-asset bound is 64,000,000 bytes and the per-asset-ID total bound is 3 GiB.
Full `.kontur` transfers use the same asset bounds and a 3.5 GiB container bound,
with bounded streaming reads. Legacy containers retain their original limits.
The input v1 and its original limits remain unmodified.

The existing genuine v1 candidate remains INCOMPLETE and must fail admission until
a separately verified byte-complete derived v2 seed exists. Never relabel v1.

## Focused verification (2026-10-30)

Interpreter: `C:/Projects/Kontur/.venv/Scripts/python.exe -B`, original installed
dependencies; no dotenv loading, network requests, GUI, model or user profile.

- `pytest tests/test_offline_catalog_v2.py -q -k 'not bootstrap'`:
  62 synthetic admission checks pass, covering v1 compatibility, exact v2
  warnings, missing/corrupt admitted bytes, rights/cache/hash/size gates,
  unresolved references, counts, metadata and collection hashes.
- `pytest tests/test_offline_catalog_v2.py -q -k bootstrap`: one synthetic
  application startup/import check passes in approved host context. The initial
  sandbox attempt was interrupted after a diagnostic stack identified a block
  in Windows asyncio `_fallback_socketpair`, before application creation. That
  interrupted attempt is not counted as a pass.
- Ruff checks and Mypy (`--follow-imports=silent --cache-dir=nul`) pass for
  `offline_catalog.py` and `tests/test_offline_catalog_v2.py`.
- The genuine v1 candidate fails both direct admission and `load_catalog_seed`.
  The bootstrap probe redirects only its seed-opening seam to the read-only
  candidate and observes the real validator rejecting it before import. Its
  file SHA-256 is unchanged before/after:
  `a18b55186b42e6d11142349f92bbb83ff34aff356d2758067d9f26e53392b051`.

That initial focused step did not establish a genuine byte-complete v2 snapshot
or a packaged runtime.

## Genuine v2 integration — 30 October 2026

The finalizer produced a new 2,632,617,984-byte SQLite file, SHA-256
`d37628fb23b7253564e4a7eeea84e1d5eb02900a9bbed921d447e4bddd4b01ab`.
Its metadata manifest SHA-256 is
`6020119aeaa4319ab9829d29461fb650416960d9c571f825fd7fc8bffd8d6bdf`.
Actual `open_seed` / `validate_seed` then verified every BLOB, collection hash,
count and warning. A separate immutable read-only audit checked domain equality,
warning IDs, genuine pins, sentinel evidence and largest/recovery BLOB hashes.

There are 6,495 tasks, 9,176 revisions, 8,248 selected revisions, 5,487 links and
3,773 admitted byte rows containing 2,586,242,042 bytes. Missing admitted bytes
and unresolved references are both zero. Source task/revision/link/selection
rows remain byte-for-byte equal; the v1 SHA-256 is unchanged. Exactly 109
metadata-only sentinel expected hashes were normalized using receipt evidence;
genuine expected hashes were retained.

The deleted historical `27.txt` URL for task31521 was replaced only after the
owner explicitly delegated that choice. Its replacement is the authentic
134,074-byte `Файлы/demo_27.txt` from the official FIPI-2027 archive, matching
the genuine pin of the same task's current asset. Historical byte identity is
unknown. `resources/catalog/recovery-provenance.json` binds the original asset
fingerprint, approval, 404, official fetch/archive/member hashes, snapshot and
the exact derived file/metadata hashes. It is pinned in the resource manifest.

The producer returns exit2 with `offlineReady=true`, `complete=false` and the
same 30 `upstream_resource_unavailable` warning revision IDs. This records source
limitations; it does not waive any admitted-byte, type, size or hash gate.
Packaged application execution and manual GUI acceptance are separate checks.

# Full backend local adapter — transport 1

This folder contains the original public Python application and tests. Production
starts `offline_bridge.py --stdio --profile <owned>/full --bundle <resources>`;
optional `--legacy-store <owned>/data/kontur.sqlite3` is native-only. No HTTP
listener is created. No .env, existing credentials, live server DB or user profile
is copied. `Settings` is constructed explicitly, never from inherited EGE_*.

`offline_app.py` owns startup and the in-process ASGI adapter. The only owner is
the server-established `owner`; a private scope marker is required by the owner
dependency. There is no browser-selected owner, dev/testing bypass or login
secret. Original owner-scoped queries, domain transactions, events, idempotency
and exam restrictions remain in the copied application.

## JSONL and reverse calls

Input is one UTF-8 JSON object per line, at most 32 MiB. Native request IDs are
nonnegative signed 64-bit integers, unique among in-flight requests. Up to four
requests execute concurrently and a bounded queue rejects overload safely.

- `{id,op:"request",method,path,body?:string,headers?:{string:string}}` =>
  `{id,ok:true,status,headers:[[name,value],...],bodyBase64:string}`.
  Only GET/POST/PUT/PATCH/DELETE and `/api/v1/` paths are accepted. There is no
  auth/cookie header field. Optional headers are case-insensitively limited to
  Content-Type, Idempotency-Key and X-Sync-Owner (three entries, each <=1024 bytes,
  no controls). X-Sync-Owner must equal the process-established owner and cannot
  select identity. JSON request bodies are at most 8 MiB; raw responses at
  most 20 MiB. Errors are `{id,ok:false,error:{code,message}}` with fixed safe text.
- `{id,op:"flush"}` => `{id,ok:true,result:{committed:true,pendingMutations:0}}`, after earlier
  requests drain and SQLite performs a FULL WAL checkpoint.
- `{id,op:"import_preview",path:string}` => `{id,ok:true,result:ImportPreview}`.
- `{id,op:"import_bundle",path:string,expectedSha256:string}` => `{id,ok:true,result:ImportResult}`.
- `{id,op:"export_bundle",path:string}` => `{id,ok:true,result:ExportResult}`.

Native chooses all paths; the browser has no transfer-path API. Native attaches
its one-use preview token and SHA lease. Python also remembers the validated
preview digest and rejects changed bytes on confirmation. The programmatic
legacy/installation bootstrap path uses the same validation without a UI prompt.
Import/export/flush use a quiescent barrier; the stdio reader remains active for
gateway results throughout. Stdout is protocol only, not application logs.

Phase 3 owns `local_codex.py` and `tests/test_local_codex.py`:

```python
LocalCodexClient(gateway_call)  # positional callback
gateway_call(operation: str, payload: dict) -> dict
```

The callback is synchronous and bounded, and may be called on a request worker.
The client constructor must be pure: no reverse call before the ready handshake.
It emits `{event:"gateway",callId,operation,payload}`. The reader accepts
`{op:"gateway_result",callId,ok:true,result:object}` or the safe error envelope.
`offline_protocol.GatewayCallError` exposes `code` and safe `message` on failure.
Operations are exactly codex_status/start/poll/cancel, using Rust DTOs. The client
must implement the original synchronous `capabilities`, `run`, `status`, `events`
and `cancel` gateway contracts, retaining hashes, requested/effective model,
effort, run IDs, event ordering and original context/exam policy. `app.state`
injection reaches the original API through `api_common.get_db` session.info.
Flush fences new client starts and drains ASGI response/background work.
`GatewayCalls.quiesce()` checks native status, cancels an active native ID
(including orphaned requests whose API transaction failed), and requires a
terminal response and an idle status. It then cancels active assistant and
Study AI runs through the original private API to commit their audit/status
writes. A client-specific
`quiesce()` is optional. Only after a second background drain and a full WAL
checkpoint may flush acknowledge zero pending writes. The start fence is released
after a non-closing flush so a later explicit Send remains possible.

Native cancel first requests termination; its initial reply may still be running.
The bridge polls for up to 30 seconds and requires a terminal reply plus idle
native status before persisting API snapshots. If a restarted child cannot
recover an in-memory run mapping, remaining active database runs are recorded
with the original failure/audit helpers as `LOCAL_RUNTIME_INTERRUPTED`. Partial
history is retained; no success output, grading or visible Study AI help is made up.

## Transfer result schemas

`ImportPreview = {formatVersion:int,format:string,contentSha256:string,
counts:{[entity:string]:int},conflictCount:int,warnings:string[]}`.
`ImportResult = {format:string,contentSha256:string,imported:{[entity:string]:int},
duplicateRecords:int,backupPath:string,warnings:string[]}`.
`ExportResult = {format:string,path:string,contentSha256:string,
counts:{[entity:string]:int}}`.

The original seven-table `kontur-transfer-v1` remains importable. The full backup
is data-only and preserves every original application table and required asset
bytes; its exact schema/limits/conflict rules are in `TRANSFER_SCHEMA.md`.
Conflicting state must be retained or rejected atomically, never silently lost.
Migration retains the original mini DB and a backup, is digest/idempotency based,
and does not log user content. Unsupported/unproven legacy history remains
unverified; it cannot fabricate grading or official source provenance.

## Catalog and runtime readiness

`--bundle` is installation-owned, not a project directory. The genuine bank seed
is `catalog/catalog.sqlite3` under the bundle, as specified in
`../../docs/CATALOG_SEED_FORMAT.md`; startup automatically loads a validated
seed and assets and fails readiness for a missing/incomplete bank.
There is no sample/demo fallback and no startup network dependency.

ROOT approved the existing read-only
`C:/Projects/Kontur/.venv/Scripts/python.exe -I -B` runtime for synthetic checks.
Phase 4 packages the existing dependency closure; no new production dependencies
are approved. These checks do not verify the shipped runtime.

Parity blockers tracked explicitly: a genuine seed has not been supplied;
the original server code-run routes require an attested isolated runner and are
not made available by falsely advertising a browser Pyodide result as that
service. The frontend/native owners must establish the approved sandboxed-run
seam before claiming code-run history parity.

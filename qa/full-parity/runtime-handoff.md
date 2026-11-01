# ROOT handoff: ignored packaged backend boundary

Status: **GENUINE_RUNTIME_PASSED**, 2026-10-31 (local date).
ROOT's offline native default gate passed with 93 tests and 9 ignored tests;
`qa/native-full-test.json` records exit0 and unchanged production source hashes.
The explicitly selected genuine packaged gate then passed 1/1 in 558.25s,
including largest50,084,031-byte asset verification, durable state/reopen and
full2,628,526,080-byte export/preview/import. Owned synthetic fixtures were cleaned.
`artifacts/genuine-packaged-runtime-proof.json` pins source/test/resource hashes
and aggregate metadata; its SHA-256 is
`22c8eaeabe66da2443206d44cc3fc1b37310aa9da3c503ae4fd099f7bc6cb02e`.
Resource manifest SHA-256:
`0c0100f69f17dce61ec1df173621af6335416f8d19c2ea27548ef09aa9352cd3`.

Cold startup measured116–121s and warm startup47–98s on this host. Full export
took40.7s; preview/import/read together114.3s. These timings are observations,
not speed guarantees. The initial failed attempt and controlled Windows path
diagnostics remain in artifacts; production fixes passed independent review and
the full native gate before the successful retry. GUI/Tauri invoke and real AI
remain unverified.

## Owned changes and actual boundary

The test surface is `native/tests/packaged_backend.rs` and this handoff. ROOT's
integrated production path/frame fixes and resource preparation are recorded in
`docs/FULL_PARITY_STATUS.md`; the original server and installed profile were not
used by this test. No new dependency. Test uses production backend.rs through
a path module and the library's public platform/domain/codex modules. Its base64
helper is test-only and strict: client-state JSON remains capped at 64 KiB,
and the separate attachment path permits at most 64,000,000 decoded bytes.

The ignored test exercises **production Rust BackendService → private JSONL →
actual bundled Python → original ASGI/SQLite plus local client-state adapter**.
It creates two independently initialized profiles below one uniquely created,
marked OS-temp fixture. No actual app AppData profile is selected. The only HTTP
state route requested is GET/PUT `/api/v1/local/client-state`; the only user value is
the harmless boolean `kontur:assistant-technical-mode:v1`. It performs no
capabilities, assistant, status, login or model call. CodexService constructor is
idle. No GUI/WebView, loopback service, external data upload or host code runner.

Sequence: empty profile → largest seed attachment GET/size/SHA check
→ boolean/version write/read → committed/pending0 flush
→ owned backend shutdown → same-profile warm reopen/read/update → full export
→ second fresh profile → digest-bound preview/import/read → flush/shutdown
→ second-profile warm reopen/read/final flush → cleanup.

The runtime may bootstrap the reviewed genuine bank from resources. A read-only
SQLite query selects only `asset_id`, `sha256` and `length(bytes)` for the largest
`catalog_asset_bytes` row, without fetching seed BLOBs. One GET to that asset's
download route must return status 200, the same decoded size and the same SHA-256.
Its bounded response Vec is released before full transfer begins. The test never
prints attachment bytes, IDs, names, URLs, task statements, solutions, code,
imported records, archive digest/path or raw application errors. Assertions use fixed safe
messages. Exported genuine bank bytes stay in the owned temporary fixture and are
deleted after child shutdown. This is synthetic **user state**, not a fake-bank
receipt. A missing/invalid bank prevents ready and fails the gate.

## Preconditions and commands — ROOT only

ROOT must wait for its resource packaging and the exclusive lease, verify the
actual resource closure/manifest SHA, backend source pins, Python/dependency
closure and bank provenance before setting the resource path. No autodiscovery,
fallback to development Python, dependency installation or placeholder seed.
Do not use the currently installed user profile. Run on Windows; the test is
Windows-only and a non-Windows zero-test result is not a gate.

The ignored test additionally requires the explicit ROOT GO marker below.
Missing marker, missing resource env, relative path or absent python/bridge fail
before process launch; there is no successful skip. Setting a marker is an
operator input, not cryptographic authorization or an automatic new approval.

After ROOT's GO, from kontur-offline, substitute only the reviewed absolute
resources directory (contains python/python.exe, backend/offline_bridge.py and
catalog/catalog.sqlite3):

```powershell
if ($env:OS -ne 'Windows_NT') { throw 'Windows runtime gate required' }
$env:KONTUR_PACKAGED_TEST_RESOURCES = '<reviewed absolute resources directory>'
$env:KONTUR_PACKAGED_TEST_GO = 'ROOT_GO_PACKAGED_BACKEND_V1'
$packagedGateExit = $null
try {
  $packagedGateCargo = Get-Command cargo -CommandType Application -ErrorAction Stop
  & $packagedGateCargo.Source test --manifest-path native/Cargo.toml --offline --locked --test packaged_backend packaged_backend_runtime_roundtrip -- --ignored --exact --nocapture --test-threads=1
  $packagedGateExit = $LASTEXITCODE
} finally {
  Remove-Item Env:KONTUR_PACKAGED_TEST_GO -ErrorAction SilentlyContinue
  Remove-Item Env:KONTUR_PACKAGED_TEST_RESOURCES -ErrorAction SilentlyContinue
}
if ($packagedGateExit -ne 0) { throw 'Packaged backend gate failed' }
```

The initial P6 handoff had not compiled this integration target. ROOT has since
compiled it through the default native gate without running the ignored runtime.
Use --offline/--locked with the already prepared runtime/toolchain; missing
dependencies are a blocker, not permission to download them. The exact test-name
filter and --ignored select only this gate, even though including backend.rs
also compiles its existing unit-test module.

## Evidence and limitations

- Capture exit status, exact command, test/source/resource-manifest SHA and
  `KONTUR_PACKAGED_BACKEND_METADATA` line in a ROOT-owned receipt. It contains only
  fixed scope labels, pass booleans and durations. No private response printing.
- Timings: initial startup, largest seed attachment download, warm startup, write/read roundtrip, each durable flush,
  full export, target startup, preview/import/read and target warm startup. They
  are measured metadata, not promises or a complete performance benchmark.
- The current test uses the same nonblocking `BackendService::launch` as the
  native window. `nativeLaunchMaxMs` records the slowest launch (must remain
  below 5000 ms); separate readiness timings still wait for the complete bank
  and an actual state request. The warm path uses a disposable seed/file stamp
  cache; first or changed files are fully checked and asset delivery still
  verifies the content hash. GUI first-click/window display is not exercised.
- `largestSeedAssetVerified` records only the fixed success boolean. A separate
  nonignored decoder unit test exercises canonical padding, decoded bounds and
  the unchanged 64 KiB JSON state cap without resources or a backend process.
- BackendService owns its spawned child Job. Every started backend has a Drop
  guard; normal sequence explicitly shuts it down before reopening/cleanup.
- Cleanup validates absolute canonical root, exact temp parent, unique prefix and
  ownership marker. It preflights then rechecks every child with symlink_metadata
  and Windows REPARSE_POINT before deletion. It never uses remove_dir_all and
  refuses junctions/symlinks/special files. Unsafe/failed cleanup preserves the
  fixture and fails normal completion. This is not protection against a malicious
  concurrent same-user filesystem attacker.
- Preview uses the concrete documented backend schema `{path}`; confirmation
  carries expectedSha256; full archive format is exactly `kontur-full-v1`.
  The separate Tauri Settings wrapper now sends the same exact `{path}` preview
  schema (source review, `native/src/commands.rs`). This runtime test still does
  not exercise the wrapper, the native dialog or the frontend Settings flow.
- This is **not** Tauri command ACL/opaque-frame IPC, GUI save-close, a native file
  dialog, real model, full asset coverage, legacy migration, all 112 routes or all
  functions/design acceptance. Original backend/frontend suites cover domain
  breadth; matrix gates remain separate. No optional mini migration was added to
  keep this gate focused on the packaged boundary.

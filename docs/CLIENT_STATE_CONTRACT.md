# Offline client state v1

Owner: phase 2 frontend schema; phase 1 implements the private local API.
This supplements the original 11 shared preferences without changing that API.
No new table or dependency. One fixed-local-owner `OwnerSyncRecord`, kind
`offline_client_state`, stable record/context identity, context version 1.
The browser never supplies an owner or record ID.

## API and archive

`GET /api/v1/local/client-state` and successful PUT return:

```json
{"version":0,"value":{"version":1,"entries":{}}}
```

Absent record is revision 0 with empty entries. PUT body:

```json
{"expectedVersion":0,"value":{"version":1,"entries":{"kontur:assistant-technical-mode:v1":false}}}
```

`version` and `expectedVersion` are nonnegative safe integers (<= 9007199254740991).
PUT is one transaction: validate everything, compare expectedVersion, replace the
whole value, increment revision exactly once, commit. Stale revision => HTTP 409;
invalid payload => HTTP 422; oversized payload => HTTP 413. GET/PUT are private
local-only routes. Value, entry schemas, and envelopes reject unknown fields.
At most 4096 entries. Entire UTF-8 PUT JSON <= 8,388,608 bytes; no truncation.
`value.version` is the schema version, distinct from the outer revision.
The existing full archive's owner_sync_records roundtrip includes this record;
import validates its value with the same schema before any mutation. A legacy
archive without the record remains valid and does not erase existing settings.

## Exact persistent-key allowlist

Only the following original localStorage keys are admitted. Entries hold typed
JSON, not raw JSON strings. Missing key means the corresponding localStorage
key is absent. Numeric panel splits and technical mode use numbers/booleans in
the API and canonical String(value) in localStorage. All other values use JSON.

| Key | Value |
| --- | --- |
| `kontur:interface-preferences:v1` | Partial Preferences object below, <=16 KiB |
| `kontur:shortcut-profile:windows:v1`, `kontur:shortcut-profile:macos:v1` | Partial ShortcutProfile below, <=16 KiB |
| `kontur:workspace-split:v3`, `kontur:workspace-split:v2` | finite number 0..100 |
| `kontur:assistant-split:v1:narrow`, `:standard`, `:wide` | finite number 0..100 |
| `kontur:assistant-technical-mode:v1` | boolean |
| `kontur:ui:task-bank:filters:v1` | partial Filters below, <=8 KiB |
| `kontur:ui:task-bank:return:v1` | ReturnState below, <=8 KiB |
| `kontur:exam-ui:v1:<id>` | ExamUi below, <=64 KiB |
| `kontur:study-checkpoint:v1:exam:<id>:item:<id>` | `{runId,question}`, runId is ID, question string <=20000 UTF-16 code units |
| `kontur:device-python-runs:v1:<encodedScope>` | array of <=20 CodeRun values below; also subject to the total 8 MiB bound |

ID = `[A-Za-z0-9][A-Za-z0-9._:-]{0,159}`; no control/path characters, and not
`__proto__`, `prototype`, `constructor`. Python run scope is
`practice:<id>:browser` or `exam:<examId>:<sessionId>:item:<itemId>:browser`,
with exactly three nonempty colon-free IDs in the exam scope, encoded exactly as JavaScript encodeURIComponent
(canonical uppercase percent escapes; colon becomes `%3A`). No opaque arbitrary
storage-key prefixes. Dynamic-key length <=512 UTF-16 code units.

Preferences fields (all optional, no unknown keys):

- theme: system/light/dark; textSize: standard/large; density: comfortable/compact;
  editorTheme: interface/light/charcoal; boardBackground: blank/grid/dots;
  assistantWidth: narrow/standard/wide; hintMode: hint/explain;
  model: auto/luna/terra/sol; effort: low/medium/high/xhigh;
  tutorMode: hint/hint_ladder/feynman/socratic/debug/review/compare/full_solution.
- reducedMotion, autoHideNavigation, rightPanelVisible, restoreWorkspace,
  editorWrap, editorLineNumbers: boolean.
- defaultTaskType: integer 1..27; editorFontSize: 12/14/16; editorTabSize: 2/4/8;
  fileCacheMb: 32/64/128/256/512/1024.
- outputHeight: finite 100..360; boardWidth/boardMarkerWidth: finite 1..24;
  boardOpacity/boardMarkerOpacity: finite 0.1..1; drawingSmoothing: finite 0..1;
  boardColor/boardMarkerColor: string matching `^#[0-9A-Fa-f]{6}$`.

ShortcutProfile keys are exactly the original command IDs:
assistant.toggle, panel.toggle, focus.toggle, surface.toggle, code.run,
answer.check, task.previous, task.next, task.bookmark, board.pen, board.marker,
board.pan, board.line, board.rect, board.ellipse, board.text, board.eraser,
board.undo, board.redo, board.clear, board.zoom.in, board.zoom.out,
board.zoom.reset. Values are null or a shortcut string: zero to four distinct
modifiers Ctrl+, Alt+, Shift+, Cmd+ in any order, then one non-whitespace,
non-control, non-plus token of 1..20 characters. A modifier alone is invalid.
Reserved combinations are rejected exactly as original
`lib/kontur-shortcuts.ts:isReservedShortcut`; frontend also requires unchanged
`normalizeShortcutBinding(keyToken) === keyToken`. Modifier order is retained:
the original macOS redo default is Cmd+Shift+KeyZ, while its setter serializes
Shift+Cmd+KeyZ. Both valid original forms must roundtrip.

Filters fields (optional): line integer1..27; query string<=500; year one of
Все годы/2027/2026/2025/2024; difficulty Все сложности/easy/medium/hard;
files Все файлы/С файлами/Без файлов; learningStatus empty/neutral/started/
incorrect/correct; bookmarksOnly boolean. ReturnState has exactly filterKey
(a JSON string of Filters, <=4096 code units), revisionId (ID), scrollTop finite
0..1000000000. Parsed filterKey must itself validate as Filters.

ExamUi has exactly itemId (ID or null), flags (map ID=>boolean) and surfaces
(map ID=>code/board), <=512 members per map. Study checkpoint fields are exact.
These are UI hints and cannot authorize or unseal an exam.

CodeRun required fields: id(ID), status(queued/running/succeeded/completed/failed/
timed_out/unavailable), stdout/string and stderr/string (each <=200000 UTF-16
code units), duration_ms(null or finite 0..1e12), exit_code(null or integer
-2147483648..2147483647), created_at(ISO UTC timestamp with milliseconds,
`YYYY-MM-DDTHH:mm:ss.sssZ`, valid date). Optional code_version(null or safe
nonnegative integer), reason(null or string<=2000). No unknown fields.

## Exclusions and frontend lifecycle

Never export localStorage wholesale. Exclude auth/signed-out/session authority,
owner IDs, owner preference caches, all draft/recovery/idempotency queues,
device-file caches/IndexedDB/pins, CLI home/auth, environment, cookies and paths.
Original sessionStorage cursor/surface/catalog navigation remains session-only.
For the private single-owner app, global preferences are canonical; the original
shared preference record still synchronizes its 11 fields without a stale
per-owner localStorage overlay.

Frontend hydrates before original screens mount. One serialized optimistic
writer saves changes; a compact non-exported local outbox stores baseVersion and an optional
inflight {expectedVersion,valueSha256}. The canonical allowed localStorage
values hold the desired recovery data. Write the marker before changing an
allowed key. SHA-256 hashes the stable JSON snapshot (object keys sorted by
Unicode code point, arrays retain order, UTF-8). This recovers a lost response
without duplicating multi-megabyte histories or overwriting conflicts.
Register this writer in the original draft flush/save-state barrier. Await it
before import/export/close. After import reload the committed client state
before rendering original screens again. No local-only recovery is a durable
ACK. Errors/conflicts/oversize preserve local data and block confirmed transfer
or close; do not silently prune Python history. The agreed 8 MiB transport admits the original 4 MB saved history without
trimming. Larger aggregate or Unicode-heavy histories still fail safely at the
explicit total bound and remain on device. Native and Python consumers must
use the same 8 MiB request body limit; response/frame bounds stay unchanged.

An explicit remote/backup conflict choice discards superseded failed local write intents, so a later flush cannot replay them over the chosen backup. Reconciliation clears the prior transport error after a verified remote ACK.

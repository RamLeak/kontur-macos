# Full original Kontur: parity and evidence matrix

2026-10-29 — **SOURCE_ONLY / INCONCLUSIVE; product integration NOT_RUN**.
Scope: `FULL_PARITY_CONTRACT.md`, original public source `C:/Projects/Kontur`
read-only. This is a requested verification artefact for the user's full offline
implementation; it does not change implementation or authorize runtime work.

Source inventory: **112 mounted backend endpoints** (111 `/api/v1/` + `/health`
alias), **10 page routes**, **1 Pyodide resource route**, **56 model tables**.
Counts came from stdlib AST/source enumeration, without importing the app.
All six router mounts are in original `backend/app/main.py:104-108`.
Re-extract after baseline changes. Source location references below are baseline
references, not claims that copied code has already passed integration.

Empty responses, absent bank, stubs, demo/synthetic tasks, skipped tests and old
mini-app gates never satisfy full parity. Every gate needs final source/artifact
SHA, command/environment, exit status, case IDs, independent oracle and omissions.
Relevant direct evidence may produce PASS; a missing layer remains NOT_RUN or
INCONCLUSIVE; a demonstrated contradiction is FAIL. No averaging hides a red gate.

## Functional matrix

Every row is currently **NOT_RUN** for final packaged full integration. The narrow
candidate-CSP fixture is recorded separately under `qa/full-parity/python-sandbox*`
and cannot turn installed application, IPC, design or bank gates green.
Its final six synthetic functional/isolation cases passed, exit 0, with no page
errors (`python-sandbox-final-result.json`). ROOT subsequently recorded explicit
human approval and applied exactly the reviewed CSP plus the existing IPC connect
sources. That source gate is satisfied; target WebView2/native IPC integration
and packaged Python execution remain unverified.

| ID | Original observable behavior and invariant | Case / required evidence |
| --- | --- | --- |
| P01 | Startup, health/capabilities and fixed private owner. No listener, inherited secret, browser-selected owner, host runner or startup inference. Original cookie ceremony is deliberately replaced by private process context. | S01/S15; packaged process and bootstrap receipt; owner-negative cases. |
| P02 | `/`, `/tasks`: catalog search, line/source/difficulty/status/skill/bookmark filters and combinations, ordered pagination, published/current/history revision rules, detail, coverage and progress. | S02; exact selected IDs/order/count/cursor oracle; genuine bank receipt required. |
| P03 | Source Settings: provenance, import idempotency, health and resumable sync state. Network refresh is adapted to explicit local file import by contract; UI explains the local operation. | S02/S14; controlled-export provenance, expected nonzero counts/source IDs and asset coverage. Missing seed is blocking. |
| P04 | Original/image/binary files, prepare/download/text/device/metadata. Exact bytes/digest/MIME, revision pin, spreadsheet conversion and safe names. Missing/corrupt bytes cannot appear available. | S03; original `device_assets.py`, `test_device_assets.py`, `test_type9_file_inputs.py`; actual stdio byte/header checks. |
| P05 | `/chat`, projects, conversation title/project/archive/delete and ordered messages; archive/restore preserves solve/code/board/version graph. | S04; original `api.py:1651-1872`, `test_conversation_archive.py`; graph comparison and reopen. |
| P06 | `/workspace`: solve lifecycle, code CAS/versions, board geometry/colors/history, annotation/eraser lifecycle, immutable attempts, authoritative answer check distinct from run output and current-revision progress. | S05/S06; `api.py:1874-2990`, check/annotation/contracts tests; stale-write and replay oracles. |
| P07 | Assistant context/options, explicit Send, start/poll/cancel/events, selected context/sample hashes, requested/effective model+effort and audit. SSE becomes local polling; owner-wide sealed policy remains. | S07/S10; fake reverse RPC for synthetic integration only; exact native DTO/context checks; real inference separately authorized. |
| P08 | Code sample create/list/detail/update/delete/version, tags and context selection; sealed exams deny samples/comparison. | S07/S10; `api.py:4083-4323`; version pins and changed/disabled sample. |
| P09 | `/analytics`, `/skills`: taxonomy/prerequisites, mastery projections/evidence, overview/events. Rebuild deterministic; forged/manual correctness cannot create verified mastery; empty denominators null. | S06/S08; `learning.py:135-247`, `api.py:4325-4595,7065`; independent event replay. |
| P10 | `/errors`: proposed/confirmed/rejected and corrected history, review completion/due state, daily plan/rebuild and comparison. Practice/exam/review/correction independent. | S08/S10; `api.py:4597-4804`, round2/exam-progress tests; provenance and duplicate-event checks. |
| P11 | `/bookmarks`: validated task/revision pairing, idempotent selection, filters and progress; new revision cannot inherit solved/stale pin silently. | S02/S06; `api.py:880-987`, `test_bookmarks_progress.py`. |
| P12 | `/exams`: blueprint/templates/variants and selection/exclusions; prepare/assets-ready/start; strict/study/pause/resume/answer/submit/report; isolated code/board/runs; study-AI snapshot/poll/cancel. | S09-S11; `api.py:4806-7063`, exams/scoring and original exam tests; deadline, revision/file pins, frozen report/hash. |
| P13 | Draft/sync records and change cursors: CAS, owner consistency tag, retry keys/conflicts/expiry, retention/full-refresh and bounded cursor. | S05/S12; `sync_records.py:152-282`, sync tests; preserve business headers and durable reopen. |
| P14 | `/settings`, shortcuts, theme/editor/font/cache/panel preferences, source/import/AI status/login and archive dialogs; localStorage/IndexedDB persistence. | S13/S14; original preference/shortcut/save source and tests; explicit UI-storage retention/backup policy. |
| P15 | Local Python: five exact self-hosted Pyodide files, original sandbox, stdout/stderr/error/stop/timeout/output limits and code/file/version pins. | S03/S11; runtime SHA and worker contracts plus packaged synthetic integration; no host Python code runner. |
| P16 | Save/close/reopen: dirty code/board/annotation/exam/preferences/drafts drain, nonce ACK, zero mutations and durable checkpoint. Failure retains recoverable window/data. | S13/S15; isolated profile/process evidence; normal GUI acceptance belongs to user. |
| P17 | v1 import, full backup and mini migration: SHA-bound preview/confirm/replay, every supported table/asset/UI state/exam, conflict atomicity, backup and idempotent non-destructive migration. | S14; semantic graph + asset digests across export/import/reopen. Seven-table fixture alone insufficient. |
| P18 | Original design and interaction across ten screens: central catalog; condition left/code-board center/one right navigation-AI; blank-first; board-first 4/7/11/12; keyboard/responsive/reduced motion and modal/error states. | S13; source/component/CSS diff is partial; actual visual/user acceptance **USER_PENDING**. |

## Minimal synthetic integration scenarios

These are planned cases, not tests already executed. Use fresh TEST_ONLY temporary
profiles/bundle copies, frozen time/IDs and known small source fixtures. No real
DB/profile/auth/.env/network/model. Synthetic content never satisfies genuine bank
readiness. Exercise real stdio → ASGI → original domain → SQLite when authorized;
mock only the native AI callback, never the domain/transport being verified.
Compare semantic records/events/hashes rather than SQLite page bytes. Reuse and
assess existing original tests; do not duplicate their private implementation.

| Case | Inputs and independent public-result oracle |
| --- | --- |
| S01 | Small labelled synthetic catalog + two owners' rows. Ready follows schema/seed; no startup network/AI. Forged owner/query/header/body cannot select owner/expose foreign IDs. Missing seed/critical bytes, bad seed hash and schema-too-new fail readiness. Deliberate local POST/GET/DELETE auth behavior documented. |
| S02 | Tasks spanning lines 4/7/9/11/12/27, published/current/old revisions, sources/difficulties, bookmarked/solved/empty/excluded states. Exact filter/combined-filter IDs/order/cursors/counts, detail/history/coverage/progress. Bookmark mismatch 422, stable replay and new-revision reset. Empty is green only with an explicit empty-filter oracle. |
| S03 | Known UTF-8 text/image/two-sheet spreadsheet and alternative-format filenames. Exercise every asset route; compare bytes/MIME/content hash, converter/sheet/source pins and safe names. Corrupt/unavailable/wrong revision/type/oversized/case-fold collision fails without success pin. Verify five runtime SHA and offline cache on reopen. |
| S04 | Project → conversation → messages → solve → code/board/versions. Create/list/update/move/archive twice/restore/delete disposable graph. Archive preserves children; no-op does not append transition evidence; foreign operations match 404/empty original behavior. Reopen retains graph/order. |
| S05 | Solve create/list/detail/patch, code read/write/versions, board and annotations create/retry/change/delete. Two stale base-version writes have exactly one winner; same key+payload one version/event, changed payload 409. Preserve colors/eraser/anchors and original flush semantics. |
| S06 | Known-answer/no-key tasks, two revisions and frozen time. Correct/incorrect server check matches fixed answer oracle; same key replays immutable attempt, changed payload conflicts. No-key unavailable produces no graded evidence; forged server provenance rejected. Manual labels/NULL legacy/new revision cannot forge current mastery; later correct preserves wrong-error history. |
| S07 | Synthetic native callback: sample create/version/update/disable/delete; context preview and assistant start/poll/events/cancel. Exact canonical bytes/digests/sample versions/model+effort; one explicit Send gives one run under retry. Wrong echo/hash rejects; no inference from mount/status/preview. Nonterminal poll stays active; final audit persists. |
| S08 | Fixed authoritative/unverified attempts and confirmed/rejected/corrected errors/reviews. Skill/detail/mastery/overview/events and repeated rebuild yield deterministic projection without duplicate evidence. Clock fixes due review/daily plan; null denominators and correction flags exact. Comparison hides restricted answers. |
| S09 | Fixed blueprint plus official-template specification with synthetic tasks. Blueprint/template instantiate/variant, prepare replay/assets-ready/start/list/active/detail/pause/resume/answer/submit/report. Preview has no timer; exact revision/file pins; solved-only exclusions never fall back. Start rechecks assets/single active strict session. Deadline rejects mutation; repeat submit returns same frozen score/report/hash. |
| S10 | With sealed strict session, call practice/check/annotation, tutor preview/start/poll/cancel, samples/comparison with missing/wrong/foreign exam IDs. Verify each original denial/redaction before forbidden mutation/content/inference. Ordinary assistant cancellation stays available with redacted id/status (`api.py:4073-4076`); study-AI poll/cancel has its original 423 policy. Allowed cancellation may append lifecycle events. Answers/report redaction holds. Browser direct native AI start/poll/cancel must be inaccessible. |
| S11 | Same task in practice + two exams: code/board/run state stays isolated. Study output never grades answer. Synthetic normal/error/stop/timeout/output-limit Python keeps version/file pins. Study-AI fake context excludes hidden answer; retry same run; changed context or strict transition obeys policy. Submit freezes workspace/report/audit and reopen retains it. |
| S12 | Two clients, same owner tag: read/write/change cursors, retry/conflicting payload/stale version/owner mismatch. One winner, bounded retry expiry/change retention/full-refresh; no cross-revision draft grading. Negative/overflow cursor rejects. Interrupted migration/transaction preserves complete old or new state. |
| S13 | All ten route sources and their views/dialogs; preference keys, shortcut conflicts/reset, editor/board/exam/draft/panel state persist. Frontend contracts/source diff is one layer. Actual dirty-close/failure/retry/reopen and visual/keyboard behavior remain user acceptance; no GUI automation here. |
| S14 | Canonical v1 plus complete supported synthetic table/asset graph. Native preview/token/expected SHA agrees with Python; confirm/change/replay semantics. Full export → fresh import → reopen retains IDs/revisions/nulls/times/events/report/settings/UI storage and asset bytes. Repeated mini migration no duplicates, original+backup retained. Newer/malformed schema, hostile SQL objects, broken FK/asset, owner conflict and disk failure reject atomically without content logs. |
| S15 | Fragment UTF-8/JSONL, coalesce valid frames, reorder IDs, interleave gateway+HTTP. Every valid frame <=32 MiB succeeds independent of poll chunking. Unknown/duplicate IDs/events, oversized unfinished frame/body/response/queue fail safely. Reverse RPC cannot block request transport. Flush barrier drains/excludes mutations; failed checkpoint keeps close unaccepted. Job ownership before child execution, no remaining descendants/listener/auth inheritance; startup/deadline budgets agree. |

## Required evidence ledger

| Gate | Required final evidence | State |
| --- | --- | --- |
| BANK | Controlled genuine export provenance, expected nonzero task/revision/source IDs, coverage/count oracle, every required asset digest; installed first launch needs no user import/download. | BLOCKED: seed absent at assignment; not reclassified by synthetic tests. |
| BACKEND | Original relevant pytest, Ruff app/tests and mypy app on final full/backend; original test preservation/diff; adapter cases using packaged dependency closure. | NOT_RUN. |
| FRONTEND | Typecheck, original contracts, static build, all route/data adapters and Pyodide hashes; no production demo/network/EventSource fallback. | PARTIAL, owner receipt: typecheck 0, focused 56/0, contract 2/0; full tests/build HOLD. Frontend body still 2 MiB versus contract/native/Python 8 MiB. |
| NATIVE | Final tests/check/build; DTO/frame/path/header/queue/Job/cancel/close safety cases. | Prior owner test exit 0 is superseded by close fix. Final native gate pending for commands e97afac8... / backend b0d9095e.... |
| PACKAGE | Installer/resource SHA, clean synthetic startup, original operations/assets, offline restart and full transfer over actual packaged stdio; no service/port/host runner. | NOT_RUN. |
| DESIGN | User checks ten screens, modals/states/layout/keyboard and normal close/reopen. | USER_PENDING. |
| AI | Synthetic context/DTO/policy/cancellation plus compatible native advertised protocol; actual inference only when separately authorized. | Focused owner receipt FAIL: 47/1, exit 1, URL-free assistant availability test. Native/packaged gates NOT_RUN; no real AI here. |

Current source review (`qa/full-parity/source-review.md`) records import-preview
envelope, missing-quiesce and frontend body-bound findings at their reviewed pins.
The subsequent close-only review resolves GET-close-fence F02 in source: all
requests take a permit; sealing precedes child flush; mutex is released across
I/O; failure and duplicate ACK retain ownership. Two synthetic test designs were
reviewed; no executable PASS for the changed SHA is adopted yet. See
`qa/full-parity/close-review-20261029.md`. Native startup 180/185 seconds, incremental frame parsing,
safe typed-error propagation and explicit seven-command local ACL are source
improvements, not completed package or original-domain acceptance gates.

## Exhaustive original mounted API inventory

Paths below are relative to `/api/v1`, except explicit `/health`. Locations are
in `C:/Projects/Kontur/backend/app/`. Each method is a separate endpoint; grouped
rows account for all 112 endpoints, including aliases and deliberate adaptations.

| Row | Methods and paths | Source lines |
| --- | --- | --- |
| P01 | GET `/health`; GET `/api/v1/health`; GET `/capabilities` | `main.py:99,100`; `api.py:794` |
| P01 | POST, GET, DELETE `/auth/session` | `security.py:272,323,328` |
| P11 | GET `/bookmarks`; PUT, DELETE `/bookmarks/{task_id}` | `api.py:880,894,945` |
| P02 | GET `/task-progress`, `/tasks/coverage`, `/tasks`, `/tasks/{task_id}`, `/tasks/{task_id}/revisions` | `api.py:973,989,996,1106,1118` |
| P03 | POST `/source/import-fixture`, `/source/import-url`, `/source/sync`; GET `/source/sync/{job_id}`; POST `/source/sync/{job_id}/resume`, `/source/sync-variants`; GET `/source/health` | `api.py:1145,1178,1227,1319,1359,1413,1493` |
| P04 | GET `/assets/{asset_id}/download`; POST `/assets/{asset_id}/prepare`, `/assets/{asset_id}/prepare-text`; GET `/assets/{asset_id}/text` | `api.py:1541,1577,1625,1636` |
| P04 | GET `/assets/{asset_id}/device`, `/assets/{asset_id}/device-metadata`, `/assets/{asset_id}/device-text` | `device_assets.py:142,154,189` |
| P05 | POST, GET `/projects`; PATCH, DELETE `/projects/{project_id}` | `api.py:1651,1672,1686,1715` |
| P05 | POST, GET `/conversations`; PATCH, DELETE `/conversations/{conversation_id}`; POST, GET `/conversations/{conversation_id}/messages` | `api.py:1738,1760,1773,1803,1824,1859` |
| P06 | POST, GET `/solve-sessions`; GET, PATCH `/solve-sessions/{solve_id}` | `api.py:1874,1914,1940,1971` |
| P06 | POST `/solve-sessions/{solve_id}/check-answer`; POST, GET `/solve-sessions/{solve_id}/attempts`; PATCH `/solve-sessions/{solve_id}/attempts/{attempt_id}` | `api.py:2191,2324,2408,2423` |
| P06 | GET, PUT `/solve-sessions/{solve_id}/code`; GET `/solve-sessions/{solve_id}/code/versions`; POST, GET `/solve-sessions/{solve_id}/code-runs`; GET `/code-runs` | `api.py:2482,2506,2568,2581,2718,2733` |
| P06 | GET, PUT `/solve-sessions/{solve_id}/board`; GET, POST `/solve-sessions/{solve_id}/annotations`; PATCH, DELETE `/annotations/{annotation_id}` | `api.py:2748,2772,2834,2850,2930,2966` |
| P07 | GET `/assistant/options`; POST `/assistant/context-preview`, `/assistant/runs`; GET `/assistant/runs/{run_id}`, `/assistant/runs/{run_id}/events`; POST `/assistant/runs/{run_id}/cancel` | `api.py:3556,3565,3650,3869,3916,4016` |
| P08 | GET, POST `/code-samples`; GET, PATCH, DELETE `/code-samples/{sample_id}`; POST `/code-samples/{sample_id}/versions` | `api.py:4083,4106,4148,4167,4220,4251` |
| P09 | GET `/skills`, `/skills/{skill_id}`, `/mastery`; POST `/analytics/rebuild`; GET `/analytics/overview`, `/learning-events` | `api.py:4325,4346,4362,4410,4570,7065` |
| P10 | GET, POST `/errors`; PATCH `/errors/{error_id}`; GET `/review-queue`; POST `/review-queue/{review_id}/complete`; GET `/daily-plan`; POST `/daily-plan/rebuild`, `/solution-comparisons` | `api.py:4597,4611,4662,4701,4715,4737,4757,4768` |
| P12 | GET, POST `/exam-blueprints`; GET `/exam-blueprint-templates/{exam_year}`; POST `/exam-blueprint-templates/{exam_year}/instantiate`, `/variants` | `api.py:4806,4820,6142`; `official_blueprints.py:73,84` |
| P12 | POST `/exams/prepare`, `/exams/{exam_id}/assets-ready`, `/exams/{exam_id}/start`; GET, POST `/exams`; GET `/exams/active`, `/exams/{exam_id}` | `api.py:5925,5996,6076,6165,6175,6207,6240` |
| P12 | GET `/exams/{exam_id}/items/{item_id}/workspace`; PUT `/exams/{exam_id}/items/{item_id}/workspace/code`, `/exams/{exam_id}/items/{item_id}/workspace/board`; POST, GET `/exams/{exam_id}/items/{item_id}/code-runs` | `api.py:6253,6773,6824,6587,6750` |
| P12 | POST `/exams/{exam_id}/items/{item_id}/study-ai/runs`; GET `/exams/{exam_id}/items/{item_id}/study-ai/runs/{run_id}`; POST `/exams/{exam_id}/items/{item_id}/study-ai/runs/{run_id}/cancel` | `api.py:6265,6455,6518` |
| P12 | POST `/exams/{exam_id}/pause`, `/exams/{exam_id}/resume`; PUT `/exams/{exam_id}/answers/{item_id}`; POST `/exams/{exam_id}/submit`; GET `/exams/{exam_id}/report` | `api.py:6889,6896,7001,7026,7047` |
| P13 | GET, PUT `/sync/records`; GET `/sync/changes` | `sync_records.py:152,162,239` |

Pages: `/`, `/tasks`, `/workspace`, `/chat`, `/exams`, `/bookmarks`, `/analytics`,
`/skills`, `/errors`, `/settings` from `frontend/app/**/page.tsx`. Projects/archive/
samples/reviews are also in-page views/dialogs. The original dynamic
`frontend/app/python-runtime/[file]/route.ts:1-10` serves five dependency files;
static packaging must retain exact bytes/MIME without a Next server handler.

## Domain backup baseline

`TRANSFER_SCHEMA.md` must classify every table as preserved, deterministically
rebuilt with equivalent proven public results, or explicitly excluded with
approved rationale. Do not blindly copy identity secrets/runtime state. UI
preferences/drafts/cache pins are separate storage, requiring explicit retention.
The original 56 model tables (`models.py:30-866`) are:

`users`, `source_policies`, `source_snapshots`, `tasks`, `task_revisions`, `bookmarks`,
`assets`, `task_assets`, `sync_runs`, `source_sync_jobs`, `source_sync_items`,
`catalog_entries`, `projects`, `conversations`, `messages`, `idempotency_reservations`,
`solve_sessions`, `attempts`, `code_documents`, `code_versions`, `code_runs`,
`board_documents`, `board_versions`, `annotations`, `code_samples`, `code_sample_versions`,
`context_packs`, `agent_runs`, `agent_events`, `tutor_feedback`, `skills`,
`skill_prerequisites`, `task_skills`, `mastery_evidence`, `mastery_states`, `error_events`,
`error_patterns`, `review_items`, `daily_plans`, `exam_blueprints`, `exam_sessions`,
`exam_items`, `exam_answers`, `exam_workspaces`, `exam_code_runs`,
`exam_study_ai_context_snapshots`, `exam_study_ai_runs`, `exam_reports`, `learning_events`,
`projection_checkpoints`, `metric_snapshots`, `eval_runs`, `jobs`, `owner_sync_records`,
`owner_sync_writes`, `owner_sync_changes`.

No heavy builds/tests/installed app/GUI/profile/real DB/real AI/network checks were
run for this matrix. The separately authorized headless sandbox fixture has its
own scope/results; it is not packaged or installed UI acceptance. Concurrent
implementation changes require findings to be rechecked against final source SHA.

# Full original Kontur, fully local — integration contract

Binding user correction, 2026-10-29: original design and ALL functionality,
including a genuine preloaded catalog and attachments. The mini editor is not
an acceptable substitute. Keep existing local work, Settings .kontur import,
native window controls, and online AI through the isolated Codex CLI service.
No GUI automation; the user will test the installed application.

## Architecture and ownership

Reuse original C:/Projects/Kontur/frontend screens/styles and backend/app domain
logic in full/frontend and full/backend. Original source remains read-only.
The frontend is a Tauri static export. Ship the existing self-hosted Pyodide
assets for sandboxed Python execution. No arbitrary Python host code runner.

Ship the original backend's existing Python runtime/dependency closure inside
the application. This is an application-owned child process, no installed OS
service, listening port, external server, or new production package. It opens
SQLite and attachments only in this application's AppData. The child and its
descendants belong to a Windows Job before execution. The existing original
FastAPI routes run through an internal ASGI adapter over bounded JSONL stdio.
No browser network connection or authentication secret is needed or exposed.

ROOT owns native/src/backend.rs, commands.rs, lib.rs and integration. Phase 1
owns full/backend (copy original public app/tests/config; local bootstrap,
ASGI bridge, offline content and transfer integration). Phase 2 owns only
full/frontend. Phase 3 owns full/backend/local_codex.py and its focused tests;
coordinate with phase 1 before shared imports. Phase 4 owns persistent process
extensions in native/src/platform.rs and runtime packaging scripts/resources.
Do not edit another owner's files or revert parallel changes.

## Transport v1

Native Tauri command local_api_request accepts {request:{method,path,body?,headers?}}.
method is GET/POST/PUT/PATCH/DELETE; path starts /api/v1/, no absolute URL,
fragments, backslashes, controls, or path traversal. body is a UTF-8 string,
bounded to 8 MiB. The original device-run history alone can exceed 2 MiB
(20 retained runs, each up to 200,000 output characters); it must not be
silently truncated. Original endpoint-specific domain limits still apply.
headers is an optional string-to-string map admitting only
Content-Type, Idempotency-Key and X-Sync-Owner (a consistency tag, not an
authentication choice). Preserve these original business semantics. Browser
authentication/cookie/credential headers and filesystem paths are forbidden.
Response is {status:number,headers:[string,string][],bodyBase64:string}.
JSON and binary assets use the same transport. Raw asset size <=20 MB;
frame size <=32 MiB; bounded output queues, safe fixed errors, no log dumps.
Frontend wraps this as a Response-compatible localFetch used by api.ts and
device-files.ts. SSE subscriptions use existing run polling locally; no URL
EventSource. Native invokes are admitted only for the bundled main window.

Native child launch: resources/python/python.exe -I -B -u
resources/backend/offline_bridge.py --stdio --profile <owned profile/full>
--bundle <owned installation resources>. All paths are selected by native.
The script adds only its own known backend path to sys.path. No project/global
Python startup customization, inherited auth/config, or .env loading.

Native-to-Python JSONL request:
{id:integer,op:"request",method,path,body?:string,headers?:object}
Python-to-native response:
{id,ok:true,status,headers,bodyBase64}, or {id,ok:false,error:{code,message}}.
Python emits {event:"ready",protocol:1} only after schema/catalog/bootstrap
success. Unknown events/IDs/malformed/oversized frames fail closed.

Additional native-only operations use the same id/ok/error envelope:
flush (committed database checkpoint and no queued mutations), import_preview
and import_bundle (native-selected validated path), export_bundle (native save
path). Phase 1 documents exact transfer payloads before integration. Existing
v1 .kontur must remain importable. Full application backups must preserve all
supported workspace/progress/settings/exam state; do not silently reduce it to
the old seven-table subset. Existing mini-store migration is idempotent and
non-destructive, with a backup retained and no user contents logged.

## Native Codex reverse calls

Python retains original assistant context, history, audit, exam policies and
background job logic. Its local Codex client calls the existing Rust service:
{event:"gateway",callId:string,operation:"codex_status"|"codex_start"|
 "codex_poll"|"codex_cancel",payload:object}.
Native returns {op:"gateway_result",callId,ok:true,result:object}, or
{op:"gateway_result",callId,ok:false,error:{code,message}}.
Payloads/results follow native/src/codex.rs public serialized types exactly.
The request transport stays live while a gateway call is pending. No GUI
command, machine tool, fallback host runner, or authentication-file copying.
Only an explicit user Send triggers inference. Login/status native commands
remain available in original Settings with the smallest necessary local copy.

## Safety and readiness

Single fixed local owner is established inside the Python process; original
owner scoping, transactions, LearningEvents, idempotency, revision pins and
strict exam restrictions remain enforced. Original require_owner/security
checks are satisfied by private process context; browser cannot choose owner.
Network source refresh becomes explicit file import, never a required startup
dependency. No fabricated tasks or missing asset silently marked available.

Readiness requires an explicit parity matrix covering every original route,
catalog/filters, workspace/code/board/files/attempts/checking, exams, bookmarks,
errors/reviews, analytics/skills, assistant/settings/shortcuts/archive, local
save/reopen, transfer, and migration. Run original relevant backend tests,
frontend typecheck/tests/static build, native tests/check/build, and packaged
runtime integration with synthetic data. A catalog absence, mandatory red gate,
or unexplained feature gap prevents a completion claim. Preserve the currently
installed app and profile until the replacement passes these checks.

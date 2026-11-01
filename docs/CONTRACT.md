# Kontur Offline v1 — approved implementation contract

## Superseding user correction — full original application, 2026-10-29

The user explicitly requires the same design AND all functionality as the
original C:/Projects/Kontur application, fully local. The previous reduced
editor and manual first import do not meet this requirement. Reuse the original
screens and domain logic; do not substitute a smaller feature set. The genuine
task catalog and required attachments must be available on first launch.
Keep .kontur import in Settings and Codex CLI for online AI. Preserve all current
offline documents/progress during upgrade. GUI acceptance belongs to the user.
The architecture below describes the earlier subset and is superseded wherever
it conflicts with this correction; retain its data preservation, CLI isolation,
validation, and permission constraints. A full-parity implementation contract
will record the chosen local transport/runtime before integration.

User confirmed 2026-10-29: separate offline Windows application, import tasks and
progress through one file, AI through installed Codex CLI. Preserve old server
source, label it as server version, uninstall its laptop application while
retaining data. No old rollout, remote activation, publication or private-key use.

## Architecture

- Tauri 2 Windows + static React/TypeScript assets; no HTTP server at runtime.
- Reuse approved existing CodeEditor, statement/board/layout components and style.
- Rust owns SQLite, data validation, IDs, import/export, dialogs and CLI process.
- Only new production dependency approved in the plan: rusqlite (bundled SQLite,
  backup). Pin 0.40.2 after actual resolver/API verification. Existing Tauri,
  serde/serde_json/windows versions follow original project; add no other package.
- Native core checksum uses platform SHA-256, not an extra crypto dependency.
- App identity app.kontur.offline.desktop, name «Контур Локальный», version 0.1.0.
- Own AppData only. No auth/.env/cookies/real DB/profile copying or inspection.

## Data and transfer

The .kontur container is a dedicated SQLite data file, not executable SQL or an
app's live database. Python exporter can create it with stdlib sqlite3; no ZIP
dependency is needed. Native opens imported containers read-only, extension
loading disabled, trusted_schema OFF, fixed typed queries only. Version1
application_id=0x4b4f4e54 and metadata format='kontur-transfer-v1'. All expected
tables/columns/types, row/body/asset limits, foreign references and SHA-256 asset
checks must validate BEFORE preview or mutation. No views/triggers accepted.

Logical entities: tasks (stable source ID + separate revision, type1..27,
title/statement/optional structured blocks and asset IDs), attempts (stable ID,
task+revision, correct/wrong/unverified/error, source imported/manual, timestamp,
code/answer), documents (backend-generated ID, optional task+revision, title,
typeNumber, code, statement, board JSON, integer optimistic revision, updatedAt),
assets (ID, name, MIME, bytes, SHA256), preferences and selected document.
No invented official task content. New blank documents are labelled own work.

Progress statuses not_started/in_progress/error/solved derive from stored
attempts; preserve complete history, everSolved, last verdict and revision.
Do not silently apply an old solved revision to changed task content.
Repeated import is idempotent by source IDs/revisions/event IDs. Conflicting
documents retain both versions. Pre-import backup + one SQLite transaction;
corrupt/unsupported/incomplete input leaves existing data unchanged.

## Wire/native seams (camelCase JSON)

Storage phase owns domain.rs, storage.rs, transfer.rs and core integration tests.
First publish serializable Rust domain structs in domain.rs and a concise
docs/WIRE.md, then coordinate field changes with ROOT and consumers.

Store::open(root) -> Store; snapshot() -> Snapshot;
create_document(CreateDocument) -> Document; document(id) -> Document;
save_document(SaveDocument {documentId,expectedRevision,...}) -> Document;
mark_attempt(MarkAttempt) -> ProgressSummary;
preview_import(path) -> ImportPreview; import_bundle(path) -> ImportResult;
export_bundle(path) -> ExportResult.
Errors have fixed code + safe message (and conflict revision where meaningful),
never raw database/CLI/auth output. Storage must acknowledge a durable commit.

Tauri commands: load_snapshot, create_document, load_document, save_document,
mark_attempt, preview_import (native Open dialog), confirm_import (bound token),
export_bundle (native Save dialog), codex_status, codex_start, codex_poll,
codex_cancel, codex_login, confirm_close. UI never supplies SQL or CLI paths.
Native packaging phase owns lib.rs/main.rs/commands.rs/platform.rs and native
manifests/lockfile/config. Storage and CLI phases do NOT edit those shared files.

UI uses window.__TAURI__.core.invoke via withGlobalTauri=true on bundled local
assets only. A browser TEST_ONLY bridge is permitted only in QA; production must
show a real unavailable error, never silently use fake data. No remote assets.
Every edit immediately updates UI, autosaves in background, awaits true disk
ack on navigation/close. Native close asks __KONTUR_LOCAL_REQUEST_CLOSE__ and
validates a fresh matching local storage ack; no fake server ACK or forced exit.

## Codex CLI

Actual installed CLI observed 0.164.0-alpha.11.1; legacy 0.153 pin is incompatible.
CLI phase owns codex.rs and CLI tests only. Probe version/help/deny features with
fresh evidence. Dedicated credential home outside project; no auth file reads,
copies or logging. Normal private CLI login if needed. Empty per-request cwd,
stdin-only bounded selected question/code/statement, ignore user/project rules,
read-only/never/ephemeral and tools/plugins/MCP/web denied. Reject tool-bearing
JSONL; bounded output/time, cancellation owns its process tree. Default CLI model
is explicit UI mode, no silent substitution of a manual choice. AI offline or
unavailable never blocks editing/storage. Only user Send starts a model request.

Runtime evidence refinement 2026-10-29: this installed CLI still advertises the
built-in `request_user_input` human-question interface after machine tools are
disabled. Its generated primary schema has questions/options and conversation
IDs, with no command/file/network operation arguments; no supported disable key
was found. An advertisement-only exception for this exact interface is allowed.
No question bridge or additional stdin is provided: production still rejects
ALL tool-bearing/unknown events and cancels its owned process. Any workstation,
web, MCP, plugin or unknown tool advertisement remains a failed safety gate.
QA must report `toolsetEmpty=false` honestly when this metadata is present and
verify the shipped CliDefault mode separately. A synthetic-provider response
does not establish real OpenAI authentication or a successful model answer.

## Acceptance and ownership

ROOT orchestrates phase chats in the existing project and one exclusive build/
GUI/install lease. No heavy build/installer/real AI call without ROOT GO. Source
code owners must not revert others. Read-only research and isolated small tests
may run independently. New dependency requires explicit ROOT/user resolution.

Required: real SQLite save/reopen; transactional failed import; duplicate import;
conflict retention; exporter/importer roundtrip; semantic solved/error history;
typecheck/tests/static build; real native offline cold-start + code/save/restart;
readable type picker, interaction timings, CLI failure/cancel and real benign
AI response when private authentication is available. Only fresh demonstrated
results can be marked CONFIRMED. No full completion while a required gate fails.

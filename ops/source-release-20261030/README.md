# Kontur offline source release — WIP snapshot

This directory is a reviewed, source-only release helper for the private
`RamLeak/kontur-offline` repository. It is intentionally **WORK IN PROGRESS**:
it does not claim full parity, production readiness, or a complete offline
runtime.

The staged source is the current repository tree, not the original server at
`C:\Projects\Kontur`. The original server is never read by this helper. No
network, Git initialization, commit, push, deployment, or external service is
used.

## What is staged

`stage-source-release.ps1` curates the source needed to review the offline
architecture:

- native Rust/Tauri source, tests, build metadata, capabilities, and app icons;
- the frontend application, components, libraries, tests, static configuration,
  package manifests, and source icons/assets/fonts;
- the Python backend application, offline modules, local Codex adapter,
  tests, dependency metadata, and small source-only test surface;
- the four local packaging/build PowerShell scripts;
- the exporter-v4, downloader, and catalog-finalizer scripts, their synthetic
  tests, and plans;
- a small set of public technical contracts covering full parity, client state,
  transfer, catalog v2, and the base contract.

The self-contained Python runtime, Pyodide files, catalog/database snapshots,
real task/bank/answer assets, user profiles, QA output, logs, receipts,
installers, archives, and other binaries are **not** part of this source
release. A runtime and catalog must be supplied separately when a runnable
offline package is assembled; they are external to this repository snapshot.

## Reproduce the staging

From the repository root, use PowerShell 7 (or compatible Windows PowerShell):

```powershell
pwsh -NoProfile -File .\ops\source-release-20261030\stage-source-release.ps1 `
  -Destination .\ops\source-release-20261030\staged-NEW
```

`-Destination` must name a new directory. The command writes the curated source
plus `RELEASE_FILELIST.tsv`, `RELEASE_SHA256SUMS.txt`, `RELEASE_SUMMARY.json`,
`RELEASE_EXCLUSIONS.md`, and `RELEASE_SECRET_SCAN.tsv`. The summary records
file count and total bytes. Every staged file must use an allowlisted path and
extension, stay under the size guard, and be free of reparse points.

To refresh the checked-in snapshot manifest after worker changes, run:

```powershell
pwsh -NoProfile -File .\ops\source-release-20261030\stage-source-release.ps1 `
  -ManifestOnly
```

This updates `SOURCE_MANIFEST.tsv` and its `SOURCE_*` audit files in this
directory. Refresh it after the final source workers finish; the current
manifest is only a review snapshot.

The guard also verifies that backend application auth code (`app/auth.py`) is
kept as source, while auth/session/cookie data and credentials remain excluded.
The secret scan reports only category, relative path, and match count; it never
writes a matched secret value. A finding rejects the run.

## Status and limitations

The architecture is split between a native desktop shell, a Next frontend, and
an offline Python backend. This source release does not prove that those pieces
form a complete self-contained distributable. Build, typecheck, backend test,
browser, runtime, catalog, and parity gates remain the responsibility of the
parent release workflow and must be rerun after final worker files land.

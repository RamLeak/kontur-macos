# Full resource packaging contract

## Distribution update — 2026-11-01

The verified installed EXE/resources and frontend/backend/native sources remain
unchanged. Only download tooling and documentation change after `2bb8684`.
Two monolithic transfers failed on DNS/TCP; twenty <=64MiB parts preserve the
exact1,297,102,998B ZIP SHA42aec3208f7bf942023422bfb16ed85830759ac05a24f59ae43cc4640451ebb2.
The Windows helper verifies parts/full archive, writes a new directory and never
opens the profile, launches the app or reads authentication files. GitHub CLI is
optional; manually downloaded parts work with built-in Windows PowerShell/.NET.
[Current download](https://github.com/RamLeak/kontur-offline/releases/download/offline-2026-11-01/Kontur-Local-Windows-Offline-Download.zip), [all parts](https://github.com/RamLeak/kontur-offline/releases/tag/offline-2026-11-01),
[instructions](DOWNLOAD_WINDOWS_RU.md). Publication requires server hashes and
helper verification. Synthetic PS7+Windows PowerShell5.1 QA and independent rereview passed.
A genuine78s PS5 roundtrip restored the same ZIP and verified all3252files /
3246 resource hashes without profile/GUI/model access.
Earlier October31 distribution links below are historical
preparation attempts, not a claim that the failed draft was published.


The Python component is verified separately in `PYTHON_RUNTIME_COMPONENT.md`.
The genuine complete backend/catalog tree, packaged runtime gate, optimized
release build, portable package and versioned local installation have passed ROOT
integration. GUI, installed-profile and real-AI acceptance remain manual.

| Script | Inputs and effect |
| --- | --- |
| `package-python-runtime.ps1` | Explicit `RuntimeRoot`, `VenvRoot`, `Destination`; optional owned `BackendRoot`. Default is a metadata plan. `-Package` creates a new artifact tree. |
| `build-full-local.ps1` | Explicit `PreparedResourcesRoot`, `StaticWebRoot`, `CargoHome`, `TargetDirectory`, `ToolchainBinDirectory`. Default validates; `-Build` runs controlled locked/offline Cargo. |
| `package-offline.ps1` | `ExecutablePath`, approved ICO, `ResourcesDirectory`, `ResourcesManifestPath` and `ResourcesManifestSha256` create a v2 portable artifact. |
| `package-install-local.ps1` | `PackageDirectory`, `ExpectedExecutableSha256` and `ExpectedResourcesManifestSha256` admit and install a v2 package to the fixed current-user location. This script performs installation rather than a dry run. |

The runtime producer's optional backend payload is restricted to top-level
`offline*.py` and `local_codex.py`, plus `app/**/*.py`. Tests, fixtures, profile,
cache and `.env`-prefixed components are excluded before payload reads. Python
application modules implementing auth, security, cookies or tokens remain code;
they are not interchangeable with authentication data.

The build helper validates the actual `native/tauri.conf.json` frontend path as
`../full/frontend/out`. It refuses legacy `web/out`; no inline Tauri override
masks an incorrect base configuration. It clears `TAURI_CONFIG` during the
controlled build and restores the caller's absent, empty or string value.
Heavy build execution remains subject to the ROOT build lease.

The complete prepared artifact has a `resources` directory and a sibling
`full-local-resources-manifest.json`. The existing
`kontur-full-local-resources-v1` header/schema is retained; its file list must
cover every prepared resource, including the catalog. Required payload includes
`python/python.exe`, `backend/offline_bridge.py` and `catalog/catalog.sqlite3`.
The published Python-only `runtime-manifest.json` is component evidence and
cannot replace this complete resource manifest.

V2 portable packages use `kontur-local-portable-v2` and carry a resource
directory, resource manifest and its SHA-256. Both package creation and external
installation admission require the caller's explicit resource-manifest pin.
Executable SHA-256 alone does not identify external executable resources.
Validation checks exact file membership, paths, sizes and hashes; unexpected
files, traversal, junctions/symlinks, startup injection files and credential or
environment data fail closed. Legacy v1 admission remains supported without
v2 resource arguments.

The installer validates the source, staged tree and final version before
publishing its marker and Desktop shortcut. An existing version with the same
EXE SHA and a different resource manifest/tree is refused before either is
changed. A resource-only release therefore needs a distinct executable/release
SHA under the existing version-directory scheme. Older installed versions are
preserved.

The approved package and shortcut icon must have SHA-256
`8f00c6cc801823ddb2122f432421c99f270317b0ce5192c99954abb0b7d180a7`.
`assets/branding/native-icon-promotion-receipt.json` records the source and backup.
Helper validation is not evidence that a full application package or replacement
installation has run. Those remain separate gates; GUI acceptance is manual.

## Verification — 2026-10-30

`artifacts/full-resource-helper-review/ps7-result.json` records 61 passing
checks on PowerShell 7.6.5: current four-script AST parsing, real synthetic v1/v2
package creation, explicit-pin admission, the actual existing-version reuse
branch, forbidden startup/environment/venv paths, retained application code and
all 3,187 verified Python payload paths. The installer main was not invoked.
`negative-gates-result.json` records three more passing rejection checks:
unexpected package file, same-size modified catalog bytes, and changed catalog
size. Helper sources remained unchanged throughout verification.

## Current rail/settings/Codex/files release — 31 October 2026

EXE SHA `7b2008f7da21335f8ca1dd53d2c099a14c484f2884751ade596144d8d1251612` and complete resource manifest SHA
`4d989ce834eded14d13e0ba5370b4824bd6d1802199b4229c659f7c9d8e2a392` were admitted by build/package/install.
All3246 resources and all ZIP members were verified;15 files of five prior
versions remain unchanged. User profiles, keys, actual solutions and Codex login
were not opened or bundled. GitHub receives source-only Git plus the separate
verified portable ZIP (ZIP64 member streaming, compressed asset below2GiB).
Archive SHA `42aec3208f7bf942023422bfb16ed85830759ac05a24f59ae43cc4640451ebb2`.

## Previous startup/editor/AI release — 31 October 2026

The current release passed build, package and versioned install with EXE SHA-256
`2983a314cb098f057011662343dcc4b614ac4447e21af5f22c9a48519938335f` and resource manifest SHA-256
`79239c68708875725d38c6bc03bdc9249b7912127e1cae4aa78d83e243b8a00e`. The manifest covers3246 files:
Python3188/backend56/catalog2, including all seven new offline_ai modules.
The genuine packaged gate passed1/1 with the same resource pin and unchanged
native sources. Post-install verification checked EXE/manifest/shortcut and
all12 baseline program files from four older versions. Installed-profile,
GUI and real-model access were not used. All previous versions are retained.

## Earlier integration — 31 October 2026

The genuine packaged backend test passed with the prepared runtime and all admitted
bank bytes, including full2.63GB transfer and largest50MB asset verification.
The optimized release EXE (18,216,960 bytes) has SHA-256
`295f1646452ee1fbc8d5700c3c14ae2f052512bdb4a2a02c338ac3ad62069446`.
The complete resource manifest pins3239 files with SHA-256
`0c0100f69f17dce61ec1df173621af6335416f8d19c2ea27548ef09aa9352cd3`.
Portable package creation and actual versioned installation both completed exit0.
Post-install checks verified the new program/manifest/shortcut and unchanged
EXE/icon/package-manifest hashes of the preceding version. No profile data was
read and the GUI/model was not launched.

Actual installation exposed a caller/producer mismatch before any installation
mutation: an absolute resource-manifest path was passed to the strict relative
marker producer. The caller now derives the leaf from the already validated
package path. The producer and admission guards remain strict.
`qa/test-install-marker.ps1` evaluates the actual top-level marker assignment
without executing installer main: the frozen old callsite fails as expected;
the corrected callsite passes11 checks, including v1 compatibility, v2
producer/reader roundtrip and absolute/traversal rejection. PS5/7 AST passes.

`ps5-ast-result.json` records successful actual PowerShell 5.1 parsing of all
four scripts. The attempted PS5 functional `.ps1` invocation exited 1 with
`AuthorizationManager check failed`; no functional PS5 pass is claimed. No
execution-policy or security-setting change was made. `ps5-attempt.json` retains
that limitation. `handoff.json` contains all current source and evidence hashes.

ROOT has switched native `frontendDist` to `../full/frontend/out`; the current
configuration matches the helper's required path. ROOT owns native/backend
integration. At the initial handoff actual build/install were HOLD; the later
ROOT integration above cleared that gate. GUI/model acceptance remains manual.

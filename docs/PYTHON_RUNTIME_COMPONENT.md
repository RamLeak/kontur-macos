# Bundled Python component — 2026-10-29

Status: `PYTHON_COMPONENT_VERIFIED_BACKEND_CATALOG_PENDING`.
The runtime is ready as a component. Full application readiness is false.

`resources/python` contains CPython 3.12.14 for Windows x64 and the existing
production dependency closure: 25 distributions, 3,187 payload files,
71,834,619 bytes. Packaging used the explicitly approved existing runtime and
wheel metadata/payload from the original production environment. No dependency
was downloaded. No user database, profile, authentication data or original
backend/catalog was copied into this component.

The original package is retained at `artifacts/full-python-3.12.14-v1`.
The exact producing script is preserved at
`artifacts/full-python-probe/producer-package-python-runtime-C627.ps1`, SHA-256
`C627CDED71DAA3022422A5DF244416A9084BACB3D2C7E6CCBA0D8DC0DFFB6B06`.
Later script revisions do not change the provenance of this artifact.

| Integrity evidence | SHA-256 |
| --- | --- |
| Artifact `full-local-resources-manifest.json` | `31adbf43394c0cea71aef3fcaa8a6b91e9220f46755993ececfbc0983cca69d0` |
| Published `resources/python/runtime-manifest.json` | `f838a940feb1e6bb075b94c04e1d842c0c8b6508292cb04e3e01c850fa7fde1a` |
| Published payload tree | `80ce35dab19a4bc6f244500e444cc602230f6aba3c5edd3f773350e0ff969bff` |
| Component handoff JSON | `4fa7bfa6ce95c5d43fb4f4e226d0ddd85e64475070669c352f138e9be17cc427` |

The tree digest uses ordinal relative paths and UTF-8 records
`relativePath<TAB>size<TAB>lowercaseSha256<LF>`; the component manifest excludes
itself. Every copied payload hash and size was checked before atomic publication.
Existing resource siblings were preserved.

The probe starts the owned executable with `-I -B`, a replacement environment
and synthetic HOME/AppData/TEMP directories. Executable, prefix, base prefix,
search paths, imported module files and distribution set must all resolve to
the owned runtime. It opens no listener and starts no backend or GUI.

| Probe under `artifacts/full-python-probe` | Result |
| --- | --- |
| `artifact-runtime-probe.json` | Exit 0, 17.66 s; 25 imports and all four functional checks passed |
| `project-runtime-probe.json` | Historical failure: 30 s deadline expired; cause not established |
| `project-startup-diagnostic/result.json` | Exit 0, 0.52 s; relocated executable/prefix verified |
| `project-full-diagnostic/result.json` | Exit 0, 14.44 s; relocated 25 imports and all four functional checks passed |

Functional checks cover an in-memory SQLite/SQLAlchemy transaction, XLSX
roundtrip, ODS roundtrip and direct in-process FastAPI ASGI handling. Successful
probes collected zero stderr bytes. The failed first relocated probe remains in
the evidence; its stderr was not collected and its cause is not claimed resolved.
The later instrumented probe recorded progress through `all_loaded_modules`.

`runtime-component-handoff.json` lists the exact probe source, runner and result
hashes. Backend code/resource packaging, offline catalog integration, full static
frontend, native integration/build, complete portable-package verification and
replacement installation remain required before full application readiness.

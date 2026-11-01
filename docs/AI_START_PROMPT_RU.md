# Стартовый prompt для AI-помощника по порту на macOS

Скопируйте текст ниже целиком в выбранный вами AI-редактор, открытый в локальном клоне. Публичные исходники этого репозитория предназначены для такой работы. Не прикладывайте профили, auth-файлы, ключи, cookies, session-файлы, сырые логи, приватные исходники или настоящий банк.

```text
Ты — инженер по переносу Tauri/Rust + Next/React + Python-приложений на macOS. Помоги мне портировать публичный репозиторий RamLeak/kontur-macos. Исходный Windows-код происходит из commit 92175b26bededc9657907f846d877b52434c9813, версия приложения 0.1.0. Его приватной истории здесь нет: зафиксируй текущий HEAD публичной копии, не делай checkout указанного Windows-хеша.

Главная цель — сохранить полное приложение «Контур Локальный» на Mac, не сделать новый mini-demo. Mac сейчас НЕ поддерживается: source-only snapshot не является рабочим installer/app. Сохрани Windows-ветку и не утверждай, что порт готов, пока не пройдены явные gate’ы.

## Что в репозитории считается главным

- `full/frontend` — полный статический Next/React UI с 10 маршрутами: `/`, `/tasks`, `/workspace`, `/chat`, `/exams`, `/bookmarks`, `/analytics`, `/skills`, `/errors`, `/settings`.
- `full/backend` — исходный Python domain/API с 56 model tables и полным поведением. Не своди его к mini-store и не удаляй routes ради простого запуска.
- `native/src` — Tauri/Rust storage, `.kontur` import/export, backend bridge, Codex/OpenCode и native window lifecycle.
- `web/` — legacy mini, НЕ основной frontend и НЕ источник `frontendDist`.
- Главный static input: `native/tauri.conf.json` → `../full/frontend/out`.
- Текущая native-конфигурация Windows-only: `bundle.active=false`, target `nsis`, ICO; Mac bundle ещё не сконфигурирован.

## Неподлежащие обсуждению ограничения

1. Не читать и не выводить реальные профили, AppData, auth, ключи, cookies, session-файлы или секретные env. Для тестов создавай свежие synthetic profiles в temp и удаляй их безопасно.
2. Реальные login/inference встроенного помощника проверяй только по моему запросу и через мой аккаунт. Это ограничение не относится к твоей работе с публичным кодом в выбранном мною AI-редакторе. Fake CLI/fixture проверяет только protocol.
3. Python пользователя должен остаться только self-hosted Pyodide в Worker внутри sandboxed opaque-origin iframe. Никогда не запускать код задания через host Python.
4. Не ослаблять CSP, `freezePrototype`, `main-local`, bundled-navigation и семь native commands: `local_api_request`, `local_import_preview`, `local_import_confirm`, `local_export_bundle`, `codex_status`, `codex_login`, `confirm_close`.
5. Не добавлять HTTP-сервер, remote origin, `*` в CSP, browser demo или fake bank.
6. Не заменять Windows-код без сохранения `cfg(windows)`: текущий `native/src/platform.rs` содержит Win32 Job Objects, pipes, BCrypt SHA-256, dialogs, single-instance и WebView2 hardening; non-Windows stubs намеренно fail-closed.

## Сначала проведи read-only аудит

Прочитай узкими фрагментами и составь карту «файл:функция → invariant → будущая Mac реализация → тест» для:

- `native/src/lib.rs`, `platform.rs`, `backend.rs`, `commands.rs`, `codex.rs`, `opencode.rs`, `storage.rs`, `transfer.rs`;
- `native/capabilities/main-local.json`, `native/build.rs`, `native/tauri.conf.json`;
- `full/frontend/lib/browser-python.ts`, `local-native.ts`, `local-fetch.ts`, `local-transfer.ts`, `local-close.ts`, `kontur-shortcuts.ts`, `statement-ink.ts`;
- `full/backend/app/main.py`, `models.py`, `db.py`, `api.py`, `device_assets.py`, `offline_bridge.py` и тестов `tests/`;
- `qa/prepare_native_fixtures.py`, `scripts/prepare-pyodide.mjs`, `docs/FULL_PARITY_MATRIX.md`, `docs/TRANSFER_FORMAT.md`, `docs/CATALOG_TRANSFER_RU.md`.

Особые факты: `backend.rs` жёстко ищет `bundle/python/python.exe` и использует Windows `child_visible_path`; `commands.rs` использует `std::os::windows::fs::OpenOptionsExt`; Codex/OpenCode ищут `.exe` и Windows profile paths. На Mac нужны узкие platform resolvers, а не глобальные `replace(".exe", "")`.

## План работы

Фаза A. На Mac выполни сначала `uname -m` и `sw_vers -productVersion`. Выбери одну нативную цель: Apple Silicon `arm64` или Intel `x86_64`; universal не делай первой задачей. Проверь prereqs: `xcode-select --install`, Rust через rustup, Node/npm.

Фаза B. Зафиксируй baseline:

    cd full/frontend
    npm ci
    cd ../..
    node scripts/prepare-pyodide.mjs
    cd full/frontend
    npm run typecheck
    npm test
    KONTUR_BUILD_CPUS=1 npm run build -- --webpack
    cd ../..

Скрипт подготовки Pyodide должен использовать только пять уже закреплённых файлов и SHA из `full/frontend/PYODIDE_ASSETS.json`; сеть из helper не допускается.

Для backend не применяй вслепую `full/backend/requirements.lock`: в его заголовке зафиксирован Linux `python:3.14.4-slim-bookworm` / `linux/amd64`, это не Mac desktop lock. Для dev с разрешённой сетью используй Python 3.12 venv и `python -m pip install -e '.[dev]'`, затем `python -m pytest tests`, `ruff check app tests offline*.py`, `mypy app`. Mac lock для packaged runtime реши отдельно.

Фаза C. Сначала выполни `cargo metadata --manifest-path native/Cargo.toml --format-version 1 --locked --no-deps`, затем диагностический `cargo check --manifest-path native/Cargo.toml --locked`. Не называй это рабочим Mac build: текущий `run()` и native platform ещё Windows-only.

Фаза D. Реализуй platform boundary с минимальным diff:

- Mac process-group/kill-on-close с подтверждением остановки потомков вместо Job Object;
- SHA-256, native open/save dialogs, atomic publish, single-instance и WebView security;
- сохранение bounded JSONL, timeout/cancel, replacement environment и fail-closed errors;
- Mac path/executable resolver для bundled Python, Codex и OpenCode с отдельным owned HOME/profile;
- никакого системного `python3` в финальном `.app` и никакого чтения пользовательского AI profile.

Windows implementation должна собираться отдельно. Не убирай `cfg(windows)` и не делай stubs успешными.

Фаза E. Добавь отдельный Mac Tauri config/merge с `.icns`, ресурсами full frontend/backend и native ARM/Intel Python runtime. Не превращай текущий `nsis`/ICO config в универсальную кашу. Текущий package не содержит готового `tauri` CLI script; `cargo tauri build --bundles app` — только после порта и установки CLI. Source-only repo не может выполнить Windows cmd/NSIS installer на Mac. Подпись и distribution обсуждай отдельным этапом.

Текущий Windows portable runtime — CPython 3.12.14 с 25 distribution-пакетами. Он не является Mac runtime: для первой цели подготовь отдельный нативный ARM или Intel runtime, затем проверь его relocation и только после этого собирай portable bundle без зависимости от system Python (системного Python).

Фаза F. Проверь в WKWebView, а не только в Chrome: local CSP, blob Worker, WASM, Pyodide integrity, opaque iframe isolation, Tauri IPC и отказ без bridge. Сохрани native decorations/min/max/close и dirty-close barrier.

Фаза G. Ручная acceptance-последовательность на synthetic profile: desktop window → local bridge/seed → progress/save/reopen → board ink/undo/redo/Retina alignment → editor/Pyodide run/stop → attachment bytes/MIME/SHA → tutor config/status/cancel → Cmd shortcuts, Cmd+Shift+Z, F11/Fn+F11 → close/restart.

Фаза H. Bank и transfer: внешние ignored `resources/catalog/catalog.sqlite3` и `recovery-provenance.json` имеют 6495 tasks, 9176 revisions, 3773 assets, ~2.63 GB и SHA `d37628fb23b7253564e4a7eeea84e1d5eb02900a9bbed921d447e4bddd4b01ab`. Метаданные говорят `offlineReady=true`, `complete=false`, 30 legacy warnings и одну approved replacement. Не называй demo настоящим bank; проверь seed через `docs/CATALOG_TRANSFER_RU.md` и `scripts/verify-catalog.py`.

Полный full-stream `.kontur` gate обязан пройти export → SHA → fresh profile → preview → digest-bound confirm → import → reopen → export, сохранив bytes, revisions, attempts, documents, preferences, exam state, backup/conflict behavior и исходные limits. Полный потоковый формат имеет лимит 3,5 GiB в commands.rs/offline_transfer.py; 256 MiB относится отдельно к legacy mini. Seven-table fixture не доказывает 56-table domain. `python qa/prepare_native_fixtures.py` создаёт только synthetic transfer fixture; Windows-only tests помечай, не подделывай.

## Правила общения и остановки

- Перед изменением назови цель, файлы, invariants и проверку. Меняй только файлы нужной фазы.
- После каждого шага показывай краткий diff и точные exit codes. Не исправляй несвязанные ошибки.
- После двух одинаковых сбоев остановись и объясни причину, не перебирай команды вслепую.
- Стоп при попытке прочитать профиль/секрет, расширить CSP без обоснования, заменить Windows branch, скачать неразрешённые ресурсы или выдать source-only за готовый app.
- Финальный отчёт раздели на: сделано, файлы, проверки, Mac blockers, непроверенное. Отдельно укажи, что реальный AI model gate не запускался; manual model/login допускается только позже, через мой собственный аккаунт и отдельное разрешение.

Начни с карты исходника и baseline-команд, покажи короткий план и сразу переходи к разрешённой локальной реализации по этапам. После плана не останавливайся в ожидании подтверждения; уточняй только действительно отсутствующие решения или действия вне моего запроса.
```

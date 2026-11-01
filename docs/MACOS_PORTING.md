# Перенос «Контур Локальный» на macOS

Статус этого документа: **SOURCE-ONLY / macOS ещё не поддерживается**.
Он описывает работу для публичного репозитория [RamLeak/kontur-macos](https://github.com/RamLeak/kontur-macos), но не выдаёт план за готовый порт и не обещает рабочий `.app`.

## С чего начинаем

- Базовый Windows-снимок: commit `92175b26bededc9657907f846d877b52434c9813`, версия приложения `0.1.0`.
- Полный UI находится в `full/frontend`: десять маршрутов `/`, `/tasks`, `/workspace`, `/chat`, `/exams`, `/bookmarks`, `/analytics`, `/skills`, `/errors`, `/settings`.
- Полный домен находится в `full/backend`: 112 исходных HTTP-операций и 56 таблиц исходной модели. Список таблиц и требуемые наблюдаемые функции закреплены в `docs/FULL_PARITY_MATRIX.md`; это не повод сводить приложение к мини-хранилищу.
- `native/src` — Tauri/Rust-обвязка, локальная SQLite, импорт/экспорт `.kontur`, дочерний Python и AI-адаптеры.
- `web/` — старый legacy mini-интерфейс. Он не является главным фронтендом и не должен стать источником `frontendDist`.
- Главный статический вход для native-конфигурации — `../full/frontend/out`, не `web/out`.

Сначала прочитайте `docs/CONTRACT.md`, `docs/FULL_PARITY_CONTRACT.md`, `docs/FULL_PARITY_MATRIX.md`, `docs/CLIENT_STATE_CONTRACT.md` и `docs/FULL_RESOURCE_PACKAGING.md`. Они задают границы сохранности; «получилось собрать» — только один из будущих gate’ов.

## Ограничения, которые нельзя обходить

- Публичная копия содержит исходники и документацию, а не готовый macOS-инсталлятор. Здесь нет обещания запуска.
- Windows-ветка должна остаться рабочей и узнаваемой: не заменяйте `cfg(windows)` общим stub’ом ради зелёного `cargo check`.
- Не ослабляйте текущую CSP, `freezePrototype`, capability `main-local` или список семи native-команд. Не добавляйте удалённые origin, «временный» `*`, HTTP-сервер или browser demo.
- Не читайте реальные профили, AppData, auth-файлы, переменные с ключами, cookies или session-файлы и не загружайте исходники/seed во внешние сервисы. Для тестов создавайте свежие synthetic-профили во временной папке и удаляйте их после проверки.
- Настоящую модель встроенного помощника, login и AI inference не запускайте без запроса владелицы этой копии. Синтетический CLI/фикстура проверяет протокол, но не доказывает inference. Это правило не мешает использовать выбранный ею AI-редактор для работы с публичными исходниками.
- Python пользователя запускается только в существующем Pyodide Worker внутри sandboxed opaque-origin iframe. Хостовый Python не превращайте в runner для кода из задания.

## Первый час на Mac

1. На целевой машине запишите только безопасные сведения: `uname -m` и `sw_vers -productVersion`. `arm64` и `x86_64` — разные первые цели; сначала собирайте нативно под одну архитектуру, universal binary не планируйте заранее.
2. Установите Apple Command Line Tools по официальной подсказке: `xcode-select --install`. Полный Xcode не называйте обязательным без проверки конкретного Tauri-гейта.
3. Установите Rust официальным `rustup`, а Node.js той версии, которая нужна текущему Next-проекту. Tauri требует Rust, а текущий UI — Node/npm.
4. Склонируйте публичный репозиторий и запишите его собственный исходный commit через `git rev-parse HEAD`. Указанный выше `92175b2…` — происхождение Windows-кода, его истории в этом новом репозитории нет; делать `checkout` на него не требуется.
5. Проверьте, что `git status` чистый, и сохраните вывод только с версиями инструментов. В публичный issue не вставляйте пути профиля, окружение или сырые логи.

## Базовая проверка до изменений

Она нужна, чтобы отделить уже существующую проблему от ошибки порта.

```bash
cd full/frontend
npm ci
cd ../..
node scripts/prepare-pyodide.mjs
cd full/frontend
npm run typecheck
npm test
KONTUR_BUILD_CPUS=1 npm run build -- --webpack
cd ../..
```

`scripts/prepare-pyodide.mjs` подготавливает ровно пять закреплённых файлов из установленного `pyodide`; скрипт не должен скачивать новый runtime. Повторный запуск после `npm ci` должен быть идемпотентным и проверять SHA-256 из `full/frontend/PYODIDE_ASSETS.json`.

Для backend сначала создайте отдельное dev-окружение. `full/backend/requirements.lock` начинается с оговорки про `python:3.14.4-slim-bookworm` и `linux/amd64`; это контейнерный Linux-lock, не desktop-lock для Mac.

```bash
cd full/backend
python3.12 -m venv .venv-macos
source .venv-macos/bin/activate
python -m pip install -e '.[dev]'   # сеть нужна; выполняйте только с явным разрешением
python -m pytest tests
ruff check app tests offline*.py
mypy app
cd ../..
```

Если сеть запрещена, не подменяйте отсутствие пакетов `pip install -r requirements.lock` и не объявляйте проверку пройденной. Сначала зафиксируйте Mac-совместимый lock отдельно.

До правок native сделайте диагностику конфигурации:

```bash
cargo metadata --manifest-path native/Cargo.toml --format-version 1 --locked --no-deps
cargo check --manifest-path native/Cargo.toml --locked
```

На текущем snapshot полноценный Mac build/run ожидаемо **не работает как есть**. Эти две команды помогают увидеть границы `cfg`, но не являются доказательством готовности.

## Фаза 0 — составьте карту, не переписывая всё

1. Зафиксируйте отдельную рабочую ветку для Mac и сохраните Windows-путь сборки.
2. Просмотрите `native/src/lib.rs`: `run()` и окно сейчас находятся под `cfg(windows)`, а `cfg(not(windows))` возвращает ошибку «поддерживает Windows».
3. Просмотрите `native/src/platform.rs`: Win32-модуль содержит Job Objects, pipes, SHA-256 через BCrypt, native dialogs, single-instance/activation, atomic publish и WebView2 hardening. Нижний `cfg(not(windows))` оставляет fail-closed stubs для SHA/process.
4. Просмотрите `native/src/backend.rs`: `BackendService::spawn` жёстко ищет `bundle/python/python.exe`, `backend/offline_bridge.py`, Windows-visible paths и передаёт ограниченное окружение.
5. Просмотрите `native/src/commands.rs`: import/export и close-flow используют `std::os::windows::fs::OpenOptionsExt` и native dialog boundary.
6. Просмотрите `native/src/codex.rs` и `native/src/opencode.rs`: обнаружение CLI, `.exe`, `LOCALAPPDATA`/`USERPROFILE`, отдельные HOME/profile и bounded JSONL protocol — части security contract, а не косметика.
7. Сверьте семь разрешённых команд в `native/capabilities/main-local.json` и `native/build.rs`: `local_api_request`, `local_import_preview`, `local_import_confirm`, `local_export_bundle`, `codex_status`, `codex_login`, `confirm_close`.

На этой фазе критерий готовности — короткая таблица «функция → Windows-реализация → Mac-реализация → тест → fail-closed поведение». Не удаляйте Windows-код, чтобы таблица выглядела короче.

## Фаза 1 — сделать platform boundary переносимой

Разделяйте алгоритм и OS-операцию. Domain/storage/transfer должны сохранить тот же wire contract, лимиты, SHA и ошибки; только platform backend получает Mac-реализацию.

- Для дочерних процессов замените Job Object на проверенный Mac-механизм process-group/kill-on-close с подтверждением остановки всех потомков. Сохраняйте bounded stdin/stdout/stderr, timeout, cancel и отсутствие наследования окружения.
- Для checksum используйте проверенный Rust/системный SHA-256 путь с теми же результатами; stub, который всегда возвращает ошибку, допустим только до готового Mac-порта.
- Для open/save `.kontur` и attachment выберите native file chooser через поддержанный Tauri/Mac API. Сохраняйте проверку локального пути, запрет неожиданных namespace, SHA-bound preview/confirm и atomic publish.
- Для single-instance/activation используйте macOS-совместимый механизм, который не раскрывает произвольный IPC. Первая копия должна получить фокус, а вторую нельзя считать успешным запуском.
- Для WebView security перенесите только проверяемые инварианты: bundled navigation, denied new windows, закрытый devtools и data directory. Не подменяйте их HTML-кнопками.
- Оставьте fail-closed ошибку, если обязательная функция ещё не перенесена. «Ничего не делает, но окно открылось» хуже явного unavailable.

Удобная форма — новый `platform_macos.rs` или точечные `cfg`-модули рядом с существующей реализацией. Названия и зависимости выберите после чтения API; не добавляйте production-зависимость без объяснения в diff.

## Фаза 2 — bundled Python и AI-адаптеры

Python-исходники `full/backend` в основном переиспользуемы. Native launcher и runtime — нет.

1. Для разработки используйте venv на выбранной архитектуре Mac. Это только dev bootstrap; приложение не должно зависеть от системного Python.
2. Для финального portable `.app` подготовьте отдельный native ARM/Intel CPython 3.12.14 runtime для `arm64` или `x86_64` и его проверенную Mac dependency closure. Windows runtime 3.12.14 с 25 дистрибутивами нельзя перепаковать как Mac runtime.
3. Перенесите `backend/offline_bridge.py` и ASGI/SQLite boundary без изменения приватного JSONL framing, ready frame, limits и profile isolation.
4. В `backend.rs` замените `python/python.exe` и `child_visible_path` на безопасное разрешение owned executable/path для Mac. Не используйте `python3` из PATH в финальном пакете.
5. В `codex.rs` и `opencode.rs` вынесите discovery `.exe`/Windows profile paths в Mac resolver. Ищите только допустимый бинарник, создавайте отдельный owned HOME/profile, не передавайте ключи в аргументах и не читайте профиль владельца. Финальный bundle не должен зависеть от system Python.
6. Сначала проверяйте `--version`/help/features и протокол на synthetic binary. Только отдельное решение владельца разрешает ручной login/model gate.

## Фаза 3 — Tauri-конфигурация и bundle

Сейчас `native/tauri.conf.json` имеет `bundle.active: false`, target `nsis` и единственную иконку `icons/icon.ico`. Это Windows-настройка, не основание для Mac bundle.

- Добавьте отдельную macOS-конфигурацию/merge с `.icns`, ресурсами `full/frontend/out`, bundled backend и Mac Python. Не меняйте Windows target и не подменяйте ICO на всех платформах.
- Проверьте, что `frontendDist` остаётся `../full/frontend/out`, а resource placement совпадает с тем, что читает `BackendService`.
- В capability оставьте только `main-local` для окна `main` и те же семь команд. Любой новый command сначала добавьте в контракт и тесты, а не «на всякий случай».
- Текущий пакет не содержит готового `tauri` CLI script и не включает Mac bundle-конфигурацию. Поэтому `cargo tauri build --bundles app` — шаг **после** порта и установки CLI, а не команда для текущего snapshot.
- Официальный Tauri-путь собирает `.app` на Mac: [macOS application bundle](https://v2.tauri.app/distribute/macos-application-bundle/). Подпись/распространение — отдельный этап; не утверждайте, что Apple Developer Account обязателен для первой локальной разработки.
- Публичный source-only snapshot не может выполнить Windows `cmd`/NSIS-инсталлятор на Mac. Проверки Windows installer остаются на Windows CI/машине.

## Фаза 4 — WKWebView, Pyodide и окно

`full/frontend/lib/browser-python.ts` загружает пять локальных Pyodide-файлов, проверяет SHA-256, создаёт sandboxed opaque-origin iframe и Worker, а `pyodide.asm.wasm` подаёт через локальный buffer. Проверяйте это именно в WKWebView, не только в Chrome.

- Убедитесь, что `worker-src blob:`, `script-src` с `blob:`/`wasm-unsafe-eval`, локальные `connect-src` и IPC позволяют стартовать Worker/WASM, но не открывают сеть.
- Проверьте, что Worker не видит parent DOM, cookies, shared IndexedDB или native invoke; Python stdin/stdout/stderr, stop, timeout и output cap должны остаться.
- Проверьте `window.__TAURI__.core.invoke` для каждой из семи команд и отказ при отсутствии native bridge. Browser fixture допустим только в QA с явной `TEST_ONLY` меткой.
- Проверьте настоящие decorations/min/max/close, dirty-close barrier, retry после ошибки и восстановление окна. Не делайте отдельные HTML-кнопки заменой системных controls.
- Не меняйте CSP ради удобства отладки. Если WKWebView требует уточнения, добавьте узкий тест и объясните каждое новое разрешение.

## Фаза 5 — поведение, которое нужно принять руками

После появления окна и local bridge прогоняйте на synthetic-профиле, без реального bank/AI:

1. Запуск, readiness и seed: приложение открывается, локальный bridge отвечает, а отсутствие seed даёт ошибку, не demo-данные.
2. Каталог, выбранная задача, progress и durable save/reopen.
3. Board/ink: линии, undo/redo, annotation, resize, zoom, pointer и отмена; сравните координаты на обычном и Retina display.
4. Code editor: ввод, tab/line numbers, save barrier, code run через browser Pyodide и остановка по timeout.
5. Task attachments: точные bytes/MIME/SHA, безопасное имя, таблицы и локальное скачивание; повреждённый или отсутствующий asset не должен выглядеть доступным.
6. Tutor settings: context preview, выбранная модель/effort, unavailable/status/cancel. Не запускайте реальную модель как часть обычной smoke-проверки.
7. Cmd-хоткеи и shortcut profile: проверьте Cmd вместо Ctrl, Cmd+Shift+Z для redo и конфликты. Проверьте F11 и Fn+F11 на конкретной клавиатуре — macOS не даёт права предположить один вариант.
8. Закрытие окна, экспорт, импорт, перезапуск и повторное открытие.

Особенно внимательно проверьте retina ink alignment: CSS pixels, device pixels, canvas backing size, zoom и scroll должны описывать одну систему координат. Снимок экрана — полезная подсказка, но не замена проверке сохранённых stroke coordinates.

## Фаза 6 — catalog и полный `.kontur` round-trip

Ресурсы bank находятся вне публичного исходного дерева и игнорируются: `resources/catalog/catalog.sqlite3` и `resources/catalog/recovery-provenance.json`. Зафиксированная сводка — 6495 задач, 9176 ревизий, 3773 assets, около 2.63 GB, SHA-256 `d37628fb23b7253564e4a7eeea84e1d5eb02900a9bbed921d447e4bddd4b01ab`.

- `offlineReady=true`, но `complete=false`; есть 30 legacy warnings и одна approved replacement. `recovery-provenance.json` — источник происхождения, а не разрешение объявить demo-пакет настоящим.
- Не создавайте маленький fake/demo bank под теми же названиями и не меняйте `offlineReady` вручную.
- Используйте `docs/CATALOG_TRANSFER_RU.md` и `scripts/verify-catalog.py` для read-only проверки внешнего seed. Не загружайте seed, ключи или сырые ответы во внешнюю модель.
- Заполните seed только после того, как Mac runtime и bridge умеют валидировать его схему, hash и готовность.

Для `.kontur` проверяйте full-stream целиком: export → SHA → fresh synthetic profile → native preview → digest-bound confirm → import → reopen → export. Полный формат уже имеет потоковый лимит 3,5 GiB (`MAX_FULL_CONTAINER_BYTES` в `native/src/commands.rs`, `MAX_CONTAINER_BYTES` в `full/backend/offline_transfer.py`); legacy mini-формат отдельно ограничен 256 MiB. Сохраняйте оба контракта, ограничения assets/body, SHA, revision/attempt/document/preferences/exam state и atomic backup/conflict semantics. Не переносите меньший legacy-лимит на полный банк. Seven-table mini fixture не доказывает полный 56-table domain.

Подготовьте тестовую фикстуру командой `python qa/prepare_native_fixtures.py`; она создаёт только synthetic transfer input, не настоящий каталог. Windows-only tests помечайте как неприменимые на Mac, но переносимые domain/transfer tests запускайте.

## Команды итоговой проверки

```bash
cd full/frontend
npm ci
cd ../..
node scripts/prepare-pyodide.mjs
cd full/frontend
npm run typecheck
npm test
KONTUR_BUILD_CPUS=1 npm run build -- --webpack
cd ../..

cd full/backend
python -m pytest tests
ruff check app tests offline*.py
mypy app
cd ../..

python qa/prepare_native_fixtures.py
cargo metadata --manifest-path native/Cargo.toml --format-version 1 --locked --no-deps
cargo check --manifest-path native/Cargo.toml --locked
```

Для каждого gate запишите commit, архитектуру, macOS version, команду, exit code и пропуски. Не называйте `cargo check` «Mac build», а source review — runtime acceptance.

## Когда можно остановиться

Остановитесь и сообщите blocker, если нарушен обязательный gate, seed отсутствует/неполон, CSP пришлось расширять без обоснования, bridge не изолирован, или тест начал читать настоящий профиль.

Работу можно считать первой рабочей Mac-вехой только после цепочки: desktop window → local bridge/seed → progress/save → ink/editor → attachments → tutor config/status → полный `.kontur` round-trip с сохранением лимитов и bytes. Реальный model/login gate остаётся ручным и необязательным, с отдельным аккаунтом и явным разрешением.

## Официальные ссылки

- Tauri prerequisites: [v2.tauri.app/start/prerequisites](https://v2.tauri.app/start/prerequisites/) — desktop Command Line Tools, Rust и framework prerequisites.
- Tauri macOS bundle: [v2.tauri.app/distribute/macos-application-bundle](https://v2.tauri.app/distribute/macos-application-bundle/) — сборка `cargo tauri build --bundles app` выполняется на Mac после подготовки проекта.

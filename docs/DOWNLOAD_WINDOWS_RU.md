# Сборка offline-пакета Windows из частей

Этот helper нужен только для локальной сборки скачанного релизного архива. Он не является частью runtime Контур, ничего не запускает и не обращается к AppData, профилю пользователя или старой установке.

## Что скачать

Для релиза `offline-2026-11-01` из private `RamLeak/kontur-offline` нужны рядом в одной папке:

- `Kontur-Local-Windows-Offline.parts.json`;
- все двадцать файлов `Kontur-Local-Windows-Offline.zip.part01` … `.part20`;
- `scripts/assemble-offline-download.ps1` (и при желании `.cmd`).

Manifest закрепляет точные размеры и SHA-256. Нельзя переименовывать части, менять их порядок или подменять существующий файл повторной загрузкой.
Прямой публичный запуск `.ps1` и запуск через `.cmd` принимают только этот production manifest: его исходный SHA-256 проверяется до обращения к частям или `gh.exe`.

## Запуск

Откройте PowerShell в папке со manifest и частями:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\assemble-offline-download.ps1
```

Если helper лежит в репозитории, а скачанные файлы — в другой папке, укажите manifest и папку для нового результата:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\assemble-offline-download.ps1 `
  -ManifestPath C:\Downloads\Kontur-Local-Windows-Offline.parts.json `
  -OutputDirectory C:\Downloads
```

`.cmd` запускается двойным кликом или из `cmd.exe`; это bootstrap-режим: он по умолчанию передаёт helper ключ `-DownloadMissing`, чтобы при отсутствующих частях предложить/запустить обычный `gh release download`. Он меняет только текущий каталог дочернего процесса на папку самого helper и не меняет глобальную execution policy:

```cmd
assemble-offline-download.cmd
```

При успехе helper создаёт новую уникальную папку `Kontur-offline-...`, кладёт в неё собранный ZIP с исходным именем и распакованную подпапку `extracted`. Существующие ZIP и каталоги не перезаписываются. ZIP проверяется как `42aec3208f7bf942023422bfb16ed85830759ac05a24f59ae43cc4640451ebb2`; после распаковки проверяются `kontur-local.exe` и `full-local-resources-manifest.json` по SHA из manifest.
Для совместимости с Windows PowerShell 5 helper заранее отклоняет planned extraction paths длиной 240 и более; при такой ошибке выберите более короткую папку `-OutputDirectory`.

## Если части ещё не скачаны

Без `gh.exe` скачайте manifest и все части вручную со страницы релиза:

<https://github.com/RamLeak/kontur-offline/releases/tag/offline-2026-11-01>

При прямом запуске `.ps1` без флага helper по умолчанию остаётся local-only и только сообщает об отсутствующих файлах. Если `gh.exe` уже установлен, для явного запуска обычной команды GitHub CLI используйте:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\assemble-offline-download.ps1 -DownloadMissing
```

Helper передаёт `gh release download` только repository, tag, точные `--pattern` имена отсутствующих частей и `--dir`. Он не читает и не выводит токены, cookies, auth-файлы или профиль GitHub. Если команда не завершилась успешно, сборка останавливается.

## Формат manifest v1

Производственный файл имеет только следующие поля:

```json
{
  "schemaVersion": 1,
  "repository": "RamLeak/kontur-offline",
  "tag": "offline-2026-11-01",
  "archive": {
    "name": "Kontur-Local-Windows-Offline.zip",
    "bytes": 1297102998,
    "sha256": "42aec3208f7bf942023422bfb16ed85830759ac05a24f59ae43cc4640451ebb2",
    "memberCount": 3252,
    "uncompressedBytes": 2725976057,
    "largestMemberBytes": 2632617984,
    "executablePath": "kontur-local.exe",
    "executableSha256": "7b2008f7da21335f8ca1dd53d2c099a14c484f2884751ade596144d8d1251612",
    "resourcesManifestPath": "full-local-resources-manifest.json",
    "resourcesManifestSha256": "4d989ce834eded14d13e0ba5370b4824bd6d1802199b4229c659f7c9d8e2a392"
  },
  "parts": [
    { "name": "Kontur-Local-Windows-Offline.zip.part01", "bytes": 67108864, "sha256": "..." }
  ]
}
```

Production manifest также имеет SHA-256 `95cba4c765d8191bd1970a79cdd3b5c32ab7cbd3d476bcbca793c418f1a6807f`. Произвольные синтетические manifest допускаются только внутренними функциями в QA; публичный запуск принимает исключительно закреплённый production manifest. Внутренние проверки используют от 1 до 99 последовательных `.partNN` файлов, ограничение 64 MiB и проверку суммы размеров.

## Проверки безопасности

Перед сборкой проверяются exact schema, pinned production manifest, имена/размеры/SHA всех частей и итоговый SHA ZIP. Перед распаковкой проверяются количество entries, общий и максимальный uncompressed size, duplicate paths, абсолютные пути, `..`, `:`, backslash, invalid Windows names, reparse/symlink entries и выход за новый extraction root. Каждый файл копируется потоково ограниченным буфером; ZIP целиком в память не загружается.

Распакованная папка предназначена для последующей ручной проверки. Helper не запускает `kontur-local.exe` и не выполняет содержимое архива.

# ROOT: совместимый экспорт каталога v4

Пользователь разрешил разовую выгрузку банка и необходимые работы. SQL/права/
источники/приватные ограничения не расширены; frozen v3 сохранён отдельно.

v4 исправляет только nullable table_manifest: SQL NULL и JSON text null дают
один logical null. Остальные обязательные JSON object/array null запрещены.
Независимое code-review подтвердило узкий diff; 38 host-тестов:37 pass/1 Windows
symlink privilege skip. Перед реальной выгрузкой обязательны свежие 38 Linux tests.

Producer: export_catalog.py
SHA256: 0db423ec0ba389358a29308810200ee50f78ce0c7bccb737be6a0b4e7ed71889
Manifest revision: 4. Полный формат: соседний CATALOG_SEED_FORMAT.md.
Точные остальные SHA/состав payload — manifest.json. В manifest нет исходников
приложения, профиля, ключей или данных пользователей.

Историческая выгрузка выполнена владельцем в согласованном read-only контексте.
Адрес, учётная запись и идентификаторы инфраструктуры в публичную копию не входят.
Входы строго /var/lib/ege-workspace/app.sqlite и /var/lib/ege-workspace/assets.
SQL mode=ro/query_only, whitelist task/catalog tables+columns, без owners/progress/
переписок/auth/config. Cache только читается с проверкой path/size/hash.

Передача только двух скриптов и manifest штатными SSH/SCP. Код v4 в новой
подпапке v4 уже выделенного временного stage; SHA проверяется до исполнения.
/tmp контейнера ограничен64MiB, поэтому output — новый catalog.sqlite3 в ранее
созданной уникальной папке kontur-offline-export-* существующего app_state.
Точные пути фиксируются в RUN_CONTEXT.json. Без sudo, mounts, restart, deploy,
миграции, изменения БД/cache/ACL или сетевого дозаполнения. Export process
ограничен768MiB address space; owner/права приложения не повышаются.

После Linux gate выполнить тот же producer. Успех только exit0 и complete=true.
Missing assets/invalid data — остановка с безопасными codes/counts, не green gate.
Выход перенести docker cp в собственный host stage, затем SCP на ноутбук,
сравнить SHA и передать consumer. Никаких raw task/answer/asset данных в лог.
Существующие output не заменяются; чужие/старые данные не удаляются.

# План derived catalog seed v2 — 2026-10-30

Статус: synthetic gates и независимое review пройдены; настоящий v1
финализирован в отдельный v2 и принят реальным consumer. 3773 admitted byte rows,
2586242042 байта, `offlineReady=true`, `complete=false`, exit2 из-за сохранённых
30 исходных предупреждений. Исходный v1 не менялся. Точные hashes и отдельное
одобренное владельцем official-file recovery описаны в `docs/CATALOG_SEED_V2.md`.

## Граница

`finalize_catalog.py` читает только явно переданный v1 seed в
`sqlite3` URI `mode=ro` с `PRAGMA query_only=ON`/`trusted_schema=OFF` и
только локальный asset-snapshot.  Исходный файл не меняется.  Логическая
SQLite backup-копия сохраняет committed WAL-состояние во временном новом
файле; публикация не заменяет
существующий output.  Нет URL-fetch, source SQL, profile/user/auth/env чтения,
сетевых вызовов, app imports или production writes.

Проверки до копирования: application id `KCS1`, SQLite `user_version=1`,
ровно семь таблиц v1, canonical JSON, v1 `manifestSha256`, все шесть
`collectionsSha256`, row counts, references, rights и фактические SHA-256
исходных BLOB.  Неизменяемый источник сверяется до и после чтения.

## Snapshot handshake

Worker snapshot передаёт два явно ограниченных пути: `--snapshot-root` и
`--snapshot-manifest`. Finalizer принимает canonical per-asset manifest ниже.
Также совместим с текущим bounded worker output: `manifest.json` с
`{"format":"kontur-asset-snapshot-v1","entries":[...]}` и соседним
`receipt.json` (`seedSha256`/`createdAt`); grouped `assetIds` разворачиваются в
per-asset entries, а успешные `path`/`sha256`/`sizeBytes`/`detectedType`
проверяются. Это позволяет worker сохранить свои URL-grouped records, не
передавая URL finalizer-у и не меняя его bounded output.

```json
{
  "format": "kontur-asset-snapshot-v1",
  "sourceSeedSha256": "<lowercase sha256 of reviewed v1 file>",
  "capturedAt": "<non-empty UTC timestamp>",
  "assets": [
    {
      "assetId": "<v1 catalog_assets.id>",
      "relativePath": "bytes/<confined-file>",
      "expectedSha256": "<lowercase sha256>",
      "sizeBytes": 123,
      "detectedType": "<non-empty detected MIME/type>",
      "fetchedAt": "<non-empty UTC timestamp>"
    }
  ],
  "counts": {"assets": 1, "bytes": 123}
}
```

В canonical форме `assets` должен содержать ровно v1 assets без исходного
`catalog_asset_bytes`; сейчас это 3,485 metadata-only/failed rows.  В worker
форме успешные entries могут также перечислять 288 cached rows; их bytes
сверяются с immutable v1 и не используются для переписывания source proof.
Пути
только slash-separated относительные компоненты без `..`, drive/UNC,
backslash, symlink/reparse point.  Файл читается с проверкой размера и
вычисленного SHA-256. Проверенные bytes по одному копируются в private staging,
затем потоково записываются через SQLite `blobopen`; весь банк в RAM не собирается.
Лимиты после фактических измерений: 64000000 байт на asset и 3 GiB по asset IDs.
`manifestSha256` опционален; если worker его
поставил, он вычисляется по canonical JSON без самого ключа.

`expectedSha256` обязан совпадать с исходным `expected_content_hash`, если тот
был задан, и с фактическим BLOB. Единственное исключение — доказанный
metadata-hash sentinel из receipt-only handshake ниже: worker ставит явный
`expectedContentHash=null`, а finalizer сначала проверяет полное исходное
fingerprint-доказательство и только затем очищает этот sentinel. `detectedType`
и `fetchedAt` обязательны. Любой настоящий expected hash остаётся
provenance-полем.

### Legacy metadata-hash sentinel handshake

Для ровно 109 доказанных URL-конфликтов worker может приложить отдельный
receipt-only блок (не запись `catalog_metadata` и не часть v2 metadata):

```json
{
  "legacyHashSentinelNormalization": {
    "entries": [{
      "assetId": "<v1 asset id>",
      "canonicalUrl": "<exact v1 canonical_url>",
      "filename": "<exact v1 filename>",
      "originalExpectedContentHash": "<exact v1 expected_content_hash>",
      "normalizedExpectedContentHash": null,
      "fingerprint": {
        "canonicalUrl": "<exact v1 canonical_url>",
        "filename": "<exact v1 filename>",
        "cacheState": "metadata_only|failed",
        "sizeBytes": 0,
        "contentHash": "<exact v1 content_hash>",
        "expectedContentHash": "<exact v1 expected_content_hash>",
        "hashVerified": false,
        "detectedType": null
      }
    }]
  }
}
```

Finalizer clears `expected_content_hash` only if the immutable v1 row has
`content_hash == expected_content_hash == sha256("metadata:" + canonical_url +
":" + filename)`, `size_bytes=0`, `hash_verified=false`, `detected_type=null`,
and cache state `metadata_only` or `failed`, while every receipt fingerprint
field matches that row exactly. The worker's actual byte `sha256`, size and
detected type remain mandatory and are independently verified. A partial
fingerprint, a genuine expected SHA, an unknown asset, or any mismatch fails
before output copy. The source evidence count is recorded only in
`LEGACY_HASH_SENTINEL_METADATA.log`; no new catalog metadata key is added.

## Derived v2

Builder меняет только `catalog_assets`, `catalog_asset_bytes` и metadata:

- для каждого asset устанавливает фактические `content_hash`, `size_bytes`,
  `detected_type`, `cache_state=ready`, `hash_verified=true`, `fetched_at`;
  URL/filename/media/rights/expected provenance не меняются;
- сохраняет task/revision/link/selection rows без изменения;
- пересчитывает шесть canonical collection hashes и `manifestSha256`;
- ставит `format=kontur-catalog-seed-v2`, `schemaVersion=2`, `sourceSchema=12`,
  `offlineReady=true`, `sourceSeedSha256=<lowercase source SHA>`;
- удаляет только разрешённые `asset_not_ready` issues после фактического
  разрешения bytes. `upstream_resource_unavailable` IDs извлекаются из
  неизменённых `catalog_revisions.source_metadata`, сверяются с v1 count и
  заново записываются полным точным набором; поэтому cap в v1 issue examples
  не теряет source warnings. При 30 таких marker-ов `complete=false`,
  `offlineReady=true`, receipt exit `2`.

Любой unknown issue, missing/extra snapshot asset, missing file, wrong hash or
size, disallowed rights, unresolved reference, bad original metadata/hash or
budget overflow останавливает операцию до output copy. Empty/fake BLOB не
создаётся и asset не пропускается.

## Проверки

```text
python -m unittest -v test_finalize_catalog.py
python -m py_compile finalize_catalog.py test_finalize_catalog.py
```

Synthetic tests фиксируют immutable input, no-overwrite/failure-no-copy,
expected-hash and missing-file rejection, unknown asset/issue and rights
rejection, count/hash rewrites, task/revision/link/selection preservation and
reconstruction of a capped source-warning sample, plus positive sentinel
normalization and negative partial-fingerprint/genuine-SHA-mismatch cases.


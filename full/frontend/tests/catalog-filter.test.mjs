import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFile(join(root, path), "utf8");

test("selected type catalog keeps source metadata filters and server number order", async () => {
  const api = await read("lib/api.ts");
  const types = await read("lib/types.ts");
  const bank = await read("components/task-bank-view.tsx");

  assert.match(types, /source_year\?: number \| null/);
  assert.match(types, /source_difficulty\?: SourceDifficulty/);
  assert.match(types, /difficulty: TaskDifficulty \| null/);
  assert.match(types, /source_metadata\?: Record<string, unknown> \| null/);
  assert.match(types, /sourceYear\?: number/);
  assert.match(types, /difficulty\?: TaskDifficulty/);
  assert.match(types, /sort\?: TaskCatalogSort/);
  assert.match(types, /catalog\?: TaskCatalogKind/);
  assert.match(api, /params\.set\("source_year", String\(query\.sourceYear\)\)/);
  assert.match(api, /params\.set\("difficulty", query\.difficulty\)/);
  assert.match(api, /params\.set\("sort", query\.sort\)/);
  assert.match(api, /params\.set\("catalog", query\.catalog\)/);
  assert.match(api, /function normalizeDifficulty\(value: unknown\)/);
  assert.match(api, /value === 0 \? "easy" : value === 1 \? "medium" : value === 2 \? "hard"/);
  assert.match(bank, /catalog: sourceCatalog/);
  assert.match(bank, /const sourceCatalog: TaskCatalogKind = "all"/);
  assert.doesNotMatch(bank, /targetExamYear: TARGET_EXAM_YEAR/);
  assert.match(bank, /useState\(initialFilters\.line \?\? getPreferences\(\)\.defaultTaskType\)/);
  assert.match(bank, /difficultyOptions = \["Все сложности", "easy", "medium", "hard"\]/);
  assert.match(bank, /sort: "number"/);
  assert.match(bank, /const alreadyDefault = query === ""/);
  assert.match(bank, /if \(alreadyDefault\) \{\s+retryCatalog\(\);\s+return;/);
  assert.match(bank, /formatSourceLabel\(sourceLabel, sourceYear\)/);
  assert.match(bank, /function formatSourceLabel\(label: string \| null \| undefined, year: number\)/);
  assert.match(bank, /className=\{styles\.filterChip\}/);
  assert.match(bank, /focusFilter\("difficulty"\)/);
  assert.match(bank, /focusFilter\("status"\)/);
  assert.match(bank, /focusFilter\("bookmarks"\)/);
  assert.match(bank, /ref=\{difficultyFilterRef\}/);
  assert.match(bank, /ref=\{bookmarksFilterRef\}/);
  assert.match(bank, /TaskImages assets=\{task\.assets\} canPrepare=\{!isDemo\} revisionHash=\{task\.revision\.content_hash\}/);
  assert.match(bank, /data-sequence-position=\{position\}/);
  assert.match(bank, /data-source-id=\{sourceId \?\? undefined\}/);
});

test("catalog coverage stays collapsed and exposes only readable cell facts", async () => {
  const api = await read("lib/api.ts");
  const types = await read("lib/types.ts");
  const bank = await read("components/task-bank-view.tsx");

  assert.match(api, /getTaskCatalogCoverage\(\): Promise<TaskCatalogCoverage>/);
  for (const field of ["published", "policy_version", "primary_years", "fallback_year", "fallback_threshold", "target_per_cell", "total_selected", "quota_met", "updated_at", "cells"]) {
    assert.match(types, new RegExp(`${field}:`));
  }
  for (const field of ["line_number", "difficulty", "primary_available", "fallback_available", "fallback_allowed", "selected_primary", "selected_fallback", "total", "shortfall"]) {
    assert.match(types, new RegExp(`${field}:`));
  }
  assert.match(bank, /<details>/);
  assert.match(bank, /Номер в покрытии/);
  assert.match(bank, /Уровень в покрытии/);
  assert.match(bank, /Резерв \{fallbackYear/);
  assert.match(bank, /Не хватает \$\{cell\.shortfall\}/);
  assert.doesNotMatch(bank, /policy_version/);
  assert.doesNotMatch(bank, /last_sync/);
});

test("catalog pagination and retry ignore stale requests", async () => {
  const bank = await read("components/task-bank-view.tsx");
  assert.match(bank, /const catalogRequestRef = useRef\(0\)/);
  assert.match(bank, /if \(!cancelled && requestId === catalogRequestRef\.current\)/);
  assert.match(bank, /if \(requestId !== catalogRequestRef\.current\) return;/);
  assert.match(bank, /catalogRequestRef\.current \+= 1/);
  assert.match(bank, /setLoadingMore\(false\)/);
});

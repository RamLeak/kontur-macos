import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFile(join(root, path), "utf8");

test("navigation rail keeps every primary route and opens the existing drawer for long content", async () => {
  const sidebar = await read("components/sidebar.tsx");
  const shell = await read("components/app-shell.tsx");
  const css = await read("components/sidebar-rail.module.css");
  const konturCss = await read("app/kontur.css");

  assert.match(sidebar, /rail\?: boolean/);
  assert.match(sidebar, /className=\{railStyles\.rail\}/);
  for (const label of ["Новый разбор", "Банк заданий", "Пробники", "Ошибки", "Закладки", "Прогресс", "Проекты", "История чатов", "Настройки", "Выйти из рабочей области"]) {
    assert.match(sidebar, new RegExp(label));
  }
  assert.match(sidebar, /id="kontur-navigation-drawer"/);
  assert.match(sidebar, /aria-controls="kontur-navigation-drawer"/);
  assert.match(sidebar, /data-rail-drawer/);
  assert.match(shell, /const navigationRailVisible=authState==="ready"&&!workspaceActive/);
  assert.match(shell, /has-navigation-rail/);
  assert.match(shell, /rail=\{navigationRailVisible\}/);
  assert.match(css, /flex: 0 0 72px/);
  assert.match(css, /railDrawer\[aria-hidden="true"\]/);
  assert.match(konturCss, /grid-template-columns: 72px minmax\(0, 1fr\)/);
  assert.match(konturCss, /\.kontur-navigation-rail\.is-rail[\s\S]*overflow: visible/);
});

test("rail keeps the content column in layout on compact viewports and preserves keyboard affordances", async () => {
  const css = await read("components/sidebar-rail.module.css");
  const konturCss = await read("app/kontur.css");

  assert.match(css, /aria-hidden="true"/);
  assert.match(css, /:focus-visible/);
  assert.match(css, /prefers-reduced-motion/);
  assert.match(css, /\.railMiddle[\s\S]*overflow: auto/);
  assert.match(css, /\.railGroup[\s\S]*justify-items: center/);
  assert.match(konturCss, /@media\(max-width:1100px\)[\s\S]*has-navigation-rail/);
  assert.match(konturCss, /\.kontur-navigation-rail\.is-rail[\s\S]*position: relative/);
});

test("shell no longer exposes a separate header focus-exit button", async () => {
  const shell = await read("components/app-shell.tsx");
  assert.doesNotMatch(shell, /Выйти из фокуса/);
});

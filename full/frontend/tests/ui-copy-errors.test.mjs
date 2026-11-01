import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

function load() {
  const modules = new Map();
  const get = name => {
    if (modules.has(name)) return modules.get(name);
    const compiled = ts.transpileModule(readFileSync(new URL(`../lib/${name}.ts`, import.meta.url), "utf8"), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    }).outputText;
    const module = { exports: {} };
    new Function("module", "exports", "require", compiled)(module, module.exports, id => {
      assert.equal(id, "./types"); return get("types");
    });
    modules.set(name, module.exports); return module.exports;
  };
  return { ...get("types"), ...get("ui-copy") };
}

test("known native connection failures survive API_UNAVAILABLE wrapping", () => {
  const { ApiError, readableError } = load();
  const reason = new ApiError("AI_UNAVAILABLE", "internal diagnostic", 503, { native_code: "codex_busy", retryable: true });
  const copy = readableError(reason, "generic fallback");
  assert.match(copy, /Codex ещё занят/);
  assert.doesNotMatch(copy, /internal|native_code|generic fallback/);
  assert.match(readableError(new ApiError("AI_UNAVAILABLE", "internal", 503, { native_code: "codex_unavailable", retryable: true }), "fallback"), /ещё проверяется/);
});

test("unknown provider diagnostics stay private and known run failures have useful guidance", () => {
  const { ApiError, readableError, assistantFailureMessage } = load();
  assert.equal(readableError(new ApiError("UNKNOWN", "internal diagnostic", 500, { native_code: "unknown detail" }), "Повторите действие."), "Повторите действие.");
  assert.equal(assistantFailureMessage("unknown detail"), null);
  assert.match(assistantFailureMessage("codex_process_failed"), /выбранную модель/);
  assert.match(assistantFailureMessage("codex_protocol"), /прочитать ответ Codex/);
  assert.match(assistantFailureMessage("AI_UNAVAILABLE"), /настройки помощника/);
});

test("owner-session and throttling errors retain their own recovery actions", () => {
  const { ApiError, readableError } = load();
  assert.match(readableError(new ApiError("AUTH_REQUIRED", "internal", 401, {}), "fallback"), /Войдите снова/);
  assert.match(readableError(new ApiError("RATE_LIMITED", "internal", 429, {}), "fallback"), /Подождите/);
});

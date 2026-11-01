import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import ts from "typescript";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const source = await readFile(join(root, "lib/local-fetch.ts"), "utf8");
const transpiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;
const loaded = { exports: {} };
new Function("module", "exports", transpiled)(loaded, loaded.exports);
const {
  localFetch,
  isLocalDesktop,
  waitForLocalMutations,
  MAX_LOCAL_REQUEST_BYTES,
  MAX_LOCAL_RESPONSE_BYTES,
} = loaded.exports;

const tick = () => new Promise((resolve) => setImmediate(resolve));
const encoded = (value) => Buffer.from(value).toString("base64");
const jsonResponse = (value) => ({ status: 200, headers: [["Content-Type", "application/json"]], bodyBase64: encoded(JSON.stringify(value)) });

async function withBridge(invoke, run) {
  const previous = globalThis.__TAURI__;
  globalThis.__TAURI__ = { core: { invoke } };
  try {
    return await run();
  } finally {
    if (previous === undefined) delete globalThis.__TAURI__;
    else globalThis.__TAURI__ = previous;
  }
}

test("startup waits for ready without retrying business failures", async () => {
  let attempts = 0;
  await withBridge(async () => {
    attempts += 1;
    if (attempts === 1) throw { code: "local_backend_starting" };
    return jsonResponse({ ready: true });
  }, async () => {
    assert.deepEqual(await (await localFetch("/api/v1/local/client-state")).json(), { ready: true });
    assert.equal(attempts, 2);
  });
  attempts = 0;
  await withBridge(async () => { attempts += 1; throw { code: "local_backend_unavailable" }; }, async () => {
    await assert.rejects(localFetch("/api/v1/local/client-state"));
    assert.equal(attempts, 1);
  });
});

test("startup wait can be cancelled before another native request", async () => {
  const controller = new AbortController();
  let attempts = 0;
  await withBridge(async () => {
    attempts += 1;
    controller.abort();
    throw { code: "local_backend_starting" };
  }, async () => {
    await assert.rejects(localFetch("/api/v1/local/client-state", { signal: controller.signal }), { name: "AbortError" });
    assert.equal(attempts, 1);
  });
});

test("localFetch preserves the bounded business headers in the Tauri request", async () => {
  const calls = [];
  await withBridge(async (command, args) => {
    calls.push({ command, args });
    return jsonResponse({ ok: true });
  }, async () => {
    assert.equal(isLocalDesktop(), true);
    const response = await localFetch("/api/v1/catalog?limit=1", {
      method: "post",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": "request-1",
        "X-Sync-Owner": "owner-1",
      },
      body: '{"query":"ю"}',
    });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { ok: true });
    assert.deepEqual(calls, [{
      command: "local_api_request",
      args: {
        request: {
          method: "POST",
          path: "/api/v1/catalog?limit=1",
          body: '{"query":"ю"}',
          headers: {
            "content-type": "application/json",
            "idempotency-key": "request-1",
            "x-sync-owner": "owner-1",
          },
        },
      },
    }]);
  });
});

test("browser credentials and malformed business headers are rejected", async () => {
  let calls = 0;
  await withBridge(async () => { calls += 1; return jsonResponse({ ok: true }); }, async () => {
    await assert.rejects(
      localFetch("/api/v1/attempts", { method: "POST", headers: { Authorization: "Bearer secret" }, body: "{}" }),
      (error) => error?.code === "LOCAL_HEADERS_FORBIDDEN",
    );
    await assert.rejects(
      localFetch("/api/v1/attempts", { headers: { "Content-Type": "text/plain" } }),
      (error) => error?.code === "LOCAL_HEADERS_FORBIDDEN",
    );
    await assert.rejects(
      localFetch("/api/v1/attempts", { headers: { "Idempotency-Key": "x\tbad" } }),
      (error) => error?.code === "INVALID_LOCAL_HEADERS" || error?.code === "LOCAL_HEADERS_FORBIDDEN",
    );
    await assert.rejects(
      localFetch("/api/v1/attempts", { headers: { "X-Sync-Owner": "x".repeat(1025) } }),
      (error) => error?.code === "INVALID_LOCAL_HEADERS",
    );
    assert.equal(calls, 0);
  });
});

test("there is no browser/network fallback outside the bundled Tauri bridge", async () => {
  const previousTauri = globalThis.__TAURI__;
  const previousFetch = globalThis.fetch;
  let networkCalls = 0;
  delete globalThis.__TAURI__;
  globalThis.fetch = async () => { networkCalls += 1; throw new Error("network should not be used"); };
  try {
    assert.equal(isLocalDesktop(), false);
    await assert.rejects(localFetch("/api/v1/catalog"), (error) => error?.code === "LOCAL_DESKTOP_REQUIRED");
    assert.equal(networkCalls, 0);
  } finally {
    if (previousTauri === undefined) delete globalThis.__TAURI__;
    else globalThis.__TAURI__ = previousTauri;
    if (previousFetch === undefined) delete globalThis.fetch;
    else globalThis.fetch = previousFetch;
  }
});

test("path and method validation rejects absolute URLs and traversal", async () => {
  let calls = 0;
  await withBridge(async () => { calls += 1; return jsonResponse({ ok: true }); }, async () => {
    for (const path of ["https://example.test/api/v1/catalog", "/api/v2/catalog", "/api/v1/../secret", "/api/v1/%2e%2e/secret", "/api/v1/catalog#fragment", "/api/v1/catalog\\file"]) {
      await assert.rejects(localFetch(path), (error) => error?.code === "INVALID_LOCAL_PATH");
    }
    await assert.rejects(localFetch("/api/v1/catalog", { method: "HEAD" }), (error) => error?.code === "INVALID_LOCAL_METHOD");
    assert.equal(calls, 0);
  });
});

test("request bodies are bounded by UTF-8 bytes", async () => {
  assert.equal(MAX_LOCAL_REQUEST_BYTES, 8 * 1024 * 1024);
  const calls = [];
  const exact = "я".repeat(MAX_LOCAL_REQUEST_BYTES / 2);
  assert.equal(new TextEncoder().encode(exact).byteLength, MAX_LOCAL_REQUEST_BYTES);
  await withBridge(async (command, args) => { calls.push({ command, args }); return { status: 204, headers: [], bodyBase64: "" }; }, async () => {
    await localFetch("/api/v1/documents", { method: "PUT", body: exact });
    await assert.rejects(localFetch("/api/v1/documents", { method: "PUT", body: exact + "я" }), (error) => error?.code === "LOCAL_REQUEST_TOO_LARGE");
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].args.request.body, exact);
});

test("binary responses and empty 204/304 responses remain Response-compatible", async () => {
  const responses = [
    { status: 204, headers: [["X-Ready", "yes"]], bodyBase64: "" },
    { status: 304, headers: [], bodyBase64: "" },
    { status: 200, headers: [["Content-Type", "application/octet-stream"]], bodyBase64: encoded(Buffer.from([0, 255, 1])) },
  ];
  await withBridge(async () => responses.shift(), async () => {
    const empty = await localFetch("/api/v1/flush");
    assert.equal(empty.status, 204);
    assert.equal(empty.body, null);
    assert.equal(await empty.text(), "");
    const notModified = await localFetch("/api/v1/flush");
    assert.equal(notModified.status, 304);
    assert.equal(notModified.body, null);
    const binary = await localFetch("/api/v1/assets/asset-1/device");
    assert.deepEqual([...new Uint8Array(await binary.arrayBuffer())], [0, 255, 1]);
  });
});

test("malformed and oversized response envelopes fail closed", async () => {
  const oversized = "A".repeat(Math.ceil((MAX_LOCAL_RESPONSE_BYTES + 1) / 3) * 4);
  const responses = [
    { status: 200, headers: [], bodyBase64: "%%%=" },
    { status: 204, headers: [], bodyBase64: encoded("unexpected") },
    { status: 200, headers: [], bodyBase64: oversized },
  ];
  await withBridge(async () => responses.shift(), async () => {
    for (let index = 0; index < 3; index += 1) {
      await assert.rejects(localFetch("/api/v1/catalog"), (error) => error?.code === "INVALID_LOCAL_RESPONSE" || error?.code === "LOCAL_RESPONSE_TOO_LARGE");
    }
  });
});

test("AbortSignal rejects promptly while a native mutation remains awaitable", async () => {
  let release;
  let called = false;
  await withBridge(async () => {
    called = true;
    return new Promise((resolve) => { release = resolve; });
  }, async () => {
    const controller = new AbortController();
    const request = localFetch("/api/v1/documents/1", { method: "PATCH", body: "{}", signal: controller.signal });
    await tick();
    assert.equal(called, true);
    controller.abort();
    await assert.rejects(request, (error) => error?.name === "AbortError");
    let waited = false;
    const waiting = waitForLocalMutations().then(() => { waited = true; });
    await tick();
    assert.equal(waited, false);
    release({ status: 200, headers: [], bodyBase64: encoded("{}") });
    await waiting;
    assert.equal(waited, true);
  });
});

test("native IPC failure is surfaced and never replaced with a network success", async () => {
  let networkCalls = 0;
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async () => { networkCalls += 1; return new Response("wrong"); };
  try {
    await withBridge(async () => { throw new Error("native detail must stay private"); }, async () => {
      await assert.rejects(localFetch("/api/v1/catalog"), (error) => error?.code === "LOCAL_API_REQUEST_FAILED");
    });
    assert.equal(networkCalls, 0);
  } finally {
    if (previousFetch === undefined) delete globalThis.fetch;
    else globalThis.fetch = previousFetch;
  }
});

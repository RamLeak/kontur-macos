import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import ts from "typescript";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const PRIOR_MEASURED_ASSET = 31_944_094;
const LARGEST_MEASURED_ASSET = 50_084_031;
const NEW_LIMIT = 64_000_000;

async function loadTypeScript(relativePath, stubs = {}) {
  const source = await readFile(join(root, relativePath), "utf8");
  const transpiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const loaded = { exports: {} };
  const requireStub = (id) => {
    if (id in stubs) return stubs[id];
    throw new Error(`unexpected import: ${id}`);
  };
  new Function("module", "exports", "require", transpiled)(loaded, loaded.exports, requireStub);
  return loaded.exports;
}

function digest(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

test("local decoded responses accept the prior and latest measured payloads", async () => {
  const local = await loadTypeScript("lib/local-fetch.ts");
  let currentBytes;
  assert.equal(local.MAX_LOCAL_RESPONSE_BYTES, NEW_LIMIT);
  await (async () => {
    const previous = globalThis.__TAURI__;
    globalThis.__TAURI__ = { core: { invoke: async () => ({
      status: 200,
      headers: [["Content-Type", "application/octet-stream"]],
      bodyBase64: currentBytes.toString("base64"),
    }) } };
    try {
      for (const size of [PRIOR_MEASURED_ASSET, LARGEST_MEASURED_ASSET]) {
        currentBytes = Buffer.alloc(size, 0x61);
        const response = await local.localFetch("/api/v1/assets/large/device");
        assert.equal((await response.arrayBuffer()).byteLength, size);
      }
    } finally {
      if (previous === undefined) delete globalThis.__TAURI__;
      else globalThis.__TAURI__ = previous;
    }
  })();
});

test("local decoded responses reject one byte above the 64,000,000-byte limit", async () => {
  const local = await loadTypeScript("lib/local-fetch.ts");
  const bytes = Buffer.alloc(NEW_LIMIT + 1, 0x62);
  const previous = globalThis.__TAURI__;
  globalThis.__TAURI__ = { core: { invoke: async () => ({
    status: 200,
    headers: [],
    bodyBase64: bytes.toString("base64"),
  }) } };
  try {
    await assert.rejects(local.localFetch("/api/v1/assets/too-large/device"), (error) => error?.code === "LOCAL_RESPONSE_TOO_LARGE");
  } finally {
    if (previous === undefined) delete globalThis.__TAURI__;
    else globalThis.__TAURI__ = previous;
  }
});

function asset(id, hash, size) {
  return {
    id,
    name: `${id}.bin`,
    media_type: "application/octet-stream",
    size_bytes: size,
    download_url: "",
    available: true,
    cache_state: "metadata_only",
    content_hash: hash,
    rights_status: null,
    canonical_url: null,
    progress_percent: null,
  };
}

test("device delivery accepts 50,084,031 and rejects 64,000,001 before body consumption", async () => {
  const bytes = new Uint8Array(LARGEST_MEASURED_ASSET).fill(0x63);
  const hash = digest(bytes);
  const calls = [];
  const cancelled = [];
  const device = await loadTypeScript("lib/device-files.ts", {
    "./api": { API_BASE: "" },
    "./local-fetch": {
      localFetch: async (path) => {
        calls.push(path);
        if (path.includes("too-large")) {
          const response = new Response(new Uint8Array(), { status: 200, headers: {
            "Content-Length": String(NEW_LIMIT + 1),
            "X-Content-Hash": "0".repeat(64),
          } });
          const body = response.body;
          const originalCancel = body?.cancel?.bind(body);
          if (originalCancel) body.cancel = (...args) => { cancelled.push(true); return originalCancel(...args); };
          return response;
        }
        return new Response(bytes, { status: 200, headers: {
          "Content-Length": String(bytes.byteLength),
          "X-Content-Hash": hash,
          "Content-Type": "application/octet-stream",
        } });
      },
    },
  });
  assert.equal(device.MAX_FILE_BYTES, NEW_LIMIT);
  const accepted = await device.loadDeviceAsset(asset("measured", hash, bytes.byteLength), "revision-a");
  assert.equal(accepted.bytes.byteLength, LARGEST_MEASURED_ASSET);
  await assert.rejects(
    device.loadDeviceAsset(asset("too-large", "0".repeat(64), NEW_LIMIT + 1), "revision-b"),
    /слишком большой/,
  );
  assert.ok(cancelled.length >= 1);
  assert.deepEqual(calls, ["/assets/measured/device", "/assets/too-large/device"]);
});

test("browser Python keeps the iframe/worker gate while applying the 64,000,000-byte total input cap", async () => {
  const browser = await loadTypeScript("lib/browser-python.ts", {
    "./browser-python-integrity": { PYTHON_RUNTIME_HASHES: {} },
  });
  assert.equal(browser.MAX_PYTHON_INPUT_BYTES, NEW_LIMIT);
  const previousFetch = globalThis.fetch;
  let fetchCalls = 0;
  globalThis.fetch = async () => {
    fetchCalls += 1;
    throw new Error("synthetic runtime fetch failure");
  };
  try {
    const accepted = browser.startBrowserPython("", [{ name: "measured.bin", bytes: new Uint8Array(LARGEST_MEASURED_ASSET) }], 1);
    const acceptedResult = await accepted.result;
    assert.equal(acceptedResult.status, "unavailable");
    assert.ok(fetchCalls > 0, "accepted input should reach the existing runtime loader gate");

    fetchCalls = 0;
    const rejected = browser.startBrowserPython("", [{ name: "too-large.bin", bytes: new Uint8Array(NEW_LIMIT + 1) }], 2);
    const rejectedResult = await rejected.result;
    assert.equal(rejectedResult.status, "failed");
    assert.match(rejectedResult.stderr, /превышают доступный объём/);
    assert.equal(fetchCalls, 0, "oversized input must stop before runtime/worker setup");
  } finally {
    if (previousFetch === undefined) delete globalThis.fetch;
    else globalThis.fetch = previousFetch;
  }
});

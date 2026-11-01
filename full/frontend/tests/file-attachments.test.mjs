import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../lib/device-files.ts", import.meta.url), "utf8");
const transpiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;

function loadDeviceFiles(stubs = {}) {
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

function taskAsset(name = "9.ods") {
  return {
    id: "asset-9",
    name,
    media_type: "application/vnd.oasis.opendocument.spreadsheet",
    size_bytes: 577 * 1024,
    download_url: "/api/v1/assets/asset-9/download",
    available: true,
    cache_state: "ready",
    content_hash: "0".repeat(64),
    rights_status: "private_authorized",
    canonical_url: null,
    progress_percent: null,
  };
}

test("local attachment keeps the exact safe filename and synthetic 9.ods bytes", async () => {
  const device = loadDeviceFiles({ "./api": { API_BASE: "" }, "./local-fetch": {} });
  const bytes = new Uint8Array(577 * 1024);
  for (let index = 0; index < bytes.length; index += 1) bytes[index] = index % 251;
  const file = await device.readLocalDeviceFile({
    name: "9.ods",
    type: "application/vnd.oasis.opendocument.spreadsheet",
    size: bytes.byteLength,
    arrayBuffer: async () => bytes.buffer,
  });
  assert.equal(file.name, "9.ods");
  assert.equal(file.bytes.byteLength, 577 * 1024);
  assert.equal(file.origin, "local");
  assert.equal(file.sha256, digest(bytes));
});

test("local attachment rejects unsafe names, oversize bytes, and duplicate Python names", async () => {
  const device = loadDeviceFiles({ "./api": { API_BASE: "" }, "./local-fetch": {} });
  const valid = { name: "data.csv", type: "text/csv", size: 1, arrayBuffer: async () => new Uint8Array([1]).buffer };
  await assert.rejects(device.readLocalDeviceFile({ ...valid, name: "..\\secret.csv" }), /Имя локального файла/);
  await assert.rejects(device.readLocalDeviceFile({ ...valid, size: device.MAX_FILE_BYTES + 1 }), /слишком большой/);
  const first = await device.readLocalDeviceFile(valid);
  const duplicate = { ...first, bytes: new Uint8Array([2]), sha256: "2".repeat(64) };
  assert.throws(() => device.validateDeviceFileSet([first, duplicate]), /совпадают имена/);
});

test("same-name local attachment replaces only the Python input, without fetching the bank asset", async () => {
  const calls = [];
  const device = loadDeviceFiles({
    "./api": { API_BASE: "" },
    "./local-fetch": { localFetch: async (path) => { calls.push(path); throw new Error("bank fetch must not run"); } },
  });
  const bytes = new Uint8Array(577 * 1024).fill(7);
  const local = { name: "9.ods", bytes, sha256: digest(bytes), mediaType: "application/vnd.oasis.opendocument.spreadsheet", origin: "local" };
  const files = await device.loadTaskDeviceFiles([taskAsset()], 9, "revision-9", undefined, [local]);
  assert.equal(files.length, 1);
  assert.equal(files[0], local);
  assert.deepEqual(calls, []);
});

test("synthetic 577 KB 9.ods device-text response accepts the v3 converter envelope", async () => {
  const bytes = new Uint8Array(577 * 1024).fill(9);
  const sourceHash = digest(bytes);
  const textBytes = new TextEncoder().encode("a\tb\n1\t2\n");
  const calls = [];
  const device = loadDeviceFiles({
    "./api": { API_BASE: "/api/v1" },
    "./local-fetch": {
      localFetch: async (path) => {
        calls.push(path);
        if (path.endsWith("/device")) return new Response(bytes, { status: 200, headers: { "x-content-hash": sourceHash, "content-type": "application/vnd.oasis.opendocument.spreadsheet" } });
        return new Response(JSON.stringify({ source_sha256: sourceHash, files: [{ filename: "9.txt", sheet_name: "Data", converter_version: "type9-tsv-v3", content_base64: Buffer.from(textBytes).toString("base64"), content_hash: digest(textBytes) }] }), { status: 200, headers: { "content-type": "application/json" } });
      },
    },
  });
  const files = await device.loadDeviceTexts({ ...taskAsset(), content_hash: sourceHash }, "revision-9");
  assert.equal(files[0].name, "9.txt");
  assert.equal(files[0].converterVersion, "type9-tsv-v3");
  assert.deepEqual(calls, ["/api/v1/assets/asset-9/device", "/api/v1/assets/asset-9/device-text"]);
});

test("local generated TXT overrides only its matching VFS filename after type9 conversion", async () => {
  const sourceBytes = new Uint8Array(577 * 1024).fill(9);
  const sourceHash = digest(sourceBytes);
  const bankFirst = new TextEncoder().encode("bank first\n");
  const bankSecond = new TextEncoder().encode("bank second\n");
  const localBytes = new TextEncoder().encode("local replacement\n");
  const device = loadDeviceFiles({
    "./api": { API_BASE: "/api/v1" },
    "./local-fetch": {
      localFetch: async (path) => {
        if (path.endsWith("/device")) return new Response(sourceBytes, { status: 200, headers: { "x-content-hash": sourceHash, "content-type": "application/vnd.oasis.opendocument.spreadsheet" } });
        return new Response(JSON.stringify({ source_sha256: sourceHash, files: [
          { filename: "9_ods.txt", sheet_name: "First", converter_version: "type9-tsv-v3", content_base64: Buffer.from(bankFirst).toString("base64"), content_hash: digest(bankFirst) },
          { filename: "9_ods_sheet-2.txt", sheet_name: "Second", converter_version: "type9-tsv-v3", content_base64: Buffer.from(bankSecond).toString("base64"), content_hash: digest(bankSecond) },
        ] }), { status: 200, headers: { "content-type": "application/json" } });
      },
    },
  });
  const local = { name: "9_ods.txt", bytes: localBytes, sha256: digest(localBytes), mediaType: "text/plain;charset=utf-8", origin: "local" };
  const files = await device.loadTaskDeviceFiles([{ ...taskAsset(), content_hash: sourceHash }], 9, "revision-9", undefined, [local]);
  assert.deepEqual(files.map((file) => file.name), ["9_ods.txt", "9_ods_sheet-2.txt"]);
  assert.equal(files[0], local);
  assert.deepEqual([...files[1].bytes], [...bankSecond]);
});

test("asset errors expose allowlisted integrity advice without raw backend JSON", async () => {
  const device = loadDeviceFiles({
    "./api": { API_BASE: "" },
    "./local-fetch": {
      localFetch: async () => new Response(JSON.stringify({ code: "ASSET_HASH_MISMATCH", secret: "must-not-leak" }), {
        status: 503,
        headers: { "content-type": "application/json" },
      }),
    },
  });
  await assert.rejects(
    device.loadDeviceAsset(taskAsset(), "revision-9"),
    (error) => error instanceof Error && /целостность/.test(error.message) && !error.message.includes("secret") && !error.message.includes("ASSET_HASH_MISMATCH"),
  );
});

test("spreadsheet column-limit responses explain the TXT fallback without exposing JSON", async () => {
  const device = loadDeviceFiles({
    "./api": { API_BASE: "" },
    "./local-fetch": {
      localFetch: async () => new Response(JSON.stringify({ detail: { code: "column_limit", secret: "must-not-leak" } }), {
        status: 503,
        headers: { "content-type": "application/json" },
      }),
    },
  });
  await assert.rejects(
    device.loadDeviceAsset(taskAsset(), "revision-9"),
    (error) => error instanceof Error && /Таблица слишком широкая/.test(error.message) && !error.message.includes("secret") && !error.message.includes("column_limit"),
  );
});

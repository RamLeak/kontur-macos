import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync, existsSync} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {preparePyodide} from '../scripts/prepare-pyodide.mjs';

const names = ['pyodide.mjs', 'pyodide.asm.mjs', 'pyodide.asm.wasm', 'python_stdlib.zip', 'pyodide-lock.json'];
const hash = value => createHash('sha256').update(value).digest('hex');
function fixture() {
  const front = mkdtempSync(path.join(os.tmpdir(), 'kontur-mac-helper-'));
  mkdirSync(path.join(front, 'node_modules/pyodide'), {recursive: true});
  const receipt = names.map((name, index) => {
    const data = Buffer.from('synthetic fixture ' + index);
    writeFileSync(path.join(front, 'node_modules/pyodide', name), data);
    return {path: 'public/python-runtime/' + name, bytes: data.length, sha256: hash(data)};
  });
  writeFileSync(path.join(front, 'PYODIDE_ASSETS.json'), JSON.stringify(receipt));
  return {front, receipt, output: path.join(front, 'public/python-runtime')};
}

function cleanup(front) {
  const target = path.resolve(front);
  assert.equal(path.dirname(target), path.resolve(os.tmpdir()));
  assert.match(path.basename(target), /^kontur-mac-helper-[A-Za-z0-9]+$/);
  rmSync(target, {recursive: true, force: true});
}

test('five pinned bytes copied, rerun preserves files', () => {
  const f = fixture();
  try {
    assert.equal(preparePyodide(f.front).files, 5);
    const original = names.map(name => readFileSync(path.join(f.output, name)));
    assert.deepEqual(preparePyodide(f.front), {ok: true, files: 5, bytes: 95, networkUsed: false});
    names.forEach((name, i) => assert.deepEqual(readFileSync(path.join(f.output, name)), original[i]));
  } finally { cleanup(f.front); }
});

test('same-size wrong source hash rejected before creating output', () => {
  const f = fixture();
  try {
    writeFileSync(path.join(f.front, 'node_modules/pyodide', names[4]), Buffer.from('synthetic fixture x'));
    assert.throws(() => preparePyodide(f.front), /SHA-256 mismatch/);
    assert.equal(existsSync(f.output), false);
  } finally { cleanup(f.front); }
});

test('unrecognized and duplicate receipt paths rejected', () => {
  const f = fixture();
  try {
    for (const badPath of ['../escape', f.receipt[0].path]) {
      const bad = structuredClone(f.receipt);
      bad[4].path = badPath;
      writeFileSync(path.join(f.front, 'PYODIDE_ASSETS.json'), JSON.stringify(bad));
      assert.throws(() => preparePyodide(f.front), /Invalid Pyodide receipt/);
      assert.equal(existsSync(f.output), false);
    }
  } finally { cleanup(f.front); }
});

test('existing changed bytes and unknown files are retained and rejected', () => {
  const f = fixture();
  try {
    preparePyodide(f.front);
    const target = path.join(f.output, names[0]);
    writeFileSync(target, 'owner data');
    assert.throws(() => preparePyodide(f.front), /mismatch/);
    assert.equal(readFileSync(target, 'utf8'), 'owner data');
    writeFileSync(target, readFileSync(path.join(f.front, 'node_modules/pyodide', names[0])));
    writeFileSync(path.join(f.output, 'unknown'), 'preserve me');
    assert.throws(() => preparePyodide(f.front), /Unexpected existing/);
    assert.equal(readFileSync(path.join(f.output, 'unknown'), 'utf8'), 'preserve me');
  } finally { cleanup(f.front); }
});

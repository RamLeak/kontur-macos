/** Copy only the five pinned local npm assets. This helper never uses the network. */
import {constants, copyFileSync, existsSync, lstatSync, mkdirSync, readFileSync, readdirSync} from 'node:fs';
import {createHash} from 'node:crypto';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const names = ['pyodide.mjs', 'pyodide.asm.mjs', 'pyodide.asm.wasm', 'python_stdlib.zip', 'pyodide-lock.json'];
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');

export function preparePyodide(front) {
  const source = path.join(front, 'node_modules', 'pyodide');
  const destination = path.join(front, 'public', 'python-runtime');
  const receipt = JSON.parse(readFileSync(path.join(front, 'PYODIDE_ASSETS.json'), 'utf8'));
  for (const directory of [front, path.join(front, 'node_modules'), path.join(front, 'public')]) {
    if (existsSync(directory) && (!lstatSync(directory).isDirectory() || lstatSync(directory).isSymbolicLink())) {
      throw Error('Expected ordinary Pyodide parent directory');
    }
  }
  if (!Array.isArray(receipt) || receipt.length !== names.length) throw Error('Unexpected Pyodide receipt');
  const pinned = new Map();
  for (const entry of receipt) {
    const name = entry.path?.replace(/^public\/python-runtime\//, '');
    if (!names.includes(name) || pinned.has(name) || !Number.isSafeInteger(entry.bytes)
      || entry.bytes <= 0 || !/^[a-f0-9]{64}$/.test(entry.sha256)) throw Error('Invalid Pyodide receipt');
    pinned.set(name, entry);
  }
  if (!existsSync(source) || !lstatSync(source).isDirectory() || lstatSync(source).isSymbolicLink()) {
    throw Error('Missing ordinary node_modules/pyodide directory; run npm ci in full/frontend first');
  }
  const verify = (file, name) => {
    const info = lstatSync(file);
    const pin = pinned.get(name);
    if (!info.isFile() || info.isSymbolicLink() || info.size !== pin.bytes) throw Error('Pyodide size/type mismatch: ' + name);
    if (sha256(readFileSync(file)) !== pin.sha256) throw Error('Pyodide SHA-256 mismatch: ' + name);
  };
  // Validate every source before creating or copying output.
  for (const name of names) verify(path.join(source, name), name);
  if (existsSync(destination)) {
    if (!lstatSync(destination).isDirectory() || lstatSync(destination).isSymbolicLink()) throw Error('Unsafe Pyodide destination');
    for (const name of readdirSync(destination)) {
      if (!names.includes(name)) throw Error('Unexpected existing Pyodide resource: ' + name);
      verify(path.join(destination, name), name);
    }
  } else {
    mkdirSync(destination, {recursive: true});
  }
  for (const name of names) {
    const target = path.join(destination, name);
    if (!existsSync(target)) copyFileSync(path.join(source, name), target, constants.COPYFILE_EXCL);
    verify(target, name);
  }
  return {ok: true, files: names.length, bytes: receipt.reduce((total, entry) => total + entry.bytes, 0), networkUsed: false};
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
    console.log(JSON.stringify(preparePyodide(path.join(root, 'full', 'frontend'))));
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}

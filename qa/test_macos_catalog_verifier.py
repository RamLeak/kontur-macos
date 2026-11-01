"""Stdlib-only checks of the read-only helper, using small synthetic inputs."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("catalog_verifier", ROOT / "scripts/verify-catalog.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


class CatalogVerifierTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "artifacts"
        scratch.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="kontur-catalog-helper-", dir=scratch)
        self.root = Path(self.temporary.name).resolve()
        self.root.relative_to(scratch.resolve())
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(setattr, verifier, "ROOT", verifier.ROOT)
        verifier.ROOT = self.root
        self.directory = self.root / "catalog"
        self.directory.mkdir()
        (self.root / "assets").mkdir()
        entries = []
        for index, name in enumerate(sorted(verifier.ALLOWED_NAMES)):
            data = ("synthetic-input-" + str(index)).encode()
            (self.directory / name).write_bytes(data)
            entries.append({"name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        self.reference = self.root / "assets/catalog-seed-reference.json"
        self.reference.write_text(json.dumps({"files": entries, "offlineReady": True, "complete": False}), encoding="utf-8")

    def test_valid_bytes_remain_unchanged(self):
        before = {file.name: file.read_bytes() for file in self.directory.iterdir()}
        code, result = verifier.verify(self.directory)
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertFalse(result["complete"])
        self.assertFalse(result["filesModified"])
        self.assertEqual(before, {file.name: file.read_bytes() for file in self.directory.iterdir()})

    def test_missing_input_is_distinct(self):
        (self.directory / "catalog.sqlite3").unlink()
        code, result = verifier.verify(self.directory)
        self.assertEqual(code, 2)
        self.assertEqual(result["missing"], ["catalog.sqlite3"])

    def test_wrong_size_is_rejected_without_mutation(self):
        file = self.directory / "catalog.sqlite3"
        file.write_bytes(b"owner input")
        self.assertEqual(verifier.verify(self.directory)[0], 1)
        self.assertEqual(file.read_bytes(), b"owner input")

    def test_same_size_changed_hash_is_rejected(self):
        file = self.directory / "catalog.sqlite3"
        file.write_bytes(b"x" * file.stat().st_size)
        code, result = verifier.verify(self.directory)
        self.assertEqual(code, 1)
        self.assertEqual(result["error"], "sha256_mismatch")

    def test_reference_cannot_introduce_other_paths(self):
        data = json.loads(self.reference.read_text(encoding="utf-8"))
        data["files"][0]["name"] = "../outside"
        self.reference.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            verifier.verify(self.directory)


if __name__ == "__main__":
    unittest.main()

"""Read-only, streaming verification of the separately supplied fixed catalog."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_NAMES = {"catalog.sqlite3", "recovery-provenance.json"}


def verify(directory: Path) -> tuple[int, dict]:
    reference = json.loads((ROOT / "assets/catalog-seed-reference.json").read_text(encoding="utf-8"))
    entries = reference["files"]
    if len(entries) != 2 or {entry["name"] for entry in entries} != ALLOWED_NAMES:
        raise ValueError("Unexpected catalog reference")
    missing = [entry["name"] for entry in entries if not (directory / entry["name"]).exists()]
    if missing:
        return 2, {"ok": False, "missing": missing, "networkUsed": False, "filesModified": False}
    checked = []
    for entry in entries:
        file = directory / entry["name"]
        if file.is_symlink() or not file.is_file() or file.stat().st_size != entry["bytes"]:
            return 1, {"ok": False, "error": "size_or_type_mismatch", "file": entry["name"]}
        checksum = hashlib.sha256()
        with file.open("rb") as source:
            while block := source.read(1024 * 1024):
                checksum.update(block)
        if checksum.hexdigest() != entry["sha256"]:
            return 1, {"ok": False, "error": "sha256_mismatch", "file": entry["name"]}
        checked.append({"file": entry["name"], "bytes": entry["bytes"], "sha256": entry["sha256"]})
    return 0, {"ok": True, "files": checked, "offlineReady": reference["offlineReady"],
               "complete": reference["complete"], "networkUsed": False, "filesModified": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-dir", type=Path, default=ROOT / "resources/catalog")
    args = parser.parse_args()
    try:
        code, result = verify(args.catalog_dir)
    except (OSError, ValueError, KeyError, TypeError):
        code, result = 1, {"ok": False, "error": "catalog_reference_or_read_error"}
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())

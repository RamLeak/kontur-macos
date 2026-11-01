"""Prepare only the legacy transfer test input; never a runtime catalog."""
import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
write_bundle = importlib.import_module("exporter.kontur_transfer").write_bundle
fixture_bundle = importlib.import_module("qa.exporter_transfer").fixture_bundle


def main():
    destination = ROOT / "qa" / "exporter_fixture.kontur"
    if destination.exists():
        print(json.dumps({"syntheticNativeFixture": "existing", "validatedBy": "Cargo data_repository tests"}))
        return 0
    result = write_bundle(destination, fixture_bundle(), created_at=1234)
    counts = (result.task_count, result.attempt_count, result.document_count, result.asset_count)
    if counts != (2, 4, 1, 1):
        raise RuntimeError("Unexpected synthetic fixture counts")
    print(json.dumps({"syntheticNativeFixture": "created", "counts": counts, "sha256": result.content_sha256}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

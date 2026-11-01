"""An explicit large run keeps the same reservation and default bounds."""
import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
downloader = importlib.import_module("download_assets")


class ExtendedBudgetTests(unittest.TestCase):
    def test_large_explicit_ceiling_keeps_additional_network_spend_bounded(self):
        # Actual first-run existing bytes plus the owner's additional 4 GiB.
        existing, additional = 698_479_487, 4 * 1024**3
        budget = downloader._ByteBudget(existing + additional)
        budget.admit_existing("synthetic/existing", existing)
        while budget.used < budget.limit:
            allowance = budget.reserve()
            self.assertLessEqual(allowance, 64_000_000)
            budget.settle(allowance, allowance)
        self.assertEqual(budget.network_bytes, additional)
        with self.assertRaises(downloader.SnapshotError) as error:
            budget.reserve()
        self.assertEqual(error.exception.code, "total_asset_budget_exceeded")

    def test_cli_default_is_unchanged_and_invalid_explicit_limits_fail(self):
        args = downloader._parser().parse_args(["--seed", "synthetic.sqlite3", "--output", "synthetic"])
        self.assertEqual(args.max_total_bytes, 2_147_483_648)
        for invalid in (True, 0, -1, 8_589_934_593):
            with self.subTest(limit=invalid), self.assertRaises(downloader.SnapshotError) as error:
                downloader._ByteBudget(invalid)
            self.assertEqual(error.exception.code, "total_asset_budget_invalid")


if __name__ == "__main__":
    unittest.main()

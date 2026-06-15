#!/usr/bin/env python3

import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


class DerivedEarningsHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_pool_db_json = pool_ops.pool_db_json
        self.addCleanup(self.restore)

    def restore(self) -> None:
        pool_ops.pool_db_json = self.original_pool_db_json

    def test_reconstructs_plot_history_from_pool_credit_rows(self) -> None:
        worker = "0x943900000000000000000000000000000000ebe0"

        def fake_pool_db_json(_sql: str):
            return [
                {
                    "bucket_at": "2026-05-28 17:00:00+00",
                    "miner_address": worker,
                    "credit_count": 2,
                    "total_wei": str(2 * 10**18),
                    "cumulative_total_wei": str(5 * 10**18),
                }
            ]

        pool_ops.pool_db_json = fake_pool_db_json
        history = pool_ops.derived_credit_history_for_dashboard(
            {"usd": "1", "zar": "2"},
            [
                {
                    "ip": "192.168.49.178",
                    "mac": "28:e2:97:4c:e4:0a",
                    "workers": [worker],
                    "hashrate_ghs": 850.0,
                    "hashrate_source": "asic",
                }
            ],
        )

        self.assertEqual(len(history), 1)
        snapshot = history[0]
        self.assertEqual(snapshot["history_source"], "pool-db-derived-credits")
        self.assertEqual(snapshot["total_bdag"], "5.00")
        self.assertEqual(snapshot["miner_estimates"][0]["workers"], [worker])
        self.assertEqual(snapshot["miner_estimates"][0]["blocks_found"], 2)
        self.assertEqual(snapshot["miner_estimates"][0]["hashrate_source"], "asic")


if __name__ == "__main__":
    unittest.main()

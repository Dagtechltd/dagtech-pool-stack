#!/usr/bin/env python3

import pathlib
import sys
import unittest
from decimal import Decimal

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


class DashboardRpcHistoryRebuildTests(unittest.TestCase):
    def test_reconstructed_global_snapshot_is_valid_history_contract(self) -> None:
        wallet = "0x05518e03e148c56e426ff9e1cbdb962b4fc5250a"
        headers = [
            {
                "order": 100,
                "miner": wallet,
                "timestamp_epoch": 1_781_000_000,
                "reward_bdag": Decimal("247.5"),
                "_rpc_source": "local-chain",
            },
            {
                "order": 101,
                "miner": "0x94390581d27ac0faf4792984068e9a4366e3ebe0",
                "timestamp_epoch": 1_781_000_030,
                "reward_bdag": Decimal("247.5"),
                "_rpc_source": "local-chain",
            },
        ]

        snapshot = pool_ops.global_history_snapshot_from_chain_headers(
            headers,
            sample_epoch=1_781_000_060,
            sample_order=101,
            rpc_name="local-chain",
            price={"usd": "1", "zar": "20"},
            requested_blocks=2,
        )

        self.assertTrue(pool_ops.is_valid_global_chain_snapshot(snapshot))
        self.assertEqual(snapshot["source_contract"], pool_ops.DASHBOARD_CHAIN_HISTORY_SOURCE_CONTRACT)
        self.assertEqual(snapshot["height_method"], "getBlockByOrder-reconstructed")
        self.assertEqual(snapshot["latest_block"], 102)
        self.assertEqual(snapshot["requested_blocks"], 2)
        self.assertEqual(snapshot["fetched_blocks"], 2)
        self.assertEqual(len(snapshot["clusters"]), 2)

    def test_payment_wallet_rebuild_snapshot_survives_earnings_compaction(self) -> None:
        wallet = "0x05518e03e148c56e426ff9e1cbdb962b4fc5250a"
        headers = [
            {
                "order": 200,
                "miner": wallet,
                "timestamp_epoch": 1_781_000_000,
                "reward_bdag": Decimal("250"),
            },
            {
                "order": 201,
                "miner": "0x94390581d27ac0faf4792984068e9a4366e3ebe0",
                "timestamp_epoch": 1_781_000_060,
                "reward_bdag": Decimal("250"),
            },
        ]

        snapshot = pool_ops.payment_wallet_earnings_snapshot_from_chain_headers(
            headers,
            sample_epoch=1_781_000_060,
            wallet_address=wallet,
            price={"usd": "1", "zar": "20"},
            requested_blocks=2,
        )

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertTrue(pool_ops.earnings_snapshot_has_plot_data(snapshot))
        compacted = pool_ops.compact_earnings_snapshot(snapshot)
        miner = compacted["miner_estimates"][0]
        self.assertEqual(miner["identity_key"], f"wallet:{wallet}")
        self.assertEqual(miner["earnings_scope"], "payment-wallet-chain-rewards")
        self.assertEqual(miner["blocks_found"], 1)
        self.assertEqual(miner["estimated_wallet_bdag_avg_hour"], "15000.00")
        self.assertIsNone(compacted["credit_balance_check"]["wallet_bdag"])

    def test_runtime_derived_history_is_opt_in(self) -> None:
        self.assertFalse(pool_ops.EARNINGS_DERIVED_HISTORY_RUNTIME_FALLBACK_ENABLED)


if __name__ == "__main__":
    unittest.main()

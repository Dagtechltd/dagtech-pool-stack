#!/usr/bin/env python3

import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


class EarningsMinerScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            name: getattr(pool_ops, name)
            for name in (
                "collect_miner_hashrate_debug",
                "collect_pool_activity",
                "upsert_pool_activity_miners",
            )
        }
        self.addCleanup(self.restore)

    def restore(self) -> None:
        for name, value in self.originals.items():
            setattr(pool_ops, name, value)

    def test_earnings_wallet_scope_only_uses_configured_current_miners(self) -> None:
        configured_worker = "0x94390581D27ac0fAf4792984068E9a4366E3EBE0"
        rogue_worker = "0xd72C6af6bbD4929A33a0cFcAF5F6d3ec98D85556"
        hashrate_probe_activity: list[dict[str, object]] = []

        pool_ops.collect_pool_activity = lambda lines=0: {
            "miners": [
                {
                    "ip": "192.168.49.178",
                    "identity_key": "mac:28:e2:97:4c:e4:0a",
                    "workers": [configured_worker],
                    "shares": 10,
                    "share_work": 100,
                    "blocks_found": 5,
                    "last_share_at": "now",
                },
                {
                    "ip": "192.168.49.179",
                    "identity_key": "mac:28:e2:97:3d:95:13",
                    "workers": [rogue_worker],
                    "shares": 10,
                    "share_work": 100,
                    "blocks_found": 5,
                    "last_share_at": "now",
                },
            ]
        }
        pool_ops.upsert_pool_activity_miners = lambda _activity: {
            "miners": [
                {
                    "ip": "192.168.49.178",
                    "mac": "28:e2:97:4c:e4:0a",
                    "device_type": "asic",
                    "display_name": "Achilles",
                    "managed": True,
                    "last_configured_ok": True,
                    "last_workers": [configured_worker],
                },
                {
                    "ip": "192.168.49.179",
                    "mac": "28:e2:97:3d:95:13",
                    "device_type": "asic",
                    "managed": False,
                    "last_configured_ok": False,
                    "last_workers": [rogue_worker],
                },
            ]
        }

        def fake_hashrate(_registry_miners, activity_miners):
            hashrate_probe_activity.extend(activity_miners)
            return {}

        pool_ops.collect_miner_hashrate_debug = fake_hashrate
        credits = {
            "totals": {"total_wei": str(1000 * pool_ops.WEI_PER_BDAG)},
            "recent_1h": {"total_wei": str(100 * pool_ops.WEI_PER_BDAG)},
            "by_address": [
                {
                    "miner_address": configured_worker,
                    "total_bdag": "600",
                    "pending_bdag": "600",
                    "paid_bdag": "0",
                    "credit_count": 6,
                    "last_credit_at": "now",
                },
                {
                    "miner_address": rogue_worker,
                    "total_bdag": "400",
                    "pending_bdag": "400",
                    "paid_bdag": "0",
                    "credit_count": 4,
                    "last_credit_at": "now",
                },
            ],
        }

        rows = pool_ops.collect_miner_earnings_estimates(credits, {"status": "failed"})

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ip"], "192.168.49.178")
        self.assertEqual(rows[0]["display_label"], "Achilles-40a")
        self.assertTrue(rows[0]["managed"])
        self.assertTrue(rows[0]["configured"])
        self.assertTrue(rows[0]["connected"])
        self.assertEqual(rows[0]["earnings_scope"], "configured-current-miners")
        self.assertEqual(rows[0]["work_percent"], "100.00")
        self.assertEqual(rows[0]["estimated_bdag_total"], "1000.00")
        self.assertEqual(rows[0]["credit_workers"], [configured_worker])
        self.assertEqual([item["ip"] for item in hashrate_probe_activity], ["192.168.49.178"])

    def test_idle_discovered_asics_are_not_earnings_wallet_miners(self) -> None:
        self.assertFalse(
            pool_ops.is_earnings_wallet_miner(
                {
                    "ip": "192.168.49.179",
                    "device_type": "asic",
                    "credit_scope": "idle-registered-asic",
                    "shares": 0,
                    "credited_blocks": 0,
                }
            )
        )


if __name__ == "__main__":
    unittest.main()

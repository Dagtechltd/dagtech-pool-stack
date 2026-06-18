#!/usr/bin/env python3

import json
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timezone

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


def iso_at(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")


def earnings_snapshot(epoch: int, miner_id: str = "aa:bb:cc:dd:ee:ff") -> dict[str, object]:
    return {
        "generated_at": iso_at(epoch),
        "total_bdag": "1",
        "credit_balance_check": {"wallet_bdag": "1"},
        "miner_estimates": [
            {
                "managed": True,
                "mac": miner_id,
                "shares": 1,
                "estimated_bdag_avg_hour": "1",
                "estimated_bdag_1h": "1",
            }
        ],
    }


class DashboardTieredHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.old_env = os.environ.copy()
        self.old_disk_dir = pool_ops.DASHBOARD_HISTORY_DISK_DIR
        self.addCleanup(self.restore_globals)

        os.environ["BDAG_DASHBOARD_HISTORY_RAM_DIR"] = str(self.root / "ram")
        pool_ops.DASHBOARD_HISTORY_DISK_DIR = self.root / "disk"

    def restore_globals(self) -> None:
        os.environ.clear()
        os.environ.update(self.old_env)
        pool_ops.DASHBOARD_HISTORY_DISK_DIR = self.old_disk_dir

    def test_rebuild_splits_dashboard_history_into_ram_and_disk_tiers(self) -> None:
        latest = 1_781_000_000
        epochs = []
        epochs.extend(latest - minute * 60 for minute in range(0, 75))
        epochs.extend(latest - minute * 300 for minute in range(13, 300))
        epochs.extend(latest - minute * 900 for minute in range(97, 300))
        epochs.extend(latest - minute * 1800 for minute in range(145, 400))
        epochs.extend(latest - minute * 7200 for minute in range(85, 380))
        snapshots = [earnings_snapshot(epoch) for epoch in sorted(set(epochs))]
        source = self.root / "earnings-snapshots.jsonl"
        source.write_text("\n".join(json.dumps(snapshot) for snapshot in snapshots) + "\n", encoding="utf-8")

        history, sample_count = pool_ops.read_dashboard_history(
            "earnings",
            source,
            pool_ops.compact_earnings_snapshot,
            pool_ops.earnings_snapshot_has_plot_data,
        )

        self.assertEqual(sample_count, len(snapshots))
        self.assertTrue((self.root / "ram" / "earnings" / "minute.json").exists())
        self.assertTrue((self.root / "disk" / "earnings" / "five_minute.json").exists())
        self.assertTrue((self.root / "disk" / "earnings" / "fifteen_minute.json").exists())
        self.assertTrue((self.root / "disk" / "earnings" / "thirty_minute.json").exists())
        self.assertTrue((self.root / "disk" / "earnings" / "two_hour.json").exists())
        self.assertLessEqual(len(history), 61 + 277 + 193 + 193 + 289)

        generated = [row["generated_at"] for row in history]
        self.assertEqual(len(generated), len(set(generated)))
        tier_payload = json.loads((self.root / "ram" / "earnings" / "minute.json").read_text(encoding="utf-8"))
        minute_epochs = [pool_ops.history_snapshot_epoch(row) for row in tier_payload["rows"]]
        self.assertTrue(minute_epochs)
        self.assertTrue(all(latest - epoch <= 3600 for epoch in minute_epochs if epoch is not None))

    def test_update_promotes_old_hot_sample_to_five_minute_disk_tier(self) -> None:
        latest = 1_781_000_000
        old = earnings_snapshot(latest - 70 * 60, miner_id="00:11:22:33:44:55")
        new = earnings_snapshot(latest, miner_id="66:77:88:99:aa:bb")
        source = self.root / "missing-source.jsonl"

        pool_ops.update_dashboard_history_with_snapshot(
            "earnings",
            source,
            old,
            pool_ops.compact_earnings_snapshot,
            pool_ops.earnings_snapshot_has_plot_data,
        )
        pool_ops.update_dashboard_history_with_snapshot(
            "earnings",
            source,
            new,
            pool_ops.compact_earnings_snapshot,
            pool_ops.earnings_snapshot_has_plot_data,
        )

        minute_payload = json.loads((self.root / "ram" / "earnings" / "minute.json").read_text(encoding="utf-8"))
        hour_payload = json.loads((self.root / "disk" / "earnings" / "five_minute.json").read_text(encoding="utf-8"))
        self.assertEqual([row["generated_at"] for row in minute_payload["rows"]], [new["generated_at"]])
        self.assertEqual([row["generated_at"] for row in hour_payload["rows"]], [old["generated_at"]])

    def test_history_tiers_do_not_exceed_frontend_gap_thresholds(self) -> None:
        tiers = {tier.name: tier for tier in pool_ops.dashboard_history_tiers()}

        self.assertEqual(tiers["minute"].step_seconds, 60)
        self.assertEqual(tiers["five_minute"].step_seconds, 300)
        self.assertEqual(tiers["fifteen_minute"].step_seconds, 900)
        self.assertEqual(tiers["thirty_minute"].step_seconds, 1800)
        self.assertEqual(tiers["two_hour"].step_seconds, 7200)

        self.assertLessEqual(tiers["five_minute"].step_seconds, 15 * 60)
        self.assertLessEqual(tiers["five_minute"].step_seconds, 30 * 60)
        self.assertLessEqual(tiers["five_minute"].step_seconds, 60 * 60)
        self.assertLessEqual(tiers["fifteen_minute"].step_seconds, 3 * 3600)
        self.assertLessEqual(tiers["thirty_minute"].step_seconds, 8 * 3600)
        self.assertLessEqual(tiers["two_hour"].step_seconds, 36 * 3600)

    def test_rebuild_sample_epochs_cover_dashboard_ranges_without_internal_gaps(self) -> None:
        latest = 1_781_000_000
        epochs = pool_ops.dashboard_history_rebuild_sample_epochs(latest, max_age_seconds=720 * 3600)
        self.assertIn(latest, epochs)
        self.assertGreater(len(epochs), 900)
        gaps = [right - left for left, right in zip(epochs, epochs[1:])]
        self.assertLessEqual(max(gaps), 2 * 3600)


if __name__ == "__main__":
    unittest.main()

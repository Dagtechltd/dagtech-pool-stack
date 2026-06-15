#!/usr/bin/env python3

import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import status_sampler  # noqa: E402


class StatusSamplerEarningsSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            name: getattr(status_sampler, name)
            for name in (
                "read_latest_earnings_snapshot_info",
                "record_earnings_snapshot",
                "log",
            )
        }
        self.addCleanup(self.restore)

    def restore(self) -> None:
        for name, value in self.originals.items():
            setattr(status_sampler, name, value)

    def test_records_when_valid_plot_history_is_stale_even_if_invalid_snapshot_is_recent(self) -> None:
        calls = []
        status_sampler.read_latest_earnings_snapshot_info = lambda: {
            "latest_epoch": 100.0,
            "latest_any_epoch": 995.0,
        }
        status_sampler.record_earnings_snapshot = lambda: calls.append("recorded") or {
            "generated_at": "2026-05-27T00:00:00+0200",
            "miner_estimates": [{"ip": "192.168.1.107"}],
        }
        status_sampler.log = lambda _message: None

        last_attempt = status_sampler.maybe_record_earnings_snapshot(
            now_epoch=1000.0,
            last_attempt_epoch=0.0,
            interval_seconds=120.0,
            enabled=True,
        )

        self.assertEqual(calls, ["recorded"])
        self.assertEqual(last_attempt, 1000.0)

    def test_recent_attempt_throttles_repeated_invalid_snapshot_writes(self) -> None:
        calls = []
        status_sampler.read_latest_earnings_snapshot_info = lambda: {
            "latest_epoch": None,
            "latest_any_epoch": 995.0,
        }
        status_sampler.record_earnings_snapshot = lambda: calls.append("recorded") or {
            "generated_at": "2026-05-27T00:00:00+0200",
            "miner_estimates": [],
        }
        status_sampler.log = lambda _message: None

        last_attempt = status_sampler.maybe_record_earnings_snapshot(
            now_epoch=1000.0,
            last_attempt_epoch=950.0,
            interval_seconds=120.0,
            enabled=True,
        )

        self.assertEqual(calls, [])
        self.assertEqual(last_attempt, 950.0)


if __name__ == "__main__":
    unittest.main()

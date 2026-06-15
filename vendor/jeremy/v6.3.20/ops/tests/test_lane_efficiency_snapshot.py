#!/usr/bin/env python3

import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import lane_efficiency_snapshot as lane_efficiency  # noqa: E402


class LaneEfficiencySnapshotTests(unittest.TestCase):
    def test_live_window_seconds_enforces_five_minute_positive_floor(self) -> None:
        self.assertEqual(0.0, lane_efficiency.live_window_seconds(0.0))
        self.assertEqual(300.0, lane_efficiency.live_window_seconds(120.0))
        self.assertEqual(1800.0, lane_efficiency.live_window_seconds(1800.0))

    def test_parse_prometheus_keeps_pool_counters(self) -> None:
        metrics = lane_efficiency.parse_prometheus(
            """
# HELP ignored ignored
pool_block_submit_outcomes_total{outcome="accepted",pool_id="0",reason="ok"} 10
process_cpu_seconds_total 99
pool_template_conversion_stall_active_miners{pool_id="0"} 3
"""
        )

        self.assertEqual(10.0, metrics['pool_block_submit_outcomes_total{outcome="accepted",pool_id="0",reason="ok"}'])
        self.assertEqual(3.0, metrics['pool_template_conversion_stall_active_miners{pool_id="0"}'])
        self.assertNotIn("process_cpu_seconds_total", metrics)

    def test_summarize_delta_is_miner_hour_normalized(self) -> None:
        first = lane_efficiency.parse_prometheus(
            """
pool_block_submit_outcomes_total{outcome="accepted",pool_id="0",reason="ok"} 100
pool_block_submit_outcomes_total{outcome="rejected-local",pool_id="0",reason="duplicate-block"} 5
pool_shares_accepted_total{pool_id="0"} 200
pool_shares_rejected_total{pool_id="0",reason="non_current_job"} 10
pool_template_conversion_stall_active_miners{pool_id="0"} 3
"""
        )
        last = lane_efficiency.parse_prometheus(
            """
pool_block_submit_outcomes_total{outcome="accepted",pool_id="0",reason="ok"} 130
pool_block_submit_outcomes_total{outcome="rejected-local",pool_id="0",reason="duplicate-block"} 8
pool_shares_accepted_total{pool_id="0"} 260
pool_shares_rejected_total{pool_id="0",reason="non_current_job"} 16
pool_template_conversion_stall_active_miners{pool_id="0"} 3
pool_template_conversion_stall_failure_ratio{pool_id="0"} 4.5
"""
        )

        summary = lane_efficiency.summarize_delta(first, last, 120.0)

        self.assertEqual(30.0, summary["accepted_blocks"])
        self.assertEqual(300.0, summary["accepted_blocks_per_miner_hour"])
        self.assertEqual(0.1, summary["rejected_local_per_accepted"])
        self.assertEqual(0.090909, summary["share_reject_ratio"])


if __name__ == "__main__":
    unittest.main()

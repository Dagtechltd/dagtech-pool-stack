#!/usr/bin/env python3

import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import paid_conversion_baseline as baseline  # noqa: E402


class PaidConversionBaselineTests(unittest.TestCase):
    def test_parse_prometheus_keeps_paid_conversion_metrics(self) -> None:
        metrics = baseline.parse_prometheus(
            """
pool_block_submit_outcomes_total{outcome="accepted",pool_id="0",reason="ok"} 10
pool_rpc_backend_node_health_submit_ready{backend="node",pool_id="0"} 0
process_cpu_seconds_total 99
"""
        )

        self.assertEqual(10.0, metrics['pool_block_submit_outcomes_total{outcome="accepted",pool_id="0",reason="ok"}'])
        self.assertEqual(0.0, metrics['pool_rpc_backend_node_health_submit_ready{backend="node",pool_id="0"}'])
        self.assertNotIn("process_cpu_seconds_total", metrics)

    def test_summarize_metrics_counts_local_drops_and_tip_overdue(self) -> None:
        first = baseline.parse_prometheus(
            """
pool_blocks_found_total{pool_id="0"} 100
pool_block_submit_outcomes_total{outcome="accepted",pool_id="0",reason="ok"} 80
pool_block_submit_outcomes_total{outcome="rejected",pool_id="0",reason="tip-overdue"} 10
pool_block_submit_outcomes_total{outcome="rejected-local",pool_id="0",reason="expired"} 20
pool_shares_accepted_total{pool_id="0"} 1000
pool_shares_rejected_total{pool_id="0",reason="invalidated_job"} 100
pool_template_conversion_stall_active_miners{pool_id="0"} 2
"""
        )
        last = baseline.parse_prometheus(
            """
pool_blocks_found_total{pool_id="0"} 110
pool_block_submit_outcomes_total{outcome="accepted",pool_id="0",reason="ok"} 85
pool_block_submit_outcomes_total{outcome="rejected",pool_id="0",reason="tip-overdue"} 13
pool_block_submit_outcomes_total{outcome="rejected-local",pool_id="0",reason="expired"} 22
pool_shares_accepted_total{pool_id="0"} 1100
pool_shares_rejected_total{pool_id="0",reason="invalidated_job"} 125
pool_template_conversion_stall_active_miners{pool_id="0"} 2
"""
        )

        summary = baseline.summarize_metrics(first, last, 1800.0)

        self.assertEqual(10.0, summary["network_target_candidates_delta"])
        self.assertEqual(5.0, summary["accepted_submit_delta"])
        self.assertEqual(5.0, summary["accepted_submit_per_miner_hour"])
        self.assertEqual(2.0, summary["local_candidate_drop_delta"])
        self.assertEqual(0.2, summary["local_candidate_drop_ratio"])
        self.assertEqual(3.0, summary["tip_overdue_delta"])
        self.assertEqual(0.2, summary["share_reject_ratio"])

    def test_paid_state_flags_fresh_shares_without_paid_submits(self) -> None:
        status = {"overall": "ok", "miner_health": {"connected_count": 1}, "sync_progress": {"status": "synced"}}
        summary = {
            "accepted_submit_delta": 0,
            "share_accept_delta": 10,
            "local_candidate_drop_ratio": 0.0,
            "selected_backend_state": {"node_mineable": 0.0, "node_submit_ready": 0.0},
        }

        state = baseline.derive_paid_mining_state(status, summary)

        self.assertEqual("template_source_unready", state["state"])
        self.assertTrue(any("not mineable" in reason for reason in state["reasons"]))

    def test_paid_state_distinguishes_no_miner_sync_only(self) -> None:
        status = {"overall": "ok", "miner_health": {"connected_count": 0}, "sync_progress": {"status": "syncing"}}
        state = baseline.derive_paid_mining_state(status, {"selected_backend_state": {}})
        self.assertEqual("sync_only_no_miners", state["state"])


if __name__ == "__main__":
    unittest.main()

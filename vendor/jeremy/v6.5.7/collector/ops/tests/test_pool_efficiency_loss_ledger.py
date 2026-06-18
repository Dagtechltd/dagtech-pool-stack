#!/usr/bin/env python3

from collections import Counter
import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


class PoolEfficiencyLossLedgerTests(unittest.TestCase):
    def test_loss_ledger_flags_block_share_and_template_waste(self) -> None:
        ledger = pool_ops.build_pool_efficiency_loss_ledger(
            block_submit_outcomes=Counter(
                {
                    "accepted:ok": 100,
                    "rejected:tip-overdue": 40,
                    "rejected-local:duplicate-block": 10,
                }
            ),
            shares_accepted_total=50,
            shares_rejected_by_reason=Counter({"invalidated_job": 70, "non_current_job": 20}),
            block_totals=Counter({"found": 150, "submitted": 150, "mature": 100}),
            blocks_rejected_by_node=Counter({"tip-overdue": 40}),
            share_processing={"count": 10, "sum_seconds": 3},
            template_conversion_stall={"active_miners": 5, "failure_ratio": 42.0},
        )

        self.assertEqual(ledger["severity"], "warning")
        self.assertEqual(ledger["block_outcomes"]["accepted_ratio_percent"], 66.67)
        self.assertEqual(ledger["share_outcomes"]["accepted_ratio_percent"], 35.71)
        self.assertTrue(any("template conversion loss" in item for item in ledger["warnings"]))
        self.assertEqual(ledger["top_loss_reasons"][0]["reason"], "invalidated_job")

    def test_loss_ledger_escalates_critical_template_conversion_loss(self) -> None:
        ledger = pool_ops.build_pool_efficiency_loss_ledger(
            block_submit_outcomes=Counter({"accepted:ok": 20, "rejected:tip-overdue": 10}),
            shares_accepted_total=100,
            shares_rejected_by_reason=Counter(),
            block_totals=Counter(),
            blocks_rejected_by_node=Counter(),
            template_conversion_stall={"active_miners": 5, "failure_ratio": 55.0},
        )

        self.assertEqual(ledger["severity"], "critical")

    def test_readiness_contract_distinguishes_contradiction_from_hard_unready(self) -> None:
        source_health = {"node_mineable": False, "node_submit_ready": False, "node_p2p_mining_fresh": True}
        job_health = {"ok": False}

        contradiction = pool_ops.selected_backend_readiness_contract("node1", source_health, job_health, True)
        hard_unready = pool_ops.selected_backend_readiness_contract("node1", source_health, job_health, False)

        self.assertTrue(contradiction["contradiction"])
        self.assertFalse(contradiction["hard_unready"])
        self.assertFalse(hard_unready["contradiction"])
        self.assertTrue(hard_unready["hard_unready"])


class PoolPrometheusMetricsParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_fetch = pool_ops.fetch_text_url
        self.old_pool_containers = pool_ops.POOL_CONTAINERS
        pool_ops.POOL_CONTAINERS = ["asic-pool"]
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        pool_ops.fetch_text_url = self.old_fetch
        pool_ops.POOL_CONTAINERS = self.old_pool_containers

    def test_pool_metrics_parse_loss_ledger_and_source_health_contract_inputs(self) -> None:
        metrics = """
pool_active_connections 5
pool_rpc_backend_selected{backend="node1",pool_id="0"} 1
pool_rpc_backend_healthy{backend="node1",pool_id="0"} 1
pool_rpc_backend_node_health_mineable{backend="node1",pool_id="0"} 0
pool_rpc_backend_node_health_submit_ready{backend="node1",pool_id="0"} 0
pool_job_health_ok{pool_id="0"} 0
pool_job_health_ready_miners{pool_id="0"} 5
pool_template_conversion_stall_active_miners{pool_id="0"} 5
pool_template_conversion_stall_failure_ratio{pool_id="0"} 55
pool_template_conversion_stall_window_candidates{kind="accepted",pool_id="0"} 2
pool_template_conversion_stall_window_candidates{kind="failed",pool_id="0"} 3
pool_block_submit_outcomes_total{outcome="accepted",pool_id="0",reason="ok"} 10
pool_block_submit_outcomes_total{outcome="rejected",pool_id="0",reason="tip-overdue"} 8
pool_block_submit_backend_outcomes_total{backend="node1",outcome="rejected",pool_id="0",reason="tip-overdue"} 8
pool_blocks_found_total{pool_id="0"} 18
pool_blocks_submitted_total{pool_id="0"} 18
pool_blocks_rejected_by_node_total{pool_id="0",reason="tip-overdue"} 8
pool_share_processing_duration_seconds_sum{pool_id="0"} 1.2
pool_share_processing_duration_seconds_count{pool_id="0"} 4
pool_shares_accepted_total{pool_id="0"} 5
pool_shares_rejected_total{pool_id="0",reason="invalidated_job"} 15
"""
        pool_ops.fetch_text_url = lambda *_args, **_kwargs: metrics

        payload = pool_ops.collect_pool_prometheus_metrics(
            {"asic-pool": {"running": True, "network_ips": ["10.0.0.2"]}}
        )

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["active_connections"], 5)
        self.assertEqual(payload["selected_backend"], "node1")
        self.assertFalse(payload["selected_backend_source_health"]["node_mineable"])
        self.assertEqual(payload["loss_ledger"]["severity"], "critical")
        self.assertEqual(payload["loss_ledger"]["share_outcomes"]["accepted_ratio_percent"], 25.0)


if __name__ == "__main__":
    unittest.main()

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

        contradiction = pool_ops.selected_backend_readiness_contract("node", source_health, job_health, True)
        hard_unready = pool_ops.selected_backend_readiness_contract("node", source_health, job_health, False)

        self.assertTrue(contradiction["contradiction"])
        self.assertFalse(contradiction["hard_unready"])
        self.assertFalse(hard_unready["contradiction"])
        self.assertTrue(hard_unready["hard_unready"])

    def test_selected_backend_unready_reasons_include_peer_freshness(self) -> None:
        reasons = pool_ops.selected_backend_unready_reasons(
            {
                "node_mineable": False,
                "node_submit_ready": False,
                "node_p2p_mining_fresh": False,
                "node_last_template_build_error_blocking": True,
            }
        )

        self.assertEqual(
            reasons,
            [
                "mineable=false",
                "submit_ready=false",
                "p2p_mining_fresh=false",
                "template_build_error_blocking=true",
            ],
        )

    def test_selected_backend_source_degradation_is_advisory_with_recent_paid_work(self) -> None:
        advisory = pool_ops.selected_backend_source_degradation(True, True)
        hard = pool_ops.selected_backend_source_degradation(True, False)

        self.assertTrue(advisory["degraded"])
        self.assertTrue(advisory["advisory"])
        self.assertFalse(advisory["hard"])
        self.assertTrue(hard["hard"])
        self.assertFalse(hard["advisory"])

    def test_catchup_policy_pauses_pool_above_threshold(self) -> None:
        policy = pool_ops.build_catchup_policy(
            {"status": "syncing", "remaining_blocks": 450},
            {"node": {"peer_ahead_blocks": 20}},
            {"pool": {"running": False}},
            {},
        )

        self.assertTrue(policy["active"])
        self.assertTrue(policy["pool_pause_active"])
        self.assertEqual(policy["threshold_blocks"], 300)
        self.assertIn("mining work is intentionally paused", policy["summary"])
        self.assertIn("Leave miners configured", policy["user_message"])
        self.assertEqual(policy["trigger"], "lag_threshold")

    def test_catchup_policy_uses_io_pressure_as_primary_trigger(self) -> None:
        policy = pool_ops.build_catchup_policy(
            {"status": "syncing", "remaining_blocks": 80},
            {"node": {"peer_ahead_blocks": 80}},
            {"pool": {"running": True}},
            {"node_mineable": False, "node_submit_ready": False},
            {"iowait_percent": 18.0, "io_some_avg10": 22.0, "io_full_avg10": 23.0},
            mining_ready=False,
        )

        self.assertTrue(policy["active"])
        self.assertEqual(policy["trigger"], "io_pressure")
        self.assertTrue(policy["io_pressure_active"])
        self.assertFalse(policy["lag_threshold_active"])
        self.assertEqual(policy["lag_blocks"], 80)
        self.assertIn("I/O-bound", policy["summary"])
        self.assertIn("I/O pressure drops", policy["next_step"])
        self.assertTrue(any("io_full_avg10" in reason for reason in policy["io_pressure_reasons"]))

    def test_catchup_policy_keeps_paid_mining_online_under_io_pressure(self) -> None:
        policy = pool_ops.build_catchup_policy(
            {"status": "syncing", "remaining_blocks": 80},
            {"node": {"peer_ahead_blocks": 80}},
            {"pool": {"running": True}},
            {"node_mineable": False, "node_submit_ready": False},
            {"iowait_percent": 18.0, "io_some_avg10": 22.0, "io_full_avg10": 23.0},
            mining_ready=False,
            pool_has_recent_paid_work=True,
        )

        self.assertFalse(policy["active"])
        self.assertEqual(policy["trigger"], "")
        self.assertTrue(policy["io_pressure_active"])
        self.assertTrue(policy["recent_paid_work_suppressed"])
        self.assertFalse(policy["pool_pause_recommended"])

    def test_catchup_policy_uses_backend_peer_lead_when_sync_claims_synced(self) -> None:
        policy = pool_ops.build_catchup_policy(
            {"status": "synced", "remaining_blocks": 0},
            {"node": {}},
            {"pool": {"running": True}},
            {
                "node_mineable": False,
                "node_submit_ready": False,
                "node_p2p_mining_fresh": True,
                "node_p2p_best_peer_lead_blocks": 80,
            },
            {"io_full_avg10": 23.0},
            mining_ready=False,
        )

        self.assertTrue(policy["active"])
        self.assertEqual(policy["trigger"], "io_pressure")
        self.assertEqual(policy["lag_blocks"], 80)

    def test_catchup_policy_does_not_pause_backend_unready_under_io_pressure_without_lag(self) -> None:
        policy = pool_ops.build_catchup_policy(
            {"status": "synced", "remaining_blocks": 0},
            {"node": {}},
            {"pool": {"running": True}},
            {"node_mineable": False, "node_submit_ready": False, "node_p2p_mining_fresh": True},
            {"iowait_percent": 21.0, "io_full_avg10": 22.0},
            mining_ready=False,
        )

        self.assertFalse(policy["active"])
        self.assertEqual(policy["trigger"], "")
        self.assertEqual(policy["lag_blocks"], 0)
        self.assertTrue(policy["backend_unready_under_pressure"])
        self.assertEqual(policy["summary"], "")
        self.assertEqual(policy["user_message"], "")

    def test_catchup_policy_pauses_importing_backend_without_known_lag(self) -> None:
        policy = pool_ops.build_catchup_policy(
            {"status": "syncing", "remaining_blocks": None},
            {"node": {"importing": True, "last_import_age_seconds": 4}},
            {"pool": {"running": True}},
            {"node_mineable": False, "node_submit_ready": False},
            {},
            mining_ready=False,
        )

        self.assertTrue(policy["active"])
        self.assertEqual(policy["trigger"], "backend_syncing")
        self.assertTrue(policy["backend_sync_active"])
        self.assertTrue(policy["node_sync_busy"])
        self.assertFalse(policy["pool_pause_active"])
        self.assertIn("mining templates are not ready", policy["summary"])

    def test_live_import_does_not_block_when_sync_status_is_synced(self) -> None:
        self.assertFalse(
            pool_ops.node_import_blocks_mining(
                {"status": "synced", "remaining_blocks": 0, "nodes": {"node": {"status": "synced", "remaining_blocks": 0}}},
                "node",
                {"importing": True, "last_import_age_seconds": 2},
            )
        )

    def test_import_blocks_when_rpc_status_is_unknown(self) -> None:
        self.assertTrue(
            pool_ops.node_import_blocks_mining(
                {"status": "unknown", "remaining_blocks": None, "nodes": {"node": {"status": "unknown"}}},
                "node",
                {"importing": True, "last_import_age_seconds": 2},
            )
        )


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
pool_rpc_backend_selected{backend="node",pool_id="0"} 1
pool_rpc_backend_healthy{backend="node",pool_id="0"} 1
pool_rpc_backend_node_health_mineable{backend="node",pool_id="0"} 0
pool_rpc_backend_node_health_submit_ready{backend="node",pool_id="0"} 0
pool_rpc_backend_node_health_p2p_mining_fresh{backend="node",pool_id="0"} 1
pool_rpc_backend_node_health_template_age_seconds{backend="node",pool_id="0"} 0.2
pool_rpc_backend_ws_connected{backend="node",pool_id="0"} 0
pool_job_health_ok{pool_id="0"} 0
pool_job_health_ready_miners{pool_id="0"} 5
pool_template_conversion_stall_active_miners{pool_id="0"} 5
pool_template_conversion_stall_failure_ratio{pool_id="0"} 55
pool_template_conversion_stall_window_candidates{kind="accepted",pool_id="0"} 2
pool_template_conversion_stall_window_candidates{kind="failed",pool_id="0"} 3
pool_block_submit_outcomes_total{outcome="accepted",pool_id="0",reason="ok"} 10
pool_block_submit_outcomes_total{outcome="rejected",pool_id="0",reason="tip-overdue"} 8
pool_block_submit_backend_outcomes_total{backend="node",outcome="rejected",pool_id="0",reason="tip-overdue"} 8
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
        self.assertEqual(payload["selected_backend"], "node")
        self.assertFalse(payload["selected_backend_source_health"]["node_mineable"])
        self.assertFalse(payload["selected_backend_source_health"]["ws_connected"])
        self.assertFalse(payload["selected_backend_source_health"]["template_delivery_effective"])
        self.assertEqual(payload["loss_ledger"]["severity"], "critical")
        self.assertEqual(payload["loss_ledger"]["share_outcomes"]["accepted_ratio_percent"], 25.0)

    def test_pool_metrics_marks_fresh_template_delivery_when_ws_metric_is_off(self) -> None:
        metrics = """
pool_active_connections 1
pool_rpc_backend_selected{backend="node",pool_id="0"} 1
pool_rpc_backend_healthy{backend="node",pool_id="0"} 1
pool_rpc_backend_ws_connected{backend="node",pool_id="0"} 0
pool_rpc_backend_node_health_mineable{backend="node",pool_id="0"} 1
pool_rpc_backend_node_health_submit_ready{backend="node",pool_id="0"} 1
pool_rpc_backend_node_health_p2p_mining_fresh{backend="node",pool_id="0"} 1
pool_rpc_backend_node_health_template_age_seconds{backend="node",pool_id="0"} 0.2
pool_rpc_backend_node_health_last_template_build_age_seconds{backend="node",pool_id="0"} 0.1
"""
        pool_ops.fetch_text_url = lambda *_args, **_kwargs: metrics

        payload = pool_ops.collect_pool_prometheus_metrics(
            {"asic-pool": {"running": True, "network_ips": ["10.0.0.2"]}}
        )

        selected = payload["selected_backend_source_health"]
        self.assertFalse(selected["ws_connected"])
        self.assertFalse(selected["ws_connected_observed"])
        self.assertTrue(selected["template_delivery_effective"])
        self.assertEqual(selected["template_delivery_mode"], "fresh-template-fallback")


if __name__ == "__main__":
    unittest.main()

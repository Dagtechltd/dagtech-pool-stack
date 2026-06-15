#!/usr/bin/env python3

import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import optimization_measurement as measurement  # noqa: E402


class OptimizationMeasurementTests(unittest.TestCase):
    def test_live_window_seconds_enforces_five_minute_positive_floor(self) -> None:
        self.assertEqual(0.0, measurement.live_window_seconds(0.0))
        self.assertEqual(300.0, measurement.live_window_seconds(120.0))
        self.assertEqual(1800.0, measurement.live_window_seconds(1800.0))

    def test_flatten_status_sample_extracts_resource_and_sync_fields(self) -> None:
        payload = {
            "overall": "syncing",
            "mode": "sync_only_no_miners",
            "can_mine": False,
            "sync_progress": {
                "status": "syncing",
                "current_block": 10,
                "highest_block": 15,
                "remaining_blocks": 5,
                "nodes": {
                    "primary": {"chain_rpc_latency_ms": 12.5},
                    "secondary": {"chain_rpc_latency_ms": 8.0},
                },
            },
            "host_pressure": {"iowait_percent": 3.0, "io_some_avg10": 1.0, "cpu_some_avg10": 2.0},
            "miner_health": {"connected_count": 0, "managed_count": 0},
            "adaptive_concurrency": {
                "pressure_level": "low",
                "workers": {"global_rpc": 6},
                "host_profile": {"profile": "pi5", "os": "linux", "arch": "arm64"},
            },
        }

        sample = measurement.flatten_status_sample(payload, "fixture", 4.2, 3.1)

        self.assertEqual(sample["sync_status"], "syncing")
        self.assertEqual(sample["current_block"], 10)
        self.assertEqual(sample["remaining_blocks"], 5)
        self.assertEqual(sample["chain_rpc_latency_ms_max"], 12.5)
        self.assertEqual(sample["adaptive_workers"]["global_rpc"], 6)
        self.assertEqual(sample["dashboard_latency_ms"], 3.1)

    def test_summarize_samples_reports_block_rate_and_worker_ranges(self) -> None:
        samples = [
            {
                "sampled_at": "2026-05-26T00:00:00+00:00",
                "sampled_epoch": 100,
                "source": "fixture",
                "overall": "syncing",
                "mode": "sync_only_no_miners",
                "sync_status": "syncing",
                "current_block": 1000,
                "remaining_blocks": 50,
                "connected_miners": 0,
                "managed_miners": 0,
                "collection_ms": 10.0,
                "chain_rpc_latency_ms_max": 5.0,
                "iowait_percent": 1.0,
                "adaptive_workers": {"global_rpc": 6},
                "host_profile": {"profile": "pi5", "os": "linux", "arch": "arm64"},
            },
            {
                "sampled_at": "2026-05-26T00:00:10+00:00",
                "sampled_epoch": 110,
                "source": "fixture",
                "overall": "ok",
                "mode": "ready_no_miners",
                "sync_status": "synced",
                "current_block": 1020,
                "remaining_blocks": 0,
                "connected_miners": 0,
                "managed_miners": 0,
                "collection_ms": 20.0,
                "chain_rpc_latency_ms_max": 7.0,
                "iowait_percent": 2.0,
                "adaptive_workers": {"global_rpc": 3},
                "host_profile": {"profile": "pi5", "os": "linux", "arch": "arm64"},
            },
        ]

        summary = measurement.summarize_samples(samples)

        self.assertEqual(summary["sample_count"], 2)
        self.assertEqual(summary["block_delta"], 20)
        self.assertEqual(summary["blocks_per_second"], 2.0)
        self.assertEqual(summary["chain_rpc_latency_ms_p95"], 7.0)
        self.assertEqual(summary["adaptive_worker_ranges"]["global_rpc"], {"min": 3, "max": 6})

    def test_parse_prometheus_keeps_direct_pool_efficiency_metrics(self) -> None:
        metrics = measurement.parse_prometheus(
            "\n".join(
                [
                    'pool_block_submit_outcomes_total{outcome="accepted",pool_id="0",reason="ok"} 10',
                    'pool_template_conversion_stall_failure_ratio{pool_id="0"} 25',
                    'pool_rpc_backend_template_fetch_duration_seconds_sum{node="node",pool_id="0"} 1.5',
                    'pool_rpc_backend_template_fetch_duration_seconds_count{node="node",pool_id="0"} 3',
                    'unrelated_metric_total 99',
                ]
            )
        )

        self.assertEqual(
            metrics['pool_block_submit_outcomes_total{outcome="accepted",pool_id="0",reason="ok"}'],
            10.0,
        )
        self.assertEqual(metrics['pool_template_conversion_stall_failure_ratio{pool_id="0"}'], 25.0)
        self.assertNotIn("unrelated_metric_total", metrics)

    def test_summarize_samples_reports_prometheus_counter_deltas(self) -> None:
        first_metrics = measurement.parse_prometheus(
            "\n".join(
                [
                    'pool_block_submit_outcomes_total{outcome="accepted",pool_id="0",reason="ok"} 10',
                    'pool_block_submit_outcomes_total{outcome="rejected-local",pool_id="0",reason="expired"} 2',
                    'pool_rpc_backend_template_errors_total{node="node",pool_id="0",reason="rpc-error"} 4',
                    'pool_rpc_backend_template_fetch_duration_seconds_sum{node="node",pool_id="0"} 1.0',
                    'pool_rpc_backend_template_fetch_duration_seconds_count{node="node",pool_id="0"} 2',
                    'pool_template_conversion_stall_failure_ratio{pool_id="0"} 10',
                    'pool_template_conversion_stall_active_miners{pool_id="0"} 2',
                ]
            )
        )
        last_metrics = measurement.parse_prometheus(
            "\n".join(
                [
                    'pool_block_submit_outcomes_total{outcome="accepted",pool_id="0",reason="ok"} 16',
                    'pool_block_submit_outcomes_total{outcome="rejected-local",pool_id="0",reason="expired"} 5',
                    'pool_rpc_backend_template_errors_total{node="node",pool_id="0",reason="rpc-error"} 5',
                    'pool_rpc_backend_template_fetch_duration_seconds_sum{node="node",pool_id="0"} 2.2',
                    'pool_rpc_backend_template_fetch_duration_seconds_count{node="node",pool_id="0"} 5',
                    'pool_template_conversion_stall_failure_ratio{pool_id="0"} 20',
                    'pool_template_conversion_stall_active_miners{pool_id="0"} 2',
                ]
            )
        )
        samples = [
            {
                "sampled_at": "2026-05-26T00:00:00+00:00",
                "sampled_epoch": 100,
                "source": "fixture",
                "overall": "ok",
                "mode": "mining",
                "sync_status": "synced",
                "current_block": 1000,
                "connected_miners": 2,
                "managed_miners": 2,
                "collection_ms": 10.0,
                "metrics_url": "http://127.0.0.1:9090/metrics",
                **measurement.flatten_prometheus_sample(first_metrics, "http://127.0.0.1:9090/metrics", 1.0),
            },
            {
                "sampled_at": "2026-05-26T00:01:00+00:00",
                "sampled_epoch": 160,
                "source": "fixture",
                "overall": "ok",
                "mode": "mining",
                "sync_status": "synced",
                "current_block": 1010,
                "connected_miners": 2,
                "managed_miners": 2,
                "collection_ms": 10.0,
                "metrics_url": "http://127.0.0.1:9090/metrics",
                **measurement.flatten_prometheus_sample(last_metrics, "http://127.0.0.1:9090/metrics", 1.5),
            },
        ]

        summary = measurement.summarize_samples(samples)

        self.assertEqual(summary["block_submit_accepted_delta"], 6.0)
        self.assertEqual(summary["block_submit_rejected_local_delta"], 3.0)
        self.assertEqual(summary["template_error_deltas"], {
            'pool_rpc_backend_template_errors_total{node="node",pool_id="0",reason="rpc-error"}': 1.0
        })
        self.assertEqual(summary["backend_template_fetch_avg_seconds"], 0.4)
        self.assertEqual(summary["template_conversion_failure_ratio_max"], 20.0)


if __name__ == "__main__":
    unittest.main()

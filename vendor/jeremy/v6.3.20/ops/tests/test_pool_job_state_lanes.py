#!/usr/bin/env python3

import json
import pathlib
import sys
import unittest
from unittest import mock

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


class PoolJobStateLaneTests(unittest.TestCase):
    def test_source_job_health_lane_summary_prefers_mac_identity(self) -> None:
        summary = pool_ops.source_job_health_lane_summary(
            {
                "job_state_available": True,
                "clients": [
                    {
                        "address": "192.168.1.14:5000",
                        "asic_mac": "28:E2:97:3E:39:63",
                        "lane_id": "mac:28:e2:97:3e:39:63",
                        "authorized": True,
                        "ready": True,
                    },
                    {
                        "address": "192.168.1.103:5001",
                        "asic_mac": "28:E2:97:1E:C0:B5",
                        "lane_id": "mac:28:e2:97:1e:c0:b5",
                        "authorized": False,
                        "ready": False,
                    },
                ],
            }
        )

        self.assertTrue(summary["job_state_available"])
        self.assertEqual(
            ["mac:28:e2:97:1e:c0:b5", "mac:28:e2:97:3e:39:63"],
            summary["active_lane_ids"],
        )
        self.assertEqual(["mac:28:e2:97:3e:39:63"], summary["authorized_lane_ids"])
        self.assertEqual(["mac:28:e2:97:3e:39:63"], summary["ready_lane_ids"])

    def test_collect_pool_metrics_ingests_job_state_clients(self) -> None:
        containers = {"pool": {"running": True, "network_ips": ["172.18.0.4"]}}
        metrics = "\n".join(
            [
                "pool_active_connections{pool_id=\"0\"} 1",
                "pool_job_health_authorized_miners{pool_id=\"0\"} 1",
                "pool_job_health_ready_miners{pool_id=\"0\"} 1",
            ]
        )
        job_state = {
            "status": "ok",
            "reason_code": "ok",
            "active_connections": 1,
            "authorized_connections": 1,
            "subscribed_connections": 1,
            "ready_connections": 1,
            "current_template_seq": 42,
            "clients": [
                {
                    "address": "192.168.1.14:5000",
                    "asic_mac": "28:E2:97:3E:39:63",
                    "lane_id": "mac:28:e2:97:3e:39:63",
                    "authorized": True,
                    "ready": True,
                }
            ],
        }

        def fake_fetch(url: str, _headers: dict[str, str], timeout: float) -> str:
            self.assertGreater(timeout, 0)
            if url.endswith("/metrics"):
                return metrics
            if url.endswith("/health/job-state"):
                return json.dumps(job_state)
            raise AssertionError(f"unexpected URL {url}")

        with mock.patch.object(pool_ops, "POOL_CONTAINERS", ["pool"]), mock.patch.object(
            pool_ops, "fetch_text_url", side_effect=fake_fetch
        ):
            payload = pool_ops.collect_pool_prometheus_metrics(containers)

        source = payload["source_job_health"]
        self.assertTrue(source["job_state_available"])
        self.assertEqual(1, source["authorized_miners"])
        self.assertEqual(["mac:28:e2:97:3e:39:63"], source["authorized_lane_ids"])
        self.assertEqual("ok", payload["containers"]["pool"]["job_state_status"])


if __name__ == "__main__":
    unittest.main()

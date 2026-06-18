#!/usr/bin/env python3

import json
import pathlib
import sys
import tempfile
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import dashboard  # noqa: E402


class DashboardStatusFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            "RUNTIME_DIR": dashboard.RUNTIME_DIR,
            "DASHBOARD_DIRECT_STATUS_FALLBACK": dashboard.DASHBOARD_DIRECT_STATUS_FALLBACK,
            "STATUS_CACHE_SECONDS": dashboard.STATUS_CACHE_SECONDS,
            "SAMPLER_CACHE_SECONDS": dashboard.SAMPLER_CACHE_SECONDS,
            "DASHBOARD_STATUS_SAMPLE_WAIT_SECONDS": dashboard.DASHBOARD_STATUS_SAMPLE_WAIT_SECONDS,
            "SYNC_ESTIMATE_STATE_FILE": dashboard.SYNC_ESTIMATE_STATE_FILE,
            "collect_status_cached": dashboard.collect_status_cached,
            "time_time": dashboard.time.time,
        }
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        dashboard.RUNTIME_DIR = self.originals["RUNTIME_DIR"]
        dashboard.DASHBOARD_DIRECT_STATUS_FALLBACK = self.originals["DASHBOARD_DIRECT_STATUS_FALLBACK"]
        dashboard.STATUS_CACHE_SECONDS = self.originals["STATUS_CACHE_SECONDS"]
        dashboard.SAMPLER_CACHE_SECONDS = self.originals["SAMPLER_CACHE_SECONDS"]
        dashboard.DASHBOARD_STATUS_SAMPLE_WAIT_SECONDS = self.originals["DASHBOARD_STATUS_SAMPLE_WAIT_SECONDS"]
        dashboard.SYNC_ESTIMATE_STATE_FILE = self.originals["SYNC_ESTIMATE_STATE_FILE"]
        dashboard.collect_status_cached = self.originals["collect_status_cached"]
        dashboard.time.time = self.originals["time_time"]
        dashboard.clear_api_cache()

    def configure_fast_path(self, tmp: str, now: float = 1000.0) -> pathlib.Path:
        runtime = pathlib.Path(tmp)
        dashboard.RUNTIME_DIR = runtime
        dashboard.DASHBOARD_DIRECT_STATUS_FALLBACK = False
        dashboard.STATUS_CACHE_SECONDS = 10.0
        dashboard.SAMPLER_CACHE_SECONDS = 10.0
        dashboard.DASHBOARD_STATUS_SAMPLE_WAIT_SECONDS = 30.0
        dashboard.time.time = lambda: now
        dashboard.collect_status_cached = lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("dashboard status must not enter direct collection on cache miss")
        )
        dashboard.clear_api_cache()
        return runtime

    def test_status_payload_uses_fresh_sampler_without_direct_collect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.configure_fast_path(tmp)
            (runtime / "status-sampler.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "epoch": 995.0,
                        "include_logs": True,
                        "payload": {
                            "generated_at": "2026-05-31T22:00:00+0000",
                            "overall": "ok",
                            "fresh": True,
                            "age_seconds": 1.0,
                            "stale_after_seconds": 15,
                            "failures": [],
                            "warnings": [],
                        },
                    }
                ),
                encoding="utf-8",
            )

            payload = dashboard.dashboard_status_payload()

        self.assertEqual(payload["overall"], "ok")
        self.assertTrue(payload["fresh"])
        self.assertEqual(payload["age_seconds"], 6.0)
        self.assertTrue(payload["status_sampler"]["hit"])
        self.assertIn("dashboard_url", payload)

    def test_payload_past_stale_after_is_not_served_as_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.configure_fast_path(tmp)
            (runtime / "status-sampler.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "epoch": 995.0,
                        "include_logs": True,
                        "payload": {
                            "generated_at": "2026-05-31T22:00:00+0000",
                            "overall": "ok",
                            "fresh": True,
                            "age_seconds": 8.0,
                            "stale_after_seconds": 10,
                            "failures": [],
                            "warnings": [],
                        },
                    }
                ),
                encoding="utf-8",
            )

            payload = dashboard.dashboard_status_payload()

        self.assertEqual(payload["overall"], "degraded")
        self.assertFalse(payload["fresh"])
        self.assertTrue(payload["collector_budget_exceeded"])
        self.assertIsInstance(payload["failures"][0], str)
        self.assertIsInstance(payload["pool"], dict)
        self.assertTrue(payload["status_sampler"]["stale"])

    def test_extended_sampler_window_serves_bounded_constrained_host_sample(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.configure_fast_path(tmp)
            dashboard.SAMPLER_CACHE_SECONDS = 120.0
            dashboard.STATUS_CACHE_SECONDS = 120.0
            (runtime / "status-sampler.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "epoch": 940.0,
                        "include_logs": True,
                        "payload": {
                            "generated_at": "2026-05-31T22:00:00+0000",
                            "overall": "syncing",
                            "fresh": True,
                            "age_seconds": 0.0,
                            "stale_after_seconds": 120,
                            "failures": [],
                            "warnings": [],
                        },
                    }
                ),
                encoding="utf-8",
            )

            payload = dashboard.dashboard_status_payload()

        self.assertEqual(payload["overall"], "syncing")
        self.assertTrue(payload["fresh"])
        self.assertEqual(payload["age_seconds"], 60.0)
        self.assertTrue(payload["status_sampler"]["hit"])
        self.assertEqual(payload["status_sampler"]["max_age_seconds"], 120.0)

    def test_api_status_rechecks_files_instead_of_serving_process_cached_ok(self) -> None:
        now = {"value": 1000.0}

        def request_api_status() -> dict[str, object]:
            handler = object.__new__(dashboard.Handler)
            handler.path = "/api/status"
            captured: list[dict[str, object]] = []
            handler.send_json = lambda payload, status=200: captured.append(payload)
            handler.do_GET()
            self.assertEqual(len(captured), 1)
            return captured[0]

        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.configure_fast_path(tmp)
            dashboard.time.time = lambda: now["value"]
            (runtime / "status-sampler.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "epoch": 991.0,
                        "include_logs": True,
                        "payload": {
                            "generated_at": "2026-05-31T22:00:00+0000",
                            "overall": "ok",
                            "fresh": True,
                            "age_seconds": 0.0,
                            "stale_after_seconds": 30,
                            "failures": [],
                            "warnings": [],
                        },
                    }
                ),
                encoding="utf-8",
            )

            first = request_api_status()
            now["value"] = 1009.9
            second = request_api_status()

        self.assertEqual(first["overall"], "ok")
        self.assertEqual(first["status_sampler"]["age_seconds"], 9.0)
        self.assertEqual(second["overall"], "degraded")
        self.assertFalse(second["fresh"])
        self.assertTrue(second["collector_budget_exceeded"])
        self.assertTrue(second["status_sampler"]["stale"])

    def test_status_payload_returns_bounded_fallback_when_cache_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.configure_fast_path(tmp)

            payload = dashboard.dashboard_status_payload()

        self.assertEqual(payload["overall"], "degraded")
        self.assertFalse(payload["fresh"])
        self.assertTrue(payload["collector_budget_exceeded"])
        self.assertEqual(payload["chain_rpc_error"], "not_checked_budgeted_status_fallback")
        self.assertIsInstance(payload["failures"][0], str)
        self.assertIsInstance(payload["pool"], dict)
        self.assertFalse(payload["status_sampler"]["hit"])
        self.assertFalse(payload["shared_status_cache"]["hit"])
        self.assertEqual(payload["mode"], "waiting_for_status_sample")
        self.assertIn("Dashboard status is warming up", payload["status_reason"])
        self.assertIn("within about 30s", payload["status_reason"])
        self.assertNotIn("cache unavailable", payload["status_reason"])
        self.assertEqual(payload["sync_progress"]["status"], "waiting_for_status_sample")
        self.assertEqual(payload["sync_estimate"]["eta_seconds"], 30)
        self.assertEqual(
            payload["sync_estimate"]["next_step"],
            "wait about 30s for the next dashboard status sample; mining may still be running",
        )
        self.assertEqual(payload["collector_budget_failure"]["class"], "waiting_for_status_sample")
        self.assertEqual(payload["collector_budget_failure"]["estimated_wait_seconds"], 30)
        self.assertIsNone(payload["collector_budget_failure"]["newest_cache_age_seconds"])

    def test_sync_estimate_uses_catchup_pause_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dashboard.SYNC_ESTIMATE_STATE_FILE = pathlib.Path(tmp) / "sync-estimate.json"
            payload = {
                "sync_progress": {
                    "status": "syncing",
                    "remaining_blocks": 450,
                    "nodes": {
                        "node": {
                            "current_block": 1000,
                            "highest_block": 1450,
                            "remaining_blocks": 450,
                        }
                    },
                },
                "sync_health": {},
                "sync_coordinator": {},
                "nodes": {"node": {}},
                "managed_node_services": ["node"],
                "catchup_policy": {
                    "active": True,
                    "lag_blocks": 450,
                    "threshold_blocks": 300,
                    "user_message": "Mining is intentionally paused while chain catch-up has priority.",
                    "next_step": "Mining resumes when lag is at or below 300 blocks.",
                },
            }

            enriched = dashboard.enrich_status_with_sync_estimate(payload)

        estimate = enriched["sync_estimate"]
        self.assertEqual(estimate["stage"], "Catch-up pause active")
        self.assertTrue(estimate["catchup_pause_active"])
        self.assertEqual(estimate["catchup_pause_lag_blocks"], 450)
        self.assertEqual(estimate["catchup_pause_threshold_blocks"], 300)
        self.assertEqual(estimate["narrative"], "Mining is intentionally paused while chain catch-up has priority.")
        self.assertEqual(estimate["next_step"], "Mining resumes when lag is at or below 300 blocks.")

    def test_status_payload_fallback_estimates_wait_from_stale_cache_age(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.configure_fast_path(tmp)
            (runtime / "status-sampler.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "epoch": 985.0,
                        "include_logs": True,
                        "payload": {
                            "generated_at": "2026-05-31T22:00:00+0000",
                            "overall": "ok",
                            "fresh": True,
                            "age_seconds": 0.0,
                            "stale_after_seconds": 10,
                            "failures": [],
                            "warnings": [],
                        },
                    }
                ),
                encoding="utf-8",
            )

            payload = dashboard.dashboard_status_payload()

        self.assertEqual(payload["mode"], "waiting_for_status_sample")
        self.assertIn("newest cached sample is about 15s old", payload["status_reason"])
        self.assertIn("current data in about 15s", payload["status_reason"])
        self.assertEqual(payload["sync_estimate"]["eta_seconds"], 15)
        self.assertEqual(
            payload["sync_estimate"]["next_step"],
            "wait about 15s for the next dashboard status sample; mining may still be running",
        )
        self.assertEqual(payload["collector_budget_failure"]["estimated_wait_seconds"], 15)
        self.assertEqual(payload["collector_budget_failure"]["newest_cache_age_seconds"], 15.0)

    def test_stale_ok_cache_is_not_served_as_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.configure_fast_path(tmp)
            (runtime / "shared-status-cache.json").write_text(
                json.dumps(
                    {
                        "with_logs": {
                            "epoch": 900.0,
                            "payload": {
                                "generated_at": "2026-05-31T21:59:00+0000",
                                "overall": "ok",
                                "fresh": True,
                                "age_seconds": 0.0,
                                "stale_after_seconds": 15,
                                "failures": [],
                                "warnings": [],
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            payload = dashboard.dashboard_status_payload()

        self.assertEqual(payload["overall"], "degraded")
        self.assertFalse(payload["fresh"])
        self.assertTrue(payload["collector_budget_exceeded"])
        self.assertTrue(payload["shared_status_cache"]["stale"])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
import unittest.mock


OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import mining_guard_30min  # noqa: E402


class MiningGuard30MinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.state_file = self.root / "state.json"
        self.history_file = self.root / "history.jsonl"
        self.log_file = self.root / "logs" / "guard.log"

    def patch_runtime_files(self):
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        return unittest.mock.patch.multiple(
            mining_guard_30min,
            STATE_FILE=self.state_file,
            HISTORY_FILE=self.history_file,
            LOG_FILE=self.log_file,
            ensure_runtime=lambda: self.log_file.parent.mkdir(parents=True, exist_ok=True),
            now_iso=lambda: "2026-06-02T08:50:00+02:00",
        )

    def test_unhealthy_sample_records_source_freshness_in_incident(self) -> None:
        incidents: list[dict[str, object]] = []
        source_freshness = {
            "policy": "fetch-only",
            "repos": [{"path": "/repo", "behind_count": 1, "recent_upstream_commits": ["abc fix"]}],
        }

        def fake_append_incident(*args, **kwargs):
            incidents.append({"args": args, "kwargs": kwargs})
            return {"id": "incident"}

        with self.patch_runtime_files(), unittest.mock.patch.object(
            mining_guard_30min, "fallback_status", return_value=(None, "timeout")
        ), unittest.mock.patch.object(
            mining_guard_30min, "source_freshness_triage", return_value=source_freshness
        ), unittest.mock.patch.object(
            mining_guard_30min, "append_incident", fake_append_incident
        ):
            sample = mining_guard_30min.run_once()

        self.assertEqual("critical", sample["guard_state"])
        self.assertEqual(source_freshness, sample["source_freshness"])
        self.assertEqual(1, len(incidents))
        details = incidents[0]["args"][4]
        self.assertEqual(source_freshness, details["source_freshness"])
        self.assertIn("existing upstream fix", details["repair_policy"])
        self.assertTrue(self.history_file.exists())

    def test_healthy_sample_does_not_fetch_source_metadata(self) -> None:
        status = {
            "overall": "ok",
            "sync_progress": {"status": "synced", "remaining_blocks": 0},
            "sync_health": {"nodes_with_recent_imports": 1},
            "pool_health": {"initial_download": False, "connected_miners": 3},
            "miner_health": {"connected_count": 3},
        }

        with self.patch_runtime_files(), unittest.mock.patch.object(
            mining_guard_30min, "MINING_GUARD_ENABLED", False
        ), unittest.mock.patch.object(
            mining_guard_30min, "fallback_status", return_value=(status, "")
        ), unittest.mock.patch.object(
            mining_guard_30min,
            "source_freshness_triage",
            side_effect=AssertionError("healthy samples should not fetch source"),
        ):
            sample = mining_guard_30min.run_once()

        self.assertEqual("ok", sample["guard_state"])
        self.assertNotIn("source_freshness", sample)

    def test_multiple_miners_without_expected_ip_accepts_recent_valid_shares(self) -> None:
        status = {
            "overall": "ok",
            "sync_progress": {"status": "synced", "remaining_blocks": 0},
            "sync_health": {"nodes_with_recent_imports": 1},
            "pool_health": {
                "initial_download": True,
                "connected_miners": 3,
                "job_notify_count": 0,
                "last_valid_share_age_seconds": 25,
            },
            "miner_health": {
                "connected_count": 3,
                "miners": [
                    {"ip": "192.168.1.14", "configured": True, "connected": True},
                    {"ip": "192.168.1.16", "configured": True, "connected": True},
                    {"ip": "192.168.1.101", "configured": True, "connected": True},
                ],
            },
        }

        with self.patch_runtime_files(), unittest.mock.patch.object(
            mining_guard_30min, "MINING_GUARD_ENABLED", True
        ), unittest.mock.patch.object(
            mining_guard_30min, "EXPECTED_ASIC_IP", ""
        ), unittest.mock.patch.object(
            mining_guard_30min, "fallback_status", return_value=(status, "")
        ), unittest.mock.patch.object(
            mining_guard_30min,
            "source_freshness_triage",
            side_effect=AssertionError("fresh shares should keep the guard healthy"),
        ):
            sample = mining_guard_30min.run_once()

        self.assertEqual("ok", sample["guard_state"])
        self.assertFalse(sample["expected_asic_required"])
        self.assertEqual([], sample["problems"])

    def test_source_repo_triage_handles_missing_path(self) -> None:
        missing = self.root / "missing-repo"

        result = mining_guard_30min.source_repo_triage(missing)

        self.assertFalse(result["available"])
        self.assertEqual("path-missing", result["error"])


if __name__ == "__main__":
    unittest.main()

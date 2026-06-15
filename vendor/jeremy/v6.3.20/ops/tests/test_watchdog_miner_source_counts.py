#!/usr/bin/env python3

import pathlib
import os
import sys
import unittest
from unittest import mock

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import watchdog  # noqa: E402


ADDRESS = "0x1111111111111111111111111111111111111111"
DEFAULT_POOL_URL = watchdog.default_miner_pool_settings()["pool_url"]


def miner_row(
    ip: str,
    *,
    lane_status: str,
    submits: int = 0,
    shares: int = 0,
    last_submit_epoch: int = 0,
    last_pool_seen_epoch: int = 0,
) -> dict[str, object]:
    return {
        "connected": True,
        "device_type": "stratum",
        "display_name": ip,
        "expected_pool_url": DEFAULT_POOL_URL,
        "expected_worker_user": ADDRESS,
        "ip": ip,
        "lane_status": lane_status,
        "last_pool_seen_epoch": last_pool_seen_epoch,
        "last_submit_epoch": last_submit_epoch,
        "shares": shares,
        "submits": submits,
        "workers": [ADDRESS],
    }


def status_for(miners: list[dict[str, object]], *, expected: int, imbalanced: int) -> dict[str, object]:
    return {
        "mining_address": ADDRESS,
        "pool_health": {"initial_download": False, "job_notify_count": 1},
        "sync_health": {},
        "sync_progress": {"remaining_blocks": 0, "status": "synced"},
        "miner_health": {
            "connected_count": len(miners),
            "managed_count": 0,
            "lane_balance": {
                "expected_lane_count": expected,
                "imbalanced_count": imbalanced,
            },
            "miners": miners,
        },
    }


def api_stalled_asic_row(
    ip: str = "192.168.1.16",
    *,
    managed: bool = True,
    mac: str = "28:e2:97:4d:44:3a",
    stale_age: int = 600,
) -> dict[str, object]:
    return {
        "configured": False,
        "connected": False,
        "debug": {"available": False},
        "debug_error": "HTTP 500 Server Error",
        "device_id": f"mac:{mac}",
        "device_type": "asic",
        "display_name": ip,
        "expected_pool_url": DEFAULT_POOL_URL,
        "expected_worker_user": ADDRESS,
        "ip": ip,
        "issue": f"miner request failed for {ip}/mcb/cgminer?cgminercmd=devs: timed out",
        "last_pool_seen_age_seconds": stale_age,
        "mac": mac,
        "managed": managed,
        "pool_active": False,
        "status": "down",
        "work_pool_active": False,
        "workers": [ADDRESS],
    }


class WatchdogMinerSourceCountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1_779_180_000
        self.old_time = watchdog.time.time
        self.old_env = {
            "BDAG_ASIC_LAN_CIDRS": os.environ.get("BDAG_ASIC_LAN_CIDRS"),
            "BDAG_MINER_SCAN_TARGET": os.environ.get("BDAG_MINER_SCAN_TARGET"),
        }
        os.environ["BDAG_ASIC_LAN_CIDRS"] = "192.168.1.0/24"
        os.environ.pop("BDAG_MINER_SCAN_TARGET", None)
        watchdog.time.time = lambda: self.now

    def tearDown(self) -> None:
        watchdog.time.time = self.old_time
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_zero_miners_do_not_create_degradation(self) -> None:
        status = status_for([], expected=0, imbalanced=0)

        self.assertEqual([], watchdog.degraded_primary_miners(status, 120))

    def test_no_window_work_does_not_make_all_miners_degraded(self) -> None:
        miners = [
            miner_row(f"192.168.1.{14 + index}", lane_status="no-window-work", last_pool_seen_epoch=self.now - 15)
            for index in range(5)
        ]
        status = status_for(miners, expected=5, imbalanced=0)

        self.assertEqual([], watchdog.degraded_primary_miners(status, 120))

    def test_pool_seen_without_submit_is_not_connected_submitting(self) -> None:
        miners = [
            miner_row("192.168.1.14", lane_status="no-work", last_pool_seen_epoch=self.now - 15),
            miner_row("192.168.1.15", lane_status="balanced", shares=12, last_submit_epoch=self.now - 10),
        ]
        status = status_for(miners, expected=2, imbalanced=1)

        self.assertEqual([], watchdog.degraded_primary_miners(status, 120))

    def test_work_pool_active_false_overrides_stale_connection_identity(self) -> None:
        row = miner_row("192.168.1.14", lane_status="no-work", submits=1, last_submit_epoch=self.now - 15)
        row["work_pool_active"] = False
        status = status_for([row], expected=1, imbalanced=1)

        self.assertEqual([], watchdog.degraded_primary_miners(status, 120))

    def test_low_lane_with_recent_submit_is_degraded(self) -> None:
        miners = [
            miner_row("192.168.1.14", lane_status="low", submits=1, last_submit_epoch=self.now - 15),
            miner_row("192.168.1.15", lane_status="balanced", shares=12, last_submit_epoch=self.now - 10),
        ]
        status = status_for(miners, expected=2, imbalanced=1)

        degraded = watchdog.degraded_primary_miners(status, 120)

        self.assertEqual(1, len(degraded))
        self.assertEqual("192.168.1.14", degraded[0]["ip"])

    def test_useful_work_stall_timer_uses_mac_and_survives_transient_degraded_sample(self) -> None:
        row = miner_row("192.168.1.16", lane_status="no-work", submits=1, last_submit_epoch=self.now - 200)
        row["mac"] = "28:e2:97:4d:44:3a"
        row["device_id"] = "mac:28:e2:97:4d:44:3a"
        state = {"miner_useful_work_stall_since": {"mac:28:e2:97:4d:44:3a": self.now - 180}}

        since = watchdog.update_useful_work_stall_since(state, [], [row], self.now)

        self.assertEqual({"mac:28:e2:97:4d:44:3a": self.now - 180}, since)

    def test_useful_work_stall_timer_ignores_legacy_ip_key(self) -> None:
        row = miner_row("192.168.1.16", lane_status="no-work", submits=1, last_submit_epoch=self.now - 200)
        row["mac"] = "28:e2:97:4d:44:3a"
        state = {"miner_useful_work_stall_since": {"192.168.1.16": self.now - 180}}

        since = watchdog.update_useful_work_stall_since(state, [], [row], self.now)

        self.assertEqual({"mac:28:e2:97:4d:44:3a": self.now}, since)

    def test_useful_work_stall_timer_clears_after_recovery(self) -> None:
        state = {"miner_useful_work_stall_since": {"mac:28:e2:97:4d:44:3a": self.now - 180}}

        since = watchdog.update_useful_work_stall_since(state, [], [], self.now)

        self.assertEqual({}, since)

    def test_api_stall_detector_requires_managed_primary_asic_and_clear_pool_faults(self) -> None:
        status = status_for([api_stalled_asic_row()], expected=1, imbalanced=0)

        affected = watchdog.asic_api_stall_primary_miners(status, stale_seconds=180)

        self.assertEqual(1, len(affected))
        self.assertEqual("192.168.1.16", affected[0]["ip"])
        self.assertTrue(affected[0]["restart_open_first"])

        unmanaged_status = status_for([api_stalled_asic_row(managed=False)], expected=1, imbalanced=0)
        self.assertEqual([], watchdog.asic_api_stall_primary_miners(unmanaged_status, stale_seconds=180))

        pool_fault_status = status_for([api_stalled_asic_row()], expected=1, imbalanced=0)
        pool_fault_status["pool_health"]["expired_job_reconnect_failed_no_share"] = True
        self.assertEqual([], watchdog.asic_api_stall_primary_miners(pool_fault_status, stale_seconds=180))

    def test_api_stall_detector_uses_authorized_mac_lane_absence_over_fresh_chatter(self) -> None:
        row = api_stalled_asic_row(stale_age=3)
        row["connected"] = True
        row["pool_lane_expected"] = True
        row["pool_lane_authorized"] = False
        row["issue"] = (
            "miner request failed for 192.168.1.16/mcb/cgminer?cgminercmd=devs: "
            "connection reset by peer"
        )
        status = status_for([row], expected=1, imbalanced=0)

        affected = watchdog.asic_api_stall_primary_miners(status, stale_seconds=180)

        self.assertEqual(1, len(affected))
        self.assertTrue(affected[0]["pool_authorized_missing"])
        self.assertTrue(affected[0]["restart_open_first"])

    def test_api_stall_detector_uses_backend_readiness_when_no_miners_can_receive_jobs(self) -> None:
        row = api_stalled_asic_row(stale_age=600)
        row["pool_lane_expected"] = True
        row["pool_lane_authorized"] = False
        status = status_for([row], expected=1, imbalanced=0)
        status["pool_health"] = {
            "initial_download": True,
            "job_notify_count": 0,
            "selected_backend_source_health": {
                "healthy": True,
                "node_mineable": True,
                "node_submit_ready": True,
                "template_delivery_effective": True,
            },
        }

        affected = watchdog.asic_api_stall_primary_miners(status, stale_seconds=180)

        self.assertEqual(1, len(affected))
        self.assertTrue(affected[0]["restart_open_first"])

    def test_api_stall_detector_still_waits_for_backend_readiness_without_job_notifies(self) -> None:
        row = api_stalled_asic_row(stale_age=600)
        status = status_for([row], expected=1, imbalanced=0)
        status["pool_health"] = {"initial_download": True, "job_notify_count": 0}

        self.assertEqual([], watchdog.asic_api_stall_primary_miners(status, stale_seconds=180))

    def test_watchdog_repairs_stale_asic_mac_override_environment(self) -> None:
        status = {
            "failures": [],
            "stack_failures": [],
            "miner_failures": [],
            "overall": "ok",
            "mode": "mining",
            "warnings": [],
            "degraded_reasons": [],
            "mining_address": ADDRESS,
            "nodes": {},
            "sync_health": {},
            "sync_progress": {"status": "synced", "remaining_blocks": 0, "nodes": {}},
            "pool_health": {"initial_download": False, "job_notify_count": 1, "valid_share_count": 4},
            "miner_health": {
                "connected_count": 2,
                "connected_count_effective": 2,
                "managed_count": 2,
                "miners": [
                    {
                        "ip": "192.168.1.100",
                        "mac": "28:e2:97:1e:c0:b5",
                        "device_type": "asic",
                        "managed": True,
                        "connected": True,
                    },
                    {
                        "ip": "192.168.1.105",
                        "mac": "28:e2:97:4d:44:3a",
                        "device_type": "asic",
                        "managed": True,
                        "connected": True,
                    },
                ],
            },
        }
        events: list[tuple[str, str, str, dict[str, object]]] = []
        written: list[dict[str, object]] = []

        def record(event_type: str, severity: str, message: str, details=None) -> None:
            events.append((event_type, severity, message, details or {}))

        with mock.patch.object(watchdog, "read_state", return_value={}), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "status_payload_needs_asic_mac_override_repair", return_value=True
        ), mock.patch.object(
            watchdog, "repair_pool_asic_mac_overrides", return_value=True
        ) as repair_mac, mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", side_effect=record
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        repair_mac.assert_called_once_with(status)
        self.assertIn("last_asic_mac_override_repair_at", result["watchdog_state"])
        self.assertTrue(any(event[0] == "watchdog_repaired_pool_asic_mac_overrides" for event in events))
        self.assertTrue(written)

    def test_api_stall_watchdog_restarts_one_asic_open_first_after_confirmation(self) -> None:
        row = api_stalled_asic_row()
        status = {
            "failures": [],
            "stack_failures": [],
            "miner_failures": ["miner request failed for 192.168.1.16/mcb/cgminer?cgminercmd=devs: timed out"],
            "mining_address": ADDRESS,
            "nodes": {},
            "sync_health": {},
            "sync_progress": {"status": "synced", "remaining_blocks": 0, "nodes": {}},
            "pool_health": {
                "initial_download": False,
                "job_notify_count": 1,
                "valid_share_count": 20,
            },
            "miner_health": {
                "connected_count": 1,
                "connected_count_effective": 1,
                "managed_count": 1,
                "miners": [row],
            },
        }
        state = {"asic_api_stall_since": {"mac:28:e2:97:4d:44:3a": self.now - 180}}
        restarts: list[tuple[list[dict[str, object]], str]] = []
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog,
            "run_miner_restarts",
            side_effect=lambda targets, reason: restarts.append((targets, reason))
            or {"status": "ok", "target_count": len(targets), "results": []},
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("asic_api_stall", result["watchdog_state"]["last_status"])
        self.assertEqual(1, len(restarts))
        self.assertIn("ASIC API-stall watchdog", restarts[0][1])
        self.assertEqual("192.168.1.16", restarts[0][0][0]["ip"])
        self.assertTrue(restarts[0][0][0]["restart_open_first"])
        self.assertEqual(
            {"mac:28:e2:97:4d:44:3a": self.now},
            result["watchdog_state"]["last_miner_restart_at_by_identity"],
        )
        self.assertEqual({}, result["watchdog_state"]["asic_api_stall_since"])
        self.assertTrue(written)

    def test_failed_expired_job_reconnect_without_clients_restarts_pool(self) -> None:
        state: dict[str, object] = {}
        events: list[tuple[str, str, str, dict[str, object]]] = []
        restarts: list[str] = []
        written: list[dict[str, object]] = []
        status = {
            "failures": [],
            "stack_failures": [],
            "miner_failures": [],
            "mining_address": ADDRESS,
            "nodes": {},
            "sync_health": {},
            "sync_progress": {"status": "synced", "remaining_blocks": 0, "nodes": {}},
            "pool_health": {
                "expired_job_reconnect_failed_no_share": True,
                "expired_job_reconnect_count": 14,
                "expired_job_reauthorize_after_reconnect_count": 14,
                "expired_job_client_timeout_after_reconnect_count": 1,
                "expired_job_client_timeout_last_at": "2026-06-03T01:08:08",
                "expired_job_client_timeout_last_line": "2026/06/03 01:08:08 [192.168.1.16:33726] read error: i/o timeout",
                "stale_submit_count": 180,
                "valid_share_count": 0,
            },
            "miner_health": {
                "connected_count": 0,
                "connected_count_effective": 0,
                "miners": [],
            },
        }

        def record(event_type: str, severity: str, message: str, details=None) -> None:
            events.append((event_type, severity, message, details or {}))

        with mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", side_effect=record
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_pool_restart", side_effect=lambda reason: restarts.append(reason) or True
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("pool_expired_job_reconnect_exhausted", result["watchdog_state"]["last_status"])
        self.assertEqual(1, len(restarts))
        self.assertIn("pool expired-job reconnect exhausted", restarts[0])
        self.assertEqual("pool_expired_job_reconnect_exhausted", events[0][0])
        self.assertEqual("critical", events[0][1])
        self.assertTrue(events[0][3]["expired_job_reconnect_failed"])
        self.assertTrue(written)


if __name__ == "__main__":
    unittest.main()

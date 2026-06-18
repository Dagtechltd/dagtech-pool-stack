#!/usr/bin/env python3

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import watchdog  # noqa: E402
import pool_ops  # noqa: E402


def node_status(*, importing: bool, last_import_age_seconds: int, latest_block: int = 1000) -> dict[str, object]:
    return {
        "nodes": {
            "node": {
                "child_running": True,
                "importing": importing,
                "latest_block": latest_block,
                "last_import_age_seconds": last_import_age_seconds,
            }
        },
        "sync_progress": {
            "nodes": {
                "node": {
                    "current_block": latest_block,
                    "remaining_blocks": 100,
                    "status": "syncing",
                }
            }
        },
        "pool_health": {"initial_download": True},
        "sync_health": {"needs_fast_sync_repair": True},
    }


class WatchdogSyncRestartTests(unittest.TestCase):
    def test_active_import_requires_fresh_import_age_when_importing_flag_is_stuck(self) -> None:
        now = 1_779_200_000
        status = node_status(importing=True, last_import_age_seconds=700)
        state = {
            "last_sync_height_changed_at_by_node": {"node": now - 700},
        }

        with mock.patch.object(watchdog, "NODES", ["node"]):
            active = watchdog.active_sync_import_nodes(status, state=state, now=now, grace_seconds=300)

        self.assertEqual([], active)

    def test_active_import_allows_fresh_importing_node(self) -> None:
        now = 1_779_200_000
        status = node_status(importing=True, last_import_age_seconds=40)
        state = {
            "last_sync_height_changed_at_by_node": {"node": now - 700},
        }

        with mock.patch.object(watchdog, "NODES", ["node"]):
            active = watchdog.active_sync_import_nodes(status, state=state, now=now, grace_seconds=300)

        self.assertEqual(["node"], active)

    def test_sync_restart_not_suppressed_for_stale_importing_node(self) -> None:
        now = 1_779_200_000
        status = node_status(importing=True, last_import_age_seconds=700)
        state = {
            "last_sync_height_changed_at_by_node": {"node": now - 700},
        }

        with mock.patch.object(watchdog, "NODES", ["node"]):
            suppressed = watchdog.suppress_sync_restart_for_active_import(
                status,
                state,
                "node has not imported a block for 700s; waiting for node sync",
                "node",
            )

        self.assertFalse(suppressed)

    def test_active_import_suppression_does_not_consume_repair_cooldown(self) -> None:
        now = 1_779_200_000
        status = node_status(importing=True, last_import_age_seconds=40)
        state = {
            "last_sync_height_changed_at_by_node": {"node": now - 700},
        }

        with mock.patch.object(watchdog, "NODES", ["node"]), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None):
            suppressed = watchdog.suppress_sync_restart_for_active_import(
                status,
                state,
                "waiting for node sync",
                "node",
            )

        self.assertTrue(suppressed)
        self.assertNotIn("last_sync_repair_at", state)
        self.assertIn("last_sync_repair_suppressed_epoch", state)

    def test_check_once_active_import_suppression_does_not_consume_repair_cooldown(self) -> None:
        now = 1_779_200_000
        status = {
            **node_status(importing=True, last_import_age_seconds=40),
            "failures": [],
            "stack_failures": [],
            "miner_failures": [],
            "warnings": ["pool is waiting for node sync to finish"],
            "overall": "syncing",
            "mining_address": "0x1111111111111111111111111111111111111111",
            "pool_health": {"initial_download": True},
            "miner_health": {"connected_count": 0, "connected_count_effective": 0, "miners": []},
        }
        state = {
            "consecutive_syncing": 4,
            "last_sync_height_by_node": {"node": 1000},
            "last_sync_height_changed_at_by_node": {"node": now - 700},
        }
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
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
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=AssertionError("suppressed import should not restart")
        ), mock.patch.object(
            watchdog, "run_repair", side_effect=AssertionError("suppressed import should not restart the stack")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("syncing", result["watchdog_state"]["last_status"])
        self.assertNotIn("last_sync_repair_at", result["watchdog_state"])
        self.assertIn("last_sync_repair_suppressed_epoch", result["watchdog_state"])
        self.assertTrue(written)

    def test_check_once_does_not_start_pool_when_catchup_pause_stopped_it(self) -> None:
        now = 1_779_200_000
        pool_failure = f"{watchdog.POOL_CONTAINER} is not running"
        status = {
            **node_status(importing=True, last_import_age_seconds=20),
            "failures": [pool_failure],
            "stack_failures": [pool_failure],
            "miner_failures": [],
            "warnings": [],
            "overall": "syncing",
            "status_reason": "catch-up pause active: chain node is 90000 blocks behind peers",
            "catchup_policy": {"active": True},
            "sync_health": {"catchup_pause_active": True},
            "mining_address": "0x1111111111111111111111111111111111111111",
            "pool_health": {},
            "miner_health": {"connected_count": 4, "connected_count_effective": 4, "miners": []},
        }
        state: dict[str, object] = {}
        written: list[dict[str, object]] = []
        events: list[tuple[str, str, str]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
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
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", side_effect=lambda event_type, severity, *_args, **_kwargs: events.append((event_type, severity, ""))
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_repair", side_effect=AssertionError("catch-up containment must not start the pool")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("pool_start_blocked", result["watchdog_state"]["last_status"])
        self.assertEqual([], result["watchdog_state"]["last_failures"])
        self.assertIn("chain catch-up pause is active", result["watchdog_state"]["last_sync_warnings"][0])
        self.assertTrue(any(item[0] == "pool_start_blocked" for item in events))
        self.assertTrue(written)

    def test_check_once_leaves_running_pool_up_when_sync_progress_is_syncing(self) -> None:
        now = 1_779_200_000
        status = {
            **node_status(importing=True, last_import_age_seconds=20),
            "failures": [],
            "stack_failures": [],
            "miner_failures": [],
            "warnings": [],
            "mode": "mining",
            "overall": "ok",
            "containers": {
                watchdog.POOL_CONTAINER: {
                    "running": True,
                    "started_at": "2026-06-14T12:00:00.000000000Z",
                }
            },
            "pool_health": {},
            "miner_health": {"connected_count": 4, "connected_count_effective": 4, "miners": []},
        }
        status["sync_progress"]["status"] = "syncing"
        status["sync_progress"]["remaining_blocks"] = 90_000
        state: dict[str, object] = {}
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
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
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_repair", side_effect=AssertionError("sync containment must not restart the stack")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("pool_sync_template_pause", result["watchdog_state"]["last_status"])
        self.assertEqual(
            ["sync progress is syncing with 90000 block(s) remaining"],
            result["watchdog_state"]["last_sync_warnings"],
        )
        self.assertEqual("sync progress is syncing with 90000 block(s) remaining", written[-1]["last_pool_sync_pause_reason"])

    def test_check_once_does_not_restart_pool_when_nested_node_syncing(self) -> None:
        now = 1_779_200_000
        status = {
            **node_status(importing=True, last_import_age_seconds=20),
            "failures": [],
            "stack_failures": [],
            "miner_failures": [],
            "warnings": ["pool is waiting for node sync to finish"],
            "mode": "mining",
            "overall": "ok",
            "containers": {
                watchdog.POOL_CONTAINER: {
                    "running": True,
                    "started_at": "2026-01-14T12:00:00.000000000Z",
                }
            },
            "pool_health": {
                "initial_download": True,
                "pool_template_frozen": True,
                "template_freeze_age_seconds": 600,
            },
            "miner_health": {"connected_count": 4, "connected_count_effective": 4, "miners": []},
        }
        state: dict[str, object] = {
            "consecutive_share_stalls": watchdog.DEFAULT_SHARE_STALL_THRESHOLD - 1,
        }
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
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
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_pool_restart", side_effect=AssertionError("sync mode must not restart the pool")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("pool_sync_template_pause", result["watchdog_state"]["last_status"])
        self.assertIn("node sync progress is syncing", result["watchdog_state"]["last_sync_warnings"][0])
        self.assertTrue(written)

    def test_targeted_node_restart_uses_runtime_container_name(self) -> None:
        commands: list[list[str]] = []

        class Result:
            ok = True

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(
            watchdog, "automation_mutation_allowed", return_value=True
        ), mock.patch.object(
            watchdog, "acquire_lock", return_value=mock.Mock(close=lambda: None)
        ), mock.patch.object(
            watchdog, "action_log_path", return_value=pathlib.Path(tmpdir) / "restart.log"
        ), mock.patch.object(
            watchdog, "write_action_state", lambda _payload: None
        ), mock.patch.object(
            watchdog, "record_failed_repair", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_logged", side_effect=lambda command, *_args, **_kwargs: commands.append(command) or Result()
        ):
            ok = watchdog.run_node_restart("node", "unit test")

        self.assertTrue(ok)
        self.assertEqual([["docker", "restart", "node"]], commands)

    def test_node_log_marks_missing_dag_tip_as_critical_repairable_damage(self) -> None:
        parsed = pool_ops.parse_node_log(
            "\n".join(
                [
                    "2026-06-04|17:34:18.911 [INFO ] Loading dag ... module=CHAIN",
                    "2026-06-04|17:34:18.911 [ERROR] The dag data was damaged (Can't find tip:10089356). you can cleanup your block data base by '--cleanup'.",
                ]
            )
        )

        self.assertTrue(parsed["critical"])
        self.assertTrue(parsed["dag_tip_damage"])
        self.assertIn("Can't find tip:10089356", parsed["dag_tip_damage_lines"][0])

    def test_node_dag_tip_cleanup_runs_narrow_cleanuptips_repair(self) -> None:
        commands: list[list[str]] = []

        class Result:
            ok = True

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(
            watchdog, "automation_mutation_allowed", return_value=True
        ), mock.patch.object(
            watchdog, "acquire_lock", return_value=mock.Mock(close=lambda: None)
        ), mock.patch.object(
            watchdog, "action_log_path", return_value=pathlib.Path(tmpdir) / "cleanuptips.log"
        ), mock.patch.object(
            watchdog, "write_action_state", lambda _payload: None
        ), mock.patch.object(
            watchdog, "record_failed_repair", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_logged", side_effect=lambda command, *_args, **_kwargs: commands.append(command) or Result()
        ):
            ok = watchdog.run_node_dag_tip_cleanup("node", "unit test")

        self.assertTrue(ok)
        self.assertEqual("bash", commands[0][0])
        script = commands[0][2]
        self.assertIn("--cleanuptips", script)
        self.assertNotIn("--cleanup\n", script)
        self.assertIn("docker stop", script)
        self.assertIn("docker start", script)

    def test_check_once_repairs_missing_dag_tip_before_generic_restart(self) -> None:
        now = 1_779_200_000
        status = {
            "failures": [
                "node wrapper is up but bdag child is not running",
                "node has critical log entries",
            ],
            "stack_failures": [
                "node wrapper is up but bdag child is not running",
                "node has critical log entries",
            ],
            "miner_failures": [],
            "warnings": [],
            "overall": "down",
            "mining_address": "0x1111111111111111111111111111111111111111",
            "pool_health": {},
            "miner_health": {"connected_count": 0, "connected_count_effective": 0, "miners": []},
            "nodes": {
                "node": {
                    "child_running": False,
                    "dag_tip_damage": True,
                    "dag_tip_damage_lines": ["The dag data was damaged (Can't find tip:10089356)."],
                }
            },
            "sync_progress": {"nodes": {}},
        }
        state: dict[str, object] = {}
        cleanup_calls: list[tuple[str, str]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", lambda _payload: None
        ), mock.patch.object(watchdog, "collect_stack_status", return_value=status), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(watchdog, "record_earnings_snapshot", return_value={}), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(watchdog, "log", lambda _message: None), mock.patch.object(
            watchdog,
            "run_node_dag_tip_cleanup",
            side_effect=lambda node, reason: cleanup_calls.append((node, reason)) or True,
        ), mock.patch.object(
            watchdog, "run_repair", side_effect=AssertionError("missing DAG tip should use cleanuptips first")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual([("node", cleanup_calls[0][1])], cleanup_calls)
        self.assertIn("--cleanuptips", cleanup_calls[0][1])
        self.assertEqual(0, result["watchdog_state"]["consecutive_failures"])
        self.assertEqual({"node": now}, result["watchdog_state"]["last_node_dag_tip_cleanup_at_by_node"])

    def test_legacy_single_node_watchdog_skips_pool_restart_when_node_syncing(self) -> None:
        script = pathlib.Path("scripts/bdag-single-node-watchdog.sh").resolve()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            root = tmp / "root"
            root.mkdir()
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            restart_marker = tmp / "pool-restart-called"

            (fake_bin / "docker").write_text(
                """#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  info)
    exit 0
    ;;
  inspect)
    template="${3:-}"
    if [[ "$template" == *State.Status* ]]; then
      printf 'running\\n'
    else
      printf '\\n'
    fi
    ;;
  logs)
    container="${@: -1}"
    if [[ "$container" == "bdagminer-pool-1" ]]; then
      for _ in {1..30}; do
        printf 'Submit Error not found in acceptedJobs Expired\\n'
      done
      printf 'pool is waiting for node sync to finish\\n'
    else
      printf 'Client in initial download\\n'
    fi
    ;;
  compose)
    printf 'restart called\\n' >> "$BDAG_SINGLE_NODE_WATCHDOG_RESTART_MARKER"
    ;;
esac
""",
                encoding="utf-8",
            )
            (fake_bin / "date").write_text(
                """#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  --iso-8601=seconds)
    printf '2026-06-17T12:00:00+00:00\\n'
    ;;
  +%s)
    printf '1779200000\\n'
    ;;
  +%Y%m%d-%H%M%S)
    printf '20260617-120000\\n'
    ;;
  *)
    /bin/date "$@"
    ;;
esac
""",
                encoding="utf-8",
            )
            (fake_bin / "flock").write_text(
                """#!/usr/bin/env bash
exit 0
""",
                encoding="utf-8",
            )
            for fake in fake_bin.iterdir():
                fake.chmod(0o755)

            env = {
                **os.environ,
                "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                "BDAG_SINGLE_NODE_WATCHDOG_ROOT": str(root),
                "BDAG_SINGLE_NODE_WATCHDOG_STATE_DIR": str(tmp / "state"),
                "BDAG_SINGLE_NODE_WATCHDOG_LOCK_FILE": str(tmp / "watchdog.lock"),
                "BDAG_SINGLE_NODE_WATCHDOG_RESTART_MARKER": str(restart_marker),
            }
            result = subprocess.run(
                ["bash", str(script)],
                cwd=pathlib.Path(__file__).resolve().parents[2],
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )

            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            self.assertFalse(restart_marker.exists())
            log = (root / "logs" / "single-node-watchdog.log").read_text(encoding="utf-8")
            self.assertIn("node sync mode active; leaving pool running", log)


if __name__ == "__main__":
    unittest.main()

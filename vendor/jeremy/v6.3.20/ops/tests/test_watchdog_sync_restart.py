#!/usr/bin/env python3

from __future__ import annotations

import pathlib
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
        "sync_health": {"needs_chain_sync_repair": True},
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

    def test_check_once_suppresses_repairs_while_chain_state_self_heal_is_active(self) -> None:
        now = 1_779_200_000
        status = {
            **node_status(importing=False, last_import_age_seconds=999),
            "failures": ["stack-node-1 is not running", f"{watchdog.POOL_CONTAINER} is not running"],
            "stack_failures": ["stack-node-1 is not running", f"{watchdog.POOL_CONTAINER} is not running"],
            "miner_failures": [],
            "warnings": [],
            "overall": "down",
            "mining_address": "0x1111111111111111111111111111111111111111",
            "pool_health": {},
            "miner_health": {"connected_count": 0, "connected_count_effective": 0, "miners": []},
        }
        state: dict[str, object] = {}
        written: list[dict[str, object]] = []
        restore_state = {"status": "started", "reason": "chain-state restore started"}

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "read_state", return_value=state
        ), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_status_cached", return_value=status
        ), mock.patch.object(
            watchdog,
            "lock_is_held",
            side_effect=lambda path: path == watchdog.CHAIN_STATE_SELF_HEAL_LOCK_FILE,
        ), mock.patch.object(
            watchdog, "read_chain_state_self_heal_state", return_value=restore_state
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_repair", side_effect=AssertionError("self-heal active must suppress stack repair")
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=AssertionError("self-heal active must suppress node restart")
        ), mock.patch.object(
            watchdog, "run_pool_restart", side_effect=AssertionError("self-heal active must suppress pool restart")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("chain_state_restore_active", result["watchdog_state"]["last_status"])
        self.assertEqual([], result["watchdog_state"]["last_failures"])
        self.assertEqual(restore_state, result["watchdog_state"]["chain_state_self_heal"])
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
            watchdog, "compose_container_name", return_value="stack-node-1"
        ), mock.patch.object(
            watchdog, "run_logged", side_effect=lambda command, *_args, **_kwargs: commands.append(command) or Result()
        ):
            ok = watchdog.run_node_restart("node", "unit test")

        self.assertTrue(ok)
        self.assertEqual([["docker", "restart", "stack-node-1"]], commands)

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
            watchdog, "compose_container_name", return_value="stack-node-1"
        ), mock.patch.object(
            watchdog, "run_logged", side_effect=lambda command, *_args, **_kwargs: commands.append(command) or Result()
        ):
            ok = watchdog.run_node_dag_tip_cleanup("node", "unit test")

        self.assertTrue(ok)
        self.assertEqual("bash", commands[0][0])
        script = commands[0][2]
        self.assertIn("--cleanuptips", script)
        self.assertNotIn("--cleanup\n", script)
        self.assertIn("node=stack-node-1", script)
        self.assertIn("docker stop", script)
        self.assertIn("docker start", script)

    def test_check_once_repairs_confirmed_empty_block_storm_with_cleanuptips(self) -> None:
        now = 1_779_200_000
        status = {
            "failures": [],
            "stack_failures": [],
            "miner_failures": [],
            "warnings": ["node is logging repeated DAG empty-block lookups"],
            "sync_warnings": ["node is logging repeated DAG empty-block lookups"],
            "overall": "syncing",
            "mining_address": "0x1111111111111111111111111111111111111111",
            "pool_health": {},
            "miner_health": {"connected_count": 0, "connected_count_effective": 0, "miners": []},
            "nodes": {
                "node": {
                    "child_running": True,
                    "dag_empty_block_storm": True,
                    "dag_empty_block_warnings": 80,
                    "dag_empty_block_lines": ["get block module=DAG error=empty blockID=8564000"],
                }
            },
            "sync_health": {
                "needs_chain_sync_repair": True,
                "dag_empty_block_storm": True,
                "dag_empty_block_storm_nodes": {"node": {"warnings": 80}},
            },
            "sync_progress": {
                "nodes": {"node": {"current_block": 10_658_989, "remaining_blocks": 2289, "status": "syncing"}}
            },
        }
        state: dict[str, object] = {"consecutive_dag_empty_block_storm": 1}
        cleanup_calls: list[tuple[str, str]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", lambda _payload: None
        ), mock.patch.object(watchdog, "collect_status_cached", return_value=status), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(watchdog, "record_earnings_snapshot", return_value={}), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(watchdog, "log", lambda _message: None), mock.patch.object(
            watchdog,
            "run_node_dag_tip_cleanup",
            side_effect=lambda node, reason: cleanup_calls.append((node, reason)) or True,
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=AssertionError("empty-block storm should clean tips first")
        ), mock.patch.object(
            watchdog, "run_repair", side_effect=AssertionError("empty-block storm should not use stack repair")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual([("node", cleanup_calls[0][1])], cleanup_calls)
        self.assertIn("DAG empty-block storm", cleanup_calls[0][1])
        self.assertEqual(0, result["watchdog_state"]["consecutive_dag_empty_block_storm"])

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


if __name__ == "__main__":
    unittest.main()

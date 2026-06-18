#!/usr/bin/env python3

import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timezone

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


class NoMinerCollectStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            name: getattr(pool_ops, name)
            for name in (
                "NODES",
                "OBSERVER_NODES",
                "SERVICES",
                "STACK_SERVICES",
                "POOL_CONTAINER",
                "POOL_CONTAINERS",
                "ensure_runtime",
                "docker_access_error",
                "local_ipv4_addresses",
                "default_miner_pool_settings",
                "run",
                "read_latest_action",
                "discover_observer_node_services",
                "docker_inspect",
                "docker_top",
                "docker_logs",
                "docker_logs_many",
                "parse_pool_log",
                "collect_template_probe_health",
                "collect_pool_prometheus_metrics",
                "collect_miner_health",
                "collect_sync_progress",
                "observe_sync_progress_health",
                "read_sync_coordinator_state",
                "collect_host_pressure",
            )
        }
        self.old_time = pool_ops.time.time
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        for name, value in self.originals.items():
            setattr(pool_ops, name, value)
        pool_ops.time.time = self.old_time

    def test_node_log_irreparable_sync_block_is_chain_state_blocker(self) -> None:
        log = "\n".join(
            [
                "2026-06-05|05:49:27.543 [INFO ] Syncing graph state module=SYNC cur=(10033359,7876980,7885776,10106135,1) target=(10051752,7882760,7891667,10089568,2)",
                "2026-06-05|05:49:29.401 [ERROR] Not DAG block:0x96189eff2f19849e6b8cb34f207718bf4603a28489a50df85931f1768fb048be module=DAG",
                "2026-06-05|05:49:29.401 [ERROR] Failed to process block:hash=0x96189eff2f19849e6b8cb34f207718bf4603a28489a50df85931f1768fb048be err=Irreparable error![0x96189eff2f19849e6b8cb34f207718bf4603a28489a50df85931f1768fb048be] module=SYNC processID=1",
                "2026-06-05|05:49:31.515 [WARN ] Served eth_getBalance err=\"missing trie node b48ba7aceb59341ed40c9cd5d086f06d6ebc73213392efa8e94b885c1e9a9481 (path ) state 0xb48ba7aceb59341ed40c9cd5d086f06d6ebc73213392efa8e94b885c1e9a9481 is not available\"",
            ]
        )

        parsed = pool_ops.parse_node_log(log)

        self.assertTrue(parsed["chain_state_blocker"])
        self.assertEqual(
            parsed["chain_state_blocker_hash"],
            "0x96189eff2f19849e6b8cb34f207718bf4603a28489a50df85931f1768fb048be",
        )
        self.assertTrue(parsed["critical"])
        self.assertEqual(parsed["missing_trie_node_warnings"], 1)

    def test_node_log_marks_busy_syncing_template_block(self) -> None:
        parsed = pool_ops.parse_node_log(
            "\n".join(
                [
                    "2026-06-09|19:45:59.592 [WARN ] BdagPool template update failed after chain head module=BDAG err=\"node busy syncing\"",
                    "2026-06-09|19:45:59.602 [WARN ] BdagPool template update failed after chain head module=BDAG err=\"node busy syncing\"",
                ]
            )
        )

        self.assertTrue(parsed["node_busy_syncing"])
        self.assertEqual(2, len(parsed["node_busy_syncing_lines"]))
        self.assertIn("node busy syncing", parsed["node_busy_syncing_lines"][0].lower())

    def test_no_miner_status_suppresses_template_and_rpc_noise(self) -> None:
        now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        pool_ops.time.time = lambda: now
        pool_ops.NODES = ["node"]
        pool_ops.OBSERVER_NODES = []
        pool_ops.STACK_SERVICES = ["postgres", "node", "asic-pool", "asic-pool"]
        pool_ops.SERVICES = list(pool_ops.STACK_SERVICES)
        pool_ops.POOL_CONTAINER = "asic-pool"
        pool_ops.POOL_CONTAINERS = ["asic-pool"]
        pool_ops.ensure_runtime = lambda: None
        pool_ops.docker_access_error = lambda: None
        pool_ops.local_ipv4_addresses = lambda: ["192.168.68.55"]
        pool_ops.default_miner_pool_settings = lambda: {
            "pool_url": "stratum+tcp://192.168.68.55:3334",
            "worker_user": "0x0000000000000000000000000000000000000000",
            "pool_password": "1234",
        }
        pool_ops.run = lambda command, timeout=20: pool_ops.CommandResult(command, 0, "", "", 0.0)
        pool_ops.read_latest_action = lambda: None
        pool_ops.discover_observer_node_services = lambda: []
        pool_ops.docker_top = lambda _name: (
            "UID PID PPID C STIME TTY TIME CMD\n"
            "root 1 0 0 12:00 ? 00:00:01 /usr/local/bin/bdag\n"
        )
        pool_ops.docker_logs = lambda _name, lines=160: ""
        pool_ops.docker_logs_many = lambda _names, lines=160: (
            "2026/05/25 11:59:30 GBT ERROR: connect: connection refused\n"
        )
        pool_ops.collect_host_pressure = lambda: {
            "iowait_percent": 10.0,
            "iowait_warning_active": False,
            "samples": [],
        }

        def fake_inspect(names):
            return {
                name: {
                    "name": name,
                    "image": "test",
                    "running": True,
                    "status": "running",
                    "restart_count": 0,
                    "exit_code": 0,
                    "error": "",
                    "ports": {},
                }
                for name in names
            }

        pool_ops.docker_inspect = fake_inspect
        def fail_if_template_probe_runs():
            raise AssertionError("no-miner status collection must not run live mining template probes")

        pool_ops.collect_template_probe_health = fail_if_template_probe_runs
        pool_ops.collect_pool_prometheus_metrics = lambda _containers: {
            "generated_at": "2026-05-25T12:00:00+0000",
            "status": "ok",
            "active_connections": 0,
            "selected_backend": "node",
            "source_job_health": {},
            "source_backend_health": {},
            "selected_backend_source_health": {},
            "template_conversion_stall": {},
            "loss_ledger": {},
        }
        pool_ops.collect_miner_health = lambda: {
            "managed_count": 0,
            "connected_count": 0,
            "failures": [],
            "warnings": [],
            "miners": [],
        }
        pool_ops.collect_sync_progress = lambda: {
            "status": "synced",
            "percent": 100.0,
            "current_block": 8_658_598,
            "highest_block": 8_658_598,
            "remaining_blocks": 0,
            "source": "nodes",
            "error": "",
            "nodes": {
                "node": {
                    "status": "synced",
                    "percent": 100.0,
                    "current_block": 8_658_598,
                    "highest_block": 8_658_598,
                    "remaining_blocks": 0,
                    "source": "node",
                    "error": "",
                    "chain_block_count": 8_658_598,
                    "chain_main_height": 7_001_831,
                    "chain_rpc_source": "getBlockCount",
                    "chain_rpc_latency_ms": 3.3,
                    "chain_rpc_attempts": 1,
                    "chain_rpc_retry_limit": 2,
                    "chain_rpc_error": "",
                }
            },
        }
        pool_ops.observe_sync_progress_health = lambda _sync_progress: {
            "active_nodes": [],
            "active_node_count": 0,
            "node_rates_blocks_per_second": {},
            "lookback_seconds": 2700,
        }
        pool_ops.read_sync_coordinator_state = lambda: {}

        status = pool_ops.collect_status(include_logs=True)

        self.assertEqual(status["overall"], "ok")
        self.assertEqual(status["mode"], "ready_no_miners")
        self.assertFalse(status["pool_health"]["needs_fast_repair"])
        self.assertFalse(status["pool_health"]["rpc_refused"])
        self.assertTrue(status["pool_health"]["rpc_refused_raw"])
        self.assertTrue(status["rpc_template_health"]["suppressed_for_no_miners"])
        self.assertEqual(status["rpc_template_health"]["suppressed_reason"], "no managed or connected miners")
        self.assertEqual(status["nodes"]["node"]["template_probe_sample_count"], 0)
        self.assertFalse(status["nodes"]["node"]["template_probe_failing"])
        self.assertEqual(status["sync_warnings"], [])
        joined_warnings = "\n".join(status["warnings"])
        self.assertNotIn("live mining template probes", joined_warnings)
        self.assertNotIn("pool recently saw RPC connection refused", joined_warnings)

    def test_no_miner_status_promotes_busy_syncing_to_syncing(self) -> None:
        now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        pool_ops.time.time = lambda: now
        pool_ops.NODES = ["node"]
        pool_ops.OBSERVER_NODES = []
        pool_ops.STACK_SERVICES = ["postgres", "node", "asic-pool", "asic-pool"]
        pool_ops.SERVICES = list(pool_ops.STACK_SERVICES)
        pool_ops.POOL_CONTAINER = "asic-pool"
        pool_ops.POOL_CONTAINERS = ["asic-pool"]
        pool_ops.ensure_runtime = lambda: None
        pool_ops.docker_access_error = lambda: None
        pool_ops.local_ipv4_addresses = lambda: ["192.168.68.55"]
        pool_ops.default_miner_pool_settings = lambda: {
            "pool_url": "stratum+tcp://192.168.68.55:3334",
            "worker_user": "0x0000000000000000000000000000000000000000",
            "pool_password": "1234",
        }
        pool_ops.run = lambda command, timeout=20: pool_ops.CommandResult(command, 0, "", "", 0.0)
        pool_ops.read_latest_action = lambda: None
        pool_ops.discover_observer_node_services = lambda: []
        pool_ops.docker_top = lambda _name: (
            "UID PID PPID C STIME TTY TIME CMD\n"
            "root 1 0 0 12:00 ? 00:00:01 /usr/local/bin/bdag\n"
        )
        pool_ops.docker_logs = lambda _name, lines=160: (
            "2026-06-09|19:45:59.592 [WARN ] BdagPool template update failed after chain head module=BDAG err=\"node busy syncing\"\n"
        )
        pool_ops.docker_logs_many = lambda _names, lines=160: (
            "2026/05/25 11:59:30 GBT ERROR: connect: connection refused\n"
        )
        pool_ops.collect_host_pressure = lambda: {
            "iowait_percent": 10.0,
            "iowait_warning_active": False,
            "samples": [],
        }

        def fake_inspect(names):
            return {
                name: {
                    "name": name,
                    "image": "test",
                    "running": True,
                    "status": "running",
                    "restart_count": 0,
                    "exit_code": 0,
                    "error": "",
                    "ports": {},
                }
                for name in names
            }

        pool_ops.docker_inspect = fake_inspect
        pool_ops.collect_template_probe_health = lambda: {
            "generated_at": "2026-05-25T12:00:00+0000",
            "cached": False,
            "suppressed_for_no_miners": True,
            "suppressed_reason": "no managed or connected miners",
            "nodes": {},
            "failing_nodes": [],
            "all_nodes_failing": False,
        }
        pool_ops.collect_pool_prometheus_metrics = lambda _containers: {
            "generated_at": "2026-05-25T12:00:00+0000",
            "status": "ok",
            "active_connections": 0,
            "selected_backend": "node",
            "source_job_health": {"ok": True},
            "source_backend_health": {},
            "selected_backend_source_health": {},
            "template_conversion_stall": {},
            "loss_ledger": {},
        }
        pool_ops.collect_miner_health = lambda: {
            "managed_count": 0,
            "connected_count": 0,
            "failures": [],
            "warnings": [],
            "miners": [],
        }
        pool_ops.collect_sync_progress = lambda: {
            "status": "synced",
            "percent": 100.0,
            "current_block": 8_658_598,
            "highest_block": 8_658_598,
            "remaining_blocks": 0,
            "source": "nodes",
            "error": "",
            "nodes": {
                "node": {
                    "status": "synced",
                    "percent": 100.0,
                    "current_block": 8_658_598,
                    "highest_block": 8_658_598,
                    "remaining_blocks": 0,
                    "source": "node",
                    "error": "",
                    "chain_block_count": 8_658_598,
                    "chain_main_height": 7_001_831,
                    "chain_rpc_source": "getBlockCount",
                    "chain_rpc_latency_ms": 3.3,
                    "chain_rpc_attempts": 1,
                    "chain_rpc_retry_limit": 2,
                    "chain_rpc_error": "",
                }
            },
        }
        pool_ops.observe_sync_progress_health = lambda _sync_progress: {
            "active_nodes": [],
            "active_node_count": 0,
            "node_rates_blocks_per_second": {},
            "lookback_seconds": 2700,
        }
        pool_ops.read_sync_coordinator_state = lambda: {}

        status = pool_ops.collect_status(include_logs=True)

        self.assertEqual(status["overall"], "syncing")
        self.assertEqual(status["sync_progress"]["status"], "syncing")
        self.assertEqual(status["sync_progress"]["error"], "node busy syncing")
        self.assertTrue(status["sync_warnings"])
        self.assertIn("node busy syncing", "\n".join(status["sync_warnings"]).lower())
        self.assertTrue(status["nodes"]["node"]["node_busy_syncing"])

    def test_no_miner_status_promotes_active_imports_to_syncing(self) -> None:
        now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        pool_ops.time.time = lambda: now
        pool_ops.NODES = ["node"]
        pool_ops.OBSERVER_NODES = []
        pool_ops.STACK_SERVICES = ["postgres", "node", "asic-pool", "asic-pool"]
        pool_ops.SERVICES = list(pool_ops.STACK_SERVICES)
        pool_ops.POOL_CONTAINER = "asic-pool"
        pool_ops.POOL_CONTAINERS = ["asic-pool"]
        pool_ops.ensure_runtime = lambda: None
        pool_ops.docker_access_error = lambda: None
        pool_ops.local_ipv4_addresses = lambda: ["192.168.68.55"]
        pool_ops.default_miner_pool_settings = lambda: {
            "pool_url": "stratum+tcp://192.168.68.55:3334",
            "worker_user": "0x0000000000000000000000000000000000000000",
            "pool_password": "1234",
        }
        pool_ops.run = lambda command, timeout=20: pool_ops.CommandResult(command, 0, "", "", 0.0)
        pool_ops.read_latest_action = lambda: None
        pool_ops.discover_observer_node_services = lambda: []
        pool_ops.docker_top = lambda _name: (
            "UID PID PPID C STIME TTY TIME CMD\n"
            "root 1 0 0 12:00 ? 00:00:01 /usr/local/bin/bdag\n"
        )
        pool_ops.docker_logs = lambda _name, lines=160: (
            "2026-06-09|19:45:59.658 [INFO ] Imported new chain segment           number=614,336 hash=a79e65..270c55 blocks=1 txs=0   elapsed=8.117ms\n"
        )
        pool_ops.docker_logs_many = lambda _names, lines=160: (
            "2026/05/25 11:59:30 GBT ERROR: connect: connection refused\n"
        )
        pool_ops.collect_host_pressure = lambda: {
            "iowait_percent": 10.0,
            "iowait_warning_active": False,
            "samples": [],
        }

        def fake_inspect(names):
            return {
                name: {
                    "name": name,
                    "image": "test",
                    "running": True,
                    "status": "running",
                    "restart_count": 0,
                    "exit_code": 0,
                    "error": "",
                    "ports": {},
                }
                for name in names
            }

        pool_ops.docker_inspect = fake_inspect
        pool_ops.collect_template_probe_health = lambda: {
            "generated_at": "2026-05-25T12:00:00+0000",
            "cached": False,
            "suppressed_for_no_miners": True,
            "suppressed_reason": "no managed or connected miners",
            "nodes": {},
            "failing_nodes": [],
            "all_nodes_failing": False,
        }
        pool_ops.collect_pool_prometheus_metrics = lambda _containers: {
            "generated_at": "2026-05-25T12:00:00+0000",
            "status": "ok",
            "active_connections": 0,
            "selected_backend": "node",
            "source_job_health": {"ok": True},
            "source_backend_health": {},
            "selected_backend_source_health": {},
            "template_conversion_stall": {},
            "loss_ledger": {},
        }
        pool_ops.collect_miner_health = lambda: {
            "managed_count": 0,
            "connected_count": 0,
            "failures": [],
            "warnings": [],
            "miners": [],
        }
        pool_ops.collect_sync_progress = lambda: {
            "status": "synced",
            "percent": 100.0,
            "current_block": 8_658_598,
            "highest_block": 8_658_598,
            "remaining_blocks": 0,
            "source": "nodes",
            "error": "",
            "nodes": {
                "node": {
                    "status": "synced",
                    "percent": 100.0,
                    "current_block": 8_658_598,
                    "highest_block": 8_658_598,
                    "remaining_blocks": 0,
                    "source": "node",
                    "error": "",
                    "chain_block_count": 8_658_598,
                    "chain_main_height": 7_001_831,
                    "chain_rpc_source": "getBlockCount",
                    "chain_rpc_latency_ms": 3.3,
                    "chain_rpc_attempts": 1,
                    "chain_rpc_retry_limit": 2,
                    "chain_rpc_error": "",
                }
            },
        }
        pool_ops.observe_sync_progress_health = lambda _sync_progress: {
            "active_nodes": ["node"],
            "active_node_count": 1,
            "node_rates_blocks_per_second": {"node": 2.0},
            "lookback_seconds": 2700,
        }
        pool_ops.read_sync_coordinator_state = lambda: {}

        status = pool_ops.collect_status(include_logs=True)

        self.assertEqual(status["overall"], "syncing")
        self.assertEqual(status["sync_progress"]["status"], "syncing")
        self.assertEqual(status["sync_progress"]["source"], "nodes:importing")
        self.assertEqual(status["sync_progress"]["error"], "node importing blocks")
        self.assertTrue(status["sync_health"]["node_importing"])
        self.assertTrue(status["nodes"]["node"]["importing"])

    def test_pool_log_detects_failed_expired_job_reconnect(self) -> None:
        log = "\n".join(
            [
                "2026/06/03 00:57:53 [RECOVERY] resending current job to 192.168.1.16:33654 after 10 expired job rejects (job=103884-abc)",
                "2026/06/03 00:58:08 [RECOVERY] reconnecting stale miner 192.168.1.16:33654 after 3 expired-job recovery attempts (job=103899-abc)",
                "2026/06/03 00:58:08 [REFRESH] expired job client reconnect (seq=103900 parent=abc)",
                "2026/06/03 00:58:08 [192.168.1.16:33726] authorize accepted user=0x05518E03e148C56e426ff9e1CBdB962B4FC5250A",
                "2026/06/03 00:59:09 [vardiff] 192.168.1.16:33726 increase pdiff 0.000000 -> 0.050000 (shares=0 expired=0 in 60s, target=20/60s)",
                "2026/06/03 01:08:08 [192.168.1.16:33726] read error: read tcp 172.18.0.4:3334->192.168.1.16:33726: i/o timeout",
            ]
        )

        pool = pool_ops.parse_pool_log(log)

        self.assertTrue(pool["expired_job_reconnect_failed_no_share"])
        self.assertEqual(1, pool["expired_job_reconnect_count"])
        self.assertEqual(1, pool["expired_job_reauthorize_after_reconnect_count"])
        self.assertEqual(1, pool["expired_job_client_timeout_after_reconnect_count"])
        self.assertIn("reauthorized", pool["expired_job_reconnect_failure_reason"])

    def test_recent_paid_work_keeps_template_probe_noise_advisory_during_sync_progress(self) -> None:
        now = datetime(2026, 5, 27, 8, 30, 0, tzinfo=timezone.utc).timestamp()
        pool_ops.time.time = lambda: now
        pool_ops.NODES = ["node"]
        pool_ops.OBSERVER_NODES = []
        pool_ops.STACK_SERVICES = ["postgres", "node", "asic-pool", "asic-pool"]
        pool_ops.SERVICES = list(pool_ops.STACK_SERVICES)
        pool_ops.POOL_CONTAINER = "asic-pool"
        pool_ops.POOL_CONTAINERS = ["asic-pool"]
        pool_ops.ensure_runtime = lambda: None
        pool_ops.docker_access_error = lambda: None
        pool_ops.local_ipv4_addresses = lambda: ["192.168.68.55"]
        pool_ops.default_miner_pool_settings = lambda: {
            "pool_url": "stratum+tcp://192.168.68.55:3334",
            "worker_user": "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc",
            "pool_password": "1234",
        }
        pool_ops.run = lambda command, timeout=20: pool_ops.CommandResult(command, 0, "", "", 0.0)
        pool_ops.read_latest_action = lambda: None
        pool_ops.discover_observer_node_services = lambda: []
        pool_ops.docker_top = lambda _name: (
            "UID PID PPID C STIME TTY TIME CMD\n"
            "root 1 0 0 12:00 ? 00:00:01 /usr/local/bin/bdag\n"
        )
        pool_ops.docker_logs = lambda _name, lines=160: ""
        pool_ops.docker_logs_many = lambda _names, lines=160: ""
        pool_ops.collect_host_pressure = lambda: {
            "iowait_percent": 10.0,
            "iowait_warning_active": False,
            "samples": [],
        }

        def fake_inspect(names):
            return {
                name: {
                    "name": name,
                    "image": "test",
                    "running": True,
                    "status": "running",
                    "restart_count": 0,
                    "exit_code": 0,
                    "error": "",
                    "ports": {},
                }
                for name in names
            }

        pool_ops.docker_inspect = fake_inspect
        base_pool = self.originals["parse_pool_log"]("")
        base_pool.update(
            {
                "initial_download": True,
                "submit_count": 1,
                "valid_share_count": 1,
                "block_submit_success_count": 1,
                "block_submit_failure_count": 0,
                "last_submit_age_seconds": 1,
                "last_valid_share_age_seconds": 1,
                "last_block_submit_age_seconds": 1,
                "share_stall": False,
                "job_stall": False,
                "pool_template_frozen": False,
                "duplicate_block_storm": False,
                "stale_job_candidate_storm": False,
                "block_submit_error_storm": False,
                "accepted_job_expired_storm": False,
                "block_submit_zero_success_storm": False,
            }
        )
        pool_ops.parse_pool_log = lambda _log: dict(base_pool)
        pool_ops.collect_template_probe_health = lambda: {
            "generated_at": "2026-05-27T08:30:00+0000",
            "cached": False,
            "nodes": {
                "node": {
                    "sample_count": 1,
                    "ok_count": 0,
                    "error_count": 1,
                    "failing": True,
                    "last_error": "timed out",
                    "benign_tx_template_error": False,
                    "benign_tx_throttle": False,
                }
            },
            "direct_rpc": {
                "sample_count": 1,
                "ok_count": 0,
                "error_count": 1,
                "failing": True,
                "last_error": "timed out",
            },
            "failing_nodes": ["node"],
            "all_nodes_failing": True,
        }
        pool_ops.collect_pool_prometheus_metrics = lambda _containers: {
            "generated_at": "2026-05-27T08:30:00+0000",
            "status": "ok",
            "active_connections": 1.0,
            "selected_backend": "node",
            "source_job_health": {"ok": True, "authorized_miners": 1, "ready_miners": 1},
            "source_backend_health": {},
            "selected_backend_source_health": {
                "healthy": True,
                "node_mineable": True,
                "node_submit_ready": True,
                "node_p2p_mining_fresh": True,
                "ws_connected": True,
            },
            "template_conversion_stall": {},
            "loss_ledger": {},
        }
        stale_lane_failure = (
            "stale-lane mac=38:1f:8d:fb:ea:fc observed_ip=192.168.1.103 "
            "ASIC API/health check is unreachable and no recent pool submissions were seen"
        )
        pool_ops.collect_miner_health = lambda: {
            "managed_count": 2,
            "connected_count": 0,
            "failures": [stale_lane_failure],
            "warnings": [],
            "miners": [],
        }
        pool_ops.collect_sync_progress = lambda: {
            "status": "syncing",
            "percent": 99.9,
            "current_block": 8_658_580,
            "highest_block": 8_658_598,
            "remaining_blocks": 18,
            "source": "nodes",
            "error": "",
            "nodes": {
                "node": {
                    "status": "syncing",
                    "percent": 99.9,
                    "current_block": 8_658_580,
                    "highest_block": 8_658_598,
                    "remaining_blocks": 18,
                    "source": "node",
                    "error": "",
                    "chain_block_count": 8_658_580,
                    "chain_main_height": 7_001_812,
                    "chain_rpc_source": "getBlockCount",
                    "chain_rpc_latency_ms": 3.3,
                    "chain_rpc_attempts": 1,
                    "chain_rpc_retry_limit": 2,
                    "chain_rpc_error": "",
                }
            },
        }
        pool_ops.observe_sync_progress_health = lambda _sync_progress: {
            "active_nodes": ["node"],
            "active_node_count": 1,
            "node_rates_blocks_per_second": {"node": 2.0},
            "lookback_seconds": 2700,
        }
        pool_ops.read_sync_coordinator_state = lambda: {}

        status = pool_ops.collect_status(include_logs=True)

        self.assertEqual(status["overall"], "ok")
        self.assertEqual(status["mode"], "mining")
        self.assertTrue(status["can_submit_blocks"])
        self.assertTrue(status["can_mine"])
        self.assertEqual(status["blocking_failures"], [])
        self.assertEqual(status["blocking_miner_failures"], [])
        self.assertEqual(status["miner_failures"], [stale_lane_failure])
        self.assertEqual(status["advisory_miner_failures"], [stale_lane_failure])
        self.assertEqual(status["sync_warnings"], [])
        self.assertFalse(status["pool_health"]["needs_fast_repair"])
        self.assertFalse(status["sync_health"]["needs_fast_sync_repair"])
        self.assertTrue(status["pool_health"]["node_template_probe_failing"])
        joined_maintenance = "\n".join(status["maintenance_warnings"])
        self.assertIn("accepted block submission remains fresh", joined_maintenance)
        self.assertIn("transient initial-download", joined_maintenance)
        self.assertIn("miner repair required but active mining continues", joined_maintenance)

    def test_miner_failures_block_when_no_active_mining_evidence(self) -> None:
        self.assertTrue(
            pool_ops.miner_failures_block_stack(
                ["miner is down"],
                connected_miners=0,
                pool_has_recent_share_activity=False,
                pool_has_recent_paid_work=False,
                source_job_health_ok=None,
            )
        )

    def test_miner_failures_are_advisory_with_active_mining_evidence(self) -> None:
        self.assertFalse(
            pool_ops.miner_failures_block_stack(
                ["one managed miner is down"],
                connected_miners=3,
                pool_has_recent_share_activity=True,
                pool_has_recent_paid_work=False,
                source_job_health_ok=False,
            )
        )


class EffectiveMinerDemandTests(unittest.TestCase):
    def test_pool_metrics_count_as_connected_miner_demand(self) -> None:
        count = pool_ops.effective_connected_miner_count(
            {"connected_count": 0},
            {"active_connections": 1.0},
            {"authorized_miners": 1, "ready_miners": 1},
        )

        self.assertEqual(count, 1)


class SyncProgressDisplayNodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_nodes = pool_ops.NODES
        self.addCleanup(self.restore_nodes)

    def restore_nodes(self) -> None:
        pool_ops.NODES = self.old_nodes

    def test_single_rpc_alias_is_displayed_as_managed_node(self) -> None:
        pool_ops.NODES = ["pool-stack-docker-node-1"]
        progress = {
            "status": "synced",
            "percent": 100.0,
            "current_block": 8_809_791,
            "highest_block": 8_809_791,
            "remaining_blocks": 0,
            "source": "nodes",
            "nodes": {
                "local-bdag": {
                    "status": "synced",
                    "percent": 100.0,
                    "current_block": 8_809_791,
                    "highest_block": 8_809_791,
                    "remaining_blocks": 0,
                    "source": "local-bdag",
                    "chain_block_count": 8_809_791,
                    "chain_rpc_source": "getBlockCount",
                    "chain_rpc_error": "",
                }
            },
        }

        aligned = pool_ops.sync_progress_for_display_nodes(progress, ["pool-stack-docker-node-1"])

        self.assertIn("pool-stack-docker-node-1", aligned["nodes"])
        self.assertNotIn("local-bdag", aligned["nodes"])
        node = aligned["nodes"]["pool-stack-docker-node-1"]
        self.assertEqual(node["source"], "pool-stack-docker-node-1")
        self.assertEqual(node["configured_source"], "local-bdag")
        self.assertEqual(node["chain_block_count"], 8_809_791)


class SharedStatusCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            name: getattr(pool_ops, name)
            for name in (
                "SHARED_STATUS_CACHE_FILE",
                "SHARED_STATUS_CACHE_ENABLED",
                "SHARED_STATUS_CACHE_SECONDS",
                "STATUS_SAMPLER_FILE",
                "STATUS_SAMPLER_ENABLED",
                "STATUS_SAMPLER_MAX_AGE_SECONDS",
                "STATUS_SAMPLER_BYPASS",
                "collect_status",
                "ensure_runtime",
            )
        }
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        for name, value in self.originals.items():
            setattr(pool_ops, name, value)

    def test_shared_status_cache_reuses_recent_status_sample(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            pool_ops.SHARED_STATUS_CACHE_FILE = pathlib.Path(tmp) / "shared-status-cache.json"
            pool_ops.STATUS_SAMPLER_FILE = pathlib.Path(tmp) / "status-sampler.json"
            pool_ops.SHARED_STATUS_CACHE_ENABLED = True
            pool_ops.SHARED_STATUS_CACHE_SECONDS = 60.0
            pool_ops.STATUS_SAMPLER_ENABLED = False
            pool_ops.ensure_runtime = lambda: None

            def fake_collect_status(include_logs=True):
                calls.append(include_logs)
                return {
                    "generated_at": "2026-05-25T12:00:00+0000",
                    "include_logs": include_logs,
                    "overall": "ok",
                }

            pool_ops.collect_status = fake_collect_status

            first = pool_ops.collect_status_cached(include_logs=True)
            second = pool_ops.collect_status_cached(include_logs=True)

            self.assertEqual(calls, [True])
            self.assertFalse(first["shared_status_cache"]["hit"])
            self.assertTrue(second["shared_status_cache"]["hit"])
            self.assertEqual(second["overall"], "ok")

    def test_status_sampler_reuses_recent_cross_process_sample(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            pool_ops.SHARED_STATUS_CACHE_FILE = pathlib.Path(tmp) / "shared-status-cache.json"
            pool_ops.STATUS_SAMPLER_FILE = pathlib.Path(tmp) / "status-sampler.json"
            pool_ops.SHARED_STATUS_CACHE_ENABLED = True
            pool_ops.SHARED_STATUS_CACHE_SECONDS = 3.0
            pool_ops.STATUS_SAMPLER_ENABLED = True
            pool_ops.STATUS_SAMPLER_MAX_AGE_SECONDS = 60.0
            pool_ops.STATUS_SAMPLER_BYPASS = False
            pool_ops.ensure_runtime = lambda: None
            pool_ops.write_status_sampler_payload(
                {
                    "generated_at": "2026-05-25T12:00:00+0000",
                    "overall": "ok",
                    "age_seconds": 0,
                    "stale_after_seconds": 30,
                },
                include_logs=True,
            )

            def fake_collect_status(include_logs=True):
                calls.append(include_logs)
                return {"overall": "down"}

            pool_ops.collect_status = fake_collect_status

            status = pool_ops.collect_status_cached(include_logs=True)

            self.assertEqual(calls, [])
            self.assertEqual(status["overall"], "ok")
            self.assertTrue(status["status_sampler"]["hit"])
            self.assertEqual(status["status_sampler"]["requested_include_logs"], True)

    def test_zero_max_age_bypasses_status_sampler(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            pool_ops.SHARED_STATUS_CACHE_FILE = pathlib.Path(tmp) / "shared-status-cache.json"
            pool_ops.STATUS_SAMPLER_FILE = pathlib.Path(tmp) / "status-sampler.json"
            pool_ops.SHARED_STATUS_CACHE_ENABLED = True
            pool_ops.STATUS_SAMPLER_ENABLED = True
            pool_ops.STATUS_SAMPLER_MAX_AGE_SECONDS = 60.0
            pool_ops.STATUS_SAMPLER_BYPASS = False
            pool_ops.ensure_runtime = lambda: None
            pool_ops.write_status_sampler_payload({"overall": "stale"}, include_logs=True)

            def fake_collect_status(include_logs=True):
                calls.append(include_logs)
                return {"overall": "ok"}

            pool_ops.collect_status = fake_collect_status

            status = pool_ops.collect_status_cached(include_logs=True, max_age_seconds=0)

            self.assertEqual(calls, [True])
            self.assertEqual(status["overall"], "ok")
            self.assertFalse(status["shared_status_cache"]["hit"])


class BackgroundMaintenanceDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            name: getattr(pool_ops, name)
            for name in (
                "BACKGROUND_MAINTENANCE_BACKOFF_ENABLED",
                "BACKGROUND_MAINTENANCE_SYNC_BACKOFF_BLOCKS",
                "BACKGROUND_MAINTENANCE_IOWAIT_WARN_PERCENT",
                "BACKGROUND_MAINTENANCE_IO_SOME_AVG10_WARN",
                "BACKGROUND_MAINTENANCE_IO_FULL_AVG10_WARN",
                "BACKGROUND_MAINTENANCE_CPU_SOME_AVG10_WARN",
                "BACKGROUND_MAINTENANCE_CHAIN_RPC_WARN_MS",
                "BACKGROUND_MAINTENANCE_LAZY_TASKS",
                "BACKGROUND_MAINTENANCE_POOL_READY_TASKS",
                "BACKGROUND_MAINTENANCE_LOADAVG_PER_CPU_WARN",
                "host_runtime_profile",
            )
        }
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        for name, value in self.originals.items():
            setattr(pool_ops, name, value)

    def test_background_maintenance_defers_during_sync_and_io_pressure(self) -> None:
        pool_ops.BACKGROUND_MAINTENANCE_BACKOFF_ENABLED = True
        pool_ops.BACKGROUND_MAINTENANCE_SYNC_BACKOFF_BLOCKS = 0
        pool_ops.BACKGROUND_MAINTENANCE_IOWAIT_WARN_PERCENT = 25.0
        pool_ops.BACKGROUND_MAINTENANCE_IO_SOME_AVG10_WARN = 20.0
        pool_ops.BACKGROUND_MAINTENANCE_IO_FULL_AVG10_WARN = 10.0
        pool_ops.BACKGROUND_MAINTENANCE_CPU_SOME_AVG10_WARN = 80.0
        status = {
            "sync_progress": {"status": "syncing", "remaining_blocks": 12},
            "host_pressure": {
                "iowait_percent": 30.0,
                "io_some_avg10": 2.0,
                "io_full_avg10": 0.0,
                "cpu_some_avg10": 3.0,
            },
        }

        decision = pool_ops.background_maintenance_decision("snapshot", status)

        self.assertFalse(decision["allowed"])
        self.assertTrue(any("chain catch-up has priority" in reason for reason in decision["reasons"]))
        self.assertTrue(any("host iowait" in reason for reason in decision["reasons"]))

    def test_background_maintenance_allows_idle_synced_host(self) -> None:
        pool_ops.BACKGROUND_MAINTENANCE_BACKOFF_ENABLED = True
        status = {
            "sync_progress": {"status": "synced", "remaining_blocks": 0},
            "host_pressure": {"iowait_percent": 1.0, "io_some_avg10": 0.0, "cpu_some_avg10": 0.0},
        }

        decision = pool_ops.background_maintenance_decision("snapshot", status)

        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["reasons"], [])

    def test_background_maintenance_defers_on_io_full_pressure(self) -> None:
        pool_ops.BACKGROUND_MAINTENANCE_BACKOFF_ENABLED = True
        pool_ops.BACKGROUND_MAINTENANCE_IOWAIT_WARN_PERCENT = 25.0
        pool_ops.BACKGROUND_MAINTENANCE_IO_SOME_AVG10_WARN = 20.0
        pool_ops.BACKGROUND_MAINTENANCE_IO_FULL_AVG10_WARN = 10.0
        status = {
            "overall": "ok",
            "mode": "mining",
            "can_mine": True,
            "can_accept_shares": True,
            "can_submit_blocks": True,
            "sync_progress": {"status": "synced", "remaining_blocks": 0},
            "host_pressure": {
                "iowait_percent": 4.0,
                "io_some_avg10": 5.0,
                "io_full_avg10": 14.0,
                "cpu_some_avg10": 0.0,
            },
        }

        decision = pool_ops.background_maintenance_decision("rawdatadir_content_seal", status)

        self.assertFalse(decision["allowed"])
        self.assertFalse(decision["pool_ready_required"])
        self.assertTrue(any("host io full pressure" in reason for reason in decision["reasons"]))

    def test_background_maintenance_defers_when_sync_remaining_is_unknown(self) -> None:
        pool_ops.BACKGROUND_MAINTENANCE_BACKOFF_ENABLED = True
        pool_ops.BACKGROUND_MAINTENANCE_SYNC_BACKOFF_BLOCKS = 0
        status = {
            "sync_progress": {"status": "syncing"},
            "host_pressure": {"iowait_percent": 1.0, "io_some_avg10": 0.0, "cpu_some_avg10": 0.0},
        }

        decision = pool_ops.background_maintenance_decision("snapshot", status)

        self.assertFalse(decision["allowed"])
        self.assertTrue(any("remaining=unknown" in reason for reason in decision["reasons"]))

    def test_background_maintenance_defers_when_chain_rpc_latency_is_high(self) -> None:
        pool_ops.BACKGROUND_MAINTENANCE_BACKOFF_ENABLED = True
        pool_ops.BACKGROUND_MAINTENANCE_CHAIN_RPC_WARN_MS = 1000.0
        status = {
            "sync_progress": {
                "status": "synced",
                "remaining_blocks": 0,
                "nodes": {"node": {"chain_rpc_latency_ms": 1500.0}},
            },
            "host_pressure": {"iowait_percent": 1.0, "io_some_avg10": 0.0, "cpu_some_avg10": 0.0},
        }

        decision = pool_ops.background_maintenance_decision("global", status)

        self.assertFalse(decision["allowed"])
        self.assertTrue(any("chain RPC latency" in reason for reason in decision["reasons"]))

    def test_lazy_archive_task_defers_until_pool_is_ready(self) -> None:
        pool_ops.BACKGROUND_MAINTENANCE_BACKOFF_ENABLED = True
        pool_ops.BACKGROUND_MAINTENANCE_LAZY_TASKS = {"history_compaction"}
        pool_ops.BACKGROUND_MAINTENANCE_POOL_READY_TASKS = {"history_compaction"}
        pool_ops.host_runtime_profile = lambda: {"cpu_count": 8}
        status = {
            "overall": "ok",
            "mode": "ready_no_miners",
            "can_mine": False,
            "can_accept_shares": False,
            "can_submit_blocks": False,
            "sync_progress": {"status": "synced", "remaining_blocks": 0},
            "host_pressure": {
                "iowait_percent": 1.0,
                "io_some_avg10": 0.0,
                "cpu_some_avg10": 0.0,
                "loadavg_1m": 1.0,
            },
        }

        decision = pool_ops.background_maintenance_decision("history_compaction", status)

        self.assertFalse(decision["allowed"])
        self.assertTrue(decision["task_is_lazy"])
        self.assertTrue(decision["pool_ready_required"])
        self.assertTrue(any("mode=ready_no_miners" in reason for reason in decision["reasons"]))
        self.assertTrue(any("pool can_mine=false" in reason for reason in decision["reasons"]))

    def test_rawdatadir_sidecar_can_run_when_pool_is_intentionally_not_ready(self) -> None:
        pool_ops.BACKGROUND_MAINTENANCE_BACKOFF_ENABLED = True
        pool_ops.BACKGROUND_MAINTENANCE_LAZY_TASKS = {"rawdatadir_sidecar"}
        pool_ops.BACKGROUND_MAINTENANCE_POOL_READY_TASKS = {
            "rawdatadir_publish",
            "rawdatadir_content_seal",
            "ipfs_content_sidecar",
            "ipfs_segment_writer",
        }
        pool_ops.host_runtime_profile = lambda: {"cpu_count": 8}
        status = {
            "overall": "syncing",
            "mode": "catchup_pause",
            "can_mine": False,
            "can_accept_shares": False,
            "can_submit_blocks": False,
            "sync_progress": {"status": "synced", "remaining_blocks": 0},
            "host_pressure": {
                "iowait_percent": 1.0,
                "io_some_avg10": 0.0,
                "cpu_some_avg10": 0.0,
                "loadavg_1m": 1.0,
            },
        }

        decision = pool_ops.background_maintenance_decision("rawdatadir_sidecar", status)

        self.assertTrue(decision["allowed"])
        self.assertTrue(decision["task_is_lazy"])
        self.assertFalse(decision["pool_ready_required"])
        self.assertEqual([], decision["reasons"])

    def test_lazy_archive_task_defers_on_load_pressure(self) -> None:
        pool_ops.BACKGROUND_MAINTENANCE_BACKOFF_ENABLED = True
        pool_ops.BACKGROUND_MAINTENANCE_LAZY_TASKS = {"global_scan"}
        pool_ops.BACKGROUND_MAINTENANCE_POOL_READY_TASKS = {"global_scan"}
        pool_ops.BACKGROUND_MAINTENANCE_LOADAVG_PER_CPU_WARN = 1.25
        pool_ops.host_runtime_profile = lambda: {"cpu_count": 8}
        status = {
            "overall": "ok",
            "mode": "mining",
            "can_mine": True,
            "can_accept_shares": True,
            "can_submit_blocks": True,
            "sync_progress": {"status": "synced", "remaining_blocks": 0},
            "host_pressure": {
                "iowait_percent": 1.0,
                "io_some_avg10": 0.0,
                "cpu_some_avg10": 0.0,
                "loadavg_1m": 11.0,
            },
        }

        decision = pool_ops.background_maintenance_decision("global_scan", status)

        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["loadavg_1m_warn"], 10.0)
        self.assertTrue(any("lazy threshold" in reason for reason in decision["reasons"]))


class AdaptiveConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            name: getattr(pool_ops, name)
            for name in (
                "HOST_PROFILE_OVERRIDE",
                "ADAPTIVE_CONCURRENCY_ENABLED",
                "ADAPTIVE_IOWAIT_WARN_PERCENT",
                "ADAPTIVE_IO_SOME_AVG10_WARN",
                "ADAPTIVE_CPU_SOME_AVG10_WARN",
                "ADAPTIVE_CHAIN_RPC_WARN_MS",
                "_HOST_RUNTIME_PROFILE_CACHE",
                "detect_total_memory_bytes",
                "detect_hardware_model",
            )
        }
        self.old_cpu_count = pool_ops.os.cpu_count
        self.old_platform_system = pool_ops.platform.system
        self.old_platform_machine = pool_ops.platform.machine
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        for name, value in self.originals.items():
            setattr(pool_ops, name, value)
        pool_ops.os.cpu_count = self.old_cpu_count
        pool_ops.platform.system = self.old_platform_system
        pool_ops.platform.machine = self.old_platform_machine

    def test_host_profile_detects_pi5_class_hardware(self) -> None:
        pool_ops.HOST_PROFILE_OVERRIDE = "auto"
        pool_ops._HOST_RUNTIME_PROFILE_CACHE = None
        pool_ops.platform.system = lambda: "Linux"
        pool_ops.platform.machine = lambda: "aarch64"
        pool_ops.os.cpu_count = lambda: 4
        pool_ops.detect_total_memory_bytes = lambda os_name=None: 4 * 1024 ** 3
        pool_ops.detect_hardware_model = lambda os_name=None: "Raspberry Pi 5 Model B Rev 1.0"

        profile = pool_ops.host_runtime_profile(force_refresh=True)

        self.assertEqual(profile["os"], "linux")
        self.assertEqual(profile["arch"], "arm64")
        self.assertEqual(profile["profile"], "pi5")

    def test_adaptive_workers_shrink_under_pressure_on_constrained_hosts(self) -> None:
        pool_ops.HOST_PROFILE_OVERRIDE = "pi5"
        pool_ops.ADAPTIVE_CONCURRENCY_ENABLED = True
        pool_ops.ADAPTIVE_IOWAIT_WARN_PERCENT = 25.0
        pool_ops._HOST_RUNTIME_PROFILE_CACHE = None
        pool_ops.os.cpu_count = lambda: 4
        pressure = {"iowait_percent": 30.0, "io_some_avg10": 0.0, "cpu_some_avg10": 0.0}

        workers = pool_ops.adaptive_worker_count("global_rpc", 24, 2048, pressure)

        self.assertEqual(workers, 1)

    def test_adaptive_workers_expand_on_large_idle_hosts(self) -> None:
        pool_ops.HOST_PROFILE_OVERRIDE = "large"
        pool_ops.ADAPTIVE_CONCURRENCY_ENABLED = True
        pool_ops._HOST_RUNTIME_PROFILE_CACHE = None
        pool_ops.os.cpu_count = lambda: 16
        pressure = {"iowait_percent": 1.0, "io_some_avg10": 0.0, "cpu_some_avg10": 0.0}

        workers = pool_ops.adaptive_worker_count("global_rpc", 24, 2048, pressure)

        self.assertEqual(workers, 24)

    def test_adaptive_workers_shrink_when_chain_rpc_latency_is_high(self) -> None:
        pool_ops.HOST_PROFILE_OVERRIDE = "standard"
        pool_ops.ADAPTIVE_CONCURRENCY_ENABLED = True
        pool_ops.ADAPTIVE_CHAIN_RPC_WARN_MS = 1000.0
        pool_ops._HOST_RUNTIME_PROFILE_CACHE = None
        pool_ops.os.cpu_count = lambda: 8
        pressure = {"chain_rpc_latency_ms": 1500.0}

        workers = pool_ops.adaptive_worker_count("global_rpc", 24, 2048, pressure)

        self.assertEqual(workers, 4)


if __name__ == "__main__":
    unittest.main()

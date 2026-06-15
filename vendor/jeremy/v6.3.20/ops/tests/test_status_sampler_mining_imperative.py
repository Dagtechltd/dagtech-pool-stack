#!/usr/bin/env python3

import os
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402
import status_sampler  # noqa: E402


class StatusSamplerMiningImperativeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            name: getattr(status_sampler, name)
            for name in (
                "MINING_IMPERATIVE_REPAIR_ENABLED",
                "MINING_IMPERATIVE_GUARD_UNITS",
                "MINING_IMPERATIVE_START_POOL_ENABLED",
                "MINING_IMPERATIVE_START_IDLE_SYNCED_POOL",
                "MINING_IMPERATIVE_POOL_START_STABLE_SAFE_SECONDS",
                "MINING_IMPERATIVE_MINER_TRACKING_REPAIR_ENABLED",
                "MINING_IMPERATIVE_MINER_ACTIVITY_REPAIR_ENABLED",
                "MINING_IMPERATIVE_ASIC_MAC_OVERRIDES_REPAIR_ENABLED",
                "MINING_IMPERATIVE_MINER_ACTIVITY_STALE_SECONDS",
                "MINING_IMPERATIVE_NODE_MINING_REPAIR_ENABLED",
                "MINING_IMPERATIVE_NODE_COMMAND_LINE_REPAIR_ENABLED",
                "MINING_IMPERATIVE_CHAIN_STATE_RESTORE_ENABLED",
                "CHAIN_STATE_MISSING_TRIE_RESTORE_WARNINGS",
                "CHAIN_STATE_ACTIVE_MINING_DEFER_SECONDS",
                "CATCHUP_PAUSE_ENABLED",
                "CATCHUP_PAUSE_THRESHOLD_BLOCKS",
                "CATCHUP_NODE_RECREATE_ENABLED",
                "CATCHUP_NODE_CACHE_MB",
                "CATCHUP_NODE_CACHE_MIN_MB",
                "CATCHUP_NODE_CACHE_MEMORY_PERCENT",
                "CATCHUP_IO_PRESSURE_PAUSE_ENABLED",
                "CATCHUP_IO_PRESSURE_MIN_LAG_BLOCKS",
                "CATCHUP_IOWAIT_WARN_PERCENT",
                "CATCHUP_IO_SOME_AVG10_WARN",
                "CATCHUP_IO_FULL_AVG10_WARN",
                "append_incident",
                "collect_pool_activity",
                "detect_total_memory_bytes",
                "log",
                "POOL_ENV_FILE",
                "POOL_START_STABILITY_FILE",
                "PROJECT_ROOT",
                "RUNTIME_DIR",
                "pool_asic_mac_override_diagnostics",
                "pool_asic_mac_overrides_value",
                "pool_container_env_value",
                "read_neighbor_macs",
                "read_miner_registry",
                "run",
                "save_miner_registry",
                "set_runtime_env_value",
                "upsert_pool_activity_miners",
            )
        }
        self.original_env = dict(os.environ)
        self.original_check_mutation_allowed = status_sampler.automation_control.check_mutation_allowed
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.restore)
        status_sampler.automation_control.check_mutation_allowed = (
            lambda *_args, **_kwargs: SimpleNamespace(allowed=True, reason="unit test allow")
        )
        status_sampler.append_incident = lambda *args, **kwargs: {}
        status_sampler.config_value = lambda name, default="": os.environ.get(name, default)
        status_sampler.log = lambda _message: None
        status_sampler.MINING_IMPERATIVE_REPAIR_ENABLED = True
        status_sampler.MINING_IMPERATIVE_START_POOL_ENABLED = True
        status_sampler.MINING_IMPERATIVE_START_IDLE_SYNCED_POOL = False
        status_sampler.MINING_IMPERATIVE_POOL_START_STABLE_SAFE_SECONDS = 0
        status_sampler.MINING_IMPERATIVE_MINER_TRACKING_REPAIR_ENABLED = True
        status_sampler.MINING_IMPERATIVE_MINER_ACTIVITY_REPAIR_ENABLED = True
        status_sampler.MINING_IMPERATIVE_ASIC_MAC_OVERRIDES_REPAIR_ENABLED = False
        status_sampler.MINING_IMPERATIVE_MINER_ACTIVITY_STALE_SECONDS = 180
        status_sampler.MINING_IMPERATIVE_NODE_MINING_REPAIR_ENABLED = True
        status_sampler.MINING_IMPERATIVE_NODE_COMMAND_LINE_REPAIR_ENABLED = False
        status_sampler.MINING_IMPERATIVE_CHAIN_STATE_RESTORE_ENABLED = True
        status_sampler.CHAIN_STATE_MISSING_TRIE_RESTORE_WARNINGS = 1
        status_sampler.CHAIN_STATE_ACTIVE_MINING_DEFER_SECONDS = 180
        status_sampler.POOL_ENV_FILE = pathlib.Path("/nonexistent/status-sampler-test.env")
        status_sampler.POOL_START_STABILITY_FILE = pathlib.Path(self.tmp.name) / "pool-start-stability.json"
        status_sampler.PROJECT_ROOT = pathlib.Path("/nonexistent/status-sampler-test-root")
        status_sampler.CATCHUP_PAUSE_ENABLED = True
        status_sampler.CATCHUP_PAUSE_THRESHOLD_BLOCKS = 300
        status_sampler.CATCHUP_NODE_RECREATE_ENABLED = True
        status_sampler.CATCHUP_NODE_CACHE_MB = 1024
        status_sampler.CATCHUP_NODE_CACHE_MIN_MB = 512
        status_sampler.CATCHUP_NODE_CACHE_MEMORY_PERCENT = 15.0
        status_sampler.CATCHUP_IO_PRESSURE_PAUSE_ENABLED = True
        status_sampler.CATCHUP_IO_PRESSURE_MIN_LAG_BLOCKS = 25
        status_sampler.CATCHUP_IOWAIT_WARN_PERCENT = 15.0
        status_sampler.CATCHUP_IO_SOME_AVG10_WARN = 20.0
        status_sampler.CATCHUP_IO_FULL_AVG10_WARN = 10.0
        os.environ["BDAG_ALLOW_UNSYNCED_NODE_MINING"] = "0"
        os.environ["BDAG_ENABLE_NODE_MINING"] = "0"
        os.environ["BDAG_NODE_MODULES"] = "Blockdag"
        os.environ["BDAG_NODE_MINING_ARGS"] = ""
        os.environ["MINING_ADDRESS"] = ""
        os.environ["MINING_POOL_ADDRESS"] = ""
        os.environ["NODE_ARGS_APPEND"] = ""
        os.environ["POOL_COINBASE_ADDRESS"] = ""
        for key in (
            "BDAG_ASIC_LAN_CIDRS",
            "BDAG_DETECTED_NETWORK_TOPOLOGY",
            "BDAG_MINER_SCAN_TARGET",
            "BDAG_NETWORK_TOPOLOGY",
            "BDAG_NODE_PEER_ADDRESSES",
            "BDAG_STORAGE_PROFILE",
            "BOOTSTRAP_PEER_ADDRESSES",
            "POOL_ASIC_MAC_OVERRIDES",
        ):
            os.environ.pop(key, None)
        for key in (
            "BDAG_COMPOSE_PROJECT_NAME",
            "COMPOSE_PROJECT_NAME",
        ):
            os.environ.pop(key, None)

    def restore(self) -> None:
        for name, value in self.originals.items():
            setattr(status_sampler, name, value)
        status_sampler.automation_control.check_mutation_allowed = self.original_check_mutation_allowed
        self.tmp.cleanup()
        os.environ.clear()
        os.environ.update(self.original_env)

    def command_result(self, command: list[str], returncode: int = 0, stdout: str = "", stderr: str = ""):
        return pool_ops.CommandResult(command, returncode, stdout, stderr, 0.0)

    def pool_compose_start_seen(self, commands: list[list[str]]) -> bool:
        return any(
            "compose" in command
            and "up" in command
            and "--no-deps" in command
            and "--no-build" in command
            and "--pull" in command
            and "never" in command
            and command[-1] == status_sampler.POOL_CONTAINER
            for command in commands
        )

    def canonical_safety(self, safe: bool = True) -> dict:
        return {
            "safe": safe,
            "schema": "stack_evm_public_reference_v1",
            "reason": "external public-chain proof matches local node" if safe else "public-chain proof failed",
        }

    def stopped_pool_payload(self, sync_status: str = "syncing", remaining_blocks: int = 5) -> dict:
        payload = {
            "overall": "syncing" if sync_status != "synced" else "ok",
            "sync_warnings": [] if sync_status == "synced" else ["behind"],
            "containers": {status_sampler.POOL_CONTAINER: {"running": False}},
            "sync_progress": {
                "status": sync_status,
                "remaining_blocks": remaining_blocks,
                "chain_block_count": 1000,
            },
            "miner_health": {"connected_count": 0, "managed_count": 0},
            "pool": {"metrics": {"active_connections": 0}, "source_job_health": {}},
            "pool_metrics": {"active_connections": 0, "source_job_health": {}},
        }
        if sync_status == "synced":
            payload["sync_progress"]["nodes"] = {
                "blockdag-node-1": {"canonical_mining_safety": self.canonical_safety(True)}
            }
        return payload

    def test_starts_stopped_pool_when_asic_lan_neighbor_is_present(self) -> None:
        commands = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["BDAG_ASIC_LAN_CIDRS"] = "192.168.1.0/24"
        status_sampler.read_neighbor_macs = lambda: {"192.168.1.107": "28:e2:97:1e:c0:b5"}

        def fake_run(command: list[str], timeout: int = 20):
            commands.append(command)
            return self.command_result(command)

        status_sampler.run = fake_run

        repair = status_sampler.mining_imperative_repair(
            self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        )

        self.assertTrue(self.pool_compose_start_seen(commands))
        self.assertIn(f"started_container:{status_sampler.POOL_CONTAINER}", repair["actions"])

    def test_first_safe_sample_waits_for_stable_window_before_starting_pool(self) -> None:
        commands = []
        incidents = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        status_sampler.MINING_IMPERATIVE_POOL_START_STABLE_SAFE_SECONDS = 90
        os.environ["BDAG_ASIC_LAN_CIDRS"] = "192.168.1.0/24"
        status_sampler.read_neighbor_macs = lambda: {"192.168.1.107": "28:e2:97:1e:c0:b5"}
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)
        status_sampler.append_incident = (
            lambda event_type, severity, *_args, **_kwargs: incidents.append((event_type, severity))
        )

        repair = status_sampler.mining_imperative_repair(
            self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        )

        self.assertFalse(self.pool_compose_start_seen(commands))
        self.assertNotIn(f"started_container:{status_sampler.POOL_CONTAINER}", repair["actions"])
        self.assertIn(("mining_imperative_pool_start_blocked", "warning"), incidents)

    def test_visible_asic_lan_neighbor_does_not_start_pool_while_syncing(self) -> None:
        commands = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["BDAG_ASIC_LAN_CIDRS"] = "192.168.1.0/24"
        status_sampler.read_neighbor_macs = lambda: {"192.168.1.107": "28:e2:97:1e:c0:b5"}
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        repair = status_sampler.mining_imperative_repair(
            self.stopped_pool_payload(sync_status="syncing", remaining_blocks=5)
        )

        self.assertFalse(self.pool_compose_start_seen(commands))
        self.assertNotIn(f"started_container:{status_sampler.POOL_CONTAINER}", repair["actions"])

    def test_synced_status_without_canonical_proof_does_not_start_pool(self) -> None:
        commands = []
        incidents = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["sync_progress"].pop("nodes", None)
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 1}
        status_sampler.read_neighbor_macs = lambda: {}
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)
        status_sampler.append_incident = (
            lambda event_type, severity, *_args, **_kwargs: incidents.append((event_type, severity))
        )

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertFalse(self.pool_compose_start_seen(commands))
        self.assertNotIn(f"started_container:{status_sampler.POOL_CONTAINER}", repair["actions"])
        self.assertIn(("mining_imperative_pool_start_blocked", "warning"), incidents)

    def test_public_chain_divergence_stops_running_pool(self) -> None:
        commands = []
        incidents = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["sync_health"] = {"public_chain_divergence": True}
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)
        status_sampler.append_incident = (
            lambda event_type, severity, *_args, **_kwargs: incidents.append((event_type, severity))
        )

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertTrue(any(command[-2:] == ["stop", status_sampler.POOL_CONTAINER] for command in commands))
        self.assertIn(f"stopped_container:{status_sampler.POOL_CONTAINER}:public_chain_divergence", repair["actions"])
        self.assertIn(("public_chain_divergence_stopped_pool", "warning"), incidents)
        self.assertFalse(any(command[:2] == ["docker", "start"] for command in commands))

    def test_compose_command_uses_stable_project_name_for_symlinked_runtime(self) -> None:
        os.environ.pop("BDAG_COMPOSE_PROJECT_NAME", None)
        os.environ.pop("COMPOSE_PROJECT_NAME", None)
        os.environ["BDAG_PROJECT_ROOT"] = "/home/jeremy/blockdag-asic-pool"

        command = pool_ops.docker_compose_command("ps")

        self.assertIn("-p", command)
        self.assertEqual(command[command.index("-p") + 1], "blockdag-asic-pool")

    def test_leaves_stopped_idle_pool_when_chain_is_synced_without_miner_demand(self) -> None:
        commands = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["BDAG_ASIC_LAN_CIDRS"] = "192.168.1.0/24"
        status_sampler.read_neighbor_macs = lambda: {}
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        repair = status_sampler.mining_imperative_repair(self.stopped_pool_payload(sync_status="synced", remaining_blocks=0))

        self.assertFalse(self.pool_compose_start_seen(commands))
        self.assertNotIn(f"started_container:{status_sampler.POOL_CONTAINER}", repair["actions"])

    def test_does_not_start_pool_without_miner_demand_or_ready_chain(self) -> None:
        commands = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["BDAG_ASIC_LAN_CIDRS"] = "192.168.1.0/24"
        status_sampler.read_neighbor_macs = lambda: {}
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        repair = status_sampler.mining_imperative_repair(self.stopped_pool_payload(sync_status="syncing", remaining_blocks=12))

        self.assertFalse(self.pool_compose_start_seen(commands))
        self.assertEqual(repair["actions"], [])

    def test_catchup_policy_from_payload_ignores_backend_unready_pressure_without_lag(self) -> None:
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["can_mine"] = False
        payload["catchup_policy"] = {
            "active": True,
            "trigger": "io_pressure",
            "io_pressure_reasons": ["io_full_avg10=12.00>=10.00"],
            "backend_unready_under_pressure": True,
            "lag_blocks": 0,
            "threshold_blocks": 300,
        }

        policy = status_sampler.catchup_policy_from_payload(payload)

        self.assertFalse(policy["active"])
        self.assertEqual(policy["trigger"], "")
        self.assertTrue(policy["backend_unready_under_pressure"])

    def test_catchup_policy_records_memory_pressure_reason(self) -> None:
        payload = self.stopped_pool_payload(sync_status="syncing", remaining_blocks=80)
        payload["can_mine"] = False
        payload["host_pressure"] = {
            "memory_available_percent": 8.5,
            "memory_available_warn_percent": 12.0,
            "memory_warning_active": True,
            "swap_used_percent": 0.0,
            "swap_used_warn_percent": 5.0,
            "swap_warning_active": False,
        }
        payload["catchup_policy"] = {
            "active": False,
            "lag_blocks": 80,
            "threshold_blocks": 300,
            "io_pressure_min_lag_blocks": 25,
            "mining_ready": False,
        }

        policy = status_sampler.catchup_policy_from_payload(payload)

        self.assertTrue(policy["active"])
        self.assertEqual(policy["trigger"], "io_pressure")
        self.assertIn("memory_available_warning", policy["io_pressure_reasons"])

    def test_backend_syncing_catchup_policy_stops_pool_without_known_lag(self) -> None:
        commands = []
        logs = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        status_sampler.log = logs.append
        payload = self.stopped_pool_payload(sync_status="syncing", remaining_blocks=None)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["can_mine"] = False
        payload["can_submit_blocks"] = False
        payload["sync_health"] = {"node_importing": True}
        payload["catchup_policy"] = {
            "active": True,
            "trigger": "backend_syncing",
            "lag_blocks": 0,
            "threshold_blocks": 300,
            "backend_sync_active": True,
            "node_sync_busy": True,
            "mining_ready": False,
        }
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        repair = status_sampler.mining_imperative_repair(payload)
        policy = status_sampler.catchup_policy_from_payload(payload)

        self.assertTrue(policy["active"])
        self.assertEqual(policy["trigger"], "backend_syncing")
        self.assertIn(f"stopped_container:{status_sampler.POOL_CONTAINER}:catchup_pause", repair["actions"])
        self.assertTrue(any(command[-2:] == ["stop", status_sampler.POOL_CONTAINER] for command in commands))
        self.assertIn("importing or busy syncing", "\n".join(logs))
        self.assertNotIn("0 blocks behind", "\n".join(logs))

    def test_node_readiness_unavailable_stops_pool_without_zero_lag_message(self) -> None:
        commands = []
        logs = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        status_sampler.log = logs.append
        payload = self.stopped_pool_payload(sync_status="syncing", remaining_blocks=None)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["can_mine"] = False
        payload["can_submit_blocks"] = False
        payload["sync_progress"]["source"] = "nodes:readiness-unavailable"
        payload["sync_progress"]["error"] = "node chain RPC/template readiness unavailable"
        payload["sync_health"] = {
            "node_readiness_unavailable": True,
            "chain_rpc_unavailable": True,
            "template_probe_unavailable": True,
        }
        payload["catchup_policy"] = {
            "active": False,
            "trigger": "backend_syncing",
            "lag_blocks": 0,
            "threshold_blocks": 300,
            "mining_ready": False,
        }
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        repair = status_sampler.mining_imperative_repair(payload)
        policy = status_sampler.catchup_policy_from_payload(payload)
        joined_logs = "\n".join(logs)

        self.assertTrue(policy["active"])
        self.assertEqual(policy["trigger"], "node_readiness_unavailable")
        self.assertIn(f"stopped_container:{status_sampler.POOL_CONTAINER}:node_readiness_unavailable", repair["actions"])
        self.assertNotIn("applied_catchup_node_runtime", repair["actions"])
        self.assertTrue(any(command[-2:] == ["stop", status_sampler.POOL_CONTAINER] for command in commands))
        self.assertIn("readiness unavailable", joined_logs)
        self.assertNotIn("0 blocks behind", joined_logs)

    def test_catchup_pause_does_not_stop_pool_with_recent_paid_work(self) -> None:
        commands = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        payload = self.stopped_pool_payload(sync_status="syncing", remaining_blocks=80)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["can_mine"] = False
        payload["sync_health"] = {"pool_has_recent_paid_work": True}
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 1}
        payload["host_pressure"] = {"io_full_avg10": 23.0}
        payload["catchup_policy"] = {
            "active": True,
            "trigger": "io_pressure",
            "lag_blocks": 80,
            "threshold_blocks": 300,
            "io_pressure_reasons": ["io_full_avg10=23.00>=10.00"],
            "io_pressure_min_lag_blocks": 25,
            "mining_ready": False,
        }
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        repair = status_sampler.mining_imperative_repair(payload)
        policy = status_sampler.catchup_policy_from_payload(payload)

        self.assertFalse(policy["active"])
        self.assertTrue(policy["recent_paid_work_suppressed"])
        self.assertNotIn(f"stopped_container:{status_sampler.POOL_CONTAINER}:catchup_pause", repair["actions"])
        self.assertFalse(any(command[-2:] == ["stop", status_sampler.POOL_CONTAINER] for command in commands))

    def test_catchup_pause_stops_pool_but_preserves_node_mining_config(self) -> None:
        commands = []
        env_updates = {}
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["BDAG_ENABLE_NODE_MINING"] = "1"
        os.environ["BDAG_NODE_MODULES"] = "Blockdag,miner"
        os.environ["BDAG_NODE_MINING_ARGS"] = "--miner --miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        os.environ["NODE_ARGS_APPEND"] = os.environ["BDAG_NODE_MINING_ARGS"]
        os.environ["BDAG_NODE_CACHE_MB"] = "512"
        os.environ["BDAG_EVM_CACHE_MB"] = "512"
        os.environ["BDAG_NODE_SERVICE"] = "node"
        payload = self.stopped_pool_payload(sync_status="syncing", remaining_blocks=450)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 1}
        payload["catchup_policy"] = {
            "active": True,
            "lag_blocks": 450,
            "threshold_blocks": 300,
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            status_sampler.PROJECT_ROOT = root
            (root / "node.conf").write_text(
                "\n".join(
                    [
                        "cache=512",
                        "cache.database=70",
                        "miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc",
                        "modules=Blockdag",
                        "modules=miner",
                        "miner=true",
                        'evmenv="--metrics --cache 2048"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            def fake_set_runtime_env(key: str, value: str):
                env_updates[key] = value
                os.environ[key] = value
                return [f"/runtime/{key}"]

            def fake_run(command: list[str], timeout: int = 20):
                commands.append(command)
                return self.command_result(command)

            status_sampler.set_runtime_env_value = fake_set_runtime_env
            status_sampler.detect_total_memory_bytes = lambda: 16 * 1024 * 1024 * 1024
            status_sampler.run = fake_run

            repair = status_sampler.mining_imperative_repair(payload)
            node_conf = (root / "node.conf").read_text(encoding="utf-8")

        self.assertIn(f"stopped_container:{status_sampler.POOL_CONTAINER}:catchup_pause", repair["actions"])
        self.assertIn("applied_catchup_node_runtime", repair["actions"])
        self.assertEqual(env_updates, {"BDAG_NODE_CACHE_MB": "1024"})
        self.assertEqual(os.environ["BDAG_ENABLE_NODE_MINING"], "1")
        self.assertEqual(os.environ["BDAG_NODE_MODULES"], "Blockdag,miner")
        self.assertEqual(
            os.environ["BDAG_NODE_MINING_ARGS"],
            "--miner --miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc",
        )
        self.assertEqual(os.environ["NODE_ARGS_APPEND"], os.environ["BDAG_NODE_MINING_ARGS"])
        self.assertIn("cache=1024", node_conf)
        self.assertIn("--cache 512", node_conf)
        self.assertIn("miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc", node_conf)
        self.assertIn("modules=miner", node_conf)
        self.assertIn("miner=true", node_conf)
        self.assertTrue(any(command[-2:] == ["stop", status_sampler.POOL_CONTAINER] for command in commands))
        self.assertFalse(any("--force-recreate" in command for command in commands))

    def test_catchup_pause_does_not_restart_stopped_pool_for_visible_miners(self) -> None:
        commands = []
        env_updates = {}
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["BDAG_ENABLE_NODE_MINING"] = "0"
        os.environ["BDAG_NODE_MODULES"] = "Blockdag"
        os.environ["BDAG_NODE_MINING_ARGS"] = ""
        os.environ["NODE_ARGS_APPEND"] = ""
        payload = self.stopped_pool_payload(sync_status="syncing", remaining_blocks=450)
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 1}
        payload["catchup_policy"] = {
            "active": True,
            "lag_blocks": 450,
            "threshold_blocks": 300,
        }

        def fake_set_runtime_env(key: str, value: str):
            env_updates[key] = value
            os.environ[key] = value
            return [f"/runtime/{key}"]

        status_sampler.set_runtime_env_value = fake_set_runtime_env
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        with tempfile.TemporaryDirectory() as tmp:
            status_sampler.PROJECT_ROOT = pathlib.Path(tmp)
            repair = status_sampler.mining_imperative_repair(payload)

        self.assertNotIn(f"started_container:{status_sampler.POOL_CONTAINER}", repair["actions"])
        self.assertNotIn(f"stopped_container:{status_sampler.POOL_CONTAINER}:catchup_pause", repair["actions"])
        self.assertFalse(any(command[:2] == ["docker", "start"] for command in commands))

    def test_recent_paid_work_clears_zero_lag_io_pressure_catchup_pause(self) -> None:
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["can_mine"] = False
        payload["host_pressure"] = {
            "iowait_percent": 35.0,
            "io_some_avg10": 30.0,
            "io_full_avg10": 25.0,
        }
        payload["pool"].update(
            {
                "block_submit_success_count": 6,
                "last_block_submit_age_seconds": 12,
            }
        )
        payload["sync_health"] = {
            "pool_has_recent_paid_work": True,
            "pool_has_recent_mining": True,
        }
        payload["catchup_policy"] = {
            "active": True,
            "trigger": "io_pressure",
            "lag_blocks": 0,
            "threshold_blocks": 300,
            "mining_ready": False,
            "backend_unready_under_pressure": True,
            "io_pressure_reasons": [
                "iowait_percent=35.00>=15.00",
                "io_some_avg10=30.00>=20.00",
            ],
        }

        policy = status_sampler.catchup_policy_from_payload(payload)

        self.assertFalse(policy["active"])
        self.assertFalse(policy["io_pressure_active"])
        self.assertFalse(policy["backend_unready_under_pressure"])

    def test_missing_trie_only_defers_chain_state_restore_during_fresh_paid_mining(self) -> None:
        commands = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 0}
        payload["pool"] = {
            **payload["pool"],
            "block_submit_success_count": 4,
            "last_block_submit_age_seconds": 12,
        }
        payload["sync_health"] = {"pool_has_recent_paid_work": True}
        payload["nodes"] = {"stack-node-1": {"missing_trie_node_warnings": 1}}
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        decision = status_sampler.chain_state_restore_decision(payload)
        repair = status_sampler.mining_imperative_repair(payload)

        self.assertFalse(decision["should_repair"])
        self.assertTrue(decision["deferred"])
        self.assertEqual(repair["actions"], [])
        self.assertFalse(any(command[-2:] == ["stop", status_sampler.POOL_CONTAINER] for command in commands))

    def test_sync_health_missing_trie_restore_defers_during_fresh_paid_mining(self) -> None:
        commands = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 4, "connected_count": 4, "managed_count": 4}
        payload["pool"] = {
            **payload["pool"],
            "block_submit_success_count": 11,
            "last_block_submit_age_seconds": 2,
        }
        payload["sync_health"] = {
            "pool_has_recent_paid_work": True,
            "needs_chain_data_restore": True,
            "chain_data_restore_required": True,
            "chain_data_restore_nodes": {
                "node": {
                    "reasons": ["node EVM trie state is unavailable (4 missing-trie warning(s))"],
                    "missing_trie_node_warnings": 4,
                }
            },
        }
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        decision = status_sampler.chain_state_restore_decision(payload)
        repair = status_sampler.mining_imperative_repair(payload)

        self.assertFalse(decision["should_repair"])
        self.assertTrue(decision["deferred"])
        self.assertIn("accepted block submission is fresh", "; ".join(decision["reasons"]))
        self.assertEqual(repair["actions"], [])
        self.assertFalse(any(command[-2:] == ["stop", status_sampler.POOL_CONTAINER] for command in commands))
        self.assertFalse(any(command[:4] == ["systemctl", "--user", "start", "--no-block"] for command in commands))

    def test_missing_trie_only_requires_corroboration_before_chain_state_restore(self) -> None:
        commands = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 0, "managed_count": 1}
        payload["pool"] = {
            **payload["pool"],
            "block_submit_success_count": 0,
            "last_block_submit_age_seconds": None,
        }
        payload["sync_health"] = {}
        payload["nodes"] = {"stack-node-1": {"missing_trie_node_warnings": 1}}
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        decision = status_sampler.chain_state_restore_decision(payload)
        repair = status_sampler.mining_imperative_repair(payload)

        self.assertFalse(decision["should_repair"])
        self.assertTrue(decision["deferred"])
        self.assertIn("requires corroboration", decision["defer_reason"])
        self.assertEqual(repair["actions"], [])
        self.assertFalse(any(command[:4] == ["systemctl", "--user", "start", "--no-block"] for command in commands))

    def test_hard_chain_state_blocker_starts_self_heal_without_sampler_pool_stop(self) -> None:
        commands = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 0}
        payload["pool"] = {
            **payload["pool"],
            "block_submit_success_count": 4,
            "last_block_submit_age_seconds": 12,
        }
        payload["sync_health"] = {"pool_has_recent_paid_work": True}
        payload["nodes"] = {
            "stack-node-1": {
                "chain_state_blocker": True,
                "chain_state_blocker_hash": "0xabc",
            }
        }
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertIn("started_chain_state_self_heal", repair["actions"])
        self.assertTrue(any(command[:4] == ["systemctl", "--user", "start", "--no-block"] for command in commands))
        self.assertFalse(any(command[-2:] == ["stop", status_sampler.POOL_CONTAINER] for command in commands))

    def test_reenables_guard_timer_when_it_drifts_disabled(self) -> None:
        commands = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = ["bdag-stack-sentinel.timer"]

        def fake_run(command: list[str], timeout: int = 20):
            commands.append(command)
            if command[:3] == ["systemctl", "--user", "is-enabled"]:
                return self.command_result(command, 1, "disabled\n", "")
            if command[:3] == ["systemctl", "--user", "is-active"]:
                return self.command_result(command, 3, "inactive\n", "")
            return self.command_result(command)

        status_sampler.run = fake_run
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertIn(["systemctl", "--user", "enable", "--now", "bdag-stack-sentinel.timer"], commands)
        self.assertIn("repaired_unit:bdag-stack-sentinel.timer", repair["actions"])

    def test_repairs_missing_tracked_miners_from_pool_activity(self) -> None:
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 0, "connected_count": 1, "managed_count": 0}
        activity = {"miners": [{"ip": "172.18.0.1"}], "unattributed_valid_shares": 8, "unattributed_blocks": 1}
        status_sampler.collect_pool_activity = lambda lines=0: activity
        status_sampler.upsert_pool_activity_miners = lambda _activity: {
            "miners": [{"ip": "192.168.1.107", "mac": "28:e2:97:1e:c0:b5"}]
        }
        status_sampler.read_miner_registry = lambda: {"miners": []}

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertIn("repaired_tracked_miners", repair["actions"])

    def test_repairs_pool_asic_mac_overrides_and_recreates_pool(self) -> None:
        commands = []
        env_updates = {}
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        status_sampler.MINING_IMPERATIVE_ASIC_MAC_OVERRIDES_REPAIR_ENABLED = True
        status_sampler.pool_asic_mac_overrides_value = (
            lambda: "192.168.1.101=2a:71:c7:f5:1f:1e,192.168.1.105=28:e2:97:4d:44:3a"
        )
        status_sampler.pool_asic_mac_override_diagnostics = lambda: {
            "identity_basis": "mac",
            "override_value": "192.168.1.101=2a:71:c7:f5:1f:1e,192.168.1.105=28:e2:97:4d:44:3a",
            "override_count": 2,
            "overrides": [],
            "unresolved_count": 0,
            "unresolved": [],
        }
        status_sampler.pool_container_env_value = lambda _key: ""
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 2, "connected_count": 2, "managed_count": 2}

        def fake_set_runtime_env(key: str, value: str):
            env_updates[key] = value
            os.environ[key] = value
            return [f"/runtime/{key}"]

        def fake_run(command: list[str], timeout: int = 20):
            commands.append(command)
            return self.command_result(command)

        status_sampler.set_runtime_env_value = fake_set_runtime_env
        status_sampler.run = fake_run

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertIn("repaired_pool_asic_mac_overrides", repair["actions"])
        self.assertEqual(
            env_updates["POOL_ASIC_MAC_OVERRIDES"],
            "192.168.1.101=2a:71:c7:f5:1f:1e,192.168.1.105=28:e2:97:4d:44:3a",
        )
        recreate = [command for command in commands if "--force-recreate" in command]
        self.assertEqual(1, len(recreate))
        self.assertIn("--no-build", recreate[0])
        self.assertIn("--pull", recreate[0])
        self.assertIn("never", recreate[0])
        self.assertEqual(recreate[0][-1], status_sampler.POOL_CONTAINER)

    def test_recreates_pool_when_asic_lan_cidr_container_env_is_stale(self) -> None:
        commands = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        status_sampler.MINING_IMPERATIVE_ASIC_MAC_OVERRIDES_REPAIR_ENABLED = True
        os.environ["BDAG_ASIC_LAN_CIDRS"] = "192.168.1.0/24"
        os.environ["POOL_ASIC_MAC_OVERRIDES"] = "192.168.1.101=2a:71:c7:f5:1f:1e"
        status_sampler.pool_asic_mac_overrides_value = lambda: "192.168.1.101=2a:71:c7:f5:1f:1e"
        status_sampler.pool_asic_mac_override_diagnostics = lambda: {
            "identity_basis": "mac",
            "override_value": "192.168.1.101=2a:71:c7:f5:1f:1e",
            "override_count": 1,
            "overrides": [],
            "unresolved_count": 0,
            "unresolved": [],
        }
        status_sampler.pool_container_env_value = (
            lambda key: "192.168.1.101=2a:71:c7:f5:1f:1e" if key == "POOL_ASIC_MAC_OVERRIDES" else ""
        )
        status_sampler.set_runtime_env_value = lambda *_args, **_kwargs: []
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 1}

        def fake_run(command: list[str], timeout: int = 20):
            commands.append(command)
            return self.command_result(command)

        status_sampler.run = fake_run

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertIn("repaired_pool_asic_mac_overrides", repair["actions"])
        recreate = [command for command in commands if "--force-recreate" in command]
        self.assertEqual(1, len(recreate))
        self.assertEqual(recreate[0][-1], status_sampler.POOL_CONTAINER)

    def test_unresolved_asic_mac_does_not_repair_to_ip_lane(self) -> None:
        incidents = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        status_sampler.MINING_IMPERATIVE_ASIC_MAC_OVERRIDES_REPAIR_ENABLED = True
        status_sampler.pool_asic_mac_overrides_value = lambda: ""
        status_sampler.pool_asic_mac_override_diagnostics = lambda: {
            "identity_basis": "mac",
            "override_value": "",
            "override_count": 0,
            "overrides": [],
            "unresolved_count": 1,
            "unresolved": [{"ip": "192.168.1.111", "issue": "asic_mac_unresolved"}],
        }
        status_sampler.pool_container_env_value = lambda _key: ""
        status_sampler.set_runtime_env_value = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not write IP-based ASIC identity")
        )
        status_sampler.run = lambda command, timeout=20: self.command_result(command)
        status_sampler.append_incident = (
            lambda event_type, severity, *_args, **_kwargs: incidents.append((event_type, severity))
        )
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 1}

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertNotIn("repaired_pool_asic_mac_overrides", repair["actions"])
        self.assertFalse(any(event == "mining_imperative_asic_mac_overrides_reconciled" for event, _ in incidents))

    def test_detects_miner_activity_visibility_gap_after_power_cycle(self) -> None:
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["miner_health"] = {
            "tracked_count": 4,
            "connected_count": 4,
            "managed_count": 4,
            "miners": [
                {
                    "mac": "28:e2:97:4d:44:3a",
                    "device_type": "asic",
                    "managed": True,
                    "connected": True,
                    "shares": 0,
                    "share_work": 0,
                    "last_share_epoch": 1000,
                    "last_share_age_seconds": 12,
                }
            ],
        }
        payload["pool_health"] = {"valid_share_count": 8, "last_valid_share_age_seconds": 10}

        self.assertTrue(status_sampler.status_payload_has_miner_activity_visibility_gap(payload))

    def test_visible_miner_shares_do_not_trigger_activity_visibility_repair(self) -> None:
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["miner_health"] = {
            "tracked_count": 4,
            "connected_count": 4,
            "managed_count": 4,
            "miners": [
                {
                    "mac": "28:e2:97:4d:44:3a",
                    "device_type": "asic",
                    "managed": True,
                    "connected": True,
                    "shares": 3,
                    "share_work": 99,
                }
            ],
        }
        payload["pool_health"] = {"valid_share_count": 8, "last_valid_share_age_seconds": 10}

        self.assertFalse(status_sampler.status_payload_has_miner_activity_visibility_gap(payload))

    def test_repairs_miner_activity_visibility_from_deep_pool_activity(self) -> None:
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {
            "tracked_count": 4,
            "connected_count": 4,
            "managed_count": 4,
            "miners": [
                {
                    "mac": "28:e2:97:4d:44:3a",
                    "device_type": "asic",
                    "managed": True,
                    "connected": True,
                    "shares": 0,
                    "share_work": 0,
                }
            ],
        }
        payload["pool_health"] = {"valid_share_count": 8, "last_valid_share_age_seconds": 10}
        activity_calls = []
        activity = {
            "miners": [{"ip": "192.168.1.102", "shares": 5, "share_work": 12345}],
            "unattributed_valid_shares": 0,
            "unattributed_blocks": 0,
        }
        status_sampler.collect_pool_activity = lambda lines=0: activity_calls.append(lines) or activity
        status_sampler.upsert_pool_activity_miners = lambda _activity: {
            "miners": [
                {
                    "ip": "192.168.1.102",
                    "mac": "28:e2:97:4d:44:3a",
                    "last_pool_seen_epoch": 1_000_000,
                    "last_share_epoch": 1_000_000,
                    "last_shares_window": 5,
                    "last_share_work_window": 12345,
                }
            ]
        }
        old_time = status_sampler.time.time
        status_sampler.time.time = lambda: 1_000_010
        self.addCleanup(lambda: setattr(status_sampler.time, "time", old_time))

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertIn(status_sampler.POOL_ACTIVITY_BOOTSTRAP_LOG_LINES, activity_calls)
        self.assertIn("repaired_miner_activity_visibility", repair["actions"])

    def test_enables_node_mining_template_support_when_miner_is_present(self) -> None:
        commands = []
        env_updates = {}
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        status_sampler.MINING_IMPERATIVE_NODE_COMMAND_LINE_REPAIR_ENABLED = True
        os.environ["MINING_ADDRESS"] = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        os.environ["BDAG_ENABLE_NODE_MINING"] = "0"
        os.environ["BDAG_NODE_MODULES"] = "Blockdag"
        os.environ["BDAG_NODE_MINING_ARGS"] = ""
        os.environ["BDAG_NODE_SERVICE"] = "node"
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 1}

        def fake_set_runtime_env(key: str, value: str):
            env_updates[key] = value
            os.environ[key] = value
            return [f"/runtime/{key}"]

        def fake_run(command: list[str], timeout: int = 20):
            commands.append(command)
            return self.command_result(command)

        status_sampler.set_runtime_env_value = fake_set_runtime_env
        status_sampler.run = fake_run

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertIn("enabled_node_mining_template_support", repair["actions"])
        self.assertEqual(env_updates["BDAG_ENABLE_NODE_MINING"], "1")
        self.assertEqual(env_updates["BDAG_NODE_MODULES"], "Blockdag")
        self.assertIn("--miner", env_updates["NODE_ARGS_APPEND"])
        self.assertIn("--miner", env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertIn("--miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc", env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertNotIn("--allowminingwhennearlysynced", env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertNotIn("--allowsubmitwhennotsynced", env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertEqual(env_updates["NODE_ARGS_APPEND"], env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertTrue(any("--force-recreate" in command for command in commands))

    def test_preserves_explicit_maxinbound_when_enabling_node_mining(self) -> None:
        commands = []
        env_updates = {}
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        address = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        os.environ["MINING_ADDRESS"] = address
        os.environ["BDAG_ENABLE_NODE_MINING"] = "0"
        os.environ["BDAG_NODE_MODULES"] = "Blockdag"
        os.environ["BDAG_NODE_MINING_ARGS"] = f"--miner --miningaddr={address} --maxinbound=1"
        os.environ["BDAG_NODE_SERVICES"] = "node"
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 1}

        def fake_set_runtime_env(key: str, value: str):
            env_updates[key] = value
            os.environ[key] = value
            return [f"/runtime/{key}"]

        status_sampler.set_runtime_env_value = fake_set_runtime_env
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertIn("enabled_node_mining_template_support", repair["actions"])
        self.assertIn("--maxinbound=1", env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertIn("--maxinbound=1", env_updates["NODE_ARGS_APPEND"])

    def test_node_mining_template_support_requires_canonical_proof(self) -> None:
        commands = []
        incidents = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["MINING_ADDRESS"] = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        os.environ["BDAG_ENABLE_NODE_MINING"] = "0"
        os.environ["BDAG_NODE_MODULES"] = "Blockdag"
        os.environ["BDAG_NODE_MINING_ARGS"] = ""
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["sync_progress"].pop("nodes", None)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 1}

        status_sampler.set_runtime_env_value = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("config edit must not run without canonical proof")
        )
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)
        status_sampler.append_incident = (
            lambda event_type, severity, *_args, **_kwargs: incidents.append((event_type, severity))
        )

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertNotIn("enabled_node_mining_template_support", repair["actions"])
        self.assertFalse(any("--force-recreate" in command for command in commands))
        self.assertIn(("mining_imperative_node_mining_gate_blocked", "warning"), incidents)

    def test_node_mining_template_repair_does_not_enable_node_conf_miner_modules(self) -> None:
        commands = []
        env_updates = {}
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["MINING_ADDRESS"] = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        os.environ["BDAG_ENABLE_NODE_MINING"] = "0"
        os.environ["BDAG_NODE_MODULES"] = "Blockdag"
        os.environ["BDAG_NODE_MINING_ARGS"] = ""
        os.environ["BDAG_NODE_SERVICE"] = "node"
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 1}

        def fake_set_runtime_env(key: str, value: str):
            env_updates[key] = value
            os.environ[key] = value
            return [f"/runtime/{key}"]

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            status_sampler.PROJECT_ROOT = root
            (root / "node.conf").write_text(
                "miningaddr=\n# modules=miner\n# miner=true\n",
                encoding="utf-8",
            )
            status_sampler.set_runtime_env_value = fake_set_runtime_env
            status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

            repair = status_sampler.mining_imperative_repair(payload)
            node_conf = (root / "node.conf").read_text(encoding="utf-8")

        self.assertIn("enabled_node_mining_template_support", repair["actions"])
        self.assertIn("miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc", node_conf)
        self.assertNotIn("\nmodules=miner", node_conf)
        self.assertNotIn("\nminer=true", node_conf)

    def test_node_args_parser_accepts_nodeworker_embedded_node_args(self) -> None:
        address = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        command_line = f"nodeworker --node-args=--miner --miningaddr={address}"

        self.assertTrue(status_sampler.node_mining_args_are_safe_and_complete(command_line, address))

    def test_runtime_env_updates_ops_env_for_systemd_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            pool_env = root / "pool.env"
            ops_env = root / "ops.env"
            (root / ".env").write_text("BDAG_ENABLE_NODE_MINING=0\n", encoding="utf-8")
            pool_env.write_text("BDAG_ENABLE_NODE_MINING=0\n", encoding="utf-8")
            ops_env.write_text("BDAG_ENABLE_NODE_MINING=0\n", encoding="utf-8")
            status_sampler.PROJECT_ROOT = root
            status_sampler.POOL_ENV_FILE = pool_env
            status_sampler.RUNTIME_DIR = root
            os.environ["BDAG_OPS_ENV_FILE"] = str(ops_env)

            changed = status_sampler.set_runtime_env_value("BDAG_ENABLE_NODE_MINING", "1")

            self.assertEqual(
                sorted(changed),
                sorted([str(root / ".env"), str(pool_env), str(ops_env)]),
            )
            self.assertIn("BDAG_ENABLE_NODE_MINING=1", (root / ".env").read_text(encoding="utf-8"))
            self.assertIn("BDAG_ENABLE_NODE_MINING=1", pool_env.read_text(encoding="utf-8"))
            self.assertIn("BDAG_ENABLE_NODE_MINING=1", ops_env.read_text(encoding="utf-8"))
            self.assertEqual(os.environ["BDAG_ENABLE_NODE_MINING"], "1")

    def test_fresh_share_evidence_suppresses_command_line_only_node_mining_repair(self) -> None:
        commands = []
        address = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        status_sampler.MINING_IMPERATIVE_NODE_COMMAND_LINE_REPAIR_ENABLED = True
        os.environ["MINING_ADDRESS"] = address
        os.environ["BDAG_ENABLE_NODE_MINING"] = "1"
        os.environ["BDAG_NODE_MODULES"] = "Blockdag"
        os.environ["BDAG_NODE_MINING_ARGS"] = f"--miner --miningaddr={address}"
        os.environ["NODE_ARGS_APPEND"] = os.environ["BDAG_NODE_MINING_ARGS"]
        os.environ["BDAG_NODE_SERVICE"] = "node"
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 1}
        payload["pool_health"] = {"valid_share_count": 8, "last_valid_share_age_seconds": 10}
        old_node_command_line = status_sampler.node_command_line
        self.addCleanup(lambda: setattr(status_sampler, "node_command_line", old_node_command_line))
        status_sampler.node_command_line = lambda _service: "nodeworker --node-args=--maxinbound=1"
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertNotIn("enabled_node_mining_template_support", repair["actions"])
        self.assertFalse(any("--force-recreate" in command for command in commands))

    def test_repairs_node_mining_args_when_unsafe_sync_bypass_is_present(self) -> None:
        commands = []
        env_updates = {}
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["MINING_ADDRESS"] = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        os.environ["BDAG_ENABLE_NODE_MINING"] = "1"
        os.environ["BDAG_NODE_MODULES"] = "Blockdag"
        os.environ["BDAG_NODE_MINING_ARGS"] = (
            "--allowminingwhennearlysynced --allowsubmitwhennotsynced --miner "
            "--miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        )
        os.environ["BDAG_NODE_SERVICE"] = "node"
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 1}

        def fake_set_runtime_env(key: str, value: str):
            env_updates[key] = value
            os.environ[key] = value
            return [f"/runtime/{key}"]

        status_sampler.set_runtime_env_value = fake_set_runtime_env
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertIn("enabled_node_mining_template_support", repair["actions"])
        self.assertIn("--miner", env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertNotIn("--allowminingwhennearlysynced", env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertNotIn("--allowsubmitwhennotsynced", env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertEqual(env_updates["NODE_ARGS_APPEND"], env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertTrue(any("--force-recreate" in command for command in commands))

    def test_repairs_unsupported_miner_rpc_module(self) -> None:
        commands = []
        env_updates = {}
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["MINING_ADDRESS"] = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        os.environ["BDAG_ENABLE_NODE_MINING"] = "1"
        os.environ["BDAG_NODE_MODULES"] = "Blockdag,miner"
        os.environ["BDAG_NODE_MINING_ARGS"] = (
            "--miner --miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        )
        os.environ["BDAG_NODE_SERVICE"] = "node"
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 1}

        def fake_set_runtime_env(key: str, value: str):
            env_updates[key] = value
            os.environ[key] = value
            return [f"/runtime/{key}"]

        status_sampler.set_runtime_env_value = fake_set_runtime_env
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertIn("enabled_node_mining_template_support", repair["actions"])
        self.assertEqual(env_updates["BDAG_NODE_MODULES"], "Blockdag")
        self.assertNotIn("--allowsubmitwhennotsynced", env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertTrue(any("--force-recreate" in command for command in commands))

    def test_recreates_node_when_live_process_has_unsafe_sync_bypass(self) -> None:
        commands = []
        env_updates = {}
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        status_sampler.MINING_IMPERATIVE_NODE_COMMAND_LINE_REPAIR_ENABLED = True
        os.environ["MINING_ADDRESS"] = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        os.environ["BDAG_ENABLE_NODE_MINING"] = "1"
        os.environ["BDAG_NODE_MODULES"] = "Blockdag"
        os.environ["BDAG_NODE_MINING_ARGS"] = (
            "--miner --miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        )
        os.environ["BDAG_NODE_SERVICE"] = "node"
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 1}

        def fake_set_runtime_env(key: str, value: str):
            env_updates[key] = value
            os.environ[key] = value
            return [f"/runtime/{key}"]

        def fake_run(command: list[str], timeout: int = 20):
            commands.append(command)
            if "exec" in command and "-T" in command and any("ps -eo args" in part for part in command):
                stdout = (
                    "nodeworker --node-args=--allowminingwhennearlysynced --allowsubmitwhennotsynced --miner "
                    "--miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc\n"
                )
                return self.command_result(command, stdout=stdout)
            return self.command_result(command)

        status_sampler.set_runtime_env_value = fake_set_runtime_env
        status_sampler.run = fake_run

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertIn("enabled_node_mining_template_support", repair["actions"])
        self.assertIn("--miner", env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertNotIn("--allowminingwhennearlysynced", env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertNotIn("--allowsubmitwhennotsynced", env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertEqual(env_updates["NODE_ARGS_APPEND"], env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertTrue(any("--force-recreate" in command for command in commands))

    def test_does_not_enable_node_mining_without_valid_address(self) -> None:
        commands = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["MINING_ADDRESS"] = "0x0000000000000000000000000000000000000000"
        os.environ["BDAG_ENABLE_NODE_MINING"] = "0"
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 1}
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertNotIn("enabled_node_mining_template_support", repair["actions"])
        self.assertFalse(any("--force-recreate" in command for command in commands))

if __name__ == "__main__":
    unittest.main()

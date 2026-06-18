from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("update_local_peers", ROOT / "ops" / "update-local-peers.py")
assert SPEC is not None
assert SPEC.loader is not None
update_local_peers = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(update_local_peers)


class UpdateLocalPeersActiveMiningGuardTest(unittest.TestCase):
    def patch_status(self, status: dict[str, object]):
        return mock.patch.object(update_local_peers, "fetch_dashboard_status", return_value=status)

    def test_defers_node_recreate_while_miners_are_active(self) -> None:
        status = {
            "can_accept_shares": True,
            "pool": {
                "metrics_active_connections": 4,
                "last_valid_share_age_seconds": 2,
                "last_submit_age_seconds": 1,
            },
        }
        with self.patch_status(status):
            reason = update_local_peers.active_mining_recreate_guard_reason()
        self.assertIn("active mining detected", reason)
        self.assertIn("4 stratum connection", reason)

    def test_zero_miner_install_does_not_defer_peer_apply(self) -> None:
        status = {
            "can_accept_shares": True,
            "pool": {
                "metrics_active_connections": 0,
                "last_valid_share_age_seconds": 999999,
                "last_submit_age_seconds": 999999,
            },
        }
        with self.patch_status(status):
            self.assertEqual(update_local_peers.active_mining_recreate_guard_reason(), "")

    def test_guard_can_be_disabled_for_explicit_maintenance(self) -> None:
        status = {
            "can_accept_shares": True,
            "pool": {
                "metrics_active_connections": 4,
                "last_valid_share_age_seconds": 2,
                "last_submit_age_seconds": 1,
            },
        }
        with mock.patch.dict("os.environ", {"BDAG_LOCAL_PEERS_DEFER_NODE_RECREATE_WHILE_MINING": "false"}):
            with self.patch_status(status):
                self.assertEqual(update_local_peers.active_mining_recreate_guard_reason(), "")

    def test_explicit_unknown_node_services_do_not_fallback_to_legacy_nodes(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch.object(
                update_local_peers,
                "read_env_values",
                return_value={"BDAG_NODE_SERVICES": "pool-stack-docker-node-1"},
            ):
                self.assertEqual(update_local_peers.configured_active_nodes({}), [])

    def test_missing_node_services_uses_current_single_node_default(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch.object(update_local_peers, "read_env_values", return_value={}):
                self.assertEqual(update_local_peers.configured_active_nodes({}), ["node"])

    def test_extracts_peerstore_startup_log_candidates(self) -> None:
        logs = """
        INFO Try to connect from peer store:{16Uiu2HAmSeedOne: [/ip4/13.57.132.47/tcp/8150 /dns4/excalibur.dagtech.network/tcp/8153]}
        INFO Try to connect from peer store:{16Uiu2HAmSeedTwo: [/ip4/54.214.229.250/tcp/8153 badaddr]}
        """

        self.assertEqual(
            [
                "/ip4/13.57.132.47/tcp/8150/p2p/16Uiu2HAmSeedOne",
                "/dns4/excalibur.dagtech.network/tcp/8153/p2p/16Uiu2HAmSeedOne",
                "/ip4/54.214.229.250/tcp/8153/p2p/16Uiu2HAmSeedTwo",
            ],
            update_local_peers.extract_peerstore_log_peers(logs),
        )

    def test_docker_logs_falls_back_to_compose_service_container(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if command == ["docker", "compose", "ps", "-q", "node"]:
                return subprocess.CompletedProcess(command, 0, "compose-node-id\n", "")
            if command == ["docker", "logs", "--tail", "5000", "node"]:
                return subprocess.CompletedProcess(command, 1, "", "no such container")
            if command == ["docker", "logs", "--tail", "5000", "compose-node-id"]:
                return subprocess.CompletedProcess(command, 0, "Node started p2p server /p2p/localPeer\n", "")
            return subprocess.CompletedProcess(command, 1, "", "unexpected")

        with mock.patch.object(update_local_peers.subprocess, "run", side_effect=fake_run):
            self.assertIn("localPeer", update_local_peers.docker_logs("node"))

        self.assertIn(["docker", "compose", "ps", "-q", "node"], calls)
        self.assertIn(["docker", "logs", "--tail", "5000", "compose-node-id"], calls)


if __name__ == "__main__":
    unittest.main()

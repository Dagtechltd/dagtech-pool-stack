from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("update_local_peers", ROOT / "ops" / "update-local-peers.py")
assert SPEC is not None
assert SPEC.loader is not None
update_local_peers = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(update_local_peers)
TRUST_SPEC = importlib.util.spec_from_file_location("ipfs_segment_trust", ROOT / "ops" / "ipfs_segment_trust.py")
assert TRUST_SPEC is not None
assert TRUST_SPEC.loader is not None
ipfs_segment_trust = importlib.util.module_from_spec(TRUST_SPEC)
TRUST_SPEC.loader.exec_module(ipfs_segment_trust)


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
                return_value={"BDAG_NODE_SERVICE": "pool-stack-docker-node-1"},
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

    def test_docker_top_detects_blockdag_node_child(self) -> None:
        output = "\n".join(
            [
                "PID                 COMMAND             COMMAND",
                "1128104             nodeworker          /usr/local/bin/nodeworker --node-binary=/usr/local/bin/blockdag-node",
                "1128116             blockdag-node       /usr/local/bin/blockdag-node --configfile /etc/bdagStack/node.conf",
            ]
        )

        self.assertTrue(update_local_peers.docker_top_has_bdag_child(output))

    def test_generated_node_peer_addresses_do_not_reseed_candidates(self) -> None:
        generated_peer = "/ip4/199.229.220.118/tcp/60001/p2p/generatedPeer"
        bootstrap_peer = "/ip4/13.57.132.47/tcp/8150/p2p/bootstrapPeer"

        with mock.patch.object(update_local_peers, "read_peer_file", return_value=[]):
            with mock.patch.object(update_local_peers, "node_peerstore_log_candidates", return_value=[]):
                with mock.patch.object(update_local_peers, "peer_tcp_latency", return_value=(True, 1.0)):
                    candidates = update_local_peers.p2p_peer_candidates(
                        {
                            "BDAG_NODE_PEER_ADDRESSES": generated_peer,
                            "BOOTSTRAP_PEER_ADDRESSES": bootstrap_peer,
                        }
                    )

        self.assertEqual([bootstrap_peer], candidates.peers)
        self.assertNotIn("BDAG_NODE_PEER_ADDRESSES", candidates.source_counts)

    def test_curated_node_launch_peers_are_bounded_public_and_peer_id_deduped(self) -> None:
        peers = [
            "/ip4/172.18.0.3/tcp/8150/p2p/privatePeer",
            "/ip4/199.229.220.118/tcp/60001/p2p/peerOne",
            "/ip4/199.229.220.118/tcp/8150/p2p/peerOne",
            "/ip4/199.229.220.118/tcp/8151/p2p/peerOne",
            "/dns4/example.blockdag.test/tcp/8152/p2p/peerTwo",
            "/ip4/13.57.132.47/tcp/8150/p2p/localPeer",
            "/ip4/102.182.77.21/tcp/8151/p2p/peerThree",
            "/ip4/121.91.173.235/tcp/8150/p2p/peerFour",
        ]

        self.assertEqual(
            [
                "/ip4/199.229.220.118/tcp/8150/p2p/peerOne",
                "/dns4/example.blockdag.test/tcp/8152/p2p/peerTwo",
                "/ip4/102.182.77.21/tcp/8151/p2p/peerThree",
            ],
            update_local_peers.curated_node_launch_peers(
                peers,
                {"BDAG_NODE_PEER_LIMIT": "3"},
                {"localPeer"},
            ),
        )

    def test_unsigned_ipfs_peer_roster_does_not_seed_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            values = {
                "BDAG_IPFS_PEER_ROSTER_DEFAULT_CID": "bafk-roster",
                "BDAG_IPFS_PEER_ROSTER_STATUS_FILE": str(pathlib.Path(tmp) / "peer-roster-status.json"),
                "BDAG_IPFS_PEER_ROSTER_REQUIRE_SIGNATURES": "1",
            }
            payload = {
                "document_type": "bdag_ipfs_peer_roster_v1",
                "network": "mainnet",
                "peers": [
                    {"multiaddr": "/ip4/13.57.132.47/tcp/8150/p2p/peerOne"},
                ],
            }

            with mock.patch.object(update_local_peers, "ipfs_cat_json", return_value=payload):
                self.assertEqual([], update_local_peers.ipfs_peer_roster_candidates(values))

            status = json.loads(pathlib.Path(values["BDAG_IPFS_PEER_ROSTER_STATUS_FILE"]).read_text(encoding="utf-8"))

        self.assertEqual(status["state"], "consume_failed")
        self.assertIn("missing required roster_signatures", status["errors"][0])

    def test_publish_peer_roster_writes_signed_bounded_roster(self) -> None:
        seed = "00" * 32
        private_key = ipfs_segment_trust.load_private_key(seed)
        public_hex = ipfs_segment_trust.public_key_hex(private_key)
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            key_file = base / "writer.key"
            key_file.write_text(f"BDAG_IPFS_SEGMENT_SIGNING_KEY_HEX={seed}\n", encoding="utf-8")
            values = {
                "BDAG_IPFS_SEGMENT_WRITER_ID": "writer-a",
                "BDAG_IPFS_SEGMENT_SIGNING_KEY_FILE": str(key_file),
                "BDAG_IPFS_SEGMENT_TRUSTED_SIGNERS": f"writer-a={public_hex}",
                "BDAG_IPFS_PEER_ROSTER_INDEX_PATH": str(base / "peer-roster.json"),
                "BDAG_IPFS_PEER_ROSTER_STATUS_FILE": str(base / "peer-roster-status.json"),
                "BDAG_IPFS_CONTENT_DISCOVERY_FILE": str(base / "discovery.json"),
                "BDAG_IPFS_PEER_ROSTER_PUBLISH_IPFS": "0",
                "BDAG_IPFS_PEER_ROSTER_MAX_PEERS": "1",
            }
            discovery = {
                "peers": [
                    {"status": "tcp-open", "multiaddr": "/ip4/172.18.0.3/tcp/8150/p2p/privatePeer"},
                    {"status": "tcp-open", "multiaddr": "/ip4/127.0.0.1/tcp/8150/p2p/loopbackPeer"},
                    {"status": "tcp-open", "multiaddr": "/ip4/199.229.220.118/tcp/60001/p2p/nonStablePortPeer"},
                    {"status": "tcp-open", "multiaddr": "/ip4/13.57.132.47/tcp/8150/p2p/peerOne"},
                    {"status": "tcp-open", "multiaddr": "/ip4/54.214.229.250/tcp/8151/p2p/peerTwo"},
                    {"status": "closed", "multiaddr": "/ip4/54.214.229.250/tcp/8151/p2p/peerClosed"},
                ]
            }

            update_local_peers.publish_peer_roster(values, discovery)

            roster = json.loads((base / "peer-roster.json").read_text(encoding="utf-8"))
            status = json.loads((base / "peer-roster-status.json").read_text(encoding="utf-8"))

        self.assertEqual(status["state"], "ready")
        self.assertEqual(roster["document_type"], "bdag_ipfs_peer_roster_v1")
        self.assertEqual(roster["network"], "mainnet")
        self.assertEqual(len(roster["peers"]), 1)
        self.assertEqual(roster["peers"][0]["peer_id"], "peerOne")
        self.assertEqual(roster["peers"][0]["publication_filter"], "public_or_dns_stable_p2p_port_one_address_per_peer_id")
        verification = ipfs_segment_trust.verify_payload_signature(
            roster,
            {
                "BDAG_IPFS_SEGMENT_TRUSTED_SIGNERS": f"writer-a={public_hex}",
                "BDAG_IPFS_RESTORE_REQUIRE_SIGNATURES": "1",
            },
            signature_field="roster_signatures",
            context="unit-test roster",
        )
        self.assertEqual(verification["state"], "verified")


if __name__ == "__main__":
    unittest.main()

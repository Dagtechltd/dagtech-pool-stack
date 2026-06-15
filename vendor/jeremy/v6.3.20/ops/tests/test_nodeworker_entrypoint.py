from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "docker" / "entrypoint-nodeworker.sh"


class NodeworkerEntrypointTest(unittest.TestCase):
    def run_entrypoint(self, extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "BDAG_ENTRYPOINT_PRINT_NODE_FLAGS": "1",
        }
        env.update(extra_env)
        with tempfile.TemporaryDirectory() as tmp:
            return subprocess.run(
                [
                    "bash",
                    str(ENTRYPOINT),
                    "/bin/true",
                    f"--node-args=--datadir={tmp}",
                ],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

    def assert_stdout_contains(self, result: subprocess.CompletedProcess[str], needle: str) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(needle, result.stdout)

    def test_print_mode_reports_node_args_append(self) -> None:
        result = self.run_entrypoint({"NODE_ARGS_APPEND": "--miner --maxpeers=160"})

        self.assert_stdout_contains(result, "NODE_ARGS_APPEND=--miner --maxpeers=160")

    def test_print_mode_reports_empty_node_args_append(self) -> None:
        result = self.run_entrypoint({})

        self.assert_stdout_contains(result, "NODE_ARGS_APPEND=")

    def test_print_mode_does_not_emit_removed_sync_flags(self) -> None:
        result = self.run_entrypoint(
            {
                "BDAG_RAWDATADIR_SIDECAR_MODE": "auto",
                "NODE_ARGS_APPEND": "--cache=1024",
            }
        )

        self.assert_stdout_contains(result, "NODE_ARGS_APPEND=--cache=1024")
        combined = result.stdout + result.stderr
        self.assertNotIn("FAST", combined.upper())
        self.assertEqual("", result.stderr)

    def test_node_mining_env_appends_guard_args_without_forcing_rpc_module(self) -> None:
        result = self.run_entrypoint(
            {
                "BDAG_ENABLE_NODE_MINING": "1",
                "BDAG_NODE_MINING_ARGS": (
                    "--miner --miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
                ),
            }
        )

        self.assert_stdout_contains(result, "--miner")
        self.assert_stdout_contains(result, "--miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc")
        combined = result.stdout + result.stderr
        self.assertNotIn("FAST", combined.upper())
        self.assertNotIn("--allowminingwhennearlysynced", result.stdout)
        self.assertNotIn("--allowsubmitwhennotsynced", result.stdout)
        self.assertNotIn("--modules=Blockdag,miner", result.stdout)

    def test_bootstrap_peers_are_deduped_by_peer_id_and_limited(self) -> None:
        result = self.run_entrypoint(
            {
                "BDAG_NODE_PEER_LIMIT": "2",
                "BDAG_NODE_PEER_ADDRESSES": (
                    "/ip4/16.28.133.168/tcp/8150/p2p/peerA,"
                    "/ip4/203.0.113.10/tcp/8150/p2p/peerA,"
                    "/dns4/node.example/tcp/8150/p2p/peerB,"
                    "/ip4/198.51.100.20/tcp/8150/p2p/peerC"
                ),
                "BOOTSTRAP_PEER_ADDRESSES": "/ip4/198.51.100.30/tcp/8150/p2p/peerD",
            }
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--addpeer=/ip4/198.51.100.30/tcp/8150/p2p/peerD", result.stdout)
        self.assertIn("--addpeer=/ip4/16.28.133.168/tcp/8150/p2p/peerA", result.stdout)
        self.assertNotIn("203.0.113.10", result.stdout)
        self.assertNotIn("peerB", result.stdout)
        self.assertNotIn("peerC", result.stdout)


if __name__ == "__main__":
    raise SystemExit(unittest.main())

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "docker" / "entrypoint-nodeworker.sh"


class NodeworkerEntrypointTest(unittest.TestCase):
    def run_entrypoint(
        self,
        extra_env: dict[str, str],
        *,
        supported_node_flags: tuple[str, ...] = ("--nofastsyncserve",),
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "BDAG_ENTRYPOINT_PRINT_NODE_FLAGS": "1",
        }
        env.update(extra_env)
        with tempfile.TemporaryDirectory() as tmp:
            fake_node = Path(tmp) / "fake-node"
            fake_node.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env bash",
                        'if [ "${1:-}" = "--help" ]; then',
                        *[
                            f"  printf '%s\\n' {shlex.quote(flag)}"
                            for flag in supported_node_flags
                        ],
                        "  exit 0",
                        "fi",
                        "exit 0",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            fake_node.chmod(0o755)
            return subprocess.run(
                [
                    "bash",
                    str(ENTRYPOINT),
                    "/bin/true",
                    f"--node-binary={fake_node}",
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
                "SYNC_SOURCE_NODE": "1",
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

        self.assertNotIn("--fastartifactsync", result.stdout)
        self.assert_stdout_contains(result, "--miner")
        self.assert_stdout_contains(result, "--miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc")
        self.assertNotIn("--allowminingwhennearlysynced", result.stdout)
        self.assertNotIn("--allowsubmitwhennotsynced", result.stdout)

    def test_node_mining_env_allows_blockdag_and_miner_rpc_modules(self) -> None:
        result = self.run_entrypoint(
            {
                "BDAG_ENABLE_NODE_MINING": "1",
                "BDAG_NODE_MODULES": "Blockdag,miner",
                "BDAG_NODE_MINING_ARGS": (
                    "--miner --miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
                ),
            }
        )

        self.assert_stdout_contains(result, "--modules=Blockdag")
        self.assert_stdout_contains(result, "--modules=miner")


if __name__ == "__main__":
    raise SystemExit(unittest.main())

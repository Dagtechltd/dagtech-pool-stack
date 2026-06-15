#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT / "scripts" / "render-release-bootstrap.py"
SPEC = importlib.util.spec_from_file_location("render_release_bootstrap", RENDERER)
assert SPEC and SPEC.loader
renderer = importlib.util.module_from_spec(SPEC)
sys.modules["render_release_bootstrap"] = renderer
SPEC.loader.exec_module(renderer)


class BootstrapSelectionTests(unittest.TestCase):
    def test_selects_runtime_payload_for_supported_hosts(self) -> None:
        cases = [
            ("Linux", "x86_64", "linux-amd64"),
            ("Linux", "amd64", "linux-amd64"),
            ("Linux", "arm64", "linux-arm64"),
            ("Linux", "aarch64", "linux-arm64"),
            ("Darwin", "x86_64", "linux-amd64"),
            ("Darwin", "arm64", "linux-arm64"),
            ("windows", "amd64", "linux-amd64"),
            ("windows", "arm64", "linux-arm64"),
        ]
        for os_name, arch, expected in cases:
            with self.subTest(os_name=os_name, arch=arch):
                self.assertEqual(renderer.select_payload_target(os_name, arch), expected)

    def test_rejects_unsupported_bootstrap_selection(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported operating system"):
            renderer.select_payload_target("FreeBSD", "amd64")
        with self.assertRaisesRegex(ValueError, "unsupported CPU architecture"):
            renderer.select_payload_target("Linux", "riscv64")

    def test_generated_bootstraps_are_pinned_to_one_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            result = subprocess.run(
                [
                    sys.executable,
                    str(RENDERER),
                    "--version",
                    "pool-v1.2.3",
                    "--repository",
                    "BlockdagEngineering/stack",
                    "--out-dir",
                    str(out_dir),
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            shell = (out_dir / "install.sh").read_text(encoding="utf-8")
            powershell = (out_dir / "install.ps1").read_text(encoding="utf-8")
        for text in (shell, powershell):
            self.assertIn("pool-v1.2.3", text)
            self.assertIn("releases/download/", text)
            self.assertNotIn("latest/download", text)
        self.assertIn('ASSET="$PACKAGE_NAME-$VERSION-$PAYLOAD_TARGET.zip"', shell)
        self.assertIn("$PackageName-$Version-$PayloadTarget.zip", powershell)


class PayloadInstallerTests(unittest.TestCase):
    def run_unix_env_write(self, payload_arch: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            package_root = Path(tmp)
            shutil.copytree(
                ROOT / "scripts" / "release" / "installers",
                package_root / "installers",
            )
            (package_root / ".env.example").write_text(
                "DOCKER_PLATFORM=linux/amd64\n",
                encoding="utf-8",
                newline="\n",
            )
            (package_root / "release-payload.env").write_text(
                "\n".join(
                    [
                        f"BDAG_RELEASE_PAYLOAD_TARGET=linux-{payload_arch}",
                        f"BDAG_RELEASE_PAYLOAD_ARCH={payload_arch}",
                        f"DOCKER_PLATFORM=linux/{payload_arch}",
                        "",
                    ]
                ),
                encoding="utf-8",
                newline="\n",
            )
            env = os.environ.copy()
            env["BDAG_INSTALL_TEST_WRITE_ENV_ONLY"] = "1"
            result = subprocess.run(
                ["bash", str(package_root / "installers" / "install-unix-common.sh")],
                env=env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return (package_root / ".env").read_text(encoding="utf-8")

    def test_unix_payload_installer_writes_amd64_platform(self) -> None:
        if os.name == "nt":
            self.skipTest("Unix installer env-write smoke is covered by Linux/macOS matrix jobs")
        self.assertIn("DOCKER_PLATFORM=linux/amd64", self.run_unix_env_write("amd64"))

    def test_unix_payload_installer_writes_arm64_platform(self) -> None:
        if os.name == "nt":
            self.skipTest("Unix installer env-write smoke is covered by Linux/macOS matrix jobs")
        self.assertIn("DOCKER_PLATFORM=linux/arm64", self.run_unix_env_write("arm64"))

    def test_installers_do_not_warn_arm_hosts_to_use_amd64_emulation(self) -> None:
        unix = (ROOT / "scripts" / "release" / "installers" / "install-unix-common.sh").read_text(
            encoding="utf-8"
        )
        windows = (
            ROOT / "scripts" / "release" / "installers" / "install-windows.ps1"
        ).read_text(encoding="utf-8")
        for text in (unix, windows):
            self.assertIn("release-payload.env", text)
            self.assertNotIn("amd64 emulation", text)


class BootstrapPeerDefaultTests(unittest.TestCase):
    LIVE_PUBLIC_BOOTSTRAP_PEERS = (
        "/ip4/13.57.132.47/tcp/8150/p2p/"
        "16Uiu2HAmDynYpWjWmgVGf9qVWvDdLnJ3ybVgDmFexizR4zMereus",
        "/ip4/18.142.70.83/tcp/8150/p2p/"
        "16Uiu2HAmBSdn2taoteYwLZJZkDm2iCwL6eQ4UaXYNBBtAwaBU18X",
    )

    def test_release_defaults_pass_bootstrap_peers_to_node(self) -> None:
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        node_conf = (ROOT / "node.conf.example").read_text(encoding="utf-8")

        for peer in self.LIVE_PUBLIC_BOOTSTRAP_PEERS:
            self.assertIn(peer, env_example)
            self.assertIn(f"addpeer={peer}", node_conf)
        self.assertIn("BOOTSTRAP_PEER_ADDRESSES: ${BOOTSTRAP_PEER_ADDRESSES:-}", compose)

    def test_release_defaults_do_not_ship_dead_or_site_local_seed_peers(self) -> None:
        node_conf = (ROOT / "node.conf.example").read_text(encoding="utf-8")

        self.assertNotIn("/ip4/52.8.80.249/tcp/8150/p2p/", node_conf)
        self.assertNotIn("/ip4/192.168.", node_conf)


if __name__ == "__main__":
    raise SystemExit(unittest.main())

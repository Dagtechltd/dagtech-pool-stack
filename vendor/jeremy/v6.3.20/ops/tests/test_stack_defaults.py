#!/usr/bin/env python3

import pathlib
import subprocess
import unittest


ROOT_DIR = pathlib.Path(__file__).resolve().parents[2]


def parse_env(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


class StackDefaultsTests(unittest.TestCase):
    def test_global_scan_window_is_stack_owned(self) -> None:
        defaults = parse_env(ROOT_DIR / "ops/config/stack-defaults.env")
        self.assertEqual(defaults["BDAG_GLOBAL_BLOCK_WINDOW"], "600")

        installer = (ROOT_DIR / "ops/install-dashboard.sh").read_text(encoding="utf-8")
        self.assertIn("BDAG_GLOBAL_BLOCK_WINDOW=$(stack_default BDAG_GLOBAL_BLOCK_WINDOW)", installer)
        self.assertIn("ensure_stack_default_env_value BDAG_GLOBAL_BLOCK_WINDOW", installer)

    def test_shared_status_sampler_is_forced_on_by_installer(self) -> None:
        defaults = parse_env(ROOT_DIR / "ops/config/stack-defaults.env")
        self.assertEqual(defaults["BDAG_STATUS_SAMPLER_ENABLED"], "1")
        self.assertEqual(defaults["BDAG_BACKGROUND_MAINTENANCE_POOL_READY_STATUS_MAX_AGE_SECONDS"], "30")

        installer = (ROOT_DIR / "ops/install-dashboard.sh").read_text(encoding="utf-8")
        self.assertIn("force_stack_default_env_value BDAG_STATUS_SAMPLER_ENABLED", installer)
        self.assertIn(
            "ensure_stack_default_env_value BDAG_BACKGROUND_MAINTENANCE_POOL_READY_STATUS_MAX_AGE_SECONDS",
            installer,
        )

        release_installer = (ROOT_DIR / "ops/release-install.sh").read_text(encoding="utf-8")
        self.assertIn("set_stack_default_env_value .env BDAG_STATUS_SAMPLER_ENABLED", release_installer)
        self.assertIn(
            "set_stack_default_env_value .env BDAG_BACKGROUND_MAINTENANCE_POOL_READY_STATUS_MAX_AGE_SECONDS",
            release_installer,
        )

    def test_native_reference_rpc_defaults_are_stack_owned(self) -> None:
        defaults = parse_env(ROOT_DIR / "ops/config/stack-defaults.env")
        self.assertEqual(defaults["BDAG_NATIVE_REFERENCE_RPC_MODE"], "auto")
        self.assertEqual(defaults["BDAG_NATIVE_REFERENCE_RPC_REMOTE_HOST"], "127.0.0.1")
        self.assertEqual(defaults["BDAG_NATIVE_REFERENCE_RPC_REMOTE_PORT"], "38131")
        self.assertEqual(defaults["BDAG_NATIVE_REFERENCE_RPC_LOCAL_BIND"], "127.0.0.1")
        self.assertEqual(defaults["BDAG_NATIVE_REFERENCE_RPC_LOCAL_PORT"], "38141")
        self.assertEqual(defaults["BDAG_CHAIN_REFERENCE_RPC_URL"], "")
        self.assertEqual(defaults["BDAG_IPFS_SEGMENT_REFERENCE_RPC_URL"], "")

        installer = (ROOT_DIR / "ops/install-p2p-services.sh").read_text(encoding="utf-8")
        self.assertIn("install_native_reference_rpc", installer)
        self.assertIn("BDAG_NATIVE_REFERENCE_RPC_SSH_TARGET", installer)
        self.assertIn("setup_native_reference_rpc.py", installer)

    def test_compose_tip_lag_fallback_matches_stack_default(self) -> None:
        defaults = parse_env(ROOT_DIR / "ops/config/stack-defaults.env")
        compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        expected = (
            "BDAG_GLOBAL_CACHE_MAX_TIP_LAG_BLOCKS: "
            f"${{BDAG_GLOBAL_CACHE_MAX_TIP_LAG_BLOCKS:-{defaults['BDAG_GLOBAL_CACHE_MAX_TIP_LAG_BLOCKS']}}}"
        )
        self.assertIn(expected, compose)

    def test_stack_defaults_validator_passes(self) -> None:
        result = subprocess.run(
            ["python3", "scripts/validate-stack-defaults.py"],
            cwd=ROOT_DIR,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "mining-appliance-preflight.py"
SPEC = importlib.util.spec_from_file_location("mining_appliance_preflight", SCRIPT)
preflight = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = preflight
SPEC.loader.exec_module(preflight)


class MiningAppliancePreflightTest(unittest.TestCase):
    def test_load_env_file_strips_quotes_and_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "# comment",
                        "QUOTED_VALUE='ready'",
                        'BDAG_NODE_CACHE_MB="1024"',
                        "EMPTY=",
                    ]
                ),
                encoding="utf-8",
            )
            env = preflight.load_env_file(env_file)
        self.assertEqual(env["QUOTED_VALUE"], "ready")
        self.assertEqual(env["BDAG_NODE_CACHE_MB"], "1024")
        self.assertEqual(env["EMPTY"], "")

    def test_network_preflight_reports_wired_route_policy(self) -> None:
        old_run = preflight.run
        old_route_policy_script = preflight.route_policy_script

        def fake_run(command, **_kwargs):
            if command[:5] == ["ip", "-o", "-4", "route", "get"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout="1.1.1.1 via 192.168.1.1 dev enx207bd51aa286 src 192.168.1.120 uid 1000 cache\n",
                    stderr="",
                )
            if command and command[0] == "python3":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=(
                        '{"ok": true, "failure_count": 0, "warning_count": 0, "issues": [], '
                        '"selected_default_route": "default via 192.168.1.1 dev enx207bd51aa286 metric 10", '
                        '"route_get": "1.1.1.1 via 192.168.1.1 dev enx207bd51aa286 src 192.168.1.120"}'
                    ),
                    stderr="",
                )
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="unexpected command")

        try:
            preflight.run = fake_run
            preflight.route_policy_script = lambda _root=None: Path("/tmp/validate-network-route-policy.py")
            checks = []
            preflight.check_network(checks, Path("/tmp"))
        finally:
            preflight.run = old_run
            preflight.route_policy_script = old_route_policy_script

        found = {check.name: check.status for check in checks}
        self.assertEqual(found["default_route"], "pass")
        self.assertEqual(found["wired_route_policy"], "pass")

    def test_constrained_env_warnings_for_large_cache_and_expensive_defaults(self) -> None:
        profile = preflight.HostProfile(
            os_name="linux",
            arch="x86_64",
            cpu_count=2,
            memory_bytes=3 * preflight.GIB,
            profile="constrained",
            kernel="test",
        )
        checks = []
        preflight.check_env_defaults(
            checks,
            {
                "BDAG_NODE_CACHE_MB": "4096",
                "NODE_MAX_PEERS": "512",
                "BDAG_SYNC_COORDINATOR_FAST_RESTART_COOLDOWN_SECONDS": "3600",
                "BDAG_STATUS_SAMPLER_ENABLED": "0",
                "BDAG_ADAPTIVE_CONCURRENCY_ENABLED": "0",
                "BDAG_ENTRYPOINT_CHOWN_MODE": "always",
            },
            profile,
        )
        warnings = {check.name for check in checks if check.status == "warn"}
        passes = {check.name for check in checks if check.status == "pass"}
        self.assertIn("active_node_topology", passes)
        self.assertIn("node_cache_budget", warnings)
        self.assertIn("peer_budget", warnings)
        self.assertIn("sync_restart_cooldown", warnings)
        self.assertIn("status_sampler", warnings)
        self.assertIn("adaptive_concurrency", warnings)
        self.assertIn("entrypoint_chown_mode", warnings)

    def test_constrained_mining_profile_accepts_disabled_node_mining(self) -> None:
        profile = preflight.HostProfile(
            os_name="linux",
            arch="aarch64",
            cpu_count=4,
            memory_bytes=8 * preflight.GIB,
            profile="constrained",
            kernel="test",
        )
        checks = []
        preflight.check_env_defaults(
            checks,
            {
                "BDAG_STORAGE_PROFILE": "usb-chain-internal-runtime",
                "BDAG_DETECTED_NETWORK_TOPOLOGY": "asic-router",
                "BDAG_SYNC_COORDINATOR_ACCELERATE_FASTSYNC": "1",
            },
            profile,
        )

        found = {check.name: check for check in checks}
        self.assertEqual(found["node_mining_runtime"].status, "pass")
        self.assertEqual(found["fastsync_acceleration"].status, "pass")

    def test_constrained_mining_profile_warns_without_maxinbound(self) -> None:
        profile = preflight.HostProfile(
            os_name="linux",
            arch="aarch64",
            cpu_count=4,
            memory_bytes=8 * preflight.GIB,
            profile="constrained",
            kernel="test",
        )
        checks = []
        preflight.check_env_defaults(
            checks,
            {
                "BDAG_ENABLE_NODE_MINING": "1",
                "BDAG_NODE_MODULES": "Blockdag",
                "BDAG_NODE_MINING_ARGS": "--miner --miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc",
                "BDAG_STORAGE_PROFILE": "single-usb-constrained",
                "BDAG_DETECTED_NETWORK_TOPOLOGY": "asic-router",
            },
            profile,
        )

        found = {check.name: check for check in checks}
        self.assertEqual(found["node_mining_runtime"].status, "warn")
        self.assertIn("--maxinbound=1", found["node_mining_runtime"].detail)

    def test_pool_template_rpc_pressure_rejects_unsafe_overrides(self) -> None:
        profile = preflight.HostProfile(
            os_name="linux",
            arch="x86_64",
            cpu_count=8,
            memory_bytes=16 * preflight.GIB,
            profile="standard",
            kernel="test",
        )
        checks = []
        preflight.check_env_defaults(
            checks,
            {
                "POOL_GBT_MIN_INTERVAL_MS": "100",
                "POOL_GBT_PRESSURE_INTERVAL_MS": "100",
                "POOL_GBT_PRESSURE_WINDOW_SECONDS": "30",
                "POOL_RPC_ROUTER_NODE_HEALTH_PROBE_SECONDS": "5",
            },
            profile,
        )

        found = {check.name: check for check in checks}
        self.assertEqual(found["pool_template_rpc_pressure"].status, "fail")
        self.assertIn("POOL_GBT_MIN_INTERVAL_MS below 1000ms", found["pool_template_rpc_pressure"].detail)
        self.assertIn("POOL_GBT_PRESSURE_INTERVAL_MS below 250ms", found["pool_template_rpc_pressure"].detail)
        self.assertIn("POOL_RPC_ROUTER_NODE_HEALTH_PROBE_SECONDS below 10s", found["pool_template_rpc_pressure"].detail)

    def test_pool_template_rpc_pressure_accepts_safe_defaults(self) -> None:
        profile = preflight.HostProfile(
            os_name="linux",
            arch="x86_64",
            cpu_count=8,
            memory_bytes=16 * preflight.GIB,
            profile="standard",
            kernel="test",
        )
        checks = []
        preflight.check_env_defaults(
            checks,
            {
                "POOL_GBT_MIN_INTERVAL_MS": "1100",
                "POOL_GBT_PRESSURE_INTERVAL_MS": "500",
                "POOL_GBT_PRESSURE_WINDOW_SECONDS": "10",
                "POOL_RPC_ROUTER_NODE_HEALTH_PROBE_SECONDS": "15",
                "POOL_RPC_ROUTER_NODE_HEALTH_MAX_AGE_SECONDS": "30",
            },
            profile,
        )

        found = {check.name: check for check in checks}
        self.assertEqual(found["pool_template_rpc_pressure"].status, "pass")

    def test_node_mining_runtime_rejects_unsafe_sync_bypass_args(self) -> None:
        profile = preflight.HostProfile(
            os_name="linux",
            arch="x86_64",
            cpu_count=8,
            memory_bytes=16 * preflight.GIB,
            profile="standard",
            kernel="test",
        )
        checks = []
        preflight.check_env_defaults(
            checks,
            {
                "BDAG_ENABLE_NODE_MINING": "1",
                "BDAG_NODE_MODULES": "Blockdag,miner",
                "BDAG_NODE_MINING_ARGS": (
                    "--allowminingwhennearlysynced --allowsubmitwhennotsynced --miner "
                    "--miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
                ),
            },
            profile,
        )

        found = {check.name: check for check in checks}
        self.assertEqual(found["node_mining_runtime"].status, "fail")
        self.assertIn("unsafe sync bypass args", found["node_mining_runtime"].detail)

    def test_constrained_mining_profile_accepts_no_fastsync_serve_policy(self) -> None:
        profile = preflight.HostProfile(
            os_name="linux",
            arch="aarch64",
            cpu_count=4,
            memory_bytes=8 * preflight.GIB,
            profile="constrained",
            kernel="test",
        )
        checks = []
        preflight.check_env_defaults(
            checks,
            {
                "SYNC_SOURCE_NODE": "0",
                "BDAG_STORAGE_PROFILE": "single-usb-constrained",
                "BDAG_DETECTED_NETWORK_TOPOLOGY": "asic-router",
            },
            profile,
        )

        found = {check.name: check for check in checks}
        self.assertEqual(found["node_mining_runtime"].status, "pass")
        self.assertEqual(found["fastsync_acceleration"].status, "pass")

    def test_sync_source_zero_does_not_make_single_device_receiver_constrained(self) -> None:
        profile = preflight.HostProfile(
            os_name="linux",
            arch="x86_64",
            cpu_count=8,
            memory_bytes=16 * preflight.GIB,
            profile="standard",
            kernel="test",
        )
        checks = []
        preflight.check_env_defaults(
            checks,
            {
                "SYNC_SOURCE_NODE": "0",
                "BDAG_STORAGE_PROFILE": "single-device",
            },
            profile,
        )

        found = {check.name: check for check in checks}
        self.assertEqual(found["node_mining_runtime"].status, "pass")
        self.assertEqual(found["node_mining_runtime"].detail, "node mining stays disabled until miners are present")

    def test_node_mining_runtime_accepts_safe_main_chain_args(self) -> None:
        profile = preflight.HostProfile(
            os_name="linux",
            arch="x86_64",
            cpu_count=8,
            memory_bytes=16 * preflight.GIB,
            profile="standard",
            kernel="test",
        )
        checks = []
        preflight.check_env_defaults(
            checks,
            {
                "BDAG_ENABLE_NODE_MINING": "1",
                "BDAG_NODE_MODULES": "Blockdag,miner",
                "BDAG_NODE_MINING_ARGS": "--miner --miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc",
            },
            profile,
        )

        found = {check.name: check for check in checks}
        self.assertEqual(found["node_mining_runtime"].status, "pass")

    def test_node_mining_runtime_rejects_unsynced_flags_without_override(self) -> None:
        profile = preflight.HostProfile(
            os_name="linux",
            arch="x86_64",
            cpu_count=8,
            memory_bytes=16 * preflight.GIB,
            profile="standard",
            kernel="test",
        )
        checks = []
        preflight.check_env_defaults(
            checks,
            {
                "BDAG_ENABLE_NODE_MINING": "1",
                "BDAG_NODE_MODULES": "Blockdag,miner",
                "BDAG_NODE_MINING_ARGS": (
                    "--allowminingwhennearlysynced --allowsubmitwhennotsynced "
                    "--miner --miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
                ),
            },
            profile,
        )

        found = {check.name: check for check in checks}
        self.assertEqual(found["node_mining_runtime"].status, "fail")
        self.assertIn("unsafe sync bypass args", found["node_mining_runtime"].detail)

    def test_usb_chain_storage_profile_reports_split_runtime(self) -> None:
        old_mount_info = preflight.mount_info
        old_is_usb_source = preflight.is_usb_source
        old_docker_root_dir = preflight.docker_root_dir

        def fake_mount_info(path: Path) -> dict[str, str]:
            value = str(path)
            if value.startswith("/mnt/usb"):
                return {"target": "/mnt/usb", "source": "/dev/sda1", "fstype": "f2fs", "options": "rw,noatime,lazytime"}
            return {"target": "/", "source": "/dev/mmcblk0p2", "fstype": "ext4", "options": "rw,relatime"}

        try:
            preflight.mount_info = fake_mount_info
            preflight.is_usb_source = lambda source: source.startswith("/dev/sda")
            preflight.docker_root_dir = lambda: ""
            profile = preflight.HostProfile("linux", "aarch64", 4, 8 * preflight.GIB, "constrained", "test")
            checks = []
            preflight.check_storage_profile(
                checks,
                Path("/opt/blockdag-pool"),
                {
                    "BDAG_CHAIN_DATA_DIR": "/mnt/usb/blockdag-chain",
                    "BDAG_NETWORK_TOPOLOGY": "asic-router",
                    "SYNC_SOURCE_NODE": "1",
                    "MINING_ADDRESS": "0x1111111111111111111111111111111111111111",
                },
                profile,
            )
        finally:
            preflight.mount_info = old_mount_info
            preflight.is_usb_source = old_is_usb_source
            preflight.docker_root_dir = old_docker_root_dir

        found = {check.name: check for check in checks}
        self.assertEqual(found["storage_profile"].status, "pass")
        self.assertEqual(found["storage_io_split"].status, "pass")

    def test_active_node_data_layout_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data" / "node" / "mainnet" / "BdagChain").mkdir(parents=True)
            checks = []
            preflight.check_node_data_layout(checks, root, {})
        found = {check.name: check.status for check in checks}
        self.assertEqual(found["active_node_data_layout"], "pass")

    def test_compose_bind_mount_overrides_default_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.override.yml").write_text(
                "\n".join(
                    [
                        "services:",
                        "  node:",
                        "    volumes:",
                        "      - /srv/bdag-chain-usb:/data:ro",
                        "      - /srv/bdag-chain-usb/node-data:/var/lib/bdagStack/node",
                    ]
                ),
                encoding="utf-8",
            )
            data_dir = preflight.env_data_dir(root, {})
        self.assertEqual(data_dir, Path("/srv/bdag-chain-usb/node-data"))

    def test_named_compose_volume_does_not_override_default_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.yml").write_text(
                "\n".join(
                    [
                        "services:",
                        "  node:",
                        "    volumes:",
                        "      - node-data:/var/lib/bdagStack/node",
                    ]
                ),
                encoding="utf-8",
            )
            data_dir = preflight.env_data_dir(root, {"BDAG_CHAIN_DATA_DIR": "./data"})
        self.assertEqual(data_dir, root / "data")

    def test_live_node_child_passes_when_compose_node_is_absent(self) -> None:
        old_run = preflight.run

        def fake_run(command: list[str], timeout: float = 5.0, cwd: Path | None = None):
            class Result:
                returncode = 0
                stdout = ""
                stderr = ""

            return Result()

        try:
            preflight.run = fake_run
            checks = []
            preflight.check_live_node_child(checks, Path("/tmp"))
        finally:
            preflight.run = old_run

        self.assertEqual(checks[0].status, "pass")

    def test_live_node_child_fails_when_wrapper_has_no_child(self) -> None:
        old_run = preflight.run

        def fake_run(command: list[str], timeout: float = 5.0, cwd: Path | None = None):
            class Result:
                returncode = 0
                stdout = "container-id\n"
                stderr = ""

            result = Result()
            if "exec" in command:
                result.returncode = 1
                result.stdout = ""
            return result

        try:
            preflight.run = fake_run
            checks = []
            preflight.check_live_node_child(checks, Path("/tmp"))
        finally:
            preflight.run = old_run

        self.assertEqual(checks[0].status, "fail")

    def test_storage_profile_passes_when_usb_chain_is_split_from_runtime(self) -> None:
        old_mount_info = preflight.mount_info
        old_is_usb_source = preflight.is_usb_source
        old_docker_root_dir = preflight.docker_root_dir

        def fake_mount_info(path: Path) -> dict[str, str]:
            value = str(path)
            if value.startswith("/mnt/usb"):
                return {"target": "/mnt/usb", "source": "/dev/sda1", "fstype": "f2fs", "options": "rw,noatime,lazytime"}
            return {"target": "/", "source": "/dev/mmcblk0p2", "fstype": "ext4", "options": "rw,relatime"}

        try:
            preflight.mount_info = fake_mount_info
            preflight.is_usb_source = lambda source: source.startswith("/dev/sda")
            preflight.docker_root_dir = lambda: "/var/lib/docker"
            profile = preflight.HostProfile("linux", "aarch64", 4, 4 * preflight.GIB, "constrained", "test")
            checks = []
            preflight.check_storage_profile(
                checks,
                Path("/opt/blockdag-pool"),
                {
                    "BDAG_STORAGE_PROFILE": "auto",
                    "BDAG_CHAIN_DATA_DIR": "/mnt/usb/blockdag-chain",
                    "BDAG_NODE_DATA_DIR": "/mnt/usb/blockdag-chain/node",
                    "BDAG_POSTGRES_DATA_DIR": "/opt/blockdag-pool/runtime-data/postgres",
                    "BDAG_RUNTIME_DIR": "/opt/blockdag-pool/runtime-data/ops-runtime",
                },
                profile,
            )
        finally:
            preflight.mount_info = old_mount_info
            preflight.is_usb_source = old_is_usb_source
            preflight.docker_root_dir = old_docker_root_dir

        found = {check.name: check for check in checks}
        self.assertEqual(found["storage_io_split"].status, "pass")
        self.assertEqual(found["storage_profile"].evidence["resolved_profile"], "usb-chain-internal-runtime")

    def test_storage_profile_warns_when_runtime_shares_usb_chain_device(self) -> None:
        old_mount_info = preflight.mount_info
        old_is_usb_source = preflight.is_usb_source
        old_docker_root_dir = preflight.docker_root_dir

        def fake_mount_info(path: Path) -> dict[str, str]:
            return {"target": "/mnt/usb", "source": "/dev/sda1", "fstype": "f2fs", "options": "rw,noatime,lazytime"}

        try:
            preflight.mount_info = fake_mount_info
            preflight.is_usb_source = lambda source: source.startswith("/dev/sda")
            preflight.docker_root_dir = lambda: "/mnt/usb/docker"
            profile = preflight.HostProfile("linux", "aarch64", 4, 4 * preflight.GIB, "constrained", "test")
            checks = []
            preflight.check_storage_profile(
                checks,
                Path("/opt/blockdag-pool"),
                {
                    "BDAG_STORAGE_PROFILE": "usb-chain-internal-runtime",
                    "BDAG_CHAIN_DATA_DIR": "/mnt/usb/blockdag-chain",
                    "BDAG_NODE_DATA_DIR": "/mnt/usb/blockdag-chain/node",
                    "BDAG_POSTGRES_DATA_DIR": "/mnt/usb/blockdag-chain/runtime/postgres",
                    "BDAG_RUNTIME_DIR": "/mnt/usb/blockdag-chain/runtime/ops-runtime",
                },
                profile,
            )
        finally:
            preflight.mount_info = old_mount_info
            preflight.is_usb_source = old_is_usb_source
            preflight.docker_root_dir = old_docker_root_dir

        found = {check.name: check.status for check in checks}
        self.assertEqual(found["storage_io_split"], "warn")
        self.assertEqual(found["explicit_storage_profile_mismatch"], "warn")
        self.assertEqual(found["docker_chain_shared_device"], "warn")

    def test_usb_chain_split_is_preferred_on_standard_hosts_too(self) -> None:
        old_mount_info = preflight.mount_info
        old_is_usb_source = preflight.is_usb_source
        old_docker_root_dir = preflight.docker_root_dir

        def fake_mount_info(path: Path) -> dict[str, str]:
            value = str(path)
            if value.startswith("/mnt/usb"):
                return {"target": "/mnt/usb", "source": "/dev/sda1", "fstype": "f2fs", "options": "rw,noatime,lazytime"}
            return {"target": "/", "source": "/dev/nvme0n1p2", "fstype": "ext4", "options": "rw,relatime"}

        try:
            preflight.mount_info = fake_mount_info
            preflight.is_usb_source = lambda source: source.startswith("/dev/sda")
            preflight.docker_root_dir = lambda: "/mnt/usb/docker"
            profile = preflight.HostProfile("linux", "x86_64", 8, 16 * preflight.GIB, "standard", "test")
            checks = []
            preflight.check_storage_profile(
                checks,
                Path("/opt/blockdag-pool"),
                {
                    "BDAG_STORAGE_PROFILE": "auto",
                    "BDAG_CHAIN_DATA_DIR": "/mnt/usb/blockdag-chain",
                    "BDAG_NODE_DATA_DIR": "/mnt/usb/blockdag-chain/node",
                    "BDAG_POSTGRES_DATA_DIR": "/mnt/usb/runtime/postgres",
                    "BDAG_RUNTIME_DIR": "/mnt/usb/runtime/ops-runtime",
                },
                profile,
            )
        finally:
            preflight.mount_info = old_mount_info
            preflight.is_usb_source = old_is_usb_source
            preflight.docker_root_dir = old_docker_root_dir

        found = {check.name: check.status for check in checks}
        self.assertEqual(found["storage_io_split"], "warn")
        self.assertEqual(found["docker_chain_shared_device"], "warn")

    def test_ephemeral_tmpfs_passes_for_run_backed_paths(self) -> None:
        old_mount_info = preflight.mount_info
        old_is_usb_source = preflight.is_usb_source

        def fake_mount_info(path: Path) -> dict[str, str]:
            return {"target": "/run", "source": "tmpfs", "fstype": "tmpfs", "options": "rw,nosuid,nodev"}

        try:
            preflight.mount_info = fake_mount_info
            preflight.is_usb_source = lambda source: False
            checks = []
            preflight.check_ephemeral_storage(checks, Path("/opt/blockdag-pool"), {"BDAG_EPHEMERAL_DIR": "/run/bdag-pool"})
        finally:
            preflight.mount_info = old_mount_info
            preflight.is_usb_source = old_is_usb_source

        found = {check.name: check.status for check in checks}
        self.assertEqual(found["ephemeral_tmpfs"], "pass")

    def test_ephemeral_tmpfs_warns_for_disk_backed_scratch(self) -> None:
        old_mount_info = preflight.mount_info
        old_is_usb_source = preflight.is_usb_source

        def fake_mount_info(path: Path) -> dict[str, str]:
            return {"target": "/", "source": "/dev/sda1", "fstype": "ext4", "options": "rw,relatime"}

        try:
            preflight.mount_info = fake_mount_info
            preflight.is_usb_source = lambda source: source.startswith("/dev/sda")
            checks = []
            preflight.check_ephemeral_storage(checks, Path("/opt/blockdag-pool"), {"BDAG_EPHEMERAL_DIR": "/opt/blockdag-pool/tmp"})
        finally:
            preflight.mount_info = old_mount_info
            preflight.is_usb_source = old_is_usb_source

        found = {check.name: check.status for check in checks}
        self.assertEqual(found["ephemeral_tmpfs"], "warn")

    def test_disk_io_guard_warns_for_tmpfs_build_tmpdir_and_large_cache(self) -> None:
        old_disk_usage = preflight.disk_usage
        old_storage_device = preflight.storage_device
        old_docker_system_df = preflight.docker_system_df

        try:
            preflight.disk_usage = lambda _path: {"free_bytes": 20 * preflight.GIB, "free_gib": 20.0}
            preflight.storage_device = lambda path: {
                "path": str(path),
                "fstype": "tmpfs" if str(path).startswith("/run") else "ext4",
            }
            preflight.docker_system_df = lambda: [{"Type": "Build Cache", "Size": "5GB"}]
            checks = []
            preflight.check_disk_io_noise_guard(
                checks,
                Path("/opt/blockdag-pool"),
                {
                    "BDAG_BUILD_TMPDIR": "/run/bdag-pool/tmp",
                    "BDAG_BUILD_CACHE_WARN_GIB": "4",
                },
                preflight.HostProfile("linux", "x86_64", 2, 3 * preflight.GIB, "constrained", "test"),
            )
        finally:
            preflight.disk_usage = old_disk_usage
            preflight.storage_device = old_storage_device
            preflight.docker_system_df = old_docker_system_df

        found = {check.name: check.status for check in checks}
        self.assertEqual(found["disk_io_root_free_space_guard"], "pass")
        self.assertEqual(found["build_tmpdir_not_tmpfs"], "warn")
        self.assertEqual(found["docker_build_cache_budget"], "warn")

    def test_docker_system_df_timeout_is_treated_as_unavailable(self) -> None:
        old_run = preflight.run

        try:
            preflight.run = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(["docker", "system", "df"], timeout=15)
            )
            self.assertEqual(preflight.docker_system_df(), [])
        finally:
            preflight.run = old_run

    def test_disk_io_guard_accepts_capacity_build_tmpdir_and_small_cache(self) -> None:
        old_disk_usage = preflight.disk_usage
        old_storage_device = preflight.storage_device
        old_docker_system_df = preflight.docker_system_df
        old_same_filesystem = preflight.same_filesystem

        try:
            preflight.disk_usage = lambda _path: {"free_bytes": 20 * preflight.GIB, "free_gib": 20.0}
            preflight.storage_device = lambda path: {
                "path": str(path),
                "fstype": "ext4",
            }
            preflight.docker_system_df = lambda: [{"Type": "Build Cache", "Size": "512MB"}]
            preflight.same_filesystem = lambda _left, _right: False
            checks = []
            preflight.check_disk_io_noise_guard(
                checks,
                Path("/opt/blockdag-pool"),
                {"BDAG_BUILD_TMPDIR": "/srv/bdag-pool-storage/build-tmp"},
                preflight.HostProfile("linux", "x86_64", 2, 3 * preflight.GIB, "constrained", "test"),
            )
        finally:
            preflight.disk_usage = old_disk_usage
            preflight.storage_device = old_storage_device
            preflight.docker_system_df = old_docker_system_df
            preflight.same_filesystem = old_same_filesystem

        found = {check.name: check.status for check in checks}
        self.assertEqual(found["disk_io_root_free_space_guard"], "pass")
        self.assertEqual(found["build_tmpdir_not_tmpfs"], "pass")
        self.assertEqual(found["docker_build_cache_budget"], "pass")


if __name__ == "__main__":
    unittest.main()

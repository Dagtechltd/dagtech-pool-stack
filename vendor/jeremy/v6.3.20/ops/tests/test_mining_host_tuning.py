#!/usr/bin/env python3

import pathlib
import unittest


ROOT_DIR = pathlib.Path(__file__).resolve().parents[2]


def read(rel: str) -> str:
    return (ROOT_DIR / rel).read_text(encoding="utf-8")


class MiningHostTuningTests(unittest.TestCase):
    def test_tuning_script_targets_compose_services_and_cgroup_controls(self) -> None:
        script = read("ops/apply-mining-host-tuning.sh")

        self.assertIn("pool_metrics_url=\"${BDAG_POOL_METRICS_URL:-http://127.0.0.1:9090/metrics}\"", script)
        self.assertIn("compose_service_container()", script)
        self.assertIn("label=com.docker.compose.service=$service", script)
        self.assertIn("container_cgroup_root()", script)
        self.assertIn("memory.low", script)
        self.assertIn('node_memory_high_percent="${BDAG_NODE_MEMORY_HIGH_PERCENT:-60}"', script)
        self.assertIn('node_memory_high_min="${BDAG_NODE_MEMORY_HIGH_MIN:-3072M}"', script)
        self.assertIn("cpu.weight", script)
        self.assertIn("io.weight", script)
        self.assertIn("BDAG_TUNE_NET_QDISC", script)
        self.assertIn("active_lan_ifaces()", script)
        self.assertIn("network_ifaces()", script)
        self.assertIn("fq_codel target 5ms interval 100ms ecn", script)

    def test_compose_defaults_keep_critical_path_above_dashboard(self) -> None:
        compose = read("docker-compose.yml")
        stack_defaults = read("ops/config/stack-defaults.env")

        self.assertIn("BDAG_NODE_CPU_SHARES=6144", stack_defaults)
        self.assertIn("BDAG_POOL_CPU_SHARES=5120", stack_defaults)
        self.assertIn("BDAG_DASHBOARD_CPU_SHARES=128", stack_defaults)
        self.assertIn("cpu_shares: 4096", compose)
        self.assertIn("cpu_shares: 3072", compose)
        self.assertIn("cpu_shares: 256", compose)
        self.assertIn("weight: 1000", compose)
        self.assertIn("weight: 900", compose)
        self.assertIn("weight: 100", compose)
        self.assertIn("shm_size: ${BDAG_NODE_SHM_SIZE:-512m}", compose)

    def test_env_example_exposes_priority_knobs(self) -> None:
        env_example = read(".env.example")

        for name in (
            "BDAG_NODE_CPU_SHARES=6144",
            "BDAG_POOL_CPU_SHARES=5120",
            "BDAG_POOL_DB_CPU_SHARES=4096",
            "BDAG_DASHBOARD_CPU_SHARES=128",
            "BDAG_NODE_MEMORY_LOW=768M",
            "BDAG_POOL_MEMORY_LOW=256M",
            "BDAG_POOL_DB_MEMORY_LOW=512M",
            "BDAG_DASHBOARD_MEMORY_LOW=64M",
            "BDAG_TUNE_NET_QDISC=1",
            "BDAG_INSTALL_APPLIANCE_HOST_PROFILE=1",
            "BDAG_INSTALL_STACK_SUPPORT_SERVICES=1",
        ):
            self.assertIn(name, env_example)

    def test_release_installer_persists_priority_knobs(self) -> None:
        installer = read("ops/release-install.sh")
        p2p_installer = read("ops/install-p2p-services.sh")

        for snippet in (
            "set_env_value .env BDAG_NODE_CPU_SHARES",
            "set_env_value .env BDAG_POOL_CPU_SHARES",
            "set_env_value .env BDAG_POOL_DB_CPU_SHARES",
            "set_env_value .env BDAG_DASHBOARD_CPU_SHARES",
            "set_env_value .env BDAG_NODE_MEMORY_LOW",
            "set_env_value .env BDAG_POOL_MEMORY_LOW",
            "set_env_value .env BDAG_POOL_DB_MEMORY_LOW",
            "set_env_value .env BDAG_DASHBOARD_MEMORY_LOW",
            "set_env_value .env BDAG_TUNE_NET_QDISC",
            "set_env_value .env BDAG_NODE_TMPFS_SIZE",
            "set_env_value .env BDAG_CONTAINER_TMPFS_SIZE",
            "install_appliance_host_profile",
            "install_stack_support_services",
            "BDAG_INSTALL_APPLIANCE_PROFILE_STRICT",
            "BDAG_INSTALL_STACK_SUPPORT_SERVICES_STRICT",
        ):
            self.assertIn(snippet, installer)
        self.assertIn("BDAG_NODE_MEMORY_HIGH_PERCENT=%s", p2p_installer)
        self.assertIn("$(env_value BDAG_NODE_MEMORY_HIGH_PERCENT 60)", p2p_installer)
        self.assertIn("$(env_value BDAG_NODE_MEMORY_HIGH_MIN 3072M)", p2p_installer)

    def test_host_profile_installer_preserves_invalid_docker_daemon_config(self) -> None:
        installer = read("scripts/install-mining-appliance-profile.sh")

        self.assertIn("dst.parent.mkdir(parents=True, exist_ok=True)", installer)
        self.assertIn("except json.JSONDecodeError", installer)
        self.assertIn(".invalid", installer)
        self.assertIn("WARNING: moved invalid Docker daemon config", installer)

    def test_dashboard_installer_persists_priority_env_for_upgrades(self) -> None:
        installer = read("ops/install-dashboard.sh")

        for key in (
            "BDAG_CONTAINER_TMPFS_SIZE",
            "BDAG_NODE_TMPFS_SIZE",
            "BDAG_NODE_CPU_SHARES",
            "BDAG_POOL_CPU_SHARES",
            "BDAG_POOL_DB_CPU_SHARES",
            "BDAG_DASHBOARD_CPU_SHARES",
            "BDAG_NODE_MEMORY_LOW",
            "BDAG_POOL_MEMORY_LOW",
            "BDAG_POOL_DB_MEMORY_LOW",
            "BDAG_DASHBOARD_MEMORY_LOW",
            "BDAG_TUNE_NET_QDISC",
        ):
            self.assertIn(f"ensure_stack_default_env_value {key}", installer)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3

import pathlib
import unittest


ROOT_DIR = pathlib.Path(__file__).resolve().parents[2]


def read(rel: str) -> str:
    return (ROOT_DIR / rel).read_text(encoding="utf-8")


class StackNamingCoherenceTests(unittest.TestCase):
    def test_compose_dashboard_exports_current_container_names(self) -> None:
        compose = read("docker-compose.yml")

        self.assertIn("  pool-db:", compose)
        self.assertIn("  node:", compose)
        self.assertIn("  pool:", compose)
        self.assertIn("BDAG_NODE_SERVICE: node", compose)
        self.assertIn("BDAG_STACK_SERVICES: postgres,node,pool", compose)
        self.assertIn("BDAG_START_SERVICES: postgres,node,pool", compose)
        self.assertIn("BDAG_ASIC_EXPECTED_MACS: ${BDAG_ASIC_EXPECTED_MACS:-}", compose)
        self.assertIn("POOL_ASIC_MAC_OVERRIDES: ${POOL_ASIC_MAC_OVERRIDES:-}", compose)
        self.assertIn("BDAG_ASIC_LAN_CIDRS: ${BDAG_ASIC_LAN_CIDRS:-}", compose)
        self.assertIn("BDAG_DOCKER_BRIDGE_CIDRS: ${BDAG_DOCKER_BRIDGE_CIDRS:-172.16.0.0/12}", compose)
        self.assertIn("BDAG_ALLOW_DOCKER_BRIDGE_ASIC_IPS: ${BDAG_ALLOW_DOCKER_BRIDGE_ASIC_IPS:-0}", compose)
        self.assertIn("BDAG_POOL_CONTAINER: pool", compose)
        self.assertIn("BDAG_POOL_DB_CONTAINER: postgres", compose)
        self.assertIn("BDAG_NODE_RPC_URL: http://node:38131", compose)
        self.assertIn("BDAG_COLLECTOR_DIRECT_STATUS_FALLBACK: ${BDAG_COLLECTOR_DIRECT_STATUS_FALLBACK:-0}", compose)
        self.assertIn("BDAG_COLLECTOR_STATUS_CACHE_SECONDS: ${BDAG_COLLECTOR_STATUS_CACHE_SECONDS:-120}", compose)
        self.assertIn("BDAG_COLLECTOR_SAMPLER_CACHE_SECONDS: ${BDAG_COLLECTOR_SAMPLER_CACHE_SECONDS:-120}", compose)
        self.assertIn("BDAG_STATUS_PAYLOAD_STALE_AFTER_SECONDS: ${BDAG_STATUS_PAYLOAD_STALE_AFTER_SECONDS:-120}", compose)

    def test_env_examples_and_installer_use_current_names(self) -> None:
        env_example = read(".env.example")
        portable = read("ops/portable.env.example")
        installer = read("ops/install-dashboard.sh")

        for text in (env_example, portable):
            self.assertIn("BDAG_POOL_CONTAINER=pool", text)
            self.assertIn("BDAG_POOL_DB_CONTAINER=postgres", text)
            self.assertIn("BDAG_NODE_SERVICE=node", text)
            self.assertIn("BDAG_STACK_SERVICES=postgres,node,pool", text)
            self.assertIn("BDAG_START_SERVICES=postgres,node,pool", text)
            self.assertIn("BDAG_ASIC_EXPECTED_MACS=", text)
            self.assertIn("POOL_ASIC_MAC_OVERRIDES=", text)
            self.assertIn("BDAG_STATUS_PAYLOAD_STALE_AFTER_SECONDS=120", text)
            self.assertIn("BDAG_STATUS_SAMPLER_MAX_AGE_SECONDS=120", text)
        self.assertIn("POOL_GBT_MIN_INTERVAL_MS=1100", env_example)
        self.assertIn("POOL_GBT_PRESSURE_INTERVAL_MS=500", env_example)
        self.assertIn("POOL_GBT_PRESSURE_WINDOW_SECONDS=10", env_example)
        self.assertIn("POOL_RPC_ROUTER_NODE_HEALTH_PROBE_SECONDS=15", env_example)
        self.assertIn("POOL_RPC_ROUTER_NODE_HEALTH_MAX_AGE_SECONDS=30", env_example)
        self.assertIn("BDAG_CHAIN_PEERSTORE_PEER_EXTRACTION_ENABLED=1", env_example)
        self.assertIn("NODE_RPC_URL=http://node:38131", env_example)
        self.assertIn("NODE_RPC_URL=http://127.0.0.1:38131", portable)

        self.assertIn("BDAG_POOL_CONTAINER=$(stack_default BDAG_POOL_CONTAINER)", installer)
        self.assertIn("BDAG_POOL_DB_CONTAINER=$(stack_default BDAG_POOL_DB_CONTAINER)", installer)
        self.assertIn("BDAG_NODE_SERVICE=$(stack_default BDAG_NODE_SERVICE)", installer)
        self.assertIn("BDAG_STACK_SERVICES=$(stack_default BDAG_STACK_SERVICES)", installer)
        self.assertIn("BDAG_START_SERVICES=$(stack_default BDAG_START_SERVICES)", installer)
        self.assertIn("BDAG_ASIC_EXPECTED_MACS=$(stack_default BDAG_ASIC_EXPECTED_MACS)", installer)
        self.assertIn("POOL_ASIC_MAC_OVERRIDES=$(stack_default POOL_ASIC_MAC_OVERRIDES)", installer)
        self.assertIn("BDAG_DASHBOARD_DIRECT_STATUS_FALLBACK=$(stack_default BDAG_DASHBOARD_DIRECT_STATUS_FALLBACK)", installer)
        self.assertIn("BDAG_DASHBOARD_STATUS_CACHE_SECONDS=$(stack_default BDAG_DASHBOARD_STATUS_CACHE_SECONDS)", installer)
        self.assertIn("BDAG_DASHBOARD_SAMPLER_CACHE_SECONDS=$(stack_default BDAG_DASHBOARD_SAMPLER_CACHE_SECONDS)", installer)
        self.assertIn("BDAG_STATUS_PAYLOAD_STALE_AFTER_SECONDS=$(stack_default BDAG_STATUS_PAYLOAD_STALE_AFTER_SECONDS)", installer)
        self.assertIn("BDAG_CHAIN_PEERSTORE_PEER_EXTRACTION_ENABLED=$(stack_default BDAG_CHAIN_PEERSTORE_PEER_EXTRACTION_ENABLED)", installer)
        self.assertIn("ensure_stack_default_env_value BDAG_STACK_SERVICES", installer)
        self.assertIn("ensure_stack_default_env_value POOL_ASIC_MAC_OVERRIDES", installer)
        self.assertIn("ensure_stack_default_env_value BDAG_CHAIN_PEERSTORE_PEER_EXTRACTION_ENABLED", installer)
        self.assertIn("ensure_stack_default_env_value BDAG_ASIC_EXPECTED_MACS", installer)
        self.assertIn("ensure_stack_default_env_value BDAG_STATUS_SAMPLER_MAX_AGE_SECONDS", installer)
        self.assertIn("ensure_stack_default_env_value BDAG_DASHBOARD_DIRECT_STATUS_FALLBACK", installer)
        self.assertIn(".config/autostart", installer)
        self.assertIn("codex-auto-resume.desktop", installer)
        self.assertIn("detect_codex_resume_session_id", installer)
        self.assertIn("ensure_env_value BDAG_CODEX_AUTO_RESUME_CHECK_WAIT_SECONDS 60", installer)
        self.assertIn("ensure_env_value BDAG_CODEX_AUTO_RESUME_CHECK_INTERVAL_SECONDS 10", installer)
        self.assertIn("ensure_codex_trusted_project", installer)
        self.assertIn('trust_level = "trusted"', installer)

    def test_release_installer_generates_current_runtime_topology(self) -> None:
        compose = read("docker-compose.yml")
        installer = read("ops/release-install.sh")

        self.assertIn("  pool:", compose)
        self.assertIn("  node:", compose)
        self.assertIn("  postgres:", compose)
        self.assertNotIn("container_name:", compose)
        self.assertIn("NODE_RPC_URL: http://node:38131", compose)
        self.assertIn("BDAG_STACK_SERVICES: postgres,node,pool", compose)
        self.assertIn("BDAG_ASIC_EXPECTED_MACS: ${BDAG_ASIC_EXPECTED_MACS:-}", compose)
        self.assertIn("POOL_ASIC_MAC_OVERRIDES: ${POOL_ASIC_MAC_OVERRIDES:-}", compose)
        self.assertIn("BDAG_ASIC_LAN_CIDRS: ${BDAG_ASIC_LAN_CIDRS:-}", compose)
        self.assertIn("POOL_GBT_MIN_INTERVAL_MS: ${POOL_GBT_MIN_INTERVAL_MS:-1100}", compose)
        self.assertIn("POOL_GBT_PRESSURE_INTERVAL_MS: ${POOL_GBT_PRESSURE_INTERVAL_MS:-500}", compose)
        self.assertIn("POOL_GBT_PRESSURE_WINDOW_SECONDS: ${POOL_GBT_PRESSURE_WINDOW_SECONDS:-10}", compose)
        self.assertIn("POOL_RPC_ROUTER_NODE_HEALTH_PROBE_SECONDS: ${POOL_RPC_ROUTER_NODE_HEALTH_PROBE_SECONDS:-15}", compose)
        self.assertIn("POOL_RPC_ROUTER_NODE_HEALTH_MAX_AGE_SECONDS: ${POOL_RPC_ROUTER_NODE_HEALTH_MAX_AGE_SECONDS:-30}", compose)
        self.assertIn("set_stack_default_env_value .env BDAG_NODE_SERVICE", installer)
        self.assertIn("set_stack_default_env_value .env BDAG_STACK_SERVICES", installer)
        self.assertIn("set_stack_default_env_value .env BDAG_START_SERVICES", installer)
        self.assertIn("set_stack_default_env_value .env BDAG_ASIC_EXPECTED_MACS", installer)
        self.assertIn("set_stack_default_env_value .env POOL_ASIC_MAC_OVERRIDES", installer)
        self.assertIn('set_env_value .env NODE_RPC_URL "http://node:38131"', installer)
        self.assertIn("set_stack_default_env_value .env POOL_GBT_MIN_INTERVAL_MS", installer)
        self.assertIn("set_stack_default_env_value .env POOL_GBT_PRESSURE_INTERVAL_MS", installer)
        self.assertIn("set_stack_default_env_value .env POOL_RPC_ROUTER_NODE_HEALTH_PROBE_SECONDS", installer)

    def test_asic_mac_identity_architecture_is_stack_level_rule(self) -> None:
        agents = read("AGENTS.md")
        doc = read("docs/asic-mac-identity-architecture.html")
        pool_ops = read("ops/pool_ops.py")
        sampler = read("ops/status_sampler.py")
        watchdog = read("ops/watchdog.py")
        mining_guard = read("ops/mining_guard_30min.py")

        self.assertIn("Source-level imperative: physical ASIC miners and ASIC work lanes", agents)
        self.assertIn("physical ASIC miners and ASIC work lanes use MAC identity only", doc)
        self.assertIn("POOL_ASIC_MAC_OVERRIDES", doc)
        self.assertIn("BDAG_ASIC_LAN_CIDRS", doc)
        self.assertIn("BDAG_ASIC_EXPECTED_MACS", pool_ops)
        self.assertIn("Remote Stratum clients outside the configured", agents)
        self.assertIn("ASIC LAN may use IP-based operational identity", agents)
        self.assertIn('"identity_basis": "mac"', pool_ops)
        self.assertIn("def pool_asic_mac_overrides_value", pool_ops)
        self.assertIn("POOL_ASIC_MAC_OVERRIDES", sampler)
        self.assertIn("def desired_asic_lan_cidrs_value", sampler)
        self.assertIn("BDAG_ASIC_LAN_CIDRS", sampler)
        self.assertIn("last_miner_restart_at_by_identity", watchdog)
        self.assertIn("BDAG_EXPECTED_ASIC_MAC", mining_guard)

    def test_watchdogs_default_to_current_names(self) -> None:
        pool_ops = read("ops/pool_ops.py")
        sampler = read("ops/status_sampler.py")
        node_guard = read("ops/node_child_guard.py")
        host_guard = read("host/mining-appliance/bdag-node-child-guard")
        peer_refresh = read("ops/update-local-peers.py")

        self.assertIn('POOL_CONTAINER = os.environ.get("BDAG_POOL_CONTAINER", "pool")', pool_ops)
        self.assertIn('POOL_DB_CONTAINER = os.environ.get("BDAG_POOL_DB_CONTAINER", "postgres")', pool_ops)
        self.assertIn('NODE_SERVICE = single_env_value("BDAG_NODE_SERVICE", "node")', pool_ops)
        self.assertIn("def sync_priority_decision", pool_ops)
        self.assertIn('"postgres,node,pool"', pool_ops)
        self.assertIn('config_value("BDAG_NODE_SERVICE", "node")', sampler)
        self.assertIn('DEFAULT_NODE_CHILD_GUARD_NODE = "node"', node_guard)
        self.assertIn('DEFAULT_NODE_CHILD_GUARD_NODE = "node"', host_guard)
        self.assertIn("blockdag-node", node_guard)
        self.assertIn("blockdag-node", host_guard)
        self.assertIn('DEFAULT_ACTIVE_NODE_SERVICE = "node"', peer_refresh)

    def test_systemd_watchdogs_share_current_names_and_sampler_defaults(self) -> None:
        root_dashboard = read("ops/systemd/bdag-dashboard.service")
        root_watchdog = read("ops/systemd/bdag-watchdog.service")
        root_sampler = read("ops/systemd/bdag-status-sampler.service")
        user_dashboard = read("ops/systemd/user-bdag-dashboard.service")
        user_watchdog = read("ops/systemd/user-bdag-watchdog.service")
        user_sampler = read("ops/systemd/user-bdag-status-sampler.service")
        user_codex_handoff = read("ops/systemd/user-bdag-codex-boot-handoff.service")
        user_codex_auto_resume = read("ops/systemd/user-bdag-codex-auto-resume.service")
        sentinel = read("ops/stack_sentinel.py")
        installer = read("ops/install-dashboard.sh")

        for unit in (root_dashboard, root_watchdog, root_sampler, user_dashboard, user_watchdog, user_sampler):
            self.assertRegex(unit, r"BDAG_NODE_SERVICES?=node")
            self.assertIn("BDAG_STACK_SERVICES=postgres,node,pool", unit)
            self.assertIn("BDAG_POOL_CONTAINER=pool", unit)
            self.assertIn("BDAG_POOL_DB_CONTAINER=postgres", unit)

        self.assertIn("bdag-status-sampler.service", root_dashboard)
        self.assertIn("bdag-status-sampler.service", root_watchdog)
        self.assertIn("BDAG_STATUS_SAMPLER_MAX_AGE_SECONDS=120", root_sampler)
        self.assertIn("BDAG_STATUS_PAYLOAD_STALE_AFTER_SECONDS=120", root_sampler)
        self.assertIn("BDAG_MINING_IMPERATIVE_GUARD_UNITS=", root_sampler)

        for unit in (user_dashboard, user_watchdog):
            self.assertIn("bdag-status-sampler.service", unit)
            self.assertIn("ops/runtime/ops.env", unit)
        self.assertIn("BDAG_STATUS_SAMPLER_MAX_AGE_SECONDS=120", user_sampler)
        self.assertIn("BDAG_STATUS_PAYLOAD_STALE_AFTER_SECONDS=120", user_sampler)
        self.assertIn("bdag-boot-repair.service", user_codex_handoff)
        self.assertIn("bdag-dashboard.service", user_codex_handoff)
        self.assertIn("codex_boot_handoff.py --repair", user_codex_handoff)
        self.assertIn("SuccessExitStatus=2", user_codex_handoff)
        self.assertIn("ops/runtime/ops.env", user_codex_handoff)
        self.assertIn("graphical-session.target", user_codex_auto_resume)
        self.assertNotIn("bdag-codex-boot-handoff.service", user_codex_auto_resume)
        self.assertIn("codex_auto_resume.py", user_codex_auto_resume)
        self.assertIn("WantedBy=graphical-session.target", user_codex_auto_resume)
        self.assertIn("ops/runtime/ops.env", user_codex_auto_resume)
        self.assertIn('loginctl enable-linger "$(id -un)"', installer)
        self.assertIn(
            '"bdag-status-sampler.service,bdag-watchdog.service,bdag-p2p-guard.service"',
            sentinel,
        )
        self.assertIn(
            '${INSTANCE}-status-sampler.service,${INSTANCE}-watchdog.service,${INSTANCE}-p2p-guard.service',
            installer,
        )
        self.assertNotIn(
            '${INSTANCE}-dashboard.service,${INSTANCE}-watchdog.service,${INSTANCE}-node-child-guard.service,'
            '${INSTANCE}-p2p-guard.service',
            installer,
        )
        self.assertIn(
            '${INSTANCE}-stack-sentinel.timer,${INSTANCE}-sync-coordinator.timer,'
            '${INSTANCE}-chain-restore-guard.timer,${INSTANCE}-local-peers.timer,'
            '${INSTANCE}-mining-30min-guard.timer',
            installer,
        )
        self.assertIn('${INSTANCE}-local-peers.timer', installer)
        self.assertIn(
            '"bdag-stack-sentinel.timer,bdag-sync-coordinator.timer,bdag-chain-restore-guard.timer,"',
            sentinel,
        )
        self.assertIn('"bdag-local-peers.timer,bdag-mining-30min-guard.timer"', sentinel)
        self.assertNotIn(
            '"bdag-dashboard.service,bdag-watchdog.service,bdag-p2p-guard.service"',
            sentinel,
        )
        self.assertNotIn("bdag-dashboard.service,bdag-watchdog.service,bdag-p2p-guard.service", sentinel)

    def test_validator_locks_current_topology_into_build_checks(self) -> None:
        validator = read("scripts/validate-pi5-restart-hardening.sh")

        self.assertIn('need_grep \'BDAG_STACK_SERVICES=postgres,node,pool\' ".env.example"', validator)
        self.assertIn('need_grep \'BDAG_START_SERVICES=postgres,node,pool\' ".env.example"', validator)
        self.assertIn('need_grep \'BDAG_ASIC_EXPECTED_MACS=\' ".env.example"', validator)
        self.assertIn('need_grep \'POOL_ASIC_MAC_OVERRIDES=\' ".env.example"', validator)
        self.assertIn('need_file "docs/asic-mac-identity-architecture.html"', validator)
        self.assertIn("physical ASIC miners and ASIC work lanes use MAC identity only", validator)
        self.assertIn("Source-level imperative: physical ASIC miners and ASIC work lanes", validator)
        self.assertIn('need_grep \'BDAG_NODE_SERVICE: node\' "docker-compose.yml"', validator)
        self.assertIn('need_grep \'BDAG_ASIC_EXPECTED_MACS: \\${BDAG_ASIC_EXPECTED_MACS:-}\' "docker-compose.yml"', validator)
        self.assertIn('need_grep \'POOL_ASIC_MAC_OVERRIDES: \\${POOL_ASIC_MAC_OVERRIDES:-}\' "docker-compose.yml"', validator)
        self.assertIn('need_grep \'BDAG_ASIC_LAN_CIDRS: \\${BDAG_ASIC_LAN_CIDRS:-}\' "docker-compose.yml"', validator)
        self.assertIn('reject_grep \'container_name:\' "docker-compose.yml"', validator)
        self.assertIn('need_file "ops/tests/test_stack_naming_coherence.py"', validator)
        self.assertIn('need_file "ops/systemd/user-bdag-codex-boot-handoff.service"', validator)
        self.assertIn('need_file "ops/systemd/user-bdag-codex-auto-resume.service"', validator)
        self.assertIn("codex-auto-resume.desktop", validator)
        self.assertIn('"--no-deps"', validator)
        self.assertIn("BDAG_CODEX_AUTO_RESUME_CHECK_WAIT_SECONDS", validator)
        self.assertIn("ask-for-approval", validator)
        self.assertIn("danger-full-access", validator)
        self.assertIn("reject_host_reboot_automation", validator)
        self.assertIn("reboot testing must remain operator-controlled", validator)
        self.assertIn('need_file "ops/systemd/bdag-status-sampler.service"', validator)
        self.assertIn('python3 "$root/scripts/validate-stack-defaults.py" "$root"', validator)
        self.assertIn('need_grep \'BDAG_STATUS_SAMPLER_MAX_AGE_SECONDS=\' ".env.example"', validator)
        self.assertIn('need_grep \'BDAG_STATUS_PAYLOAD_STALE_AFTER_SECONDS=\' ".env.example"', validator)
        self.assertIn('need_grep \'POOL_GBT_MIN_INTERVAL_MS=\' ".env.example"', validator)
        self.assertIn('need_grep \'pool_template_rpc_pressure\' "scripts/mining-appliance-preflight.py"', validator)
        self.assertIn('need_grep \'ensure_stack_default_env_value BDAG_POOL_CONTAINER\' "ops/install-dashboard.sh"', validator)


if __name__ == "__main__":
    unittest.main()

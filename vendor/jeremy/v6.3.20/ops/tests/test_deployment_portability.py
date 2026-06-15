#!/usr/bin/env python3

import pathlib
import re
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
ROOT_DIR = OPS_DIR.parent
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


class DeploymentPortabilityTests(unittest.TestCase):
    def test_node_child_detection_accepts_packaged_binary_name(self) -> None:
        top = """UID PID PPID C STIME TTY TIME CMD
root 1 0 0 07:45 ? 00:00:00 runuser -u bdagStack -g bdagStack -- /usr/local/bin/nodeworker
dnsmasq 64 55 0 07:45 ? 00:00:00 /usr/local/bin/blockdag-node --configfile /etc/bdagStack/node.conf
"""

        self.assertTrue(pool_ops.bdag_child_running_from_top(top))

    def test_node_child_detection_keeps_legacy_bdag_binary_name(self) -> None:
        top = """UID PID PPID C STIME TTY TIME CMD
bdag 64 55 0 07:45 ? 00:00:00 /usr/local/bin/bdag --configfile /etc/bdagStack/node.conf
"""

        self.assertTrue(pool_ops.bdag_child_running_from_top(top))

    def test_node_child_detection_does_not_count_wrapper_only(self) -> None:
        top = """UID PID PPID C STIME TTY TIME CMD
root 1 0 0 07:45 ? 00:00:00 runuser -u bdagStack -g bdagStack -- /usr/local/bin/nodeworker
dnsmasq 55 1 0 07:45 ? 00:00:00 /usr/local/bin/nodeworker --node-binary=/usr/local/bin/blockdag-node
"""

        self.assertFalse(pool_ops.bdag_child_running_from_top(top))

    def test_fetch_text_url_uses_python_http_client_not_host_curl(self) -> None:
        captured: dict[str, object] = {}

        class FakeHeaders:
            def get_content_charset(self) -> str:
                return "utf-8"

        class FakeResponse:
            headers = FakeHeaders()

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return b"pool_active_connections 0\n"

        def fake_urlopen(request: object, timeout: float) -> FakeResponse:
            captured["url"] = getattr(request, "full_url", "")
            captured["timeout"] = timeout
            return FakeResponse()

        def forbidden_subprocess_run(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("fetch_text_url must not require host curl")

        old_urlopen = pool_ops.urllib.request.urlopen
        old_run = pool_ops.subprocess.run
        try:
            pool_ops.urllib.request.urlopen = fake_urlopen
            pool_ops.subprocess.run = forbidden_subprocess_run
            text = pool_ops.fetch_text_url("http://127.0.0.1:9090/metrics", {"accept": "text/plain"}, timeout=2.5)
        finally:
            pool_ops.urllib.request.urlopen = old_urlopen
            pool_ops.subprocess.run = old_run

        self.assertEqual(text, "pool_active_connections 0\n")
        self.assertEqual(captured["url"], "http://127.0.0.1:9090/metrics")
        self.assertEqual(captured["timeout"], 2.5)

    def test_compose_dashboard_targets_stack_container_names(self) -> None:
        compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        dashboard_block = compose.split("\n  dashboard:\n", 1)[1].split("\n  # --------------------------------------------------------------------------\n  # Optional CPU miner", 1)[0]

        self.assertIn("BDAG_NODE_SERVICE: node", compose)
        self.assertIn("BDAG_NETWORK: mainnet", compose)
        self.assertIn("BDAG_RAWDATADIR_NETWORK: ${BDAG_RAWDATADIR_NETWORK:-mainnet}", compose)
        self.assertIn("BDAG_STACK_SERVICES: postgres,node,pool", compose)
        self.assertIn("BDAG_POOL_CONTAINER: pool", compose)
        self.assertIn("BDAG_POOL_DB_CONTAINER: postgres", compose)
        self.assertIn("BDAG_NODE_RPC_URL: http://node:38131", compose)
        self.assertIn("BDAG_COLLECTOR_API: ${BDAG_COLLECTOR_API:-http://collector:9280}", dashboard_block)
        self.assertIn("BDAG_DASHBOARD_PORT: 8088", dashboard_block)
        self.assertIn("ADDR: ${DASHBOARD_LISTEN:-0.0.0.0:8088}", dashboard_block)
        self.assertIn('"${DASHBOARD_BIND:-0.0.0.0}:${DASHBOARD_HOST_PORT:-8088}:8088"', dashboard_block)
        self.assertNotIn(":9290", dashboard_block)
        self.assertNotIn("DASHBOARD_EVM_RPC_URL:", compose)
        self.assertNotIn("BDAG_RPC_URL: http://node:38131", compose)
        self.assertNotIn("collector: { condition: service_started }", dashboard_block)
        self.assertNotIn("node: { condition: service_started }", dashboard_block)
        self.assertNotIn("pool: { condition: service_started }", dashboard_block)

    def test_mainnet_is_the_only_deployment_network_name(self) -> None:
        compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("BDAG_NETWORK: mainnet", compose)
        self.assertNotRegex(compose, r"BDAG_NETWORK:\s*\$\{")

        deployment_files = [
            ROOT_DIR / "docker-compose.yml",
            ROOT_DIR / ".env.example",
            ROOT_DIR / "ops" / "portable.env.example",
            ROOT_DIR / "ops" / "maintain-rawdatadir-sidecar.sh",
            ROOT_DIR / "ops" / "install-p2p-services.sh",
            ROOT_DIR / "ops" / "verify-rawdatadir-sidecar.py",
            ROOT_DIR / "ops" / "ipfs_segment_writer.py",
            ROOT_DIR / "ops" / "seal_rawdatadir_sidecar_content.py",
            ROOT_DIR / "ops" / "chain-state-self-heal.sh",
        ]
        alias_re = re.compile(
            r"\bBDAG_(?:RAWDATADIR_|CHAIN_STATE_)?NETWORK\b[^\n]*(?:\bmain\b|\bprod(?:uction)?\b)",
            re.IGNORECASE,
        )
        default_re = re.compile(r"\bBDAG_(?:RAWDATADIR_|CHAIN_STATE_)?NETWORK:-([A-Za-z0-9_-]+)")
        for path in deployment_files:
            text = path.read_text(encoding="utf-8")
            for line_no, line in enumerate(text.splitlines(), start=1):
                self.assertIsNone(alias_re.search(line), f"{path}:{line_no}: {line}")
                for default in default_re.finditer(line):
                    self.assertEqual(default.group(1), "mainnet", f"{path}:{line_no}: {line}")

    def test_release_collector_image_uses_packaged_collector_source(self) -> None:
        compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        dockerfile = (ROOT_DIR / "dockerfile").read_text(encoding="utf-8")
        dockerfile_dev = (ROOT_DIR / "dockerfile-dev").read_text(encoding="utf-8")
        release_dashboard_block = dockerfile.split("FROM ubuntu:24.04 AS dashboard", 1)[1]
        dev_dashboard_block = dockerfile_dev.split("FROM ubuntu:24.04 AS dashboard", 1)[1]

        self.assertIn("dashboard_src: ${DASHBOARD_SRC_CONTEXT:-../dashboard2}", compose)
        self.assertIn("collector_src: ${COLLECTOR_SRC_CONTEXT:-../collector}", compose)
        self.assertIn("COPY --from=dashboard-build /out/dashboard /usr/local/bin/dashboard", dockerfile)
        self.assertIn('ENTRYPOINT ["/usr/local/bin/dashboard"]', dockerfile)
        self.assertIn("COPY --from=collector_src . /opt/collector", dockerfile)
        self.assertIn("COPY --from=dashboard_src . .", dockerfile_dev)
        self.assertIn("go build -trimpath -o /out/dashboard .", dockerfile_dev)
        self.assertIn("COPY --from=dashboard-build /out/dashboard /usr/local/bin/dashboard", dockerfile_dev)
        self.assertNotIn("requirements-dev.txt", release_dashboard_block)
        self.assertNotIn("requirements-dev.txt", dev_dashboard_block)

    def test_dashboard_release_build_has_no_dead_git_ref_arg(self) -> None:
        compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        dockerfile = (ROOT_DIR / "dockerfile").read_text(encoding="utf-8")
        release_validator = (ROOT_DIR / "scripts" / "validate-release-build.sh").read_text(encoding="utf-8")

        self.assertNotIn("DASHBOARD_REPO:", compose)
        self.assertNotIn("DASHBOARD_REF:", compose)
        self.assertNotIn("DASHBOARD_REF:-", compose)
        self.assertNotIn('ref="${DASHBOARD_REF:-develop}"', dockerfile)
        self.assertIn('reject_grep \'DASHBOARD_REF:\' "docker-compose.yml"', release_validator)

    def test_host_dashboard_env_uses_host_reachable_chain_rpc(self) -> None:
        installer = (ROOT_DIR / "ops" / "install-dashboard.sh").read_text(encoding="utf-8")
        portable_env = (ROOT_DIR / "ops" / "portable.env.example").read_text(encoding="utf-8")

        self.assertIn("BDAG_NODE_RPC_URL=http://127.0.0.1:38131", installer)
        self.assertIn("BDAG_GLOBAL_CHAIN_RPC_URLS=node=http://127.0.0.1:38131", installer)
        self.assertIn("BDAG_NODE_RPC_URL=http://127.0.0.1:38131", portable_env)
        self.assertIn("BDAG_GLOBAL_CHAIN_RPC_URLS=node=http://127.0.0.1:38131", portable_env)
        self.assertNotIn("NODE_RPC_URL" "S=", portable_env)

    def test_compose_protects_temp_paths_from_overlay_io(self) -> None:
        compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertGreaterEqual(compose.count("/var/tmp:size=${BDAG_CONTAINER_TMPFS_SIZE:-128m},mode=1777"), 4)
        self.assertIn("cpu_shares: 4096", compose)
        self.assertIn("cpu_shares: 3072", compose)
        self.assertIn("cpu_shares: 256", compose)
        self.assertGreaterEqual(compose.count("TMPDIR: /tmp"), 5)
        self.assertGreaterEqual(compose.count("TMP: /tmp"), 5)
        self.assertGreaterEqual(compose.count("TEMP: /tmp"), 5)

    def test_compose_mounts_configured_persistent_data_paths(self) -> None:
        compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("postgres-data:/var/lib/postgresql/data", compose)
        self.assertIn("${NODE_DATA_DIR:-node-data}:/var/lib/bdagStack/node", compose)
        self.assertIn("nodeworker-data:/var/lib/bdagStack/nodeworker", compose)
        self.assertIn("  postgres-data:", compose)
        self.assertIn("  node-data:", compose)
        self.assertIn("  nodeworker-data:", compose)

    def test_pool_node_health_defaults_live_in_stack_defaults(self) -> None:
        stack_defaults = (ROOT_DIR / "ops" / "config" / "stack-defaults.env").read_text(encoding="utf-8")

        self.assertEqual(1, stack_defaults.count("POOL_RPC_ROUTER_NODE_HEALTH_ENABLED=true"))

    def test_p2p_installer_env_value_prints_resolved_value(self) -> None:
        installer = (ROOT_DIR / "ops" / "install-p2p-services.sh").read_text(encoding="utf-8")

        self.assertIn('value="$(strip_env_quotes "$value")"', installer)
        self.assertIn('printf \'%s\\n\' "$value"', installer)
        self.assertNotIn("strip_env_quotes \"$value\"\n  printf '\\n'", installer)

    def test_pool_node_health_gate_is_enabled_by_default(self) -> None:
        stack_defaults = (ROOT_DIR / "ops" / "config" / "stack-defaults.env").read_text(encoding="utf-8")
        env_example = (ROOT_DIR / ".env.example").read_text(encoding="utf-8")
        stack_defaults = (ROOT_DIR / "ops" / "config" / "stack-defaults.env").read_text(encoding="utf-8")
        validator = (ROOT_DIR / "scripts" / "validate-pi5-restart-hardening.sh").read_text(encoding="utf-8")

        self.assertIn("POOL_RPC_ROUTER_NODE_HEALTH_ENABLED=true", stack_defaults)
        self.assertIn("POOL_RPC_ROUTER_NODE_HEALTH_ENABLED=true", env_example)
        self.assertIn("POOL_RPC_ROUTER_NODE_HEALTH_ENABLED=true", stack_defaults)
        self.assertIn("POOL_RPC_ROUTER_NODE_HEALTH_ENABLED=", validator)

    def test_live_deploy_copy_contract_covers_live_validator_files(self) -> None:
        deploy = (ROOT_DIR / "ops" / "deploy-live-runtime-update.sh").read_text(encoding="utf-8")
        validator = (ROOT_DIR / "scripts" / "validate-pi5-restart-hardening.sh").read_text(encoding="utf-8")
        files_match = re.search(r"FILES=\((.*?)\n\)", deploy, re.DOTALL)
        self.assertIsNotNone(files_match)
        deploy_files = set(re.findall(r'"([^"]+)"', files_match.group(1)))
        ignored = {
            ".env.cpu.example",
            ".github/workflows/build-cpu.yml",
            ".github/workflows/build.yml",
            ".github/workflows/rc-hardening.yml",
            "docker-compose.yml",
            "scripts/check-doc-consistency.py",
            "scripts/release/installers/install-unix-common.sh",
            "scripts/release/installers/install-windows.ps1",
        }
        required = {
            rel
            for rel in re.findall(r'need_file "([^"]+)"', validator)
            if rel not in ignored and not rel.startswith(".github/")
        }

        self.assertEqual([], sorted(required - deploy_files))

    def test_live_runtime_validator_requires_current_runtime_surfaces(self) -> None:
        validator = (ROOT_DIR / "scripts" / "validate-pi5-restart-hardening.sh").read_text(encoding="utf-8")

        self.assertIn('if [[ "$mode" == "source" && -e "$root/ops/observability" ]]; then', validator)
        self.assertIn('need_grep \'NODE_RPC_URL: http://node:38131\' "docker-compose.yml"', validator)
        self.assertIn("reject_grep '(^|[^A-Z0-9_])NODE_RPC_URLS([^A-Z0-9_]|$)' \"docker-compose.yml\"", validator)
        self.assertIn('need_grep \'BDAG_STACK_SERVICES=postgres,node,pool\' ".env.example"', validator)
        self.assertIn('reject_grep \'container_name:\' "docker-compose.yml"', validator)

    def test_live_runtime_validator_keeps_release_packaging_source_only(self) -> None:
        validator = (ROOT_DIR / "scripts" / "validate-pi5-restart-hardening.sh").read_text(encoding="utf-8")

        self.assertIn('need_grep \'check-release-archive.py\' ".github/workflows/build.yml"', validator)
        self.assertIn('need_grep \'check-release-archive.py\' ".github/workflows/build-cpu.yml"', validator)
        self.assertIn('reject_grep \'BDAG_P2P_LAN_PEERS=\' ".env.cpu.example"', validator)

    def test_live_deploy_rollback_validates_manifest_not_new_rc_contract(self) -> None:
        deploy = (ROOT_DIR / "ops" / "deploy-live-runtime-update.sh").read_text(encoding="utf-8")
        rollback_body = deploy.split("rollback_from_backup()", 1)[1].split("if [[ -n \"$ROLLBACK_DIR\" ]]", 1)[0]

        self.assertIn("validate_rollback_restored", deploy)
        self.assertIn("validate_rollback_restored || die", rollback_body)
        self.assertNotIn("run_target_validation", rollback_body)

    def test_release_installer_defaults_to_zero_miner_sources(self) -> None:
        installer = (ROOT_DIR / "ops" / "release-install.sh").read_text(encoding="utf-8")

        self.assertIn('configure discovered miner sources now?" "n"', installer)

    def test_linux_installers_start_sync_services_before_pool(self) -> None:
        local_installer = (ROOT_DIR / "ops" / "release-install.sh").read_text(encoding="utf-8")
        payload_installer = (
            ROOT_DIR / "scripts" / "release" / "installers" / "install-unix-common.sh"
        ).read_text(encoding="utf-8")

        self.assertIn("automation_control.py ensure-normal", local_installer)
        self.assertIn("compose_cmd up -d --no-build --pull never --no-deps postgres node dashboard", local_installer)
        self.assertNotIn("compose_cmd up -d --no-build --pull never\n", local_installer)
        self.assertIn("automation_control.py ensure-normal", payload_installer)
        self.assertIn("docker_cli compose up -d --no-build --pull never --no-deps postgres node dashboard", payload_installer)
        self.assertNotIn("docker compose up -d --no-build --pull never\n", payload_installer)

    def test_release_installer_extracts_preserved_chain_peer_evidence(self) -> None:
        installer = (ROOT_DIR / "ops" / "release-install.sh").read_text(encoding="utf-8")

        self.assertIn("discover_preserved_chain_peers", installer)
        self.assertIn('python3 ops/update-local-peers.py --env-file "$ROOT/.env" --force-apply', installer)
        self.assertIn("peer-discovery-current.json", installer)

    def test_installers_pin_pool_host_and_asic_lan_scope(self) -> None:
        env_example = (ROOT_DIR / ".env.example").read_text(encoding="utf-8")
        compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        local_installer = (ROOT_DIR / "ops" / "release-install.sh").read_text(encoding="utf-8")
        entrypoint = (ROOT_DIR / "docker" / "entrypoint-nodeworker.sh").read_text(encoding="utf-8")
        payload_installer = (
            ROOT_DIR / "scripts" / "release" / "installers" / "install-unix-common.sh"
        ).read_text(encoding="utf-8")
        windows_installer = (
            ROOT_DIR / "scripts" / "release" / "installers" / "install-windows.ps1"
        ).read_text(encoding="utf-8")
        validator = (ROOT_DIR / "scripts" / "validate-pi5-restart-hardening.sh").read_text(encoding="utf-8")

        self.assertIn("BDAG_DOCKER_BRIDGE_CIDRS=172.16.0.0/12", env_example)
        self.assertIn("BDAG_ALLOW_DOCKER_BRIDGE_ASIC_IPS=0", env_example)
        self.assertIn("BDAG_ASIC_LAN_CIDRS: ${BDAG_ASIC_LAN_CIDRS:-}", compose)
        self.assertIn("tr ',' ' '", entrypoint)
        self.assertIn('append_node_arg_prefix_once "--modules=${word}"', entrypoint)
        self.assertIn('set_env_value .env BDAG_ASIC_LAN_CIDRS "$scan_target"', local_installer)
        self.assertIn("validate_pool_lan_config", local_installer)
        self.assertIn('set_env_value .env BDAG_ASIC_LAN_CIDRS "$MINER_SCAN_TARGET"', payload_installer)
        self.assertIn("validate_pool_lan_config", payload_installer)
        self.assertIn("refusing Docker bridge pool endpoint", payload_installer)
        self.assertIn("Set-EnvValue .env BDAG_ASIC_LAN_CIDRS $minerScanTarget", windows_installer)
        self.assertIn("Assert-PoolLanConfig", windows_installer)
        self.assertIn("Refusing Docker bridge pool endpoint", windows_installer)
        self.assertIn("BDAG_DOCKER_BRIDGE_CIDRS=172.16.0.0/12", validator)
        self.assertIn("BDAG_ALLOW_DOCKER_BRIDGE_ASIC_IPS=0", validator)

    def test_release_docs_keep_zero_miner_default_invariant(self) -> None:
        agents = (ROOT_DIR / "AGENTS.md").read_text(encoding="utf-8")
        readme = (ROOT_DIR / "README.md").read_text(encoding="utf-8")

        self.assertIn("Fresh installs assume zero miner sources", agents)
        self.assertIn("Fresh installs assume zero miner sources", readme)
        self.assertIn("0..N ASIC or Stratum miners", agents)

    def test_p2p_firewall_uses_single_compose_port(self) -> None:
        env_example = (ROOT_DIR / ".env.example").read_text(encoding="utf-8")
        firewall = (ROOT_DIR / "ops" / "allow-p2p-iptables.sh").read_text(encoding="utf-8")
        installer = (ROOT_DIR / "ops" / "install-p2p-services.sh").read_text(encoding="utf-8")
        unit = (ROOT_DIR / "ops" / "systemd" / "bdag-p2p-firewall.service").read_text(encoding="utf-8")

        combined = "\n".join([env_example, firewall, installer, unit])
        self.assertIn("P2P_PORT=8150", env_example)
        self.assertIn('PORT="${P2P_PORT:-8150}"', firewall)
        self.assertIn("Environment=P2P_PORT=8150", unit)
        self.assertNotIn("BDAG_P2P_PORTS", combined)
        self.assertNotIn("--dports", firewall)

    def test_p2p_installer_enables_ipfs_sidecars_from_stack_defaults(self) -> None:
        installer = (ROOT_DIR / "ops" / "install-p2p-services.sh").read_text(encoding="utf-8")
        env_example = (ROOT_DIR / ".env.example").read_text(encoding="utf-8")
        defaults = (ROOT_DIR / "ops" / "config" / "stack-defaults.env").read_text(encoding="utf-8")
        raw_timer = (ROOT_DIR / "ops" / "systemd" / "user-bdag-rawdatadir-sidecar.timer").read_text(encoding="utf-8")
        content_timer = (ROOT_DIR / "ops" / "systemd" / "user-bdag-ipfs-content-sidecar.timer").read_text(encoding="utf-8")
        segment_timer = (ROOT_DIR / "ops" / "systemd" / "user-bdag-ipfs-segment-writer.timer").read_text(encoding="utf-8")

        self.assertIn("BDAG_STACK_DEFAULTS_FILE=", installer)
        self.assertIn("stack-defaults.env", installer)
        self.assertIn("env_value()", installer)
        self.assertIn(
            "BDAG_RAWDATADIR_SIDECAR_RSYNC_BWLIMIT=$(env_value BDAG_RAWDATADIR_SIDECAR_RSYNC_BWLIMIT 4096)",
            installer,
        )
        self.assertIn(
            "BDAG_RAWDATADIR_SIDECAR_CATCHUP_RSYNC_BWLIMIT=$(env_value BDAG_RAWDATADIR_SIDECAR_CATCHUP_RSYNC_BWLIMIT 1024)",
            installer,
        )
        for config in (env_example, defaults):
            self.assertIn("BDAG_BTRFS_CHECKPOINT_VOLUME_MODE=auto", config)
            self.assertIn("BDAG_BTRFS_CHECKPOINT_VOLUME_SIZE_GIB=128", config)
            self.assertIn("BDAG_BTRFS_CHECKPOINT_VOLUME_MOUNT=./data-restore/btrfs-checkpoints", config)
            self.assertIn("BDAG_RAWDATADIR_ARTIFACT_BASE=./data-restore/btrfs-checkpoints/rawdatadir-artifacts", config)
            self.assertIn("BDAG_RAWDATADIR_SIDECAR_DIR=./data-restore/btrfs-checkpoints/rawdatadir-sidecar/mainnet", config)
            self.assertIn("BDAG_RAWDATADIR_SIDECAR_CONTENT_BASE=./data-restore/btrfs-checkpoints/rawdatadir-sidecar-content", config)
            self.assertIn(
                "BDAG_BACKGROUND_MAINTENANCE_LAZY_TASKS=dashboard_global_sampler,global_blockchain_scan,global_scan,"
                "rawdatadir_sidecar,rawdatadir_content_seal,ipfs_content_sidecar,ipfs_segment_writer,"
                "history_compaction",
                config,
            )
            self.assertIn("BDAG_BACKGROUND_MAINTENANCE_SYNC_PRIORITY_EXEMPT_TASKS=ipfs_segment_writer", config)
            self.assertIn("BDAG_BACKGROUND_MAINTENANCE_IO_PRESSURE_EXEMPT_TASKS=", config)
            self.assertIn(
                "BDAG_BACKGROUND_MAINTENANCE_POOL_READY_TASKS=rawdatadir_content_seal,ipfs_content_sidecar",
                config,
            )
            self.assertIn("BDAG_RAWDATADIR_SIDECAR_CATCHUP_RSYNC_BWLIMIT=1024", config)
            self.assertIn("BDAG_IPFS_SEGMENT_STALE_HEAD_RESET_ENABLED=1", config)
            self.assertIn("BDAG_IPFS_SEGMENT_STALE_HEAD_MAX_LAG_ORDERS=3600", config)
            self.assertIn("BDAG_IPFS_SEGMENT_WRITER_ELECTION_RULE=rendezvous_sha256_v1", config)
            self.assertIn("BDAG_IPFS_SEGMENT_BOOTSTRAP_LOCAL_PUBLISH=0", config)
            self.assertIn("BDAG_IPFS_SEGMENT_BOOTSTRAP_UNTRUSTED_PUBLISH=0", config)
            self.assertIn("BDAG_IPFS_SEGMENT_SIGNING_KEY_FILE=./ops/runtime/ipfs-content/segment-writer.key", config)
            self.assertIn("BDAG_RAWDATADIR_SIGNING_KEY_FILE=./ops/runtime/ipfs-content/segment-writer.key", config)
            self.assertIn("BDAG_IPFS_RAWDATADIR_CONTENT_INDEX_PATH=./ops/runtime/ipfs-content/rawdatadir-content-index.json", config)
            self.assertIn("BDAG_IPFS_RAWDATADIR_CONTENT_DEFAULT_INDEX_CID=", config)
            self.assertIn("BDAG_IPFS_RAWDATADIR_CONTENT_PUBLISH_IPNS=auto", config)
            self.assertIn("BDAG_IPFS_STATE_CHECKPOINT_REQUIRED=1", config)
            self.assertIn("BDAG_RESTORE_POINT_MAX_AGE_SECONDS=600", config)
            self.assertIn("BDAG_RESTORE_GUARD_IPFS_TIMERS=bdag-rawdatadir-sidecar.timer,bdag-rawdatadir-sidecar-verify.timer,bdag-ipfs-content-sidecar.timer", config)
            self.assertIn("BDAG_IPFS_PEER_ROSTER_ENABLED=1", config)
            self.assertIn("BDAG_IPFS_PEER_ROSTER_INDEX_PATH=./ops/runtime/ipfs-content/peer-roster.json", config)
            self.assertIn("BDAG_IPFS_PEER_ROSTER_STATUS_FILE=./ops/runtime/ipfs-content/peer-roster-status.json", config)
            self.assertIn("BDAG_IPFS_PEER_ROSTER_PUBLISH_IPFS=1", config)
            self.assertIn("BDAG_IPFS_PEER_ROSTER_MAX_PEERS=64", config)
            self.assertIn("BDAG_IPFS_PEER_ROSTER_REQUIRE_SIGNATURES=1", config)
            self.assertIn("BDAG_RAWDATADIR_REQUIRE_TRUSTED_SIGNER=1", config)
            self.assertIn("BDAG_RAWDATADIR_REQUIRE_CHAIN_ANCHOR=1", config)
            self.assertIn("BDAG_RAWDATADIR_CHAIN_ANCHOR_REFERENCE_EVM_URL=https://rpc.blockdag.engineering", config)
            self.assertIn("BDAG_IPFS_SEGMENT_REQUIRE_SIGNATURES=1", config)
            self.assertIn("BDAG_IPFS_RESTORE_REQUIRE_SIGNATURES=1", config)
            self.assertIn("BDAG_IPFS_RESTORE_VERIFY_INDEX_LINEAGE=1", config)
            self.assertIn("BDAG_IPFS_RESTORE_ACCEPTED_HEAD_ENABLED=1", config)
            self.assertIn(
                "BDAG_IPFS_RESTORE_ACCEPTED_HEAD_STATE_FILE=./ops/runtime/ipfs-content/restore-accepted-head.json",
                config,
            )
            self.assertIn("BDAG_IPFS_RESTORE_CHAIN_ANCHOR_ENABLED=1", config)
            self.assertIn("BDAG_IPFS_RESTORE_REQUIRE_CHAIN_ANCHOR=0", config)
            self.assertIn("BDAG_IPFS_RESTORE_CHAIN_ANCHOR_FULL_SPAN_MAX_ORDERS=300", config)
            self.assertIn("BDAG_IPFS_RESTORE_CHAIN_ANCHOR_SKIP_ENVIRONMENT_GATES=1", config)
            self.assertIn("BDAG_IPFS_RAWDATADIR_RESTORE_PRESTART=1", config)
            self.assertIn("BDAG_IPFS_RAWDATADIR_RESTORE_DISCOVERY_FILE=./ops/ipfs-content-discovery.json", config)
            self.assertIn("BDAG_IPFS_RAWDATADIR_RESTORE_STATUS_FILE=./ops/runtime/ipfs-content/rawdatadir-restore-status.json", config)
            self.assertIn("BDAG_IPFS_BACKFILL_INDEX_PATH=./ops/runtime/ipfs-content/backfill-genesis-index.json", config)
            self.assertIn("BDAG_IPFS_BACKFILL_MAX_SEGMENTS_PER_RUN=1", config)
            self.assertIn("BDAG_IPFS_SEGMENT_MAX_SEGMENTS_PER_RUN=1", config)
            self.assertIn("BDAG_IPFS_SEGMENT_PUBLISH_IPNS=auto", config)
        self.assertIn("install_mining_host_tuning\ninstall_rawdatadir_sidecar_timers", installer)
        self.assertIn("install_rawdatadir_sidecar_timers\ninstall_ipfs_content_sidecar_timer", installer)
        self.assertIn("install_ipfs_content_sidecar_timer\ninstall_native_reference_rpc", installer)
        self.assertIn("install_native_reference_rpc\ninstall_ipfs_segment_writer_timer", installer)
        self.assertLess(
            installer.index("install_rawdatadir_sidecar_timers()"),
            installer.index("install_ipfs_segment_writer_timer()"),
        )
        self.assertLess(
            installer.index("ensure_ipfs_segment_identity || return 1"),
            installer.index('cat > "$user_config_dir/bdag-rawdatadir-sidecar.env"'),
        )
        self.assertIn("BDAG_IPFS_SEGMENT_WRITER_ROSTER=$(env_value BDAG_IPFS_SEGMENT_WRITER_ROSTER \"\")", installer)
        self.assertIn(
            "BDAG_IPFS_SEGMENT_WRITER_ELECTION_RULE=$(env_value BDAG_IPFS_SEGMENT_WRITER_ELECTION_RULE rendezvous_sha256_v1)",
            installer,
        )
        self.assertIn("ensure_ipfs_segment_identity || return 1", installer)
        self.assertIn("BDAG_IPFS_SEGMENT_BOOTSTRAP_LOCAL_PUBLISH=$(env_value BDAG_IPFS_SEGMENT_BOOTSTRAP_LOCAL_PUBLISH 0)", installer)
        self.assertIn("BDAG_IPFS_SEGMENT_BOOTSTRAP_UNTRUSTED_PUBLISH=$(env_value BDAG_IPFS_SEGMENT_BOOTSTRAP_UNTRUSTED_PUBLISH 0)", installer)
        self.assertIn("BDAG_IPFS_STATE_CHECKPOINT_REQUIRED=$(env_value BDAG_IPFS_STATE_CHECKPOINT_REQUIRED 1)", installer)
        self.assertIn("BDAG_RESTORE_POINT_MAX_AGE_SECONDS=$(env_value BDAG_RESTORE_POINT_MAX_AGE_SECONDS 600)", installer)
        self.assertIn(
            'BDAG_RESTORE_GUARD_IPFS_TIMERS=$(env_value BDAG_RESTORE_GUARD_IPFS_TIMERS "bdag-rawdatadir-sidecar.timer,bdag-rawdatadir-sidecar-verify.timer,bdag-ipfs-content-sidecar.timer")',
            installer,
        )
        self.assertIn("BDAG_IPFS_PEER_ROSTER_ENABLED=1", defaults)
        self.assertIn("BDAG_IPFS_PEER_ROSTER_REQUIRE_SIGNATURES=1", defaults)
        self.assertIn(
            'BDAG_IPFS_SEGMENT_SIGNING_KEY_FILE=$(env_value BDAG_IPFS_SEGMENT_SIGNING_KEY_FILE "$ROOT/ops/runtime/ipfs-content/segment-writer.key")',
            installer,
        )
        self.assertIn(
            'BDAG_RAWDATADIR_SIGNING_KEY_FILE=$(env_value BDAG_RAWDATADIR_SIGNING_KEY_FILE "$ROOT/ops/runtime/ipfs-content/segment-writer.key")',
            installer,
        )
        self.assertIn(
            'BDAG_IPFS_SEGMENT_SIGNING_KEY_FILE=$(env_value BDAG_IPFS_SEGMENT_SIGNING_KEY_FILE "$ROOT/ops/runtime/ipfs-content/segment-writer.key")',
            installer,
        )
        self.assertIn("BDAG_IPFS_RAWDATADIR_CONTENT_INDEX_PATH=$(env_value BDAG_IPFS_RAWDATADIR_CONTENT_INDEX_PATH", installer)
        self.assertIn("BDAG_IPFS_RAWDATADIR_CONTENT_PUBLISH_IPNS=$(env_value BDAG_IPFS_RAWDATADIR_CONTENT_PUBLISH_IPNS auto)", installer)
        self.assertIn("BDAG_RAWDATADIR_REQUIRE_TRUSTED_SIGNER=$(env_value BDAG_RAWDATADIR_REQUIRE_TRUSTED_SIGNER 1)", installer)
        self.assertIn("BDAG_RAWDATADIR_REQUIRE_CHAIN_ANCHOR=$(env_value BDAG_RAWDATADIR_REQUIRE_CHAIN_ANCHOR 1)", installer)
        self.assertIn(
            "BDAG_RAWDATADIR_CHAIN_ANCHOR_REFERENCE_EVM_URL=$(env_value BDAG_RAWDATADIR_CHAIN_ANCHOR_REFERENCE_EVM_URL https://rpc.blockdag.engineering)",
            installer,
        )
        self.assertIn("BDAG_IPFS_SEGMENT_REQUIRE_SIGNATURES=$(env_value BDAG_IPFS_SEGMENT_REQUIRE_SIGNATURES 1)", installer)
        self.assertIn("BDAG_IPFS_SEGMENT_UPDATE_DISCOVERY_FOR_CUSTOM_INDEX", (ROOT_DIR / "ops" / "ipfs_segment_writer.py").read_text(encoding="utf-8"))
        self.assertIn(
            'BDAG_CHAIN_INTEGRITY_MAX_SEGMENT_ORDERS=$(env_value BDAG_CHAIN_INTEGRITY_MAX_SEGMENT_ORDERS "$(env_value BDAG_IPFS_SEGMENT_ORDERS_PER_SEGMENT 300)")',
            installer,
        )
        self.assertIn("BDAG_IPFS_SEGMENT_MAX_SEGMENTS_PER_RUN=$(env_value BDAG_IPFS_SEGMENT_MAX_SEGMENTS_PER_RUN 1)", installer)
        self.assertIn(
            'BDAG_IPFS_SEGMENT_RESTORE_DIR=$(env_value BDAG_IPFS_SEGMENT_RESTORE_DIR "$ROOT/ops/runtime/ipfs-segment-restore-drills")',
            installer,
        )
        self.assertIn(
            "BDAG_IPFS_RESTORE_ACCEPTED_HEAD_ENABLED=$(env_value BDAG_IPFS_RESTORE_ACCEPTED_HEAD_ENABLED 1)",
            installer,
        )
        self.assertIn(
            "BDAG_IPFS_RESTORE_CHAIN_ANCHOR_ENABLED=$(env_value BDAG_IPFS_RESTORE_CHAIN_ANCHOR_ENABLED 1)",
            installer,
        )
        self.assertIn(
            "BDAG_IPFS_RESTORE_CHAIN_ANCHOR_FULL_SPAN_MAX_ORDERS=$(env_value BDAG_IPFS_RESTORE_CHAIN_ANCHOR_FULL_SPAN_MAX_ORDERS 300)",
            installer,
        )
        self.assertIn("BDAG_IPFS_SEGMENT_PUBLISH_IPNS=$(env_value BDAG_IPFS_SEGMENT_PUBLISH_IPNS auto)", installer)
        self.assertIn("configure_btrfs_checkpoint_volume", (ROOT_DIR / "ops" / "release-install.sh").read_text(encoding="utf-8"))
        self.assertIn("--warn-only --enforce-blockers", (ROOT_DIR / "ops" / "release-install.sh").read_text(encoding="utf-8"))
        for timer in (raw_timer, content_timer, segment_timer):
            self.assertIn("OnActiveSec=5m", timer)
            self.assertIn("OnUnitActiveSec=5m", timer)
            self.assertIn("RandomizedDelaySec=2m", timer)

    def test_p2p_installer_removes_retired_rawdatadir_source_unit(self) -> None:
        installer = (ROOT_DIR / "ops" / "install-p2p-services.sh").read_text(encoding="utf-8")

        self.assertIn("retire_legacy_rawdatadir_source_timer()", installer)
        self.assertIn("bdag-rawdatadir-source.service", installer)
        self.assertIn("bdag-rawdatadir-source.timer", installer)
        self.assertIn("disable --now bdag-rawdatadir-source.timer bdag-rawdatadir-source.service", installer)
        self.assertIn("bdag-ipfs-content-sidecar", installer)
        self.assertNotIn("publish-" "rawdatadir-artifact.sh", installer)

    def test_btrfs_checkpoint_volume_owns_content_chunk_paths(self) -> None:
        installer = (ROOT_DIR / "ops" / "release-install.sh").read_text(encoding="utf-8")

        self.assertIn("ensure_btrfs_checkpoint_owned_dir", installer)
        self.assertIn("$mountpoint/rawdatadir-sidecar-content/artifacts", installer)
        self.assertIn("$mountpoint/rawdatadir-sidecar-content/chunk-store", installer)
        self.assertIn("$mountpoint/rawdatadir-sidecar-content/chunk-store/sha256", installer)
        self.assertNotIn("chown -R", installer)

    def test_live_deploy_enters_transition_hold_before_target_mutations(self) -> None:
        deploy = (ROOT_DIR / "ops" / "deploy-live-runtime-update.sh").read_text(encoding="utf-8")
        main = deploy.split('if [[ -n "$ROLLBACK_DIR" ]]; then', 1)[1]

        self.assertLess(main.index("begin_deploy_transition_hold"), main.index("runtime_compose_guard"))
        self.assertLess(main.index("begin_deploy_transition_hold"), main.index("migrate_runtime_compose"))
        self.assertIn('--allowed-mutation "deploy-live-runtime-update:systemd_restart:*"', deploy)

    def test_host_node_child_guard_delegates_to_shared_guard(self) -> None:
        host_guard = (ROOT_DIR / "host" / "mining-appliance" / "bdag-node-child-guard").read_text(encoding="utf-8")

        self.assertIn('script = project_root / "ops" / "node_child_guard.py"', host_guard)
        self.assertIn("os.execv", host_guard)
        self.assertNotIn("def repair_node", host_guard)

    def test_release_installer_has_prestart_ipfs_rawdatadir_restore_gate(self) -> None:
        installer = (ROOT_DIR / "ops" / "release-install.sh").read_text(encoding="utf-8")

        self.assertIn("run_prestart_ipfs_rawdatadir_restore()", installer)
        self.assertIn("set_existing_or_stack_default_env_value()", installer)
        self.assertIn('if grep -q "^${key}=" "$file"; then', installer)
        self.assertIn("BDAG_IPFS_RAWDATADIR_RESTORE_ARTIFACT_CID", installer)
        self.assertIn("BDAG_IPFS_RAWDATADIR_RESTORE_INDEX_CID", installer)
        self.assertIn("BDAG_IPFS_RAWDATADIR_RESTORE_DISCOVERY_FILE", installer)
        self.assertIn("set_existing_or_stack_default_env_value .env BDAG_IPFS_SEGMENT_TRUSTED_SIGNERS", installer)
        self.assertIn("set_existing_or_stack_default_env_value .env BDAG_RAWDATADIR_TRUSTED_SIGNERS", installer)
        self.assertIn("ops/restore-rawdatadir-segment-artifact.py", installer)
        self.assertIn("seed_chain_data\n  run_prestart_ipfs_rawdatadir_restore\n  run_prestart_ipfs_restore_drill", installer)


if __name__ == "__main__":
    unittest.main()

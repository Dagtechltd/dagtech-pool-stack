import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ChainStateSelfHealMainnetOnlyTest(unittest.TestCase):
    def test_self_heal_refuses_non_mainnet_network_and_pins_restore_path(self) -> None:
        script = (ROOT / "ops" / "chain-state-self-heal.sh").read_text(encoding="utf-8")

        self.assertIn("chain-state self-heal refuses non-mainnet network", script)
        self.assertIn('NETWORK="mainnet"', script)
        self.assertIn('NODE_NETWORK_DIR="$NODE_DATA_DIR/$NETWORK"', script)
        self.assertNotIn('${NETWORK:-mainnet}', script)

    def test_destructive_self_heal_defaults_to_fail_closed(self) -> None:
        script = (ROOT / "ops" / "chain-state-self-heal.sh").read_text(encoding="utf-8")
        stack_defaults = (ROOT / "ops" / "config" / "stack-defaults.env").read_text(encoding="utf-8")
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        portable_env = (ROOT / "ops" / "portable.env.example").read_text(encoding="utf-8")

        self.assertIn('enabled="${BDAG_CHAIN_STATE_SELF_HEAL_ENABLED:-0}"', script)
        self.assertIn("BDAG_CHAIN_STATE_SELF_HEAL_ENABLED=0", stack_defaults)
        self.assertIn("BDAG_CHAIN_STATE_SELF_HEAL_ENABLED=0", env_example)
        self.assertIn("BDAG_CHAIN_STATE_SELF_HEAL_ENABLED=0", portable_env)
        self.assertNotIn("BDAG_CHAIN_STATE_SELF_HEAL_ALLOW_LOCAL_CANDIDATES", stack_defaults)

    def test_self_heal_rejects_sealed_sidecar_artifacts_as_raw_datadirs(self) -> None:
        script = (ROOT / "ops" / "chain-state-self-heal.sh").read_text(encoding="utf-8")

        self.assertNotIn("data-restore/rawdatadir-sidecar-content/current", script)
        self.assertIn("reject_sealed_artifact_source", script)
        self.assertIn("rawdatadir-sidecar-content", script)
        self.assertIn("DO_NOT_PUBLISH.txt", script)
        self.assertIn('"raw_datadir_checkpoint"', script)
        self.assertIn('[[ -d "$source/chunks" && -f "$source/manifest.json" ]]', script)

        pre_restore_start = script.split('json_state "started" "chain-state restore started"', 1)[0]
        self.assertNotIn('stop_service_best_effort "$POOL_SERVICE"', pre_restore_start)

    def test_self_heal_checks_automation_control_before_stopping_services(self) -> None:
        script = (ROOT / "ops" / "chain-state-self-heal.sh").read_text(encoding="utf-8")

        self.assertIn("automation_allows_self_heal", script)
        self.assertIn("automation_control.ACTION_STACK_CLEAN_RESTORE", script)
        self.assertIn('json_state "blocked" "automation control blocked chain-state self-heal', script)
        pre_restore_start = script.split('json_state "started" "chain-state restore started"', 1)[0]
        self.assertIn("automation_allows_self_heal", pre_restore_start)
        self.assertIn("automation control blocked chain-state self-heal", pre_restore_start)

    def test_self_heal_rejects_live_hot_rsync_mirror_of_same_node(self) -> None:
        script = (ROOT / "ops" / "chain-state-self-heal.sh").read_text(encoding="utf-8")

        self.assertIn("reject_live_hot_rsync_source", script)
        self.assertIn('"live_hot_rsync"', script)
        self.assertIn('"$NODE_NETWORK_DIR"', script)
        self.assertIn("is a live_hot_rsync mirror of this node data", script)

    def test_self_heal_quarantine_stays_outside_node_data_dir(self) -> None:
        script = (ROOT / "ops" / "chain-state-self-heal.sh").read_text(encoding="utf-8")

        self.assertIn('DEFAULT_QUARANTINE_ROOT="$CHAIN_DATA_DIR/chain-quarantine"', script)
        self.assertIn('DEFAULT_QUARANTINE_ROOT="$(dirname "$NODE_DATA_DIR")/chain-quarantine"', script)
        self.assertIn('chain-state quarantine dir must not be inside node data dir', script)
        self.assertIn("mv_path", script)
        self.assertIn("rsync_path", script)
        self.assertIn('BDAG_CHAIN_STATE_RESTORE_CHOWN:-999:999', script)

    def test_self_heal_dashboard_restart_is_not_restore_fatal(self) -> None:
        script = (ROOT / "ops" / "chain-state-self-heal.sh").read_text(encoding="utf-8")

        self.assertIn('compose up -d --no-build --pull never "$NODE_SERVICE"', script)
        self.assertIn("dashboard restart failed after chain-state restore; continuing", script)
        self.assertIn('json_state "failed" "node restart failed after restore"', script)
        self.assertNotIn('json_state "failed" "node/dashboard restart failed after restore"', script)


if __name__ == "__main__":
    unittest.main()

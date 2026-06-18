from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class RawdatadirSidecarFinalizationPipelineTest(unittest.TestCase):
    def test_final_stopped_sync_marks_content_finalized_after_stop_gate(self) -> None:
        script = (ROOT / "ops" / "publish-rawdatadir-artifact.sh").read_text(encoding="utf-8")

        stop_call = "\nstop_active_node_for_final_sync\ntrap start_active_node_after_final_sync EXIT INT TERM\n"
        finalized_env = "BDAG_RAWDATADIR_SIDECAR_CONTENT_FINALIZED=1 \\"

        self.assertIn('collect_finalization_anchor_env >"$FINALIZATION_ANCHOR_FILE" 2>>"$LOG_FILE"', script)
        self.assertLess(script.index(stop_call), script.index(finalized_env))
        self.assertIn("BDAG_RAWDATADIR_SIDECAR_FINAL_STOPPED_SYNC=1 \\", script)
        self.assertIn("BDAG_RAWDATADIR_REQUIRE_EVM_REFERENCE_FRESH=0 \\", script)
        self.assertIn(finalized_env, script)

    def test_mutable_sidecar_preserves_open_restore_point_before_refresh(self) -> None:
        script = (ROOT / "ops" / "maintain-rawdatadir-sidecar.sh").read_text(encoding="utf-8")

        self.assertIn("OPEN_RESTORE_ENABLED=", script)
        self.assertIn("create_open_restore_point", script)
        self.assertLess(script.index("create_open_restore_point"), script.index('run_low_priority "${rsync_command[@]}"'))
        self.assertIn("cp -al", script)
        self.assertIn("bdag_open_sidecar_restore_point_v1", script)

    def test_local_sidecar_copy_ignores_only_source_mode_disabled(self) -> None:
        script = (ROOT / "ops" / "maintain-rawdatadir-sidecar.sh").read_text(encoding="utf-8")

        self.assertIn("LOCAL_SIDECAR_COPY=", script)
        self.assertIn("local_sidecar_copy_can_ignore_reasons", script)
        self.assertIn("source_mode_disabled", script)
        self.assertIn("raw datadir sidecar local copy continuing", script)

    def test_content_seal_rechecks_background_pressure_after_sync(self) -> None:
        script = (ROOT / "ops" / "maintain-rawdatadir-sidecar.sh").read_text(encoding="utf-8")

        self.assertIn("maintenance_backoff_reason rawdatadir_content_seal", script)
        self.assertIn("deferring raw datadir sidecar content sealing", script)
        self.assertLess(
            script.index("raw datadir sidecar safe check passed"),
            script.index("maintenance_backoff_reason rawdatadir_content_seal"),
        )
        self.assertLess(
            script.index("maintenance_backoff_reason rawdatadir_content_seal"),
            script.index("sealing raw datadir sidecar content artifact"),
        )

    def test_final_stopped_sync_keeps_storage_safety_but_disables_live_freshness(self) -> None:
        script = (ROOT / "ops" / "maintain-rawdatadir-sidecar.sh").read_text(encoding="utf-8")

        self.assertIn("FINAL_STOPPED_SYNC=", script)
        self.assertIn("final stopped sidecar sync: skipping live-status background maintenance gate", script)
        self.assertIn("final stopped sidecar sync: skipping content-seal live pressure gate", script)
        self.assertIn("eligibility_require_evm_reference_fresh=0", script)
        self.assertIn("fastartifact_source_eligibility.py", script)
        self.assertIn('BDAG_RAWDATADIR_REQUIRE_EVM_REFERENCE_FRESH="$eligibility_require_evm_reference_fresh"', script)
        self.assertIn("verify-rawdatadir-sidecar.py", script)


if __name__ == "__main__":
    unittest.main()

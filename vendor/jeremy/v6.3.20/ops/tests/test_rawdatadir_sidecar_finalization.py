from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class RawdatadirSidecarFinalizationPipelineTest(unittest.TestCase):
    def test_mutable_sidecar_preserves_open_restore_point_before_refresh(self) -> None:
        script = (ROOT / "ops" / "maintain-rawdatadir-sidecar.sh").read_text(encoding="utf-8")

        self.assertIn("OPEN_RESTORE_ENABLED=", script)
        self.assertIn("create_open_restore_point", script)
        self.assertLess(script.index("create_open_restore_point"), script.index('run_low_priority "${rsync_command[@]}"'))
        self.assertIn("cp -al", script)
        self.assertIn("bdag_open_sidecar_restore_point_v1", script)

    def test_local_sidecar_copy_ignores_only_sidecar_mode_disabled(self) -> None:
        script = (ROOT / "ops" / "maintain-rawdatadir-sidecar.sh").read_text(encoding="utf-8")

        self.assertIn("LOCAL_SIDECAR_COPY=", script)
        self.assertIn("local_sidecar_copy_can_ignore_reasons", script)
        self.assertIn("sidecar_mode_disabled", script)
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
        self.assertIn("safety_require_evm_reference_fresh=0", script)
        self.assertIn("BDAG_RAWDATADIR_SIDECAR_CONTENT_FINALIZED=1", script)
        self.assertIn("rawdatadir_sidecar_safety.py", script)
        self.assertIn('BDAG_RAWDATADIR_REQUIRE_EVM_REFERENCE_FRESH="$safety_require_evm_reference_fresh"', script)
        self.assertIn("verify-rawdatadir-sidecar.py", script)

    def test_content_seal_receives_signing_identity_from_timer_env(self) -> None:
        script = (ROOT / "ops" / "maintain-rawdatadir-sidecar.sh").read_text(encoding="utf-8")

        self.assertIn("append_seal_env_if_set", script)
        self.assertIn("append_seal_env_if_set BDAG_RAWDATADIR_SIGNING_KEY_FILE", script)
        self.assertIn("append_seal_env_if_set BDAG_RAWDATADIR_SIGNING_KEY_ID", script)
        self.assertIn("append_seal_env_if_set BDAG_RAWDATADIR_SIGNING_KEY_HEX", script)
        self.assertIn("append_seal_env_if_set BDAG_RAWDATADIR_TRUSTED_SIGNERS", script)
        self.assertIn("append_seal_env_if_set BDAG_IPFS_SEGMENT_SIGNING_KEY_FILE", script)
        self.assertIn("append_seal_env_if_set BDAG_IPFS_SEGMENT_WRITER_ID", script)
        self.assertLess(
            script.index("append_seal_env_if_set BDAG_RAWDATADIR_SIGNING_KEY_FILE"),
            script.index('sudo -n env "${seal_env[@]}"'),
        )


if __name__ == "__main__":
    unittest.main()

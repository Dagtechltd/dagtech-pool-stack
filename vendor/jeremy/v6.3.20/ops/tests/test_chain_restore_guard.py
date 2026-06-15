import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))
MODULE_PATH = ROOT / "ops" / "chain_restore_guard.py"
SPEC = importlib.util.spec_from_file_location("chain_restore_guard", MODULE_PATH)
chain_restore_guard = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(chain_restore_guard)


class ChainRestoreGuardTest(unittest.TestCase):
    def write_json(self, path: Path, payload: dict[str, object], now: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        os.utime(path, (now, now))

    def test_segment_writer_status_does_not_satisfy_state_checkpoint_freshness(self) -> None:
        now = int(time.time())
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            segment_status = base / "segment-writer-status.json"
            content_status = base / "ipfs-content-sidecar-status.json"
            raw_index = base / "rawdatadir-content-index.json"
            self.write_json(
                segment_status,
                {
                    "state": "published",
                    "index_cid": "bafk-segment-index",
                    "last_order": 123,
                },
                now,
            )

            with mock.patch.object(
                chain_restore_guard,
                "configured_status_files",
                return_value={
                    "ipfs_segment_writer": segment_status,
                    "ipfs_content_sidecar": content_status,
                    "rawdatadir_content_index": raw_index,
                },
            ), mock.patch.object(chain_restore_guard, "MAX_RESTORE_AGE_SECONDS", 600):
                status = chain_restore_guard.restore_status(now)

        self.assertFalse(status["fresh"])
        self.assertEqual(status["fresh_sources"], [])
        self.assertFalse(status["raw_state_checkpoint"]["ready"])
        self.assertIn("rawdatadir_state_checkpoint", status["stale_or_missing"])
        self.assertIn(
            "rawdatadir_manifest_not_verified:missing",
            status["raw_state_checkpoint"]["reasons"],
        )

    def test_verified_raw_state_checkpoint_satisfies_restore_freshness(self) -> None:
        now = int(time.time())
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            content_status = base / "ipfs-content-sidecar-status.json"
            raw_index = base / "rawdatadir-content-index.json"
            self.write_json(
                content_status,
                {
                    "state": "published",
                    "artifact_cid": "bafy-raw-artifact",
                    "index_cid": "bafk-raw-index",
                    "manifest_verification": {"state": "verified", "verified_signature_key_ids": ["writer-a"]},
                },
                now,
            )
            self.write_json(
                raw_index,
                {
                    "document_type": "bdag_ipfs_content_index_v1",
                    "artifact_type": "raw_datadir_checkpoint",
                    "network": "mainnet",
                    "artifact_cid": "bafy-raw-artifact",
                    "index_cid": "bafk-raw-index",
                    "tip_order": 10560000,
                    "tip_hash": "0x" + "a" * 64,
                    "state_root": "0x" + "b" * 64,
                    "manifest_verification": {"state": "verified", "verified_signature_key_ids": ["writer-a"]},
                },
                now,
            )

            with mock.patch.object(
                chain_restore_guard,
                "configured_status_files",
                return_value={
                    "ipfs_content_sidecar": content_status,
                    "rawdatadir_content_index": raw_index,
                },
            ), mock.patch.object(chain_restore_guard, "MAX_RESTORE_AGE_SECONDS", 600):
                status = chain_restore_guard.restore_status(now)

        self.assertTrue(status["fresh"])
        self.assertEqual(status["fresh_sources"], ["rawdatadir_state_checkpoint"])
        self.assertTrue(status["raw_state_checkpoint"]["ready"])
        self.assertEqual(status["raw_state_checkpoint"]["artifact_cid"], "bafy-raw-artifact")
        self.assertEqual(status["raw_state_checkpoint"]["index_cid"], "bafk-raw-index")

    def test_raw_state_checkpoint_without_verified_manifest_is_not_fresh(self) -> None:
        now = int(time.time())
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            content_status = base / "ipfs-content-sidecar-status.json"
            raw_index = base / "rawdatadir-content-index.json"
            self.write_json(
                content_status,
                {
                    "state": "published",
                    "artifact_cid": "bafy-raw-artifact",
                    "index_cid": "bafk-raw-index",
                },
                now,
            )
            self.write_json(
                raw_index,
                {
                    "document_type": "bdag_ipfs_content_index_v1",
                    "artifact_type": "raw_datadir_checkpoint",
                    "network": "mainnet",
                    "artifact_cid": "bafy-raw-artifact",
                    "index_cid": "bafk-raw-index",
                },
                now,
            )

            with mock.patch.object(
                chain_restore_guard,
                "configured_status_files",
                return_value={
                    "ipfs_content_sidecar": content_status,
                    "rawdatadir_content_index": raw_index,
                },
            ), mock.patch.object(chain_restore_guard, "MAX_RESTORE_AGE_SECONDS", 600):
                status = chain_restore_guard.restore_status(now)

        self.assertFalse(status["fresh"])
        self.assertIn(
            "rawdatadir_manifest_not_verified:missing",
            status["raw_state_checkpoint"]["reasons"],
        )

    def test_timer_start_respects_automation_control(self) -> None:
        now = int(time.time())
        decision = mock.Mock()
        decision.allowed = False
        decision.reason = "transition_hold does not allow this mutation"
        decision.as_dict.return_value = {"allowed": False, "reason": decision.reason}

        with mock.patch.object(chain_restore_guard, "configured_timers", return_value=["bdag-ipfs-content-sidecar.timer"]), mock.patch.object(
            chain_restore_guard,
            "unit_active",
            return_value=False,
        ), mock.patch.object(
            chain_restore_guard,
            "unit_start_allowed",
            return_value=decision,
        ), mock.patch.object(
            chain_restore_guard,
            "start_unit",
            side_effect=AssertionError("systemctl start must not run"),
        ), mock.patch.object(
            chain_restore_guard,
            "append_incident",
        ) as append_incident:
            result = chain_restore_guard.ensure_ipfs_timers({}, now)

        self.assertFalse(result["bdag-ipfs-content-sidecar.timer"]["started"])
        self.assertEqual(decision.reason, result["bdag-ipfs-content-sidecar.timer"]["stderr"])
        append_incident.assert_called_once()


if __name__ == "__main__":
    unittest.main()

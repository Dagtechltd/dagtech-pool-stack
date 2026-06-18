from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "ops" / "restore_candidate_validate.py"
SPEC = importlib.util.spec_from_file_location("restore_candidate_validate", MODULE_PATH)
restore_candidate_validate = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(restore_candidate_validate)


class RestoreCandidateValidateTest(unittest.TestCase):
    def write_json(self, path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def make_artifact(
        self,
        root: Path,
        manifest_extra: dict[str, object] | None = None,
        metadata_extra: dict[str, object] | None = None,
    ) -> tuple[Path, Path]:
        artifact = root / "artifact"
        artifact.mkdir()
        (artifact / "node-chain-mainnet.tar.zst").write_bytes(b"payload")
        manifest = {
            "artifact_type": "chain_checkpoint",
            "network": "mainnet",
            "chain_id": "1043",
            "genesis_hash": "0xabc",
            "tip_order": 9500000,
            "tip_hash": "0xtip",
            "state_root": "0x1234",
            "metadata": {
                "archive": "node-chain-mainnet.tar.zst",
                "source": "finalized-snapshot",
            },
            "signatures": [{"key_id": "test", "signature": "abcd"}],
        }
        if manifest_extra:
            manifest.update(manifest_extra)
        self.write_json(artifact / "manifest.json", manifest)
        metadata = {
            "signed_manifest_valid": True,
            "finalized_source": True,
            "independent_anchor_match": True,
            "offline_db_open": True,
            "restore_trial_passed": True,
            "consensus_validated": True,
            "mineable_validated": False,
        }
        if metadata_extra:
            metadata.update(metadata_extra)
        metadata_path = artifact / "restore-candidate-metadata.json"
        self.write_json(metadata_path, metadata)
        return artifact, metadata_path

    def test_validated_artifact_policy_passes_without_mineable_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact, metadata_path = self.make_artifact(Path(tmp))

            payload = restore_candidate_validate.validate_candidate(
                artifact,
                "artifact",
                metadata_path=metadata_path,
            )

        self.assertTrue(payload["promotable"])
        self.assertEqual([], payload["blocking_reasons"])
        self.assertFalse(payload["mineable_validated"])

    def test_require_mineable_adds_mineable_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact, metadata_path = self.make_artifact(Path(tmp))

            payload = restore_candidate_validate.validate_candidate(
                artifact,
                "artifact",
                metadata_path=metadata_path,
                require_mineable=True,
            )

        self.assertFalse(payload["promotable"])
        self.assertIn("mineable_validation_failed", payload["blocking_reasons"])

    def test_do_not_publish_marker_blocks_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact, metadata_path = self.make_artifact(Path(tmp))
            (artifact / "DO_NOT_PUBLISH.txt").write_text("forensic only\n", encoding="utf-8")

            payload = restore_candidate_validate.validate_candidate(
                artifact,
                "artifact",
                metadata_path=metadata_path,
            )

        self.assertFalse(payload["do_not_publish_absent"])
        self.assertIn("do_not_publish_present", payload["blocking_reasons"])

    def test_unsigned_manifest_blocks_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact, metadata_path = self.make_artifact(Path(tmp), manifest_extra={"signatures": []})

            payload = restore_candidate_validate.validate_candidate(
                artifact,
                "artifact",
                metadata_path=metadata_path,
            )

        self.assertFalse(payload["signed_manifest_valid"])
        self.assertIn("unsigned_manifest", payload["blocking_reasons"])

    def test_unsupported_artifact_type_blocks_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact, metadata_path = self.make_artifact(
                Path(tmp),
                manifest_extra={"artifact_type": "legacy_archive"},
            )

            payload = restore_candidate_validate.validate_candidate(
                artifact,
                "artifact",
                metadata_path=metadata_path,
            )

        self.assertFalse(payload["promotable"])
        self.assertIn("unsupported_artifact_type:legacy_archive", payload["blocking_reasons"])

    def test_signature_material_without_validation_blocks_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact, metadata_path = self.make_artifact(
                Path(tmp),
                metadata_extra={"signed_manifest_valid": False},
            )

            payload = restore_candidate_validate.validate_candidate(
                artifact,
                "artifact",
                metadata_path=metadata_path,
            )

        self.assertFalse(payload["signed_manifest_valid"])
        self.assertIn("signed_manifest_validation_failed", payload["blocking_reasons"])

    def test_missing_artifact_payload_blocks_content_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact, metadata_path = self.make_artifact(Path(tmp))
            (artifact / "node-chain-mainnet.tar.zst").unlink()

            payload = restore_candidate_validate.validate_candidate(
                artifact,
                "artifact",
                metadata_path=metadata_path,
            )

        self.assertFalse(payload["content_complete"])
        self.assertIn("content_incomplete", payload["blocking_reasons"])

    def test_manifest_payload_paths_must_stay_under_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact, metadata_path = self.make_artifact(
                Path(tmp),
                manifest_extra={"metadata": {"archive": "../outside.tar.zst"}},
            )

            payload = restore_candidate_validate.validate_candidate(
                artifact,
                "artifact",
                metadata_path=metadata_path,
            )

        self.assertFalse(payload["content_complete"])
        self.assertIn("content_incomplete", payload["blocking_reasons"])

    def test_active_single_mining_source_blocks_even_when_other_evidence_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact, metadata_path = self.make_artifact(
                Path(tmp),
                metadata_extra={"source_was_active_single_mining_node": True},
            )

            payload = restore_candidate_validate.validate_candidate(
                artifact,
                "artifact",
                metadata_path=metadata_path,
            )

        self.assertTrue(payload["source_was_active_single_mining_node"])
        self.assertIn("active_single_mining_node_source", payload["blocking_reasons"])

    def test_hard_restore_failure_text_blocks_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact, metadata_path = self.make_artifact(
                Path(tmp),
                metadata_extra={
                    "restore_logs": [
                        "unknown ancestor while importing restored datadir",
                        "Chain is stateless, wait state sync",
                    ]
                },
            )

            payload = restore_candidate_validate.validate_candidate(
                artifact,
                "artifact",
                metadata_path=metadata_path,
            )

        self.assertIn("unknown_ancestor", payload["blocking_reasons"])
        self.assertIn("stateless_genesis_after_restore", payload["blocking_reasons"])

    def test_zero_state_root_or_genesis_mismatch_blocks_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact, metadata_path = self.make_artifact(
                Path(tmp),
                manifest_extra={"state_root": "0x00000000000000000000000000000000"},
                metadata_extra={"genesis_match": False},
            )

            payload = restore_candidate_validate.validate_candidate(
                artifact,
                "artifact",
                metadata_path=metadata_path,
            )

        self.assertFalse(payload["state_root_nonzero_expected_blocks"])
        self.assertIn("state_root_zero_or_missing", payload["blocking_reasons"])
        self.assertIn("network_or_genesis_mismatch", payload["blocking_reasons"])

    def test_private_or_ephemeral_paths_fail_file_safety(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact, metadata_path = self.make_artifact(Path(tmp))
            (artifact / "bdageth").mkdir()
            (artifact / "bdageth" / "nodekey").write_text("secret-ish\n", encoding="utf-8")

            payload = restore_candidate_validate.validate_candidate(
                artifact,
                "artifact",
                metadata_path=metadata_path,
            )

        self.assertFalse(payload["file_safe"])
        self.assertIn("file_safety_failed", payload["blocking_reasons"])
        self.assertIn("bdageth/nodekey", payload["evidence"]["unsafe_paths"])

    def test_cli_json_returns_nonzero_for_blocked_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact, _metadata_path = self.make_artifact(Path(tmp))
            (artifact / "DO_NOT_PUBLISH").write_text("unsafe\n", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "--candidate",
                    str(artifact),
                    "--type",
                    "artifact",
                    "--json",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertIn("do_not_publish_present", payload["blocking_reasons"])


if __name__ == "__main__":
    unittest.main()

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "ops" / "ipfs_content_sidecar.py"
SPEC = importlib.util.spec_from_file_location("ipfs_content_sidecar", MODULE_PATH)
ipfs_content_sidecar = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(ipfs_content_sidecar)


class IPFSContentSidecarTest(unittest.TestCase):
    def test_parse_cid_uses_final_ipfs_add_line(self) -> None:
        self.assertEqual(
            ipfs_content_sidecar.parse_cid("bafy-child file\nbafy-root dir\n"),
            "bafy-root",
        )

    def test_do_not_publish_marker_blocks_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "artifact"
            artifact.mkdir()
            manifest = artifact / "manifest.json"
            manifest.write_text(json.dumps({"signatures": [{"signature": "abcd"}]}), encoding="utf-8")
            (artifact / "DO_NOT_PUBLISH.txt").write_text("unsafe\n", encoding="utf-8")

            blockers = ipfs_content_sidecar.artifact_publish_blockers(
                artifact,
                manifest,
                json.loads(manifest.read_text(encoding="utf-8")),
                {},
            )

        self.assertTrue(any(item.startswith("do_not_publish_marker:") for item in blockers))

    def test_unsigned_manifest_blocks_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "artifact"
            artifact.mkdir()
            manifest = artifact / "manifest.json"
            manifest.write_text(json.dumps({"artifact_type": "raw_datadir_checkpoint"}), encoding="utf-8")

            blockers = ipfs_content_sidecar.artifact_publish_blockers(
                artifact,
                manifest,
                json.loads(manifest.read_text(encoding="utf-8")),
                {},
            )

        self.assertIn("manifest_unsigned", blockers)

    def test_non_mainnet_manifest_network_blocks_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "artifact"
            artifact.mkdir()
            manifest = artifact / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "artifact_type": "raw_datadir_checkpoint",
                        "network": "not-mainnet",
                        "signatures": [{"key_id": "test", "signature": "abcd"}],
                    }
                ),
                encoding="utf-8",
            )

            blockers = ipfs_content_sidecar.artifact_publish_blockers(
                artifact,
                manifest,
                json.loads(manifest.read_text(encoding="utf-8")),
                {},
            )

        self.assertIn("manifest_non_mainnet_network:not-mainnet", blockers)

    def test_dry_run_ready_requires_publish_allowed_source_and_signed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            artifact = base / "current"
            artifact.mkdir()
            manifest = artifact / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "artifact_type": "raw_datadir_checkpoint",
                        "network": "mainnet",
                        "block_total": 123,
                        "signatures": [{"key_id": "test", "signature": "abcd"}],
                    }
                ),
                encoding="utf-8",
            )
            status = base / "status.json"
            env = {
                "BDAG_PROJECT_ROOT": str(ROOT),
                "BDAG_IPFS_CONTENT_SIDECAR_MODE": "auto",
                "BDAG_RAWDATADIR_SIDECAR_CONTENT_BASE": str(base.parent),
                "BDAG_IPFS_CONTENT_ARTIFACT_DIR": str(artifact),
                "BDAG_IPFS_CONTENT_ARTIFACT_MANIFEST": str(manifest),
                "BDAG_IPFS_CONTENT_STATUS_FILE": str(status),
                "BDAG_IPFS_CONTENT_SKIP_MAINTENANCE_DECISION": "1",
            }

            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                ipfs_content_sidecar,
                "source_eligibility",
                return_value={"eligible": True, "publish_allowed": True, "reasons": []},
            ):
                rc = ipfs_content_sidecar.main(["--dry-run"])

            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertEqual(payload["state"], "ready")
        self.assertEqual(payload["action"], "dry_run")
        self.assertEqual(payload["eligibility"]["publish_allowed"], True)
        self.assertTrue(payload["source_eligibility_required"])

    def test_dry_run_defers_when_source_eligibility_fails_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            artifact = base / "current"
            artifact.mkdir()
            manifest = artifact / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "artifact_type": "raw_datadir_checkpoint",
                        "network": "mainnet",
                        "block_total": 123,
                        "signatures": [{"key_id": "test", "signature": "abcd"}],
                    }
                ),
                encoding="utf-8",
            )
            status = base / "status.json"
            env = {
                "BDAG_PROJECT_ROOT": str(ROOT),
                "BDAG_IPFS_CONTENT_SIDECAR_MODE": "auto",
                "BDAG_RAWDATADIR_ARTIFACT_BASE": str(base),
                "BDAG_IPFS_CONTENT_STATUS_FILE": str(status),
                "BDAG_IPFS_CONTENT_SKIP_MAINTENANCE_DECISION": "1",
            }

            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                ipfs_content_sidecar,
                "source_eligibility",
                return_value={"eligible": False, "publish_allowed": False, "reasons": ["local_evm_unavailable"]},
            ):
                rc = ipfs_content_sidecar.main(["--dry-run"])

            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertEqual(payload["state"], "deferred")
        self.assertEqual(payload["eligibility"]["reasons"], ["local_evm_unavailable"])
        self.assertEqual(payload["reasons"], ["local_evm_unavailable"])
        self.assertTrue(payload["source_eligibility_required"])

    def test_source_publish_allowed_is_required_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            status = base / "status.json"
            env = {
                "BDAG_PROJECT_ROOT": str(ROOT),
                "BDAG_IPFS_CONTENT_SIDECAR_MODE": "auto",
                "BDAG_RAWDATADIR_ARTIFACT_BASE": str(base),
                "BDAG_IPFS_CONTENT_STATUS_FILE": str(status),
                "BDAG_IPFS_CONTENT_SKIP_MAINTENANCE_DECISION": "1",
            }

            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                ipfs_content_sidecar,
                "source_eligibility",
                return_value={"eligible": True, "publish_allowed": False, "reasons": []},
            ):
                rc = ipfs_content_sidecar.main(["--dry-run"])

            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertEqual(payload["state"], "deferred")
        self.assertEqual(payload["reasons"], ["source_publish_not_allowed"])
        self.assertTrue(payload["source_eligibility_required"])

    def test_source_eligibility_can_be_disabled_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            artifact = base / "current"
            artifact.mkdir()
            manifest = artifact / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "artifact_type": "raw_datadir_checkpoint",
                        "network": "mainnet",
                        "block_total": 123,
                        "signatures": [{"key_id": "test", "signature": "abcd"}],
                    }
                ),
                encoding="utf-8",
            )
            status = base / "status.json"
            env = {
                "BDAG_PROJECT_ROOT": str(ROOT),
                "BDAG_IPFS_CONTENT_SIDECAR_MODE": "auto",
                "BDAG_IPFS_CONTENT_ARTIFACT_DIR": str(artifact),
                "BDAG_IPFS_CONTENT_ARTIFACT_MANIFEST": str(manifest),
                "BDAG_IPFS_CONTENT_STATUS_FILE": str(status),
                "BDAG_IPFS_CONTENT_SKIP_MAINTENANCE_DECISION": "1",
                "BDAG_IPFS_CONTENT_REQUIRE_SOURCE_ELIGIBILITY": "0",
            }

            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                ipfs_content_sidecar,
                "source_eligibility",
                return_value={"eligible": False, "publish_allowed": False, "reasons": ["source_mode_disabled"]},
            ):
                rc = ipfs_content_sidecar.main(["--dry-run"])

            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertEqual(payload["state"], "ready")
        self.assertFalse(payload["source_eligibility_required"])

    def test_waiting_state_republishes_current_ipns_pointer_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            status = base / "status.json"
            index_path = base / "latest-index.json"
            index_path.write_text(json.dumps({"document_type": "bdag_ipfs_segment_index_v1"}), encoding="utf-8")
            env = {
                "BDAG_PROJECT_ROOT": str(ROOT),
                "BDAG_IPFS_CONTENT_SIDECAR_MODE": "auto",
                "BDAG_RAWDATADIR_ARTIFACT_BASE": str(base),
                "BDAG_IPFS_CONTENT_STATUS_FILE": str(status),
                "BDAG_IPFS_CONTENT_LATEST_INDEX_PATH": str(index_path),
                "BDAG_IPFS_CONTENT_DISCOVERY_FILE": str(base / "missing-discovery.json"),
                "BDAG_IPFS_CONTENT_DEFAULT_INDEX_CID": "bafk-current-index",
                "BDAG_IPFS_CONTENT_PUBLISH_IPNS": "1",
                "BDAG_IPFS_CONTENT_SKIP_MAINTENANCE_DECISION": "1",
            }

            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                ipfs_content_sidecar,
                "source_eligibility",
                return_value={"eligible": True, "publish_allowed": True, "reasons": []},
            ), mock.patch.object(
                ipfs_content_sidecar,
                "ipfs_pin_present",
                return_value=True,
            ), mock.patch.object(
                ipfs_content_sidecar,
                "publish_ipns",
                return_value={"ok": True, "stdout": "published"},
            ) as publish_ipns:
                rc = ipfs_content_sidecar.main([])

            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertEqual(payload["state"], "waiting_for_signed_artifact")
        self.assertEqual(payload["action"], "waiting_republish_current_ipns")
        self.assertEqual(payload["index_cid"], "bafk-current-index")
        self.assertEqual(payload["ipns"], {"ok": True, "stdout": "published"})
        publish_ipns.assert_called_once_with("bafk-current-index", mock.ANY)

    def test_ipns_republish_uses_discovery_cid_before_env_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            discovery = base / "discovery.json"
            discovery.write_text(json.dumps({"current_latest_index_cid": "bafk-discovery-index"}), encoding="utf-8")
            env = {
                "BDAG_IPFS_CONTENT_DISCOVERY_FILE": str(discovery),
                "BDAG_IPFS_CONTENT_DEFAULT_INDEX_CID": "bafk-env-default",
            }

            index_cid = ipfs_content_sidecar.current_index_cid({}, env)

        self.assertEqual(index_cid, "bafk-discovery-index")


if __name__ == "__main__":
    unittest.main()

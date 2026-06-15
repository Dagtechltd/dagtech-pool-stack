import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "ops" / "ipfs_content_sidecar.py"
SPEC = importlib.util.spec_from_file_location("ipfs_content_sidecar", MODULE_PATH)
ipfs_content_sidecar = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(ipfs_content_sidecar)
RESTORE_MODULE_PATH = ROOT / "ops" / "restore-rawdatadir-segment-artifact.py"
RESTORE_SPEC = importlib.util.spec_from_file_location("restore_rawdatadir_segment_artifact", RESTORE_MODULE_PATH)
restore_rawdatadir_segment_artifact = importlib.util.module_from_spec(RESTORE_SPEC)
assert RESTORE_SPEC and RESTORE_SPEC.loader
RESTORE_SPEC.loader.exec_module(restore_rawdatadir_segment_artifact)


class IPFSContentSidecarTest(unittest.TestCase):
    def write_signed_artifact(self, artifact: Path) -> tuple[Path, dict[str, object], str]:
        artifact.mkdir(parents=True, exist_ok=True)
        chunks = artifact / "chunks"
        chunks.mkdir()
        body = b"trusted checkpoint test\n"
        chunk = chunks / "0.bin"
        chunk.write_bytes(body)
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        manifest: dict[str, object] = {
            "format_version": 2,
            "artifact_type": "raw_datadir_checkpoint",
            "network": "mainnet",
            "chain_id": 1404,
            "genesis_hash": "0x" + "a" * 64,
            "tip_order": 123,
            "tip_hash": "0x" + "b" * 64,
            "block_total": 124,
            "state_root": "0x" + "c" * 64,
            "chunk_hash_algo": "sha256",
            "encoding": "content-addressed-raw-chunks",
            "layout": "directory",
            "created_at": "2026-06-11T00:00:00Z",
            "metadata": {
                "source": "unit-test-sidecar",
                "finalized_sidecar": "1",
                "publishable": "1",
                "canonical_json": "json_sort_keys_sha256_v1",
            },
            "sources": [{"name": "rawdatadir-sidecar", "chunk_start": 0, "chunk_count": 1}],
            "chunks": [
                {
                    "id": 0,
                    "source": "rawdatadir-sidecar",
                    "class": "raw_datadir_file_chunk",
                    "path": "chunks/0.bin",
                    "compressed_size": len(body),
                    "uncompressed_size": len(body),
                    "compressed_sha256": restore_rawdatadir_segment_artifact.hashlib.sha256(body).hexdigest(),
                }
            ],
            "files": [
                {
                    "path": "BdagChain/unit-test.dat",
                    "class": "raw_datadir_file",
                    "size": len(body),
                    "sha256": restore_rawdatadir_segment_artifact.hashlib.sha256(body).hexdigest(),
                    "chunk_start": 0,
                    "chunk_count": 1,
                    "mode": 0o644,
                }
            ],
        }
        artifact_root = restore_rawdatadir_segment_artifact.compute_artifact_root(manifest)
        manifest["artifact_root"] = artifact_root
        manifest["signatures"] = [
            {
                "key_id": "unit-test",
                "algorithm": "ed25519",
                "public_key": public_key.hex(),
                "signature": private_key.sign(bytes.fromhex(artifact_root)).hex(),
                "signed_at": "2026-06-11T00:00:01Z",
            }
        ]
        (artifact / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        return artifact, manifest, f"unit-test={public_key.hex()}"

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

    def test_dry_run_ready_requires_signed_sidecar_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            artifact = base / "current"
            _, _, trusted_signers = self.write_signed_artifact(artifact)
            manifest = artifact / "manifest.json"
            status = base / "status.json"
            env = {
                "BDAG_PROJECT_ROOT": str(ROOT),
                "BDAG_IPFS_CONTENT_SIDECAR_MODE": "auto",
                "BDAG_RAWDATADIR_SIDECAR_CONTENT_BASE": str(base.parent),
                "BDAG_IPFS_CONTENT_ARTIFACT_DIR": str(artifact),
                "BDAG_IPFS_CONTENT_ARTIFACT_MANIFEST": str(manifest),
                "BDAG_IPFS_CONTENT_STATUS_FILE": str(status),
                "BDAG_IPFS_CONTENT_SKIP_MAINTENANCE_DECISION": "1",
                "BDAG_RAWDATADIR_TRUSTED_SIGNERS": trusted_signers,
                "BDAG_RAWDATADIR_REQUIRE_TRUSTED_SIGNER": "1",
                "BDAG_RAWDATADIR_REQUIRE_CHAIN_ANCHOR": "0",
            }

            with mock.patch.dict(os.environ, env, clear=False):
                rc = ipfs_content_sidecar.main(["--dry-run"])

            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertEqual(payload["state"], "ready")
        self.assertEqual(payload["action"], "dry_run")
        self.assertEqual(payload["manifest_verification"]["state"], "verified")
        self.assertEqual(payload["manifest_verification"]["verified_signature_key_ids"], ["unit-test"])

    def test_publish_blocks_signature_not_in_trust_roster(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "artifact"
            _, _, _trusted_signers = self.write_signed_artifact(artifact)
            manifest = artifact / "manifest.json"

            blockers = ipfs_content_sidecar.artifact_publish_blockers(
                artifact,
                manifest,
                json.loads(manifest.read_text(encoding="utf-8")),
                {
                    "BDAG_RAWDATADIR_TRUSTED_SIGNERS": "other=" + ("00" * 32),
                    "BDAG_RAWDATADIR_REQUIRE_TRUSTED_SIGNER": "1",
                    "BDAG_RAWDATADIR_REQUIRE_CHAIN_ANCHOR": "0",
                },
            )

        self.assertIn("manifest_signature_untrusted", blockers)

    def test_publish_blocks_missing_required_chain_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "artifact"
            _, _, trusted_signers = self.write_signed_artifact(artifact)
            manifest = artifact / "manifest.json"

            blockers = ipfs_content_sidecar.artifact_publish_blockers(
                artifact,
                manifest,
                json.loads(manifest.read_text(encoding="utf-8")),
                {
                    "BDAG_RAWDATADIR_TRUSTED_SIGNERS": trusted_signers,
                    "BDAG_RAWDATADIR_REQUIRE_TRUSTED_SIGNER": "1",
                    "BDAG_RAWDATADIR_REQUIRE_CHAIN_ANCHOR": "1",
                    "BDAG_RAWDATADIR_CHAIN_ANCHOR_REFERENCE_EVM_URL": "http://reference-rpc",
                },
            )

        self.assertTrue(any(item.startswith("manifest_chain_anchor_untrusted:") for item in blockers))

    def test_waiting_state_republishes_current_ipns_pointer_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            status = base / "status.json"
            index_path = base / "rawdatadir-content-index.json"
            index_path.write_text(
                json.dumps(
                    {
                        "document_type": "bdag_ipfs_content_index_v1",
                        "index_cid": "bafk-current-index",
                    }
                ),
                encoding="utf-8",
            )
            env = {
                "BDAG_PROJECT_ROOT": str(ROOT),
                "BDAG_IPFS_CONTENT_SIDECAR_MODE": "auto",
                "BDAG_RAWDATADIR_ARTIFACT_BASE": str(base),
                "BDAG_IPFS_CONTENT_ARTIFACT_DIR": str(base / "current"),
                "BDAG_IPFS_CONTENT_ARTIFACT_MANIFEST": str(base / "current" / "manifest.json"),
                "BDAG_IPFS_CONTENT_STATUS_FILE": str(status),
                "BDAG_IPFS_RAWDATADIR_CONTENT_INDEX_PATH": str(index_path),
                "BDAG_IPFS_CONTENT_DISCOVERY_FILE": str(base / "missing-discovery.json"),
                "BDAG_IPFS_RAWDATADIR_CONTENT_PUBLISH_IPNS": "1",
                "BDAG_IPFS_CONTENT_SKIP_MAINTENANCE_DECISION": "1",
            }

            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
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

    def test_ipns_republish_uses_rawdatadir_discovery_cid_before_env_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            discovery = base / "discovery.json"
            discovery.write_text(json.dumps({"current_rawdatadir_index_cid": "bafk-raw-index"}), encoding="utf-8")
            env = {
                "BDAG_IPFS_CONTENT_DISCOVERY_FILE": str(discovery),
                "BDAG_IPFS_RAWDATADIR_CONTENT_DEFAULT_INDEX_CID": "bafk-env-default",
            }

            index_cid = ipfs_content_sidecar.current_index_cid({}, env)

        self.assertEqual(index_cid, "bafk-raw-index")

    def test_ipns_republish_does_not_use_segment_discovery_cid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            discovery = base / "discovery.json"
            discovery.write_text(json.dumps({"current_latest_index_cid": "bafk-segment-index"}), encoding="utf-8")
            env = {
                "BDAG_IPFS_CONTENT_DISCOVERY_FILE": str(discovery),
                "BDAG_IPFS_RAWDATADIR_CONTENT_DEFAULT_INDEX_CID": "bafk-raw-default",
            }

            index_cid = ipfs_content_sidecar.current_index_cid({}, env)

        self.assertEqual(index_cid, "bafk-raw-default")

    def test_published_raw_checkpoint_updates_dedicated_discovery_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            discovery = base / "discovery.json"
            discovery.write_text(
                json.dumps(
                    {
                        "document_type": "bdag_ipfs_content_discovery_v1",
                        "current_latest_index_cid": "bafk-segment-index",
                    }
                ),
                encoding="utf-8",
            )
            env = {"BDAG_IPFS_CONTENT_DISCOVERY_FILE": str(discovery)}

            ipfs_content_sidecar.update_discovery(
                "bafk-raw-index",
                "bafy-raw-artifact",
                {
                    "artifact_type": "raw_datadir_checkpoint",
                    "network": "mainnet",
                    "chain_id": 1404,
                    "tip_order": 123,
                    "tip_hash": "0xabc",
                    "state_root": "0xdef",
                },
                env,
            )
            payload = json.loads(discovery.read_text(encoding="utf-8"))

        self.assertEqual(payload["current_latest_index_cid"], "bafk-segment-index")
        self.assertEqual(payload["current_rawdatadir_index_cid"], "bafk-raw-index")
        self.assertEqual(payload["current_rawdatadir_artifact_cid"], "bafy-raw-artifact")
        self.assertEqual(payload["current_rawdatadir_content"]["document_type"], "bdag_ipfs_content_index_v1")


if __name__ == "__main__":
    unittest.main()

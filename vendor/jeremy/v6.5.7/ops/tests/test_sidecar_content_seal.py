import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "ops" / "seal_rawdatadir_sidecar_content.py"
SPEC = importlib.util.spec_from_file_location("seal_rawdatadir_sidecar_content", MODULE_PATH)
seal = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(seal)


class SidecarContentSealTest(unittest.TestCase):
    def make_sidecar(self, base: Path) -> Path:
        sidecar = base / "sidecar" / "mainnet"
        (sidecar / "BdagChain").mkdir(parents=True)
        (sidecar / "BdagChain" / "block.dat").write_bytes(b"abcdefghij")
        return sidecar

    def test_seals_signed_chunk_manifest_and_excludes_identity_backups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sidecar = self.make_sidecar(base)
            (sidecar / "peerstore.syncv2-backup-20260525035115").mkdir()
            (sidecar / "peerstore.syncv2-backup-20260525035115" / "peer").write_text("private-ish\n", encoding="utf-8")
            (sidecar / "bdageth" / "nodes").mkdir(parents=True)
            (sidecar / "bdageth" / "nodes" / "node").write_text("cache\n", encoding="utf-8")

            content_base = base / "content"
            status = base / "status.json"
            env = {
                "BDAG_PROJECT_ROOT": str(ROOT),
                "BDAG_RAWDATADIR_SIDECAR_DIR": str(sidecar),
                "BDAG_RAWDATADIR_SIDECAR_CONTENT_BASE": str(content_base),
                "BDAG_RAWDATADIR_SIDECAR_CONTENT_STATUS_FILE": str(status),
                "BDAG_RAWDATADIR_SIDECAR_CONTENT_CHUNK_SIZE": "4",
                "BDAG_RAWDATADIR_SIDECAR_CONTENT_FINALIZED": "1",
                "BDAG_FASTSYNC_ARTIFACT_SIGNING_KEY_ID": "test-key",
                "BDAG_FASTSYNC_ARTIFACT_SIGNING_KEY_HEX": "00" * 32,
                "BDAG_RAWDATADIR_STATE_ROOT": "0x" + ("1" * 64),
                "BDAG_RAWDATADIR_GENESIS_HASH": "0x" + ("2" * 64),
            }

            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                seal,
                "collect_anchor",
                return_value={
                    "network": "mainnet",
                    "chain_id": 1404,
                    "block_total": 10,
                    "tip_order": 9,
                    "tip_hash": "0x" + ("3" * 64),
                    "state_root": "0x" + ("1" * 64),
                    "genesis_hash": "0x" + ("2" * 64),
                },
            ):
                rc = seal.main([])

            self.assertEqual(rc, 0)
            payload = json.loads(status.read_text(encoding="utf-8"))
            manifest = json.loads((content_base / "current" / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["state"], "sealed")
        self.assertTrue(payload["signed"])
        self.assertTrue(payload["publishable"])
        self.assertEqual(manifest["artifact_root"], seal.manifest_root(manifest))
        self.assertEqual(manifest["metadata"]["finalized_sidecar"], "1")
        self.assertEqual(manifest["metadata"]["publishable"], "1")
        self.assertEqual(manifest["signatures"][0]["key_id"], "test-key")
        self.assertEqual([item["path"] for item in manifest["files"]], ["BdagChain/block.dat"])
        self.assertEqual(len(manifest["chunks"]), 1)
        self.assertFalse((content_base / "current" / "DO_NOT_PUBLISH.txt").exists())

    def test_signed_hot_sidecar_without_finalization_is_not_publishable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sidecar = self.make_sidecar(base)
            content_base = base / "content"
            status = base / "status.json"
            env = {
                "BDAG_PROJECT_ROOT": str(ROOT),
                "BDAG_RAWDATADIR_SIDECAR_DIR": str(sidecar),
                "BDAG_RAWDATADIR_SIDECAR_CONTENT_BASE": str(content_base),
                "BDAG_RAWDATADIR_SIDECAR_CONTENT_STATUS_FILE": str(status),
                "BDAG_FASTSYNC_ARTIFACT_SIGNING_KEY_ID": "test-key",
                "BDAG_FASTSYNC_ARTIFACT_SIGNING_KEY_HEX": "00" * 32,
                "BDAG_RAWDATADIR_STATE_ROOT": "0x" + ("1" * 64),
                "BDAG_RAWDATADIR_GENESIS_HASH": "0x" + ("2" * 64),
            }

            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                seal,
                "collect_anchor",
                return_value={
                    "network": "mainnet",
                    "chain_id": 1404,
                    "block_total": 10,
                    "tip_order": 9,
                    "tip_hash": "0x" + ("3" * 64),
                    "state_root": "0x" + ("1" * 64),
                    "genesis_hash": "0x" + ("2" * 64),
                },
            ):
                rc = seal.main([])

            payload = json.loads(status.read_text(encoding="utf-8"))
            manifest = json.loads((content_base / "current" / "manifest.json").read_text(encoding="utf-8"))
            marker = content_base / "current" / "DO_NOT_PUBLISH.txt"
            marker_exists = marker.exists()
            marker_text = marker.read_text(encoding="utf-8") if marker_exists else ""

        self.assertEqual(rc, 0)
        self.assertEqual(payload["state"], "sealed_not_publishable")
        self.assertTrue(payload["signed"])
        self.assertFalse(payload["finalized"])
        self.assertFalse(payload["publishable"])
        self.assertIn("hot_sidecar_not_finalized", payload["reasons"])
        self.assertEqual(manifest["metadata"]["finalized_sidecar"], "0")
        self.assertEqual(manifest["metadata"]["publishable"], "0")
        self.assertTrue(marker_exists)
        self.assertIn("hot_sidecar_not_finalized", marker_text)

    def test_configured_finalization_anchor_does_not_need_live_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sidecar = self.make_sidecar(base)
            content_base = base / "content"
            status = base / "status.json"
            env = {
                "BDAG_PROJECT_ROOT": str(ROOT),
                "BDAG_RAWDATADIR_SIDECAR_DIR": str(sidecar),
                "BDAG_RAWDATADIR_SIDECAR_CONTENT_BASE": str(content_base),
                "BDAG_RAWDATADIR_SIDECAR_CONTENT_STATUS_FILE": str(status),
                "BDAG_RAWDATADIR_SIDECAR_CONTENT_FINALIZED": "1",
                "BDAG_FASTSYNC_ARTIFACT_SIGNING_KEY_ID": "test-key",
                "BDAG_FASTSYNC_ARTIFACT_SIGNING_KEY_HEX": "00" * 32,
                "BDAG_RAWDATADIR_BLOCK_TOTAL": "10",
                "BDAG_RAWDATADIR_TIP_ORDER": "9",
                "BDAG_RAWDATADIR_TIP_HASH": "0x" + ("3" * 64),
                "BDAG_RAWDATADIR_STATE_ROOT": "0x" + ("1" * 64),
                "BDAG_RAWDATADIR_GENESIS_HASH": "0x" + ("2" * 64),
            }

            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                seal,
                "rpc",
                side_effect=AssertionError("live RPC should not be used with a complete finalization anchor"),
            ), mock.patch.object(
                seal,
                "evm_rpc",
                side_effect=AssertionError("live EVM RPC should not be used with a complete finalization anchor"),
            ):
                rc = seal.main([])

            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertEqual(payload["state"], "sealed")
        self.assertTrue(payload["publishable"])
        self.assertEqual(payload["anchor"]["anchor_source"], "configured_finalization_anchor")


if __name__ == "__main__":
    unittest.main()

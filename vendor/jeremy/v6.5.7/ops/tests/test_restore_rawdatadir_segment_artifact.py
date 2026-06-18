from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "ops" / "restore-rawdatadir-segment-artifact.py"
SPEC = importlib.util.spec_from_file_location("restore_rawdatadir_segment_artifact", MODULE_PATH)
restore_rawdatadir_segment_artifact = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(restore_rawdatadir_segment_artifact)


class RestoreRawdatadirSegmentArtifactTest(unittest.TestCase):
    def make_args(
        self,
        artifact: Path,
        *,
        allow_unsigned: bool = False,
        allow_test_unsafe_metadata: bool = False,
    ) -> argparse.Namespace:
        return argparse.Namespace(
            local_artifact_dir=str(artifact),
            remote_artifact_dir=None,
            remote=None,
            ssh_control_socket=None,
            network="mainnet",
            min_tip_order=0,
            allow_unsigned=allow_unsigned,
            allow_test_unsafe_metadata=allow_test_unsafe_metadata,
        )

    def write_signed_artifact(
        self,
        root: Path,
        *,
        metadata: dict[str, str] | None = None,
        marker: str | None = None,
    ) -> tuple[Path, dict[str, object]]:
        artifact = root / "artifact"
        chunks_dir = artifact / "chunks"
        chunks_dir.mkdir(parents=True)
        chunks = [b"abc", b"defghi"]
        for index, body in enumerate(chunks):
            (chunks_dir / f"{index}.bin").write_bytes(body)
        file_body = b"".join(chunks)
        file_digest = hashlib.sha256(file_body).hexdigest()
        manifest: dict[str, object] = {
            "format_version": 2,
            "artifact_type": "raw_datadir_checkpoint",
            "network": "mainnet",
            "chain_id": 1404,
            "genesis_hash": "0x" + "a" * 64,
            "tip_order": 123,
            "tip_hash": "0x" + "b" * 64,
            "block_total": 456,
            "state_root": "0x" + "c" * 64,
            "chunk_hash_algo": "sha256",
            "layout": "directory",
            "created_at": "2026-06-01T00:00:00Z",
            "metadata": {
                "raw_datadir_source": "unit-test-sidecar",
                "publishable": "1",
                "finalized_sidecar": "1",
                **(metadata or {}),
            },
            "sources": [{"name": "raw_datadir", "chunk_start": 0, "chunk_count": len(chunks)}],
            "chunks": [
                {
                    "id": index,
                    "source": "raw_datadir",
                    "class": "raw_datadir",
                    "path": f"chunks/{index}.bin",
                    "compressed_size": len(body),
                    "compressed_sha256": hashlib.sha256(body).hexdigest(),
                }
                for index, body in enumerate(chunks)
            ],
            "files": [
                {
                    "path": "BdagChain/data.bin",
                    "class": "raw_datadir",
                    "size": len(file_body),
                    "sha256": file_digest,
                    "chunk_start": 0,
                    "chunk_count": len(chunks),
                    "mode": 0o644,
                }
            ],
        }
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        artifact_root = restore_rawdatadir_segment_artifact.compute_artifact_root(manifest)
        manifest["artifact_root"] = artifact_root
        manifest["signatures"] = [
            {
                "key_id": "unit-test",
                "algorithm": "ed25519",
                "public_key": public_key.hex(),
                "signature": private_key.sign(bytes.fromhex(artifact_root)).hex(),
                "signed_at": "2026-06-01T00:00:01Z",
            }
        ]
        (artifact / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        if marker:
            (artifact / marker).write_text("unsafe\n", encoding="utf-8")
        return artifact, manifest

    def test_signed_local_restore_reconstructs_file_and_writes_signature_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            artifact, manifest = self.write_signed_artifact(base)
            target = base / "target"
            status = base / "status.json"

            with contextlib.redirect_stdout(io.StringIO()):
                rc = restore_rawdatadir_segment_artifact.main(
                    [
                        "--local-artifact-dir",
                        str(artifact),
                        "--target-dir",
                        str(target),
                        "--status-file",
                        str(status),
                        "--progress-every",
                        "0",
                    ]
                )

            payload = json.loads(status.read_text(encoding="utf-8"))

            self.assertEqual(rc, 0)
            self.assertEqual((target / "BdagChain/data.bin").read_bytes(), b"abcdefghi")
            self.assertEqual(payload["manifest"]["artifact_root"], manifest["artifact_root"])
            self.assertEqual(payload["manifest"]["verified_signature_key_ids"], ["unit-test"])

    def test_canonical_artifact_root_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact, manifest = self.write_signed_artifact(Path(tmp))
            manifest["artifact_root"] = "0" * 64
            (artifact / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

            with self.assertRaisesRegex(
                restore_rawdatadir_segment_artifact.RestoreError,
                "artifact_root mismatch",
            ):
                restore_rawdatadir_segment_artifact.validate_manifest(manifest, self.make_args(artifact))

    def test_bad_ed25519_signature_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact, manifest = self.write_signed_artifact(Path(tmp))
            manifest["signatures"][0]["signature"] = "00" * 64  # type: ignore[index]

            with self.assertRaisesRegex(
                restore_rawdatadir_segment_artifact.RestoreError,
                "no valid Ed25519 signature",
            ):
                restore_rawdatadir_segment_artifact.validate_manifest(manifest, self.make_args(artifact))

    def test_chunk_hash_mismatch_still_blocks_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            artifact, _manifest = self.write_signed_artifact(base)
            (artifact / "chunks" / "1.bin").write_bytes(b"xxxxxx")

            with self.assertRaisesRegex(
                restore_rawdatadir_segment_artifact.RestoreError,
                "chunk hash mismatch",
            ), contextlib.redirect_stdout(io.StringIO()):
                restore_rawdatadir_segment_artifact.main(
                    [
                        "--local-artifact-dir",
                        str(artifact),
                        "--target-dir",
                        str(base / "target"),
                        "--status-file",
                        str(base / "status.json"),
                        "--progress-every",
                        "0",
                    ]
                )

    def test_unsafe_publish_metadata_and_markers_are_rejected(self) -> None:
        cases = (
            ({"DO_NOT_PUBLISH": "1"}, None, "DO_NOT_PUBLISH"),
            ({"publishable": "0"}, None, "publishable=0"),
            ({"finalized_sidecar": "0"}, None, "finalized_sidecar=0"),
            ({}, "DO_NOT_PUBLISH.txt", "do_not_publish_marker"),
        )
        for metadata, marker, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                artifact, manifest = self.write_signed_artifact(Path(tmp), metadata=metadata, marker=marker)

                with self.assertRaisesRegex(
                    restore_rawdatadir_segment_artifact.RestoreError,
                    expected,
                ):
                    restore_rawdatadir_segment_artifact.validate_manifest(manifest, self.make_args(artifact))

    def test_test_only_unsafe_metadata_override_allows_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact, manifest = self.write_signed_artifact(
                Path(tmp),
                metadata={"publishable": "0", "finalized_sidecar": "0"},
                marker="DO_NOT_PUBLISH",
            )

            verification = restore_rawdatadir_segment_artifact.validate_manifest(
                manifest,
                self.make_args(artifact, allow_test_unsafe_metadata=True),
            )

        self.assertEqual(verification["verified_signature_key_ids"], ["unit-test"])


if __name__ == "__main__":
    unittest.main()

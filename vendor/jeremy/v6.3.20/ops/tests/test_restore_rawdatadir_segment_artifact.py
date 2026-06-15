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
from unittest import mock

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
        trusted_signers: str = "",
        require_trusted_signer: bool = True,
        reference_evm_rpc_url: str = "",
        require_chain_anchor: bool = False,
        chain_anchor_finality_blocks: int = 0,
    ) -> argparse.Namespace:
        if not trusted_signers and (artifact / "manifest.json").exists():
            try:
                manifest = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
                signature = manifest["signatures"][0]
                trusted_signers = f"{signature['key_id']}={signature['public_key']}"
            except Exception:
                trusted_signers = ""
        return argparse.Namespace(
            local_artifact_dir=str(artifact),
            remote_artifact_dir=None,
            ipfs_artifact_cid=None,
            ipfs_index_cid=None,
            ipfs_index_file=None,
            discovery=None,
            remote=None,
            ssh_control_socket=None,
            ipfs_binary="ipfs",
            ipfs_timeout=600,
            network="mainnet",
            min_tip_order=0,
            allow_unsigned=allow_unsigned,
            trusted_signers=trusted_signers,
            require_trusted_signer=require_trusted_signer,
            reference_evm_rpc_url=reference_evm_rpc_url,
            require_chain_anchor=require_chain_anchor,
            chain_anchor_timeout=8,
            chain_anchor_finality_blocks=chain_anchor_finality_blocks,
            allow_test_unsafe_metadata=allow_test_unsafe_metadata,
        )

    def write_signed_artifact(
        self,
        root: Path,
        *,
        metadata: dict[str, object] | None = None,
        evm_anchor: dict[str, object] | None = None,
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
        manifest_metadata: dict[str, object] = {
            "raw_datadir_source": "unit-test-sidecar",
            "publishable": "1",
            "finalized_sidecar": "1",
            **(metadata or {}),
        }
        if evm_anchor:
            manifest_metadata["evm_anchor"] = evm_anchor
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
            "metadata": manifest_metadata,
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
                        "--trusted-signers",
                        f"unit-test={manifest['signatures'][0]['public_key']}",
                        "--no-require-chain-anchor",
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

    def test_signature_from_untrusted_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact, manifest = self.write_signed_artifact(Path(tmp))

            with self.assertRaisesRegex(
                restore_rawdatadir_segment_artifact.RestoreError,
                "trusted raw-datadir signer",
            ):
                restore_rawdatadir_segment_artifact.validate_manifest(
                    manifest,
                    self.make_args(artifact, trusted_signers="other=" + "00" * 32),
                )

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
                        "--trusted-signers",
                        f"unit-test={_manifest['signatures'][0]['public_key']}",
                        "--no-require-chain-anchor",
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

    def test_signed_evm_chain_anchor_is_verified_against_reference_rpc(self) -> None:
        anchor_hash = "0x" + "d" * 64
        anchor_state = "0x" + "e" * 64
        genesis = "0x" + "a" * 64
        evm_anchor = {
            "chain_id": 1404,
            "block_number": 100,
            "block_hash": anchor_hash,
            "state_root": anchor_state,
            "genesis_hash": genesis,
        }
        with tempfile.TemporaryDirectory() as tmp:
            artifact, manifest = self.write_signed_artifact(Path(tmp), evm_anchor=evm_anchor)

            def fake_evm_rpc(_url: str, method: str, params: list[object], _timeout: int) -> object:
                if method == "eth_chainId":
                    return "0x57c"
                if method == "eth_blockNumber":
                    return "0x3e8"
                if method == "eth_getBlockByNumber" and params[0] == "0x64":
                    return {"hash": anchor_hash, "stateRoot": anchor_state}
                if method == "eth_getBlockByNumber" and params[0] == "0x0":
                    return {"hash": genesis}
                raise AssertionError(f"unexpected RPC {method} {params}")

            with mock.patch.object(restore_rawdatadir_segment_artifact, "evm_rpc", side_effect=fake_evm_rpc):
                verification = restore_rawdatadir_segment_artifact.validate_manifest(
                    manifest,
                    self.make_args(
                        artifact,
                        reference_evm_rpc_url="http://reference-rpc",
                        require_chain_anchor=True,
                        chain_anchor_finality_blocks=600,
                    ),
                )

        self.assertEqual(verification["chain_anchor"]["state"], "verified")
        self.assertEqual(verification["chain_anchor"]["anchor_block_number"], 100)
        self.assertEqual(verification["chain_anchor"]["finality_lag_blocks"], 900)

    def test_required_evm_chain_anchor_rejects_mismatch(self) -> None:
        evm_anchor = {
            "chain_id": 1404,
            "block_number": 100,
            "block_hash": "0x" + "d" * 64,
            "state_root": "0x" + "e" * 64,
            "genesis_hash": "0x" + "a" * 64,
        }
        with tempfile.TemporaryDirectory() as tmp:
            artifact, manifest = self.write_signed_artifact(Path(tmp), evm_anchor=evm_anchor)

            def fake_evm_rpc(_url: str, method: str, params: list[object], _timeout: int) -> object:
                if method == "eth_chainId":
                    return "0x57c"
                if method == "eth_blockNumber":
                    return "0x3e8"
                if method == "eth_getBlockByNumber" and params[0] == "0x64":
                    return {"hash": "0x" + "f" * 64, "stateRoot": "0x" + "e" * 64}
                if method == "eth_getBlockByNumber" and params[0] == "0x0":
                    return {"hash": "0x" + "a" * 64}
                raise AssertionError(f"unexpected RPC {method} {params}")

            with mock.patch.object(restore_rawdatadir_segment_artifact, "evm_rpc", side_effect=fake_evm_rpc):
                with self.assertRaisesRegex(
                    restore_rawdatadir_segment_artifact.RestoreError,
                    "EVM block hash mismatch",
                ):
                    restore_rawdatadir_segment_artifact.validate_manifest(
                        manifest,
                        self.make_args(
                            artifact,
                            reference_evm_rpc_url="http://reference-rpc",
                            require_chain_anchor=True,
                            chain_anchor_finality_blocks=600,
                        ),
                    )

    def test_required_evm_chain_anchor_rejects_missing_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact, manifest = self.write_signed_artifact(Path(tmp))

            with self.assertRaisesRegex(
                restore_rawdatadir_segment_artifact.RestoreError,
                "manifest missing signed EVM chain anchor",
            ):
                restore_rawdatadir_segment_artifact.validate_manifest(
                    manifest,
                    self.make_args(
                        artifact,
                        reference_evm_rpc_url="http://reference-rpc",
                        require_chain_anchor=True,
                    ),
                )

    def test_ipfs_content_index_resolves_rawdatadir_artifact_cid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            index = base / "index.json"
            index.write_text(
                json.dumps(
                    {
                        "document_type": "bdag_ipfs_content_index_v1",
                        "network": "mainnet",
                        "artifact_type": "raw_datadir_checkpoint",
                        "artifact_cid": "bafybeigdyrzt",
                    }
                ),
                encoding="utf-8",
            )
            args = self.make_args(base, trusted_signers="unit-test=" + "00" * 32)
            args.local_artifact_dir = None
            args.ipfs_index_file = str(index)

            cid = restore_rawdatadir_segment_artifact.resolve_ipfs_artifact_cid(args)

        self.assertEqual(cid, "bafybeigdyrzt")

    def test_ipfs_content_index_rejects_segment_index_documents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            index = base / "index.json"
            index.write_text(
                json.dumps({"document_type": "bdag_ipfs_segment_index_v1", "network": "mainnet"}),
                encoding="utf-8",
            )
            args = self.make_args(base, trusted_signers="unit-test=" + "00" * 32)
            args.local_artifact_dir = None
            args.ipfs_index_file = str(index)

            with self.assertRaisesRegex(
                restore_rawdatadir_segment_artifact.RestoreError,
                "unsupported IPFS content index",
            ):
                restore_rawdatadir_segment_artifact.resolve_ipfs_artifact_cid(args)

    def test_discovery_can_resolve_rawdatadir_index_ipns_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            discovery = base / "discovery.json"
            discovery.write_text(
                json.dumps({"rawdatadir_latest_index_ipns": "/ipns/k51qzi5uqu5dexample"}),
                encoding="utf-8",
            )
            args = self.make_args(base, trusted_signers="unit-test=" + "00" * 32)
            args.local_artifact_dir = None
            args.discovery = str(discovery)
            seen: list[str] = []

            def fake_cat(_args, ipfs_path):
                seen.append(ipfs_path)
                return json.dumps(
                    {
                        "document_type": "bdag_ipfs_content_index_v1",
                        "network": "mainnet",
                        "artifact_type": "raw_datadir_checkpoint",
                        "artifact_cid": "bafyartifact",
                    }
                ).encode("utf-8")

            with mock.patch.object(restore_rawdatadir_segment_artifact, "run_ipfs_cat", side_effect=fake_cat):
                cid = restore_rawdatadir_segment_artifact.resolve_ipfs_artifact_cid(args)

        self.assertEqual(cid, "bafyartifact")
        self.assertEqual(seen, ["/ipns/k51qzi5uqu5dexample"])


if __name__ == "__main__":
    unittest.main()

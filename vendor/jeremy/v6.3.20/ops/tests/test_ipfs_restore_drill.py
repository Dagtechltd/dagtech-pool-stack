import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "ops" / "ipfs_restore_drill.py"
SPEC = importlib.util.spec_from_file_location("ipfs_restore_drill", MODULE_PATH)
ipfs_restore_drill = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(ipfs_restore_drill)

SIGNING_KEY_HEX = "22" * 32
SIGNING_PUBLIC_HEX = ipfs_restore_drill.ipfs_segment_trust.public_key_hex(
    ipfs_restore_drill.ipfs_segment_trust.load_private_key(SIGNING_KEY_HEX)
)
SIGNING_ENV = {
    "BDAG_IPFS_SEGMENT_WRITER_ID": "writer-a",
    "BDAG_IPFS_SEGMENT_SIGNING_KEY_HEX": SIGNING_KEY_HEX,
    "BDAG_IPFS_SEGMENT_TRUSTED_SIGNERS": f"writer-a={SIGNING_PUBLIC_HEX}",
    "BDAG_IPFS_RESTORE_REQUIRE_SIGNATURES": "1",
}


def canonical(payload: Any) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def block(order: int) -> dict[str, Any]:
    raw_hex = f"0bad{order:04x}"
    return {
        "order": order,
        "hash": f"0x{order:064x}",
        "header": {"timestamp": 1_700_000_000 + order},
        "raw_block_hex": raw_hex,
        "raw_block_sha256": hashlib.sha256(raw_hex.encode("ascii")).hexdigest(),
    }


def segment_fixture(segment_id: int, start: int, end: int, previous_manifest_cid: str | None) -> tuple[dict[str, Any], dict[str, bytes]]:
    payload_cid = f"baf-payload-{segment_id}"
    manifest_cid = f"baf-manifest-{segment_id}"
    blocks = [block(order) for order in range(start, end + 1)]
    payload = {
        "document_type": "bdag_chain_order_segment_payload_v1",
        "network": "mainnet",
        "segment_id": segment_id,
        "start_order": start,
        "end_order": end,
        "block_count": len(blocks),
        "build_algorithm": "getBlockByOrder_verbose_header_plus_raw_block_hex_v1",
        "blocks": blocks,
    }
    payload_raw = canonical(payload)
    manifest = {
        "document_type": "bdag_ipfs_segment_manifest_v1",
        "network": "mainnet",
        "generated_at": "2026-06-09T00:00:00+0200",
        "segment_id": segment_id,
        "start_order": start,
        "end_order": end,
        "block_count": len(blocks),
        "start_hash": blocks[0]["hash"],
        "end_hash": blocks[-1]["hash"],
        "start_timestamp": blocks[0]["header"]["timestamp"],
        "end_timestamp": blocks[-1]["header"]["timestamp"],
        "previous_segment_manifest_cid": previous_manifest_cid,
        "base_anchor_order": start - 1,
        "base_anchor_hash": None,
        "payload_cid": payload_cid,
        "payload_sha256": sha256(payload_raw),
        "payload_size_bytes": len(payload_raw),
        "payload_format": "bdag_chain_order_segment_payload_v1",
        "source": {"rpc_source": "unit", "rpc_method": "getBlockByOrder"},
        "writer": {"mode": "local_writer", "kubo_peer_id": "peer", "writer_id": "writer-a", "ipns_name": ""},
        "election": {"phase": "local_writer", "rule": "unit", "fallback": "unit"},
        "trust_model": "unit",
    }
    manifest = ipfs_restore_drill.ipfs_segment_trust.sign_payload(
        manifest,
        SIGNING_ENV,
        signature_field="manifest_signatures",
    )
    manifest_raw = canonical(manifest)
    record = {
        "segment_id": segment_id,
        "start_order": start,
        "end_order": end,
        "block_count": len(blocks),
        "start_hash": blocks[0]["hash"],
        "end_hash": blocks[-1]["hash"],
        "payload_cid": payload_cid,
        "payload_sha256": sha256(payload_raw),
        "payload_size_bytes": len(payload_raw),
        "manifest_cid": manifest_cid,
        "manifest_sha256": sha256(manifest_raw),
    }
    return record, {payload_cid: payload_raw, manifest_cid: manifest_raw}


class IPFSRestoreDrillTest(unittest.TestCase):
    def signing_env(self, base: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {
            **SIGNING_ENV,
            "BDAG_IPFS_RESTORE_ACCEPTED_HEAD_STATE_FILE": str(base / "accepted-head.json"),
        }
        if extra:
            env.update(extra)
        return env

    def write_fixtures(self, cid_dir: Path, fixtures: dict[str, bytes]) -> None:
        cid_dir.mkdir(parents=True, exist_ok=True)
        for cid, raw in fixtures.items():
            (cid_dir / f"{cid}.json").write_bytes(raw)

    def build_index(
        self,
        records: list[dict[str, Any]],
        *,
        previous_index_cid: str = "",
        previous_index: dict[str, Any] | None = None,
        previous_head_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        index = {
            "document_type": "bdag_ipfs_segment_index_v1",
            "network": "mainnet",
            "status": "active_single_writer_segments",
            "segments": records,
            "current_head": {
                "segment_id": records[-1]["segment_id"],
                "start_order": records[-1]["start_order"],
                "end_order": records[-1]["end_order"],
                "end_hash": records[-1]["end_hash"],
                "manifest_cid": records[-1]["manifest_cid"],
                "payload_cid": records[-1]["payload_cid"],
            },
            "history_completeness": {
                "complete_from_order": records[0]["start_order"],
                "backfill_required_before_order": None,
            },
        }
        if previous_index_cid:
            previous_head = (
                previous_head_override
                if previous_head_override is not None
                else dict((previous_index or {}).get("current_head") or {})
            )
            index["previous_index_cid"] = previous_index_cid
            index["previous_index_link"] = {
                "document_type": "bdag_ipfs_segment_previous_index_link_v1",
                "index_cid": previous_index_cid,
                "linked_at": "2026-06-09T00:01:00+0200",
                "reason": "segment_append",
                "previous_current_head": previous_head,
            }
            index["append_only_index_policy"] = {
                "immutable_index_cids": True,
                "latest_pointer_is_mutable_discovery_only": True,
            }
        index = ipfs_restore_drill.ipfs_segment_trust.sign_payload(
            index,
            SIGNING_ENV,
            signature_field="index_signatures",
        )
        return index

    def write_index(self, base: Path, records: list[dict[str, Any]]) -> Path:
        index = self.build_index(records)
        path = base / "latest-index.json"
        path.write_text(json.dumps(index), encoding="utf-8")
        return path

    def test_verify_local_index_and_fixture_cids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            record1, fixture1 = segment_fixture(1, 1, 2, None)
            record2, fixture2 = segment_fixture(2, 3, 4, record1["manifest_cid"])
            cid_dir = base / "cid-fixtures"
            self.write_fixtures(cid_dir, {**fixture1, **fixture2})
            index = self.write_index(base, [record1, record2])
            status = base / "status.json"

            with mock.patch.dict(os.environ, self.signing_env(base), clear=False):
                rc = ipfs_restore_drill.main(
                    [
                        "--index",
                        str(index),
                        "--cid-dir",
                        str(cid_dir),
                        "--status-file",
                        str(status),
                        "--max-segments",
                        "0",
                    ]
                )
            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertEqual(payload["state"], "verified")
        self.assertEqual(payload["segments_verified"], 2)
        self.assertEqual(payload["first_verified_order"], 1)
        self.assertEqual(payload["last_verified_order"], 4)
        self.assertEqual(payload["verified_segments"][0]["writer_authority"]["state"], "not_enforced_no_roster")
        self.assertTrue(payload["index_lineage_verified"])
        self.assertEqual(payload["index_lineage_depth"], 0)
        self.assertFalse(payload["usable_for_destructive_restore"])

    def test_enforces_roster_elected_writer_for_manifest_signature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            record, fixtures = segment_fixture(1, 1, 2, None)
            cid_dir = base / "cid-fixtures"
            self.write_fixtures(cid_dir, fixtures)
            index = self.write_index(base, [record])
            status = base / "status.json"

            env = self.signing_env(
                base,
                {
                    "BDAG_IPFS_SEGMENT_WRITER_ROSTER": "writer-a",
                    "BDAG_IPFS_SEGMENT_WRITER_ELECTION_RULE": "rendezvous_sha256_v1",
                },
            )
            with mock.patch.dict(os.environ, env, clear=False):
                rc = ipfs_restore_drill.main(
                    [
                        "--index",
                        str(index),
                        "--cid-dir",
                        str(cid_dir),
                        "--status-file",
                        str(status),
                    ]
                )
            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertEqual(payload["verified_segments"][0]["writer_authority"]["state"], "enforced")
        self.assertEqual(payload["verified_segments"][0]["writer_authority"]["selected_writer_id"], "writer-a")

    def test_rejects_trusted_but_non_elected_manifest_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            record, fixtures = segment_fixture(1, 1, 2, None)
            cid_dir = base / "cid-fixtures"
            self.write_fixtures(cid_dir, fixtures)
            index = self.write_index(base, [record])
            status = base / "status.json"

            env = self.signing_env(
                base,
                {
                    "BDAG_IPFS_SEGMENT_WRITER_ROSTER": "writer-b",
                    "BDAG_IPFS_SEGMENT_WRITER_ELECTION_RULE": "rendezvous_sha256_v1",
                },
            )
            with mock.patch.dict(os.environ, env, clear=False):
                rc = ipfs_restore_drill.main(
                    [
                        "--index",
                        str(index),
                        "--cid-dir",
                        str(cid_dir),
                        "--status-file",
                        str(status),
                    ]
                )
            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 1)
        self.assertEqual(payload["state"], "failed")
        self.assertTrue(any("not signed by the elected writer" in reason for reason in payload["reasons"]))

    def test_verifies_recursive_previous_index_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            record1, fixture1 = segment_fixture(1, 1, 2, None)
            record2, fixture2 = segment_fixture(2, 3, 4, record1["manifest_cid"])
            previous_index = self.build_index([record1])
            previous_index_cid = "baf-index-previous"
            current_index = self.build_index(
                [record1, record2],
                previous_index_cid=previous_index_cid,
                previous_index=previous_index,
            )
            cid_dir = base / "cid-fixtures"
            self.write_fixtures(
                cid_dir,
                {
                    **fixture1,
                    **fixture2,
                    previous_index_cid: canonical(previous_index),
                },
            )
            index = base / "latest-index.json"
            index.write_text(json.dumps(current_index), encoding="utf-8")
            status = base / "status.json"

            with mock.patch.dict(os.environ, self.signing_env(base), clear=False):
                rc = ipfs_restore_drill.main(
                    [
                        "--index",
                        str(index),
                        "--cid-dir",
                        str(cid_dir),
                        "--status-file",
                        str(status),
                        "--max-segments",
                        "0",
                    ]
                )
            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertTrue(payload["index_lineage_verified"])
        self.assertEqual(payload["index_lineage_depth"], 1)
        self.assertEqual(payload["index_lineage_links"][0]["previous_index_cid"], previous_index_cid)

    def test_successful_ipfs_drill_records_accepted_head_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            record, fixtures = segment_fixture(1, 1, 2, None)
            index = self.build_index([record])
            index_cid = "baf-index-current"
            cid_dir = base / "cid-fixtures"
            self.write_fixtures(cid_dir, {**fixtures, index_cid: canonical(index)})
            status = base / "status.json"
            accepted = base / "accepted.json"

            env = self.signing_env(base, {"BDAG_IPFS_RESTORE_ACCEPTED_HEAD_STATE_FILE": str(accepted)})
            with mock.patch.dict(os.environ, env, clear=False):
                rc = ipfs_restore_drill.main(
                    [
                        "--index-cid",
                        index_cid,
                        "--cid-dir",
                        str(cid_dir),
                        "--status-file",
                        str(status),
                    ]
                )
            payload = json.loads(status.read_text(encoding="utf-8"))
            state = json.loads(accepted.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertTrue(payload["accepted_head"]["enforced"])
        self.assertTrue(payload["accepted_head"]["updated"])
        self.assertEqual(state["document_type"], "bdag_ipfs_restore_accepted_head_v1")
        self.assertEqual(state["current_head_end_order"], 2)
        self.assertEqual(state["current_index_cid"], index_cid)
        self.assertEqual(state["restore_policy"], "anti_rollback_state_only_no_chain_datadir_mutation")

    def test_rejects_index_rollback_below_accepted_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            record, fixtures = segment_fixture(1, 1, 2, None)
            index = self.build_index([record])
            index_cid = "baf-index-old"
            cid_dir = base / "cid-fixtures"
            self.write_fixtures(cid_dir, {**fixtures, index_cid: canonical(index)})
            status = base / "status.json"
            accepted = base / "accepted.json"
            accepted.write_text(
                json.dumps(
                    {
                        "document_type": "bdag_ipfs_restore_accepted_head_v1",
                        "network": "mainnet",
                        "current_head_end_order": 4,
                        "current_index_cid": "baf-index-later",
                    }
                ),
                encoding="utf-8",
            )

            env = self.signing_env(base, {"BDAG_IPFS_RESTORE_ACCEPTED_HEAD_STATE_FILE": str(accepted)})
            with mock.patch.dict(os.environ, env, clear=False):
                rc = ipfs_restore_drill.main(
                    [
                        "--index-cid",
                        index_cid,
                        "--cid-dir",
                        str(cid_dir),
                        "--status-file",
                        str(status),
                    ]
                )
            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 1)
        self.assertEqual(payload["state"], "failed")
        self.assertTrue(any("rejected index rollback" in reason for reason in payload["reasons"]))

    def test_rejects_newer_ipfs_index_that_does_not_link_to_accepted_cid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            record1, fixture1 = segment_fixture(1, 1, 2, None)
            record2, fixture2 = segment_fixture(2, 3, 4, record1["manifest_cid"])
            index = self.build_index([record1, record2])
            index_cid = "baf-index-current"
            cid_dir = base / "cid-fixtures"
            self.write_fixtures(cid_dir, {**fixture1, **fixture2, index_cid: canonical(index)})
            status = base / "status.json"
            accepted = base / "accepted.json"
            accepted.write_text(
                json.dumps(
                    {
                        "document_type": "bdag_ipfs_restore_accepted_head_v1",
                        "network": "mainnet",
                        "current_head_end_order": 2,
                        "current_index_cid": "baf-index-accepted",
                    }
                ),
                encoding="utf-8",
            )

            env = self.signing_env(base, {"BDAG_IPFS_RESTORE_ACCEPTED_HEAD_STATE_FILE": str(accepted)})
            with mock.patch.dict(os.environ, env, clear=False):
                rc = ipfs_restore_drill.main(
                    [
                        "--index-cid",
                        index_cid,
                        "--cid-dir",
                        str(cid_dir),
                        "--status-file",
                        str(status),
                        "--max-segments",
                        "0",
                    ]
                )
            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 1)
        self.assertEqual(payload["state"], "failed")
        self.assertTrue(any("rejected non-lineage index" in reason for reason in payload["reasons"]))

    def test_accepts_newer_ipfs_index_that_links_to_accepted_cid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            record1, fixture1 = segment_fixture(1, 1, 2, None)
            record2, fixture2 = segment_fixture(2, 3, 4, record1["manifest_cid"])
            accepted_index = self.build_index([record1])
            accepted_cid = "baf-index-accepted"
            current_index = self.build_index(
                [record1, record2],
                previous_index_cid=accepted_cid,
                previous_index=accepted_index,
            )
            current_cid = "baf-index-current"
            cid_dir = base / "cid-fixtures"
            self.write_fixtures(
                cid_dir,
                {
                    **fixture1,
                    **fixture2,
                    accepted_cid: canonical(accepted_index),
                    current_cid: canonical(current_index),
                },
            )
            status = base / "status.json"
            accepted = base / "accepted.json"
            accepted.write_text(
                json.dumps(
                    {
                        "document_type": "bdag_ipfs_restore_accepted_head_v1",
                        "network": "mainnet",
                        "current_head_end_order": 2,
                        "current_index_cid": accepted_cid,
                    }
                ),
                encoding="utf-8",
            )

            env = self.signing_env(base, {"BDAG_IPFS_RESTORE_ACCEPTED_HEAD_STATE_FILE": str(accepted)})
            with mock.patch.dict(os.environ, env, clear=False):
                rc = ipfs_restore_drill.main(
                    [
                        "--index-cid",
                        current_cid,
                        "--cid-dir",
                        str(cid_dir),
                        "--status-file",
                        str(status),
                        "--max-segments",
                        "0",
                    ]
                )
            payload = json.loads(status.read_text(encoding="utf-8"))
            state = json.loads(accepted.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertTrue(payload["accepted_head"]["lineage_cid_enforced"])
        self.assertEqual(state["current_head_end_order"], 4)
        self.assertEqual(state["current_index_cid"], current_cid)

    def test_chain_anchor_marks_archive_trusted_when_live_reference_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            record, fixtures = segment_fixture(1, 1, 2, None)
            cid_dir = base / "cid-fixtures"
            self.write_fixtures(cid_dir, fixtures)
            index = self.write_index(base, [record])
            status = base / "status.json"
            env = self.signing_env(
                base,
                {
                    "BDAG_IPFS_RESTORE_CHAIN_SOURCE_RPC_URL": "http://source:38131",
                    "BDAG_IPFS_RESTORE_CHAIN_REFERENCE_RPC_URL": "http://reference:38131",
                },
            )

            trusted_anchor = {
                "state": "trusted",
                "trusted": True,
                "reasons": [],
                "source_url": "http://source:38131",
                "reference_url": "http://reference:38131",
            }
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                ipfs_restore_drill.chain_integrity_gate,
                "evaluate_chain_integrity",
                return_value=trusted_anchor,
            ) as evaluate:
                rc = ipfs_restore_drill.main(
                    [
                        "--index",
                        str(index),
                        "--cid-dir",
                        str(cid_dir),
                        "--status-file",
                        str(status),
                    ]
                )
            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertTrue(payload["chain_anchor_trusted"])
        self.assertTrue(payload["archive_trusted_for_chain_reference"])
        self.assertEqual(payload["chain_anchor"]["state"], "trusted")
        config = evaluate.call_args.args[0]
        self.assertEqual(config["workflow"], "ipfs_restore_drill")
        self.assertEqual(config["source_rpc_url"], "http://source:38131")
        self.assertEqual(config["reference_rpc_url"], "http://reference:38131")
        self.assertEqual(config["start_order"], 1)
        self.assertEqual(config["end_order"], 2)

    def test_required_chain_anchor_failure_blocks_restore_drill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            record, fixtures = segment_fixture(1, 1, 2, None)
            cid_dir = base / "cid-fixtures"
            self.write_fixtures(cid_dir, fixtures)
            index = self.write_index(base, [record])
            status = base / "status.json"
            env = self.signing_env(
                base,
                {
                    "BDAG_IPFS_RESTORE_CHAIN_SOURCE_RPC_URL": "http://source:38131",
                    "BDAG_IPFS_RESTORE_CHAIN_REFERENCE_RPC_URL": "http://reference:38131",
                    "BDAG_IPFS_RESTORE_REQUIRE_CHAIN_ANCHOR": "1",
                },
            )

            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                ipfs_restore_drill.chain_integrity_gate,
                "evaluate_chain_integrity",
                return_value={"state": "rejected_mismatch", "trusted": False, "reasons": ["order_2_hash_mismatch"]},
            ):
                rc = ipfs_restore_drill.main(
                    [
                        "--index",
                        str(index),
                        "--cid-dir",
                        str(cid_dir),
                        "--status-file",
                        str(status),
                    ]
                )
            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 1)
        self.assertEqual(payload["state"], "failed")
        self.assertTrue(any("chain anchor is not trusted" in reason for reason in payload["reasons"]))

    def test_rejects_previous_index_lineage_head_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            record1, fixture1 = segment_fixture(1, 1, 2, None)
            record2, fixture2 = segment_fixture(2, 3, 4, record1["manifest_cid"])
            previous_index = self.build_index([record1])
            previous_index_cid = "baf-index-previous"
            current_index = self.build_index(
                [record1, record2],
                previous_index_cid=previous_index_cid,
                previous_index=previous_index,
                previous_head_override={"end_order": 999, "manifest_cid": "wrong"},
            )
            cid_dir = base / "cid-fixtures"
            self.write_fixtures(
                cid_dir,
                {
                    **fixture1,
                    **fixture2,
                    previous_index_cid: canonical(previous_index),
                },
            )
            index = base / "latest-index.json"
            index.write_text(json.dumps(current_index), encoding="utf-8")
            status = base / "status.json"

            with mock.patch.dict(os.environ, self.signing_env(base), clear=False):
                rc = ipfs_restore_drill.main(
                    [
                        "--index",
                        str(index),
                        "--cid-dir",
                        str(cid_dir),
                        "--status-file",
                        str(status),
                    ]
                )
            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 1)
        self.assertEqual(payload["state"], "failed")
        self.assertTrue(any("previous_current_head does not match" in reason for reason in payload["reasons"]))

    def test_rejects_non_contiguous_index_before_fetching_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            record1, fixture1 = segment_fixture(1, 1, 2, None)
            record2, fixture2 = segment_fixture(2, 4, 5, record1["manifest_cid"])
            cid_dir = base / "cid-fixtures"
            self.write_fixtures(cid_dir, {**fixture1, **fixture2})
            index = self.write_index(base, [record1, record2])
            status = base / "status.json"

            with mock.patch.dict(os.environ, self.signing_env(base), clear=False):
                rc = ipfs_restore_drill.main(
                    [
                        "--index",
                        str(index),
                        "--cid-dir",
                        str(cid_dir),
                        "--status-file",
                        str(status),
                    ]
                )
            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 1)
        self.assertEqual(payload["state"], "failed")
        self.assertTrue(any("not contiguous" in reason for reason in payload["reasons"]))

    def test_rejects_tampered_payload_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            record, fixtures = segment_fixture(1, 1, 2, None)
            payload = json.loads(fixtures[record["payload_cid"]].decode("utf-8"))
            payload["blocks"][0]["raw_block_hex"] = "ffff"
            fixtures[record["payload_cid"]] = canonical(payload)
            cid_dir = base / "cid-fixtures"
            self.write_fixtures(cid_dir, fixtures)
            index = self.write_index(base, [record])
            status = base / "status.json"

            with mock.patch.dict(os.environ, self.signing_env(base), clear=False):
                rc = ipfs_restore_drill.main(
                    [
                        "--index",
                        str(index),
                        "--cid-dir",
                        str(cid_dir),
                        "--status-file",
                        str(status),
                    ]
                )
            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 1)
        self.assertEqual(payload["state"], "failed")
        self.assertTrue(any("payload sha256 mismatch" in reason for reason in payload["reasons"]))


if __name__ == "__main__":
    unittest.main()

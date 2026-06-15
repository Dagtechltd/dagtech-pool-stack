import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "ops" / "ipfs_segment_writer.py"
SPEC = importlib.util.spec_from_file_location("ipfs_segment_writer", MODULE_PATH)
ipfs_segment_writer = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(ipfs_segment_writer)

SIGNING_KEY_HEX = "11" * 32
SIGNING_PUBLIC_HEX = ipfs_segment_writer.ipfs_segment_trust.public_key_hex(
    ipfs_segment_writer.ipfs_segment_trust.load_private_key(SIGNING_KEY_HEX)
)
SIGNING_ENV = {
    "BDAG_NETWORK": "mainnet",
    "BDAG_IPFS_SEGMENT_WRITER_ID": "writer-a",
    "BDAG_IPFS_SEGMENT_SIGNING_KEY_HEX": SIGNING_KEY_HEX,
    "BDAG_IPFS_SEGMENT_TRUSTED_SIGNERS": f"writer-a={SIGNING_PUBLIC_HEX}",
}


class IPFSSegmentWriterTest(unittest.TestCase):
    def test_choose_next_range_starts_live_tail_by_default(self) -> None:
        env = {
            "BDAG_IPFS_SEGMENT_FINALITY_LAG_ORDERS": "600",
            "BDAG_IPFS_SEGMENT_ORDERS_PER_SEGMENT": "300",
        }

        start, end, safe_tip, reason = ipfs_segment_writer.choose_next_range({}, 10_000, env)

        self.assertEqual(safe_tip, 9_400)
        self.assertEqual(start, 9_101)
        self.assertEqual(end, 9_400)
        self.assertEqual(reason, "live_tail_start")

    def test_choose_next_range_appends_after_current_head(self) -> None:
        index = {"current_head": {"end_order": 1234}}
        env = {
            "BDAG_IPFS_SEGMENT_FINALITY_LAG_ORDERS": "100",
            "BDAG_IPFS_SEGMENT_ORDERS_PER_SEGMENT": "50",
            "BDAG_IPFS_SEGMENT_STALE_HEAD_RESET_ENABLED": "0",
        }

        start, end, safe_tip, reason = ipfs_segment_writer.choose_next_range(index, 1_400, env)

        self.assertEqual(safe_tip, 1_300)
        self.assertEqual(start, 1_235)
        self.assertEqual(end, 1_284)
        self.assertEqual(reason, "append_after_current_head")

    def test_choose_next_range_resets_stale_live_tail_head(self) -> None:
        index = {"current_head": {"end_order": 9_502_504, "segment_id": 1, "manifest_cid": "baf-old"}}
        env = {
            "BDAG_IPFS_SEGMENT_FINALITY_LAG_ORDERS": "600",
            "BDAG_IPFS_SEGMENT_ORDERS_PER_SEGMENT": "300",
            "BDAG_IPFS_SEGMENT_STALE_HEAD_RESET_ENABLED": "1",
            "BDAG_IPFS_SEGMENT_STALE_HEAD_MAX_LAG_ORDERS": "3600",
        }

        start, end, safe_tip, reason = ipfs_segment_writer.choose_next_range(index, 10_400_000, env)

        self.assertEqual(safe_tip, 10_399_400)
        self.assertEqual(start, 10_399_101)
        self.assertEqual(end, 10_399_400)
        self.assertEqual(reason, "stale_head_live_tail_reset")

    def test_choose_next_range_can_start_after_deprecated_tip(self) -> None:
        index = {"deprecated_content": [{"tip_order": 8_639_851}]}
        env = {
            "BDAG_IPFS_SEGMENT_START_POLICY": "after_deprecated",
            "BDAG_IPFS_SEGMENT_FINALITY_LAG_ORDERS": "600",
            "BDAG_IPFS_SEGMENT_ORDERS_PER_SEGMENT": "300",
        }

        start, end, _, reason = ipfs_segment_writer.choose_next_range(index, 9_500_000, env)

        self.assertEqual(start, 8_639_852)
        self.assertEqual(end, 8_640_151)
        self.assertEqual(reason, "after_deprecated_tip")

    def test_update_index_marks_incomplete_live_tail_history(self) -> None:
        record = {
            "segment_id": 1,
            "start_order": 100,
            "end_order": 399,
            "end_hash": "0xend",
            "manifest_cid": "baf-manifest",
            "payload_cid": "baf-payload",
        }

        index = ipfs_segment_writer.update_index({}, record, {"BDAG_NETWORK": "mainnet"})

        self.assertEqual(index["current_head"]["end_order"], 399)
        self.assertEqual(index["history_completeness"]["complete_from_order"], 100)
        self.assertEqual(index["history_completeness"]["backfill_required_before_order"], 100)
        self.assertEqual(len(index["segments"]), 1)
        self.assertNotIn("signatures", index)
        self.assertNotIn("index_root", index)

    def test_reset_index_for_live_tail_epoch_preserves_stale_head_as_deprecated(self) -> None:
        old_index = {
            "current_head": {"segment_id": 7, "end_order": 9_502_504, "manifest_cid": "baf-old"},
            "segments": [{"segment_id": 7, "start_order": 9_502_205, "end_order": 9_502_504}],
            "history_completeness": {"complete_from_order": 9_502_205},
            "recovered_from_discovery_cid": "baf-old-index",
        }

        index = ipfs_segment_writer.reset_index_for_live_tail_epoch(
            old_index,
            10_399_101,
            10_399_400,
            10_400_000,
            10_399_400,
            {"BDAG_NETWORK": "mainnet"},
        )

        self.assertEqual(index["segments"], [])
        self.assertEqual(index["history_completeness"]["complete_from_order"], 10_399_101)
        self.assertEqual(index["deprecated_content"][0]["type"], "superseded_stale_live_tail_epoch")
        self.assertEqual(index["deprecated_content"][0]["previous_index_cid"], "baf-old-index")

    def test_update_index_rejects_non_mainnet_network(self) -> None:
        record = {
            "segment_id": 1,
            "start_order": 100,
            "end_order": 399,
            "end_hash": "0xend",
            "manifest_cid": "baf-manifest",
            "payload_cid": "baf-payload",
        }

        with self.assertRaisesRegex(RuntimeError, "refuses non-mainnet"):
            ipfs_segment_writer.update_index({}, record, {"BDAG_NETWORK": "not-mainnet"})

    def test_chain_reference_rpc_url_falls_back_to_public_rpc_list(self) -> None:
        env = {
            "BDAG_PUBLIC_RPC_URLS": "local=http://source:38131,engineering=https://rpc.blockdag.engineering",
        }

        url = ipfs_segment_writer.chain_reference_rpc_url(env, "http://source:38131")

        self.assertEqual("https://rpc.blockdag.engineering", url)

    def test_chain_reference_rpc_candidates_keep_all_public_fallbacks(self) -> None:
        env = {
            "BDAG_PUBLIC_RPC_URLS": "bad=https://bad.example,local=http://source:38131,good=https://good.example",
        }

        urls = ipfs_segment_writer.chain_reference_rpc_candidates(env, "http://source:38131")

        self.assertEqual(["https://bad.example", "https://good.example"], urls)

    def test_chain_reference_rpc_url_prefers_explicit_value(self) -> None:
        env = {
            "BDAG_CHAIN_REFERENCE_RPC_URL": "http://reference:38131",
            "BDAG_PUBLIC_RPC_URLS": "engineering=https://rpc.blockdag.engineering",
        }

        url = ipfs_segment_writer.chain_reference_rpc_url(env, "http://source:38131")

        self.assertEqual("http://reference:38131", url)

    def test_update_index_rejects_non_contiguous_append(self) -> None:
        index = {
            "segments": [
                {
                    "segment_id": 1,
                    "start_order": 1,
                    "end_order": 10,
                    "manifest_cid": "baf-manifest-1",
                    "payload_cid": "baf-payload-1",
                }
            ],
            "current_head": {"segment_id": 1, "end_order": 10},
        }
        record = {
            "segment_id": 2,
            "start_order": 12,
            "end_order": 20,
            "end_hash": "0xend",
            "manifest_cid": "baf-manifest-2",
            "payload_cid": "baf-payload-2",
        }

        with self.assertRaisesRegex(RuntimeError, "non-contiguous"):
            ipfs_segment_writer.update_index(index, record, {"BDAG_NETWORK": "mainnet"})

    def test_index_from_discovery_recovers_current_head_summary(self) -> None:
        discovery = {
            "current_latest_index_cid": "baf-index",
            "current_content": {
                "document_type": "bdag_ipfs_segment_index_v1",
                "status": "active_deterministic_writer_segments",
                "current_head": {"segment_id": 3, "end_order": 300, "manifest_cid": "baf-manifest"},
                "history_completeness": {"complete_from_order": 100},
            },
        }

        index = ipfs_segment_writer.index_from_discovery(discovery, {"BDAG_NETWORK": "mainnet"})

        self.assertTrue(index["recovered_from_discovery"])
        self.assertEqual(index["current_head"]["end_order"], 300)
        self.assertEqual(index["history_completeness"]["complete_from_order"], 100)

    def test_load_index_recovers_previous_index_from_discovery_cid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            index_path = base / "latest-index.json"
            index_path.write_text("{}", encoding="utf-8")
            discovery = base / "discovery.json"
            discovery.write_text(json.dumps({"current_latest_index_cid": "baf-index"}), encoding="utf-8")
            previous_index = {
                "document_type": "bdag_ipfs_segment_index_v1",
                "current_head": {"segment_id": 4, "end_order": 400, "manifest_cid": "baf-manifest"},
            }
            env = {"BDAG_NETWORK": "mainnet", "BDAG_IPFS_CONTENT_DISCOVERY_FILE": str(discovery)}

            with mock.patch.object(ipfs_segment_writer, "ipfs_cat_json", return_value=previous_index):
                index = ipfs_segment_writer.load_index_with_discovery(index_path, env)

        self.assertEqual(index["current_head"]["end_order"], 400)
        self.assertEqual(index["recovered_from_discovery_cid"], "baf-index")

    def test_attach_previous_index_link_records_append_only_lineage(self) -> None:
        current = {"document_type": "bdag_ipfs_segment_index_v1", "segments": []}
        previous = {
            "current_head": {"segment_id": 2, "end_order": 9502504, "manifest_cid": "baf-manifest"},
            "history_completeness": {"complete_from_order": 9502145},
        }

        linked = ipfs_segment_writer.attach_previous_index_link(
            current,
            "baf-previous-index",
            previous,
            "stale_head_live_tail_reset",
        )

        self.assertEqual(linked["previous_index_cid"], "baf-previous-index")
        self.assertEqual(linked["previous_index_link"]["previous_current_head"]["end_order"], 9502504)
        self.assertTrue(linked["append_only_index_policy"]["immutable_index_cids"])
        self.assertTrue(linked["append_only_index_policy"]["latest_pointer_is_mutable_discovery_only"])

    def test_attach_previous_index_link_declares_policy_without_previous_cid(self) -> None:
        current = {"document_type": "bdag_ipfs_segment_index_v1", "segments": []}

        linked = ipfs_segment_writer.attach_previous_index_link(current, "", {}, "segment_append")

        self.assertNotIn("previous_index_cid", linked)
        self.assertTrue(linked["append_only_index_policy"]["immutable_index_cids"])
        self.assertTrue(linked["append_only_index_policy"]["latest_pointer_is_mutable_discovery_only"])

    def test_update_discovery_records_previous_index_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            discovery = Path(tmp) / "discovery.json"
            discovery.write_text(
                json.dumps(
                    {
                        "document_type": "bdag_ipfs_content_discovery_v1",
                        "current_latest_index_cid": "baf-old-index",
                    }
                ),
                encoding="utf-8",
            )
            index = {
                "status": "active_deterministic_writer_segments",
                "segments": [{"segment_id": 1}],
                "current_head": {"segment_id": 1, "end_order": 10473200},
                "history_completeness": {"complete_from_order": 10472901},
                "previous_index_cid": "baf-old-index",
                "append_only_index_policy": {"immutable_index_cids": True},
            }

            ipfs_segment_writer.update_discovery(
                "baf-new-index",
                index,
                {"BDAG_IPFS_CONTENT_DISCOVERY_FILE": str(discovery)},
            )

            payload = json.loads(discovery.read_text(encoding="utf-8"))

        self.assertEqual(payload["previous_latest_index_cid"], "baf-old-index")
        self.assertEqual(payload["current_latest_index_cid"], "baf-new-index")
        self.assertEqual(payload["current_content"]["previous_index_cid"], "baf-old-index")
        self.assertTrue(payload["current_content"]["append_only_index_policy"]["immutable_index_cids"])

    def test_build_segment_publishes_signed_manifest_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            captured: dict[str, dict] = {}

            def add_side_effect(_path, payload, _env):
                captured[payload["document_type"]] = json.loads(json.dumps(payload))
                if payload["document_type"] == "bdag_chain_order_segment_payload_v1":
                    return ("baf-payload", "payload-sha", 100)
                return ("baf-manifest", "manifest-sha", 200)

            blocks = [
                {
                    "order": 1,
                    "hash": "0x1",
                    "header": {"timestamp": 123},
                    "raw_block_hex": "abcd",
                    "raw_block_sha256": "raw-sha",
                }
            ]

            with mock.patch.object(ipfs_segment_writer, "fetch_segment_blocks", return_value=blocks), mock.patch.object(
                ipfs_segment_writer,
                "segment_dir",
                return_value=Path(tmp),
            ), mock.patch.object(
                ipfs_segment_writer,
                "add_checked_json",
                side_effect=add_side_effect,
            ), mock.patch.object(
                ipfs_segment_writer,
                "ipfs_peer_id",
                return_value="peer",
            ):
                record = ipfs_segment_writer.build_segment(
                    mock.Mock(),
                    "unit",
                    "http://source:38131",
                    1,
                    1,
                    {},
                    SIGNING_ENV,
                )

        manifest = captured["bdag_ipfs_segment_manifest_v1"]
        self.assertNotIn("manifest_cid", manifest)
        self.assertEqual(manifest["signature_status"], "signed")
        self.assertEqual(manifest["manifest_signatures"][0]["writer_id"], "writer-a")
        self.assertEqual(manifest["manifest_signatures"][0]["public_key_hex"], SIGNING_PUBLIC_HEX)
        self.assertEqual(record["manifest_signature_status"], "signed")
        self.assertEqual(record["manifest_signatures"][0]["writer_id"], "writer-a")

    def test_build_segment_requires_signing_key_by_default(self) -> None:
        blocks = [
            {
                "order": 1,
                "hash": "0x1",
                "header": {"timestamp": 123},
                "raw_block_hex": "abcd",
                "raw_block_sha256": "raw-sha",
            }
        ]

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            ipfs_segment_writer,
            "fetch_segment_blocks",
            return_value=blocks,
        ), mock.patch.object(
            ipfs_segment_writer,
            "segment_dir",
            return_value=Path(tmp),
        ), self.assertRaisesRegex(
            RuntimeError,
            "signing is required",
        ):
            ipfs_segment_writer.build_segment(
                mock.Mock(),
                "unit",
                "http://source:38131",
                1,
                1,
                {},
                {"BDAG_NETWORK": "mainnet"},
            )

    def test_canonical_json_bytes_are_stable(self) -> None:
        left = ipfs_segment_writer.canonical_json_bytes({"b": 1, "a": [2, 3]})
        right = ipfs_segment_writer.canonical_json_bytes({"a": [2, 3], "b": 1})

        self.assertEqual(left, right)

    def test_normal_publish_preflight_requirement_has_no_env_disable(self) -> None:
        self.assertTrue(ipfs_segment_writer.normal_publish_requires_preflight({"BDAG_IPFS_SEGMENT_REQUIRE_PREFLIGHT": "0"}))

    def test_writer_election_uses_deterministic_roster(self) -> None:
        roster = "writer-b,writer-a,writer-a"
        probe = ipfs_segment_writer.writer_election(
            {"BDAG_IPFS_SEGMENT_WRITER_ID": "writer-a", "BDAG_IPFS_SEGMENT_WRITER_ROSTER": roster},
            100,
            399,
            "baf-prev",
        )
        selected = probe["selected_writer_id"]
        other = "writer-b" if selected == "writer-a" else "writer-a"

        allowed = ipfs_segment_writer.writer_election(
            {"BDAG_IPFS_SEGMENT_WRITER_ID": selected, "BDAG_IPFS_SEGMENT_WRITER_ROSTER": roster},
            100,
            399,
            "baf-prev",
        )
        denied = ipfs_segment_writer.writer_election(
            {"BDAG_IPFS_SEGMENT_WRITER_ID": other, "BDAG_IPFS_SEGMENT_WRITER_ROSTER": roster},
            100,
            399,
            "baf-prev",
        )

        self.assertTrue(allowed["allowed"])
        self.assertFalse(denied["allowed"])
        self.assertEqual(allowed["roster_size"], 2)

    def test_empty_writer_roster_allows_bootstrap_seed(self) -> None:
        with mock.patch.object(ipfs_segment_writer, "ipfs_peer_id", return_value="peer-local"):
            election = ipfs_segment_writer.writer_election({}, 100, 399)

        self.assertTrue(election["allowed"])
        self.assertEqual(election["mode"], "bootstrap_single_writer")
        self.assertEqual(election["local_writer_id"], "peer-local")

    def test_auto_ipns_publish_waits_for_key(self) -> None:
        with mock.patch.object(ipfs_segment_writer, "run_command", side_effect=AssertionError("must not publish")):
            result = ipfs_segment_writer.publish_ipns("baf-index", {"BDAG_IPFS_SEGMENT_PUBLISH_IPNS": "auto"})

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "auto_publish_waiting_for_ipns_key")

    def test_bootstrap_seed_can_publish_without_reference_rpc(self) -> None:
        election = {
            "allowed": True,
            "mode": "bootstrap_single_writer",
            "roster_size": 0,
        }

        result = ipfs_segment_writer.publication_integrity_gate(
            {
                "BDAG_IPFS_SEGMENT_BOOTSTRAP_LOCAL_PUBLISH": "1",
                "BDAG_IPFS_SEGMENT_BOOTSTRAP_UNTRUSTED_PUBLISH": "1",
            },
            Path("/tmp/index.json"),
            "http://source:38131",
            "",
            100,
            399,
            election,
        )

        self.assertEqual(result["state"], "bootstrap_local_reference_absent")
        self.assertEqual(result["mutation_policy"], "allowed_for_bootstrap_seed_without_roster_only")

    def test_publication_gate_tries_next_public_reference_when_reference_unavailable(self) -> None:
        env = {
            "BDAG_PUBLIC_RPC_URLS": "bad=https://bad.example,good=https://good.example",
        }
        deferred = {"state": "deferred_reference_unavailable", "trusted": False, "reasons": ["http_403"]}
        trusted = {"state": "trusted", "trusted": True, "reasons": []}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            ipfs_segment_writer,
            "run_preflight",
            side_effect=[deferred, trusted],
        ) as run_preflight:
            result = ipfs_segment_writer.publication_integrity_gate(
                env,
                Path(tmp) / "index.json",
                "http://source:38131",
                "",
                1,
                2,
                {},
            )

        self.assertEqual("trusted", result["state"])
        self.assertEqual("https://good.example", result["selected_reference_url"])
        self.assertEqual(["https://bad.example", "https://good.example"], [call.args[3] for call in run_preflight.call_args_list])
        self.assertEqual("deferred_reference_unavailable", result["reference_attempts"][0]["state"])

    def test_publication_gate_does_not_fallback_from_explicit_reference(self) -> None:
        env = {
            "BDAG_PUBLIC_RPC_URLS": "good=https://good.example",
        }
        deferred = {"state": "deferred_reference_unavailable", "trusted": False, "reasons": ["http_403"]}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            ipfs_segment_writer,
            "run_preflight",
            return_value=deferred,
        ) as run_preflight:
            with self.assertRaises(ipfs_segment_writer.RetryableDefer):
                ipfs_segment_writer.publication_integrity_gate(
                    env,
                    Path(tmp) / "index.json",
                    "http://source:38131",
                    "https://explicit.example",
                    1,
                    2,
                    {},
                )

        run_preflight.assert_called_once()

    def test_bootstrap_local_publish_is_fail_closed_by_default(self) -> None:
        election = {
            "allowed": True,
            "mode": "bootstrap_single_writer",
            "roster_size": 0,
        }

        self.assertFalse(ipfs_segment_writer.bootstrap_local_publish_allowed({}, election))
        self.assertFalse(
            ipfs_segment_writer.bootstrap_local_publish_allowed(
                {"BDAG_IPFS_SEGMENT_BOOTSTRAP_LOCAL_PUBLISH": "1"},
                election,
            )
        )
        self.assertTrue(
            ipfs_segment_writer.bootstrap_local_publish_allowed(
                {
                    "BDAG_IPFS_SEGMENT_BOOTSTRAP_LOCAL_PUBLISH": "1",
                    "BDAG_IPFS_SEGMENT_BOOTSTRAP_UNTRUSTED_PUBLISH": "1",
                },
                election,
            )
        )

    def test_preflight_runs_chain_gate_without_ipfs_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            status = base / "status.json"
            index = base / "latest-index.json"
            index.write_text("{}", encoding="utf-8")
            env = {
                **SIGNING_ENV,
                "BDAG_PROJECT_ROOT": str(ROOT),
                "BDAG_IPFS_SEGMENT_STATUS_FILE": str(status),
                "BDAG_IPFS_SEGMENT_SKIP_MAINTENANCE_DECISION": "1",
            }
            trusted = {
                "state": "trusted",
                "trusted": True,
                "source_url": "http://source:38131",
                "reference_url": "http://reference:38131",
                "segment_preflight": {"block_count": 2},
            }

            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                ipfs_segment_writer,
                "run_preflight",
                return_value=trusted,
            ) as run_preflight, mock.patch.object(
                ipfs_segment_writer,
                "ipfs_add",
                side_effect=AssertionError("preflight must not call ipfs add"),
            ), mock.patch.object(
                ipfs_segment_writer,
                "ipfs_pin_present",
                side_effect=AssertionError("preflight must not inspect Kubo pins"),
            ), mock.patch.object(
                ipfs_segment_writer,
                "publish_ipns",
                side_effect=AssertionError("preflight must not publish IPNS"),
            ):
                rc = ipfs_segment_writer.main(
                    [
                        "--preflight",
                        "--source-rpc-url",
                        "http://source:38131",
                        "--reference-rpc-url",
                        "http://reference:38131",
                        "--start-order",
                        "1",
                        "--end-order",
                        "2",
                        "--index",
                        str(index),
                    ]
                )

            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertEqual(payload["state"], "trusted")
        self.assertEqual(payload["action"], "preflight")
        self.assertEqual(payload["mutation_policy"], "no_ipfs_add_pin_cat_ipns_or_index_write_in_preflight")
        run_preflight.assert_called_once()

    def test_preflight_rejected_state_returns_failure_without_ipfs_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            status = base / "status.json"
            index = base / "latest-index.json"
            index.write_text("{}", encoding="utf-8")
            env = {
                **SIGNING_ENV,
                "BDAG_PROJECT_ROOT": str(ROOT),
                "BDAG_IPFS_SEGMENT_STATUS_FILE": str(status),
                "BDAG_IPFS_SEGMENT_SKIP_MAINTENANCE_DECISION": "1",
            }
            rejected = {
                "state": "rejected_mismatch",
                "trusted": False,
                "reasons": ["order_2_hash"],
                "source_url": "http://source:38131",
                "reference_url": "http://reference:38131",
            }

            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                ipfs_segment_writer,
                "run_preflight",
                return_value=rejected,
            ), mock.patch.object(
                ipfs_segment_writer,
                "ipfs_add",
                side_effect=AssertionError("preflight must not call ipfs add"),
            ), mock.patch.object(
                ipfs_segment_writer,
                "publish_ipns",
                side_effect=AssertionError("preflight must not publish IPNS"),
            ):
                rc = ipfs_segment_writer.main(
                    [
                        "--preflight",
                        "--source-rpc-url",
                        "http://source:38131",
                        "--reference-rpc-url",
                        "http://reference:38131",
                        "--start-order",
                        "1",
                        "--end-order",
                        "2",
                        "--index",
                        str(index),
                    ]
                )

            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 1)
        self.assertEqual(payload["state"], "rejected_mismatch")
        self.assertEqual(payload["action"], "preflight")

    def test_normal_publish_requires_trusted_preflight_before_ipfs_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            status = base / "status.json"
            index = base / "latest-index.json"
            index.write_text("{}", encoding="utf-8")
            env = {
                **SIGNING_ENV,
                "BDAG_PROJECT_ROOT": str(ROOT),
                "BDAG_IPFS_SEGMENT_STATUS_FILE": str(status),
                "BDAG_IPFS_SEGMENT_SKIP_MAINTENANCE_DECISION": "1",
                "BDAG_CHAIN_SOURCE_RPC_URL": "http://source:38131",
                "BDAG_CHAIN_REFERENCE_RPC_URL": "http://reference:38131",
                "BDAG_IPFS_SEGMENT_FINALITY_LAG_ORDERS": "0",
                "BDAG_IPFS_SEGMENT_ORDERS_PER_SEGMENT": "2",
            }
            rejected = {
                "state": "rejected_mismatch",
                "trusted": False,
                "reasons": ["order_2_hash"],
            }

            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                ipfs_segment_writer,
                "import_pool_ops",
            ) as import_pool_ops, mock.patch.object(
                ipfs_segment_writer,
                "run_preflight",
                return_value=rejected,
            ) as run_preflight, mock.patch.object(
                ipfs_segment_writer,
                "build_segment",
                side_effect=AssertionError("normal publish must not build before trusted preflight"),
            ), mock.patch.object(
                ipfs_segment_writer,
                "ipfs_add",
                side_effect=AssertionError("normal publish must not call ipfs add before trusted preflight"),
            ), mock.patch.object(
                ipfs_segment_writer,
                "publish_ipns",
                side_effect=AssertionError("normal publish must not publish IPNS before trusted preflight"),
            ):
                pool_ops = mock.Mock()
                pool_ops.mining_rpc_urls.return_value = [("node", "http://source:38131")]
                pool_ops.fetch_chain_order_tip.return_value = (2, "test-tip")
                import_pool_ops.return_value = pool_ops
                rc = ipfs_segment_writer.main(["--index", str(index)])

            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertEqual(payload["state"], "deferred")
        self.assertTrue(payload["retrying"])
        self.assertIn("chain integrity preflight not trusted", payload["reasons"][0])
        run_preflight.assert_called_once()

    def test_explicit_normal_publish_beyond_safe_tip_defers_before_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            status = base / "status.json"
            index = base / "latest-index.json"
            index.write_text("{}", encoding="utf-8")
            env = {
                **SIGNING_ENV,
                "BDAG_PROJECT_ROOT": str(ROOT),
                "BDAG_IPFS_SEGMENT_STATUS_FILE": str(status),
                "BDAG_IPFS_SEGMENT_SKIP_MAINTENANCE_DECISION": "1",
                "BDAG_CHAIN_SOURCE_RPC_URL": "http://source:38131",
                "BDAG_CHAIN_REFERENCE_RPC_URL": "http://reference:38131",
                "BDAG_IPFS_SEGMENT_FINALITY_LAG_ORDERS": "10",
            }

            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                ipfs_segment_writer,
                "import_pool_ops",
            ) as import_pool_ops, mock.patch.object(
                ipfs_segment_writer,
                "run_preflight",
                side_effect=AssertionError("explicit unsafe range must not preflight"),
            ), mock.patch.object(
                ipfs_segment_writer,
                "build_segment",
                side_effect=AssertionError("explicit unsafe range must not build"),
            ), mock.patch.object(
                ipfs_segment_writer,
                "ipfs_add",
                side_effect=AssertionError("explicit unsafe range must not call ipfs add"),
            ):
                pool_ops = mock.Mock()
                pool_ops.mining_rpc_urls.return_value = [("node", "http://source:38131")]
                pool_ops.fetch_chain_order_tip.return_value = (100, "test-tip")
                import_pool_ops.return_value = pool_ops
                rc = ipfs_segment_writer.main(["--index", str(index), "--start-order", "95", "--end-order", "100"])

            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertEqual(payload["state"], "waiting_for_finalized_range")
        self.assertEqual(payload["safe_tip"], 90)
        self.assertEqual(payload["reason"], "explicit_range_exceeds_safe_tip")

    def test_normal_publish_includes_trusted_preflight_in_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            status = base / "status.json"
            index = base / "latest-index.json"
            index.write_text("{}", encoding="utf-8")
            env = {
                **SIGNING_ENV,
                "BDAG_PROJECT_ROOT": str(ROOT),
                "BDAG_IPFS_SEGMENT_STATUS_FILE": str(status),
                "BDAG_IPFS_SEGMENT_SKIP_MAINTENANCE_DECISION": "1",
                "BDAG_CHAIN_SOURCE_RPC_URL": "http://source:38131",
                "BDAG_CHAIN_REFERENCE_RPC_URL": "http://reference:38131",
                "BDAG_IPFS_SEGMENT_FINALITY_LAG_ORDERS": "0",
                "BDAG_IPFS_SEGMENT_ORDERS_PER_SEGMENT": "2",
            }
            trusted = {"state": "trusted", "trusted": True, "segment_preflight": {"block_count": 2}}
            record = {
                "segment_id": 1,
                "start_order": 1,
                "end_order": 2,
                "start_hash": "0x1",
                "end_hash": "0x2",
                "manifest_cid": "baf-manifest",
                "payload_cid": "baf-payload",
            }

            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                ipfs_segment_writer,
                "import_pool_ops",
            ) as import_pool_ops, mock.patch.object(
                ipfs_segment_writer,
                "run_preflight",
                return_value=trusted,
            ), mock.patch.object(
                ipfs_segment_writer,
                "build_segment",
                return_value=record,
            ), mock.patch.object(
                ipfs_segment_writer,
                "add_checked_json",
                return_value=("baf-index", "sha", 123),
            ), mock.patch.object(
                ipfs_segment_writer,
                "update_discovery",
                return_value=None,
            ) as update_discovery, mock.patch.object(
                ipfs_segment_writer,
                "publish_ipns",
                return_value={"published": False, "reason": "disabled"},
            ) as publish_ipns:
                pool_ops = mock.Mock()
                pool_ops.mining_rpc_urls.return_value = [("node", "http://source:38131")]
                pool_ops.fetch_chain_order_tip.return_value = (2, "test-tip")
                import_pool_ops.return_value = pool_ops
                rc = ipfs_segment_writer.main(["--index", str(index)])

            payload = json.loads(status.read_text(encoding="utf-8"))
            written_index = json.loads(index.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertEqual(payload["state"], "published")
        self.assertEqual(payload["chain_integrity"]["state"], "trusted")
        self.assertEqual(payload["writer_election"]["mode"], "bootstrap_single_writer")
        self.assertEqual(payload["segments_written"], 1)
        self.assertEqual(payload["current_head"]["end_order"], 2)
        self.assertEqual(payload["ipns"]["reason"], "custom_index_discovery_disabled")
        self.assertTrue(payload["append_only_index_policy"]["immutable_index_cids"])
        self.assertEqual(written_index["current_head"]["end_order"], 2)
        self.assertTrue(written_index["append_only_index_policy"]["latest_pointer_is_mutable_discovery_only"])
        update_discovery.assert_not_called()
        publish_ipns.assert_not_called()

    def test_normal_publish_preflights_each_segment_before_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            status = base / "status.json"
            index = base / "latest-index.json"
            index.write_text("{}", encoding="utf-8")
            env = {
                **SIGNING_ENV,
                "BDAG_PROJECT_ROOT": str(ROOT),
                "BDAG_IPFS_SEGMENT_STATUS_FILE": str(status),
                "BDAG_IPFS_SEGMENT_SKIP_MAINTENANCE_DECISION": "1",
                "BDAG_CHAIN_SOURCE_RPC_URL": "http://source:38131",
                "BDAG_CHAIN_REFERENCE_RPC_URL": "http://reference:38131",
                "BDAG_IPFS_SEGMENT_FINALITY_LAG_ORDERS": "0",
                "BDAG_IPFS_SEGMENT_ORDERS_PER_SEGMENT": "2",
                "BDAG_IPFS_SEGMENT_MAX_SEGMENTS_PER_RUN": "2",
                "BDAG_IPFS_SEGMENT_START_ORDER": "1",
            }
            call_order: list[str] = []

            def preflight_side_effect(_env, _index_path, _source_url, _reference_url, start, end):
                call_order.append(f"preflight:{start}-{end}")
                return {"state": "trusted", "trusted": True, "segment_preflight": {"start_order": start, "end_order": end}}

            def build_side_effect(
                _pool_ops,
                _source_name,
                _source_url,
                start,
                end,
                _index,
                _env,
                _election=None,
                _publication_integrity=None,
            ):
                call_order.append(f"build:{start}-{end}")
                return {
                    "segment_id": 1 if start == 1 else 2,
                    "start_order": start,
                    "end_order": end,
                    "start_hash": f"0x{start}",
                    "end_hash": f"0x{end}",
                    "manifest_cid": f"baf-manifest-{start}",
                    "payload_cid": f"baf-payload-{start}",
                }

            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                ipfs_segment_writer,
                "import_pool_ops",
            ) as import_pool_ops, mock.patch.object(
                ipfs_segment_writer,
                "run_preflight",
                side_effect=preflight_side_effect,
            ), mock.patch.object(
                ipfs_segment_writer,
                "build_segment",
                side_effect=build_side_effect,
            ), mock.patch.object(
                ipfs_segment_writer,
                "add_checked_json",
                return_value=("baf-index", "sha", 123),
            ), mock.patch.object(
                ipfs_segment_writer,
                "update_discovery",
                return_value=None,
            ), mock.patch.object(
                ipfs_segment_writer,
                "publish_ipns",
                return_value={"published": False, "reason": "disabled"},
            ):
                pool_ops = mock.Mock()
                pool_ops.mining_rpc_urls.return_value = [("node", "http://source:38131")]
                pool_ops.fetch_chain_order_tip.return_value = (4, "test-tip")
                import_pool_ops.return_value = pool_ops
                rc = ipfs_segment_writer.main(["--index", str(index)])

            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertEqual(call_order, ["preflight:1-2", "build:1-2", "preflight:3-4", "build:3-4"])
        self.assertEqual(payload["segments_written"], 2)
        self.assertEqual(len(payload["chain_integrity_preflights"]), 2)

    def test_normal_publish_defers_when_another_roster_writer_is_selected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            status = base / "status.json"
            index = base / "latest-index.json"
            index.write_text("{}", encoding="utf-8")
            probe = ipfs_segment_writer.writer_election(
                {"BDAG_IPFS_SEGMENT_WRITER_ID": "writer-a", "BDAG_IPFS_SEGMENT_WRITER_ROSTER": "writer-a,writer-b"},
                1,
                2,
            )
            local_writer = "writer-b" if probe["selected_writer_id"] == "writer-a" else "writer-a"
            env = {
                "BDAG_PROJECT_ROOT": str(ROOT),
                "BDAG_IPFS_SEGMENT_STATUS_FILE": str(status),
                "BDAG_IPFS_SEGMENT_SKIP_MAINTENANCE_DECISION": "1",
                "BDAG_CHAIN_SOURCE_RPC_URL": "http://source:38131",
                "BDAG_CHAIN_REFERENCE_RPC_URL": "http://reference:38131",
                "BDAG_IPFS_SEGMENT_FINALITY_LAG_ORDERS": "0",
                "BDAG_IPFS_SEGMENT_ORDERS_PER_SEGMENT": "2",
                "BDAG_IPFS_SEGMENT_WRITER_ROSTER": "writer-a,writer-b",
                "BDAG_IPFS_SEGMENT_WRITER_ID": local_writer,
            }

            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                ipfs_segment_writer,
                "import_pool_ops",
            ) as import_pool_ops, mock.patch.object(
                ipfs_segment_writer,
                "run_preflight",
                side_effect=AssertionError("non-elected writer must not preflight"),
            ), mock.patch.object(
                ipfs_segment_writer,
                "build_segment",
                side_effect=AssertionError("non-elected writer must not build"),
            ):
                pool_ops = mock.Mock()
                pool_ops.mining_rpc_urls.return_value = [("node", "http://source:38131")]
                pool_ops.fetch_chain_order_tip.return_value = (2, "test-tip")
                import_pool_ops.return_value = pool_ops
                rc = ipfs_segment_writer.main(["--index", str(index)])

            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertEqual(payload["state"], "deferred")
        self.assertEqual(payload["writer_election"]["reason"], "another_writer_selected")


if __name__ == "__main__":
    unittest.main()

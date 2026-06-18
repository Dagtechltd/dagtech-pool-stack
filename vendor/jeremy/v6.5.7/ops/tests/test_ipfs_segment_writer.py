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
        }

        start, end, safe_tip, reason = ipfs_segment_writer.choose_next_range(index, 1_400, env)

        self.assertEqual(safe_tip, 1_300)
        self.assertEqual(start, 1_235)
        self.assertEqual(end, 1_284)
        self.assertEqual(reason, "append_after_current_head")

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

    def test_build_segment_publishes_unsigned_manifest_metadata(self) -> None:
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
                    {"BDAG_NETWORK": "mainnet"},
                )

        manifest = captured["bdag_ipfs_segment_manifest_v1"]
        self.assertNotIn("manifest_cid", manifest)
        self.assertNotIn("signature_status", manifest)
        self.assertNotIn("manifest_root", manifest)
        self.assertNotIn("signatures", manifest)
        self.assertNotIn("manifest_root", record)
        self.assertNotIn("manifest_signatures", record)

    def test_canonical_json_bytes_are_stable(self) -> None:
        left = ipfs_segment_writer.canonical_json_bytes({"b": 1, "a": [2, 3]})
        right = ipfs_segment_writer.canonical_json_bytes({"a": [2, 3], "b": 1})

        self.assertEqual(left, right)

    def test_normal_publish_preflight_requirement_has_no_env_disable(self) -> None:
        self.assertTrue(ipfs_segment_writer.normal_publish_requires_preflight({"BDAG_IPFS_SEGMENT_REQUIRE_PREFLIGHT": "0"}))

    def test_preflight_runs_chain_gate_without_ipfs_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            status = base / "status.json"
            index = base / "latest-index.json"
            index.write_text("{}", encoding="utf-8")
            env = {
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

    def test_normal_publish_includes_trusted_preflight_in_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            status = base / "status.json"
            index = base / "latest-index.json"
            index.write_text("{}", encoding="utf-8")
            env = {
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
            ), mock.patch.object(
                ipfs_segment_writer,
                "publish_ipns",
                return_value={"published": False, "reason": "disabled"},
            ):
                pool_ops = mock.Mock()
                pool_ops.mining_rpc_urls.return_value = [("node", "http://source:38131")]
                pool_ops.fetch_chain_order_tip.return_value = (2, "test-tip")
                import_pool_ops.return_value = pool_ops
                rc = ipfs_segment_writer.main(["--index", str(index)])

            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertEqual(payload["state"], "published")
        self.assertEqual(payload["chain_integrity"]["state"], "trusted")
        self.assertEqual(payload["segments_written"], 1)

    def test_normal_publish_preflights_each_segment_before_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            status = base / "status.json"
            index = base / "latest-index.json"
            index.write_text("{}", encoding="utf-8")
            env = {
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

            def build_side_effect(_pool_ops, _source_name, _source_url, start, end, _index, _env):
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


if __name__ == "__main__":
    unittest.main()

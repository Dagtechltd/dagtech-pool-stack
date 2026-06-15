import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "ops" / "ipfs_segment_backfill.py"
SPEC = importlib.util.spec_from_file_location("ipfs_segment_backfill", MODULE_PATH)
ipfs_segment_backfill = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(ipfs_segment_backfill)


class IPFSSegmentBackfillTest(unittest.TestCase):
    def test_next_start_order_uses_genesis_default_for_empty_index(self) -> None:
        self.assertEqual(ipfs_segment_backfill.next_start_order({}, 1), 1)

    def test_normalized_start_order_treats_order_zero_as_genesis_identity(self) -> None:
        self.assertEqual(ipfs_segment_backfill.normalized_start_order(0), 1)
        self.assertEqual(ipfs_segment_backfill.normalized_start_order(-10), 1)
        self.assertEqual(ipfs_segment_backfill.normalized_start_order(7), 7)

    def test_next_start_order_resumes_after_current_head(self) -> None:
        index = {
            "current_head": {"end_order": 600},
            "segments": [{"segment_id": 1, "start_order": 301, "end_order": 600}],
        }
        self.assertEqual(ipfs_segment_backfill.next_start_order(index, 1), 601)

    def test_plan_reports_missing_range_without_running_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            index = base / "candidate.json"
            index.write_text(
                json.dumps(
                    {
                        "current_head": {"end_order": 600},
                        "segments": [{"segment_id": 1, "start_order": 301, "end_order": 600}],
                    }
                ),
                encoding="utf-8",
            )
            status = base / "status.json"
            env = {
                "BDAG_IPFS_BACKFILL_INDEX_PATH": str(index),
                "BDAG_IPFS_SEGMENT_ORDERS_PER_SEGMENT": "100",
                "BDAG_IPFS_BACKFILL_MAX_SEGMENTS_PER_RUN": "2",
            }

            with mock.patch.object(ipfs_segment_backfill.ipfs_segment_writer, "load_env", return_value=env), mock.patch.object(
                ipfs_segment_backfill.ipfs_segment_writer,
                "main",
                side_effect=AssertionError("plan must not run writer"),
            ):
                rc = ipfs_segment_backfill.main(
                    [
                        "--plan",
                        "--status-file",
                        str(status),
                        "--stop-order",
                        "950",
                        "--json",
                    ]
                )
            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertEqual(payload["state"], "planned")
        self.assertEqual(payload["next_start_order"], 601)
        self.assertEqual(payload["remaining_orders"], 350)
        self.assertEqual(payload["segments_remaining"], 4)
        self.assertEqual(payload["planned_segments_this_run"], 2)
        self.assertEqual(payload["last_planned_end_order"], 800)
        self.assertIn("order_0_is_genesis_identity_only", payload["genesis_order_policy"])
        self.assertEqual(payload["mutation_policy"], "plan_only_no_rpc_no_ipfs_no_index_write_except_status")

    def test_plan_requires_stop_order_to_keep_backfill_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / "status.json"
            with mock.patch.object(ipfs_segment_backfill.ipfs_segment_writer, "load_env", return_value={}):
                rc = ipfs_segment_backfill.main(["--plan", "--status-file", str(status), "--json"])
            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 1)
        self.assertEqual(payload["state"], "blocked")
        self.assertEqual(payload["reason"], "stop_order_required")

    def test_main_requires_bounded_stop_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / "status.json"
            with mock.patch.object(ipfs_segment_backfill.ipfs_segment_writer, "load_env", return_value={}):
                rc = ipfs_segment_backfill.main(["--status-file", str(status), "--json"])
            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 1)
        self.assertEqual(payload["state"], "blocked")
        self.assertEqual(payload["reason"], "stop_order_required")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3

import argparse
import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import paid_conversion_release_gate as gate  # noqa: E402


def args(**overrides):
    values = {
        "min_seconds": 60.0,
        "min_miner_hours": 0.01,
        "min_accepted_submits": 1.0,
        "min_accepted_per_miner_hour": 1.0,
        "max_local_drop_ratio": 0.05,
        "max_share_reject_ratio": 0.25,
        "max_local_rejects_per_accepted": 0.05,
        "require_chain_confirmation": True,
        "min_confirmed_paid_blocks": 1.0,
        "allow_dirty_repos": False,
        "allow_no_miners": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class PaidConversionReleaseGateTests(unittest.TestCase):
    def passing_baseline(self):
        return {
            "metadata": {"document_type": "paid_block_conversion_baseline"},
            "paid_mining_state": {"state": "mining_paid_ok"},
            "summary": {
                "measured_seconds": 3600,
                "miner_hours": 1.0,
                "active_miners_end": 1,
                "accepted_submit_delta": 12,
                "accepted_submit_per_miner_hour": 12,
                "local_candidate_drop_ratio": 0.01,
                "share_reject_ratio": 0.02,
                "confirmed_blue_paid_blocks": 11,
                "selected_backend_state": {"node_mineable": 1.0, "node_submit_ready": 1.0},
            },
            "source_repos": {
                "pool": {"exists": True, "dirty": False},
                "blockdag_corechain": {"exists": True, "dirty": False},
            },
        }

    def test_passing_baseline_requires_paid_chain_confirmation(self) -> None:
        record = gate.baseline_evidence(pathlib.Path("baseline.json"), self.passing_baseline(), args())

        self.assertTrue(record["gate_passed"], record["failures"])
        self.assertEqual(11, record["metrics"]["confirmed_paid_blocks"])

    def test_missing_chain_confirmation_fails_by_default(self) -> None:
        payload = self.passing_baseline()
        del payload["summary"]["confirmed_blue_paid_blocks"]

        record = gate.baseline_evidence(pathlib.Path("baseline.json"), payload, args())

        self.assertFalse(record["gate_passed"])
        self.assertIn("missing confirmed paid-chain evidence", record["failures"])

    def test_unready_backend_and_dirty_repo_fail(self) -> None:
        payload = self.passing_baseline()
        payload["summary"]["selected_backend_state"]["node_submit_ready"] = 0.0
        payload["source_repos"]["blockdag_corechain"]["dirty"] = True

        record = gate.baseline_evidence(pathlib.Path("baseline.json"), payload, args())

        self.assertFalse(record["gate_passed"])
        self.assertIn("selected backend is not submit-ready", record["failures"])
        self.assertIn("source repo blockdag_corechain is dirty", record["failures"])

    def test_ab_summary_quality_and_local_drop_gate(self) -> None:
        payload = {
            "eligible_for_compare": False,
            "quality_flags": ["measured_seconds<3600"],
            "measured_seconds": 30,
            "miner_hours": 0.005,
            "connected_miners_min": 1,
            "accepted_blocks": 3,
            "accepted_blocks_per_miner_hour": 600,
            "rejected_local_per_accepted": 0.2,
            "confirmed_paid_blocks": 3,
        }

        record = gate.ab_summary_evidence(pathlib.Path("summary.json"), payload, args())

        self.assertFalse(record["gate_passed"])
        self.assertIn("A/B summary is not eligible_for_compare", record["failures"])
        self.assertIn("quality flag: measured_seconds<3600", record["failures"])
        self.assertIn("rejected_local_per_accepted 0.2 > allowed 0.05", record["failures"])


if __name__ == "__main__":
    unittest.main()

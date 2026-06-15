#!/usr/bin/env python3

from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock


OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import sync_coordinator  # noqa: E402


class SyncCoordinatorStateTests(unittest.TestCase):
    def test_status_source_uses_local_sampler_before_collector(self) -> None:
        with mock.patch.object(sync_coordinator, "collect_stack_status", return_value={"overall": "ok"}) as collect:
            self.assertEqual({"overall": "ok"}, sync_coordinator.collect_status_cached())

        collect.assert_called_once_with(
            include_logs=False,
            max_age_seconds=sync_coordinator.STATUS_MAX_AGE_SECONDS,
            prefer_collector=False,
        )

    def test_build_state_uses_container_liveness_and_chain_height(self) -> None:
        status = {
            "overall": "syncing",
            "containers": {
                "node": {
                    "running": True,
                    "status": "running",
                    "name": "blockdag-asic-pool-node-1",
                }
            },
            "nodes": {
                "node": {
                    "chain_block_count": 10852049,
                    "latest_block": None,
                    "importing": True,
                    "last_import_age_seconds": 4,
                }
            },
            "sync_progress": {
                "status": "syncing",
                "remaining_blocks": 99,
                "nodes": {
                    "node": {
                        "remaining_blocks": 12,
                    }
                },
            },
        }

        with mock.patch.object(sync_coordinator, "NODES", ["node"]), mock.patch.object(
            sync_coordinator, "collect_status_cached", return_value=status
        ):
            state = sync_coordinator.build_state()

        node = state["nodes"]["node"]
        self.assertTrue(node["running"])
        self.assertEqual(10852049, node["height"])
        self.assertEqual(12, node["remaining_blocks"])
        self.assertTrue(node["importing"])
        self.assertEqual("syncing", state["sync_status"])
        self.assertEqual("syncing", state["overall"])


if __name__ == "__main__":
    unittest.main()

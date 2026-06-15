#!/usr/bin/env python3

import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


class SyncProgressRPCTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            name: getattr(pool_ops, name)
            for name in (
                "native_sync_progress",
                "node_chain_rpc_snapshot",
                "node_template_health_snapshot",
            )
        }
        self.addCleanup(self.restore)

    def restore(self) -> None:
        for name, value in self.originals.items():
            setattr(pool_ops, name, value)

    def test_template_health_syncing_state_drives_sync_progress(self) -> None:
        pool_ops.node_chain_rpc_snapshot = lambda *_args, **_kwargs: {
            "chain_rpc_source": "getBlockCount",
            "chain_block_count": 100,
            "chain_main_height": 90,
            "chain_rpc_error": "",
        }
        pool_ops.node_template_health_snapshot = lambda *_args, **_kwargs: {
            "template_health_available": True,
            "template_health_chain_current": False,
            "template_health_sync_allowed": False,
            "template_health_sync_reason_code": "node_syncing",
            "template_health_sync_reason": "node busy syncing",
            "template_health_main_order": 100,
            "template_health_p2p_best_peer_main_order": 130,
            "template_health_p2p_best_peer_lead_blocks": 30,
        }
        pool_ops.native_sync_progress = lambda *_args, **_kwargs: None

        progress = pool_ops.node_sync_progress("node", "http://node:38131")

        self.assertEqual(progress["status"], "syncing")
        self.assertEqual(progress["source"], "node:getTemplateHealth")
        self.assertEqual(progress["remaining_blocks"], 30)
        self.assertEqual(progress["error"], "node busy syncing")

    def test_template_health_current_state_allows_synced_progress(self) -> None:
        pool_ops.node_chain_rpc_snapshot = lambda *_args, **_kwargs: {
            "chain_rpc_source": "getBlockCount",
            "chain_block_count": 100,
            "chain_main_height": 90,
            "chain_rpc_error": "",
        }
        pool_ops.node_template_health_snapshot = lambda *_args, **_kwargs: {
            "template_health_available": True,
            "template_health_reason_code": "ok",
            "template_health_sync_reason_code": "ok",
            "template_health_chain_current": True,
            "template_health_sync_allowed": True,
            "template_health_main_order": 100,
            "template_health_p2p_best_peer_main_order": 100,
            "template_health_p2p_best_peer_lead_blocks": 0,
        }
        pool_ops.native_sync_progress = lambda *_args, **_kwargs: None

        progress = pool_ops.node_sync_progress("node", "http://node:38131")

        self.assertEqual(progress["status"], "synced")
        self.assertTrue(progress["template_health_available"])
        self.assertEqual(progress["remaining_blocks"], 0)


if __name__ == "__main__":
    unittest.main()

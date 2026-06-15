#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


SCRIPT = Path(__file__).with_name("release-readiness-check.py")
SPEC = importlib.util.spec_from_file_location("release_readiness_check", SCRIPT)
assert SPEC and SPEC.loader
readiness = importlib.util.module_from_spec(SPEC)
sys.modules["release_readiness_check"] = readiness
SPEC.loader.exec_module(readiness)


class MockRPCHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        method = body["method"]
        result = {
            "getNodeInfo": {"ID": "self-node", "network": "mainnet", "connections": 4},
            "getTemplateHealth": {
                "mineable_now": True,
                "submit_ready": True,
                "reason_code": "ok",
            },
            "getPeerInfo": [
                {
                    "id": "self-node",
                    "address": "/ip4/10.0.0.1/tcp/8150/p2p/self-node",
                    "active": True,
                    "state": True,
                },
                {
                    "id": "loopback",
                    "address": "/ip4/127.0.0.1/tcp/8150/p2p/loopback",
                    "active": True,
                    "state": True,
                },
                {
                    "id": "inactive",
                    "address": "/ip4/10.0.0.9/tcp/8150/p2p/inactive",
                    "active": False,
                    "state": True,
                },
                {
                    "id": "good",
                    "address": "/ip4/52.8.80.249/tcp/8150/p2p/good",
                    "active": True,
                    "state": True,
                },
            ],
            "getBlockTemplate": {
                "height": 42,
                "previousblockhash": "abcd",
                "txroot": "tx",
                "stateroot": "state",
                "coinbase_address": "0x0000000000000000000000000000000000000000",
                "pow_diff_reference": {"nbits": "1d00ffff"},
            },
        }[method]
        encoded = json.dumps({"jsonrpc": "2.0", "id": body.get("id"), "result": result})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded.encode("utf-8"))

    def log_message(self, fmt: str, *args: object) -> None:
        return


class ReadinessCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), MockRPCHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.rpc_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.thread.join(timeout=2)

    def args(self) -> argparse.Namespace:
        return argparse.Namespace(
            env_file="/nonexistent",
            rpc_url=self.rpc_url,
            rpc_user="test",
            rpc_pass="test",
            timeout=2.0,
            min_peers=1,
            stability_samples=1,
            stability_interval=0.0,
            pow_type=10,
            mining_address="",
            skip_postgres=True,
            postgres_service="postgres",
            pg_url=None,
            schema_file=None,
            json=False,
        )

    def test_readiness_passes_with_sane_peer_and_template(self) -> None:
        results = readiness.run_checks(self.args())
        self.assertTrue(all(result.ok for result in results), results)
        peer_result = next(result for result in results if result.name == "peer_sanity")
        self.assertIn("1 sane peers", peer_result.detail)

    def test_peer_gate_fails_when_minimum_exceeds_filtered_peers(self) -> None:
        args = self.args()
        args.min_peers = 2
        results = readiness.run_checks(args)
        peer_result = next(result for result in results if result.name == "peer_sanity")
        self.assertFalse(peer_result.ok)
        self.assertIn("need 2", peer_result.detail)

    def test_empty_peer_address_is_not_treated_as_loopback(self) -> None:
        self.assertFalse(readiness.is_loopback_or_unspecified(""))
        self.assertTrue(readiness.is_loopback_or_unspecified("127.0.0.1"))

    def test_peer_gate_rejects_empty_peer_address(self) -> None:
        args = self.args()
        args.min_peers = 1

        def fake_rpc_call(url, user, password, method, params=None, timeout=5.0):
            if method == "getPeerInfo":
                return [{"id": "empty-address", "address": "", "active": True, "state": True}]
            raise AssertionError(method)

        with mock.patch.object(readiness, "rpc_call", side_effect=fake_rpc_call):
            result = readiness.check_peer_sanity(args, {"ID": "self-node"})
        self.assertFalse(result.ok)
        self.assertIn("invalid=1", result.detail)

    def test_rpc_timeout_returns_clean_check_error(self) -> None:
        with mock.patch.object(readiness.urllib.request, "urlopen", side_effect=TimeoutError()):
            with self.assertRaises(readiness.CheckError) as ctx:
                readiness.rpc_call(self.rpc_url, "test", "test", "getNodeInfo", timeout=0.1)
        self.assertIn("getNodeInfo timed out after 0.1s", str(ctx.exception))

    def test_run_checks_continues_when_get_node_info_is_unavailable(self) -> None:
        args = self.args()
        args.min_peers = 0

        def fake_rpc_call(url, user, password, method, params=None, timeout=5.0):
            if method == "getNodeInfo":
                raise readiness.CheckError("getNodeInfo timed out after 5.0s")
            if method == "getTemplateHealth":
                return {"mineable_now": True, "submit_ready": True}
            if method == "getPeerInfo":
                return []
            if method == "getBlockTemplate":
                return {
                    "height": 42,
                    "previousblockhash": "abcd",
                    "txroot": "tx",
                    "stateroot": "state",
                    "coinbase_address": "0x0000000000000000000000000000000000000000",
                    "pow_diff_reference": {"nbits": "1d00ffff"},
                }
            raise AssertionError(method)

        with mock.patch.object(readiness, "rpc_call", side_effect=fake_rpc_call):
            results = readiness.run_checks(args)
        node_result = next(result for result in results if result.name == "node_rpc")
        self.assertTrue(node_result.ok)
        self.assertTrue(node_result.skipped)
        self.assertTrue(all(result.ok for result in results), results)

    def test_template_health_gate_rejects_submit_not_ready(self) -> None:
        args = self.args()
        with mock.patch.object(
            readiness,
            "rpc_call",
            return_value={
                "mineable_now": True,
                "submit_ready": False,
                "p2p_mining_fresh": True,
                "get_block_template_ready": True,
            },
        ):
            result = readiness.check_sync_or_mineable(args)
        self.assertFalse(result.ok)
        self.assertIn("submit_ready=false", result.detail)

    def test_template_health_gate_rejects_stale_p2p(self) -> None:
        args = self.args()
        with mock.patch.object(
            readiness,
            "rpc_call",
            return_value={
                "mineable_now": True,
                "submit_ready": True,
                "p2p_mining_fresh": False,
                "p2p_mining_fresh_reason_code": "all_consensus_peers_stale",
                "get_block_template_ready": True,
            },
        ):
            result = readiness.check_sync_or_mineable(args)
        self.assertFalse(result.ok)
        self.assertIn("p2p_mining_fresh=false:all_consensus_peers_stale", result.detail)

    def test_template_health_gate_rejects_blocking_template_error(self) -> None:
        args = self.args()
        with mock.patch.object(
            readiness,
            "rpc_call",
            return_value={
                "mineable_now": True,
                "submit_ready": True,
                "p2p_mining_fresh": True,
                "get_block_template_ready": True,
                "last_template_build_error_blocking": True,
                "last_template_build_error_code": "evm_pending_nonce_drift",
            },
        ):
            result = readiness.check_sync_or_mineable(args)
        self.assertFalse(result.ok)
        self.assertIn("blocking template build error: evm_pending_nonce_drift", result.detail)

    def test_mining_rpc_stability_passes_followup_sample(self) -> None:
        args = self.args()
        args.stability_samples = 2
        result = readiness.check_mining_rpc_stability(args, {"ID": "self-node"})
        self.assertTrue(result.ok)
        self.assertIn("2 mining RPC samples stable", result.detail)

    def test_mining_rpc_stability_detects_flapping_template(self) -> None:
        args = self.args()
        args.stability_samples = 3
        template_calls = 0

        def fake_rpc_call(url, user, password, method, params=None, timeout=5.0):
            nonlocal template_calls
            if method == "getTemplateHealth":
                return {"mineable_now": True, "submit_ready": True}
            if method == "getPeerInfo":
                return [
                    {
                        "id": "good",
                        "address": "/ip4/52.8.80.249/tcp/8150/p2p/good",
                        "active": True,
                        "state": True,
                    }
                ]
            if method == "getBlockTemplate":
                template_calls += 1
                if template_calls == 1:
                    return {"height": 42}
                return {
                    "height": 43,
                    "previousblockhash": "abcd",
                    "txroot": "tx",
                    "stateroot": "state",
                    "coinbase_address": "0x0000000000000000000000000000000000000000",
                    "pow_diff_reference": {"nbits": "1d00ffff"},
                }
            raise AssertionError(method)

        with mock.patch.object(readiness, "rpc_call", side_effect=fake_rpc_call):
            result = readiness.check_mining_rpc_stability(args, {})
        self.assertFalse(result.ok)
        self.assertIn("sample 2/3 failed", result.detail)
        self.assertIn("template missing", result.detail)

    def test_postgres_schema_requires_credit_unique_index(self) -> None:
        args = self.args()
        args.skip_postgres = False
        args.pg_url = "postgres://bdag_pool:test@127.0.0.1:5432/bdagpool"

        proc = mock.Mock(returncode=0, stdout="index:credits_block_miner_unique\n", stderr="")
        with mock.patch.object(readiness.subprocess, "run", return_value=proc) as run:
            result = readiness.check_postgres_schema(args, {})

        self.assertFalse(result.ok)
        self.assertIn("index:credits_block_miner_unique", result.detail)
        query = run.call_args.args[0][-1]
        self.assertIn("pg_indexes", query)
        self.assertIn("credits_block_miner_unique", query)

    def test_postgres_schema_requires_block_submission_history(self) -> None:
        args = self.args()
        args.skip_postgres = False
        args.pg_url = "postgres://bdag_pool:test@127.0.0.1:5432/bdagpool"

        proc = mock.Mock(
            returncode=0,
            stdout="block_submissions.candidate_hash\nindex:block_submissions_created_at_idx\n",
            stderr="",
        )
        with mock.patch.object(readiness.subprocess, "run", return_value=proc) as run:
            result = readiness.check_postgres_schema(args, {})

        self.assertFalse(result.ok)
        self.assertIn("block_submissions.candidate_hash", result.detail)
        self.assertIn("index:block_submissions_created_at_idx", result.detail)
        query = run.call_args.args[0][-1]
        self.assertIn("block_submissions", query)
        self.assertIn("block_submissions_outcome_created_idx", query)

    def test_postgres_schema_passes_with_credit_unique_index(self) -> None:
        args = self.args()
        args.skip_postgres = False
        args.pg_url = "postgres://bdag_pool:test@127.0.0.1:5432/bdagpool"

        proc = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(readiness.subprocess, "run", return_value=proc):
            result = readiness.check_postgres_schema(args, {})

        self.assertTrue(result.ok)
        self.assertIn("required indexes present", result.detail)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3

import pathlib
import sys
import unittest
from datetime import datetime, timezone

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


class ChainRpcResilienceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_retries = pool_ops.NODE_CHAIN_RPC_RETRIES
        self.old_pool_rpc_refused_warn_seconds = pool_ops.POOL_RPC_REFUSED_WARN_SECONDS
        self.old_mining_rpc_call = pool_ops.mining_rpc_call
        self.old_json_rpc_call = pool_ops.json_rpc_call
        self.old_evm_reference_rpc_urls = pool_ops.evm_reference_rpc_urls
        self.old_alignment_always_sample = pool_ops.EVM_PUBLIC_ALIGNMENT_ALWAYS_SAMPLE
        self.old_alignment_min_samples = pool_ops.EVM_PUBLIC_ALIGNMENT_MIN_SAMPLES
        self.old_alignment_sample_blocks = pool_ops.EVM_PUBLIC_ALIGNMENT_SAMPLE_BLOCKS
        self.old_alignment_min_reference_lag = pool_ops.EVM_PUBLIC_ALIGNMENT_MIN_REFERENCE_LAG
        self.old_sleep = pool_ops.time.sleep
        self.old_time = pool_ops.time.time
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        pool_ops.NODE_CHAIN_RPC_RETRIES = self.old_retries
        pool_ops.POOL_RPC_REFUSED_WARN_SECONDS = self.old_pool_rpc_refused_warn_seconds
        pool_ops.mining_rpc_call = self.old_mining_rpc_call
        pool_ops.json_rpc_call = self.old_json_rpc_call
        pool_ops.evm_reference_rpc_urls = self.old_evm_reference_rpc_urls
        pool_ops.EVM_PUBLIC_ALIGNMENT_ALWAYS_SAMPLE = self.old_alignment_always_sample
        pool_ops.EVM_PUBLIC_ALIGNMENT_MIN_SAMPLES = self.old_alignment_min_samples
        pool_ops.EVM_PUBLIC_ALIGNMENT_SAMPLE_BLOCKS = self.old_alignment_sample_blocks
        pool_ops.EVM_PUBLIC_ALIGNMENT_MIN_REFERENCE_LAG = self.old_alignment_min_reference_lag
        pool_ops.time.sleep = self.old_sleep
        pool_ops.time.time = self.old_time

    def test_get_block_count_retries_once_before_marking_unavailable(self) -> None:
        pool_ops.NODE_CHAIN_RPC_RETRIES = 2
        pool_ops.time.sleep = lambda *_args, **_kwargs: None
        calls = []

        def fake_rpc(_url, method, _params, timeout):
            calls.append((method, timeout))
            if method == "getBlockCount" and len(calls) == 1:
                raise TimeoutError("timed out")
            if method == "getBlockCount":
                return "8656586"
            if method == "getMainChainHeight":
                return "7001831"
            raise AssertionError(method)

        pool_ops.mining_rpc_call = fake_rpc

        snapshot = pool_ops.node_chain_rpc_snapshot("node", "http://node:38131", timeout=8.0)

        self.assertEqual(snapshot["chain_rpc_error"], "")
        self.assertEqual(snapshot["chain_block_count"], 8656586)
        self.assertEqual(snapshot["chain_main_height"], 7001831)
        self.assertEqual(snapshot["chain_rpc_attempts"], 2)
        self.assertEqual(snapshot["chain_rpc_timeout_seconds"], 8.0)
        self.assertIsNotNone(snapshot["chain_rpc_latency_ms"])
        self.assertEqual([method for method, _timeout in calls], ["getBlockCount", "getBlockCount", "getMainChainHeight"])

    def test_unknown_sync_progress_preserves_chain_rpc_attempt_detail(self) -> None:
        pool_ops.NODE_CHAIN_RPC_RETRIES = 2
        pool_ops.time.sleep = lambda *_args, **_kwargs: None

        def fake_rpc(_url, method, _params, _timeout):
            if method == "getBlockCount":
                raise TimeoutError("timed out")
            raise AssertionError(method)

        pool_ops.mining_rpc_call = fake_rpc

        progress = pool_ops.node_sync_progress("node", "http://node:38131", timeout=8.0)

        self.assertEqual(progress["status"], "unknown")
        self.assertEqual(progress["chain_rpc_attempts"], 2)
        self.assertEqual(progress["chain_rpc_retry_limit"], 2)
        self.assertIn("after 2 attempt", progress["chain_rpc_error"])

    def test_chain_snapshot_never_uses_eth_blocknumber_as_chain_count(self) -> None:
        pool_ops.NODE_CHAIN_RPC_RETRIES = 1

        def fake_mining_rpc(_url, method, _params, timeout):
            if method == "getBlockCount":
                raise RuntimeError("method not available")
            raise AssertionError(method)

        pool_ops.mining_rpc_call = fake_mining_rpc
        pool_ops.json_rpc_call = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("eth_blockNumber is not chain height"))

        snapshot = pool_ops.node_chain_rpc_snapshot("node", "http://127.0.0.1:18545", timeout=8.0)

        self.assertIsNone(snapshot["chain_block_count"])
        self.assertEqual(snapshot["chain_rpc_source"], "unavailable")
        self.assertIn("getBlockCount", snapshot["chain_rpc_error"])

    def test_main_chain_height_is_diagnostic_not_block_count_fallback(self) -> None:
        pool_ops.NODE_CHAIN_RPC_RETRIES = 1

        def fake_mining_rpc(_url, method, _params, timeout):
            if method == "getBlockCount":
                raise RuntimeError("method not available")
            if method == "getMainChainHeight":
                return "7001831"
            raise AssertionError(method)

        pool_ops.mining_rpc_call = fake_mining_rpc

        snapshot = pool_ops.node_chain_rpc_snapshot("node", "http://127.0.0.1:38131", timeout=8.0)

        self.assertIsNone(snapshot["chain_block_count"])
        self.assertEqual(snapshot["chain_main_height"], 7001831)
        self.assertEqual(snapshot["chain_main_height_source"], "getMainChainHeight")
        self.assertEqual(snapshot["chain_rpc_source"], "unavailable")

    def test_node_sync_progress_reports_evm_lag_as_syncing(self) -> None:
        pool_ops.NODE_CHAIN_RPC_RETRIES = 1

        def fake_mining_rpc(_url, method, _params, timeout):
            if method == "getBlockCount":
                return "10000"
            if method == "getMainChainHeight":
                return "10000"
            raise AssertionError(method)

        def fake_json_rpc(url, method, _params, timeout):
            if method == "eth_blockNumber":
                if url == "http://reference:18545":
                    return "0x2710"
                return "0x1f40"
            raise AssertionError(method)

        pool_ops.mining_rpc_call = fake_mining_rpc
        pool_ops.json_rpc_call = fake_json_rpc
        pool_ops.evm_reference_rpc_urls = lambda: [("reference", "http://reference:18545")]

        progress = pool_ops.node_sync_progress("node", "http://127.0.0.1:38131", timeout=8.0)

        self.assertEqual(progress["status"], "syncing")
        self.assertEqual(progress["source"], "node:evm-head-lag")
        self.assertEqual(progress["current_block"], 8000)
        self.assertEqual(progress["highest_block"], 10000)
        self.assertEqual(progress["remaining_blocks"], 2000)
        self.assertEqual(progress["chain_block_count"], 10000)
        self.assertEqual(progress["evm_block_count"], 8000)
        self.assertEqual(progress["evm_lag_to_chain"], 2000)
        self.assertEqual(progress["evm_lag_to_reference"], 2000)
        self.assertEqual(progress["evm_gap_to_chain_count"], 2000)
        self.assertEqual(progress["current_block_source"], "eth_blockNumber")

    def test_canonical_safety_allows_zero_lag_public_evm_hash_diagnostic(self) -> None:
        pool_ops.EVM_PUBLIC_ALIGNMENT_ALWAYS_SAMPLE = True
        pool_ops.EVM_PUBLIC_ALIGNMENT_SAMPLE_BLOCKS = 3
        pool_ops.EVM_PUBLIC_ALIGNMENT_MIN_SAMPLES = 2
        pool_ops.EVM_PUBLIC_ALIGNMENT_MIN_REFERENCE_LAG = 1000

        def fake_json_rpc(url, method, params, timeout):
            if method == "eth_blockNumber":
                return "0x3e8"
            if method == "eth_getBlockByNumber":
                height = int(params[0], 16)
                suffix = "local" if url == "http://local:18545" else "reference"
                return {
                    "number": params[0],
                    "hash": f"0x{height:060x}{1 if suffix == 'local' else 2:04x}",
                    "miner": "0x05518e03e148c56e426ff9e1cbdb962b4fc5250a",
                    "timestamp": hex(1780784000 + height),
                }
            raise AssertionError(method)

        pool_ops.json_rpc_call = fake_json_rpc
        pool_ops.evm_reference_rpc_urls = lambda: [("public", "http://public:18545")]

        snapshot = pool_ops.evm_rpc_lag_snapshot("node", "http://local:18545", 1200, timeout=8.0)

        safety = snapshot["canonical_mining_safety"]
        self.assertTrue(safety["safe"], safety)
        self.assertEqual(safety["hash_mismatch_count"], 3)
        self.assertFalse(safety["public_chain_diverged"])
        self.assertIn("diagnostic", safety["reason"])

    def test_rpc_refused_is_recent_only_inside_warning_window(self) -> None:
        now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        pool_ops.time.time = lambda: now
        pool_ops.POOL_RPC_REFUSED_WARN_SECONDS = 120

        fresh = pool_ops.parse_pool_log(
            "2026/05/25 11:59:15 GBT ERROR: connect: connection refused\n"
        )
        stale = pool_ops.parse_pool_log(
            "2026/05/25 11:55:00 GBT ERROR: connect: connection refused\n"
        )

        self.assertTrue(fresh["rpc_refused"])
        self.assertTrue(fresh["rpc_refused_recent"])
        self.assertEqual(fresh["last_rpc_refused_age_seconds"], 45)
        self.assertTrue(stale["rpc_refused"])
        self.assertFalse(stale["rpc_refused_recent"])
        self.assertEqual(stale["last_rpc_refused_age_seconds"], 300)

    def test_no_miner_sync_noise_includes_template_and_stale_rpc_noise(self) -> None:
        self.assertTrue(pool_ops.is_no_miner_sync_noise("node is refusing live mining template probes"))
        self.assertTrue(pool_ops.is_no_miner_sync_noise("pool recently saw RPC connection refused"))
        self.assertTrue(pool_ops.is_no_miner_sync_noise("pool is waiting for node sync to finish"))
        self.assertFalse(pool_ops.is_no_miner_sync_noise("pool has not accepted a valid share"))

    def test_parse_proc_pressure_extracts_io_wait_signal(self) -> None:
        parsed = pool_ops.parse_proc_pressure(
            "some avg10=12.34 avg60=2.00 avg300=0.50 total=123456\n"
            "full avg10=0.25 avg60=0.10 avg300=0.05 total=789\n"
        )

        self.assertEqual(parsed["some_avg10"], 12.34)
        self.assertEqual(parsed["full_avg10"], 0.25)

    def test_strip_ansi_accepts_timeout_bytes(self) -> None:
        self.assertEqual(pool_ops.strip_ansi(b"\x1b[31mtimeout\x1b[0m"), "timeout")

    def test_parse_proc_stat_cpu_extracts_iowait_counter(self) -> None:
        parsed = pool_ops.parse_proc_stat_cpu(
            "cpu  100 20 30 400 50 0 0 0 0 0\n"
            "cpu0 10 2 3 40 5 0 0 0 0 0\n"
        )

        self.assertEqual(parsed["total"], 600)
        self.assertEqual(parsed["idle"], 400)
        self.assertEqual(parsed["iowait"], 50)

    def test_sustained_iowait_warning_uses_recent_samples(self) -> None:
        samples = [
            {"iowait_percent": 26.0},
            {"iowait_percent": 27.5},
            {"iowait_percent": 25.1},
        ]

        self.assertTrue(pool_ops.host_pressure_iowait_sustained(samples, 25.0, 3))
        self.assertFalse(pool_ops.host_pressure_iowait_sustained(samples[-2:], 25.0, 3))
        self.assertEqual(
            len(pool_ops.host_pressure_warning_messages({"iowait_warning_active": True, "samples": samples})),
            1,
        )


if __name__ == "__main__":
    unittest.main()

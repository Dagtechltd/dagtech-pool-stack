#!/usr/bin/env python3

import os
import pathlib
import sys
import tempfile
import unittest
from decimal import Decimal

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


def trusted_global_cache(latest_block: int, clusters: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "status": "ok",
        "schema_version": pool_ops.GLOBAL_CACHE_SCHEMA_VERSION,
        "source_truth": pool_ops.GLOBAL_STATS_SOURCE_TRUTH,
        "source_contract": "blockdag-mining-rpc-v1",
        "height_method": "getBlockCount",
        "updated_at_epoch": 100,
        "latest_block": latest_block,
        "chain_block_count": latest_block,
        "latest_order": max(0, latest_block - 1),
        "requested_blocks": 1,
        "fetched_blocks": 1,
        "unknown_blocks": 0,
        "partial_scan": False,
        "head_only": False,
        "maintenance_deferred": False,
        "clusters": clusters or [],
        "fetch_errors": [],
    }


class GlobalTabRpcSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_env = os.environ.copy()
        self.old_nodes = pool_ops.NODES
        self.old_services = pool_ops.SERVICES
        self.old_pool_containers = pool_ops.POOL_CONTAINERS
        self.old_docker_container_ip = pool_ops.docker_container_ip
        self.old_global_chain_peer_rpc_enabled = pool_ops.GLOBAL_CHAIN_PEER_RPC_ENABLED
        self.old_global_chain_peer_rpc_limit = pool_ops.GLOBAL_CHAIN_PEER_RPC_LIMIT
        self.old_global_chain_peer_rpc_port = pool_ops.GLOBAL_CHAIN_PEER_RPC_PORT
        self.old_live_peers_file = pool_ops.LIVE_PEERS_FILE
        self.old_chain_peerstore_candidates_file = pool_ops.CHAIN_PEERSTORE_CANDIDATES_FILE
        self.old_peer_discovery_file = pool_ops.PEER_DISCOVERY_FILE
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        os.environ.clear()
        os.environ.update(self.old_env)
        pool_ops.NODES = self.old_nodes
        pool_ops.SERVICES = self.old_services
        pool_ops.POOL_CONTAINERS = self.old_pool_containers
        pool_ops.docker_container_ip = self.old_docker_container_ip
        pool_ops.GLOBAL_CHAIN_PEER_RPC_ENABLED = self.old_global_chain_peer_rpc_enabled
        pool_ops.GLOBAL_CHAIN_PEER_RPC_LIMIT = self.old_global_chain_peer_rpc_limit
        pool_ops.GLOBAL_CHAIN_PEER_RPC_PORT = self.old_global_chain_peer_rpc_port
        pool_ops.LIVE_PEERS_FILE = self.old_live_peers_file
        pool_ops.CHAIN_PEERSTORE_CANDIDATES_FILE = self.old_chain_peerstore_candidates_file
        pool_ops.PEER_DISCOVERY_FILE = self.old_peer_discovery_file

    def test_global_chain_uses_mining_rpc_when_node_rpc_is_configured(self) -> None:
        pool_ops.GLOBAL_CHAIN_PEER_RPC_ENABLED = False
        os.environ["BDAG_NODE_RPC_URL"] = "http://127.0.0.1:38131"
        for key in ("BDAG_GLOBAL_CHAIN_RPC_URLS",):
            os.environ.pop(key, None)

        self.assertEqual(pool_ops.global_chain_rpc_urls(), [("node", "http://127.0.0.1:38131")])

    def test_global_chain_adds_live_peer_rpc_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            pool_ops.LIVE_PEERS_FILE = root / "live-peers-current.txt"
            pool_ops.CHAIN_PEERSTORE_CANDIDATES_FILE = root / "chain-peerstore-candidates.txt"
            pool_ops.PEER_DISCOVERY_FILE = root / "peer-discovery-current.json"
            pool_ops.LIVE_PEERS_FILE.write_text(
                "/ip4/198.51.100.10/tcp/8150/p2p/16Uiu2HAmExample\n",
                encoding="utf-8",
            )
            pool_ops.PEER_DISCOVERY_FILE.write_text('{"peers":[]}\n', encoding="utf-8")
            pool_ops.GLOBAL_CHAIN_PEER_RPC_ENABLED = True
            pool_ops.GLOBAL_CHAIN_PEER_RPC_LIMIT = 4
            pool_ops.GLOBAL_CHAIN_PEER_RPC_PORT = 38131
            os.environ["BDAG_GLOBAL_CHAIN_RPC_URLS"] = "node=http://127.0.0.1:38131"

            self.assertEqual(
                pool_ops.global_chain_rpc_urls(),
                [
                    ("node", "http://127.0.0.1:38131"),
                    ("live-peer-198.51.100.10", "http://198.51.100.10:38131"),
                ],
            )

    def test_global_evm_rpc_stays_separate_for_wallet_reads(self) -> None:
        for key in ("BDAG_GLOBAL_RPC_URLS", "BDAG_EVM_RPC_URLS", "WALLET_RPC_URLS"):
            os.environ.pop(key, None)
        pool_ops.NODES = ["node"]
        pool_ops.SERVICES = ["node"]
        pool_ops.POOL_CONTAINERS = []
        pool_ops.docker_container_ip = lambda name: "172.22.0.2" if name == "node" else ""

        self.assertEqual(
            pool_ops.global_evm_rpc_urls(),
            [("node", "http://172.22.0.2:18545")],
        )

    def test_global_rewrites_compose_service_hostname_for_host_dashboard(self) -> None:
        pool_ops.GLOBAL_CHAIN_PEER_RPC_ENABLED = False
        os.environ["BDAG_GLOBAL_CHAIN_RPC_URLS"] = "node=http://node:38131"
        pool_ops.NODES = ["node"]
        pool_ops.SERVICES = ["node"]
        pool_ops.POOL_CONTAINERS = []
        pool_ops.docker_container_ip = lambda name: "172.22.0.2" if name == "node" else ""

        self.assertEqual(
            pool_ops.global_chain_rpc_urls(),
            [("node", "http://172.22.0.2:38131")],
        )


class GlobalTabFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_read_json_file = pool_ops.read_json_file
        self.old_read_global_history = pool_ops.read_global_history
        self.old_seconds_since_epoch = pool_ops.seconds_since_epoch
        self.old_global_chain_rpc_urls = pool_ops.global_chain_rpc_urls
        self.old_public_evm_rpc_urls = pool_ops.public_evm_rpc_urls
        self.old_json_rpc_call = pool_ops.json_rpc_call
        self.old_mining_rpc_call = pool_ops.mining_rpc_call
        self.old_background_maintenance_decision = pool_ops.background_maintenance_decision
        self.old_global_evm_fallback_enabled = pool_ops.GLOBAL_EVM_FALLBACK_ENABLED
        self.old_global_evm_fallback_block_window = pool_ops.GLOBAL_EVM_FALLBACK_BLOCK_WINDOW
        self.old_global_evm_fallback_rpc_workers = pool_ops.GLOBAL_EVM_FALLBACK_RPC_WORKERS
        self.old_collect_local_pool_global_clusters = pool_ops.collect_local_pool_global_clusters
        self.old_write_global_cache = pool_ops.write_global_cache
        pool_ops.GLOBAL_EVM_FALLBACK_ENABLED = False
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        pool_ops.read_json_file = self.old_read_json_file
        pool_ops.read_global_history = self.old_read_global_history
        pool_ops.seconds_since_epoch = self.old_seconds_since_epoch
        pool_ops.global_chain_rpc_urls = self.old_global_chain_rpc_urls
        pool_ops.public_evm_rpc_urls = self.old_public_evm_rpc_urls
        pool_ops.json_rpc_call = self.old_json_rpc_call
        pool_ops.mining_rpc_call = self.old_mining_rpc_call
        pool_ops.background_maintenance_decision = self.old_background_maintenance_decision
        pool_ops.GLOBAL_EVM_FALLBACK_ENABLED = self.old_global_evm_fallback_enabled
        pool_ops.GLOBAL_EVM_FALLBACK_BLOCK_WINDOW = self.old_global_evm_fallback_block_window
        pool_ops.GLOBAL_EVM_FALLBACK_RPC_WORKERS = self.old_global_evm_fallback_rpc_workers
        pool_ops.collect_local_pool_global_clusters = self.old_collect_local_pool_global_clusters
        pool_ops.write_global_cache = self.old_write_global_cache

    def test_global_rejects_old_evm_cache_instead_of_showing_it_stale(self) -> None:
        cached = {
            "status": "ok",
            "updated_at_epoch": 100,
            "latest_block": 123,
            "clusters": [{"address": "0xabc", "blocks": 1}],
            "fetch_errors": [],
        }

        def fake_read_json_file(path: pathlib.Path, fallback: object) -> object:
            if path == pool_ops.GLOBAL_CACHE_FILE:
                return cached
            return fallback

        pool_ops.read_json_file = fake_read_json_file
        pool_ops.read_global_history = lambda limit=None: [{"latest_block": 122, "clusters": []}]
        pool_ops.seconds_since_epoch = lambda: 999_999
        pool_ops.global_chain_rpc_urls = lambda: [("bad-node", "http://127.0.0.1:38131")]
        pool_ops.public_evm_rpc_urls = lambda: []
        pool_ops.background_maintenance_decision = lambda task: {"allowed": True, "task": task, "reasons": []}

        def fail_rpc(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("rpc unavailable")

        pool_ops.mining_rpc_call = fail_rpc

        payload = pool_ops.collect_global_blockchain()

        self.assertEqual(payload["status"], "failed")
        self.assertFalse(payload["cache"]["hit"])
        self.assertEqual(payload["clusters"], [])
        self.assertIn("ignored stale global cache", payload["fetch_errors"][0])
        self.assertIn("getBlockCount", payload["error"])

    def test_global_does_not_use_evm_fallback_by_default_when_chain_rpc_fails(self) -> None:
        cached = {
            "status": "degraded",
            "source_contract": "evm-rpc-fallback-v1",
            "updated_at_epoch": 100,
            "requested_blocks": 64,
            "fetched_blocks": 64,
            "clusters": [{"address": "0xabc", "blocks": 64}],
        }

        def fake_read_json_file(path: pathlib.Path, fallback: object) -> object:
            if path == pool_ops.GLOBAL_CACHE_FILE:
                return cached
            return fallback

        pool_ops.GLOBAL_EVM_FALLBACK_ENABLED = False
        pool_ops.read_json_file = fake_read_json_file
        pool_ops.read_global_history = lambda limit=None: []
        pool_ops.seconds_since_epoch = lambda: 120
        pool_ops.global_chain_rpc_urls = lambda: [("bad-node", "http://127.0.0.1:38131")]
        pool_ops.public_evm_rpc_urls = lambda: (_ for _ in ()).throw(AssertionError("EVM fallback must be opt-in for global production stats"))
        pool_ops.background_maintenance_decision = lambda task: {"allowed": True, "task": task, "reasons": []}
        pool_ops.mining_rpc_call = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("chain rpc unavailable"))

        payload = pool_ops.collect_global_blockchain()

        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["source_truth"], pool_ops.GLOBAL_STATS_SOURCE_TRUTH)
        self.assertEqual(payload["clusters"], [])
        self.assertIn("getBlockCount", payload["error"])

    def test_evm_fallback_uses_configured_scan_window(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc".lower()
        fetched_blocks: list[int] = []

        pool_ops.GLOBAL_EVM_FALLBACK_ENABLED = True
        pool_ops.GLOBAL_EVM_FALLBACK_BLOCK_WINDOW = 5
        pool_ops.GLOBAL_EVM_FALLBACK_RPC_WORKERS = 1
        pool_ops.read_json_file = lambda _path, fallback: fallback
        pool_ops.read_global_history = lambda limit=None: []
        pool_ops.seconds_since_epoch = lambda: 120
        pool_ops.global_chain_rpc_urls = lambda: [("bad-chain", "http://bad-chain")]
        pool_ops.public_evm_rpc_urls = lambda: [("evm", "http://evm-rpc")]
        pool_ops.background_maintenance_decision = lambda task: {"allowed": True, "task": task, "reasons": []}
        pool_ops.collect_local_pool_global_clusters = lambda *_args, **_kwargs: []
        pool_ops.write_global_cache = lambda _payload: None

        def fake_mining_rpc(_url: str, method: str, _params: list[object], timeout: float = 0) -> object:
            if method == "getBlockCount":
                return 10
            raise RuntimeError("chain order unavailable")

        def fake_json_rpc(_url: str, method: str, params: list[object], timeout: float = 0) -> object:
            if method == "eth_blockNumber":
                return hex(20)
            if method == "eth_getBlockByNumber":
                block_number = int(str(params[0]), 16)
                fetched_blocks.append(block_number)
                return {
                    "number": hex(block_number),
                    "timestamp": hex(1_780_000_000 + block_number),
                    "miner": wallet,
                }
            raise AssertionError(f"unexpected JSON-RPC method {method}")

        pool_ops.mining_rpc_call = fake_mining_rpc
        pool_ops.json_rpc_call = fake_json_rpc

        payload = pool_ops.collect_global_blockchain()

        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["source_contract"], "evm-rpc-fallback-v1")
        self.assertEqual(payload["requested_blocks"], 5)
        self.assertEqual(payload["fetched_blocks"], 5)
        self.assertEqual(payload["scan_start_block"], 16)
        self.assertEqual(payload["scan_end_block"], 20)
        self.assertEqual(fetched_blocks, [16, 17, 18, 19, 20])

    def test_global_returns_stale_only_for_trusted_chain_cache_when_rpc_fails(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        cached = trusted_global_cache(9000000, [{"address": wallet, "blocks": 1}])

        def fake_read_json_file(path: pathlib.Path, fallback: object) -> object:
            if path == pool_ops.GLOBAL_CACHE_FILE:
                return cached
            return fallback

        pool_ops.read_json_file = fake_read_json_file
        pool_ops.read_global_history = lambda limit=None: [cached]
        pool_ops.seconds_since_epoch = lambda: 999_999
        pool_ops.global_chain_rpc_urls = lambda: [("bad-node", "http://127.0.0.1:38131")]
        pool_ops.background_maintenance_decision = lambda task: {"allowed": True, "task": task, "reasons": []}
        pool_ops.mining_rpc_call = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("rpc unavailable"))

        payload = pool_ops.collect_global_blockchain()

        self.assertEqual(payload["status"], "stale")
        self.assertTrue(payload["cache_hit"])
        self.assertEqual(payload["latest_block"], 9000000)
        self.assertEqual(payload["clusters"][0]["address"], wallet)

    def test_global_rejects_trusted_cache_that_is_ahead_of_live_chain_tip(self) -> None:
        cached = trusted_global_cache(
            101,
            [{"address": "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc", "blocks": 1}],
        )

        def fake_read_json_file(path: pathlib.Path, fallback: object) -> object:
            if path == pool_ops.GLOBAL_CACHE_FILE:
                return cached
            return fallback

        pool_ops.read_json_file = fake_read_json_file
        pool_ops.read_global_history = lambda limit=None: [cached]
        pool_ops.seconds_since_epoch = lambda: 120
        pool_ops.global_chain_rpc_urls = lambda: [("chain", "http://chain-rpc")]
        pool_ops.public_evm_rpc_urls = lambda: []
        pool_ops.background_maintenance_decision = lambda task: {"allowed": True, "task": task, "reasons": []}

        def fake_mining_rpc(_url: str, method: str, _params: list[object], timeout: float = 0) -> object:
            if method == "getBlockCount":
                return 100
            raise RuntimeError("scan disabled in test")

        pool_ops.mining_rpc_call = fake_mining_rpc

        payload = pool_ops.collect_global_blockchain()

        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["clusters"], [])
        self.assertIn("ahead of live chain tip", payload["fetch_errors"][0])

    def test_global_cache_one_block_behind_is_not_returned_as_ok(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        cached = trusted_global_cache(100, [{"address": wallet, "blocks": 1}])

        def fake_read_json_file(path: pathlib.Path, fallback: object) -> object:
            if path == pool_ops.GLOBAL_CACHE_FILE:
                return cached
            return fallback

        pool_ops.read_json_file = fake_read_json_file
        pool_ops.read_global_history = lambda limit=None: [cached]
        pool_ops.seconds_since_epoch = lambda: 120
        pool_ops.global_chain_rpc_urls = lambda: [("chain", "http://chain-rpc")]
        pool_ops.background_maintenance_decision = lambda task: {"allowed": True, "task": task, "reasons": []}

        def fake_mining_rpc(_url: str, method: str, _params: list[object], timeout: float = 0) -> object:
            if method == "getBlockCount":
                return 101
            raise RuntimeError("scan unavailable")

        pool_ops.mining_rpc_call = fake_mining_rpc

        payload = pool_ops.collect_global_blockchain()

        self.assertEqual(payload["status"], "stale")
        self.assertTrue(payload["cache_hit"])
        self.assertEqual(payload["latest_block"], 101)
        self.assertEqual(payload["scan_end_block"], 100)
        self.assertEqual(payload["chain_tip_lag_blocks"], 1)
        self.assertIn("tip lag 1", payload["fetch_errors"][0])

    def test_global_live_head_promotes_latest_block_without_losing_scan_window(self) -> None:
        old_probe = pool_ops.probe_global_chain_block_count
        old_display_probe = pool_ops.probe_global_display_block_height
        self.addCleanup(lambda: setattr(pool_ops, "probe_global_chain_block_count", old_probe))
        self.addCleanup(lambda: setattr(pool_ops, "probe_global_display_block_height", old_display_probe))
        pool_ops.probe_global_chain_block_count = lambda: (150, "chain", "http://chain-rpc", [])
        pool_ops.probe_global_display_block_height = lambda: (
            120,
            "chain:getBlockTemplate",
            {"template_height": 120},
            [],
        )

        payload = pool_ops.refresh_global_chain_head(
            {
                "status": "ok",
                "latest_block": 100,
                "chain_block_count": 100,
                "scan_end_block": 99,
                "clusters": [{"address": "0xabc", "blocks": 1}],
            }
        )

        self.assertEqual(payload["latest_block"], 120)
        self.assertEqual(payload["display_latest_block"], 120)
        self.assertEqual(payload["chain_latest_block"], 120)
        self.assertEqual(payload["chain_block_count"], 150)
        self.assertEqual(payload["scan_end_block"], 99)
        self.assertEqual(payload["chain_tip_lag_blocks"], 51)

    def test_global_live_head_keeps_evm_fallback_height_domain(self) -> None:
        old_probe = pool_ops.probe_global_chain_block_count
        old_display_probe = pool_ops.probe_global_display_block_height
        self.addCleanup(lambda: setattr(pool_ops, "probe_global_chain_block_count", old_probe))
        self.addCleanup(lambda: setattr(pool_ops, "probe_global_display_block_height", old_display_probe))
        pool_ops.probe_global_chain_block_count = lambda: (11066032, "chain", "http://chain-rpc", [])
        pool_ops.probe_global_display_block_height = lambda: (
            8546225,
            "chain:getBlockTemplate",
            {"template_height": 8546225},
            [],
        )

        payload = pool_ops.refresh_global_chain_head(
            {
                "status": "degraded",
                "source_contract": "evm-rpc-fallback-v1",
                "rpc_source": "public-evm",
                "latest_block": 10760132,
                "chain_block_count": 10760132,
                "evm_latest_block": 10760132,
                "scan_end_block": 10760132,
                "clusters": [{"address": "0xabc", "blocks": 1}],
            }
        )

        self.assertEqual(payload["latest_block"], 10760132)
        self.assertEqual(payload["display_latest_block"], 10760132)
        self.assertEqual(payload["chain_latest_block"], 10760132)
        self.assertEqual(payload["chain_block_count"], 10760132)
        self.assertEqual(payload["native_chain_block_count"], 11066032)
        self.assertEqual(payload["native_display_latest_block"], 8546225)
        self.assertEqual(payload["chain_tip_lag_blocks"], 0)


class GlobalHistoryWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_env = os.environ.copy()
        self.originals = {
            name: getattr(pool_ops, name)
            for name in (
                "GLOBAL_HISTORY_FILE",
                "GLOBAL_HISTORY_STATE_FILE",
                "GLOBAL_HISTORY_LIMIT",
                "GLOBAL_HISTORY_COMPACT_MULTIPLIER",
                "DASHBOARD_HISTORY_DISK_DIR",
                "ensure_runtime",
            )
        }
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        for name, value in self.originals.items():
            setattr(pool_ops, name, value)
        os.environ.clear()
        os.environ.update(self.old_env)

    def test_global_history_appends_and_compacts_only_after_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            pool_ops.GLOBAL_HISTORY_FILE = root / "global-history.jsonl"
            pool_ops.GLOBAL_HISTORY_STATE_FILE = root / "global-history-state.json"
            pool_ops.GLOBAL_HISTORY_LIMIT = 3
            pool_ops.GLOBAL_HISTORY_COMPACT_MULTIPLIER = 2
            pool_ops.DASHBOARD_HISTORY_DISK_DIR = root / "dashboard-history"
            os.environ["BDAG_DASHBOARD_HISTORY_RAM_DIR"] = str(root / "dashboard-history-ram")
            pool_ops.ensure_runtime = lambda: None

            for block in range(6):
                pool_ops.record_global_snapshot({"latest_block": block})

            self.assertEqual(len(pool_ops.GLOBAL_HISTORY_FILE.read_text(encoding="utf-8").splitlines()), 6)
            self.assertEqual([row["latest_block"] for row in pool_ops.read_global_history()], [0, 1, 2, 3, 4, 5])

            pool_ops.record_global_snapshot({"latest_block": 6})

            self.assertEqual([row["latest_block"] for row in pool_ops.read_global_history()], [4, 5, 6])
            state = pool_ops.read_json_file(pool_ops.GLOBAL_HISTORY_STATE_FILE, {})
            self.assertEqual(state["row_count"], 3)
            self.assertTrue(state["compacted"])


class GlobalChainRpcCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            name: getattr(pool_ops, name)
            for name in (
                "GLOBAL_BLOCK_WINDOW",
                "read_json_file",
                "read_global_history",
                "seconds_since_epoch",
                "global_chain_rpc_urls",
                "global_evm_rpc_urls",
                "json_rpc_call",
                "mining_rpc_call",
                "background_maintenance_decision",
                "adaptive_worker_count",
                "pool_db_json",
                "fetch_cmc_price",
                "collect_peer_location_guess",
                "record_global_snapshot",
                "write_json_file",
            )
        }
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        for name, value in self.originals.items():
            setattr(pool_ops, name, value)

    def test_global_stats_use_chain_order_and_exclude_zero_address_cluster(self) -> None:
        wallet_a = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc".lower()
        wallet_b = "0x05518e03e148c56e426ff9e1cbdb962b4fc5250a".lower()
        hashes = {
            0: "0x" + "01" * 32,
            1: "0x" + "02" * 32,
            2: "0x" + "03" * 32,
            3: "0x" + "04" * 32,
        }
        coinbase = {
            hashes[0]: wallet_a,
            hashes[1]: wallet_a,
            hashes[2]: wallet_b,
            hashes[3]: pool_ops.ZERO_ETH_ADDRESS,
        }
        calls: list[str] = []

        pool_ops.GLOBAL_BLOCK_WINDOW = 4
        pool_ops.read_json_file = lambda _path, fallback: fallback
        pool_ops.read_global_history = lambda limit=None: []
        pool_ops.global_chain_rpc_urls = lambda: [("chain", "http://chain-rpc")]
        pool_ops.global_evm_rpc_urls = lambda: (_ for _ in ()).throw(AssertionError("Global mining stats must not use EVM RPC discovery"))
        pool_ops.json_rpc_call = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Global mining stats must not use eth_* RPC"))
        pool_ops.background_maintenance_decision = lambda task: {"allowed": True, "task": task, "reasons": []}
        pool_ops.adaptive_worker_count = lambda *_args, **_kwargs: 1
        pool_ops.pool_db_json = lambda _sql: []
        pool_ops.fetch_cmc_price = lambda: {"status": "failed"}
        pool_ops.collect_peer_location_guess = lambda: {"location": "Unknown", "location_confidence": "n/a", "observations": []}
        pool_ops.record_global_snapshot = lambda _snapshot: None
        pool_ops.write_json_file = lambda *_args, **_kwargs: None

        def fake_mining_rpc(_url: str, method: str, params: list[object], timeout: float = 0) -> object:
            calls.append(method)
            if method == "getBlockCount":
                return 4
            if method == "getBlockTotal":
                return 3
            if method == "getBlockByOrder":
                order = int(params[0])
                if order == -1:
                    order = 3
                return {"order": order, "hash": hashes[order], "timestamp": 1_780_000_000 + order}
            if method == "getBlockHeader":
                order = {value: key for key, value in hashes.items()}[str(params[0])]
                return {"time": 1_780_000_000 + order, "reward": 26_000_000_000}
            if method == "getCoinbaseAddress":
                return coinbase[str(params[0])]
            raise AssertionError(f"unexpected RPC method {method}")

        pool_ops.mining_rpc_call = fake_mining_rpc

        payload = pool_ops.collect_global_blockchain()

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["source_truth"], pool_ops.GLOBAL_STATS_SOURCE_TRUTH)
        self.assertEqual(payload["height_method"], "getBlockCount")
        self.assertEqual(payload["chain_block_count"], 4)
        self.assertEqual(payload["latest_order"], 3)
        self.assertEqual(payload["latest_order_method"], "getBlockByOrder(-1)")
        self.assertEqual(payload["scan_start_order"], 0)
        self.assertEqual(payload["scan_end_order"], 3)
        self.assertEqual(payload["fetched_blocks"], 4)
        self.assertEqual(payload["unique_miners"], 2)
        self.assertEqual(payload["zero_address_blocks"], 1)
        self.assertEqual(payload["attributed_blocks"], 3)
        self.assertNotIn(pool_ops.ZERO_ETH_ADDRESS, {row["address"] for row in payload["clusters"]})
        self.assertEqual(payload["clusters"][0]["address"], wallet_a)
        self.assertEqual(payload["clusters"][0]["blocks"], 2)
        self.assertEqual(payload["clusters"][0]["estimated_bdag"], "520.00")
        self.assertIn("getBlockCount", calls)
        self.assertIn("getBlockByOrder", calls)
        self.assertNotIn("getBlockhashByRange", calls)
        self.assertNotIn("eth_blockNumber", calls)

    def test_global_scan_uses_order_tip_not_block_count_tip(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc".lower()
        cached_evm = {
            "status": "degraded",
            "source_contract": "evm-rpc-fallback-v1",
            "updated_at_epoch": 100,
            "latest_block": 999,
            "requested_blocks": 64,
            "fetched_blocks": 64,
            "clusters": [{"address": wallet, "blocks": 64}],
        }
        hashes = {
            2: "0x" + "32" * 32,
            3: "0x" + "33" * 32,
        }
        requested_orders: list[int] = []

        pool_ops.GLOBAL_BLOCK_WINDOW = 2
        pool_ops.read_json_file = lambda _path, fallback: cached_evm
        pool_ops.read_global_history = lambda limit=None: []
        pool_ops.seconds_since_epoch = lambda: 120
        pool_ops.global_chain_rpc_urls = lambda: [("chain", "http://chain-rpc")]
        pool_ops.global_evm_rpc_urls = lambda: (_ for _ in ()).throw(AssertionError("fresh EVM fallback cache must not bypass chain RPC"))
        pool_ops.json_rpc_call = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Global mining stats must not use eth_* RPC"))
        pool_ops.background_maintenance_decision = lambda task: {"allowed": True, "task": task, "reasons": []}
        pool_ops.adaptive_worker_count = lambda *_args, **_kwargs: 1
        pool_ops.pool_db_json = lambda _sql: []
        pool_ops.fetch_cmc_price = lambda: {"status": "failed"}
        pool_ops.collect_peer_location_guess = lambda: {"location": "Unknown", "location_confidence": "n/a", "observations": []}
        pool_ops.record_global_snapshot = lambda _snapshot: None
        pool_ops.write_json_file = lambda *_args, **_kwargs: None

        def fake_mining_rpc(_url: str, method: str, params: list[object], timeout: float = 0) -> object:
            if method == "getBlockCount":
                return 100
            if method == "getBlockTotal":
                return 3
            if method == "getBlockByOrder":
                order = int(params[0])
                if order == -1:
                    order = 3
                else:
                    requested_orders.append(order)
                if order >= 4:
                    raise RuntimeError("Order is too big")
                return {"order": order, "hash": hashes[order], "timestamp": 1_780_000_000 + order}
            if method == "getBlockHeader":
                order = {value: key for key, value in hashes.items()}[str(params[0])]
                return {"time": 1_780_000_000 + order, "reward": 26_000_000_000}
            if method == "getCoinbaseAddress":
                return wallet
            raise AssertionError(f"unexpected RPC method {method}")

        pool_ops.mining_rpc_call = fake_mining_rpc

        payload = pool_ops.collect_global_blockchain()

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["source_truth"], pool_ops.GLOBAL_STATS_SOURCE_TRUTH)
        self.assertEqual(payload["chain_block_count"], 100)
        self.assertEqual(payload["latest_order"], 3)
        self.assertEqual(payload["latest_order_method"], "getBlockByOrder(-1)")
        self.assertEqual(payload["scan_start_order"], 2)
        self.assertEqual(payload["scan_end_order"], 3)
        self.assertEqual(sorted(requested_orders), [2, 3])

    def test_global_order_tip_falls_back_to_next_rpc_candidate(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc".lower()
        hashes = {
            2: "0x" + "62" * 32,
            3: "0x" + "63" * 32,
        }
        order_tip_urls: list[str] = []

        pool_ops.GLOBAL_BLOCK_WINDOW = 2
        pool_ops.read_json_file = lambda _path, fallback: fallback
        pool_ops.read_global_history = lambda limit=None: []
        pool_ops.global_chain_rpc_urls = lambda: [("bad", "http://bad-rpc"), ("good", "http://good-rpc")]
        pool_ops.global_evm_rpc_urls = lambda: (_ for _ in ()).throw(AssertionError("Global mining stats must not use EVM RPC discovery"))
        pool_ops.json_rpc_call = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Global mining stats must not use eth_* RPC"))
        pool_ops.background_maintenance_decision = lambda task: {"allowed": True, "task": task, "reasons": []}
        pool_ops.adaptive_worker_count = lambda *_args, **_kwargs: 1
        pool_ops.pool_db_json = lambda _sql: []
        pool_ops.fetch_cmc_price = lambda: {"status": "failed"}
        pool_ops.collect_peer_location_guess = lambda: {"location": "Unknown", "location_confidence": "n/a", "observations": []}
        pool_ops.record_global_snapshot = lambda _snapshot: None
        pool_ops.write_json_file = lambda *_args, **_kwargs: None

        def fake_mining_rpc(url: str, method: str, params: list[object], timeout: float = 0) -> object:
            if method == "getBlockCount":
                return 4
            if method == "getBlockByOrder":
                order_tip_urls.append(url)
                if url == "http://bad-rpc":
                    raise RuntimeError("remote chain-order proxy refused")
                order = int(params[0])
                if order == -1:
                    order = 3
                return {"order": order, "hash": hashes[order], "timestamp": 1_780_000_000 + order}
            if method == "getBlockTotal":
                if url == "http://bad-rpc":
                    raise RuntimeError("remote chain-order proxy refused")
                return 3
            if method == "getBlockHeader":
                order = {value: key for key, value in hashes.items()}[str(params[0])]
                return {"time": 1_780_000_000 + order, "reward": 26_000_000_000}
            if method == "getCoinbaseAddress":
                return wallet
            raise AssertionError(f"unexpected RPC method {method}")

        pool_ops.mining_rpc_call = fake_mining_rpc

        payload = pool_ops.collect_global_blockchain()

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["rpc_source"], "good")
        self.assertEqual(payload["latest_order"], 3)
        self.assertEqual(payload["fetched_blocks"], 2)
        self.assertIn("http://bad-rpc", order_tip_urls)
        self.assertIn("http://good-rpc", order_tip_urls)
        self.assertIn("bad:", payload["rpc_order_probe_errors"][0])

    def test_global_cache_validation_uses_scan_tip_not_refreshed_head_count(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc".lower()
        cached = trusted_global_cache(
            100,
            [{"address": "0xfeed00000000000000000000000000000000feed", "blocks": 2}],
        )
        cached["chain_block_count"] = 100
        cached["latest_order"] = 99
        cached["scan_end_order"] = 3
        cached["scan_end_block"] = 3
        hashes = {
            4: "0x" + "44" * 32,
            5: "0x" + "55" * 32,
        }
        requested_orders: list[int] = []

        pool_ops.GLOBAL_BLOCK_WINDOW = 2
        pool_ops.read_json_file = lambda path, fallback: cached if path == pool_ops.GLOBAL_CACHE_FILE else fallback
        pool_ops.read_global_history = lambda limit=None: []
        pool_ops.seconds_since_epoch = lambda: 120
        pool_ops.global_chain_rpc_urls = lambda: [("chain", "http://chain-rpc")]
        pool_ops.global_evm_rpc_urls = lambda: (_ for _ in ()).throw(AssertionError("fresh chain cache must not use EVM RPC"))
        pool_ops.json_rpc_call = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Global mining stats must not use eth_* RPC"))
        pool_ops.background_maintenance_decision = lambda task: {"allowed": True, "task": task, "reasons": []}
        pool_ops.adaptive_worker_count = lambda *_args, **_kwargs: 1
        pool_ops.pool_db_json = lambda _sql: []
        pool_ops.fetch_cmc_price = lambda: {"status": "failed"}
        pool_ops.collect_peer_location_guess = lambda: {"location": "Unknown", "location_confidence": "n/a", "observations": []}
        pool_ops.record_global_snapshot = lambda _snapshot: None
        pool_ops.write_json_file = lambda *_args, **_kwargs: None

        def fake_mining_rpc(_url: str, method: str, params: list[object], timeout: float = 0) -> object:
            if method == "getBlockCount":
                return 100
            if method == "getBlockTotal":
                return 5
            if method == "getBlockByOrder":
                order = int(params[0])
                if order == -1:
                    order = 5
                else:
                    requested_orders.append(order)
                return {"order": order, "hash": hashes[order], "timestamp": 1_780_000_000 + order}
            if method == "getBlockHeader":
                order = {value: key for key, value in hashes.items()}[str(params[0])]
                return {"time": 1_780_000_000 + order, "reward": 26_000_000_000}
            if method == "getCoinbaseAddress":
                return wallet
            raise AssertionError(f"unexpected RPC method {method}")

        pool_ops.mining_rpc_call = fake_mining_rpc

        payload = pool_ops.collect_global_blockchain()

        self.assertEqual(payload["status"], "ok")
        self.assertFalse(payload.get("cache_hit", False))
        self.assertEqual(payload["chain_block_count"], 100)
        self.assertEqual(payload["latest_order"], 5)
        self.assertEqual(payload["scan_start_order"], 4)
        self.assertEqual(payload["scan_end_order"], 5)
        self.assertEqual(payload["clusters"][0]["address"], wallet)
        self.assertEqual(sorted(requested_orders), [4, 5])

    def test_global_partial_scan_is_degraded_and_not_cached(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc".lower()
        hashes = {
            0: "0x" + "11" * 32,
            1: "0x" + "12" * 32,
            2: "0x" + "13" * 32,
            3: "0x" + "14" * 32,
        }
        recorded: list[dict[str, object]] = []
        cache_writes: list[object] = []

        pool_ops.GLOBAL_BLOCK_WINDOW = 4
        pool_ops.read_json_file = lambda _path, fallback: fallback
        pool_ops.read_global_history = lambda limit=None: []
        pool_ops.global_chain_rpc_urls = lambda: [("chain", "http://chain-rpc")]
        pool_ops.global_evm_rpc_urls = lambda: (_ for _ in ()).throw(AssertionError("Global mining stats must not use EVM RPC discovery"))
        pool_ops.json_rpc_call = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Global mining stats must not use eth_* RPC"))
        pool_ops.background_maintenance_decision = lambda task: {"allowed": True, "task": task, "reasons": []}
        pool_ops.adaptive_worker_count = lambda *_args, **_kwargs: 1
        pool_ops.pool_db_json = lambda _sql: []
        pool_ops.fetch_cmc_price = lambda: {"status": "failed"}
        pool_ops.collect_peer_location_guess = lambda: {"location": "Unknown", "location_confidence": "n/a", "observations": []}
        pool_ops.record_global_snapshot = lambda snapshot: recorded.append(snapshot)
        pool_ops.write_json_file = lambda *args, **_kwargs: cache_writes.append(args)

        def fake_mining_rpc(_url: str, method: str, params: list[object], timeout: float = 0) -> object:
            if method == "getBlockCount":
                return 4
            if method == "getBlockTotal":
                return 3
            if method == "getBlockByOrder":
                order = int(params[0])
                if order == -1:
                    order = 3
                if order == 2:
                    raise RuntimeError("temporary order fetch failure")
                return {"order": order, "hash": hashes[order], "timestamp": 1_780_000_000 + order}
            if method == "getBlockHeader":
                return {"time": 1_780_000_000, "reward": 26_000_000_000}
            if method == "getCoinbaseAddress":
                return wallet
            raise AssertionError(f"unexpected RPC method {method}")

        pool_ops.mining_rpc_call = fake_mining_rpc

        payload = pool_ops.collect_global_blockchain()

        self.assertEqual(payload["status"], "degraded")
        self.assertTrue(payload["partial_scan"])
        self.assertEqual(payload["requested_blocks"], 4)
        self.assertEqual(payload["fetched_blocks"], 3)
        self.assertEqual(payload["unknown_blocks"], 1)
        self.assertEqual(payload["clusters"][0]["share_percent"], "75.00")
        self.assertIn("partial", payload["error"])
        self.assertEqual(recorded, [])
        self.assertEqual(cache_writes, [])

    def test_global_missing_rewards_keep_credited_bdag_exact(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc".lower()
        hashes = {
            0: "0x" + "21" * 32,
            1: "0x" + "22" * 32,
        }

        pool_ops.GLOBAL_BLOCK_WINDOW = 2
        pool_ops.read_json_file = lambda _path, fallback: fallback
        pool_ops.read_global_history = lambda limit=None: []
        pool_ops.global_chain_rpc_urls = lambda: [("chain", "http://chain-rpc")]
        pool_ops.global_evm_rpc_urls = lambda: (_ for _ in ()).throw(AssertionError("Global mining stats must not use EVM RPC discovery"))
        pool_ops.json_rpc_call = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Global mining stats must not use eth_* RPC"))
        pool_ops.background_maintenance_decision = lambda task: {"allowed": True, "task": task, "reasons": []}
        pool_ops.adaptive_worker_count = lambda *_args, **_kwargs: 1
        pool_ops.pool_db_json = lambda _sql: []
        pool_ops.fetch_cmc_price = lambda: {"status": "failed"}
        pool_ops.collect_peer_location_guess = lambda: {"location": "Unknown", "location_confidence": "n/a", "observations": []}
        pool_ops.record_global_snapshot = lambda _snapshot: None
        pool_ops.write_json_file = lambda *_args, **_kwargs: None

        def fake_mining_rpc(_url: str, method: str, params: list[object], timeout: float = 0) -> object:
            if method == "getBlockCount":
                return 2
            if method == "getBlockTotal":
                return 1
            if method == "getBlockByOrder":
                order = int(params[0])
                if order == -1:
                    order = 1
                return {"order": order, "hash": hashes[order], "timestamp": 1_780_000_000 + order}
            if method == "getBlockHeader":
                order = {value: key for key, value in hashes.items()}[str(params[0])]
                payload = {"time": 1_780_000_000 + order}
                if order == 0:
                    payload["reward"] = 26_000_000_000
                return payload
            if method == "getCoinbaseAddress":
                return wallet
            raise AssertionError(f"unexpected RPC method {method}")

        pool_ops.mining_rpc_call = fake_mining_rpc

        payload = pool_ops.collect_global_blockchain()

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["clusters"][0]["credited_bdag"], "260.00")
        self.assertEqual(payload["clusters"][0]["known_reward_bdag"], "260.00")
        self.assertEqual(payload["clusters"][0]["estimated_bdag"], "520.00")
        self.assertTrue(payload["clusters"][0]["reward_estimated"])
        self.assertEqual(payload["clusters"][0]["reward_missing_blocks"], 1)


class EarningsEvmRpcSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_global_evm_rpc_urls = pool_ops.global_evm_rpc_urls
        self.old_node_rpc_endpoint = pool_ops.node_rpc_endpoint
        self.old_named_urls_from_env = pool_ops.named_urls_from_env
        self.old_json_rpc_balance = pool_ops.json_rpc_balance
        self.old_adaptive_worker_count = pool_ops.adaptive_worker_count
        self.old_local_evm_balance_probe_enabled = pool_ops.LOCAL_EVM_BALANCE_PROBE_ENABLED
        self.old_local_evm_balance_probe_pause = pool_ops.local_evm_balance_probe_pause
        pool_ops.LOCAL_EVM_BALANCE_PROBE_ENABLED = True
        pool_ops.local_evm_balance_probe_pause = lambda: {"paused": False, "reason": "", "reasons": []}
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        pool_ops.global_evm_rpc_urls = self.old_global_evm_rpc_urls
        pool_ops.node_rpc_endpoint = self.old_node_rpc_endpoint
        pool_ops.named_urls_from_env = self.old_named_urls_from_env
        pool_ops.json_rpc_balance = self.old_json_rpc_balance
        pool_ops.adaptive_worker_count = self.old_adaptive_worker_count
        pool_ops.LOCAL_EVM_BALANCE_PROBE_ENABLED = self.old_local_evm_balance_probe_enabled
        pool_ops.local_evm_balance_probe_pause = self.old_local_evm_balance_probe_pause

    def test_wallet_balances_use_evm_rpc_not_mining_rpc(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        called_urls: list[str] = []
        pool_ops.global_evm_rpc_urls = lambda: [("node-evm", "http://172.22.0.5:18545")]
        pool_ops.node_rpc_endpoint = lambda: ("node-mining", "http://127.0.0.1:38131")
        pool_ops.named_urls_from_env = lambda _name, _defaults: []
        pool_ops.adaptive_worker_count = lambda *_args, **_kwargs: 1

        def fake_balance(url: str, _address: str, timeout: float = 6.0) -> dict[str, str]:
            called_urls.append(url)
            return {"wei": "1000000000000000000", "bdag": "1.00"}

        pool_ops.json_rpc_balance = fake_balance

        payload = pool_ops.collect_wallet_balances_for_addresses([wallet])

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(called_urls, ["http://172.22.0.5:18545"])
        self.assertEqual(payload["addresses"][0]["type"], "evm-rpc")

    def test_wallet_balance_for_addresses_skips_local_evm_rpc_when_paused(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        called_urls: list[str] = []
        pool_ops.global_evm_rpc_urls = lambda: [("node-evm", "http://172.22.0.5:18545")]
        pool_ops.named_urls_from_env = lambda name, _defaults: (
            [("public-evm", "http://public-evm")] if name == "BDAG_PUBLIC_RPC_URLS" else []
        )
        pool_ops.local_evm_balance_probe_pause = lambda: {
            "paused": True,
            "reason": "node is syncing",
            "reasons": ["node is syncing"],
        }
        pool_ops.adaptive_worker_count = lambda *_args, **_kwargs: 1

        def fake_balance(url: str, _address: str, timeout: float = 6.0) -> dict[str, str]:
            called_urls.append(url)
            return {"wei": "1000000000000000000", "bdag": "1.00"}

        pool_ops.json_rpc_balance = fake_balance

        payload = pool_ops.collect_wallet_balances_for_addresses([wallet])

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(called_urls, ["http://public-evm"])
        self.assertEqual(payload["addresses"][0]["type"], "public-rpc")
        self.assertTrue(payload["local_evm_rpc"]["paused"])

    def test_wallet_balance_for_addresses_skips_local_evm_rpc_when_disabled(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        called_urls: list[str] = []
        pool_ops.LOCAL_EVM_BALANCE_PROBE_ENABLED = False
        pool_ops.global_evm_rpc_urls = lambda: (_ for _ in ()).throw(
            AssertionError("disabled local balance probes must not discover the node EVM RPC")
        )
        pool_ops.local_evm_balance_probe_pause = self.old_local_evm_balance_probe_pause
        pool_ops.named_urls_from_env = lambda name, _defaults: (
            [("public-evm", "http://public-evm")] if name == "BDAG_PUBLIC_RPC_URLS" else []
        )
        pool_ops.adaptive_worker_count = lambda *_args, **_kwargs: 1

        def fake_balance(url: str, _address: str, timeout: float = 6.0) -> dict[str, str]:
            called_urls.append(url)
            return {"wei": "1000000000000000000", "bdag": "1.00"}

        pool_ops.json_rpc_balance = fake_balance

        payload = pool_ops.collect_wallet_balances_for_addresses([wallet])

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(called_urls, ["http://public-evm"])
        self.assertEqual(payload["addresses"][0]["type"], "public-rpc")
        self.assertTrue(payload["local_evm_rpc"]["paused"])
        self.assertEqual(payload["local_evm_rpc"]["reason"], "local EVM balance probes are disabled")

    def test_wallet_cross_check_marks_local_evm_rpc_skipped_when_paused(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        called_urls: list[str] = []
        pool_ops.global_evm_rpc_urls = lambda: [("node-evm", "http://172.22.0.5:18545")]
        pool_ops.named_urls_from_env = lambda name, _defaults: (
            [("public-evm", "http://public-evm")] if name == "BDAG_PUBLIC_RPC_URLS" else []
        )
        pool_ops.local_evm_balance_probe_pause = lambda: {
            "paused": True,
            "reason": "chain-state restore candidate is under observation",
            "reasons": ["chain-state restore candidate is under observation"],
        }

        def fake_balance(url: str, _address: str, timeout: float = 6.0) -> dict[str, str]:
            called_urls.append(url)
            return {"wei": "1000000000000000000", "bdag": "1.00"}

        pool_ops.json_rpc_balance = fake_balance

        payload = pool_ops.collect_wallet_balances(wallet)

        self.assertEqual(called_urls, ["http://public-evm"])
        self.assertTrue(payload["local_evm_rpc"]["paused"])
        self.assertEqual(payload["sources"][0]["source"], "node-evm")
        self.assertEqual(payload["sources"][0]["status"], "skipped")
        self.assertEqual(payload["sources"][1]["type"], "public-rpc")


class GlobalLocalPoolOverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_pool_db_json = pool_ops.pool_db_json
        self.old_read_miner_registry = pool_ops.read_miner_registry
        self.old_read_global_pool_labels = pool_ops.read_global_pool_labels
        self.old_nodes = pool_ops.NODES
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        pool_ops.pool_db_json = self.old_pool_db_json
        pool_ops.read_miner_registry = self.old_read_miner_registry
        pool_ops.read_global_pool_labels = self.old_read_global_pool_labels
        pool_ops.NODES = self.old_nodes

    def test_local_pool_credit_row_uses_single_asic_worker_identity(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        pool_ops.NODES = ["node"]
        pool_ops.pool_db_json = lambda _sql: [
            {
                "miner_address": wallet,
                "credit_count": 7,
                "found_blocks": 7,
                "total_wei": "70000000000000000000",
                "first_seen_at": "2026-05-27T00:00:00Z",
                "last_seen_at": "2026-05-27T00:01:00Z",
            }
        ]
        pool_ops.read_miner_registry = lambda: {
            "miners": [
                {
                    "ip": "192.168.1.107",
                    "mac": "28:e2:97:1e:c0:b5",
                    "display_name": "Achilles",
                    "device_type": "asic",
                    "last_workers": [wallet],
                    "last_shares_window": 11,
                    "last_configured_ok": True,
                    "last_share_epoch": 200,
                }
            ]
        }

        rows = pool_ops.collect_local_pool_global_clusters(
            scan_window_seconds=120,
            total_global_blocks=100,
            scan_window_hours=Decimal("0.0333333333"),
            price={"status": "ok", "usd": "0.01", "zar": "0.18"},
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["address"], wallet)
        self.assertEqual(rows[0]["pool_name"], "Achilles-0b5")
        self.assertEqual(rows[0]["nodes"], ["node"])
        self.assertTrue(rows[0]["local_pool"])
        self.assertEqual(rows[0]["shares"], 7)
        self.assertEqual(rows[0]["credit_blocks"], 7)
        self.assertEqual(rows[0]["found_blocks"], 7)
        self.assertEqual(rows[0]["credited_bdag"], "70.00")

    def test_local_pool_shared_worker_uses_pool_aggregate_identity(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        captured_sql = {}
        pool_ops.NODES = ["node"]

        def fake_pool_db_json(sql: str):
            captured_sql["sql"] = sql
            return [
                {
                    "miner_address": wallet.lower(),
                    "credit_count": 11,
                    "found_blocks": 11,
                    "total_wei": "110000000000000000000",
                    "first_seen_at": "2026-05-27T00:00:00Z",
                    "last_seen_at": "2026-05-27T00:01:00Z",
                }
            ]

        pool_ops.pool_db_json = fake_pool_db_json
        pool_ops.read_miner_registry = lambda: {
            "miners": [
                {
                    "ip": "192.168.1.107",
                    "mac": "28:e2:97:1e:c0:b5",
                    "device_type": "asic",
                    "last_workers": [wallet],
                    "last_shares_window": 11,
                    "last_configured_ok": True,
                    "last_share_epoch": 200,
                },
                {
                    "ip": "192.168.1.108",
                    "mac": "28:e2:97:1e:c0:b6",
                    "device_type": "asic",
                    "last_workers": [wallet.lower()],
                    "last_shares_window": 9,
                    "last_configured_ok": True,
                    "last_share_epoch": 201,
                },
            ]
        }

        rows = pool_ops.collect_local_pool_global_clusters(
            scan_window_seconds=120,
            total_global_blocks=100,
            scan_window_hours=Decimal("0.0333333333"),
            price={"status": "ok", "usd": "0.01", "zar": "0.18"},
        )

        self.assertIn("lower(c.miner_address) AS miner_address", captured_sql["sql"])
        self.assertIn("GROUP BY lower(c.miner_address)", captured_sql["sql"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["address"], wallet.lower())
        self.assertEqual(rows[0]["pool_name"], "Local pool (2 ASICs)")
        self.assertEqual(rows[0]["pool_label"], f"Local pool (2 ASICs) ({pool_ops.short_eth_address(wallet.lower())})")
        self.assertEqual(rows[0]["device_type"], "pool")
        self.assertEqual(rows[0]["mac"], "")
        self.assertEqual(rows[0]["identity_key"], "")
        self.assertEqual(rows[0]["local_asic_count"], 2)
        self.assertEqual(rows[0]["local_miner_count"], 2)
        self.assertEqual(rows[0]["local_macs"], ["28:e2:97:1e:c0:b5", "28:e2:97:1e:c0:b6"])
        self.assertEqual(rows[0]["credit_blocks"], 11)

    def test_local_pool_overlay_is_preserved_when_address_matches_chain_cluster(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        merged = pool_ops.merge_global_local_pool_clusters(
            [{"address": wallet, "blocks": 2, "credit_blocks": 2, "last_seen_at": "2026-05-27T00:01:00Z"}],
            [{
                "address": wallet,
                "blocks": 3,
                "pool_name": "Local pool (2 ASICs)",
                "local_pool": True,
                "credit_blocks": 3,
                "local_asic_count": 2,
            }],
        )

        self.assertEqual(len(merged), 1)
        self.assertTrue(merged[0]["local_pool"])
        self.assertEqual(merged[0]["pool_name"], "Local pool (2 ASICs)")
        self.assertEqual(merged[0]["blocks"], 2)
        self.assertEqual(merged[0]["credit_blocks"], 2)
        self.assertEqual(merged[0]["local_credit_blocks"], 3)
        self.assertEqual(merged[0]["local_asic_count"], 2)

    def test_local_pool_only_rows_do_not_create_global_chain_production(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        merged = pool_ops.merge_global_local_pool_clusters(
            [],
            [{"address": wallet, "blocks": 3, "pool_name": "Achilles-0b5", "local_pool": True, "credit_blocks": 3}],
        )

        self.assertEqual(merged, [])

    def test_local_pool_identity_is_not_replaced_by_static_global_label(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        pool_ops.read_global_pool_labels = lambda: {wallet.lower(): "Pipin"}

        payload = pool_ops.annotate_global_pool_labels(
            {
                "clusters": [
                    {
                        "address": wallet,
                        "pool_name": "Achilles-0b5",
                        "local_pool": True,
                    }
                ],
                "history": [],
            }
        )

        self.assertEqual(payload["clusters"][0]["pool_name"], "Achilles-0b5")
        self.assertEqual(payload["clusters"][0]["pool_label"], "Achilles-0b5 (0xA1Ee...7DFc)")


class GlobalMaintenanceBackoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_read_json_file = pool_ops.read_json_file
        self.old_read_global_history = pool_ops.read_global_history
        self.old_background_maintenance_decision = pool_ops.background_maintenance_decision
        self.old_global_chain_rpc_urls = pool_ops.global_chain_rpc_urls
        self.old_mining_rpc_call = pool_ops.mining_rpc_call
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        pool_ops.read_json_file = self.old_read_json_file
        pool_ops.read_global_history = self.old_read_global_history
        pool_ops.background_maintenance_decision = self.old_background_maintenance_decision
        pool_ops.global_chain_rpc_urls = self.old_global_chain_rpc_urls
        pool_ops.mining_rpc_call = self.old_mining_rpc_call

    def test_global_scan_defers_to_stale_cache_when_maintenance_backoff_blocks_work(self) -> None:
        cached = trusted_global_cache(
            123,
            [{"address": "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc", "blocks": 1}],
        )

        def fake_read_json_file(path: pathlib.Path, fallback: object) -> object:
            if path == pool_ops.GLOBAL_CACHE_FILE:
                return cached
            return fallback

        def should_not_fetch_rpc() -> list[tuple[str, str]]:
            raise AssertionError("global chain RPC discovery must not run while maintenance is deferred")

        pool_ops.read_json_file = fake_read_json_file
        pool_ops.read_global_history = lambda limit=None: [cached]
        pool_ops.background_maintenance_decision = lambda task: {
            "allowed": False,
            "task": task,
            "reasons": ["chain catch-up has priority status=syncing remaining=42 threshold=0"],
        }
        pool_ops.global_chain_rpc_urls = should_not_fetch_rpc

        payload = pool_ops.collect_global_blockchain()

        self.assertEqual(payload["status"], "stale")
        self.assertTrue(payload["maintenance_deferred"])
        self.assertIn("global blockchain scan deferred", payload["error"])
        self.assertEqual(payload["latest_block"], 123)

    def test_global_scan_returns_lightweight_head_when_deferred_without_cache(self) -> None:
        def fake_read_json_file(path: pathlib.Path, fallback: object) -> object:
            if path == pool_ops.GLOBAL_CACHE_FILE:
                return {}
            return fallback

        pool_ops.read_json_file = fake_read_json_file
        pool_ops.read_global_history = lambda limit=None: []
        pool_ops.background_maintenance_decision = lambda task: {
            "allowed": False,
            "task": task,
            "reasons": ["host io pressure avg10 28.00 >= 20.00"],
            "adaptive_concurrency": {"workers": {"global_rpc": 1}},
        }
        pool_ops.global_chain_rpc_urls = lambda: [("node", "http://127.0.0.1:38131")]
        pool_ops.mining_rpc_call = lambda _url, method, _params, timeout=0: 123 if method == "getBlockCount" else None

        payload = pool_ops.collect_global_blockchain()

        self.assertEqual(payload["status"], "deferred")
        self.assertTrue(payload["head_only"])
        self.assertTrue(payload["maintenance_deferred"])
        self.assertEqual(payload["latest_block"], 123)
        self.assertEqual(payload["latest_order"], 122)
        self.assertEqual(payload["height_method"], "getBlockCount")
        self.assertEqual(payload["fetched_blocks"], 0)
        self.assertEqual(payload["unique_miners"], 0)
        self.assertEqual(payload["clusters"], [])


if __name__ == "__main__":
    unittest.main()

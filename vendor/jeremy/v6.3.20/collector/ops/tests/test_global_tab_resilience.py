#!/usr/bin/env python3

import os
import pathlib
import sys
import unittest
from decimal import Decimal

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


class GlobalTabRpcSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_env = os.environ.copy()
        self.old_nodes = pool_ops.NODES
        self.old_services = pool_ops.SERVICES
        self.old_pool_containers = pool_ops.POOL_CONTAINERS
        self.old_docker_container_ip = pool_ops.docker_container_ip
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        os.environ.clear()
        os.environ.update(self.old_env)
        pool_ops.NODES = self.old_nodes
        pool_ops.SERVICES = self.old_services
        pool_ops.POOL_CONTAINERS = self.old_pool_containers
        pool_ops.docker_container_ip = self.old_docker_container_ip

    def test_global_uses_evm_rpc_even_when_node_rpc_is_mining_rpc(self) -> None:
        os.environ["BDAG_NODE_RPC_URLS"] = "node1=http://127.0.0.1:38131"
        for key in ("BDAG_GLOBAL_RPC_URLS", "BDAG_EVM_RPC_URLS", "WALLET_RPC_URLS"):
            os.environ.pop(key, None)
        pool_ops.NODES = ["bdag-miner-node-1"]
        pool_ops.SERVICES = ["bdag-miner-node-1"]
        pool_ops.POOL_CONTAINERS = []
        pool_ops.docker_container_ip = lambda name: "172.22.0.2" if name == "bdag-miner-node-1" else ""

        self.assertEqual(
            pool_ops.global_evm_rpc_urls(),
            [("bdag-miner-node-1", "http://172.22.0.2:18545")],
        )

    def test_global_rewrites_compose_service_hostname_for_host_dashboard(self) -> None:
        os.environ["BDAG_GLOBAL_RPC_URLS"] = "node1=http://bdag-miner-node-1:18545"
        pool_ops.NODES = ["bdag-miner-node-1"]
        pool_ops.SERVICES = ["bdag-miner-node-1"]
        pool_ops.POOL_CONTAINERS = []
        pool_ops.docker_container_ip = lambda name: "172.22.0.2" if name == "bdag-miner-node-1" else ""

        self.assertEqual(
            pool_ops.global_evm_rpc_urls(),
            [("node1", "http://172.22.0.2:18545")],
        )


class NodeMiningRpcCredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_env = os.environ.copy()
        self.old_read_env_file_value = pool_ops.read_env_file_value
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        os.environ.clear()
        os.environ.update(self.old_env)
        pool_ops.read_env_file_value = self.old_read_env_file_value

    def clear_rpc_env(self) -> None:
        for key in (
            "BDAG_NODE_MINING_RPC_USER",
            "BDAG_NODE_MINING_RPC_PASS",
            "BDAG_NODE_RPC_USER",
            "BDAG_NODE_RPC_PASS",
            "NODE_RPC_USER",
            "NODE_RPC_PASS",
        ):
            os.environ.pop(key, None)

    def test_node_mining_rpc_credentials_fall_back_to_stack_env_file(self) -> None:
        self.clear_rpc_env()
        values = {"NODE_RPC_USER": "stack-user", "NODE_RPC_PASS": "stack-pass"}
        pool_ops.read_env_file_value = lambda _path, name: values.get(name)

        self.assertEqual(pool_ops.node_mining_rpc_credentials(), ("stack-user", "stack-pass"))

    def test_node_mining_rpc_credentials_prefer_dashboard_specific_env(self) -> None:
        os.environ["BDAG_NODE_MINING_RPC_USER"] = "dashboard-user"
        os.environ["BDAG_NODE_MINING_RPC_PASS"] = "dashboard-pass"
        os.environ["NODE_RPC_USER"] = "stack-user"
        os.environ["NODE_RPC_PASS"] = "stack-pass"

        self.assertEqual(pool_ops.node_mining_rpc_credentials(), ("dashboard-user", "dashboard-pass"))


class GlobalTabFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_read_json_file = pool_ops.read_json_file
        self.old_read_global_history = pool_ops.read_global_history
        self.old_seconds_since_epoch = pool_ops.seconds_since_epoch
        self.old_global_evm_rpc_urls = pool_ops.global_evm_rpc_urls
        self.old_json_rpc_call = pool_ops.json_rpc_call
        self.old_background_maintenance_decision = pool_ops.background_maintenance_decision
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        pool_ops.read_json_file = self.old_read_json_file
        pool_ops.read_global_history = self.old_read_global_history
        pool_ops.seconds_since_epoch = self.old_seconds_since_epoch
        pool_ops.global_evm_rpc_urls = self.old_global_evm_rpc_urls
        pool_ops.json_rpc_call = self.old_json_rpc_call
        pool_ops.background_maintenance_decision = self.old_background_maintenance_decision

    def test_global_returns_stale_cache_instead_of_raising_when_evm_rpc_fails(self) -> None:
        cached = {
            "status": "ok",
            "updated_at_epoch": 100,
            "latest_block": 123,
            "avg_block_seconds": "0.5",
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
        pool_ops.global_evm_rpc_urls = lambda: [("bad-node", "http://127.0.0.1:18545")]
        pool_ops.background_maintenance_decision = lambda _name: {"allowed": True, "reasons": []}

        def fail_rpc(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("rpc unavailable")

        pool_ops.json_rpc_call = fail_rpc

        payload = pool_ops.collect_global_blockchain()

        self.assertEqual(payload["status"], "stale")
        self.assertTrue(payload["cache_hit"])
        self.assertEqual(payload["latest_block"], 123)
        self.assertEqual(payload["avg_blocks_per_second"], "2.000")
        self.assertEqual(payload["max_avg_block_transactions_per_second"], "209716.00")
        self.assertEqual(payload["clusters"][0]["address"], cached["clusters"][0]["address"])
        self.assertEqual(payload["clusters"][0]["blocks"], cached["clusters"][0]["blocks"])
        self.assertIn("unable to fetch latest global block height", payload["error"])


class GlobalMaintenanceBackoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_global_deferred_block_window = pool_ops.GLOBAL_DEFERRED_BLOCK_WINDOW
        self.old_global_deferred_rpc_workers = pool_ops.GLOBAL_DEFERRED_RPC_WORKERS
        self.old_read_json_file = pool_ops.read_json_file
        self.old_write_json_file = pool_ops.write_json_file
        self.old_read_global_history = pool_ops.read_global_history
        self.old_record_global_snapshot = pool_ops.record_global_snapshot
        self.old_background_maintenance_decision = pool_ops.background_maintenance_decision
        self.old_adaptive_worker_count = pool_ops.adaptive_worker_count
        self.old_global_evm_rpc_urls = pool_ops.global_evm_rpc_urls
        self.old_json_rpc_call = pool_ops.json_rpc_call
        self.old_fetch_block_header = pool_ops.fetch_block_header
        self.old_pool_db_json = pool_ops.pool_db_json
        self.old_fetch_cmc_price = pool_ops.fetch_cmc_price
        self.old_collect_peer_location_guess = pool_ops.collect_peer_location_guess
        self.old_collect_local_pool_global_clusters = pool_ops.collect_local_pool_global_clusters
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        pool_ops.GLOBAL_DEFERRED_BLOCK_WINDOW = self.old_global_deferred_block_window
        pool_ops.GLOBAL_DEFERRED_RPC_WORKERS = self.old_global_deferred_rpc_workers
        pool_ops.read_json_file = self.old_read_json_file
        pool_ops.write_json_file = self.old_write_json_file
        pool_ops.read_global_history = self.old_read_global_history
        pool_ops.record_global_snapshot = self.old_record_global_snapshot
        pool_ops.background_maintenance_decision = self.old_background_maintenance_decision
        pool_ops.adaptive_worker_count = self.old_adaptive_worker_count
        pool_ops.global_evm_rpc_urls = self.old_global_evm_rpc_urls
        pool_ops.json_rpc_call = self.old_json_rpc_call
        pool_ops.fetch_block_header = self.old_fetch_block_header
        pool_ops.pool_db_json = self.old_pool_db_json
        pool_ops.fetch_cmc_price = self.old_fetch_cmc_price
        pool_ops.collect_peer_location_guess = self.old_collect_peer_location_guess
        pool_ops.collect_local_pool_global_clusters = self.old_collect_local_pool_global_clusters

    def setup_deferred_scan(self) -> list[dict[str, object]]:
        snapshots: list[dict[str, object]] = []
        pool_ops.read_json_file = lambda _path, fallback: fallback
        pool_ops.write_json_file = lambda *_args, **_kwargs: None
        pool_ops.read_global_history = lambda limit=None: []
        pool_ops.record_global_snapshot = lambda snapshot: snapshots.append(snapshot)
        pool_ops.background_maintenance_decision = lambda _name: {
            "allowed": False,
            "reasons": ["host io pressure avg10 42.00 >= 20.00"],
            "io_some_avg10": 42.0,
        }
        pool_ops.adaptive_worker_count = lambda _kind, configured, item_count, _pressure=None: min(configured, item_count)
        pool_ops.global_evm_rpc_urls = lambda: [("test-rpc", "http://127.0.0.1:18545")]
        pool_ops.json_rpc_call = lambda *_args, **_kwargs: "0x7f"
        pool_ops.fetch_block_header = lambda _url, block_number, timeout=8.0: {
            "number": hex(block_number),
            "timestamp": hex(1_780_000_000 + block_number),
            "miner": "0xa1ee1005c4ff181e93e717d2c624554b66ab7dfc",
        }
        pool_ops.pool_db_json = lambda _sql: {
            "block_count": 128,
            "avg_reward_wei": "10000000000000000000",
            "total_reward_wei": "1280000000000000000000",
        }
        pool_ops.fetch_cmc_price = lambda: {"status": "ok", "usd": "0.01", "zar": "0.18"}
        pool_ops.collect_peer_location_guess = lambda: {"observations": []}
        pool_ops.collect_local_pool_global_clusters = lambda *_args, **_kwargs: []
        return snapshots

    def test_global_scan_uses_small_window_when_maintenance_is_deferred(self) -> None:
        snapshots = self.setup_deferred_scan()
        pool_ops.GLOBAL_DEFERRED_BLOCK_WINDOW = 4
        pool_ops.GLOBAL_DEFERRED_RPC_WORKERS = 2

        payload = pool_ops.collect_global_blockchain()

        self.assertEqual(payload["status"], "degraded")
        self.assertTrue(payload["maintenance_deferred"])
        self.assertTrue(payload["deferred_scan"])
        self.assertFalse(payload["head_only"])
        self.assertEqual(payload["latest_block"], 127)
        self.assertEqual(payload["requested_blocks"], 4)
        self.assertEqual(payload["fetched_blocks"], 4)
        self.assertEqual(payload["global_rpc_worker_count"], 2)
        self.assertEqual(snapshots[0]["requested_blocks"], 4)
        self.assertTrue(snapshots[0]["maintenance_deferred"])
        self.assertIn("reduced to 4 blocks", payload["error"])

    def test_global_scan_can_use_head_only_when_deferred_window_is_zero(self) -> None:
        snapshots = self.setup_deferred_scan()
        pool_ops.GLOBAL_DEFERRED_BLOCK_WINDOW = 0

        payload = pool_ops.collect_global_blockchain()

        self.assertEqual(payload["status"], "deferred")
        self.assertTrue(payload["maintenance_deferred"])
        self.assertTrue(payload["head_only"])
        self.assertEqual(payload["latest_block"], 127)
        self.assertEqual(payload["requested_blocks"], 0)
        self.assertEqual(payload["fetched_blocks"], 0)
        self.assertEqual(snapshots, [])


class GlobalTabRateMetricTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_read_json_file = pool_ops.read_json_file
        self.old_write_json_file = pool_ops.write_json_file
        self.old_read_global_history = pool_ops.read_global_history
        self.old_record_global_snapshot = pool_ops.record_global_snapshot
        self.old_background_maintenance_decision = pool_ops.background_maintenance_decision
        self.old_adaptive_worker_count = pool_ops.adaptive_worker_count
        self.old_global_evm_rpc_urls = pool_ops.global_evm_rpc_urls
        self.old_json_rpc_call = pool_ops.json_rpc_call
        self.old_fetch_block_header = pool_ops.fetch_block_header
        self.old_pool_db_json = pool_ops.pool_db_json
        self.old_fetch_cmc_price = pool_ops.fetch_cmc_price
        self.old_collect_peer_location_guess = pool_ops.collect_peer_location_guess
        self.old_collect_local_pool_global_clusters = pool_ops.collect_local_pool_global_clusters
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        pool_ops.read_json_file = self.old_read_json_file
        pool_ops.write_json_file = self.old_write_json_file
        pool_ops.read_global_history = self.old_read_global_history
        pool_ops.record_global_snapshot = self.old_record_global_snapshot
        pool_ops.background_maintenance_decision = self.old_background_maintenance_decision
        pool_ops.adaptive_worker_count = self.old_adaptive_worker_count
        pool_ops.global_evm_rpc_urls = self.old_global_evm_rpc_urls
        pool_ops.json_rpc_call = self.old_json_rpc_call
        pool_ops.fetch_block_header = self.old_fetch_block_header
        pool_ops.pool_db_json = self.old_pool_db_json
        pool_ops.fetch_cmc_price = self.old_fetch_cmc_price
        pool_ops.collect_peer_location_guess = self.old_collect_peer_location_guess
        pool_ops.collect_local_pool_global_clusters = self.old_collect_local_pool_global_clusters

    def test_global_payload_reports_blocks_per_second_and_max_tx_rate(self) -> None:
        wallet = "0xa1ee1005c4ff181e93e717d2c624554b66ab7dfc"
        snapshots: list[dict[str, object]] = []

        pool_ops.read_json_file = lambda _path, fallback: fallback
        pool_ops.write_json_file = lambda *_args, **_kwargs: None
        pool_ops.read_global_history = lambda limit=None: []
        pool_ops.record_global_snapshot = lambda snapshot: snapshots.append(snapshot)
        pool_ops.background_maintenance_decision = lambda _name: {"allowed": True}
        pool_ops.adaptive_worker_count = lambda *_args, **_kwargs: 1
        pool_ops.global_evm_rpc_urls = lambda: [("test-rpc", "http://127.0.0.1:18545")]
        pool_ops.json_rpc_call = lambda *_args, **_kwargs: "0x3"
        pool_ops.fetch_block_header = lambda _url, block_number, timeout=8.0: {
            "number": hex(block_number),
            "timestamp": hex(1_780_000_000 + block_number),
            "miner": wallet,
        }
        pool_ops.pool_db_json = lambda _sql: {
            "block_count": 4,
            "avg_reward_wei": "10000000000000000000",
            "total_reward_wei": "40000000000000000000",
        }
        pool_ops.fetch_cmc_price = lambda: {"status": "ok", "usd": "0.01", "zar": "0.18"}
        pool_ops.collect_peer_location_guess = lambda: {"observations": []}
        pool_ops.collect_local_pool_global_clusters = lambda *_args, **_kwargs: []

        payload = pool_ops.collect_global_blockchain()

        self.assertEqual(payload["avg_block_seconds"], "1.0")
        self.assertEqual(payload["avg_blocks_per_second"], "1.000")
        self.assertEqual(payload["max_transactions_per_block"], pool_ops.BDAG_MAX_TRANSACTIONS_PER_BLOCK)
        self.assertEqual(payload["max_avg_block_transactions_per_second"], "104858.00")
        self.assertEqual(snapshots[0]["avg_blocks_per_second"], "1.000")
        self.assertEqual(snapshots[0]["max_avg_block_transactions_per_second"], "104858.00")

    def test_pool_hourly_fiat_rates_do_not_round_small_totals_to_zero_first(self) -> None:
        _bdag_hour, usd_hour, zar_hour = pool_ops._pool_earning_rates_from_amount(
            Decimal("50.17"),
            {"usd": "0.000054", "zar": "0.000876"},
            Decimal("0.05"),
        )

        self.assertEqual(usd_hour, "0.054184")
        self.assertEqual(zar_hour, "0.878978")


class GlobalTabWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_global_block_window = pool_ops.GLOBAL_BLOCK_WINDOW
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        pool_ops.GLOBAL_BLOCK_WINDOW = self.old_global_block_window

    def test_default_global_window_matches_live_rate_plot_window(self) -> None:
        self.assertEqual(pool_ops.GLOBAL_BLOCK_WINDOW, 2048)

    def test_global_cache_rejects_payload_from_different_scan_window(self) -> None:
        pool_ops.GLOBAL_BLOCK_WINDOW = 2048

        self.assertFalse(
            pool_ops.global_cache_window_matches(
                {
                    "status": "ok",
                    "latest_block": 10_000,
                    "requested_blocks": 256,
                }
            )
        )
        self.assertTrue(
            pool_ops.global_cache_window_matches(
                {
                    "status": "ok",
                    "latest_block": 10_000,
                    "requested_blocks": 2048,
                }
            )
        )


class EarningsEvmRpcSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_global_evm_rpc_urls = pool_ops.global_evm_rpc_urls
        self.old_node_rpc_urls = pool_ops.node_rpc_urls
        self.old_named_urls_from_env = pool_ops.named_urls_from_env
        self.old_json_rpc_balance = pool_ops.json_rpc_balance
        self.old_adaptive_worker_count = pool_ops.adaptive_worker_count
        self.old_local_evm_balance_probe_enabled = pool_ops.LOCAL_EVM_BALANCE_PROBE_ENABLED
        pool_ops.LOCAL_EVM_BALANCE_PROBE_ENABLED = True
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        pool_ops.global_evm_rpc_urls = self.old_global_evm_rpc_urls
        pool_ops.node_rpc_urls = self.old_node_rpc_urls
        pool_ops.named_urls_from_env = self.old_named_urls_from_env
        pool_ops.json_rpc_balance = self.old_json_rpc_balance
        pool_ops.adaptive_worker_count = self.old_adaptive_worker_count
        pool_ops.LOCAL_EVM_BALANCE_PROBE_ENABLED = self.old_local_evm_balance_probe_enabled

    def test_wallet_balances_use_evm_rpc_not_mining_rpc(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        called_urls: list[str] = []
        pool_ops.global_evm_rpc_urls = lambda: [("node1-evm", "http://172.22.0.5:18545")]
        pool_ops.node_rpc_urls = lambda: [("node1-mining", "http://127.0.0.1:38131")]
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

    def test_wallet_balance_for_addresses_skips_local_evm_rpc_when_disabled(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        called_urls: list[str] = []
        pool_ops.LOCAL_EVM_BALANCE_PROBE_ENABLED = False
        pool_ops.global_evm_rpc_urls = lambda: (_ for _ in ()).throw(
            AssertionError("disabled local balance probes must not discover the node EVM RPC")
        )
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


class GlobalLocalPoolOverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_pool_db_json = pool_ops.pool_db_json
        self.old_read_miner_registry = pool_ops.read_miner_registry
        self.old_read_global_pool_labels = pool_ops.read_global_pool_labels
        self.old_read_status_sampler_payload = pool_ops.read_status_sampler_payload
        self.old_collect_miner_health = pool_ops.collect_miner_health
        self.old_nodes = pool_ops.NODES
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        pool_ops.pool_db_json = self.old_pool_db_json
        pool_ops.read_miner_registry = self.old_read_miner_registry
        pool_ops.read_global_pool_labels = self.old_read_global_pool_labels
        pool_ops.read_status_sampler_payload = self.old_read_status_sampler_payload
        pool_ops.collect_miner_health = self.old_collect_miner_health
        pool_ops.NODES = self.old_nodes

    def test_local_pool_credit_row_uses_asic_worker_identity(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        pool_ops.NODES = ["bdag-miner-node-1"]
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
                }
            ]
        }
        pool_ops.read_status_sampler_payload = lambda *_args, **_kwargs: None
        pool_ops.collect_miner_health = lambda: {
            "miners": [
                {"ip": "192.168.1.107", "workers": [wallet], "shares": 11},
                {"ip": "192.168.1.108", "workers": [wallet], "shares": 13},
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
        self.assertEqual(rows[0]["nodes"], ["bdag-miner-node-1"])
        self.assertTrue(rows[0]["local_pool"])
        self.assertEqual(rows[0]["shares"], 24)
        self.assertEqual(rows[0]["local_shares"], 24)
        self.assertEqual(rows[0]["miner_tab_shares"], 24)
        self.assertEqual(rows[0]["pool_credit_shares"], 7)
        self.assertEqual(rows[0]["shares_source_contract"], "miners-tab:miner_health.miners[].shares")
        self.assertEqual(rows[0]["credit_blocks"], 7)
        self.assertEqual(rows[0]["found_blocks"], 7)
        self.assertEqual(rows[0]["credited_bdag"], "70.00")

    def test_local_pool_overlay_is_preserved_when_address_matches_chain_cluster(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        merged = pool_ops.merge_global_local_pool_clusters(
            [{"address": wallet, "blocks": 2, "last_seen_at": "2026-05-27T00:01:00Z"}],
            [{"address": wallet, "blocks": 3, "pool_name": "Achilles-0b5", "local_pool": True, "credit_blocks": 3, "local_shares": 9}],
        )

        self.assertEqual(len(merged), 1)
        self.assertTrue(merged[0]["local_pool"])
        self.assertEqual(merged[0]["pool_name"], "Achilles-0b5")
        self.assertEqual(merged[0]["credit_blocks"], 3)
        self.assertEqual(merged[0]["local_shares"], 9)

    def test_cached_global_local_pool_row_gets_current_miner_tab_shares(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        pool_ops.read_status_sampler_payload = lambda *_args, **_kwargs: None
        pool_ops.collect_miner_health = lambda: {
            "miners": [
                {"workers": [wallet], "shares": 40},
                {"workers": [wallet], "shares": 2},
            ]
        }

        payload = pool_ops.apply_miner_tab_shares_to_global_payload(
            {
                "clusters": [
                    {
                        "address": wallet,
                        "local_pool": True,
                        "shares": 7,
                        "blocks": 3,
                    }
                ]
            }
        )

        self.assertEqual(payload["clusters"][0]["shares"], 42)
        self.assertEqual(payload["clusters"][0]["local_shares"], 42)
        self.assertEqual(payload["clusters"][0]["miner_tab_shares"], 42)
        self.assertEqual(payload["clusters"][0]["shares_source_contract"], "miners-tab:miner_health.miners[].shares")

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


if __name__ == "__main__":
    unittest.main()

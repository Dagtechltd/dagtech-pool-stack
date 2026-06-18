#!/usr/bin/env python3

import json
import os
import pathlib
import sys
import tempfile
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


class MinerRetirementIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.retirements_file = pathlib.Path(self.tmp.name) / "miner-retirements.json"
        self.old_retirements_file = pool_ops.MINER_RETIREMENTS_FILE
        pool_ops.MINER_RETIREMENTS_FILE = self.retirements_file
        self.addCleanup(self.restore_retirements_file)

    def restore_retirements_file(self) -> None:
        pool_ops.MINER_RETIREMENTS_FILE = self.old_retirements_file

    def write_retirement(self) -> None:
        self.retirements_file.write_text(
            json.dumps(
                {
                    "retired_miners": [
                        {
                            "display_name": "Athena",
                            "mac": "28:e2:97:4c:e4:0a",
                            "ips": ["192.168.1.102"],
                            "worker_user": "0x1719E0ee598c15957448D5E568948101DF78e7A0",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    def test_active_different_identity_at_retired_ip_is_not_hidden(self) -> None:
        self.write_retirement()
        item = {
            "ip": "192.168.1.102",
            "mac": "2a:71:c7:f5:1f:1e",
            "workers": ["0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"],
            "submits": 4,
            "shares": 2,
        }

        decision = pool_ops.retired_miner_identity_decision(item, item["ip"], item["mac"])

        self.assertFalse(decision["retired"])
        self.assertTrue(decision["conflict"])
        self.assertEqual(decision["matched_by"], "ip-observation")
        self.assertFalse(pool_ops.is_retired_miner_identity(item, item["ip"], item["mac"]))

    def test_same_mac_retirement_remains_authoritative(self) -> None:
        self.write_retirement()
        item = {
            "ip": "192.168.1.102",
            "mac": "28:e2:97:4c:e4:0a",
            "workers": ["0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"],
            "shares": 2,
        }

        decision = pool_ops.retired_miner_identity_decision(item, item["ip"], item["mac"])

        self.assertTrue(decision["retired"])
        self.assertFalse(decision["conflict"])
        self.assertEqual(decision["matched_by"], "mac")

    def test_ip_without_mac_is_observation_only(self) -> None:
        self.write_retirement()
        item = {"ip": "192.168.1.102"}

        decision = pool_ops.retired_miner_identity_decision(item, item["ip"], "")

        self.assertFalse(decision["retired"])
        self.assertTrue(decision["conflict"])
        self.assertEqual(decision["matched_by"], "ip-observation")

    def test_worker_match_without_mac_is_observation_only(self) -> None:
        self.write_retirement()
        item = {
            "ip": "192.168.1.200",
            "workers": ["0x1719E0ee598c15957448D5E568948101DF78e7A0"],
            "shares": 2,
        }

        decision = pool_ops.retired_miner_identity_decision(item, item["ip"], "")

        self.assertFalse(decision["retired"])
        self.assertFalse(decision["conflict"])
        self.assertEqual(decision["matched_by"], "")

    def test_configure_miners_skips_retired_mac_identity(self) -> None:
        self.write_retirement()
        old_discover_miner = pool_ops.discover_miner
        old_configure_miner = pool_ops.configure_miner
        old_read_neighbor_macs = pool_ops.read_neighbor_macs
        self.addCleanup(lambda: setattr(pool_ops, "discover_miner", old_discover_miner))
        self.addCleanup(lambda: setattr(pool_ops, "configure_miner", old_configure_miner))
        self.addCleanup(lambda: setattr(pool_ops, "read_neighbor_macs", old_read_neighbor_macs))
        pool_ops.read_neighbor_macs = lambda: {}
        pool_ops.discover_miner = lambda ip, timeout=0: {
            "ip": ip,
            "mac": "28:e2:97:4c:e4:0a",
            "model": "X100",
        }

        def fail_if_called(**_kwargs):
            raise AssertionError("retired miner should not be configured")

        pool_ops.configure_miner = fail_if_called

        result = pool_ops.configure_miners(
            ["192.168.1.102"],
            admin_password="admin-pass",
            pool_url="stratum+tcp://192.168.1.120:3334",
            worker_user="0x05518E03e148C56e426ff9e1CBdB962B4FC5250A",
        )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["results"][0]["status"], "skipped")
        self.assertEqual(result["results"][0]["reason"], "retired-miner-mac")
        self.assertEqual(result["results"][0]["mac"], "28:e2:97:4c:e4:0a")


class MinerHealthCountTests(unittest.TestCase):
    def test_ok_count_includes_unmanaged_tracked_miners(self) -> None:
        health = [
            {"managed": False, "status": "ok", "connected": True, "device_type": "stratum"},
            {"managed": False, "status": "ok", "connected": True, "device_type": "stratum"},
            {"managed": True, "status": "degraded", "connected": True, "device_type": "asic"},
        ]

        counts = pool_ops.miner_health_count_summary(health)

        self.assertEqual(counts["tracked_count"], 3)
        self.assertEqual(counts["connected_count"], 3)
        self.assertEqual(counts["managed_count"], 1)
        self.assertEqual(counts["managed_ok_count"], 0)
        self.assertEqual(counts["ok_count"], 2)


class MinerRegistryIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry_file = pathlib.Path(self.tmp.name) / "miners.json"
        self.retirements_file = pathlib.Path(self.tmp.name) / "retirements.json"
        self.old_registry_file = pool_ops.MINER_REGISTRY_FILE
        self.old_retirements_file = pool_ops.MINER_RETIREMENTS_FILE
        self.old_read_neighbors = pool_ops.read_neighbor_macs
        self.old_discover_miner = pool_ops.discover_miner
        pool_ops.MINER_REGISTRY_FILE = self.registry_file
        pool_ops.MINER_RETIREMENTS_FILE = self.retirements_file
        pool_ops.read_neighbor_macs = lambda: {}
        self.addCleanup(self.restore)

    def restore(self) -> None:
        pool_ops.MINER_REGISTRY_FILE = self.old_registry_file
        pool_ops.MINER_RETIREMENTS_FILE = self.old_retirements_file
        pool_ops.read_neighbor_macs = self.old_read_neighbors
        pool_ops.discover_miner = self.old_discover_miner

    def test_registry_keeps_distinct_macs_when_ip_is_reused(self) -> None:
        registry = pool_ops.save_miner_registry(
            [
                {"ip": "192.168.1.103", "mac": "28:e2:97:2e:00:1b", "managed": True, "device_type": "asic"},
                {"ip": "192.168.1.103", "mac": "88:a2:9e:a8:02:79", "managed": True, "device_type": "asic"},
            ]
        )

        macs = {item["mac"] for item in registry["miners"]}
        self.assertEqual(macs, {"28:e2:97:2e:00:1b", "88:a2:9e:a8:02:79"})

    def test_registry_suppresses_unknown_mac_observation_at_retired_ip(self) -> None:
        self.retirements_file.write_text(
            json.dumps(
                {
                    "retired_miners": [
                        {
                            "display_name": "ExternalPool",
                            "mac": "28:e2:97:4c:e4:0a",
                            "ips": ["192.168.1.103"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        registry = pool_ops.save_miner_registry(
            [
                {"ip": "192.168.1.103", "device_type": "stratum", "discovered_by": "pool-log"},
                {"ip": "192.168.1.103", "mac": "28:e2:97:2e:00:1b", "device_type": "asic"},
            ]
        )

        self.assertEqual(len(registry["miners"]), 1)
        self.assertEqual(registry["miners"][0]["mac"], "28:e2:97:2e:00:1b")

    def test_display_label_defaults_to_mac_and_suffixes_explicit_name(self) -> None:
        self.assertEqual(
            pool_ops.miner_display_label({"mac": "28:e2:97:2e:00:1b"}),
            "28:e2:97:2e:00:1b",
        )
        self.assertEqual(
            pool_ops.miner_display_label({"display_name": "Athena", "mac": "28:e2:97:2e:00:1b"}),
            "Athena-01b",
        )

    def test_miner_mac_from_payload_accepts_firmware_name_field(self) -> None:
        self.assertEqual(
            pool_ops.miner_mac_from_payload({"name": "28:E2:97:2E:00:1B"}, "192.168.1.107", {}),
            "28:e2:97:2e:00:1b",
        )

    def test_registry_includes_direct_lan_neighbor_hint(self) -> None:
        old_target = os.environ.get("BDAG_MINER_SCAN_TARGET")
        old_read_neighbor_macs = pool_ops.read_neighbor_macs
        os.environ["BDAG_MINER_SCAN_TARGET"] = "192.168.1.0/24"
        self.addCleanup(lambda: os.environ.pop("BDAG_MINER_SCAN_TARGET", None) if old_target is None else os.environ.__setitem__("BDAG_MINER_SCAN_TARGET", old_target))
        self.addCleanup(lambda: setattr(pool_ops, "read_neighbor_macs", old_read_neighbor_macs))
        pool_ops.read_neighbor_macs = lambda: {"192.168.1.107": "28:e2:97:1e:c0:b5"}

        registry = pool_ops.read_miner_registry()

        self.assertEqual(len(registry["miners"]), 1)
        miner = registry["miners"][0]
        self.assertEqual(miner["ip"], "192.168.1.107")
        self.assertEqual(miner["mac"], "28:e2:97:1e:c0:b5")
        self.assertEqual(miner["device_type"], "asic")
        self.assertIn("lan-hint", miner["sources"])

    def test_registry_ignores_docker_bridge_neighbor_hints(self) -> None:
        old_target = os.environ.get("BDAG_MINER_SCAN_TARGET")
        old_read_neighbor_macs = pool_ops.read_neighbor_macs
        os.environ["BDAG_MINER_SCAN_TARGET"] = "192.168.1.0/24"
        self.addCleanup(lambda: os.environ.pop("BDAG_MINER_SCAN_TARGET", None) if old_target is None else os.environ.__setitem__("BDAG_MINER_SCAN_TARGET", old_target))
        self.addCleanup(lambda: setattr(pool_ops, "read_neighbor_macs", old_read_neighbor_macs))
        pool_ops.read_neighbor_macs = lambda: {
            "172.18.0.3": "42:da:59:7c:fc:5d",
            "192.168.1.107": "28:e2:97:1e:c0:b5",
        }

        registry = pool_ops.read_miner_registry()

        self.assertEqual([item["ip"] for item in registry["miners"]], ["192.168.1.107"])
        self.assertEqual(registry["miners"][0]["mac"], "28:e2:97:1e:c0:b5")

    def test_scan_targets_reject_docker_bridge_cidrs(self) -> None:
        with self.assertRaises(ValueError):
            pool_ops.parse_scan_targets("172.18.0.0/30")

    def test_pool_activity_upsert_prunes_existing_docker_bridge_pseudo_miner(self) -> None:
        self.registry_file.write_text(
            json.dumps(
                {
                    "updated_at": "2026-06-05T05:02:41+0000",
                    "miners": [
                        {
                            "ip": "172.18.0.1",
                            "mac": "4e:bc:c0:90:6a:aa",
                            "device_type": "stratum",
                            "discovered_by": "pool-log",
                            "sources": ["pool-log"],
                            "last_workers": ["0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"],
                        },
                        {
                            "ip": "192.168.1.107",
                            "mac": "28:e2:97:1e:c0:b5",
                            "device_type": "asic",
                            "discovered_by": "asic-api",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        registry = pool_ops.upsert_pool_activity_miners({"miners": []})

        self.assertEqual([item["ip"] for item in registry["miners"]], ["192.168.1.107"])

    def test_save_registry_rejects_recent_docker_bridge_pseudo_miner(self) -> None:
        registry = pool_ops.save_miner_registry(
            [
                {
                    "ip": "172.18.0.1",
                    "mac": "4e:bc:c0:90:6a:aa",
                    "device_id": "mac:4e:bc:c0:90:6a:aa",
                    "device_type": "stratum",
                    "discovered_by": "pool-log",
                    "sources": ["pool-log"],
                    "last_pool_seen_epoch": 1780638628,
                    "last_pool_seen_at": "2026/06/05 05:02:41",
                    "last_workers": ["0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"],
                },
                {
                    "ip": "192.168.1.107",
                    "mac": "28:e2:97:1e:c0:b5",
                    "device_type": "asic",
                    "discovered_by": "asic-api",
                },
            ]
        )

        self.assertEqual([item["ip"] for item in registry["miners"]], ["192.168.1.107"])
        self.assertFalse(any(pool_ops.is_docker_bridge_ipv4(item["ip"]) for item in registry["miners"]))

    def test_pool_endpoint_fallback_uses_host_lan_not_docker_bridge(self) -> None:
        old_run = pool_ops.run
        old_pool_url = os.environ.get("BDAG_POOL_URL")
        old_pool_host = os.environ.get("BDAG_POOL_HOST")
        os.environ.pop("BDAG_POOL_URL", None)
        os.environ.pop("BDAG_POOL_HOST", None)
        self.addCleanup(lambda: setattr(pool_ops, "run", old_run))
        self.addCleanup(lambda: os.environ.pop("BDAG_POOL_URL", None) if old_pool_url is None else os.environ.__setitem__("BDAG_POOL_URL", old_pool_url))
        self.addCleanup(lambda: os.environ.pop("BDAG_POOL_HOST", None) if old_pool_host is None else os.environ.__setitem__("BDAG_POOL_HOST", old_pool_host))

        def fake_run(command, timeout=20):
            text = " ".join(command)
            if text.startswith("ip -4 route get"):
                return pool_ops.CommandResult(command, 0, "1.1.1.1 via 172.18.0.1 dev eth0 src 172.18.0.4\n", "", 0.0)
            if text.startswith("hostname -I"):
                return pool_ops.CommandResult(command, 0, "172.18.0.4 192.168.1.120\n", "", 0.0)
            if text.startswith("ip -4 -o addr"):
                return pool_ops.CommandResult(
                    command,
                    0,
                    "2: eth0    inet 172.18.0.4/16 brd 172.18.255.255 scope global eth0\n"
                    "3: enx0    inet 192.168.1.120/24 brd 192.168.1.255 scope global enx0\n",
                    "",
                    0.0,
                )
            return pool_ops.CommandResult(command, 0, "", "", 0.0)

        pool_ops.run = fake_run

        defaults = pool_ops.default_miner_pool_settings()

        self.assertEqual(defaults["pool_url"], "stratum+tcp://192.168.1.120:3334")

    def test_pool_log_dhcp_change_uses_asic_api_mac_when_arp_is_empty(self) -> None:
        worker = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        mac = "28:e2:97:2e:00:1b"
        pool_ops.discover_miner = lambda ip, timeout=0: {
            "ip": ip,
            "mac": mac,
            "name": "28:E2:97:2E:00:1B",
            "model": "X100",
            "hardware": "20.10.SA",
            "firmware": "2.2.2",
            "mcbversion": "MCB_V6_3_5",
            "pool_count": 1,
        } if ip == "192.168.1.101" else None
        pool_ops.save_miner_registry(
            [
                {
                    "ip": "192.168.1.126",
                    "mac": mac,
                    "device_id": f"mac:{mac}",
                    "device_type": "asic",
                    "managed": True,
                    "last_configured_ok": True,
                    "expected_pool_url": pool_ops.default_miner_pool_settings()["pool_url"],
                    "expected_worker_user": worker,
                }
            ]
        )

        registry = pool_ops.upsert_pool_activity_miners(
            {
                "miners": [
                    {
                        "ip": "192.168.1.101",
                        "workers": [worker],
                        "ports": ["44373"],
                        "shares": 12,
                        "share_work": 1000,
                        "blocks_found": 2,
                        "last_seen_at": "2026/06/04 18:17:44",
                        "last_share_at": "2026/06/04 18:17:44",
                        "last_submit_at": "2026/06/04 18:17:44",
                    }
                ]
            }
        )

        miners = [item for item in registry["miners"] if item.get("mac") == mac]
        self.assertEqual(len(miners), 1)
        miner = miners[0]
        self.assertEqual(miner["ip"], "192.168.1.101")
        self.assertEqual(set(miner["ip_history"]), {"192.168.1.126", "192.168.1.101"})
        self.assertEqual(miner["device_id"], f"mac:{mac}")
        self.assertEqual(miner["device_type"], "asic")
        self.assertTrue(miner["managed"])
        self.assertTrue(miner["last_configured_ok"])
        self.assertEqual(miner["model"], "X100")
        self.assertEqual(miner["last_shares_window"], 12)
        self.assertIn("pool-log", miner["sources"])
        self.assertIn("asic-api", miner["sources"])


class PoolActivityAttributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_read_miner_registry = pool_ops.read_miner_registry
        self.old_read_neighbor_macs = pool_ops.read_neighbor_macs
        self.addCleanup(self.restore_registry)

    def restore_registry(self) -> None:
        pool_ops.read_miner_registry = self.old_read_miner_registry
        pool_ops.read_neighbor_macs = self.old_read_neighbor_macs

    def test_shared_worker_without_job_mapping_is_not_assigned_to_one_miner(self) -> None:
        worker = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        pool_ops.read_miner_registry = lambda: {
            "miners": [
                {"ip": "192.168.1.14", "mac": "28:e2:97:3e:39:63", "expected_worker_user": worker},
                {"ip": "192.168.1.103", "mac": "28:e2:97:1e:c0:b5", "expected_worker_user": worker},
            ]
        }
        log = "\n".join(
            [
                f"2026/05/26 06:20:00 [192.168.1.14:40541] authorize accepted user={worker}",
                f"2026/05/26 06:20:00 [192.168.1.103:45403] authorize accepted user={worker}",
                f"2026/05/26 06:20:01 valid share accepted 100.0 -> 500 worker={worker} job=missing-notify",
            ]
        )

        miners = {item["ip"]: item for item in pool_ops.parse_pool_activity(log)["miners"]}

        self.assertEqual(miners["192.168.1.14"]["shares"], 0)
        self.assertEqual(miners["192.168.1.103"]["shares"], 0)

    def test_shared_worker_with_job_mapping_uses_job_client(self) -> None:
        worker = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        pool_ops.read_miner_registry = lambda: {
            "miners": [
                {"ip": "192.168.1.14", "mac": "28:e2:97:3e:39:63", "expected_worker_user": worker},
                {"ip": "192.168.1.103", "mac": "28:e2:97:1e:c0:b5", "expected_worker_user": worker},
            ]
        }
        log = "\n".join(
            [
                "2026/05/26 06:20:00 Sending to 192.168.1.14:40541: jobID=job-1",
                f"2026/05/26 06:20:01 valid share accepted 100.0 -> 500 worker={worker} job=job-1",
            ]
        )

        miners = {item["ip"]: item for item in pool_ops.parse_pool_activity(log)["miners"]}

        self.assertEqual(miners["192.168.1.14"]["shares"], 1)
        self.assertEqual(miners["192.168.1.14"]["share_work"], 500)
        self.assertNotIn("192.168.1.103", miners)

    def test_same_mac_ip_change_keeps_worker_and_work_on_one_identity(self) -> None:
        worker = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        mac = "28:e2:97:3e:39:63"
        pool_ops.read_miner_registry = lambda: {
            "miners": [
                {
                    "ip": "192.168.1.14",
                    "mac": mac,
                    "display_name": "Athena",
                    "expected_worker_user": worker,
                    "ip_history": ["192.168.1.14"],
                },
            ]
        }
        pool_ops.read_neighbor_macs = lambda: {
            "192.168.1.14": mac,
            "192.168.1.114": mac,
        }
        log = "\n".join(
            [
                f"2026/05/26 06:20:00 [192.168.1.14:40541] authorize accepted user={worker}",
                f"2026/05/26 06:20:01 [192.168.1.114:45403] authorize accepted user={worker}",
                f"2026/05/26 06:20:02 valid share accepted 100.0 -> 500 worker={worker} job=missing-client",
                "2026/05/26 06:20:03 Sending to 192.168.1.114:45403: jobID=job-1",
                f"2026/05/26 06:20:04 valid share accepted 100.0 -> 700 worker={worker} job=job-1",
                "2026/05/26 06:20:05 BLOCK FOUND height(le)=8000001 job=job-1 hash=aaa target=bbb",
            ]
        )

        activity = pool_ops.parse_pool_activity(log)
        miners = activity["miners"]

        self.assertEqual(activity["unattributed_valid_shares"], 0)
        self.assertEqual(len(miners), 1)
        self.assertEqual(miners[0]["identity_key"], f"mac:{mac}")
        self.assertEqual(miners[0]["display_label"], "Athena-963")
        self.assertEqual(miners[0]["ip"], "192.168.1.114")
        self.assertEqual(set(miners[0]["ip_history"]), {"192.168.1.14", "192.168.1.114"})
        self.assertEqual(miners[0]["shares"], 2)
        self.assertEqual(miners[0]["share_work"], 1200)
        self.assertEqual(miners[0]["blocks_found"], 1)

    def test_shared_worker_with_direct_client_log_uses_client_identity(self) -> None:
        worker = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        pool_ops.read_miner_registry = lambda: {
            "miners": [
                {"ip": "192.168.1.14", "mac": "28:e2:97:3e:39:63", "expected_worker_user": worker},
                {"ip": "192.168.1.103", "mac": "28:e2:97:1e:c0:b5", "expected_worker_user": worker},
            ]
        }
        log = "\n".join(
            [
                f"2026/05/26 06:20:00 submit from client=192.168.1.103:45403 worker={worker} job=job-1_01000000 extranonce2=00000000 ntime=6a15d193 nonce=1",
                f"2026/05/26 06:20:01 ✅ valid share accepted 100.0 → 500 client=192.168.1.103:45403 worker={worker} job=job-1_01000000",
            ]
        )

        miners = {item["ip"]: item for item in pool_ops.parse_pool_activity(log)["miners"]}

        self.assertEqual(miners["192.168.1.103"]["shares"], 1)
        self.assertEqual(miners["192.168.1.103"]["share_work"], 500)
        self.assertNotIn("192.168.1.14", miners)

    def test_shared_worker_with_submit_client_maps_later_share_by_extranonce(self) -> None:
        worker = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        pool_ops.read_miner_registry = lambda: {
            "miners": [
                {"ip": "192.168.1.14", "mac": "28:e2:97:3e:39:63", "expected_worker_user": worker},
                {"ip": "192.168.1.103", "mac": "28:e2:97:1e:c0:b5", "expected_worker_user": worker},
            ]
        }
        log = "\n".join(
            [
                f"2026/05/26 06:20:00 submit from client=192.168.1.14:40541 worker={worker} job=job-1_02000000 extranonce2=00000000 ntime=6a15d193 nonce=1",
                f"2026/05/26 06:20:01 ✅ valid share accepted 100.0 → 500 worker={worker} job=job-2_02000000",
            ]
        )

        miners = {item["ip"]: item for item in pool_ops.parse_pool_activity(log)["miners"]}

        self.assertEqual(miners["192.168.1.14"]["shares"], 1)
        self.assertEqual(miners["192.168.1.14"]["share_work"], 500)

    def test_legacy_authorize_order_maps_job_extranonce_suffixes(self) -> None:
        worker = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        pool_ops.read_miner_registry = lambda: {
            "miners": [
                {"ip": "192.168.1.14", "mac": "28:e2:97:3e:39:63", "expected_worker_user": worker},
                {"ip": "192.168.1.16", "mac": "28:e2:97:4d:44:3a", "expected_worker_user": worker},
                {"ip": "192.168.1.101", "mac": "2a:71:c7:f5:1f:1e", "expected_worker_user": worker},
                {"ip": "192.168.1.103", "mac": "28:e2:97:2e:00:1b", "expected_worker_user": worker},
            ]
        }
        log = "\n".join(
            [
                f"2026/05/27 00:20:57 [192.168.1.14:52953] authorize accepted user={worker}",
                f"2026/05/27 00:20:57 [192.168.1.16:42015] authorize accepted user={worker}",
                f"2026/05/27 00:20:57 [192.168.1.101:41101] authorize accepted user={worker}",
                f"2026/05/27 00:20:57 [192.168.1.103:43866] authorize accepted user={worker}",
                f"2026/05/27 00:21:01 valid share accepted 10.0 -> 100 worker={worker} job=abc_01000000",
                f"2026/05/27 00:21:02 valid share accepted 10.0 -> 200 worker={worker} job=abc_02000000",
                f"2026/05/27 00:21:03 valid share accepted 10.0 -> 300 worker={worker} job=abc_03000000",
                f"2026/05/27 00:21:04 valid share accepted 10.0 -> 400 worker={worker} job=abc_04000000",
                "2026/05/27 00:21:05 BLOCK FOUND height(le)=8000001 job=abc_01000000 hash=aaa target=bbb",
                "2026/05/27 00:21:06 BLOCK FOUND height(le)=8000002 job=abc_02000000 hash=aaa target=bbb",
                "2026/05/27 00:21:07 BLOCK FOUND height(le)=8000003 job=abc_03000000 hash=aaa target=bbb",
                "2026/05/27 00:21:08 BLOCK FOUND height(le)=8000004 job=abc_04000000 hash=aaa target=bbb",
            ]
        )

        activity = pool_ops.parse_pool_activity(log)
        miners = {item["ip"]: item for item in activity["miners"]}

        self.assertEqual(activity["unattributed_valid_shares"], 0)
        self.assertEqual(activity["unattributed_blocks"], 0)
        self.assertEqual(miners["192.168.1.14"]["share_work"], 100)
        self.assertEqual(miners["192.168.1.16"]["share_work"], 200)
        self.assertEqual(miners["192.168.1.101"]["share_work"], 300)
        self.assertEqual(miners["192.168.1.103"]["share_work"], 400)
        self.assertEqual(miners["192.168.1.14"]["blocks_found"], 1)
        self.assertEqual(miners["192.168.1.16"]["job_extranonces"], ["02000000"])

    def test_fresh_legacy_authorize_order_overrides_stale_extranonce_registry(self) -> None:
        worker = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        pool_ops.read_miner_registry = lambda: {
            "miners": [
                {
                    "ip": "192.168.1.14",
                    "mac": "28:e2:97:3e:39:63",
                    "display_name": "Odysseus",
                    "expected_worker_user": worker,
                    "last_pool_job_extranonces": ["05000000"],
                },
                {
                    "ip": "192.168.1.16",
                    "mac": "28:e2:97:4d:44:3a",
                    "display_name": "Penelope",
                    "expected_worker_user": worker,
                    "last_pool_job_extranonces": ["04000000"],
                },
                {
                    "ip": "192.168.1.101",
                    "mac": "2a:71:c7:f5:1f:1e",
                    "display_name": "Athena",
                    "expected_worker_user": worker,
                    "last_pool_job_extranonces": ["06000000"],
                },
            ]
        }
        log = "\n".join(
            [
                f"2026/05/30 11:02:28 [192.168.1.14:40201] authorize accepted user={worker}",
                f"2026/05/30 11:02:28 [192.168.1.16:40202] authorize accepted user={worker}",
                f"2026/05/30 11:02:28 [192.168.1.101:40203] authorize accepted user={worker}",
                f"2026/05/30 11:02:29 [192.168.1.101:40204] authorize accepted user={worker}",
                f"2026/05/30 11:02:33 [192.168.1.14:40205] authorize accepted user={worker}",
                f"2026/05/30 11:02:33 [192.168.1.16:40206] authorize accepted user={worker}",
                f"2026/05/30 11:03:00 valid share accepted 10.0 -> 400 worker={worker} job=abc_04000000",
                f"2026/05/30 11:03:01 valid share accepted 10.0 -> 500 worker={worker} job=abc_05000000",
                f"2026/05/30 11:03:02 valid share accepted 10.0 -> 600 worker={worker} job=abc_06000000",
                "2026/05/30 11:03:03 BLOCK FOUND height(le)=8000001 job=abc_04000000 hash=aaa target=bbb",
                "2026/05/30 11:03:04 BLOCK FOUND height(le)=8000002 job=abc_05000000 hash=aaa target=bbb",
                "2026/05/30 11:03:05 BLOCK FOUND height(le)=8000003 job=abc_06000000 hash=aaa target=bbb",
            ]
        )

        activity = pool_ops.parse_pool_activity(log)
        miners = {item["display_label"]: item for item in activity["miners"]}

        self.assertEqual(activity["unattributed_valid_shares"], 0)
        self.assertEqual(activity["unattributed_blocks"], 0)
        self.assertEqual(miners["Athena-f1e"]["share_work"], 400)
        self.assertEqual(miners["Odysseus-963"]["share_work"], 500)
        self.assertEqual(miners["Penelope-43a"]["share_work"], 600)
        self.assertEqual(miners["Athena-f1e"]["blocks_found"], 1)
        self.assertEqual(miners["Odysseus-963"]["blocks_found"], 1)
        self.assertEqual(miners["Penelope-43a"]["blocks_found"], 1)

    def test_registry_extranonce_mapping_recovers_short_log_tail(self) -> None:
        worker = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        pool_ops.read_miner_registry = lambda: {
            "miners": [
                {
                    "ip": "192.168.1.14",
                    "mac": "28:e2:97:3e:39:63",
                    "expected_worker_user": worker,
                    "last_pool_job_extranonces": ["07000000"],
                },
                {"ip": "192.168.1.103", "mac": "28:e2:97:2e:00:1b", "expected_worker_user": worker},
            ]
        }
        log = f"2026/05/27 00:21:01 valid share accepted 10.0 -> 500 worker={worker} job=abc_07000000"

        miners = {item["ip"]: item for item in pool_ops.parse_pool_activity(log)["miners"]}

        self.assertEqual(miners["192.168.1.14"]["shares"], 1)
        self.assertEqual(miners["192.168.1.14"]["share_work"], 500)
        self.assertNotIn("192.168.1.103", miners)

    def test_ambiguous_registry_extranonce_is_not_assigned_to_first_miner(self) -> None:
        worker = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        pool_ops.read_miner_registry = lambda: {
            "miners": [
                {
                    "ip": "192.168.1.14",
                    "mac": "28:e2:97:3e:39:63",
                    "expected_worker_user": worker,
                    "last_pool_job_extranonces": ["04000000"],
                },
                {
                    "ip": "192.168.1.101",
                    "mac": "2a:71:c7:f5:1f:1e",
                    "expected_worker_user": worker,
                    "last_pool_job_extranonces": ["04000000"],
                },
            ]
        }
        log = f"2026/05/30 11:03:00 valid share accepted 10.0 -> 400 worker={worker} job=abc_04000000"

        activity = pool_ops.parse_pool_activity(log)

        self.assertEqual(activity["miners"], [])
        self.assertEqual(activity["unattributed_valid_shares"], 1)

    def test_reconnect_unknown_extranonce_uses_recent_authorized_mac_client(self) -> None:
        worker = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        pool_ops.read_miner_registry = lambda: {
            "miners": [
                {
                    "ip": "192.168.1.14",
                    "mac": "28:e2:97:3e:39:63",
                    "display_name": "Achilles",
                    "expected_worker_user": worker,
                    "last_pool_job_extranonces": ["25000000"],
                },
                {
                    "ip": "192.168.1.103",
                    "mac": "28:e2:97:4c:e4:0a",
                    "display_name": "Hector",
                    "expected_worker_user": worker,
                    "last_pool_job_extranonces": ["26000000"],
                },
            ]
        }
        log = "\n".join(
            [
                f"2026/05/27 07:24:16 [192.168.1.103:51374] authorize accepted user={worker}",
                "2026/05/27 07:24:16 PUSHDIF -> 192.168.1.103:51374 mining.set_difficulty 0.05000000",
                f"2026/05/27 07:24:25 submit from worker={worker} job=80978-18b35b4d0363621e_2a000000 extranonce2=00000000 ntime=6a169c28 nonce=b8ba6f7b suppressed=0",
                f"2026/05/27 07:24:25 valid share accepted 4024.201881 -> 263730094 worker={worker} job=80978-18b35b4d0363621e_2a000000 suppressed=0",
                "2026/05/27 07:24:27 BLOCK FOUND height(le)=7135944 job=80979-18b35b4d68217614_2a000000 hash=abc target=def",
                f"2026/05/27 07:24:28 valid share accepted 100.0 -> 500 worker={worker} job=80980-18b35b4d68217615_25000000",
            ]
        )

        activity = pool_ops.parse_pool_activity(log)
        miners = {item["display_label"]: item for item in activity["miners"]}

        self.assertEqual(activity["unattributed_valid_shares"], 0)
        self.assertEqual(activity["unattributed_blocks"], 0)
        self.assertEqual(miners["Hector-40a"]["shares"], 1)
        self.assertEqual(miners["Hector-40a"]["share_work"], 263730094)
        self.assertEqual(miners["Hector-40a"]["blocks_found"], 1)
        self.assertEqual(miners["Hector-40a"]["job_extranonces"], ["2a000000"])
        self.assertEqual(miners["Achilles-963"]["shares"], 1)
        self.assertEqual(miners["Achilles-963"]["share_work"], 500)

    def test_recovery_resend_maps_ephemeral_lane_suffix_to_current_ip(self) -> None:
        worker = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        pool_ops.read_miner_registry = lambda: {
            "miners": [
                {
                    "ip": "192.168.1.14",
                    "mac": "28:e2:97:3e:39:63",
                    "expected_worker_user": worker,
                    "last_pool_job_extranonces": ["07000000"],
                },
                {
                    "ip": "192.168.1.103",
                    "mac": "28:e2:97:2e:00:1b",
                    "expected_worker_user": worker,
                    "last_pool_job_extranonces": ["0f000000"],
                },
            ]
        }
        log = "\n".join(
            [
                "2026/05/26 06:20:00 [RECOVERY] resending current job to 192.168.1.14:55264 after 3 expired job rejects (job=job-1_0f000000)",
                f"2026/05/26 06:20:01 ✅ valid share accepted 100.0 → 500 worker={worker} job=job-2_0f000000",
                "2026/05/26 06:20:02 🎯 BLOCK FOUND height(le)=1 job=job-3_0f000000 hash=abc target=def",
            ]
        )

        miners = {item["ip"]: item for item in pool_ops.parse_pool_activity(log)["miners"]}

        self.assertEqual(miners["192.168.1.14"]["shares"], 1)
        self.assertEqual(miners["192.168.1.14"]["share_work"], 500)
        self.assertEqual(miners["192.168.1.14"]["blocks_found"], 1)
        self.assertIn("0f000000", miners["192.168.1.14"]["job_extranonces"])
        self.assertNotIn("192.168.1.103", miners)

    def test_collect_pool_activity_bootstraps_when_short_tail_is_unattributed(self) -> None:
        worker = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        pool_ops.read_miner_registry = lambda: {
            "miners": [
                {"ip": "192.168.1.14", "mac": "28:e2:97:3e:39:63", "expected_worker_user": worker},
                {"ip": "192.168.1.103", "mac": "28:e2:97:2e:00:1b", "expected_worker_user": worker},
            ]
        }
        short_log = f"2026/05/27 00:21:01 valid share accepted 10.0 -> 500 worker={worker} job=abc_01000000"
        full_log = "\n".join(
            [
                f"2026/05/27 00:20:57 [192.168.1.14:52953] authorize accepted user={worker}",
                short_log,
            ]
        )
        old_docker_logs_many = pool_ops.docker_logs_many
        old_bootstrap_lines = pool_ops.POOL_ACTIVITY_BOOTSTRAP_LOG_LINES
        self.addCleanup(lambda: setattr(pool_ops, "docker_logs_many", old_docker_logs_many))
        self.addCleanup(lambda: setattr(pool_ops, "POOL_ACTIVITY_BOOTSTRAP_LOG_LINES", old_bootstrap_lines))
        calls = []
        pool_ops.POOL_ACTIVITY_BOOTSTRAP_LOG_LINES = 20
        pool_ops.docker_logs_many = lambda _containers, lines=2500: calls.append(lines) or (full_log if lines == 20 else short_log)

        activity = pool_ops.collect_pool_activity(lines=2)
        miners = {item["ip"]: item for item in activity["miners"]}

        self.assertEqual(calls, [2, 20])
        self.assertEqual(activity["bootstrap_log_lines"], 20)
        self.assertEqual(miners["192.168.1.14"]["share_work"], 500)

    def test_collect_pool_activity_bootstraps_when_tail_is_partially_unattributed(self) -> None:
        worker = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        pool_ops.read_miner_registry = lambda: {
            "miners": [
                {
                    "ip": "192.168.1.14",
                    "mac": "28:e2:97:3e:39:63",
                    "expected_worker_user": worker,
                    "last_pool_job_extranonces": ["02000000"],
                },
                {"ip": "192.168.1.16", "mac": "28:e2:97:4d:44:3a", "expected_worker_user": worker},
            ]
        }
        short_log = "\n".join(
            [
                f"2026/05/27 00:21:01 valid share accepted 10.0 -> 500 worker={worker} job=abc_02000000",
                f"2026/05/27 00:21:02 valid share accepted 10.0 -> 700 worker={worker} job=abc_01000000",
            ]
        )
        full_log = "\n".join(
            [
                f"2026/05/27 00:20:57 [192.168.1.16:52953] authorize accepted user={worker}",
                short_log,
            ]
        )
        old_docker_logs_many = pool_ops.docker_logs_many
        old_bootstrap_lines = pool_ops.POOL_ACTIVITY_BOOTSTRAP_LOG_LINES
        self.addCleanup(lambda: setattr(pool_ops, "docker_logs_many", old_docker_logs_many))
        self.addCleanup(lambda: setattr(pool_ops, "POOL_ACTIVITY_BOOTSTRAP_LOG_LINES", old_bootstrap_lines))
        calls = []
        pool_ops.POOL_ACTIVITY_BOOTSTRAP_LOG_LINES = 20
        pool_ops.docker_logs_many = lambda _containers, lines=2500: calls.append(lines) or (full_log if lines == 20 else short_log)

        activity = pool_ops.collect_pool_activity(lines=2)
        miners = {item["ip"]: item for item in activity["miners"]}

        self.assertEqual(calls, [2, 20])
        self.assertEqual(activity["bootstrap_log_lines"], 20)
        self.assertEqual(miners["192.168.1.14"]["share_work"], 500)
        self.assertEqual(miners["192.168.1.16"]["share_work"], 700)

    def test_docker_bridge_alias_does_not_hide_registered_asic_work(self) -> None:
        worker = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        pool_ops.read_miner_registry = lambda: {
            "miners": [
                {
                    "ip": "192.168.1.107",
                    "mac": "28:e2:97:1e:c0:b5",
                    "expected_worker_user": worker,
                }
            ]
        }
        log = "\n".join(
            [
                f"2026/05/26 22:34:00 [172.22.0.1:55572] authorize accepted user={worker}",
                "2026/05/26 22:34:01 PUSHDIF -> 172.22.0.1:55572 mining.set_difficulty 0.14229287",
                f"2026/05/26 22:34:02 ✅ valid share accepted 100.0 → 500 worker={worker} job=job-1_01000000",
                f"2026/05/26 22:34:03 🎯 BLOCK FOUND height(le)=7105000 job=job-1_01000000 hash=abc target=def",
            ]
        )

        miners = {item["ip"]: item for item in pool_ops.parse_pool_activity(log)["miners"]}

        self.assertEqual(miners["192.168.1.107"]["shares"], 1)
        self.assertEqual(miners["192.168.1.107"]["share_work"], 500)
        self.assertEqual(miners["192.168.1.107"]["blocks_found"], 1)

    def test_docker_bridge_alias_uses_single_direct_lan_hint_without_saved_registry(self) -> None:
        worker = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        old_target = os.environ.get("BDAG_MINER_SCAN_TARGET")
        os.environ["BDAG_MINER_SCAN_TARGET"] = "192.168.1.0/24"
        self.addCleanup(lambda: os.environ.pop("BDAG_MINER_SCAN_TARGET", None) if old_target is None else os.environ.__setitem__("BDAG_MINER_SCAN_TARGET", old_target))
        pool_ops.read_neighbor_macs = lambda: {"192.168.1.107": "28:e2:97:1e:c0:b5"}
        pool_ops.read_miner_registry = lambda: pool_ops.augment_miner_registry_with_lan_hints({"updated_at": None, "miners": []})
        log = "\n".join(
            [
                f"2026/05/29 18:47:00 [172.18.0.1:40971] authorize accepted user={worker}",
                "2026/05/29 18:47:01 PUSHDIF -> 172.18.0.1:40971 mining.set_difficulty 0.12452441",
                f"2026/05/29 18:47:02 ✅ valid share accepted 100.0 → 500 worker={worker} job=job-1_02000000",
                "2026/05/29 18:47:03 🎯 BLOCK FOUND height(le)=7349303 job=job-1_02000000 hash=abc target=def",
            ]
        )

        activity = pool_ops.parse_pool_activity(log)
        miners = {item["ip"]: item for item in activity["miners"]}

        self.assertEqual(activity["unattributed_valid_shares"], 0)
        self.assertEqual(activity["unattributed_blocks"], 0)
        self.assertIn("192.168.1.107", miners)
        self.assertEqual(miners["192.168.1.107"]["identity_key"], "mac:28:e2:97:1e:c0:b5")
        self.assertEqual(miners["192.168.1.107"]["shares"], 1)
        self.assertEqual(miners["192.168.1.107"]["share_work"], 500)
        self.assertEqual(miners["192.168.1.107"]["blocks_found"], 1)


class MinerHealthConfiguredScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_collect_pool_activity = pool_ops.collect_pool_activity
        self.old_upsert_pool_activity_miners = pool_ops.upsert_pool_activity_miners
        self.old_seconds_since_epoch = pool_ops.seconds_since_epoch
        self.old_discover_miner = pool_ops.discover_miner
        self.old_get_miner_cgminer_devs = pool_ops.get_miner_cgminer_devs
        self.old_mac_for_ip = pool_ops.mac_for_ip
        pool_ops.discover_miner = lambda ip, timeout=0: None
        pool_ops.get_miner_cgminer_devs = lambda ip, timeout=0: {}
        pool_ops.mac_for_ip = lambda ip: ""
        self.addCleanup(self.restore)

    def restore(self) -> None:
        pool_ops.collect_pool_activity = self.old_collect_pool_activity
        pool_ops.upsert_pool_activity_miners = self.old_upsert_pool_activity_miners
        pool_ops.seconds_since_epoch = self.old_seconds_since_epoch
        pool_ops.discover_miner = self.old_discover_miner
        pool_ops.get_miner_cgminer_devs = self.old_get_miner_cgminer_devs
        pool_ops.mac_for_ip = self.old_mac_for_ip

    def test_stale_unconfigured_pool_log_miner_does_not_drive_failure(self) -> None:
        worker = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        pool_ops.collect_pool_activity = lambda lines=0: {"miners": []}
        pool_ops.upsert_pool_activity_miners = lambda activity: {
            "updated_at": "2026-05-26T00:00:00+0200",
            "miners": [
                {
                    "ip": "192.168.1.104",
                    "mac": "10:27:f5:90:a4:2c",
                    "device_id": "mac:10:27:f5:90:a4:2c",
                    "device_type": "stratum",
                    "discovered_by": "pool-log",
                    "expected_pool_url": pool_ops.default_miner_pool_settings()["pool_url"],
                    "last_workers": [worker],
                    "last_pool_seen_epoch": 100,
                    "last_configured_ok": False,
                    "managed": False,
                }
            ],
        }
        pool_ops.seconds_since_epoch = lambda: 100 + pool_ops.POOL_CONNECTED_STALE_SECONDS + 10

        health = pool_ops.collect_miner_health()

        self.assertEqual(health["failures"], [])
        self.assertEqual(health["miners"], [])

    def test_recent_unconfigured_pool_log_ghost_is_not_an_expected_lane(self) -> None:
        worker = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        pool_ops.collect_pool_activity = lambda lines=0: {"miners": []}
        pool_ops.upsert_pool_activity_miners = lambda activity: {
            "updated_at": "2026-05-26T00:00:00+0200",
            "miners": [
                {
                    "ip": "192.168.1.104",
                    "mac": "10:27:f5:90:a4:2c",
                    "device_id": "mac:10:27:f5:90:a4:2c",
                    "device_type": "stratum",
                    "discovered_by": "pool-log",
                    "expected_pool_url": pool_ops.default_miner_pool_settings()["pool_url"],
                    "expected_worker_user": worker,
                    "last_workers": [worker],
                    "last_pool_seen_epoch": 100,
                    "last_configured_ok": False,
                    "managed": False,
                }
            ],
        }
        pool_ops.seconds_since_epoch = lambda: 110

        health = pool_ops.collect_miner_health()

        self.assertEqual(health["failures"], [])
        self.assertEqual(health["miners"], [])
        self.assertEqual(health["lane_balance"]["expected_lane_count"], 0)

    def test_fresh_registry_windows_display_after_short_log_window_misses_miner(self) -> None:
        worker = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        pool_ops.collect_pool_activity = lambda lines=0: {"miners": []}
        pool_ops.upsert_pool_activity_miners = lambda activity: {
            "updated_at": "2026-06-07T14:30:00+0200",
            "miners": [
                {
                    "ip": "192.168.1.102",
                    "mac": "28:e2:97:4d:44:3a",
                    "device_id": "mac:28:e2:97:4d:44:3a",
                    "device_type": "asic",
                    "expected_pool_url": pool_ops.default_miner_pool_settings()["pool_url"],
                    "expected_worker_user": worker,
                    "last_workers": [worker],
                    "last_pool_seen_epoch": 100,
                    "last_submit_epoch": 100,
                    "last_share_epoch": 100,
                    "last_jobs_window": 11,
                    "last_shares_window": 8,
                    "last_share_work_window": 700,
                    "last_share_difficulty_window": 0.8,
                    "last_blocks_window": 9,
                    "last_submits_window": 10,
                    "managed": True,
                }
            ],
        }
        pool_ops.seconds_since_epoch = lambda: 110

        miner = pool_ops.collect_miner_health()["miners"][0]

        self.assertTrue(miner["connected"])
        self.assertEqual(miner["shares"], 8)
        self.assertEqual(miner["share_work"], 700)
        self.assertEqual(miner["blocks_found"], 9)
        self.assertEqual(miner["submits"], 10)
        self.assertEqual(miner["jobs"], 11)
        self.assertTrue(miner["work_pool_active"])

    def test_stale_registry_windows_do_not_display_as_current_work(self) -> None:
        worker = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        pool_ops.collect_pool_activity = lambda lines=0: {"miners": []}
        pool_ops.upsert_pool_activity_miners = lambda activity: {
            "updated_at": "2026-06-04T18:25:00+0200",
            "miners": [
                {
                    "ip": "192.168.1.109",
                    "mac": "28:e2:97:3d:95:13",
                    "device_id": "mac:28:e2:97:3d:95:13",
                    "device_type": "asic",
                    "expected_pool_url": pool_ops.default_miner_pool_settings()["pool_url"],
                    "expected_worker_user": worker,
                    "last_workers": [worker],
                    "last_pool_seen_epoch": 100,
                    "last_shares_window": 8,
                    "last_share_work_window": 700,
                    "last_blocks_window": 9,
                    "last_submits_window": 10,
                    "managed": True,
                }
            ],
        }
        pool_ops.seconds_since_epoch = lambda: 110

        miner = pool_ops.collect_miner_health()["miners"][0]

        self.assertEqual(miner["shares"], 0)
        self.assertEqual(miner["share_work"], 0)
        self.assertEqual(miner["blocks_found"], 0)
        self.assertEqual(miner["submits"], 0)
        self.assertFalse(miner["work_pool_active"])

    def test_managed_pool_log_stratum_miner_is_ok_when_expected_worker_is_active(self) -> None:
        worker = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        pool_ops.collect_pool_activity = lambda lines=0: {
            "miners": [
                {
                    "ip": "192.168.1.14",
                    "workers": [worker],
                    "shares": 1,
                    "share_work": 500,
                    "blocks_found": 1,
                    "last_seen_at": "2026/05/26 21:40:42",
                }
            ]
        }
        pool_ops.upsert_pool_activity_miners = lambda activity: {
            "updated_at": "2026-05-26T00:00:00+0200",
            "miners": [
                {
                    "ip": "192.168.1.14",
                    "mac": "28:e2:97:3e:39:63",
                    "device_id": "mac:28:e2:97:3e:39:63",
                    "device_type": "stratum",
                    "discovered_by": "pool-log",
                    "expected_pool_url": pool_ops.default_miner_pool_settings()["pool_url"],
                    "expected_worker_user": worker,
                    "last_workers": [worker],
                    "last_pool_seen_epoch": 100,
                    "managed": True,
                }
            ],
        }
        pool_ops.seconds_since_epoch = lambda: 110

        health = pool_ops.collect_miner_health()

        self.assertEqual(health["failures"], [])
        self.assertEqual(health["miners"][0]["configured"], True)
        self.assertEqual(health["miners"][0]["status"], "ok")

    def test_pool_worker_user_matches_hex_address_case_insensitively(self) -> None:
        self.assertTrue(
            pool_ops.pool_worker_user_matches(
                "0x05518e03e148c56e426ff9e1cbdb962b4fc5250a",
                "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A",
            )
        )

    def test_managed_pool_log_stratum_miner_remains_expected_lane_when_down(self) -> None:
        worker = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        pool_ops.collect_pool_activity = lambda lines=0: {
            "miners": [
                {
                    "ip": "192.168.1.14",
                    "workers": [worker],
                    "shares": 1,
                    "share_work": 500,
                    "last_seen_at": "2026/05/31 08:05:29",
                }
            ]
        }
        pool_ops.upsert_pool_activity_miners = lambda activity: {
            "updated_at": "2026-05-31T10:00:00+0200",
            "miners": [
                {
                    "ip": "192.168.1.14",
                    "mac": "28:e2:97:3e:39:63",
                    "device_id": "mac:28:e2:97:3e:39:63",
                    "device_type": "stratum",
                    "discovered_by": "pool-log",
                    "expected_pool_url": pool_ops.default_miner_pool_settings()["pool_url"],
                    "expected_worker_user": worker,
                    "last_workers": [worker],
                    "last_pool_seen_epoch": 100,
                    "managed": True,
                },
                {
                    "ip": "192.168.1.101",
                    "mac": "2a:71:c7:f5:1f:1e",
                    "device_id": "mac:2a:71:c7:f5:1f:1e",
                    "device_type": "stratum",
                    "discovered_by": "pool-log",
                    "expected_pool_url": pool_ops.default_miner_pool_settings()["pool_url"],
                    "expected_worker_user": worker,
                    "last_workers": [worker],
                    "last_pool_seen_epoch": 100,
                    "managed": True,
                    "last_configured_ok": True,
                },
            ],
        }
        pool_ops.seconds_since_epoch = lambda: 100 + pool_ops.POOL_CONNECTED_STALE_SECONDS + 10

        health = pool_ops.collect_miner_health()
        miners = {item["mac"]: item for item in health["miners"]}

        self.assertEqual(health["lane_balance"]["expected_lane_count"], 2)
        self.assertTrue(miners["2a:71:c7:f5:1f:1e"]["expected_work_lane"])
        self.assertEqual(miners["2a:71:c7:f5:1f:1e"]["lane_status"], "low")
        self.assertEqual(miners["2a:71:c7:f5:1f:1e"]["status"], "down")

    def test_inactive_stale_share_window_is_not_rendered_as_current_miner(self) -> None:
        worker = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        pool_ops.collect_pool_activity = lambda lines=0: {
            "miners": [
                {
                    "ip": "192.168.1.14",
                    "workers": [worker],
                    "shares": 1,
                    "share_work": 500,
                    "last_seen_at": "2026/05/30 11:00:00",
                }
            ]
        }
        pool_ops.upsert_pool_activity_miners = lambda activity: {
            "updated_at": "2026-05-30T11:00:00+0200",
            "miners": [
                {
                    "ip": "192.168.1.14",
                    "mac": "28:e2:97:3e:39:63",
                    "device_id": "mac:28:e2:97:3e:39:63",
                    "device_type": "stratum",
                    "discovered_by": "pool-log",
                    "expected_pool_url": pool_ops.default_miner_pool_settings()["pool_url"],
                    "expected_worker_user": worker,
                    "last_workers": [worker],
                    "last_pool_seen_epoch": 100,
                    "managed": True,
                },
                {
                    "ip": "192.168.1.109",
                    "mac": "c8:d3:ff:a3:f4:e1",
                    "device_id": "mac:c8:d3:ff:a3:f4:e1",
                    "device_type": "asic",
                    "discovered_by": "lan-scan",
                    "expected_pool_url": pool_ops.default_miner_pool_settings()["pool_url"],
                    "expected_worker_user": worker,
                    "last_workers": [worker],
                    "last_pool_seen_epoch": 100,
                    "last_share_epoch": 100,
                    "last_share_work_window": 250,
                    "managed": False,
                    "configured": False,
                    "last_configured_ok": False,
                },
            ],
        }
        pool_ops.seconds_since_epoch = lambda: 100 + pool_ops.POOL_CONNECTED_STALE_SECONDS + 10

        health = pool_ops.collect_miner_health()
        miners = {item["ip"]: item for item in health["miners"]}

        self.assertEqual(miners["192.168.1.14"]["work_percent"], "100.00")
        self.assertNotIn("192.168.1.109", miners)

    def test_unmanaged_pool_log_stratum_miner_is_not_marked_configured(self) -> None:
        worker = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        pool_ops.collect_pool_activity = lambda lines=0: {
            "miners": [
                {
                    "ip": "192.168.1.106",
                    "workers": [worker],
                    "shares": 1,
                    "share_work": 500,
                    "blocks_found": 1,
                    "last_seen_at": "2026/05/26 21:40:42",
                }
            ]
        }
        pool_ops.upsert_pool_activity_miners = lambda activity: {
            "updated_at": "2026-05-26T00:00:00+0200",
            "miners": [
                {
                    "ip": "192.168.1.106",
                    "mac": "40:ae:30:34:35:a1",
                    "device_id": "mac:40:ae:30:34:35:a1",
                    "device_type": "stratum",
                    "discovered_by": "pool-log",
                    "expected_pool_url": pool_ops.default_miner_pool_settings()["pool_url"],
                    "expected_worker_user": worker,
                    "last_workers": [worker],
                    "last_pool_seen_epoch": 100,
                    "managed": False,
                    "last_configured_ok": False,
                }
            ],
        }
        pool_ops.seconds_since_epoch = lambda: 110

        health = pool_ops.collect_miner_health()

        self.assertEqual(health["failures"], [])
        self.assertEqual(health["miners"][0]["managed"], False)
        self.assertEqual(health["miners"][0]["configured"], False)
        self.assertEqual(health["miners"][0]["pool_active"], True)
        self.assertEqual(health["miners"][0]["work_pool_active"], True)
        self.assertEqual(health["lane_balance"]["expected_lane_count"], 0)
        self.assertFalse(health["miners"][0]["expected_work_lane"])
        self.assertEqual(health["miners"][0]["expected_work_percent"], "0.00")


if __name__ == "__main__":
    unittest.main()

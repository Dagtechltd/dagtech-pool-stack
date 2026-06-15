#!/usr/bin/env python3

import json
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
        pool_ops.MINER_REGISTRY_FILE = self.registry_file
        pool_ops.MINER_RETIREMENTS_FILE = self.retirements_file
        pool_ops.read_neighbor_macs = lambda: {}
        self.addCleanup(self.restore)

    def restore(self) -> None:
        pool_ops.MINER_REGISTRY_FILE = self.old_registry_file
        pool_ops.MINER_RETIREMENTS_FILE = self.old_retirements_file
        pool_ops.read_neighbor_macs = self.old_read_neighbors

    def test_registry_keeps_distinct_macs_when_ip_is_reused(self) -> None:
        registry = pool_ops.save_miner_registry(
            [
                {"ip": "192.168.1.103", "mac": "28:e2:97:2e:00:1b", "managed": True, "device_type": "asic"},
                {"ip": "192.168.1.103", "mac": "88:a2:9e:a8:02:79", "managed": True, "device_type": "asic"},
            ]
        )

        macs = {item["mac"] for item in registry["miners"]}
        self.assertEqual(macs, {"28:e2:97:2e:00:1b", "88:a2:9e:a8:02:79"})

    def test_display_label_defaults_to_mac_and_suffixes_explicit_name(self) -> None:
        self.assertEqual(
            pool_ops.miner_display_label({"mac": "28:e2:97:2e:00:1b"}),
            "28:e2:97:2e:00:1b",
        )
        self.assertEqual(
            pool_ops.miner_display_label({"display_name": "Athena", "mac": "28:e2:97:2e:00:1b"}),
            "Athena-01b",
        )


class PoolActivityAttributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_read_miner_registry = pool_ops.read_miner_registry
        self.addCleanup(self.restore_registry)

    def restore_registry(self) -> None:
        pool_ops.read_miner_registry = self.old_read_miner_registry

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


class MinerHealthConfiguredScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_collect_pool_activity = pool_ops.collect_pool_activity
        self.old_upsert_pool_activity_miners = pool_ops.upsert_pool_activity_miners
        self.old_seconds_since_epoch = pool_ops.seconds_since_epoch
        self.addCleanup(self.restore)

    def restore(self) -> None:
        pool_ops.collect_pool_activity = self.old_collect_pool_activity
        pool_ops.upsert_pool_activity_miners = self.old_upsert_pool_activity_miners
        pool_ops.seconds_since_epoch = self.old_seconds_since_epoch

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
        self.assertEqual(health["lane_balance"]["expected_lane_count"], 1)


if __name__ == "__main__":
    unittest.main()

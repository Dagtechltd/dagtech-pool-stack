from __future__ import annotations

import importlib.util
import tempfile
import unittest
import unittest.mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "ops" / "fastartifact_source_eligibility.py"


spec = importlib.util.spec_from_file_location("fastartifact_source_eligibility", MODULE_PATH)
eligibility = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(eligibility)


class FastArtifactSourceEligibilityTest(unittest.TestCase):
    def test_active_node_defaults_to_node(self) -> None:
        env = {"BDAG_NODE_SERVICES": "node", "BDAG_NODE_DATA_DIR": "./data/node"}

        self.assertEqual(eligibility.active_node_service(env), "node")
        self.assertTrue(str(eligibility.node_data_dir(env, "node")).endswith("data/node"))

    def test_empty_path_env_values_use_defaults(self) -> None:
        env = {
            "BDAG_NODE_DATA_DIR": "",
            "BDAG_RAWDATADIR_SIDECAR_SOURCE": "",
            "BDAG_RAWDATADIR_SIDECAR_DIR": "",
            "BDAG_RAWDATADIR_ARTIFACT_BASE": "",
        }

        self.assertEqual(eligibility.node_data_dir(env, "node"), eligibility.resolve_path("./data/node"))
        self.assertEqual(
            eligibility.env_path(env, "BDAG_RAWDATADIR_SIDECAR_SOURCE", "./data/node/mainnet"),
            eligibility.resolve_path("./data/node/mainnet"),
        )
        self.assertEqual(
            eligibility.env_path(env, "BDAG_RAWDATADIR_SIDECAR_DIR", "./data-restore/rawdatadir-sidecar/mainnet"),
            eligibility.resolve_path("./data-restore/rawdatadir-sidecar/mainnet"),
        )
        self.assertEqual(
            eligibility.env_path(env, "BDAG_RAWDATADIR_ARTIFACT_BASE", "./data-restore/rawdatadir"),
            eligibility.resolve_path("./data-restore/rawdatadir"),
        )

    def test_path_classification_flags_usb_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)

            def fake_mount_info(_path: Path) -> dict[str, str]:
                return {"source": "/dev/sda1", "fstype": "ext4", "target": str(tmp_path), "options": "rw"}

            def fake_device_facts(_source: str) -> dict[str, object]:
                return {"disk": "sda", "transport": "usb", "removable": False, "hotplug": False}

            with (
                unittest.mock.patch.object(eligibility, "mount_info", fake_mount_info),
                unittest.mock.patch.object(eligibility, "block_device_facts", fake_device_facts),
            ):
                payload = eligibility.classify_path("active_node_datadir", tmp_path)

        self.assertTrue(payload["unsafe"])
        self.assertIn("usb_or_removable", payload["unsafe_reasons"])

    def test_path_classification_flags_removable_mount_path(self) -> None:
        path = Path("/media/user/USB/data")
        with (
            unittest.mock.patch.object(
                eligibility,
                "mount_info",
                lambda _path: {
                    "source": "/dev/nvme0n1p2",
                    "fstype": "ext4",
                    "target": "/media/user/USB",
                    "options": "rw",
                },
            ),
            unittest.mock.patch.object(
                eligibility,
                "block_device_facts",
                lambda _source: {"disk": "nvme0n1", "transport": "nvme", "removable": False, "hotplug": False},
            ),
        ):
            payload = eligibility.classify_path("artifact_base", path)

        self.assertTrue(payload["unsafe"])
        self.assertIn("removable_mount_path", payload["unsafe_reasons"])

    def test_evm_sync_sample_uses_reference_rpc_not_dag_height(self) -> None:
        values = {
            ("http://local:18545", "eth_blockNumber"): 8_000,
            ("http://reference:18545", "eth_blockNumber"): 8_750,
        }

        def fake_quantity(url: str, method: str, timeout: float = 5.0) -> int:
            return values[(url, method)]

        with unittest.mock.patch.object(eligibility, "json_rpc_quantity", fake_quantity):
            payload = eligibility.source_evm_sync_sample(
                {
                    "BDAG_RAWDATADIR_EVM_RPC_URL": "http://local:18545",
                    "BDAG_RAWDATADIR_EVM_REFERENCE_RPC_URLS": "reference=http://reference:18545",
                    "BDAG_RAWDATADIR_MAX_EVM_REFERENCE_LAG": "1000",
                }
            )

        self.assertEqual(payload["local_evm_block"], 8_000)
        self.assertEqual(payload["reference_evm_block"], 8_750)
        self.assertEqual(payload["lag_to_reference"], 750)
        self.assertTrue(payload["fresh"])

    def test_evm_sync_sample_rejects_stale_local_evm(self) -> None:
        values = {
            ("http://local:18545", "eth_blockNumber"): 8_000,
            ("http://reference:18545", "eth_blockNumber"): 9_500,
        }

        with unittest.mock.patch.object(
            eligibility,
            "json_rpc_quantity",
            lambda url, method, timeout=5.0: values[(url, method)],
        ):
            payload = eligibility.source_evm_sync_sample(
                {
                    "BDAG_RAWDATADIR_EVM_RPC_URL": "http://local:18545",
                    "BDAG_RAWDATADIR_EVM_REFERENCE_RPC_URLS": "reference=http://reference:18545",
                    "BDAG_RAWDATADIR_MAX_EVM_REFERENCE_LAG": "1000",
                }
            )

        self.assertEqual(payload["lag_to_reference"], 1500)
        self.assertFalse(payload["fresh"])

    def test_evm_sync_sample_falls_back_to_active_node_container_ip(self) -> None:
        values = {
            ("http://172.18.0.2:18545/", "eth_blockNumber"): 9_000,
            ("http://reference:18545", "eth_blockNumber"): 9_100,
        }

        def fake_quantity(url: str, method: str, timeout: float = 5.0) -> int:
            if url == "http://127.0.0.1:18545":
                raise OSError("connection refused")
            return values[(url, method)]

        with (
            unittest.mock.patch.object(eligibility, "json_rpc_quantity", fake_quantity),
            unittest.mock.patch.object(eligibility, "docker_container_ip", lambda _service: "172.18.0.2"),
        ):
            payload = eligibility.source_evm_sync_sample(
                {
                    "BDAG_NODE_SERVICES": "node",
                    "BDAG_RAWDATADIR_EVM_RPC_URL": "http://127.0.0.1:18545",
                    "BDAG_RAWDATADIR_EVM_REFERENCE_RPC_URLS": "reference=http://reference:18545",
                    "BDAG_RAWDATADIR_MAX_EVM_REFERENCE_LAG": "1000",
                }
            )

        self.assertEqual(payload["local_evm_rpc_url"], "http://172.18.0.2:18545/")
        self.assertEqual(payload["local_evm_block"], 9_000)
        self.assertEqual(payload["lag_to_reference"], 100)
        self.assertTrue(payload["fresh"])

    def test_source_eligibility_rejects_low_io_usb_storage_profile_when_mount_detection_misses(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            env = {
                "BDAG_NODE_SERVICES": "node",
                "BDAG_NODE_DATA_DIR": str(tmp_path / "node"),
                "BDAG_RAWDATADIR_SIDECAR_SOURCE": str(tmp_path / "node" / "mainnet"),
                "BDAG_RAWDATADIR_SIDECAR_DIR": str(tmp_path / "sidecar"),
                "BDAG_RAWDATADIR_ARTIFACT_BASE": str(tmp_path / "artifact"),
                "BDAG_STORAGE_PROFILE": "single-usb-constrained",
                "BDAG_DETECTED_NETWORK_TOPOLOGY": "asic-router",
                "BDAG_RAWDATADIR_MIN_FREE_GIB": "0",
                "BDAG_RAWDATADIR_MIN_RAM_GIB": "0",
                "BDAG_RAWDATADIR_MIN_CPU_COUNT": "1",
            }
            (tmp_path / "artifact").mkdir()

            payload = self._build_payload_with_safe_paths(tmp_path, env)

        self.assertFalse(payload["eligible"])
        self.assertEqual(payload["storage_profile"], "single-usb-constrained")
        self.assertEqual(payload["network_topology"], "asic-router")
        self.assertIn("storage_profile_usb_low_io:single-usb-constrained", payload["reasons"])

    def test_sync_source_node_disabled_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            env = {
                "SYNC_SOURCE_NODE": "0",
                "BDAG_NODE_SERVICES": "node",
                "BDAG_NODE_DATA_DIR": str(tmp_path / "node"),
                "BDAG_RAWDATADIR_SIDECAR_SOURCE": str(tmp_path / "node" / "mainnet"),
                "BDAG_RAWDATADIR_SIDECAR_DIR": str(tmp_path / "sidecar"),
                "BDAG_RAWDATADIR_ARTIFACT_BASE": str(tmp_path / "artifact"),
                "BDAG_RAWDATADIR_MIN_FREE_GIB": "0",
                "BDAG_RAWDATADIR_MIN_RAM_GIB": "0",
                "BDAG_RAWDATADIR_MIN_CPU_COUNT": "1",
            }
            (tmp_path / "artifact").mkdir()

            payload = self._build_payload_with_safe_paths(tmp_path, env)

        self.assertFalse(payload["eligible"])
        self.assertFalse(payload["sync_source_node"])
        self.assertIn("source_mode_disabled", payload["reasons"])

    def test_sync_source_node_enabled_overrides_legacy_no_fastsync_serve(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            env = {
                "SYNC_SOURCE_NODE": "1",
                "BDAG_NO_FASTSYNC_SERVE": "1",
                "BDAG_NODE_SERVICES": "node",
                "BDAG_NODE_DATA_DIR": str(tmp_path / "node"),
                "BDAG_RAWDATADIR_SIDECAR_SOURCE": str(tmp_path / "node" / "mainnet"),
                "BDAG_RAWDATADIR_SIDECAR_DIR": str(tmp_path / "sidecar"),
                "BDAG_RAWDATADIR_ARTIFACT_BASE": str(tmp_path / "artifact"),
                "BDAG_RAWDATADIR_MIN_FREE_GIB": "0",
                "BDAG_RAWDATADIR_MIN_RAM_GIB": "0",
                "BDAG_RAWDATADIR_MIN_CPU_COUNT": "1",
            }
            (tmp_path / "artifact").mkdir()

            payload = self._build_payload_with_safe_paths(tmp_path, env)

        self.assertTrue(payload["eligible"])
        self.assertTrue(payload["sync_source_node"])

    def test_non_mainnet_rawdatadir_network_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            env = {
                "SYNC_SOURCE_NODE": "1",
                "BDAG_FASTSNAP_NETWORK": "not-mainnet",
                "BDAG_NODE_SERVICES": "node",
                "BDAG_NODE_DATA_DIR": str(tmp_path / "node"),
                "BDAG_RAWDATADIR_SIDECAR_SOURCE": str(tmp_path / "node" / "mainnet"),
                "BDAG_RAWDATADIR_SIDECAR_DIR": str(tmp_path / "sidecar"),
                "BDAG_RAWDATADIR_ARTIFACT_BASE": str(tmp_path / "artifact"),
                "BDAG_RAWDATADIR_MIN_FREE_GIB": "0",
                "BDAG_RAWDATADIR_MIN_RAM_GIB": "0",
                "BDAG_RAWDATADIR_MIN_CPU_COUNT": "1",
            }
            (tmp_path / "artifact").mkdir()

            payload = self._build_payload_with_safe_paths(tmp_path, env)

        self.assertFalse(payload["eligible"])
        self.assertEqual(payload["network"], "mainnet")
        self.assertIn("non-mainnet raw datadir network is unsupported:not-mainnet", payload["reasons"])

    def _build_payload_with_safe_paths(self, tmp_path: Path, env: dict[str, str]) -> dict[str, object]:
        def safe_classification(name: str, path: Path) -> dict[str, object]:
            return {
                "name": name,
                "path": str(path),
                "mount": {
                    "source": "/dev/nvme0n1p1",
                    "fstype": "ext4",
                    "target": str(tmp_path),
                    "options": "rw",
                },
                "device": {"disk": "nvme0n1", "transport": "nvme", "removable": False, "hotplug": False},
                "unsafe": False,
                "unsafe_reasons": [],
            }

        fresh_evm_sample = {
            "local_evm_block": 9000,
            "reference_evm_block": 9000,
            "lag_to_reference": 0,
            "max_lag": 1000,
            "fresh": True,
        }
        with (
            unittest.mock.patch.object(eligibility, "load_env", lambda: env),
            unittest.mock.patch.object(eligibility, "classify_path", safe_classification),
            unittest.mock.patch.object(eligibility, "total_memory_bytes", lambda: 16 * 1024**3),
            unittest.mock.patch.object(eligibility, "source_evm_sync_sample", lambda _env: fresh_evm_sample),
        ):
            return eligibility.build_payload(full=False)


if __name__ == "__main__":
    unittest.main()

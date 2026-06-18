import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "ops" / "chain_integrity_gate.py"
SPEC = importlib.util.spec_from_file_location("chain_integrity_gate", MODULE_PATH)
chain_integrity_gate = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(chain_integrity_gate)


def block(order: int, suffix: str | None = None) -> dict[str, Any]:
    token = suffix or f"{order:064x}"
    return {
        "order": order,
        "hash": f"0x{token[-64:]}",
        "stateRoot": f"0xstate{order}",
        "txRoot": f"0xtx{order}",
        "parentroot": f"0xparent{max(0, order - 1)}",
        "parents": [f"0xparent{max(0, order - 1)}"],
        "height": order,
    }


class FakeRpc:
    def __init__(
        self,
        source_blocks: dict[int, dict[str, Any]],
        reference_blocks: dict[int, dict[str, Any]] | None = None,
        source_tip: int | None = None,
        reference_tip: int | None = None,
    ) -> None:
        self.source_blocks = source_blocks
        self.reference_blocks = reference_blocks or source_blocks
        self.source_tip = source_tip if source_tip is not None else max(source_blocks)
        self.reference_tip = reference_tip if reference_tip is not None else max(self.reference_blocks)
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []

    def __call__(
        self,
        url: str,
        method: str,
        params: list[Any],
        _timeout: float,
        _env: Mapping[str, str],
    ) -> Any:
        self.calls.append((url, method, tuple(params)))
        blocks = self.reference_blocks if "reference" in url else self.source_blocks
        tip = self.reference_tip if "reference" in url else self.source_tip
        if method == "getBlockByOrder":
            order = int(params[0])
            return dict(blocks[tip if order < 0 else order])
        if method in {"getBlockTotal", "getBlockCount"}:
            return tip
        if method in {"getBlockhash", "getBlockHash"}:
            return blocks[int(params[0])]["hash"]
        if method in {"getBlockDagInfo", "getBlockDAGInfo", "getNetworkInfo"}:
            return {"network": "mainnet"}
        raise RuntimeError(f"unexpected method {method}")


class ChainIntegrityGateTest(unittest.TestCase):
    def base_env(self) -> dict[str, str]:
        return {
            "BDAG_CHAIN_INTEGRITY_SKIP_ENVIRONMENT_GATES": "1",
            "BDAG_CHAIN_INTEGRITY_MAX_SEGMENT_ORDERS": "128",
        }

    def write_index(self, directory: Path, payload: dict[str, Any]) -> Path:
        path = directory / "latest-index.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def config(self, index: Path, **extra: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "workflow": "ipfs_segment_writer",
            "source_rpc_url": "http://source:38131",
            "reference_rpc_url": "http://reference:38131",
            "index": str(index),
            "start_order": 1,
            "end_order": 2,
        }
        payload.update(extra)
        return payload

    def test_redacted_url_drops_credentials_and_query_tokens(self) -> None:
        redacted = chain_integrity_gate.redacted_url("https://user:pass@example.test:8443/rpc?token=secret")

        self.assertEqual(redacted, "https://example.test:8443/rpc")
        self.assertNotIn("secret", redacted)
        self.assertNotIn("user", redacted)
        self.assertNotIn("pass", redacted)

    def test_trusted_when_segment_matches_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = self.write_index(Path(tmp), {})
            blocks = {0: block(0), 1: block(1), 2: block(2)}
            fake_rpc = FakeRpc(blocks)

            result = chain_integrity_gate.evaluate_chain_integrity(
                self.config(index),
                env=self.base_env(),
                rpc=fake_rpc,
            )

        self.assertEqual(result["state"], "trusted")
        self.assertTrue(result["trusted"])
        self.assertEqual(result["segment_preflight"]["block_count"], 2)
        self.assertRegex(result["segment_preflight"]["canonical_payload_sha256"], r"^[0-9a-f]{64}$")

    def test_source_tip_behind_index_head_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = self.write_index(
                Path(tmp),
                {
                    "segments": [{"segment_id": 1, "start_order": 1, "end_order": 10, "start_hash": block(1)["hash"], "end_hash": block(10)["hash"]}],
                    "current_head": {"end_order": 10, "end_hash": block(10)["hash"]},
                },
            )
            blocks = {order: block(order) for order in range(0, 13)}
            fake_rpc = FakeRpc(blocks, source_tip=5, reference_tip=12)

            result = chain_integrity_gate.evaluate_chain_integrity(
                self.config(index, start_order=11, end_order=12),
                env=self.base_env(),
                rpc=fake_rpc,
            )

        self.assertEqual(result["state"], "rejected_source_unready")
        self.assertIn("source_tip_5_behind_required_12", result["reasons"])

    def test_reference_mismatch_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = self.write_index(Path(tmp), {})
            source_blocks = {0: block(0), 1: block(1), 2: block(2)}
            reference_blocks = {0: block(0), 1: block(1), 2: block(2, "f" * 64)}
            fake_rpc = FakeRpc(source_blocks, reference_blocks)

            result = chain_integrity_gate.evaluate_chain_integrity(
                self.config(index),
                env=self.base_env(),
                rpc=fake_rpc,
            )

        self.assertEqual(result["state"], "rejected_mismatch")
        self.assertTrue(any("order_2_hash" in reason for reason in result["reasons"]))

    def test_index_gap_rejects_before_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = self.write_index(
                Path(tmp),
                {
                    "segments": [
                        {"segment_id": 1, "start_order": 1, "end_order": 5, "start_hash": block(1)["hash"], "end_hash": block(5)["hash"]},
                        {"segment_id": 2, "start_order": 7, "end_order": 10, "start_hash": block(7)["hash"], "end_hash": block(10)["hash"]},
                    ],
                    "current_head": {"end_order": 10, "end_hash": block(10)["hash"]},
                },
            )
            fake_rpc = FakeRpc({0: block(0), 1: block(1), 2: block(2)})

            result = chain_integrity_gate.evaluate_chain_integrity(
                self.config(index),
                env=self.base_env(),
                rpc=fake_rpc,
            )

        self.assertEqual(result["state"], "rejected_index_gap")
        self.assertEqual(fake_rpc.calls, [])

    def test_repair_hold_defers_as_incident(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            index = self.write_index(base, {})
            control = base / "automation-control.json"
            control.write_text(json.dumps({"schema_version": 1, "state": "repair_hold", "owner": "codex"}), encoding="utf-8")
            env = {
                "BDAG_AUTOMATION_CONTROL_FILE": str(control),
                "BDAG_CHAIN_INTEGRITY_INCIDENTS_FILE": str(base / "missing-incidents.jsonl"),
                "BDAG_CHAIN_INTEGRITY_ACTIVE_LOCKS": str(base / "missing.lock"),
            }

            result = chain_integrity_gate.evaluate_chain_integrity(
                self.config(index),
                env=env,
                rpc=FakeRpc({0: block(0), 1: block(1), 2: block(2)}),
            )

        self.assertEqual(result["state"], "deferred_incident")
        self.assertIn("automation_control_state_repair_hold", result["reasons"])

    def test_pressure_defers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            index = self.write_index(base, {})
            control = base / "automation-control.json"
            control.write_text(json.dumps({"schema_version": 1, "state": "normal"}), encoding="utf-8")
            env = {
                "BDAG_AUTOMATION_CONTROL_FILE": str(control),
                "BDAG_CHAIN_INTEGRITY_INCIDENTS_FILE": str(base / "missing-incidents.jsonl"),
                "BDAG_CHAIN_INTEGRITY_ACTIVE_LOCKS": str(base / "missing.lock"),
            }

            def dashboard(_url: str, _timeout: float) -> tuple[dict[str, Any], float]:
                return {
                    "host_pressure": {
                        "samples": [
                            {"io_some_avg10": 6.0, "io_full_avg10": 0.5, "cpu_some_avg10": 1.0, "chain_rpc_p95_ms": 10},
                            {"io_some_avg10": 6.0, "io_full_avg10": 0.5, "cpu_some_avg10": 1.0, "chain_rpc_p95_ms": 10},
                            {"io_some_avg10": 6.0, "io_full_avg10": 0.5, "cpu_some_avg10": 1.0, "chain_rpc_p95_ms": 10},
                        ]
                    }
                }, 0.01

            result = chain_integrity_gate.evaluate_chain_integrity(
                self.config(index),
                env=env,
                rpc=FakeRpc({0: block(0), 1: block(1), 2: block(2)}),
                dashboard_fetcher=dashboard,
            )

        self.assertEqual(result["state"], "deferred_pressure")
        self.assertTrue(any(reason.startswith("io_some_avg10_") for reason in result["reasons"]))

    def test_unsigned_and_do_not_publish_artifact_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            index = self.write_index(base, {})
            artifact = base / "artifact"
            artifact.mkdir()
            manifest = artifact / "manifest.json"
            manifest.write_text(json.dumps({"artifact_type": "raw_datadir_checkpoint"}), encoding="utf-8")
            (artifact / "DO_NOT_PUBLISH.txt").write_text("unsafe\n", encoding="utf-8")

            result = chain_integrity_gate.evaluate_chain_integrity(
                self.config(
                    index,
                    artifact_dir=str(artifact),
                    artifact_manifest=str(manifest),
                    require_signed_manifest=True,
                ),
                env=self.base_env(),
                rpc=FakeRpc({0: block(0), 1: block(1), 2: block(2)}),
            )

        self.assertEqual(result["state"], "rejected_source_unready")
        self.assertIn("manifest_unsigned", result["reasons"])
        self.assertTrue(any(reason.startswith("do_not_publish_marker:") for reason in result["reasons"]))


if __name__ == "__main__":
    unittest.main()

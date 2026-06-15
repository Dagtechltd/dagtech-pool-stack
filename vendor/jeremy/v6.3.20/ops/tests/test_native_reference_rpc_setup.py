#!/usr/bin/env python3

import importlib.util
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "ops" / "setup_native_reference_rpc.py"
SPEC = importlib.util.spec_from_file_location("setup_native_reference_rpc", MODULE_PATH)
setup_native_reference_rpc = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(setup_native_reference_rpc)


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
    def __init__(self, *, reference_native: bool = True, mismatch_genesis: bool = False) -> None:
        self.reference_native = reference_native
        self.source_blocks = {0: block(0), 1: block(1), 2: block(2)}
        self.reference_blocks = dict(self.source_blocks)
        if mismatch_genesis:
            self.reference_blocks[0] = block(0, "f" * 64)
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
        reference = "reference" in url or "38141" in url
        if reference and not self.reference_native and method == "getBlockByOrder":
            raise RuntimeError("the method getBlockByOrder does not exist/is not available")
        blocks = self.reference_blocks if reference else self.source_blocks
        if method == "getBlockByOrder":
            order = int(params[0])
            return dict(blocks[2 if order < 0 else order])
        if method in {"getBlockhash", "getBlockHash"}:
            return blocks[int(params[0])]["hash"]
        if method in {"getBlockDagInfo", "getBlockDAGInfo", "getNetworkInfo"}:
            return {"network": "mainnet"}
        raise RuntimeError(f"unexpected method {method}")


class NativeReferenceRpcSetupTests(unittest.TestCase):
    def test_rejects_evm_only_reference_rpc(self) -> None:
        result = setup_native_reference_rpc.validate_native_reference_rpc(
            "http://reference:38131",
            source_url="http://source:38131",
            rpc=FakeRpc(reference_native=False),
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "native_method_unavailable")
        self.assertIn("getBlockByOrder", result["reasons"][0])

    def test_rejects_reference_same_as_source(self) -> None:
        result = setup_native_reference_rpc.validate_native_reference_rpc(
            "http://source:38131",
            source_url="http://source:38131",
            rpc=FakeRpc(),
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "not_independent")
        self.assertIn("reference_rpc_must_be_independent", result["reasons"])

    def test_valid_reference_writes_all_ipfs_reference_env_keys(self) -> None:
        result = setup_native_reference_rpc.validate_native_reference_rpc(
            "http://reference:38131",
            source_url="http://source:38131",
            rpc=FakeRpc(),
        )
        self.assertTrue(result["ok"])

        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "ops.env"
            setup_native_reference_rpc.apply_validated_env(env_file, "http://reference:38131", result)
            text = env_file.read_text(encoding="utf-8")

        self.assertIn("BDAG_CHAIN_REFERENCE_RPC_URL=http://reference:38131", text)
        self.assertIn("BDAG_IPFS_SEGMENT_REFERENCE_RPC_URL=http://reference:38131", text)
        self.assertIn("BDAG_IPFS_RESTORE_CHAIN_REFERENCE_RPC_URL=http://reference:38131", text)
        self.assertIn("BDAG_NATIVE_REFERENCE_RPC_SETUP_STATUS=validated", text)

    def test_rejects_genesis_mismatch(self) -> None:
        result = setup_native_reference_rpc.validate_native_reference_rpc(
            "http://reference:38131",
            source_url="http://source:38131",
            rpc=FakeRpc(mismatch_genesis=True),
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "mismatch")
        self.assertIn("genesis_hash_mismatch", result["reasons"])

    def test_tunnel_unit_binds_localhost_and_fails_fast(self) -> None:
        unit = setup_native_reference_rpc.tunnel_unit_text(
            ssh_target="jeremy@example.test",
            local_bind="127.0.0.1",
            local_port=38141,
            remote_host="127.0.0.1",
            remote_port=38131,
            key_path=Path("/tmp/key"),
            known_hosts=Path("/tmp/known_hosts"),
        )

        self.assertIn("-L 127.0.0.1:38141:127.0.0.1:38131", unit)
        self.assertIn("ExitOnForwardFailure=yes", unit)
        self.assertIn("BatchMode=yes", unit)
        self.assertIn("Restart=always", unit)


if __name__ == "__main__":
    unittest.main()

import importlib.util
import tempfile
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "verify-rawdatadir-sidecar.py"
spec = importlib.util.spec_from_file_location("verify_rawdatadir_sidecar", MODULE_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RawdatadirSidecarVerifyTest(unittest.TestCase):
    def make_sidecar(self, root: Path) -> Path:
        sidecar = root / "sidecar" / "mainnet"
        chain = sidecar / "BdagChain"
        chain.mkdir(parents=True)
        (chain / "CURRENT").write_text("MANIFEST-000001\n", encoding="utf-8")
        (chain / "MANIFEST-000001").write_text("", encoding="utf-8")
        return sidecar

    def test_safe_sidecar_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = self.make_sidecar(Path(tmp))
            payload = module.verify(sidecar, None, None)
        self.assertTrue(payload["safe"])
        self.assertEqual([], payload["reasons"])
        self.assertEqual(0, payload["unsafe_path_count"])

    def test_lock_and_node_state_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = self.make_sidecar(Path(tmp))
            nodes = sidecar / "bdageth" / "nodes"
            nodes.mkdir(parents=True)
            (sidecar / "bdageth" / "LOCK").write_text("", encoding="utf-8")
            payload = module.verify(sidecar, None, None)
        self.assertFalse(payload["safe"])
        self.assertIn("unsafe_ephemeral_or_private_paths_present", payload["reasons"])
        self.assertIn("bdageth/LOCK", payload["unsafe_paths"])
        self.assertIn("bdageth/nodes", payload["unsafe_paths"])

    def test_missing_chain_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = Path(tmp) / "sidecar" / "mainnet"
            sidecar.mkdir(parents=True)
            payload = module.verify(sidecar, None, None)
        self.assertFalse(payload["safe"])
        self.assertIn("missing_BdagChain", payload["reasons"])


if __name__ == "__main__":
    unittest.main()

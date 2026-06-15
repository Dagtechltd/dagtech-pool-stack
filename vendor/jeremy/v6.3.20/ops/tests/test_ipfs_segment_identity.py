import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "ops" / "ipfs_segment_identity.py"
SPEC = importlib.util.spec_from_file_location("ipfs_segment_identity", MODULE_PATH)
ipfs_segment_identity = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(ipfs_segment_identity)


def load_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if "=" not in raw or raw.startswith("#"):
            continue
        key, value = raw.split("=", 1)
        result[key] = value
    return result


class IPFSSegmentIdentityTest(unittest.TestCase):
    def test_ensure_identity_creates_stable_key_backed_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            env_file = base / ".env"
            env_file.write_text("BDAG_IPFS_SEGMENT_WRITER_MODE=auto\n", encoding="utf-8")

            first = ipfs_segment_identity.ensure_identity(env_file)
            second = ipfs_segment_identity.ensure_identity(env_file)
            env = load_env(env_file)
            key_exists = (base / "ops/runtime/ipfs-content/segment-writer.key").exists()

        self.assertTrue(first["created_key"])
        self.assertFalse(second["created_key"])
        self.assertEqual(first["writer_id"], second["writer_id"])
        self.assertEqual(first["public_key_hex"], second["public_key_hex"])
        self.assertEqual(env["BDAG_IPFS_SEGMENT_WRITER_ID"], first["writer_id"])
        self.assertIn(f"{first['writer_id']}={first['public_key_hex']}", env["BDAG_IPFS_SEGMENT_TRUSTED_SIGNERS"])
        self.assertIn(f"{first['writer_id']}={first['public_key_hex']}", env["BDAG_RAWDATADIR_TRUSTED_SIGNERS"])
        self.assertIn(first["writer_id"], env["BDAG_IPFS_SEGMENT_WRITER_ROSTER"])
        self.assertEqual(env["BDAG_IPFS_SEGMENT_REQUIRE_SIGNATURES"], "1")
        self.assertEqual(env["BDAG_IPFS_RESTORE_REQUIRE_SIGNATURES"], "1")
        self.assertEqual(env["BDAG_RAWDATADIR_SIGNING_KEY_ID"], first["writer_id"])
        self.assertEqual(env["BDAG_RAWDATADIR_REQUIRE_TRUSTED_SIGNER"], "1")
        self.assertNotIn("BDAG_IPFS_SEGMENT_SIGNING_KEY_HEX", env)
        self.assertNotIn("BDAG_RAWDATADIR_SIGNING_KEY_HEX", env)
        self.assertTrue(key_exists)


if __name__ == "__main__":
    unittest.main()

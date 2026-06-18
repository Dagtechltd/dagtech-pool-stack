import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ChainStateSelfHealMainnetOnlyTest(unittest.TestCase):
    def test_self_heal_refuses_non_mainnet_network_and_pins_restore_path(self) -> None:
        script = (ROOT / "ops" / "chain-state-self-heal.sh").read_text(encoding="utf-8")

        self.assertIn("chain-state self-heal refuses non-mainnet network", script)
        self.assertIn('NETWORK="mainnet"', script)
        self.assertIn('NODE_NETWORK_DIR="$NODE_DATA_DIR/$NETWORK"', script)
        self.assertNotIn('${NETWORK:-mainnet}', script)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3

from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


ADDRESS = "0x1111111111111111111111111111111111111111"


class PoolOpsEnvAliasTests(unittest.TestCase):
    def test_compose_mining_pool_address_is_seeded_from_mining_address(self) -> None:
        with mock.patch.dict(pool_ops.os.environ, {"MINING_ADDRESS": ADDRESS}, clear=True):
            pool_ops.apply_stack_env_aliases()

            self.assertEqual(ADDRESS, pool_ops.os.environ["MINING_POOL_ADDRESS"])

    def test_mining_address_is_seeded_from_compose_mining_pool_address(self) -> None:
        with mock.patch.dict(pool_ops.os.environ, {"MINING_POOL_ADDRESS": ADDRESS}, clear=True):
            pool_ops.apply_stack_env_aliases()

            self.assertEqual(ADDRESS, pool_ops.os.environ["MINING_ADDRESS"])

    def test_read_env_value_accepts_mining_pool_address_alias(self) -> None:
        with mock.patch.dict(pool_ops.os.environ, {"MINING_POOL_ADDRESS": ADDRESS}, clear=True):
            self.assertEqual(ADDRESS, pool_ops.read_env_value("MINING_ADDRESS"))


if __name__ == "__main__":
    unittest.main()

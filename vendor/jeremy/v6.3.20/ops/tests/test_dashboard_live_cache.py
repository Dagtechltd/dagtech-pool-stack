#!/usr/bin/env python3

import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import dashboard  # noqa: E402


class DashboardLiveCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        dashboard.API_CACHE.clear()
        self.addCleanup(dashboard.API_CACHE.clear)

    def test_cached_payload_zero_ttl_calls_factory_every_time(self) -> None:
        calls = []

        def factory() -> dict[str, int]:
            calls.append(1)
            return {"call": len(calls)}

        self.assertEqual(dashboard.cached_payload("earnings", 0, factory), {"call": 1})
        self.assertEqual(dashboard.cached_payload("earnings", 0, factory), {"call": 2})
        self.assertNotIn("earnings", dashboard.API_CACHE)


if __name__ == "__main__":
    unittest.main()

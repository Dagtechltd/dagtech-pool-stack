#!/usr/bin/env python3
import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import compose_migrations  # noqa: E402


class RuntimeComposeMigrationTests(unittest.TestCase):
    def test_release_env_defaults_keep_sync_source_and_reconnect_guards(self) -> None:
        env_example = (OPS_DIR.parent / ".env.example").read_text()

        self.assertIn("SYNC_SOURCE_NODE=0\n", env_example)
        self.assertIn("POOL_EXPIRED_JOB_CLIENT_RECONNECT_THRESHOLD=3\n", env_example)
        self.assertIn("POOL_EXPIRED_JOB_CLIENT_RECONNECT_WINDOW_SECONDS=120\n", env_example)
        self.assertIn("POOL_EXPIRED_JOB_CLIENT_RECONNECT_COOLDOWN_SECONDS=60\n", env_example)

    def test_adds_submit_hardening_flags_to_each_existing_pool_service(self) -> None:
        compose = """services:
  asic-pool:
    environment:
      NODE_RPC_URLS: http://node:38131
      NODE_RPC_USER: ${NODE_RPC_USER:-test}
  asic-pool-hector:
    environment:
      NODE_RPC_URLS: http://node:38131
      NODE_RPC_USER: ${NODE_RPC_USER:-test}
  node:
    environment:
      NODE_RPC_URLS: unused
"""

        result = compose_migrations.ensure_pool_submit_hardening_flags(compose)

        self.assertTrue(result.changed)
        self.assertEqual(12, result.inserted_count)
        self.assertIn(
            "      POOL_SUBMIT_STALE_BLOCK_CANDIDATES: ${POOL_SUBMIT_STALE_BLOCK_CANDIDATES:-false}\n"
            "      POOL_SUBMIT_BLOCK_HEADER_V2_ENABLED: ${POOL_SUBMIT_BLOCK_HEADER_V2_ENABLED:-true}\n"
            "      POOL_STALE_RACE_CLIENT_RESEND_THRESHOLD: ${POOL_STALE_RACE_CLIENT_RESEND_THRESHOLD:-1}\n"
            "      POOL_EXPIRED_JOB_CLIENT_RECONNECT_THRESHOLD: ${POOL_EXPIRED_JOB_CLIENT_RECONNECT_THRESHOLD:-3}\n"
            "      POOL_EXPIRED_JOB_CLIENT_RECONNECT_WINDOW_SECONDS: ${POOL_EXPIRED_JOB_CLIENT_RECONNECT_WINDOW_SECONDS:-120}\n"
            "      POOL_EXPIRED_JOB_CLIENT_RECONNECT_COOLDOWN_SECONDS: ${POOL_EXPIRED_JOB_CLIENT_RECONNECT_COOLDOWN_SECONDS:-60}\n"
            "      NODE_RPC_USER:",
            result.text,
        )
        self.assertIn(
            "  asic-pool-hector:\n"
            "    environment:\n"
            "      NODE_RPC_URLS: http://node:38131\n"
            "      POOL_SUBMIT_STALE_BLOCK_CANDIDATES: ${POOL_SUBMIT_STALE_BLOCK_CANDIDATES:-false}\n"
            "      POOL_SUBMIT_BLOCK_HEADER_V2_ENABLED: ${POOL_SUBMIT_BLOCK_HEADER_V2_ENABLED:-true}\n"
            "      POOL_STALE_RACE_CLIENT_RESEND_THRESHOLD: ${POOL_STALE_RACE_CLIENT_RESEND_THRESHOLD:-1}\n"
            "      POOL_EXPIRED_JOB_CLIENT_RECONNECT_THRESHOLD: ${POOL_EXPIRED_JOB_CLIENT_RECONNECT_THRESHOLD:-3}\n"
            "      POOL_EXPIRED_JOB_CLIENT_RECONNECT_WINDOW_SECONDS: ${POOL_EXPIRED_JOB_CLIENT_RECONNECT_WINDOW_SECONDS:-120}\n"
            "      POOL_EXPIRED_JOB_CLIENT_RECONNECT_COOLDOWN_SECONDS: ${POOL_EXPIRED_JOB_CLIENT_RECONNECT_COOLDOWN_SECONDS:-60}\n"
            "      NODE_RPC_USER:",
            result.text,
        )
        self.assertNotIn(
            "node:\n"
            "    environment:\n"
            "      POOL_SUBMIT_STALE_BLOCK_CANDIDATES",
            result.text,
        )

    def test_existing_submit_hardening_flags_are_noop(self) -> None:
        compose = """services:
  asic-pool:
    environment:
      NODE_RPC_URLS: http://node:38131
      POOL_SUBMIT_STALE_BLOCK_CANDIDATES: ${POOL_SUBMIT_STALE_BLOCK_CANDIDATES:-false}
      POOL_SUBMIT_BLOCK_HEADER_V2_ENABLED: ${POOL_SUBMIT_BLOCK_HEADER_V2_ENABLED:-true}
      POOL_STALE_RACE_CLIENT_RESEND_THRESHOLD: ${POOL_STALE_RACE_CLIENT_RESEND_THRESHOLD:-1}
      POOL_EXPIRED_JOB_CLIENT_RECONNECT_THRESHOLD: ${POOL_EXPIRED_JOB_CLIENT_RECONNECT_THRESHOLD:-3}
      POOL_EXPIRED_JOB_CLIENT_RECONNECT_WINDOW_SECONDS: ${POOL_EXPIRED_JOB_CLIENT_RECONNECT_WINDOW_SECONDS:-120}
      POOL_EXPIRED_JOB_CLIENT_RECONNECT_COOLDOWN_SECONDS: ${POOL_EXPIRED_JOB_CLIENT_RECONNECT_COOLDOWN_SECONDS:-60}
"""

        result = compose_migrations.ensure_pool_submit_hardening_flags(compose)

        self.assertFalse(result.changed)
        self.assertEqual(0, result.inserted_count)
        self.assertEqual(compose, result.text)

    def test_missing_pool_service_is_reported_as_unmodified(self) -> None:
        compose = """services:
  node:
    environment:
      NODE_RPC_URLS: unused
"""

        result = compose_migrations.ensure_pool_submit_hardening_flags(compose)

        self.assertFalse(result.changed)
        self.assertEqual(0, result.inserted_count)


if __name__ == "__main__":
    unittest.main()

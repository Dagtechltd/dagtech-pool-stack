#!/usr/bin/env python3

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402
import pool_start_gate  # noqa: E402
import stack_sentinel  # noqa: E402
import status_sampler  # noqa: E402
import watchdog  # noqa: E402


def unsafe_catchup_status() -> dict[str, object]:
    return {
        "fresh": True,
        "mode": "catchup_pause",
        "overall": "syncing",
        "status_reason": "catch-up pause active: node is behind peers",
        "catchup_policy": {"active": True},
        "sync_health": {"catchup_pause_active": True},
    }


def safe_canonical_status() -> dict[str, object]:
    return {
        "fresh": True,
        "mode": "synced",
        "overall": "ok",
        "sync_progress": {
            "status": "synced",
            "remaining_blocks": 0,
            "nodes": {
                "blockdag-node-1": {
                    "canonical_mining_safety": {
                        "safe": True,
                        "schema": "stack_evm_public_reference_v1",
                        "reason": "external public-chain proof matches local node",
                    }
                }
            },
        },
        "rpc_template_health": {"all_nodes_ready": True},
    }


class SingleGateOrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_stack_services = list(pool_ops.STACK_SERVICES)
        self.addCleanup(self.restore)

    def restore(self) -> None:
        pool_ops.STACK_SERVICES = self.original_stack_services

    def fake_inspect(self, labels: dict[str, str]):
        def run(command, **_kwargs):
            name = command[-1]
            service = labels.get(name)
            if service is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="not found")
            return SimpleNamespace(returncode=0, stdout=f"{service}\n", stderr="")

        return run

    def test_shared_gate_blocks_catchup_status(self) -> None:
        decision = pool_start_gate.pool_start_decision(unsafe_catchup_status())

        self.assertFalse(decision.allowed)
        self.assertIn("chain catch-up pause is active", decision.reason)
        self.assertIn("overall stack status is syncing", decision.reason)

    def test_shared_gate_blocks_sync_progress_even_when_top_level_is_mining(self) -> None:
        status = safe_canonical_status()
        status["mode"] = "mining"
        status["overall"] = "ok"
        sync_progress = status["sync_progress"]
        sync_progress["status"] = "syncing"
        sync_progress["remaining_blocks"] = 5

        decision = pool_start_gate.pool_start_decision(status)

        self.assertFalse(decision.allowed)
        self.assertIn("sync progress is syncing with 5 block(s) remaining", decision.reason)

    def test_shared_gate_blocks_synced_status_without_canonical_proof(self) -> None:
        decision = pool_start_gate.pool_start_decision(
            {"fresh": True, "mode": "synced", "overall": "ok", "rpc_template_health": {"all_nodes_ready": True}}
        )

        self.assertFalse(decision.allowed)
        self.assertIn("canonical public-chain safety proof is missing", decision.reason)

    def test_shared_gate_allows_ready_status_with_canonical_proof(self) -> None:
        decision = pool_start_gate.pool_start_decision(safe_canonical_status())

        self.assertTrue(decision.allowed, decision.reason)

    def test_status_sampler_cannot_direct_start_pool_when_gate_blocks(self) -> None:
        incidents: list[tuple[str, str]] = []
        with mock.patch.object(status_sampler, "run", side_effect=AssertionError("docker start must not run")), mock.patch.object(
            status_sampler, "log", lambda _message: None
        ), mock.patch.object(
            status_sampler.automation_control,
            "check_mutation_allowed",
            return_value=SimpleNamespace(allowed=True, reason="unit test allow"),
        ), mock.patch.object(
            status_sampler,
            "record_incident",
            side_effect=lambda event_type, severity, *_args: incidents.append((event_type, severity)),
        ):
            ok = status_sampler.start_pool_container(unsafe_catchup_status(), "unit test")

        self.assertFalse(ok)
        self.assertEqual([("mining_imperative_pool_start_blocked", "warning")], incidents)

    def test_stack_sentinel_cannot_start_pool_when_latest_status_blocks(self) -> None:
        incidents: list[tuple[str, str]] = []
        with mock.patch.object(
            stack_sentinel.pool_start_gate,
            "read_latest_status_payload",
            return_value=unsafe_catchup_status(),
        ), mock.patch.object(
            stack_sentinel, "automation_mutation_allowed", return_value=True
        ), mock.patch.object(
            stack_sentinel, "run_logged", side_effect=AssertionError("docker start must not run")
        ), mock.patch.object(
            stack_sentinel, "log", lambda _message: None
        ), mock.patch.object(
            stack_sentinel,
            "append_incident",
            side_effect=lambda event_type, severity, *_args, **_kwargs: incidents.append((event_type, severity)),
        ):
            ok = stack_sentinel.start_container(stack_sentinel.POOL_CONTAINER, "unit test", {}, 123)

        self.assertFalse(ok)
        self.assertEqual([("sentinel_pool_start_blocked", "warning")], incidents)

    def test_watchdog_cannot_restart_pool_when_latest_status_blocks(self) -> None:
        events: list[tuple[str, str]] = []
        with mock.patch.object(
            watchdog.pool_start_gate,
            "read_latest_status_payload",
            return_value=unsafe_catchup_status(),
        ), mock.patch.object(
            watchdog, "automation_mutation_allowed", return_value=True
        ), mock.patch.object(
            watchdog, "acquire_lock", side_effect=AssertionError("repair lock must not be acquired")
        ), mock.patch.object(
            watchdog, "run_logged", side_effect=AssertionError("docker restart must not run")
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog,
            "record_efficiency_event",
            side_effect=lambda event_type, severity, *_args, **_kwargs: events.append((event_type, severity)),
        ):
            ok = watchdog.run_pool_restart("unit test")

        self.assertFalse(ok)
        self.assertEqual([("pool_restart_blocked", "warning")], events)

    def test_generic_stack_start_excludes_pool_when_gate_blocks(self) -> None:
        pool_ops.STACK_SERVICES = [
            "blockdag-postgres-1",
            "blockdag-node-1",
            "blockdag-pool-1",
        ]
        labels = {
            "blockdag-postgres-1": "postgres",
            "blockdag-node-1": "node",
            "blockdag-pool-1": "pool",
        }
        commands: list[list[str]] = []

        class Result:
            ok = True

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            pool_ops.subprocess, "run", side_effect=self.fake_inspect(labels)
        ), mock.patch.object(
            pool_ops.pool_start_gate, "read_latest_status_payload", return_value=unsafe_catchup_status()
        ), mock.patch.object(
            pool_ops, "run_logged", side_effect=lambda command, *_args, **_kwargs: commands.append(command) or Result()
        ), mock.patch.object(
            pool_ops, "stop_planned_sync_service", return_value=True
        ):
            ok = pool_ops.start_stack(pathlib.Path(tmpdir) / "start.log")

        self.assertTrue(ok)
        self.assertEqual(1, len(commands))
        self.assertIn("postgres", commands[0])
        self.assertIn("node", commands[0])
        self.assertNotIn("pool", commands[0])

    def test_non_pool_start_command_never_falls_back_to_all_services(self) -> None:
        pool_ops.STACK_SERVICES = ["blockdag-pool-1"]
        with mock.patch.object(
            pool_ops.subprocess,
            "run",
            side_effect=self.fake_inspect({"blockdag-pool-1": "pool"}),
        ):
            command = pool_ops.docker_compose_start_command(include_pool=False)

        self.assertEqual([], command)


if __name__ == "__main__":
    unittest.main()

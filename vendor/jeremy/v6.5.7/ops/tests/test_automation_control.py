#!/usr/bin/env python3

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
import unittest.mock


OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import automation_control  # noqa: E402
import guard_core  # noqa: E402
import pool_ops  # noqa: E402
import stack_sentinel  # noqa: E402
import status_sampler  # noqa: E402
import watchdog  # noqa: E402


class AutomationControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.state_path = self.root / "automation-control.json"
        self.lock_path = self.root / "automation-control.lock"
        self.event_path = self.root / "automation-control-events.jsonl"
        self.now = datetime(2026, 5, 31, 21, 0, tzinfo=timezone.utc)

    def control_state(
        self,
        state: str,
        *,
        expires_at: datetime | None = None,
        allowed_mutations: list[str] | None = None,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "state": state,
            "owner": "operator",
            "owner_unit": "test",
            "pid": 123,
            "reason": "unit test",
            "correlation_id": "test",
            "created_at": self.now.isoformat(),
            "updated_at": self.now.isoformat(),
            "expires_at": expires_at.isoformat() if expires_at else None,
            "allowed_mutations": allowed_mutations or [],
            "suppressed_count": 0,
            "last_transition": {"from": "normal", "to": state, "at": self.now.isoformat(), "by": "test"},
        }

    def write_state(self, state: dict[str, object]) -> None:
        automation_control.write_control_state(
            state,
            state_path=self.state_path,
            lock_path=self.lock_path,
            now=self.now,
        )

    def check(self, action: str = automation_control.ACTION_ASIC_POOL_START, target: str = "asic-pool"):
        return automation_control.check_mutation_allowed(
            action,
            actor="watchdog",
            target=target,
            reason="test mutation",
            state_path=self.state_path,
            event_path=self.event_path,
            lock_path=self.lock_path,
            now=self.now,
        )

    def event_lines(self) -> list[dict[str, object]]:
        if not self.event_path.exists():
            return []
        return [json.loads(line) for line in self.event_path.read_text(encoding="utf-8").splitlines()]

    def test_missing_control_denies_high_risk_mutation_and_logs(self) -> None:
        decision = self.check()

        self.assertFalse(decision.allowed)
        self.assertEqual("missing", decision.control_status)
        events = self.event_lines()
        self.assertEqual(1, len(events))
        self.assertEqual("automation_control_denied", events[0]["event_type"])

    def test_corrupt_control_denies_high_risk_mutation_and_logs(self) -> None:
        self.state_path.write_text("{not-json", encoding="utf-8")

        decision = self.check()

        self.assertFalse(decision.allowed)
        self.assertEqual("corrupt", decision.control_status)
        self.assertEqual(1, len(self.event_lines()))

    def test_schema_invalid_control_denies_high_risk_mutation_and_logs(self) -> None:
        self.state_path.write_text(json.dumps({"schema_version": 1, "state": "normal"}), encoding="utf-8")

        decision = self.check()

        self.assertFalse(decision.allowed)
        self.assertEqual("schema_invalid", decision.control_status)
        self.assertEqual(1, len(self.event_lines()))

    def test_expired_control_denies_high_risk_mutation_and_logs(self) -> None:
        expired = self.control_state("normal", expires_at=self.now - timedelta(seconds=1))
        self.state_path.write_text(json.dumps(expired), encoding="utf-8")

        decision = self.check()

        self.assertFalse(decision.allowed)
        self.assertEqual("expired", decision.control_status)
        self.assertEqual(1, len(self.event_lines()))

    def test_repair_hold_controlled_stop_and_chain_incident_deny_high_risk_mutations(self) -> None:
        for state in ("repair_hold", "controlled_stop", "chain_incident"):
            with self.subTest(state=state):
                self.write_state(self.control_state(state))
                self.event_path.unlink(missing_ok=True)

                decision = self.check()

                self.assertFalse(decision.allowed)
                self.assertEqual(state, decision.control_state)
                self.assertIn("denies high-risk", decision.reason)
                self.assertEqual(1, len(self.event_lines()))

    def test_normal_control_allows_high_risk_mutation(self) -> None:
        self.write_state(self.control_state("normal"))

        decision = self.check()

        self.assertTrue(decision.allowed)
        self.assertEqual([], self.event_lines())

    def test_ensure_normal_control_state_creates_missing_control(self) -> None:
        created, previous_status, path = automation_control.ensure_normal_control_state(
            state_path=self.state_path,
            lock_path=self.lock_path,
            owner="unit-test",
            owner_unit="test_automation_control",
            reason="create missing default",
            correlation_id="test-create",
            now=self.now,
        )

        self.assertTrue(created)
        self.assertEqual("missing", previous_status)
        self.assertEqual(str(self.state_path), path)
        control, status, _reason = automation_control.read_control_state(
            state_path=self.state_path,
            now=self.now,
        )
        self.assertEqual("ok", status)
        self.assertIsNotNone(control)
        self.assertEqual("normal", control["state"])

    def test_ensure_normal_control_state_preserves_existing_hold(self) -> None:
        self.write_state(self.control_state("repair_hold"))

        created, previous_status, _path = automation_control.ensure_normal_control_state(
            state_path=self.state_path,
            lock_path=self.lock_path,
            owner="unit-test",
            owner_unit="test_automation_control",
            reason="must not overwrite hold",
            now=self.now,
        )

        self.assertFalse(created)
        self.assertEqual("ok", previous_status)
        control, status, _reason = automation_control.read_control_state(
            state_path=self.state_path,
            now=self.now,
        )
        self.assertEqual("ok", status)
        self.assertEqual("repair_hold", control["state"])

    def test_ensure_normal_control_state_leaves_corrupt_file_without_repair_flag(self) -> None:
        self.state_path.write_text("{not-json", encoding="utf-8")

        created, previous_status, _path = automation_control.ensure_normal_control_state(
            state_path=self.state_path,
            lock_path=self.lock_path,
            owner="unit-test",
            owner_unit="test_automation_control",
            reason="must not overwrite corrupt file by default",
            now=self.now,
        )

        self.assertFalse(created)
        self.assertEqual("corrupt", previous_status)
        self.assertEqual("{not-json", self.state_path.read_text(encoding="utf-8"))

    def test_transition_hold_requires_exact_allowlist_match(self) -> None:
        self.write_state(self.control_state("transition_hold"))

        denied = self.check()

        self.assertFalse(denied.allowed)
        self.write_state(self.control_state("transition_hold", allowed_mutations=["asic-pool"]))

        target_only = self.check()

        self.assertFalse(target_only.allowed)
        self.write_state(
            self.control_state(
                "transition_hold",
                allowed_mutations=[automation_control.ACTION_ASIC_POOL_START],
            )
        )

        action_only_with_target = self.check()

        self.assertFalse(action_only_with_target.allowed)
        self.write_state(
            self.control_state(
                "transition_hold",
                allowed_mutations=[f"{automation_control.ACTION_ASIC_POOL_START}:asic-pool"],
            )
        )

        allowed = self.check()

        self.assertTrue(allowed.allowed)

    def test_transition_hold_allows_target_wildcard_for_specific_action_only(self) -> None:
        self.write_state(
            self.control_state(
                "transition_hold",
                allowed_mutations=[f"{automation_control.ACTION_ASIC_MINER_OPEN_RESTART}:*"],
            )
        )

        allowed = self.check(
            automation_control.ACTION_ASIC_MINER_OPEN_RESTART,
            target="192.168.1.16",
        )
        broader_restart = self.check(
            automation_control.ACTION_ASIC_MINER_RESTART,
            target="192.168.1.16",
        )

        self.assertTrue(allowed.allowed)
        self.assertFalse(broader_restart.allowed)

    def patch_default_control_paths(self):
        return unittest.mock.patch.multiple(
            automation_control,
            DEFAULT_STATE_PATH=self.state_path,
            DEFAULT_LOCK_PATH=self.lock_path,
            DEFAULT_EVENT_PATH=self.event_path,
        )

    def test_watchdog_suppresses_current_stack_rpc_and_pool_repairs_when_control_missing(self) -> None:
        calls: list[str] = []
        events: list[tuple[str, str, str]] = []

        def fake_record(event_type: str, severity: str, component: str, message: str, details=None) -> None:
            events.append((event_type, severity, message))

        with self.patch_default_control_paths(), unittest.mock.patch.object(
            watchdog, "log", lambda _message: None
        ), unittest.mock.patch.object(
            watchdog, "record_efficiency_event", fake_record
        ), unittest.mock.patch.object(
            watchdog, "acquire_lock", side_effect=AssertionError("lock should not be acquired")
        ), unittest.mock.patch.object(
            watchdog, "start_stack", side_effect=lambda _log_path: calls.append("start_stack") or True
        ), unittest.mock.patch.object(
            watchdog, "run_logged", side_effect=AssertionError("docker command should not run")
        ):
            self.assertFalse(
                watchdog.run_repair("start", "asic-pool is not running")
            )
            self.assertFalse(watchdog.run_pool_restart("submit-path stall"))

        self.assertEqual([], calls)
        self.assertGreaterEqual(len(self.event_lines()), 2)
        self.assertTrue(all(item[0] == "automation_control_suppressed" for item in events))

    def test_watchdog_suppresses_asic_miner_restart_when_control_missing(self) -> None:
        events: list[tuple[str, str, str]] = []

        def fake_record(event_type: str, severity: str, component: str, message: str, details=None) -> None:
            events.append((event_type, severity, message))

        with self.patch_default_control_paths(), unittest.mock.patch.object(
            watchdog, "log", lambda _message: None
        ), unittest.mock.patch.object(
            guard_core, "append_incident", fake_record
        ), unittest.mock.patch.object(
            watchdog, "read_miner_admin_password", side_effect=AssertionError("password should not be read")
        ), unittest.mock.patch.object(
            watchdog, "acquire_lock", side_effect=AssertionError("lock should not be acquired")
        ):
            result = watchdog.run_miner_restarts([{"ip": "192.168.68.10"}], "ASIC hashrate watchdog")

        self.assertEqual("suppressed", result["status"])
        self.assertEqual(1, len(self.event_lines()))
        self.assertEqual("automation_control_suppressed", events[0][0])

    def test_status_sampler_suppresses_systemd_start_when_control_missing(self) -> None:
        incidents: list[tuple[str, str, str]] = []
        commands: list[list[str]] = []

        def fake_run(command: list[str], timeout: int = 20):
            commands.append(command)
            if command[:3] == ["systemctl", "--user", "is-enabled"]:
                return pool_ops.CommandResult(command, 1, "disabled\n", "", 0.0)
            if command[:3] == ["systemctl", "--user", "is-active"]:
                return pool_ops.CommandResult(command, 3, "inactive\n", "", 0.0)
            raise AssertionError("systemd start must not run when automation control is missing")

        with self.patch_default_control_paths(), unittest.mock.patch.object(
            status_sampler, "run", fake_run
        ), unittest.mock.patch.object(
            status_sampler, "log", lambda _message: None
        ), unittest.mock.patch.object(
            status_sampler,
            "record_incident",
            side_effect=lambda event_type, severity, message, *_args, **_kwargs: incidents.append(
                (event_type, severity, message)
            ),
        ):
            ok = status_sampler.ensure_user_unit("bdag-stack-sentinel.timer", {})

        self.assertFalse(ok)
        self.assertEqual(
            [
                ["systemctl", "--user", "is-enabled", "bdag-stack-sentinel.timer"],
                ["systemctl", "--user", "is-active", "bdag-stack-sentinel.timer"],
            ],
            commands,
        )
        self.assertEqual(1, len(self.event_lines()))
        self.assertEqual("mining_imperative_user_unit_start_blocked", incidents[0][0])

    def test_watchdog_api_stall_miner_restart_uses_open_restart_before_configure(self) -> None:
        self.write_state(
            self.control_state(
                "transition_hold",
                allowed_mutations=[f"{automation_control.ACTION_ASIC_MINER_OPEN_RESTART}:*"],
            )
        )
        action_log = self.root / "action-restart-miners.log"
        lock_handle = unittest.mock.Mock()
        writes: list[dict[str, object]] = []

        with self.patch_default_control_paths(), unittest.mock.patch.object(
            watchdog, "log", lambda _message: None
        ), unittest.mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), unittest.mock.patch.object(
            watchdog, "read_miner_admin_password", return_value="redacted"
        ), unittest.mock.patch.object(
            watchdog, "acquire_lock", return_value=lock_handle
        ), unittest.mock.patch.object(
            watchdog, "action_log_path", return_value=action_log
        ), unittest.mock.patch.object(
            watchdog, "write_action_state", side_effect=lambda payload: writes.append(dict(payload))
        ), unittest.mock.patch.object(
            watchdog, "restart_miner_open", return_value={"ip": "192.168.1.16", "status": "ok", "response": ""}
        ) as open_restart, unittest.mock.patch.object(
            watchdog, "restart_miner", side_effect=AssertionError("auth restart should not be used")
        ), unittest.mock.patch.object(
            watchdog, "configure_miner", side_effect=AssertionError("API-stall repair must not rewrite config first")
        ):
            result = watchdog.run_miner_restarts(
                [{"ip": "192.168.1.16", "configured": False, "restart_open_first": True}],
                "ASIC API-stall watchdog: local API timed out",
            )

        self.assertEqual("ok", result["status"])
        self.assertEqual("restart-open-api-stall", result["results"][0]["action"])
        open_restart.assert_called_once_with("192.168.1.16")
        lock_handle.close.assert_called_once()
        self.assertEqual("running", writes[0]["status"])
        self.assertEqual("ok", writes[-1]["status"])

    def test_sentinel_suppresses_pool_starts_when_control_missing(self) -> None:
        incidents: list[tuple[str, str, str, str]] = []

        def fake_incident(event_type: str, severity: str, component: str, message: str, details=None) -> None:
            incidents.append((event_type, severity, component, message))

        state: dict[str, object] = {}
        with self.patch_default_control_paths(), unittest.mock.patch.object(
            stack_sentinel, "log", lambda _message: None
        ), unittest.mock.patch.object(
            stack_sentinel, "append_incident", fake_incident
        ), unittest.mock.patch.object(
            stack_sentinel, "run_logged", side_effect=AssertionError("docker command should not run")
        ):
            self.assertFalse(
                stack_sentinel.start_container(
                    stack_sentinel.POOL_CONTAINER,
                    "ASIC pool container is stopped",
                    state,
                    100,
                )
            )
            self.assertFalse(
                stack_sentinel.recreate_container(
                    stack_sentinel.POOL_CONTAINER,
                    "ASIC pool container is restarting",
                    state,
                    101,
                )
            )
            self.assertFalse(
                stack_sentinel.start_container(
                    stack_sentinel.POOL_CONTAINER,
                    "ASIC pool container is stopped",
                    state,
                    102,
                )
            )

        self.assertGreaterEqual(len(self.event_lines()), 3)
        self.assertTrue(all(item[0] == "automation_control_suppressed" for item in incidents))

    def test_sentinel_blocks_pool_start_when_status_cannot_prove_safe(self) -> None:
        blocked, reason = stack_sentinel.pool_start_blocked_by_status(None)
        self.assertTrue(blocked)
        self.assertIn("status unavailable", reason)

        blocked, reason = stack_sentinel.pool_start_blocked_by_status({"fresh": False})
        self.assertTrue(blocked)
        self.assertIn("status is stale", reason)

    def test_sentinel_leaves_pool_stopped_during_catchup_pause(self) -> None:
        state: dict[str, object] = {}
        starts: list[str] = []
        incidents: list[tuple[str, str, str, str]] = []
        inspected = {
            stack_sentinel.POOL_DB_CONTAINER: {"running": True, "status": "running"},
            "node-a": {"running": True, "status": "running"},
            stack_sentinel.POOL_CONTAINER: {"running": False, "status": "exited"},
        }
        status = {
            "fresh": True,
            "catchup_policy": {"active": True},
            "sync_health": {"catchup_pause_active": True},
        }

        def fake_incident(event_type: str, severity: str, component: str, message: str, details=None) -> None:
            incidents.append((event_type, severity, component, message))

        def fake_start(service: str, reason: str, state_obj: dict[str, object], now: int) -> bool:
            starts.append(service)
            return True

        with unittest.mock.patch.object(stack_sentinel, "NODES", ["node-a"]), unittest.mock.patch.object(
            stack_sentinel, "docker_inspect", return_value=inspected
        ), unittest.mock.patch.object(
            stack_sentinel, "start_container", side_effect=fake_start
        ), unittest.mock.patch.object(
            stack_sentinel, "append_incident", fake_incident
        ), unittest.mock.patch.object(
            stack_sentinel, "notify_user", lambda _title, _body: None
        ), unittest.mock.patch.object(
            stack_sentinel, "log", lambda _message: None
        ):
            stack_sentinel.inspect_and_repair_containers(status, state, 100)

        self.assertEqual([], starts)
        self.assertIn("chain catch-up pause is active", state["pool_start_blocked_reason"])
        self.assertNotIn(stack_sentinel.POOL_CONTAINER, state["actionable_stopped_containers"])
        self.assertTrue(any(item[0] == "sentinel_pool_start_blocked" for item in incidents))

    def test_sentinel_leaves_pool_stopped_during_public_chain_divergence(self) -> None:
        state: dict[str, object] = {}
        starts: list[str] = []
        inspected = {
            stack_sentinel.POOL_DB_CONTAINER: {"running": True, "status": "running"},
            "node-a": {"running": True, "status": "running"},
            stack_sentinel.POOL_CONTAINER: {"running": False, "status": "exited"},
        }
        status = {
            "fresh": True,
            "sync_health": {"public_chain_divergence": True},
        }

        with unittest.mock.patch.object(stack_sentinel, "NODES", ["node-a"]), unittest.mock.patch.object(
            stack_sentinel, "docker_inspect", return_value=inspected
        ), unittest.mock.patch.object(
            stack_sentinel, "start_container", side_effect=lambda service, *_args: starts.append(service) or True
        ), unittest.mock.patch.object(
            stack_sentinel, "append_incident", lambda *_args, **_kwargs: None
        ), unittest.mock.patch.object(
            stack_sentinel, "notify_user", lambda _title, _body: None
        ), unittest.mock.patch.object(
            stack_sentinel, "log", lambda _message: None
        ):
            stack_sentinel.inspect_and_repair_containers(status, state, 100)

        self.assertEqual([], starts)
        self.assertIn("public-chain divergence containment is active", state["pool_start_blocked_reason"])
        self.assertNotIn(stack_sentinel.POOL_CONTAINER, state["actionable_stopped_containers"])

    def test_sentinel_suppresses_systemd_start_when_control_missing(self) -> None:
        incidents: list[tuple[str, str, str, str]] = []

        def fake_incident(event_type: str, severity: str, component: str, message: str, details=None) -> None:
            incidents.append((event_type, severity, component, message))

        with self.patch_default_control_paths(), unittest.mock.patch.object(
            stack_sentinel, "log", lambda _message: None
        ), unittest.mock.patch.object(
            guard_core, "append_incident", fake_incident
        ), unittest.mock.patch.object(
            guard_core, "unit_active", return_value=False
        ), unittest.mock.patch.object(
            guard_core, "systemctl_user", side_effect=AssertionError("systemctl start should not run")
        ):
            stack_sentinel.start_unit(
                "bdag-watchdog.service",
                {},
                200,
                log=stack_sentinel.log,
                incident_source="stack-sentinel",
            )

        self.assertEqual(1, len(self.event_lines()))
        self.assertEqual("automation_control_suppressed", incidents[0][0])

    def test_sentinel_log_scan_timeout_records_warning_without_crashing(self) -> None:
        incidents: list[tuple[str, str, str, str, dict[str, object] | None]] = []
        messages: list[str] = []

        def fake_incident(event_type: str, severity: str, component: str, message: str, details=None) -> None:
            incidents.append((event_type, severity, component, message, details))

        timeout = subprocess.TimeoutExpired(
            ["docker", "logs"],
            12,
            output=b"partial stdout",
            stderr=b"partial stderr",
        )

        with unittest.mock.patch.object(stack_sentinel, "NODES", ["node"]), unittest.mock.patch.object(
            stack_sentinel.subprocess, "run", side_effect=timeout
        ), unittest.mock.patch.object(
            stack_sentinel, "append_incident", fake_incident
        ), unittest.mock.patch.object(
            stack_sentinel, "log", lambda message: messages.append(message)
        ):
            state: dict[str, object] = {}
            stack_sentinel.check_node_log_red_flags(state, 100)

        self.assertEqual(1, len(incidents))
        self.assertEqual("node_log_scan_timeout", incidents[0][0])
        self.assertEqual("warning", incidents[0][1])
        self.assertEqual("stack-sentinel", incidents[0][2])
        self.assertEqual(1, len(messages))
        self.assertEqual(14, incidents[0][4]["stdout_bytes"])
        self.assertEqual(14, incidents[0][4]["stderr_bytes"])


if __name__ == "__main__":
    unittest.main()

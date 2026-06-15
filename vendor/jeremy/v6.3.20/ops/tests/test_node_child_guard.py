#!/usr/bin/env python3

import pathlib
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace


OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import node_child_guard  # noqa: E402


class NodeChildGuardTests(unittest.TestCase):
    def test_child_detection_accepts_packaged_blockdag_node_name(self) -> None:
        top = """PID COMMAND         COMMAND
1   runuser         runuser -u bdagStack -g bdagStack -- /usr/local/bin/nodeworker
66  blockdag-node   /usr/local/bin/blockdag-node --configfile /etc/bdagStack/node.conf
"""

        self.assertTrue(node_child_guard.bdag_child_running_from_top(top))

    def test_child_detection_keeps_legacy_bdag_name(self) -> None:
        top = """PID COMMAND COMMAND
66 bdag    /usr/local/bin/bdag --configfile /etc/bdagStack/node.conf
"""

        self.assertTrue(node_child_guard.bdag_child_running_from_top(top))

    def test_child_detection_does_not_count_nodeworker_wrapper_only(self) -> None:
        top = """PID COMMAND    COMMAND
1   nodeworker /usr/local/bin/nodeworker --node-binary=/usr/local/bin/blockdag-node
"""

        self.assertFalse(node_child_guard.bdag_child_running_from_top(top))

    def test_compose_command_omits_missing_env_file(self) -> None:
        original_root = node_child_guard.PROJECT_ROOT
        original_env = node_child_guard.POOL_ENV_FILE
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                node_child_guard.PROJECT_ROOT = pathlib.Path(tmpdir)
                node_child_guard.POOL_ENV_FILE = pathlib.Path(tmpdir) / ".env"
                command = node_child_guard.compose_command("ps")
            finally:
                node_child_guard.PROJECT_ROOT = original_root
                node_child_guard.POOL_ENV_FILE = original_env

        self.assertNotIn("--env-file", command)
        self.assertEqual(command[-3:], ["-f", f"{tmpdir}/docker-compose.yml", "ps"])

    def test_compose_command_uses_existing_env_file(self) -> None:
        original_root = node_child_guard.PROJECT_ROOT
        original_env = node_child_guard.POOL_ENV_FILE
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = pathlib.Path(tmpdir) / ".env"
            env_path.write_text("POSTGRES_PASSWORD=test\n", encoding="utf-8")
            try:
                node_child_guard.PROJECT_ROOT = pathlib.Path(tmpdir)
                node_child_guard.POOL_ENV_FILE = env_path
                command = node_child_guard.compose_command("ps")
            finally:
                node_child_guard.PROJECT_ROOT = original_root
                node_child_guard.POOL_ENV_FILE = original_env

        self.assertIn("--env-file", command)
        self.assertEqual(command[command.index("--env-file") + 1], str(env_path))

    def test_restart_uses_compose_service_label(self) -> None:
        calls: list[list[str]] = []
        original_run = node_child_guard.run

        def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
            calls.append(command)
            if command[:2] == ["docker", "inspect"]:
                return SimpleNamespace(returncode=0, stdout="node\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        try:
            node_child_guard.run = fake_run
            with mock.patch.object(node_child_guard, "node_mutation_allowed", return_value=True):
                ok = node_child_guard.restart_node("node", "child missing", {}, 1000)
        finally:
            node_child_guard.run = original_run

        self.assertTrue(ok)
        self.assertIn("restart", calls[1])
        self.assertEqual(calls[1][-2:], ["restart", "node"])

    def test_restart_falls_back_to_direct_docker_restart(self) -> None:
        calls: list[list[str]] = []
        original_run = node_child_guard.run

        def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
            calls.append(command)
            if command[:2] == ["docker", "inspect"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="not found")
            if command[:3] == ["docker", "compose", "-p"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="compose failed")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        try:
            node_child_guard.run = fake_run
            with mock.patch.object(node_child_guard, "node_mutation_allowed", return_value=True):
                ok = node_child_guard.restart_node("node", "child missing", {}, 1000)
        finally:
            node_child_guard.run = original_run

        self.assertTrue(ok)
        self.assertIn(["docker", "restart", "node"], calls)

    def test_restart_is_suppressed_when_automation_control_denies(self) -> None:
        calls: list[list[str]] = []
        decision = SimpleNamespace(
            allowed=False,
            reason="transition_hold does not allow this mutation",
            control_state="transition_hold",
            control_status="ok",
        )

        with mock.patch.object(
            node_child_guard.automation_control,
            "check_mutation_allowed",
            return_value=decision,
        ), mock.patch.object(
            node_child_guard,
            "run",
            side_effect=lambda command, **_kwargs: calls.append(command) or SimpleNamespace(returncode=0, stdout="", stderr=""),
        ), mock.patch.object(
            node_child_guard,
            "log",
            lambda _message: None,
        ):
            state: dict[str, object] = {}
            ok = node_child_guard.restart_node("node", "child missing", state, 1000)

        self.assertFalse(ok)
        self.assertEqual([], calls)
        self.assertEqual("transition_hold", state["automation_suppressed_by_node"]["node"]["control_state"])

    def test_start_is_suppressed_when_automation_control_denies(self) -> None:
        calls: list[list[str]] = []
        decision = SimpleNamespace(
            allowed=False,
            reason="controlled_stop denies high-risk mutation",
            control_state="controlled_stop",
            control_status="ok",
        )

        with mock.patch.object(
            node_child_guard.automation_control,
            "check_mutation_allowed",
            return_value=decision,
        ), mock.patch.object(
            node_child_guard,
            "run",
            side_effect=lambda command, **_kwargs: calls.append(command) or SimpleNamespace(returncode=0, stdout="", stderr=""),
        ), mock.patch.object(
            node_child_guard,
            "log",
            lambda _message: None,
        ):
            state: dict[str, object] = {}
            ok = node_child_guard.start_node("node", "container stopped", state, 1000)

        self.assertFalse(ok)
        self.assertEqual([], calls)
        self.assertEqual("controlled_stop", state["automation_suppressed_by_node"]["node"]["control_state"])

    def test_default_guard_node_covers_current_service_name(self) -> None:
        self.assertEqual(node_child_guard.DEFAULT_NODE_CHILD_GUARD_NODE, "node")


if __name__ == "__main__":
    unittest.main()

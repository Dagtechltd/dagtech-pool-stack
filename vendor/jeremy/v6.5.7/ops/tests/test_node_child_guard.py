#!/usr/bin/env python3

import pathlib
import sys
import tempfile
import http.server
import threading
import unittest
from types import SimpleNamespace


OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import node_child_guard  # noqa: E402


class ProbeHandler(http.server.BaseHTTPRequestHandler):
    status = 200
    payload = b'{"jsonrpc":"2.0","id":"node-child-guard","result":123}'

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.send_response(type(self).status)
        self.send_header("Content-Length", str(len(type(self).payload)))
        self.end_headers()
        self.wfile.write(type(self).payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def run_probe_server(status: int, payload: bytes) -> http.server.HTTPServer:
    class Handler(ProbeHandler):
        pass

    Handler.status = status
    Handler.payload = payload
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


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
            ok = node_child_guard.restart_node("node", "child missing", {}, 1000)
        finally:
            node_child_guard.run = original_run

        self.assertTrue(ok)
        self.assertIn(["docker", "restart", "node"], calls)

    def test_default_guard_nodes_cover_current_service_name(self) -> None:
        self.assertEqual(node_child_guard.DEFAULT_NODE_CHILD_GUARD_NODES, "node")

    def test_json_rpc_probe_accepts_valid_result(self) -> None:
        server = run_probe_server(
            200,
            b'{"jsonrpc":"2.0","id":"node-child-guard","result":123}',
        )
        try:
            ok, reason = node_child_guard.json_rpc_probe("127.0.0.1", server.server_port)
        finally:
            server.shutdown()
            server.server_close()

        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_json_rpc_probe_detects_busy_rpc(self) -> None:
        server = run_probe_server(503, b"503 Too busy. Try again later.")
        try:
            ok, reason = node_child_guard.json_rpc_probe("127.0.0.1", server.server_port)
        finally:
            server.shutdown()
            server.server_close()

        self.assertFalse(ok)
        self.assertEqual(reason, "too_busy")

    def test_main_restarts_tcp_open_json_rpc_wedged_node_after_grace(self) -> None:
        original_paths = (
            node_child_guard.STATE_FILE,
            node_child_guard.LOCK_FILE,
            node_child_guard.LOG_DIR,
            node_child_guard.LOG_FILE,
        )
        original_nodes = node_child_guard.NODES
        original_time = node_child_guard.time.time
        original_inspect = node_child_guard.inspect_container
        original_child = node_child_guard.bdag_child_running
        original_tcp = node_child_guard.tcp_open
        original_probe = node_child_guard.json_rpc_probe
        original_restart = node_child_guard.restart_node
        restarts: list[tuple[str, str]] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = pathlib.Path(tmpdir)
            try:
                node_child_guard.STATE_FILE = runtime / "state.json"
                node_child_guard.LOCK_FILE = runtime / "guard.lock"
                node_child_guard.LOG_DIR = runtime / "logs"
                node_child_guard.LOG_FILE = runtime / "logs" / "guard.log"
                node_child_guard.NODES = ["node"]
                node_child_guard.STATE_FILE.write_text(
                    '{"rpc_wedged_since_by_node":{"node":800}}',
                    encoding="utf-8",
                )
                node_child_guard.time.time = lambda: 1000
                node_child_guard.inspect_container = lambda _node: {
                    "exists": True,
                    "running": True,
                    "ip": "172.18.0.5",
                }
                node_child_guard.bdag_child_running = lambda _node: True
                node_child_guard.tcp_open = lambda _host, port, timeout=1.5: port == 38131
                node_child_guard.json_rpc_probe = lambda _host: (False, "too_busy")

                def fake_restart(node: str, reason: str, _state: dict, _now: int) -> bool:
                    restarts.append((node, reason))
                    return True

                node_child_guard.restart_node = fake_restart

                self.assertEqual(node_child_guard.main(), 0)
            finally:
                (
                    node_child_guard.STATE_FILE,
                    node_child_guard.LOCK_FILE,
                    node_child_guard.LOG_DIR,
                    node_child_guard.LOG_FILE,
                ) = original_paths
                node_child_guard.NODES = original_nodes
                node_child_guard.time.time = original_time
                node_child_guard.inspect_container = original_inspect
                node_child_guard.bdag_child_running = original_child
                node_child_guard.tcp_open = original_tcp
                node_child_guard.json_rpc_probe = original_probe
                node_child_guard.restart_node = original_restart

        self.assertEqual(len(restarts), 1)
        self.assertEqual(restarts[0][0], "node")
        self.assertIn("JSON-RPC unhealthy", restarts[0][1])
        self.assertIn("too_busy", restarts[0][1])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3

from __future__ import annotations

import json
import pathlib
import sys
import threading
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import mining_readiness_gate as gate  # noqa: E402


RpcHandler = Callable[[str, list[Any] | dict[str, Any], dict[str, int]], Any]


class FakeJsonRpcServer:
    def __init__(self, handler: RpcHandler, required_auth: str = "") -> None:
        self.handler = handler
        self.required_auth = required_auth
        self.calls: dict[str, int] = {}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self.httpd.owner = self  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> "FakeJsonRpcServer":
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=2)
        self.httpd.server_close()

    def _handle(self, method: str, params: list[Any] | dict[str, Any]) -> Any:
        self.calls[method] = self.calls.get(method, 0) + 1
        return self.handler(method, params, self.calls)

    @staticmethod
    def _handler_class() -> type[BaseHTTPRequestHandler]:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                owner = self.server.owner  # type: ignore[attr-defined]
                if owner.required_auth and self.headers.get("Authorization") != owner.required_auth:
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                method = payload["method"]
                params = payload.get("params") or []
                response = owner._handle(method, params)
                body = json.dumps(response).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        return Handler


def rpc_result(value: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "result": value}


def rpc_error(code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}}


def healthy_health(main_order: int) -> dict[str, Any]:
    return {
        "main_order": main_order,
        "is_current": True,
        "mineable_now": True,
        "submit_ready": True,
        "get_block_template_ready": True,
        "template_usable": True,
        "sync_allowed": True,
        "p2p_mining_fresh": True,
        "p2p_best_peer_lead_blocks": 0,
        "submit_no_synced": False,
        "last_template_build_error_code": "",
        "reason_code": "",
        "template_parent": "0xparent",
    }


class MiningReadinessGateTests(unittest.TestCase):
    def test_node_connection_refused_fails_closed(self) -> None:
        result = gate.evaluate_gate(
            [gate.Backend("node", "http://127.0.0.1:1")],
            timeout=0.05,
            sample_count=1,
            sample_interval_seconds=0,
            after_chain_incident=True,
        )

        self.assertFalse(result["ok"])
        self.assertIn("no_ready_backend", result["failures"])
        failures = result["backends"]["node"]["samples"][0]["failures"]
        self.assertIn("getBlockCount failed: connection_refused", failures)

    def test_template_parent_stale_is_rejected(self) -> None:
        def handler(method: str, _params: list[Any] | dict[str, Any], _calls: dict[str, int]) -> Any:
            if method == "getBlockCount":
                return rpc_result(1000)
            if method == "getTemplateHealth":
                health = healthy_health(2000)
                health["reason_code"] = "template_parent_stale"
                return rpc_result(health)
            if method == "getBlockTemplate":
                return rpc_result({"main_order": 2000, "parent": "0xparent"})
            return rpc_error(-32601, "method not found")

        with FakeJsonRpcServer(handler) as server:
            result = gate.evaluate_gate(
                [gate.Backend("node", server.url)],
                timeout=0.5,
                sample_count=1,
                sample_interval_seconds=0,
                after_chain_incident=True,
            )

        self.assertFalse(result["ok"])
        failures = result["backends"]["node"]["samples"][0]["failures"]
        self.assertIn("blocking_template_error:template_parent_stale", failures)

    def test_empty_block_template_is_rejected(self) -> None:
        def handler(method: str, _params: list[Any] | dict[str, Any], _calls: dict[str, int]) -> Any:
            if method == "getBlockCount":
                return rpc_result(1000)
            if method == "getTemplateHealth":
                return rpc_result(healthy_health(2000))
            if method == "getBlockTemplate":
                return rpc_result({})
            return rpc_error(-32601, "method not found")

        with FakeJsonRpcServer(handler) as server:
            result = gate.evaluate_gate(
                [gate.Backend("node", server.url)],
                timeout=0.5,
                sample_count=1,
                sample_interval_seconds=0,
                after_chain_incident=True,
            )

        self.assertFalse(result["ok"])
        failures = result["backends"]["node"]["samples"][0]["failures"]
        self.assertIn("getBlockTemplate_empty", failures)

    def test_block_template_probe_uses_current_node_rpc_signature(self) -> None:
        mining_address = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"

        def handler(method: str, params: list[Any] | dict[str, Any], _calls: dict[str, int]) -> Any:
            if method == "getBlockCount":
                return rpc_result(1000)
            if method == "getTemplateHealth":
                health = healthy_health(2000)
                health.pop("is_current")
                health["chain_current"] = True
                return rpc_result(health)
            if method == "getBlockTemplate":
                if params != [[], 10, mining_address]:
                    return rpc_error(-32602, f"unexpected params: {params!r}")
                return rpc_result({"main_order": 2000, "parent": "0xparent"})
            return rpc_error(-32601, "method not found")

        with FakeJsonRpcServer(handler) as server:
            result = gate.evaluate_gate(
                [gate.Backend("node", server.url)],
                timeout=0.5,
                sample_count=1,
                sample_interval_seconds=0,
                after_chain_incident=True,
                mining_address=mining_address,
            )

        self.assertTrue(result["ok"], result["failures"])
        self.assertEqual(["node"], result["eligible_backends"])

    def test_old_node_without_template_health_fails_closed_after_incident(self) -> None:
        def handler(method: str, _params: list[Any] | dict[str, Any], _calls: dict[str, int]) -> Any:
            if method == "getBlockCount":
                return rpc_result(1000)
            if method == "getTemplateHealth":
                return rpc_error(-32601, "method not found")
            if method == "getBlockTemplate":
                return rpc_result({"main_order": 2000, "parent": "0xparent"})
            return rpc_error(-32601, "method not found")

        with FakeJsonRpcServer(handler) as server:
            result = gate.evaluate_gate(
                [gate.Backend("node", server.url)],
                timeout=0.5,
                sample_count=1,
                sample_interval_seconds=0,
                after_chain_incident=True,
            )

        self.assertFalse(result["ok"])
        failures = result["backends"]["node"]["samples"][0]["failures"]
        self.assertIn("template_health_missing_after_chain_incident", failures)

    def test_healthy_backend_passes_after_three_non_regressing_samples(self) -> None:
        def handler(method: str, _params: list[Any] | dict[str, Any], calls: dict[str, int]) -> Any:
            height = 1000 + max(0, calls.get("getBlockCount", 1) - 1)
            main_order = 2000 + max(0, calls.get("getTemplateHealth", 1) - 1)
            if method == "getBlockCount":
                return rpc_result(height)
            if method == "getTemplateHealth":
                return rpc_result(healthy_health(main_order))
            if method == "getBlockTemplate":
                return rpc_result({"main_order": main_order, "parent": "0xparent"})
            return rpc_error(-32601, "method not found")

        with FakeJsonRpcServer(handler) as server:
            result = gate.evaluate_gate(
                [gate.Backend("node", server.url)],
                timeout=0.5,
                sample_count=3,
                sample_interval_seconds=0,
                after_chain_incident=True,
            )

        self.assertTrue(result["ok"], result["failures"])
        self.assertEqual(["node"], result["eligible_backends"])
        backend = result["backends"]["node"]
        self.assertTrue(backend["ready"], backend["failures"])
        self.assertEqual(3, len(backend["samples"]))
        self.assertEqual(1002, backend["height"])
        self.assertEqual(2002, backend["main_order"])

    def test_authenticated_backend_uses_env_basic_auth_and_redacts_url(self) -> None:
        required = "Basic dXNlcjpwYXNz"

        def handler(method: str, _params: list[Any] | dict[str, Any], _calls: dict[str, int]) -> Any:
            if method == "getBlockCount":
                return rpc_result(1000)
            if method == "getTemplateHealth":
                return rpc_result(healthy_health(2000))
            if method == "getBlockTemplate":
                return rpc_result({"main_order": 2000, "parent": "0xparent"})
            return rpc_error(-32601, "method not found")

        with FakeJsonRpcServer(handler, required_auth=required) as server, unittest.mock.patch.dict(
            gate.os.environ,
            {"NODE_RPC_USER": "user", "NODE_RPC_PASS": "pass"},
            clear=False,
        ):
            result = gate.evaluate_gate(
                [gate.Backend("node", server.url)],
                timeout=0.5,
                sample_count=1,
                sample_interval_seconds=0,
                after_chain_incident=True,
            )

        self.assertTrue(result["ok"], result["failures"])
        self.assertEqual(server.url, result["backends"]["node"]["url"])
        self.assertNotIn("user:pass", json.dumps(result))

    def test_reference_lag_rejects_backend(self) -> None:
        def backend_handler(method: str, _params: list[Any] | dict[str, Any], _calls: dict[str, int]) -> Any:
            if method == "getBlockCount":
                return rpc_result(1000)
            if method == "getTemplateHealth":
                return rpc_result(healthy_health(2000))
            if method == "getBlockTemplate":
                return rpc_result({"main_order": 2000, "parent": "0xparent"})
            return rpc_error(-32601, "method not found")

        def reference_handler(method: str, _params: list[Any] | dict[str, Any], _calls: dict[str, int]) -> Any:
            if method == "getBlockCount":
                return rpc_result(1121)
            if method == "getTemplateHealth":
                return rpc_result({"main_order": 2121})
            return rpc_error(-32601, "method not found")

        with FakeJsonRpcServer(backend_handler) as backend, FakeJsonRpcServer(reference_handler) as reference:
            result = gate.evaluate_gate(
                [gate.Backend("node", backend.url)],
                reference_rpc_url=reference.url,
                timeout=0.5,
                sample_count=1,
                sample_interval_seconds=0,
                after_chain_incident=True,
                max_reference_lag=120,
            )

        self.assertFalse(result["ok"])
        failures = result["backends"]["node"]["samples"][0]["failures"]
        self.assertIn("reference_height_lag_121_gt_120", failures)
        self.assertIn("reference_main_order_lag_121_gt_120", failures)

    def test_active_node_topology_accepts_direct_backend(self) -> None:
        topology = gate.validate_topology(
            node_services=["node"],
            pool_rpc_backends=["node"],
            running_containers=["node"],
            eligible_backends=["node"],
        )

        self.assertTrue(topology["ok"])

    def test_active_node_topology_rejects_extra_runtime_backend(self) -> None:
        extra_service = "unexpected-chain-service"
        extra_alias = "unexpected-backend"
        topology = gate.validate_topology(
            node_services=["node", extra_service],
            pool_rpc_backends=["node", extra_alias],
            running_containers=["node", extra_service],
            eligible_backends=["node", extra_alias],
        )

        self.assertFalse(topology["ok"])
        self.assertIn(f"unexpected_extra_service:{extra_service}", topology["failures"])
        self.assertIn(f"unexpected_extra_pool_backend:{extra_alias}", topology["failures"])
        self.assertIn(f"unexpected_extra_running_container:{extra_service}", topology["failures"])
        self.assertIn(f"unexpected_extra_eligible_backend:{extra_alias}", topology["failures"])


if __name__ == "__main__":
    unittest.main()

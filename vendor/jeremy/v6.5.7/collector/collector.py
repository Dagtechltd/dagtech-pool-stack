#!/usr/bin/env python3
"""Read-only BlockDAG collector API."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from incident_journal import read_recent_incidents
from pool_ops import (
    RUNTIME_DIR,
    collect_earnings,
    collect_global_blockchain,
    collect_global_pool_earnings_window,
    collect_status_cached,
    ensure_runtime,
    now_iso,
)


HOST = os.environ.get("BDAG_COLLECTOR_BIND", "127.0.0.1")
PORT = int(os.environ.get("BDAG_COLLECTOR_PORT", "9280"))
STATUS_CACHE_SECONDS = float(
    os.environ.get("BDAG_COLLECTOR_STATUS_CACHE_SECONDS", "10")
)
EARNINGS_CACHE_SECONDS = float(
    os.environ.get("BDAG_COLLECTOR_EARNINGS_CACHE_SECONDS", "30")
)
GLOBAL_CACHE_SECONDS = float(
    os.environ.get("BDAG_COLLECTOR_GLOBAL_CACHE_SECONDS", "60")
)
SAMPLER_CACHE_SECONDS = float(
    os.environ.get("BDAG_COLLECTOR_SAMPLER_CACHE_SECONDS", "10")
)
P2P_GUARD_STATE = RUNTIME_DIR / "p2p-health-state.json"
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

API_CACHE: dict[str, tuple[float, object]] = {}
API_CACHE_LOCK = threading.Lock()


def cached_payload(key: str, ttl: float, factory):
    now = time.time()
    with API_CACHE_LOCK:
        cached = API_CACHE.get(key)
        if cached and now - cached[0] < ttl:
            return cached[1]
    payload = factory()
    with API_CACHE_LOCK:
        API_CACHE[key] = (now, payload)
    return payload


def bounded_tail(raw_tail: str | None) -> int:
    try:
        tail = int(raw_tail or "240")
    except ValueError:
        tail = 240
    return max(1, min(tail, 1000))


def log_container(service: str) -> str:
    if service == "node":
        return os.environ.get("BDAG_COLLECTOR_NODE_LOG_CONTAINER") or os.environ.get("BDAG_NODE_LOG_CONTAINER", "stack-node-1")
    if service == "pool":
        return os.environ.get("BDAG_COLLECTOR_POOL_LOG_CONTAINER") or os.environ.get("BDAG_POOL_LOG_CONTAINER", "stack-pool-1")
    raise ValueError(f"unsupported log service {service!r}")


def docker_logs(service: str, tail: int) -> dict[str, object]:
    container = log_container(service)
    started = time.time()
    try:
        completed = subprocess.run(
            ["docker", "logs", "--tail", str(tail), container],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return {
            "generated_at": now_iso(),
            "service": service,
            "container": container,
            "tail": tail,
            "content": f"Timed out reading docker logs for {container}",
            "duration_seconds": round(time.time() - started, 3),
        }

    content = completed.stdout + completed.stderr
    content = ANSI_ESCAPE_RE.sub("", content).rstrip("\n")
    if completed.returncode != 0 and not content:
        content = f"docker logs exited with status {completed.returncode}"
    return {
        "generated_at": now_iso(),
        "service": service,
        "container": container,
        "tail": tail,
        "content": content,
        "exit_code": completed.returncode,
        "duration_seconds": round(time.time() - started, 3),
    }


def sampler_status() -> dict[str, object]:
    return {
        "generated_at": now_iso(),
        "status_cache_seconds": STATUS_CACHE_SECONDS,
        "earnings_cache_seconds": EARNINGS_CACHE_SECONDS,
        "global_cache_seconds": GLOBAL_CACHE_SECONDS,
        "cache_keys": sorted(API_CACHE.keys()),
    }


class CollectorHandler(BaseHTTPRequestHandler):
    server_version = "bdag-collector/1"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{now_iso()}] {self.client_address[0]} {fmt % args}")

    def send_json(self, payload: object, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib method name.
        self.send_json({"generated_at": now_iso(), "error": "collector API is read-only"}, HTTPStatus.METHOD_NOT_ALLOWED)

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name.
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path == "/healthz":
            self.send_json({"generated_at": now_iso(), "status": "ok"})
            return
        if path == "/api/status":
            self.send_json(cached_payload("status", STATUS_CACHE_SECONDS, lambda: collect_status_cached(include_logs=True)))
            return
        if path == "/api/earnings":
            self.send_json(cached_payload("earnings", EARNINGS_CACHE_SECONDS, lambda: collect_earnings(include_history=True)))
            return
        if path == "/api/global":
            self.send_json(cached_payload("global", GLOBAL_CACHE_SECONDS, collect_global_blockchain))
            return
        if path == "/api/global/pool-earnings":
            self.send_json(
                cached_payload(
                    "global-pool-earnings-600",
                    GLOBAL_CACHE_SECONDS,
                    lambda: collect_global_pool_earnings_window(600),
                )
            )
            return
        if path == "/api/sampler":
            self.send_json(cached_payload("sampler", SAMPLER_CACHE_SECONDS, sampler_status))
            return
        if path == "/api/incidents":
            self.send_json({"generated_at": now_iso(), "incidents": read_recent_incidents(100)})
            return
        if path == "/api/p2p":
            if P2P_GUARD_STATE.exists():
                try:
                    self.send_json(json.loads(P2P_GUARD_STATE.read_text(encoding="utf-8")))
                except json.JSONDecodeError as exc:
                    self.send_json({"generated_at": now_iso(), "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            else:
                self.send_json({"generated_at": now_iso(), "error": "p2p guard state not available"}, HTTPStatus.NOT_FOUND)
            return
        if path == "/api/logs/node":
            self.send_json(docker_logs("node", bounded_tail((query.get("tail") or [None])[0])))
            return
        if path == "/api/logs/pool":
            self.send_json(docker_logs("pool", bounded_tail((query.get("tail") or [None])[0])))
            return

        self.send_json({"generated_at": now_iso(), "error": "not found"}, HTTPStatus.NOT_FOUND)


def main() -> None:
    ensure_runtime()
    Path(RUNTIME_DIR).mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), CollectorHandler)
    print(f"[{now_iso()}] collector listening on http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()

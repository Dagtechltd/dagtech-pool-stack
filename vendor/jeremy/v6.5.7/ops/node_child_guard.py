#!/usr/bin/env python3
"""Restart a node container if nodeworker is up but the bdag child is gone."""

from __future__ import annotations

import json
import os
import fcntl
import base64
import http.client
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(os.environ.get("BDAG_PROJECT_ROOT", Path(__file__).resolve().parents[1]))
RUNTIME_DIR = Path(os.environ.get("BDAG_RUNTIME_DIR", PROJECT_ROOT / "ops" / "runtime"))
LOG_DIR = RUNTIME_DIR / "logs"
STATE_FILE = RUNTIME_DIR / "node-child-guard-state.json"
LOCK_FILE = RUNTIME_DIR / "node-child-guard.lock"
LOG_FILE = LOG_DIR / "node-child-guard.log"
DEFAULT_NODE_CHILD_GUARD_NODES = "node"


def default_pool_env_file() -> Path:
    for candidate in (PROJECT_ROOT / ".env", PROJECT_ROOT / "asic-pool" / ".env"):
        if candidate.exists():
            return candidate
    return PROJECT_ROOT / ".env"


POOL_ENV_FILE = Path(os.environ.get("BDAG_POOL_ENV_FILE", default_pool_env_file()))
NODES = [
    item.strip()
    for item in os.environ.get(
        "BDAG_NODE_CHILD_GUARD_NODES",
        os.environ.get("BDAG_NODE_SERVICES", DEFAULT_NODE_CHILD_GUARD_NODES),
    ).split(",")
    if item.strip()
]
COOLDOWN_SECONDS = int(os.environ.get("BDAG_NODE_CHILD_GUARD_RESTART_COOLDOWN_SECONDS", "180"))
RPC_REFUSED_SECONDS = int(os.environ.get("BDAG_NODE_CHILD_GUARD_RPC_REFUSED_SECONDS", "300"))
RPC_WEDGED_SECONDS = int(os.environ.get("BDAG_NODE_CHILD_GUARD_RPC_WEDGED_SECONDS", "180"))
RPC_PROBE_TIMEOUT_SECONDS = float(os.environ.get("BDAG_NODE_CHILD_GUARD_RPC_PROBE_TIMEOUT_SECONDS", "2.5"))


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def ensure_runtime() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def log(message: str) -> None:
    ensure_runtime()
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now_iso()}] {message}\n")


def run(command: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def read_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(state: dict[str, Any]) -> None:
    ensure_runtime()
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def inspect_container(name: str) -> dict[str, Any]:
    result = run(["docker", "inspect", name], timeout=10)
    if result.returncode != 0:
        return {"exists": False, "running": False}
    try:
        payload = json.loads(result.stdout)[0]
    except (IndexError, json.JSONDecodeError):
        return {"exists": False, "running": False}
    state = payload.get("State") or {}
    networks = ((payload.get("NetworkSettings") or {}).get("Networks") or {}).values()
    ips = [str(row.get("IPAddress") or "") for row in networks if row.get("IPAddress")]
    return {
        "exists": True,
        "running": bool(state.get("Running")),
        "status": state.get("Status"),
        "ip": ips[0] if ips else "",
    }


def bdag_child_running(name: str) -> bool:
    result = run(["docker", "top", name, "-eo", "pid,comm,args"], timeout=8)
    if result.returncode != 0:
        log(f"docker top failed node={name} stderr={result.stderr.strip()}")
        return False
    return bdag_child_running_from_top(result.stdout)


def bdag_child_running_from_top(top: str) -> bool:
    for line in top.splitlines()[1:]:
        columns = line.split(None, 2)
        command = columns[1] if len(columns) > 1 else ""
        args = columns[2] if len(columns) > 2 else ""
        first_arg = args.split(None, 1)[0] if args else ""
        executable_names = {Path(command).name, Path(first_arg).name}
        if executable_names & {"bdag", "blockdag-node"}:
            return True
    return False


def read_env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def node_rpc_credentials() -> tuple[str, str]:
    env_values = read_env_values(POOL_ENV_FILE)
    user = os.environ.get("NODE_RPC_USER") or env_values.get("NODE_RPC_USER") or "test"
    password = os.environ.get("NODE_RPC_PASS") or env_values.get("NODE_RPC_PASS") or "test"
    return user, password


def json_rpc_probe(host: str, port: int = 38131, timeout: float = RPC_PROBE_TIMEOUT_SECONDS) -> tuple[bool, str]:
    if not host:
        return False, "missing_host"
    user, password = node_rpc_credentials()
    auth = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    body = json.dumps({"jsonrpc": "1.0", "id": "node-child-guard", "method": "getBlockCount", "params": []})
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request(
            "POST",
            "/",
            body=body,
            headers={
                "Authorization": f"Basic {auth}",
                "Connection": "close",
                "Content-Type": "application/json",
            },
        )
        response = conn.getresponse()
        payload = response.read(4096)
    except (OSError, http.client.HTTPException, TimeoutError) as exc:
        return False, f"transport:{exc}"
    finally:
        conn.close()
    if response.status == http.client.SERVICE_UNAVAILABLE:
        return False, "too_busy"
    if response.status >= 400:
        return False, f"http_{response.status}"
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, f"invalid_json:{exc}"
    if isinstance(decoded, dict) and decoded.get("error"):
        return False, f"jsonrpc_error:{decoded.get('error')}"
    if not isinstance(decoded, dict) or "result" not in decoded:
        return False, "invalid_jsonrpc_response"
    return True, "ok"


def tcp_open(host: str, port: int, timeout: float = 1.5) -> bool:
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def docker_compose_project_name() -> str:
    configured = os.environ.get("BDAG_COMPOSE_PROJECT_NAME") or os.environ.get("COMPOSE_PROJECT_NAME")
    if configured:
        return configured
    return PROJECT_ROOT.name


def compose_command(*args: str) -> list[str]:
    command = [
        "docker",
        "compose",
        "-p",
        docker_compose_project_name(),
    ]
    if POOL_ENV_FILE.exists():
        command.extend(
            [
                "--env-file",
                str(POOL_ENV_FILE),
            ]
        )
    command.extend([
        "-f",
        str(PROJECT_ROOT / "docker-compose.yml"),
        *args,
    ])
    return command


def compose_service_name(name: str) -> str:
    result = run(["docker", "inspect", "-f", '{{ index .Config.Labels "com.docker.compose.service" }}', name], timeout=8)
    service = result.stdout.strip() if result.returncode == 0 else ""
    if service and service != "<no value>":
        return service
    return name


def restart_node(node: str, reason: str, state: dict[str, Any], now: int) -> bool:
    last = int((state.get("last_restart_at_by_node") or {}).get(node) or 0)
    if now - last < COOLDOWN_SECONDS:
        log(f"restart suppressed node={node} cooldown_remaining={COOLDOWN_SECONDS - (now - last)}s reason={reason}")
        return False
    compose_target = compose_service_name(node)
    result = run(compose_command("restart", compose_target), timeout=180)
    if result.returncode != 0:
        fallback = run(["docker", "restart", node], timeout=180)
        if fallback.returncode == 0:
            result = fallback
    restarted = dict(state.get("last_restart_at_by_node") or {})
    restarted[node] = now
    state["last_restart_at_by_node"] = restarted
    state["last_restart_reason_by_node"] = {**dict(state.get("last_restart_reason_by_node") or {}), node: reason}
    log(f"restart node={node} rc={result.returncode} reason={reason} stdout={result.stdout.strip()} stderr={result.stderr.strip()}")
    return result.returncode == 0


def start_node(node: str, reason: str, state: dict[str, Any], now: int) -> bool:
    last = int((state.get("last_restart_at_by_node") or {}).get(node) or 0)
    if now - last < COOLDOWN_SECONDS:
        log(f"start suppressed node={node} cooldown_remaining={COOLDOWN_SECONDS - (now - last)}s reason={reason}")
        return False
    compose_target = compose_service_name(node)
    result = run(compose_command("up", "-d", "--no-deps", compose_target), timeout=180)
    if result.returncode != 0:
        fallback = run(["docker", "start", node], timeout=180)
        if fallback.returncode == 0:
            result = fallback
    restarted = dict(state.get("last_restart_at_by_node") or {})
    restarted[node] = now
    state["last_restart_at_by_node"] = restarted
    state["last_restart_reason_by_node"] = {**dict(state.get("last_restart_reason_by_node") or {}), node: reason}
    log(f"start node={node} rc={result.returncode} reason={reason} stdout={result.stdout.strip()} stderr={result.stderr.strip()}")
    return result.returncode == 0


def main() -> int:
    ensure_runtime()
    lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another node-child-guard run is active; skipping")
        os.close(lock_fd)
        return 0

    now = int(time.time())
    state = read_state()
    rpc_refused_since = dict(state.get("rpc_refused_since_by_node") or {})
    rpc_wedged_since = dict(state.get("rpc_wedged_since_by_node") or {})

    for node in NODES:
        info = inspect_container(node)
        if not info.get("exists"):
            log(f"skip missing node={node}")
            continue
        if not info.get("running"):
            start_node(node, f"container status={info.get('status')}", state, now)
            continue

        child = bdag_child_running(node)
        rpc_ok = tcp_open(str(info.get("ip") or ""), 38131)
        ws_ok = tcp_open(str(info.get("ip") or ""), 18546)
        rpc_json_ok = False
        rpc_json_error = ""
        if rpc_ok:
            rpc_json_ok, rpc_json_error = json_rpc_probe(str(info.get("ip") or ""))
        state.setdefault("last_seen_by_node", {})[node] = {
            "at": now_iso(),
            "child_running": child,
            "rpc_open": rpc_ok,
            "rpc_json_ok": rpc_json_ok,
            "rpc_json_error": rpc_json_error,
            "ws_open": ws_ok,
            "ip": info.get("ip") or "",
        }
        if not child:
            rpc_refused_since.pop(node, None)
            rpc_wedged_since.pop(node, None)
            restart_node(node, "bdag child process missing while container is running", state, now)
            continue
        if rpc_json_ok:
            rpc_refused_since.pop(node, None)
            rpc_wedged_since.pop(node, None)
            log(f"ok node={node} child=true rpc_open={rpc_ok} rpc_json=true ws_open={ws_ok}")
            continue
        if rpc_ok:
            rpc_refused_since.pop(node, None)
            first_wedged = int(rpc_wedged_since.get(node) or now)
            rpc_wedged_since[node] = first_wedged
            wedged_for = now - first_wedged
            log(
                f"rpc wedged node={node} child=true wedged_for={wedged_for}s "
                f"ip={info.get('ip') or ''} error={rpc_json_error}"
            )
            if wedged_for >= RPC_WEDGED_SECONDS:
                restart_node(node, f"JSON-RPC unhealthy for {wedged_for}s while bdag child is running: {rpc_json_error}", state, now)
                rpc_wedged_since.pop(node, None)
            continue
        if ws_ok:
            rpc_refused_since.pop(node, None)
            rpc_wedged_since.pop(node, None)
            log(f"ok node={node} child=true rpc_open=false rpc_json=skipped ws_open=true")
            continue
        rpc_wedged_since.pop(node, None)
        first_refused = int(rpc_refused_since.get(node) or now)
        rpc_refused_since[node] = first_refused
        refused_for = now - first_refused
        log(f"rpc refused node={node} child=true refused_for={refused_for}s ip={info.get('ip') or ''}")
        if refused_for >= RPC_REFUSED_SECONDS:
            restart_node(node, f"RPC/WS refused for {refused_for}s while bdag child is running", state, now)
            rpc_refused_since.pop(node, None)

    state["rpc_refused_since_by_node"] = rpc_refused_since
    state["rpc_wedged_since_by_node"] = rpc_wedged_since
    state["updated_at"] = now_iso()
    write_state(state)
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

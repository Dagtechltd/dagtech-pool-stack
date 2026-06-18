#!/usr/bin/env python3
"""Wait until the BlockDAG backend is healthy enough for productive mining."""

from __future__ import annotations

import argparse
import json
import time
from typing import Any

from incident_journal import append_incident
from pool_ops import collect_status, now_iso
from rpc_router import recommend_rpc_primary, write_rpc_router_state


def guard_state(status: dict[str, Any]) -> dict[str, Any]:
    decision = recommend_rpc_primary(status)
    write_rpc_router_state(status, decision)
    scores = decision.get("scores") or {}
    healthy_nodes = [
        node
        for node, item in scores.items()
        if float(item.get("score") or 0) >= 75
        and not item.get("mining_template_failing")
        and item.get("child_running")
        and not item.get("critical")
    ]
    pool = status.get("pool_health") or status.get("pool") or {}
    stack_failures = status.get("stack_failures") or []
    ready = bool(
        not stack_failures
        and healthy_nodes
        and status.get("overall") in {"ok", "syncing"}
        and not pool.get("initial_download")
    )
    return {
        "generated_at": now_iso(),
        "ready": ready,
        "overall": status.get("overall"),
        "status_reason": status.get("status_reason"),
        "healthy_nodes": healthy_nodes,
        "current_primary": decision.get("current_primary"),
        "recommended_primary": decision.get("recommended_primary"),
        "router_decision": decision,
        "stack_failures": stack_failures,
        "pool_initial_download": pool.get("initial_download"),
    }


def wait_until_ready(timeout_seconds: int, interval_seconds: int) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last_state: dict[str, Any] = {}
    while True:
        status = collect_status(include_logs=True)
        last_state = guard_state(status)
        if last_state["ready"]:
            append_incident(
                "startup_guard_ready",
                "info",
                "startup-guard",
                "backend has at least one healthy template-capable node",
                last_state,
                status=status,
            )
            return last_state
        if time.time() >= deadline:
            append_incident(
                "startup_guard_timeout",
                "warning",
                "startup-guard",
                "backend did not become healthy before timeout",
                last_state,
                status=status,
            )
            return last_state
        time.sleep(max(1, interval_seconds))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait", action="store_true", help="wait until ready instead of checking once")
    parser.add_argument("--timeout", type=int, default=600, help="wait timeout in seconds")
    parser.add_argument("--interval", type=int, default=10, help="poll interval in seconds")
    parser.add_argument("--json", action="store_true", help="print JSON output")
    args = parser.parse_args()

    payload = wait_until_ready(args.timeout, args.interval) if args.wait else guard_state(collect_status(include_logs=True))
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(f"ready={payload['ready']} overall={payload.get('overall')} healthy_nodes={','.join(payload.get('healthy_nodes') or [])}")
        print(f"current={payload.get('current_primary')} recommended={payload.get('recommended_primary')}")
        if payload.get("status_reason"):
            print(f"reason={payload.get('status_reason')}")
    return 0 if payload["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

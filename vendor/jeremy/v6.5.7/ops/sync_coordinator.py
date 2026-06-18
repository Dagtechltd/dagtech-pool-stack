#!/usr/bin/env python3
"""Record active-node catch-up state without mutating the stack."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any


def bootstrap_stack_env() -> None:
    project_root = Path(os.environ.get("BDAG_PROJECT_ROOT") or Path(__file__).resolve().parents[1])
    runtime_dir = Path(os.environ.get("BDAG_RUNTIME_DIR") or project_root / "ops" / "runtime")
    candidates = [
        Path(os.environ["BDAG_OPS_ENV_FILE"]) if os.environ.get("BDAG_OPS_ENV_FILE") else runtime_dir / "ops.env",
        Path(os.environ["BDAG_POOL_ENV_FILE"]) if os.environ.get("BDAG_POOL_ENV_FILE") else None,
        project_root / ".env",
        project_root / "asic-pool" / ".env",
    ]
    for path in candidates:
        if path is None:
            continue
        if not path.is_absolute():
            path = project_root / path
        if not path.exists():
            continue
        for line in path.read_text(errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            if stripped.startswith("export "):
                stripped = stripped[7:].strip()
            key, value = stripped.split("=", 1)
            value = value.strip().strip("'\"")
            os.environ.setdefault(key.strip(), value)


bootstrap_stack_env()

from pool_ops import NODES, RUNTIME_DIR, now_iso, write_json_file  # noqa: E402
from stack_status_source import collect_stack_status  # noqa: E402


STATE_FILE = RUNTIME_DIR / "sync-coordinator-state.json"


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_state() -> dict[str, Any]:
    status = collect_stack_status(include_logs=False)
    nodes = status.get("nodes") if isinstance(status, dict) else {}
    sync_progress = status.get("sync_progress") if isinstance(status, dict) else {}
    active_node = NODES[0] if NODES else "node"
    node_info = nodes.get(active_node, {}) if isinstance(nodes, dict) else {}
    remaining = safe_int(node_info.get("remaining_blocks"), safe_int(sync_progress.get("remaining_blocks") if isinstance(sync_progress, dict) else 0))
    state = {
        "updated_at": now_iso(),
        "mode": "active_node_catchup",
        "action": "monitor",
        "repairable": False,
        "reason": "single-backend topology; coordinator does not stop or copy node data",
        "active_node": active_node,
        "nodes": {
            active_node: {
                "running": bool(node_info.get("running")),
                "height": safe_int(node_info.get("latest_block")),
                "remaining_blocks": remaining,
                "importing": bool(node_info.get("importing")),
                "last_import_age_seconds": safe_int(node_info.get("last_import_age_seconds")),
            }
        },
        "sync_status": sync_progress.get("status") if isinstance(sync_progress, dict) else None,
        "overall": status.get("overall") if isinstance(status, dict) else None,
    }
    return state


def run_once(json_output: bool = False) -> dict[str, Any]:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    state = build_state()
    write_json_file(STATE_FILE, state)
    if json_output:
        print(json.dumps(state, indent=2, sort_keys=True))
    return state


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record BlockDAG active-node sync state")
    parser.add_argument("--once", action="store_true", help="run one check and write state")
    parser.add_argument("--loop", action="store_true", help="run continuously")
    parser.add_argument("--repair", action="store_true", help="accepted for compatibility; no mutation is performed")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args, _unknown = parser.parse_known_args(argv)
    if args.loop:
        while True:
            run_once(json_output=args.json)
            time.sleep(max(1, args.interval))
    run_once(json_output=args.json or args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

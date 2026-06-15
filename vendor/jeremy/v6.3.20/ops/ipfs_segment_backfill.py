#!/usr/bin/env python3
"""Bounded candidate backfill runner for signed BlockDAG IPFS segments.

This script intentionally writes to a separate candidate index by default. It
does not update the live discovery/IPNS pointer unless the caller explicitly
sets BDAG_IPFS_SEGMENT_UPDATE_DISCOVERY_FOR_CUSTOM_INDEX=1.
"""

from __future__ import annotations

import argparse
import math
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(os.environ.get("BDAG_PROJECT_ROOT") or Path(__file__).resolve().parents[1]).resolve()
ENV_FILE = Path(os.environ.get("BDAG_ENV_FILE") or ROOT / ".env")
OPS_DIR = ROOT / "ops"
sys.path.insert(0, str(OPS_DIR))
import ipfs_segment_writer  # type: ignore  # noqa: E402


def env_int(env: Mapping[str, str], key: str, default: int) -> int:
    value = str(env.get(key, "")).strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        tmp = Path(handle.name)
    tmp.replace(path)


def resolve_path(env: Mapping[str, str], key: str, default: str) -> Path:
    value = str(env.get(key) or default).strip()
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def next_start_order(index: Mapping[str, Any], default_start: int) -> int:
    segments = ipfs_segment_writer.segments(index)
    if segments:
        return int(segments[-1].get("end_order") or 0) + 1
    head = ipfs_segment_writer.current_head(index)
    if head and isinstance(head.get("end_order"), int):
        return int(head["end_order"]) + 1
    return default_start


def normalized_start_order(value: int) -> int:
    """Backfill segment payloads start at order 1.

    Order 0 is genesis identity for validation, not a backfilled order-range
    payload in the current segment format.
    """

    return max(1, value)


def backfill_status_path(env: Mapping[str, str]) -> Path:
    return resolve_path(env, "BDAG_IPFS_BACKFILL_STATUS_FILE", "./ops/runtime/ipfs-content/backfill-status.json")


def candidate_index_path(env: Mapping[str, str]) -> Path:
    return resolve_path(env, "BDAG_IPFS_BACKFILL_INDEX_PATH", "./ops/runtime/ipfs-content/backfill-genesis-index.json")


def run_backfill_batch(env: dict[str, str], *, index_path: Path, start: int, stop: int, segments: int, orders_per_segment: int) -> dict[str, Any]:
    written = 0
    current = start
    last_rc = 0
    deferred_reason = ""
    ranges: list[dict[str, int]] = []
    previous_discovery_policy = os.environ.get("BDAG_IPFS_SEGMENT_UPDATE_DISCOVERY_FOR_CUSTOM_INDEX")
    previous_ipns_policy = os.environ.get("BDAG_IPFS_SEGMENT_PUBLISH_IPNS")
    try:
        os.environ["BDAG_IPFS_SEGMENT_UPDATE_DISCOVERY_FOR_CUSTOM_INDEX"] = "0"
        os.environ["BDAG_IPFS_SEGMENT_PUBLISH_IPNS"] = "0"
        while written < segments and current <= stop:
            end = min(stop, current + orders_per_segment - 1)
            rc = ipfs_segment_writer.main(
                [
                    "--index",
                    str(index_path),
                    "--start-order",
                    str(current),
                    "--end-order",
                    str(end),
                ]
            )
            last_rc = rc
            if rc != 0:
                break
            updated_index = ipfs_segment_writer.load_json(index_path)
            updated_head = ipfs_segment_writer.current_head(updated_index)
            if not updated_head or int(updated_head.get("end_order") or 0) < end:
                deferred_reason = "writer_completed_without_advancing_candidate_index"
                break
            ranges.append({"start_order": current, "end_order": end})
            written += 1
            current = end + 1
    finally:
        if previous_discovery_policy is None:
            os.environ.pop("BDAG_IPFS_SEGMENT_UPDATE_DISCOVERY_FOR_CUSTOM_INDEX", None)
        else:
            os.environ["BDAG_IPFS_SEGMENT_UPDATE_DISCOVERY_FOR_CUSTOM_INDEX"] = previous_discovery_policy
        if previous_ipns_policy is None:
            os.environ.pop("BDAG_IPFS_SEGMENT_PUBLISH_IPNS", None)
        else:
            os.environ["BDAG_IPFS_SEGMENT_PUBLISH_IPNS"] = previous_ipns_policy
    state = "complete" if written and current > stop else "advanced" if written else "deferred" if deferred_reason else "failed" if last_rc else "complete"
    return {
        "state": state,
        "reason": deferred_reason,
        "segments_written": written,
        "ranges": ranges,
        "next_start_order": current,
        "last_rc": last_rc,
    }


def build_plan(
    *,
    index_path: Path,
    status_file: Path,
    index: Mapping[str, Any],
    default_start: int,
    stop: int,
    max_segments: int,
    orders_per_segment: int,
) -> dict[str, Any]:
    start = next_start_order(index, normalized_start_order(default_start))
    remaining_orders = max(0, stop - start + 1) if stop > 0 else 0
    segments_remaining = math.ceil(remaining_orders / orders_per_segment) if remaining_orders else 0
    planned_segments_this_run = min(max_segments, segments_remaining) if stop > 0 else 0
    return {
        "generated_at": now_iso(),
        "state": "planned" if stop > 0 else "blocked",
        "reason": "" if stop > 0 else "stop_order_required",
        "index_path": str(index_path),
        "status_file": str(status_file),
        "next_start_order": start,
        "stop_order": stop,
        "orders_per_segment": orders_per_segment,
        "max_segments_per_run": max_segments,
        "remaining_orders": remaining_orders,
        "segments_remaining": segments_remaining,
        "planned_segments_this_run": planned_segments_this_run,
        "last_planned_end_order": min(stop, start + planned_segments_this_run * orders_per_segment - 1)
        if planned_segments_this_run
        else None,
        "genesis_order_policy": "order_0_is_genesis_identity_only; segment backfill payloads start at order 1",
        "promotion_policy": "candidate_only_no_discovery_or_ipns_until_full_verification",
        "mutation_policy": "plan_only_no_rpc_no_ipfs_no_index_write_except_status",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", default="", help="candidate backfill index path")
    parser.add_argument("--start-order", type=int, default=0, help="first order for an empty candidate index")
    parser.add_argument("--stop-order", type=int, default=0, help="inclusive stop order for this bounded run")
    parser.add_argument("--max-segments", type=int, default=0, help="maximum segments to append in this run")
    parser.add_argument("--status-file", default="", help="status JSON path")
    parser.add_argument("--plan", action="store_true", help="write a read-only backfill plan without RPC/IPFS/index mutation")
    parser.add_argument("--json", action="store_true", help="print status JSON")
    args = parser.parse_args(argv)

    env = ipfs_segment_writer.load_env()
    index_path = Path(args.index).expanduser().resolve() if args.index else candidate_index_path(env)
    status_file = Path(args.status_file).expanduser().resolve() if args.status_file else backfill_status_path(env)
    default_start = normalized_start_order(args.start_order or env_int(env, "BDAG_IPFS_BACKFILL_START_ORDER", 1))
    stop = args.stop_order or env_int(env, "BDAG_IPFS_BACKFILL_STOP_ORDER", 0)
    max_segments = args.max_segments or env_int(env, "BDAG_IPFS_BACKFILL_MAX_SEGMENTS_PER_RUN", 1)
    orders_per_segment = max(1, env_int(env, "BDAG_IPFS_SEGMENT_ORDERS_PER_SEGMENT", 300))
    index = ipfs_segment_writer.load_json(index_path)

    if args.plan:
        payload = build_plan(
            index_path=index_path,
            status_file=status_file,
            index=index,
            default_start=default_start,
            stop=stop,
            max_segments=max_segments,
            orders_per_segment=orders_per_segment,
        )
        atomic_write_json(status_file, payload)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["state"] == "planned" else 1

    if stop <= 0:
        payload = {
            "generated_at": now_iso(),
            "state": "blocked",
            "reason": "stop_order_required",
            "index_path": str(index_path),
            "note": "Set --stop-order or BDAG_IPFS_BACKFILL_STOP_ORDER so backfill remains bounded.",
        }
        atomic_write_json(status_file, payload)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return 1

    start = next_start_order(index, default_start)
    if start > stop:
        payload = {
            "generated_at": now_iso(),
            "state": "complete",
            "index_path": str(index_path),
            "next_start_order": start,
            "stop_order": stop,
        }
        atomic_write_json(status_file, payload)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    payload = run_backfill_batch(
        env,
        index_path=index_path,
        start=start,
        stop=stop,
        segments=max_segments,
        orders_per_segment=orders_per_segment,
    )
    payload.update(
        {
            "generated_at": now_iso(),
            "index_path": str(index_path),
            "status_file": str(status_file),
            "start_order": start,
            "stop_order": stop,
            "orders_per_segment": orders_per_segment,
            "promotion_policy": "candidate_only_no_discovery_or_ipns_until_full_verification",
        }
    )
    atomic_write_json(status_file, payload)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["last_rc"] == 0 else int(payload["last_rc"])


if __name__ == "__main__":
    raise SystemExit(main())

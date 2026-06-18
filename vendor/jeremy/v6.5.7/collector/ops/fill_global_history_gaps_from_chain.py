#!/usr/bin/env python3
"""Fill Global-tab history gaps with samples reconstructed from chain headers."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pool_ops  # noqa: E402
from reconstruct_global_history_from_chain import (  # noqa: E402
    fetch_headers,
    header_timestamp,
    load_price,
    parse_dashboard_time,
    reconstruct_snapshot,
    reward_bdag,
    validate_jsonl,
)


def fetch_one_header(
    rpc_urls: list[tuple[str, str]],
    number: int,
    cache: dict[int, dict[str, Any]],
    batch_size: int,
    timeout: float,
) -> dict[str, Any]:
    if number not in cache:
        headers, errors = fetch_headers(rpc_urls, [number], batch_size, timeout)
        cache.update(headers)
        if number not in cache:
            raise RuntimeError("; ".join(errors[-3:]) or f"unable to fetch block {number}")
    return cache[number]


def block_at_or_before_time(
    rpc_urls: list[tuple[str, str]],
    target: datetime,
    low: int,
    high: int,
    cache: dict[int, dict[str, Any]],
    batch_size: int,
    timeout: float,
) -> int:
    target_epoch = int(target.timestamp())
    best = low
    while low <= high:
        mid = (low + high) // 2
        header = fetch_one_header(rpc_urls, mid, cache, batch_size, timeout)
        stamp = header_timestamp(header)
        if stamp <= target_epoch:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    return best


def backup_runtime_files(label: str) -> Path:
    backup_dir = pool_ops.RUNTIME_DIR / "plot-history-backups" / label
    backup_dir.mkdir(parents=True, exist_ok=True)
    for path in [pool_ops.GLOBAL_HISTORY_FILE, pool_ops.GLOBAL_CACHE_FILE]:
        if path.exists():
            shutil.copy2(path, backup_dir / path.name)
    return backup_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Fill missing Global history plot samples from chain headers.")
    parser.add_argument("--since-hours", type=float, default=24.0)
    parser.add_argument("--gap-threshold-seconds", type=int, default=7 * 60)
    parser.add_argument("--target-interval-seconds", type=int, default=5 * 60)
    parser.add_argument("--block-window", type=int, default=pool_ops.GLOBAL_BLOCK_WINDOW)
    parser.add_argument("--batch-size", type=int, default=96)
    parser.add_argument("--rpc-timeout", type=float, default=12.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows = pool_ops.read_jsonl_file(pool_ops.GLOBAL_HISTORY_FILE, limit=None)
    parsed = []
    for row in rows:
        generated_at = parse_dashboard_time(row.get("generated_at"))
        latest_block = int(row.get("latest_block") or 0)
        if generated_at is not None and latest_block > 0:
            parsed.append((generated_at, latest_block, row))
    if len(parsed) < 2:
        raise SystemExit("not enough Global history rows to inspect gaps")
    parsed.sort(key=lambda item: (item[0], item[1]))

    latest_time = parsed[-1][0]
    cutoff = latest_time - timedelta(hours=args.since_hours)
    gaps: list[tuple[datetime, int, datetime, int]] = []
    for previous, current in zip(parsed, parsed[1:]):
        prev_time, prev_block, _ = previous
        cur_time, cur_block, _ = current
        if cur_time < cutoff:
            continue
        if (cur_time - prev_time).total_seconds() > args.gap_threshold_seconds:
            gaps.append((prev_time, prev_block, cur_time, cur_block))
    if not gaps:
        print("no fillable Global history gaps found")
        return 0

    rpc_urls = pool_ops.node_rpc_urls()
    if not rpc_urls:
        raise SystemExit("no node RPC URLs available")
    avg_reward = reward_bdag(rows)
    price = load_price()
    header_cache: dict[int, dict[str, Any]] = {}
    synthetic_rows: list[dict[str, Any]] = []
    started = time.time()
    for prev_time, prev_block, cur_time, cur_block in gaps:
        target = prev_time + timedelta(seconds=args.target_interval_seconds)
        while target < cur_time:
            target_block = block_at_or_before_time(
                rpc_urls,
                target,
                prev_block,
                cur_block,
                header_cache,
                args.batch_size,
                args.rpc_timeout,
            )
            start_block = max(0, target_block - args.block_window + 1)
            missing = [number for number in range(start_block, target_block + 1) if number not in header_cache]
            headers, _errors = fetch_headers(rpc_urls, missing, args.batch_size, args.rpc_timeout)
            header_cache.update(headers)
            window_headers = [header_cache[number] for number in range(start_block, target_block + 1) if number in header_cache]
            synthetic_rows.append(
                reconstruct_snapshot(
                    {
                        "generated_at": target.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "latest_block": target_block,
                    },
                    window_headers,
                    avg_reward,
                    price,
                    args.block_window,
                )
            )
            target += timedelta(seconds=args.target_interval_seconds)

    existing_keys = {(row.get("generated_at"), int(row.get("latest_block") or 0)) for _, _, row in parsed}
    merged = [pool_ops.annotate_global_pool_labels(dict(row)) for _, _, row in parsed]
    for row in synthetic_rows:
        key = (row.get("generated_at"), int(row.get("latest_block") or 0))
        if key not in existing_keys:
            merged.append(row)
            existing_keys.add(key)
    merged.sort(
        key=lambda row: (
            parse_dashboard_time(row.get("generated_at")) or datetime.min.replace(tzinfo=timezone.utc),
            int(row.get("latest_block") or 0),
        )
    )

    label = f"{time.strftime('%Y%m%d-%H%M%S')}-global-chain-gap-fill"
    out_path = pool_ops.RUNTIME_DIR / f"global-history.gap-filled-{label}.jsonl"
    pool_ops.write_jsonl_file(out_path, merged, mode=0o600)
    count, missing_required = validate_jsonl(out_path)
    summary = {
        "generated_at": pool_ops.now_iso(),
        "dry_run": args.dry_run,
        "gaps_found": len(gaps),
        "synthetic_rows_added": len(synthetic_rows),
        "history_rows_before": len(rows),
        "history_rows_after": count,
        "missing_required_fields": missing_required,
        "block_window": args.block_window,
        "avg_reward_bdag": str(avg_reward) if isinstance(avg_reward, Decimal) else None,
        "elapsed_seconds": round(time.time() - started, 3),
        "output_path": str(out_path),
        "gap_ranges": [
            {
                "from": prev_time.isoformat(),
                "to": cur_time.isoformat(),
                "seconds": int((cur_time - prev_time).total_seconds()),
                "from_block": prev_block,
                "to_block": cur_block,
            }
            for prev_time, prev_block, cur_time, cur_block in gaps
        ],
    }
    report_path = pool_ops.RUNTIME_DIR / f"global-history-gap-fill-report-{label}.json"
    pool_ops.write_json_file(report_path, summary, mode=0o600)
    if missing_required:
        raise SystemExit(f"validation failed: {missing_required} rows missing required fields")
    if not args.dry_run:
        backup_dir = backup_runtime_files(label)
        shutil.copy2(out_path, pool_ops.GLOBAL_HISTORY_FILE)
        cache = pool_ops.read_json_file(pool_ops.GLOBAL_CACHE_FILE, {})
        if isinstance(cache, dict):
            cache["history"] = merged[-pool_ops.GLOBAL_HISTORY_LIMIT :]
            cache["global_history_gap_filled_at"] = pool_ops.now_iso()
            cache["global_history_gap_fill_report"] = str(report_path)
            pool_ops.write_json_file(pool_ops.GLOBAL_CACHE_FILE, cache, mode=0o600)
        summary["backup_dir"] = str(backup_dir)
        pool_ops.write_json_file(report_path, summary, mode=0o600)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

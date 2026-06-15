#!/usr/bin/env python3
"""Rebuild Global-tab plot history from BlockDAG chain headers.

The Global dashboard plot is derived from recent block headers. If the dashboard
cache/history is stale, out of order, or partially missing fiat/reward fields,
this script reconstructs the history from node RPC instead of trusting the old
cached cluster rows.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pool_ops  # noqa: E402


RPC_TIMEOUT = 12.0


def parse_dashboard_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def rpc_batch_call(url: str, requests: list[dict[str, Any]], timeout: float) -> list[dict[str, Any]]:
    body = json.dumps(requests, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"content-type": "application/json", "accept": "application/json", "user-agent": pool_ops.HTTP_USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read(50_000_000).decode("utf-8", "replace"))
    if not isinstance(payload, list):
        raise RuntimeError(f"batch RPC response was not a list: {type(payload).__name__}")
    return [item for item in payload if isinstance(item, dict)]


def fetch_headers(
    rpc_urls: list[tuple[str, str]],
    numbers: list[int],
    batch_size: int,
    timeout: float,
) -> tuple[dict[int, dict[str, Any]], list[str]]:
    headers: dict[int, dict[str, Any]] = {}
    errors: list[str] = []
    pending = sorted({number for number in numbers if number >= 0})
    for offset in range(0, len(pending), batch_size):
        chunk = pending[offset : offset + batch_size]
        requests = [
            {"jsonrpc": "2.0", "id": number, "method": "eth_getBlockByNumber", "params": [hex(number), False]}
            for number in chunk
        ]
        chunk_done = False
        for source_name, url in rpc_urls:
            try:
                response = rpc_batch_call(url, requests, timeout)
            except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError, RuntimeError) as exc:
                errors.append(f"{source_name}: batch {chunk[0]}-{chunk[-1]} failed: {exc}")
                continue
            by_id = {int(item.get("id")): item for item in response if item.get("id") is not None}
            missing: list[int] = []
            for number in chunk:
                item = by_id.get(number)
                result = item.get("result") if isinstance(item, dict) else None
                if isinstance(result, dict):
                    result["_rpc_source"] = source_name
                    headers[number] = result
                else:
                    missing.append(number)
            if missing:
                errors.append(f"{source_name}: missing {len(missing)} headers in batch {chunk[0]}-{chunk[-1]}")
            if len(missing) < len(chunk):
                chunk_done = True
                break
        if not chunk_done:
            errors.append(f"all RPC sources failed for batch {chunk[0]}-{chunk[-1]}")
    return headers, errors


def decimal_or_none(value: Any) -> Decimal | None:
    try:
        if value is None or value == "":
            return None
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def inferred_reward_bdag(rows: list[dict[str, Any]]) -> Decimal | None:
    samples: list[Decimal] = []
    for row in rows:
        for cluster in row.get("clusters") or []:
            if not isinstance(cluster, dict):
                continue
            blocks = int(cluster.get("blocks") or 0)
            estimated = decimal_or_none(cluster.get("estimated_bdag"))
            if blocks > 0 and estimated is not None and estimated > 0:
                samples.append(estimated / Decimal(blocks))
    if not samples:
        return None
    samples.sort()
    return samples[len(samples) // 2]


def reward_bdag(rows: list[dict[str, Any]]) -> Decimal | None:
    try:
        reward_summary = pool_ops.pool_db_json(
            """
            SELECT json_build_object(
              'avg_reward_wei', COALESCE(avg(reward), 0)::text
            )
            FROM blocks;
            """
        )
        value = pool_ops.wei_to_bdag((reward_summary or {}).get("avg_reward_wei"))
        if value > 0:
            return value
    except Exception:
        pass
    return inferred_reward_bdag(rows)


def load_price() -> dict[str, Any]:
    price = pool_ops.read_json_file(pool_ops.PRICE_CACHE_FILE, {})
    if isinstance(price, dict) and price.get("usd") is not None and price.get("zar") is not None:
        return price
    return {}


def fiat_value(amount_bdag: Decimal | None, price: dict[str, Any], currency: str) -> str | None:
    if amount_bdag is None:
        return None
    value = decimal_or_none(price.get(currency))
    if value is None:
        return None
    return pool_ops.decimal_to_str(amount_bdag * value, places=2)


def header_number(header: dict[str, Any]) -> int:
    return int(str(header.get("number") or "0"), 16)


def header_timestamp(header: dict[str, Any]) -> int:
    return int(str(header.get("timestamp") or "0"), 16)


def reconstruct_snapshot(
    original: dict[str, Any],
    headers: list[dict[str, Any]],
    avg_reward_bdag: Decimal | None,
    price: dict[str, Any],
    block_window: int,
) -> dict[str, Any]:
    headers = sorted(headers, key=header_number)
    latest_block = int(original.get("latest_block") or (header_number(headers[-1]) if headers else 0))
    generated_at = original.get("generated_at") or pool_ops.now_iso()
    cluster_map: dict[str, dict[str, Any]] = {}
    first_seen_epoch: int | None = None
    last_seen_epoch: int | None = None
    rpc_sources: Counter[str] = Counter()
    for header in headers:
        address = str(header.get("miner") or header.get("author") or header.get("coinbase") or "").lower()
        if not address:
            continue
        height = header_number(header)
        epoch = header_timestamp(header)
        source = str(header.get("_rpc_source") or "")
        if source:
            rpc_sources[source] += 1
        item = cluster_map.setdefault(
            address,
            {
                "address": address,
                "blocks": 0,
                "first_height": height,
                "last_height": height,
                "first_seen_epoch": epoch,
                "last_seen_epoch": epoch,
                "rpc_sources": [],
            },
        )
        item["blocks"] += 1
        item["first_height"] = min(int(item["first_height"]), height)
        item["last_height"] = max(int(item["last_height"]), height)
        item["first_seen_epoch"] = min(int(item["first_seen_epoch"]), epoch)
        item["last_seen_epoch"] = max(int(item["last_seen_epoch"]), epoch)
        if source:
            item["rpc_sources"].append(source)
        first_seen_epoch = epoch if first_seen_epoch is None else min(first_seen_epoch, epoch)
        last_seen_epoch = epoch if last_seen_epoch is None else max(last_seen_epoch, epoch)

    total_blocks = max(1, len(headers))
    window_seconds = max(1, int((last_seen_epoch or 0) - (first_seen_epoch or 0)))
    scan_window_hours = Decimal(window_seconds) / Decimal("3600")
    clusters: list[dict[str, Any]] = []
    for rank, cluster in enumerate(
        sorted(cluster_map.values(), key=lambda item: (int(item["blocks"]), int(item["last_seen_epoch"])), reverse=True),
        start=1,
    ):
        blocks = int(cluster["blocks"])
        share = Decimal(blocks) / Decimal(total_blocks)
        estimated_bdag = avg_reward_bdag * Decimal(blocks) if avg_reward_bdag is not None else None
        bdag_hour = estimated_bdag / scan_window_hours if estimated_bdag is not None and scan_window_hours > 0 else None
        clusters.append(
            {
                "rank": rank,
                "address": cluster["address"],
                "address_short": pool_ops.short_eth_address(str(cluster["address"])),
                "pool_name": "",
                "pool_label": pool_ops.short_eth_address(str(cluster["address"])),
                "estimated_bdag_avg_hour": pool_ops.decimal_to_str(bdag_hour) if bdag_hour is not None else None,
                "estimated_usd_avg_hour": fiat_value(bdag_hour, price, "usd"),
                "estimated_zar_avg_hour": fiat_value(bdag_hour, price, "zar"),
                "estimated_bdag_recent_hour": pool_ops.decimal_to_str(bdag_hour) if bdag_hour is not None else None,
                "estimated_usd_recent_hour": fiat_value(bdag_hour, price, "usd"),
                "estimated_zar_recent_hour": fiat_value(bdag_hour, price, "zar"),
                "blocks": blocks,
                "share_percent": pool_ops.decimal_to_str(share * Decimal("100"), places=2),
                "estimated_bdag": pool_ops.decimal_to_str(estimated_bdag) if estimated_bdag is not None else None,
                "estimated_usd": fiat_value(estimated_bdag, price, "usd"),
                "estimated_zar": fiat_value(estimated_bdag, price, "zar"),
            }
        )
    snapshot = {
        "generated_at": generated_at,
        "latest_block": latest_block,
        "scan_window_hours": pool_ops.decimal_to_str(scan_window_hours, places=2),
        "clusters": clusters,
        "reconstructed": {
            "source": "chain_headers",
            "block_window": block_window,
            "header_count": len(headers),
            "rpc_sources": dict(rpc_sources),
        },
    }
    return pool_ops.annotate_global_pool_labels(snapshot)


def backup_runtime_files(label: str) -> Path:
    backup_dir = pool_ops.RUNTIME_DIR / "plot-history-backups" / label
    backup_dir.mkdir(parents=True, exist_ok=True)
    for path in [pool_ops.GLOBAL_HISTORY_FILE, pool_ops.GLOBAL_CACHE_FILE]:
        if path.exists():
            shutil.copy2(path, backup_dir / path.name)
    return backup_dir


def validate_jsonl(path: Path) -> tuple[int, int]:
    rows = 0
    missing = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        payload = json.loads(line)
        rows += 1
        if not payload.get("generated_at") or not payload.get("latest_block"):
            missing += 1
    return rows, missing


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconstruct Global dashboard history from BlockDAG chain headers.")
    parser.add_argument("--since-hours", type=float, default=72.0, help="rebuild samples newer than this many hours")
    parser.add_argument("--all", action="store_true", help="rebuild every retained global history row")
    parser.add_argument("--block-window", type=int, default=pool_ops.GLOBAL_BLOCK_WINDOW)
    parser.add_argument("--batch-size", type=int, default=96)
    parser.add_argument("--rpc-timeout", type=float, default=RPC_TIMEOUT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows = pool_ops.read_jsonl_file(pool_ops.GLOBAL_HISTORY_FILE, limit=None)
    if not rows:
        raise SystemExit("no global history rows to reconstruct")
    dated_rows: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        parsed = parse_dashboard_time(row.get("generated_at"))
        if parsed is not None:
            dated_rows.append((parsed, row))
    if not dated_rows:
        raise SystemExit("no parseable generated_at timestamps in global history")
    dated_rows.sort(key=lambda item: (item[0], int(item[1].get("latest_block") or 0)))

    latest_time = dated_rows[-1][0]
    cutoff = datetime.min.replace(tzinfo=timezone.utc) if args.all else latest_time.timestamp() - (args.since_hours * 3600)
    to_rebuild: list[tuple[datetime, dict[str, Any]]] = []
    kept: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, int]] = set()
    for dt, row in dated_rows:
        latest_block = int(row.get("latest_block") or 0)
        key = (str(row.get("generated_at")), latest_block)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if args.all or dt.timestamp() >= float(cutoff):
            to_rebuild.append((dt, row))
        else:
            kept.append(pool_ops.annotate_global_pool_labels(dict(row)))
    if not to_rebuild:
        raise SystemExit("no rows selected for reconstruction")

    rpc_urls = pool_ops.node_rpc_urls()
    if not rpc_urls:
        raise SystemExit("no node RPC URLs available")
    avg_reward = reward_bdag(rows)
    price = load_price()
    print(
        f"reconstructing {len(to_rebuild)} rows from {rpc_urls[0][0]} primary; "
        f"kept={len(kept)} block_window={args.block_window} avg_reward_bdag={avg_reward}",
        flush=True,
    )

    rebuilt: list[dict[str, Any]] = []
    header_cache: dict[int, dict[str, Any]] = {}
    all_errors: list[str] = []
    started = time.time()
    for index, (dt, row) in enumerate(to_rebuild, start=1):
        latest_block = int(row.get("latest_block") or 0)
        start_block = max(0, latest_block - args.block_window + 1)
        needed = [number for number in range(start_block, latest_block + 1) if number not in header_cache]
        headers, errors = fetch_headers(rpc_urls, needed, args.batch_size, args.rpc_timeout)
        header_cache.update(headers)
        all_errors.extend(errors[-10:])
        window_headers = [header_cache[number] for number in range(start_block, latest_block + 1) if number in header_cache]
        rebuilt.append(reconstruct_snapshot(row, window_headers, avg_reward, price, args.block_window))
        prune_before = start_block
        for number in [number for number in header_cache if number < prune_before]:
            del header_cache[number]
        if index == 1 or index == len(to_rebuild) or index % 25 == 0:
            elapsed = time.time() - started
            print(
                f"{index}/{len(to_rebuild)} latest={latest_block} headers={len(window_headers)} "
                f"new={len(needed)} elapsed={elapsed:.1f}s",
                flush=True,
            )

    rebuilt_by_key = {(str(row.get("generated_at")), int(row.get("latest_block") or 0)): row for row in rebuilt}
    output_rows: list[dict[str, Any]] = []
    for dt, row in dated_rows:
        key = (str(row.get("generated_at")), int(row.get("latest_block") or 0))
        if key in rebuilt_by_key:
            output_rows.append(rebuilt_by_key[key])
        elif row not in output_rows:
            output_rows.append(pool_ops.annotate_global_pool_labels(dict(row)))
    output_rows.sort(key=lambda item: (parse_dashboard_time(item.get("generated_at")) or datetime.min.replace(tzinfo=timezone.utc), int(item.get("latest_block") or 0)))

    label = f"{time.strftime('%Y%m%d-%H%M%S')}-global-chain-rebuild"
    out_path = pool_ops.RUNTIME_DIR / f"global-history.reconstructed-{label}.jsonl"
    pool_ops.write_jsonl_file(out_path, output_rows, mode=0o600)
    count, missing = validate_jsonl(out_path)
    summary = {
        "generated_at": pool_ops.now_iso(),
        "dry_run": args.dry_run,
        "history_rows_before": len(rows),
        "history_rows_after": count,
        "rows_rebuilt": len(rebuilt),
        "rows_kept": len(output_rows) - len(rebuilt),
        "missing_required_fields": missing,
        "block_window": args.block_window,
        "rpc_sources": [name for name, _ in rpc_urls],
        "avg_reward_bdag": str(avg_reward) if avg_reward is not None else None,
        "price_used": {"usd": price.get("usd"), "zar": price.get("zar")},
        "output_path": str(out_path),
        "errors": all_errors[-25:],
    }
    report_path = pool_ops.RUNTIME_DIR / f"global-history-rebuild-report-{label}.json"
    pool_ops.write_json_file(report_path, summary, mode=0o600)
    if missing:
        raise SystemExit(f"validation failed: {missing} rows missing required fields")
    if not args.dry_run:
        backup_dir = backup_runtime_files(label)
        shutil.copy2(out_path, pool_ops.GLOBAL_HISTORY_FILE)
        cache = pool_ops.read_json_file(pool_ops.GLOBAL_CACHE_FILE, {})
        if isinstance(cache, dict):
            cache["history"] = output_rows[-pool_ops.GLOBAL_HISTORY_LIMIT :]
            cache["global_history_rebuilt_at"] = pool_ops.now_iso()
            cache["global_history_rebuild_report"] = str(report_path)
            pool_ops.write_json_file(pool_ops.GLOBAL_CACHE_FILE, cache, mode=0o600)
        summary["backup_dir"] = str(backup_dir)
        pool_ops.write_json_file(report_path, summary, mode=0o600)
        print(f"installed rebuilt history; backup={backup_dir}", flush=True)
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Backfill miner plot fields from retained asic-pool logs.

The dashboard's miner plot history is stored in earnings-snapshots.jsonl. Some
older compact samples did not include blocks_found/share_work/hashrate fields.
This utility reconstructs what it can from retained pool logs without touching
mining services.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pool_ops import (
    EARNINGS_SNAPSHOT_FILE,
    POOL_ACTIVITY_LOG_LINES,
    RUNTIME_DIR,
    decimal_to_str,
    merge_unique_strings,
    now_iso,
    parse_earnings_timestamp,
    parse_pool_activity,
)


DOCKER_TS_RE = re.compile(r"^(\S+)\s+(.*)$")


def parse_docker_epoch(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_log_lines(raw_lines: list[str]) -> list[tuple[float, str]]:
    parsed: list[tuple[float, str]] = []
    for raw in raw_lines:
        match = DOCKER_TS_RE.match(raw.rstrip("\n"))
        if not match:
            continue
        epoch = parse_docker_epoch(match.group(1))
        if epoch is None:
            continue
        parsed.append((epoch, match.group(2)))
    parsed.sort(key=lambda item: item[0])
    return parsed


def docker_logs_since(since: str, until: str | None = None) -> list[str]:
    command = ["docker", "logs", "--timestamps", "--since", since]
    if until:
        command.extend(["--until", until])
    command.append("asic-pool")
    proc = subprocess.run(command, text=True, capture_output=True, check=False)
    text = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    return text.splitlines()


def read_log_lines(path: Path | None, since: str, until: str | None = None) -> list[str]:
    if path:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    return docker_logs_since(since, until)


def row_identity(row: dict[str, Any]) -> tuple[str, ...]:
    workers = row.get("workers") if isinstance(row.get("workers"), list) else []
    return tuple(str(worker) for worker in workers if worker)


def safe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def backfill_snapshot(
    snapshot: dict[str, Any],
    window_lines: deque[tuple[float, str]],
    overwrite: bool = False,
) -> tuple[bool, dict[str, int]]:
    stats = {
        "miner_rows_seen": 0,
        "miner_rows_matched": 0,
        "blocks_set": 0,
        "share_work_set": 0,
        "shares_set": 0,
        "hashrate_set": 0,
    }
    miners = snapshot.get("miner_estimates")
    if not isinstance(miners, list) or not miners or not window_lines:
        return False, stats

    first_epoch = window_lines[0][0]
    last_epoch = window_lines[-1][0]
    duration = max(1.0, last_epoch - first_epoch)
    activity = parse_pool_activity("\n".join(line for _, line in window_lines))
    by_ip = {str(item.get("ip")): item for item in activity.get("miners", []) if item.get("ip")}
    by_worker: dict[str, dict[str, Any]] = {}
    for item in activity.get("miners", []):
        for worker in item.get("workers", []) or []:
            by_worker[str(worker)] = item

    changed = False
    for row in miners:
        if not isinstance(row, dict):
            continue
        stats["miner_rows_seen"] += 1
        match = by_ip.get(str(row.get("ip") or ""))
        if match is None:
            for worker in row_identity(row):
                if worker in by_worker:
                    match = by_worker[worker]
                    break
        if match is None:
            continue
        stats["miner_rows_matched"] += 1

        field_pairs = (
            ("blocks_found", int(match.get("blocks_found", 0) or 0), "blocks_set"),
            ("share_work", int(match.get("share_work", 0) or 0), "share_work_set"),
            ("shares", int(match.get("shares", 0) or 0), "shares_set"),
        )
        for key, value, stat_key in field_pairs:
            if overwrite or key not in row:
                if row.get(key) != value:
                    row[key] = value
                    row[f"{key}_reconstructed"] = True
                    stats[stat_key] += 1
                    changed = True

        share_work = safe_float(row.get("share_work"))
        if share_work is not None and share_work > 0:
            # Pool share_work is difficulty * 65536. Multiplying by 1024 maps
            # accepted-work rate to observed ASIC GH/s on this X100 pool.
            estimated_ghs = (share_work / duration) * 1024.0 / 1_000_000_000.0
            if estimated_ghs > 0 and (overwrite or row.get("hashrate_ghs") is None):
                value = round(estimated_ghs, 3)
                row["hashrate_ghs"] = value
                row["av_hashrate_ghs"] = value
                row["hashrate"] = value
                row["av_hashrate"] = value
                row["hashrate_available"] = False
                row["hashrate_source"] = "reconstructed-share-work"
                row["hashrate_reconstructed"] = True
                stats["hashrate_set"] += 1
                changed = True

    if changed:
        snapshot["plot_reconstruction"] = {
            "source": "asic-pool docker logs",
            "backfilled_at": now_iso(),
            "log_window_lines": len(window_lines),
            "log_window_start": datetime.fromtimestamp(first_epoch, timezone.utc).isoformat(),
            "log_window_end": datetime.fromtimestamp(last_epoch, timezone.utc).isoformat(),
            "hashrate_note": "hashrate is estimated from accepted share_work over the retained log window, not recovered ASIC telemetry",
        }
    return changed, stats


def load_snapshots(path: Path) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                snapshots.append(item)
    return snapshots


def write_snapshots(path: Path, snapshots: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp-{int(time.time())}")
    with tmp.open("w", encoding="utf-8") as handle:
        for snapshot in snapshots:
            handle.write(json.dumps(snapshot, separators=(",", ":")) + "\n")
    tmp.chmod(0o600)
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-file", type=Path, default=EARNINGS_SNAPSHOT_FILE)
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--since", default="2026-05-11T07:21:00+02:00")
    parser.add_argument("--until")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    snapshots = load_snapshots(args.snapshot_file)
    raw_logs = read_log_lines(args.log_file, args.since, args.until)
    log_lines = parse_log_lines(raw_logs)
    if not log_lines:
        raise SystemExit("No timestamped asic-pool log lines were available for reconstruction.")

    window: deque[tuple[float, str]] = deque(maxlen=POOL_ACTIVITY_LOG_LINES)
    log_index = 0
    changed_count = 0
    skipped_before_logs = 0
    totals = {
        "miner_rows_seen": 0,
        "miner_rows_matched": 0,
        "blocks_set": 0,
        "share_work_set": 0,
        "shares_set": 0,
        "hashrate_set": 0,
    }

    timed_snapshots: list[tuple[float, dict[str, Any]]] = []
    for snapshot in snapshots:
        parsed = parse_earnings_timestamp(snapshot.get("generated_at"))
        if parsed is None:
            continue
        timed_snapshots.append((parsed.timestamp(), snapshot))
    timed_snapshots.sort(key=lambda item: item[0])

    for snapshot_epoch, snapshot in timed_snapshots:
        if snapshot_epoch < log_lines[0][0]:
            skipped_before_logs += 1
            continue
        while log_index < len(log_lines) and log_lines[log_index][0] <= snapshot_epoch:
            window.append(log_lines[log_index])
            log_index += 1
        changed, stats = backfill_snapshot(snapshot, window, overwrite=args.overwrite)
        if changed:
            changed_count += 1
        for key, value in stats.items():
            totals[key] += value

    backup_path = None
    if changed_count and not args.dry_run:
        backup_dir = RUNTIME_DIR / "plot-history-backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"earnings-snapshots-before-block-hashrate-backfill-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
        shutil.copy2(args.snapshot_file, backup_path)
        write_snapshots(args.snapshot_file, snapshots)

    report = {
        "status": "dry-run" if args.dry_run else "ok",
        "snapshot_file": str(args.snapshot_file),
        "backup_path": str(backup_path) if backup_path else None,
        "log_file": str(args.log_file) if args.log_file else "docker logs asic-pool",
        "log_line_count": len(log_lines),
        "log_start": datetime.fromtimestamp(log_lines[0][0], timezone.utc).isoformat(),
        "log_end": datetime.fromtimestamp(log_lines[-1][0], timezone.utc).isoformat(),
        "snapshots_total": len(snapshots),
        "snapshots_before_log_window": skipped_before_logs,
        "snapshots_changed": changed_count,
        "field_updates": totals,
        "hashrate_reconstruction": {
            "type": "effective_accepted_work_ghs",
            "formula": "share_work / log_window_seconds * 1024 / 1e9",
            "note": "This is reconstructed from accepted share work, not historical ASIC API telemetry.",
        },
    }
    report_path = RUNTIME_DIR / f"miner-plot-backfill-report-{time.strftime('%Y%m%d-%H%M%S')}.json"
    if not args.dry_run:
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        report_path.chmod(0o600)
    report["report_path"] = str(report_path)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Short before/after comparison for external mining-stack optimizations."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from pathlib import Path
from typing import Any

from pool_ops import RUNTIME_DIR, now_iso
from rpc_router import current_rpc_primary
from stack_ab_test import current_stack_name, scan_window


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(value: dt.datetime | int | float) -> str:
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.timezone.utc).isoformat()
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()


def per_min(count: int | float | None, seconds: int | float | None) -> float:
    if not seconds:
        return 0.0
    return round(float(count or 0) * 60 / float(seconds), 4)


def enrich(row: dict[str, Any]) -> dict[str, Any]:
    seconds = float(row.get("measured_seconds") or 0)
    logs = row.get("log_counts") or {}
    valid_shares = int(logs.get("valid_shares") or 0)
    submit_ok = int(logs.get("submit_ok") or 0)
    submit_errors = int(logs.get("submit_errors") or 0) + int(logs.get("block_submit_errors") or 0)
    too_late = int(logs.get("too_late") or 0) + int(logs.get("overdue") or 0)
    stale_jobs = int(logs.get("stale_jobs") or 0) + int(logs.get("stale_jobs_upper") or 0)
    duplicates = int(logs.get("duplicates") or 0)
    row["derived"] = {
        "valid_shares_per_min": per_min(valid_shares, seconds),
        "successful_submit_lines_per_min": per_min(submit_ok, seconds),
        "submit_errors_per_min": per_min(submit_errors, seconds),
        "too_late_per_min": per_min(too_late, seconds),
        "stale_jobs_per_min": per_min(stale_jobs, seconds),
        "duplicates_per_min": per_min(duplicates, seconds),
        "submit_errors_per_ok": round(submit_errors / max(1, submit_ok), 4),
        "too_late_per_ok": round(too_late / max(1, submit_ok), 4),
        "stale_jobs_per_ok": round(stale_jobs / max(1, submit_ok), 4),
        "duplicates_per_ok": round(duplicates / max(1, submit_ok), 4),
    }
    return row


def pct_delta(before: float | None, after: float | None) -> float | None:
    if before is None or after is None or before == 0:
        return None
    return round((after - before) * 100 / before, 3)


def comparison(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    b = before.get("derived") or {}
    a = after.get("derived") or {}
    fields = [
        "local_chain_share_pct",
        "local_blocks_per_hour",
        "db_blocks_per_hour",
        "chain_blocks_per_hour",
    ]
    derived_fields = [
        "valid_shares_per_min",
        "successful_submit_lines_per_min",
        "submit_errors_per_min",
        "too_late_per_min",
        "stale_jobs_per_min",
        "duplicates_per_min",
        "submit_errors_per_ok",
        "too_late_per_ok",
        "stale_jobs_per_ok",
        "duplicates_per_ok",
    ]
    result = {}
    for field in fields:
        result[field] = {
            "before": before.get(field),
            "after": after.get(field),
            "delta": round(float(after.get(field) or 0) - float(before.get(field) or 0), 4),
            "pct_delta": pct_delta(before.get(field), after.get(field)),
        }
    for field in derived_fields:
        result[field] = {
            "before": b.get(field),
            "after": a.get(field),
            "delta": round(float(a.get(field) or 0) - float(b.get(field) or 0), 4),
            "pct_delta": pct_delta(b.get(field), a.get(field)),
        }
    return result


def render_report(payload: dict[str, Any]) -> str:
    lines = [
        "# External Optimization A/B Comparison",
        "",
        f"Generated: {payload['generated_at']}",
        f"Stack: `{payload['stack']}`",
        f"RPC primary at finish: `{payload['rpc_primary']}`",
        "",
        "## Windows",
        "",
        f"- Before: `{payload['before']['measured_start_utc']}` to `{payload['before']['measured_end_utc']}`",
        f"- After: `{payload['after']['measured_start_utc']}` to `{payload['after']['measured_end_utc']}`",
        "",
        "## Metrics",
        "",
        "| Metric | Before | After | Delta | % Delta |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    labels = {
        "local_chain_share_pct": "Local chain share %",
        "local_blocks_per_hour": "Local blocks/hour",
        "db_blocks_per_hour": "DB blocks/hour",
        "valid_shares_per_min": "Valid shares/min",
        "successful_submit_lines_per_min": "Successful submit lines/min",
        "submit_errors_per_min": "Submit errors/min",
        "too_late_per_min": "Too late/min",
        "stale_jobs_per_min": "Stale jobs/min",
        "duplicates_per_min": "Duplicates/min",
        "submit_errors_per_ok": "Submit errors per OK",
        "too_late_per_ok": "Too late per OK",
        "stale_jobs_per_ok": "Stale jobs per OK",
        "duplicates_per_ok": "Duplicates per OK",
    }
    for key, label in labels.items():
        row = payload["comparison"].get(key, {})
        pct = "" if row.get("pct_delta") is None else f"{row['pct_delta']:.3f}%"
        lines.append(f"| {label} | {row.get('before')} | {row.get('after')} | {row.get('delta')} | {pct} |")
    lines.extend(
        [
            "",
            "## Interpretation Guardrail",
            "",
            "This is a short operational A/B window. Treat it as an early signal. Local chain share is more meaningful than raw local blocks/hour when competing pools change their hash share.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-minutes", type=int, default=15, help="minutes before marker for baseline")
    parser.add_argument("--observe-minutes", type=int, default=5, help="minutes to observe after marker")
    parser.add_argument("--no-wait", action="store_true", help="measure immediately without waiting for observe window")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--write-report", action="store_true", help="write markdown report under ops/runtime")
    args = parser.parse_args()

    marker = utc_now()
    if not args.no_wait:
        time.sleep(max(1, args.observe_minutes * 60))
    end = utc_now()
    before_start = int((marker - dt.timedelta(minutes=args.baseline_minutes)).timestamp())
    before_end = int(marker.timestamp())
    after_start = int(marker.timestamp())
    after_end = int(end.timestamp())
    before = enrich(scan_window({"phase": "before", "stack": current_stack_name()}, before_start, before_end))
    after = enrich(scan_window({"phase": "after", "stack": current_stack_name()}, after_start, after_end))
    payload = {
        "generated_at": now_iso(),
        "marker_utc": iso(marker),
        "baseline_minutes": args.baseline_minutes,
        "observe_minutes": round((after_end - after_start) / 60, 3),
        "stack": current_stack_name(),
        "rpc_primary": current_rpc_primary(),
        "before": before,
        "after": after,
        "comparison": comparison(before, after),
    }
    if args.write_report:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = RUNTIME_DIR / f"external-optimization-ab-{stamp}.md"
        path.write_text(render_report(payload), encoding="utf-8")
        (RUNTIME_DIR / "latest-external-optimization-ab.txt").write_text(str(path) + "\n", encoding="utf-8")
        print(path)
    elif args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(render_report(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


#!/usr/bin/env python3
"""Bounded, low-noise summary of pool logs for operational comparisons."""

from __future__ import annotations

import argparse
import json
import re
from typing import Any

from pool_ops import POOL_CONTAINER, RUNTIME_DIR, now_iso, run


SUBMIT_RE = re.compile(r"submit from worker=", re.IGNORECASE)
SUBMIT_SUPPRESSED_RE = re.compile(r"\ssuppressed=([0-9]+)", re.IGNORECASE)


PATTERNS = {
    "submit": SUBMIT_RE,
    "valid_share": re.compile(r"valid share accepted", re.IGNORECASE),
    "block_ok": re.compile(r"Block submitted successfully|Block & Credits saved|block submission processed", re.IGNORECASE),
    "block_error": re.compile(r"Block submission too late|block submit error|submit error|submission failed", re.IGNORECASE),
    "too_late": re.compile(r"too late|overdue", re.IGNORECASE),
    "stale": re.compile(r"stale job|STALE JOB|Stale/Expired|not found in acceptedJobs", re.IGNORECASE),
    "duplicate": re.compile(r"duplicate|already known", re.IGNORECASE),
    "template_error": re.compile(r"GBT ERROR|getBlockTemplate|template fetch error|Failed to create new block template", re.IGNORECASE),
    "rpc_refused": re.compile(r"connection refused|ECONNREFUSED|rpc refused", re.IGNORECASE),
    "vardiff": re.compile(r"PUSHDIF|vardiff|set_difficulty", re.IGNORECASE),
    "auth_reject": re.compile(r"authorize.*(reject|fail|denied)", re.IGNORECASE),
}


def summarize_logs(since: str, until: str | None = None, tail: int | None = None) -> dict[str, Any]:
    command = ["docker", "logs"]
    if since:
        command.extend(["--since", since])
    if until:
        command.extend(["--until", until])
    if tail:
        command.extend(["--tail", str(tail)])
    command.append(POOL_CONTAINER)
    result = run(command, timeout=45)
    text = "\n".join(part for part in (result.stdout, result.stderr) if part)
    counts = {key: 0 for key in PATTERNS}
    lines = 0
    for line in text.splitlines():
        lines += 1
        for key, pattern in PATTERNS.items():
            match = pattern.search(line)
            if not match:
                continue
            if key == "submit":
                suppressed = SUBMIT_SUPPRESSED_RE.search(line)
                counts[key] += 1 + safe_int(suppressed.group(1), 0) if suppressed else 1
            elif key == "valid_share":
                suppressed = SUBMIT_SUPPRESSED_RE.search(line)
                counts[key] += 1 + safe_int(suppressed.group(1), 0) if suppressed else 1
            else:
                counts[key] += 1
    block_ok = counts["block_ok"]
    return {
        "generated_at": now_iso(),
        "container": POOL_CONTAINER,
        "since": since,
        "until": until,
        "tail": tail,
        "command_ok": result.ok,
        "command_error": "" if result.ok else (result.stderr or result.stdout)[-1000:],
        "line_count": lines,
        "counts": counts,
        "ratios": {
            "block_errors_per_ok": round(counts["block_error"] / max(1, block_ok), 4),
            "too_late_per_ok": round(counts["too_late"] / max(1, block_ok), 4),
            "stale_per_ok": round(counts["stale"] / max(1, block_ok), 4),
            "duplicate_per_ok": round(counts["duplicate"] / max(1, block_ok), 4),
            "valid_share_ratio": round(counts["valid_share"] / max(1, counts["submit"]), 4),
        },
    }


def safe_int(value: str | None, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="15m", help="docker logs --since value")
    parser.add_argument("--until", default=None, help="docker logs --until value")
    parser.add_argument("--tail", type=int, default=None, help="docker logs --tail value")
    parser.add_argument("--write-json", action="store_true", help="write summary JSON under ops/runtime")
    args = parser.parse_args()

    payload = summarize_logs(args.since, args.until, args.tail)
    if args.write_json:
        safe_since = str(args.since).replace(":", "").replace("/", "-")
        path = RUNTIME_DIR / f"pool-log-summary-{safe_since}-{payload['generated_at'].replace(':', '')}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
        print(path)
    else:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

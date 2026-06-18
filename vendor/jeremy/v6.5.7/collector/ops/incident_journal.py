#!/usr/bin/env python3
"""Structured incident journal for BlockDAG mining operations."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from pool_ops import LOG_DIR, RUNTIME_DIR, ensure_runtime, now_iso


INCIDENTS_FILE = Path(os.environ.get("BDAG_INCIDENTS_FILE", LOG_DIR / "incidents.jsonl"))
INCIDENT_SUMMARY_FILE = Path(os.environ.get("BDAG_INCIDENT_SUMMARY_FILE", RUNTIME_DIR / "incident-summary.json"))
MAX_SUMMARY_INCIDENTS = int(os.environ.get("BDAG_INCIDENT_SUMMARY_LIMIT", "200"))


def _pool_summary(pool: dict[str, Any]) -> dict[str, Any]:
    return {
        "submit_count": pool.get("submit_count"),
        "valid_share_count": pool.get("valid_share_count"),
        "block_submit_success_count": pool.get("block_submit_success_count"),
        "block_submit_error_count": pool.get("block_submit_error_count"),
        "stale_submit_count": pool.get("stale_submit_count"),
        "stale_job_candidate_count": pool.get("stale_job_candidate_count"),
        "duplicate_block_count": pool.get("duplicate_block_count"),
        "last_valid_share_age_seconds": pool.get("last_valid_share_age_seconds"),
        "last_block_submit_age_seconds": pool.get("last_block_submit_age_seconds"),
        "share_stall": pool.get("share_stall"),
        "job_stall": pool.get("job_stall"),
        "pool_template_frozen": pool.get("pool_template_frozen"),
        "duplicate_block_storm": pool.get("duplicate_block_storm"),
        "stale_job_candidate_storm": pool.get("stale_job_candidate_storm"),
        "block_submit_error_storm": pool.get("block_submit_error_storm"),
    }


def status_summary(status: dict[str, Any] | None) -> dict[str, Any]:
    if not status:
        return {}
    pool = status.get("pool_health") or status.get("pool") or {}
    miner_health = status.get("miner_health") or {}
    nodes = status.get("nodes") or {}
    return {
        "generated_at": status.get("generated_at"),
        "overall": status.get("overall"),
        "status_reason": status.get("status_reason"),
        "pool_endpoint": status.get("pool_endpoint"),
        "connected_miners": miner_health.get("connected_count"),
        "managed_miners": miner_health.get("managed_count"),
        "sync_health": status.get("sync_health"),
        "pool": _pool_summary(pool),
        "nodes": {
            name: {
                "child_running": info.get("child_running"),
                "latest_block": info.get("latest_block"),
                "last_import_age_seconds": info.get("last_import_age_seconds"),
                "import_count": info.get("import_count"),
                "mining_template_error_count": info.get("mining_template_error_count"),
                "mining_template_failing": info.get("mining_template_failing"),
                "critical": info.get("critical"),
                "peer_ahead_blocks": info.get("peer_ahead_blocks"),
            }
            for name, info in nodes.items()
            if isinstance(info, dict)
        },
    }


def append_incident(
    event_type: str,
    severity: str,
    component: str,
    message: str,
    details: dict[str, Any] | None = None,
    status: dict[str, Any] | None = None,
    action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_runtime()
    payload = {
        "id": f"{int(time.time())}-{os.getpid()}",
        "generated_at": now_iso(),
        "event_type": event_type,
        "severity": severity,
        "component": component,
        "message": message,
        "details": details or {},
        "status": status_summary(status),
        "action": action or {},
    }
    INCIDENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with INCIDENTS_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
    update_incident_summary(payload)
    return payload


def read_recent_incidents(limit: int = 100) -> list[dict[str, Any]]:
    if not INCIDENTS_FILE.exists():
        return []
    lines = INCIDENTS_FILE.read_text(errors="replace").splitlines()[-max(1, limit):]
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def update_incident_summary(newest: dict[str, Any] | None = None) -> dict[str, Any]:
    rows = read_recent_incidents(MAX_SUMMARY_INCIDENTS)
    counts: dict[str, int] = {}
    severity_counts: dict[str, int] = {}
    for row in rows:
        counts[str(row.get("event_type") or "unknown")] = counts.get(str(row.get("event_type") or "unknown"), 0) + 1
        severity = str(row.get("severity") or "unknown")
        severity_counts[severity] = severity_counts.get(severity, 0) + 1
    payload = {
        "generated_at": now_iso(),
        "incident_count": len(rows),
        "counts_by_type": counts,
        "counts_by_severity": severity_counts,
        "latest": newest or (rows[-1] if rows else None),
    }
    INCIDENT_SUMMARY_FILE.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return payload


def main() -> int:
    payload = update_incident_summary()
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


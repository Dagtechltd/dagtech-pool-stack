#!/usr/bin/env python3
"""Read-only paid block conversion baseline collector.

This is the Phase 0/1 evidence tool for the durable paid-block conversion plan.
It intentionally does not restart services, mutate runtime configuration, or
ask the dashboard to do expensive full-chain scans unless explicitly requested.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

from pool_ops import PROJECT_ROOT, RUNTIME_DIR, now_iso, run


DEFAULT_STATUS_URL = "http://127.0.0.1:9280/api/status"
DEFAULT_GLOBAL_URL = "http://127.0.0.1:9280/api/global"
DEFAULT_METRICS_URL = "http://127.0.0.1:9092/metrics"
REPORT_DIR = RUNTIME_DIR / "reports"
PROM_LINE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([-+0-9.eE]+)\s*$")
SOURCE_REPOS = {
    "pool_stack_docker": PROJECT_ROOT,
    "pool": Path(os.environ.get("BDAG_POOL_SOURCE", "/home/jeremy/blockdag-source/pool")),
    "collector": Path(os.environ.get("BDAG_COLLECTOR_SOURCE", "/home/jeremy/blockdag-source/collector")),
    "blockdag_corechain": Path(os.environ.get("BDAG_CORECHAIN_SOURCE", "/home/jeremy/blockdag-source/blockdag-corechain")),
}
KEEP_PREFIXES = (
    "pool_block_submit_outcomes_total",
    "pool_block_candidate_reject_job_age_seconds_",
    "pool_blocks_found_total",
    "pool_jobs_marked_stale_total",
    "pool_template_broadcasts_total",
    "pool_shares_accepted_total",
    "pool_shares_rejected_total",
    "pool_rpc_backend_node_health_mineable",
    "pool_rpc_backend_node_health_submit_ready",
    "pool_rpc_backend_node_health_template_age_seconds",
    "pool_rpc_backend_node_health_last_template_build_error_blocking",
    "pool_rpc_backend_node_health_template_invalidations_total",
    "pool_rpc_backend_selected",
    "pool_rpc_backend_healthy",
    "pool_rpc_backend_template_errors_total",
    "pool_template_conversion_stall_",
)


def fetch_json(url: str, timeout: float = 10.0) -> tuple[dict[str, Any], str]:
    try:
        req = urllib.request.Request(url, headers={"accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8")), ""
    except Exception as exc:  # noqa: BLE001 - baseline should keep going.
        return {}, str(exc)


def fetch_text(url: str, timeout: float = 10.0) -> tuple[str, str]:
    try:
        req = urllib.request.Request(url, headers={"accept": "text/plain"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace"), ""
    except Exception as exc:  # noqa: BLE001 - baseline should keep going.
        return "", str(exc)


def parse_prometheus(text: str) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = PROM_LINE_RE.match(line)
        if not match:
            continue
        name, labels, value = match.groups()
        if not name.startswith(KEEP_PREFIXES):
            continue
        try:
            metrics[name + (labels or "")] = float(value)
        except ValueError:
            continue
    return metrics


def metric_delta(first: dict[str, float], last: dict[str, float], fragment: str) -> tuple[float, dict[str, float]]:
    rows: dict[str, float] = {}
    total = 0.0
    for key, value in sorted(last.items()):
        if fragment not in key:
            continue
        delta = value - first.get(key, 0.0)
        if delta <= 0:
            continue
        rows[key] = round(delta, 6)
        total += delta
    return round(total, 6), rows


def metric_current(metrics: dict[str, float], fragment: str) -> dict[str, float]:
    return {key: value for key, value in sorted(metrics.items()) if fragment in key}


def ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)


def connected_miners(status: dict[str, Any]) -> int:
    miner_health = status.get("miner_health") if isinstance(status.get("miner_health"), dict) else {}
    return int(miner_health.get("connected_count") or miner_health.get("managed_count") or 0)


def selected_backend_state(metrics: dict[str, float]) -> dict[str, Any]:
    selected = ""
    for key, value in metrics.items():
        if key.startswith("pool_rpc_backend_selected") and value > 0:
            match = re.search(r'backend="([^"]+)"', key)
            selected = match.group(1) if match else key
            break
    def first_value(fragment: str) -> float | None:
        for key, value in metrics.items():
            if fragment in key and (not selected or f'backend="{selected}"' in key):
                return value
        return None
    return {
        "selected_backend": selected,
        "selected_metric": 1 if selected else 0,
        "healthy": first_value("pool_rpc_backend_healthy"),
        "node_mineable": first_value("pool_rpc_backend_node_health_mineable"),
        "node_submit_ready": first_value("pool_rpc_backend_node_health_submit_ready"),
        "node_template_age_seconds": first_value("pool_rpc_backend_node_health_template_age_seconds"),
        "last_template_build_error_blocking": first_value(
            "pool_rpc_backend_node_health_last_template_build_error_blocking"
        ),
    }


def summarize_metrics(first: dict[str, float], last: dict[str, float], measured_seconds: float) -> dict[str, Any]:
    accepted, accepted_rows = metric_delta(first, last, 'pool_block_submit_outcomes_total{outcome="accepted"')
    rejected, rejected_rows = metric_delta(first, last, 'pool_block_submit_outcomes_total{outcome="rejected"')
    rejected_local, rejected_local_rows = metric_delta(
        first,
        last,
        'pool_block_submit_outcomes_total{outcome="rejected-local"',
    )
    shares_accepted, shares_accepted_rows = metric_delta(first, last, "pool_shares_accepted_total")
    shares_rejected, shares_rejected_rows = metric_delta(first, last, "pool_shares_rejected_total")
    found, found_rows = metric_delta(first, last, "pool_blocks_found_total")
    local_candidate_drop = rejected_local
    total_submit_outcomes = accepted + rejected + rejected_local
    active_miners = last.get('pool_template_conversion_stall_active_miners{pool_id="0"}', 0.0)
    miner_hours = active_miners * measured_seconds / 3600.0 if active_miners > 0 and measured_seconds > 0 else 0.0
    return {
        "measured_seconds": round(measured_seconds, 3),
        "active_miners_end": active_miners,
        "miner_hours": round(miner_hours, 6),
        "network_target_candidates_delta": found,
        "accepted_submit_delta": accepted,
        "accepted_submit_per_miner_hour": round(accepted / miner_hours, 6) if miner_hours > 0 else 0.0,
        "rejected_submit_delta": rejected,
        "local_candidate_drop_delta": local_candidate_drop,
        "local_candidate_drop_ratio": ratio(local_candidate_drop, max(1.0, total_submit_outcomes)),
        "tip_overdue_delta": sum(value for key, value in rejected_rows.items() if "tip-overdue" in key),
        "share_accept_delta": shares_accepted,
        "share_reject_delta": shares_rejected,
        "share_reject_ratio": ratio(shares_rejected, shares_accepted + shares_rejected),
        "selected_backend_state": selected_backend_state(last),
        "accepted_rows": accepted_rows,
        "rejected_rows": rejected_rows,
        "rejected_local_rows": rejected_local_rows,
        "found_rows": found_rows,
        "shares_accepted_rows": shares_accepted_rows,
        "shares_rejected_rows": shares_rejected_rows,
        "template_error_counters": metric_current(last, "pool_rpc_backend_template_errors_total"),
        "template_invalidation_counters": metric_current(
            last,
            "pool_rpc_backend_node_health_template_invalidations_total",
        ),
        "candidate_reject_age_counts": metric_current(last, "pool_block_candidate_reject_job_age_seconds_count"),
        "candidate_reject_age_sums": metric_current(last, "pool_block_candidate_reject_job_age_seconds_sum"),
    }


def derive_paid_mining_state(status: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    miners = connected_miners(status)
    sync_progress = status.get("sync_progress") if isinstance(status.get("sync_progress"), dict) else {}
    backend = summary.get("selected_backend_state") if isinstance(summary.get("selected_backend_state"), dict) else {}
    accepted = float(summary.get("accepted_submit_delta") or 0)
    shares = float(summary.get("share_accept_delta") or 0)
    local_drop_ratio = float(summary.get("local_candidate_drop_ratio") or 0)
    node_mineable = backend.get("node_mineable")
    node_submit_ready = backend.get("node_submit_ready")
    reasons: list[str] = []

    if status.get("overall") == "down":
        return {"state": "stack_down", "reasons": ["dashboard overall down"]}
    if miners <= 0:
        if sync_progress.get("status") == "synced":
            return {"state": "ready_no_miners", "reasons": ["no connected miners"]}
        return {"state": "sync_only_no_miners", "reasons": ["no connected miners"]}
    if node_mineable == 0 or node_submit_ready == 0:
        reasons.append("selected backend is not mineable or submit-ready")
    if shares > 0 and accepted <= 0:
        reasons.append("fresh shares without accepted block submits in sample")
    if local_drop_ratio >= 0.05:
        reasons.append(f"local candidate drop ratio {local_drop_ratio:.3f} >= 0.05")
    if accepted > 0 and not reasons:
        return {"state": "mining_paid_ok", "reasons": ["accepted block submits observed"]}
    if accepted > 0:
        return {"state": "mining_paid_degraded", "reasons": reasons or ["accepted submits need chain confirmation"]}
    if node_mineable == 0 or node_submit_ready == 0:
        return {"state": "template_source_unready", "reasons": reasons}
    return {"state": "mining_unpaid", "reasons": reasons or ["no accepted block submits observed"]}


def repo_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False}
    head = run(["git", "-C", str(path), "rev-parse", "--short", "HEAD"], timeout=5)
    branch = run(["git", "-C", str(path), "branch", "--show-current"], timeout=5)
    status = run(["git", "-C", str(path), "status", "--short"], timeout=5)
    return {
        "path": str(path),
        "exists": True,
        "head": (head.stdout or "").strip() if head.ok else "",
        "branch": (branch.stdout or "").strip() if branch.ok else "",
        "dirty": bool((status.stdout or "").strip()) if status.ok else None,
        "status": (status.stdout or status.stderr or "").strip()[:4000],
    }


def docker_container_state() -> dict[str, Any]:
    result = run(["docker", "ps", "-a", "--format", "{{json .}}"], timeout=15)
    if not result.ok:
        return {"error": (result.stderr or result.stdout)[-2000:]}
    rows: dict[str, Any] = {}
    for line in result.stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = item.get("Names") or item.get("Name") or item.get("Container")
        if name and str(name).startswith(("bdag-", "asic-pool", "postgres")):
            rows[str(name)] = item
    return rows


def collect_baseline(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    status, status_error = fetch_json(args.status_url, timeout=args.timeout)
    metrics_url = args.metrics_url or DEFAULT_METRICS_URL
    metrics_text, metrics_error = fetch_text(metrics_url, timeout=args.timeout)
    first_metrics = parse_prometheus(metrics_text)
    if args.duration > 0:
        time.sleep(args.duration)
        status, status_error = fetch_json(args.status_url, timeout=args.timeout)
        metrics_text, metrics_error = fetch_text(metrics_url, timeout=args.timeout)
    last_metrics = parse_prometheus(metrics_text)
    measured = time.monotonic() - started
    global_state: dict[str, Any] = {}
    global_error = "skipped"
    if args.include_global:
        global_state, global_error = fetch_json(args.global_url, timeout=max(args.timeout, 15.0))
    summary = summarize_metrics(first_metrics, last_metrics, measured)
    paid_state = derive_paid_mining_state(status, summary)
    return {
        "metadata": {
            "document_type": "paid_block_conversion_baseline",
            "generated_at": now_iso(),
            "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "status_url": args.status_url,
            "metrics_url": metrics_url,
            "duration_seconds_requested": args.duration,
            "resource_note": "read-only HTTP, Prometheus, docker ps, and git status sampling; no service restart or runtime mutation",
        },
        "paid_mining_state": paid_state,
        "summary": summary,
        "status": status,
        "status_error": status_error,
        "global": global_state,
        "global_error": global_error,
        "metrics_error": metrics_error,
        "source_repos": {name: repo_state(path) for name, path in SOURCE_REPOS.items()},
        "containers": docker_container_state(),
        "quarantine": {
            "corechain_staged_patch": "/home/jeremy/blockdag-source/quarantine/blockdag-corechain-staged-paid-conversion-regression-20260530.patch",
            "corechain_staged_stat": "/home/jeremy/blockdag-source/quarantine/blockdag-corechain-staged-paid-conversion-regression-20260530.stat",
        },
    }


def html_escape(value: Any) -> str:
    return (
        str(value if value is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_html(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    paid_state = payload.get("paid_mining_state") or {}
    rows = [
        ("Paid Mining State", paid_state.get("state")),
        ("State Reasons", "; ".join(paid_state.get("reasons") or [])),
        ("Measured Seconds", summary.get("measured_seconds")),
        ("Active Miners", summary.get("active_miners_end")),
        ("Accepted Submit Delta", summary.get("accepted_submit_delta")),
        ("Accepted Submit / Miner Hour", summary.get("accepted_submit_per_miner_hour")),
        ("Network Target Candidates", summary.get("network_target_candidates_delta")),
        ("Local Candidate Drop Ratio", summary.get("local_candidate_drop_ratio")),
        ("Tip Overdue Delta", summary.get("tip_overdue_delta")),
        ("Share Reject Ratio", summary.get("share_reject_ratio")),
    ]
    row_html = "\n".join(
        f"<tr><th>{html_escape(label)}</th><td><code>{html_escape(value)}</code></td></tr>"
        for label, value in rows
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Paid Block Conversion Baseline</title>
  <style>
    body {{ margin:0; background:#0d1117; color:#e6edf3; font:14px/1.55 system-ui,sans-serif; }}
    main {{ max-width:1180px; margin:0 auto; padding:28px 20px 56px; }}
    table {{ width:100%; border-collapse:collapse; background:#161b22; }}
    th,td {{ border:1px solid #30363d; padding:9px 10px; text-align:left; vertical-align:top; }}
    th {{ background:#1f2937; }}
    code,pre {{ background:#090d13; border:1px solid #30363d; color:#d8f7ff; border-radius:6px; }}
    code {{ padding:1px 5px; }}
    pre {{ padding:12px; overflow:auto; }}
    .muted {{ color:#a6b3c2; }}
  </style>
  <script type="application/json" id="agent-metadata">{json.dumps(payload.get("metadata", {}), sort_keys=True)}</script>
</head>
<body>
<main>
  <h1>Paid Block Conversion Baseline</h1>
  <p class="muted">Read-only baseline for the durable paid-block conversion plan. Shares are connectivity telemetry; accepted submits and confirmed chain data are paid-mining evidence.</p>
  <table>{row_html}</table>
  <h2>Full Payload</h2>
  <pre>{html_escape(json.dumps(payload, indent=2, sort_keys=True, default=str))}</pre>
</main>
</body>
</html>
"""


def write_reports(payload: dict[str, Any]) -> dict[str, str]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = REPORT_DIR / f"paid-conversion-baseline-{stamp}.json"
    html_path = REPORT_DIR / f"paid-conversion-baseline-{stamp}.html"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    html_path.write_text(render_html(payload), encoding="utf-8")
    return {"json": str(json_path), "html": str(html_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status-url", default=DEFAULT_STATUS_URL)
    parser.add_argument("--metrics-url", default=DEFAULT_METRICS_URL)
    parser.add_argument("--global-url", default=DEFAULT_GLOBAL_URL)
    parser.add_argument("--duration", type=float, default=0.0, help="optional second sample delay for counter deltas")
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--include-global", action="store_true", help="also fetch /api/global; may be heavier")
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args()

    payload = collect_baseline(args)
    if args.write_report:
        payload["reports"] = write_reports(payload)
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

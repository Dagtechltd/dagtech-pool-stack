#!/usr/bin/env python3
"""Measure active miner-lane block conversion from direct pool metrics."""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from pool_ops import RUNTIME_DIR, now_iso


DEFAULT_METRICS_URL = "http://127.0.0.1:9092/metrics"
REPORT_DIR = RUNTIME_DIR / "reports"
PROM_LINE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([-+0-9.eE]+)\s*$")
KEEP_PREFIXES = (
    "pool_block_submit_outcomes_total",
    "pool_rpc_backend_submit_total",
    "pool_duplicate_block_candidates_rejected_local_total",
    "pool_stale_block_candidates_rejected_local_total",
    "pool_stale_block_candidates_submitted_total",
    "pool_template_conversion_stall_active_miners",
    "pool_template_conversion_stall_failure_ratio",
    "pool_template_conversion_stall_window_candidates",
    "pool_shares_accepted_total",
    "pool_shares_rejected_total",
)


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


def fetch_metrics(url: str, timeout: float) -> dict[str, float]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return parse_prometheus(response.read().decode("utf-8", "replace"))


def positive_delta(first: dict[str, float], last: dict[str, float], fragment: str) -> tuple[float, dict[str, float]]:
    rows: dict[str, float] = {}
    total = 0.0
    for key, value in last.items():
        if fragment not in key:
            continue
        delta = value - first.get(key, 0.0)
        if delta <= 0:
            continue
        rows[key] = round(delta, 6)
        total += delta
    return round(total, 6), rows


def active_miners(metrics: dict[str, float]) -> float:
    return metrics.get('pool_template_conversion_stall_active_miners{pool_id="0"}', 0.0) or 0.0


def summarize_delta(first: dict[str, float], last: dict[str, float], measured_seconds: float) -> dict[str, Any]:
    accepted, accepted_rows = positive_delta(first, last, 'pool_block_submit_outcomes_total{outcome="accepted"')
    rejected, rejected_rows = positive_delta(first, last, 'pool_block_submit_outcomes_total{outcome="rejected"')
    rejected_local, rejected_local_rows = positive_delta(
        first,
        last,
        'pool_block_submit_outcomes_total{outcome="rejected-local"',
    )
    shares_accepted, shares_accepted_rows = positive_delta(first, last, "pool_shares_accepted_total")
    shares_rejected, shares_rejected_rows = positive_delta(first, last, "pool_shares_rejected_total")
    active = active_miners(last)
    miner_hours = active * measured_seconds / 3600.0 if active > 0 else 0.0
    return {
        "generated_at": now_iso(),
        "measured_seconds": round(measured_seconds, 3),
        "active_miners_end": active,
        "miner_hours": round(miner_hours, 6),
        "accepted_blocks": accepted,
        "accepted_blocks_per_hour": round(accepted * 3600.0 / measured_seconds, 3) if measured_seconds > 0 else 0.0,
        "accepted_blocks_per_miner_hour": round(accepted / miner_hours, 3) if miner_hours > 0 else 0.0,
        "rejected_per_accepted": round(rejected / accepted, 6) if accepted > 0 else 0.0,
        "rejected_local_per_accepted": round(rejected_local / accepted, 6) if accepted > 0 else 0.0,
        "shares_accepted": shares_accepted,
        "shares_rejected": shares_rejected,
        "share_reject_ratio": round(shares_rejected / max(1.0, shares_accepted + shares_rejected), 6),
        "template_conversion_failure_ratio_end": last.get('pool_template_conversion_stall_failure_ratio{pool_id="0"}'),
        "accepted_rows": accepted_rows,
        "rejected_rows": rejected_rows,
        "rejected_local_rows": rejected_local_rows,
        "shares_accepted_rows": shares_accepted_rows,
        "shares_rejected_rows": shares_rejected_rows,
        "window_candidates_end": {
            key: value
            for key, value in sorted(last.items())
            if key.startswith("pool_template_conversion_stall_window_candidates")
        },
    }


def html_escape(value: Any) -> str:
    text = str(value if value is not None else "")
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_html(summary: dict[str, Any], metadata: dict[str, Any]) -> str:
    rows = [
        ("Measured Seconds", summary.get("measured_seconds")),
        ("Active Miners", summary.get("active_miners_end")),
        ("Accepted Blocks", summary.get("accepted_blocks")),
        ("Accepted Blocks / Hour", summary.get("accepted_blocks_per_hour")),
        ("Accepted Blocks / Miner Hour", summary.get("accepted_blocks_per_miner_hour")),
        ("Rejected / Accepted", summary.get("rejected_per_accepted")),
        ("Local Rejected / Accepted", summary.get("rejected_local_per_accepted")),
        ("Share Reject Ratio", summary.get("share_reject_ratio")),
        ("Template Conversion Failure %", summary.get("template_conversion_failure_ratio_end")),
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
  <title>BlockDAG Lane Efficiency Snapshot</title>
  <style>
    body {{ margin:0; background:#101318; color:#eef3f8; font:14px/1.55 system-ui,sans-serif; }}
    main {{ max-width:1100px; margin:0 auto; padding:28px 20px 52px; }}
    h1 {{ margin:0 0 8px; }}
    table {{ border-collapse:collapse; width:100%; background:#171d25; margin:16px 0; }}
    th,td {{ border:1px solid #334052; padding:9px 10px; text-align:left; vertical-align:top; }}
    th {{ background:#202837; }}
    code,pre {{ background:#0a0e13; border:1px solid #334052; border-radius:6px; color:#d7f5ff; }}
    code {{ padding:1px 5px; }}
    pre {{ padding:12px; overflow:auto; }}
    .muted {{ color:#aab6c5; }}
  </style>
  <script type="application/json" id="agent-metadata">{json.dumps({"metadata": metadata, "summary": summary}, sort_keys=True)}</script>
</head>
<body>
<main>
  <h1>BlockDAG Lane Efficiency Snapshot</h1>
  <p class="muted">Direct pool Prometheus counter delta. This avoids dashboard status API latency and avoids fixed-log-window miner plot bias.</p>
  <table>{row_html}</table>
  <h2>Outcome Deltas</h2>
  <pre>{html_escape(json.dumps(summary, indent=2, sort_keys=True))}</pre>
</main>
</body>
</html>
"""


def run_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    first = fetch_metrics(args.metrics_url, args.timeout)
    time.sleep(max(0.0, args.duration))
    last = fetch_metrics(args.metrics_url, args.timeout)
    measured = time.monotonic() - started
    summary = summarize_delta(first, last, measured)
    summary["metrics_url"] = args.metrics_url
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics-url", default=DEFAULT_METRICS_URL)
    parser.add_argument("--duration", type=float, default=90.0)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args()

    summary = run_snapshot(args)
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.write_report:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        json_path = REPORT_DIR / f"lane-efficiency-snapshot-{stamp}.json"
        html_path = REPORT_DIR / f"lane-efficiency-snapshot-{stamp}.html"
        metadata = {
            "document_type": "bdag_lane_efficiency_snapshot",
            "metrics_url": args.metrics_url,
            "duration_seconds": args.duration,
            "resource_note": "read-only direct Prometheus scrape; no dashboard status scan and no service restart",
        }
        json_path.write_text(json.dumps({"metadata": metadata, "summary": summary}, indent=2, sort_keys=True), encoding="utf-8")
        html_path.write_text(render_html(summary, metadata), encoding="utf-8")
        print(str(html_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

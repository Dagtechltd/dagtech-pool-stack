#!/usr/bin/env python3
"""Collect low-overhead optimization baseline samples for the BlockDAG stack."""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path
from typing import Any

from pool_ops import RUNTIME_DIR, collect_status_cached, host_runtime_profile, now_iso, seconds_since_epoch


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return round(ordered[index], 3)


def number(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_status_url(url: str, timeout: float) -> tuple[dict[str, Any], float]:
    started = time.monotonic()
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else {}, round((time.monotonic() - started) * 1000, 3)


def collect_status_sample(status_url: str | None = None, timeout: float = 8.0) -> dict[str, Any]:
    started = time.monotonic()
    dashboard_latency_ms = None
    if status_url:
        status, dashboard_latency_ms = fetch_status_url(status_url, timeout)
        source = status_url
    else:
        status = collect_status_cached(include_logs=False)
        source = "local-collector"
    collection_ms = round((time.monotonic() - started) * 1000, 3)
    return flatten_status_sample(status, source, collection_ms, dashboard_latency_ms)


def flatten_status_sample(
    status: dict[str, Any],
    source: str,
    collection_ms: float,
    dashboard_latency_ms: float | None = None,
) -> dict[str, Any]:
    sync = status.get("sync_progress") if isinstance(status.get("sync_progress"), dict) else {}
    host = status.get("host_pressure") if isinstance(status.get("host_pressure"), dict) else {}
    adaptive = status.get("adaptive_concurrency") if isinstance(status.get("adaptive_concurrency"), dict) else {}
    miner = status.get("miner_health") if isinstance(status.get("miner_health"), dict) else {}
    nodes = sync.get("nodes") if isinstance(sync.get("nodes"), dict) else {}
    chain_latencies = [
        value
        for value in (number(item.get("chain_rpc_latency_ms")) for item in nodes.values() if isinstance(item, dict))
        if value is not None
    ]
    current_block = number(sync.get("current_block"))
    highest_block = number(sync.get("highest_block"))
    adaptive_workers = adaptive.get("workers") if isinstance(adaptive.get("workers"), dict) else {}
    return {
        "sampled_at": now_iso(),
        "sampled_epoch": seconds_since_epoch(),
        "source": source,
        "collection_ms": collection_ms,
        "dashboard_latency_ms": dashboard_latency_ms,
        "overall": status.get("overall"),
        "mode": status.get("mode"),
        "can_mine": status.get("can_mine"),
        "sync_status": sync.get("status"),
        "current_block": int(current_block) if current_block is not None else None,
        "highest_block": int(highest_block) if highest_block is not None else None,
        "remaining_blocks": int(number(sync.get("remaining_blocks")) or 0) if sync.get("remaining_blocks") is not None else None,
        "chain_rpc_latency_ms_max": max(chain_latencies) if chain_latencies else None,
        "chain_rpc_latency_ms_avg": round(sum(chain_latencies) / len(chain_latencies), 3) if chain_latencies else None,
        "connected_miners": int(number(miner.get("connected_count")) or 0),
        "managed_miners": int(number(miner.get("managed_count")) or 0),
        "iowait_percent": number(host.get("iowait_percent")),
        "cpu_busy_percent": number(host.get("cpu_busy_percent")),
        "io_some_avg10": number(host.get("io_some_avg10")),
        "cpu_some_avg10": number(host.get("cpu_some_avg10")),
        "memory_some_avg10": number(host.get("memory_some_avg10")),
        "adaptive_pressure_level": adaptive.get("pressure_level"),
        "adaptive_workers": adaptive_workers,
        "host_profile": status.get("host_profile") or adaptive.get("host_profile") or host_runtime_profile(),
    }


def summarize_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    if not samples:
        return {
            "sample_count": 0,
            "status": "empty",
            "generated_at": now_iso(),
            "host_profile": host_runtime_profile(),
        }
    first = samples[0]
    last = samples[-1]
    elapsed = max(0, float(last.get("sampled_epoch") or 0) - float(first.get("sampled_epoch") or 0))
    first_block = number(first.get("current_block"))
    last_block = number(last.get("current_block"))
    block_delta = None
    blocks_per_second = None
    if first_block is not None and last_block is not None:
        block_delta = int(last_block - first_block)
        if elapsed > 0:
            blocks_per_second = round(block_delta / elapsed, 4)

    def values(field: str) -> list[float]:
        return [value for value in (number(sample.get(field)) for sample in samples) if value is not None]

    worker_ranges: dict[str, dict[str, int]] = {}
    for sample in samples:
        workers = sample.get("adaptive_workers") if isinstance(sample.get("adaptive_workers"), dict) else {}
        for key, raw in workers.items():
            value = int(number(raw) or 0)
            if value <= 0:
                continue
            row = worker_ranges.setdefault(key, {"min": value, "max": value})
            row["min"] = min(row["min"], value)
            row["max"] = max(row["max"], value)

    return {
        "status": "ok",
        "generated_at": now_iso(),
        "sample_count": len(samples),
        "first_sample_at": first.get("sampled_at"),
        "last_sample_at": last.get("sampled_at"),
        "elapsed_seconds": elapsed,
        "source": last.get("source"),
        "host_profile": last.get("host_profile") or host_runtime_profile(),
        "overall_values": sorted({str(sample.get("overall")) for sample in samples if sample.get("overall")}),
        "mode_values": sorted({str(sample.get("mode")) for sample in samples if sample.get("mode")}),
        "sync_status_values": sorted({str(sample.get("sync_status")) for sample in samples if sample.get("sync_status")}),
        "block_delta": block_delta,
        "blocks_per_second": blocks_per_second,
        "current_block_first": first.get("current_block"),
        "current_block_last": last.get("current_block"),
        "remaining_blocks_last": last.get("remaining_blocks"),
        "connected_miners_max": max(values("connected_miners") or [0]),
        "managed_miners_max": max(values("managed_miners") or [0]),
        "collection_ms_p95": percentile(values("collection_ms"), 95),
        "dashboard_latency_ms_p95": percentile(values("dashboard_latency_ms"), 95),
        "chain_rpc_latency_ms_p95": percentile(values("chain_rpc_latency_ms_max"), 95),
        "iowait_percent_max": percentile(values("iowait_percent"), 100),
        "io_some_avg10_max": percentile(values("io_some_avg10"), 100),
        "cpu_some_avg10_max": percentile(values("cpu_some_avg10"), 100),
        "adaptive_worker_ranges": worker_ranges,
    }


def html_escape(value: Any) -> str:
    text = str(value if value is not None else "")
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_html_report(summary: dict[str, Any], samples: list[dict[str, Any]]) -> str:
    rows = [
        ("Samples", summary.get("sample_count")),
        ("Elapsed seconds", summary.get("elapsed_seconds")),
        ("Source", summary.get("source")),
        ("Modes", ", ".join(summary.get("mode_values") or [])),
        ("Sync statuses", ", ".join(summary.get("sync_status_values") or [])),
        ("Block delta", summary.get("block_delta")),
        ("Blocks/sec", summary.get("blocks_per_second")),
        ("Collection p95 ms", summary.get("collection_ms_p95")),
        ("Dashboard p95 ms", summary.get("dashboard_latency_ms_p95")),
        ("Chain RPC p95 ms", summary.get("chain_rpc_latency_ms_p95")),
        ("I/O wait max %", summary.get("iowait_percent_max")),
        ("IO PSI avg10 max", summary.get("io_some_avg10_max")),
        ("CPU PSI avg10 max", summary.get("cpu_some_avg10_max")),
    ]
    metric_rows = "\n".join(
        f"<tr><th>{html_escape(label)}</th><td>{html_escape(value)}</td></tr>"
        for label, value in rows
    )
    worker_rows = "\n".join(
        f"<tr><td>{html_escape(kind)}</td><td>{limits['min']}</td><td>{limits['max']}</td></tr>"
        for kind, limits in sorted((summary.get("adaptive_worker_ranges") or {}).items())
    )
    last_samples = samples[-12:]
    sample_rows = "\n".join(
        "<tr>"
        f"<td>{html_escape(sample.get('sampled_at'))}</td>"
        f"<td>{html_escape(sample.get('overall'))}</td>"
        f"<td>{html_escape(sample.get('mode'))}</td>"
        f"<td>{html_escape(sample.get('sync_status'))}</td>"
        f"<td>{html_escape(sample.get('current_block'))}</td>"
        f"<td>{html_escape(sample.get('remaining_blocks'))}</td>"
        f"<td>{html_escape(sample.get('chain_rpc_latency_ms_max'))}</td>"
        f"<td>{html_escape(sample.get('iowait_percent'))}</td>"
        "</tr>"
        for sample in last_samples
    )
    host_profile = summary.get("host_profile") if isinstance(summary.get("host_profile"), dict) else {}
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BlockDAG Optimization Measurement</title>
  <style>body{{font:14px/1.5 system-ui,sans-serif;max-width:1120px;margin:32px auto;padding:0 20px;background:#0d1117;color:#eef3f8}}table{{width:100%;border-collapse:collapse;margin:16px 0}}td,th{{border:1px solid #303b4d;padding:8px;text-align:left}}th{{background:#1d2633}}code{{background:#090d13;border:1px solid #303b4d;border-radius:5px;padding:1px 5px}}</style>
</head>
<body>
  <h1>BlockDAG Optimization Measurement</h1>
  <p>Generated: <code>{html_escape(summary.get('generated_at'))}</code></p>
  <p>Host profile: <code>{html_escape(host_profile.get('profile'))}</code>, OS <code>{html_escape(host_profile.get('os'))}</code>, arch <code>{html_escape(host_profile.get('arch'))}</code>, CPU <code>{html_escape(host_profile.get('cpu_count'))}</code>, memory GiB <code>{html_escape(host_profile.get('memory_gib'))}</code></p>
  <h2>Summary</h2>
  <table>{metric_rows}</table>
  <h2>Adaptive Worker Ranges</h2>
  <table><tr><th>Kind</th><th>Min</th><th>Max</th></tr>{worker_rows}</table>
  <h2>Recent Samples</h2>
  <table><tr><th>Time</th><th>Overall</th><th>Mode</th><th>Sync</th><th>Block</th><th>Remaining</th><th>RPC ms</th><th>IO wait %</th></tr>{sample_rows}</table>
</body>
</html>
"""


def run_measurement(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    label = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in args.label.strip()) or "measurement"
    jsonl_path = output_dir / f"{label}-{stamp}.jsonl"
    samples: list[dict[str, Any]] = []
    deadline = time.monotonic() + max(0.0, args.duration_seconds)
    while True:
        sample = collect_status_sample(args.status_url, timeout=args.timeout_seconds)
        samples.append(sample)
        with jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(sample, sort_keys=True) + "\n")
        if time.monotonic() >= deadline or args.duration_seconds <= 0:
            break
        sleep_for = min(max(0.1, args.interval_seconds), max(0.0, deadline - time.monotonic()))
        if sleep_for > 0:
            time.sleep(sleep_for)

    summary = summarize_samples(samples)
    summary["label"] = label
    summary["jsonl_path"] = str(jsonl_path)
    summary_path = output_dir / f"{label}-{stamp}.summary.json"
    html_path = output_dir / f"{label}-{stamp}.html"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    html_path.write_text(render_html_report(summary, samples), encoding="utf-8")
    latest_path = output_dir / "latest-optimization-measurement.txt"
    latest_path.write_text(str(html_path) + "\n", encoding="utf-8")
    return {**summary, "summary_path": str(summary_path), "html_path": str(html_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=float, default=60.0)
    parser.add_argument("--interval-seconds", type=float, default=10.0)
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    parser.add_argument("--status-url", help="optional dashboard /api/status URL to measure HTTP latency")
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--output-dir", default=str(RUNTIME_DIR / "measurements"))
    parser.add_argument("--json", action="store_true", help="print JSON summary")
    args = parser.parse_args()
    result = run_measurement(args)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(result["html_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

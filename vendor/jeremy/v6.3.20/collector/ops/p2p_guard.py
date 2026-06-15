#!/usr/bin/env python3
"""Low-overhead P2P and network health guard for BlockDAG mining."""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from incident_journal import append_incident
from pool_ops import (
    LOG_DIR,
    NODES,
    RUNTIME_DIR,
    collect_status_cached,
    container_peer_ips,
    ensure_runtime,
    is_lan_ipv4,
    now_iso,
    read_jsonl_file,
    run,
    seconds_since_epoch,
    write_jsonl_file,
)
STATE_FILE = Path(os.environ.get("BDAG_P2P_GUARD_STATE_FILE", RUNTIME_DIR / "p2p-health-state.json"))
HISTORY_FILE = Path(os.environ.get("BDAG_P2P_GUARD_HISTORY_FILE", RUNTIME_DIR / "p2p-health-history.jsonl"))
MARKER_DIR = Path(os.environ.get("BDAG_P2P_GUARD_MARKER_DIR", RUNTIME_DIR))
LOG_FILE = Path(os.environ.get("BDAG_P2P_GUARD_LOG_FILE", LOG_DIR / "p2p-guard.log"))

DEFAULT_INTERVAL_SECONDS = int(os.environ.get("BDAG_P2P_GUARD_INTERVAL", "300"))
HISTORY_LIMIT = int(os.environ.get("BDAG_P2P_GUARD_HISTORY_LIMIT", "10000"))
NATIVE_TIMEOUT = float(os.environ.get("BDAG_P2P_GUARD_NATIVE_TIMEOUT", "2.0"))
PING_TIMEOUT = int(os.environ.get("BDAG_P2P_GUARD_PING_TIMEOUT", "1"))
MAX_PEER_PINGS = int(os.environ.get("BDAG_P2P_GUARD_MAX_PEER_PINGS", "6"))
MAX_MINER_PINGS = int(os.environ.get("BDAG_P2P_GUARD_MAX_MINER_PINGS", "8"))
MIN_NATIVE_PEERS = int(os.environ.get("BDAG_P2P_GUARD_MIN_NATIVE_PEERS", "4"))
ACTIVE_NODE_WARN_SCORE = float(os.environ.get("BDAG_P2P_GUARD_ACTIVE_WARN_SCORE", "80"))
LAN_GATEWAY_WARN_MS = float(os.environ.get("BDAG_P2P_GUARD_GATEWAY_WARN_MS", "20"))
INCIDENT_COOLDOWN_SECONDS = int(os.environ.get("BDAG_P2P_GUARD_INCIDENT_COOLDOWN", "600"))

NODE_METRIC_PORTS = {
    "bdag-miner-node-1": int(os.environ.get("BDAG_NODE1_METRICS_PORT", "6061")),
}

NATIVE_METRICS = {
    "Blockdag_mainheight",
    "Blockdag_mainlayer",
    "Blockdag_mainorder",
    "Blockdag_tips_total",
    "Blockdag_unsequenced",
    "chain_head_block",
    "p2p_peers",
    "p2p_peers_",
    "p2p_peers_inbound",
    "p2p_peers_outbound",
    "p2p_dials_",
    "p2p_dials_error_connection",
    "p2p_dials_error_known",
    "p2p_dials_error_saturated",
    "p2p_dials_error_useless",
    "p2p_dials_success",
    "p2p_ingress_",
    "p2p_egress_",
    "blockchain_resyncTimeouts",
}

METRIC_RE = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)(?:\s+|\{[^}]*\}\s+)([-+0-9.eE]+)$")
PING_TIME_RE = re.compile(r"time=([0-9.]+)\s*ms")


def log(message: str) -> None:
    ensure_runtime()
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now_iso()}] {message}\n")


def read_json_file(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json_file(path: Path, payload: Any, mode: int = 0o644) -> None:
    ensure_runtime()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    os.chmod(tmp, mode)
    tmp.replace(path)


def parse_prometheus_metrics(text: str) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = METRIC_RE.match(line)
        if not match:
            continue
        name, raw = match.groups()
        if name not in NATIVE_METRICS:
            continue
        try:
            metrics[name] = float(raw)
        except ValueError:
            continue
    return metrics


def fetch_native_metrics(port: int) -> tuple[dict[str, float], str]:
    url = f"http://127.0.0.1:{port}/debug/metrics/prometheus"
    try:
        with urllib.request.urlopen(url, timeout=NATIVE_TIMEOUT) as response:
            text = response.read().decode("utf-8", errors="replace")
        return parse_prometheus_metrics(text), ""
    except (OSError, urllib.error.URLError) as exc:
        return {}, str(exc)


def default_route() -> dict[str, Any]:
    result = run(["ip", "route", "show", "default"], timeout=3)
    line = result.stdout.splitlines()[0] if result.stdout.splitlines() else ""
    parts = line.split()
    payload: dict[str, Any] = {"raw": line, "gateway": "", "interface": "", "ok": result.ok}
    if "via" in parts:
        payload["gateway"] = parts[parts.index("via") + 1]
    if "dev" in parts:
        payload["interface"] = parts[parts.index("dev") + 1]
    iface = str(payload.get("interface") or "")
    payload["mining_interface_ok"] = bool(iface.startswith(("en", "eth")) or iface == os.environ.get("BDAG_MINING_IFACE"))
    payload["uses_wifi"] = iface.startswith(("wl", "wifi"))
    payload["uses_zerotier"] = iface.startswith("zt")
    return payload


def iface_stats(iface: str) -> dict[str, Any]:
    if not iface:
        return {}
    base = Path("/sys/class/net") / iface
    stats: dict[str, Any] = {"interface": iface}
    for name in ("operstate", "mtu", "speed"):
        try:
            stats[name] = (base / name).read_text(encoding="utf-8").strip()
        except OSError:
            stats[name] = None
    for name in ("rx_errors", "tx_errors", "rx_dropped", "tx_dropped", "rx_packets", "tx_packets", "rx_bytes", "tx_bytes"):
        try:
            stats[name] = int((base / "statistics" / name).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            stats[name] = None
    return stats


def counter_delta(current: dict[str, Any], previous: dict[str, Any] | None, key: str) -> int | None:
    if not previous:
        return None
    try:
        now_value = int(current.get(key))
        before_value = int(previous.get(key))
    except (TypeError, ValueError):
        return None
    if now_value < before_value:
        return None
    return now_value - before_value


def ping_once(ip: str) -> dict[str, Any]:
    started = time.time()
    result = run(["ping", "-n", "-c", "1", "-W", str(PING_TIMEOUT), ip], timeout=PING_TIMEOUT + 2)
    elapsed_ms = round((time.time() - started) * 1000, 3)
    match = PING_TIME_RE.search(result.stdout)
    rtt_ms = float(match.group(1)) if match else None
    return {
        "ip": ip,
        "up": bool(result.ok and rtt_ms is not None),
        "rtt_ms": rtt_ms,
        "elapsed_ms": elapsed_ms,
        "error": "" if result.ok else (result.stderr or result.stdout).strip()[-200:],
    }


def ping_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    good = [float(item["rtt_ms"]) for item in results if item.get("rtt_ms") is not None]
    return {
        "count": len(results),
        "up_count": len(good),
        "down_count": len(results) - len(good),
        "avg_rtt_ms": round(statistics.fmean(good), 3) if good else None,
        "max_rtt_ms": round(max(good), 3) if good else None,
    }


def miner_ping_targets(status: dict[str, Any]) -> list[dict[str, str]]:
    rows = ((status.get("miner_health") or {}).get("miners") or [])
    targets: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        ip = str(row.get("ip") or "")
        if not is_lan_ipv4(ip) or ip in seen:
            continue
        seen.add(ip)
        targets.append({"ip": ip, "miner": str(row.get("display_name") or ip)})
    return targets[:MAX_MINER_PINGS]


def public_peer_sources() -> dict[str, list[str]]:
    sources: dict[str, list[str]] = {}
    for node in NODES:
        try:
            sources[node] = container_peer_ips(node)
        except Exception as exc:  # noqa: BLE001 - one failed docker exec should not kill the guard.
            log(f"peer source read failed node={node}: {exc}")
            sources[node] = []
    return sources


def metric_delta(name: str, current: dict[str, float], previous: dict[str, Any] | None) -> float | None:
    if not previous:
        return None
    previous_metrics = previous.get("native_metrics") if isinstance(previous.get("native_metrics"), dict) else {}
    try:
        before = float(previous_metrics.get(name))
        now_value = float(current.get(name))
    except (TypeError, ValueError):
        return None
    if now_value < before:
        return None
    return round(now_value - before, 3)


def node_p2p_score(node: str, status: dict[str, Any], native: dict[str, float], peer_count: int) -> tuple[float, list[str]]:
    info = ((status.get("nodes") or {}).get(node) or {})
    score = 100.0
    reasons: list[str] = []

    if not info.get("child_running"):
        score -= 100
        reasons.append("child-not-running")
    if info.get("critical"):
        score -= 80
        reasons.append("critical-log")

    template_errors = int(
        info.get("mining_template_hard_error_count")
        if info.get("mining_template_hard_error_count") is not None
        else info.get("mining_template_error_count") or 0
    )
    if template_errors:
        score -= min(50, template_errors * 8)
        reasons.append(f"template-errors-{template_errors}")
    if info.get("mining_template_failing"):
        score -= 35
        reasons.append("template-failing")

    raw_import_age = info.get("last_import_age_seconds")
    import_age: int | None
    try:
        import_age = int(raw_import_age) if raw_import_age is not None else None
    except (TypeError, ValueError):
        import_age = None
    if import_age is None:
        score -= 10
        reasons.append("import-age-unknown")
    elif import_age > 180:
        score -= 45
        reasons.append(f"import-stale-{import_age}s")
    elif import_age > 90:
        score -= 20
        reasons.append(f"import-slow-{import_age}s")

    peer_ahead = int(info.get("peer_ahead_blocks") or 0)
    if peer_ahead:
        score -= min(50, peer_ahead * 3)
        reasons.append(f"peer-ahead-{peer_ahead}")

    native_peers = int(native.get("p2p_peers_", native.get("p2p_peers", 0)) or 0)
    effective_peers = max(native_peers, peer_count)
    if effective_peers < MIN_NATIVE_PEERS:
        score -= 25
        reasons.append(f"low-peer-count-{effective_peers}")

    p2p_errors = int(info.get("p2p_stream_errors") or 0)
    if p2p_errors:
        healthy_context = bool(
            effective_peers >= MIN_NATIVE_PEERS
            and import_age is not None
            and import_age <= 30
            and peer_ahead == 0
            and not template_errors
            and not info.get("mining_template_failing")
        )
        if healthy_context:
            score -= min(12, p2p_errors * 0.5)
            reasons.append(f"p2p-stream-errors-soft-{p2p_errors}")
        else:
            score -= min(40, p2p_errors * 2)
            reasons.append(f"p2p-stream-errors-{p2p_errors}")

    if score < 0:
        score = 0.0
    return round(score, 3), reasons


def pool_quality(status: dict[str, Any]) -> dict[str, Any]:
    pool = status.get("pool_health") or status.get("pool") or {}
    submits = int(pool.get("submit_count") or 0)
    valid = int(pool.get("valid_share_count") or 0)
    ok = int(pool.get("block_submit_success_count") or 0)
    errors = int(pool.get("block_submit_error_count") or 0)
    stale_jobs = int(pool.get("stale_job_candidate_count") or 0)
    overdue = int(pool.get("tip_overdue_count") or 0)
    return {
        "submits": submits,
        "valid_shares": valid,
        "block_submit_success": ok,
        "block_submit_errors": errors,
        "stale_job_candidates": stale_jobs,
        "tip_overdue": overdue,
        "valid_share_ratio": round(valid / max(1, submits), 4),
        "block_error_ratio": round(errors / max(1, ok), 4),
        "stale_job_ratio": round(stale_jobs / max(1, ok), 4),
        "tip_overdue_ratio": round(overdue / max(1, ok), 4),
        "last_valid_share_age_seconds": pool.get("last_valid_share_age_seconds"),
        "last_block_submit_age_seconds": pool.get("last_block_submit_age_seconds"),
    }


def build_snapshot(previous: dict[str, Any] | None = None) -> dict[str, Any]:
    ensure_runtime()
    status = collect_status_cached(include_logs=True)
    route = default_route()
    iface = str(route.get("interface") or "")
    current_iface_stats = iface_stats(iface)
    previous_iface_stats = previous.get("network", {}).get("interface_stats", {}) if previous else {}
    current_iface_stats["rx_errors_delta"] = counter_delta(current_iface_stats, previous_iface_stats, "rx_errors")
    current_iface_stats["tx_errors_delta"] = counter_delta(current_iface_stats, previous_iface_stats, "tx_errors")
    current_iface_stats["rx_dropped_delta"] = counter_delta(current_iface_stats, previous_iface_stats, "rx_dropped")
    current_iface_stats["tx_dropped_delta"] = counter_delta(current_iface_stats, previous_iface_stats, "tx_dropped")

    gateway = str(route.get("gateway") or "")
    gateway_ping = ping_once(gateway) if gateway else {"ip": "", "up": False, "rtt_ms": None, "error": "no default gateway"}

    peer_sources = public_peer_sources()
    unique_peer_ips = sorted({ip for ips in peer_sources.values() for ip in ips})
    peer_ping_results = [ping_once(ip) for ip in unique_peer_ips[:MAX_PEER_PINGS]]

    miner_targets = miner_ping_targets(status)
    miner_pings = []
    for target in miner_targets:
        result = ping_once(target["ip"])
        result["miner"] = target["miner"]
        miner_pings.append(result)

    nodes: dict[str, Any] = {}
    for node in NODES:
        native, native_error = fetch_native_metrics(NODE_METRIC_PORTS.get(node, 0))
        previous_node = (previous.get("nodes") or {}).get(node) if previous else None
        peer_count = len(peer_sources.get(node, []))
        score, reasons = node_p2p_score(node, status, native, peer_count)
        nodes[node] = {
            "score": score,
            "state": "ok" if score >= 90 else "degraded" if score >= 70 else "bad",
            "reasons": reasons,
            "public_peer_count": peer_count,
            "public_peers_sample": peer_sources.get(node, [])[:12],
            "native_metrics_error": native_error,
            "native_metrics": native,
            "native_peers": int(native.get("p2p_peers_", native.get("p2p_peers", 0)) or 0),
            "native_dial_errors_delta": sum(
                value or 0
                for value in (
                    metric_delta("p2p_dials_error_connection", native, previous_node or {}),
                    metric_delta("p2p_dials_error_known", native, previous_node or {}),
                    metric_delta("p2p_dials_error_saturated", native, previous_node or {}),
                    metric_delta("p2p_dials_error_useless", native, previous_node or {}),
                )
            ),
            "p2p_ingress_delta": metric_delta("p2p_ingress_", native, previous_node or {}),
            "p2p_egress_delta": metric_delta("p2p_egress_", native, previous_node or {}),
        }

    active = NODES[0] if NODES else ""
    active_score = float(nodes.get(active or "", {}).get("score", 0.0))

    recommendations: list[str] = []
    if route.get("uses_wifi") or route.get("uses_zerotier") or not route.get("mining_interface_ok"):
        recommendations.append("default-route-not-on-wired-mining-interface")
    if not gateway_ping.get("up") or (gateway_ping.get("rtt_ms") is not None and float(gateway_ping["rtt_ms"]) > LAN_GATEWAY_WARN_MS):
        recommendations.append("lan-gateway-latency-or-loss")
    for node, row in nodes.items():
        if float(row.get("score") or 0.0) < ACTIVE_NODE_WARN_SCORE:
            recommendations.append(f"node-p2p-degraded-{node}")
    if any(int(item.get("down_count") or 0) for item in (ping_summary(miner_pings),)):
        recommendations.append("lan-miner-ping-loss")

    guard_state = "ok"
    if recommendations:
        guard_state = "warning"
    if active and active_score < 70:
        guard_state = "critical"

    payload = {
        "generated_at": now_iso(),
        "generated_epoch": seconds_since_epoch(),
        "guard_state": guard_state,
        "active_node": active,
        "active_node_score": active_score,
        "overall_score": round(min([float(row.get("score", 0.0)) for row in nodes.values()] or [0.0]), 3),
        "recommendations": recommendations,
        "nodes": nodes,
        "rpc_health": {"active_node": active, "reason": "single backend mode"},
        "pool_quality": pool_quality(status),
        "network": {
            "default_route": route,
            "interface_stats": current_iface_stats,
            "gateway_ping": gateway_ping,
            "public_peer_ip_count": len(unique_peer_ips),
            "public_peer_ping_summary": ping_summary(peer_ping_results),
            "public_peer_pings": peer_ping_results,
            "miner_ping_summary": ping_summary(miner_pings),
            "miner_pings": miner_pings,
        },
        "status_overall": status.get("overall"),
        "status_reason": status.get("status_reason"),
    }
    return payload


def append_history(snapshot: dict[str, Any]) -> None:
    rows = read_jsonl_file(HISTORY_FILE, limit=max(0, HISTORY_LIMIT - 1))
    rows.append(snapshot)
    write_jsonl_file(HISTORY_FILE, rows[-HISTORY_LIMIT:], mode=0o600)


def maybe_record_incident(snapshot: dict[str, Any], previous: dict[str, Any] | None) -> None:
    if snapshot.get("guard_state") == "ok":
        return
    signature = json.dumps(
        {
            "guard_state": snapshot.get("guard_state"),
            "active_node": snapshot.get("active_node"),
            "recommendations": snapshot.get("recommendations"),
        },
        sort_keys=True,
    )
    previous_signature = str((previous or {}).get("last_incident_signature") or "")
    previous_epoch = int((previous or {}).get("last_incident_epoch") or 0)
    now = int(snapshot.get("generated_epoch") or time.time())
    if signature == previous_signature and now - previous_epoch < INCIDENT_COOLDOWN_SECONDS:
        snapshot["last_incident_signature"] = previous_signature
        snapshot["last_incident_epoch"] = previous_epoch
        return
    snapshot["last_incident_signature"] = signature
    snapshot["last_incident_epoch"] = now
    append_incident(
        "p2p_guard",
        "critical" if snapshot.get("guard_state") == "critical" else "warning",
        "p2p-guard",
        "P2P/network health guard detected degradation",
        {
            "active_node": snapshot.get("active_node"),
            "active_node_score": snapshot.get("active_node_score"),
            "recommendations": snapshot.get("recommendations"),
            "pool_quality": snapshot.get("pool_quality"),
            "network": snapshot.get("network"),
        },
    )


def sample_once(write_marker: str | None = None) -> dict[str, Any]:
    previous = read_json_file(STATE_FILE, {})
    snapshot = build_snapshot(previous if isinstance(previous, dict) else None)
    maybe_record_incident(snapshot, previous if isinstance(previous, dict) else None)
    write_json_file(STATE_FILE, snapshot, mode=0o644)
    append_history(snapshot)
    if write_marker is not None:
        marker_name = time.strftime("p2p-guard-marker-%Y%m%d-%H%M%S.json")
        marker = {
            "name": write_marker or "p2p guard marker",
            "written_at": now_iso(),
            "written_epoch": snapshot.get("generated_epoch"),
            "snapshot": snapshot,
        }
        write_json_file(MARKER_DIR / marker_name, marker, mode=0o644)
        snapshot["marker_path"] = str(MARKER_DIR / marker_name)
    return snapshot


def summarize_window(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"sample_count": 0}

    def avg(path: tuple[str, ...]) -> float | None:
        values: list[float] = []
        for row in rows:
            item: Any = row
            for key in path:
                item = item.get(key) if isinstance(item, dict) else None
            try:
                if item is not None:
                    values.append(float(item))
            except (TypeError, ValueError):
                continue
        return round(statistics.fmean(values), 4) if values else None

    critical = sum(1 for row in rows if row.get("guard_state") == "critical")
    warning = sum(1 for row in rows if row.get("guard_state") == "warning")
    return {
        "sample_count": len(rows),
        "first_at": rows[0].get("generated_at"),
        "last_at": rows[-1].get("generated_at"),
        "critical_samples": critical,
        "warning_samples": warning,
        "avg_overall_score": avg(("overall_score",)),
        "avg_active_node_score": avg(("active_node_score",)),
        "avg_block_error_ratio": avg(("pool_quality", "block_error_ratio")),
        "avg_valid_share_ratio": avg(("pool_quality", "valid_share_ratio")),
        "avg_stale_job_ratio": avg(("pool_quality", "stale_job_ratio")),
        "avg_tip_overdue_ratio": avg(("pool_quality", "tip_overdue_ratio")),
        "avg_gateway_rtt_ms": avg(("network", "gateway_ping", "rtt_ms")),
        "avg_public_peer_rtt_ms": avg(("network", "public_peer_ping_summary", "avg_rtt_ms")),
        "avg_miner_rtt_ms": avg(("network", "miner_ping_summary", "avg_rtt_ms")),
    }


def compare_marker(marker_path: Path, window_seconds: int) -> dict[str, Any]:
    marker = read_json_file(marker_path, {})
    marker_epoch = int(marker.get("written_epoch") or marker.get("snapshot", {}).get("generated_epoch") or 0)
    if not marker_epoch:
        raise RuntimeError(f"marker has no written epoch: {marker_path}")
    rows = read_jsonl_file(HISTORY_FILE, limit=HISTORY_LIMIT)
    before = [
        row
        for row in rows
        if marker_epoch - window_seconds <= int(row.get("generated_epoch") or 0) < marker_epoch
    ]
    after = [
        row
        for row in rows
        if marker_epoch <= int(row.get("generated_epoch") or 0) <= marker_epoch + window_seconds
    ]
    return {
        "generated_at": now_iso(),
        "marker": str(marker_path),
        "marker_name": marker.get("name"),
        "marker_written_at": marker.get("written_at"),
        "window_seconds": window_seconds,
        "baseline": summarize_window([marker["snapshot"]]) if isinstance(marker.get("snapshot"), dict) else {"sample_count": 0},
        "before": summarize_window(before),
        "after": summarize_window(after),
    }


def loop(interval: int) -> None:
    log(f"p2p guard started interval={interval}s")
    while True:
        try:
            snapshot = sample_once()
            log(
                "sample "
                f"state={snapshot.get('guard_state')} active={snapshot.get('active_node')} "
                f"active_score={snapshot.get('active_node_score')} recommendations={snapshot.get('recommendations')}"
            )
        except Exception as exc:  # noqa: BLE001 - guard must keep monitoring.
            log(f"p2p guard sample failed: {exc}")
            append_incident("p2p_guard_failed", "warning", "p2p-guard", str(exc))
        time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="take one sample and print JSON")
    parser.add_argument("--loop", action="store_true", help="run continuously")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS, help="loop interval in seconds")
    parser.add_argument("--mark", default=None, help="write a comparison marker with this label")
    parser.add_argument("--compare-marker", default=None, help="compare before/after windows around marker file")
    parser.add_argument("--window-seconds", type=int, default=3600, help="comparison window size")
    args = parser.parse_args()

    ensure_runtime()
    if args.compare_marker:
        print(json.dumps(compare_marker(Path(args.compare_marker), args.window_seconds), indent=2, sort_keys=True))
        return 0
    if args.loop:
        loop(args.interval)
        return 0
    snapshot = sample_once(write_marker=args.mark)
    print(json.dumps(snapshot, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

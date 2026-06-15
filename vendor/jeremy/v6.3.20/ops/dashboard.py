#!/usr/bin/env python3
"""Local BlockDAG pool operations dashboard."""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import fcntl
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from incident_journal import read_recent_incidents
from pool_ops import (
    EARNINGS_SNAPSHOT_EXPECTED_INTERVAL_SECONDS,
    GLOBAL_CACHE_FILE,
    GLOBAL_HISTORY_LIMIT,
    GLOBAL_STATS_SOURCE_TRUTH,
    PROJECT_ROOT,
    RUNTIME_DIR,
    collect_global_blockchain,
    collect_earnings,
    collect_status_cached,
    configure_miners,
    default_miner_pool_settings,
    ensure_runtime,
    make_handoff,
    mark_configured_miners,
    miner_display_label,
    miner_identity_key,
    normalize_mac,
    now_iso,
    read_latest_earnings_snapshot_info,
    read_dashboard_plot_rebuild_state,
    read_valid_global_history,
    read_miner_registry,
    record_earnings_snapshot,
    refresh_global_chain_head,
    save_miner_admin_password,
    scan_miners,
    upsert_miner_registry,
    warm_dashboard_history_caches,
    write_action_state,
)


HOST = os.environ.get("BDAG_DASHBOARD_BIND", "127.0.0.1")
PORT = int(os.environ.get("BDAG_DASHBOARD_PORT", "8088"))
ACTION_TOKEN = os.environ.get("BDAG_DASHBOARD_TOKEN", "")
REQUIRE_TOKEN = os.environ.get("BDAG_DASHBOARD_REQUIRE_TOKEN", "auto")
WATCHDOG = PROJECT_ROOT / "ops" / "watchdog.py"
P2P_GUARD_STATE = RUNTIME_DIR / "p2p-health-state.json"
REPORTS_DIR = RUNTIME_DIR / "reports"
DEFAULT_STATUS_CACHE_SECONDS = os.environ.get("BDAG_STATUS_PAYLOAD_STALE_AFTER_SECONDS", "120")
STATUS_CACHE_SECONDS = float(os.environ.get("BDAG_DASHBOARD_STATUS_CACHE_SECONDS", DEFAULT_STATUS_CACHE_SECONDS))
EARNINGS_CACHE_SECONDS = float(os.environ.get("BDAG_DASHBOARD_EARNINGS_CACHE_SECONDS", "30"))
SAMPLER_CACHE_SECONDS = float(os.environ.get("BDAG_DASHBOARD_SAMPLER_CACHE_SECONDS", DEFAULT_STATUS_CACHE_SECONDS))
DASHBOARD_STATUS_SAMPLE_WAIT_SECONDS = max(
    5.0,
    float(
        os.environ.get(
            "BDAG_DASHBOARD_STATUS_SAMPLE_WAIT_SECONDS",
            os.environ.get("BDAG_STATUS_PAYLOAD_STALE_AFTER_SECONDS", str(max(SAMPLER_CACHE_SECONDS, STATUS_CACHE_SECONDS))),
        )
    ),
)
DASHBOARD_DIRECT_STATUS_FALLBACK = os.environ.get(
    "BDAG_DASHBOARD_DIRECT_STATUS_FALLBACK", "0"
).strip().lower() in {"1", "true", "yes", "on"}
DASHBOARD_COLLECTOR_API = os.environ.get("BDAG_COLLECTOR_API", "").strip().rstrip("/")
DASHBOARD_COLLECTOR_STATUS_TIMEOUT = float(os.environ.get("BDAG_DASHBOARD_COLLECTOR_STATUS_TIMEOUT", "15"))
EARNINGS_SAMPLER_ENABLED = os.environ.get("BDAG_DASHBOARD_EARNINGS_SAMPLER_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
EARNINGS_SAMPLER_INTERVAL_SECONDS = max(
    30.0,
    float(os.environ.get("BDAG_DASHBOARD_EARNINGS_SAMPLER_INTERVAL_SECONDS", "60")),
)
DASHBOARD_POOL_METRICS_TIMEOUT = float(os.environ.get("BDAG_DASHBOARD_POOL_METRICS_TIMEOUT", "1.5"))
TEMPLATE_BACKEND_STATE_CACHE_SECONDS = float(
    os.environ.get("BDAG_DASHBOARD_TEMPLATE_BACKEND_STATE_CACHE_SECONDS", str(STATUS_CACHE_SECONDS))
)
SYNC_ESTIMATE_STATE_FILE = RUNTIME_DIR / "dashboard-sync-estimate-state.json"
EARNINGS_SAMPLER_LOCK_FILE = RUNTIME_DIR / "dashboard-earnings-sampler.lock"
EARNINGS_SAMPLER_STATE_FILE = RUNTIME_DIR / "dashboard-earnings-sampler-state.json"
GLOBAL_SAMPLER_ENABLED = os.environ.get("BDAG_DASHBOARD_GLOBAL_SAMPLER_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
GLOBAL_SAMPLER_INTERVAL_SECONDS = max(
    15.0,
    float(os.environ.get("BDAG_DASHBOARD_GLOBAL_SAMPLER_INTERVAL_SECONDS", "60")),
)
GLOBAL_SAMPLER_STATE_FILE = RUNTIME_DIR / "dashboard-global-sampler-state.json"
PROMETHEUS_SAMPLE_RE = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|[-+]?Inf|NaN)$"
)
PROMETHEUS_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"')
PROCESSED_BLOCKS_RE = re.compile(r"Processed\s+([0-9,]+)\s+blocks\s+in\s+the\s+last\s+([0-9.]+)s")
API_CACHE: dict[str, tuple[float, object]] = {}
API_CACHE_LOCK = threading.Lock()
GLOBAL_REFRESH_LOCK = threading.Lock()


def cached_payload(key: str, ttl: float, factory):
    now = time.time()
    with API_CACHE_LOCK:
        cached = API_CACHE.get(key)
        if cached and now - cached[0] < ttl:
            return cached[1]
    payload = factory()
    with API_CACHE_LOCK:
        API_CACHE[key] = (now, payload)
    return payload


def clear_api_cache(*keys: str) -> None:
    with API_CACHE_LOCK:
        if keys:
            for key in keys:
                API_CACHE.pop(key, None)
        else:
            API_CACHE.clear()


def read_dashboard_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def dashboard_cache_age(epoch: object) -> float | None:
    try:
        return max(0.0, time.time() - float(epoch))
    except (TypeError, ValueError):
        return None


def dashboard_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def attach_dashboard_endpoint(payload: dict[str, object]) -> dict[str, object]:
    payload["dashboard_bind"] = HOST
    payload["dashboard_port"] = PORT
    display_host = "127.0.0.1" if HOST in {"0.0.0.0", "::", ""} else HOST
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    payload["dashboard_url"] = f"http://{display_host}:{PORT}"
    payload["dashboard_plot_rebuild"] = read_dashboard_plot_rebuild_state()
    return payload


def cached_status_for_dashboard(include_logs: bool = True) -> tuple[dict[str, object] | None, dict[str, object]]:
    sampler_path = RUNTIME_DIR / "status-sampler.json"
    shared_cache_path = RUNTIME_DIR / "shared-status-cache.json"
    diagnostics: dict[str, object] = {
        "status_sampler": {
            "hit": False,
            "path": str(sampler_path),
            "age_seconds": None,
            "stale": False,
        },
        "shared_status_cache": {
            "hit": False,
            "path": str(shared_cache_path),
            "age_seconds": None,
            "stale": False,
        },
    }

    sampler = read_dashboard_json(sampler_path)
    if isinstance(sampler, dict):
        sampler_age = dashboard_cache_age(sampler.get("epoch"))
        sampler_diag = diagnostics["status_sampler"]
        assert isinstance(sampler_diag, dict)
        sampler_diag["age_seconds"] = round(sampler_age, 3) if sampler_age is not None else None
        payload = sampler.get("payload")
        if isinstance(payload, dict) and sampler_age is not None and sampler_age <= SAMPLER_CACHE_SECONDS:
            result = dict(payload)
            result_age = round((dashboard_float(result.get("age_seconds")) or 0.0) + sampler_age, 3)
            stale_after = dashboard_float(result.get("stale_after_seconds"))
            payload_fresh = result.get("fresh")
            if payload_fresh is not False and (stale_after is None or result_age <= stale_after):
                result["age_seconds"] = result_age
                result["fresh"] = True
                result["status_sampler"] = {
                    "hit": True,
                    "path": str(sampler_path),
                    "age_seconds": round(sampler_age, 3),
                    "max_age_seconds": SAMPLER_CACHE_SECONDS,
                    "include_logs": bool(sampler.get("include_logs")),
                    "requested_include_logs": include_logs,
                }
                return result, diagnostics
        sampler_diag["stale"] = payload is not None

    shared_cache = read_dashboard_json(shared_cache_path)
    if isinstance(shared_cache, dict):
        key = "with_logs" if include_logs else "no_logs"
        row = shared_cache.get(key)
        if isinstance(row, dict):
            cache_age = dashboard_cache_age(row.get("epoch"))
            shared_diag = diagnostics["shared_status_cache"]
            assert isinstance(shared_diag, dict)
            shared_diag["age_seconds"] = round(cache_age, 3) if cache_age is not None else None
            payload = row.get("payload")
            if isinstance(payload, dict) and cache_age is not None and cache_age <= STATUS_CACHE_SECONDS:
                result = dict(payload)
                result_age = round((dashboard_float(result.get("age_seconds")) or 0.0) + cache_age, 3)
                stale_after = dashboard_float(result.get("stale_after_seconds"))
                payload_fresh = result.get("fresh")
                if payload_fresh is not False and (stale_after is None or result_age <= stale_after):
                    result["age_seconds"] = result_age
                    result["fresh"] = True
                    result["shared_status_cache"] = {
                        "hit": True,
                        "path": str(shared_cache_path),
                        "age_seconds": round(cache_age, 3),
                        "max_age_seconds": STATUS_CACHE_SECONDS,
                        "key": key,
                    }
                    return result, diagnostics
            shared_diag["stale"] = payload is not None

    return None, diagnostics


def collector_status_for_dashboard() -> dict[str, object] | None:
    if not DASHBOARD_COLLECTOR_API:
        return None
    url = f"{DASHBOARD_COLLECTOR_API}/api/status"
    try:
        request = Request(url, headers={"accept": "application/json"})
        with urlopen(request, timeout=DASHBOARD_COLLECTOR_STATUS_TIMEOUT) as response:
            payload = json.loads(response.read(5_000_000).decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    result = dict(payload)
    result["dashboard_collector_api"] = {
        "hit": True,
        "url": url,
        "timeout_seconds": DASHBOARD_COLLECTOR_STATUS_TIMEOUT,
    }
    return result


def rounded_wait_seconds(seconds: float) -> int:
    if seconds <= 15:
        return max(1, int(round(seconds)))
    return max(5, int(((seconds + 4.999) // 5) * 5))


def dashboard_status_wait_context(diagnostics: dict[str, object]) -> tuple[str, int, float | None]:
    ages: list[float] = []
    for key in ("status_sampler", "shared_status_cache"):
        info = diagnostics.get(key)
        if not isinstance(info, dict):
            continue
        age = dashboard_float(info.get("age_seconds"))
        if age is not None:
            ages.append(age)

    newest_age = min(ages) if ages else None
    if newest_age is None:
        wait_seconds = rounded_wait_seconds(DASHBOARD_STATUS_SAMPLE_WAIT_SECONDS)
        return (
            "Dashboard status is warming up. Waiting for the next sampler update; "
            f"it usually arrives within about {wait_seconds}s.",
            wait_seconds,
            None,
        )

    remaining = DASHBOARD_STATUS_SAMPLE_WAIT_SECONDS - newest_age
    wait_seconds = rounded_wait_seconds(remaining if remaining > 1 else DASHBOARD_STATUS_SAMPLE_WAIT_SECONDS)
    return (
        "Dashboard status is waiting for the next sampler update. "
        f"The newest cached sample is about {rounded_wait_seconds(newest_age)}s old; "
        f"the next dashboard refresh should have current data in about {wait_seconds}s.",
        wait_seconds,
        newest_age,
    )


def dashboard_status_fast_fallback(diagnostics: dict[str, object]) -> dict[str, object]:
    failure_message, wait_seconds, newest_age = dashboard_status_wait_context(diagnostics)
    payload: dict[str, object] = {
        "generated_at": now_iso(),
        "overall": "degraded",
        "status_reason": failure_message,
        "mode": "waiting_for_status_sample",
        "can_mine": False,
        "can_accept_shares": False,
        "can_submit_blocks": False,
        "fresh": False,
        "age_seconds": 0.0,
        "stale_after_seconds": STATUS_CACHE_SECONDS,
        "project_root": str(PROJECT_ROOT),
        "runtime_dir": str(RUNTIME_DIR),
        "truth_sources": {
            "status": "status_sampler or shared_status_cache",
            "chain_block_count": "not_checked_budgeted_status_fallback",
        },
        "blocking_failures": [failure_message],
        "degraded_reasons": [],
        "failures": [failure_message],
        "stack_failures": [failure_message],
        "miner_failures": [],
        "warnings": ["dashboard status returned bounded fallback because cached status was unavailable"],
        "sync_warnings": [],
        "maintenance_warnings": [],
        "collector_budget_exceeded": True,
        "collector_budget_failure": {
            "component": "dashboard_status",
            "class": "waiting_for_status_sample",
            "detail": failure_message,
            "estimated_wait_seconds": wait_seconds,
            "newest_cache_age_seconds": newest_age,
        },
        "chain_rpc_error": "not_checked_budgeted_status_fallback",
        "containers": {},
        "nodes": {},
        "node_services": [],
        "stack_services": [],
        "sync_progress": {"status": "waiting_for_status_sample", "percent": None, "remaining_blocks": None},
        "sync_estimate": {
            "stage": "Waiting for status sample",
            "narrative": failure_message,
            "next_step": f"wait about {wait_seconds}s for the next dashboard status sample; mining may still be running",
            "eta_seconds": wait_seconds,
        },
        "sync_health": {},
        "pool": {},
        "pool_metrics": {},
        "pool_health": {},
        "miner_health": {"miners": [], "failures": []},
        "local_ips": [],
        "pool_endpoint": "",
        "pool_port": None,
        "latest_action": None,
        "status_sampler": diagnostics.get("status_sampler"),
        "shared_status_cache": diagnostics.get("shared_status_cache"),
    }
    return attach_dashboard_endpoint(payload)


def write_earnings_sampler_state(payload: dict[str, object]) -> None:
    try:
        write_json(EARNINGS_SAMPLER_STATE_FILE, payload)
    except Exception:
        pass


def latest_earnings_snapshot_age() -> float | None:
    info = read_latest_earnings_snapshot_info()
    latest_epoch = info.get("latest_epoch")
    if latest_epoch is None:
        return None
    try:
        return max(0.0, time.time() - float(latest_epoch))
    except (TypeError, ValueError):
        return None


def record_dashboard_earnings_sample(reason: str) -> bool:
    ensure_runtime()
    age = latest_earnings_snapshot_age()
    if age is not None and age < EARNINGS_SAMPLER_INTERVAL_SECONDS * 0.8:
        return False
    try:
        with EARNINGS_SAMPLER_LOCK_FILE.open("w", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            snapshot = record_earnings_snapshot()
    except Exception as exc:  # noqa: BLE001 - dashboard sampling must never stop serving.
        write_earnings_sampler_state(
            {
                "updated_at": now_iso(),
                "status": "failed",
                "reason": reason,
                "error": str(exc),
            }
        )
        return False
    clear_api_cache("earnings", "sampler")
    write_earnings_sampler_state(
        {
            "updated_at": now_iso(),
            "status": "ok",
            "reason": reason,
            "snapshot_at": snapshot.get("generated_at"),
            "miner_count": len(snapshot.get("miner_estimates") or []),
        }
    )
    return True


def earnings_sampler_loop() -> None:
    while True:
        record_dashboard_earnings_sample("dashboard-background-sampler")
        time.sleep(EARNINGS_SAMPLER_INTERVAL_SECONDS)


def start_earnings_sampler() -> None:
    if not EARNINGS_SAMPLER_ENABLED:
        return
    thread = threading.Thread(target=earnings_sampler_loop, name="earnings-sampler", daemon=True)
    thread.start()


def write_global_sampler_state(payload: dict[str, object]) -> None:
    try:
        write_json(GLOBAL_SAMPLER_STATE_FILE, payload)
    except Exception:
        pass


def run_global_refresh(reason: str) -> None:
    started = time.time()
    try:
        payload = collect_global_blockchain()
        clear_api_cache("global", "sampler")
        write_global_sampler_state(
            {
                "updated_at": now_iso(),
                "status": payload.get("status", "unknown"),
                "reason": reason,
                "duration_seconds": round(time.time() - started, 3),
                "latest_block": payload.get("latest_block"),
                "chain_latest_block": payload.get("chain_latest_block"),
                "requested_blocks": payload.get("requested_blocks"),
                "fetched_blocks": payload.get("fetched_blocks"),
                "error": payload.get("error", ""),
            }
        )
    except Exception as exc:  # noqa: BLE001 - dashboard must keep serving from last good data.
        write_global_sampler_state(
            {
                "updated_at": now_iso(),
                "status": "failed",
                "reason": reason,
                "duration_seconds": round(time.time() - started, 3),
                "error": str(exc),
            }
        )


def trigger_global_refresh(reason: str) -> bool:
    if not GLOBAL_REFRESH_LOCK.acquire(blocking=False):
        return False

    def worker() -> None:
        try:
            run_global_refresh(reason)
        finally:
            GLOBAL_REFRESH_LOCK.release()

    thread = threading.Thread(target=worker, name="global-sampler", daemon=True)
    thread.start()
    return True


def global_sampler_loop() -> None:
    trigger_global_refresh("dashboard-global-sampler-start")
    while True:
        time.sleep(GLOBAL_SAMPLER_INTERVAL_SECONDS)
        trigger_global_refresh("dashboard-global-background-sampler")


def start_global_sampler() -> None:
    if not GLOBAL_SAMPLER_ENABLED:
        return
    thread = threading.Thread(target=global_sampler_loop, name="global-sampler-loop", daemon=True)
    thread.start()


def collect_global_dashboard_payload(reason: str) -> dict[str, object]:
    cached = read_json(GLOBAL_CACHE_FILE, {})
    if isinstance(cached, dict) and cached:
        payload: dict[str, object] = dict(cached)
        payload["history"] = read_valid_global_history(limit=GLOBAL_HISTORY_LIMIT)
        payload = refresh_global_chain_head(payload)
        cache_meta = dict(payload.get("cache") or {}) if isinstance(payload.get("cache"), dict) else {}
        updated_epoch = safe_float(payload.get("updated_at_epoch"), None)
        if updated_epoch is not None:
            cache_meta["age_seconds"] = max(0, round(time.time() - updated_epoch, 1))
        cache_meta["hit"] = True
        cache_meta["served_by"] = "dashboard-cache-live-head"
        payload["cache"] = cache_meta
        payload["cache_hit"] = True
        if payload.get("chain_tip_lag_blocks"):
            payload["scan_lagged"] = True
        trigger_global_refresh(reason)
        return payload

    trigger_global_refresh(f"{reason}-no-cache")
    return refresh_global_chain_head(
        {
            "status": "deferred",
            "source": "dashboard-global-cache",
            "source_truth": GLOBAL_STATS_SOURCE_TRUTH,
            "schema_version": 2,
            "error": "global cache is warming; refresh again shortly",
            "clusters": [],
            "history": [],
            "cache": {"hit": False, "served_by": "dashboard-cache-live-head"},
            "refresh_pending": True,
        }
    )


def parse_prometheus_labels(label_text: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for match in PROMETHEUS_LABEL_RE.finditer(label_text or ""):
        labels[match.group(1)] = match.group(2).replace(r"\"", '"').replace(r"\\", "\\")
    return labels


def safe_int(value: object, default: int | None = None) -> int | None:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value: object, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def read_json(path: Path, fallback: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def write_json(path: Path, payload: object) -> None:
    ensure_runtime()
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def eta_iso(epoch: float | None) -> str:
    if epoch is None:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(epoch))


def node_processed_rate_from_tail(node: dict[str, object]) -> tuple[float | None, str]:
    tail = node.get("tail")
    if not isinstance(tail, list):
        return None, ""
    for line in reversed(tail):
        match = PROCESSED_BLOCKS_RE.search(str(line or ""))
        if not match:
            continue
        blocks = safe_float(match.group(1).replace(",", ""))
        seconds = safe_float(match.group(2))
        if blocks is None or seconds is None or seconds <= 0:
            continue
        return blocks / seconds, f"recent node log: {int(blocks)} blocks/{seconds:g}s"
    return None, ""


def sync_progress_for_node(payload: dict[str, object], node_name: str) -> dict[str, object]:
    sync_progress = payload.get("sync_progress") if isinstance(payload.get("sync_progress"), dict) else {}
    nodes = sync_progress.get("nodes") if isinstance(sync_progress.get("nodes"), dict) else {}
    node_progress = nodes.get(node_name) if isinstance(nodes, dict) else None
    if isinstance(node_progress, dict):
        return node_progress
    if sync_progress.get("source") == node_name:
        return sync_progress
    return {}


def choose_sync_leader(payload: dict[str, object]) -> str:
    coordinator = payload.get("sync_coordinator") if isinstance(payload.get("sync_coordinator"), dict) else {}
    leader = str(coordinator.get("leader") or "")
    if leader:
        return leader
    sync_progress = payload.get("sync_progress") if isinstance(payload.get("sync_progress"), dict) else {}
    nodes = sync_progress.get("nodes") if isinstance(sync_progress.get("nodes"), dict) else {}
    candidates: list[tuple[int, str]] = []
    if isinstance(nodes, dict):
        for name, progress in nodes.items():
            if not isinstance(progress, dict):
                continue
            current = safe_int(progress.get("current_block"), 0) or 0
            if current > 0:
                candidates.append((current, str(name)))
    candidates.sort(reverse=True)
    return candidates[0][1] if candidates else ""


def enrich_status_with_sync_estimate(payload: dict[str, object]) -> dict[str, object]:
    now = time.time()
    sync_progress = payload.get("sync_progress") if isinstance(payload.get("sync_progress"), dict) else {}
    sync_health = payload.get("sync_health") if isinstance(payload.get("sync_health"), dict) else {}
    coordinator = payload.get("sync_coordinator") if isinstance(payload.get("sync_coordinator"), dict) else {}
    catchup_policy = payload.get("catchup_policy") if isinstance(payload.get("catchup_policy"), dict) else {}
    nodes = payload.get("nodes") if isinstance(payload.get("nodes"), dict) else {}
    managed_nodes = payload.get("managed_node_services") if isinstance(payload.get("managed_node_services"), list) else []
    single_active_node = len(managed_nodes) == 1
    leader = choose_sync_leader(payload)
    mode = str(coordinator.get("mode") or "active_node_catchup")
    threshold = safe_int(((coordinator.get("last_decision") or {}).get("thresholds") or {}).get("leader_near_tip_blocks"), 5) or 5

    state = read_json(SYNC_ESTIMATE_STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}
    previous_nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    new_state = {"updated_at": eta_iso(now), "nodes": {}}
    estimate_nodes: dict[str, object] = {}

    progress_nodes = sync_progress.get("nodes") if isinstance(sync_progress.get("nodes"), dict) else {}
    node_names = sorted(set(list(nodes.keys()) + list(progress_nodes.keys() if isinstance(progress_nodes, dict) else [])))
    for name in node_names:
        progress = sync_progress_for_node(payload, name)
        current = safe_int(progress.get("current_block"))
        highest = safe_int(progress.get("highest_block"))
        remaining = safe_int(progress.get("remaining_blocks"))
        percent = safe_float(progress.get("percent"))
        node_info = nodes.get(name) if isinstance(nodes.get(name), dict) else {}
        log_rate, log_rate_source = node_processed_rate_from_tail(node_info)

        previous = previous_nodes.get(name) if isinstance(previous_nodes.get(name), dict) else {}
        previous_current = safe_int(previous.get("current_block"))
        previous_remaining = safe_int(previous.get("remaining_blocks"))
        previous_epoch = safe_float(previous.get("epoch"))
        observed_import_rate = None
        observed_net_rate = None
        if current is not None and previous_current is not None and previous_epoch is not None:
            elapsed = now - previous_epoch
            if 5 <= elapsed <= 7200 and current > previous_current:
                observed_import_rate = (current - previous_current) / elapsed
        if remaining is not None and previous_remaining is not None and previous_epoch is not None:
            elapsed = now - previous_epoch
            if 5 <= elapsed <= 7200 and previous_remaining > remaining:
                observed_net_rate = (previous_remaining - remaining) / elapsed

        rate = observed_net_rate or observed_import_rate or log_rate
        rate_source = (
            "net catch-up across dashboard samples"
            if observed_net_rate
            else "block import across dashboard samples"
            if observed_import_rate
            else log_rate_source
        )
        eta_seconds = remaining / rate if remaining is not None and rate and rate > 0 else None
        seed_remaining = max(0, remaining - threshold) if remaining is not None else None
        eta_to_seed_seconds = seed_remaining / rate if seed_remaining is not None and rate and rate > 0 else None
        estimate_nodes[name] = {
            "current_block": current,
            "highest_block": highest,
            "remaining_blocks": remaining,
            "percent": percent,
            "rate_blocks_per_second": round(rate, 3) if rate else None,
            "rate_source": rate_source,
            "eta_seconds": round(eta_seconds) if eta_seconds is not None else None,
            "eta_at": eta_iso(now + eta_seconds) if eta_seconds is not None else "",
            "eta_to_seed_seconds": round(eta_to_seed_seconds) if eta_to_seed_seconds is not None else None,
            "eta_to_seed_at": eta_iso(now + eta_to_seed_seconds) if eta_to_seed_seconds is not None else "",
            "planned_pause": False,
            "leader": bool(name == leader),
        }
        if current is not None or remaining is not None:
            new_state["nodes"][name] = {
                "epoch": now,
                "current_block": current,
                "remaining_blocks": remaining,
                "highest_block": highest,
            }

    leader_estimate = estimate_nodes.get(leader) if isinstance(estimate_nodes.get(leader), dict) else {}
    remaining = safe_int(leader_estimate.get("remaining_blocks")) if leader_estimate else safe_int(sync_progress.get("remaining_blocks"))
    rate = safe_float(leader_estimate.get("rate_blocks_per_second")) if leader_estimate else None
    catchup_active = bool(catchup_policy.get("active"))
    catchup_trigger = str(catchup_policy.get("trigger") or "")
    stage = (
        "I/O-bound catch-up pause"
        if catchup_active and catchup_trigger == "io_pressure"
        else "Catch-up pause active"
        if catchup_active
        else "Synced"
        if sync_progress.get("status") == "synced"
        else "Active-node catch-up"
        if single_active_node
        else "Syncing"
    )
    if catchup_active:
        narrative = str(
            catchup_policy.get("user_message")
            or "Mining is intentionally paused while the chain node catches up to peers."
        )
    elif sync_progress.get("status") == "synced":
        narrative = "Managed nodes are synced to the current network tip."
    elif single_active_node and leader:
        narrative = f"{leader} is the only active production node. The pool will wait for this node to finish sync before mining jobs are sent."
    else:
        narrative = "Managed nodes are syncing; the pool will wait for backend sync before mining jobs are sent."

    payload["sync_estimate"] = {
        "generated_at": eta_iso(now),
        "stage": stage,
        "mode": mode,
        "leader": leader,
        "seed_threshold_blocks": threshold,
        "remaining_blocks": remaining,
        "rate_blocks_per_second": rate,
        "rate_source": leader_estimate.get("rate_source") if leader_estimate else "",
        "eta_seconds": leader_estimate.get("eta_seconds") if leader_estimate else None,
        "eta_at": leader_estimate.get("eta_at") if leader_estimate else "",
        "eta_to_seed_seconds": leader_estimate.get("eta_to_seed_seconds") if leader_estimate else None,
        "eta_to_seed_at": leader_estimate.get("eta_to_seed_at") if leader_estimate else "",
        "narrative": narrative,
        "catchup_pause_active": catchup_active,
        "catchup_pause_lag_blocks": catchup_policy.get("lag_blocks"),
        "catchup_pause_threshold_blocks": catchup_policy.get("threshold_blocks"),
        "next_step": catchup_policy.get("next_step") if catchup_active else "",
        "nodes": estimate_nodes,
    }
    write_json(SYNC_ESTIMATE_STATE_FILE, new_state)
    return payload


def template_backend_state_from_metrics(text: str, source: str) -> dict[str, object]:
    state: dict[str, object] = {"source": source, "backends": {}}
    backends = state["backends"]
    assert isinstance(backends, dict)

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = PROMETHEUS_SAMPLE_RE.match(line)
        if not match:
            continue
        metric_name, label_text, raw_value = match.groups()
        try:
            value = float(raw_value)
        except ValueError:
            continue
        labels = parse_prometheus_labels(label_text or "")
        if metric_name in {
            "pool_rpc_backend_selected",
            "pool_rpc_backend_healthy",
            "pool_rpc_backend_score",
            "pool_rpc_backend_template_age_seconds",
            "pool_rpc_backend_ws_connected",
        }:
            backend = labels.get("backend")
            if not backend:
                continue
            row = backends.setdefault(backend, {})
            if not isinstance(row, dict):
                continue
            if metric_name == "pool_rpc_backend_selected":
                row["selected"] = value > 0
                if value > 0:
                    state["selected_backend"] = backend
            elif metric_name == "pool_rpc_backend_healthy":
                row["healthy"] = value > 0
            elif metric_name == "pool_rpc_backend_score":
                row["score"] = value
            elif metric_name == "pool_rpc_backend_template_age_seconds":
                row["template_age_seconds"] = round(value, 3)
            elif metric_name == "pool_rpc_backend_ws_connected":
                row["ws_connected"] = value > 0

    if backends:
        state["backend_count"] = len(backends)
        state["healthy_backend_count"] = sum(
            1 for row in backends.values() if isinstance(row, dict) and row.get("healthy") is True
        )
    return state


def collect_template_backend_states(endpoints: list[str]) -> tuple[list[dict[str, object]], list[str]]:
    states: list[dict[str, object]] = []
    errors: list[str] = []
    for endpoint in endpoints:
        url = f"http://{endpoint}/metrics"
        request = Request(url, headers={"accept": "text/plain", "user-agent": "BDAGDashboard/1.0"})
        try:
            with urlopen(request, timeout=DASHBOARD_POOL_METRICS_TIMEOUT) as response:
                metrics_text = response.read(1024 * 1024).decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 - advisory dashboard enrichment only.
            errors.append(f"{endpoint}: {exc}")
            continue
        state = template_backend_state_from_metrics(metrics_text, endpoint)
        if state.get("fan_in") or state.get("backends"):
            states.append(state)
    return states, errors


def enrich_status_with_template_backend_state(payload: dict[str, object]) -> dict[str, object]:
    pool_metrics = payload.get("pool_metrics")
    if not isinstance(pool_metrics, dict):
        return payload
    containers = pool_metrics.get("containers")
    if not isinstance(containers, dict):
        return payload

    endpoints: list[str] = []
    for info in containers.values():
        if not isinstance(info, dict):
            continue
        endpoint = str(info.get("endpoint") or "").strip()
        if not endpoint:
            continue
        endpoints.append(endpoint)
    if not endpoints:
        return payload

    cache_key = "template_backend_state:" + ",".join(sorted(endpoints))
    states, errors = cached_payload(
        cache_key,
        TEMPLATE_BACKEND_STATE_CACHE_SECONDS,
        lambda: collect_template_backend_states(endpoints),
    )

    if states:
        pool_metrics["template_backend_state"] = states[0] if len(states) == 1 else {"pools": states}
    if errors:
        pool_metrics["template_backend_state_error"] = "; ".join(errors[:2])
    return payload


def dashboard_status_payload() -> dict[str, object]:
    collector_payload = collector_status_for_dashboard()
    if collector_payload is not None:
        return attach_dashboard_endpoint(collector_payload)

    cached, diagnostics = cached_status_for_dashboard(include_logs=True)
    if cached is not None:
        return attach_dashboard_endpoint(cached)

    if not DASHBOARD_DIRECT_STATUS_FALLBACK:
        return dashboard_status_fast_fallback(diagnostics)

    payload = enrich_status_with_template_backend_state(enrich_status_with_sync_estimate(collect_status_cached(include_logs=True)))
    return attach_dashboard_endpoint(payload)


def token_required() -> bool:
    if REQUIRE_TOKEN.lower() in {"1", "true", "yes"}:
        return True
    if REQUIRE_TOKEN.lower() in {"0", "false", "no"}:
        return False
    return HOST not in {"127.0.0.1", "localhost", "::1"}


def get_action_token() -> str:
    ensure_runtime()
    global ACTION_TOKEN
    if ACTION_TOKEN:
        return ACTION_TOKEN
    path = RUNTIME_DIR / "dashboard-token.txt"
    if path.exists():
        ACTION_TOKEN = path.read_text(encoding="utf-8").strip()
        return ACTION_TOKEN
    ACTION_TOKEN = secrets.token_urlsafe(24)
    path.write_text(ACTION_TOKEN + "\n", encoding="utf-8")
    path.chmod(0o600)
    return ACTION_TOKEN


def collect_sampler_status() -> dict[str, object]:
    now = int(time.time())
    threshold = EARNINGS_SNAPSHOT_EXPECTED_INTERVAL_SECONDS * 3
    info = read_latest_earnings_snapshot_info()
    latest_epoch = info.get("latest_epoch")
    latest_age = int(now - float(latest_epoch)) if latest_epoch is not None else None
    stale = latest_age is None or latest_age > threshold
    if stale:
        status = "stale"
        reason = "The earnings/miner plot sampler has not written a fresh valid snapshot."
    elif info.get("latest_at"):
        status = "ok"
        reason = ""
    else:
        status = "missing"
        reason = "No valid earnings/miner plot snapshot has been recorded yet."
    return {
        "generated_at": now_iso(),
        "status": status,
        "stale": stale,
        "reason": reason,
        "latest_at": info.get("latest_at"),
        "latest_age_seconds": latest_age,
        "expected_interval_seconds": EARNINGS_SNAPSHOT_EXPECTED_INTERVAL_SECONDS,
        "stale_threshold_seconds": threshold,
        "snapshot_info": info,
    }


def start_background_action(name: str, command: list[str], reason: str) -> dict[str, str]:
    ensure_runtime()
    log_path = RUNTIME_DIR / "logs" / f"dashboard-{name}-{int(time.time())}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "name": name,
        "reason": reason,
        "status": "running",
        "started_at": now_iso(),
        "finished_at": None,
        "log_path": str(log_path),
    }
    write_action_state(state)

    def runner() -> None:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"[{now_iso()}] $ {' '.join(command)}\n")
            log.flush()
            started = time.time()
            proc = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                text=True,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
            elapsed = round(time.time() - started, 3)
            log.write(f"\n[{now_iso()}] exit={proc.returncode} elapsed={elapsed}s\n")
        state.update(
            {
                "status": "ok" if proc.returncode == 0 else "failed",
                "finished_at": now_iso(),
                "elapsed": elapsed,
            }
        )
        write_action_state(state)

    threading.Thread(target=runner, daemon=True).start()
    return {"status": "started", "log_path": str(log_path)}


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BlockDAG Pool Operations</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f8;
      --panel: #ffffff;
      --panel-alt: #f8fafc;
      --line: #d7dbe0;
      --text: #16202a;
      --muted: #617181;
      --ok: #197a46;
      --warn: #a45b00;
      --down: #b3261e;
      --sync: #1d5f99;
      --button: #1c2b36;
      --button-text: #ffffff;
      --button-secondary-bg: #ffffff;
      --input-bg: #ffffff;
      --chart-bg: #fbfcfd;
      --pre-bg: #101820;
      --pre-text: #dfe7ef;
      --progress-bg: #e5e9ee;
      --shadow: rgba(0,0,0,0.05);
    }
    :root[data-theme="dark"] {
      color-scheme: dark;
      --bg: #0e141b;
      --panel: #151d26;
      --panel-alt: #111923;
      --line: #2b3948;
      --text: #e7edf4;
      --muted: #9aaaba;
      --ok: #48c684;
      --warn: #f2ae49;
      --down: #ff746c;
      --sync: #6cb7ff;
      --button: #d9e4ef;
      --button-text: #101820;
      --button-secondary-bg: #1b2632;
      --input-bg: #101820;
      --chart-bg: #101820;
      --pre-bg: #070b10;
      --pre-text: #dce7f2;
      --progress-bg: #22303d;
      --shadow: rgba(0,0,0,0.35);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 2;
    }
    h1 { margin: 0; font-size: 20px; font-weight: 700; }
    main {
      padding: 18px 24px 28px;
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 16px;
      width: 100%;
      min-width: 0;
    }
    .toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: flex-end; }
    .tabs {
      display: flex;
      gap: 8px;
      padding: 10px 24px 0;
      background: var(--bg);
    }
    .tab-button {
      background: transparent;
      border-color: transparent;
      color: var(--muted);
      border-radius: 6px 6px 0 0;
    }
    .tab-button.active {
      background: var(--panel);
      border-color: var(--line);
      border-bottom-color: var(--panel);
      color: var(--text);
    }
    .tab-page {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 16px;
      width: 100%;
      min-width: 0;
    }
    .hidden { display: none; }
    .holding-screen {
      position: fixed;
      inset: 0;
      z-index: 50;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 24px;
      background: color-mix(in srgb, var(--bg) 94%, transparent);
    }
    .holding-screen.active {
      display: flex;
    }
    .holding-panel {
      width: min(760px, 100%);
      background: var(--panel);
      border: 1px solid var(--line);
      box-shadow: 0 12px 40px var(--shadow);
      padding: 22px;
    }
    .holding-title {
      margin: 0 0 6px;
      font-size: 24px;
      font-weight: 750;
    }
    .holding-progress {
      width: 100%;
      height: 12px;
      background: var(--progress-bg);
      border: 1px solid var(--line);
      margin: 16px 0 8px;
      overflow: hidden;
    }
    .holding-progress-fill {
      width: 0%;
      height: 100%;
      background: var(--sync);
      transition: width 0.25s ease;
    }
    .holding-grid {
      display: grid;
      grid-template-columns: max-content minmax(0, 1fr);
      gap: 8px 14px;
      margin-top: 16px;
      font-size: 13px;
    }
    .holding-grid .label {
      color: var(--muted);
      font-weight: 700;
    }
    .holding-grid .value {
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 16px; }
    .status-overview {
      display: grid;
      grid-template-columns: minmax(280px, 0.8fr) minmax(0, 2.2fr);
      gap: 16px;
      width: 100%;
      min-width: 0;
      justify-self: stretch;
      align-items: stretch;
    }
    .status-overview.single-card {
      grid-template-columns: minmax(0, 1fr);
    }
    .status-overview.single-card > .panel {
      grid-column: 1 / -1;
    }
    .status-overview.single-card .node-card-grid {
      display: none;
    }
    .status-overview .panel,
    .status-overview .node-card-grid {
      grid-column: auto;
    }
    .status-overview .node-card-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      align-content: start;
      align-self: stretch;
      justify-self: stretch;
      width: 100%;
    }
    .status-overview .node-card-group {
      display: contents;
    }
    .status-overview .node-card-group-title {
      grid-column: 1 / -1;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      min-width: 0;
    }
    .span-2 { grid-column: span 2; }
    .span-3 { grid-column: span 3; }
    .span-4 { grid-column: span 4; }
    .span-6 { grid-column: span 6; }
    .span-8 { grid-column: span 8; }
    .span-12 { grid-column: span 12; }
    .kpi-label { color: var(--muted); font-size: 12px; text-transform: uppercase; font-weight: 700; }
    .stack-summary-row {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 18px;
      min-width: 0;
    }
    .stack-summary-main {
      min-width: 0;
    }
    .height-summary {
      min-width: 170px;
      text-align: right;
    }
    .height-value {
      margin-top: 6px;
      color: var(--text);
      font-size: 32px;
      font-weight: 800;
      font-variant-numeric: tabular-nums;
      line-height: 1;
      white-space: nowrap;
    }
    .stack-endpoint {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 10px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
    }
    .stack-endpoint-value {
      color: var(--text);
      font-variant-numeric: tabular-nums;
      text-transform: none;
      white-space: nowrap;
    }
    .kpi-value {
      margin-top: 8px;
      font-size: 24px;
      font-weight: 750;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .status-reason {
      margin-top: 6px;
      min-height: 17px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .mining-state {
      margin-top: 10px;
      padding: 8px 10px;
      border: 1px solid var(--line);
      background: var(--panel-alt);
      font-size: 13px;
      line-height: 1.35;
    }
    .mining-state .label {
      color: var(--muted);
      font-weight: 750;
      margin-right: 8px;
      text-transform: uppercase;
    }
    .mining-state .value {
      color: var(--text);
      font-weight: 700;
      overflow-wrap: anywhere;
    }
    .mining-state.paused {
      border-color: rgba(201, 90, 0, 0.35);
      background: rgba(201, 90, 0, 0.08);
    }
    .mining-state.ready {
      border-color: rgba(46, 125, 50, 0.35);
      background: rgba(46, 125, 50, 0.08);
    }
    .sampler-alert {
      margin-top: 10px;
      padding: 10px 12px;
      border: 1px solid rgba(201, 90, 0, 0.35);
      border-left: 4px solid var(--warn);
      border-radius: 6px;
      background: rgba(201, 90, 0, 0.08);
      color: var(--text);
      font-size: 13px;
      line-height: 1.4;
    }
    .subtle { color: var(--muted); font-size: 13px; }
    .ok { color: var(--ok); }
    .syncing { color: var(--sync); }
    .down { color: var(--down); }
    .warn { color: var(--warn); }
    table { width: 100%; border-collapse: collapse; table-layout: fixed; }
    th, td { padding: 9px 8px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; font-size: 13px; overflow-wrap: anywhere; }
    th { color: var(--muted); font-size: 12px; text-transform: uppercase; }
    tr:last-child td { border-bottom: 0; }
    button {
      border: 1px solid var(--button);
      background: var(--button);
      color: var(--button-text);
      border-radius: 6px;
      padding: 9px 12px;
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
      min-height: 36px;
    }
    button.secondary { background: var(--button-secondary-bg); color: var(--button); }
    button.danger { background: var(--down); border-color: var(--down); }
    button:disabled { opacity: 0.55; cursor: wait; }
    input {
      border: 1px solid var(--line);
      background: var(--input-bg);
      color: var(--text);
      border-radius: 6px;
      padding: 9px 10px;
      min-height: 36px;
      min-width: 220px;
      font: inherit;
    }
    label { display: grid; gap: 6px; color: var(--muted); font-size: 12px; font-weight: 700; text-transform: uppercase; }
    label input { color: var(--text); font-size: 13px; font-weight: 400; text-transform: none; }
    input[type="checkbox"] { min-width: 0; min-height: 0; width: 16px; height: 16px; padding: 0; }
    .form-grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 12px; align-items: end; }
    .field-span-2 { grid-column: span 2; }
    .field-span-3 { grid-column: span 3; }
    .field-span-4 { grid-column: span 4; }
    .field-span-6 { grid-column: span 6; }
    .button-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; }
    .checkbox-cell { width: 42px; }
    .right { text-align: right; }
    .nowrap { white-space: nowrap; }
    .table-scroll { overflow-x: auto; }
    .wide-table {
      width: max-content;
      min-width: 100%;
      table-layout: auto;
    }
    .equal-column-table {
      width: 100%;
      min-width: 100%;
      table-layout: fixed;
    }
    .equal-column-table th,
    .equal-column-table td,
    .equal-column-table .nowrap {
      white-space: normal;
    }
    .chart-wrap {
      height: 280px;
      margin-top: 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--chart-bg);
      overflow: hidden;
    }
    .chart-head {
      display: flex;
      gap: 12px;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      margin-top: 8px;
    }
    .chart-controls {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .range-button.active,
    .global-range-button.active {
      background: var(--button);
      color: var(--button-text);
    }
    canvas { display: block; width: 100%; height: 100%; }
    .chart-legend { display: flex; gap: 14px; flex-wrap: wrap; margin-top: 8px; }
    .legend-key { display: inline-flex; gap: 6px; align-items: center; color: var(--muted); font-size: 12px; }
    .legend-key::before { content: ""; width: 10px; height: 10px; border-radius: 2px; background: var(--key-color); }
    .miner-row { background: var(--miner-row-color, transparent); }
    .pool-row { background: var(--pool-row-color, transparent); }
    .miner-dot {
      display: inline-block;
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: var(--miner-color);
      margin-right: 8px;
      vertical-align: middle;
    }
    .pool-dot {
      display: inline-block;
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: var(--pool-color);
      margin-right: 8px;
      vertical-align: middle;
    }
    .miner-name,
    .pool-name {
      font-weight: 700;
      color: var(--text);
    }
    pre {
      margin: 8px 0 0;
      padding: 12px;
      background: var(--pre-bg);
      color: var(--pre-text);
      border-radius: 6px;
      overflow: auto;
      max-height: 360px;
      font-size: 12px;
      line-height: 1.45;
    }
    .list { margin: 8px 0 0; padding-left: 18px; }
    .list li { margin: 4px 0; }
    .status-dot {
      display: inline-block;
      width: 9px;
      height: 9px;
      border-radius: 50%;
      margin-right: 6px;
      background: var(--muted);
    }
    .status-dot.ok { background: var(--ok); }
    .status-dot.syncing { background: var(--sync); }
    .status-dot.down { background: var(--down); }
    .sync-progress { margin-top: 12px; }
    .sync-progress-bar {
      height: 12px;
      border-radius: 999px;
      overflow: hidden;
      background: var(--progress-bg);
      border: 1px solid var(--line);
      box-shadow: inset 0 1px 2px var(--shadow);
    }
    .sync-progress-fill {
      height: 100%;
      width: 0%;
      border-radius: inherit;
      background: linear-gradient(90deg, #f5a623, #21a366);
      transition: width 350ms ease;
    }
    .sync-progress-meta {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      margin-top: 6px;
      font-size: 12px;
      color: var(--muted);
      flex-wrap: wrap;
    }
    .sync-narrative {
      margin-top: 10px;
      padding-top: 10px;
      border-top: 1px solid var(--line);
      color: var(--text);
      font-size: 13px;
      line-height: 1.4;
      overflow-wrap: anywhere;
    }
    .sync-detail-list {
      display: grid;
      grid-template-columns: max-content minmax(0, 1fr);
      gap: 5px 12px;
      margin-top: 10px;
      font-size: 12px;
      line-height: 1.35;
    }
    .sync-detail-list .label {
      color: var(--muted);
      font-weight: 700;
      text-transform: uppercase;
    }
    .sync-detail-list .value {
      color: var(--text);
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .sync-progress-node {
      margin-top: 10px;
    }
    .sync-progress-node .sync-progress-bar {
      height: 8px;
    }
    .node-card-grid {
      grid-column: span 12;
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 12px;
      width: 100%;
      min-width: 0;
      justify-self: stretch;
    }
    .node-card-group {
      grid-column: 1 / -1;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 1fr));
      gap: 12px;
      width: 100%;
      min-width: 0;
      justify-self: stretch;
    }
    .node-card-group-title {
      grid-column: 1 / -1;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0;
      text-transform: uppercase;
      color: var(--muted);
    }
    .node-card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      min-width: 0;
      width: 100%;
    }
    .node-card.observer {
      background: var(--panel-alt);
      border-style: dashed;
    }
    .node-card.observer .kpi-label::after {
      content: "not routed";
      display: inline-flex;
      align-items: center;
      margin-left: 6px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 1px 7px;
      font-size: 11px;
      color: var(--muted);
      text-transform: none;
      white-space: nowrap;
    }
    .node-card .kpi-value {
      font-size: 22px;
    }
    .node-card-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
      min-width: 0;
    }
    .node-card-title {
      min-width: 0;
      overflow-wrap: anywhere;
      line-height: 1.35;
    }
    .node-badges {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 6px;
      min-width: 0;
    }
    .node-role {
      display: inline-flex;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 1px 7px;
      margin-left: 6px;
      font-size: 11px;
      color: var(--muted);
      text-transform: capitalize;
      white-space: nowrap;
    }
    .node-badges .node-role {
      margin-left: 0;
    }
    .node-log-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 12px;
      margin-top: 10px;
    }
    .node-log-block {
      min-width: 0;
    }
    .node-log-block pre {
      max-height: 260px;
    }
    @media (max-width: 900px) {
      header { align-items: flex-start; flex-direction: column; }
      .toolbar { justify-content: flex-start; }
      .status-overview { grid-template-columns: minmax(0, 1fr); }
      .status-overview .node-card-grid {
        grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 1fr));
      }
      .span-2, .span-3, .span-4, .span-6, .span-8, .node-card-grid { grid-column: span 12; }
      .field-span-2, .field-span-3, .field-span-4, .field-span-6 { grid-column: span 12; }
      .stack-summary-row { align-items: stretch; }
      .height-summary { min-width: 130px; }
      .height-value { font-size: 26px; }
      main { padding: 14px; }
      .tabs { padding-left: 14px; padding-right: 14px; }
      input { min-width: 100%; }
      input[type="checkbox"] { min-width: 0; }
    }
  </style>
  <script>
    (() => {
      const stored = localStorage.getItem("bdag-dashboard-theme");
      const theme = stored || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      document.documentElement.dataset.theme = theme;
    })();
  </script>
</head>
<body>
  <div id="rebuildHoldingScreen" class="holding-screen" role="status" aria-live="polite" aria-hidden="true">
    <div class="holding-panel">
      <h2 class="holding-title">Rebuilding Dashboard Plot Data</h2>
      <div class="subtle">Global production, wallet 24h earnings, and plot tiers are being rebuilt from the local chain RPC. Existing local ASIC plot rows are preserved by MAC address during upgrades.</div>
      <div class="holding-progress" aria-label="Dashboard plot rebuild progress">
        <div id="rebuildProgressFill" class="holding-progress-fill"></div>
      </div>
      <div id="rebuildProgressText" class="kpi-value">Starting...</div>
      <div class="holding-grid">
        <span class="label">Phase</span><span id="rebuildPhase" class="value">...</span>
        <span class="label">Samples</span><span id="rebuildSamples" class="value">...</span>
        <span class="label">Rows</span><span id="rebuildRows" class="value">...</span>
        <span class="label">Errors</span><span id="rebuildErrors" class="value">...</span>
        <span class="label">Log</span><span id="rebuildLogPath" class="value">...</span>
        <span class="label">State</span><span id="rebuildStatePath" class="value">...</span>
      </div>
    </div>
  </div>
  <header>
    <div>
      <h1>BlockDAG Pool Operations</h1>
      <div class="subtle" id="meta">Loading...</div>
    </div>
    <div class="toolbar">
      <input id="token" type="password" placeholder="Action token">
      <button id="themeToggle" class="secondary" type="button" onclick="toggleTheme()">Dark</button>
      <button class="secondary" onclick="refresh()">Refresh</button>
      <button onclick="action('start')">Start</button>
      <button onclick="action('restart')">Restart</button>
      <button class="danger" onclick="action('clean_restore')">Clean Restore</button>
      <button class="secondary" onclick="action('handoff')">Codex Handoff</button>
    </div>
  </header>
  <nav class="tabs">
    <button id="tabButton-status" class="tab-button active" onclick="showTab('status')">Status: Stack</button>
    <button id="tabButton-miners" class="tab-button" onclick="showTab('miners')">Miners: Local ASICs</button>
    <button id="tabButton-global" class="tab-button" onclick="showTab('global')">Global: Chain Production</button>
    <button id="tabButton-earnings" class="tab-button" onclick="showTab('earnings')">Earnings: Wallet</button>
  </nav>
  <main>
    <section id="tab-status" class="tab-page">
    <section class="status-overview">
      <div class="panel">
        <div class="stack-endpoint">
          <span>Pool Endpoint</span>
          <span class="stack-endpoint-value" id="poolEndpoint">...</span>
        </div>
        <div class="stack-summary-row">
          <div class="stack-summary-main">
            <div class="kpi-label">Stack</div>
            <div class="kpi-value" id="overall">...</div>
          </div>
          <div class="height-summary">
            <div class="kpi-label">Height</div>
            <div class="height-value" id="syncHeight">...</div>
          </div>
        </div>
        <div class="status-reason" id="statusReason"></div>
        <div class="mining-state" id="miningStateBox">
          <span class="label">Mining</span><span class="value" id="syncMiningState">...</span>
        </div>
        <div class="sync-progress">
          <div class="sync-progress-bar" title="Node EVM sync progress">
            <div class="sync-progress-fill" id="syncProgressFill"></div>
          </div>
          <div class="sync-progress-meta">
            <span id="syncProgressPercent">...</span>
            <span id="syncProgressGap">...</span>
          </div>
        </div>
        <div id="syncNarrative" class="sync-narrative"></div>
        <div class="sync-detail-list">
          <span class="label">Mode</span><span class="value" id="syncMode">...</span>
          <span class="label" id="syncActiveLabel">Active</span><span class="value" id="syncActiveNode">...</span>
          <span class="label">Rate</span><span class="value" id="syncRate">...</span>
          <span class="label">ETA</span><span class="value" id="syncEta">...</span>
          <span class="label">Next</span><span class="value" id="syncNextStep">...</span>
        </div>
      </div>
      <div id="nodeCards" class="node-card-grid"></div>
    </section>
    <section class="grid">
      <div class="panel span-8">
        <div class="kpi-label">Containers</div>
        <table>
          <thead><tr><th>Name</th><th>Status</th><th>Image</th><th>Restarts</th></tr></thead>
          <tbody id="containers"></tbody>
        </table>
      </div>
      <div class="panel span-4">
        <div class="kpi-label">Alerts</div>
        <ul class="list" id="alerts"></ul>
      </div>
    </section>
    <section class="grid">
      <div class="panel span-12">
        <div class="kpi-label">Node Logs</div>
        <div id="nodeLogsGrid" class="node-log-grid"></div>
      </div>
    </section>
    <section class="grid">
      <div class="panel span-6">
        <div class="kpi-label">Pool</div>
        <div id="poolSummary" class="subtle"></div>
        <pre id="poolLog"></pre>
      </div>
      <div class="panel span-6">
        <div class="kpi-label">Latest Action</div>
        <pre id="actionLog"></pre>
      </div>
    </section>
    </section>
    <section id="tab-miners" class="tab-page hidden">
      <section class="grid">
        <div class="panel span-12">
          <div class="kpi-label">Active Miner Lanes</div>
          <div id="minerHealthSummary" class="subtle" style="margin-top: 8px;"></div>
          <div class="table-scroll">
          <table class="wide-table">
            <thead><tr><th class="nowrap">Miner</th><th class="nowrap">Type</th><th>Status</th><th>Configured</th><th>Connected</th><th class="nowrap">Workers</th><th class="right">Shares</th><th class="right">Work %</th><th class="right">Expected %</th><th class="nowrap">Lane</th><th class="right">Work</th><th class="right">Found Blocks</th><th>Last Share</th><th>Issue</th></tr></thead>
            <tbody id="managedMinersTable"></tbody>
          </table>
          </div>
        </div>
      </section>
      <section class="grid">
        <div class="panel span-12">
          <div class="kpi-label">Miner Performance Trend</div>
          <div class="subtle" id="minerWorkChartMetricSummary" style="margin-top: 8px;">Accepted work percentage by miner</div>
          <div class="chart-head">
            <div class="chart-controls">
              <button class="secondary range-button miner-work-range-button active" data-range="1" onclick="setMinerWorkChartRange(1)">1h</button>
              <button class="secondary range-button miner-work-range-button" data-range="4" onclick="setMinerWorkChartRange(4)">4h</button>
              <button class="secondary range-button miner-work-range-button" data-range="12" onclick="setMinerWorkChartRange(12)">12h</button>
              <button class="secondary range-button miner-work-range-button" data-range="24" onclick="setMinerWorkChartRange(24)">24h</button>
              <button class="secondary range-button miner-work-range-button" data-range="72" onclick="setMinerWorkChartRange(72)">3d</button>
              <button class="secondary range-button miner-work-range-button" data-range="168" onclick="setMinerWorkChartRange(168)">Week</button>
              <button class="secondary range-button miner-work-range-button" data-range="720" onclick="setMinerWorkChartRange(720)">Month</button>
              <button class="secondary range-button miner-work-metric-button active" data-metric="work" onclick="setMinerWorkChartMetric('work')">Work %</button>
              <button class="secondary range-button miner-work-metric-button" data-metric="blocks" onclick="setMinerWorkChartMetric('blocks')">Blocks</button>
              <button class="secondary range-button miner-work-metric-button" data-metric="hashrate" onclick="setMinerWorkChartMetric('hashrate')">Hashrate</button>
            </div>
            <div class="subtle" id="minerWorkChartRangeLabel"></div>
          </div>
          <div id="minerWorkSamplerAlert" class="sampler-alert hidden"></div>
          <div class="chart-wrap"><canvas id="minerWorkChart"></canvas></div>
          <div class="chart-legend" id="minerWorkChartLegend"></div>
        </div>
      </section>
      <section class="grid">
        <div class="panel span-12">
          <div class="kpi-label">LAN Miner Configuration</div>
          <div class="form-grid" style="margin-top: 12px;">
            <label class="field-span-3">Scan Target
              <input id="minerScanTarget" placeholder="192.168.1.0/24">
            </label>
            <label class="field-span-3">Pool URL
              <input id="minerPoolUrl" placeholder="stratum+tcp://POOL_LAN_IP:3334">
            </label>
            <label class="field-span-3">Worker / Wallet
              <input id="minerWorkerUser" placeholder="0x...">
            </label>
            <label class="field-span-2">Pool Password
              <input id="minerPoolPassword" value="1234">
            </label>
            <label class="field-span-3">Admin Password
              <input id="minerAdminPassword" type="password" autocomplete="off" placeholder="ASIC admin password">
            </label>
            <div class="field-span-6 button-row">
              <button onclick="scanMinerLan()">Scan LAN</button>
              <button class="secondary" onclick="selectAllMiners(true)">Select All</button>
              <button class="secondary" onclick="selectAllMiners(false)">Clear</button>
              <button onclick="configureSelectedMiners()">Configure Selected</button>
              <button class="secondary" onclick="saveMinerAuth()">Save Password For Watchdog</button>
            </div>
          </div>
          <div class="subtle" style="margin-top: 10px;">Scans are limited to private LAN IPv4 targets. Existing miner pool lists are backed up before changes.</div>
        </div>
      </section>
      <section class="grid">
        <div class="panel span-12">
          <div class="kpi-label">Discovered Miners</div>
          <table>
            <thead><tr><th class="checkbox-cell"></th><th>Miner</th><th>Model</th><th>Firmware</th><th>Current Pool</th><th>Active</th><th>Result</th></tr></thead>
            <tbody id="minersTable"></tbody>
          </table>
          <pre id="minersOutput">No scan has run yet.</pre>
        </div>
      </section>
    </section>
    <section id="tab-global" class="tab-page hidden">
      <section class="grid">
        <div class="panel span-2">
          <div class="kpi-label">Latest Block</div>
          <div class="kpi-value" id="globalLatestBlock">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">Scanned Blocks</div>
          <div class="kpi-value" id="globalScannedBlocks">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">Unique Miners</div>
          <div class="kpi-value" id="globalUniqueMiners">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">Scan Window</div>
          <div class="kpi-value" id="globalScanWindow">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">Avg Block Sec</div>
          <div class="kpi-value" id="globalAvgBlockSec">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">Top Share</div>
          <div class="kpi-value" id="globalTopShare">...</div>
        </div>
      </section>
      <section class="grid">
        <div class="panel span-12">
          <div class="kpi-label">Confirmed Chain Production By Pool</div>
          <div class="subtle" style="margin-top: 8px;">Pool rows use chain-confirmed production over the displayed Scan Window.</div>
          <div class="subtle" id="globalTableWindow" style="margin-top: 8px;">Table period: waiting for scan window.</div>
          <div class="subtle" id="globalSourceStatus" style="margin-top: 8px;">Waiting for chain RPC source details.</div>
          <div class="table-scroll" style="margin-top: 12px;">
            <table class="wide-table equal-column-table">
              <thead><tr><th class="nowrap">Pool</th><th class="right">Chain Blocks In Window</th><th class="right">Work %</th><th class="right">Reward BDAG</th><th class="right">Est. Wallet BDAG</th><th class="right">USD Total</th><th class="right">ZAR Total</th><th>Last Seen</th></tr></thead>
              <tbody id="globalPoolsTable"></tbody>
            </table>
          </div>
          <div class="subtle" style="margin-top: 10px;">Credit-block and found-block duplicates are intentionally hidden; chain blocks remain separate when local pool shares differ from chain-confirmed production.</div>
        </div>
      </section>
      <section class="grid">
        <div class="panel span-12">
          <div class="kpi-label">Pool Earnings Trend</div>
          <div class="subtle" id="globalChartMetricSummary" style="margin-top: 8px;">USD per pool per hour</div>
          <div class="chart-head">
            <div class="chart-controls">
              <button class="secondary global-range-button active" data-range="1" onclick="setGlobalChartRange(1)">1h</button>
              <button class="secondary global-range-button" data-range="4" onclick="setGlobalChartRange(4)">4h</button>
              <button class="secondary global-range-button" data-range="12" onclick="setGlobalChartRange(12)">12h</button>
              <button class="secondary global-range-button" data-range="24" onclick="setGlobalChartRange(24)">24h</button>
              <button class="secondary global-range-button" data-range="72" onclick="setGlobalChartRange(72)">3d</button>
              <button class="secondary global-range-button" data-range="168" onclick="setGlobalChartRange(168)">Week</button>
              <button class="secondary global-range-button" data-range="720" onclick="setGlobalChartRange(720)">Month</button>
              <button class="secondary range-button global-metric-button active" data-metric="usd" onclick="setGlobalChartMetric('usd')">USD/h</button>
              <button class="secondary range-button global-metric-button" data-metric="blocks" onclick="setGlobalChartMetric('blocks')">Blocks/h</button>
            </div>
            <div class="subtle" id="globalChartRangeLabel"></div>
          </div>
          <div class="chart-wrap"><canvas id="globalChart"></canvas></div>
          <div class="chart-legend" id="globalChartLegend"></div>
        </div>
      </section>
      <section class="grid">
        <div class="panel span-12">
          <div class="kpi-label">Observed Peer IPs</div>
          <div class="subtle" style="margin-top: 8px;">These are public P2P peers seen on the node sockets, geolocated by IP. They may be relays, VPS hosts, or NAT gateways rather than the physical miners.</div>
          <div class="table-scroll" style="margin-top: 12px;">
            <table class="wide-table">
              <thead><tr><th class="nowrap">IP</th><th>Guessed Location</th><th>Country</th><th>Region</th><th>City</th><th>ASN</th><th>Org</th><th class="right">Seen By</th></tr></thead>
              <tbody id="globalPeerIpsTable"></tbody>
            </table>
          </div>
        </div>
      </section>
    </section>
    <section id="tab-earnings" class="tab-page hidden">
      <section class="grid">
        <div class="panel span-2">
          <div class="kpi-label">Current Price ZAR</div>
          <div class="kpi-value" id="earnCurrentPriceZar">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">24h Avg BDAG/h</div>
          <div class="kpi-value" id="earnWalletAvgBdagHour">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">Recent Earned BDAG/h</div>
          <div class="kpi-value" id="earnWalletRecentBdagHour">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">24h Earned ZAR</div>
          <div class="kpi-value" id="earnWallet24hZar">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">24h Earned USD</div>
          <div class="kpi-value" id="earnWallet24hUsd">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">24h Earned BDAG</div>
          <div class="kpi-value" id="earnWallet24hBdag">...</div>
        </div>
      </section>
      <section class="grid">
        <div class="panel span-2">
          <div class="kpi-label">Current Price USD</div>
          <div class="kpi-value" id="earnCurrentPriceUsd">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">Avg Income USD/h</div>
          <div class="kpi-value" id="earnAvgIncomeUsdHour">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">Avg Income BDAG/h</div>
          <div class="kpi-value" id="earnAvgIncomeBdagHour">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">Payment Wallet ZAR</div>
          <div class="kpi-value" id="earnTotalZar">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">Payment Wallet USD</div>
          <div class="kpi-value" id="earnTotalUsd">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">Payment Wallet BDAG</div>
          <div class="kpi-value" id="earnWalletBdag">...</div>
        </div>
      </section>
      <section class="grid">
        <div class="panel span-12">
          <div class="kpi-label">Estimated Earnings By Miner</div>
          <div class="table-scroll">
            <table class="wide-table">
              <thead><tr><th class="nowrap">Miner</th><th class="nowrap">Workers</th><th class="right">Shares</th><th class="right">Work %</th><th class="right">Credit Blocks</th><th class="right">Credited BDAG</th><th class="right">Found Blocks</th><th class="right">Est. Wallet BDAG</th><th class="right">Wallet Recent BDAG/h</th><th class="right">Wallet Avg BDAG/h</th><th class="right">USD Total</th><th class="right">ZAR Total</th><th>Last Share</th></tr></thead>
              <tbody id="minerEarningsTable"></tbody>
            </table>
          </div>
          <div class="subtle" style="margin-top: 10px;">Per-miner wallet BDAG is estimated from accepted share work because rewards land at the wallet/worker address, not directly against each ASIC IP. Worker credits shared across miners.</div>
        </div>
      </section>
      <section class="grid">
        <div class="panel span-12">
          <div class="kpi-label">Miner Earnings Trend</div>
          <div class="subtle" id="earningsChartUnitSummary" style="margin-top: 8px;">USD per miner per hour</div>
          <div class="chart-head">
            <div class="chart-controls">
              <button class="secondary range-button earnings-range-button active" data-range="1" onclick="setEarningsChartRange(1)">1h</button>
              <button class="secondary range-button earnings-range-button" data-range="4" onclick="setEarningsChartRange(4)">4h</button>
              <button class="secondary range-button earnings-range-button" data-range="12" onclick="setEarningsChartRange(12)">12h</button>
              <button class="secondary range-button earnings-range-button" data-range="24" onclick="setEarningsChartRange(24)">24h</button>
              <button class="secondary range-button earnings-range-button" data-range="72" onclick="setEarningsChartRange(72)">3d</button>
              <button class="secondary range-button earnings-range-button" data-range="168" onclick="setEarningsChartRange(168)">Week</button>
              <button class="secondary range-button earnings-range-button" data-range="720" onclick="setEarningsChartRange(720)">Month</button>
              <button class="secondary range-button earnings-unit-button" data-unit="bdag" onclick="setEarningsChartUnit('bdag')">BDAG</button>
              <button class="secondary range-button earnings-unit-button active" data-unit="usd" onclick="setEarningsChartUnit('usd')">USD</button>
              <button class="secondary range-button earnings-unit-button" data-unit="zar" onclick="setEarningsChartUnit('zar')">ZAR</button>
            </div>
            <div class="subtle" id="earningsChartRangeLabel"></div>
          </div>
          <div id="earningsSamplerAlert" class="sampler-alert hidden"></div>
          <div class="chart-wrap"><canvas id="earningsChart"></canvas></div>
          <div class="chart-legend" id="earningsChartLegend"></div>
        </div>
      </section>
      <section class="grid">
        <div class="panel span-6">
          <div class="kpi-label">Address Credits</div>
          <table>
            <thead><tr><th>Address</th><th class="right">Credits</th><th class="right">Total BDAG</th><th class="right">Pending</th><th>Last Credit</th></tr></thead>
            <tbody id="addressCreditsTable"></tbody>
          </table>
        </div>
        <div class="panel span-6">
          <div class="kpi-label">Payment Wallet Cross-Check</div>
          <table>
            <thead><tr><th>Source</th><th>Status</th><th class="right">BDAG</th><th>Detail</th></tr></thead>
            <tbody id="walletSourcesTable"></tbody>
          </table>
        </div>
      </section>
      <section class="grid">
        <div class="panel span-6">
          <div class="kpi-label">Price Feed</div>
          <pre id="priceFeedOutput"></pre>
        </div>
        <div class="panel span-6">
          <div class="kpi-label">Earnings Snapshot Log</div>
          <pre id="earningsHistoryOutput"></pre>
        </div>
      </section>
    </section>
  </main>
  <script>
    let busy = false;
    let miners = [];
    let minerResults = {};
    let minerDefaultsLoaded = false;
    let earningsLoaded = false;
    let lastEarningsData = null;
    let earningsRefreshInFlight = false;
    let globalLoaded = false;
    let lastGlobalData = null;
    let globalRefreshInFlight = false;
    let rebuildPollTimer = null;
    const defaultServiceOrder = ["postgres", "node", "pool"];
    function text(id, value) { document.getElementById(id).textContent = value ?? ""; }
    function currentTheme() {
      return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
    }
    function setTheme(theme) {
      const normalized = theme === "dark" ? "dark" : "light";
      document.documentElement.dataset.theme = normalized;
      localStorage.setItem("bdag-dashboard-theme", normalized);
      const button = document.getElementById("themeToggle");
      if (button) {
        button.textContent = normalized === "dark" ? "Light" : "Dark";
        button.setAttribute("aria-pressed", normalized === "dark" ? "true" : "false");
      }
    }
    function toggleTheme() {
      setTheme(currentTheme() === "dark" ? "light" : "dark");
    }
    function fmt(value) { return value === null || value === undefined ? "n/a" : value.toLocaleString ? value.toLocaleString() : value; }
    function hasValue(value) { return value !== null && value !== undefined && value !== ""; }
    function firstPresent(...values) {
      for (const value of values) {
        if (hasValue(value)) return value;
      }
      return null;
    }
    function metricEnabled(value) {
      if (value === true || value === false) return value;
      const numeric = Number(value);
      if (Number.isFinite(numeric)) return numeric > 0;
      const textValue = String(value ?? "").toLowerCase();
      return ["true", "yes", "on", "enabled"].includes(textValue);
    }
    function templateBackendStates(data) {
      const metrics = data.pool_metrics || {};
      const rawState = metrics.template_backend_state || {};
      return Array.isArray(rawState.pools)
        ? rawState.pools
        : (rawState.fan_in || rawState.backends ? [rawState] : []);
    }
    function firstTemplateBackendState(data) {
      return templateBackendStates(data)[0] || {};
    }
    function backendKeyForNode(name) {
      const match = String(name || "").match(/(?:^|-)node-(\d+)$/) || String(name || "").match(/^node(\d+)$/);
      return match ? `node${match[1]}` : String(name || "");
    }
    function backendInfoForNode(name, backends) {
      const key = backendKeyForNode(name);
      return backends?.[name] || backends?.[key] || null;
    }
    function nodeRole(name, node, data) {
      if (node?.role) return String(node.role);
      const observers = data?.observer_node_services || [];
      return observers.includes(name) ? "observer" : "managed";
    }
    function nodeHealthScope(role) {
      return role === "observer" ? "advisory" : "production";
    }
    function templateBackendStatusText(data) {
      const metrics = data.pool_metrics || {};
      const state = firstTemplateBackendState(data);
      const parts = [];

      const backends = state.backends || {};
      const backendNames = Object.keys(backends).sort();
      if (backendNames.length) {
        const healthy = backendNames.filter(name => metricEnabled(backends[name]?.healthy)).length;
        const wsBackends = backendNames.filter(name => metricEnabled(backends[name]?.ws_connected));
        parts.push(`template_backends=${healthy}/${backendNames.length}`);
        if (wsBackends.length) parts.push(`template_ws=${wsBackends.join(",")}`);
      } else {
        const probeNodes = data.rpc_template_health?.nodes || {};
        const probeNames = Object.keys(probeNodes).sort();
        if (probeNames.length) {
          const healthy = probeNames.filter(name => !probeNodes[name]?.failing).length;
          parts.push(`template_probes=${healthy}/${probeNames.length}`);
        }
      }
      return parts.join(" ");
    }
    function selectedBackendSourceHealth(data) {
      const poolHealth = data.pool_health || {};
      const metrics = data.pool_metrics || {};
      return poolHealth.selected_backend_source_health || metrics.selected_backend_source_health || {};
    }
    function selectedBackendTemplateReady(data) {
      const selected = selectedBackendSourceHealth(data);
      const readinessKeys = ["healthy", "node_mineable", "node_submit_ready", "node_p2p_mining_fresh"];
      const hasReadinessSignal = readinessKeys.some((key) => hasValue(selected[key]));
      if (!hasReadinessSignal) return true;
      return readinessKeys.every((key) => !hasValue(selected[key]) || metricEnabled(selected[key]));
    }
    function sourceHealthStatusText(data) {
      const poolHealth = data.pool_health || {};
      const metrics = data.pool_metrics || {};
      const jobHealth = poolHealth.source_job_health || metrics.source_job_health || {};
      const selected = poolHealth.selected_backend_source_health || metrics.selected_backend_source_health || {};
      const contract = poolHealth.selected_backend_readiness_contract || {};
      const parts = [];
      if (hasValue(jobHealth.ok)) parts.push(`job_health=${metricEnabled(jobHealth.ok) ? "ok" : "degraded"}`);
      const sourceFlags = [];
      if (hasValue(selected.node_mineable)) sourceFlags.push(`mineable=${metricEnabled(selected.node_mineable) ? "yes" : "no"}`);
      if (hasValue(selected.node_submit_ready)) sourceFlags.push(`submit=${metricEnabled(selected.node_submit_ready) ? "ready" : "not-ready"}`);
      if (hasValue(selected.node_p2p_mining_fresh)) sourceFlags.push(`p2p=${metricEnabled(selected.node_p2p_mining_fresh) ? "fresh" : "stale"}`);
      if (hasValue(selected.node_template_age_seconds)) sourceFlags.push(`node_template_age=${fmt(selected.node_template_age_seconds)}s`);
      if (sourceFlags.length) parts.push(`source_health=${sourceFlags.join("/")}`);
      if (poolHealth.source_selected_backend_hard_degraded) parts.push("source_fault=hard");
      else if (poolHealth.source_health_transient_degraded) parts.push("source_fault=advisory");
      if (contract.contradiction) parts.push("readiness=contradiction");
      return parts.join(" ");
    }
    function lossLedgerStatusText(data) {
      const poolHealth = data.pool_health || {};
      const metrics = data.pool_metrics || {};
      const ledger = poolHealth.loss_ledger || metrics.loss_ledger || {};
      const block = ledger.block_outcomes || {};
      const share = ledger.share_outcomes || {};
      const topLoss = (ledger.top_loss_reasons || [])[0] || {};
      const parts = [];
      if (ledger.severity) parts.push(`efficiency=${ledger.severity}`);
      if (hasValue(block.accepted_ratio_percent)) parts.push(`block_accept=${fmt(block.accepted_ratio_percent)}%`);
      if (hasValue(block.loss_ratio_percent)) parts.push(`block_loss=${fmt(block.loss_ratio_percent)}%`);
      if (hasValue(share.accepted_ratio_percent)) parts.push(`share_accept=${fmt(share.accepted_ratio_percent)}%`);
      if (topLoss.reason) {
        const ratio = hasValue(topLoss.ratio_percent) ? ` ${fmt(topLoss.ratio_percent)}%` : "";
        parts.push(`top_loss=${topLoss.plane || "pool"}:${topLoss.reason}${ratio}`);
      }
      return parts.join(" ");
    }
    function statusClass(overall) { return overall === "ok" ? "ok" : overall === "syncing" ? "syncing" : "down"; }
    function poolRunning(data) {
      const containers = data.containers || {};
      const names = data.pool_containers || [data.pool_container || "pool"];
      for (const name of names) {
        if (containers[name]?.running) return true;
      }
      if (hasValue(data.catchup_policy?.pool_running)) return Boolean(data.catchup_policy.pool_running);
      const metricsStatus = data.pool_health?.metrics?.status || data.pool_metrics?.status;
      return metricsStatus === "ok";
    }
    function connectedMinerCount(data) {
      const health = data.miner_health || {};
      const sourceHealth = data.pool_health?.source_job_health || data.pool_metrics?.source_job_health || {};
      return Number(firstPresent(
        health.connected_count_effective,
        health.connected_count,
        sourceHealth.ready_miners,
        sourceHealth.authorized_miners,
        data.pool_health?.connected_miners,
        0
      )) || 0;
    }
    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
    }
    function shortEth(value) {
      return String(value ?? "").replace(/0x[a-fA-F0-9]{40}/g, match => `${match.slice(0, 6)}...${match.slice(-4)}`);
    }
    function escapeShortEth(value) {
      return escapeHtml(shortEth(value));
    }
    const globalPoolNames = {};
    function globalPoolName(address) {
      return globalPoolNames[String(address || "").toLowerCase()] || "";
    }
    function globalPoolLabel(row) {
      const address = row?.address || row?.address_short || "";
      const name = row?.pool_name || globalPoolName(address);
      return name ? `${name} (${shortEth(address)})` : shortEth(address);
    }
    function globalNodesLabel(row) {
      if (Array.isArray(row?.nodes) && row.nodes.length) return row.nodes.join(", ");
      if (row?.local_pool) return "local pool";
      const name = row?.pool_name || globalPoolName(row?.address);
      if (name) return name;
      return (row?.rpc_sources || []).join(", ");
    }
    function showTab(name) {
      for (const page of document.querySelectorAll(".tab-page")) page.classList.add("hidden");
      for (const button of document.querySelectorAll(".tab-button")) button.classList.remove("active");
      document.getElementById("tab-" + name).classList.remove("hidden");
      document.getElementById("tabButton-" + name).classList.add("active");
      if (name === "earnings") refreshEarnings();
      if (name === "miners") refreshEarnings();
      if (name === "global") refreshGlobal();
    }
    function scheduleRebuildPoll(active) {
      if (!active && rebuildPollTimer) {
        clearTimeout(rebuildPollTimer);
        rebuildPollTimer = null;
        return;
      }
      if (!active || rebuildPollTimer) return;
      rebuildPollTimer = setTimeout(() => {
        rebuildPollTimer = null;
        refresh();
      }, 5000);
    }
    function renderRebuildHoldingScreen(state) {
      const active = Boolean(state?.active);
      const screen = document.getElementById("rebuildHoldingScreen");
      if (!screen) return active;
      screen.classList.toggle("active", active);
      screen.setAttribute("aria-hidden", active ? "false" : "true");
      scheduleRebuildPoll(active);
      if (!active) return false;
      const progress = firstNumeric(state.percent);
      const done = firstNumeric(state.progress);
      const total = firstNumeric(state.total);
      const computedPercent = progress !== null ? progress : (done !== null && total ? (done / total) * 100 : null);
      const bounded = computedPercent === null ? 0 : Math.max(0, Math.min(100, computedPercent));
      const fill = document.getElementById("rebuildProgressFill");
      if (fill) fill.style.width = `${bounded.toFixed(1)}%`;
      text("rebuildProgressText", computedPercent === null ? "Planning rebuild..." : `${bounded.toFixed(1)}%`);
      text("rebuildPhase", state.phase || "running");
      const sampleBits = [];
      if (done !== null || total !== null) sampleBits.push(`${fmt(done ?? 0)} / ${fmt(total ?? 0)} headers`);
      if (state.sample_count !== undefined) sampleBits.push(`${fmt(state.sample_count)} dashboard samples`);
      text("rebuildSamples", sampleBits.join(" | ") || "planning sample windows");
      const preserved = state.preserved_asic_history || {};
      const wallet24h = state.wallet_24h_rebuild || {};
      const macs = Array.isArray(preserved.preserved_macs) ? preserved.preserved_macs : [];
      const rowBits = [];
      if (state.global_rows !== undefined) rowBits.push(`Global ${fmt(state.global_rows)}`);
      if (state.earnings_rows !== undefined) rowBits.push(`Wallet/ASIC ${fmt(state.earnings_rows)}`);
      if (wallet24h.annotated_rows !== undefined) rowBits.push(`24h earnings ${fmt(wallet24h.annotated_rows)}`);
      if (preserved.preserved_mac_count !== undefined) rowBits.push(`ASIC MACs preserved ${fmt(preserved.preserved_mac_count)}`);
      if (macs.length) rowBits.push(`MAC ${macs.slice(0, 8).join(", ")}${macs.length > 8 ? ` +${macs.length - 8}` : ""}`);
      text("rebuildRows", rowBits.join(" | ") || "rebuilding chain-derived rows; preserving local ASIC plot history by MAC");
      text("rebuildErrors", state.error || `fetch errors ${fmt(state.errors || 0)}${state.partial_samples !== undefined ? ` | partial samples ${fmt(state.partial_samples)}` : ""}`);
      text("rebuildLogPath", state.log_file || "runtime rebuild log pending");
      text("rebuildStatePath", state.path || "");
      return true;
    }
    async function refresh() {
      try {
        const response = await fetch("/api/status", {cache: "no-store"});
        const data = await response.json();
        render(data);
      } catch (error) {
        text("meta", "Dashboard API unavailable: " + error);
        text("overall", "down");
        text("statusReason", "Dashboard API unavailable.");
        document.getElementById("overall").className = "kpi-value down";
      }
    }
    function render(data) {
      renderRebuildHoldingScreen(data.dashboard_plot_rebuild || {});
      text("meta", data.generated_at + " | " + data.project_root + " | dashboard " + (data.dashboard_url || "unknown"));
      text("overall", data.overall);
      const catchupPolicy = data.catchup_policy || {};
      const syncProgress = data.sync_progress || {};
      const templateReady = selectedBackendTemplateReady(data);
      text(
        "statusReason",
        catchupPolicy.active
          ? (catchupPolicy.summary || catchupPolicy.user_message || "Catch-up pause active.")
          : (data.overall === "ok"
            ? (syncProgress.status === "synced"
              ? (templateReady
                ? ""
                : "Node sync is complete, but the selected backend's template checks are not yet healthy.")
              : `Node is ${syncProgress.status || "syncing"}; the pool is not mining until sync completes.`)
            : (data.status_reason || "Reason unavailable."))
      );
      document.getElementById("overall").className = "kpi-value " + statusClass(data.overall);
      const nodeNames = data.node_services || Object.keys(data.nodes || {});
      renderSyncProgress(data.sync_progress || {}, data);
      renderNodeCards(nodeNames, data.nodes || {}, data.sync_progress || {}, data);
      text("poolEndpoint", data.pool_endpoint || `127.0.0.1:${data.pool_port || "3334"}`);
      hydrateMinerDefaults(data);
      const tbody = document.getElementById("containers");
      tbody.innerHTML = "";
      const serviceOrder = data.stack_services || defaultServiceOrder;
      const extraServices = Object.keys(data.containers || {}).filter(name => !serviceOrder.includes(name));
      for (const name of [...serviceOrder, ...extraServices]) {
        const info = data.containers[name] || {};
        const tr = document.createElement("tr");
        const cls = info.running ? "ok" : "down";
        tr.innerHTML = `<td>${name}</td><td><span class="status-dot ${cls}"></span>${info.status || "missing"}</td><td>${info.image || ""}</td><td>${info.restart_count ?? ""}</td>`;
        tbody.appendChild(tr);
      }
      const alerts = document.getElementById("alerts");
      alerts.innerHTML = "";
      const messages = [...(data.failures || []), ...(data.warnings || [])];
      if (messages.length === 0) messages.push("No active alerts.");
      for (const message of messages) {
        const li = document.createElement("li");
        li.textContent = message;
        alerts.appendChild(li);
      }
      renderNodeLogs(nodeNames, data.nodes || {}, data);
      const poolHealth = data.pool_health || {};
      const submitRecovery = poolHealth.submit_stall_self_healed_recently
        ? `submit_recovery=self-healed accepted_age=${fmt(poolHealth.last_block_submit_age_seconds)}s`
        : (poolHealth.submit_stall_recovery_recent
          ? `submit_recovery=active recovery_age=${fmt(poolHealth.submit_stall_last_recovery_age_seconds)}s`
          : "submit_recovery=idle");
      const selectedBackend = poolHealth.selected_backend || data.pool_metrics?.selected_backend || "unknown";
      const templateBackendStatus = templateBackendStatusText(data);
      const sourceHealthStatus = sourceHealthStatusText(data);
      const lossLedgerStatus = lossLedgerStatusText(data);
      const hostPressureStatus = hostPressureText(data.host_pressure || {});
      const rpcRefusedStatus = data.pool?.rpc_refused_recent
        ? "recent"
        : (data.pool?.rpc_refused ? `stale age=${fmt(data.pool.last_rpc_refused_age_seconds)}s` : "false");
      text(
        "poolSummary",
        `endpoint=${data.pool_endpoint || "unknown"} local_ips=${(data.local_ips || []).join(", ") || "none"} `
        + `initial_download=${data.pool.initial_download} gbt_errors=${data.pool.gbt_errors} rpc_refused=${rpcRefusedStatus} `
        + `valid_shares=${fmt(poolHealth.valid_share_count)} stale_submits=${fmt(poolHealth.stale_submit_count)} `
        + `stale_jobs=${fmt(poolHealth.stale_job_candidate_count)} submit_errors=${fmt(poolHealth.block_submit_error_count)} `
        + `duplicate_blocks=${fmt(poolHealth.duplicate_block_count)} `
        + `last_valid_share_age=${fmt(poolHealth.last_valid_share_age_seconds)}s share_stall=${poolHealth.share_stall ? "yes" : "no"} `
        + `selected_backend=${selectedBackend}${templateBackendStatus ? ` ${templateBackendStatus}` : ""}`
        + `${sourceHealthStatus ? ` ${sourceHealthStatus}` : ""}`
        + `${lossLedgerStatus ? ` ${lossLedgerStatus}` : ""} ${submitRecovery}`
        + `${hostPressureStatus ? ` ${hostPressureStatus}` : ""}`
      );
      text("poolLog", (data.pool.tail || []).join("\n"));
      text("actionLog", data.latest_action ? JSON.stringify(data.latest_action, null, 2) : "No action has run yet.");
      renderManagedMiners(data.miner_health || {});
    }
    function syncProgressText(progress) {
      const percentValue = Number(progress.percent);
      const hasPercent = Number.isFinite(percentValue);
      const bounded = hasPercent ? Math.max(0, Math.min(100, percentValue)) : 0;
      if (progress.status === "synced") return `${bounded.toFixed(2)}% synced`;
      const remaining = Number(progress.remaining_blocks);
      const displayPercent = progress.status === "syncing" && Number.isFinite(remaining) && remaining > 0 && bounded >= 100
        ? 99.99
        : bounded;
      return hasPercent ? `${displayPercent.toFixed(2)}% ${progress.status || ""}` : `sync ${progress.status || "unknown"}`;
    }
    function syncGapText(progress) {
      if (progress.status === "synced") return "gap 0 blocks";
      if (progress.remaining_blocks !== null && progress.remaining_blocks !== undefined) {
        return `gap ${fmt(progress.remaining_blocks)} blocks`
          + (progress.current_block && progress.highest_block ? ` (${fmt(progress.current_block)} / ${fmt(progress.highest_block)})` : "");
      }
      return progress.error || "sync progress unavailable";
    }
    function durationText(seconds) {
      const value = Number(seconds);
      if (!Number.isFinite(value) || value < 0) return "estimating";
      if (value < 60) return "<1m";
      const minutes = Math.round(value / 60);
      if (minutes < 60) return `${minutes}m`;
      const hours = Math.floor(minutes / 60);
      const mins = minutes % 60;
      if (hours < 24) return mins ? `${hours}h ${mins}m` : `${hours}h`;
      const days = Math.floor(hours / 24);
      const remHours = hours % 24;
      return remHours ? `${days}d ${remHours}h` : `${days}d`;
    }
    function etaText(seconds, at) {
      const parsed = Number(seconds);
      if (!Number.isFinite(parsed) || parsed <= 0) return "estimating after the next progress sample";
      return `about ${durationText(parsed)}${at ? `, around ${formatDisplayTime(at)}` : ""}`;
    }
    function syncRateText(estimate) {
      const rate = Number(estimate?.rate_blocks_per_second);
      if (!Number.isFinite(rate) || rate <= 0) return "estimating from the next sample";
      const source = estimate.rate_source ? ` (${estimate.rate_source})` : "";
      return `${rate.toFixed(rate >= 10 ? 1 : 2)} blocks/s${source}`;
    }
    function hostPressureText(host) {
      const parts = [];
      if (hasValue(host.loadavg_1m)) parts.push(`load1=${Number(host.loadavg_1m).toFixed(2)}`);
      if (hasValue(host.io_some_avg10)) parts.push(`io_some10=${Number(host.io_some_avg10).toFixed(2)}%`);
      if (hasValue(host.io_full_avg10)) parts.push(`io_full10=${Number(host.io_full_avg10).toFixed(2)}%`);
      if (hasValue(host.iowait_percent)) parts.push(`iowait=${Number(host.iowait_percent).toFixed(2)}%`);
      if (hasValue(host.cpu_busy_percent)) parts.push(`cpu_busy=${Number(host.cpu_busy_percent).toFixed(2)}%`);
      if (host.iowait_warning_active) parts.push("io_wait=sustained");
      if (hasValue(host.cpu_some_avg10)) parts.push(`cpu_some10=${Number(host.cpu_some_avg10).toFixed(2)}%`);
      return parts.length ? `host_pressure ${parts.join(" ")}` : "";
    }
    function renderSyncEstimate(data, progress) {
      const estimate = data.sync_estimate || {};
      const templateReady = selectedBackendTemplateReady(data);
      if (data.mode === "status_cache_unavailable" || data.collector_budget_exceeded && !(data.sync_progress || {}).status) {
        text("syncNarrative", "Status sampler is unavailable; waiting for a fresh stack sample.");
        text("syncMode", "unknown");
        const activeLabel = document.getElementById("syncActiveLabel");
        const activeValue = document.getElementById("syncActiveNode");
        if (activeLabel) activeLabel.classList.add("hidden");
        if (activeValue) activeValue.classList.add("hidden");
        text("syncHeight", "n/a");
        text("syncRate", "estimating from the next sample");
        text("syncEta", "estimating after the next progress sample");
        text("syncNextStep", "restore status sampling so dashboard readiness can be evaluated");
        return;
      }
      const leader = estimate.leader || data.sync_health?.planned_pause_leader || "";
      const leaderNode = estimate.nodes?.[leader] || {};
      const managedNodes = data.managed_node_services || [];
      const singleManagedNode = managedNodes.length === 1;
      const remaining = firstPresent(leaderNode.remaining_blocks, estimate.remaining_blocks, progress.remaining_blocks);
      const current = firstPresent(leaderNode.current_block, progress.current_block);
      const highest = firstPresent(leaderNode.highest_block, progress.highest_block);
      const singleNode = singleManagedNode ? (data.nodes || {})[managedNodes[0]] || {} : {};
      const displayedHeight = firstPresent(current, singleNode.chain_block_count, null);
      const heightText = hasValue(displayedHeight)
        ? (hasValue(highest) && Number(highest) !== Number(displayedHeight)
          ? `${fmt(displayedHeight)} / ${fmt(highest)}`
          : fmt(displayedHeight))
        : "n/a";
      const threshold = firstPresent(estimate.seed_threshold_blocks, data.sync_coordinator?.last_decision?.thresholds?.leader_near_tip_blocks, 5);
      const defaultNarrative = progress.status === "synced"
        ? (singleManagedNode ? "Managed node is synced to the current network tip." : "Managed nodes are synced.")
        : (singleManagedNode ? "Managed node is syncing." : "Managed nodes are syncing.");
      text("syncNarrative", estimate.narrative || defaultNarrative);
      text("syncMode", estimate.stage || progress.status || "unknown");
      text("syncHeight", heightText);
      const activeLabel = document.getElementById("syncActiveLabel");
      const activeValue = document.getElementById("syncActiveNode");
      if (singleManagedNode) {
        if (activeLabel) activeLabel.classList.add("hidden");
        if (activeValue) activeValue.classList.add("hidden");
      } else {
        if (activeLabel) activeLabel.classList.remove("hidden");
        if (activeValue) activeValue.classList.remove("hidden");
        text(
          "syncActiveNode",
          leader
            ? `${leader} ${fmt(current)} / ${fmt(highest)}; ${fmt(remaining)} block(s) remaining`
            : `${fmt(current)} / ${fmt(highest)}; ${fmt(remaining)} block(s) remaining`
        );
      }
      text("syncRate", syncRateText(estimate));
      text("syncEta", etaText(estimate.eta_seconds, estimate.eta_at));
      const miningStateBox = document.getElementById("miningStateBox");
      const chainStateBlocker = data.sync_health?.chain_state_blocker;
      const chainStateBlockerNodes = data.sync_health?.chain_state_blocker_nodes || {};
      const catchupPaused = Boolean(estimate.catchup_pause_active || data.catchup_policy?.active || data.mode === "catchup_pause");
      const poolUp = poolRunning(data);
      const minerCount = connectedMinerCount(data);
      if (chainStateBlocker) {
        const firstBlocker = Object.values(chainStateBlockerNodes)[0] || {};
        const blockerHash = firstBlocker.hash || "unknown block";
        text("syncMiningState", `Stopped: node chain state is stuck on irreparable sync block ${blockerHash}. Restore or resync node data before mining.`);
        if (miningStateBox) {
          miningStateBox.classList.add("paused");
          miningStateBox.classList.remove("ready");
        }
      } else if (catchupPaused) {
        const lag = firstPresent(estimate.catchup_pause_lag_blocks, data.catchup_policy?.lag_blocks, remaining);
        const pauseText = hasValue(lag)
          ? `Paused for chain catch-up; node is ${fmt(lag)} block(s) behind peers and the pool is not mining.`
          : "Paused for chain catch-up; the pool is not mining until the node catches up.";
        text("syncMiningState", pauseText);
        if (miningStateBox) {
          miningStateBox.classList.add("paused");
          miningStateBox.classList.remove("ready");
        }
      } else if (progress.status !== "synced") {
        const blocks = hasValue(remaining) ? `${fmt(remaining)} block(s)` : "unknown blocks";
        text("syncMiningState", `Syncing: ${blocks} remain before full sync; mining stays paused until sync and template checks are healthy.`);
        if (miningStateBox) {
          miningStateBox.classList.add("paused");
          miningStateBox.classList.remove("ready");
        }
      } else if (!poolUp) {
        text("syncMiningState", "Stopped: chain is synced, but the pool is not running.");
        if (miningStateBox) {
          miningStateBox.classList.add("paused");
          miningStateBox.classList.remove("ready");
        }
      } else if (!templateReady) {
        text("syncMiningState", "Waiting: chain is synced and pool is running, but backend template checks are not healthy.");
        if (miningStateBox) {
          miningStateBox.classList.add("paused");
          miningStateBox.classList.remove("ready");
        }
      } else if (data.can_mine && data.can_submit_blocks) {
        text("syncMiningState", `Mining: chain synced, pool running, and ${minerCount ? `${fmt(minerCount)} ASIC lane(s)` : "ASIC lanes"} can submit blocks.`);
        if (miningStateBox) {
          miningStateBox.classList.add("ready");
          miningStateBox.classList.remove("paused");
        }
      } else if (data.can_accept_shares) {
        text("syncMiningState", `Starting: pool is accepting ASIC work${minerCount ? ` from ${fmt(minerCount)} lane(s)` : ""}; waiting for fresh share/block-submit proof.`);
        if (miningStateBox) {
          miningStateBox.classList.add("paused");
          miningStateBox.classList.remove("ready");
        }
      } else {
        text("syncMiningState", "Waiting: chain is synced, but mining readiness has not been proven yet.");
        if (miningStateBox) {
          miningStateBox.classList.add("paused");
          miningStateBox.classList.remove("ready");
        }
      }
      if (estimate.next_step) {
        text("syncNextStep", estimate.next_step);
      } else if (catchupPaused) {
        text("syncNextStep", "wait for catch-up pause to clear; mining resumes when lag is below the configured threshold");
      } else if (progress.status !== "synced") {
        text("syncNextStep", "wait for full sync; mining resumes after gap reaches 0 and backend template checks are healthy");
      } else if (!poolUp) {
        text("syncNextStep", "start or repair the pool; chain sync is complete and the ASIC target is configured");
      } else if (!templateReady) {
        text("syncNextStep", "wait for backend template checks to become healthy before mining jobs are sent");
      } else if (data.can_mine && data.can_submit_blocks) {
        text("syncNextStep", "mining is active; monitor accepted shares and block-submit counters");
      } else if (data.can_accept_shares && minerCount > 0) {
        text("syncNextStep", "wait for the next accepted share or block-submit sample; the ASIC lane is connected");
      } else if (progress.status === "synced") {
        text("syncNextStep", "wait for the configured ASIC to reconnect and submit current work");
      } else {
        text("syncNextStep", "wait for nodes to finish syncing; the pool is holding mining jobs until backend sync is complete");
      }
    }
    function renderNodeSyncProgress(id, name, progress) {
      const nodeContainer = document.getElementById(id);
      nodeContainer.innerHTML = "";
      if (!name || !progress) return;
      nodeContainer.innerHTML = nodeSyncProgressHtml(name, progress);
    }
    function nodeSyncProgressHtml(name, progress, data = {}, node = {}) {
      const estimate = data.sync_estimate || {};
      const nodePercent = Number(progress.percent);
      const nodeBounded = Number.isFinite(nodePercent) ? Math.max(0, Math.min(100, nodePercent)) : 0;
      const nodeEstimate = estimate.nodes?.[name] || {};
      const eta = nodeEstimate.eta_seconds ? ` | ETA ${etaText(nodeEstimate.eta_seconds, nodeEstimate.eta_at)}` : "";
      const rate = nodeEstimate.rate_blocks_per_second ? ` | ${syncRateText(nodeEstimate)}` : "";
      return `
        <div class="sync-progress-bar" title="${escapeHtml(name)} EVM sync progress">
          <div class="sync-progress-fill" style="width: ${nodeBounded}%"></div>
        </div>
        <div class="sync-progress-meta">
          <span>${escapeHtml(syncProgressText(progress))}</span>
          <span>${escapeHtml(syncGapText(progress))}</span>
        </div>
        <div class="sync-progress-meta">
          <span>${escapeHtml(`${eta}${rate}`.replace(/^ \\| /, ""))}</span>
        </div>`;
    }
    function renderSyncProgress(progress, data = {}) {
      const fill = document.getElementById("syncProgressFill");
      const percentValue = Number(progress.percent);
      const hasPercent = Number.isFinite(percentValue);
      const bounded = hasPercent ? Math.max(0, Math.min(100, percentValue)) : 0;
      fill.style.width = `${bounded}%`;
      text("syncProgressPercent", syncProgressText(progress));
      text("syncProgressGap", syncGapText(progress));
      renderSyncEstimate(data, progress);
    }
    function nodeSummaryText(node) {
      if (!node) return "node data unavailable";
      const chain = hasValue(node.chain_block_count) ? ` chain_blocks=${fmt(node.chain_block_count)} source=${node.chain_rpc_source || "getBlockCount"}` : " chain_blocks=n/a";
      const rpcLatency = hasValue(node.chain_rpc_latency_ms) ? ` rpc_ms=${fmt(node.chain_rpc_latency_ms)}` : (node.chain_rpc_error ? " rpc=unavailable" : "");
      const rpcAttempts = Number(node.chain_rpc_attempts) > 1 ? ` rpc_attempts=${fmt(node.chain_rpc_attempts)}/${fmt(node.chain_rpc_retry_limit)}` : "";
      return `child=${node.child_running}${chain}${rpcLatency}${rpcAttempts} main_height=${fmt(node.chain_main_height)} best_main_order=${fmt(node.best_main_order)} import_age=${fmt(node.last_import_age_seconds)}s peer_ahead=${fmt(node.peer_ahead_blocks)} bad_peers=${fmt(node.invalid_peer_errors)} p2p_resets=${fmt(node.p2p_stream_errors)}`;
    }
    function nodeBlockHeight(name, node, syncNode, data) {
      return firstPresent(syncNode?.chain_block_count, node?.chain_block_count, null);
    }
    function renderNodeCards(nodeNames, nodes, syncProgress, data) {
      const container = document.getElementById("nodeCards");
      container.innerHTML = "";
      const syncNodes = syncProgress.nodes || {};
      const backendState = firstTemplateBackendState(data).backends || {};
      const managedNodeNames = data.managed_node_services || [];
      const observerNodeNames = data.observer_node_services || [];
      const singleManagedTopology = managedNodeNames.length === 1 && observerNodeNames.length === 0;
      const fallbackOnly = data.mode === "status_cache_unavailable" || (data.collector_budget_exceeded && !nodeNames.length);
      const overview = container.closest(".status-overview");
      if (overview) overview.classList.toggle("single-card", singleManagedTopology || fallbackOnly);
      if (singleManagedTopology || fallbackOnly) {
        return;
      }
      if (!nodeNames.length) {
        container.innerHTML = `<div class="node-card"><div class="kpi-label">Nodes</div><div class="kpi-value">n/a</div><div class="subtle">No node services reported.</div></div>`;
        return;
      }
      const managed = [];
      const observers = [];
      for (const name of nodeNames) {
        const node = nodes[name] || {};
        const roleValue = nodeRole(name, node, data);
        const healthScope = node.health_scope || nodeHealthScope(roleValue);
        const isObserver = roleValue === "observer" || healthScope === "advisory";
        (isObserver ? observers : managed).push({name, node, roleValue, healthScope});
      }
      function appendGroup(title, entries) {
        if (!entries.length) return;
        const group = document.createElement("div");
        group.className = "node-card-group";
        group.innerHTML = `<div class="node-card-group-title">${escapeHtml(title)}</div>`;
        for (const entry of entries) {
          const {name, node, roleValue, healthScope} = entry;
          const isObserver = roleValue === "observer" || healthScope === "advisory";
        const backend = backendInfoForNode(name, backendState) || {};
        const fanRole = backend.fan_in_role || (backend.selected ? "selected" : "");
        const roleHtml = `<span class="node-role">${escapeHtml(roleValue)}</span>`
          + (fanRole ? `<span class="node-role">${escapeHtml(fanRole)}</span>` : "");
        const wsText = hasValue(backend.ws_connected) ? ` ws=${metricEnabled(backend.ws_connected) ? "on" : "off"}` : "";
        const templateAge = hasValue(backend.template_age_seconds) ? ` template_age=${fmt(backend.template_age_seconds)}s` : "";
        const syncNode = syncNodes[name] || {};
        const syncHtml = isObserver && !hasValue(syncNode.status)
          ? `<div class="subtle">Advisory observer; not included in production sync health.</div>`
          : nodeSyncProgressHtml(name, syncNode, data, node);
        const blockHeight = nodeBlockHeight(name, node, syncNode, data);
        const blockHeightText = hasValue(blockHeight) ? fmt(blockHeight) : "chain RPC unavailable";
        const div = document.createElement("div");
        div.className = `node-card${isObserver ? " observer" : ""}`;
        div.innerHTML = `
          <div class="node-card-head">
            <div class="kpi-label node-card-title">${escapeHtml(name)} Sync</div>
            <div class="node-badges">${roleHtml}</div>
          </div>
          <div class="kpi-value">${escapeHtml(blockHeightText)}</div>
          <div class="sync-progress sync-progress-node">${syncHtml}</div>
          <div class="subtle">${escapeHtml(healthScope)} scope | ${escapeHtml(nodeSummaryText(node))}${escapeHtml(templateAge + wsText)}</div>`;
          group.appendChild(div);
        }
        container.appendChild(group);
      }
      appendGroup(managed.length === 1 ? "Managed production routing node" : "Managed production routing nodes", managed);
      appendGroup("Observer nodes - advisory only", observers);
    }
    function renderNodeLogs(nodeNames, nodes, data) {
      const container = document.getElementById("nodeLogsGrid");
      container.innerHTML = "";
      if (!nodeNames.length) {
        container.innerHTML = `<div class="subtle">No node logs available.</div>`;
        return;
      }
      for (const name of nodeNames) {
        const node = nodes[name] || {};
        const roleValue = nodeRole(name, node, data || {});
        const div = document.createElement("div");
        div.className = "node-log-block";
        div.innerHTML = `
          <div class="kpi-label">${escapeHtml(name)}<span class="node-role">${escapeHtml(roleValue)}</span></div>
          <div class="subtle">${escapeHtml(nodeSummaryText(node))}</div>
          <pre>${escapeHtml((node.tail || []).join("\\n"))}</pre>`;
        container.appendChild(div);
      }
    }
    function hydrateMinerDefaults(data) {
      if (minerDefaultsLoaded) return;
      const endpoint = data.pool_endpoint || `127.0.0.1:${data.pool_port || "3334"}`;
      const firstIp = (data.local_ips || [])[0] || "192.168.1.1";
      const parts = firstIp.split(".");
      if (!document.getElementById("minerScanTarget").value && parts.length === 4) {
        document.getElementById("minerScanTarget").value = `${parts[0]}.${parts[1]}.${parts[2]}.0/24`;
      }
      if (!document.getElementById("minerPoolUrl").value) document.getElementById("minerPoolUrl").value = `stratum+tcp://${endpoint}`;
      if (!document.getElementById("minerWorkerUser").value && data.mining_address) document.getElementById("minerWorkerUser").value = data.mining_address;
      minerDefaultsLoaded = true;
    }
    function renderMiners() {
      const tbody = document.getElementById("minersTable");
      tbody.innerHTML = "";
      if (!miners.length) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="7" class="subtle">No miners discovered yet.</td>`;
        tbody.appendChild(tr);
        return;
      }
      for (const miner of miners) {
        const result = minerResults[miner.ip];
        const pool = miner.current_pool || {};
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td class="checkbox-cell"><input type="checkbox" class="miner-select" value="${escapeHtml(miner.ip)}" checked></td>
          <td class="nowrap miner-name"><span class="miner-dot"></span>${escapeHtml(minerDisplayLabel(miner))}<br><span class="subtle">${escapeHtml(miner.ip ? `observed ${miner.ip}` : "")}</span></td>
          <td>${escapeHtml(miner.model || miner.hardware || "unknown")}</td>
          <td>${escapeHtml(miner.firmware || miner.mcbversion || "")}</td>
          <td>${escapeHtml(pool.url || "")}<br><span class="subtle">${escapeShortEth(pool.user || "")}</span></td>
          <td>${miner.active ? "yes" : "no"}</td>
          <td>${result ? escapeHtml(result.status + (result.error ? ": " + result.error : "")) : ""}</td>
        `;
        tbody.appendChild(tr);
      }
    }
    function selectedMinerIps() {
      return Array.from(document.querySelectorAll(".miner-select:checked")).map(input => input.value);
    }
    function selectAllMiners(checked) {
      for (const input of document.querySelectorAll(".miner-select")) input.checked = checked;
    }
    function activeMinerLaneRow(miner) {
      if (!miner) return false;
      return Boolean(
        miner.connected
        || miner.pool_active
        || miner.configured
        || miner.managed
        || miner.expected_work_lane
        || miner.work_pool_active
        || (miner.lane_status && miner.lane_status !== "not-tracked")
      );
    }
    function localAsicMinerLaneRow(miner) {
      if (!activeMinerLaneRow(miner)) return false;
      return String(miner.device_type || "").toLowerCase() === "asic";
    }
    function renderManagedMiners(health) {
      const tbody = document.getElementById("managedMinersTable");
      if (!tbody) return;
      tbody.innerHTML = "";
      const lane = health.lane_balance || {};
      const allRows = health.miners || [];
      const rows = allRows.filter(localAsicMinerLaneRow);
      const hiddenRows = Math.max(0, allRows.length - rows.length);
      text("minerHealthSummary", `active-asics=${fmt(rows.length)} hidden-non-asic-or-inactive=${fmt(hiddenRows)} tracked=${fmt(health.tracked_count || 0)} connected=${fmt(health.connected_count || 0)} managed=${fmt(health.managed_count || 0)} ok=${fmt(health.ok_count || 0)} stratum-hidden=${fmt(health.stratum_count || 0)} lanes=${fmt(lane.expected_lane_count || 0)} expected=${escapeHtml(lane.expected_work_percent || "0.00")}% imbalanced=${fmt(lane.imbalanced_count || 0)}`);
      if (!rows.length) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="14" class="subtle">No active local ASIC lanes are currently present.</td>`;
        tbody.appendChild(tr);
        return;
      }
      for (const miner of rows) {
        const cls = miner.status === "ok" || miner.status === "connected" ? "ok" : miner.status === "degraded" ? "warn" : miner.status === "inactive" ? "syncing" : "down";
        const workers = (miner.workers || []).join(", ") || miner.expected_worker_user || "";
        const issue = miner.issue || miner.api_error || "";
        const identity = minerIdentity(miner);
        const color = minerColor(identity);
        const name = minerDisplayLabel(miner);
        const tr = document.createElement("tr");
        tr.className = "miner-row";
        tr.style.setProperty("--miner-row-color", transparentColor(color, 0.08));
        tr.style.setProperty("--miner-color", color);
        tr.innerHTML = `
          <td class="nowrap miner-name"><span class="miner-dot"></span>${escapeHtml(name)}</td>
          <td class="nowrap">${escapeHtml(miner.device_type || "unknown")}</td>
          <td class="${cls}">${escapeHtml(miner.status)}</td>
          <td>${miner.configured ? "yes" : "no"}</td>
          <td>${miner.connected || miner.pool_active ? "yes" : "no"}</td>
          <td class="nowrap">${escapeShortEth(workers)}</td>
          <td class="right">${fmt(miner.shares || 0)}</td>
          <td class="right">${escapeHtml(miner.work_percent || "0.00")}</td>
          <td class="right">${escapeHtml(miner.expected_work_percent || "0.00")}</td>
          <td class="nowrap">${escapeHtml(miner.lane_status || "")}</td>
          <td class="right">${fmt(miner.share_work || 0)}</td>
          <td class="right">${fmt(miner.blocks_found || 0)}</td>
          <td>${escapeHtml(miner.last_share_at || "")}</td>
          <td>${escapeHtml(issue)}</td>
        `;
        tbody.appendChild(tr);
      }
    }
    async function scanMinerLan() {
      if (busy) return;
      busy = true;
      for (const btn of document.querySelectorAll("button")) btn.disabled = true;
      text("minersOutput", "Scanning LAN...");
      try {
        const response = await fetch("/api/miners/scan", {
          method: "POST",
          headers: {"content-type": "application/json"},
          body: JSON.stringify({target: document.getElementById("minerScanTarget").value, token: document.getElementById("token").value})
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "scan failed");
        miners = payload.miners || [];
        minerResults = {};
        renderMiners();
        text("minersOutput", JSON.stringify(payload, null, 2));
      } catch (error) {
        text("minersOutput", String(error));
        alert(String(error));
      } finally {
        busy = false;
        for (const btn of document.querySelectorAll("button")) btn.disabled = false;
      }
    }
    async function configureSelectedMiners() {
      const ips = selectedMinerIps();
      if (!ips.length) return alert("Select at least one miner.");
      const adminPassword = document.getElementById("minerAdminPassword").value;
      if (!adminPassword) return alert("Enter the miner admin password.");
      const poolUrl = document.getElementById("minerPoolUrl").value.trim();
      const workerUser = document.getElementById("minerWorkerUser").value.trim();
      const poolPassword = document.getElementById("minerPoolPassword").value;
      if (!poolUrl || !workerUser) return alert("Pool URL and worker/wallet are required.");
      if (!confirm(`Configure ${ips.length} miner(s) to ${poolUrl}?`)) return;

      busy = true;
      for (const btn of document.querySelectorAll("button")) btn.disabled = true;
      text("minersOutput", "Configuring selected miners...");
      try {
        const response = await fetch("/api/miners/configure", {
          method: "POST",
          headers: {"content-type": "application/json"},
          body: JSON.stringify({
            ips,
            admin_password: adminPassword,
            pool_url: poolUrl,
            worker_user: workerUser,
            pool_password: poolPassword,
            token: document.getElementById("token").value
          })
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "configuration failed");
        minerResults = {};
        for (const item of payload.results || []) minerResults[item.ip] = item;
        renderMiners();
        text("minersOutput", JSON.stringify(payload, null, 2));
      } catch (error) {
        text("minersOutput", String(error));
        alert(String(error));
      } finally {
        busy = false;
        for (const btn of document.querySelectorAll("button")) btn.disabled = false;
      }
    }
    async function saveMinerAuth() {
      const adminPassword = document.getElementById("minerAdminPassword").value;
      if (!adminPassword) return alert("Enter the miner admin password first.");
      if (!confirm("Save this password locally so the watchdog can repair miners without asking again?")) return;
      try {
        const response = await fetch("/api/miners/save-auth", {
          method: "POST",
          headers: {"content-type": "application/json"},
          body: JSON.stringify({admin_password: adminPassword, token: document.getElementById("token").value})
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "save failed");
        alert("Saved for watchdog repairs.");
      } catch (error) {
        alert(String(error));
      }
    }
    function currency(value, prefix, places = 2) {
      if (value === null || value === undefined || value === "") return "n/a";
      return `${prefix}${Number(value).toLocaleString(undefined, {maximumFractionDigits: places})}`;
    }
    function priceQuote(value, prefix) {
      return currency(value, prefix, 6);
    }
    const minerColors = ["#2563eb", "#16a34a", "#dc2626", "#d97706", "#7c3aed", "#0891b2", "#be185d", "#4b5563", "#0f766e", "#9333ea", "#b45309", "#0284c7"];
    function hashString(value) {
      let hash = 0;
      const text = String(value || "");
      for (let i = 0; i < text.length; i += 1) {
        hash = ((hash << 5) - hash + text.charCodeAt(i)) | 0;
      }
      return Math.abs(hash);
    }
    function normalizedMac(value) {
      const text = String(value || "").trim().toLowerCase().replaceAll("-", ":");
      if (/^[0-9a-f]{12}$/.test(text)) return text.match(/.{1,2}/g).join(":");
      return /^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$/.test(text) ? text : "";
    }
    function minerMac(row) {
      const direct = normalizedMac(row.mac);
      if (direct) return direct;
      const device = String(row.device_id || row.identity_key || "").trim().toLowerCase();
      return device.startsWith("mac:") ? normalizedMac(device.slice(4)) : "";
    }
    function minerMacSuffix(row) {
      const mac = minerMac(row).replaceAll(":", "");
      return mac ? mac.slice(-3) : "";
    }
    function minerIdentity(row) {
      const mac = minerMac(row);
      if (mac) return `mac:${mac}`;
      return String(row.identity_key || row.device_id || "").trim();
    }
    function minerDisplayName(row) {
      const explicit = String(row.display_name || row.name || "").trim();
      if (explicit) return explicit;
      const mac = minerMac(row);
      return mac || "unknown-mac";
    }
    function minerShortIp(row) {
      const ip = String(row.ip || "").trim();
      const parts = ip.split(".");
      const last = parts.length === 4 ? parts[3] : "";
      return /^\d{1,3}$/.test(last) ? `.${last}` : "";
    }
    function minerDisplayLabel(row) {
      const provided = String(row.display_label || "").trim();
      if (provided) return provided;
      const explicit = String(row.display_name || row.name || "").trim();
      const suffix = minerMacSuffix(row);
      if (explicit) return `${explicit}-${suffix || "unknown-mac"}`;
      return minerMac(row) || "unknown-mac";
    }
    function minerColor(identity) {
      if (!identity) return "#4b5563";
      return minerColors[hashString(identity) % minerColors.length];
    }
    function globalPoolIdentity(row) {
      if (typeof row === "string") return row.trim().toLowerCase();
      return String(row?.address || row?.address_short || row?.pool_label || row?.pool_name || "").trim().toLowerCase();
    }
    function globalPoolColor(identity) {
      const key = globalPoolIdentity(identity);
      if (!key) return "#4b5563";
      return minerColors[hashString(`pool:${key}`) % minerColors.length];
    }
    function transparentColor(hex, alpha) {
      const match = String(hex || "").match(/^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i);
      if (!match) return `rgba(75,85,99,${alpha})`;
      const r = parseInt(match[1], 16);
      const g = parseInt(match[2], 16);
      const b = parseInt(match[3], 16);
      return `rgba(${r},${g},${b},${alpha})`;
    }
    function numberValue(value) {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    }
    function isDockerBridgePseudoMiner(row) {
      const ip = String(row?.ip || "");
      const parts = ip.split(".").map(part => Number(part));
      const isDockerBridge = parts.length === 4 && parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31;
      if (!isDockerBridge) return false;
      const deviceType = String(row?.device_type || "").toLowerCase();
      const sourceText = `${row?.discovered_by || ""} ${(row?.sources || []).join(" ")}`.toLowerCase();
      return deviceType !== "asic" || sourceText.includes("pool-log");
    }
    function visibleMinerRows(rows) {
      return (rows || []).filter(row => {
        if (isDockerBridgePseudoMiner(row)) return false;
        if (String(row?.credit_scope || "") === "idle-registered-asic") return false;
        if (row?.managed || row?.configured || row?.connected) return true;
        const shares = Number(row?.shares || 0);
        const credits = Number(row?.credited_blocks || 0);
        return Array.isArray(row?.credit_workers) && row.credit_workers.length > 0 && (shares > 0 || credits > 0);
      });
    }
    function formatDisplayTime(value) {
      const parsed = Date.parse(value);
      if (!Number.isFinite(parsed)) return value || "n/a";
      return new Date(parsed).toLocaleString(undefined, {month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit"});
    }
    function formatDuration(seconds) {
      const total = Math.max(0, Math.round(Number(seconds) || 0));
      if (!total) return "n/a";
      const hours = Math.floor(total / 3600);
      const minutes = Math.floor((total % 3600) / 60);
      const secs = total % 60;
      if (hours) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
      if (minutes) return `${minutes}m ${String(secs).padStart(2, "0")}s`;
      return `${secs}s`;
    }
    function formatGlobalTableWindow(data) {
      const seconds = Number(data.scan_window_seconds || 0);
      const duration = formatDuration(seconds);
      const start = data.scan_start_block !== undefined ? fmt(data.scan_start_block) : "n/a";
      const end = data.scan_end_block !== undefined ? fmt(data.scan_end_block) : fmt(data.latest_block);
      const fetched = Number(data.fetched_blocks || 0);
      const requested = Number(data.requested_blocks || fetched || 0);
      const blockText = requested && fetched !== requested ? `${fmt(fetched)} of ${fmt(requested)} scanned blocks` : `${fmt(fetched || requested)} scanned blocks`;
      const updated = data.updated_at ? `, ending ${formatDisplayTime(data.updated_at)}` : "";
      return `Table period: ${duration}, blocks ${start} to ${end}, ${blockText}${updated}.`;
    }
    let earningsChartRangeHours = 1;
    let earningsChartUnit = "usd";
    function updateEarningsRangeButtons() {
      for (const button of document.querySelectorAll(".earnings-range-button")) {
        button.classList.toggle("active", Number(button.dataset.range || 0) === earningsChartRangeHours);
      }
    }
    function updateEarningsUnitButtons() {
      for (const button of document.querySelectorAll(".earnings-unit-button")) {
        button.classList.toggle("active", String(button.dataset.unit || "") === earningsChartUnit);
      }
      const summary = document.getElementById("earningsChartUnitSummary");
      if (summary) summary.textContent = `${earningsChartUnit.toUpperCase()} per miner per hour`;
    }
    function setEarningsChartRange(hours) {
      earningsChartRangeHours = hours;
      updateEarningsRangeButtons();
      if (lastEarningsData) drawEarningsChart(lastEarningsData);
    }
    function setEarningsChartUnit(unit) {
      if (!["bdag", "usd", "zar"].includes(unit)) return;
      earningsChartUnit = unit;
      updateEarningsUnitButtons();
      if (lastEarningsData) drawEarningsChart(lastEarningsData);
    }
    let minerWorkChartRangeHours = 1;
    let minerWorkChartMetric = "work";
    const minerWorkMetricConfigs = {
      work: {
        label: "Accepted work percentage by miner",
        axis: "%",
        detail: "Work %",
        empty: "No miner work-share history available yet.",
        floor: 0,
        ceiling: 100,
        minYMax: 20,
      },
      blocks: {
        label: "Actual found blocks by miner",
        axis: "blocks",
        detail: "Blocks",
        empty: "No per-miner block history available yet.",
        floor: 0,
        ceiling: null,
        minYMax: 1,
      },
      hashrate: {
        label: "Hashrate by miner; reconstructed history uses accepted-work estimates",
        axis: "GH/s",
        detail: "GH/s",
        empty: "No per-miner hashrate history available yet.",
        floor: 0,
        ceiling: null,
        minYMax: 1,
      },
    };
    function updateMinerWorkRangeButtons() {
      for (const button of document.querySelectorAll(".miner-work-range-button")) {
        button.classList.toggle("active", Number(button.dataset.range || 0) === minerWorkChartRangeHours);
      }
    }
    function updateMinerWorkMetricButtons() {
      for (const button of document.querySelectorAll(".miner-work-metric-button")) {
        button.classList.toggle("active", String(button.dataset.metric || "") === minerWorkChartMetric);
      }
      const summary = document.getElementById("minerWorkChartMetricSummary");
      if (summary) summary.textContent = (minerWorkMetricConfigs[minerWorkChartMetric] || minerWorkMetricConfigs.work).label;
    }
    function setMinerWorkChartRange(hours) {
      minerWorkChartRangeHours = hours;
      updateMinerWorkRangeButtons();
      if (lastEarningsData) drawMinerWorkChart(lastEarningsData);
    }
    function setMinerWorkChartMetric(metric) {
      if (!minerWorkMetricConfigs[metric]) return;
      minerWorkChartMetric = metric;
      updateMinerWorkMetricButtons();
      if (lastEarningsData) drawMinerWorkChart(lastEarningsData);
    }
    function parseDashboardTime(value) {
      if (!value) return null;
      const text = String(value).trim().replace(/([+-]\d{2})(\d{2})$/, "$1:$2");
      const parsed = Date.parse(text);
      return Number.isFinite(parsed) ? parsed : null;
    }
    function chartRangeLabel(hours) {
      if (hours === 72) return "3d";
      if (hours === 168) return "week";
      if (hours === 720) return "month";
      return `${hours}h`;
    }
    function chartHistoryFreshness(data) {
      if (!data?.history_stale) return "";
      const age = Number(data.history_latest_age_seconds || 0);
      const ageText = age >= 3600 ? `${(age / 3600).toFixed(1)}h` : `${Math.round(age / 60)}m`;
      const latest = data.history_latest_at ? ` since ${formatDisplayTime(data.history_latest_at)}` : "";
      return ` | sampler stopped ${ageText}${latest}`;
    }
    function samplerAlertMessage(data) {
      if (!data) return "";
      if (data.history_stale) {
        const age = Number(data.history_latest_age_seconds || 0);
        const ageText = age >= 3600 ? `${(age / 3600).toFixed(1)} hours` : `${Math.round(age / 60)} minutes`;
        const latest = data.history_latest_at ? ` Last good plot sample: ${formatDisplayTime(data.history_latest_at)}.` : "";
        const reason = data.history_stale_reason ? ` ${data.history_stale_reason}` : "";
        return `Sampler stopped: earnings and miner plots are not receiving fresh history. No valid sample for ${ageText}.${latest}${reason}`;
      }
      if (data.history_sampler_status === "missing") {
        return "No earnings/miner plot sampler history exists yet. The status sampler should create the first sample shortly.";
      }
      return "";
    }
    function renderSamplerAlert(id, data) {
      const el = document.getElementById(id);
      if (!el) return;
      const message = samplerAlertMessage(data);
      if (!message) {
        el.classList.add("hidden");
        el.textContent = "";
        return;
      }
      el.textContent = message;
      el.classList.remove("hidden");
    }
    function formatChartTime(ms, hours = earningsChartRangeHours) {
      const options = hours > 168
        ? {month: "short", day: "numeric"}
        : hours > 24
          ? {month: "short", day: "numeric", hour: "2-digit"}
          : hours >= 12
            ? {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}
            : {hour: "2-digit", minute: "2-digit"};
      return new Date(ms).toLocaleString(undefined, options);
    }
    function chartRangeProfile(hours) {
      const rangeHours = Number(hours) || 1;
      if (rangeHours <= 1) return {bucketMs: 30 * 1000, smoothMs: 60 * 1000, gapMs: 8 * 60 * 1000, detail: "30s"};
      if (rangeHours <= 4) return {bucketMs: 60 * 1000, smoothMs: 2 * 60 * 1000, gapMs: 15 * 60 * 1000, detail: "1m"};
      if (rangeHours <= 12) return {bucketMs: 3 * 60 * 1000, smoothMs: 6 * 60 * 1000, gapMs: 30 * 60 * 1000, detail: "3m"};
      if (rangeHours <= 24) return {bucketMs: 5 * 60 * 1000, smoothMs: 12 * 60 * 1000, gapMs: 60 * 60 * 1000, detail: "5m"};
      if (rangeHours <= 72) return {bucketMs: 15 * 60 * 1000, smoothMs: 45 * 60 * 1000, gapMs: 3 * 60 * 60 * 1000, detail: "15m"};
      if (rangeHours <= 168) return {bucketMs: 30 * 60 * 1000, smoothMs: 2 * 60 * 60 * 1000, gapMs: 8 * 60 * 60 * 1000, detail: "30m"};
      return {bucketMs: 2 * 60 * 60 * 1000, smoothMs: 6 * 60 * 60 * 1000, gapMs: 36 * 60 * 60 * 1000, detail: "2h"};
    }
    function chartTickCount(chartW, hours) {
      const maxTicks = Number(hours) <= 4 ? 8 : Number(hours) <= 24 ? 7 : 6;
      return Math.min(maxTicks, Math.max(2, Math.floor(chartW / 125)));
    }
    function clampChartValue(value, floor = 0, ceiling = null) {
      let result = value;
      if (floor !== null) result = Math.max(floor, result);
      if (ceiling !== null) result = Math.min(ceiling, result);
      return result;
    }
    function filterChartPointsForRange(points, latestTime, rangeHours) {
      const sorted = [...points]
        .filter(point => Number.isFinite(point.t) && Number.isFinite(point.v))
        .sort((a, b) => a.t - b.t);
      if (!sorted.length || latestTime === null) return sorted;
      const cutoff = latestTime - (rangeHours * 60 * 60 * 1000);
      const filtered = sorted.filter(point => point.t >= cutoff && point.t <= latestTime);
      if (filtered.length) {
        const anchor = [...sorted].reverse().find(point => point.t < cutoff);
        if (anchor) filtered.unshift({...anchor, t: cutoff, clipped: true});
      }
      return filtered;
    }
    function bucketChartPoints(points, rangeHours, floor = 0, ceiling = null) {
      const sorted = [...points].sort((a, b) => a.t - b.t);
      if (sorted.length < 3) {
        return sorted.map(point => ({...point, v: clampChartValue(point.v, floor, ceiling)}));
      }
      const profile = chartRangeProfile(rangeHours);
      const buckets = new Map();
      for (const point of sorted) {
        const bucketKey = Math.floor(point.t / profile.bucketMs) * profile.bucketMs;
        const bucket = buckets.get(bucketKey) || {tSum: 0, vSum: 0, count: 0};
        bucket.tSum += point.t;
        bucket.vSum += point.v;
        bucket.count += 1;
        buckets.set(bucketKey, bucket);
      }
      return Array.from(buckets.values())
        .map(bucket => ({
          t: Math.round(bucket.tSum / bucket.count),
          v: clampChartValue(bucket.vSum / bucket.count, floor, ceiling),
          samples: bucket.count,
        }))
        .sort((a, b) => a.t - b.t);
    }
    function smoothChartPoints(points, rangeHours, floor = 0, ceiling = null) {
      const sorted = bucketChartPoints(points, rangeHours, floor, ceiling);
      if (sorted.length < 4) return sorted;
      const windowMs = chartRangeProfile(rangeHours).smoothMs;
      return sorted.map(point => {
        let weightedValue = 0;
        let weightTotal = 0;
        for (const peer of sorted) {
          const distance = Math.abs(peer.t - point.t);
          if (distance > windowMs) continue;
          const weight = 1 - (distance / (windowMs + 1));
          weightedValue += peer.v * weight;
          weightTotal += weight;
        }
        let value = weightTotal ? weightedValue / weightTotal : point.v;
        value = clampChartValue(value, floor, ceiling);
        return {...point, rawV: point.v, v: value};
      });
    }
    function drawSmoothChartSegment(ctx, coords) {
      if (!coords.length) return;
      ctx.beginPath();
      ctx.moveTo(coords[0].x, coords[0].y);
      if (coords.length === 1) {
        ctx.lineTo(coords[0].x + 0.01, coords[0].y);
      } else if (coords.length === 2) {
        ctx.lineTo(coords[1].x, coords[1].y);
      } else {
        for (let i = 1; i < coords.length - 2; i += 1) {
          const midX = (coords[i].x + coords[i + 1].x) / 2;
          const midY = (coords[i].y + coords[i + 1].y) / 2;
          ctx.quadraticCurveTo(coords[i].x, coords[i].y, midX, midY);
        }
        const penultimate = coords[coords.length - 2];
        const last = coords[coords.length - 1];
        ctx.quadraticCurveTo(penultimate.x, penultimate.y, last.x, last.y);
      }
      ctx.stroke();
    }
    function drawSmoothChartLine(ctx, points, xFor, yFor, rangeHours) {
      if (!points.length) return;
      const gapMs = chartRangeProfile(rangeHours).gapMs;
      let segment = [];
      for (let i = 0; i < points.length; i += 1) {
        const point = points[i];
        if (i > 0 && point.t - points[i - 1].t > gapMs) {
          drawSmoothChartSegment(ctx, segment);
          segment = [];
        }
        segment.push({x: xFor(point), y: yFor(point)});
      }
      drawSmoothChartSegment(ctx, segment);
    }
    function firstNumeric(...values) {
      for (const value of values) {
        const parsed = numberValue(value);
        if (parsed !== null) return parsed;
      }
      return null;
    }
    function minerEarningsPerHour(row, unit, price) {
      const bdag = firstNumeric(
        row.estimated_wallet_bdag_recent_hour,
        row.estimated_wallet_bdag_avg_hour,
        row.estimated_wallet_bdag_1h,
        row.estimated_bdag_avg_hour,
        row.estimated_bdag_1h
      );
      const usd = firstNumeric(
        row.estimated_wallet_usd_recent_hour,
        row.estimated_wallet_usd_avg_hour,
        row.estimated_wallet_usd_1h,
        row.estimated_usd_avg_hour,
        row.estimated_usd_1h
      );
      const zar = firstNumeric(
        row.estimated_wallet_zar_recent_hour,
        row.estimated_wallet_zar_avg_hour,
        row.estimated_wallet_zar_1h,
        row.estimated_zar_avg_hour,
        row.estimated_zar_1h
      );
      const usdPrice = numberValue(price?.usd);
      const zarPrice = numberValue(price?.zar);
      if (unit === "bdag") return bdag ?? (usd !== null && usdPrice ? usd / usdPrice : null) ?? (zar !== null && zarPrice ? zar / zarPrice : null);
      if (unit === "zar") return zar ?? (bdag !== null && zarPrice !== null ? bdag * zarPrice : null) ?? (usd !== null && usdPrice && zarPrice !== null ? usd * (zarPrice / usdPrice) : null);
      return usd ?? (bdag !== null && usdPrice !== null ? bdag * usdPrice : null) ?? (zar !== null && zarPrice && usdPrice !== null ? zar * (usdPrice / zarPrice) : null);
    }
    function formatEarningsChartValue(value, unit) {
      if (unit === "bdag") return currency(value, "", 0);
      if (unit === "zar") return currency(value, "R");
      return currency(value, "$");
    }
    function earningsDbToWalletScale(data) {
      const onchain24 = firstNumeric(data.earnings_24h?.bdag, data.hourly_averages?.wallet_24h_bdag);
      const db24 = firstNumeric(data.earnings_24h?.db_credit_fallback_bdag, data.credits?.recent_24h?.wallet_total_bdag, data.credits?.recent_24h?.total_bdag);
      const onchain1 = firstNumeric(data.onchain_earnings?.last_1h?.earned_bdag, data.hourly_averages?.recent_bdag_hour);
      const db1 = firstNumeric(data.credits?.recent_1h?.total_bdag);
      const candidates = [];
      if (onchain24 !== null && db24 !== null && db24 > 0) candidates.push(onchain24 / db24);
      if (onchain1 !== null && db1 !== null && db1 > 0) candidates.push(onchain1 / db1);
      const factor = candidates.find(value => Number.isFinite(value) && value > 1.5 && value < 100) || 1;
      return {factor, normalized: factor !== 1};
    }
    function isLegacyDbScaleEarningsRow(row) {
      const hasActualWalletBdag = firstNumeric(row.estimated_wallet_bdag_recent_hour, row.estimated_wallet_bdag_avg_hour, row.estimated_wallet_bdag_1h) !== null;
      const hasZarFields = firstNumeric(row.estimated_zar_avg_hour, row.estimated_zar_1h, row.estimated_wallet_zar_recent_hour, row.estimated_wallet_zar_avg_hour, row.estimated_wallet_zar_1h) !== null;
      const hasUsdOnly = firstNumeric(row.estimated_wallet_usd_recent_hour, row.estimated_usd_avg_hour, row.estimated_usd_1h) !== null;
      return !hasActualWalletBdag && !hasZarFields && hasUsdOnly;
    }
    function applyLegacyEarningsScale(value, row, scale) {
      return value !== null && isLegacyDbScaleEarningsRow(row) ? value * scale.factor : value;
    }
    function drawEarningsChart(data) {
      const canvas = document.getElementById("earningsChart");
      const legend = document.getElementById("earningsChartLegend");
      const rangeLabel = document.getElementById("earningsChartRangeLabel");
      if (!canvas) return;
      updateEarningsRangeButtons();
      updateEarningsUnitButtons();
      const rect = canvas.parentElement.getBoundingClientRect();
      const ratio = window.devicePixelRatio || 1;
      const width = Math.max(360, Math.floor(rect.width));
      const height = Math.max(240, Math.floor(rect.height));
      canvas.width = Math.floor(width * ratio);
      canvas.height = Math.floor(height * ratio);
      const ctx = canvas.getContext("2d");
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "#fbfcfd";
      ctx.fillRect(0, 0, width, height);

      const snapshots = [...(data.history || []), {
        generated_at: data.generated_at,
        miner_estimates: data.miner_estimates || [],
      }];
      const timestamped = snapshots
        .map(snapshot => ({
          at: parseDashboardTime(snapshot.generated_at),
          miners: snapshot.miner_estimates || [],
        }))
        .filter(snapshot => snapshot.at !== null)
        .sort((a, b) => a.at - b.at);

      const latestTime = timestamped.length ? timestamped[timestamped.length - 1].at : null;
      const cutoff = latestTime === null ? null : latestTime - (earningsChartRangeHours * 60 * 60 * 1000);
      const scale = earningsDbToWalletScale(data);
      const seriesMap = new Map();
      for (const snapshot of timestamped) {
        for (const row of snapshot.miners) {
          const value = applyLegacyEarningsScale(minerEarningsPerHour(row, earningsChartUnit, data.price || {}), row, scale);
          if (value === null) continue;
          const key = minerIdentity(row);
          if (!key) continue;
          if (!seriesMap.has(key)) {
            seriesMap.set(key, {key, label: minerDisplayLabel(row), points: []});
          }
          seriesMap.get(key).label = minerDisplayLabel(row);
          seriesMap.get(key).points.push({t: snapshot.at, v: value});
        }
      }

      const series = Array.from(seriesMap.values())
        .map(item => {
          return {...item, points: filterChartPointsForRange(item.points, latestTime, earningsChartRangeHours)};
        })
        .filter(item => item.points.length)
        .sort((a, b) => (b.points[b.points.length - 1]?.v || 0) - (a.points[a.points.length - 1]?.v || 0));

      const visibleSeries = series.map(item => ({...item, points: smoothChartPoints(item.points, earningsChartRangeHours)}));

      if (rangeLabel) {
        const period = latestTime !== null && cutoff !== null ? `${formatChartTime(cutoff, earningsChartRangeHours)} to ${formatChartTime(latestTime, earningsChartRangeHours)}` : "no earnings history yet";
        const normalized = scale.normalized ? ` | legacy history normalized x${scale.factor.toFixed(2)}` : "";
        rangeLabel.textContent = `${chartRangeLabel(earningsChartRangeHours)} window | detail ${chartRangeProfile(earningsChartRangeHours).detail} | ${earningsChartUnit.toUpperCase()}/h${normalized}${chartHistoryFreshness(data)} | ${period}`;
      }

      if (!visibleSeries.length) {
        if (legend) legend.innerHTML = "";
        ctx.fillStyle = "#617181";
        ctx.font = "13px system-ui, sans-serif";
        ctx.fillText("No miner earnings history available yet.", 16, 34);
        return;
      }

      const legendLimit = 8;
      if (legend) {
        legend.innerHTML = "";
        for (let i = 0; i < Math.min(visibleSeries.length, legendLimit); i += 1) {
          const item = visibleSeries[i];
          const span = document.createElement("span");
          span.className = "legend-key";
          span.style.setProperty("--key-color", minerColor(item.key));
          span.textContent = item.label;
          legend.appendChild(span);
        }
        if (visibleSeries.length > legendLimit) {
          const more = document.createElement("span");
          more.className = "subtle";
          more.textContent = `+${visibleSeries.length - legendLimit} more`;
          legend.appendChild(more);
        }
      }

      const allPoints = visibleSeries.flatMap(item => item.points);
      const minTime = Math.min(...allPoints.map(point => point.t));
      const maxTime = Math.max(...allPoints.map(point => point.t));
      const maxValue = Math.max(...allPoints.map(point => point.v), 1);
      const margin = {top: 24, right: 18, bottom: 54, left: earningsChartUnit === "bdag" || earningsChartUnit === "zar" ? 78 : 58};
      const chartW = width - margin.left - margin.right;
      const chartH = height - margin.top - margin.bottom;
      const xSpan = Math.max(maxTime - minTime, 1);
      const yMax = maxValue * 1.1;
      const yTicks = 5;
      const xTicks = chartTickCount(chartW, earningsChartRangeHours);

      ctx.strokeStyle = "#d7dbe0";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(margin.left, margin.top);
      ctx.lineTo(margin.left, margin.top + chartH);
      ctx.lineTo(width - margin.right, margin.top + chartH);
      ctx.stroke();

      ctx.fillStyle = "#617181";
      ctx.font = "12px system-ui, sans-serif";
      ctx.textAlign = "right";
      for (let i = 0; i <= yTicks; i += 1) {
        const ratioY = i / yTicks;
        const value = yMax * (1 - ratioY);
        const y = margin.top + chartH * ratioY;
        ctx.strokeStyle = "#edf0f3";
        ctx.beginPath();
        ctx.moveTo(margin.left, y);
        ctx.lineTo(width - margin.right, y);
        ctx.stroke();
        ctx.fillText(formatEarningsChartValue(value, earningsChartUnit), margin.left - 8, y + 4);
      }

      ctx.textAlign = "center";
      for (let i = 0; i <= xTicks; i += 1) {
        const ratioX = i / xTicks;
        const t = minTime + xSpan * ratioX;
        const x = margin.left + chartW * ratioX;
        ctx.strokeStyle = "#edf0f3";
        ctx.beginPath();
        ctx.moveTo(x, margin.top);
        ctx.lineTo(x, margin.top + chartH);
        ctx.stroke();
        ctx.fillText(formatChartTime(t, earningsChartRangeHours), x, height - 18);
      }

      for (let i = 0; i < visibleSeries.length; i += 1) {
        const item = visibleSeries[i];
        const color = minerColor(item.key);
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.lineWidth = 2;
        drawSmoothChartLine(
          ctx,
          item.points,
          point => margin.left + ((point.t - minTime) / xSpan) * chartW,
          point => margin.top + chartH - ((point.v / yMax) * chartH),
          earningsChartRangeHours
        );
        const last = item.points[item.points.length - 1];
        const lastX = margin.left + ((last.t - minTime) / xSpan) * chartW;
        const lastY = margin.top + chartH - ((last.v / yMax) * chartH);
        ctx.beginPath();
        ctx.arc(lastX, lastY, 3, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    function minerWorkPercent(row) {
      const parsed = numberValue(row.work_percent);
      return parsed === null ? null : parsed;
    }
    function minerBlocksFound(row) {
      return firstNumeric(row.blocks_found, row.found_blocks);
    }
    function minerHashrateGhs(row) {
      return firstNumeric(
        row.av_hashrate_ghs,
        row.hashrate_ghs,
        row.observed_hashrate_ghs,
        row.av_hashrate,
        row.hashrate
      );
    }
    function minerChartMetricValue(row) {
      if (minerWorkChartMetric === "blocks") return minerBlocksFound(row);
      if (minerWorkChartMetric === "hashrate") return minerHashrateGhs(row);
      return minerWorkPercent(row);
    }
    function formatMinerMetricValue(value, metric = minerWorkChartMetric) {
      if (metric === "work") return `${value.toFixed(0)}%`;
      if (metric === "hashrate") return `${value.toLocaleString(undefined, {maximumFractionDigits: 1})}`;
      if (value < 10 && value % 1 !== 0) return value.toFixed(1);
      return value.toLocaleString(undefined, {maximumFractionDigits: 0});
    }
    function drawMinerWorkChart(data) {
      const canvas = document.getElementById("minerWorkChart");
      const legend = document.getElementById("minerWorkChartLegend");
      const rangeLabel = document.getElementById("minerWorkChartRangeLabel");
      if (!canvas) return;
      updateMinerWorkRangeButtons();
      updateMinerWorkMetricButtons();
      const metricConfig = minerWorkMetricConfigs[minerWorkChartMetric] || minerWorkMetricConfigs.work;
      const rect = canvas.parentElement.getBoundingClientRect();
      const ratio = window.devicePixelRatio || 1;
      const width = Math.max(360, Math.floor(rect.width));
      const height = Math.max(240, Math.floor(rect.height));
      canvas.width = Math.floor(width * ratio);
      canvas.height = Math.floor(height * ratio);
      const ctx = canvas.getContext("2d");
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "#fbfcfd";
      ctx.fillRect(0, 0, width, height);

      const snapshots = [...(data.history || []), {
        generated_at: data.generated_at,
        miner_estimates: data.miner_estimates || [],
      }];
      const timestamped = snapshots
        .map(snapshot => ({
          at: parseDashboardTime(snapshot.generated_at),
          miners: snapshot.miner_estimates || [],
        }))
        .filter(snapshot => snapshot.at !== null)
        .sort((a, b) => a.at - b.at);

      const latestTime = timestamped.length ? timestamped[timestamped.length - 1].at : null;
      const cutoff = latestTime === null ? null : latestTime - (minerWorkChartRangeHours * 60 * 60 * 1000);
      const seriesMap = new Map();
      for (const snapshot of timestamped) {
        for (const row of snapshot.miners) {
          const value = minerChartMetricValue(row);
          if (value === null) continue;
          const key = minerIdentity(row);
          if (!key) continue;
          if (!seriesMap.has(key)) {
            seriesMap.set(key, {key, label: minerDisplayLabel(row), points: []});
          }
          seriesMap.get(key).label = minerDisplayLabel(row);
          seriesMap.get(key).points.push({t: snapshot.at, v: value});
        }
      }

      const visibleSeries = Array.from(seriesMap.values())
        .map(item => {
          return {...item, points: filterChartPointsForRange(item.points, latestTime, minerWorkChartRangeHours)};
        })
        .filter(item => item.points.length)
        .sort((a, b) => (b.points[b.points.length - 1]?.v || 0) - (a.points[a.points.length - 1]?.v || 0))
        .map(item => ({...item, points: smoothChartPoints(item.points, minerWorkChartRangeHours, metricConfig.floor, metricConfig.ceiling)}));

      if (rangeLabel) {
        const period = latestTime !== null && cutoff !== null ? `${formatChartTime(cutoff, minerWorkChartRangeHours)} to ${formatChartTime(latestTime, minerWorkChartRangeHours)}` : "no miner history yet";
        rangeLabel.textContent = `${chartRangeLabel(minerWorkChartRangeHours)} window | detail ${chartRangeProfile(minerWorkChartRangeHours).detail} | ${metricConfig.detail}${chartHistoryFreshness(data)} | ${period}`;
      }

      if (!visibleSeries.length) {
        if (legend) legend.innerHTML = "";
        ctx.fillStyle = "#617181";
        ctx.font = "13px system-ui, sans-serif";
        ctx.fillText(metricConfig.empty, 16, 34);
        return;
      }

      const legendLimit = 8;
      if (legend) {
        legend.innerHTML = "";
        for (let i = 0; i < Math.min(visibleSeries.length, legendLimit); i += 1) {
          const item = visibleSeries[i];
          const span = document.createElement("span");
          span.className = "legend-key";
          span.style.setProperty("--key-color", minerColor(item.key));
          span.textContent = item.label;
          legend.appendChild(span);
        }
        if (visibleSeries.length > legendLimit) {
          const more = document.createElement("span");
          more.className = "subtle";
          more.textContent = `+${visibleSeries.length - legendLimit} more`;
          legend.appendChild(more);
        }
      }

      const allPoints = visibleSeries.flatMap(item => item.points);
      const minTime = Math.min(...allPoints.map(point => point.t));
      const maxTime = Math.max(...allPoints.map(point => point.t));
      const maxValue = Math.max(...allPoints.map(point => point.v), 1);
      const margin = {top: 24, right: 18, bottom: 54, left: minerWorkChartMetric === "hashrate" ? 68 : 58};
      const chartW = width - margin.left - margin.right;
      const chartH = height - margin.top - margin.bottom;
      const xSpan = Math.max(maxTime - minTime, 1);
      const yMaxRaw = Math.max(metricConfig.minYMax || 1, maxValue * 1.15);
      const yMax = metricConfig.ceiling === null ? yMaxRaw : Math.min(metricConfig.ceiling, yMaxRaw);
      const yTicks = 5;
      const xTicks = chartTickCount(chartW, minerWorkChartRangeHours);

      ctx.strokeStyle = "#d7dbe0";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(margin.left, margin.top);
      ctx.lineTo(margin.left, margin.top + chartH);
      ctx.lineTo(width - margin.right, margin.top + chartH);
      ctx.stroke();

      ctx.fillStyle = "#617181";
      ctx.font = "12px system-ui, sans-serif";
      ctx.textAlign = "right";
      for (let i = 0; i <= yTicks; i += 1) {
        const ratioY = i / yTicks;
        const value = yMax * (1 - ratioY);
        const y = margin.top + chartH * ratioY;
        ctx.strokeStyle = "#edf0f3";
        ctx.beginPath();
        ctx.moveTo(margin.left, y);
        ctx.lineTo(width - margin.right, y);
        ctx.stroke();
        ctx.fillText(formatMinerMetricValue(value), margin.left - 8, y + 4);
      }

      ctx.textAlign = "center";
      for (let i = 0; i <= xTicks; i += 1) {
        const ratioX = i / xTicks;
        const t = minTime + xSpan * ratioX;
        const x = margin.left + chartW * ratioX;
        ctx.strokeStyle = "#edf0f3";
        ctx.beginPath();
        ctx.moveTo(x, margin.top);
        ctx.lineTo(x, margin.top + chartH);
        ctx.stroke();
        ctx.fillText(formatChartTime(t, minerWorkChartRangeHours), x, height - 18);
      }

      for (const item of visibleSeries) {
        const color = minerColor(item.key);
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.lineWidth = 2;
        drawSmoothChartLine(
          ctx,
          item.points,
          point => margin.left + ((point.t - minTime) / xSpan) * chartW,
          point => margin.top + chartH - ((point.v / yMax) * chartH),
          minerWorkChartRangeHours
        );
        const last = item.points[item.points.length - 1];
        const lastX = margin.left + ((last.t - minTime) / xSpan) * chartW;
        const lastY = margin.top + chartH - ((last.v / yMax) * chartH);
        ctx.beginPath();
        ctx.arc(lastX, lastY, 3, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    let globalChartRangeHours = 1;
    let globalChartMetric = "usd";
    function updateGlobalRangeButtons() {
      for (const button of document.querySelectorAll(".global-range-button")) {
        button.classList.toggle("active", Number(button.dataset.range || 0) === globalChartRangeHours);
      }
    }
    function updateGlobalMetricButtons() {
      for (const button of document.querySelectorAll(".global-metric-button")) {
        button.classList.toggle("active", String(button.dataset.metric || "") === globalChartMetric);
      }
      const summary = document.getElementById("globalChartMetricSummary");
      if (summary) {
        summary.textContent = globalChartMetric === "blocks"
          ? "Blocks produced per pool per hour"
          : "USD per pool per hour";
      }
    }
    function setGlobalChartRange(hours) {
      globalChartRangeHours = hours;
      updateGlobalRangeButtons();
      if (lastGlobalData) drawGlobalChart(lastGlobalData);
    }
    function setGlobalChartMetric(metric) {
      if (!["usd", "blocks"].includes(metric)) return;
      globalChartMetric = metric;
      updateGlobalMetricButtons();
      if (lastGlobalData) drawGlobalChart(lastGlobalData);
    }
    function poolUsdPerHour(row) {
      return numberValue(
        row.estimated_usd_avg_hour ??
        row.estimated_usd_recent_hour ??
        row.estimated_wallet_usd_avg_hour ??
        row.estimated_wallet_usd_recent_hour ??
        row.estimated_usd_1h ??
        row.estimated_wallet_usd_1h
      );
    }
    function poolBlocksPerHour(row, snapshot) {
      const direct = firstNumeric(
        row.blocks_per_hour,
        row.blocks_avg_hour,
        row.blocks_recent_hour,
        row.blocks_1h
      );
      if (direct !== null) return direct;
      const blocks = numberValue(row.blocks);
      const windowHours = firstNumeric(row.scan_window_hours, snapshot?.scan_window_hours);
      return blocks !== null && windowHours !== null && windowHours > 0 ? blocks / windowHours : null;
    }
    function poolGlobalChartValue(row, snapshot) {
      if (globalChartMetric === "blocks") return poolBlocksPerHour(row, snapshot);
      return poolUsdPerHour(row);
    }
    function formatGlobalChartValue(value) {
      if (globalChartMetric === "blocks") {
        if (value >= 100) return `${value.toLocaleString(undefined, {maximumFractionDigits: 0})}/h`;
        return `${value.toLocaleString(undefined, {maximumFractionDigits: 2})}/h`;
      }
      return currency(value, "$");
    }
    function drawGlobalChart(data) {
      const canvas = document.getElementById("globalChart");
      const legend = document.getElementById("globalChartLegend");
      const rangeLabel = document.getElementById("globalChartRangeLabel");
      if (!canvas) return;
      updateGlobalRangeButtons();
      updateGlobalMetricButtons();
      const rect = canvas.parentElement.getBoundingClientRect();
      const ratio = window.devicePixelRatio || 1;
      const width = Math.max(360, Math.floor(rect.width));
      const height = Math.max(240, Math.floor(rect.height));
      canvas.width = Math.floor(width * ratio);
      canvas.height = Math.floor(height * ratio);
      const ctx = canvas.getContext("2d");
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "#fbfcfd";
      ctx.fillRect(0, 0, width, height);

      const snapshots = [...(data.history || []), {
        generated_at: data.updated_at,
        scan_window_hours: data.scan_window_hours,
        clusters: data.clusters || [],
      }];
      const timestamped = snapshots
        .map(snapshot => ({
          at: parseDashboardTime(snapshot.generated_at),
          scan_window_hours: snapshot.scan_window_hours,
          pools: snapshot.clusters || [],
        }))
        .filter(snapshot => snapshot.at !== null)
        .sort((a, b) => a.at - b.at);

      const latestTime = timestamped.length ? timestamped[timestamped.length - 1].at : null;
      const cutoff = latestTime === null ? null : latestTime - (globalChartRangeHours * 60 * 60 * 1000);
      const seriesMap = new Map();
      for (const snapshot of timestamped) {
        for (const row of snapshot.pools) {
          const value = poolGlobalChartValue(row, snapshot);
          if (value === null) continue;
          const key = globalPoolIdentity(row);
          if (!key) continue;
          if (!seriesMap.has(key)) {
            seriesMap.set(key, {key, label: globalPoolLabel(row), points: []});
          }
          seriesMap.get(key).points.push({t: snapshot.at, v: value});
        }
      }

      const series = Array.from(seriesMap.values())
        .map(item => {
          return {...item, points: filterChartPointsForRange(item.points, latestTime, globalChartRangeHours)};
        })
        .filter(item => item.points.length)
        .sort((a, b) => (b.points[b.points.length - 1]?.v || 0) - (a.points[a.points.length - 1]?.v || 0));

      const visibleSeries = series.map(item => ({...item, points: smoothChartPoints(item.points, globalChartRangeHours)}));

      if (rangeLabel) {
        const period = latestTime !== null && cutoff !== null ? `${formatChartTime(cutoff, globalChartRangeHours)} to ${formatChartTime(latestTime, globalChartRangeHours)}` : "no pool history yet";
        rangeLabel.textContent = `${chartRangeLabel(globalChartRangeHours)} window | detail ${chartRangeProfile(globalChartRangeHours).detail} | ${period}`;
      }

      if (!visibleSeries.length) {
        if (legend) legend.innerHTML = "";
        ctx.fillStyle = "#617181";
        ctx.font = "13px system-ui, sans-serif";
        ctx.fillText(`No pool ${globalChartMetric === "blocks" ? "block-production" : "earnings"} history available yet.`, 16, 34);
        return;
      }

      const legendLimit = visibleSeries.length;
      if (legend) {
        legend.innerHTML = "";
        for (let i = 0; i < Math.min(visibleSeries.length, legendLimit); i += 1) {
          const item = visibleSeries[i];
          const span = document.createElement("span");
          span.className = "legend-key";
          span.style.setProperty("--key-color", globalPoolColor(item.key));
          span.textContent = item.label;
          legend.appendChild(span);
        }
        if (visibleSeries.length > legendLimit) {
          const more = document.createElement("span");
          more.className = "subtle";
          more.textContent = `+${visibleSeries.length - legendLimit} more`;
          legend.appendChild(more);
        }
      }

      const allPoints = visibleSeries.flatMap(item => item.points);
      const minTime = Math.min(...allPoints.map(point => point.t));
      const maxTime = Math.max(...allPoints.map(point => point.t));
      const maxValue = Math.max(...allPoints.map(point => point.v), 1);
      const margin = {top: 24, right: 18, bottom: 54, left: 58};
      const chartW = width - margin.left - margin.right;
      const chartH = height - margin.top - margin.bottom;
      const xSpan = Math.max(maxTime - minTime, 1);
      const yMax = maxValue * 1.1;
      const yTicks = 5;
      const xTicks = chartTickCount(chartW, globalChartRangeHours);

      ctx.strokeStyle = "#d7dbe0";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(margin.left, margin.top);
      ctx.lineTo(margin.left, margin.top + chartH);
      ctx.lineTo(width - margin.right, margin.top + chartH);
      ctx.stroke();

      ctx.fillStyle = "#617181";
      ctx.font = "12px system-ui, sans-serif";
      ctx.textAlign = "right";
      for (let i = 0; i <= yTicks; i += 1) {
        const ratioY = i / yTicks;
        const value = yMax * (1 - ratioY);
        const y = margin.top + chartH * ratioY;
        ctx.strokeStyle = "#edf0f3";
        ctx.beginPath();
        ctx.moveTo(margin.left, y);
        ctx.lineTo(width - margin.right, y);
        ctx.stroke();
        ctx.fillText(formatGlobalChartValue(value), margin.left - 8, y + 4);
      }

      ctx.textAlign = "center";
      for (let i = 0; i <= xTicks; i += 1) {
        const ratioX = i / xTicks;
        const t = minTime + xSpan * ratioX;
        const x = margin.left + chartW * ratioX;
        ctx.strokeStyle = "#edf0f3";
        ctx.beginPath();
        ctx.moveTo(x, margin.top);
        ctx.lineTo(x, margin.top + chartH);
        ctx.stroke();
        ctx.fillText(formatChartTime(t, globalChartRangeHours), x, height - 18);
      }

      for (let i = 0; i < visibleSeries.length; i += 1) {
        const item = visibleSeries[i];
        const color = globalPoolColor(item.key);
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.lineWidth = 2;
        drawSmoothChartLine(
          ctx,
          item.points,
          point => margin.left + ((point.t - minTime) / xSpan) * chartW,
          point => margin.top + chartH - ((point.v / yMax) * chartH),
          globalChartRangeHours
        );
        const last = item.points[item.points.length - 1];
        const lastX = margin.left + ((last.t - minTime) / xSpan) * chartW;
        const lastY = margin.top + chartH - ((last.v / yMax) * chartH);
        ctx.beginPath();
        ctx.arc(lastX, lastY, 3, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    async function refreshEarnings() {
      if (earningsRefreshInFlight) return;
      earningsRefreshInFlight = true;
      text("priceFeedOutput", "Loading earnings...");
      try {
        const response = await fetch("/api/earnings", {cache: "no-store"});
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "earnings request failed");
        renderEarnings(data);
        earningsLoaded = true;
      } catch (error) {
        text("priceFeedOutput", String(error));
      } finally {
        earningsRefreshInFlight = false;
      }
    }
    async function refreshGlobal() {
      if (globalRefreshInFlight) return;
      globalRefreshInFlight = true;
      try {
        const response = await fetch("/api/global", {cache: "no-store"});
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "global request failed");
        renderGlobal(data);
      } catch (error) {
        text("globalLatestBlock", "error");
        text("globalScannedBlocks", "error");
        text("globalUniqueMiners", "error");
        text("globalScanWindow", "error");
        text("globalAvgBlockSec", "error");
        text("globalTopShare", "error");
        text("globalTableWindow", `Table period: unavailable (${String(error)}).`);
      const table = document.getElementById("globalPoolsTable");
      table.innerHTML = "";
      const tr = document.createElement("tr");
      tr.innerHTML = `<td colspan="8">${escapeHtml(String(error))}</td>`;
      table.appendChild(tr);
      const sourceStatus = document.getElementById("globalSourceStatus");
      if (sourceStatus) {
        sourceStatus.textContent = `status=failed | dashboard-global | no trusted data | ${String(error)}`;
        sourceStatus.className = "subtle down";
      }
      } finally {
        globalLoaded = true;
        globalRefreshInFlight = false;
      }
    }
    function renderEarnings(data) {
      lastEarningsData = data;
      const totals = data.credits?.totals || {};
      const hourly = data.hourly_averages || {};
      const paymentWallet = data.payment_wallet_balance || data.wallet_balance || {};
      const creditWallet = data.credit_wallet_balance || data.wallet?.aggregate || null;
      const walletBdag = hasValue(paymentWallet.total_bdag) ? paymentWallet.total_bdag : (data.credit_balance_check?.actual_wallet_bdag || "n/a");
      const priceOk = data.price?.status === "ok" && data.price?.source === "exchange-average";
      const usdPrice = priceOk ? numberValue(data.price?.usd) : null;
      const zarPrice = priceOk ? numberValue(data.price?.zar) : null;
      const wallet24hBdagValue = firstNumeric(
        data.earnings_24h?.bdag,
        data.earnings_24h?.db_credit_diagnostic_bdag,
        data.credits?.recent_24h?.wallet_total_bdag,
        data.credits?.recent_24h?.total_bdag,
        hourly.wallet_24h_bdag
      );
      const wallet24hAvgValue = wallet24hBdagValue !== null ? wallet24hBdagValue / 24 : firstNumeric(hourly.wallet_24h_avg_bdag_hour);
      const walletAvgHour = wallet24hAvgValue !== null ? wallet24hAvgValue.toFixed(2) : "n/a";
      const avgIncomeHourValue = firstNumeric(
        wallet24hAvgValue,
        hourly.recent_bdag_hour,
        hourly.tracked_avg_bdag_hour,
        hourly.wallet_tracked_avg_bdag_hour,
        hourly.wallet_avg_bdag_hour_since_pool_start
      );
      const avgIncomeHour = avgIncomeHourValue !== null ? avgIncomeHourValue.toFixed(2) : "n/a";
      const avgIncomeUsdHour = numberValue(avgIncomeHour) !== null && usdPrice !== null ? currency(numberValue(avgIncomeHour) * usdPrice, "$") : "n/a";
      const walletRecentHour = hourly.wallet_recent_bdag_hour || data.onchain_earnings?.last_1h?.earned_bdag || "n/a";
      const wallet24hBdag = wallet24hBdagValue !== null ? wallet24hBdagValue.toFixed(2) : "n/a";
      const wallet24hUsdValue = firstNumeric(data.earnings_24h?.usd, wallet24hBdagValue !== null && usdPrice !== null ? wallet24hBdagValue * usdPrice : null, data.wallet_24h_usd);
      const wallet24hZarValue = firstNumeric(data.earnings_24h?.zar, wallet24hBdagValue !== null && zarPrice !== null ? wallet24hBdagValue * zarPrice : null, data.wallet_24h_zar);
      const wallet24hUsd = wallet24hUsdValue !== null ? currency(wallet24hUsdValue, "$") : "n/a";
      const wallet24hZar = wallet24hZarValue !== null ? currency(wallet24hZarValue, "R") : "n/a";
      const currentPriceUsd = priceQuote(usdPrice, "$");
      const currentPriceZar = priceQuote(zarPrice, "R");
      text("earnWalletBdag", walletBdag);
      text("earnAvgIncomeBdagHour", avgIncomeHour);
      text("earnWalletAvgBdagHour", walletAvgHour);
      text("earnWalletRecentBdagHour", walletRecentHour);
      text("earnWallet24hZar", wallet24hZar);
      text("earnWallet24hBdag", wallet24hBdag);
      text("earnWallet24hUsd", wallet24hUsd);
      text("earnAvgIncomeUsdHour", avgIncomeUsdHour);
      text("earnCurrentPriceUsd", currentPriceUsd);
      text("earnCurrentPriceZar", currentPriceZar);
      text("earnTotalUsd", currency(data.wallet_total_usd || data.total_usd, "$"));
      text("earnTotalZar", currency(data.wallet_total_zar || data.total_zar, "R"));

      const addressBody = document.getElementById("addressCreditsTable");
      addressBody.innerHTML = "";
      for (const row of data.credits?.by_address || []) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td class="nowrap" title="${escapeHtml(row.miner_address)}">${escapeShortEth(row.miner_address)}</td><td class="right">${fmt(row.credit_count)}</td><td class="right">${escapeHtml(row.total_bdag)}</td><td class="right">${escapeHtml(row.pending_bdag)}</td><td>${escapeHtml(row.last_credit_at || "")}</td>`;
        addressBody.appendChild(tr);
      }

      const walletBody = document.getElementById("walletSourcesTable");
      walletBody.innerHTML = "";
      if (paymentWallet) {
        const aggregate = paymentWallet;
        const cls = aggregate.status === "ok" ? "ok" : aggregate.status === "partial" ? "warn" : "down";
        const tr = document.createElement("tr");
        tr.innerHTML = `<td>Payment wallet</td><td class="${cls}">${escapeHtml(aggregate.status || "")}</td><td class="right">${escapeHtml(aggregate.total_bdag || "")}</td><td>${escapeHtml(`${aggregate.ok_address_count || 0}/${aggregate.address_count || 0} wallet addresses, ${aggregate.source_truth || "on-chain"}`)}</td>`;
        walletBody.appendChild(tr);
        for (const balance of aggregate.addresses || []) {
          const rowCls = balance.status === "ok" ? "ok" : "warn";
          const detail = balance.status === "ok" ? `${balance.source || ""} ${balance.type || ""}` : (balance.error || "");
          const row = document.createElement("tr");
          row.innerHTML = `<td title="${escapeHtml(balance.address)}">${escapeHtml(balance.address_short || shortEth(balance.address))}</td><td class="${rowCls}">${escapeHtml(balance.status || "")}</td><td class="right">${escapeHtml(balance.bdag || "")}</td><td>${escapeHtml(detail)}</td>`;
          walletBody.appendChild(row);
        }
      }
      if (creditWallet && Number(creditWallet.address_count || 0) > Number(paymentWallet.address_count || 0)) {
        const cls = creditWallet.status === "ok" ? "ok" : creditWallet.status === "partial" ? "warn" : "down";
        const tr = document.createElement("tr");
        tr.innerHTML = `<td>Credit addresses total</td><td class="${cls}">${escapeHtml(creditWallet.status || "")}</td><td class="right">${escapeHtml(creditWallet.total_bdag || "")}</td><td>${escapeHtml(`${creditWallet.ok_address_count || 0}/${creditWallet.address_count || 0} historical credit addresses`)}</td>`;
        walletBody.appendChild(tr);
      }
      for (const source of data.wallet?.sources || []) {
        const cls = source.status === "ok" ? "ok" : "warn";
        const detail = source.error || (source.block_number_balance_updated_at ? `balance block ${source.block_number_balance_updated_at}` : "");
        const tr = document.createElement("tr");
        tr.innerHTML = `<td>${escapeHtml(`Primary ${source.source}`)}</td><td class="${cls}">${escapeHtml(source.status)}</td><td class="right">${escapeHtml(source.bdag || "")}</td><td>${escapeHtml(detail)}</td>`;
        walletBody.appendChild(tr);
      }

      const minerBody = document.getElementById("minerEarningsTable");
      minerBody.innerHTML = "";
      for (const row of visibleMinerRows(data.miner_estimates)) {
        const tr = document.createElement("tr");
        const workers = (row.workers || []).join(", ");
        const creditWorkers = (row.credit_workers || []).join(", ");
        const workerNote = creditWorkers ? `credited: ${shortEth(creditWorkers)}` : "";
        const identity = minerIdentity(row);
        const color = minerColor(identity);
        const name = minerDisplayLabel(row);
        const mac = minerMac(row);
        const identityDetail = mac ? `MAC ${mac}` : (row.device_type || "");
        tr.className = "miner-row";
        tr.style.setProperty("--miner-row-color", transparentColor(color, 0.08));
        tr.style.setProperty("--miner-color", color);
        tr.innerHTML = `<td class="nowrap miner-name"><span class="miner-dot"></span>${escapeHtml(name)} <span class="subtle">${escapeHtml(identityDetail)}</span></td><td class="nowrap" title="${escapeHtml(workers)}">${escapeShortEth(workers)}${workerNote ? ` <span class="subtle">${escapeHtml(workerNote)}</span>` : ""}</td><td class="right">${fmt(row.shares)}</td><td class="right">${escapeHtml(row.work_percent)}</td><td class="right">${fmt(row.credited_blocks || 0)}</td><td class="right">${escapeHtml(row.credited_bdag_total || "0")}</td><td class="right">${fmt(row.blocks_found)}</td><td class="right">${escapeHtml(row.estimated_wallet_bdag_total || row.estimated_bdag_total || "")}</td><td class="right">${escapeHtml(row.estimated_wallet_bdag_recent_hour || row.estimated_bdag_avg_hour || row.estimated_bdag_1h || "")}</td><td class="right">${escapeHtml(row.estimated_wallet_bdag_avg_hour || row.tracked_avg_bdag_hour || "")}</td><td class="right">${currency(row.estimated_wallet_usd_total || row.estimated_usd_total, "$")}</td><td class="right">${currency(row.estimated_wallet_zar_total || row.estimated_zar_total, "R")}</td><td>${escapeHtml(row.last_share_at || "")}</td>`;
        minerBody.appendChild(tr);
      }

      drawEarningsChart(data);
      drawMinerWorkChart(data);
      renderSamplerAlert("earningsSamplerAlert", data);
      renderSamplerAlert("minerWorkSamplerAlert", data);
      text("priceFeedOutput", JSON.stringify({price: data.price, earnings_24h: data.earnings_24h, onchain_earnings: data.onchain_earnings, payment_wallet_balance: data.payment_wallet_balance, credit_wallet_balance: data.credit_wallet_balance, wallet_balance: data.wallet_balance, hourly_averages: data.hourly_averages, credit_balance_check: data.credit_balance_check}, null, 2));
      text("earningsHistoryOutput", JSON.stringify({snapshot_log: data.snapshot_log, recent: (data.history || []).slice(-24)}, null, 2));
    }
    function renderGlobal(data) {
      lastGlobalData = data;
      const liveLatestBlock = firstNumeric(data.chain_latest_block, data.latest_block);
      text("globalLatestBlock", fmt(liveLatestBlock));
      const fetchedBlocks = Number(data.fetched_blocks || 0);
      const requestedBlocks = Number(data.requested_blocks || fetchedBlocks || 0);
      text("globalScannedBlocks", requestedBlocks && fetchedBlocks !== requestedBlocks ? `${fmt(fetchedBlocks)} / ${fmt(requestedBlocks)}` : fmt(fetchedBlocks || requestedBlocks));
      text("globalUniqueMiners", fmt(data.unique_miners));
      text("globalScanWindow", data.scan_window_hours ? `${data.scan_window_hours}h` : "n/a");
      text("globalAvgBlockSec", data.avg_block_seconds ? `${data.avg_block_seconds}s` : "n/a");
      text("globalTopShare", data.clusters?.[0]?.share_percent ? `${data.clusters[0].share_percent}%` : "n/a");
      text("globalTableWindow", formatGlobalTableWindow(data));
      let freshnessLabel = "no trusted data";
      if (data.status === "ok") freshnessLabel = data.cache_hit ? "validated cache" : "fresh chain scan";
      else if (data.status === "stale") freshnessLabel = "last-good stale cache";
      else if (data.status === "degraded") freshnessLabel = "partial chain scan";
      else if (data.status === "deferred") freshnessLabel = "head-only deferred";
      const sourceBits = [
        data.status ? `status=${data.status}` : "",
        data.source_truth || data.source || "",
        data.rpc_source ? `rpc=${data.rpc_source}` : "",
        data.chain_latest_block !== undefined ? `live-tip=${fmt(data.chain_latest_block)}` : "",
        data.latest_order !== undefined ? `order=${fmt(data.latest_order)}` : "",
        data.latest_order_method ? `order-method=${data.latest_order_method}` : "",
        requestedBlocks ? `fetched=${fmt(fetchedBlocks)}/${fmt(requestedBlocks)}` : "",
        data.unknown_blocks ? `unknown=${fmt(data.unknown_blocks)}` : "",
        data.chain_tip_lag_blocks ? `scan-lag=${fmt(data.chain_tip_lag_blocks)} blocks` : "",
        data.cache_tip_lag_blocks !== undefined ? `cache-lag=${fmt(data.cache_tip_lag_blocks)}` : "",
        data.head_only ? "head-only=no production table" : "",
        data.maintenance_deferred ? "maintenance deferred" : "",
        data.cache ? `cache=${data.cache.hit ? "hit" : "miss"}${data.cache.age_seconds !== undefined ? ` age=${fmt(data.cache.age_seconds)}s` : ""}${data.cache.ttl_seconds !== undefined ? ` ttl=${fmt(data.cache.ttl_seconds)}s` : ""}` : "",
        freshnessLabel,
        data.zero_address_blocks ? `zero-address blocks=${fmt(data.zero_address_blocks)}` : "",
      ].filter(Boolean);
      const sourceStatus = document.getElementById("globalSourceStatus");
      if (sourceStatus) {
        const error = data.error ? ` | ${data.error}` : "";
        sourceStatus.textContent = `${sourceBits.join(" | ")}${error}`;
        sourceStatus.className = data.status === "ok" && !data.zero_address_blocks && !data.partial_scan ? "subtle ok" : data.status === "failed" ? "subtle down" : "subtle warn";
      }

      const peerBody = document.getElementById("globalPeerIpsTable");
      peerBody.innerHTML = "";
      for (const item of data.peer_location?.observations || []) {
        const tr = document.createElement("tr");
        const seenBy = (item.seen_by || []).join(", ");
        tr.innerHTML = `<td class="nowrap">${escapeHtml(item.ip || "")}</td><td>${escapeHtml(item.location || "")}</td><td>${escapeHtml(item.country_code || item.country || "")}</td><td>${escapeHtml(item.region_code || item.region || "")}</td><td>${escapeHtml(item.city || "")}</td><td>${escapeHtml(item.asn ? String(item.asn) : "")}</td><td>${escapeHtml(item.org || "")}</td><td class="right nowrap">${escapeHtml(seenBy || "1")}</td>`;
        peerBody.appendChild(tr);
      }

      const body = document.getElementById("globalPoolsTable");
      body.innerHTML = "";
      if (!data.clusters || data.clusters.length === 0) {
        const tr = document.createElement("tr");
        const reason = data.error || "No chain-sourced mining clusters are available for this window.";
        tr.innerHTML = `<td colspan="8">${escapeHtml(reason)}</td>`;
        body.appendChild(tr);
      }
      for (const row of data.clusters || []) {
        const tr = document.createElement("tr");
        const share = row.share_percent ? `${escapeHtml(row.share_percent)}%` : "n/a";
        const poolName = row.pool_name || globalPoolName(row.address);
        const poolAddress = row.address || row.address_short || "";
        const poolIdentity = globalPoolIdentity(row);
        const poolColor = globalPoolColor(poolIdentity);
        const sourceBadge = row.invalid_payout ? ` <span class="down">invalid payout</span>` : (row.local_pool ? ` <span class="subtle">chain confirmed + local shares</span>` : "");
        const poolCell = poolName
          ? `<span class="pool-dot"></span>${escapeHtml(poolName)} <span class="subtle">${escapeShortEth(poolAddress)}</span>${sourceBadge}`
          : `<span class="pool-dot"></span>${escapeShortEth(poolAddress)}`;
        const chainBlocks = firstPresent(row.blocks, row.found_blocks);
        const creditedBdag = firstPresent(row.credited_bdag, row.estimated_bdag);
        const walletBdag = firstPresent(row.estimated_wallet_bdag, row.estimated_bdag);
        tr.className = "pool-row";
        tr.style.setProperty("--pool-row-color", transparentColor(poolColor, 0.08));
        tr.style.setProperty("--pool-color", poolColor);
        tr.innerHTML = `<td class="nowrap pool-name" title="${escapeHtml(poolAddress)}">${poolCell}</td><td class="right">${fmt(chainBlocks)}</td><td class="right">${share}</td><td class="right">${escapeHtml(creditedBdag || "")}</td><td class="right">${escapeHtml(walletBdag || "")}</td><td class="right">${currency(row.estimated_usd, "$")}</td><td class="right">${currency(row.estimated_zar, "R")}</td><td class="nowrap">${escapeHtml(formatDisplayTime(row.last_seen_at))}</td>`;
        body.appendChild(tr);
      }
      drawGlobalChart(data);
    }
    async function action(name) {
      if (busy) return;
      if (name === "clean_restore" && !confirm("This stops the stack, backs up node data, restores the latest snapshot, and starts again. Continue?")) return;
      busy = true;
      for (const btn of document.querySelectorAll("button")) btn.disabled = true;
      try {
        const response = await fetch("/api/action", {
          method: "POST",
          headers: {"content-type": "application/json"},
          body: JSON.stringify({action: name, token: document.getElementById("token").value})
        });
        const payload = await response.json();
        if (!response.ok) alert(payload.error || "Action failed");
        await refresh();
      } catch (error) {
        alert(String(error));
      } finally {
        busy = false;
        for (const btn of document.querySelectorAll("button")) btn.disabled = false;
      }
    }
    setTheme(currentTheme());
    refresh();
    setInterval(refresh, 60000);
    setInterval(() => {
      if (earningsLoaded && (
        !document.getElementById("tab-earnings").classList.contains("hidden")
        || !document.getElementById("tab-miners").classList.contains("hidden")
      )) refreshEarnings();
    }, 60000);
    setInterval(() => { if (globalLoaded && !document.getElementById("tab-global").classList.contains("hidden")) refreshGlobal(); }, 60000);
    window.addEventListener("resize", () => {
      if (lastEarningsData && !document.getElementById("tab-earnings").classList.contains("hidden")) drawEarningsChart(lastEarningsData);
      if (lastEarningsData && !document.getElementById("tab-miners").classList.contains("hidden")) drawMinerWorkChart(lastEarningsData);
      if (lastGlobalData && !document.getElementById("tab-global").classList.contains("hidden")) drawGlobalChart(lastGlobalData);
    });
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "BDAGDashboard/1.0"
    client_disconnect_errors = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)

    def log_client_disconnect(self, exc: BaseException) -> None:
        client = self.client_address[0] if self.client_address else "unknown"
        try:
            with (RUNTIME_DIR / "dashboard-access.log").open("a", encoding="utf-8") as log:
                log.write(f"[{now_iso()}] {client} client disconnected during response: {exc.__class__.__name__}\n")
        except OSError:
            pass

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature.
        try:
            with (RUNTIME_DIR / "dashboard-access.log").open("a", encoding="utf-8") as log:
                log.write(f"[{now_iso()}] {self.address_string()} {fmt % args}\n")
        except OSError:
            pass

    def send_body(self, body: bytes, content_type: str, status: int = 200) -> None:
        try:
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except self.client_disconnect_errors as exc:
            self.log_client_disconnect(exc)

    def send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_body(body, "application/json; charset=utf-8", status)

    def serve_report(self, path: str) -> None:
        rel = unquote(path.removeprefix("/reports/"))
        if not rel or "/" in rel or "\\" in rel:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        report_path = (REPORTS_DIR / rel).resolve()
        reports_root = REPORTS_DIR.resolve()
        if reports_root not in report_path.parents or report_path.suffix.lower() != ".html" or not report_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_body(report_path.read_bytes(), "text/html; charset=utf-8", HTTPStatus.OK)

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name.
        path = urlparse(self.path).path
        if path == "/":
            body = HTML.encode("utf-8")
            self.send_body(body, "text/html; charset=utf-8", HTTPStatus.OK)
            return
        if path.startswith("/reports/"):
            self.serve_report(path)
            return
        if path == "/api/status":
            self.send_json(dashboard_status_payload())
            return
        if path == "/api/token-required":
            self.send_json({"required": token_required(), "token_file": str(RUNTIME_DIR / "dashboard-token.txt")})
            return
        if path == "/api/miners/defaults":
            self.send_json(default_miner_pool_settings())
            return
        if path == "/api/miners/registry":
            self.send_json(read_miner_registry())
            return
        if path == "/api/global":
            try:
                self.send_json(collect_global_dashboard_payload("api-global"))
            except Exception as exc:  # noqa: BLE001
                self.send_json(
                    {
                        "status": "failed",
                        "source": "dashboard-global",
                        "source_truth": GLOBAL_STATS_SOURCE_TRUTH,
                        "schema_version": 2,
                        "error": str(exc),
                        "clusters": [],
                        "history": [],
                    }
                )
            return
        if path == "/api/earnings":
            record_dashboard_earnings_sample("api-earnings")
            self.send_json(cached_payload("earnings", EARNINGS_CACHE_SECONDS, lambda: collect_earnings(include_history=True)))
            return
        if path == "/api/sampler":
            self.send_json(cached_payload("sampler", SAMPLER_CACHE_SECONDS, collect_sampler_status))
            return
        if path == "/api/incidents":
            self.send_json({"generated_at": now_iso(), "incidents": read_recent_incidents(100)})
            return
        if path == "/api/p2p":
            if P2P_GUARD_STATE.exists():
                try:
                    self.send_json(json.loads(P2P_GUARD_STATE.read_text(encoding="utf-8")))
                except json.JSONDecodeError as exc:
                    self.send_json({"generated_at": now_iso(), "error": str(exc)}, status=500)
            else:
                self.send_json({"generated_at": now_iso(), "error": "p2p guard state not available"}, status=404)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - stdlib method name.
        path = urlparse(self.path).path
        if path not in {"/api/action", "/api/miners/scan", "/api/miners/configure", "/api/miners/save-auth"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        length = int(self.headers.get("content-length", "0") or "0")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self.send_json({"error": "invalid JSON"}, status=400)
            return

        if token_required() and payload.get("token") != get_action_token():
            self.send_json({"error": "invalid action token"}, status=403)
            return
        with API_CACHE_LOCK:
            API_CACHE.clear()

        if path == "/api/miners/scan":
            try:
                result = scan_miners(payload.get("target"))
                defaults = default_miner_pool_settings()
                registry = upsert_miner_registry(result.get("miners", []), defaults["pool_url"], defaults["worker_user"])
                registry_by_mac = {
                    normalize_mac(item.get("mac")): item
                    for item in registry.get("miners", [])
                    if normalize_mac(item.get("mac"))
                }
                for miner in result.get("miners", []):
                    mac = normalize_mac(miner.get("mac"))
                    registered = registry_by_mac.get(mac, {}) if mac else {}
                    if registered.get("display_name"):
                        miner["display_name"] = registered["display_name"]
                    miner["identity_key"] = miner_identity_key({**registered, **miner})
                    miner["display_label"] = miner_display_label({**registered, **miner})
                self.send_json(result)
            except Exception as exc:  # noqa: BLE001 - return scanner validation errors to the browser.
                self.send_json({"error": str(exc)}, status=400)
            return

        if path == "/api/miners/save-auth":
            try:
                result = save_miner_admin_password(str(payload.get("admin_password") or ""))
                write_action_state({"name": "save-miner-auth", "status": "ok", "finished_at": now_iso()})
                self.send_json(result)
            except Exception as exc:  # noqa: BLE001
                self.send_json({"error": str(exc)}, status=400)
            return

        if path == "/api/miners/configure":
            ips = payload.get("ips") or []
            if not isinstance(ips, list) or not all(isinstance(item, str) for item in ips):
                self.send_json({"error": "ips must be a list of miner IP addresses"}, status=400)
                return
            if not ips:
                self.send_json({"error": "no miners selected"}, status=400)
                return
            admin_password = str(payload.get("admin_password") or "")
            pool_url = str(payload.get("pool_url") or "")
            worker_user = str(payload.get("worker_user") or "")
            pool_password = str(payload.get("pool_password") or "1234")
            if not admin_password or not pool_url or not worker_user:
                self.send_json({"error": "admin_password, pool_url, and worker_user are required"}, status=400)
                return
            result = configure_miners(
                ips=ips,
                admin_password=admin_password,
                pool_url=pool_url,
                worker_user=worker_user,
                pool_password=pool_password,
                replace_existing=True,
            )
            mark_configured_miners(result.get("results", []), pool_url, worker_user)
            write_action_state(
                {
                    "name": "configure-miners",
                    "status": result["status"],
                    "finished_at": now_iso(),
                    "miner_count": len(ips),
                    "pool_url": pool_url,
                    "worker_user": worker_user,
                    "results": [
                        {
                            "ip": item.get("ip"),
                            "status": item.get("status"),
                            "active": item.get("active"),
                            "backup_path": item.get("backup_path"),
                            "error": item.get("error"),
                            "delete_errors": item.get("delete_errors"),
                        }
                        for item in result.get("results", [])
                    ],
                }
            )
            self.send_json(result)
            return

        action = payload.get("action")
        if action == "start":
            result = start_background_action("start", [sys.executable, str(WATCHDOG), "--repair", "start", "--reason", "dashboard start"], "dashboard start")
        elif action == "restart":
            result = start_background_action("restart", [sys.executable, str(WATCHDOG), "--repair", "restart", "--reason", "dashboard restart"], "dashboard restart")
        elif action == "clean_restore":
            result = start_background_action("clean-restore", [sys.executable, str(WATCHDOG), "--repair", "clean", "--reason", "dashboard clean restore"], "dashboard clean restore")
        elif action == "handoff":
            path = make_handoff()
            result = {"status": "ok", "path": str(path)}
            write_action_state({"name": "codex-handoff", "status": "ok", "finished_at": now_iso(), "path": str(path)})
        else:
            self.send_json({"error": f"unknown action: {action}"}, status=400)
            return
        self.send_json(result)


def main() -> int:
    ensure_runtime()
    history_warmup = warm_dashboard_history_caches()
    warmed = history_warmup.get("histories", {}) if isinstance(history_warmup, dict) else {}
    if warmed:
        summary = ", ".join(
            f"{name}:{payload.get('chart_rows', 0) if isinstance(payload, dict) else 0}"
            for name, payload in warmed.items()
        )
        print(f"Dashboard history warmup {history_warmup.get('status', 'unknown')}: {summary}")
    if token_required():
        token = get_action_token()
        print(f"Action token file: {RUNTIME_DIR / 'dashboard-token.txt'}")
        print(f"Action token: {token}")
    start_earnings_sampler()
    start_global_sampler()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"BlockDAG dashboard listening on http://{HOST}:{PORT}")
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Shared BlockDAG pool operations for the dashboard and watchdog."""

from __future__ import annotations

import base64
from collections import Counter, deque
import json
import os
import ipaddress
import platform
import re
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


def path_from_env(name: str, default: str | Path, base: Path | None = None) -> Path:
    raw = os.environ.get(name)
    path = Path(raw).expanduser() if raw else Path(default).expanduser()
    if not path.is_absolute():
        path = (base or Path.cwd()) / path
    return path.resolve()


def split_env_list(name: str, default: str) -> list[str]:
    raw = os.environ[name] if name in os.environ else default
    return [item.strip() for item in re.split(r"[,;]", raw) if item.strip()]


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def env_int(name: str, default: int, minimum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def env_float(name: str, default: float, minimum: float | None = None) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def unique_names(names: list[str]) -> list[str]:
    result: list[str] = []
    for name in names:
        if name and name not in result:
            result.append(name)
    return result


def normalize_os_name(raw: str | None = None) -> str:
    value = (raw or platform.system() or "unknown").strip().lower()
    if value in {"darwin", "mac", "macos"}:
        return "darwin"
    if value.startswith("win"):
        return "windows"
    if value.startswith("linux"):
        return "linux"
    return value or "unknown"


def normalize_arch_name(raw: str | None = None) -> str:
    value = (raw or platform.machine() or "unknown").strip().lower()
    if value in {"x86_64", "amd64"}:
        return "amd64"
    if value in {"aarch64", "arm64"}:
        return "arm64"
    if value.startswith("armv7") or value.startswith("armv6"):
        return "arm"
    return value or "unknown"


def detect_total_memory_bytes(os_name: str | None = None) -> int | None:
    system = normalize_os_name(os_name)
    if system == "linux":
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
        except (OSError, ValueError):
            return None
    if system == "darwin":
        try:
            raw = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True, timeout=1).strip()
            return int(raw)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
    return None


def detect_hardware_model(os_name: str | None = None) -> str:
    system = normalize_os_name(os_name)
    if system == "linux":
        for path in (Path("/proc/device-tree/model"), Path("/sys/firmware/devicetree/base/model")):
            try:
                return path.read_text(encoding="utf-8").replace("\x00", "").strip()
            except OSError:
                continue
    if system == "darwin":
        try:
            return subprocess.check_output(["sysctl", "-n", "hw.model"], text=True, timeout=1).strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    return ""


_HOST_RUNTIME_PROFILE_CACHE: dict[str, Any] | None = None


def host_runtime_profile(force_refresh: bool = False) -> dict[str, Any]:
    global _HOST_RUNTIME_PROFILE_CACHE
    if _HOST_RUNTIME_PROFILE_CACHE is not None and not force_refresh:
        return dict(_HOST_RUNTIME_PROFILE_CACHE)

    os_name = normalize_os_name()
    arch = normalize_arch_name()
    cpu_count = max(1, os.cpu_count() or 1)
    memory_bytes = detect_total_memory_bytes(os_name)
    memory_gib = round(memory_bytes / (1024 ** 3), 2) if memory_bytes else None
    hardware_model = detect_hardware_model(os_name)
    model_lower = hardware_model.lower()
    override = HOST_PROFILE_OVERRIDE
    profile_source = "auto"
    if override not in {"", "auto"}:
        profile = override
        profile_source = "env"
    elif os_name == "linux" and "raspberry pi 5" in model_lower:
        profile = "pi5"
    elif cpu_count <= 4 or (memory_bytes is not None and memory_bytes <= 6 * 1024 ** 3):
        profile = "constrained"
    elif cpu_count <= 8 or (memory_bytes is not None and memory_bytes <= 16 * 1024 ** 3):
        profile = "standard"
    else:
        profile = "large"

    payload = {
        "os": os_name,
        "arch": arch,
        "profile": profile,
        "profile_source": profile_source,
        "cpu_count": cpu_count,
        "memory_bytes": memory_bytes,
        "memory_gib": memory_gib,
        "hardware_model": hardware_model,
        "psi_available": os_name == "linux" and Path("/proc/pressure/io").exists(),
    }
    _HOST_RUNTIME_PROFILE_CACHE = dict(payload)
    return payload


DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = path_from_env("BDAG_PROJECT_ROOT", DEFAULT_PROJECT_ROOT, DEFAULT_PROJECT_ROOT)
RUNTIME_DIR = path_from_env("BDAG_RUNTIME_DIR", PROJECT_ROOT / "ops" / "runtime", PROJECT_ROOT)
LOG_DIR = RUNTIME_DIR / "logs"
SHARED_STATUS_CACHE_FILE = RUNTIME_DIR / "shared-status-cache.json"
STATUS_SAMPLER_FILE = RUNTIME_DIR / "status-sampler.json"
SYNC_PROGRESS_HEALTH_STATE_FILE = RUNTIME_DIR / "sync-progress-health-state.json"
SYNC_PROGRESS_ACTIVE_LOOKBACK_SECONDS = int(os.environ.get("BDAG_SYNC_PROGRESS_ACTIVE_LOOKBACK_SECONDS", "2700"))
POOL_ENV_FILE = path_from_env("BDAG_POOL_ENV_FILE", PROJECT_ROOT / "asic-pool" / ".env", PROJECT_ROOT)
DATA_DIR = path_from_env("BDAG_DATA_DIR", PROJECT_ROOT / "data", PROJECT_ROOT)

POOL_CONTAINER = os.environ.get("BDAG_POOL_CONTAINER", "asic-pool")
POOL_CONTAINERS = unique_names([POOL_CONTAINER, *split_env_list("BDAG_POOL_CONTAINERS", "")])
POOL_DB_CONTAINER = os.environ.get("BDAG_POOL_DB_CONTAINER", "pool-db")
POOL_DB_USER = os.environ.get("BDAG_POOL_DB_USER", os.environ.get("POSTGRES_USER", "bdag_pool"))
POOL_DB_NAME = os.environ.get("BDAG_POOL_DB_NAME", os.environ.get("POSTGRES_DB", "bdagpool"))
PRIMARY_NODE_SERVICE = (
    os.environ.get("BDAG_NODE_SERVICE", "").strip()
    or (split_env_list("BDAG_NODE_SERVICES", "bdag-miner-node-1") or ["bdag-miner-node-1"])[0]
)
NODES = [PRIMARY_NODE_SERVICE]
STACK_SERVICES = split_env_list(
    "BDAG_STACK_SERVICES",
    "pool-db,bdag-miner-node-1,asic-pool",
)
SERVICES = unique_names([*STACK_SERVICES, POOL_DB_CONTAINER, *NODES, *POOL_CONTAINERS])
NODE_DATA_DIRS = split_env_list("BDAG_NODE_DATA_DIRS", "node1")
NODE_METRIC_PORTS = {
    "bdag-miner-node-1": int(os.environ.get("BDAG_NODE1_METRICS_PORT", "6061")),
}
NATIVE_SYNC_LEAD_THRESHOLD = int(os.environ.get("BDAG_NATIVE_SYNC_LEAD_THRESHOLD_BLOCKS", "5"))


def primary_node_service() -> str:
    return NODES[0] if NODES else PRIMARY_NODE_SERVICE


def node_role(name: str) -> str:
    return "managed" if name == primary_node_service() else "external"


def node_health_scope(name: str) -> str:
    return "production" if name == primary_node_service() else "ignored"


def node_affects_production_health(name: str) -> bool:
    return name == primary_node_service()

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
SENSITIVE_ARG_RE = re.compile(r"((?:--rpcpass|--password|--pass)(?:=|\s+))([^\s\"'\]]+)", re.IGNORECASE)
SENSITIVE_ENV_RE = re.compile(
    r"((?:NODE_RPC_PASS|POOL_PRIVATE_KEY|POSTGRES_PASSWORD|MINER_ADMIN_PASSWORD|PASSWORD|PASS)=)([^\s,;\"'\]]+)",
    re.IGNORECASE,
)
BLOCK_RE = re.compile(r"Imported new chain segment\s+.*?number\s*=?\s*([0-9,]+)")
MAIN_ORDER_RE = re.compile(r"bestMainOrder=([0-9,]+)")
PEER_AHEAD_RE = re.compile(r"peer main order exceeds.*?by\s+([0-9,]+)\s+blocks")
SYNC_DELTA_RE = re.compile(r"syncPeerDelta=([0-9,]+)")
PROMETHEUS_METRIC_RE = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)(?:\s+|\{[^}]*\}\s+)([-+0-9.eE]+)$")
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
NODE_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\|(\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?")

MINER_BACKUP_DIR = RUNTIME_DIR / "miner-backups"
MINER_HTTP_TIMEOUT = env_float("BDAG_MINER_HTTP_TIMEOUT", 2.5, minimum=0.1)
MINER_HASHRATE_PROBE_TIMEOUT = env_float("BDAG_MINER_HASHRATE_PROBE_TIMEOUT", 1.0, minimum=0.1)
MINER_HASHRATE_PROBE_WORKERS = env_int("BDAG_MINER_HASHRATE_PROBE_WORKERS", 8, minimum=1)
MINER_SCAN_TIMEOUT = env_float("BDAG_MINER_SCAN_TIMEOUT", 0.8, minimum=0.1)
MINER_SCAN_WORKERS = env_int("BDAG_MINER_SCAN_WORKERS", 64, minimum=1)
MINER_SCAN_MAX_TARGETS = env_int("BDAG_MINER_SCAN_MAX_TARGETS", 1024, minimum=1)
MINER_LOGIN_KEY_HEX = "21" * 16
MINER_ZERO_IV_HEX = "00" * 16
MINER_REGISTRY_FILE = RUNTIME_DIR / "miners.json"
MINER_RETIREMENTS_FILE = RUNTIME_DIR / "miner-retirements.json"
MINER_ADMIN_PASSWORD_FILE = RUNTIME_DIR / "miner-admin-password.txt"
EARNINGS_SNAPSHOT_FILE = RUNTIME_DIR / "earnings-snapshots.jsonl"
EARNINGS_ONCHAIN_CACHE_FILE = RUNTIME_DIR / "earnings-onchain-cache.json"
PRICE_CACHE_FILE = RUNTIME_DIR / "price-cache.json"
GLOBAL_CACHE_FILE = RUNTIME_DIR / "global-cache.json"
GLOBAL_HISTORY_FILE = RUNTIME_DIR / "global-history.jsonl"
GLOBAL_HISTORY_STATE_FILE = RUNTIME_DIR / "global-history-state.json"
GLOBAL_POOL_LABEL_FILE = RUNTIME_DIR / "global-pool-labels.json"
SYNC_COORDINATOR_STATE_FILE = RUNTIME_DIR / "sync-coordinator-state.json"
HOST_PRESSURE_STATE_FILE = RUNTIME_DIR / "host-pressure-state.json"
PEER_GEO_CACHE_FILE = RUNTIME_DIR / "peer-geo-cache.json"
NODE_TEMPLATE_PROBE_CACHE_FILE = RUNTIME_DIR / "node-template-probe-cache.json"
WEI_PER_BDAG = Decimal("1000000000000000000")
ZERO_ETH_ADDRESS = "0x" + ("0" * 40)
ETH_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
try:
    WALLET_MATCH_TOLERANCE_BDAG = Decimal(os.environ.get("BDAG_WALLET_MATCH_TOLERANCE_BDAG", "1000"))
except InvalidOperation:
    WALLET_MATCH_TOLERANCE_BDAG = Decimal("1000")
BDAG_BITMART_SYMBOL = os.environ.get("BDAG_BITMART_SYMBOL", "BDAG_USDT")
BDAG_COINSTORE_SYMBOL = os.environ.get("BDAG_COINSTORE_SYMBOL", "BDAGUSDT")
BDAG_PIONEX_SYMBOL = os.environ.get("BDAG_PIONEX_SYMBOL", "BDAG_USDT")
USD_ZAR_RATE_URL = os.environ.get("BDAG_USD_ZAR_RATE_URL", "https://open.er-api.com/v6/latest/USD")
PRICE_CACHE_TTL_SECONDS = int(os.environ.get("BDAG_PRICE_CACHE_TTL_SECONDS", "300"))
PRICE_MIN_OK_SOURCES = int(os.environ.get("BDAG_PRICE_MIN_OK_SOURCES", "2"))
GLOBAL_CACHE_TTL_SECONDS = int(os.environ.get("BDAG_GLOBAL_CACHE_TTL_SECONDS", "300"))
GLOBAL_BLOCK_WINDOW = int(os.environ.get("BDAG_GLOBAL_BLOCK_WINDOW", "2048"))
GLOBAL_DEFERRED_BLOCK_WINDOW = env_int("BDAG_GLOBAL_DEFERRED_BLOCK_WINDOW", 64, minimum=0)
GLOBAL_DEFERRED_RPC_WORKERS = env_int("BDAG_GLOBAL_DEFERRED_RPC_WORKERS", 2, minimum=1)
GLOBAL_RPC_WORKERS = env_int("BDAG_GLOBAL_RPC_WORKERS", 24, minimum=1)
# Corechain MaxTxPerBlock is derived from 1 MiB block payload / 10 byte minimum tx payload + 1.
BDAG_MAX_TRANSACTIONS_PER_BLOCK = env_int("BDAG_MAX_TRANSACTIONS_PER_BLOCK", 104858, minimum=1)
GLOBAL_HISTORY_LIMIT = int(os.environ.get("BDAG_GLOBAL_HISTORY_LIMIT", "9000"))
GLOBAL_HISTORY_COMPACT_MULTIPLIER = max(1, int(os.environ.get("BDAG_GLOBAL_HISTORY_COMPACT_MULTIPLIER", "2")))
NODE_EVM_RPC_PORT = int(os.environ.get("BDAG_NODE_EVM_RPC_PORT", "18545"))
EARNINGS_HISTORY_RETENTION_SECONDS = int(os.environ.get("BDAG_EARNINGS_HISTORY_RETENTION_SECONDS", str(35 * 86400)))
EARNINGS_DASHBOARD_HISTORY_SECONDS = int(os.environ.get("BDAG_EARNINGS_DASHBOARD_HISTORY_SECONDS", str(31 * 86400)))
EARNINGS_SNAPSHOT_EXPECTED_INTERVAL_SECONDS = int(os.environ.get("BDAG_WATCHDOG_EARNINGS_SNAPSHOT_INTERVAL_SECONDS", "120"))
EARNINGS_ONCHAIN_CACHE_SECONDS = int(os.environ.get("BDAG_EARNINGS_ONCHAIN_CACHE_SECONDS", "120"))
EARNINGS_ONCHAIN_WINDOW_ENABLED = env_bool("BDAG_EARNINGS_ONCHAIN_WINDOW_ENABLED", True)
LOCAL_EVM_BALANCE_PROBE_ENABLED = env_bool("BDAG_LOCAL_EVM_BALANCE_PROBE_ENABLED", False)
EARNINGS_DERIVED_HISTORY_ENABLED = env_bool("BDAG_EARNINGS_DERIVED_HISTORY_ENABLED", True)
EARNINGS_DERIVED_HISTORY_BUCKET_SECONDS = env_int("BDAG_EARNINGS_DERIVED_HISTORY_BUCKET_SECONDS", 300, minimum=60)
DASHBOARD_HISTORY_HOT_SECONDS = env_int("BDAG_DASHBOARD_HISTORY_HOT_SECONDS", 3600, minimum=60)
DASHBOARD_HISTORY_HOT_STEP_SECONDS = env_int("BDAG_DASHBOARD_HISTORY_HOT_STEP_SECONDS", 60, minimum=60)
DASHBOARD_HISTORY_HOURLY_SECONDS = env_int("BDAG_DASHBOARD_HISTORY_HOURLY_SECONDS", 24 * 3600, minimum=3600)
DASHBOARD_HISTORY_HOURLY_STEP_SECONDS = env_int("BDAG_DASHBOARD_HISTORY_HOURLY_STEP_SECONDS", 3600, minimum=3600)
DASHBOARD_HISTORY_DAILY_SECONDS = env_int("BDAG_DASHBOARD_HISTORY_DAILY_SECONDS", 7 * 86400, minimum=86400)
DASHBOARD_HISTORY_DAILY_STEP_SECONDS = env_int("BDAG_DASHBOARD_HISTORY_DAILY_STEP_SECONDS", 86400, minimum=86400)
DASHBOARD_HISTORY_WEEKLY_SECONDS = env_int("BDAG_DASHBOARD_HISTORY_WEEKLY_SECONDS", 31 * 86400, minimum=7 * 86400)
DASHBOARD_HISTORY_WEEKLY_STEP_SECONDS = env_int("BDAG_DASHBOARD_HISTORY_WEEKLY_STEP_SECONDS", 7 * 86400, minimum=7 * 86400)
DASHBOARD_HISTORY_DISK_DIR = path_from_env("BDAG_DASHBOARD_HISTORY_DISK_DIR", RUNTIME_DIR / "dashboard-history", PROJECT_ROOT)
PEER_GEO_CACHE_TTL_SECONDS = int(os.environ.get("BDAG_PEER_GEO_CACHE_TTL_SECONDS", "86400"))
PEER_GEO_LOOKUP_TIMEOUT = float(os.environ.get("BDAG_PEER_GEO_LOOKUP_TIMEOUT", "8.0"))
MINER_STALE_SECONDS = int(os.environ.get("BDAG_MINER_STALE_SECONDS", "120"))
POOL_ACTIVITY_LOG_LINES = int(os.environ.get("BDAG_POOL_ACTIVITY_LOG_LINES", "2000"))
POOL_CONNECTED_STALE_SECONDS = int(os.environ.get("BDAG_POOL_CONNECTED_STALE_SECONDS", str(MINER_STALE_SECONDS)))
MINER_REGISTRY_POOL_LOG_STALE_SECONDS = int(
    os.environ.get("BDAG_MINER_REGISTRY_POOL_LOG_STALE_SECONDS", str(max(POOL_CONNECTED_STALE_SECONDS * 2, 600)))
)
MINER_REGISTRY_EXPECTED_ASIC_STALE_SECONDS = int(
    os.environ.get("BDAG_MINER_REGISTRY_EXPECTED_ASIC_STALE_SECONDS", "86400")
)
try:
    MINER_LOW_DIFF_THRESHOLD = Decimal(os.environ.get("BDAG_MINER_LOW_DIFF_THRESHOLD", "0.02"))
except InvalidOperation:
    MINER_LOW_DIFF_THRESHOLD = Decimal("0.02")
MINER_LOW_DIFF_MIN_SUBMITS = int(os.environ.get("BDAG_MINER_LOW_DIFF_MIN_SUBMITS", "100"))
POOL_TEMPLATE_FREEZE_SECONDS = int(os.environ.get("BDAG_POOL_TEMPLATE_FREEZE_SECONDS", "120"))
POOL_VALID_SHARE_STALE_SECONDS = int(os.environ.get("BDAG_POOL_VALID_SHARE_STALE_SECONDS", "300"))
POOL_JOB_NOTIFY_STALE_SECONDS = int(os.environ.get("BDAG_POOL_JOB_NOTIFY_STALE_SECONDS", "180"))
POOL_DUP_BLOCK_STORM_COUNT = int(os.environ.get("BDAG_POOL_DUP_BLOCK_STORM_COUNT", "25"))
POOL_DUP_BLOCK_STORM_RATIO = int(os.environ.get("BDAG_POOL_DUP_BLOCK_STORM_RATIO", "3"))
POOL_STALE_JOB_STORM_COUNT = int(os.environ.get("BDAG_POOL_STALE_JOB_STORM_COUNT", "25"))
POOL_BLOCK_SUBMIT_ERROR_STORM_COUNT = int(os.environ.get("BDAG_POOL_BLOCK_SUBMIT_ERROR_STORM_COUNT", "10"))
POOL_BLOCK_SUBMIT_ERROR_STORM_RATIO = int(os.environ.get("BDAG_POOL_BLOCK_SUBMIT_ERROR_STORM_RATIO", "2"))
POOL_BLOCK_SUBMIT_ZERO_SUCCESS_FAILURE_COUNT = int(
    os.environ.get("BDAG_POOL_BLOCK_SUBMIT_ZERO_SUCCESS_FAILURE_COUNT", "25")
)
POOL_BLOCK_SUBMIT_ZERO_SUCCESS_ERROR_COUNT = int(
    os.environ.get("BDAG_POOL_BLOCK_SUBMIT_ZERO_SUCCESS_ERROR_COUNT", "8")
)
POOL_ACCEPTED_JOB_EXPIRED_STORM_COUNT = int(
    os.environ.get("BDAG_POOL_ACCEPTED_JOB_EXPIRED_STORM_COUNT", "25")
)
POOL_ACCEPTED_JOB_EXPIRED_STORM_RATIO = int(
    os.environ.get("BDAG_POOL_ACCEPTED_JOB_EXPIRED_STORM_RATIO", "2")
)
POOL_METRICS_PORT = int(os.environ.get("BDAG_POOL_METRICS_PORT", "9090"))
POOL_METRICS_TIMEOUT = float(os.environ.get("BDAG_POOL_METRICS_TIMEOUT", "2.0"))
POOL_SUBMIT_RECOVERY_RECENT_SECONDS = int(os.environ.get("BDAG_POOL_SUBMIT_RECOVERY_RECENT_SECONDS", "180"))
POOL_SUBMIT_RECOVERY_ACCEPTED_RESUME_SECONDS = int(
    os.environ.get("BDAG_POOL_SUBMIT_RECOVERY_ACCEPTED_RESUME_SECONDS", "90")
)
POOL_RPC_REFUSED_WARN_SECONDS = int(os.environ.get("BDAG_POOL_RPC_REFUSED_WARN_SECONDS", "120"))
NODE_IMPORT_STALE_SECONDS = int(os.environ.get("BDAG_NODE_IMPORT_STALE_SECONDS", "180"))
NODE_LAG_WARN_BLOCKS = int(os.environ.get("BDAG_NODE_LAG_WARN_BLOCKS", "5"))
NODE_P2P_ERROR_WARN_COUNT = int(os.environ.get("BDAG_NODE_P2P_ERROR_WARN_COUNT", "10"))
NODE_ORPHAN_ERROR_STORM_COUNT = int(os.environ.get("BDAG_NODE_ORPHAN_ERROR_STORM_COUNT", "20"))
NODE_MINING_RPC_PORT = int(os.environ.get("BDAG_NODE_MINING_RPC_PORT", "38131"))
NODE_MINING_RPC_USER = os.environ.get("BDAG_NODE_MINING_RPC_USER", "test")
NODE_MINING_RPC_PASS = os.environ.get("BDAG_NODE_MINING_RPC_PASS", "test")
NODE_TEMPLATE_PROBE_CACHE_SECONDS = int(os.environ.get("BDAG_NODE_TEMPLATE_PROBE_CACHE_SECONDS", "60"))
NODE_TEMPLATE_PROBE_SAMPLES = max(1, int(os.environ.get("BDAG_NODE_TEMPLATE_PROBE_SAMPLES", "1")))
NODE_TEMPLATE_PROBE_TIMEOUT = float(os.environ.get("BDAG_NODE_TEMPLATE_PROBE_TIMEOUT", "1.5"))
NODE_CHAIN_RPC_TIMEOUT = float(os.environ.get("BDAG_NODE_CHAIN_RPC_TIMEOUT", "8.0"))
NODE_CHAIN_RPC_RETRIES = max(1, int(os.environ.get("BDAG_NODE_CHAIN_RPC_RETRIES", "2")))
HOST_PRESSURE_IOWAIT_WARN_PERCENT = env_float("BDAG_HOST_PRESSURE_IOWAIT_WARN_PERCENT", 25.0, minimum=0.0)
HOST_PRESSURE_IOWAIT_WARN_SAMPLES = env_int("BDAG_HOST_PRESSURE_IOWAIT_WARN_SAMPLES", 3, minimum=2)
HOST_PRESSURE_HISTORY_SAMPLES = max(
    HOST_PRESSURE_IOWAIT_WARN_SAMPLES,
    env_int("BDAG_HOST_PRESSURE_HISTORY_SAMPLES", 6, minimum=HOST_PRESSURE_IOWAIT_WARN_SAMPLES),
)
HTTP_USER_AGENT = os.environ.get("BDAG_HTTP_USER_AGENT", "blockdag-dashboard/1.0")
SHARED_STATUS_CACHE_ENABLED = env_bool("BDAG_SHARED_STATUS_CACHE_ENABLED", True)
SHARED_STATUS_CACHE_SECONDS = env_float("BDAG_SHARED_STATUS_CACHE_SECONDS", 3.0, minimum=0.0)
STATUS_SAMPLER_ENABLED = env_bool("BDAG_STATUS_SAMPLER_ENABLED", True)
STATUS_SAMPLER_MAX_AGE_SECONDS = env_float("BDAG_STATUS_SAMPLER_MAX_AGE_SECONDS", 12.0, minimum=0.0)
STATUS_SAMPLER_BYPASS = env_bool("BDAG_STATUS_SAMPLER_BYPASS", False)
HOST_PROFILE_OVERRIDE = os.environ.get("BDAG_HOST_PROFILE", "auto").strip().lower() or "auto"
ADAPTIVE_CONCURRENCY_ENABLED = env_bool("BDAG_ADAPTIVE_CONCURRENCY_ENABLED", True)
ADAPTIVE_IOWAIT_WARN_PERCENT = env_float(
    "BDAG_ADAPTIVE_IOWAIT_WARN_PERCENT",
    HOST_PRESSURE_IOWAIT_WARN_PERCENT,
    minimum=0.0,
)
ADAPTIVE_IO_SOME_AVG10_WARN = env_float("BDAG_ADAPTIVE_IO_SOME_AVG10_WARN", 20.0, minimum=0.0)
ADAPTIVE_CPU_SOME_AVG10_WARN = env_float("BDAG_ADAPTIVE_CPU_SOME_AVG10_WARN", 80.0, minimum=0.0)
ADAPTIVE_CHAIN_RPC_WARN_MS = env_float("BDAG_ADAPTIVE_CHAIN_RPC_WARN_MS", 1000.0, minimum=0.0)
BACKGROUND_MAINTENANCE_BACKOFF_ENABLED = env_bool("BDAG_BACKGROUND_MAINTENANCE_BACKOFF_ENABLED", True)
BACKGROUND_MAINTENANCE_SYNC_BACKOFF_BLOCKS = env_int("BDAG_BACKGROUND_MAINTENANCE_SYNC_BACKOFF_BLOCKS", 0, minimum=0)
BACKGROUND_MAINTENANCE_IOWAIT_WARN_PERCENT = env_float(
    "BDAG_BACKGROUND_MAINTENANCE_IOWAIT_WARN_PERCENT",
    HOST_PRESSURE_IOWAIT_WARN_PERCENT,
    minimum=0.0,
)
BACKGROUND_MAINTENANCE_IO_SOME_AVG10_WARN = env_float(
    "BDAG_BACKGROUND_MAINTENANCE_IO_SOME_AVG10_WARN",
    20.0,
    minimum=0.0,
)
BACKGROUND_MAINTENANCE_CPU_SOME_AVG10_WARN = env_float(
    "BDAG_BACKGROUND_MAINTENANCE_CPU_SOME_AVG10_WARN",
    80.0,
    minimum=0.0,
)
BACKGROUND_MAINTENANCE_CHAIN_RPC_WARN_MS = env_float(
    "BDAG_BACKGROUND_MAINTENANCE_CHAIN_RPC_WARN_MS",
    ADAPTIVE_CHAIN_RPC_WARN_MS,
    minimum=0.0,
)


def is_transient_template_tx_error_text(text: str) -> bool:
    lowered = text.lower()
    return (
        "blockdag is downloading tx" in lowered
        or "blockdag is downloading blocks" in lowered
        or "client in initial download" in lowered
        or "nonce too low" in lowered
    )


def is_no_miner_sync_noise(item: Any) -> bool:
    text = str(item)
    return (
        "live mining template probes" in text
        or "pool recently saw RPC connection refused" in text
        or text.startswith("pool is waiting for node sync to finish")
    )


DEFAULT_GLOBAL_POOL_LABELS = {}


@dataclass
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    elapsed: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "elapsed": self.elapsed,
        }


def ensure_runtime() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    MINER_BACKUP_DIR.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def strip_ansi(value: str | bytes) -> str:
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    return ANSI_RE.sub("", value)


def redact_sensitive_text(value: str) -> str:
    text = strip_ansi(value)
    text = SENSITIVE_ARG_RE.sub(r"\1<redacted>", text)
    text = SENSITIVE_ENV_RE.sub(r"\1<redacted>", text)
    return text


def run(command: list[str], timeout: int = 20) -> CommandResult:
    start = time.time()
    try:
        proc = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return CommandResult(
            command=command,
            returncode=proc.returncode,
            stdout=strip_ansi(proc.stdout),
            stderr=strip_ansi(proc.stderr),
            elapsed=round(time.time() - start, 3),
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            command=command,
            returncode=124,
            stdout=strip_ansi(exc.stdout or ""),
            stderr=strip_ansi((exc.stderr or "") + f"\nTimed out after {timeout}s"),
            elapsed=round(time.time() - start, 3),
        )
    except FileNotFoundError as exc:
        return CommandResult(
            command=command,
            returncode=127,
            stdout="",
            stderr=str(exc),
            elapsed=round(time.time() - start, 3),
        )


def read_env_file_value(path: Path, name: str) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value
    return None


def read_env_value(name: str) -> str | None:
    aliases = {
        "MINING_ADDRESS": ["BDAG_MINING_ADDRESS"],
        "POOL_PORT": ["BDAG_POOL_PORT"],
    }.get(name, [])
    for env_name in [*aliases, name]:
        value = os.environ.get(env_name)
        if value:
            return value
    return read_env_file_value(POOL_ENV_FILE, name)


def node_mining_rpc_credentials() -> tuple[str, str]:
    user = (
        os.environ.get("BDAG_NODE_MINING_RPC_USER")
        or os.environ.get("BDAG_NODE_RPC_USER")
        or os.environ.get("NODE_RPC_USER")
        or read_env_value("BDAG_NODE_MINING_RPC_USER")
        or read_env_value("BDAG_NODE_RPC_USER")
        or read_env_value("NODE_RPC_USER")
        or NODE_MINING_RPC_USER
        or "test"
    )
    password = (
        os.environ.get("BDAG_NODE_MINING_RPC_PASS")
        or os.environ.get("BDAG_NODE_RPC_PASS")
        or os.environ.get("NODE_RPC_PASS")
        or read_env_value("BDAG_NODE_MINING_RPC_PASS")
        or read_env_value("BDAG_NODE_RPC_PASS")
        or read_env_value("NODE_RPC_PASS")
        or NODE_MINING_RPC_PASS
        or "test"
    )
    return user, password


def valid_eth_address(value: Any) -> bool:
    return bool(ETH_ADDRESS_RE.fullmatch(str(value or "")))


def is_spendable_eth_address(value: Any) -> bool:
    address = str(value or "")
    return valid_eth_address(address) and address.lower() != ZERO_ETH_ADDRESS


def configured_command(name: str, default: list[str]) -> list[str]:
    if name not in os.environ:
        return default
    raw = os.environ.get(name, "").strip()
    return shlex.split(raw) if raw else []


def decimal_to_str(value: Decimal, places: int = 2) -> str:
    quant = Decimal(1).scaleb(-places)
    return format(value.quantize(quant), "f")


def percent_to_str(value: Decimal) -> str:
    places = 2
    if Decimal("0") < abs(value) < Decimal("0.01"):
        places = 6
    elif Decimal("0.01") <= abs(value) < Decimal("1"):
        places = 4
    return decimal_to_str(value, places=places)


def safe_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def wei_to_bdag(value: str | int | Decimal | None) -> Decimal:
    try:
        return Decimal(str(value or "0")) / WEI_PER_BDAG
    except (InvalidOperation, ValueError):
        return Decimal("0")


def seconds_since_epoch() -> int:
    return int(time.time())


def parse_proc_pressure(text: str) -> dict[str, float]:
    pressure: dict[str, float] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        scope = parts[0]
        if scope not in {"some", "full"}:
            continue
        for item in parts[1:]:
            if "=" not in item:
                continue
            key, raw = item.split("=", 1)
            if key in {"avg10", "avg60", "avg300", "total"}:
                value = safe_float(raw)
                if value is not None:
                    pressure[f"{scope}_{key}"] = value
    return pressure


def parse_proc_stat_cpu(text: str) -> dict[str, int]:
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] != "cpu":
            continue
        values = []
        for raw in parts[1:]:
            try:
                values.append(int(raw))
            except ValueError:
                values.append(0)
        if len(values) < 5:
            return {}
        return {
            "total": sum(values),
            "idle": values[3],
            "iowait": values[4],
        }
    return {}


def host_pressure_iowait_sustained(samples: list[dict[str, Any]], threshold: float, sample_count: int) -> bool:
    recent = samples[-sample_count:]
    if len(recent) < sample_count:
        return False
    values = [safe_float(item.get("iowait_percent")) for item in recent]
    return all(value is not None and value >= threshold for value in values)


def host_pressure_warning_messages(pressure: dict[str, Any]) -> list[str]:
    samples = pressure.get("samples") if isinstance(pressure.get("samples"), list) else []
    if not pressure.get("iowait_warning_active"):
        return []
    values = [
        safe_float(item.get("iowait_percent"))
        for item in samples[-HOST_PRESSURE_IOWAIT_WARN_SAMPLES:]
    ]
    values = [value for value in values if value is not None]
    if values:
        avg = sum(values) / len(values)
        current = safe_float(pressure.get("iowait_percent"), avg) or avg
        detail = f"current={current:.2f}% avg={avg:.2f}%"
    else:
        detail = f"threshold={HOST_PRESSURE_IOWAIT_WARN_PERCENT:.2f}%"
    return [
        "host IO wait is sustained across recent dashboard samples "
        f"({detail}, threshold={HOST_PRESSURE_IOWAIT_WARN_PERCENT:.2f}%)"
    ]


def collect_host_pressure() -> dict[str, Any]:
    pressure: dict[str, Any] = {
        "loadavg_1m": None,
        "loadavg_5m": None,
        "loadavg_15m": None,
        "io_some_avg10": None,
        "io_full_avg10": None,
        "iowait_percent": None,
        "cpu_busy_percent": None,
        "iowait_warn_percent": HOST_PRESSURE_IOWAIT_WARN_PERCENT,
        "iowait_warn_samples": HOST_PRESSURE_IOWAIT_WARN_SAMPLES,
        "samples": [],
        "iowait_warning_active": False,
        "cpu_some_avg10": None,
        "memory_some_avg10": None,
    }
    try:
        load_parts = Path("/proc/loadavg").read_text(encoding="utf-8").split()
        if len(load_parts) >= 3:
            pressure["loadavg_1m"] = safe_float(load_parts[0])
            pressure["loadavg_5m"] = safe_float(load_parts[1])
            pressure["loadavg_15m"] = safe_float(load_parts[2])
    except OSError:
        pass

    for name in ("io", "cpu", "memory"):
        try:
            parsed = parse_proc_pressure(Path(f"/proc/pressure/{name}").read_text(encoding="utf-8"))
        except OSError:
            continue
        pressure[f"{name}_some_avg10"] = parsed.get("some_avg10")
        pressure[f"{name}_full_avg10"] = parsed.get("full_avg10")

    try:
        cpu = parse_proc_stat_cpu(Path("/proc/stat").read_text(encoding="utf-8"))
    except OSError:
        cpu = {}
    previous = read_json_file(HOST_PRESSURE_STATE_FILE, {})
    if cpu:
        previous_cpu = previous.get("cpu") if isinstance(previous, dict) else {}
        total_delta = int(cpu.get("total", 0)) - int((previous_cpu or {}).get("total", 0) or 0)
        idle_delta = int(cpu.get("idle", 0)) - int((previous_cpu or {}).get("idle", 0) or 0)
        iowait_delta = int(cpu.get("iowait", 0)) - int((previous_cpu or {}).get("iowait", 0) or 0)
        if total_delta > 0 and previous_cpu:
            pressure["iowait_percent"] = round(max(0.0, iowait_delta * 100.0 / total_delta), 2)
            pressure["cpu_busy_percent"] = round(max(0.0, (total_delta - idle_delta) * 100.0 / total_delta), 2)
        previous_samples = previous.get("samples") if isinstance(previous, dict) and isinstance(previous.get("samples"), list) else []
        samples = [
            item for item in previous_samples
            if isinstance(item, dict) and item.get("iowait_percent") is not None
        ]
        if pressure["iowait_percent"] is not None:
            samples.append(
                {
                    "epoch": time.time(),
                    "generated_at": now_iso(),
                    "iowait_percent": pressure["iowait_percent"],
                    "cpu_busy_percent": pressure["cpu_busy_percent"],
                }
            )
        samples = samples[-HOST_PRESSURE_HISTORY_SAMPLES:]
        pressure["samples"] = samples
        pressure["iowait_warning_active"] = host_pressure_iowait_sustained(
            samples,
            HOST_PRESSURE_IOWAIT_WARN_PERCENT,
            HOST_PRESSURE_IOWAIT_WARN_SAMPLES,
        )
        write_json_file(HOST_PRESSURE_STATE_FILE, {"generated_at": now_iso(), "cpu": cpu, "samples": samples})
    return pressure


ADAPTIVE_WORKER_PROFILE_LIMITS: dict[str, dict[str, int]] = {
    "pi5": {
        "global_rpc": 6,
        "miner_scan": 16,
        "miner_hashrate": 2,
        "peer_geo": 2,
        "wallet_balance": 4,
        "price_fetch": 2,
    },
    "constrained": {
        "global_rpc": 8,
        "miner_scan": 24,
        "miner_hashrate": 4,
        "peer_geo": 4,
        "wallet_balance": 4,
        "price_fetch": 2,
    },
    "standard": {
        "global_rpc": 16,
        "miner_scan": 48,
        "miner_hashrate": 6,
        "peer_geo": 6,
        "wallet_balance": 8,
        "price_fetch": 3,
    },
    "large": {
        "global_rpc": 32,
        "miner_scan": 96,
        "miner_hashrate": 12,
        "peer_geo": 8,
        "wallet_balance": 12,
        "price_fetch": 3,
    },
}
ADAPTIVE_WORKER_MINIMUMS: dict[str, int] = {
    "global_rpc": 1,
    "miner_scan": 1,
    "miner_hashrate": 1,
    "peer_geo": 1,
    "wallet_balance": 1,
    "price_fetch": 1,
}


def adaptive_pressure_level(pressure: dict[str, Any] | None) -> str:
    if not isinstance(pressure, dict):
        return "unknown"
    iowait = safe_float(pressure.get("iowait_percent"))
    io_some = safe_float(pressure.get("io_some_avg10"))
    cpu_some = safe_float(pressure.get("cpu_some_avg10"))
    chain_rpc_latency = safe_float(pressure.get("chain_rpc_latency_ms"))
    if (
        bool(pressure.get("iowait_warning_active"))
        or (iowait is not None and iowait >= ADAPTIVE_IOWAIT_WARN_PERCENT)
        or (io_some is not None and io_some >= ADAPTIVE_IO_SOME_AVG10_WARN)
        or (cpu_some is not None and cpu_some >= ADAPTIVE_CPU_SOME_AVG10_WARN)
        or (chain_rpc_latency is not None and chain_rpc_latency >= ADAPTIVE_CHAIN_RPC_WARN_MS)
    ):
        return "high"
    if (
        (iowait is not None and iowait >= ADAPTIVE_IOWAIT_WARN_PERCENT / 2)
        or (io_some is not None and io_some >= ADAPTIVE_IO_SOME_AVG10_WARN / 2)
        or (cpu_some is not None and cpu_some >= ADAPTIVE_CPU_SOME_AVG10_WARN / 2)
        or (chain_rpc_latency is not None and chain_rpc_latency >= ADAPTIVE_CHAIN_RPC_WARN_MS / 2)
    ):
        return "moderate"
    return "low"


def adaptive_worker_count(
    kind: str,
    configured_limit: int,
    item_count: int,
    pressure: dict[str, Any] | None = None,
) -> int:
    requested = max(1, min(max(1, configured_limit), max(1, item_count)))
    if not ADAPTIVE_CONCURRENCY_ENABLED:
        return requested

    profile = host_runtime_profile()
    profile_limits = ADAPTIVE_WORKER_PROFILE_LIMITS.get(
        str(profile.get("profile") or ""),
        ADAPTIVE_WORKER_PROFILE_LIMITS["standard"],
    )
    cpu_scaled_ceiling = max(1, int(profile.get("cpu_count") or 1) * 2)
    ceiling = max(1, min(profile_limits.get(kind, cpu_scaled_ceiling), cpu_scaled_ceiling, configured_limit))
    count = max(ADAPTIVE_WORKER_MINIMUMS.get(kind, 1), min(requested, ceiling))
    level = adaptive_pressure_level(pressure)
    if level == "high":
        count = min(count, max(ADAPTIVE_WORKER_MINIMUMS.get(kind, 1), count // 4 or 1))
    elif level == "moderate":
        count = min(count, max(ADAPTIVE_WORKER_MINIMUMS.get(kind, 1), count // 2 or 1))
    return max(1, min(count, requested))


def adaptive_worker_budgets(pressure: dict[str, Any] | None = None) -> dict[str, Any]:
    configured = {
        "global_rpc": GLOBAL_RPC_WORKERS,
        "miner_scan": MINER_SCAN_WORKERS,
        "miner_hashrate": MINER_HASHRATE_PROBE_WORKERS,
        "peer_geo": 8,
        "wallet_balance": 8,
        "price_fetch": 3,
    }
    workers = {
        kind: adaptive_worker_count(kind, value, value, pressure)
        for kind, value in configured.items()
    }
    return {
        "enabled": ADAPTIVE_CONCURRENCY_ENABLED,
        "pressure_level": adaptive_pressure_level(pressure),
        "configured_caps": configured,
        "workers": workers,
        "host_profile": host_runtime_profile(),
    }


def read_json_file(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return fallback


def write_json_file(path: Path, payload: Any, mode: int | None = None) -> None:
    ensure_runtime()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if mode is not None:
        tmp.chmod(mode)
    os.replace(tmp, path)


def write_status_sampler_payload(payload: dict[str, Any], include_logs: bool = True) -> None:
    write_json_file(
        STATUS_SAMPLER_FILE,
        {
            "schema_version": 1,
            "updated_at": now_iso(),
            "epoch": time.time(),
            "include_logs": include_logs,
            "payload": payload,
        },
        mode=0o600,
    )


def read_status_sampler_payload(include_logs: bool, max_age_seconds: float | None = None) -> dict[str, Any] | None:
    if not STATUS_SAMPLER_ENABLED or STATUS_SAMPLER_BYPASS or STATUS_SAMPLER_MAX_AGE_SECONDS <= 0:
        return None
    if max_age_seconds is not None and max_age_seconds <= 0:
        return None
    sampler_max_age = STATUS_SAMPLER_MAX_AGE_SECONDS
    if max_age_seconds is not None:
        sampler_max_age = min(sampler_max_age, max(0.0, float(max_age_seconds)))
    if sampler_max_age <= 0:
        return None

    snapshot = read_json_file(STATUS_SAMPLER_FILE, {})
    if not isinstance(snapshot, dict):
        return None
    payload = snapshot.get("payload")
    if not isinstance(payload, dict):
        return None
    sampled_at = safe_float(snapshot.get("epoch"), 0.0) or 0.0
    age = max(0.0, time.time() - sampled_at)
    if age > sampler_max_age:
        return None

    result = dict(payload)
    result["age_seconds"] = round((safe_float(result.get("age_seconds"), 0.0) or 0.0) + age, 3)
    stale_after = safe_float(result.get("stale_after_seconds"))
    if stale_after is not None:
        result["fresh"] = bool(result["age_seconds"] <= stale_after)
    result["status_sampler"] = {
        "enabled": True,
        "hit": True,
        "file": str(STATUS_SAMPLER_FILE),
        "include_logs": bool(snapshot.get("include_logs")),
        "requested_include_logs": include_logs,
        "age_seconds": round(age, 3),
        "max_age_seconds": sampler_max_age,
    }
    return result


def read_sync_coordinator_state() -> dict[str, Any]:
    state = read_json_file(SYNC_COORDINATOR_STATE_FILE, {})
    return state if isinstance(state, dict) else {}


def docker_compose_command(*args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--env-file",
        str(POOL_ENV_FILE),
        "-f",
        str(PROJECT_ROOT / "docker-compose.yml"),
        *args,
    ]


def read_jsonl_file(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    if limit is not None:
        lines = lines[-limit:]
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def write_jsonl_file(path: Path, rows: list[dict[str, Any]], mode: int | None = None) -> None:
    ensure_runtime()
    path.write_text("\n".join(json.dumps(row, separators=(",", ":")) for row in rows if isinstance(row, dict)) + ("\n" if rows else ""), encoding="utf-8")
    if mode is not None:
        path.chmod(mode)


def append_jsonl_file(path: Path, row: dict[str, Any], mode: int | None = None) -> None:
    ensure_runtime()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":")) + "\n")
    if mode is not None:
        path.chmod(mode)


def count_text_lines(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            return sum(1 for _line in handle)
    except OSError:
        return 0


def compact_jsonl_file(path: Path, limit: int, mode: int | None = None) -> int:
    rows = read_jsonl_file(path, limit=limit)
    write_jsonl_file(path, rows[-limit:], mode=mode)
    return len(rows[-limit:])


@dataclass(frozen=True)
class DashboardHistoryTier:
    name: str
    storage: str
    min_age_seconds: int
    max_age_seconds: int
    step_seconds: int


def dashboard_history_ram_dir() -> Path:
    configured = os.environ.get("BDAG_DASHBOARD_HISTORY_RAM_DIR")
    if configured:
        return path_from_env("BDAG_DASHBOARD_HISTORY_RAM_DIR", configured, PROJECT_ROOT)
    candidates: list[Path] = []
    if Path("/dev/shm").exists():
        candidates.append(Path("/dev/shm"))
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        candidates.append(Path(xdg_runtime).expanduser())
    for base in candidates:
        try:
            if base.exists() and os.access(base, os.W_OK):
                return (base / f"{PROJECT_ROOT.name}-dashboard-history").resolve()
        except OSError:
            continue
    return (DASHBOARD_HISTORY_DISK_DIR / "hot-fallback").resolve()


def dashboard_history_tiers() -> list[DashboardHistoryTier]:
    hot_max = max(DASHBOARD_HISTORY_HOT_SECONDS, DASHBOARD_HISTORY_HOT_STEP_SECONDS)
    hourly_max = max(DASHBOARD_HISTORY_HOURLY_SECONDS, hot_max + DASHBOARD_HISTORY_HOURLY_STEP_SECONDS)
    daily_max = max(DASHBOARD_HISTORY_DAILY_SECONDS, hourly_max + DASHBOARD_HISTORY_DAILY_STEP_SECONDS)
    weekly_max = max(DASHBOARD_HISTORY_WEEKLY_SECONDS, daily_max + DASHBOARD_HISTORY_WEEKLY_STEP_SECONDS)
    return [
        DashboardHistoryTier("minute", "ram", 0, hot_max, DASHBOARD_HISTORY_HOT_STEP_SECONDS),
        DashboardHistoryTier("hour", "disk", hot_max, hourly_max, DASHBOARD_HISTORY_HOURLY_STEP_SECONDS),
        DashboardHistoryTier("day", "disk", hourly_max, daily_max, DASHBOARD_HISTORY_DAILY_STEP_SECONDS),
        DashboardHistoryTier("week", "disk", daily_max, weekly_max, DASHBOARD_HISTORY_WEEKLY_STEP_SECONDS),
    ]


def dashboard_history_tier_path(kind: str, tier: DashboardHistoryTier) -> Path:
    safe_kind = re.sub(r"[^a-zA-Z0-9_.-]+", "-", kind).strip("-") or "default"
    base = dashboard_history_ram_dir() if tier.storage == "ram" else DASHBOARD_HISTORY_DISK_DIR
    return base / safe_kind / f"{tier.name}.json"


def dashboard_history_state_path(kind: str) -> Path:
    safe_kind = re.sub(r"[^a-zA-Z0-9_.-]+", "-", kind).strip("-") or "default"
    return DASHBOARD_HISTORY_DISK_DIR / safe_kind / "state.json"


def history_snapshot_epoch(snapshot: dict[str, Any]) -> float | None:
    parsed = parse_earnings_timestamp(snapshot.get("generated_at") or snapshot.get("updated_at"))
    return parsed.timestamp() if parsed is not None else None


def _read_dashboard_history_tier(path: Path) -> list[dict[str, Any]]:
    payload = read_json_file(path, {})
    rows = payload.get("rows") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _dedupe_history_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, tuple[float, dict[str, Any]]] = {}
    for row in rows:
        epoch = history_snapshot_epoch(row)
        if epoch is None:
            continue
        key = str(row.get("generated_at") or row.get("updated_at") or epoch)
        existing = by_key.get(key)
        if existing is None or epoch >= existing[0]:
            by_key[key] = (epoch, row)
    return [row for _epoch, row in sorted(by_key.values(), key=lambda item: item[0])]


def build_dashboard_history_tiers(
    snapshots: list[dict[str, Any]],
    compact_snapshot,
    snapshot_has_data,
) -> tuple[dict[str, list[dict[str, Any]]], float | None]:
    timed: list[tuple[float, dict[str, Any]]] = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict) or not snapshot_has_data(snapshot):
            continue
        compacted = compact_snapshot(snapshot)
        if not isinstance(compacted, dict):
            continue
        epoch = history_snapshot_epoch(compacted)
        if epoch is None:
            continue
        timed.append((epoch, compacted))
    if not timed:
        return {tier.name: [] for tier in dashboard_history_tiers()}, None

    latest_epoch = max(epoch for epoch, _snapshot in timed)
    buckets: dict[str, dict[int, tuple[float, dict[str, Any]]]] = {
        tier.name: {} for tier in dashboard_history_tiers()
    }
    for epoch, snapshot in timed:
        age = max(0, int(latest_epoch - epoch))
        for tier in dashboard_history_tiers():
            if age > tier.max_age_seconds:
                continue
            if tier.min_age_seconds and age <= tier.min_age_seconds:
                continue
            bucket_key = int(epoch // max(1, tier.step_seconds))
            existing = buckets[tier.name].get(bucket_key)
            if existing is None or epoch >= existing[0]:
                buckets[tier.name][bucket_key] = (epoch, snapshot)
            break

    tier_rows: dict[str, list[dict[str, Any]]] = {}
    for tier in dashboard_history_tiers():
        values = sorted(buckets[tier.name].values(), key=lambda item: item[0])
        tier_rows[tier.name] = [snapshot for _epoch, snapshot in values]
    return tier_rows, latest_epoch


def write_dashboard_history_tiers(
    kind: str,
    tier_rows: dict[str, list[dict[str, Any]]],
    latest_epoch: float | None,
    source_sample_count: int | None = None,
) -> None:
    state_rows: dict[str, int] = {}
    for tier in dashboard_history_tiers():
        rows = tier_rows.get(tier.name, [])
        path = dashboard_history_tier_path(kind, tier)
        write_json_file(
            path,
            {
                "schema_version": 1,
                "kind": kind,
                "tier": tier.name,
                "storage": tier.storage,
                "step_seconds": tier.step_seconds,
                "min_age_seconds": tier.min_age_seconds,
                "max_age_seconds": tier.max_age_seconds,
                "latest_epoch": latest_epoch,
                "updated_at": now_iso(),
                "rows": rows,
            },
            mode=0o600,
        )
        state_rows[tier.name] = len(rows)

    previous_state = read_json_file(dashboard_history_state_path(kind), {})
    if source_sample_count is None and isinstance(previous_state, dict):
        source_sample_count = safe_int(previous_state.get("source_sample_count"), 0)
    write_json_file(
        dashboard_history_state_path(kind),
        {
            "schema_version": 1,
            "kind": kind,
            "updated_at": now_iso(),
            "latest_epoch": latest_epoch,
            "ram_dir": str(dashboard_history_ram_dir()),
            "disk_dir": str(DASHBOARD_HISTORY_DISK_DIR),
            "source_sample_count": source_sample_count or 0,
            "tiers": [
                {
                    "name": tier.name,
                    "storage": tier.storage,
                    "step_seconds": tier.step_seconds,
                    "min_age_seconds": tier.min_age_seconds,
                    "max_age_seconds": tier.max_age_seconds,
                    "rows": state_rows.get(tier.name, 0),
                }
                for tier in dashboard_history_tiers()
            ],
        },
        mode=0o600,
    )


def load_dashboard_history_tiers(kind: str) -> tuple[list[dict[str, Any]], bool, bool]:
    rows: list[dict[str, Any]] = []
    any_file = False
    hot_file_exists = False
    for tier in dashboard_history_tiers():
        path = dashboard_history_tier_path(kind, tier)
        if path.exists():
            any_file = True
            if tier.name == "minute":
                hot_file_exists = True
        rows.extend(_read_dashboard_history_tier(path))
    return _dedupe_history_rows(rows), any_file, hot_file_exists


def rebuild_dashboard_history_from_source(
    kind: str,
    source_file: Path,
    compact_snapshot,
    snapshot_has_data,
) -> tuple[list[dict[str, Any]], int]:
    source_rows = read_jsonl_file(source_file)
    tier_rows, latest_epoch = build_dashboard_history_tiers(source_rows, compact_snapshot, snapshot_has_data)
    write_dashboard_history_tiers(kind, tier_rows, latest_epoch, source_sample_count=len(source_rows))
    rows = [row for tier in dashboard_history_tiers() for row in tier_rows.get(tier.name, [])]
    return _dedupe_history_rows(rows), len(source_rows)


def read_dashboard_history(
    kind: str,
    source_file: Path,
    compact_snapshot,
    snapshot_has_data,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    rows, any_file, hot_file_exists = load_dashboard_history_tiers(kind)
    state = read_json_file(dashboard_history_state_path(kind), {})
    source_sample_count = safe_int(state.get("source_sample_count") if isinstance(state, dict) else None, len(rows)) or 0
    if source_file.exists() and (not any_file or not hot_file_exists):
        rows, source_sample_count = rebuild_dashboard_history_from_source(kind, source_file, compact_snapshot, snapshot_has_data)
    if limit is not None:
        rows = rows[-max(0, limit):]
    return rows, source_sample_count


def update_dashboard_history_with_snapshot(
    kind: str,
    source_file: Path,
    snapshot: dict[str, Any],
    compact_snapshot,
    snapshot_has_data,
) -> None:
    rows, any_file, _hot_file_exists = load_dashboard_history_tiers(kind)
    state = read_json_file(dashboard_history_state_path(kind), {})
    source_sample_count = safe_int(state.get("source_sample_count") if isinstance(state, dict) else None, 0) or 0
    if not any_file and source_file.exists():
        source_rows = read_jsonl_file(source_file)
        source_sample_count = len(source_rows)
        candidates = source_rows
    else:
        source_sample_count += 1
        candidates = rows + [snapshot]
    tier_rows, latest_epoch = build_dashboard_history_tiers(candidates, compact_snapshot, snapshot_has_data)
    write_dashboard_history_tiers(kind, tier_rows, latest_epoch, source_sample_count=source_sample_count)


def warm_dashboard_history_caches() -> dict[str, Any]:
    """Rebuild RAM hot tiers from append logs after dashboard startup."""
    warmed: dict[str, Any] = {"status": "ok", "histories": {}}
    targets = [
        ("global", GLOBAL_HISTORY_FILE, compact_global_snapshot_for_history, global_snapshot_has_plot_data),
        ("earnings", EARNINGS_SNAPSHOT_FILE, compact_earnings_snapshot, earnings_snapshot_has_plot_data),
    ]
    for kind, source_file, compact_snapshot, snapshot_has_data in targets:
        try:
            rows, sample_count = rebuild_dashboard_history_from_source(kind, source_file, compact_snapshot, snapshot_has_data)
            warmed["histories"][kind] = {
                "status": "ok",
                "source": str(source_file),
                "sample_count": sample_count,
                "chart_rows": len(rows),
                "ram_dir": str(dashboard_history_ram_dir()),
                "disk_dir": str(DASHBOARD_HISTORY_DISK_DIR),
            }
        except Exception as exc:  # noqa: BLE001 - dashboard startup must continue without history warmup.
            warmed["status"] = "degraded"
            warmed["histories"][kind] = {
                "status": "failed",
                "source": str(source_file),
                "error": str(exc),
            }
    return warmed


def merge_unique_strings(*values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        if value is None:
            continue
        items = value if isinstance(value, list) else [value]
        for item in items:
            text = str(item or "").strip()
            if text and text not in result:
                result.append(text)
    return result


def local_ipv4_addresses() -> list[str]:
    addresses: list[str] = []

    def add(value: str) -> None:
        if value.startswith("127.") or value == "0.0.0.0":
            return
        if value not in addresses:
            addresses.append(value)

    route = run(["ip", "-4", "route", "get", "1.1.1.1"], timeout=5).stdout
    route_src = re.search(r"\bsrc\s+((?:\d{1,3}\.){3}\d{1,3})\b", route)
    if route_src:
        add(route_src.group(1))

    hostname_ips = run(["hostname", "-I"], timeout=5).stdout
    for match in IPV4_RE.findall(hostname_ips):
        add(match)

    ip_addr = run(["ip", "-4", "-o", "addr", "show", "scope", "global"], timeout=5).stdout
    for match in re.findall(r"\binet\s+((?:\d{1,3}\.){3}\d{1,3})/", ip_addr):
        add(match)

    return addresses


class MinerAPIError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def is_lan_ipv4(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.version == 4 and (address.is_private or address.is_link_local)


def is_docker_bridge_pool_log_client(ip: str, mac: str = "") -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return address.version == 4 and address in ipaddress.ip_network("172.16.0.0/12")


def is_docker_bridge_pseudo_miner(item: dict[str, Any]) -> bool:
    """Return true for Docker bridge Stratum clients that are not physical miners."""
    ip = str(item.get("ip") or "")
    if not is_docker_bridge_pool_log_client(ip):
        return False
    device_type = str(item.get("device_type") or "").lower()
    discovered_by = str(item.get("discovered_by") or "").lower()
    sources = {str(source).lower() for source in merge_unique_strings(item.get("sources"))}
    return device_type != "asic" or discovered_by == "pool-log" or "pool-log" in sources


def is_configured_miner_record(item: dict[str, Any]) -> bool:
    """Return true only for miners explicitly managed/configured for this pool."""
    return bool(item.get("managed") or item.get("configured") or item.get("last_configured_ok"))


def is_earnings_wallet_miner(item: dict[str, Any]) -> bool:
    """Return true for miner rows allowed to participate in wallet earnings."""
    if is_docker_bridge_pseudo_miner(item):
        return False
    if str(item.get("credit_scope") or "") == "idle-registered-asic":
        return False
    if item.get("managed") or item.get("configured") or item.get("connected"):
        return True
    if item.get("credit_workers") and (safe_int(item.get("shares"), 0) > 0 or safe_int(item.get("credited_blocks"), 0) > 0):
        return True
    return False


def is_ipv4(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.version == 4


MAC_RE = re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b")


def normalize_mac(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", ":")
    if not text:
        return ""
    if re.fullmatch(r"[0-9a-f]{12}", text):
        text = ":".join(text[index : index + 2] for index in range(0, 12, 2))
    return text if MAC_RE.fullmatch(text) else ""


def read_neighbor_macs() -> dict[str, str]:
    result = run(["ip", "neigh", "show"], timeout=5)
    if not result.ok:
        return {}
    neighbors: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if not parts or not is_ipv4(parts[0]):
            continue
        if "lladdr" not in parts:
            continue
        index = parts.index("lladdr")
        if index + 1 >= len(parts):
            continue
        mac = normalize_mac(parts[index + 1])
        if mac:
            neighbors[parts[0]] = mac
    return neighbors


def mac_for_ip(ip: str, neighbors: dict[str, str] | None = None) -> str:
    if not is_lan_ipv4(ip):
        return ""
    if neighbors is None:
        neighbors = read_neighbor_macs()
    return normalize_mac(neighbors.get(ip))


def miner_mac_from_payload(miner: dict[str, Any], ip: str, neighbors: dict[str, str] | None = None) -> str:
    for key in ("mac", "mac_address", "macAddress", "ethaddr", "hwaddr"):
        mac = normalize_mac(miner.get(key))
        if mac:
            return mac
    return mac_for_ip(ip, neighbors)


def miner_observation_epoch(miner: dict[str, Any]) -> int:
    values = []
    for key in ("last_seen_epoch", "last_pool_seen_epoch", "last_share_epoch"):
        try:
            values.append(int(miner.get(key, 0) or 0))
        except (TypeError, ValueError):
            pass
    return max(values or [0])


def merge_miner_records(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    result = dict(existing)
    old_ip = str(existing.get("ip") or "")
    new_ip = str(incoming.get("ip") or old_ip)
    for key, value in incoming.items():
        if value not in (None, "", [], {}):
            result[key] = value

    result["ip"] = new_ip
    result["sources"] = merge_unique_strings(existing.get("sources"), incoming.get("sources"))
    result["last_workers"] = merge_unique_strings(existing.get("last_workers"), incoming.get("last_workers"))
    result["last_ports"] = merge_unique_strings(existing.get("last_ports"), incoming.get("last_ports"))
    result["ip_history"] = merge_unique_strings(existing.get("ip_history"), old_ip, incoming.get("ip_history"), new_ip)
    result["managed"] = bool(existing.get("managed") or incoming.get("managed"))
    result["last_configured_ok"] = bool(existing.get("last_configured_ok") or incoming.get("last_configured_ok"))

    if existing.get("device_type") == "asic" or incoming.get("device_type") == "asic":
        result["device_type"] = "asic"
    if existing.get("model") and incoming.get("model") == "Stratum client":
        result["model"] = existing["model"]
    if existing.get("discovered_by") == "lan-scan" and incoming.get("discovered_by") == "pool-log":
        result["discovered_by"] = existing["discovered_by"]

    mac = normalize_mac(incoming.get("mac")) or normalize_mac(existing.get("mac"))
    if mac:
        result["mac"] = mac
        result["device_id"] = f"mac:{mac}"
    return result


def miner_display_identity(item: dict[str, Any]) -> str:
    mac = normalize_mac(item.get("mac"))
    if mac:
        return f"mac:{mac}"
    device_id = str(item.get("device_id") or "").strip()
    if device_id.startswith("mac:"):
        return device_id
    return ""


def mac_suffix(value: Any, width: int = 3) -> str:
    mac = normalize_mac(value)
    if not mac:
        return ""
    compact = mac.replace(":", "")
    return compact[-max(1, width) :]


def miner_identity_key(item: dict[str, Any]) -> str:
    mac = normalize_mac(item.get("mac"))
    if not mac:
        device_id = str(item.get("device_id") or "").strip().lower()
        if device_id.startswith("mac:"):
            mac = normalize_mac(device_id.removeprefix("mac:"))
    return f"mac:{mac}" if mac else ""


def miner_display_label(item: dict[str, Any]) -> str:
    mac = normalize_mac(item.get("mac"))
    if not mac:
        device_id = str(item.get("device_id") or "").strip().lower()
        if device_id.startswith("mac:"):
            mac = normalize_mac(device_id.removeprefix("mac:"))
    name = str(item.get("display_name") or item.get("name") or "").strip()
    if name and mac:
        return f"{name}-{mac_suffix(mac)}"
    if name:
        return f"{name}-unknown-mac"
    if mac:
        return mac
    return "unknown-mac"


def assign_miner_display_names(miners: list[dict[str, Any]]) -> None:
    """Preserve explicit names only.

    Fresh release installs must not invent site-specific ASIC names. The
    dashboard defaults to the MAC address and appends a MAC suffix to any
    explicit human name, so duplicate names cannot hide distinct devices.
    """
    for item in miners:
        if item.get("display_name") is not None:
            item["display_name"] = str(item.get("display_name") or "").strip()


def is_pool_log_only_miner(item: dict[str, Any]) -> bool:
    if item.get("managed"):
        return False
    device_type = str(item.get("device_type") or "")
    discovered_by = str(item.get("discovered_by") or "")
    return device_type == "stratum" or discovered_by == "pool-log"


def is_known_primary_pool_log_miner(item: dict[str, Any]) -> bool:
    defaults = default_miner_pool_settings()
    expected_pool_url = str(item.get("expected_pool_url") or "").strip()
    workers = [str(worker).lower() for worker in merge_unique_strings(item.get("last_workers"))]
    has_wallet_worker = any(re.fullmatch(r"0x[a-f0-9]{40}", worker) for worker in workers)
    return bool(
        is_pool_log_only_miner(item)
        and normalize_mac(item.get("mac"))
        and expected_pool_url == defaults["pool_url"]
        and has_wallet_worker
    )


def prune_inactive_miner_records(miners: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now_epoch = seconds_since_epoch()
    asic_current_ips: set[str] = set()
    asic_known_ips: set[str] = set()
    for item in miners:
        ip = str(item.get("ip") or "")
        if not is_ipv4(ip):
            continue
        if item.get("device_type") != "asic" and not item.get("managed"):
            continue
        asic_current_ips.add(ip)
        for historical_ip in merge_unique_strings(item.get("ip_history"), ip):
            if is_ipv4(historical_ip):
                asic_known_ips.add(historical_ip)

    pruned: list[dict[str, Any]] = []
    for item in miners:
        ip = str(item.get("ip") or "")
        if not is_ipv4(ip):
            continue
        if is_docker_bridge_pool_log_client(ip, str(item.get("mac") or "")) and is_pool_log_only_miner(item):
            continue
        if is_pool_log_only_miner(item):
            last_seen_epoch = int(item.get("last_pool_seen_epoch", 0) or item.get("last_seen_epoch", 0) or 0)
            stale_limit = (
                MINER_REGISTRY_EXPECTED_ASIC_STALE_SECONDS
                if is_known_primary_pool_log_miner(item)
                else MINER_REGISTRY_POOL_LOG_STALE_SECONDS
            )
            stale = not last_seen_epoch or now_epoch - last_seen_epoch > stale_limit
            duplicate_old_asic_ip = ip in asic_known_ips and ip not in asic_current_ips
            if stale or duplicate_old_asic_ip:
                continue
        pruned.append(item)
    return pruned


def default_miner_scan_target() -> str:
    configured = os.environ.get("BDAG_MINER_SCAN_TARGET")
    if configured:
        return configured
    addresses = local_ipv4_addresses()
    if addresses:
        return str(ipaddress.ip_network(f"{addresses[0]}/24", strict=False))
    return "192.168.1.0/24"


def default_miner_pool_settings() -> dict[str, str]:
    pool_port = read_env_value("POOL_PORT") or "3334"
    pool_url = os.environ.get("BDAG_POOL_URL")
    if not pool_url:
        pool_host = os.environ.get("BDAG_POOL_HOST")
        if not pool_host:
            local_ips = local_ipv4_addresses()
            pool_host = local_ips[0] if local_ips else "127.0.0.1"
        pool_url = f"stratum+tcp://{pool_host}:{pool_port}"
    return {
        "scan_target": default_miner_scan_target(),
        "pool_url": pool_url,
        "worker_user": os.environ.get("BDAG_MINER_WORKER_USER") or read_env_value("MINING_ADDRESS") or "",
        "pool_password": os.environ.get("BDAG_MINER_POOL_PASSWORD", "1234"),
    }


def read_retired_miner_rows() -> list[dict[str, Any]]:
    payload = read_json_file(MINER_RETIREMENTS_FILE, {"retired_miners": []})
    rows = payload.get("retired_miners") if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def retired_miner_row_identities(row: dict[str, Any]) -> tuple[set[str], set[str], set[str], str]:
    name = str(row.get("display_name") or "").strip()
    macs = {mac for mac in [normalize_mac(row.get("mac"))] if mac}
    ips = {
        str(ip)
        for ip in merge_unique_strings(row.get("observed_ips"), row.get("ips_observed"), row.get("ips"), row.get("ip"))
        if is_ipv4(str(ip))
    }
    workers = {
        worker_text
        for worker in merge_unique_strings(
            row.get("observed_worker_user"),
            row.get("observed_worker_users"),
            row.get("worker_user"),
            row.get("worker_users"),
            row.get("workers"),
        )
        for worker_text in [str(worker or "").strip().lower()]
        if re.fullmatch(r"0x[a-f0-9]{40}", worker_text)
    }
    return macs, ips, workers, name


def read_retired_miner_identities() -> tuple[set[str], set[str], set[str], set[str]]:
    """Return retired metadata. Only retired_macs is an identity key."""
    retired_macs: set[str] = set()
    retired_ips: set[str] = set()
    retired_workers: set[str] = set()
    retired_names: set[str] = set()
    for row in read_retired_miner_rows():
        macs, ips, workers, name = retired_miner_row_identities(row)
        if name:
            retired_names.add(name)
        retired_macs.update(macs)
        retired_ips.update(ips)
        retired_workers.update(workers)
    return retired_macs, retired_ips, retired_workers, retired_names


def retired_miner_identity_decision(item: dict[str, Any], ip: str = "", mac: str = "") -> dict[str, Any]:
    candidate_mac = normalize_mac(mac) or normalize_mac(item.get("mac"))
    candidate_ip = str(ip or item.get("ip") or "")
    rows = read_retired_miner_rows()

    for row in rows:
        row_macs, _row_ips, _row_workers, name = retired_miner_row_identities(row)
        if candidate_mac and candidate_mac in row_macs:
            return {"retired": True, "matched_by": "mac", "retired_name": name, "conflict": False}

    for row in rows:
        row_macs, row_ips, _row_workers, name = retired_miner_row_identities(row)
        if candidate_ip and candidate_ip in row_ips:
            return {
                "retired": False,
                "matched_by": "ip-observation",
                "retired_name": name,
                "conflict": True,
                "candidate_mac": candidate_mac,
                "retired_macs": sorted(row_macs),
            }

    return {"retired": False, "matched_by": "", "retired_name": "", "conflict": False}


def is_retired_miner_identity(item: dict[str, Any], ip: str = "", mac: str = "") -> bool:
    """Skip miners intentionally moved away from this local pool.

    MAC is the only permanent ASIC identity. IPs, worker labels, ports, and
    names are observations only; they can help fetch or display an ASIC but must
    not retire, reintroduce, or suppress a miner by themselves.
    """
    return bool(retired_miner_identity_decision(item, ip, mac).get("retired"))


def read_miner_registry() -> dict[str, Any]:
    registry = read_json_file(MINER_REGISTRY_FILE, {"updated_at": None, "miners": []})
    if not isinstance(registry, dict):
        return {"updated_at": None, "miners": []}
    miners = registry.get("miners")
    if not isinstance(miners, list):
        registry["miners"] = []
    else:
        registry["miners"] = [
            item for item in miners
            if isinstance(item, dict) and not is_docker_bridge_pseudo_miner(item)
        ]
    return registry


def save_miner_registry(miners: list[dict[str, Any]]) -> dict[str, Any]:
    neighbors = read_neighbor_macs()
    by_identity: dict[str, dict[str, Any]] = {}
    for miner in sorted(miners, key=miner_observation_epoch):
        ip = str(miner.get("ip", ""))
        if not is_ipv4(ip):
            continue
        item = dict(miner)
        mac = miner_mac_from_payload(item, ip, neighbors)
        if is_retired_miner_identity(item, ip, mac):
            continue
        if mac:
            item["mac"] = mac
            item["device_id"] = f"mac:{mac}"
        item["ip_history"] = merge_unique_strings(item.get("ip_history"), ip)
        key = f"mac:{mac}" if mac else f"ip:{ip}"
        by_identity[key] = merge_miner_records(by_identity[key], item) if key in by_identity else item

    cleaned = prune_inactive_miner_records(list(by_identity.values()))
    cleaned.sort(
        key=lambda item: (
            0 if normalize_mac(item.get("mac")) else 1,
            normalize_mac(item.get("mac")) or "",
            int(ipaddress.ip_address(item["ip"])),
        )
    )
    assign_miner_display_names(cleaned)
    registry = {"updated_at": now_iso(), "miners": cleaned}
    write_json_file(MINER_REGISTRY_FILE, registry)
    return registry


def upsert_miner_registry(miners: list[dict[str, Any]], expected_pool_url: str | None = None, worker_user: str | None = None) -> dict[str, Any]:
    registry = read_miner_registry()
    existing = {str(item.get("ip")): dict(item) for item in registry.get("miners", []) if item.get("ip")}
    existing_by_mac = {
        normalize_mac(item.get("mac")): dict(item)
        for item in registry.get("miners", [])
        if normalize_mac(item.get("mac"))
    }
    neighbors = read_neighbor_macs()
    defaults = default_miner_pool_settings()
    expected_url = expected_pool_url or defaults["pool_url"]
    expected_user = worker_user or defaults["worker_user"]

    for miner in miners:
        ip = str(miner.get("ip", ""))
        if not is_lan_ipv4(ip):
            continue
        mac = miner_mac_from_payload(miner, ip, neighbors)
        pools = miner.get("pools") or []
        configured = any(str(pool.get("url", "")) == expected_url and str(pool.get("user", "")) == expected_user for pool in pools)
        item = existing_by_mac.get(mac) if mac else None
        item = dict(item or existing.get(ip, {"ip": ip}))
        item.update(
            {
                "ip": ip,
                "mac": mac or item.get("mac", ""),
                "device_id": f"mac:{mac}" if mac else item.get("device_id", ""),
                "ip_history": merge_unique_strings(item.get("ip_history"), ip),
                "device_type": "asic",
                "discovered_by": "lan-scan",
                "sources": merge_unique_strings(item.get("sources"), "lan-scan"),
                "model": miner.get("model", item.get("model", "")),
                "hardware": miner.get("hardware", item.get("hardware", "")),
                "firmware": miner.get("firmware", item.get("firmware", "")),
                "mcbversion": miner.get("mcbversion", item.get("mcbversion", "")),
                "expected_pool_url": expected_url,
                "expected_worker_user": expected_user,
                "last_seen_at": now_iso(),
                "last_seen_epoch": seconds_since_epoch(),
                "managed": bool(item.get("managed") or configured),
                "last_configured_ok": bool(item.get("last_configured_ok") or configured),
                "last_pool_count": miner.get("pool_count"),
            }
        )
        existing[ip] = item
        if mac:
            existing_by_mac[mac] = item

    return save_miner_registry(list(existing.values()))


def mark_configured_miners(results: list[dict[str, Any]], pool_url: str, worker_user: str) -> dict[str, Any]:
    registry = read_miner_registry()
    existing = {str(item.get("ip")): dict(item) for item in registry.get("miners", []) if item.get("ip")}
    existing_by_mac = {
        normalize_mac(item.get("mac")): dict(item)
        for item in registry.get("miners", [])
        if normalize_mac(item.get("mac"))
    }
    neighbors = read_neighbor_macs()
    for result in results:
        ip = str(result.get("ip", ""))
        if not is_lan_ipv4(ip):
            continue
        mac = miner_mac_from_payload(result, ip, neighbors)
        item = existing_by_mac.get(mac) if mac else None
        item = dict(item or existing.get(ip, {"ip": ip}))
        ok = result.get("status") in {"ok", "partial"} and not result.get("error")
        item.update(
            {
                "ip": ip,
                "mac": mac or item.get("mac", ""),
                "device_id": f"mac:{mac}" if mac else item.get("device_id", ""),
                "ip_history": merge_unique_strings(item.get("ip_history"), ip),
                "device_type": "asic",
                "sources": merge_unique_strings(item.get("sources"), "configured"),
                "expected_pool_url": pool_url,
                "expected_worker_user": worker_user,
                "managed": bool(ok or item.get("managed")),
                "last_configured_ok": bool(ok),
                "last_configure_result": result.get("status"),
                "last_configure_error": result.get("error", ""),
                "last_configured_at": now_iso(),
            }
        )
        existing[ip] = item
        if mac:
            existing_by_mac[mac] = item
    return save_miner_registry(list(existing.values()))


def save_miner_admin_password(password: str) -> dict[str, Any]:
    if not password:
        raise ValueError("admin password is required")
    ensure_runtime()
    MINER_ADMIN_PASSWORD_FILE.write_text(password + "\n", encoding="utf-8")
    MINER_ADMIN_PASSWORD_FILE.chmod(0o600)
    return {"status": "ok", "path": str(MINER_ADMIN_PASSWORD_FILE), "saved_at": now_iso()}


def read_miner_admin_password() -> str | None:
    value = os.environ.get("BDAG_MINER_ADMIN_PASSWORD")
    if value:
        return value
    if MINER_ADMIN_PASSWORD_FILE.exists():
        try:
            return MINER_ADMIN_PASSWORD_FILE.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return None


def _range_targets(token: str) -> list[str]:
    prefix, last = token.rsplit(".", 1)
    start_text, end_text = last.split("-", 1)
    start = int(start_text)
    end = int(end_text)
    if start > end or start < 0 or end > 255:
        raise ValueError(f"invalid IP range: {token}")
    return [f"{prefix}.{item}" for item in range(start, end + 1)]


def parse_scan_targets(target_spec: str | None) -> list[str]:
    spec = (target_spec or default_miner_scan_target()).strip()
    if not spec:
        spec = default_miner_scan_target()

    targets: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[,\s]+", spec):
        if not token:
            continue
        if "/" in token:
            network = ipaddress.ip_network(token, strict=False)
            if network.version != 4:
                raise ValueError(f"IPv6 scan targets are not supported: {token}")
            addresses = [str(network.network_address)] if network.prefixlen == 32 else [str(item) for item in network.hosts()]
        elif "-" in token and token.count(".") == 3:
            addresses = _range_targets(token)
        else:
            address = ipaddress.ip_address(token)
            if address.version != 4:
                raise ValueError(f"IPv6 scan targets are not supported: {token}")
            addresses = [str(address)]

        for address in addresses:
            if not is_lan_ipv4(address):
                raise ValueError(f"refusing non-LAN scan target: {address}")
            if address not in seen:
                seen.add(address)
                targets.append(address)

    if len(targets) > MINER_SCAN_MAX_TARGETS:
        raise ValueError(f"scan target expands to {len(targets)} hosts; limit is {MINER_SCAN_MAX_TARGETS}")
    return targets


def miner_request(
    ip: str,
    path: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float = MINER_HTTP_TIMEOUT,
    raw_authorization: bool = False,
) -> dict[str, Any]:
    if not is_lan_ipv4(ip):
        raise MinerAPIError(f"refusing non-LAN miner address: {ip}")

    data = None
    headers = {"accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["content-type"] = "application/json;charset=UTF-8"
    if token:
        headers["authorization"] = token if raw_authorization else f"Bearer {token}"
        headers["x-access-token"] = token

    request = urllib.request.Request(f"http://{ip}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_body = response.read(1_000_000).decode("utf-8", "replace")
            try:
                body: Any = json.loads(raw_body) if raw_body else None
            except json.JSONDecodeError:
                body = raw_body
            return {"status": response.status, "body": body, "raw": raw_body}
    except urllib.error.HTTPError as exc:
        body = exc.read(100_000).decode("utf-8", "replace")
        safe_path = re.sub(r"password=[^&]+", "password=<redacted>", path)
        raise MinerAPIError(f"miner HTTP {exc.code} for {safe_path}", status=exc.code, body=body) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        safe_path = re.sub(r"password=[^&]+", "password=<redacted>", path)
        raise MinerAPIError(f"miner request failed for {ip}{safe_path}: {exc}") from exc


def get_miner_pools(ip: str, timeout: float = MINER_HTTP_TIMEOUT) -> list[dict[str, Any]]:
    response = miner_request(ip, "/mcb/pools", timeout=timeout)
    body = response["body"]
    if not isinstance(body, list):
        raise MinerAPIError(f"{ip} did not return a miner pool list")
    return [item for item in body if isinstance(item, dict)]


def get_miner_status(ip: str, timeout: float = MINER_HTTP_TIMEOUT) -> dict[str, Any]:
    try:
        response = miner_request(ip, "/mcb/status", timeout=timeout)
    except MinerAPIError:
        return {}
    return response["body"] if isinstance(response["body"], dict) else {}


def get_miner_cgminer_devs(ip: str, timeout: float = MINER_HTTP_TIMEOUT) -> dict[str, Any]:
    response = miner_request(ip, "/mcb/cgminer?cgminercmd=devs", timeout=timeout)
    body = response["body"]
    if not isinstance(body, dict):
        raise MinerAPIError(f"{ip} did not return cgminer device data")
    data = body.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise MinerAPIError(f"{ip} cgminer device data was empty")
    return data[0]


def discover_miner(ip: str, timeout: float = MINER_SCAN_TIMEOUT) -> dict[str, Any] | None:
    started = time.time()
    try:
        pools = get_miner_pools(ip, timeout=timeout)
    except MinerAPIError:
        return None

    status = get_miner_status(ip, timeout=timeout)
    active_pool = next((pool for pool in pools if pool.get("active")), pools[0] if pools else {})
    mac = miner_mac_from_payload(status, ip)
    return {
        "ip": ip,
        "mac": mac,
        "device_id": f"mac:{mac}" if mac else "",
        "model": status.get("model", ""),
        "hardware": status.get("hardware", ""),
        "firmware": status.get("firmware", ""),
        "mcbversion": status.get("mcbversion", ""),
        "pool_count": len(pools),
        "active": bool(active_pool.get("active")),
        "current_pool": active_pool,
        "pools": pools,
        "response_ms": round((time.time() - started) * 1000),
    }


def scan_miners(target_spec: str | None = None) -> dict[str, Any]:
    ensure_runtime()
    started = now_iso()
    targets = parse_scan_targets(target_spec)
    miners: list[dict[str, Any]] = []
    pressure = collect_host_pressure()
    worker_count = adaptive_worker_count("miner_scan", MINER_SCAN_WORKERS, len(targets), pressure)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {executor.submit(discover_miner, target): target for target in targets}
        for future in as_completed(futures):
            miner = future.result()
            if miner:
                miners.append(miner)

    miners.sort(key=lambda item: int(ipaddress.ip_address(item["ip"])))
    return {
        "started_at": started,
        "finished_at": now_iso(),
        "target_spec": target_spec or default_miner_scan_target(),
        "target_count": len(targets),
        "worker_count": worker_count,
        "adaptive_concurrency": adaptive_worker_budgets(pressure),
        "miner_count": len(miners),
        "miners": miners,
    }


def encrypt_miner_password(password: str) -> str:
    plain = password.encode("utf-8")
    pad_len = (-len(plain)) % 16
    padded = plain + (b"\x00" * pad_len)
    proc = subprocess.run(
        [
            "openssl",
            "enc",
            "-aes-128-cbc",
            "-K",
            MINER_LOGIN_KEY_HEX,
            "-iv",
            MINER_ZERO_IV_HEX,
            "-nopad",
            "-nosalt",
            "-e",
        ],
        input=padded,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        error = proc.stderr.decode("utf-8", "replace").strip()
        raise MinerAPIError(f"failed to encrypt miner password with openssl: {error}")
    return proc.stdout.hex()


def miner_login(ip: str, admin_password: str) -> str:
    cipher_text = encrypt_miner_password(admin_password)
    query = urllib.parse.urlencode({"username": "admin", "password": cipher_text, "cipher": "true"})
    response = miner_request(ip, f"/user/login?{query}", timeout=MINER_HTTP_TIMEOUT)
    body = response["body"]
    if not isinstance(body, dict):
        raise MinerAPIError(f"{ip} login returned an unexpected response")
    token = body.get("JWT Token") or body.get("token") or body.get("jwt") or body.get("JWT")
    if not token:
        raise MinerAPIError(f"{ip} login did not return an access token")
    return str(token)


def miner_put_auth(ip: str, path: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
    try:
        return miner_request(ip, path, method="PUT", payload=payload, token=token)
    except MinerAPIError as exc:
        if exc.status not in {401, 403}:
            raise
    return miner_request(ip, path, method="PUT", payload=payload, token=token, raw_authorization=True)


def restart_miner(ip: str, admin_password: str) -> dict[str, Any]:
    token = miner_login(ip, admin_password)
    response = miner_put_auth(ip, "/mcb/restart", {}, token)
    return {"ip": ip, "status": "ok", "response": response["body"]}


def restart_miner_open(ip: str) -> dict[str, Any]:
    response = miner_request(ip, "/mcb/restart", method="PUT", payload={}, timeout=MINER_HTTP_TIMEOUT)
    return {"ip": ip, "status": "ok", "response": response["body"]}


def pool_matches(pool: dict[str, Any], desired: dict[str, str]) -> bool:
    return (
        str(pool.get("url", "")) == desired["url"]
        and str(pool.get("user", "")) == desired["user"]
        and str(pool.get("pass", "")) == desired["pass"]
    )


def backup_miner_pools(ip: str, pools: list[dict[str, Any]]) -> Path:
    ensure_runtime()
    safe_ip = re.sub(r"[^0-9.]+", "-", ip)
    path = MINER_BACKUP_DIR / f"{safe_ip}-pools-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(pools, indent=2), encoding="utf-8")
    return path


def add_miner_pool(ip: str, token: str, desired: dict[str, str]) -> dict[str, Any]:
    payload = {"url": desired["url"], "user": desired["user"], "pass": desired["pass"]}
    try:
        return miner_put_auth(ip, "/mcb/newpool", payload, token)
    except MinerAPIError:
        payload["pool-priority"] = 0
        return miner_put_auth(ip, "/mcb/newpool", payload, token)


def delete_miner_pool(ip: str, token: str, pool: dict[str, Any]) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = [dict(pool)]
    if "pool-priority" in pool:
        attempts.append({"pool-priority": pool["pool-priority"]})
    if "url" in pool:
        attempts.append({"url": pool.get("url"), "user": pool.get("user", ""), "pass": pool.get("pass", "")})

    last_error: MinerAPIError | None = None
    for payload in attempts:
        try:
            return miner_put_auth(ip, "/mcb/delpool", payload, token)
        except MinerAPIError as exc:
            last_error = exc
    raise last_error or MinerAPIError(f"failed to delete pool on {ip}")


def configure_miner(
    ip: str,
    admin_password: str,
    pool_url: str,
    worker_user: str,
    pool_password: str = "1234",
    replace_existing: bool = True,
) -> dict[str, Any]:
    if not admin_password:
        raise MinerAPIError("admin password is required")
    desired = {"url": pool_url, "user": worker_user, "pass": pool_password}

    before = get_miner_pools(ip)
    backup_path = backup_miner_pools(ip, before)
    token = miner_login(ip, admin_password)

    if not any(pool_matches(pool, desired) for pool in before):
        add_miner_pool(ip, token, desired)
        time.sleep(1)

    after_add = get_miner_pools(ip)
    if not any(pool_matches(pool, desired) for pool in after_add):
        raise MinerAPIError(f"{ip} did not accept the new pool entry")

    deleted: list[dict[str, Any]] = []
    delete_errors: list[str] = []
    kept_desired = False
    if replace_existing:
        ordered = sorted(after_add, key=lambda item: int(item.get("pool-priority", 0) or 0), reverse=True)
        for pool in ordered:
            if pool_matches(pool, desired) and not kept_desired:
                kept_desired = True
                continue
            try:
                delete_miner_pool(ip, token, pool)
                deleted.append(
                    {
                        "url": pool.get("url", ""),
                        "user": pool.get("user", ""),
                        "pool-priority": pool.get("pool-priority"),
                    }
                )
                time.sleep(0.25)
            except MinerAPIError as exc:
                delete_errors.append(f"priority={pool.get('pool-priority')} url={pool.get('url')}: {exc}")

    time.sleep(1)
    final_pools = get_miner_pools(ip)
    desired_priority = {**desired, "pool-priority": 0}
    if final_pools:
        try:
            miner_put_auth(ip, "/mcb/pools", [desired_priority], token)
            time.sleep(1)
            final_pools = get_miner_pools(ip)
        except MinerAPIError as exc:
            delete_errors.append(f"persist pools order failed: {exc}")
    final_desired = next((pool for pool in final_pools if pool_matches(pool, desired)), None)
    return {
        "ip": ip,
        "status": "partial" if delete_errors else "ok",
        "backup_path": str(backup_path),
        "configured_pool": desired,
        "active": bool(final_desired and final_desired.get("active")),
        "deleted": deleted,
        "delete_errors": delete_errors,
        "final_pools": final_pools,
    }


def configure_miners(
    ips: list[str],
    admin_password: str,
    pool_url: str,
    worker_user: str,
    pool_password: str = "1234",
    replace_existing: bool = True,
) -> dict[str, Any]:
    ensure_runtime()
    results: list[dict[str, Any]] = []
    for ip in ips:
        try:
            results.append(
                configure_miner(
                    ip=ip,
                    admin_password=admin_password,
                    pool_url=pool_url,
                    worker_user=worker_user,
                    pool_password=pool_password,
                    replace_existing=replace_existing,
                )
            )
        except Exception as exc:  # noqa: BLE001 - report per-device failures to the dashboard.
            results.append({"ip": ip, "status": "failed", "error": str(exc)})

    failed = [item for item in results if item.get("status") == "failed"]
    partial = [item for item in results if item.get("status") == "partial"]
    status = "failed" if len(failed) == len(results) and results else "partial" if failed or partial else "ok"
    return {"status": status, "finished_at": now_iso(), "results": results}


def docker_compose_service_candidates(name: str) -> list[str]:
    candidates = [name]
    if name in {POOL_DB_CONTAINER, "postgres", "pool-db"}:
        candidates.extend(["pool-db", "postgres"])
    if name in set(POOL_CONTAINERS) | {"pool", "asic-pool"}:
        candidates.extend(["pool", "asic-pool"])
    if name in set(NODES) | {"node", "bdag-miner-node-1"}:
        candidates.extend(["node", "bdag-miner-node-1"])
    return unique_names([candidate for candidate in candidates if candidate])


def docker_compose_container_ids_for_service(service: str) -> list[str]:
    result = run(
        ["docker", "ps", "-a", "--filter", f"label=com.docker.compose.service={service}", "--format", "{{.ID}}"],
        timeout=8,
    )
    if not result.ok:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def docker_container_ids_for_alias(name: str) -> list[str]:
    ids: list[str] = []
    for service in docker_compose_service_candidates(name):
        ids.extend(docker_compose_container_ids_for_service(service))
    return unique_names(ids)


def docker_inspect_payload(names: list[str]) -> list[dict[str, Any]]:
    if not names:
        return []
    result = run(["docker", "inspect", *names], timeout=12)
    payload: list[dict[str, Any]] = []
    if result.ok:
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
    for name in names:
        single = run(["docker", "inspect", name], timeout=8)
        if not single.ok:
            continue
        try:
            payload.extend(json.loads(single.stdout))
        except json.JSONDecodeError:
            continue
    return payload


def docker_inspect_row(item: dict[str, Any]) -> dict[str, Any]:
    name = str(item.get("Name", "")).lstrip("/")
    state = item.get("State") or {}
    config = item.get("Config") or {}
    labels = config.get("Labels") or {}
    network_settings = item.get("NetworkSettings") or {}
    networks = network_settings.get("Networks") or {}
    network_rows = {
        network_name: {
            "ip_address": network_info.get("IPAddress", ""),
            "gateway": network_info.get("Gateway", ""),
        }
        for network_name, network_info in networks.items()
        if isinstance(network_info, dict)
    }
    return {
        "name": name,
        "image": config.get("Image", ""),
        "running": bool(state.get("Running")),
        "status": state.get("Status", "unknown"),
        "started_at": state.get("StartedAt", ""),
        "finished_at": state.get("FinishedAt", ""),
        "restart_count": int(item.get("RestartCount", 0) or 0),
        "exit_code": state.get("ExitCode", 0),
        "error": state.get("Error", ""),
        "ports": network_settings.get("Ports") or {},
        "networks": network_rows,
        "network_ips": [row["ip_address"] for row in network_rows.values() if row.get("ip_address")],
        "compose_project": labels.get("com.docker.compose.project", ""),
        "compose_service": labels.get("com.docker.compose.service", ""),
    }


def docker_inspect(names: list[str]) -> dict[str, dict[str, Any]]:
    payload = docker_inspect_payload(names)
    resolved = {str(item.get("Name", "")).lstrip("/") for item in payload}
    missing = [name for name in names if name not in resolved]
    alias_ids: list[str] = []
    for name in missing:
        alias_ids.extend(docker_container_ids_for_alias(name))
    if alias_ids:
        payload.extend(docker_inspect_payload(unique_names(alias_ids)))

    by_name: dict[str, dict[str, Any]] = {}
    by_service: dict[str, dict[str, Any]] = {}
    for item in payload:
        row = docker_inspect_row(item)
        if row["name"]:
            by_name[row["name"]] = row
        if row["compose_service"]:
            by_service.setdefault(row["compose_service"], row)

    inspected: dict[str, dict[str, Any]] = {}
    for name in names:
        if name in by_name:
            inspected[name] = by_name[name]
            continue
        for service in docker_compose_service_candidates(name):
            if service in by_service:
                inspected[name] = by_service[service]
                break
    return inspected


def docker_top(name: str) -> str:
    return run(["docker", "top", docker_container_name(name)], timeout=8).stdout


def bdag_child_running_from_top(top: str) -> bool:
    for line in top.splitlines()[1:]:
        parts = line.split(None, 7)
        if len(parts) >= 8:
            command = parts[7]
        else:
            command = line
        executable = command.split(None, 1)[0] if command.split(None, 1) else ""
        executable_name = Path(executable).name
        if executable_name in {"bdag", "blockdag-node"}:
            return True
    return False


def docker_logs(name: str, lines: int = 160) -> str:
    result = run(["docker", "logs", "-n", str(lines), docker_container_name(name)], timeout=12)
    return redact_sensitive_text(result.stdout + "\n" + result.stderr).strip()


def docker_logs_many(names: list[str], lines: int = 160) -> str:
    chunks = []
    for name in unique_names(names):
        log = docker_logs(name, lines=lines)
        if log:
            chunks.append(log)
    return "\n".join(chunks)


def docker_access_error() -> str | None:
    probe = run(["docker", "ps"], timeout=8)
    if probe.ok:
        return None
    stderr = (probe.stderr or probe.stdout or "").strip()
    lowered = stderr.lower()
    if (
        "permission denied while trying to connect to the docker api" in lowered
        or "cannot connect to the docker daemon" in lowered
        or "is the docker daemon running" in lowered
        or probe.returncode == 127
    ):
        return stderr or "docker access unavailable"
    return None


def _last_match_int(pattern: re.Pattern[str], text: str) -> int | None:
    matches = pattern.findall(text)
    if not matches:
        return None
    return int(str(matches[-1]).replace(",", ""))


def _node_log_epoch(line: str | None) -> float | None:
    if not line:
        return None
    match = NODE_LOG_TS_RE.search(line)
    if not match:
        return None
    microsecond = (match.group(3) or "0")[:6].ljust(6, "0")
    try:
        parsed = datetime.strptime(f"{match.group(1)} {match.group(2)}.{microsecond}", "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc).timestamp()


def _node_log_iso(line: str | None) -> str | None:
    if not line:
        return None
    match = NODE_LOG_TS_RE.search(line)
    if not match:
        return None
    suffix = f".{match.group(3)}" if match.group(3) else ""
    return f"{match.group(1)}T{match.group(2)}{suffix}Z"


def parse_node_log(log: str) -> dict[str, Any]:
    lines = [line for line in strip_ansi(log).splitlines() if line.strip()]
    recent = lines[-80:]
    text = "\n".join(recent)
    imported = _last_match_int(BLOCK_RE, text)
    best_main_order = _last_match_int(MAIN_ORDER_RE, text)
    peer_ahead = _last_match_int(PEER_AHEAD_RE, text)
    sync_delta = _last_match_int(SYNC_DELTA_RE, text)
    imported_lines = [line for line in recent if BLOCK_RE.search(line)]
    last_import_line = imported_lines[-1] if imported_lines else None
    last_import_epoch = _node_log_epoch(last_import_line)
    last_import_age = int(max(0, time.time() - last_import_epoch)) if last_import_epoch is not None else None
    invalid_peer_lines = [
        line
        for line in recent
        if "Could not make peer" in line and "failed to parse multiaddr" in line
    ]
    p2p_stream_lines = [
        line
        for line in recent
        if "ErrStreamRead" in line or "stream reset" in line
    ]
    orphan_error_lines = [
        line
        for line in recent
        if "already have block (orphan)" in line
    ]
    template_error_lines = [line for line in recent if "Failed to create new block template" in line]
    template_transient_tx_error_lines = [
        line
        for line in template_error_lines
        if is_transient_template_tx_error_text(line)
    ]
    template_nonce_too_low_lines = [
        line
        for line in template_error_lines
        if "nonce too low" in line
    ]
    template_hard_error_lines = [
        line
        for line in template_error_lines
        if not is_transient_template_tx_error_text(line)
    ]
    critical_lines = [
        line
        for line in recent
        if "[CRIT" in line
        or "Failed to truncate extra state histories" in line
        or "fatal" in line.lower()
    ]
    return {
        "latest_block": imported,
        "best_main_order": best_main_order,
        "peer_ahead_blocks": peer_ahead or sync_delta,
        "importing": imported is not None,
        "last_import_at": _node_log_iso(last_import_line),
        "last_import_age_seconds": last_import_age,
        "import_count": len(imported_lines),
        "invalid_peer_errors": len(invalid_peer_lines),
        "p2p_stream_errors": len(p2p_stream_lines),
        "orphan_block_errors": len(orphan_error_lines),
        "orphan_block_error_storm": bool(
            len(orphan_error_lines) >= NODE_ORPHAN_ERROR_STORM_COUNT
            and len(imported_lines) == 0
        ),
        "orphan_block_error_lines": orphan_error_lines[-5:],
        "p2p_error_lines": (invalid_peer_lines + p2p_stream_lines)[-5:],
        "mining_template_error_count": len(template_error_lines),
        "mining_template_hard_error_count": len(template_hard_error_lines),
        "mining_template_transient_tx_error_count": len(template_transient_tx_error_lines),
        "mining_template_nonce_too_low_count": len(template_nonce_too_low_lines),
        "mining_template_error_lines": template_error_lines[-5:],
        "mining_template_hard_error_lines": template_hard_error_lines[-5:],
        "mining_template_failing": len(template_hard_error_lines) >= 3,
        "critical": bool(critical_lines),
        "critical_lines": critical_lines[-5:],
        "tail": recent[-24:],
    }


def parse_pool_log(log: str) -> dict[str, Any]:
    lines = [line for line in strip_ansi(log).splitlines() if line.strip()]
    recent = lines[-600:]
    text = "\n".join(recent)
    initial_download = "Client in initial download" in text
    rpc_refused_lines = [line for line in recent if "connect: connection refused" in line]
    rpc_refused = bool(rpc_refused_lines)
    last_rpc_refused_line = rpc_refused_lines[-1] if rpc_refused_lines else None
    last_rpc_refused_age_seconds = _pool_log_age_seconds(last_rpc_refused_line)
    rpc_refused_recent = bool(
        last_rpc_refused_age_seconds is not None
        and last_rpc_refused_age_seconds <= POOL_RPC_REFUSED_WARN_SECONDS
    )
    gbt_errors = sum(1 for line in recent if "GBT ERROR" in line)
    valid_share_lines = [line for line in recent if VALID_SHARE_RE.search(line)]
    valid_share_count = sum(valid_share_line_weight(line) for line in valid_share_lines)
    submit_lines = [line for line in recent if SUBMIT_RE.search(line)]
    submit_count = sum(submit_line_weight(line) for line in submit_lines)
    stale_submit_lines = [
        line
        for line in recent
        if "not found in acceptedJobs" in line or "Stale/Expired" in line
    ]
    job_notify_lines = [
        line
        for line in recent
        if JOB_NOTIFY_DETAIL_RE.search(line) or JOB_NOTIFY_RE.search(line) or PUSHDIF_RE.search(line)
    ]
    head_change_lines = [
        line
        for line in recent
        if "[REFRESH] head change" in line or "[REFRESH] ws tip change" in line or "[REFRESH] poll tip change" in line
    ]
    block_submit_ok_lines = [line for line in recent if "Block submitted successfully" in line]
    block_submit_err_lines = [
        line
        for line in recent
        if "Block submission too late" in line or "[REFRESH] block submit error" in line
    ]
    duplicate_block_lines = [line for line in recent if "[DUP BLOCK]" in line or "duplicate block submission" in line]
    stale_job_candidate_lines = [line for line in recent if "[STALE JOB]" in line]
    tip_overdue_lines = [
        line
        for line in recent
        if "main chain tip is overdue" in line or "tips of block is expired" in line or "Obsolete depth" in line
    ]
    submit_stall_event_lines = [
        line
        for line in recent
        if (
            "[SUBMIT-STALL]" in line
            or ("[ROUTER]" in line and "after submit stall" in line)
        )
    ]
    submit_stall_events = [_parse_submit_stall_event(line) for line in submit_stall_event_lines]
    submit_stall_recovery_events = [
        event
        for event in submit_stall_events
        if event.get("action") in {"backend-fallback", "invalidated"}
    ]
    block_submit_failure_count = len(block_submit_err_lines) + len(duplicate_block_lines) + len(stale_job_candidate_lines)
    accepted_job_expired_storm = bool(
        len(stale_submit_lines) >= POOL_ACCEPTED_JOB_EXPIRED_STORM_COUNT
        and (
            valid_share_count == 0
            or len(stale_submit_lines) > max(1, valid_share_count) * POOL_ACCEPTED_JOB_EXPIRED_STORM_RATIO
        )
    )
    block_submit_zero_success_storm = bool(
        len(block_submit_ok_lines) == 0
        and block_submit_failure_count >= POOL_BLOCK_SUBMIT_ZERO_SUCCESS_FAILURE_COUNT
        and (
            len(block_submit_err_lines) >= POOL_BLOCK_SUBMIT_ZERO_SUCCESS_ERROR_COUNT
            or len(tip_overdue_lines) >= POOL_BLOCK_SUBMIT_ZERO_SUCCESS_ERROR_COUNT
            or len(duplicate_block_lines) >= POOL_BLOCK_SUBMIT_ZERO_SUCCESS_FAILURE_COUNT
        )
    )
    freeze_lines = [line for line in recent if "FREEZE DETECTED" in line]
    observed_submit_count = max(
        submit_count,
        valid_share_count
        + len(stale_submit_lines)
        + len(stale_job_candidate_lines)
        + len(duplicate_block_lines)
        + len(block_submit_ok_lines)
        + len(block_submit_err_lines),
    )
    last_valid_share_line = valid_share_lines[-1] if valid_share_lines else None
    last_submit_line = submit_lines[-1] if submit_lines else last_valid_share_line
    last_job_notify_line = job_notify_lines[-1] if job_notify_lines else None
    last_head_change_line = head_change_lines[-1] if head_change_lines else None
    last_block_submit_line = block_submit_ok_lines[-1] if block_submit_ok_lines else None
    last_block_submit_epoch = _pool_log_epoch(last_block_submit_line)
    last_block_submit_age_seconds = _pool_log_age_seconds(last_block_submit_line)
    last_freeze_line = freeze_lines[-1] if freeze_lines else None
    freeze_age_match = FREEZE_RE.search(last_freeze_line or "")
    freeze_age_seconds = float(freeze_age_match.group(1)) if freeze_age_match else None
    last_valid_share_age_seconds = _pool_log_age_seconds(last_valid_share_line)
    last_job_notify_age_seconds = _pool_log_age_seconds(last_job_notify_line)
    last_submit_stall_event = submit_stall_events[-1] if submit_stall_events else {}
    last_submit_stall_recovery = submit_stall_recovery_events[-1] if submit_stall_recovery_events else {}
    last_recovery_epoch = (
        float(last_submit_stall_recovery["epoch"])
        if last_submit_stall_recovery.get("epoch") is not None
        else None
    )
    last_recovery_age_seconds = (
        int(max(0, time.time() - last_recovery_epoch))
        if last_recovery_epoch is not None
        else None
    )
    submit_stall_recovery_recent = (
        last_recovery_age_seconds is not None
        and last_recovery_age_seconds <= POOL_SUBMIT_RECOVERY_RECENT_SECONDS
    )
    accepted_after_recovery = bool(
        last_recovery_epoch is not None
        and last_block_submit_epoch is not None
        and last_block_submit_epoch >= last_recovery_epoch
    )
    submit_stall_self_healed_recently = bool(
        submit_stall_recovery_recent
        and accepted_after_recovery
        and last_block_submit_age_seconds is not None
        and last_block_submit_age_seconds <= POOL_SUBMIT_RECOVERY_ACCEPTED_RESUME_SECONDS
    )
    return {
        "initial_download": initial_download,
        "rpc_refused": rpc_refused,
        "rpc_refused_recent": rpc_refused_recent,
        "last_rpc_refused_age_seconds": last_rpc_refused_age_seconds,
        "rpc_refused_warn_seconds": POOL_RPC_REFUSED_WARN_SECONDS,
        "gbt_errors": gbt_errors,
        "submit_count": observed_submit_count,
        "valid_share_count": valid_share_count,
        "stale_submit_count": len(stale_submit_lines),
        "accepted_job_expired_storm": accepted_job_expired_storm,
        "accepted_job_expired_storm_threshold": POOL_ACCEPTED_JOB_EXPIRED_STORM_COUNT,
        "accepted_job_expired_storm_ratio": POOL_ACCEPTED_JOB_EXPIRED_STORM_RATIO,
        "job_notify_count": len(job_notify_lines),
        "head_change_count": len(head_change_lines),
        "block_submit_success_count": len(block_submit_ok_lines),
        "block_submit_error_count": len(block_submit_err_lines),
        "block_submit_failure_count": block_submit_failure_count,
        "block_submit_zero_success_storm": block_submit_zero_success_storm,
        "duplicate_block_count": len(duplicate_block_lines),
        "stale_job_candidate_count": len(stale_job_candidate_lines),
        "duplicate_block_storm": bool(
            len(duplicate_block_lines) >= POOL_DUP_BLOCK_STORM_COUNT
            and len(duplicate_block_lines) > max(1, valid_share_count) * POOL_DUP_BLOCK_STORM_RATIO
        ),
        "stale_job_candidate_storm": bool(
            len(stale_job_candidate_lines) >= POOL_STALE_JOB_STORM_COUNT
            and len(stale_job_candidate_lines) > max(1, len(block_submit_ok_lines))
        ),
        "block_submit_error_storm": bool(
            len(block_submit_err_lines) >= POOL_BLOCK_SUBMIT_ERROR_STORM_COUNT
            and len(block_submit_err_lines) > max(1, len(block_submit_ok_lines)) * POOL_BLOCK_SUBMIT_ERROR_STORM_RATIO
        ),
        "tip_overdue_count": len(tip_overdue_lines),
        "submit_stall_event_count": len(submit_stall_events),
        "submit_stall_recovery_count": len(submit_stall_recovery_events),
        "submit_stall_last_event": last_submit_stall_event,
        "submit_stall_last_recovery": last_submit_stall_recovery,
        "submit_stall_last_reason": str(last_submit_stall_event.get("reason") or ""),
        "submit_stall_last_action": str(last_submit_stall_event.get("action") or ""),
        "submit_stall_last_event_at": last_submit_stall_event.get("at"),
        "submit_stall_last_event_age_seconds": last_submit_stall_event.get("age_seconds"),
        "submit_stall_last_recovery_at": last_submit_stall_recovery.get("at"),
        "submit_stall_last_recovery_age_seconds": last_recovery_age_seconds,
        "submit_stall_recovery_recent": submit_stall_recovery_recent,
        "submit_stall_accepted_after_recovery": accepted_after_recovery,
        "submit_stall_self_healed_recently": submit_stall_self_healed_recently,
        "submit_stall_recent_event_lines": submit_stall_event_lines[-6:],
        "template_freeze_count": len(freeze_lines),
        "template_freeze_age_seconds": freeze_age_seconds,
        "pool_template_frozen": bool(freeze_age_seconds is not None and freeze_age_seconds >= POOL_TEMPLATE_FREEZE_SECONDS),
        "last_submit_at": _parse_log_timestamp(last_submit_line),
        "last_submit_age_seconds": _pool_log_age_seconds(last_submit_line),
        "last_valid_share_at": _parse_log_timestamp(last_valid_share_line),
        "last_valid_share_age_seconds": last_valid_share_age_seconds,
        "last_job_notify_at": _parse_log_timestamp(last_job_notify_line),
        "last_job_notify_age_seconds": last_job_notify_age_seconds,
        "last_head_change_at": _parse_log_timestamp(last_head_change_line),
        "last_head_change_age_seconds": _pool_log_age_seconds(last_head_change_line),
        "last_block_submit_at": _parse_log_timestamp(last_block_submit_line),
        "last_block_submit_age_seconds": last_block_submit_age_seconds,
        "share_stall": _is_stale_age(last_valid_share_age_seconds, POOL_VALID_SHARE_STALE_SECONDS),
        "job_stall": _is_stale_age(last_job_notify_age_seconds, POOL_JOB_NOTIFY_STALE_SECONDS),
        "tail": recent[-24:],
    }


LOG_TS_RE = re.compile(r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})")
PUSHDIF_RE = re.compile(r"PUSHDIF\s+->\s+((?:\d{1,3}\.){3}\d{1,3}):([0-9]+)\s+mining\.set_difficulty\s+([0-9.]+)")
AUTH_ACCEPT_RE = re.compile(r"\[((?:\d{1,3}\.){3}\d{1,3}):([0-9]+)\]\s+authorize accepted user=([^\s]+)")
SUBSCRIBE_ACCEPT_RE = re.compile(
    r"\[((?:\d{1,3}\.){3}\d{1,3}):([0-9]+)\]\s+subscribe accepted extranonce1=([0-9a-fA-F]{8})"
)
JOB_NOTIFY_RE = re.compile(r"Sending to ((?:\d{1,3}\.){3}\d{1,3}):\d+:\s+jobID=([^\s]+)")
JOB_NOTIFY_DETAIL_RE = re.compile(r"Sending to ((?:\d{1,3}\.){3}\d{1,3}):([0-9]+):\s+jobID=([^\s]+)")
CLIENT_ADDR_RE = re.compile(r"\bclient=((?:\d{1,3}\.){3}\d{1,3}):([0-9]+)")
JOB_EXTRANONCE_RE = re.compile(r"_([0-9a-fA-F]{8})(?:\s|$)")
SUBMIT_RE = re.compile(
    r"submit from\s+(?:client=((?:\d{1,3}\.){3}\d{1,3}):([0-9]+)\s+)?worker=([^\s]+)\s+job=([^\s]+)"
)
SUBMIT_SUPPRESSED_RE = re.compile(r"\ssuppressed=([0-9]+)")
VALID_SHARE_RE = re.compile(
    r"valid share accepted\s+([0-9.]+)\s+[^0-9]+([0-9]+)\s+"
    r"(?:client=((?:\d{1,3}\.){3}\d{1,3}):([0-9]+)\s+)?worker=([^\s]+)\s+job=([^\s]+)"
)
VALID_SHARE_SUPPRESSED_DIFF_RE = re.compile(r"\ssuppressedDiff=([0-9.]+)")
VALID_SHARE_SUPPRESSED_WORK_RE = re.compile(r"\ssuppressedWork=([0-9]+)")
BLOCK_FOUND_RE = re.compile(r"BLOCK FOUND .*job=([^\s]+)")
FREEZE_RE = re.compile(r"Same parent hash for\s+([0-9.]+)\s+seconds")
PROMETHEUS_SAMPLE_RE = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+"
    r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)$"
)
PROMETHEUS_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"')


def mining_rpc_urls() -> list[tuple[str, str]]:
    urls: list[tuple[str, str]] = []
    name = primary_node_service()
    ip = run(
        ["docker", "inspect", name, "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"],
        timeout=8,
    ).stdout.strip()
    if valid_ipv4(ip):
        urls.append((name, f"http://{ip}:{NODE_MINING_RPC_PORT}"))
    return urls


def mining_rpc_call(url: str, method: str, params: list[Any], timeout: float = NODE_TEMPLATE_PROBE_TIMEOUT) -> Any:
    rpc_user, rpc_pass = node_mining_rpc_credentials()
    credentials = f"{rpc_user}:{rpc_pass}".encode("utf-8")
    token = base64.b64encode(credentials).decode("ascii")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "authorization": f"Basic {token}",
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": HTTP_USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read(1_000_000).decode("utf-8", "replace"))
    if payload.get("error") is not None and "result" not in payload:
        raise RuntimeError(str(payload.get("error") or payload))
    return payload.get("result")


def probe_template_endpoint(name: str, url: str) -> dict[str, Any]:
    ok_count = 0
    errors: list[str] = []
    last_height = None
    params: list[Any] = [[], 10]
    pool_address = read_env_value("MINING_ADDRESS") or ""
    if pool_address:
        params.append(pool_address)
    for _ in range(NODE_TEMPLATE_PROBE_SAMPLES):
        try:
            result = mining_rpc_call(url, "getBlockTemplate", params)
            if not isinstance(result, dict):
                raise RuntimeError(f"unexpected getBlockTemplate result: {result!r}")
            ok_count += 1
            last_height = result.get("height")
        except Exception as exc:  # noqa: BLE001 - probe results are diagnostics, not fatal status collection.
            errors.append(str(exc))
    sample_count = ok_count + len(errors)
    error_ratio = round(len(errors) / max(1, sample_count), 4)
    tx_download_errors = [error for error in errors if "Blockdag is downloading tx" in error]
    nonce_too_low_errors = [error for error in errors if "nonce too low" in error]
    transient_tx_template_errors = [
        error
        for error in errors
        if is_transient_template_tx_error_text(error)
    ]
    benign_tx_throttle = bool(errors and len(tx_download_errors) == len(errors))
    benign_tx_template_error = bool(errors and len(transient_tx_template_errors) == len(errors))
    failing = bool(errors and not benign_tx_template_error and ok_count == 0)
    return {
        "name": name,
        "url": url,
        "sample_count": sample_count,
        "ok_count": ok_count,
        "error_count": len(errors),
        "tx_download_throttle_count": len(tx_download_errors),
        "nonce_too_low_count": len(nonce_too_low_errors),
        "transient_tx_template_count": len(transient_tx_template_errors),
        "benign_tx_throttle": benign_tx_throttle,
        "benign_tx_template_error": benign_tx_template_error,
        "error_ratio": error_ratio,
        "failing": failing,
        "last_height": last_height,
        "last_error": errors[-1] if errors else "",
        "errors": errors[-3:],
    }


def collect_template_probe_health() -> dict[str, Any]:
    now = int(time.time())
    cached = read_json_file(NODE_TEMPLATE_PROBE_CACHE_FILE, {})
    try:
        cached_at = int(cached.get("epoch") or 0) if isinstance(cached, dict) else 0
    except (TypeError, ValueError):
        cached_at = 0
    if isinstance(cached, dict) and cached_at and now - cached_at < NODE_TEMPLATE_PROBE_CACHE_SECONDS:
        payload = dict(cached.get("payload") or {})
        payload["cached"] = True
        payload["cache_age_seconds"] = now - cached_at
        return payload

    endpoints = mining_rpc_urls()
    probes = {name: probe_template_endpoint(name, url) for name, url in endpoints}
    node_probes = {name: probes[name] for name in NODES if name in probes}
    failing_nodes = [name for name, probe in node_probes.items() if probe.get("failing")]
    payload = {
        "generated_at": now_iso(),
        "cached": False,
        "cache_age_seconds": 0,
        "sample_count": NODE_TEMPLATE_PROBE_SAMPLES,
        "cache_ttl_seconds": NODE_TEMPLATE_PROBE_CACHE_SECONDS,
        "nodes": node_probes,
        "failing_nodes": failing_nodes,
        "all_nodes_failing": bool(node_probes and len(failing_nodes) == len(node_probes)),
    }
    write_json_file(
        NODE_TEMPLATE_PROBE_CACHE_FILE,
        {"epoch": now, "generated_at": now_iso(), "payload": payload},
    )
    return payload


def _parse_log_timestamp(line: str | None) -> str | None:
    if not line:
        return None
    match = LOG_TS_RE.search(line)
    return match.group(1) if match else None


def _pool_log_epoch(line: str | None) -> float | None:
    if not line:
        return None
    match = LOG_TS_RE.search(line)
    if not match:
        return None
    try:
        parsed = datetime.strptime(match.group(1), "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc).timestamp()


def _pool_log_age_seconds(line: str | None) -> int | None:
    epoch = _pool_log_epoch(line)
    if epoch is None:
        return None
    return int(max(0, time.time() - epoch))


def _is_stale_age(age_seconds: int | None, threshold_seconds: int) -> bool:
    return age_seconds is not None and age_seconds > threshold_seconds


def _parse_key_value_from_log(line: str, key: str) -> str:
    match = re.search(rf"\b{re.escape(key)}=([^\s]+)", line)
    return match.group(1) if match else ""


def _parse_submit_stall_event(line: str) -> dict[str, Any]:
    action = "event"
    if "recovery triggered" in line:
        action = "triggered"
    elif "recovered by switching backend" in line:
        action = "backend-fallback"
    elif "[ROUTER]" in line and "after submit stall" in line:
        action = "backend-fallback"
    elif "invalidated" in line:
        action = "invalidated"

    backend_from = ""
    backend_to = ""
    switch_match = re.search(r"switched backend\s+([^\s]+)\s+->\s+([^\s]+)", line)
    if switch_match:
        backend_from = switch_match.group(1)
        backend_to = switch_match.group(2)
    else:
        backend_from = _parse_key_value_from_log(line, "backend")
        to_match = re.search(r"switching backend to\s+([^\s]+)", line)
        if not to_match:
            to_match = re.search(r"recovered by switching backend to\s+([^\s]+)", line)
        backend_to = to_match.group(1) if to_match else ""

    epoch = _pool_log_epoch(line)
    return {
        "action": action,
        "reason": _parse_key_value_from_log(line, "reason"),
        "backend_from": backend_from,
        "backend_to": backend_to,
        "parent": _parse_key_value_from_log(line, "parent"),
        "old_parent": _parse_key_value_from_log(line, "old_parent"),
        "new_parent": _parse_key_value_from_log(line, "new_parent"),
        "job_age": _parse_key_value_from_log(line, "job_age"),
        "invalidated_jobs": _parse_key_value_from_log(line, "invalidated_jobs"),
        "at": _parse_log_timestamp(line),
        "epoch": epoch,
        "age_seconds": int(max(0, time.time() - epoch)) if epoch is not None else None,
        "line": line,
    }


def _parse_prometheus_labels(text: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for match in PROMETHEUS_LABEL_RE.finditer(text or ""):
        value = match.group(2).replace(r"\"", '"').replace(r"\\", "\\")
        labels[match.group(1)] = value
    return labels


def _metric_counter_key(labels: dict[str, str], *names: str) -> str:
    return ":".join(str(labels.get(name, "")) for name in names)


def _prometheus_json_number(value: float) -> float | int:
    return int(value) if value.is_integer() else value


def _float_metric(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _prometheus_counter_json(counter: Counter[str] | dict[str, Any]) -> dict[str, float | int]:
    rows = counter.items() if isinstance(counter, dict) else []
    return {str(key): _prometheus_json_number(_float_metric(value)) for key, value in sorted(rows)}


def _ratio_percent(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return round((numerator / denominator) * 100, 2)


def build_pool_efficiency_loss_ledger(
    block_submit_outcomes: Counter[str] | dict[str, Any],
    shares_accepted_total: float,
    shares_rejected_by_reason: Counter[str] | dict[str, Any],
    block_totals: Counter[str] | dict[str, Any],
    blocks_rejected_by_node: Counter[str] | dict[str, Any],
    share_processing: dict[str, Any] | None = None,
    template_conversion_stall: dict[str, Any] | None = None,
    block_submit_backend_outcomes: Counter[str] | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize pool efficiency counters into an operator-facing loss ledger."""
    block_outcomes = Counter({str(key): _float_metric(value) for key, value in dict(block_submit_outcomes or {}).items()})
    share_rejections = Counter({str(key): _float_metric(value) for key, value in dict(shares_rejected_by_reason or {}).items()})
    blocks = Counter({str(key): _float_metric(value) for key, value in dict(block_totals or {}).items()})
    node_rejects = Counter({str(key): _float_metric(value) for key, value in dict(blocks_rejected_by_node or {}).items()})
    backend_outcomes = Counter({str(key): _float_metric(value) for key, value in dict(block_submit_backend_outcomes or {}).items()})
    processing = share_processing if isinstance(share_processing, dict) else {}
    conversion = template_conversion_stall if isinstance(template_conversion_stall, dict) else {}

    block_total = float(sum(block_outcomes.values()))
    block_accepted = float(sum(value for key, value in block_outcomes.items() if key.startswith("accepted:")))
    block_rejected_node = float(sum(value for key, value in block_outcomes.items() if key.startswith("rejected:")))
    block_rejected_local = float(sum(value for key, value in block_outcomes.items() if key.startswith("rejected-local:")))
    block_rejected = block_rejected_node + block_rejected_local
    block_accept_ratio = _ratio_percent(block_accepted, block_total)
    block_loss_ratio = None if block_accept_ratio is None else round(max(0.0, 100.0 - block_accept_ratio), 2)

    share_accepted = _float_metric(shares_accepted_total)
    share_rejected = float(sum(share_rejections.values()))
    share_total = share_accepted + share_rejected
    share_accept_ratio = _ratio_percent(share_accepted, share_total)
    duplicate_share_rejects = float(
        sum(value for key, value in share_rejections.items() if key.startswith("duplicate_block"))
    )
    stale_share_rejects = (
        share_rejections.get("invalidated_job", 0.0)
        + share_rejections.get("non_current_job", 0.0)
        + share_rejections.get("stale_block_candidate", 0.0)
    )

    processing_count = _float_metric(processing.get("count"))
    processing_sum = _float_metric(processing.get("sum_seconds"))
    processing_avg = round(processing_sum / processing_count, 6) if processing_count > 0 else None

    top_loss_reasons: list[dict[str, Any]] = []
    for key, value in block_outcomes.items():
        if value <= 0 or key.startswith("accepted:"):
            continue
        top_loss_reasons.append(
            {
                "plane": "block_submit",
                "reason": key,
                "count": _prometheus_json_number(value),
                "ratio_percent": _ratio_percent(value, block_total),
            }
        )
    for reason, value in share_rejections.items():
        if value <= 0:
            continue
        top_loss_reasons.append(
            {
                "plane": "share",
                "reason": reason,
                "count": _prometheus_json_number(value),
                "ratio_percent": _ratio_percent(value, share_total),
            }
        )
    top_loss_reasons.sort(key=lambda row: float(row.get("count") or 0), reverse=True)

    warnings: list[str] = []
    active_miners = safe_int(conversion.get("active_miners"), 0)
    conversion_failure_ratio = _float_metric(conversion.get("failure_ratio"), -1.0)
    if active_miners >= 2 and conversion_failure_ratio >= 15.0:
        warnings.append(
            f"template conversion loss is {conversion_failure_ratio:.2f}% with {active_miners} active miner lanes"
        )
    if block_total >= 20 and block_accept_ratio is not None and block_accept_ratio < 70.0:
        warnings.append(f"block submit acceptance is {block_accept_ratio:.2f}% over {int(block_total)} outcomes")
    tip_overdue = block_outcomes.get("rejected:tip-overdue", node_rejects.get("tip-overdue", 0.0))
    tip_overdue_ratio = _ratio_percent(tip_overdue, block_total)
    if block_total >= 20 and tip_overdue_ratio is not None and tip_overdue_ratio >= 10.0:
        warnings.append(f"tip-overdue block loss is {tip_overdue_ratio:.2f}% of submit outcomes")
    duplicate_local = block_outcomes.get("rejected-local:duplicate-block", 0.0)
    duplicate_local_ratio = _ratio_percent(duplicate_local, block_total)
    if block_total >= 20 and duplicate_local_ratio is not None and duplicate_local_ratio >= 5.0:
        warnings.append(f"duplicate-block local loss is {duplicate_local_ratio:.2f}% of submit outcomes")
    if share_total >= 50 and share_accept_ratio is not None and share_accept_ratio < 70.0:
        warnings.append(f"share acceptance is {share_accept_ratio:.2f}% over {int(share_total)} share outcomes")
    if processing_avg is not None and processing_avg > 0.2:
        warnings.append(f"average share processing latency is {processing_avg:.3f}s")

    severity = "ok"
    if warnings:
        severity = "warning"
    if (
        (block_total >= 20 and block_accept_ratio is not None and block_accept_ratio < 50.0)
        or (active_miners >= 2 and conversion_failure_ratio >= 50.0)
    ):
        severity = "critical"

    return {
        "version": 1,
        "severity": severity,
        "active_miners": active_miners,
        "block_outcomes": {
            "accepted": _prometheus_json_number(block_accepted),
            "rejected": _prometheus_json_number(block_rejected),
            "rejected_by_node": _prometheus_json_number(block_rejected_node),
            "rejected_local": _prometheus_json_number(block_rejected_local),
            "total": _prometheus_json_number(block_total),
            "accepted_ratio_percent": block_accept_ratio,
            "loss_ratio_percent": block_loss_ratio,
            "by_outcome_reason": _prometheus_counter_json(block_outcomes),
        },
        "share_outcomes": {
            "accepted": _prometheus_json_number(share_accepted),
            "rejected": _prometheus_json_number(share_rejected),
            "total": _prometheus_json_number(share_total),
            "accepted_ratio_percent": share_accept_ratio,
            "duplicate_block_rejects": _prometheus_json_number(duplicate_share_rejects),
            "stale_job_rejects": _prometheus_json_number(float(stale_share_rejects)),
            "rejected_by_reason": _prometheus_counter_json(share_rejections),
        },
        "block_totals": _prometheus_counter_json(blocks),
        "blocks_rejected_by_node": _prometheus_counter_json(node_rejects),
        "block_submit_backend_outcomes": _prometheus_counter_json(backend_outcomes),
        "share_processing": {
            "count": _prometheus_json_number(processing_count),
            "sum_seconds": round(processing_sum, 6),
            "avg_seconds": processing_avg,
        },
        "top_loss_reasons": top_loss_reasons[:8],
        "warnings": warnings,
    }


def selected_backend_readiness_contract(
    selected_backend: str,
    selected_source_health: dict[str, Any],
    source_job_health: dict[str, Any],
    pool_has_recent_mining: bool,
) -> dict[str, Any]:
    source = selected_source_health if isinstance(selected_source_health, dict) else {}
    job_health = source_job_health if isinstance(source_job_health, dict) else {}
    checks: dict[str, bool] = {}
    for key in ("node_mineable", "node_submit_ready", "node_p2p_mining_fresh", "healthy", "ws_connected"):
        if key in source:
            checks[key] = bool(source.get(key))
    if source.get("node_last_template_build_error_blocking") is True:
        checks["node_last_template_build_error_blocking_clear"] = False

    job_ok_raw = job_health.get("ok")
    job_ok = None if job_ok_raw is None else bool(job_ok_raw)
    node_unready = any(
        checks.get(key) is False
        for key in ("node_mineable", "node_submit_ready", "node_p2p_mining_fresh")
    ) or checks.get("node_last_template_build_error_blocking_clear") is False
    job_unready = job_ok is False
    contradiction = bool(pool_has_recent_mining and (node_unready or job_unready))
    hard_unready = bool((node_unready or job_unready) and not pool_has_recent_mining)
    return {
        "version": 1,
        "selected_backend": selected_backend,
        "pool_has_recent_mining": pool_has_recent_mining,
        "job_health_ok": job_ok,
        "checks": checks,
        "contradiction": contradiction,
        "hard_unready": hard_unready,
        "advisory_degraded": bool(contradiction),
        "truth_basis": "recent accepted pool work overrides stale readiness booleans for operator status; fix the metric/gate if they disagree",
    }


def collect_pool_prometheus_metrics(containers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "generated_at": now_iso(),
        "status": "unavailable",
        "error": "",
        "containers": {},
        "active_connections": None,
        "selected_backend": "",
        "active_node_source": "",
        "block_submit_outcomes": {},
        "block_submit_backend_outcomes": {},
        "blocks": {},
        "blocks_rejected_by_node": {},
        "shares_accepted_total": 0.0,
        "shares_rejected_by_reason": {},
        "share_processing": {},
        "loss_ledger": {},
        "submit_stall_recoveries": {},
        "submit_stall_recoveries_total": 0.0,
        "template_backend_state": {},
        "source_job_health": {},
        "source_backend_health": {},
        "selected_backend_source_health": {},
        "active_node_source_health": {},
        "template_conversion_stall": {},
    }
    if POOL_METRICS_PORT <= 0:
        payload["error"] = "pool metrics port disabled"
        return payload

    errors: list[str] = []
    any_ok = False
    block_submit_outcomes: Counter[str] = Counter()
    block_submit_backend_outcomes: Counter[str] = Counter()
    block_totals: Counter[str] = Counter()
    blocks_rejected_by_node: Counter[str] = Counter()
    shares_accepted_total = 0.0
    shares_rejected_by_reason: Counter[str] = Counter()
    share_processing_count = 0.0
    share_processing_sum = 0.0
    submit_recoveries: Counter[str] = Counter()
    selected_backend = ""
    active_connections: float | None = None
    source_job_health: dict[str, Any] = {}
    source_backend_health: dict[str, dict[str, Any]] = {}
    template_conversion_stall: dict[str, Any] = {}
    template_backend_source = ""

    for name in POOL_CONTAINERS:
        info = containers.get(name) if isinstance(containers, dict) else None
        if not isinstance(info, dict) or not info.get("running"):
            continue
        ips = [str(ip) for ip in info.get("network_ips") or [] if ip]
        if not ips:
            errors.append(f"{name}: no container network IP")
            continue
        endpoint = f"{ips[0]}:{POOL_METRICS_PORT}"
        row: dict[str, Any] = {"endpoint": endpoint, "status": "unavailable", "error": ""}
        try:
            text = fetch_text_url(
                f"http://{endpoint}/metrics",
                {"accept": "text/plain", "user-agent": HTTP_USER_AGENT},
                timeout=POOL_METRICS_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - metrics are advisory only.
            row["error"] = str(exc)
            errors.append(f"{name}: {exc}")
            payload["containers"][name] = row
            continue

        row["status"] = "ok"
        any_ok = True
        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            match = PROMETHEUS_SAMPLE_RE.match(line.strip())
            if not match:
                continue
            metric_name, label_text, raw_value = match.groups()
            try:
                value = float(raw_value)
            except ValueError:
                continue
            labels = _parse_prometheus_labels(label_text or "")
            if metric_name == "pool_active_connections":
                active_connections = value if active_connections is None else active_connections + value
            elif metric_name.startswith("pool_job_health_"):
                key = metric_name.removeprefix("pool_job_health_")
                if key == "accepted_jobs":
                    accepted = source_job_health.setdefault("accepted_jobs", {})
                    if isinstance(accepted, dict):
                        accepted[str(labels.get("state") or "unknown")] = _prometheus_json_number(value)
                elif key == "ok":
                    source_job_health["ok"] = value > 0
                else:
                    source_job_health[key] = _prometheus_json_number(value)
            elif metric_name in {
                "pool_rpc_backend_selected",
                "pool_rpc_backend_healthy",
                "pool_rpc_backend_score",
                "pool_rpc_backend_template_age_seconds",
                "pool_rpc_backend_ws_connected",
            }:
                backend = labels.get("backend")
                if not backend:
                    continue
                row = source_backend_health.setdefault(backend, {})
                if metric_name == "pool_rpc_backend_selected":
                    row["selected"] = value > 0
                    if value > 0:
                        selected_backend = backend
                elif metric_name == "pool_rpc_backend_healthy":
                    row["healthy"] = value > 0
                elif metric_name == "pool_rpc_backend_score":
                    row["score"] = value
                elif metric_name == "pool_rpc_backend_template_age_seconds":
                    row["template_age_seconds"] = round(value, 3)
                elif metric_name == "pool_rpc_backend_ws_connected":
                    row["ws_connected"] = value > 0
            elif metric_name.startswith("pool_rpc_backend_node_health_"):
                backend = labels.get("backend")
                if not backend:
                    continue
                row = source_backend_health.setdefault(backend, {})
                key = metric_name.removeprefix("pool_rpc_backend_node_health_")
                if key in {
                    "mineable",
                    "submit_ready",
                    "p2p_mining_fresh",
                    "p2p_sync_peer_fresh",
                    "p2p_sync_peer_present",
                    "pending_template_build",
                    "last_template_build_error_blocking",
                }:
                    row[f"node_{key}"] = value > 0
                elif key in {"last_template_invalidation_sequence", "pending_template_invalidation"}:
                    cause = str(labels.get("cause") or "unknown")
                    bucket = row.setdefault(f"node_{key}", {})
                    if isinstance(bucket, dict):
                        bucket[cause] = _prometheus_json_number(value)
                else:
                    row[f"node_{key}"] = _prometheus_json_number(value)
            elif metric_name.startswith("pool_template_conversion_stall_"):
                key = metric_name.removeprefix("pool_template_conversion_stall_")
                if key == "window_candidates":
                    candidates = template_conversion_stall.setdefault("window_candidates", {})
                    if isinstance(candidates, dict):
                        candidates[str(labels.get("kind") or "unknown")] = _prometheus_json_number(value)
                else:
                    template_conversion_stall[key] = _prometheus_json_number(value)
            elif metric_name == "pool_block_submit_outcomes_total":
                block_submit_outcomes[_metric_counter_key(labels, "outcome", "reason")] += value
            elif metric_name == "pool_block_submit_backend_outcomes_total":
                block_submit_backend_outcomes[_metric_counter_key(labels, "backend", "outcome", "reason")] += value
            elif metric_name == "pool_blocks_rejected_by_node_total":
                blocks_rejected_by_node[str(labels.get("reason") or "unknown")] += value
            elif metric_name.startswith("pool_blocks_") and metric_name.endswith("_total"):
                block_key = metric_name.removeprefix("pool_blocks_").removesuffix("_total")
                block_totals[block_key] += value
            elif metric_name == "pool_shares_accepted_total":
                shares_accepted_total += value
            elif metric_name == "pool_shares_rejected_total":
                shares_rejected_by_reason[str(labels.get("reason") or "unknown")] += value
            elif metric_name == "pool_share_processing_duration_seconds_count":
                share_processing_count += value
            elif metric_name == "pool_share_processing_duration_seconds_sum":
                share_processing_sum += value
            elif metric_name == "pool_submit_stall_recoveries_total":
                submit_recoveries[_metric_counter_key(labels, "action", "reason")] += value
        payload["containers"][name] = row
        if source_backend_health and not template_backend_source:
            template_backend_source = endpoint

    payload["status"] = "ok" if any_ok else "unavailable"
    payload["error"] = "; ".join(errors[:3])
    payload["active_connections"] = active_connections
    payload["selected_backend"] = selected_backend
    payload["active_node_source"] = selected_backend
    payload["block_submit_outcomes"] = dict(block_submit_outcomes)
    payload["block_submit_backend_outcomes"] = dict(block_submit_backend_outcomes)
    payload["blocks"] = dict(block_totals)
    payload["blocks_rejected_by_node"] = dict(blocks_rejected_by_node)
    payload["shares_accepted_total"] = _prometheus_json_number(shares_accepted_total)
    payload["shares_rejected_by_reason"] = dict(shares_rejected_by_reason)
    payload["share_processing"] = {
        "count": _prometheus_json_number(share_processing_count),
        "sum_seconds": round(share_processing_sum, 6),
        "avg_seconds": round(share_processing_sum / share_processing_count, 6) if share_processing_count > 0 else None,
    }
    payload["submit_stall_recoveries"] = dict(submit_recoveries)
    payload["submit_stall_recoveries_total"] = float(sum(submit_recoveries.values()))
    payload["source_job_health"] = source_job_health
    payload["source_backend_health"] = source_backend_health
    payload["template_conversion_stall"] = template_conversion_stall
    if source_backend_health:
        payload["template_backend_state"] = {
            "source": template_backend_source,
            "backends": source_backend_health,
            "job_health": source_job_health,
            "template_conversion_stall": template_conversion_stall,
            "selected_backend": selected_backend,
            "backend_count": len(source_backend_health),
            "healthy_backend_count": sum(1 for row in source_backend_health.values() if row.get("healthy") is True),
        }
        selected_health = source_backend_health.get(selected_backend) if selected_backend else None
        payload["selected_backend_source_health"] = selected_health if isinstance(selected_health, dict) else {}
        payload["active_node_source_health"] = payload["selected_backend_source_health"]
    payload["loss_ledger"] = build_pool_efficiency_loss_ledger(
        block_submit_outcomes=block_submit_outcomes,
        shares_accepted_total=shares_accepted_total,
        shares_rejected_by_reason=shares_rejected_by_reason,
        block_totals=block_totals,
        blocks_rejected_by_node=blocks_rejected_by_node,
        share_processing=payload["share_processing"],
        template_conversion_stall=template_conversion_stall,
        block_submit_backend_outcomes=block_submit_backend_outcomes,
    )
    return payload


def parse_pool_activity(log: str) -> dict[str, Any]:
    job_to_client: dict[str, dict[str, str]] = {}
    extranonce_to_client: dict[str, dict[str, str]] = {}
    worker_to_client: dict[str, dict[str, str]] = {}
    worker_client_priority: dict[str, int] = {}
    ambiguous_worker_clients: set[str] = set()
    miners: dict[str, dict[str, Any]] = {}

    def note_worker_client(worker: str, ip: str, port: str = "", priority: int = 1) -> None:
        current = worker_to_client.get(worker)
        if current and current.get("ip") != ip:
            ambiguous_worker_clients.add(worker)
        current_priority = worker_client_priority.get(worker, -1)
        if priority < current_priority:
            return
        worker_to_client[worker] = {"ip": ip, "port": port}
        worker_client_priority[worker] = priority

    def client_for_worker(worker: str) -> dict[str, str] | None:
        if worker in ambiguous_worker_clients:
            return None
        return worker_to_client.get(worker)

    def job_extranonce(job_id: str) -> str:
        match = JOB_EXTRANONCE_RE.search(str(job_id or ""))
        return match.group(1).lower() if match else ""

    def client_from_addr(ip: str | None, port: str | None = "") -> dict[str, str] | None:
        if not ip or not is_ipv4(str(ip)):
            return None
        return {"ip": str(ip), "port": str(port or "")}

    def client_from_line(line: str) -> dict[str, str] | None:
        match = CLIENT_ADDR_RE.search(line)
        if not match:
            return None
        return client_from_addr(match.group(1), match.group(2))

    def note_job_client(job_id: str, client: dict[str, str]) -> None:
        if not job_id or not client:
            return
        job_to_client.setdefault(job_id, client)
        extranonce = job_extranonce(job_id)
        if extranonce:
            extranonce_to_client.setdefault(extranonce, client)

    def client_for_job_or_worker(job_id: str, worker: str = "") -> dict[str, str] | None:
        return (
            job_to_client.get(job_id)
            or extranonce_to_client.get(job_extranonce(job_id))
            or (client_for_worker(worker) if worker else None)
        )

    for registered in read_miner_registry().get("miners", []):
        ip = str(registered.get("ip") or "")
        if not is_ipv4(ip):
            continue
        priority = 2 if normalize_mac(registered.get("mac")) or registered.get("display_name") else 1
        for worker in merge_unique_strings(registered.get("last_workers"), registered.get("expected_worker_user")):
            note_worker_client(worker, ip, priority=priority)

    def miner_for_ip(ip: str) -> dict[str, Any]:
        item = miners.setdefault(
            ip,
            {
                "ip": ip,
                "device_type": "stratum",
                "source": "pool-log",
                "jobs": 0,
                "submits": 0,
                "shares": 0,
                "share_work": 0,
                "share_difficulty": 0.0,
                "blocks_found": 0,
                "ports": [],
                "first_seen_at": None,
                "last_seen_at": None,
                "last_job_at": None,
                "last_submit_at": None,
                "last_share_at": None,
                "last_block_at": None,
                "last_difficulty": None,
                "workers": [],
            },
        )
        return item

    def note_seen(item: dict[str, Any], line: str, field: str = "last_seen_at") -> None:
        timestamp = _parse_log_timestamp(line)
        if timestamp:
            if not item.get("first_seen_at"):
                item["first_seen_at"] = timestamp
            item[field] = timestamp
            item["last_seen_at"] = timestamp

    def note_port(item: dict[str, Any], port: str | None) -> None:
        if not port:
            return
        ports = item.setdefault("ports", [])
        if port not in ports:
            ports.append(port)

    def note_worker(item: dict[str, Any], worker: str | None) -> None:
        if not worker:
            return
        workers = item.setdefault("workers", [])
        if worker not in workers:
            workers.append(worker)

    for line in strip_ansi(log).splitlines():
        pushdif = PUSHDIF_RE.search(line)
        if pushdif:
            ip, port, difficulty = pushdif.groups()
            item = miner_for_ip(ip)
            note_port(item, port)
            item["last_difficulty"] = difficulty
            note_seen(item, line, "last_job_at")
            continue

        auth = AUTH_ACCEPT_RE.search(line)
        if auth:
            ip, port, worker = auth.groups()
            note_worker_client(worker, ip, port=port, priority=1)
            item = miner_for_ip(ip)
            note_port(item, port)
            note_worker(item, worker)
            note_seen(item, line)
            continue

        subscribe = SUBSCRIBE_ACCEPT_RE.search(line)
        if subscribe:
            ip, port, extranonce = subscribe.groups()
            client = {"ip": ip, "port": port}
            extranonce_to_client.setdefault(extranonce.lower(), client)
            item = miner_for_ip(ip)
            note_port(item, port)
            note_seen(item, line)
            continue

        notify = JOB_NOTIFY_DETAIL_RE.search(line)
        if notify:
            ip, port, job_id = notify.groups()
            note_job_client(job_id, {"ip": ip, "port": port})
            item = miner_for_ip(ip)
            note_port(item, port)
            item["jobs"] += 1
            note_seen(item, line, "last_job_at")
            continue

        legacy_notify = JOB_NOTIFY_RE.search(line)
        if legacy_notify:
            ip, job_id = legacy_notify.groups()
            note_job_client(job_id, {"ip": ip, "port": ""})
            item = miner_for_ip(ip)
            item["jobs"] += 1
            note_seen(item, line, "last_job_at")
            continue

        submit = SUBMIT_RE.search(line)
        if submit:
            client_ip, client_port, worker, job_id = submit.groups()
            direct_client = client_from_addr(client_ip, client_port)
            if direct_client:
                note_job_client(job_id, direct_client)
            client = direct_client or client_for_job_or_worker(job_id, worker)
            if not client:
                continue
            note_job_client(job_id, client)
            item = miner_for_ip(client["ip"])
            note_port(item, client.get("port"))
            note_worker(item, worker)
            item["submits"] += submit_line_weight(line)
            note_seen(item, line, "last_submit_at")
            continue

        share = VALID_SHARE_RE.search(line)
        if share:
            client_ip = share.group(3)
            client_port = share.group(4)
            worker = share.group(5)
            job_id = share.group(6)
            direct_client = client_from_addr(client_ip, client_port)
            if direct_client:
                note_job_client(job_id, direct_client)
            client = direct_client or client_for_job_or_worker(job_id, worker)
            if not client:
                continue
            note_job_client(job_id, client)
            item = miner_for_ip(client["ip"])
            note_port(item, client.get("port"))
            item["shares"] += valid_share_line_weight(line)
            item["share_difficulty"] = round(
                float(item["share_difficulty"]) + float(share.group(1)) + valid_share_line_suppressed_diff(line),
                8,
            )
            item["share_work"] += int(share.group(2)) + valid_share_line_suppressed_work(line)
            note_worker(item, worker)
            note_seen(item, line, "last_share_at")
            note_seen(item, line, "last_submit_at")
            continue

        block = BLOCK_FOUND_RE.search(line)
        if block:
            job_id = block.group(1)
            direct_client = client_from_line(line)
            if direct_client:
                note_job_client(job_id, direct_client)
            client = direct_client or client_for_job_or_worker(job_id)
            if not client:
                continue
            item = miner_for_ip(client["ip"])
            note_port(item, client.get("port"))
            item["blocks_found"] += 1
            note_seen(item, line, "last_block_at")

    for item in miners.values():
        observed_submits = int(item.get("shares", 0) or 0) + int(item.get("blocks_found", 0) or 0)
        if int(item.get("submits", 0) or 0) < observed_submits:
            item["submits"] = observed_submits

    return {
        "generated_at": now_iso(),
        "miners": sorted(miners.values(), key=lambda item: int(ipaddress.ip_address(item["ip"]))),
    }


def submit_line_weight(line: str) -> int:
    if not SUBMIT_RE.search(line):
        return 0
    suppressed = SUBMIT_SUPPRESSED_RE.search(line)
    return 1 + safe_int(suppressed.group(1), 0) if suppressed else 1


def valid_share_line_weight(line: str) -> int:
    if not VALID_SHARE_RE.search(line):
        return 0
    suppressed = SUBMIT_SUPPRESSED_RE.search(line)
    return 1 + safe_int(suppressed.group(1), 0) if suppressed else 1


def valid_share_line_suppressed_diff(line: str) -> float:
    suppressed = VALID_SHARE_SUPPRESSED_DIFF_RE.search(line)
    if not suppressed:
        return 0.0
    try:
        return float(suppressed.group(1))
    except (TypeError, ValueError):
        return 0.0


def valid_share_line_suppressed_work(line: str) -> int:
    suppressed = VALID_SHARE_SUPPRESSED_WORK_RE.search(line)
    return safe_int(suppressed.group(1), 0) if suppressed else 0


def collect_pool_activity(lines: int = 2500) -> dict[str, Any]:
    log = docker_logs_many(POOL_CONTAINERS, lines=lines)
    return parse_pool_activity(log)


def upsert_pool_activity_miners(activity: dict[str, Any]) -> dict[str, Any]:
    """Persist miners seen passively in the stratum pool logs."""
    registry = read_miner_registry()
    existing = {str(item.get("ip")): dict(item) for item in registry.get("miners", []) if item.get("ip")}
    existing_by_mac = {
        normalize_mac(item.get("mac")): dict(item)
        for item in registry.get("miners", [])
        if normalize_mac(item.get("mac"))
    }
    neighbors = read_neighbor_macs()
    defaults = default_miner_pool_settings()
    changed = False

    for miner in activity.get("miners", []):
        ip = str(miner.get("ip", ""))
        if not is_ipv4(ip):
            continue

        mac = miner_mac_from_payload(miner, ip, neighbors)
        if is_docker_bridge_pool_log_client(ip, mac):
            continue
        item = existing_by_mac.get(mac) if mac else None
        item = dict(item or existing.get(ip, {"ip": ip}))
        workers = merge_unique_strings(item.get("last_workers"), miner.get("workers"))
        ports = merge_unique_strings(item.get("last_ports"), miner.get("ports"))
        last_seen_log_at = str(miner.get("last_seen_at") or "")
        previous_seen_log_at = str(item.get("last_pool_seen_log_at") or "")
        last_submit_log_at = str(miner.get("last_submit_at") or "")
        previous_submit_log_at = str(item.get("last_submit_log_at") or "")
        last_share_log_at = str(miner.get("last_share_at") or "")
        previous_share_log_at = str(item.get("last_share_log_at") or "")
        now_epoch = seconds_since_epoch()

        if not item.get("device_type"):
            has_asic_metadata = bool(item.get("managed") or item.get("model") or item.get("hardware") or item.get("firmware"))
            item["device_type"] = "asic" if has_asic_metadata else "stratum"
        if not item.get("discovered_by"):
            item["discovered_by"] = "lan-scan" if item.get("device_type") == "asic" else "pool-log"
        if not item.get("model") and item.get("device_type") == "stratum":
            item["model"] = "Stratum client"

        item.update(
            {
                "ip": ip,
                "mac": mac or item.get("mac", ""),
                "device_id": f"mac:{mac}" if mac else item.get("device_id", ""),
                "ip_history": merge_unique_strings(item.get("ip_history"), ip),
                "sources": merge_unique_strings(item.get("sources"), "pool-log"),
                "auto_discovered": bool(item.get("auto_discovered", item.get("discovered_by") == "pool-log")),
                "expected_pool_url": item.get("expected_pool_url") or defaults["pool_url"],
                "expected_worker_user": item.get("expected_worker_user") or (workers[0] if workers else defaults["worker_user"]),
                "last_pool_seen_at": last_seen_log_at or item.get("last_pool_seen_at") or now_iso(),
                "last_pool_seen_log_at": last_seen_log_at or item.get("last_pool_seen_log_at"),
                "last_job_at": miner.get("last_job_at") or item.get("last_job_at"),
                "last_submit_at": miner.get("last_submit_at") or item.get("last_submit_at"),
                "last_share_at": last_share_log_at or item.get("last_share_at"),
                "last_share_log_at": last_share_log_at or item.get("last_share_log_at"),
                "last_block_at": miner.get("last_block_at") or item.get("last_block_at"),
                "last_workers": workers,
                "last_ports": ports,
                "last_difficulty": miner.get("last_difficulty") or item.get("last_difficulty"),
                "last_jobs_window": int(miner.get("jobs", 0) or 0),
                "last_submits_window": int(miner.get("submits", 0) or 0),
                "last_shares_window": int(miner.get("shares", 0) or 0),
                "last_share_work_window": int(miner.get("share_work", 0) or 0),
                "last_share_difficulty_window": miner.get("share_difficulty", 0),
                "last_blocks_window": int(miner.get("blocks_found", 0) or 0),
                "last_activity_checked_at": now_iso(),
            }
        )
        if last_seen_log_at and last_seen_log_at != previous_seen_log_at:
            item["last_pool_seen_epoch"] = now_epoch
        elif "last_pool_seen_epoch" not in item:
            item["last_pool_seen_epoch"] = now_epoch
        if last_submit_log_at and last_submit_log_at != previous_submit_log_at:
            item["last_submit_epoch"] = now_epoch
            item["last_submit_log_at"] = last_submit_log_at
        elif last_submit_log_at and "last_submit_epoch" not in item:
            item["last_submit_epoch"] = now_epoch
        if last_share_log_at and last_share_log_at != previous_share_log_at:
            item["last_share_epoch"] = now_epoch
        elif last_share_log_at and "last_share_epoch" not in item:
            item["last_share_epoch"] = now_epoch

        existing[ip] = item
        if mac:
            existing_by_mac[mac] = item
        changed = True

    return save_miner_registry(list(existing.values())) if changed else registry


def miner_health_count_summary(health: list[dict[str, Any]]) -> dict[str, int]:
    managed_count = sum(1 for item in health if item["managed"])
    return {
        "managed_count": managed_count,
        "managed_ok_count": sum(1 for item in health if item["managed"] and item["status"] == "ok"),
        "ok_count": sum(1 for item in health if item["status"] == "ok"),
        "tracked_count": len(health),
        "connected_count": sum(1 for item in health if item.get("connected")),
        "stratum_count": sum(1 for item in health if item.get("device_type") == "stratum"),
    }


def collect_miner_health() -> dict[str, Any]:
    defaults = default_miner_pool_settings()
    activity = collect_pool_activity(lines=POOL_ACTIVITY_LOG_LINES)
    registry = upsert_pool_activity_miners(activity)
    miners = registry.get("miners", [])
    activity_by_ip = {item["ip"]: item for item in activity["miners"]}
    health: list[dict[str, Any]] = []
    failures: list[str] = []
    warnings: list[str] = []
    now_epoch = seconds_since_epoch()

    for registered in miners:
        ip = str(registered.get("ip", ""))
        if not is_ipv4(ip):
            continue
        expected_url = registered.get("expected_pool_url") or defaults["pool_url"]
        expected_user = registered.get("expected_worker_user") or defaults["worker_user"]
        activity_item = activity_by_ip.get(ip, {})
        device_type = str(registered.get("device_type") or ("asic" if registered.get("model") else "stratum"))
        discovered_by = str(registered.get("discovered_by") or "")
        api_expected = device_type == "asic" and is_lan_ipv4(ip)
        api_error = ""
        debug_error = ""
        discovered = None
        cgminer_devs: dict[str, Any] = {}
        configured = False
        pool_active = False
        if api_expected:
            try:
                discovered = discover_miner(ip, timeout=MINER_HTTP_TIMEOUT)
                pools = discovered.get("pools", []) if discovered else []
                configured = any(str(pool.get("url", "")) == expected_url and str(pool.get("user", "")) == expected_user for pool in pools)
                pool_active = any(
                    str(pool.get("url", "")) == expected_url
                    and str(pool.get("user", "")) == expected_user
                    and bool(pool.get("active"))
                    for pool in pools
                )
            except Exception as exc:  # noqa: BLE001 - dashboard should show per-miner API failures.
                api_error = str(exc)
            try:
                cgminer_devs = get_miner_cgminer_devs(ip, timeout=MINER_HTTP_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - debug API is useful but should not hide pool-side health.
                debug_error = str(exc)

        mac = normalize_mac((discovered or {}).get("mac")) or normalize_mac(registered.get("mac")) or mac_for_ip(ip)
        if is_docker_bridge_pool_log_client(ip, mac):
            # Docker bridge clients can appear in pool logs during local health/API calls.
            # They are not physical ASICs and should not affect miner counts or repairs.
            continue
        retirement_decision = retired_miner_identity_decision({**registered, **activity_item}, ip, mac)
        if retirement_decision.get("conflict"):
            label = miner_display_label({**registered, "mac": mac})
            retired_name = retirement_decision.get("retired_name") or "retired miner"
            warnings.append(
                f"{label} mac={mac or 'unknown-mac'} observed_ip={ip} is active in pool logs but shares "
                f"an observed retired-miner IP for {retired_name}; "
                "keeping it active because only MAC address can retire an ASIC"
            )
        last_pool_seen_epoch = int(registered.get("last_pool_seen_epoch", 0) or 0)
        last_submit_epoch = int(registered.get("last_submit_epoch", 0) or 0)
        last_share_epoch = int(registered.get("last_share_epoch", 0) or 0)
        workers = merge_unique_strings(activity_item.get("workers"), registered.get("last_workers"))
        ports = merge_unique_strings(activity_item.get("ports"), registered.get("last_ports"))
        connected = bool(activity_item) or bool(last_pool_seen_epoch and now_epoch - last_pool_seen_epoch <= POOL_CONNECTED_STALE_SECONDS)
        managed = bool(registered.get("managed"))
        configured_record = bool(registered.get("configured") or registered.get("managed") or registered.get("last_configured_ok"))
        if not api_expected and (is_pool_log_only_miner(registered) or device_type == "stratum" or discovered_by == "pool-log"):
            expected_worker_seen = str(expected_user).lower() in {str(worker).lower() for worker in workers}
            if connected and expected_url == defaults["pool_url"] and expected_worker_seen:
                configured = configured_record
                pool_active = True
        current_submits = int(activity_item.get("submits", 0) or 0)
        current_shares = int(activity_item.get("shares", 0) or 0)
        current_blocks_found = int(activity_item.get("blocks_found", 0) or 0)
        has_recent_shares = current_shares > 0
        has_recent_blocks = current_blocks_found > 0
        pool_seen_age = now_epoch - last_pool_seen_epoch if last_pool_seen_epoch else None
        submit_age = now_epoch - last_submit_epoch if last_submit_epoch else None
        share_age = now_epoch - last_share_epoch if last_share_epoch else None
        expected_worker_seen = str(expected_user).lower() in {str(worker).lower() for worker in workers}
        current_pool_activity = bool(activity_item) and expected_url == defaults["pool_url"] and (
            expected_worker_seen or current_submits > 0 or has_recent_shares or has_recent_blocks
        )
        work_pool_active = bool(
            (managed or configured_record or current_pool_activity)
            and (current_pool_activity or pool_active or has_recent_shares or has_recent_blocks)
        )
        primary_pool_log = configured_record and is_known_primary_pool_log_miner({**registered, "last_workers": workers})
        relevant = managed or configured_record or work_pool_active or has_recent_shares or has_recent_blocks or primary_pool_log
        if not relevant and is_pool_log_only_miner(registered):
            continue
        shares = activity_item.get("shares", registered.get("last_shares_window", 0))
        share_work = activity_item.get("share_work", registered.get("last_share_work_window", 0))
        share_work_int = int(share_work or 0)
        share_difficulty = activity_item.get("share_difficulty", registered.get("last_share_difficulty_window", 0))
        blocks_found = activity_item.get("blocks_found", registered.get("last_blocks_window", 0))
        last_share_at = activity_item.get("last_share_at") or registered.get("last_share_at")
        last_job_at = activity_item.get("last_job_at") or registered.get("last_job_at")
        last_submit_at = activity_item.get("last_submit_at") or registered.get("last_submit_at")
        issue = api_error or debug_error
        last_difficulty = activity_item.get("last_difficulty") or registered.get("last_difficulty")
        last_difficulty_value = safe_decimal(last_difficulty)
        submits = int(activity_item.get("submits", registered.get("last_submits_window", 0)) or 0)
        low_difficulty_flood = bool(
            last_difficulty_value is not None
            and last_difficulty_value > 0
            and last_difficulty_value < MINER_LOW_DIFF_THRESHOLD
            and current_submits >= MINER_LOW_DIFF_MIN_SUBMITS
        )

        status = "inactive"
        if managed:
            if (api_error or debug_error) and not has_recent_shares and not has_recent_blocks and not connected:
                status = "down"
                label = miner_display_label({**registered, "mac": mac})
                failures.append(
                    f"{label} mac={mac or 'unknown-mac'} observed_ip={ip} ASIC API/health check is unreachable or degraded "
                    "and no recent pool submissions were seen"
                    + (f" (last pool seen {pool_seen_age}s ago)" if pool_seen_age is not None else "")
                    + f": {api_error or debug_error}"
                )
            elif configured and (pool_active or has_recent_shares or has_recent_blocks):
                status = "ok"
            elif configured:
                status = "degraded"
                warnings.append(
                    f"{miner_display_label({**registered, 'mac': mac})} mac={mac or 'unknown-mac'} "
                    f"observed_ip={ip} is configured but no recent accepted shares were seen"
                )
            elif has_recent_shares or connected:
                status = "degraded"
                warnings.append(
                    f"{miner_display_label({**registered, 'mac': mac})} mac={mac or 'unknown-mac'} "
                    f"observed_ip={ip} is submitting to the pool but ASIC API configuration could not be confirmed"
                )
            else:
                status = "down"
                failures.append(
                    f"{miner_display_label({**registered, 'mac': mac})} mac={mac or 'unknown-mac'} "
                    f"observed_ip={ip} is not configured for the expected local pool"
                )
        elif configured or has_recent_shares or has_recent_blocks:
            status = "ok"
        elif connected:
            status = "connected"
        elif primary_pool_log:
            status = "down"
            age = now_epoch - last_pool_seen_epoch if last_pool_seen_epoch else None
            label = miner_display_label({**registered, "mac": mac})
            failures.append(
                f"{label} mac={mac or 'unknown-mac'} observed_ip={ip} has not been seen by the pool"
                + (f" for {age}s" if age is not None else "")
            )
        elif device_type == "asic" and discovered_by != "pool-log" and not configured:
            warnings.append(
                f"{miner_display_label({**registered, 'mac': mac})} mac={mac or 'unknown-mac'} "
                f"observed_ip={ip} is discovered but not managed by the local pool"
            )
        if low_difficulty_flood:
            status = "degraded" if status == "ok" else status
            warning = (
                f"{miner_display_label({**registered, 'mac': mac})} mac={mac or 'unknown-mac'} observed_ip={ip} "
                "is submitting very low-difficulty work "
                f"(difficulty {last_difficulty}, submits {submits})"
            )
            if managed:
                failures.append(warning)
            else:
                warnings.append(warning)
            issue = issue or warning

        sources = registered.get("sources") if isinstance(registered.get("sources"), list) else []
        discovered_by_value = discovered_by or (str(sources[0]) if sources else "")
        health.append(
            {
                "ip": ip,
                "mac": mac,
                "device_id": f"mac:{mac}" if mac else str(registered.get("device_id") or ""),
                "identity_key": miner_identity_key({**registered, "mac": mac}),
                "display_name": registered.get("display_name") or "",
                "display_label": miner_display_label({**registered, "mac": mac}),
                "managed": managed,
                "device_type": device_type,
                "discovered_by": discovered_by_value,
                "auto_discovered": bool(registered.get("auto_discovered")),
                "status": status,
                "configured": configured,
                "connected": connected,
                "pool_active": pool_active,
                "work_pool_active": work_pool_active,
                "api_error": api_error,
                "debug_error": debug_error,
                "issue": issue,
                "model": (discovered or registered).get("model", ""),
                "hardware": (discovered or registered).get("hardware", ""),
                "firmware": (discovered or registered).get("firmware", ""),
                "debug": {
                    "available": bool(cgminer_devs),
                    "minerstatus": cgminer_devs.get("minerstatus"),
                    "hashrate": cgminer_devs.get("hashrate"),
                    "av_hashrate": cgminer_devs.get("av_hashrate"),
                    "accepted": cgminer_devs.get("accepted"),
                    "rejected": cgminer_devs.get("rejected"),
                    "hwerrors": cgminer_devs.get("hwerrors"),
                    "hwerr_ratio": cgminer_devs.get("hwerr_ration"),
                    "temperature": cgminer_devs.get("temp"),
                    "fanspeed": cgminer_devs.get("fanspeed"),
                    "valid": cgminer_devs.get("valid"),
                    "uptime_seconds": cgminer_devs.get("time"),
                    "powerplan": cgminer_devs.get("powerplan"),
                },
                "expected_pool_url": expected_url,
                "expected_worker_user": expected_user,
                "workers": workers,
                "ports": ports,
                "jobs": activity_item.get("jobs", registered.get("last_jobs_window", 0)),
                "submits": submits,
                "shares": shares,
                "share_work": share_work_int,
                "work_percent": "0.00",
                "relevant_for_work_share": relevant,
                "low_difficulty_flood": low_difficulty_flood,
                "share_difficulty": share_difficulty,
                "blocks_found": blocks_found,
                "last_difficulty": last_difficulty,
                "last_job_at": last_job_at,
                "last_submit_at": last_submit_at,
                "last_submit_epoch": last_submit_epoch,
                "last_submit_age_seconds": submit_age,
                "last_share_at": last_share_at,
                "last_share_epoch": last_share_epoch,
                "last_share_age_seconds": share_age,
                "last_block_at": activity_item.get("last_block_at") or registered.get("last_block_at"),
                "last_pool_seen_at": activity_item.get("last_seen_at") or registered.get("last_pool_seen_at"),
                "last_pool_seen_epoch": last_pool_seen_epoch,
                "last_pool_seen_age_seconds": pool_seen_age,
            }
        )

    total_work = sum(item["share_work"] for item in health if item.get("relevant_for_work_share") and item.get("share_work", 0) > 0)
    if not total_work:
        total_work = sum(item["share_work"] for item in health if item.get("share_work", 0) > 0)
    expected_lane_rows = [
        item
        for item in health
        if item.get("relevant_for_work_share") and item.get("work_pool_active")
    ]
    expected_lane_count = len(expected_lane_rows)
    expected_lane_percent = Decimal("100") / Decimal(expected_lane_count) if expected_lane_count > 0 else Decimal("0")
    imbalanced_lanes: list[str] = []
    for item in health:
        share_work_int = int(item.get("share_work", 0) or 0)
        if total_work > 0 and share_work_int > 0:
            item["work_percent"] = percent_to_str((Decimal(share_work_int) / Decimal(total_work)) * Decimal("100"))
        item["expected_work_percent"] = percent_to_str(expected_lane_percent) if expected_lane_count > 0 and item.get("relevant_for_work_share") else "0.00"
        item["work_ratio_to_expected"] = None
        item["lane_status"] = "not-tracked"
        if expected_lane_count > 0 and item.get("relevant_for_work_share"):
            if total_work <= 0:
                item["lane_status"] = "no-window-work"
                continue
            actual_percent = safe_decimal(item.get("work_percent")) or Decimal("0")
            ratio = actual_percent / expected_lane_percent if expected_lane_percent > 0 else Decimal("0")
            item["work_ratio_to_expected"] = decimal_to_str(ratio, places=2)
            if share_work_int <= 0 and item.get("connected"):
                item["lane_status"] = "no-work"
            elif ratio < Decimal("0.50"):
                item["lane_status"] = "low"
            elif ratio > Decimal("1.70"):
                item["lane_status"] = "high"
            else:
                item["lane_status"] = "balanced"
            if item["lane_status"] in {"no-work", "low", "high"} and item.get("connected"):
                label = item.get("display_label") or miner_display_label(item)
                mac = item.get("mac") or "unknown-mac"
                imbalanced_lanes.append(
                    f"{label} mac={mac} lane={item['lane_status']} actual={item['work_percent']}% expected={item['expected_work_percent']}%"
                )
    if imbalanced_lanes:
        suffix = f"; +{len(imbalanced_lanes) - 4} more" if len(imbalanced_lanes) > 4 else ""
        warnings.append("miner lane balance advisory by MAC identity: " + "; ".join(imbalanced_lanes[:4]) + suffix)

    counts = miner_health_count_summary(health)
    return {
        "generated_at": now_iso(),
        "registry_updated_at": registry.get("updated_at"),
        **counts,
        "lane_balance": {
            "identity_basis": "mac",
            "expected_lane_count": expected_lane_count,
            "expected_work_percent": percent_to_str(expected_lane_percent) if expected_lane_count > 0 else "0.00",
            "imbalanced_count": len(imbalanced_lanes),
        },
        "failures": failures,
        "warnings": warnings,
        "miners": health,
    }


def repair_managed_miners(reason: str = "watchdog miner repair") -> dict[str, Any]:
    password = read_miner_admin_password()
    health = collect_miner_health()
    if not password:
        return {"status": "skipped", "reason": "no saved miner admin password", "health": health}

    defaults = default_miner_pool_settings()
    targets = [
        item
        for item in health.get("miners", [])
        if item.get("managed") and item.get("status") != "ok" and is_lan_ipv4(str(item.get("ip", "")))
    ]
    results = []
    for target in targets:
        ip = target["ip"]
        try:
            if target.get("low_difficulty_flood"):
                results.append(
                    {
                        **restart_miner(ip, password),
                        "action": "restart",
                        "reason": "low-difficulty submit flood",
                    }
                )
            else:
                results.append(
                    configure_miner(
                        ip=ip,
                        admin_password=password,
                        pool_url=target.get("expected_pool_url") or defaults["pool_url"],
                        worker_user=defaults["worker_user"],
                        pool_password=defaults["pool_password"],
                        replace_existing=True,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - keep repairing other miners.
            results.append({"ip": ip, "status": "failed", "error": str(exc)})

    if results:
        mark_configured_miners(results, defaults["pool_url"], defaults["worker_user"])
    failed = [item for item in results if item.get("status") == "failed"]
    return {
        "status": "failed" if failed else "ok",
        "reason": reason,
        "target_count": len(targets),
        "results": results,
    }


def collect_status(include_logs: bool = True) -> dict[str, Any]:
    ensure_runtime()
    docker_error = docker_access_error()
    display_nodes = list(NODES)
    pool_port = read_env_value("POOL_PORT") or "3334"
    local_ips = local_ipv4_addresses()
    configured_pool_url = default_miner_pool_settings()["pool_url"]
    parsed_pool_url = urllib.parse.urlparse(configured_pool_url)
    if parsed_pool_url.netloc:
        pool_endpoint = parsed_pool_url.netloc
    elif local_ips:
        pool_endpoint = f"{local_ips[0]}:{pool_port}"
    else:
        pool_endpoint = f"127.0.0.1:{pool_port}"
    disk = run(["df", "-h", str(PROJECT_ROOT)], timeout=8).stdout.strip()
    host_pressure = collect_host_pressure()
    host_profile = host_runtime_profile()
    adaptive_concurrency = adaptive_worker_budgets(host_pressure)
    if docker_error:
        containers = {
            service: {
                "name": service,
                "image": "",
                "running": False,
                "status": "unavailable",
                "restart_count": 0,
                "exit_code": None,
                "error": f"docker access unavailable: {docker_error}",
                "ports": {},
            }
            for service in SERVICES
        }
        node_details = {
            node: {
                "role": node_role(node),
                "health_scope": node_health_scope(node),
                "affects_production_health": node_affects_production_health(node),
                "child_running": False,
                "latest_block": None,
                "best_main_order": None,
                "peer_ahead_blocks": None,
                "importing": False,
                "last_import_at": None,
                "last_import_age_seconds": None,
                "import_count": 0,
                "invalid_peer_errors": 0,
                "p2p_stream_errors": 0,
                "p2p_error_lines": [],
                "mining_template_error_count": 0,
                "mining_template_nonce_too_low_count": 0,
                "mining_template_error_lines": [],
                "mining_template_failing": False,
                "critical": False,
                "critical_lines": [],
                "tail": [],
            }
            for node in display_nodes
        }
        empty_pool = {
            "initial_download": False,
            "rpc_refused": False,
            "gbt_errors": 0,
            "submit_count": 0,
            "valid_share_count": 0,
            "stale_submit_count": 0,
            "accepted_job_expired_storm": False,
            "accepted_job_expired_storm_threshold": POOL_ACCEPTED_JOB_EXPIRED_STORM_COUNT,
            "accepted_job_expired_storm_ratio": POOL_ACCEPTED_JOB_EXPIRED_STORM_RATIO,
            "job_notify_count": 0,
            "head_change_count": 0,
            "block_submit_success_count": 0,
            "block_submit_error_count": 0,
            "block_submit_failure_count": 0,
            "block_submit_zero_success_storm": False,
            "submit_stall_event_count": 0,
            "submit_stall_recovery_count": 0,
            "submit_stall_last_event": {},
            "submit_stall_last_recovery": {},
            "submit_stall_last_reason": "",
            "submit_stall_last_action": "",
            "submit_stall_last_event_at": None,
            "submit_stall_last_event_age_seconds": None,
            "submit_stall_last_recovery_at": None,
            "submit_stall_last_recovery_age_seconds": None,
            "submit_stall_recovery_recent": False,
            "submit_stall_accepted_after_recovery": False,
            "submit_stall_self_healed_recently": False,
            "submit_stall_recent_event_lines": [],
            "duplicate_block_count": 0,
            "duplicate_block_storm": False,
            "tip_overdue_count": 0,
            "template_freeze_count": 0,
            "template_freeze_age_seconds": None,
            "pool_template_frozen": False,
            "last_submit_at": None,
            "last_submit_age_seconds": None,
            "last_valid_share_at": None,
            "last_valid_share_age_seconds": None,
            "last_job_notify_at": None,
            "last_job_notify_age_seconds": None,
            "last_head_change_at": None,
            "last_head_change_age_seconds": None,
            "last_block_submit_at": None,
            "last_block_submit_age_seconds": None,
            "share_stall": False,
            "job_stall": False,
            "tail": [],
        }
        empty_miner_health = {
            "generated_at": now_iso(),
            "registry_updated_at": None,
            "managed_count": 0,
            "ok_count": 0,
            "tracked_count": 0,
            "connected_count": 0,
            "stratum_count": 0,
            "failures": [],
            "warnings": [],
            "miners": [],
        }
        sync_progress = {
            **unknown_sync_progress("nodes", f"docker access unavailable: {docker_error}"),
            "nodes": {
                node: unknown_sync_progress(node, f"docker access unavailable: {docker_error}")
                for node in display_nodes
            },
        }
        return {
            "status_version": 2,
            "generated_at": now_iso(),
            "generated_epoch_seconds": seconds_since_epoch(),
            "fresh": True,
            "age_seconds": 0,
            "stale_after_seconds": 30,
            "stale_sources": ["docker"],
            "mode": "unknown",
            "can_mine": False,
            "can_accept_shares": False,
            "can_submit_blocks": False,
            "truth_sources": {
                "chain_block_count": "getBlockCount",
                "chain_main_height": "getMainChainHeight diagnostic",
                "template_height": "diagnostic_only",
                "node_log_height": "diagnostic_only",
            },
            "blocking_failures": [f"docker access unavailable: {docker_error}"],
            "degraded_reasons": [],
            "project_root": str(PROJECT_ROOT),
            "runtime_dir": str(RUNTIME_DIR),
            "pool_env_file": str(POOL_ENV_FILE),
            "stack_services": SERVICES,
            "node_services": display_nodes,
            "managed_node_services": NODES,
            "pool_container": POOL_CONTAINER,
            "pool_containers": POOL_CONTAINERS,
            "pool_db_container": POOL_DB_CONTAINER,
            "overall": "down",
            "status_reason": f"docker access unavailable: {docker_error}",
            "containers": containers,
            "nodes": node_details,
            "sync_progress": sync_progress,
            "sync_health": {
                "block_lag": None,
                "main_order_lag": None,
                "lag_warn_blocks": NODE_LAG_WARN_BLOCKS,
                "import_stale_seconds": NODE_IMPORT_STALE_SECONDS,
                "p2p_error_warn_count": NODE_P2P_ERROR_WARN_COUNT,
                "nodes_with_recent_imports": 0,
                "needs_fast_sync_repair": False,
            },
            "pool": empty_pool,
            "pool_metrics": {
                "generated_at": now_iso(),
                "status": "unavailable",
                "error": f"docker access unavailable: {docker_error}",
                "containers": {},
                "active_connections": None,
                "selected_backend": "",
                "active_node_source": "",
                "block_submit_outcomes": {},
                "block_submit_backend_outcomes": {},
                "blocks": {},
                "blocks_rejected_by_node": {},
                "shares_accepted_total": 0.0,
                "shares_rejected_by_reason": {},
                "share_processing": {},
                "loss_ledger": {},
                "submit_stall_recoveries": {},
                "submit_stall_recoveries_total": 0.0,
                "source_job_health": {},
                "source_backend_health": {},
                "selected_backend_source_health": {},
                "active_node_source_health": {},
                "template_conversion_stall": {},
            },
            "pool_health": {
                **empty_pool,
                "connected_miners": 0,
                "managed_miners": 0,
                "needs_fast_repair": False,
            },
            "failures": [f"docker access unavailable: {docker_error}"],
            "stack_failures": [f"docker access unavailable: {docker_error}"],
            "miner_failures": [],
            "warnings": [],
            "sync_warnings": [],
            "maintenance_warnings": [],
            "miner_health": empty_miner_health,
            "mining_address": read_env_value("MINING_ADDRESS"),
            "pool_port": pool_port,
            "local_ips": local_ips,
            "pool_endpoint": pool_endpoint,
            "host_pressure": host_pressure,
            "host_profile": host_profile,
            "adaptive_concurrency": adaptive_concurrency,
            "disk": disk,
            "docker_images": "",
            "docker_access_error": docker_error,
        }
    display_nodes = list(NODES)
    services_for_status = list(SERVICES)
    inspected = docker_inspect(services_for_status)
    containers: dict[str, dict[str, Any]] = {}
    node_details: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    stack_failures: list[str] = []
    warnings: list[str] = []
    sync_warnings: list[str] = []
    maintenance_warnings: list[str] = []

    def add_sync_warning(message: str) -> None:
        warnings.append(message)
        sync_warnings.append(message)

    def add_maintenance_warning(message: str) -> None:
        warnings.append(message)
        maintenance_warnings.append(message)

    for message in host_pressure_warning_messages(host_pressure):
        add_maintenance_warning(message)

    for service in services_for_status:
        info = inspected.get(service)
        if not info:
            info = {
                "name": service,
                "image": "",
                "running": False,
                "status": "missing",
                "restart_count": 0,
                "exit_code": None,
                "error": "container not found",
                "ports": {},
            }
        containers[service] = info
        if service in SERVICES and not info.get("running"):
            stack_failures.append(f"{service} is not running")

    for node in display_nodes:
        top = docker_top(node) if containers[node].get("running") else ""
        child_running = bdag_child_running_from_top(top)
        managed_node = node in NODES
        if managed_node and containers[node].get("running") and not child_running:
            stack_failures.append(f"{node} wrapper is up but bdag child is not running")

        log = docker_logs(node, lines=220) if include_logs and containers[node].get("running") else ""
        parsed = parse_node_log(log)
        if managed_node and parsed["critical"]:
            stack_failures.append(f"{node} has critical log entries")
        if managed_node and parsed["peer_ahead_blocks"]:
            add_sync_warning(f"{node} is still catching up by {parsed['peer_ahead_blocks']} main-order blocks")
        if managed_node and parsed["last_import_age_seconds"] is not None and parsed["last_import_age_seconds"] > NODE_IMPORT_STALE_SECONDS:
            add_sync_warning(
                f"{node} has not imported a block for {parsed['last_import_age_seconds']}s "
                f"(limit {NODE_IMPORT_STALE_SECONDS}s)"
            )
        if managed_node and parsed["invalid_peer_errors"] >= NODE_P2P_ERROR_WARN_COUNT:
            add_maintenance_warning(
                f"{node} logged {parsed['invalid_peer_errors']} malformed peer errors in the recent log window"
            )
        if managed_node and parsed["p2p_stream_errors"] >= NODE_P2P_ERROR_WARN_COUNT:
            add_maintenance_warning(
                f"{node} logged {parsed['p2p_stream_errors']} P2P stream-reset errors in the recent log window"
            )
        if managed_node and parsed["orphan_block_error_storm"]:
            add_sync_warning(
                f"{node} is logging repeated already-have-block orphan sync errors "
                f"({parsed['orphan_block_errors']} recent errors, no recent imports)"
            )
        if managed_node and parsed["mining_template_failing"]:
            add_sync_warning(
                f"{node} cannot create fresh mining templates "
                f"({parsed['mining_template_error_count']} recent errors)"
            )

        node_details[node] = {
            "role": node_role(node),
            "health_scope": node_health_scope(node),
            "affects_production_health": node_affects_production_health(node),
            "child_running": child_running,
            "latest_block": parsed["latest_block"],
            "best_main_order": parsed["best_main_order"],
            "peer_ahead_blocks": parsed["peer_ahead_blocks"],
            "importing": parsed["importing"],
            "last_import_at": parsed["last_import_at"],
            "last_import_age_seconds": parsed["last_import_age_seconds"],
            "import_count": parsed["import_count"],
            "invalid_peer_errors": parsed["invalid_peer_errors"],
            "p2p_stream_errors": parsed["p2p_stream_errors"],
            "orphan_block_errors": parsed["orphan_block_errors"],
            "orphan_block_error_storm": parsed["orphan_block_error_storm"],
            "orphan_block_error_lines": parsed["orphan_block_error_lines"],
            "p2p_error_lines": parsed["p2p_error_lines"],
            "mining_template_error_count": parsed["mining_template_error_count"],
            "mining_template_hard_error_count": parsed["mining_template_hard_error_count"],
            "mining_template_transient_tx_error_count": parsed["mining_template_transient_tx_error_count"],
            "mining_template_nonce_too_low_count": parsed["mining_template_nonce_too_low_count"],
            "mining_template_error_lines": parsed["mining_template_error_lines"],
            "mining_template_hard_error_lines": parsed["mining_template_hard_error_lines"],
            "mining_template_failing": parsed["mining_template_failing"],
            "critical": parsed["critical"],
            "critical_lines": parsed["critical_lines"],
            "tail": parsed["tail"],
        }

    running_pool_containers = [name for name in POOL_CONTAINERS if containers.get(name, {}).get("running")]
    pool_log = docker_logs_many(running_pool_containers, lines=180) if include_logs and running_pool_containers else ""
    pool = parse_pool_log(pool_log)
    pool_metrics = collect_pool_prometheus_metrics(containers) if include_logs else {
        "generated_at": now_iso(),
        "status": "skipped",
        "error": "logs excluded from status collection",
        "containers": {},
        "active_connections": None,
        "selected_backend": "",
        "active_node_source": "",
        "block_submit_outcomes": {},
        "block_submit_backend_outcomes": {},
        "blocks": {},
        "blocks_rejected_by_node": {},
        "shares_accepted_total": 0.0,
        "shares_rejected_by_reason": {},
        "share_processing": {},
        "loss_ledger": {},
        "submit_stall_recoveries": {},
        "submit_stall_recoveries_total": 0.0,
        "source_job_health": {},
        "source_backend_health": {},
        "selected_backend_source_health": {},
        "active_node_source_health": {},
        "template_conversion_stall": {},
    }
    pool["metrics"] = pool_metrics
    pool["selected_backend"] = pool_metrics.get("selected_backend") or ""
    pool["active_node_source"] = pool_metrics.get("active_node_source") or pool["selected_backend"]
    pool["metrics_active_connections"] = pool_metrics.get("active_connections")
    pool["metrics_submit_stall_recoveries_total"] = pool_metrics.get("submit_stall_recoveries_total")
    source_job_health = (
        pool_metrics.get("source_job_health")
        if isinstance(pool_metrics.get("source_job_health"), dict)
        else {}
    )
    source_backend_health = (
        pool_metrics.get("source_backend_health")
        if isinstance(pool_metrics.get("source_backend_health"), dict)
        else {}
    )
    selected_source_health = (
        pool_metrics.get("selected_backend_source_health")
        if isinstance(pool_metrics.get("selected_backend_source_health"), dict)
        else {}
    )
    pool["source_job_health"] = source_job_health
    pool["source_backend_health"] = source_backend_health
    pool["selected_backend_source_health"] = selected_source_health
    pool["active_node_source_health"] = selected_source_health
    pool["template_conversion_stall"] = (
        pool_metrics.get("template_conversion_stall")
        if isinstance(pool_metrics.get("template_conversion_stall"), dict)
        else {}
    )
    pool_loss_ledger = (
        pool_metrics.get("loss_ledger")
        if isinstance(pool_metrics.get("loss_ledger"), dict)
        else {}
    )
    pool["loss_ledger"] = pool_loss_ledger
    miner_health = collect_miner_health() if include_logs else {"failures": [], "warnings": [], "miners": []}
    connected_miners = int(miner_health.get("connected_count", 0) or 0)
    managed_miners = int(miner_health.get("managed_count", 0) or 0)
    miner_demand_present = connected_miners > 0 or managed_miners > 0
    template_probe_running_nodes = any(containers.get(node, {}).get("running") for node in NODES)
    template_probe_health = (
        collect_template_probe_health()
        if include_logs and miner_demand_present and template_probe_running_nodes
        else {
            "generated_at": now_iso(),
            "cached": False,
            "suppressed_for_no_miners": bool(include_logs and not miner_demand_present),
            "suppressed_reason": "no managed or connected miners" if include_logs and not miner_demand_present else "",
            "nodes": {},
            "failing_nodes": [],
            "all_nodes_failing": False,
        }
    )
    for node in NODES:
        probe = ((template_probe_health.get("nodes") or {}).get(node) or {})
        node_details.setdefault(node, {})
        node_details[node].update(
            {
                "template_probe_sample_count": int(probe.get("sample_count") or 0),
                "template_probe_ok_count": int(probe.get("ok_count") or 0),
                "template_probe_error_count": int(probe.get("error_count") or 0),
                "template_probe_tx_download_throttle_count": int(probe.get("tx_download_throttle_count") or 0),
                "template_probe_nonce_too_low_count": int(probe.get("nonce_too_low_count") or 0),
                "template_probe_transient_tx_template_count": int(probe.get("transient_tx_template_count") or 0),
                "template_probe_benign_tx_throttle": bool(probe.get("benign_tx_throttle")),
                "template_probe_benign_tx_template_error": bool(probe.get("benign_tx_template_error")),
                "template_probe_error_ratio": float(probe.get("error_ratio") or 0.0),
                "template_probe_last_height": probe.get("last_height"),
                "template_probe_last_error": probe.get("last_error") or "",
                "template_probe_failing": bool(probe.get("failing")),
            }
        )
        if probe.get("failing"):
            node_details[node]["mining_template_failing"] = True
            node_details[node]["mining_template_error_count"] = int(
                node_details[node].get("mining_template_error_count") or 0
            ) + int(probe.get("error_count") or 0)
            message = (
                f"{node} is refusing live mining template probes "
                f"({probe.get('error_count')}/{probe.get('sample_count')} failed"
            )
            if probe.get("last_error"):
                message += f": {probe.get('last_error')}"
            message += ")"
            add_sync_warning(message)
        elif int(probe.get("error_count") or 0) > 0 and not (
            probe.get("benign_tx_template_error") or probe.get("benign_tx_throttle")
        ):
            add_maintenance_warning(
                f"{node} had intermittent mining template probe errors "
                f"({probe.get('error_count')}/{probe.get('sample_count')} failed)"
            )

    managed_node_details = {node: node_details.get(node, {}) for node in NODES}
    block_values = [item["latest_block"] for item in managed_node_details.values() if item.get("latest_block") is not None]
    main_order_values = [item["best_main_order"] for item in managed_node_details.values() if item.get("best_main_order") is not None]
    block_lag = max(block_values) - min(block_values) if len(block_values) >= 2 else None
    main_order_lag = max(main_order_values) - min(main_order_values) if len(main_order_values) >= 2 else None
    if block_lag is not None and block_lag > NODE_LAG_WARN_BLOCKS:
        add_sync_warning(f"node block heights differ by {block_lag} blocks (limit {NODE_LAG_WARN_BLOCKS})")
    if main_order_lag is not None and main_order_lag > NODE_LAG_WARN_BLOCKS:
        add_sync_warning(f"node main-order values differ by {main_order_lag} blocks (limit {NODE_LAG_WARN_BLOCKS})")
    sync_health = {
        "block_lag": block_lag,
        "main_order_lag": main_order_lag,
        "lag_warn_blocks": NODE_LAG_WARN_BLOCKS,
        "import_stale_seconds": NODE_IMPORT_STALE_SECONDS,
        "p2p_error_warn_count": NODE_P2P_ERROR_WARN_COUNT,
        "nodes_with_recent_imports": sum(1 for item in managed_node_details.values() if item.get("importing")),
        "needs_fast_sync_repair": False,
    }
    pool_has_recent_mining = any(
        pool.get(field) is not None and int(pool.get(field) or 0) <= max_age
        for field, max_age in (
            ("last_submit_age_seconds", 30),
            ("last_valid_share_age_seconds", 60),
            ("last_block_submit_age_seconds", 60),
        )
    )
    pool_initial_download_transient = bool(
        pool.get("initial_download")
        and pool_has_recent_mining
        and not pool.get("share_stall")
    )
    pool_rpc_refused_hard = bool(
        pool.get("rpc_refused_recent")
        and connected_miners > 0
        and not pool_has_recent_mining
        and not any("bdag child" in item for item in stack_failures)
    )
    if pool_rpc_refused_hard:
        add_sync_warning("pool recently saw RPC connection refused")
    elif pool.get("rpc_refused_recent") and not any("bdag child" in item for item in stack_failures):
        add_maintenance_warning("pool saw a transient RPC connection refused while accepted mining work remains fresh")
    source_job_health_ok_raw = source_job_health.get("ok") if isinstance(source_job_health, dict) else None
    source_job_health_ok = None if source_job_health_ok_raw is None else bool(source_job_health_ok_raw)
    selected_source_checks = []
    if isinstance(selected_source_health, dict):
        for key in ("node_mineable", "node_submit_ready", "node_p2p_mining_fresh"):
            if key in selected_source_health:
                selected_source_checks.append(bool(selected_source_health.get(key)))
        if selected_source_health.get("node_last_template_build_error_blocking") is True:
            selected_source_checks.append(False)
    selected_source_degraded = bool(selected_source_checks and not all(selected_source_checks))
    source_job_hard_degraded = bool(source_job_health_ok is False and not pool_has_recent_mining)
    source_selected_backend_hard_degraded = bool(selected_source_degraded and not pool_has_recent_mining)
    source_health_transient_degraded = bool(
        (source_job_health_ok is False or selected_source_degraded)
        and pool_has_recent_mining
    )
    pool["source_job_health_ok"] = source_job_health_ok
    pool["source_job_hard_degraded"] = source_job_hard_degraded
    pool["source_selected_backend_degraded"] = selected_source_degraded
    pool["source_selected_backend_hard_degraded"] = source_selected_backend_hard_degraded
    pool["source_health_transient_degraded"] = source_health_transient_degraded
    pool["source_selected_backend_submit_ready"] = (
        selected_source_health.get("node_submit_ready")
        if isinstance(selected_source_health, dict)
        else None
    )
    pool["source_selected_backend_mineable"] = (
        selected_source_health.get("node_mineable")
        if isinstance(selected_source_health, dict)
        else None
    )
    pool["source_selected_backend_p2p_fresh"] = (
        selected_source_health.get("node_p2p_mining_fresh")
        if isinstance(selected_source_health, dict)
        else None
    )
    pool_initial_download_needs_repair = bool(pool.get("initial_download") and not pool_initial_download_transient)
    pool["initial_download_transient"] = pool_initial_download_transient
    pool["initial_download_needs_repair"] = pool_initial_download_needs_repair
    sync_health["pool_initial_download_transient"] = pool_initial_download_transient
    sync_health["pool_has_recent_mining"] = pool_has_recent_mining
    readiness_contract = selected_backend_readiness_contract(
        str(pool.get("selected_backend") or ""),
        selected_source_health,
        source_job_health,
        pool_has_recent_mining,
    )
    pool["selected_backend_readiness_contract"] = readiness_contract
    if pool_initial_download_needs_repair:
        add_sync_warning("pool is waiting for node sync to finish")
    elif pool_initial_download_transient:
        add_maintenance_warning(
            "pool saw a transient initial-download template response while active mining stayed fresh"
        )
    if connected_miners > 0 and pool_loss_ledger.get("warnings"):
        ledger_warnings = [str(item) for item in pool_loss_ledger.get("warnings", []) if item]
        if ledger_warnings:
            add_maintenance_warning("pool efficiency loss ledger: " + "; ".join(ledger_warnings[:3]))
    if connected_miners > 0 and readiness_contract.get("contradiction"):
        backend = readiness_contract.get("selected_backend") or "active node source"
        checks = readiness_contract.get("checks") if isinstance(readiness_contract.get("checks"), dict) else {}
        add_maintenance_warning(
            f"active node source readiness contradiction: {backend} reports "
            f"mineable={checks.get('node_mineable')} submit_ready={checks.get('node_submit_ready')} "
            "while accepted mining work remains recent"
        )
    if connected_miners > 0 and source_job_hard_degraded:
        add_sync_warning("pool source job health reports not-ok and accepted work is stale")
    elif connected_miners > 0 and source_job_health_ok is False:
        add_maintenance_warning("pool source job health is advisory-degraded while accepted work remains fresh")
    if connected_miners > 0 and source_selected_backend_hard_degraded:
        backend = pool.get("active_node_source") or "active node source"
        add_sync_warning(f"pool source health says {backend} is not mineable/submit-ready and accepted work is stale")
    elif connected_miners > 0 and source_health_transient_degraded:
        backend = pool.get("active_node_source") or "active node source"
        add_maintenance_warning(f"pool source health says {backend} is degraded, but accepted work remains fresh")
    if connected_miners > 0 and pool.get("share_stall"):
        age = pool.get("last_valid_share_age_seconds")
        age_text = f"{age}s" if age is not None else "unknown"
        add_sync_warning(
            f"pool has not accepted a valid share for {age_text} "
            f"while {connected_miners} miner(s) are connected"
        )
    effective_job_stall = bool(connected_miners > 0 and pool.get("job_stall") and not pool_has_recent_mining)
    if effective_job_stall:
        age = pool.get("last_job_notify_age_seconds")
        age_text = f"{age}s" if age is not None else "unknown"
        add_sync_warning(
            f"pool has not pushed a fresh job for {age_text} "
            f"while miners are connected"
        )
    if connected_miners > 0 and pool.get("accepted_job_expired_storm"):
        add_sync_warning(
            "pool is rejecting an accepted-job expired storm instead of accepting fresh shares "
            f"({pool.get('stale_submit_count')} expired job submits vs "
            f"{pool.get('valid_share_count')} valid shares)"
        )
    elif connected_miners > 0 and pool.get("stale_submit_count", 0) > max(10, pool.get("valid_share_count", 0) * 2):
        add_sync_warning("pool is mostly rejecting stale jobs instead of accepting fresh shares")
    if connected_miners > 0 and pool.get("pool_template_frozen"):
        add_sync_warning(
            f"pool mining template is frozen for {pool.get('template_freeze_age_seconds')}s"
        )
    if connected_miners > 0 and pool.get("duplicate_block_storm"):
        add_sync_warning(
            f"pool is receiving mostly duplicate block submissions "
            f"({pool.get('duplicate_block_count')} recent duplicate submits)"
        )
    if connected_miners > 0 and pool.get("stale_job_candidate_storm"):
        add_sync_warning(
            f"pool is finding too many block candidates on stale jobs "
            f"({pool.get('stale_job_candidate_count')} recent stale-job candidates)"
        )
    if connected_miners > 0 and pool.get("block_submit_error_storm"):
        add_sync_warning(
            f"pool has a high block submit error rate "
            f"({pool.get('block_submit_error_count')} errors vs "
            f"{pool.get('block_submit_success_count')} successes)"
        )
    if connected_miners > 0 and pool.get("submit_stall_self_healed_recently"):
        recovery = pool.get("submit_stall_last_recovery") if isinstance(pool.get("submit_stall_last_recovery"), dict) else {}
        backend_to = str(recovery.get("backend_to") or pool.get("active_node_source") or "active node source")
        accepted_age = pool.get("last_block_submit_age_seconds")
        accepted_text = f"{accepted_age}s ago" if accepted_age is not None else "after recovery"
        add_maintenance_warning(
            f"pool self-healed its submit path via {backend_to}; accepted block submit resumed {accepted_text}"
        )
    elif connected_miners > 0 and pool.get("submit_stall_recovery_recent"):
        recovery_age = pool.get("submit_stall_last_recovery_age_seconds")
        age_text = f"{recovery_age}s ago" if recovery_age is not None else "recently"
        add_maintenance_warning(
            f"pool submit-path in-process recovery ran {age_text}; waiting for accepted submit confirmation"
        )
    if connected_miners > 0 and pool.get("block_submit_zero_success_storm"):
        add_sync_warning(
            "pool is finding block candidates but none are being accepted "
            f"({pool.get('block_submit_failure_count')} recent submit-path failures, "
            f"{pool.get('duplicate_block_count')} duplicates, "
            f"{pool.get('block_submit_error_count')} submit errors)"
        )

    failures = stack_failures + miner_health.get("failures", [])
    miner_warnings = miner_health.get("warnings", [])
    warnings.extend(miner_warnings)
    maintenance_warnings.extend(miner_warnings)
    pool_health = {
        **pool,
        "rpc_refused_raw": bool(pool.get("rpc_refused")),
        "rpc_refused": pool_rpc_refused_hard,
        "connected_miners": connected_miners,
        "managed_miners": managed_miners,
        "rpc_template_failing": False,
        "node_template_probe_failing": bool(template_probe_health.get("failing_nodes")),
        "share_stall": bool(pool.get("share_stall") and connected_miners > 0),
        "job_stall": effective_job_stall,
        "needs_fast_repair": bool(
            pool_initial_download_needs_repair
            or pool_rpc_refused_hard
            or (template_probe_health.get("failing_nodes") and connected_miners > 0)
            or (pool.get("pool_template_frozen") and connected_miners > 0)
            or (pool.get("duplicate_block_storm") and connected_miners > 0)
            or (pool.get("stale_job_candidate_storm") and connected_miners > 0)
            or (pool.get("block_submit_error_storm") and connected_miners > 0)
            or (pool.get("accepted_job_expired_storm") and connected_miners > 0)
            or (source_job_hard_degraded and connected_miners > 0)
            or (source_selected_backend_hard_degraded and connected_miners > 0)
            or (
                pool.get("block_submit_zero_success_storm")
                and connected_miners > 0
                and not pool.get("submit_stall_recovery_recent")
            )
            or (pool.get("share_stall") and connected_miners > 0)
            or effective_job_stall
        ),
    }
    sync_health["needs_fast_sync_repair"] = bool(sync_warnings and not failures) or pool_health["needs_fast_repair"]

    pool_port = read_env_value("POOL_PORT") or "3334"
    local_ips = local_ipv4_addresses()
    configured_pool_url = default_miner_pool_settings()["pool_url"]
    parsed_pool_url = urllib.parse.urlparse(configured_pool_url)
    if parsed_pool_url.netloc:
        pool_endpoint = parsed_pool_url.netloc
    elif local_ips:
        pool_endpoint = f"{local_ips[0]}:{pool_port}"
    else:
        pool_endpoint = f"127.0.0.1:{pool_port}"

    disk = run(["df", "-h", str(PROJECT_ROOT)], timeout=8).stdout.strip()
    docker_images_timeout_raw = os.environ.get("BDAG_STATUS_DOCKER_IMAGES_TIMEOUT", "2")
    try:
        docker_images_timeout = max(1, int(docker_images_timeout_raw))
    except ValueError:
        docker_images_timeout = 2
    docker_images = run(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}} {{.Size}}"],
        timeout=docker_images_timeout,
    ).stdout.strip()
    sync_progress = collect_sync_progress()
    adaptive_concurrency = adaptive_worker_budgets(
        {**host_pressure, "chain_rpc_latency_ms": _sync_chain_rpc_latency_ms(sync_progress)}
    )
    for node, progress in (sync_progress.get("nodes") or {}).items():
        if not isinstance(progress, dict):
            continue
        node_details.setdefault(node, {})
        node_details[node]["chain_block_count"] = progress.get("chain_block_count")
        node_details[node]["chain_main_height"] = progress.get("chain_main_height")
        node_details[node]["chain_rpc_source"] = progress.get("chain_rpc_source")
        node_details[node]["chain_rpc_url"] = progress.get("chain_rpc_url")
        node_details[node]["chain_rpc_latency_ms"] = progress.get("chain_rpc_latency_ms")
        node_details[node]["chain_rpc_attempts"] = progress.get("chain_rpc_attempts")
        node_details[node]["chain_rpc_timeout_seconds"] = progress.get("chain_rpc_timeout_seconds")
        node_details[node]["chain_rpc_retry_limit"] = progress.get("chain_rpc_retry_limit")
        node_details[node]["chain_rpc_error"] = progress.get("chain_rpc_error") or progress.get("error") or ""
        for key, value in progress.items():
            if key.startswith("template_health"):
                node_details[node][key] = value
    no_miner_node_only = bool(
        connected_miners == 0
        and managed_miners == 0
        and any(containers.get(node, {}).get("running") for node in NODES)
    )
    no_miner_sync_only = bool(no_miner_node_only and sync_progress.get("status") == "syncing")
    if no_miner_node_only:
        warnings = [
            item for item in warnings
            if not is_no_miner_sync_noise(item)
        ]
        sync_warnings = [
            item for item in sync_warnings
            if not is_no_miner_sync_noise(item)
        ]
        pool["initial_download_needs_repair"] = False
        pool_health["rpc_template_failing"] = False
        pool_health["node_template_probe_failing"] = False
        pool_health["initial_download_needs_repair"] = False
        pool_health["needs_fast_repair"] = False
        sync_health["needs_fast_sync_repair"] = False
        if isinstance(template_probe_health, dict):
            template_probe_health["suppressed_for_no_miners"] = True
            template_probe_health["failing_nodes"] = []
            template_probe_health["all_nodes_failing"] = False
        for node in NODES:
            node_details.setdefault(node, {})["template_probe_failing"] = False
        if no_miner_sync_only:
            add_sync_warning("no miners present; node is syncing and mining work remains idle")
        else:
            add_maintenance_warning("no miners present; pool services stay up but no mining work is sent")
    sync_progress_health = observe_sync_progress_health(sync_progress)
    active_sync_progress_nodes = sync_progress_health.get("active_nodes") or []
    if active_sync_progress_nodes:
        sync_health["nodes_with_recent_imports"] = max(
            int(sync_health.get("nodes_with_recent_imports") or 0),
            int(sync_progress_health.get("active_node_count") or 0),
        )
        hard_pool_needs_repair = bool(
            pool_health.get("rpc_refused")
            or pool_health.get("rpc_template_failing")
            or pool_health.get("node_template_probe_failing")
            or pool_health.get("share_stall")
            or pool_health.get("job_stall")
            or pool_health.get("pool_template_frozen")
            or pool_health.get("duplicate_block_storm")
            or pool_health.get("stale_job_candidate_storm")
            or pool_health.get("block_submit_error_storm")
            or pool_health.get("accepted_job_expired_storm")
            or pool_health.get("source_job_hard_degraded")
            or pool_health.get("source_selected_backend_hard_degraded")
            or (
                pool_health.get("block_submit_zero_success_storm")
                and not pool_health.get("submit_stall_recovery_recent")
            )
        )
        if sync_progress.get("status") == "syncing" and pool_health.get("initial_download"):
            pool_health["initial_download_needs_repair"] = False
            pool_health["needs_fast_repair"] = hard_pool_needs_repair
        if sync_progress.get("status") == "syncing" and not failures and not pool_health["needs_fast_repair"]:
            sync_health["needs_fast_sync_repair"] = False
    sync_health["sync_progress_health"] = sync_progress_health

    overall = "ok"
    if failures:
        overall = "down"
    elif sync_warnings:
        overall = "syncing"
    status_reason = ""
    if overall != "ok":
        reason_items = failures or sync_warnings or warnings
        status_reason = "; ".join(str(item) for item in reason_items[:3])
        if len(reason_items) > 3:
            status_reason += f"; +{len(reason_items) - 3} more"

    mode = "mining" if connected_miners > 0 else ("sync_only_no_miners" if no_miner_sync_only else "ready_no_miners")
    can_accept_shares = bool(connected_miners > 0 and containers.get(POOL_CONTAINER, {}).get("running") and not failures)
    can_submit_blocks = bool(can_accept_shares and not pool_health.get("needs_fast_repair") and not sync_warnings)
    can_mine = bool(can_accept_shares and can_submit_blocks)
    truth_sources = {
        "chain_block_count": "getBlockCount",
        "chain_main_height": "getMainChainHeight diagnostic",
        "template_height": "diagnostic_only",
        "node_log_height": "diagnostic_only",
    }

    return {
        "status_version": 2,
        "generated_at": now_iso(),
        "generated_epoch_seconds": seconds_since_epoch(),
        "fresh": True,
        "age_seconds": 0,
        "stale_after_seconds": 30,
        "stale_sources": [],
        "mode": mode,
        "can_mine": can_mine,
        "can_accept_shares": can_accept_shares,
        "can_submit_blocks": can_submit_blocks,
        "truth_sources": truth_sources,
        "blocking_failures": failures,
        "degraded_reasons": sync_warnings + maintenance_warnings,
        "project_root": str(PROJECT_ROOT),
        "runtime_dir": str(RUNTIME_DIR),
        "pool_env_file": str(POOL_ENV_FILE),
        "stack_services": SERVICES,
        "node_services": display_nodes,
        "managed_node_services": NODES,
        "pool_container": POOL_CONTAINER,
        "pool_containers": POOL_CONTAINERS,
        "pool_db_container": POOL_DB_CONTAINER,
        "overall": overall,
        "status_reason": status_reason,
        "containers": containers,
        "nodes": node_details,
        "sync_progress": sync_progress,
        "sync_health": sync_health,
        "rpc_template_health": template_probe_health,
        "pool": pool,
        "pool_metrics": pool_metrics,
        "pool_health": pool_health,
        "failures": failures,
        "stack_failures": stack_failures,
        "miner_failures": miner_health.get("failures", []),
        "warnings": warnings,
        "sync_warnings": sync_warnings,
        "maintenance_warnings": maintenance_warnings,
        "miner_health": miner_health,
        "mining_address": read_env_value("MINING_ADDRESS"),
        "pool_port": pool_port,
        "local_ips": local_ips,
        "pool_endpoint": pool_endpoint,
        "host_pressure": host_pressure,
        "host_profile": host_profile,
        "adaptive_concurrency": adaptive_concurrency,
        "disk": disk,
        "docker_images": docker_images,
    }


def collect_status_cached(include_logs: bool = True, max_age_seconds: float | None = None) -> dict[str, Any]:
    max_age = SHARED_STATUS_CACHE_SECONDS if max_age_seconds is None else max(0.0, float(max_age_seconds))
    if not SHARED_STATUS_CACHE_ENABLED or max_age <= 0:
        payload = collect_status(include_logs=include_logs)
        payload["shared_status_cache"] = {"enabled": False, "hit": False, "max_age_seconds": max_age}
        return payload

    sampler_payload = read_status_sampler_payload(include_logs=include_logs, max_age_seconds=max_age_seconds)
    if sampler_payload is not None:
        return sampler_payload

    now = time.time()
    cache = read_json_file(SHARED_STATUS_CACHE_FILE, {})
    if not isinstance(cache, dict):
        cache = {}
    key = "with_logs" if include_logs else "no_logs"
    row = cache.get(key) if isinstance(cache.get(key), dict) else {}
    cached_at = safe_float(row.get("epoch"), 0.0) if isinstance(row, dict) else 0.0
    payload = row.get("payload") if isinstance(row, dict) else None
    if isinstance(payload, dict) and cached_at is not None and now - cached_at <= max_age:
        cache_age = round(max(0.0, now - cached_at), 3)
        result = dict(payload)
        result["age_seconds"] = round((safe_float(result.get("age_seconds"), 0.0) or 0.0) + cache_age, 3)
        stale_after = safe_float(result.get("stale_after_seconds"))
        if stale_after is not None:
            result["fresh"] = bool(result["age_seconds"] <= stale_after)
        result["shared_status_cache"] = {
            "enabled": True,
            "hit": True,
            "key": key,
            "age_seconds": cache_age,
            "max_age_seconds": max_age,
        }
        return result

    result = collect_status(include_logs=include_logs)
    result["shared_status_cache"] = {
        "enabled": True,
        "hit": False,
        "key": key,
        "age_seconds": 0.0,
        "max_age_seconds": max_age,
    }
    result["status_sampler"] = {
        "enabled": STATUS_SAMPLER_ENABLED,
        "hit": False,
        "file": str(STATUS_SAMPLER_FILE),
        "max_age_seconds": STATUS_SAMPLER_MAX_AGE_SECONDS,
    }
    cache[key] = {
        "epoch": now,
        "generated_at": now_iso(),
        "include_logs": include_logs,
        "payload": result,
    }
    write_json_file(SHARED_STATUS_CACHE_FILE, cache, mode=0o600)
    return result


def _sync_remaining_blocks(sync_progress: dict[str, Any]) -> int:
    remaining = safe_int(sync_progress.get("remaining_blocks"), -1)
    if remaining >= 0:
        return remaining
    node_remaining = [
        safe_int(item.get("remaining_blocks"), -1)
        for item in (sync_progress.get("nodes") or {}).values()
        if isinstance(item, dict)
    ]
    return max([value for value in node_remaining if value >= 0] or [-1])


def _sync_chain_rpc_latency_ms(sync_progress: dict[str, Any]) -> float | None:
    values = [
        safe_float(item.get("chain_rpc_latency_ms"))
        for item in (sync_progress.get("nodes") or {}).values()
        if isinstance(item, dict)
    ]
    values = [value for value in values if value is not None]
    return max(values) if values else None


def background_maintenance_decision(task: str, status: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return whether optional background work should run on this tick."""
    if not BACKGROUND_MAINTENANCE_BACKOFF_ENABLED:
        return {
            "allowed": True,
            "task": task,
            "reasons": [],
            "backoff_enabled": False,
            "host_profile": host_runtime_profile(),
        }

    payload = status if isinstance(status, dict) else collect_status_cached(include_logs=False)
    sync_progress = payload.get("sync_progress") if isinstance(payload.get("sync_progress"), dict) else {}
    host_pressure = payload.get("host_pressure") if isinstance(payload.get("host_pressure"), dict) else {}
    reasons: list[str] = []
    sync_status = str(sync_progress.get("status") or "unknown")
    remaining_blocks = _sync_remaining_blocks(sync_progress)
    chain_rpc_latency_ms = _sync_chain_rpc_latency_ms(sync_progress)
    if sync_status == "syncing" and (
        remaining_blocks < 0 or remaining_blocks > BACKGROUND_MAINTENANCE_SYNC_BACKOFF_BLOCKS
    ):
        remaining_text = "unknown" if remaining_blocks < 0 else str(remaining_blocks)
        reasons.append(
            "chain catch-up has priority "
            f"status={sync_status} remaining={remaining_text} "
            f"threshold={BACKGROUND_MAINTENANCE_SYNC_BACKOFF_BLOCKS}"
        )

    iowait = safe_float(host_pressure.get("iowait_percent"))
    if bool(host_pressure.get("iowait_warning_active")):
        reasons.append("host IO wait warning is active")
    elif iowait is not None and iowait >= BACKGROUND_MAINTENANCE_IOWAIT_WARN_PERCENT:
        reasons.append(
            f"host iowait {iowait:.2f}% >= {BACKGROUND_MAINTENANCE_IOWAIT_WARN_PERCENT:.2f}%"
        )

    io_some = safe_float(host_pressure.get("io_some_avg10"))
    if io_some is not None and io_some >= BACKGROUND_MAINTENANCE_IO_SOME_AVG10_WARN:
        reasons.append(
            f"host io pressure avg10 {io_some:.2f} >= {BACKGROUND_MAINTENANCE_IO_SOME_AVG10_WARN:.2f}"
        )

    cpu_some = safe_float(host_pressure.get("cpu_some_avg10"))
    if cpu_some is not None and cpu_some >= BACKGROUND_MAINTENANCE_CPU_SOME_AVG10_WARN:
        reasons.append(
            f"host cpu pressure avg10 {cpu_some:.2f} >= {BACKGROUND_MAINTENANCE_CPU_SOME_AVG10_WARN:.2f}"
        )
    if chain_rpc_latency_ms is not None and chain_rpc_latency_ms >= BACKGROUND_MAINTENANCE_CHAIN_RPC_WARN_MS:
        reasons.append(
            f"chain RPC latency {chain_rpc_latency_ms:.1f}ms >= {BACKGROUND_MAINTENANCE_CHAIN_RPC_WARN_MS:.1f}ms"
        )

    return {
        "allowed": not reasons,
        "task": task,
        "reasons": reasons,
        "backoff_enabled": True,
        "sync_status": sync_status,
        "remaining_blocks": remaining_blocks,
        "sync_backoff_blocks": BACKGROUND_MAINTENANCE_SYNC_BACKOFF_BLOCKS,
        "iowait_percent": iowait,
        "iowait_warn_percent": BACKGROUND_MAINTENANCE_IOWAIT_WARN_PERCENT,
        "io_some_avg10": io_some,
        "io_some_avg10_warn": BACKGROUND_MAINTENANCE_IO_SOME_AVG10_WARN,
        "cpu_some_avg10": cpu_some,
        "cpu_some_avg10_warn": BACKGROUND_MAINTENANCE_CPU_SOME_AVG10_WARN,
        "chain_rpc_latency_ms": chain_rpc_latency_ms,
        "chain_rpc_latency_warn_ms": BACKGROUND_MAINTENANCE_CHAIN_RPC_WARN_MS,
        "host_profile": host_runtime_profile(),
        "adaptive_concurrency": adaptive_worker_budgets({**host_pressure, "chain_rpc_latency_ms": chain_rpc_latency_ms}),
        "shared_status_cache": payload.get("shared_status_cache"),
    }


def pool_db_json(sql: str) -> Any:
    db_container = docker_container_name(POOL_DB_CONTAINER)
    result = run(
        ["docker", "exec", db_container, "psql", "-U", POOL_DB_USER, "-d", POOL_DB_NAME, "-t", "-A", "-c", sql],
        timeout=20,
    )
    if not result.ok:
        raise RuntimeError((result.stderr or result.stdout or f"{db_container} query failed").strip())
    text = result.stdout.strip()
    return json.loads(text) if text else None


def collect_credit_totals() -> dict[str, Any]:
    sql = """
    SELECT json_build_object(
      'generated_at', now()::text,
      'totals', json_build_object(
        'credit_count', count(*),
        'total_wei', COALESCE(sum(amount), 0)::text,
        'paid_wei', COALESCE(sum(amount) FILTER (WHERE is_paid), 0)::text,
        'pending_wei', COALESCE(sum(amount) FILTER (WHERE NOT is_paid), 0)::text,
        'first_credit_at', min(created_at)::text,
        'last_credit_at', max(created_at)::text
      ),
      'recent_1h', (
        SELECT json_build_object(
          'credit_count', count(*),
          'total_wei', COALESCE(sum(amount), 0)::text
        )
        FROM credits
        WHERE created_at >= now() - interval '1 hour'
      ),
      'recent_24h', (
        SELECT json_build_object(
          'credit_count', count(*),
          'total_wei', COALESCE(sum(amount), 0)::text,
          'paid_wei', COALESCE(sum(amount) FILTER (WHERE is_paid), 0)::text,
          'pending_wei', COALESCE(sum(amount) FILTER (WHERE NOT is_paid), 0)::text,
          'first_credit_at', min(created_at)::text,
          'last_credit_at', max(created_at)::text
        )
        FROM credits
        WHERE created_at >= now() - interval '24 hours'
      ),
      'recent_24h_by_address', COALESCE((
        SELECT json_agg(row_to_json(t))
        FROM (
          SELECT miner_address,
                 count(*) AS credit_count,
                 COALESCE(sum(amount), 0)::text AS total_wei,
                 COALESCE(sum(amount) FILTER (WHERE is_paid), 0)::text AS paid_wei,
                 COALESCE(sum(amount) FILTER (WHERE NOT is_paid), 0)::text AS pending_wei,
                 min(created_at)::text AS first_credit_at,
                 max(created_at)::text AS last_credit_at
          FROM credits
          WHERE created_at >= now() - interval '24 hours'
          GROUP BY miner_address
          ORDER BY sum(amount) DESC
        ) t
      ), '[]'::json),
      'by_address', COALESCE((
        SELECT json_agg(row_to_json(t))
        FROM (
          SELECT miner_address,
                 count(*) AS credit_count,
                 COALESCE(sum(amount), 0)::text AS total_wei,
                 COALESCE(sum(amount) FILTER (WHERE is_paid), 0)::text AS paid_wei,
                 COALESCE(sum(amount) FILTER (WHERE NOT is_paid), 0)::text AS pending_wei,
                 min(created_at)::text AS first_credit_at,
                 max(created_at)::text AS last_credit_at
          FROM credits
          GROUP BY miner_address
          ORDER BY sum(amount) DESC
        ) t
      ), '[]'::json),
      'blocks', (
        SELECT json_build_object(
          'count', count(*),
          'total_reward_wei', COALESCE(sum(reward), 0)::text,
          'pending', count(*) FILTER (WHERE status = 'PENDING'),
          'mature', count(*) FILTER (WHERE status = 'MATURE'),
          'paid', count(*) FILTER (WHERE status = 'PAID'),
          'orphaned', count(*) FILTER (WHERE status = 'ORPHANED')
        )
        FROM blocks
      )
    )
    FROM credits;
    """
    payload = pool_db_json(sql)
    if not isinstance(payload, dict):
        return {"error": "unexpected pool-db response"}

    for key in ("total_wei", "paid_wei", "pending_wei"):
        payload["totals"][key.replace("_wei", "_bdag")] = decimal_to_str(wei_to_bdag(payload["totals"].get(key)))
    payload["recent_1h"]["total_bdag"] = decimal_to_str(wei_to_bdag(payload["recent_1h"].get("total_wei")))
    for key in ("total_wei", "paid_wei", "pending_wei"):
        payload["recent_24h"][key.replace("_wei", "_bdag")] = decimal_to_str(wei_to_bdag(payload["recent_24h"].get(key)))
    wallet_recent_24h_wei = Decimal("0")
    for item in payload.get("recent_24h_by_address", []):
        for key in ("total_wei", "paid_wei", "pending_wei"):
            item[key.replace("_wei", "_bdag")] = decimal_to_str(wei_to_bdag(item.get(key)))
        if is_spendable_eth_address(item.get("miner_address")):
            wallet_recent_24h_wei += Decimal(str(item.get("total_wei") or "0"))
    payload["recent_24h"]["wallet_total_wei"] = str(wallet_recent_24h_wei.quantize(Decimal("1")))
    payload["recent_24h"]["wallet_total_bdag"] = decimal_to_str(wei_to_bdag(wallet_recent_24h_wei))
    for item in payload.get("by_address", []):
        for key in ("total_wei", "paid_wei", "pending_wei"):
            item[key.replace("_wei", "_bdag")] = decimal_to_str(wei_to_bdag(item.get(key)))
    payload["blocks"]["total_reward_bdag"] = decimal_to_str(wei_to_bdag(payload["blocks"].get("total_reward_wei")))
    return payload


def json_rpc_balance(url: str, address: str, timeout: float = 6.0) -> dict[str, Any]:
    result = json_rpc_call(url, "eth_getBalance", [address, "latest"], timeout=timeout)
    if result is None:
        raise RuntimeError("JSON-RPC response did not include result")
    value = int(str(result), 16)
    return {"wei": str(value), "bdag": decimal_to_str(wei_to_bdag(value))}


def json_rpc_balance_at(url: str, address: str, block_number: int | str, timeout: float = 8.0) -> dict[str, Any]:
    block_tag = hex(block_number) if isinstance(block_number, int) else str(block_number)
    result = json_rpc_call(url, "eth_getBalance", [address, block_tag], timeout=timeout)
    if result is None:
        raise RuntimeError("JSON-RPC response did not include result")
    value = int(str(result), 16)
    return {"wei": str(value), "bdag": decimal_to_str(wei_to_bdag(value))}


def named_urls_from_env(name: str, defaults: list[tuple[str, str]]) -> list[tuple[str, str]]:
    if name not in os.environ:
        return defaults
    urls: list[tuple[str, str]] = []
    for index, item in enumerate(split_env_list(name, ""), start=1):
        if "=" in item:
            source, url = item.split("=", 1)
            urls.append((source.strip() or f"{name.lower()}-{index}", url.strip()))
        else:
            urls.append((f"{name.lower()}-{index}", item))
    return urls


def valid_url(value: str) -> bool:
    if not value or any(ord(ch) < 32 or ch.isspace() for ch in value):
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    try:
        return bool(parsed.hostname)
    except ValueError:
        return False


def valid_ipv4(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).version == 4
    except ValueError:
        return False


def node_rpc_urls() -> list[tuple[str, str]]:
    configured = named_urls_from_env("BDAG_NODE_RPC_URLS", [])
    if configured:
        valid_configured = [
            (source.strip() or "configured-node", url.strip())
            for source, url in configured
            if valid_url(url.strip())
        ]
        if valid_configured:
            return valid_configured

    return mining_rpc_urls()


def docker_container_name(name: str) -> str:
    info = docker_inspect([name]).get(name) or {}
    return str(info.get("name") or name)


def docker_container_ip(name: str) -> str:
    info = docker_inspect([name]).get(name) or {}
    for ip in info.get("network_ips") or []:
        if valid_ipv4(str(ip)):
            return str(ip)
    return ""


def _host_url_for_dashboard(url: str) -> str:
    """Translate compose-only service hostnames to container IPs for host-side collectors."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    hostname = parsed.hostname or ""
    if hostname not in SERVICES and hostname not in NODES and hostname not in POOL_CONTAINERS:
        return url
    ip = docker_container_ip(hostname)
    if not ip:
        return url
    port = parsed.port or NODE_EVM_RPC_PORT
    netloc = f"{ip}:{port}"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path or "", parsed.query, parsed.fragment))


def global_evm_rpc_urls() -> list[tuple[str, str]]:
    configured = (
        named_urls_from_env("BDAG_GLOBAL_RPC_URLS", [])
        or named_urls_from_env("BDAG_EVM_RPC_URLS", [])
        or named_urls_from_env("WALLET_RPC_URLS", [])
    )
    urls: list[tuple[str, str]] = []
    for source, url in configured:
        normalized = _host_url_for_dashboard(url.strip())
        if valid_url(normalized):
            urls.append((source.strip() or "configured-evm", normalized))
    if urls:
        return urls

    name = primary_node_service()
    ip = docker_container_ip(name)
    if ip:
        urls.append((name, f"http://{ip}:{NODE_EVM_RPC_PORT}"))
    return urls


def local_evm_balance_disabled_reason() -> str:
    return "local EVM balance probes are disabled"


def local_evm_balance_rpc_urls() -> list[tuple[str, str]]:
    if not LOCAL_EVM_BALANCE_PROBE_ENABLED:
        return []
    return global_evm_rpc_urls()


def unknown_sync_progress(source: str = "node", error: str = "") -> dict[str, Any]:
    return {
        "status": "unknown",
        "percent": None,
        "current_block": None,
        "highest_block": None,
        "starting_block": None,
        "remaining_blocks": None,
        "source": source,
        "error": error,
    }


def parse_rpc_quantity(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if value.startswith(("0x", "0X")):
            return int(value, 16)
        return int(value)
    raise ValueError(f"invalid RPC quantity: {value!r}")


def node_chain_rpc_snapshot(source: str, url: str, timeout: float = NODE_CHAIN_RPC_TIMEOUT) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "chain_rpc_source": "getBlockCount",
        "chain_rpc_url": url,
        "chain_rpc_error": "",
        "chain_block_count": None,
        "chain_main_height": None,
        "chain_rpc_latency_ms": None,
        "chain_rpc_attempts": 0,
        "chain_rpc_timeout_seconds": timeout,
        "chain_rpc_retry_limit": NODE_CHAIN_RPC_RETRIES,
    }
    errors: list[str] = []
    for attempt in range(NODE_CHAIN_RPC_RETRIES):
        start = time.monotonic()
        try:
            snapshot["chain_block_count"] = parse_rpc_quantity(
                mining_rpc_call(url, "getBlockCount", [], timeout=timeout)
            )
            snapshot["chain_rpc_latency_ms"] = round((time.monotonic() - start) * 1000, 1)
            snapshot["chain_rpc_attempts"] = attempt + 1
            break
        except Exception as exc:
            snapshot["chain_rpc_latency_ms"] = round((time.monotonic() - start) * 1000, 1)
            snapshot["chain_rpc_attempts"] = attempt + 1
            errors.append(str(exc))
            if attempt + 1 < NODE_CHAIN_RPC_RETRIES:
                time.sleep(0.2)
    if snapshot["chain_block_count"] is None:
        detail = errors[-1] if errors else "unknown error"
        snapshot["chain_rpc_error"] = f"getBlockCount failed for {source} after {NODE_CHAIN_RPC_RETRIES} attempt(s): {detail}"
        return snapshot

    try:
        snapshot["chain_main_height"] = parse_rpc_quantity(
            mining_rpc_call(url, "getMainChainHeight", [], timeout=timeout)
        )
    except Exception as exc:
        snapshot["chain_main_height_error"] = f"getMainChainHeight failed for {source}: {exc}"
    return snapshot


def parse_prometheus_metric_values(text: str, names: set[str]) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = PROMETHEUS_METRIC_RE.match(line)
        if not match:
            continue
        name, raw = match.groups()
        if name not in names:
            continue
        try:
            metrics[name] = float(raw)
        except ValueError:
            continue
    return metrics


def fetch_node_native_sync_metrics(source: str, timeout: float = 1.5) -> dict[str, float]:
    port = NODE_METRIC_PORTS.get(source)
    if not port:
        return {}
    url = f"http://127.0.0.1:{port}/debug/metrics/prometheus"
    names = {
        "chain_head_block",
        "Blockdag_mainorder",
        "p2p_miningFreshness_bestPeerMainOrder",
        "p2p_miningFreshness_bestPeerLeadBlocks",
    }
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError):
        return {}
    return parse_prometheus_metric_values(text, names)


def native_sync_progress(source: str) -> dict[str, Any] | None:
    metrics = fetch_node_native_sync_metrics(source)
    if not metrics:
        return None
    local_main_order = safe_int(metrics.get("Blockdag_mainorder"), 0)
    best_main_order = safe_int(metrics.get("p2p_miningFreshness_bestPeerMainOrder"), 0)
    lead = safe_int(metrics.get("p2p_miningFreshness_bestPeerLeadBlocks"), 0)
    if best_main_order > 0 and local_main_order > 0:
        if best_main_order <= local_main_order:
            return None
        lead = best_main_order - local_main_order
    if lead <= NATIVE_SYNC_LEAD_THRESHOLD:
        return None

    current = safe_int(metrics.get("chain_head_block"), 0) or local_main_order
    highest = current + lead
    percent = round(max(0.0, min(100.0, (current / max(1, highest)) * 100)), 2)
    return {
        "status": "syncing",
        "percent": percent,
        "current_block": current,
        "highest_block": highest,
        "starting_block": 0,
        "remaining_blocks": lead,
        "source": f"{source}:native-p2p-lead",
        "error": "",
        "local_main_order": local_main_order,
        "best_peer_main_order": best_main_order,
        "native_sync_lead_blocks": lead,
    }


def node_template_health_snapshot(source: str, url: str, timeout: float = NODE_TEMPLATE_PROBE_TIMEOUT) -> dict[str, Any]:
    try:
        result = mining_rpc_call(url, "getTemplateHealth", [], timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - template health is advisory for sync display.
        return {
            "template_health_available": False,
            "template_health_error": f"getTemplateHealth failed for {source}: {exc}",
        }
    if not isinstance(result, dict):
        return {
            "template_health_available": False,
            "template_health_error": f"getTemplateHealth returned non-object result for {source}",
        }
    return {
        "template_health_available": True,
        "template_health": result,
        "template_health_reason_code": str(result.get("reason_code") or ""),
        "template_health_reason": str(result.get("reason") or ""),
        "template_health_mineable_now": bool(result.get("mineable_now")),
        "template_health_submit_ready": bool(result.get("submit_ready")),
        "template_health_template_usable": bool(result.get("template_usable")),
        "template_health_sync_allowed": result.get("sync_allowed"),
        "template_health_sync_reason_code": str(result.get("sync_reason_code") or ""),
        "template_health_sync_reason": str(result.get("sync_reason") or ""),
        "template_health_chain_current": result.get("chain_current"),
        "template_health_main_order": result.get("main_order"),
        "template_health_p2p_best_peer_main_order": result.get("p2p_best_peer_main_order"),
        "template_health_p2p_best_peer_lead_blocks": result.get("p2p_best_peer_lead_blocks"),
        "template_health_p2p_mining_fresh": result.get("p2p_mining_fresh"),
        "template_health_p2p_mining_fresh_reason_code": str(result.get("p2p_mining_fresh_reason_code") or ""),
    }


def template_health_sync_gap(template: dict[str, Any]) -> int:
    main_order = safe_int(template.get("template_health_main_order"))
    best_peer = safe_int(template.get("template_health_p2p_best_peer_main_order"))
    lead = safe_int(template.get("template_health_p2p_best_peer_lead_blocks"), 0)
    gap = max(0, lead or 0)
    if main_order is not None and best_peer is not None:
        gap = max(gap, best_peer - main_order)
    return max(0, gap)


def template_health_reports_syncing(template: dict[str, Any]) -> bool:
    if not template.get("template_health_available"):
        return False
    reason_code = str(template.get("template_health_reason_code") or "").lower()
    sync_reason_code = str(template.get("template_health_sync_reason_code") or "").lower()
    p2p_reason_code = str(template.get("template_health_p2p_mining_fresh_reason_code") or "").lower()
    chain_current = template.get("template_health_chain_current")
    sync_allowed = template.get("template_health_sync_allowed")
    gap = template_health_sync_gap(template)
    return bool(
        chain_current is False
        or sync_allowed is False
        or "sync" in reason_code
        or ("sync" in sync_reason_code and sync_reason_code != "ok")
        or ("sync" in p2p_reason_code and p2p_reason_code != "ok")
        or gap > NATIVE_SYNC_LEAD_THRESHOLD
    )


def node_sync_progress(source: str, url: str, timeout: float = NODE_CHAIN_RPC_TIMEOUT) -> dict[str, Any]:
    try:
        if not valid_url(url):
            raise RuntimeError("invalid node RPC URL")
        chain = node_chain_rpc_snapshot(source, url, timeout=timeout)
        current = safe_int(chain.get("chain_block_count"), None)
        if current is None:
            return {
                **unknown_sync_progress(source, str(chain.get("chain_rpc_error") or "getBlockCount unavailable")),
                **chain,
            }

        template = node_template_health_snapshot(source, url)
        if template_health_reports_syncing(template):
            remaining = template_health_sync_gap(template)
            highest = current + remaining if remaining > 0 else None
            reason = str(
                template.get("template_health_sync_reason")
                or template.get("template_health_reason")
                or "node reports sync is not current"
            )
            return {
                "status": "syncing",
                "percent": round(max(0.0, min(100.0, (current / max(1, highest)) * 100)), 2) if highest else None,
                "current_block": current,
                "highest_block": highest,
                "starting_block": None,
                "remaining_blocks": remaining if remaining > 0 else None,
                "source": f"{source}:getTemplateHealth",
                "error": reason,
                "current_block_source": "getBlockCount",
                **chain,
                **template,
            }

        native = native_sync_progress(source)
        if native:
            native.update(chain)
            native.update(template)
            native["current_block"] = current
            native["highest_block"] = None
            native["current_block_source"] = "getBlockCount"
            return native

        return {
            "status": "synced",
            "percent": 100.0,
            "current_block": current,
            "highest_block": current,
            "starting_block": None,
            "remaining_blocks": 0,
            "source": source,
            "error": "",
            "current_block_source": "getBlockCount",
            **chain,
            **template,
        }
    except Exception as exc:
        return unknown_sync_progress(source, str(exc))


def collect_sync_progress() -> dict[str, Any]:
    urls = node_rpc_urls()
    if not urls:
        return {
            **unknown_sync_progress("nodes", "no node RPC URLs available"),
            "nodes": {node: unknown_sync_progress(node, "node RPC URL unavailable") for node in NODES},
        }

    per_node = {source: node_sync_progress(source, url) for source, url in urls}
    known = [item for item in per_node.values() if item.get("status") != "unknown"]
    syncing = [item for item in known if item.get("status") == "syncing"]
    if syncing:
        status = "syncing"
        percent_values = [float(item["percent"]) for item in syncing if item.get("percent") is not None]
        percent = round(min(percent_values), 2) if percent_values else None
        remaining_values = [int(item["remaining_blocks"]) for item in syncing if item.get("remaining_blocks") is not None]
        current_values = [int(item["current_block"]) for item in syncing if item.get("current_block") is not None]
        highest_values = [int(item["highest_block"]) for item in syncing if item.get("highest_block") is not None]
        starting_values = [int(item["starting_block"]) for item in syncing if item.get("starting_block") is not None]
        error = ""
    elif known and all(item.get("status") == "synced" for item in known):
        status = "synced"
        percent = 100.0
        remaining_values = [0]
        current_values = [int(item["current_block"]) for item in known if item.get("current_block") is not None]
        highest_values = [int(item["highest_block"]) for item in known if item.get("highest_block") is not None]
        starting_values = []
        error = ""
    else:
        status = "unknown"
        percent = None
        remaining_values = []
        current_values = []
        highest_values = []
        starting_values = []
        error = "; ".join(item.get("error", "") for item in per_node.values() if item.get("error"))

    return {
        "status": status,
        "percent": percent,
        "current_block": min(current_values) if current_values else None,
        "highest_block": max(highest_values) if highest_values else None,
        "chain_block_count": min(
            int(item["chain_block_count"])
            for item in known
            if item.get("chain_block_count") is not None
        ) if known and any(item.get("chain_block_count") is not None for item in known) else None,
        "chain_main_height": min(
            int(item["chain_main_height"])
            for item in known
            if item.get("chain_main_height") is not None
        ) if known and any(item.get("chain_main_height") is not None for item in known) else None,
        "chain_rpc_source": "getBlockCount",
        "starting_block": min(starting_values) if starting_values else None,
        "remaining_blocks": max(remaining_values) if remaining_values else (0 if status == "synced" else None),
        "source": "nodes",
        "error": error,
        "nodes": per_node,
    }


def observe_sync_progress_health(sync_progress: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    previous = read_json_file(SYNC_PROGRESS_HEALTH_STATE_FILE, {})
    previous_nodes = previous.get("nodes") if isinstance(previous.get("nodes"), dict) else {}
    progress_nodes = sync_progress.get("nodes") if isinstance(sync_progress.get("nodes"), dict) else {}
    new_state: dict[str, Any] = {"updated_at": now_iso(), "epoch": now, "nodes": {}}
    active_nodes: list[str] = []
    node_rates: dict[str, float] = {}

    for node, progress in progress_nodes.items():
        if not isinstance(progress, dict):
            continue
        current = safe_int(progress.get("current_block"))
        highest = safe_int(progress.get("highest_block"))
        remaining = safe_int(progress.get("remaining_blocks"))
        if current is None:
            continue

        previous_progress = previous_nodes.get(node) if isinstance(previous_nodes.get(node), dict) else {}
        previous_current = safe_int(previous_progress.get("current_block"))
        previous_epoch = safe_float(previous_progress.get("epoch"))
        if previous_current is not None and previous_epoch is not None:
            elapsed = now - previous_epoch
            if 5 <= elapsed <= SYNC_PROGRESS_ACTIVE_LOOKBACK_SECONDS and current > previous_current:
                active_nodes.append(str(node))
                node_rates[str(node)] = round((current - previous_current) / elapsed, 3)

        new_state["nodes"][str(node)] = {
            "epoch": now,
            "current_block": current,
            "highest_block": highest,
            "remaining_blocks": remaining,
            "status": progress.get("status"),
        }

    write_json_file(SYNC_PROGRESS_HEALTH_STATE_FILE, new_state)
    return {
        "active_nodes": active_nodes,
        "active_node_count": len(active_nodes),
        "node_rates_blocks_per_second": node_rates,
        "lookback_seconds": SYNC_PROGRESS_ACTIVE_LOOKBACK_SECONDS,
    }


def explorer_api_balance(base_url: str, address: str, timeout: float = 6.0) -> dict[str, Any]:
    query = urllib.parse.urlencode({"module": "account", "action": "balance", "address": address, "tag": "latest"})
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api?{query}",
        headers={"accept": "application/json", "user-agent": HTTP_USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read(1_000_000).decode("utf-8", "replace"))
    result = payload.get("result")
    if result is None:
        raise RuntimeError(str(payload))
    return {"wei": str(result), "bdag": decimal_to_str(wei_to_bdag(result))}


def blockscout_v2_balance(base_url: str, address: str, timeout: float = 6.0) -> dict[str, Any]:
    encoded_address = urllib.parse.quote(address, safe="")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/v2/addresses/{encoded_address}",
        headers={"accept": "application/json", "user-agent": HTTP_USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read(1_000_000).decode("utf-8", "replace"))
    result = payload.get("coin_balance")
    if result is None:
        raise RuntimeError(str(payload))
    return {
        "wei": str(result),
        "bdag": decimal_to_str(wei_to_bdag(result)),
        "block_number_balance_updated_at": payload.get("block_number_balance_updated_at"),
    }


def json_rpc_call(url: str, method: str, params: list[Any], timeout: float = 6.0) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"content-type": "application/json", "accept": "application/json", "user-agent": HTTP_USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read(1_000_000).decode("utf-8", "replace"))
    if payload.get("error") is not None and "result" not in payload:
        raise RuntimeError(str(payload.get("error") or payload))
    return payload.get("result")


def fetch_block_header(url: str, block_number: int, timeout: float = 8.0) -> dict[str, Any]:
    result = json_rpc_call(url, "eth_getBlockByNumber", [hex(block_number), False], timeout=timeout)
    if not isinstance(result, dict):
        raise RuntimeError("block header response was not a JSON object")
    return result


def short_eth_address(address: str) -> str:
    value = str(address or "")
    if re.fullmatch(r"0x[a-fA-F0-9]{40}", value):
        return f"{value[:6]}...{value[-4:]}"
    return value


def read_global_pool_labels() -> dict[str, str]:
    labels = {address.lower(): name for address, name in DEFAULT_GLOBAL_POOL_LABELS.items()}
    configured = read_json_file(GLOBAL_POOL_LABEL_FILE, {})
    if isinstance(configured, dict):
        for address, name in configured.items():
            normalized = str(address or "").strip().lower()
            label = str(name or "").strip()
            if normalized and label:
                labels[normalized] = label
    return labels


def annotate_global_pool_labels(payload: dict[str, Any]) -> dict[str, Any]:
    labels = read_global_pool_labels()

    def annotate_cluster(cluster: dict[str, Any]) -> dict[str, Any]:
        address = str(cluster.get("address") or "")
        existing_name = str(cluster.get("pool_name") or "").strip()
        label = labels.get(address.lower(), "")
        if label and not (cluster.get("local_pool") and existing_name):
            cluster["pool_name"] = label
            cluster["pool_label"] = f"{label} ({short_eth_address(address)})"
        elif existing_name and address:
            cluster["pool_label"] = f"{existing_name} ({short_eth_address(address)})"
        elif existing_name:
            cluster["pool_label"] = existing_name
        elif address:
            cluster["pool_label"] = short_eth_address(address)
        return cluster

    clusters = payload.get("clusters")
    if isinstance(clusters, list):
        payload["clusters"] = [annotate_cluster(dict(cluster)) for cluster in clusters if isinstance(cluster, dict)]
    history = payload.get("history")
    if isinstance(history, list):
        annotated_history = []
        for snapshot in history:
            if not isinstance(snapshot, dict):
                continue
            item = dict(snapshot)
            item_clusters = item.get("clusters")
            if isinstance(item_clusters, list):
                item["clusters"] = [annotate_cluster(dict(cluster)) for cluster in item_clusters if isinstance(cluster, dict)]
            annotated_history.append(item)
        payload["history"] = annotated_history
    return payload


def _decode_proc_net_ipv4(token: str) -> str | None:
    if len(token) != 8:
        return None
    try:
        return ".".join(str(part) for part in bytes.fromhex(token)[::-1])
    except ValueError:
        return None


def _parse_proc_net_peer_ips(text: str) -> list[str]:
    ips: list[str] = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        if parts[3] not in {"01", "02"}:
            continue
        remote = parts[2]
        if ":" not in remote:
            continue
        ip_hex, _ = remote.split(":", 1)
        ip = _decode_proc_net_ipv4(ip_hex)
        if not ip:
            continue
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if address.version != 4 or address.is_private or address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
            continue
        if ip not in ips:
            ips.append(ip)
    return ips


def container_peer_ips(name: str) -> list[str]:
    ips: list[str] = []
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        result = run(["docker", "exec", name, "cat", path], timeout=8)
        for ip in _parse_proc_net_peer_ips(result.stdout):
            if ip not in ips:
                ips.append(ip)
    return ips


def fetch_peer_geo(ip: str) -> dict[str, Any]:
    try:
        payload = json.loads(
            fetch_text_url(
                f"https://ipwho.is/{ip}",
                {"accept": "application/json", "user-agent": HTTP_USER_AGENT},
                timeout=PEER_GEO_LOOKUP_TIMEOUT,
            )
        )
    except Exception as exc:  # noqa: BLE001 - geolocation is best-effort only.
        return {"ip": ip, "success": False, "error": str(exc), "updated_at_epoch": seconds_since_epoch()}

    if not isinstance(payload, dict):
        return {"ip": ip, "success": False, "error": "unexpected geo response", "updated_at_epoch": seconds_since_epoch()}

    connection = payload.get("connection")
    if not isinstance(connection, dict):
        connection = {}
    return {
        "ip": ip,
        "success": bool(payload.get("success")),
        "country": payload.get("country"),
        "country_code": payload.get("country_code"),
        "region": payload.get("region"),
        "region_code": payload.get("region_code"),
        "city": payload.get("city"),
        "asn": connection.get("asn"),
        "org": connection.get("org"),
        "isp": connection.get("isp"),
        "latitude": payload.get("latitude"),
        "longitude": payload.get("longitude"),
        "updated_at_epoch": seconds_since_epoch(),
    }


def _peer_location_label(geo: dict[str, Any]) -> str:
    parts = [
        str(geo.get("city") or "").strip(),
        str(geo.get("region_code") or geo.get("region") or "").strip(),
        str(geo.get("country_code") or geo.get("country") or "").strip(),
    ]
    label = ", ".join(part for part in parts if part)
    return label or "Unknown"


def _peer_country_label(geo: dict[str, Any]) -> str:
    return str(geo.get("country") or geo.get("country_code") or "").strip() or "Unknown"


def _peer_region_label(geo: dict[str, Any]) -> str:
    parts = [
        str(geo.get("region") or geo.get("region_code") or "").strip(),
        str(geo.get("country") or geo.get("country_code") or "").strip(),
    ]
    label = ", ".join(part for part in parts if part)
    return label or _peer_country_label(geo)


def _peer_city_label(geo: dict[str, Any]) -> str:
    parts = [
        str(geo.get("city") or "").strip(),
        str(geo.get("region") or geo.get("region_code") or "").strip(),
        str(geo.get("country") or geo.get("country_code") or "").strip(),
    ]
    label = ", ".join(part for part in parts if part)
    return label or _peer_region_label(geo)


def _peer_asn_label(geo: dict[str, Any]) -> str:
    asn = str(geo.get("asn") or "").strip()
    org = str(geo.get("org") or geo.get("isp") or "").strip()
    if asn and org:
        return f"ASN {asn} ({org})"
    if asn:
        return f"ASN {asn}"
    return org or "Unknown"


def _peer_provider_multiplier(geo: dict[str, Any]) -> int:
    text = f"{geo.get('org') or ''} {geo.get('isp') or ''} {geo.get('asn') or ''}".lower()
    if any(term in text for term in ("amazon", "aws", "google cloud", "microsoft", "azure", "digitalocean", "contabo", "hetzner", "ovh", "linode", "vultr", "oracle", "alibaba", "netcup", "a100 row")):
        return 10
    if any(term in text for term in ("afrihost",)):
        return 160
    if any(term in text for term in ("vox",)):
        return 130
    if any(term in text for term in ("vodafone",)):
        return 120
    if any(term in text for term in ("mobile broadband", "mobile", "wireless")):
        return 90
    if any(term in text for term in ("telecom", "broadband", "internet", "isp")):
        return 110
    return 100


def collect_peer_location_guess() -> dict[str, Any]:
    cached = read_json_file(PEER_GEO_CACHE_FILE, {})
    if not isinstance(cached, dict):
        cached = {}
    entries = cached.get("entries")
    if not isinstance(entries, dict):
        entries = {}

    peer_ips: list[str] = []
    peer_sources: dict[str, list[str]] = {}
    for node in NODES:
        ips = container_peer_ips(node)
        peer_sources[node] = ips
        for ip in ips:
            if ip not in peer_ips:
                peer_ips.append(ip)

    public_peer_ips = [ip for ip in peer_ips if is_ipv4(ip) and not is_lan_ipv4(ip)]
    if not public_peer_ips:
        return {
            "status": "ok",
            "source": "p2p-peer-ip",
            "peer_ip_count": 0,
            "geo_ip_count": 0,
            "location": "Unknown (no public peer IPs)",
            "location_confidence": "0%",
            "peer_sources": peer_sources,
        }

    now = seconds_since_epoch()
    pending = [
        ip
        for ip in public_peer_ips
        if ip not in entries or now - int(entries.get(ip, {}).get("updated_at_epoch", 0) or 0) > PEER_GEO_CACHE_TTL_SECONDS
    ]
    if pending:
        worker_count = adaptive_worker_count("peer_geo", 8, len(pending))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {executor.submit(fetch_peer_geo, ip): ip for ip in pending}
            for future in as_completed(futures):
                result = future.result()
                entries[str(result.get("ip"))] = result
        write_json_file(PEER_GEO_CACHE_FILE, {"updated_at_epoch": now, "entries": entries}, mode=0o600)

    observations: list[dict[str, Any]] = []
    for ip in public_peer_ips:
        geo = entries.get(ip)
        if not isinstance(geo, dict):
            geo = {"ip": ip, "success": False, "error": "missing geo entry"}
        seen_by = [node for node, ips in peer_sources.items() if ip in ips]
        weight = max(1, len(seen_by)) * _peer_provider_multiplier(geo)
        observation = {
            "ip": ip,
            "seen_by": seen_by,
            "seen_count": len(seen_by),
            "location": _peer_location_label(geo) if geo.get("success") else "Geo lookup failed",
            "country_label": _peer_country_label(geo) if geo.get("success") else "Geo lookup failed",
            "region_label": _peer_region_label(geo) if geo.get("success") else "Geo lookup failed",
            "city_label": _peer_city_label(geo) if geo.get("success") else "Geo lookup failed",
            "asn_label": _peer_asn_label(geo) if geo.get("success") else "Geo lookup failed",
            "country": geo.get("country"),
            "country_code": geo.get("country_code"),
            "region": geo.get("region"),
            "region_code": geo.get("region_code"),
            "city": geo.get("city"),
            "asn": geo.get("asn"),
            "org": geo.get("org") or geo.get("isp"),
            "success": bool(geo.get("success")),
            "weight": weight,
            "provider_multiplier": _peer_provider_multiplier(geo),
        }
        observations.append(observation)

    geolocated = [item for item in observations if item.get("success")]
    if not geolocated:
        return {
            "status": "ok",
            "source": "p2p-peer-ip",
            "peer_ip_count": len(public_peer_ips),
            "geo_ip_count": 0,
            "location": "Unknown (geo lookup failed)",
            "location_confidence": "0%",
            "peer_sources": peer_sources,
            "observations": sorted(observations, key=lambda item: (-int(item.get("seen_count", 0) or 0), str(item.get("ip") or ""))),
        }

    total_weight = sum(int(item.get("weight", 1) or 1) for item in geolocated) or len(geolocated)

    def build_rankings(level: str) -> list[dict[str, Any]]:
        grouped: Counter[str] = Counter()
        representative: dict[str, dict[str, Any]] = {}
        for geo in geolocated:
            weight = int(geo.get("weight", 1) or 1)
            if level == "country":
                key = str(geo.get("country_code") or geo.get("country") or "").strip()
                label = str(geo.get("country") or geo.get("country_code") or "").strip()
            elif level == "region":
                key = "::".join(
                    [
                        str(geo.get("country_code") or geo.get("country") or "").strip(),
                        str(geo.get("region_code") or geo.get("region") or "").strip(),
                    ]
                )
                label = _peer_region_label(geo)
            elif level == "city":
                key = "::".join(
                    [
                        str(geo.get("country_code") or geo.get("country") or "").strip(),
                        str(geo.get("region_code") or geo.get("region") or "").strip(),
                        str(geo.get("city") or "").strip(),
                    ]
                )
                label = _peer_city_label(geo)
            elif level == "asn":
                key = str(geo.get("asn") or "").strip()
                label = _peer_asn_label(geo)
            else:
                key = ""
                label = "Unknown"
            if not key:
                continue
            grouped[key] += weight
            representative.setdefault(key, {"label": label, "geo": geo})

        ranked: list[dict[str, Any]] = []
        for key, count in grouped.most_common(5):
            item = representative[key]
            ranked.append(
                {
                    "level": level,
                    "label": item["label"],
                    "count": count,
                    "share_percent": decimal_to_str((Decimal(count) / Decimal(total_weight)) * Decimal("100"), places=1),
                    "representative": item["geo"],
                }
            )
        return ranked

    rankings = {
        "country": build_rankings("country"),
        "region": build_rankings("region"),
        "city": build_rankings("city"),
        "asn": build_rankings("asn"),
    }

    preferred_levels = [
        ("city", Decimal("0.15")),
        ("region", Decimal("0.20")),
        ("country", Decimal("0.25")),
        ("asn", Decimal("0.20")),
    ]
    best_guess: dict[str, Any] | None = None
    for level, threshold in preferred_levels:
        ranked = rankings.get(level) or []
        if not ranked:
            continue
        top = ranked[0]
        share = Decimal(str(top.get("share_percent") or "0")).quantize(Decimal("0.1"))
        if Decimal(str(top.get("count") or 0)) >= 2 or share / Decimal("100") >= threshold:
            best_guess = {
                "level": level,
                "location": top["label"],
                "count": top["count"],
                "confidence": f"{top['share_percent']}%",
            }
            break
    if best_guess is None and rankings["country"]:
        top = rankings["country"][0]
        best_guess = {
            "level": "country",
            "location": top["label"],
            "count": top["count"],
            "confidence": f"{top['share_percent']}%",
        }
    if best_guess is None:
        top = geolocated[0]
        best_guess = {
            "level": "unknown",
            "location": _peer_location_label(top),
            "count": 1,
            "confidence": "0.0%",
        }

    asns = unique_names(
        [
            str(geo.get("asn") or "")
            for geo in geolocated
            if geo.get("asn") not in {None, ""}
        ]
    )
    orgs = unique_names(
        [
            str(geo.get("org") or geo.get("isp") or "")
            for geo in geolocated
            if geo.get("org") or geo.get("isp")
        ]
    )
    return {
        "status": "ok",
        "source": "p2p-peer-ip",
        "peer_ip_count": len(public_peer_ips),
        "geo_ip_count": len(geolocated),
        "location": f"{best_guess['location']} (best guess)",
        "location_confidence": f"{best_guess['confidence']}",
        "best_guess": best_guess,
        "rankings": rankings,
        "peer_sources": peer_sources,
        "asn_samples": asns[:5],
        "org_samples": orgs[:5],
        "observations": sorted(observations, key=lambda item: (-int(item.get("seen_count", 0) or 0), str(item.get("ip") or ""))),
    }


def global_snapshot_has_plot_data(snapshot: dict[str, Any]) -> bool:
    clusters = snapshot.get("clusters")
    return isinstance(clusters, list) and any(isinstance(cluster, dict) for cluster in clusters)


def compact_global_snapshot_for_history(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated_at": snapshot.get("generated_at") or snapshot.get("updated_at"),
        "latest_block": snapshot.get("latest_block"),
        "requested_blocks": snapshot.get("requested_blocks"),
        "fetched_blocks": snapshot.get("fetched_blocks"),
        "head_only": bool(snapshot.get("head_only")),
        "maintenance_deferred": bool(snapshot.get("maintenance_deferred")),
        "deferred_scan": bool(snapshot.get("deferred_scan")),
        "scan_window_hours": snapshot.get("scan_window_hours"),
        "avg_blocks_per_second": snapshot.get("avg_blocks_per_second"),
        "max_transactions_per_block": snapshot.get("max_transactions_per_block"),
        "max_avg_block_transactions_per_second": snapshot.get("max_avg_block_transactions_per_second"),
        "clusters": [
            {
                "address": cluster.get("address"),
                "address_short": cluster.get("address_short"),
                "pool_name": cluster.get("pool_name", ""),
                "pool_label": cluster.get("pool_label", cluster.get("address_short")),
                "source": cluster.get("source", "on-chain"),
                "local_pool": bool(cluster.get("local_pool")),
                "estimated_bdag_avg_hour": cluster.get("estimated_bdag_avg_hour"),
                "estimated_usd_avg_hour": cluster.get("estimated_usd_avg_hour"),
                "estimated_zar_avg_hour": cluster.get("estimated_zar_avg_hour"),
                "estimated_bdag_recent_hour": cluster.get("estimated_bdag_recent_hour"),
                "estimated_usd_recent_hour": cluster.get("estimated_usd_recent_hour"),
                "estimated_zar_recent_hour": cluster.get("estimated_zar_recent_hour"),
                "blocks": cluster.get("blocks"),
                "shares": cluster.get("shares", cluster.get("blocks")),
                "credit_blocks": cluster.get("credit_blocks", cluster.get("blocks")),
                "found_blocks": cluster.get("found_blocks", cluster.get("blocks")),
                "share_percent": cluster.get("share_percent"),
                "credited_bdag": cluster.get("credited_bdag", cluster.get("estimated_bdag")),
                "estimated_bdag": cluster.get("estimated_bdag"),
                "estimated_usd": cluster.get("estimated_usd"),
                "estimated_zar": cluster.get("estimated_zar"),
            }
            for cluster in snapshot.get("clusters") or []
            if isinstance(cluster, dict)
        ],
    }


def read_global_history(limit: int | None = None) -> list[dict[str, Any]]:
    history, _sample_count = read_dashboard_history(
        "global",
        GLOBAL_HISTORY_FILE,
        compact_global_snapshot_for_history,
        global_snapshot_has_plot_data,
        limit=limit,
    )
    if not history and GLOBAL_HISTORY_FILE.exists():
        return read_jsonl_file(GLOBAL_HISTORY_FILE, limit=limit)
    return history


def record_global_snapshot(snapshot: dict[str, Any]) -> None:
    ensure_runtime()
    append_jsonl_file(GLOBAL_HISTORY_FILE, snapshot, mode=0o600)
    update_dashboard_history_with_snapshot(
        "global",
        GLOBAL_HISTORY_FILE,
        snapshot,
        compact_global_snapshot_for_history,
        global_snapshot_has_plot_data,
    )
    state = read_json_file(GLOBAL_HISTORY_STATE_FILE, {})
    previous_count = safe_int(state.get("row_count") if isinstance(state, dict) else None, 0)
    row_count = previous_count + 1 if previous_count > 0 else count_text_lines(GLOBAL_HISTORY_FILE)
    compact_threshold = max(GLOBAL_HISTORY_LIMIT + 1, GLOBAL_HISTORY_LIMIT * GLOBAL_HISTORY_COMPACT_MULTIPLIER)
    compacted = False
    if row_count > compact_threshold:
        row_count = compact_jsonl_file(GLOBAL_HISTORY_FILE, GLOBAL_HISTORY_LIMIT, mode=0o600)
        compacted = True
    write_json_file(
        GLOBAL_HISTORY_STATE_FILE,
        {
            "updated_at": now_iso(),
            "row_count": row_count,
            "limit": GLOBAL_HISTORY_LIMIT,
            "compact_threshold": compact_threshold,
            "compacted": compacted,
        },
        mode=0o600,
    )


def _pool_earning_rates_from_cluster(cluster: dict[str, Any], scan_window_hours: Decimal | None) -> tuple[str | None, str | None, str | None]:
    est_bdag = decimal_value(cluster.get("estimated_bdag"))
    if est_bdag is None or not scan_window_hours or scan_window_hours <= 0:
        return None, None, None
    hourly_bdag = est_bdag / scan_window_hours
    hourly_usd = decimal_value(cluster.get("estimated_usd"))
    hourly_zar = decimal_value(cluster.get("estimated_zar"))
    if hourly_usd is not None and est_bdag != 0:
        hourly_usd = hourly_usd / scan_window_hours
    if hourly_zar is not None and est_bdag != 0:
        hourly_zar = hourly_zar / scan_window_hours
    return (
        decimal_to_str(hourly_bdag),
        decimal_to_str(hourly_usd) if hourly_usd is not None else None,
        decimal_to_str(hourly_zar) if hourly_zar is not None else None,
    )


def _pool_earning_rates_from_amount(
    amount_bdag: Decimal | None,
    price: dict[str, Any],
    scan_window_hours: Decimal | None,
) -> tuple[str | None, str | None, str | None]:
    if amount_bdag is None or not scan_window_hours or scan_window_hours <= 0:
        return None, None, None
    hourly_bdag = amount_bdag / scan_window_hours
    return (
        decimal_to_str(hourly_bdag),
        fiat_value(hourly_bdag, price, "usd", places=6),
        fiat_value(hourly_bdag, price, "zar", places=6),
    )


def local_worker_identity_map() -> dict[str, dict[str, Any]]:
    registry = read_miner_registry()
    workers: dict[str, dict[str, Any]] = {}
    for miner in registry.get("miners", []) or []:
        if not isinstance(miner, dict):
            continue
        worker_values = merge_unique_strings(miner.get("last_workers"), miner.get("workers"))
        label = miner_display_label(miner)
        for worker in worker_values:
            if not is_spendable_eth_address(worker):
                continue
            workers[str(worker).lower()] = {
                "pool_name": label,
                "display_name": miner.get("display_name") or miner.get("name") or "",
                "display_label": label,
                "device_type": miner.get("device_type") or "",
                "ip": miner.get("ip") or "",
                "mac": normalize_mac(miner.get("mac")),
                "identity_key": miner_display_identity(miner),
            }
    return workers


def collect_local_pool_global_clusters(
    scan_window_seconds: int,
    total_global_blocks: int,
    scan_window_hours: Decimal | None,
    price: dict[str, Any],
) -> list[dict[str, Any]]:
    seconds = max(1, int(scan_window_seconds or 1))
    sql = f"""
    SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json)
    FROM (
      SELECT c.miner_address,
             count(*) AS credit_count,
             count(DISTINCT c.block_hash) AS found_blocks,
             COALESCE(sum(c.amount), 0)::text AS total_wei,
             COALESCE(sum(c.amount) FILTER (WHERE b.status = 'PAID'), 0)::text AS paid_wei,
             COALESCE(sum(c.amount) FILTER (WHERE b.status <> 'PAID' OR b.status IS NULL), 0)::text AS pending_wei,
             min(c.created_at)::text AS first_credit_at,
             max(c.created_at)::text AS last_credit_at,
             to_char(min(c.created_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS first_seen_at,
             to_char(max(c.created_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS last_seen_at
      FROM credits c
      LEFT JOIN blocks b ON b.hash = c.block_hash
      WHERE c.created_at >= now() - ({seconds} * interval '1 second')
      GROUP BY c.miner_address
      ORDER BY count(DISTINCT c.block_hash) DESC, sum(c.amount) DESC
    ) t;
    """
    try:
        rows = pool_db_json(sql) or []
    except Exception as exc:  # noqa: BLE001
        return [{"source": "local-pool-db", "status": "failed", "error": str(exc), "local_pool": True}]
    if not isinstance(rows, list):
        return []

    identities = local_worker_identity_map()
    miner_share_totals: dict[str, Any]
    try:
        miner_share_totals = collect_miner_tab_share_totals()
    except Exception as exc:  # noqa: BLE001 - Global should still render chain data if miner health is unavailable.
        miner_share_totals = {
            "status": "failed",
            "source": "miner-health",
            "source_contract": "miners-tab:miner_health.miners[].shares",
            "error": str(exc),
            "total_shares": None,
            "by_worker": {},
        }
    local_addresses = {
        str(row.get("miner_address") or "").strip().lower()
        for row in rows
        if isinstance(row, dict) and is_spendable_eth_address(str(row.get("miner_address") or "").strip())
    }
    single_local_address = len(local_addresses) == 1
    clusters: list[dict[str, Any]] = []
    denominator = Decimal(max(1, int(total_global_blocks or 0)))
    for row in rows:
        if not isinstance(row, dict):
            continue
        address = str(row.get("miner_address") or "").strip()
        if not is_spendable_eth_address(address):
            continue
        credit_count = int(row.get("credit_count", 0) or 0)
        found_blocks = int(row.get("found_blocks", 0) or 0)
        total_bdag = wei_to_bdag(row.get("total_wei"))
        hourly_bdag = total_bdag / scan_window_hours if scan_window_hours and scan_window_hours > 0 else None
        identity = identities.get(address.lower(), {})
        pool_name = str(identity.get("display_label") or identity.get("pool_name") or "Local pool")
        share = Decimal(found_blocks) / denominator
        worker_share_map = miner_share_totals.get("by_worker") if isinstance(miner_share_totals.get("by_worker"), dict) else {}
        miner_tab_shares = worker_share_map.get(address.lower())
        if miner_tab_shares is None and single_local_address and miner_share_totals.get("total_shares") is not None:
            miner_tab_shares = safe_int(miner_share_totals.get("total_shares"), 0)
        shares_source_error = str(miner_share_totals.get("error") or "")
        clusters.append(
            {
                "rank": None,
                "address": address,
                "address_short": short_eth_address(address),
                "pool_name": pool_name,
                "pool_label": f"{pool_name} ({short_eth_address(address)})",
                "source": "local-pool-db",
                "local_pool": True,
                "nodes": list(NODES),
                "rpc_sources": ["local-pool-db"],
                "workers": [address],
                "shares": miner_tab_shares,
                "local_shares": miner_tab_shares,
                "miner_tab_shares": miner_tab_shares,
                "pool_credit_shares": credit_count,
                "shares_source": str(miner_share_totals.get("source") or "miner-health"),
                "shares_source_contract": str(miner_share_totals.get("source_contract") or "miners-tab:miner_health.miners[].shares"),
                "shares_source_error": shares_source_error,
                "blocks": found_blocks,
                "credit_blocks": credit_count,
                "found_blocks": found_blocks,
                "share_percent": decimal_to_str(share * Decimal("100"), places=2),
                "credited_bdag": decimal_to_str(total_bdag),
                "estimated_bdag": decimal_to_str(total_bdag),
                "estimated_usd": fiat_value(total_bdag, price, "usd"),
                "estimated_zar": fiat_value(total_bdag, price, "zar"),
                "estimated_wallet_bdag": decimal_to_str(total_bdag),
                "estimated_bdag_avg_hour": decimal_to_str(hourly_bdag) if hourly_bdag is not None else None,
                "estimated_usd_avg_hour": fiat_value(hourly_bdag, price, "usd", places=6) if hourly_bdag is not None else None,
                "estimated_zar_avg_hour": fiat_value(hourly_bdag, price, "zar", places=6) if hourly_bdag is not None else None,
                "estimated_bdag_recent_hour": decimal_to_str(hourly_bdag) if hourly_bdag is not None else None,
                "estimated_usd_recent_hour": fiat_value(hourly_bdag, price, "usd", places=6) if hourly_bdag is not None else None,
                "estimated_zar_recent_hour": fiat_value(hourly_bdag, price, "zar", places=6) if hourly_bdag is not None else None,
                "first_seen_at": row.get("first_seen_at") or row.get("first_credit_at"),
                "last_seen_at": row.get("last_seen_at") or row.get("last_credit_at"),
                "location": "local pool",
                "location_confidence": "pool-db",
                "identity_key": identity.get("identity_key") or "",
                "ip": identity.get("ip") or "",
                "mac": identity.get("mac") or "",
                "device_type": identity.get("device_type") or "",
            }
        )
    return clusters


def merge_global_local_pool_clusters(
    chain_clusters: list[dict[str, Any]],
    local_clusters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = [dict(cluster) for cluster in chain_clusters if isinstance(cluster, dict)]
    by_address = {str(cluster.get("address") or "").lower(): cluster for cluster in merged}
    for local in local_clusters:
        if not isinstance(local, dict) or local.get("status") == "failed":
            continue
        address = str(local.get("address") or "").lower()
        existing = by_address.get(address)
        if existing is None:
            merged.append(dict(local))
            continue
        existing["local_pool"] = True
        existing["source"] = "on-chain+local-pool-db"
        for key in (
            "pool_name",
            "pool_label",
            "nodes",
            "workers",
            "shares",
            "local_shares",
            "miner_tab_shares",
            "pool_credit_shares",
            "shares_source",
            "shares_source_contract",
            "shares_source_error",
            "credit_blocks",
            "credited_bdag",
            "found_blocks",
            "estimated_wallet_bdag",
            "identity_key",
            "ip",
            "mac",
            "device_type",
        ):
            if local.get(key) not in (None, "", []):
                existing[key] = local[key]
    merged.sort(key=lambda item: (int(item.get("blocks", 0) or 0), str(item.get("last_seen_at") or "")), reverse=True)
    for rank, cluster in enumerate(merged, start=1):
        cluster["rank"] = rank
    return merged


def collect_miner_tab_share_totals() -> dict[str, Any]:
    status = read_status_sampler_payload(include_logs=True)
    source = "status-sampler"
    health = status.get("miner_health") if isinstance(status, dict) else None
    if not isinstance(health, dict) or not isinstance(health.get("miners"), list):
        source = "live-miner-health"
        health = collect_miner_health()

    by_worker: dict[str, int] = {}
    total_shares = 0
    miners = [item for item in health.get("miners", []) if isinstance(item, dict)]
    for miner in miners:
        shares = safe_int(miner.get("shares"), 0)
        total_shares += shares
        workers = [
            str(worker).lower()
            for worker in merge_unique_strings(miner.get("workers"), miner.get("expected_worker_user"))
            if is_spendable_eth_address(str(worker))
        ]
        workers = unique_names(workers)
        if len(workers) == 1:
            by_worker[workers[0]] = by_worker.get(workers[0], 0) + shares

    return {
        "status": "ok",
        "source": source,
        "source_contract": "miners-tab:miner_health.miners[].shares",
        "miner_count": len(miners),
        "total_shares": total_shares,
        "by_worker": by_worker,
    }


def apply_miner_tab_shares_to_global_payload(payload: dict[str, Any]) -> dict[str, Any]:
    clusters = payload.get("clusters")
    if not isinstance(clusters, list) or not any(isinstance(item, dict) and item.get("local_pool") for item in clusters):
        return payload

    try:
        miner_share_totals = collect_miner_tab_share_totals()
    except Exception as exc:  # noqa: BLE001 - keep cached Global rows renderable if miner health is unavailable.
        miner_share_totals = {
            "source": "miner-health",
            "source_contract": "miners-tab:miner_health.miners[].shares",
            "error": str(exc),
            "total_shares": None,
            "by_worker": {},
        }

    worker_share_map = miner_share_totals.get("by_worker") if isinstance(miner_share_totals.get("by_worker"), dict) else {}
    local_addresses = {
        str(item.get("address") or "").lower()
        for item in clusters
        if isinstance(item, dict) and item.get("local_pool") and is_spendable_eth_address(str(item.get("address") or ""))
    }
    single_local_address = len(local_addresses) == 1
    for item in clusters:
        if not isinstance(item, dict) or not item.get("local_pool"):
            continue
        address = str(item.get("address") or "").lower()
        miner_tab_shares = worker_share_map.get(address)
        if miner_tab_shares is None and single_local_address and miner_share_totals.get("total_shares") is not None:
            miner_tab_shares = safe_int(miner_share_totals.get("total_shares"), 0)
        item["shares_source"] = str(miner_share_totals.get("source") or "miner-health")
        item["shares_source_contract"] = str(miner_share_totals.get("source_contract") or "miners-tab:miner_health.miners[].shares")
        item["shares_source_error"] = str(miner_share_totals.get("error") or "")
        if miner_tab_shares is not None:
            item["shares"] = miner_tab_shares
            item["local_shares"] = miner_tab_shares
            item["miner_tab_shares"] = miner_tab_shares
    return payload


def enrich_global_rate_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    max_tx_per_block = safe_int(payload.get("max_transactions_per_block"), BDAG_MAX_TRANSACTIONS_PER_BLOCK)
    if max_tx_per_block <= 0:
        max_tx_per_block = BDAG_MAX_TRANSACTIONS_PER_BLOCK
    payload["max_transactions_per_block"] = max_tx_per_block

    avg_blocks_per_second = safe_decimal(payload.get("avg_blocks_per_second"))
    if avg_blocks_per_second is None:
        avg_block_seconds = safe_decimal(payload.get("avg_block_seconds"))
        if avg_block_seconds is not None and avg_block_seconds > 0:
            avg_blocks_per_second = Decimal("1") / avg_block_seconds
            payload["avg_blocks_per_second"] = decimal_to_str(avg_blocks_per_second, places=3)

    if safe_decimal(payload.get("max_avg_block_transactions_per_second")) is None and avg_blocks_per_second is not None:
        payload["max_avg_block_transactions_per_second"] = decimal_to_str(
            avg_blocks_per_second * Decimal(max_tx_per_block),
            places=2,
        )
    return payload


def global_cache_window_matches(cached: dict[str, Any]) -> bool:
    requested_blocks = safe_int(cached.get("requested_blocks"), 0)
    if requested_blocks <= 0:
        return False
    latest_block = safe_int(cached.get("latest_block"), requested_blocks - 1)
    expected_blocks = min(max(GLOBAL_BLOCK_WINDOW, 1), max(0, latest_block) + 1)
    return requested_blocks == expected_blocks


def refresh_global_chain_head(payload: dict[str, Any]) -> dict[str, Any]:
    """Add a live EVM chain tip to dashboard payloads without changing scan-window data."""
    rpc_sources = global_evm_rpc_urls() or [("local-chain", f"http://127.0.0.1:{NODE_EVM_RPC_PORT}")]
    errors: list[str] = []
    for source_name, source_url in rpc_sources:
        try:
            latest_hex = json_rpc_call(source_url, "eth_blockNumber", [], timeout=4.0)
            latest_block = int(str(latest_hex), 16)
            scanned_tip = safe_int(payload.get("latest_block"), 0)
            payload["chain_latest_block"] = latest_block
            payload["chain_latest_block_source"] = source_name or "eth_blockNumber"
            payload["chain_latest_block_updated_at"] = now_iso()
            payload["chain_tip_lag_blocks"] = max(0, latest_block - scanned_tip)
            return payload
        except Exception as exc:  # noqa: BLE001 - live tip freshness is best-effort.
            errors.append(f"{source_name}: {exc}")
    if errors:
        existing_errors = list(payload.get("head_probe_errors") or [])
        payload["head_probe_errors"] = [*existing_errors, *errors][:20]
    return payload


def collect_global_blockchain() -> dict[str, Any]:
    cached = read_json_file(GLOBAL_CACHE_FILE, {})
    if isinstance(cached, dict):
        cached = enrich_global_rate_metrics(cached)
        cached = apply_miner_tab_shares_to_global_payload(cached)
    cached_at = int(cached.get("updated_at_epoch", 0) or 0) if isinstance(cached, dict) else 0
    if (
        cached.get("status") == "ok"
        and global_cache_window_matches(cached)
        and seconds_since_epoch() - cached_at <= GLOBAL_CACHE_TTL_SECONDS
    ):
        return annotate_global_pool_labels(refresh_global_chain_head({**cached, "cache_hit": True, "history": read_global_history(limit=GLOBAL_HISTORY_LIMIT)}))

    def stale_or_failed(error: str, errors: list[str] | None = None) -> dict[str, Any]:
        history = read_global_history(limit=GLOBAL_HISTORY_LIMIT)
        if isinstance(cached, dict) and cached.get("status") == "ok":
            return annotate_global_pool_labels(
                refresh_global_chain_head({
                    **cached,
                    "status": "stale",
                    "stale": True,
                    "cache_hit": True,
                    "error": error,
                    "fetch_errors": errors or cached.get("fetch_errors", []),
                    "history": history,
                })
            )
        return annotate_global_pool_labels(refresh_global_chain_head(
            {
                "status": "failed",
                "source": "on-chain",
                "error": error,
                "fetch_errors": errors or [],
                "clusters": [],
                "history": history,
            }
        ))

    maintenance_decision = background_maintenance_decision("global_blockchain_scan")
    maintenance_deferred = not maintenance_decision.get("allowed", True)
    maintenance_reason = "; ".join(str(item) for item in maintenance_decision.get("reasons", []) if item)

    rpc_sources = global_evm_rpc_urls() or [("local-chain", f"http://127.0.0.1:{NODE_EVM_RPC_PORT}")]
    latest_errors: list[str] = []
    rpc_name = ""
    rpc_url = ""
    latest_block: int | None = None
    for source_name, source_url in rpc_sources:
        try:
            latest_hex = json_rpc_call(source_url, "eth_blockNumber", [], timeout=8.0)
            latest_block = int(str(latest_hex), 16)
            rpc_name = source_name
            rpc_url = source_url
            break
        except Exception as exc:  # noqa: BLE001
            latest_errors.append(f"{source_name}: {exc}")
    if latest_block is None:
        return stale_or_failed("unable to fetch latest global block height from EVM RPC", latest_errors)

    if maintenance_deferred and GLOBAL_DEFERRED_BLOCK_WINDOW <= 0:
        history = read_global_history(limit=GLOBAL_HISTORY_LIMIT)
        payload = annotate_global_pool_labels(
            {
                "status": "deferred",
                "source": "on-chain-head",
                "updated_at": now_iso(),
                "updated_at_epoch": seconds_since_epoch(),
                "rpc_source": rpc_name,
                "latest_block": latest_block,
                "requested_blocks": 0,
                "global_block_window": GLOBAL_BLOCK_WINDOW,
                "global_deferred_block_window": GLOBAL_DEFERRED_BLOCK_WINDOW,
                "fetched_blocks": 0,
                "global_rpc_worker_count": 0,
                "adaptive_concurrency": adaptive_worker_budgets(
                    {
                        "iowait_percent": maintenance_decision.get("iowait_percent"),
                        "io_some_avg10": maintenance_decision.get("io_some_avg10"),
                        "cpu_some_avg10": maintenance_decision.get("cpu_some_avg10"),
                        "chain_rpc_latency_ms": maintenance_decision.get("chain_rpc_latency_ms"),
                    }
                ),
                "scan_start_block": None,
                "scan_end_block": latest_block,
                "scan_window_seconds": 0,
                "scan_window_hours": "0.00",
                "unique_miners": 0,
                "chain_unique_miners": 0,
                "clusters": [],
                "chain_clusters": [],
                "local_pool_clusters": [],
                "peer_location": {"observations": []},
                "fetch_errors": latest_errors[:20],
                "history": history,
                "head_only": True,
                "maintenance_deferred": True,
                "maintenance_decision": maintenance_decision,
                "deferred_scan": True,
                "error": f"global blockchain scan reduced to head-only: {maintenance_reason}",
            }
        )
        payload = refresh_global_chain_head(payload)
        write_json_file(GLOBAL_CACHE_FILE, payload, mode=0o600)
        return payload

    scan_block_window = GLOBAL_BLOCK_WINDOW
    configured_rpc_workers = GLOBAL_RPC_WORKERS
    if maintenance_deferred:
        scan_block_window = min(max(GLOBAL_DEFERRED_BLOCK_WINDOW, 1), max(GLOBAL_BLOCK_WINDOW, 1))
        configured_rpc_workers = min(GLOBAL_DEFERRED_RPC_WORKERS, GLOBAL_RPC_WORKERS)

    requested_count = min(max(scan_block_window, 1), latest_block + 1)
    start_block = max(0, latest_block - requested_count + 1)
    block_numbers = list(range(start_block, latest_block + 1))

    def load_block(block_number: int) -> dict[str, Any]:
        last_error: Exception | None = None
        for source_name, source_url in rpc_sources:
            try:
                header = fetch_block_header(source_url, block_number, timeout=8.0)
                header["_rpc_source"] = source_name
                return header
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        raise RuntimeError(str(last_error or f"failed to fetch block {block_number}"))

    headers: list[dict[str, Any]] = []
    fetch_errors: list[str] = []
    global_pressure = {
        "iowait_percent": maintenance_decision.get("iowait_percent"),
        "io_some_avg10": maintenance_decision.get("io_some_avg10"),
        "cpu_some_avg10": maintenance_decision.get("cpu_some_avg10"),
        "chain_rpc_latency_ms": maintenance_decision.get("chain_rpc_latency_ms"),
    }
    global_worker_count = adaptive_worker_count("global_rpc", configured_rpc_workers, len(block_numbers), global_pressure)
    with ThreadPoolExecutor(max_workers=global_worker_count) as pool:
        future_map = {pool.submit(load_block, number): number for number in block_numbers}
        for future in as_completed(future_map):
            number = future_map[future]
            try:
                headers.append(future.result())
            except Exception as exc:  # noqa: BLE001
                fetch_errors.append(f"{number}: {exc}")

    headers.sort(key=lambda item: int(str(item.get("number") or "0"), 16))
    if not headers:
        return stale_or_failed("unable to fetch block headers", fetch_errors)

    reward_summary = {}
    try:
        reward_summary = pool_db_json(
            """
            SELECT json_build_object(
              'block_count', count(*),
              'avg_reward_wei', COALESCE(avg(reward), 0)::text,
              'total_reward_wei', COALESCE(sum(reward), 0)::text
            )
            FROM blocks;
            """
        ) or {}
    except Exception as exc:  # noqa: BLE001
        reward_summary = {"error": str(exc)}

    avg_reward_bdag = None if reward_summary.get("error") else wei_to_bdag(reward_summary.get("avg_reward_wei"))
    total_blocks = len(headers)
    total_reward_estimate = avg_reward_bdag * Decimal(total_blocks) if avg_reward_bdag is not None else None
    price = fetch_cmc_price()
    peer_location = collect_peer_location_guess()

    cluster_map: dict[str, dict[str, Any]] = {}
    first_seen_epoch: int | None = None
    last_seen_epoch: int | None = None
    for header in headers:
        miner = str(header.get("miner") or header.get("author") or header.get("coinbase") or "").lower()
        if not miner:
            continue
        epoch = int(str(header.get("timestamp") or "0"), 16)
        height = int(str(header.get("number") or "0"), 16)
        entry = cluster_map.setdefault(
            miner,
            {
                "address": miner,
                "blocks": 0,
                "first_height": height,
                "last_height": height,
                "first_seen_epoch": epoch,
                "last_seen_epoch": epoch,
                "rpc_sources": [],
            },
        )
        entry["blocks"] += 1
        entry["first_height"] = min(entry["first_height"], height)
        entry["last_height"] = max(entry["last_height"], height)
        entry["first_seen_epoch"] = min(entry["first_seen_epoch"], epoch)
        entry["last_seen_epoch"] = max(entry["last_seen_epoch"], epoch)
        entry["rpc_sources"].append(str(header.get("_rpc_source") or rpc_name))
        first_seen_epoch = epoch if first_seen_epoch is None else min(first_seen_epoch, epoch)
        last_seen_epoch = epoch if last_seen_epoch is None else max(last_seen_epoch, epoch)

    clusters = sorted(cluster_map.values(), key=lambda item: (item["blocks"], item["last_seen_epoch"]), reverse=True)
    unique_miners = len(clusters)
    window_seconds = max(1, (last_seen_epoch or 0) - (first_seen_epoch or 0))
    avg_block_seconds = window_seconds / max(1, total_blocks - 1) if total_blocks > 1 else None
    avg_blocks_per_second = Decimal(total_blocks - 1) / Decimal(window_seconds) if total_blocks > 1 else None
    max_avg_block_transactions_per_second = (
        avg_blocks_per_second * Decimal(BDAG_MAX_TRANSACTIONS_PER_BLOCK)
        if avg_blocks_per_second is not None
        else None
    )
    total_reward_estimate_bdag = decimal_to_str(total_reward_estimate, places=2) if total_reward_estimate is not None else None
    scan_window_hours = Decimal(str(window_seconds)) / Decimal("3600") if window_seconds > 0 else None
    enriched_clusters: list[dict[str, Any]] = []
    for rank, cluster in enumerate(clusters, start=1):
        blocks = int(cluster["blocks"])
        share = Decimal(blocks) / Decimal(total_blocks) if total_blocks else Decimal("0")
        est_bdag = avg_reward_bdag * Decimal(blocks) if avg_reward_bdag is not None else None
        est_bdag_hour, est_usd_hour, est_zar_hour = _pool_earning_rates_from_amount(est_bdag, price, scan_window_hours)
        enriched_clusters.append(
            {
                "rank": rank,
                "address": cluster["address"],
                "address_short": short_eth_address(cluster["address"]),
                "blocks": blocks,
                "share_percent": decimal_to_str(share * Decimal("100"), places=2),
                "estimated_bdag": decimal_to_str(est_bdag, places=2) if est_bdag is not None else None,
                "estimated_usd": fiat_value(est_bdag, price, "usd") if est_bdag is not None else None,
                "estimated_zar": fiat_value(est_bdag, price, "zar") if est_bdag is not None else None,
                "estimated_bdag_avg_hour": est_bdag_hour,
                "estimated_usd_avg_hour": est_usd_hour,
                "estimated_zar_avg_hour": est_zar_hour,
                "estimated_bdag_recent_hour": est_bdag_hour,
                "estimated_usd_recent_hour": est_usd_hour,
                "estimated_zar_recent_hour": est_zar_hour,
                "first_seen_at": datetime.fromtimestamp(cluster["first_seen_epoch"], tz=timezone.utc).isoformat(),
                "last_seen_at": datetime.fromtimestamp(cluster["last_seen_epoch"], tz=timezone.utc).isoformat(),
                "avg_block_seconds": decimal_to_str(Decimal(str(avg_block_seconds)), places=1) if avg_block_seconds is not None and blocks > 1 else None,
                "location": str(peer_location.get("location") or "Unknown"),
                "location_confidence": str(peer_location.get("location_confidence") or "n/a"),
                "rpc_sources": unique_names(cluster["rpc_sources"]),
            }
        )

    local_pool_clusters = collect_local_pool_global_clusters(
        window_seconds,
        total_blocks,
        scan_window_hours,
        price,
    )
    display_clusters = merge_global_local_pool_clusters(enriched_clusters, local_pool_clusters)

    payload = {
        "status": "degraded" if maintenance_deferred else "ok",
        "source": "on-chain",
        "updated_at": now_iso(),
        "updated_at_epoch": seconds_since_epoch(),
        "rpc_source": rpc_name,
        "latest_block": latest_block,
        "requested_blocks": requested_count,
        "global_block_window": GLOBAL_BLOCK_WINDOW,
        "global_deferred_block_window": GLOBAL_DEFERRED_BLOCK_WINDOW,
        "effective_block_window": requested_count,
        "fetched_blocks": total_blocks,
        "global_rpc_worker_count": global_worker_count,
        "adaptive_concurrency": adaptive_worker_budgets(global_pressure),
        "scan_start_block": start_block,
        "scan_end_block": latest_block,
        "scan_window_seconds": window_seconds,
        "scan_window_hours": decimal_to_str(Decimal(str(window_seconds)) / Decimal("3600"), places=2),
        "avg_block_seconds": decimal_to_str(Decimal(str(avg_block_seconds)), places=1) if avg_block_seconds is not None else None,
        "avg_blocks_per_second": decimal_to_str(avg_blocks_per_second, places=3) if avg_blocks_per_second is not None else None,
        "max_transactions_per_block": BDAG_MAX_TRANSACTIONS_PER_BLOCK,
        "max_avg_block_transactions_per_second": (
            decimal_to_str(max_avg_block_transactions_per_second, places=2)
            if max_avg_block_transactions_per_second is not None
            else None
        ),
        "avg_reward_bdag": decimal_to_str(avg_reward_bdag, places=2) if avg_reward_bdag is not None else None,
        "estimated_total_reward_bdag": total_reward_estimate_bdag,
        "estimated_total_reward_usd": fiat_value(total_reward_estimate, price, "usd") if total_reward_estimate is not None else None,
        "estimated_total_reward_zar": fiat_value(total_reward_estimate, price, "zar") if total_reward_estimate is not None else None,
        "unique_miners": len(display_clusters),
        "chain_unique_miners": unique_miners,
        "clusters": display_clusters,
        "chain_clusters": enriched_clusters,
        "local_pool_clusters": local_pool_clusters,
        "peer_location": peer_location,
        "fetch_errors": fetch_errors[:20],
        "head_only": False,
        "maintenance_deferred": maintenance_deferred,
        "maintenance_decision": maintenance_decision,
        "deferred_scan": maintenance_deferred,
    }
    if maintenance_deferred:
        payload["error"] = (
            f"global blockchain scan reduced to {requested_count} blocks "
            f"while maintenance is deferred: {maintenance_reason}"
        )
    if isinstance(reward_summary, dict) and reward_summary.get("error"):
        payload["reward_summary_error"] = reward_summary["error"]
    record_global_snapshot(
        {
            "generated_at": payload["updated_at"],
            "latest_block": payload["latest_block"],
            "requested_blocks": payload["requested_blocks"],
            "fetched_blocks": payload["fetched_blocks"],
            "head_only": False,
            "maintenance_deferred": maintenance_deferred,
            "deferred_scan": maintenance_deferred,
            "scan_window_hours": payload["scan_window_hours"],
            "avg_blocks_per_second": payload["avg_blocks_per_second"],
            "max_transactions_per_block": payload["max_transactions_per_block"],
            "max_avg_block_transactions_per_second": payload["max_avg_block_transactions_per_second"],
            "clusters": [
                {
                    "address": cluster["address"],
                    "address_short": cluster["address_short"],
                    "pool_name": cluster.get("pool_name", ""),
                    "pool_label": cluster.get("pool_label", cluster["address_short"]),
                    "source": cluster.get("source", "on-chain"),
                    "local_pool": bool(cluster.get("local_pool")),
                    "estimated_bdag_avg_hour": cluster.get("estimated_bdag_avg_hour"),
                    "estimated_usd_avg_hour": cluster.get("estimated_usd_avg_hour"),
                    "estimated_zar_avg_hour": cluster.get("estimated_zar_avg_hour"),
                    "estimated_bdag_recent_hour": cluster.get("estimated_bdag_recent_hour"),
                    "estimated_usd_recent_hour": cluster.get("estimated_usd_recent_hour"),
                    "estimated_zar_recent_hour": cluster.get("estimated_zar_recent_hour"),
                    "blocks": cluster.get("blocks"),
                    "shares": cluster.get("shares", cluster.get("blocks")),
                    "credit_blocks": cluster.get("credit_blocks", cluster.get("blocks")),
                    "found_blocks": cluster.get("found_blocks", cluster.get("blocks")),
                    "share_percent": cluster.get("share_percent"),
                    "credited_bdag": cluster.get("credited_bdag", cluster.get("estimated_bdag")),
                    "estimated_bdag": cluster.get("estimated_bdag"),
                    "estimated_usd": cluster.get("estimated_usd"),
                    "estimated_zar": cluster.get("estimated_zar"),
                }
                for cluster in display_clusters
            ],
        }
    )
    payload["history"] = read_global_history(limit=GLOBAL_HISTORY_LIMIT)
    payload = refresh_global_chain_head(payload)
    payload = annotate_global_pool_labels(payload)
    write_json_file(GLOBAL_CACHE_FILE, payload, mode=0o600)
    return payload


def collect_global_pool_earnings_window(block_window: int = 600) -> dict[str, Any]:
    """Collect pool production share for a fixed recent block window without touching Global trend cache."""
    requested_window = max(1, int(block_window or 600))
    rpc_sources = global_evm_rpc_urls() or [("local-chain", f"http://127.0.0.1:{NODE_EVM_RPC_PORT}")]
    latest_errors: list[str] = []
    rpc_name = ""
    latest_block: int | None = None
    for source_name, source_url in rpc_sources:
        try:
            latest_hex = json_rpc_call(source_url, "eth_blockNumber", [], timeout=8.0)
            latest_block = int(str(latest_hex), 16)
            rpc_name = source_name
            break
        except Exception as exc:  # noqa: BLE001
            latest_errors.append(f"{source_name}: {exc}")
    if latest_block is None:
        return refresh_global_chain_head(
            {
                "status": "failed",
                "source": "on-chain",
                "updated_at": now_iso(),
                "updated_at_epoch": seconds_since_epoch(),
                "error": "unable to fetch latest global block height from EVM RPC",
                "fetch_errors": latest_errors[:20],
                "requested_blocks": requested_window,
                "fetched_blocks": 0,
                "clusters": [],
            }
        )

    requested_count = min(requested_window, latest_block + 1)
    start_block = max(0, latest_block - requested_count + 1)
    block_numbers = list(range(start_block, latest_block + 1))

    def load_block(block_number: int) -> dict[str, Any]:
        last_error: Exception | None = None
        for source_name, source_url in rpc_sources:
            try:
                header = fetch_block_header(source_url, block_number, timeout=8.0)
                header["_rpc_source"] = source_name
                return header
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        raise RuntimeError(str(last_error or f"failed to fetch block {block_number}"))

    headers: list[dict[str, Any]] = []
    fetch_errors: list[str] = []
    worker_count = adaptive_worker_count("global_pool_earnings_rpc", GLOBAL_RPC_WORKERS, len(block_numbers), {})
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        future_map = {pool.submit(load_block, number): number for number in block_numbers}
        for future in as_completed(future_map):
            number = future_map[future]
            try:
                headers.append(future.result())
            except Exception as exc:  # noqa: BLE001
                fetch_errors.append(f"{number}: {exc}")
    headers.sort(key=lambda item: int(str(item.get("number") or "0"), 16))
    if not headers:
        return refresh_global_chain_head(
            {
                "status": "failed",
                "source": "on-chain",
                "updated_at": now_iso(),
                "updated_at_epoch": seconds_since_epoch(),
                "rpc_source": rpc_name,
                "latest_block": latest_block,
                "requested_blocks": requested_count,
                "fetched_blocks": 0,
                "scan_start_block": start_block,
                "scan_end_block": latest_block,
                "clusters": [],
                "fetch_errors": fetch_errors[:20],
                "error": "unable to fetch block headers for fixed pool earnings window",
            }
        )

    reward_summary = {}
    try:
        reward_summary = pool_db_json(
            """
            SELECT json_build_object(
              'block_count', count(*),
              'avg_reward_wei', COALESCE(avg(reward), 0)::text
            )
            FROM blocks;
            """
        ) or {}
    except Exception as exc:  # noqa: BLE001
        reward_summary = {"error": str(exc)}

    avg_reward_bdag = None if reward_summary.get("error") else wei_to_bdag(reward_summary.get("avg_reward_wei"))
    price = fetch_cmc_price()
    cluster_map: dict[str, dict[str, Any]] = {}
    first_seen_epoch: int | None = None
    last_seen_epoch: int | None = None
    for header in headers:
        miner = str(header.get("miner") or header.get("author") or header.get("coinbase") or "").lower()
        if not miner:
            continue
        epoch = int(str(header.get("timestamp") or "0"), 16)
        height = int(str(header.get("number") or "0"), 16)
        entry = cluster_map.setdefault(
            miner,
            {
                "address": miner,
                "blocks": 0,
                "first_height": height,
                "last_height": height,
                "first_seen_epoch": epoch,
                "last_seen_epoch": epoch,
                "rpc_sources": [],
            },
        )
        entry["blocks"] += 1
        entry["first_height"] = min(entry["first_height"], height)
        entry["last_height"] = max(entry["last_height"], height)
        entry["first_seen_epoch"] = min(entry["first_seen_epoch"], epoch)
        entry["last_seen_epoch"] = max(entry["last_seen_epoch"], epoch)
        entry["rpc_sources"].append(str(header.get("_rpc_source") or rpc_name))
        first_seen_epoch = epoch if first_seen_epoch is None else min(first_seen_epoch, epoch)
        last_seen_epoch = epoch if last_seen_epoch is None else max(last_seen_epoch, epoch)

    total_blocks = len(headers)
    window_seconds = max(1, (last_seen_epoch or 0) - (first_seen_epoch or 0))
    scan_window_hours = Decimal(str(window_seconds)) / Decimal("3600") if window_seconds > 0 else None
    clusters = sorted(cluster_map.values(), key=lambda item: (item["blocks"], item["last_seen_epoch"]), reverse=True)
    enriched_clusters: list[dict[str, Any]] = []
    for rank, cluster in enumerate(clusters, start=1):
        blocks = int(cluster["blocks"])
        share = Decimal(blocks) / Decimal(total_blocks) if total_blocks else Decimal("0")
        est_bdag = avg_reward_bdag * Decimal(blocks) if avg_reward_bdag is not None else None
        est_bdag_hour, est_usd_hour, est_zar_hour = _pool_earning_rates_from_amount(est_bdag, price, scan_window_hours)
        enriched_clusters.append(
            {
                "rank": rank,
                "address": cluster["address"],
                "address_short": short_eth_address(cluster["address"]),
                "blocks": blocks,
                "share_percent": decimal_to_str(share * Decimal("100"), places=2),
                "estimated_bdag": decimal_to_str(est_bdag, places=2) if est_bdag is not None else None,
                "estimated_usd": fiat_value(est_bdag, price, "usd") if est_bdag is not None else None,
                "estimated_zar": fiat_value(est_bdag, price, "zar") if est_bdag is not None else None,
                "estimated_bdag_avg_hour": est_bdag_hour,
                "estimated_usd_avg_hour": est_usd_hour,
                "estimated_zar_avg_hour": est_zar_hour,
                "estimated_bdag_recent_hour": est_bdag_hour,
                "estimated_usd_recent_hour": est_usd_hour,
                "estimated_zar_recent_hour": est_zar_hour,
                "first_seen_at": datetime.fromtimestamp(cluster["first_seen_epoch"], tz=timezone.utc).isoformat(),
                "last_seen_at": datetime.fromtimestamp(cluster["last_seen_epoch"], tz=timezone.utc).isoformat(),
                "rpc_sources": unique_names(cluster["rpc_sources"]),
            }
        )

    local_pool_clusters = collect_local_pool_global_clusters(window_seconds, total_blocks, scan_window_hours, price)
    display_clusters = merge_global_local_pool_clusters(enriched_clusters, local_pool_clusters)
    payload = annotate_global_pool_labels(
        {
            "status": "ok" if not fetch_errors else "degraded",
            "source": "on-chain-fixed-window",
            "updated_at": now_iso(),
            "updated_at_epoch": seconds_since_epoch(),
            "rpc_source": rpc_name,
            "latest_block": latest_block,
            "requested_blocks": requested_count,
            "effective_block_window": requested_count,
            "fetched_blocks": total_blocks,
            "scan_start_block": start_block,
            "scan_end_block": latest_block,
            "scan_window_seconds": window_seconds,
            "scan_window_hours": decimal_to_str(scan_window_hours, places=2) if scan_window_hours is not None else None,
            "unique_miners": len(display_clusters),
            "chain_unique_miners": len(enriched_clusters),
            "clusters": display_clusters,
            "chain_clusters": enriched_clusters,
            "local_pool_clusters": local_pool_clusters,
            "fetch_errors": fetch_errors[:20],
        }
    )
    if isinstance(reward_summary, dict) and reward_summary.get("error"):
        payload["reward_summary_error"] = reward_summary["error"]
    return refresh_global_chain_head(payload)


def collect_wallet_balances(address: str | None = None) -> dict[str, Any]:
    wallet = address or read_env_value("MINING_ADDRESS")
    if not wallet:
        return {"address": None, "sources": []}

    sources: list[dict[str, Any]] = []
    local_sources = local_evm_balance_rpc_urls()
    for name, url in local_sources:
        try:
            balance = json_rpc_balance(url, wallet)
            sources.append({"source": name, "type": "local-rpc", "status": "ok", **balance})
        except Exception as exc:  # noqa: BLE001 - report source failures independently.
            sources.append({"source": name, "type": "local-rpc", "status": "failed", "error": str(exc)})

    for source, base_url in named_urls_from_env("BDAG_EXPLORER_API_URLS", [("bdagscan-api", "https://bdagscan.com")]):
        try:
            balance = explorer_api_balance(base_url, wallet)
            sources.append({"source": source, "type": "explorer-api", "status": "ok", **balance})
        except Exception as exc:  # noqa: BLE001 - explorers may not expose an Etherscan-compatible API.
            sources.append({"source": source, "type": "explorer-api", "status": "failed", "error": str(exc)})

    for source, base_url in named_urls_from_env(
        "BDAG_BLOCKSCOUT_V2_URLS",
        [("blockdag-engineering-v2", "https://explorer.blockdag.engineering")],
    ):
        try:
            balance = blockscout_v2_balance(base_url, wallet)
            sources.append({"source": source, "type": "explorer-api", "status": "ok", **balance})
        except Exception as exc:  # noqa: BLE001
            sources.append({"source": source, "type": "explorer-api", "status": "failed", "error": str(exc)})

    for source, url in named_urls_from_env(
        "BDAG_PUBLIC_RPC_URLS",
        [
            ("bdagscan-rpc", "https://rpc.bdagscan.com"),
            ("blockdag-engineering-rpc", "https://rpc.blockdag.engineering"),
        ],
    ):
        try:
            balance = json_rpc_balance(url, wallet)
            sources.append({"source": source, "type": "public-rpc", "status": "ok", **balance})
        except Exception as exc:  # noqa: BLE001
            sources.append({"source": source, "type": "public-rpc", "status": "failed", "error": str(exc)})

    ok_sources = [source for source in sources if source.get("status") == "ok"]
    local_ok_sources = [source for source in ok_sources if source.get("type") == "local-rpc"]
    primary_sources = local_ok_sources or ok_sources
    primary_values = [int(str(source["wei"])) for source in primary_sources if source.get("wei") is not None]
    local_values = [int(str(source["wei"])) for source in local_ok_sources if source.get("wei") is not None]
    all_values = [int(str(source["wei"])) for source in ok_sources if source.get("wei") is not None]
    tolerance_wei = int(WALLET_MATCH_TOLERANCE_BDAG * WEI_PER_BDAG)

    def spread(values: list[int]) -> int | None:
        return max(values) - min(values) if values else None

    primary_spread = spread(primary_values)
    local_spread = spread(local_values)
    all_spread = spread(all_values)
    return {
        "address": wallet,
        "match": primary_spread is not None and primary_spread <= tolerance_wei,
        "exact_match": len(set(primary_values)) <= 1 and bool(primary_values),
        "all_sources_match": all_spread is not None and all_spread <= tolerance_wei,
        "all_sources_exact_match": len(set(all_values)) <= 1 and bool(all_values),
        "local_match": local_spread is not None and local_spread <= tolerance_wei,
        "local_exact_match": len(set(local_values)) <= 1 and bool(local_values),
        "balance_match_tolerance_bdag": decimal_to_str(WALLET_MATCH_TOLERANCE_BDAG),
        "balance_spread_bdag": decimal_to_str(wei_to_bdag(primary_spread)) if primary_spread is not None else None,
        "all_sources_balance_spread_bdag": decimal_to_str(wei_to_bdag(all_spread)) if all_spread is not None else None,
        "ok_source_count": len(ok_sources),
        "local_evm_rpc": {
            "paused": not LOCAL_EVM_BALANCE_PROBE_ENABLED,
            "reason": None if LOCAL_EVM_BALANCE_PROBE_ENABLED else local_evm_balance_disabled_reason(),
            "skipped_source_count": 0 if LOCAL_EVM_BALANCE_PROBE_ENABLED else len(local_sources),
        },
        "sources": sources,
    }


def wallet_addresses_from_credits(credits: dict[str, Any]) -> list[str]:
    addresses: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        address = str(value or "").strip()
        key = address.lower()
        if not is_spendable_eth_address(address) or key in seen:
            return
        seen.add(key)
        addresses.append(address)

    add(read_env_value("MINING_ADDRESS"))
    for section in ("recent_24h_by_address", "by_address"):
        for item in credits.get(section, []) or []:
            if isinstance(item, dict):
                add(item.get("miner_address"))
    return addresses


def collect_wallet_balances_for_addresses(addresses: list[str]) -> dict[str, Any]:
    unique_addresses: list[str] = []
    seen: set[str] = set()
    for address in addresses:
        key = str(address or "").strip().lower()
        if not is_spendable_eth_address(address) or key in seen:
            continue
        seen.add(key)
        unique_addresses.append(str(address).strip())

    if not unique_addresses:
        return {
            "status": "empty",
            "source_truth": "on-chain eth_getBalance latest",
            "address_count": 0,
            "ok_address_count": 0,
            "total_wei": "0",
            "total_bdag": "0.00",
            "addresses": [],
        }

    local_rpc_sources = [(source, url, "evm-rpc") for source, url in local_evm_balance_rpc_urls()]
    rpc_sources = list(local_rpc_sources)
    rpc_sources.extend(
        (source, url, "public-rpc")
        for source, url in named_urls_from_env(
            "BDAG_PUBLIC_RPC_URLS",
            [
                ("bdagscan-rpc", "https://rpc.bdagscan.com"),
                ("blockdag-engineering-rpc", "https://rpc.blockdag.engineering"),
            ],
        )
    )

    def balance_for_address(address: str) -> dict[str, Any]:
        failures: list[str] = []
        for source, url, source_type in rpc_sources:
            try:
                balance = json_rpc_balance(url, address, timeout=4.0)
                return {
                    "address": address,
                    "address_short": short_eth_address(address),
                    "status": "ok",
                    "source": source,
                    "type": source_type,
                    **balance,
                }
            except Exception as exc:  # noqa: BLE001 - each source is tried independently.
                failures.append(f"{source}: {exc}")
        return {
            "address": address,
            "address_short": short_eth_address(address),
            "status": "failed",
            "error": "; ".join(failures[-2:]) if failures else "no RPC sources configured",
        }

    balances: list[dict[str, Any]] = []
    worker_count = adaptive_worker_count("wallet_balance", 8, len(unique_addresses))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {executor.submit(balance_for_address, address): address for address in unique_addresses}
        for future in as_completed(futures):
            balances.append(future.result())
    balances.sort(key=lambda item: item.get("address", "").lower())

    total_wei = sum(int(str(item.get("wei") or "0")) for item in balances if item.get("status") == "ok")
    ok_count = sum(1 for item in balances if item.get("status") == "ok")
    if ok_count == len(unique_addresses):
        status = "ok"
    elif ok_count > 0:
        status = "partial"
    else:
        status = "failed"

    return {
        "status": status,
        "source_truth": "on-chain eth_getBalance latest",
        "address_count": len(unique_addresses),
        "ok_address_count": ok_count,
        "total_wei": str(total_wei),
        "total_bdag": decimal_to_str(wei_to_bdag(total_wei)),
        "worker_count": worker_count,
        "local_evm_rpc": {
            "paused": not LOCAL_EVM_BALANCE_PROBE_ENABLED,
            "reason": None if LOCAL_EVM_BALANCE_PROBE_ENABLED else local_evm_balance_disabled_reason(),
            "skipped_source_count": 0 if LOCAL_EVM_BALANCE_PROBE_ENABLED else len(local_rpc_sources),
        },
        "addresses": balances,
    }


def parse_chain_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def native_transfer_address(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("hash") or "").lower()
    return str(value or "").lower()


def blockscout_v2_address_transactions(address: str, cutoff_at: datetime, timeout: float = 8.0) -> dict[str, Any]:
    base_url = named_urls_from_env(
        "BDAG_BLOCKSCOUT_V2_URLS",
        [("blockdag-engineering-v2", "https://explorer.blockdag.engineering")],
    )[0][1].rstrip("/")
    encoded_address = urllib.parse.quote(address, safe="")
    params: dict[str, Any] = {}
    items: list[dict[str, Any]] = []
    page_count = 0
    while page_count < 20:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        request = urllib.request.Request(
            f"{base_url}/api/v2/addresses/{encoded_address}/transactions{query}",
            headers={"accept": "application/json", "user-agent": HTTP_USER_AGENT},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read(2_000_000).decode("utf-8", "replace"))
        page_count += 1
        page_items = [item for item in payload.get("items", []) if isinstance(item, dict)]
        stop = False
        for item in page_items:
            parsed_at = parse_chain_timestamp(item.get("timestamp"))
            if parsed_at is not None and parsed_at < cutoff_at:
                stop = True
                continue
            items.append(item)
        next_params = payload.get("next_page_params")
        if stop or not isinstance(next_params, dict) or not next_params:
            break
        params = next_params
    return {"source": base_url, "items": items, "page_count": page_count}


def archive_rpc_urls() -> list[tuple[str, str]]:
    configured = named_urls_from_env("BDAG_ARCHIVE_RPC_URLS", [])
    if configured:
        return configured
    return [
        *named_urls_from_env(
            "BDAG_PUBLIC_RPC_URLS",
            [
                ("bdagscan-rpc", "https://rpc.bdagscan.com"),
                ("blockdag-engineering-rpc", "https://rpc.blockdag.engineering"),
            ],
        ),
        *local_evm_balance_rpc_urls(),
    ]


def rpc_block_timestamp(url: str, block_number: int) -> int:
    header = fetch_block_header(url, block_number)
    return int(str(header.get("timestamp") or "0x0"), 16)


def first_block_at_or_after(url: str, latest_block: int, target_timestamp: int) -> int:
    lo = 0
    hi = latest_block
    while lo < hi:
        mid = (lo + hi) // 2
        if rpc_block_timestamp(url, mid) < target_timestamp:
            lo = mid + 1
        else:
            hi = mid
    return lo


def onchain_cache_key(address: str, hours: int) -> str:
    return f"{address.lower()}:{hours}h"


def collect_onchain_wallet_window_earnings(address: str | None, hours: int = 24) -> dict[str, Any]:
    if not is_spendable_eth_address(address):
        return {"status": "skipped", "error": "no valid mining address", "hours": hours}
    address = str(address).strip()
    if not EARNINGS_ONCHAIN_WINDOW_ENABLED:
        return {
            "status": "skipped",
            "cache_hit": False,
            "generated_at": now_iso(),
            "source": "disabled-by-configuration",
            "source_truth": "on-chain balance window disabled",
            "hours": hours,
            "address": address,
            "earned_bdag": None,
            "local_evm_rpc": {
                "paused": not LOCAL_EVM_BALANCE_PROBE_ENABLED,
                "reason": None if LOCAL_EVM_BALANCE_PROBE_ENABLED else local_evm_balance_disabled_reason(),
            },
            "error": "on-chain wallet window probes are disabled by BDAG_EARNINGS_ONCHAIN_WINDOW_ENABLED=0",
        }
    ensure_runtime()
    key = onchain_cache_key(address, hours)
    cache = read_json_file(EARNINGS_ONCHAIN_CACHE_FILE, {})
    cached = cache.get(key) if isinstance(cache, dict) else None
    now_epoch = seconds_since_epoch()
    if isinstance(cached, dict) and now_epoch - int(cached.get("generated_epoch", 0)) < EARNINGS_ONCHAIN_CACHE_SECONDS:
        return {**cached, "cache_hit": True}

    local_sources = local_evm_balance_rpc_urls()
    latest_sources = local_sources or named_urls_from_env(
        "BDAG_PUBLIC_RPC_URLS",
        [
            ("bdagscan-rpc", "https://rpc.bdagscan.com"),
            ("blockdag-engineering-rpc", "https://rpc.blockdag.engineering"),
        ],
    )
    if not latest_sources:
        return {"status": "failed", "hours": hours, "address": address, "error": "no EVM RPC sources"}
    local_name, local_url = latest_sources[0]
    try:
        latest_block = int(str(json_rpc_call(local_url, "eth_blockNumber", [], timeout=8.0)), 16)
        latest_timestamp = rpc_block_timestamp(local_url, latest_block)
        target_timestamp = latest_timestamp - (hours * 3600)
        start_block = first_block_at_or_after(local_url, latest_block, target_timestamp)
        start_timestamp = rpc_block_timestamp(local_url, start_block)
        latest_balance = json_rpc_balance_at(local_url, address, latest_block, timeout=8.0)
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "hours": hours, "address": address, "source": local_name, "error": str(exc)}

    start_balance = None
    start_source = None
    start_errors: list[str] = []
    for source, url in archive_rpc_urls():
        try:
            start_balance = json_rpc_balance_at(url, address, max(0, start_block - 1), timeout=10.0)
            start_source = source
            break
        except Exception as exc:  # noqa: BLE001
            start_errors.append(f"{source}: {exc}")
    if start_balance is None:
        return {
            "status": "failed",
            "hours": hours,
            "address": address,
            "latest_block": latest_block,
            "start_block": start_block,
            "error": "; ".join(start_errors[-3:]) or "no archive RPC returned historical balance",
        }

    cutoff_at = datetime.fromtimestamp(target_timestamp, timezone.utc)
    incoming_wei = Decimal("0")
    outgoing_wei = Decimal("0")
    fee_wei = Decimal("0")
    incoming_count = 0
    outgoing_count = 0
    transfer_source = None
    try:
        tx_payload = blockscout_v2_address_transactions(address, cutoff_at)
        transfer_source = tx_payload.get("source")
        for tx in tx_payload.get("items", []):
            from_address = native_transfer_address(tx.get("from"))
            to_address = native_transfer_address(tx.get("to"))
            try:
                value_wei = Decimal(str(tx.get("value") or "0"))
            except InvalidOperation:
                value_wei = Decimal("0")
            if to_address == address.lower():
                incoming_wei += value_wei
                incoming_count += 1
            if from_address == address.lower():
                outgoing_wei += value_wei
                outgoing_count += 1
                fee = tx.get("fee")
                fee_value = fee.get("value") if isinstance(fee, dict) else None
                try:
                    fee_wei += Decimal(str(fee_value or "0"))
                except InvalidOperation:
                    pass
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "failed",
            "hours": hours,
            "address": address,
            "latest_block": latest_block,
            "start_block": start_block,
            "error": f"transaction reconciliation failed: {exc}",
        }

    latest_wei = Decimal(str(latest_balance["wei"]))
    start_wei = Decimal(str(start_balance["wei"]))
    earned_wei = latest_wei - start_wei - incoming_wei + outgoing_wei + fee_wei
    payload = {
        "status": "ok",
        "cache_hit": False,
        "generated_at": now_iso(),
        "generated_epoch": now_epoch,
        "source": "on-chain-balance-reconciled-with-native-transfers",
        "hours": hours,
        "address": address,
        "latest_block": latest_block,
        "latest_block_time": datetime.fromtimestamp(latest_timestamp, timezone.utc).isoformat(),
        "latest_balance_source": local_name,
        "start_block": start_block,
        "start_block_time": datetime.fromtimestamp(start_timestamp, timezone.utc).isoformat(),
        "start_balance_source": start_source,
        "transfer_source": transfer_source,
        "local_evm_rpc": {
            "paused": not LOCAL_EVM_BALANCE_PROBE_ENABLED,
            "reason": None if LOCAL_EVM_BALANCE_PROBE_ENABLED else local_evm_balance_disabled_reason(),
            "latest_source_scope": "local-rpc" if local_sources else "public-rpc",
        },
        "balance_start_bdag": decimal_to_str(wei_to_bdag(start_wei)),
        "balance_latest_bdag": decimal_to_str(wei_to_bdag(latest_wei)),
        "net_balance_change_bdag": decimal_to_str(wei_to_bdag(latest_wei - start_wei)),
        "incoming_bdag": decimal_to_str(wei_to_bdag(incoming_wei)),
        "incoming_tx_count": incoming_count,
        "outgoing_bdag": decimal_to_str(wei_to_bdag(outgoing_wei)),
        "outgoing_tx_count": outgoing_count,
        "outgoing_fee_bdag": decimal_to_str(wei_to_bdag(fee_wei), places=6),
        "earned_bdag": decimal_to_str(wei_to_bdag(earned_wei)),
    }
    if not isinstance(cache, dict):
        cache = {}
    cache[key] = payload
    write_json_file(EARNINGS_ONCHAIN_CACHE_FILE, cache, mode=0o600)
    return payload


def fetch_text_url(url: str, headers: dict[str, str], timeout: float = 12.0) -> str:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def fetch_json_url(url: str, headers: dict[str, str] | None = None, timeout: float = 12.0) -> Any:
    return json.loads(fetch_text_url(url, headers or {}, timeout=timeout))


def decimal_or_none(value: Any) -> Decimal | None:
    try:
        if value is None:
            return None
        parsed = Decimal(str(value))
        return parsed if parsed > 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def fetch_bitmart_trade_price(symbol: str = BDAG_BITMART_SYMBOL) -> Decimal:
    url = f"https://www.bitmart.com/trade/{urllib.parse.quote(symbol)}"
    html = fetch_text_url(url, {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "user-agent": HTTP_USER_AGENT})
    patterns = [
        r'lastPrice:"([0-9][0-9,]*(?:\.[0-9]+)?)"',
        r'<title>([0-9][0-9,]*(?:\.[0-9]+)?) \| [^<]+</title>',
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if not match:
            continue
        price = decimal_or_none(match.group(1).replace(",", ""))
        if price is not None:
            return price
    raise RuntimeError("BitMart BDAG price was not parseable")


def fetch_coinstore_trade_price(symbol: str = BDAG_COINSTORE_SYMBOL) -> Decimal:
    url = "https://api.coinstore.com/api/v1/market/tickers"
    payload = fetch_json_url(url, {"accept": "application/json", "user-agent": HTTP_USER_AGENT})
    for row in payload.get("data", []) if isinstance(payload, dict) else []:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol", "")).upper() != symbol.upper():
            continue
        price = decimal_or_none(row.get("close"))
        if price is not None:
            return price
    raise RuntimeError("Coinstore BDAG price was not found in the ticker list")


def fetch_pionex_trade_price(symbol: str = BDAG_PIONEX_SYMBOL) -> Decimal:
    url = f"https://api.pionex.com/api/v1/market/trades?symbol={urllib.parse.quote(symbol)}&limit=1"
    payload = fetch_json_url(url, {"accept": "application/json", "user-agent": HTTP_USER_AGENT})
    trades = payload.get("data", {}).get("trades", []) if isinstance(payload, dict) else []
    if trades:
        price = decimal_or_none(trades[0].get("price"))
        if price is not None:
            return price
    raise RuntimeError("Pionex BDAG price was not found in the trade feed")


def fetch_usd_zar_rate(cached: dict[str, Any] | None = None) -> tuple[Decimal, str | None]:
    warnings: list[str] = []
    try:
        payload = fetch_json_url(USD_ZAR_RATE_URL, {"accept": "application/json", "user-agent": HTTP_USER_AGENT})
        rate = decimal_or_none((payload or {}).get("rates", {}).get("ZAR")) if isinstance(payload, dict) else None
        if rate is None:
            raise RuntimeError("USD/ZAR rate missing from FX payload")
        return rate, None
    except Exception as exc:  # noqa: BLE001
        warnings.append(str(exc))
        if isinstance(cached, dict):
            cached_rate = decimal_or_none(cached.get("usd_zar_rate"))
            if cached_rate is not None:
                return cached_rate, "; ".join(warnings)
        raise


def valid_current_price_cache(cached: Any) -> bool:
    if not isinstance(cached, dict):
        return False
    cached_at = int(cached.get("updated_at_epoch", 0) or 0)
    if cached.get("status") != "ok" or cached.get("source") != "exchange-average":
        return False
    if cached_at <= 0 or seconds_since_epoch() - cached_at > PRICE_CACHE_TTL_SECONDS:
        return False
    if decimal_or_none(cached.get("usd")) is None or decimal_or_none(cached.get("zar")) is None:
        return False
    return True


def fetch_cmc_price() -> dict[str, Any]:
    cached = read_json_file(PRICE_CACHE_FILE, {})
    if valid_current_price_cache(cached):
        return {**cached, "cache_hit": True}

    source_specs = [
        ("coinstore", fetch_coinstore_trade_price),
        ("bitmart", fetch_bitmart_trade_price),
        ("pionex", fetch_pionex_trade_price),
    ]
    sources: list[dict[str, Any]] = []
    worker_count = adaptive_worker_count("price_fetch", len(source_specs), len(source_specs))
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {pool.submit(fetcher): name for name, fetcher in source_specs}
        for future in as_completed(futures):
            name = futures[future]
            try:
                price = future.result()
                sources.append({"source": name, "status": "ok", "price": decimal_to_str(price, places=8)})
            except Exception as exc:  # noqa: BLE001
                sources.append({"source": name, "status": "failed", "error": str(exc)})

    ok_prices = [decimal_value(source.get("price")) for source in sources if source.get("status") == "ok"]
    ok_prices = [price for price in ok_prices if price is not None and price > 0]
    if len(ok_prices) < PRICE_MIN_OK_SOURCES:
        return {
            "status": "failed",
            "source": "exchange-average",
            "error": f"Unable to fetch a current BDAG price from at least {PRICE_MIN_OK_SOURCES} exchange sources",
            "sources": sources,
            "cached": cached or None,
        }

    usd_price = sum(ok_prices) / Decimal(len(ok_prices))
    try:
        usd_zar_rate, fx_warning = fetch_usd_zar_rate(cached if isinstance(cached, dict) else None)
    except Exception as exc:  # noqa: BLE001
        if valid_current_price_cache(cached) and cached.get("usd_zar_rate") is not None:
            usd_zar_rate = decimal_or_none(cached.get("usd_zar_rate")) or Decimal("0")
            fx_warning = str(exc)
        else:
            return {
                "status": "failed",
                "source": "exchange-average",
                "error": f"Unable to fetch USD/ZAR rate: {exc}",
                "sources": sources,
                "cached": cached or None,
            }

    zar_price = usd_price * usd_zar_rate
    price = {
        "status": "ok",
        "source": "exchange-average",
        "updated_at": now_iso(),
        "updated_at_epoch": seconds_since_epoch(),
        "usd": decimal_to_str(usd_price, places=6),
        "zar": decimal_to_str(zar_price, places=6),
        "usd_zar_rate": decimal_to_str(usd_zar_rate, places=6),
        "worker_count": worker_count,
        "sources": sources,
    }
    if fx_warning:
        price["fx_warning"] = fx_warning
    if valid_current_price_cache(cached):
        price["cached"] = {"usd": cached.get("usd"), "zar": cached.get("zar"), "usd_zar_rate": cached.get("usd_zar_rate")}
    write_json_file(PRICE_CACHE_FILE, price, mode=0o600)
    return price


def fiat_value(amount_bdag: Decimal, price: dict[str, Any], currency: str, places: int = 2) -> str | None:
    value = price.get(currency.lower())
    if value is None:
        return None
    try:
        return decimal_to_str(amount_bdag * Decimal(str(value)), places=places)
    except (InvalidOperation, ValueError):
        return None


def collect_miner_hashrate_debug(registry_miners: list[dict[str, Any]], activity_miners: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Collect fast ASIC-reported hashrates for miners currently relevant to the pool."""
    activity_ips = {
        str(item.get("ip") or "")
        for item in activity_miners
        if item.get("ip") and not is_retired_miner_identity(item, str(item.get("ip") or ""))
    }
    candidates: dict[str, dict[str, Any]] = {}
    for registered in registry_miners:
        ip = str(registered.get("ip") or "")
        if not is_lan_ipv4(ip):
            continue
        if is_retired_miner_identity(registered, ip):
            continue
        if not (
            ip in activity_ips
            or registered.get("managed")
            or registered.get("device_type") == "asic"
            or normalize_mac(registered.get("mac"))
        ):
            continue
        candidates[ip] = registered
    if not candidates:
        return {}

    results: dict[str, dict[str, Any]] = {}

    def probe(ip: str) -> tuple[str, dict[str, Any]]:
        try:
            devs = get_miner_cgminer_devs(ip, timeout=MINER_HASHRATE_PROBE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - hashrate is optional plot data.
            return ip, {"available": False, "error": str(exc)}
        return ip, {
            "available": True,
            "hashrate": devs.get("hashrate"),
            "av_hashrate": devs.get("av_hashrate"),
            "accepted": devs.get("accepted"),
            "rejected": devs.get("rejected"),
            "temperature": devs.get("temp"),
            "source": "asic-cgminer-devs",
        }

    worker_count = adaptive_worker_count("miner_hashrate", MINER_HASHRATE_PROBE_WORKERS, len(candidates))
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {pool.submit(probe, ip): ip for ip in candidates}
        for future in as_completed(futures):
            ip, payload = future.result()
            results[ip] = payload
    return results


def collect_miner_earnings_estimates(credit_totals: dict[str, Any], price: dict[str, Any]) -> list[dict[str, Any]]:
    activity = collect_pool_activity(lines=POOL_ACTIVITY_LOG_LINES)
    registry = upsert_pool_activity_miners(activity)
    registry_by_ip = {str(item.get("ip")): item for item in registry.get("miners", []) if item.get("ip")}
    active_activity_miners = [
        item
        for item in activity.get("miners", [])
        if not is_retired_miner_identity(
            {**item, **registry_by_ip.get(str(item.get("ip") or ""), {})},
            str(item.get("ip") or ""),
            normalize_mac((registry_by_ip.get(str(item.get("ip") or ""), {}) or {}).get("mac")),
        )
    ]
    earnings_activity_miners = [
        item
        for item in active_activity_miners
        if is_configured_miner_record(registry_by_ip.get(str(item.get("ip") or ""), {}))
    ]
    configured_registry_miners = [
        item
        for item in registry.get("miners", [])
        if is_configured_miner_record(item)
    ]
    hashrate_by_ip = collect_miner_hashrate_debug(configured_registry_miners, earnings_activity_miners)
    credit_by_address = {
        str(item.get("miner_address")): item
        for item in credit_totals.get("by_address", [])
        if item.get("miner_address")
    }
    worker_to_ips: dict[str, set[str]] = {}
    for item in earnings_activity_miners:
        activity_ip = str(item.get("ip") or "")
        activity_mac = normalize_mac((registry_by_ip.get(activity_ip, {}) or {}).get("mac"))
        if is_docker_bridge_pool_log_client(activity_ip, activity_mac):
            continue
        ip = str(item.get("ip") or "")
        for worker in item.get("workers", []):
            worker_to_ips.setdefault(str(worker), set()).add(ip)
    total_work = sum(int(item.get("share_work", 0) or 0) for item in earnings_activity_miners)
    total_bdag = wei_to_bdag(credit_totals.get("totals", {}).get("total_wei"))
    recent_bdag = wei_to_bdag(credit_totals.get("recent_1h", {}).get("total_wei"))
    estimates: list[dict[str, Any]] = []
    seen_estimate_keys: set[str] = set()
    for item in earnings_activity_miners:
        activity_ip = str(item.get("ip") or "")
        registered = registry_by_ip.get(activity_ip, {})
        registered_mac = normalize_mac(registered.get("mac"))
        if is_docker_bridge_pool_log_client(activity_ip, registered_mac):
            continue
        work = int(item.get("share_work", 0) or 0)
        hashrate = hashrate_by_ip.get(item["ip"], {})
        workers = merge_unique_strings(item.get("workers"), registered.get("last_workers"))
        unique_workers = [worker for worker in workers if len(worker_to_ips.get(worker, set())) <= 1]
        shared_workers = [worker for worker in workers if len(worker_to_ips.get(worker, set())) > 1]
        credit_workers = [worker for worker in unique_workers if worker in credit_by_address]
        credit_rows = [credit_by_address[worker] for worker in credit_workers]
        credited_bdag = sum(((decimal_value(row.get("total_bdag")) or Decimal("0")) for row in credit_rows), Decimal("0"))
        pending_bdag = sum(((decimal_value(row.get("pending_bdag")) or Decimal("0")) for row in credit_rows), Decimal("0"))
        paid_bdag = sum(((decimal_value(row.get("paid_bdag")) or Decimal("0")) for row in credit_rows), Decimal("0"))
        credited_blocks = sum(int(row.get("credit_count", 0) or 0) for row in credit_rows)
        last_credit_at = max([str(row.get("last_credit_at") or "") for row in credit_rows] or [""]) or None
        share = Decimal(work) / Decimal(total_work) if total_work else Decimal("0")
        estimated_total = total_bdag * share
        estimated_hour = recent_bdag * share
        estimate_key = miner_identity_key({**registered, "mac": registered_mac}) or activity_ip
        configured = is_configured_miner_record(registered)
        seen_estimate_keys.add(estimate_key)
        estimates.append(
            {
                "ip": item["ip"],
                "mac": registered_mac,
                "device_id": registered.get("device_id") or (f"mac:{registered_mac}" if registered_mac else ""),
                "identity_key": miner_identity_key({**registered, "mac": registered_mac}),
                "display_name": registered.get("display_name") or "",
                "display_label": miner_display_label({**registered, "mac": registered_mac}),
                "managed": bool(registered.get("managed")),
                "configured": configured,
                "connected": True,
                "earnings_scope": "configured-current-miners",
                "device_type": registered.get("device_type") or item.get("device_type") or "stratum",
                "workers": workers,
                "credit_workers": credit_workers,
                "shared_workers": shared_workers,
                "credit_scope": "unique-workers" if credit_workers else "shared-workers" if shared_workers else "none",
                "credited_blocks": credited_blocks,
                "credited_bdag_total": decimal_to_str(credited_bdag),
                "credited_bdag_paid": decimal_to_str(paid_bdag),
                "credited_bdag_pending": decimal_to_str(pending_bdag),
                "last_credit_at": last_credit_at,
                "shares": item.get("shares", 0),
                "share_work": work,
                "work_percent": percent_to_str(share * Decimal("100")),
                "blocks_found": item.get("blocks_found", 0),
                "hashrate": hashrate.get("hashrate"),
                "av_hashrate": hashrate.get("av_hashrate"),
                "hashrate_ghs": hashrate.get("hashrate"),
                "av_hashrate_ghs": hashrate.get("av_hashrate"),
                "hashrate_available": bool(hashrate.get("available")),
                "hashrate_source": hashrate.get("source", ""),
                "hashrate_error": hashrate.get("error", ""),
                "last_share_at": item.get("last_share_at"),
                "estimated_bdag_total": decimal_to_str(estimated_total),
                "estimated_usd_total": fiat_value(estimated_total, price, "usd"),
                "estimated_zar_total": fiat_value(estimated_total, price, "zar"),
                "estimated_bdag_avg_hour": decimal_to_str(estimated_hour),
                "estimated_usd_avg_hour": fiat_value(estimated_hour, price, "usd"),
                "estimated_zar_avg_hour": fiat_value(estimated_hour, price, "zar"),
                "estimated_bdag_1h": decimal_to_str(estimated_hour),
                "estimated_usd_1h": fiat_value(estimated_hour, price, "usd"),
                "estimated_zar_1h": fiat_value(estimated_hour, price, "zar"),
            }
        )
    for registered in registry.get("miners", []):
        ip = str(registered.get("ip") or "")
        registered_mac = normalize_mac(registered.get("mac"))
        if not is_configured_miner_record(registered):
            continue
        if registered.get("device_type") != "asic" or not is_lan_ipv4(ip):
            continue
        if is_retired_miner_identity(registered, ip, registered_mac):
            continue
        estimate_key = miner_identity_key({**registered, "mac": registered_mac}) or ip
        if estimate_key in seen_estimate_keys:
            continue
        hashrate = hashrate_by_ip.get(ip, {})
        estimates.append(
            {
                "ip": ip,
                "mac": registered_mac,
                "device_id": registered.get("device_id") or (f"mac:{registered_mac}" if registered_mac else ""),
                "identity_key": estimate_key,
                "display_name": registered.get("display_name") or "",
                "display_label": miner_display_label({**registered, "mac": registered_mac}),
                "managed": bool(registered.get("managed")),
                "configured": True,
                "connected": False,
                "earnings_scope": "configured-current-miners",
                "device_type": "asic",
                "workers": merge_unique_strings(registered.get("last_workers")),
                "credit_workers": [],
                "shared_workers": [],
                "credit_scope": "idle-registered-asic",
                "credited_blocks": 0,
                "credited_bdag_total": "0",
                "credited_bdag_paid": "0",
                "credited_bdag_pending": "0",
                "last_credit_at": None,
                "shares": 0,
                "share_work": 0,
                "work_percent": "0.00",
                "blocks_found": 0,
                "hashrate": hashrate.get("hashrate"),
                "av_hashrate": hashrate.get("av_hashrate"),
                "hashrate_ghs": hashrate.get("hashrate"),
                "av_hashrate_ghs": hashrate.get("av_hashrate"),
                "hashrate_available": bool(hashrate.get("available")),
                "hashrate_source": hashrate.get("source", ""),
                "hashrate_error": hashrate.get("error", ""),
                "last_share_at": registered.get("last_share_at"),
                "estimated_bdag_total": "0",
                "estimated_usd_total": fiat_value(Decimal("0"), price, "usd"),
                "estimated_zar_total": fiat_value(Decimal("0"), price, "zar"),
                "estimated_bdag_avg_hour": "0",
                "estimated_usd_avg_hour": fiat_value(Decimal("0"), price, "usd"),
                "estimated_zar_avg_hour": fiat_value(Decimal("0"), price, "zar"),
                "estimated_bdag_1h": "0",
                "estimated_usd_1h": fiat_value(Decimal("0"), price, "usd"),
                "estimated_zar_1h": fiat_value(Decimal("0"), price, "zar"),
            }
        )
    return estimates


def read_earnings_history(limit: int | None = None) -> list[dict[str, Any]]:
    if not EARNINGS_SNAPSHOT_FILE.exists():
        return []
    selected: list[str] | deque[str]
    try:
        if limit is None:
            selected = []
            with EARNINGS_SNAPSHOT_FILE.open("r", encoding="utf-8") as handle:
                for line in handle:
                    selected.append(line)
        else:
            selected = deque(maxlen=max(0, limit))
            with EARNINGS_SNAPSHOT_FILE.open("r", encoding="utf-8") as handle:
                for line in handle:
                    selected.append(line)
    except OSError:
        return []
    history: list[dict[str, Any]] = []
    for line in selected:
        try:
            history.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return history


def _earnings_history_bucket_seconds(age_seconds: float) -> int:
    if age_seconds <= DASHBOARD_HISTORY_HOT_SECONDS:
        return DASHBOARD_HISTORY_HOT_STEP_SECONDS
    if age_seconds <= DASHBOARD_HISTORY_HOURLY_SECONDS:
        return DASHBOARD_HISTORY_HOURLY_STEP_SECONDS
    if age_seconds <= DASHBOARD_HISTORY_DAILY_SECONDS:
        return DASHBOARD_HISTORY_DAILY_STEP_SECONDS
    return DASHBOARD_HISTORY_WEEKLY_STEP_SECONDS


def compact_miner_estimate_for_history(miner: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "ip",
        "mac",
        "device_id",
        "identity_key",
        "display_name",
        "display_label",
        "managed",
        "configured",
        "connected",
        "earnings_scope",
        "credit_scope",
        "shares",
        "share_work",
        "work_percent",
        "blocks_found",
        "hashrate",
        "av_hashrate",
        "hashrate_ghs",
        "av_hashrate_ghs",
        "hashrate_available",
        "hashrate_source",
        "estimated_bdag_avg_hour",
        "estimated_bdag_1h",
        "estimated_usd_avg_hour",
        "estimated_usd_1h",
        "estimated_zar_avg_hour",
        "estimated_zar_1h",
        "estimated_wallet_bdag_recent_hour",
        "estimated_wallet_bdag_avg_hour",
        "estimated_wallet_bdag_1h",
        "estimated_wallet_usd_recent_hour",
        "estimated_wallet_usd_avg_hour",
        "estimated_wallet_usd_1h",
        "estimated_wallet_zar_recent_hour",
        "estimated_wallet_zar_avg_hour",
        "estimated_wallet_zar_1h",
    ]
    return {key: miner.get(key) for key in keys if key in miner}


def earnings_snapshot_has_plot_data(snapshot: dict[str, Any]) -> bool:
    miners = snapshot.get("miner_estimates")
    return isinstance(miners, list) and any(
        isinstance(miner, dict) and is_earnings_wallet_miner(miner)
        for miner in miners
    )


def read_latest_earnings_snapshot_info(max_tail_bytes: int = 2 * 1024 * 1024) -> dict[str, Any]:
    info: dict[str, Any] = {
        "path": str(EARNINGS_SNAPSHOT_FILE),
        "exists": EARNINGS_SNAPSHOT_FILE.exists(),
        "file_size_bytes": 0,
        "latest_at": None,
        "latest_epoch": None,
        "latest_has_plot_data": False,
        "latest_any_at": None,
        "latest_any_epoch": None,
        "tail_lines_scanned": 0,
        "error": "",
    }
    if not EARNINGS_SNAPSHOT_FILE.exists():
        info["error"] = "snapshot file does not exist"
        return info
    try:
        size = EARNINGS_SNAPSHOT_FILE.stat().st_size
        info["file_size_bytes"] = size
        start = max(0, size - max(4096, int(max_tail_bytes)))
        with EARNINGS_SNAPSHOT_FILE.open("rb") as handle:
            handle.seek(start)
            if start > 0:
                handle.readline()
            lines = handle.readlines()
    except OSError as exc:
        info["error"] = str(exc)
        return info

    info["tail_lines_scanned"] = len(lines)
    for raw in reversed(lines):
        try:
            snapshot = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if not isinstance(snapshot, dict):
            continue
        parsed = parse_earnings_timestamp(snapshot.get("generated_at"))
        if parsed is None:
            continue
        epoch = parsed.timestamp()
        if info["latest_any_epoch"] is None:
            info["latest_any_at"] = parsed.strftime("%Y-%m-%dT%H:%M:%S%z")
            info["latest_any_epoch"] = epoch
        if earnings_snapshot_has_plot_data(snapshot):
            info["latest_at"] = parsed.strftime("%Y-%m-%dT%H:%M:%S%z")
            info["latest_epoch"] = epoch
            info["latest_has_plot_data"] = True
            return info
    if info["latest_any_epoch"] is not None:
        info["error"] = "tail contains snapshots but none with miner plot data"
    else:
        info["error"] = "no valid snapshots found in file tail"
    return info


def compact_earnings_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    credit_balance = snapshot.get("credit_balance_check") if isinstance(snapshot.get("credit_balance_check"), dict) else {}
    return {
        "generated_at": snapshot.get("generated_at"),
        "total_bdag": snapshot.get("total_bdag"),
        "credit_balance_check": {
            "wallet_bdag": credit_balance.get("wallet_bdag"),
        },
        "miner_estimates": [
            compact_miner_estimate_for_history(miner)
            for miner in snapshot.get("miner_estimates") or []
            if isinstance(miner, dict)
            and is_earnings_wallet_miner(miner)
        ],
    }


def compact_earnings_history_for_dashboard(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    timed: list[tuple[float, dict[str, Any]]] = []
    for snapshot in history:
        if not isinstance(snapshot, dict):
            continue
        if not earnings_snapshot_has_plot_data(snapshot):
            continue
        parsed = parse_earnings_timestamp(snapshot.get("generated_at"))
        if parsed is None:
            continue
        timed.append((parsed.timestamp(), snapshot))
    if not timed:
        return []

    timed.sort(key=lambda item: item[0])
    latest_epoch = timed[-1][0]
    cutoff = latest_epoch - max(3600, EARNINGS_DASHBOARD_HISTORY_SECONDS)
    anchor: tuple[float, dict[str, Any]] | None = None
    buckets: dict[tuple[int, int], tuple[float, dict[str, Any]]] = {}
    for epoch, snapshot in timed:
        if epoch < cutoff:
            anchor = (epoch, snapshot)
            continue
        age = max(0.0, latest_epoch - epoch)
        bucket_seconds = _earnings_history_bucket_seconds(age)
        bucket_key = (bucket_seconds, int(epoch // bucket_seconds))
        existing = buckets.get(bucket_key)
        if existing is None or epoch >= existing[0]:
            buckets[bucket_key] = (epoch, snapshot)

    selected = list(buckets.values())
    if anchor is not None:
        selected.append(anchor)
    selected.sort(key=lambda item: item[0])
    return [compact_earnings_snapshot(snapshot) for _, snapshot in selected]


def read_compact_earnings_history_for_dashboard() -> tuple[list[dict[str, Any]], int]:
    return read_dashboard_history(
        "earnings",
        EARNINGS_SNAPSHOT_FILE,
        compact_earnings_snapshot,
        earnings_snapshot_has_plot_data,
    )


def format_earnings_history_timestamp(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = parse_earnings_timestamp(text)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")


def miner_template_by_worker(miner_estimates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for miner in miner_estimates:
        if not isinstance(miner, dict):
            continue
        workers = merge_unique_strings(miner.get("credit_workers"), miner.get("workers"))
        for worker in workers:
            if worker:
                mapping[str(worker).lower()] = miner
    return mapping


def derived_credit_history_for_dashboard(
    price: dict[str, Any],
    miner_estimates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not EARNINGS_DERIVED_HISTORY_ENABLED:
        return []
    bucket_seconds = max(60, int(EARNINGS_DERIVED_HISTORY_BUCKET_SECONDS))
    retention_seconds = max(3600, int(EARNINGS_DASHBOARD_HISTORY_SECONDS))
    sql = f"""
    WITH params AS (
      SELECT
        now() - interval '{retention_seconds} seconds' AS start_at,
        interval '{bucket_seconds} seconds' AS bucket_width
    ),
    prior AS (
      SELECT COALESCE(sum(amount), 0) AS total_wei
      FROM credits, params
      WHERE created_at < params.start_at
    ),
    bucketed AS (
      SELECT
        date_bin(params.bucket_width, created_at, timestamp '2000-01-01') AS bucket_at,
        miner_address,
        count(*) AS credit_count,
        COALESCE(sum(amount), 0) AS total_wei
      FROM credits, params
      WHERE created_at >= params.start_at
      GROUP BY bucket_at, miner_address
    ),
    bucket_totals AS (
      SELECT bucket_at, COALESCE(sum(total_wei), 0) AS bucket_wei
      FROM bucketed
      GROUP BY bucket_at
    ),
    running AS (
      SELECT
        bucket_at,
        (SELECT total_wei FROM prior)
          + COALESCE(sum(bucket_wei) OVER (ORDER BY bucket_at ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), 0)
          AS cumulative_total_wei
      FROM bucket_totals
    )
    SELECT COALESCE(json_agg(row_to_json(t) ORDER BY bucket_at, miner_address), '[]'::json)
    FROM (
      SELECT
        b.bucket_at::text AS bucket_at,
        b.miner_address,
        b.credit_count,
        b.total_wei::text AS total_wei,
        r.cumulative_total_wei::text AS cumulative_total_wei
      FROM bucketed b
      JOIN running r USING (bucket_at)
      ORDER BY b.bucket_at, b.miner_address
    ) t;
    """
    try:
        rows = pool_db_json(sql)
    except Exception:
        return []
    if not isinstance(rows, list):
        return []

    by_worker = miner_template_by_worker(miner_estimates)
    bucket_hours = Decimal(str(bucket_seconds)) / Decimal("3600")
    grouped: dict[str, dict[str, Any]] = {}
    bucket_totals: dict[str, Decimal] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        generated_at = format_earnings_history_timestamp(row.get("bucket_at"))
        if not generated_at:
            continue
        amount_bdag = wei_to_bdag(row.get("total_wei"))
        bucket_totals[generated_at] = bucket_totals.get(generated_at, Decimal("0")) + amount_bdag
        snapshot = grouped.setdefault(
            generated_at,
            {
                "generated_at": generated_at,
                "total_bdag": decimal_to_str(wei_to_bdag(row.get("cumulative_total_wei"))),
                "credit_balance_check": {"wallet_bdag": None},
                "miner_estimates": [],
                "history_source": "pool-db-derived-credits",
                "bucket_seconds": bucket_seconds,
            },
        )
        address = str(row.get("miner_address") or "")
        template = by_worker.get(address.lower(), {})
        rate_bdag = amount_bdag / bucket_hours if bucket_hours > 0 else Decimal("0")
        miner = {
            "ip": template.get("ip") or "",
            "mac": template.get("mac") or "",
            "device_id": template.get("device_id") or "",
            "display_name": template.get("display_name") or "",
            "device_type": template.get("device_type") or "chain-derived",
            "workers": [address] if address else [],
            "credit_workers": [address] if address else [],
            "credit_scope": "pool-db-derived",
            "shares": None,
            "share_work": None,
            "blocks_found": int(row.get("credit_count", 0) or 0),
            "estimated_bdag_avg_hour": decimal_to_str(rate_bdag),
            "estimated_bdag_1h": decimal_to_str(rate_bdag),
            "estimated_usd_avg_hour": fiat_value(rate_bdag, price, "usd"),
            "estimated_usd_1h": fiat_value(rate_bdag, price, "usd"),
            "estimated_zar_avg_hour": fiat_value(rate_bdag, price, "zar"),
            "estimated_zar_1h": fiat_value(rate_bdag, price, "zar"),
            "history_source": "pool-db-derived-credits",
        }
        for key in ("hashrate", "av_hashrate", "hashrate_ghs", "av_hashrate_ghs", "hashrate_available", "hashrate_source"):
            if key in template:
                miner[key] = template.get(key)
        snapshot["miner_estimates"].append(miner)

    for generated_at, snapshot in grouped.items():
        total = bucket_totals.get(generated_at, Decimal("0"))
        if total <= 0:
            continue
        for miner in snapshot.get("miner_estimates", []):
            address_total = Decimal("0")
            try:
                rate = Decimal(str(miner.get("estimated_bdag_avg_hour") or "0"))
                address_total = rate * bucket_hours
            except (InvalidOperation, ValueError):
                pass
            miner["work_percent"] = percent_to_str((address_total / total) * Decimal("100")) if address_total > 0 else "0.00"

    return [grouped[key] for key in sorted(grouped)]


def merge_earnings_history(actual: list[dict[str, Any]], derived: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not derived:
        return actual
    actual_epochs = [
        parsed.timestamp()
        for parsed in (parse_earnings_timestamp(item.get("generated_at")) for item in actual if isinstance(item, dict))
        if parsed is not None
    ]
    skip_seconds = max(EARNINGS_SNAPSHOT_EXPECTED_INTERVAL_SECONDS * 2, EARNINGS_DERIVED_HISTORY_BUCKET_SECONDS // 2)
    merged = list(actual)
    for snapshot in derived:
        parsed = parse_earnings_timestamp(snapshot.get("generated_at")) if isinstance(snapshot, dict) else None
        if parsed is None:
            continue
        epoch = parsed.timestamp()
        if any(abs(epoch - actual_epoch) <= skip_seconds for actual_epoch in actual_epochs):
            continue
        merged.append(snapshot)
    return compact_earnings_history_for_dashboard(merged)


def parse_earnings_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+0000"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?", text):
        try:
            return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def decimal_value(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def collect_hourly_averages(
    history: list[dict[str, Any]],
    current_total_bdag: Decimal,
    current_recent_bdag: Decimal,
    current_miner_estimates: list[dict[str, Any]],
    current_wallet_bdag: Decimal | None = None,
    pool_started_at: Any = None,
    current_earned_24h_bdag: Decimal | None = None,
    current_earned_24h_source: str | None = None,
) -> dict[str, Any]:
    current_snapshot = {
        "generated_at": now_iso(),
        "total_bdag": decimal_to_str(current_total_bdag),
        "credit_balance_check": {
            "wallet_bdag": decimal_to_str(current_wallet_bdag) if current_wallet_bdag is not None else None,
        },
        "miner_estimates": current_miner_estimates,
    }
    snapshots = history + [current_snapshot]
    timed_totals: list[tuple[datetime, Decimal, str]] = []
    timed_wallets: list[tuple[datetime, Decimal, str]] = []
    miner_rate_samples: dict[str, list[Decimal]] = {}

    for snapshot in snapshots:
        parsed_at = parse_earnings_timestamp(snapshot.get("generated_at"))
        total = decimal_value(snapshot.get("total_bdag"))
        if parsed_at is not None and total is not None:
            timed_totals.append((parsed_at, total, str(snapshot.get("generated_at"))))
        wallet_total = decimal_value((snapshot.get("credit_balance_check") or {}).get("wallet_bdag"))
        if parsed_at is not None and wallet_total is not None:
            timed_wallets.append((parsed_at, wallet_total, str(snapshot.get("generated_at"))))
        for miner in snapshot.get("miner_estimates") or []:
            if not isinstance(miner, dict) or not is_earnings_wallet_miner(miner):
                continue
            key = str(
                miner.get("identity_key")
                or miner.get("device_id")
                or (f"mac:{normalize_mac(miner.get('mac'))}" if normalize_mac(miner.get("mac")) else "")
                or miner.get("ip")
                or ""
            )
            if not key:
                continue
            value = decimal_value(miner.get("estimated_bdag_avg_hour") or miner.get("estimated_bdag_1h"))
            if value is None:
                continue
            miner_rate_samples.setdefault(key, []).append(value)

    tracked_avg_bdag_hour = None
    tracked_hours = None
    window_started_at = None
    window_ended_at = None
    if len(timed_totals) >= 2:
        timed_totals.sort(key=lambda item: item[0])
        first_at, first_total, first_label = timed_totals[0]
        last_at, last_total, last_label = timed_totals[-1]
        elapsed_seconds = Decimal(str((last_at - first_at).total_seconds()))
        if elapsed_seconds > 0:
            tracked_hours = elapsed_seconds / Decimal("3600")
            tracked_avg_bdag_hour = (last_total - first_total) / tracked_hours
            window_started_at = first_label
            window_ended_at = last_label

    wallet_tracked_avg_bdag_hour = None
    wallet_recent_bdag_hour = None
    wallet_24h_bdag = None
    wallet_24h_source = "wallet-balance-history"
    wallet_net_recent_bdag_hour = None
    wallet_net_24h_bdag = None
    wallet_runtime_avg_bdag_hour = None
    wallet_runtime_hours = None
    wallet_window_started_at = None
    wallet_window_ended_at = None
    if len(timed_wallets) >= 2:
        timed_wallets.sort(key=lambda item: item[0])
        if current_wallet_bdag is None:
            current_wallet_bdag = timed_wallets[-1][1]
        first_at, first_total, first_label = timed_wallets[0]
        last_at, last_total, last_label = timed_wallets[-1]
        elapsed_seconds = Decimal(str((last_at - first_at).total_seconds()))
        if elapsed_seconds > 0:
            wallet_tracked_avg_bdag_hour = (last_total - first_total) / (elapsed_seconds / Decimal("3600"))
            wallet_runtime_hours = elapsed_seconds / Decimal("3600")
            wallet_window_started_at = first_label
            wallet_window_ended_at = last_label
        one_hour_ago = last_at.timestamp() - 3600
        recent_candidates = [item for item in timed_wallets if item[0].timestamp() <= one_hour_ago]
        if recent_candidates:
            recent_at, recent_total, _ = recent_candidates[-1]
            recent_elapsed = Decimal(str((last_at - recent_at).total_seconds()))
            if recent_elapsed > 0:
                wallet_recent_bdag_hour = (last_total - recent_total) / (recent_elapsed / Decimal("3600"))
        if wallet_recent_bdag_hour is not None:
            if wallet_recent_bdag_hour < 0:
                wallet_recent_bdag_hour = current_recent_bdag if current_recent_bdag > 0 else Decimal("0")
            elif current_recent_bdag > 0 and wallet_recent_bdag_hour > current_recent_bdag * Decimal("3"):
                wallet_recent_bdag_hour = current_recent_bdag
        day_candidates = [item for item in timed_wallets if item[0].timestamp() <= last_at.timestamp() - 86400]
        if day_candidates:
            day_at, day_total, _ = day_candidates[-1]
            day_elapsed = Decimal(str((last_at - day_at).total_seconds()))
            if day_elapsed > 0:
                wallet_24h_bdag = last_total - day_total
        if wallet_24h_bdag is None and current_wallet_bdag is not None:
            wallet_24h_bdag = current_wallet_bdag

    if current_wallet_bdag is not None and wallet_runtime_hours is not None and wallet_runtime_hours < Decimal("24"):
        wallet_24h_bdag = current_wallet_bdag

    wallet_net_recent_bdag_hour = wallet_recent_bdag_hour
    wallet_net_24h_bdag = wallet_24h_bdag
    if current_earned_24h_bdag is not None:
        wallet_24h_bdag = current_earned_24h_bdag
        wallet_recent_bdag_hour = current_recent_bdag
        wallet_24h_source = current_earned_24h_source or "pool-db-credits-24h"

    wallet_24h_avg_bdag_hour = (wallet_24h_bdag / Decimal("24")) if wallet_24h_bdag is not None else None

    parsed_pool_started_at = parse_earnings_timestamp(pool_started_at)
    if parsed_pool_started_at is not None:
        elapsed_seconds = Decimal(str((datetime.now(timezone.utc) - parsed_pool_started_at).total_seconds()))
        if elapsed_seconds > 0:
            wallet_runtime_hours = elapsed_seconds / Decimal("3600")
            wallet_runtime_avg_bdag_hour = current_total_bdag / wallet_runtime_hours

    miners: dict[str, dict[str, Any]] = {}
    for ip, values in miner_rate_samples.items():
        miners[ip] = {
            "avg_bdag_hour": decimal_to_str(sum(values) / Decimal(len(values))),
            "samples": len(values),
        }

    return {
        "recent_bdag_hour": decimal_to_str(current_recent_bdag),
        "tracked_avg_bdag_hour": decimal_to_str(tracked_avg_bdag_hour) if tracked_avg_bdag_hour is not None else None,
        "tracked_hours": decimal_to_str(tracked_hours, places=2) if tracked_hours is not None else None,
        "sample_count": len(timed_totals),
        "window_started_at": window_started_at,
        "window_ended_at": window_ended_at,
        "wallet_bdag": decimal_to_str(current_wallet_bdag) if current_wallet_bdag is not None else None,
        "wallet_avg_bdag_hour_since_pool_start": decimal_to_str(wallet_runtime_avg_bdag_hour) if wallet_runtime_avg_bdag_hour is not None else None,
        "wallet_runtime_hours": decimal_to_str(wallet_runtime_hours, places=2) if wallet_runtime_hours is not None else None,
        "wallet_recent_bdag_hour": decimal_to_str(wallet_recent_bdag_hour) if wallet_recent_bdag_hour is not None else None,
        "wallet_24h_bdag": decimal_to_str(wallet_24h_bdag) if wallet_24h_bdag is not None else None,
        "wallet_24h_avg_bdag_hour": decimal_to_str(wallet_24h_avg_bdag_hour) if wallet_24h_avg_bdag_hour is not None else None,
        "wallet_24h_source": wallet_24h_source,
        "wallet_net_recent_bdag_hour": decimal_to_str(wallet_net_recent_bdag_hour) if wallet_net_recent_bdag_hour is not None else None,
        "wallet_net_24h_bdag": decimal_to_str(wallet_net_24h_bdag) if wallet_net_24h_bdag is not None else None,
        "wallet_tracked_avg_bdag_hour": decimal_to_str(wallet_tracked_avg_bdag_hour) if wallet_tracked_avg_bdag_hour is not None else None,
        "wallet_window_started_at": wallet_window_started_at,
        "wallet_window_ended_at": wallet_window_ended_at,
        "miners": miners,
    }


def collect_earnings(include_history: bool = True) -> dict[str, Any]:
    try:
        credits = collect_credit_totals()
    except Exception as exc:  # noqa: BLE001
        credits = {"error": str(exc), "totals": {"total_wei": "0", "total_bdag": "0"}}
    price = fetch_cmc_price()
    primary_mining_address = read_env_value("MINING_ADDRESS")
    wallet = collect_wallet_balances(primary_mining_address)
    miner_estimates = (
        [miner for miner in collect_miner_earnings_estimates(credits, price) if is_earnings_wallet_miner(miner)]
        if "error" not in credits
        else []
    )
    total_bdag = wei_to_bdag(credits.get("totals", {}).get("total_wei"))
    db_recent_bdag = wei_to_bdag(credits.get("recent_1h", {}).get("total_wei"))
    db_recent_24h_bdag = wei_to_bdag(
        credits.get("recent_24h", {}).get("wallet_total_wei")
        or credits.get("recent_24h", {}).get("total_wei")
    )
    onchain_24h = collect_onchain_wallet_window_earnings(primary_mining_address, hours=24)
    onchain_1h = collect_onchain_wallet_window_earnings(primary_mining_address, hours=1)
    recent_24h_bdag = decimal_value(onchain_24h.get("earned_bdag")) if onchain_24h.get("status") == "ok" else None
    if recent_24h_bdag is None:
        recent_24h_bdag = db_recent_24h_bdag
    recent_bdag = decimal_value(onchain_1h.get("earned_bdag")) if onchain_1h.get("status") == "ok" else None
    if recent_bdag is None:
        recent_bdag = db_recent_bdag
    payment_wallet_addresses = [primary_mining_address] if is_spendable_eth_address(primary_mining_address) else []
    payment_wallet_balance = collect_wallet_balances_for_addresses(payment_wallet_addresses)
    credit_wallet_balance = collect_wallet_balances_for_addresses(wallet_addresses_from_credits(credits)) if "error" not in credits else {
        "status": "failed",
        "source_truth": "on-chain eth_getBalance latest",
        "address_count": 0,
        "ok_address_count": 0,
        "total_wei": "0",
        "total_bdag": None,
        "addresses": [],
        "error": credits.get("error"),
    }
    wallet_balance = payment_wallet_balance
    wallet["payment"] = payment_wallet_balance
    wallet["aggregate"] = credit_wallet_balance
    wallet_bdag = None
    if wallet_balance.get("ok_address_count", 0) > 0:
        wallet_bdag = decimal_value(wallet_balance.get("total_bdag"))
    for source in wallet.get("sources", []):
        if wallet_bdag is None and source.get("status") == "ok" and source.get("bdag") is not None:
            wallet_bdag = Decimal(str(source["bdag"]))
            break
    if include_history:
        history, history_sample_count = read_compact_earnings_history_for_dashboard()
    else:
        history, history_sample_count = [], 0
    derived_history = derived_credit_history_for_dashboard(price, miner_estimates) if include_history else []
    if derived_history:
        history = merge_earnings_history(history, derived_history)
    hourly_averages = collect_hourly_averages(
        history,
        total_bdag,
        recent_bdag,
        miner_estimates,
        wallet_bdag,
        credits.get("totals", {}).get("first_credit_at"),
        current_earned_24h_bdag=recent_24h_bdag,
        current_earned_24h_source=onchain_24h.get("source") if onchain_24h.get("status") == "ok" else "pool-db-credits-24h",
    )
    wallet_runtime_avg = decimal_value(hourly_averages.get("wallet_avg_bdag_hour_since_pool_start"))
    wallet_recent_avg = decimal_value(hourly_averages.get("wallet_recent_bdag_hour")) or wallet_runtime_avg
    for miner in miner_estimates:
        tracked_key = str(
            miner.get("identity_key")
            or miner.get("device_id")
            or (f"mac:{normalize_mac(miner.get('mac'))}" if normalize_mac(miner.get("mac")) else "")
            or miner.get("ip")
            or ""
        )
        tracked = hourly_averages.get("miners", {}).get(tracked_key) or hourly_averages.get("miners", {}).get(str(miner.get("ip") or ""))
        if tracked:
            miner["tracked_avg_bdag_hour"] = tracked["avg_bdag_hour"]
            miner["tracked_avg_samples"] = tracked["samples"]

    tracked_miner_total = sum(
        (decimal_value(miner.get("tracked_avg_bdag_hour")) or Decimal("0")) for miner in miner_estimates
    )

    for miner in miner_estimates:
        estimated_total = decimal_value(miner.get("estimated_bdag_total"))
        estimated_hour = decimal_value(miner.get("estimated_bdag_avg_hour") or miner.get("estimated_bdag_1h"))
        tracked_hour = decimal_value(miner.get("tracked_avg_bdag_hour"))

        if wallet_bdag is not None and total_bdag > 0 and estimated_total is not None:
            estimated_wallet_total = (estimated_total / total_bdag) * wallet_bdag
            miner["estimated_wallet_bdag_total"] = decimal_to_str(estimated_wallet_total)
            miner["estimated_wallet_usd_total"] = fiat_value(estimated_wallet_total, price, "usd")
            miner["estimated_wallet_zar_total"] = fiat_value(estimated_wallet_total, price, "zar")
        if wallet_recent_avg is not None and db_recent_bdag > 0 and estimated_hour is not None:
            estimated_wallet_recent_hour = (estimated_hour / db_recent_bdag) * wallet_recent_avg
            miner["estimated_wallet_bdag_recent_hour"] = decimal_to_str(estimated_wallet_recent_hour)
            miner["estimated_wallet_usd_recent_hour"] = fiat_value(estimated_wallet_recent_hour, price, "usd")
            miner["estimated_wallet_zar_recent_hour"] = fiat_value(estimated_wallet_recent_hour, price, "zar")
        if wallet_runtime_avg is not None:
            if tracked_miner_total > 0 and tracked_hour is not None:
                estimated_wallet_avg_hour = (tracked_hour / tracked_miner_total) * wallet_runtime_avg
            elif recent_bdag > 0 and estimated_hour is not None:
                estimated_wallet_avg_hour = (estimated_hour / recent_bdag) * wallet_runtime_avg
            else:
                estimated_wallet_avg_hour = None
            if estimated_wallet_avg_hour is not None:
                miner["estimated_wallet_bdag_avg_hour"] = decimal_to_str(estimated_wallet_avg_hour)
                miner["estimated_wallet_usd_avg_hour"] = fiat_value(estimated_wallet_avg_hour, price, "usd")
                miner["estimated_wallet_zar_avg_hour"] = fiat_value(estimated_wallet_avg_hour, price, "zar")
    credit_balance_check = {
        "source_truth": wallet_balance.get("source_truth", "on-chain eth_getBalance latest for payment wallet"),
        "wallet_scope": "payment-wallet",
        "payment_wallet_address": primary_mining_address,
        "wallet_status": wallet_balance.get("status"),
        "wallet_address_count": wallet_balance.get("address_count"),
        "wallet_ok_address_count": wallet_balance.get("ok_address_count"),
        "credit_address_wallet_status": credit_wallet_balance.get("status"),
        "credit_address_wallet_count": credit_wallet_balance.get("address_count"),
        "credit_address_wallet_ok_count": credit_wallet_balance.get("ok_address_count"),
        "credit_address_wallet_bdag": credit_wallet_balance.get("total_bdag"),
        "credited_bdag": decimal_to_str(total_bdag),
        "lifetime_credited_bdag": decimal_to_str(total_bdag),
        "wallet_bdag": decimal_to_str(wallet_bdag) if wallet_bdag is not None else None,
        "actual_wallet_bdag": decimal_to_str(wallet_bdag) if wallet_bdag is not None else None,
        "wallet_covers_credits": bool(wallet_bdag is not None and wallet_bdag >= total_bdag),
        "difference_bdag": decimal_to_str(wallet_bdag - total_bdag) if wallet_bdag is not None else None,
        "lifetime_credits_minus_wallet_bdag": decimal_to_str(total_bdag - wallet_bdag) if wallet_bdag is not None else None,
        "reconciliation_note": "Wallet balance is the live on-chain balance of the configured payment wallet. Credit-address totals are shown separately for historical worker addresses.",
    }
    generated_at = now_iso()
    generated_dt = parse_earnings_timestamp(generated_at)
    latest_history_at = None
    latest_history_age_seconds = None
    history_stale_threshold_seconds = EARNINGS_SNAPSHOT_EXPECTED_INTERVAL_SECONDS * 3
    if history:
        parsed_history = [
            parsed
            for parsed in (parse_earnings_timestamp(snapshot.get("generated_at")) for snapshot in history if isinstance(snapshot, dict))
            if parsed is not None
        ]
        if parsed_history:
            latest_dt = max(parsed_history)
            latest_history_at = latest_dt.strftime("%Y-%m-%dT%H:%M:%S%z")
            if generated_dt is not None:
                latest_history_age_seconds = max(0, int((generated_dt - latest_dt).total_seconds()))
    history_stale = bool(
        latest_history_age_seconds is not None
        and latest_history_age_seconds > history_stale_threshold_seconds
    )
    if history_stale:
        history_sampler_status = "stale"
        history_stale_reason = (
            "The earnings/miner plot sampler has not written a fresh valid snapshot "
            f"within {history_stale_threshold_seconds} seconds."
        )
    elif latest_history_at:
        history_sampler_status = "ok"
        history_stale_reason = ""
    else:
        history_sampler_status = "missing"
        history_stale_reason = "No valid earnings/miner plot snapshots were found."
    return {
        "generated_at": generated_at,
        "credits": credits,
        "price": price,
        "wallet": wallet,
        "wallet_balance": wallet_balance,
        "payment_wallet_balance": payment_wallet_balance,
        "credit_wallet_balance": credit_wallet_balance,
        "onchain_earnings": {
            "primary_address": primary_mining_address,
            "last_1h": onchain_1h,
            "last_24h": onchain_24h,
        },
        "earnings_24h": {
            "source": onchain_24h.get("source") if onchain_24h.get("status") == "ok" else "pool-db-credits-24h",
            "bdag": decimal_to_str(recent_24h_bdag),
            "usd": fiat_value(recent_24h_bdag, price, "usd"),
            "zar": fiat_value(recent_24h_bdag, price, "zar"),
            "credit_count": credits.get("recent_24h", {}).get("credit_count"),
            "first_credit_at": credits.get("recent_24h", {}).get("first_credit_at"),
            "last_credit_at": credits.get("recent_24h", {}).get("last_credit_at"),
            "onchain_reconciliation": onchain_24h,
            "db_credit_fallback_bdag": decimal_to_str(db_recent_24h_bdag),
        },
        "credit_balance_check": credit_balance_check,
        "hourly_averages": hourly_averages,
        "miner_estimates": miner_estimates,
        "total_usd": fiat_value(total_bdag, price, "usd"),
        "total_zar": fiat_value(total_bdag, price, "zar"),
        "wallet_24h_usd": fiat_value(decimal_value(hourly_averages.get("wallet_24h_bdag")) or Decimal("0"), price, "usd") if hourly_averages.get("wallet_24h_bdag") is not None else None,
        "wallet_24h_zar": fiat_value(decimal_value(hourly_averages.get("wallet_24h_bdag")) or Decimal("0"), price, "zar") if hourly_averages.get("wallet_24h_bdag") is not None else None,
        "wallet_total_usd": fiat_value(wallet_bdag, price, "usd") if wallet_bdag is not None else None,
        "wallet_total_zar": fiat_value(wallet_bdag, price, "zar") if wallet_bdag is not None else None,
        "snapshot_log": str(EARNINGS_SNAPSHOT_FILE),
        "history": history if include_history else [],
        "history_sample_count": history_sample_count,
        "history_derived_sample_count": len(derived_history),
        "history_total_sample_count": len(history) if include_history else 0,
        "history_derivation_source": "pool-db credits" if derived_history else "",
        "history_retention_days": decimal_to_str(Decimal(EARNINGS_DASHBOARD_HISTORY_SECONDS) / Decimal("86400"), places=1),
        "history_expected_interval_seconds": EARNINGS_SNAPSHOT_EXPECTED_INTERVAL_SECONDS,
        "history_stale_threshold_seconds": history_stale_threshold_seconds,
        "history_latest_at": latest_history_at,
        "history_latest_age_seconds": latest_history_age_seconds,
        "history_stale": history_stale,
        "history_sampler_status": history_sampler_status,
        "history_stale_reason": history_stale_reason,
    }


def record_earnings_snapshot() -> dict[str, Any]:
    ensure_runtime()
    # The status sampler/watchdog only appends a fresh point. Loading the full
    # historical earnings plot here adds avoidable memory pressure to a
    # mining-critical process.
    earnings = collect_earnings(include_history=False)
    credits = earnings.get("credits") if isinstance(earnings.get("credits"), dict) else {}
    if credits.get("error"):
        raise RuntimeError(f"credit totals unavailable: {credits.get('error')}")
    total_bdag = credits.get("totals", {}).get("total_bdag") if isinstance(credits.get("totals"), dict) else None
    if decimal_value(total_bdag) is None:
        raise RuntimeError("credit totals did not include a parseable total_bdag")
    miner_estimates = [
        compact_miner_estimate_for_history(miner)
        for miner in earnings.get("miner_estimates", [])
        if isinstance(miner, dict)
        and is_earnings_wallet_miner(miner)
    ]
    if not miner_estimates:
        raise RuntimeError("earnings snapshot has no wallet miner rows")
    snapshot = {
        "generated_at": earnings["generated_at"],
        "total_bdag": total_bdag,
        "credit_balance_check": earnings.get("credit_balance_check"),
        "miner_estimates": miner_estimates,
    }
    with EARNINGS_SNAPSHOT_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot) + "\n")
    EARNINGS_SNAPSHOT_FILE.chmod(0o600)
    update_dashboard_history_with_snapshot(
        "earnings",
        EARNINGS_SNAPSHOT_FILE,
        snapshot,
        compact_earnings_snapshot,
        earnings_snapshot_has_plot_data,
    )
    return snapshot


def action_log_path(action_name: str) -> Path:
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", action_name).strip("-")
    return LOG_DIR / f"action-{safe_name}-{int(time.time())}.log"


def write_action_state(state: dict[str, Any]) -> None:
    ensure_runtime()
    (RUNTIME_DIR / "latest-action.json").write_text(json.dumps(state, indent=2))


def read_latest_action() -> dict[str, Any] | None:
    path = RUNTIME_DIR / "latest-action.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def run_logged(command: list[str], log_path: Path, timeout: int | None = None) -> CommandResult:
    ensure_runtime()
    start = time.time()
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n[{now_iso()}] $ {' '.join(command)}\n")
        log.flush()
        try:
            proc = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                text=True,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
            code = proc.returncode
        except subprocess.TimeoutExpired:
            code = 124
            log.write(f"\n[{now_iso()}] timed out after {timeout}s\n")
        elapsed = round(time.time() - start, 3)
        log.write(f"\n[{now_iso()}] exit={code} elapsed={elapsed}s\n")
    return CommandResult(command=command, returncode=code, stdout="", stderr="", elapsed=elapsed)


def backup_node_dir(node_name: str, log_path: Path) -> None:
    node_path = Path(node_name).expanduser()
    source = node_path if node_path.is_absolute() else DATA_DIR / node_path
    if not source.exists():
        return
    target = source.with_name(f"{source.name}.backup-{time.strftime('%Y%m%d-%H%M%S')}")
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"[{now_iso()}] moving {source} to {target}\n")
    shutil.move(str(source), str(target))


def restore_clean(log_path: Path) -> bool:
    clean_command = configured_command("BDAG_CLEAN_RESTORE_COMMAND", [])
    if clean_command:
        return run_logged(clean_command, log_path, timeout=1800).ok

    steps = [configured_command("BDAG_STOP_COMMAND", ["make", "down-two"])]
    for step in steps:
        if not step:
            continue
        result = run_logged(step, log_path, timeout=120)
        if not result.ok:
            return False

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for node_dir in NODE_DATA_DIRS:
        backup_node_dir(node_dir, log_path)

    for step in (
        configured_command("BDAG_RESTORE_NODE_COMMAND", ["make", "restore-node-snapshot"]),
        configured_command("BDAG_START_COMMAND", ["make", "up-stack"]),
    ):
        if not step:
            continue
        result = run_logged(step, log_path, timeout=1800)
        if not result.ok:
            return False
    return True


def start_stack(log_path: Path) -> bool:
    command = configured_command("BDAG_START_COMMAND", ["make", "up-two"])
    if not command:
        return False
    return run_logged(command, log_path, timeout=180).ok


def restart_stack(log_path: Path) -> bool:
    stop_command = configured_command("BDAG_STOP_COMMAND", ["make", "down-two"])
    start_command = configured_command("BDAG_START_COMMAND", ["make", "up-two"])
    down = run_logged(stop_command, log_path, timeout=180) if stop_command else CommandResult(stop_command, 0, "", "", 0)
    up = run_logged(start_command, log_path, timeout=180) if start_command else CommandResult(start_command, 1, "", "", 0)
    return down.ok and up.ok


def _render_restart_checklist(status: dict[str, Any], handoff_path: Path) -> str:
    pool_health = status.get("pool_health", {}) or {}
    lines = [
        "# Codex Restart Checklist",
        "",
        "Read these first after a restart:",
        f"- {handoff_path}",
        f"- {RUNTIME_DIR / 'latest-action.json'}",
        f"- {RUNTIME_DIR / 'watchdog-state.json'}",
        "",
        "Current state:",
        f"- overall: {status.get('overall')}",
        f"- share_stall: {pool_health.get('share_stall')}",
        f"- last_valid_share_age_seconds: {pool_health.get('last_valid_share_age_seconds')}",
        f"- stale_submit_count: {pool_health.get('stale_submit_count')}",
        f"- last_action: {(status.get('latest_action') or {}).get('name') or 'none'}",
        "",
        "Startup checks:",
        "- confirm the dashboard API is listening",
        "- confirm bdag-watchdog.service is running",
        "- confirm asic-pool and both node containers are running",
        "- if miners are connected but valid shares stop, restart the stack",
        "- if the dashboard port is down, restart the user services first",
        "",
        "Recovery priority:",
        "1. Bring the dashboard and watchdog back up",
        "2. Check share stall and stale submit counts",
        "3. Restart the stack if the pool is not accepting fresh shares",
        "4. Confirm miners resume valid shares",
        "",
        "Prompt:",
        f"Inspect {handoff_path}, refresh the latest action and watchdog state, then repair the pool if shares are stalled.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def make_handoff() -> Path:
    ensure_runtime()
    status = collect_status(include_logs=True)
    path = RUNTIME_DIR / "codex-handoff.md"
    checklist_path = RUNTIME_DIR / "codex-restart-checklist.md"
    body = [
        "# BlockDAG Pool Handoff",
        "",
        f"Generated: {status['generated_at']}",
        f"Project: {status['project_root']}",
        f"Overall: {status['overall']}",
        f"Mining address: {status.get('mining_address') or 'unknown'}",
        "",
        "## Failures",
        "",
    ]
    if status["failures"]:
        body.extend(f"- {item}" for item in status["failures"])
    else:
        body.append("- none")
    body.extend(["", "## Warnings", ""])
    if status["warnings"]:
        body.extend(f"- {item}" for item in status["warnings"])
    else:
        body.append("- none")
    body.extend(["", "## Containers", ""])
    for name, info in status["containers"].items():
        body.append(
            f"- {name}: status={info.get('status')} running={info.get('running')} restarts={info.get('restart_count')}"
        )
    body.extend(["", "## Nodes", ""])
    for name, info in status["nodes"].items():
        body.append(
            f"- {name}: child={info['child_running']} latest_block={info['latest_block']} "
            f"best_main_order={info['best_main_order']} import_age={info['last_import_age_seconds']}s "
            f"peer_ahead={info['peer_ahead_blocks']} bad_peers={info['invalid_peer_errors']} "
            f"p2p_resets={info['p2p_stream_errors']}"
        )
        body.append("")
        body.append("```text")
        body.extend(info["tail"][-20:])
        body.append("```")
    body.extend(["", "## Pool Log Tail", "", "```text"])
    body.extend(status["pool"]["tail"][-40:])
    body.extend(["```", "", "## Restart Checklist", ""])
    body.append(f"- {checklist_path}")
    body.extend(["", "## Suggested Codex Prompt", ""])
    body.append(
        f"Please inspect {PROJECT_ROOT}, read {path} and {checklist_path}, "
        "check Docker status/logs, and repair the BlockDAG pool without deleting backups."
    )
    checklist_path.write_text(_render_restart_checklist(status, path), encoding="utf-8")
    checklist_path.chmod(0o600)
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path

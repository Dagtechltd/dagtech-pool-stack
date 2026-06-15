#!/usr/bin/env python3
"""Shared BlockDAG pool operations for the dashboard and watchdog."""

from __future__ import annotations

import base64
import bisect
from collections import Counter, deque
import glob
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
from typing import Any, Mapping

import pool_start_gate


def path_from_env(name: str, default: str | Path, base: Path | None = None) -> Path:
    raw = os.environ.get(name)
    path = Path(raw).expanduser() if raw else Path(default).expanduser()
    if not path.is_absolute():
        path = (base or Path.cwd()) / path
    return path.resolve()


def apply_stack_env_aliases() -> None:
    """Normalize mining address env aliases before subprocesses inherit them."""
    aliases = {
        "MINING_POOL_ADDRESS": ("MINING_ADDRESS", "BDAG_MINING_ADDRESS", "POOL_COINBASE_ADDRESS"),
        "MINING_ADDRESS": ("BDAG_MINING_ADDRESS", "MINING_POOL_ADDRESS", "POOL_COINBASE_ADDRESS"),
    }
    for target, sources in aliases.items():
        if os.environ.get(target):
            continue
        for source in sources:
            value = os.environ.get(source)
            if value:
                os.environ[target] = value
                break


def bootstrap_stack_env() -> None:
    """Load the stack env before module-level defaults are frozen."""
    project_root = Path(os.environ.get("BDAG_PROJECT_ROOT") or Path(__file__).resolve().parents[1]).expanduser()
    if not project_root.is_absolute():
        project_root = (Path.cwd() / project_root).resolve()
    else:
        project_root = project_root.resolve()
    pool_env = Path(os.environ["BDAG_POOL_ENV_FILE"]) if os.environ.get("BDAG_POOL_ENV_FILE") else None
    if pool_env is not None and not pool_env.is_absolute():
        pool_env = project_root / pool_env
    runtime_dir = Path(os.environ.get("BDAG_RUNTIME_DIR") or project_root / "ops" / "runtime").expanduser()
    if not runtime_dir.is_absolute():
        runtime_dir = project_root / runtime_dir
    ops_env = Path(os.environ["BDAG_OPS_ENV_FILE"]) if os.environ.get("BDAG_OPS_ENV_FILE") else runtime_dir / "ops.env"
    if not ops_env.is_absolute():
        ops_env = project_root / ops_env

    stack_defaults = Path(
        os.environ.get("BDAG_STACK_DEFAULTS_FILE") or project_root / "ops" / "config" / "stack-defaults.env"
    )
    if not stack_defaults.is_absolute():
        stack_defaults = project_root / stack_defaults

    protected_env = set(os.environ)
    for index, path in enumerate((stack_defaults, ops_env, pool_env, project_root / ".env")):
        if path is None or not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                continue
            value = value.strip()
            if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            if key in protected_env:
                continue
            if index == 0:
                os.environ.setdefault(key, value)
            else:
                os.environ[key] = value
    apply_stack_env_aliases()


bootstrap_stack_env()


def split_env_list(name: str, default: str) -> list[str]:
    raw = os.environ[name] if name in os.environ else default
    return [item.strip() for item in re.split(r"[,;]", raw) if item.strip()]


def split_mac_env_list(name: str, default: str = "") -> list[str]:
    macs: list[str] = []
    seen: set[str] = set()
    for item in split_env_list(name, default):
        candidate = item.split("=", 1)[-1].strip() if "=" in item else item
        mac = normalize_mac(candidate)
        if mac and mac not in seen:
            macs.append(mac)
            seen.add(mac)
    return macs


def single_env_value(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    return value or default


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
            return parse_meminfo(Path("/proc/meminfo").read_text(encoding="utf-8")).get("MemTotal")
        except (OSError, ValueError):
            return None
    if system == "darwin":
        try:
            raw = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True, timeout=1).strip()
            return int(raw)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
    return None


def parse_meminfo(text: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        parts = raw_value.split()
        if not parts:
            continue
        try:
            value = int(parts[0])
        except ValueError:
            continue
        unit = parts[1].lower() if len(parts) > 1 else ""
        if unit == "kb":
            value *= 1024
        values[key.strip()] = value
    return values


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
STATUS_PAYLOAD_STALE_AFTER_SECONDS = env_float(
    "BDAG_STATUS_PAYLOAD_STALE_AFTER_SECONDS",
    120.0,
    minimum=5.0,
)
CATCHUP_PAUSE_ENABLED = env_bool("BDAG_CATCHUP_PAUSE_ENABLED", True)
CATCHUP_PAUSE_THRESHOLD_BLOCKS = env_int("BDAG_CATCHUP_PAUSE_THRESHOLD_BLOCKS", 300, minimum=1)
CATCHUP_NODE_CACHE_MB = env_int("BDAG_CATCHUP_NODE_CACHE_MB", 1024, minimum=0)
CATCHUP_IO_PRESSURE_PAUSE_ENABLED = env_bool("BDAG_CATCHUP_IO_PRESSURE_PAUSE_ENABLED", True)
CATCHUP_IO_PRESSURE_MIN_LAG_BLOCKS = env_int("BDAG_CATCHUP_IO_PRESSURE_MIN_LAG_BLOCKS", 25, minimum=1)
CATCHUP_IOWAIT_WARN_PERCENT = env_float("BDAG_CATCHUP_IOWAIT_WARN_PERCENT", 15.0, minimum=0.0)
CATCHUP_IO_SOME_AVG10_WARN = env_float("BDAG_CATCHUP_IO_SOME_AVG10_WARN", 20.0, minimum=0.0)
CATCHUP_IO_FULL_AVG10_WARN = env_float("BDAG_CATCHUP_IO_FULL_AVG10_WARN", 10.0, minimum=0.0)
SYNC_PRIORITY_ENABLED = env_bool("BDAG_SYNC_PRIORITY_ENABLED", True)
SYNC_PRIORITY_MIN_LAG_BLOCKS = env_int("BDAG_SYNC_PRIORITY_MIN_LAG_BLOCKS", 25, minimum=0)
SYNC_PRIORITY_DEFER_DASHBOARD_SAMPLERS = env_bool("BDAG_SYNC_PRIORITY_DEFER_DASHBOARD_SAMPLERS", True)
SYNC_PROGRESS_ACTIVE_LOOKBACK_SECONDS = int(os.environ.get("BDAG_SYNC_PROGRESS_ACTIVE_LOOKBACK_SECONDS", "2700"))
DEFAULT_POOL_ENV_FILE = PROJECT_ROOT / ".env"
POOL_ENV_FILE = path_from_env("BDAG_POOL_ENV_FILE", DEFAULT_POOL_ENV_FILE, PROJECT_ROOT)
DATA_DIR = path_from_env("BDAG_DATA_DIR", PROJECT_ROOT / "data", PROJECT_ROOT)

POOL_CONTAINER = os.environ.get("BDAG_POOL_CONTAINER", "pool")
POOL_CONTAINERS = unique_names([POOL_CONTAINER, *split_env_list("BDAG_POOL_CONTAINERS", "")])
POOL_DB_CONTAINER = os.environ.get("BDAG_POOL_DB_CONTAINER", "postgres")
POOL_DB_USER = os.environ.get("BDAG_POOL_DB_USER", os.environ.get("POSTGRES_USER", "bdag_pool"))
POOL_DB_NAME = os.environ.get("BDAG_POOL_DB_NAME", os.environ.get("POSTGRES_DB", "bdagpool"))
NODE_SERVICE = single_env_value("BDAG_NODE_SERVICE", "node")
NODES = [NODE_SERVICE]
OBSERVER_NODES: list[str] = []
STACK_SERVICES = split_env_list(
    "BDAG_STACK_SERVICES",
    "postgres,node,pool",
)
SERVICES = unique_names([*STACK_SERVICES, POOL_DB_CONTAINER, *NODES, *POOL_CONTAINERS])
NODE_DATA_DIRS = split_env_list("BDAG_NODE_DATA_DIRS", "node")
NODE_METRIC_PORTS = {
    "node": int(os.environ.get("BDAG_NODE_METRICS_PORT", "6060")),
}
NATIVE_SYNC_LEAD_THRESHOLD = int(os.environ.get("BDAG_NATIVE_SYNC_LEAD_THRESHOLD_BLOCKS", "5"))


def node_role(name: str) -> str:
    return "managed" if name in NODES else "observer"


def node_health_scope(name: str) -> str:
    return "production" if name in NODES else "advisory"


def node_affects_production_health(name: str) -> bool:
    return name in NODES

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
NODE_NOT_DAG_BLOCK_RE = re.compile(r"Not DAG block:\s*(0x[0-9a-fA-F]+)")
NODE_IRREPARABLE_SYNC_RE = re.compile(
    r"Failed to process block:hash=(0x[0-9a-fA-F]+).*?Irreparable error",
    re.IGNORECASE,
)
NODE_MISSING_TRIE_RE = re.compile(r"missing trie node\s+([0-9a-fA-F]+)", re.IGNORECASE)
NODE_RAWDB_PEBBLE_NOT_FOUND_RE = re.compile(r"\bpebble:\s+not found\b.*\bmodule=RAWDB\b", re.IGNORECASE)
NODE_RAWDB_FREEZER_MISSING_HEADER_RE = re.compile(
    r"block header missing,\s*can't freeze block\s+([0-9,]+)",
    re.IGNORECASE,
)
NODE_DAG_EMPTY_BLOCK_RE = re.compile(r"\bempty blockID=(\d+)\b", re.IGNORECASE)
NODE_DAG_ORDER_MISSING_RE = re.compile(r"DAG can't find block in order\(([\d,]+)\)", re.IGNORECASE)
NODE_STATE_HISTORY_TRUNCATE_RE = re.compile(
    r"Failed to truncate extra state histories.*?out of range",
    re.IGNORECASE,
)
CHAIN_STATE_MISSING_TRIE_RESTORE_WARNINGS = env_int(
    "BDAG_CHAIN_STATE_MISSING_TRIE_RESTORE_WARNINGS",
    3,
    minimum=1,
)
CHAIN_STATE_RAWDB_NOT_FOUND_RESTORE_WARNINGS = env_int(
    "BDAG_CHAIN_STATE_RAWDB_NOT_FOUND_RESTORE_WARNINGS",
    20,
    minimum=1,
)
CHAIN_STATE_RAWDB_FREEZER_MISSING_HEADER_WARNINGS = env_int(
    "BDAG_CHAIN_STATE_RAWDB_FREEZER_MISSING_HEADER_WARNINGS",
    2,
    minimum=1,
)
CHAIN_STATE_ORPHAN_STORM_RESTORE_PEER_AHEAD_BLOCKS = env_int(
    "BDAG_CHAIN_STATE_ORPHAN_STORM_RESTORE_PEER_AHEAD_BLOCKS",
    1000,
    minimum=1,
)
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
DEFAULT_DOCKER_BRIDGE_CIDRS = "172.16.0.0/12"
MINER_DHCP_LEASE_FILE_PATTERNS = split_env_list(
    "BDAG_MINER_DHCP_LEASE_FILES",
    "/var/lib/NetworkManager/dnsmasq-*.leases,/var/lib/misc/dnsmasq.leases,/run/dnsmasq/*.leases",
)
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
LIVE_PEERS_FILE = RUNTIME_DIR / "live-peers-current.txt"
CHAIN_PEERSTORE_CANDIDATES_FILE = RUNTIME_DIR / "chain-peerstore-candidates.txt"
PEER_DISCOVERY_FILE = RUNTIME_DIR / "peer-discovery-current.json"
SYNC_COORDINATOR_STATE_FILE = RUNTIME_DIR / "sync-coordinator-state.json"
HOST_PRESSURE_STATE_FILE = RUNTIME_DIR / "host-pressure-state.json"
PEER_GEO_CACHE_FILE = RUNTIME_DIR / "peer-geo-cache.json"
NODE_TEMPLATE_PROBE_CACHE_FILE = RUNTIME_DIR / "node-template-probe-cache.json"
WEI_PER_BDAG = Decimal("1000000000000000000")
ATOMS_PER_BDAG = Decimal("100000000")
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
GLOBAL_BLOCK_WINDOW = env_int("BDAG_GLOBAL_BLOCK_WINDOW", 600, minimum=1)
GLOBAL_RPC_WORKERS = env_int("BDAG_GLOBAL_RPC_WORKERS", 24, minimum=1)
GLOBAL_EVM_FALLBACK_BLOCK_WINDOW = env_int("BDAG_GLOBAL_EVM_FALLBACK_BLOCK_WINDOW", GLOBAL_BLOCK_WINDOW, minimum=1)
GLOBAL_EVM_FALLBACK_RPC_WORKERS = env_int("BDAG_GLOBAL_EVM_FALLBACK_RPC_WORKERS", GLOBAL_RPC_WORKERS, minimum=1)
GLOBAL_CHAIN_ORDER_RPC_TIMEOUT = env_float("BDAG_GLOBAL_CHAIN_ORDER_RPC_TIMEOUT", 3.0, minimum=0.5)
GLOBAL_CHAIN_BLOCK_RPC_TIMEOUT = env_float("BDAG_GLOBAL_CHAIN_BLOCK_RPC_TIMEOUT", 3.0, minimum=0.5)
GLOBAL_CHAIN_PREFLIGHT_SAMPLE_MIN_BLOCKS = env_int("BDAG_GLOBAL_CHAIN_PREFLIGHT_SAMPLE_MIN_BLOCKS", 64, minimum=1)
GLOBAL_POOL_HEIGHT_MAX_AGE_SECONDS = env_int("BDAG_GLOBAL_POOL_HEIGHT_MAX_AGE_SECONDS", 600, minimum=0)
GLOBAL_HISTORY_LIMIT = int(os.environ.get("BDAG_GLOBAL_HISTORY_LIMIT", "9000"))
GLOBAL_HISTORY_COMPACT_MULTIPLIER = max(1, int(os.environ.get("BDAG_GLOBAL_HISTORY_COMPACT_MULTIPLIER", "2")))
GLOBAL_CACHE_SCHEMA_VERSION = 2
GLOBAL_STATS_SOURCE_TRUTH = "chain-rpc:getBlockCount/getBlockByOrder/getBlockHeader/getCoinbaseAddress"
GLOBAL_CACHE_MAX_TIP_LAG_BLOCKS = env_int("BDAG_GLOBAL_CACHE_MAX_TIP_LAG_BLOCKS", 30, minimum=0)
GLOBAL_EVM_FALLBACK_ENABLED = env_bool("BDAG_GLOBAL_EVM_FALLBACK_ENABLED", False)
GLOBAL_CHAIN_PEER_RPC_ENABLED = env_bool("BDAG_GLOBAL_CHAIN_PEER_RPC_ENABLED", True)
GLOBAL_CHAIN_PEER_RPC_LIMIT = env_int("BDAG_GLOBAL_CHAIN_PEER_RPC_LIMIT", 4, minimum=0)
GLOBAL_CHAIN_PEER_RPC_PORT = env_int(
    "BDAG_GLOBAL_CHAIN_PEER_RPC_PORT",
    env_int("BDAG_NODE_MINING_RPC_PORT", 38131, minimum=1),
    minimum=1,
)
GLOBAL_CHAIN_PEER_RPC_TIMEOUT = env_float("BDAG_GLOBAL_CHAIN_PEER_RPC_TIMEOUT", 1.0, minimum=0.25)
NODE_EVM_RPC_PORT = int(os.environ.get("BDAG_NODE_EVM_RPC_PORT", "18545"))
EVM_SYNC_LAG_THRESHOLD_BLOCKS = env_int("BDAG_EVM_SYNC_LAG_THRESHOLD_BLOCKS", 1000, minimum=0)
EVM_PUBLIC_ALIGNMENT_SAMPLE_BLOCKS = env_int("BDAG_EVM_PUBLIC_ALIGNMENT_SAMPLE_BLOCKS", 3, minimum=1)
EVM_PUBLIC_ALIGNMENT_MIN_SAMPLES = env_int("BDAG_EVM_PUBLIC_ALIGNMENT_MIN_SAMPLES", 1, minimum=1)
EVM_PUBLIC_ALIGNMENT_MIN_REFERENCE_LAG = env_int(
    "BDAG_EVM_PUBLIC_ALIGNMENT_MIN_REFERENCE_LAG",
    EVM_SYNC_LAG_THRESHOLD_BLOCKS,
    minimum=0,
)
EVM_PUBLIC_ALIGNMENT_ALWAYS_SAMPLE = env_bool("BDAG_EVM_PUBLIC_ALIGNMENT_ALWAYS_SAMPLE", True)
EARNINGS_HISTORY_RETENTION_SECONDS = int(os.environ.get("BDAG_EARNINGS_HISTORY_RETENTION_SECONDS", str(35 * 86400)))
EARNINGS_DASHBOARD_HISTORY_SECONDS = int(os.environ.get("BDAG_EARNINGS_DASHBOARD_HISTORY_SECONDS", str(31 * 86400)))
EARNINGS_SNAPSHOT_EXPECTED_INTERVAL_SECONDS = int(os.environ.get("BDAG_WATCHDOG_EARNINGS_SNAPSHOT_INTERVAL_SECONDS", "60"))
EARNINGS_ONCHAIN_CACHE_SECONDS = int(os.environ.get("BDAG_EARNINGS_ONCHAIN_CACHE_SECONDS", "120"))
EARNINGS_ONCHAIN_WINDOW_ENABLED = env_bool("BDAG_EARNINGS_ONCHAIN_WINDOW_ENABLED", True)
LOCAL_EVM_BALANCE_PROBE_ENABLED = env_bool("BDAG_LOCAL_EVM_BALANCE_PROBE_ENABLED", True)
LOCAL_EVM_BALANCE_PROBE_PAUSE_DURING_SYNC = env_bool("BDAG_LOCAL_EVM_BALANCE_PROBE_PAUSE_DURING_SYNC", True)
LOCAL_EVM_BALANCE_PROBE_STATUS_MAX_AGE_SECONDS = env_float(
    "BDAG_LOCAL_EVM_BALANCE_PROBE_STATUS_MAX_AGE_SECONDS",
    180.0,
    minimum=0.0,
)
EARNINGS_DERIVED_HISTORY_ENABLED = env_bool("BDAG_EARNINGS_DERIVED_HISTORY_ENABLED", True)
EARNINGS_DERIVED_HISTORY_RUNTIME_FALLBACK_ENABLED = env_bool("BDAG_EARNINGS_DERIVED_HISTORY_RUNTIME_FALLBACK_ENABLED", False)
EARNINGS_DERIVED_HISTORY_BUCKET_SECONDS = env_int("BDAG_EARNINGS_DERIVED_HISTORY_BUCKET_SECONDS", 300, minimum=60)
DASHBOARD_HISTORY_HOT_SECONDS = env_int("BDAG_DASHBOARD_HISTORY_HOT_SECONDS", 3600, minimum=60)
DASHBOARD_HISTORY_HOT_STEP_SECONDS = env_int("BDAG_DASHBOARD_HISTORY_HOT_STEP_SECONDS", 60, minimum=60)
DASHBOARD_HISTORY_FIVE_MINUTE_SECONDS = env_int("BDAG_DASHBOARD_HISTORY_FIVE_MINUTE_SECONDS", 24 * 3600, minimum=3600)
DASHBOARD_HISTORY_FIVE_MINUTE_STEP_SECONDS = env_int("BDAG_DASHBOARD_HISTORY_FIVE_MINUTE_STEP_SECONDS", 5 * 60, minimum=60)
DASHBOARD_HISTORY_FIFTEEN_MINUTE_SECONDS = env_int("BDAG_DASHBOARD_HISTORY_FIFTEEN_MINUTE_SECONDS", 3 * 86400, minimum=24 * 3600)
DASHBOARD_HISTORY_FIFTEEN_MINUTE_STEP_SECONDS = env_int("BDAG_DASHBOARD_HISTORY_FIFTEEN_MINUTE_STEP_SECONDS", 15 * 60, minimum=60)
DASHBOARD_HISTORY_THIRTY_MINUTE_SECONDS = env_int("BDAG_DASHBOARD_HISTORY_THIRTY_MINUTE_SECONDS", 7 * 86400, minimum=3 * 86400)
DASHBOARD_HISTORY_THIRTY_MINUTE_STEP_SECONDS = env_int("BDAG_DASHBOARD_HISTORY_THIRTY_MINUTE_STEP_SECONDS", 30 * 60, minimum=60)
DASHBOARD_HISTORY_TWO_HOUR_SECONDS = env_int("BDAG_DASHBOARD_HISTORY_TWO_HOUR_SECONDS", 31 * 86400, minimum=7 * 86400)
DASHBOARD_HISTORY_TWO_HOUR_STEP_SECONDS = env_int("BDAG_DASHBOARD_HISTORY_TWO_HOUR_STEP_SECONDS", 2 * 3600, minimum=60)
DASHBOARD_HISTORY_DISK_DIR = path_from_env("BDAG_DASHBOARD_HISTORY_DISK_DIR", RUNTIME_DIR / "dashboard-history", PROJECT_ROOT)
DASHBOARD_HISTORY_REBUILD_BLOCK_WINDOW = env_int("BDAG_DASHBOARD_HISTORY_REBUILD_BLOCK_WINDOW", 64, minimum=1)
DASHBOARD_HISTORY_REBUILD_RPC_WORKERS = env_int("BDAG_DASHBOARD_HISTORY_REBUILD_RPC_WORKERS", 12, minimum=1)
DASHBOARD_HISTORY_REBUILD_LOOKBACK_ORDERS = env_int("BDAG_DASHBOARD_HISTORY_REBUILD_LOOKBACK_ORDERS", 5_000_000, minimum=1)
DASHBOARD_HISTORY_REBUILD_RPC_TIMEOUT_SECONDS = env_float("BDAG_DASHBOARD_HISTORY_REBUILD_RPC_TIMEOUT_SECONDS", 6.0, minimum=0.5)
DASHBOARD_HISTORY_REBUILD_STATE_FILE = path_from_env("BDAG_DASHBOARD_HISTORY_REBUILD_STATE_FILE", RUNTIME_DIR / "dashboard-rpc-history-rebuild-state.json", PROJECT_ROOT)
DASHBOARD_HISTORY_REBUILD_ACTIVE_STALE_SECONDS = env_int("BDAG_DASHBOARD_HISTORY_REBUILD_ACTIVE_STALE_SECONDS", 120, minimum=10)
DASHBOARD_HISTORY_REBUILD_PRESERVE_ASIC_HISTORY = env_bool("BDAG_DASHBOARD_HISTORY_REBUILD_PRESERVE_ASIC_HISTORY", True)
DASHBOARD_CHAIN_HISTORY_SOURCE_CONTRACT = "blockdag-mining-rpc-history-v1"
PEER_GEO_CACHE_TTL_SECONDS = int(os.environ.get("BDAG_PEER_GEO_CACHE_TTL_SECONDS", "86400"))
PEER_GEO_LOOKUP_TIMEOUT = float(os.environ.get("BDAG_PEER_GEO_LOOKUP_TIMEOUT", "8.0"))
PUBLIC_EVM_RPC_DEFAULTS = [
    ("blockdag-engineering-rpc", "https://rpc.blockdag.engineering"),
    ("bdagscan-rpc", "https://rpc.bdagscan.com"),
]
MINER_STALE_SECONDS = int(os.environ.get("BDAG_MINER_STALE_SECONDS", "120"))
POOL_ACTIVITY_LOG_LINES = int(os.environ.get("BDAG_POOL_ACTIVITY_LOG_LINES", "2000"))
POOL_ACTIVITY_BOOTSTRAP_LOG_LINES = int(os.environ.get("BDAG_POOL_ACTIVITY_BOOTSTRAP_LOG_LINES", "20000"))
POOL_CONNECTED_STALE_SECONDS = int(os.environ.get("BDAG_POOL_CONNECTED_STALE_SECONDS", str(MINER_STALE_SECONDS)))
MINER_REGISTRY_POOL_LOG_STALE_SECONDS = int(
    os.environ.get("BDAG_MINER_REGISTRY_POOL_LOG_STALE_SECONDS", str(max(POOL_CONNECTED_STALE_SECONDS * 2, 600)))
)
MINER_REGISTRY_EXPECTED_ASIC_STALE_SECONDS = int(
    os.environ.get("BDAG_MINER_REGISTRY_EXPECTED_ASIC_STALE_SECONDS", "86400")
)
MINER_REGISTRY_MAX_PORTS = env_int("BDAG_MINER_REGISTRY_MAX_PORTS", 16, minimum=1)
MINER_REGISTRY_MAX_JOB_EXTRANONCES = env_int("BDAG_MINER_REGISTRY_MAX_JOB_EXTRANONCES", 64, minimum=1)
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
POOL_TEMPLATE_DELIVERY_FRESH_SECONDS = env_float("BDAG_POOL_TEMPLATE_DELIVERY_FRESH_SECONDS", 5.0, minimum=0.1)
POOL_SUBMIT_RECOVERY_RECENT_SECONDS = int(os.environ.get("BDAG_POOL_SUBMIT_RECOVERY_RECENT_SECONDS", "180"))
POOL_SUBMIT_RECOVERY_ACCEPTED_RESUME_SECONDS = int(
    os.environ.get("BDAG_POOL_SUBMIT_RECOVERY_ACCEPTED_RESUME_SECONDS", "90")
)
POOL_RPC_REFUSED_WARN_SECONDS = int(os.environ.get("BDAG_POOL_RPC_REFUSED_WARN_SECONDS", "120"))
POOL_INITIAL_DOWNLOAD_RECENT_SECONDS = env_int("BDAG_POOL_INITIAL_DOWNLOAD_RECENT_SECONDS", 120, minimum=0)
NODE_IMPORT_STALE_SECONDS = int(os.environ.get("BDAG_NODE_IMPORT_STALE_SECONDS", "180"))
NODE_LAG_WARN_BLOCKS = int(os.environ.get("BDAG_NODE_LAG_WARN_BLOCKS", "5"))
NODE_P2P_ERROR_WARN_COUNT = int(os.environ.get("BDAG_NODE_P2P_ERROR_WARN_COUNT", "10"))
NODE_ORPHAN_ERROR_STORM_COUNT = int(os.environ.get("BDAG_NODE_ORPHAN_ERROR_STORM_COUNT", "20"))
NODE_GRAPH_SYNC_CHURN_COUNT = int(os.environ.get("BDAG_NODE_GRAPH_SYNC_CHURN_COUNT", "8"))
NODE_DAG_EMPTY_BLOCK_STORM_COUNT = env_int("BDAG_NODE_DAG_EMPTY_BLOCK_STORM_COUNT", 40, minimum=1)
NODE_MINING_RPC_PORT = int(os.environ.get("BDAG_NODE_MINING_RPC_PORT", "38131"))
NODE_MINING_RPC_USER = os.environ.get("BDAG_NODE_MINING_RPC_USER") or os.environ.get("NODE_RPC_USER", "test")
NODE_MINING_RPC_PASS = os.environ.get("BDAG_NODE_MINING_RPC_PASS") or os.environ.get("NODE_RPC_PASS", "test")
NODE_TEMPLATE_PROBE_CACHE_SECONDS = int(os.environ.get("BDAG_NODE_TEMPLATE_PROBE_CACHE_SECONDS", "60"))
NODE_TEMPLATE_PROBE_SAMPLES = max(1, int(os.environ.get("BDAG_NODE_TEMPLATE_PROBE_SAMPLES", "1")))
NODE_TEMPLATE_PROBE_TIMEOUT = float(os.environ.get("BDAG_NODE_TEMPLATE_PROBE_TIMEOUT", "1.5"))
NODE_CHAIN_RPC_TIMEOUT = float(os.environ.get("BDAG_NODE_CHAIN_RPC_TIMEOUT", "8.0"))
NODE_CHAIN_RPC_RETRIES = max(1, int(os.environ.get("BDAG_NODE_CHAIN_RPC_RETRIES", "2")))
NEIGHBOR_MAC_CACHE_SECONDS = env_float("BDAG_NEIGHBOR_MAC_CACHE_SECONDS", 2.0, minimum=0.0)
HOST_PRESSURE_IOWAIT_WARN_PERCENT = env_float("BDAG_HOST_PRESSURE_IOWAIT_WARN_PERCENT", 25.0, minimum=0.0)
HOST_PRESSURE_MEMORY_AVAILABLE_WARN_PERCENT = env_float(
    "BDAG_HOST_PRESSURE_MEMORY_AVAILABLE_WARN_PERCENT",
    12.0,
    minimum=0.0,
)
HOST_PRESSURE_SWAP_USED_WARN_PERCENT = env_float(
    "BDAG_HOST_PRESSURE_SWAP_USED_WARN_PERCENT",
    5.0,
    minimum=0.0,
)
HOST_PRESSURE_SWAP_MEMORY_PSI_WARN_AVG10 = env_float(
    "BDAG_HOST_PRESSURE_SWAP_MEMORY_PSI_WARN_AVG10",
    1.0,
    minimum=0.0,
)
HOST_PRESSURE_IOWAIT_WARN_SAMPLES = env_int("BDAG_HOST_PRESSURE_IOWAIT_WARN_SAMPLES", 3, minimum=2)
HOST_PRESSURE_HISTORY_SAMPLES = max(
    HOST_PRESSURE_IOWAIT_WARN_SAMPLES,
    env_int("BDAG_HOST_PRESSURE_HISTORY_SAMPLES", 6, minimum=HOST_PRESSURE_IOWAIT_WARN_SAMPLES),
)
HTTP_USER_AGENT = os.environ.get("BDAG_HTTP_USER_AGENT", "blockdag-dashboard/1.0")
SHARED_STATUS_CACHE_ENABLED = env_bool("BDAG_SHARED_STATUS_CACHE_ENABLED", True)
SHARED_STATUS_CACHE_SECONDS = env_float("BDAG_SHARED_STATUS_CACHE_SECONDS", 3.0, minimum=0.0)
STATUS_SAMPLER_ENABLED = env_bool("BDAG_STATUS_SAMPLER_ENABLED", True)
STATUS_SAMPLER_MAX_AGE_SECONDS = env_float("BDAG_STATUS_SAMPLER_MAX_AGE_SECONDS", 120.0, minimum=0.0)
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
ADAPTIVE_MEMORY_AVAILABLE_WARN_PERCENT = env_float(
    "BDAG_ADAPTIVE_MEMORY_AVAILABLE_WARN_PERCENT",
    HOST_PRESSURE_MEMORY_AVAILABLE_WARN_PERCENT,
    minimum=0.0,
)
ADAPTIVE_SWAP_USED_WARN_PERCENT = env_float(
    "BDAG_ADAPTIVE_SWAP_USED_WARN_PERCENT",
    HOST_PRESSURE_SWAP_USED_WARN_PERCENT,
    minimum=0.0,
)
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
BACKGROUND_MAINTENANCE_IO_FULL_AVG10_WARN = env_float(
    "BDAG_BACKGROUND_MAINTENANCE_IO_FULL_AVG10_WARN",
    CATCHUP_IO_FULL_AVG10_WARN,
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
BACKGROUND_MAINTENANCE_MEMORY_AVAILABLE_WARN_PERCENT = env_float(
    "BDAG_BACKGROUND_MAINTENANCE_MEMORY_AVAILABLE_WARN_PERCENT",
    HOST_PRESSURE_MEMORY_AVAILABLE_WARN_PERCENT,
    minimum=0.0,
)
BACKGROUND_MAINTENANCE_SWAP_USED_WARN_PERCENT = env_float(
    "BDAG_BACKGROUND_MAINTENANCE_SWAP_USED_WARN_PERCENT",
    HOST_PRESSURE_SWAP_USED_WARN_PERCENT,
    minimum=0.0,
)
BACKGROUND_MAINTENANCE_POOL_READY_STATUS_MAX_AGE_SECONDS = env_float(
    "BDAG_BACKGROUND_MAINTENANCE_POOL_READY_STATUS_MAX_AGE_SECONDS",
    30.0,
    minimum=0.0,
)
BACKGROUND_MAINTENANCE_LAZY_TASKS = set(
    split_env_list(
        "BDAG_BACKGROUND_MAINTENANCE_LAZY_TASKS",
        (
            "dashboard_global_sampler,global_blockchain_scan,global_scan,"
            "rawdatadir_sidecar,rawdatadir_content_seal,ipfs_content_sidecar,"
            "ipfs_segment_writer,history_compaction,snapshot"
        ),
    )
)
BACKGROUND_MAINTENANCE_POOL_READY_TASKS = set(
    split_env_list(
        "BDAG_BACKGROUND_MAINTENANCE_POOL_READY_TASKS",
        "rawdatadir_content_seal,ipfs_content_sidecar",
    )
)
BACKGROUND_MAINTENANCE_SYNC_PRIORITY_EXEMPT_TASKS = set(
    split_env_list(
        "BDAG_BACKGROUND_MAINTENANCE_SYNC_PRIORITY_EXEMPT_TASKS",
        "ipfs_segment_writer",
    )
)
BACKGROUND_MAINTENANCE_IO_PRESSURE_EXEMPT_TASKS = set(
    split_env_list(
        "BDAG_BACKGROUND_MAINTENANCE_IO_PRESSURE_EXEMPT_TASKS",
        "",
    )
)
BACKGROUND_MAINTENANCE_LOADAVG_PER_CPU_WARN = env_float(
    "BDAG_BACKGROUND_MAINTENANCE_LOADAVG_PER_CPU_WARN",
    1.25,
    minimum=0.1,
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


def is_recent_mining_sync_noise(item: Any) -> bool:
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


_DOCKER_USE_SUDO_CACHE: bool | None = None
DOCKER_ACCESS_ERROR_MARKERS = (
    "permission denied while trying to connect to the docker api",
    "cannot connect to the docker daemon",
    "is the docker daemon running",
    "connect: permission denied",
)


def command_uses_docker(command: list[str]) -> bool:
    return bool(command) and command[0] == "docker"


def sudo_docker_command(command: list[str]) -> list[str]:
    return ["sudo", "-n", *command]


def docker_result_looks_like_access_error(result: CommandResult) -> bool:
    text = f"{result.stderr}\n{result.stdout}".lower()
    return result.returncode == 127 or any(marker in text for marker in DOCKER_ACCESS_ERROR_MARKERS)


def docker_sudo_fallback_enabled() -> bool:
    return env_bool("BDAG_DOCKER_SUDO_FALLBACK", True)


def docker_use_sudo_requested() -> bool:
    return env_bool("BDAG_DOCKER_USE_SUDO", False)


def run_subprocess_capture(command: list[str], timeout: int) -> CommandResult:
    start = time.time()
    try:
        proc = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            text=False,
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
        stderr = strip_ansi(exc.stderr or "")
        if stderr:
            stderr = f"{stderr}\nTimed out after {timeout}s"
        else:
            stderr = f"Timed out after {timeout}s"
        return CommandResult(
            command=command,
            returncode=124,
            stdout=strip_ansi(exc.stdout or ""),
            stderr=stderr,
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


def run(command: list[str], timeout: int = 20) -> CommandResult:
    global _DOCKER_USE_SUDO_CACHE
    if command_uses_docker(command) and docker_sudo_fallback_enabled():
        if docker_use_sudo_requested() or _DOCKER_USE_SUDO_CACHE is True:
            return run_subprocess_capture(sudo_docker_command(command), timeout)
        direct = run_subprocess_capture(command, timeout)
        if direct.ok:
            _DOCKER_USE_SUDO_CACHE = False
            return direct
        if docker_result_looks_like_access_error(direct):
            sudo_result = run_subprocess_capture(sudo_docker_command(command), timeout)
            if sudo_result.ok:
                _DOCKER_USE_SUDO_CACHE = True
                return sudo_result
        return direct

    return run_subprocess_capture(command, timeout)


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
        "MINING_ADDRESS": ["BDAG_MINING_ADDRESS", "MINING_POOL_ADDRESS", "POOL_COINBASE_ADDRESS"],
        "MINING_POOL_ADDRESS": ["MINING_ADDRESS", "BDAG_MINING_ADDRESS", "POOL_COINBASE_ADDRESS"],
        "POOL_PORT": ["BDAG_POOL_PORT"],
    }.get(name, [])
    for env_name in [*aliases, name]:
        value = os.environ.get(env_name)
        if value:
            return value
    return read_env_file_value(POOL_ENV_FILE, name)


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


def effective_connected_miner_count(
    miner_health: Mapping[str, Any],
    pool_metrics: Mapping[str, Any],
    source_job_health: Mapping[str, Any],
) -> int:
    return max(
        safe_int(miner_health.get("connected_count"), 0),
        safe_int(pool_metrics.get("active_connections"), 0),
        safe_int(source_job_health.get("authorized_miners"), 0),
        safe_int(source_job_health.get("ready_miners"), 0),
    )


def source_job_health_lane_summary(source_job_health: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return current Stratum lane state from the pool's authoritative job-health feed."""
    if not isinstance(source_job_health, Mapping):
        return {
            "job_state_available": False,
            "active_lane_ids": [],
            "authorized_lane_ids": [],
            "ready_lane_ids": [],
            "clients_by_lane": {},
        }
    active: set[str] = set()
    authorized: set[str] = set()
    ready: set[str] = set()
    clients_by_lane: dict[str, list[dict[str, Any]]] = {}
    for client in source_job_health.get("clients") or []:
        if not isinstance(client, Mapping):
            continue
        lane_id = str(client.get("lane_id") or "").strip()
        mac = normalize_mac(client.get("asic_mac"))
        if not lane_id and mac:
            lane_id = f"mac:{mac}"
        if not lane_id:
            continue
        item = dict(client)
        item["lane_id"] = lane_id
        if mac:
            item["asic_mac"] = mac
        clients_by_lane.setdefault(lane_id, []).append(item)
        active.add(lane_id)
        if item.get("authorized"):
            authorized.add(lane_id)
        if item.get("ready"):
            ready.add(lane_id)
    return {
        "job_state_available": bool(source_job_health.get("job_state_available")),
        "active_lane_ids": sorted(active),
        "authorized_lane_ids": sorted(authorized),
        "ready_lane_ids": sorted(ready),
        "clients_by_lane": clients_by_lane,
    }


def miner_failures_block_stack(
    miner_failures: list[str],
    connected_miners: int,
    pool_has_recent_share_activity: bool,
    pool_has_recent_paid_work: bool,
    source_job_health_ok: bool | None,
) -> bool:
    if not miner_failures:
        return False
    if connected_miners <= 0:
        return True
    return not (
        pool_has_recent_share_activity
        or pool_has_recent_paid_work
        or source_job_health_ok is True
    )


def selected_backend_unready_reasons(selected_source_health: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    for key, label in (
        ("node_mineable", "mineable=false"),
        ("node_submit_ready", "submit_ready=false"),
        ("node_p2p_mining_fresh", "p2p_mining_fresh=false"),
    ):
        if key in selected_source_health and not bool(selected_source_health.get(key)):
            reasons.append(label)
    if selected_source_health.get("node_last_template_build_error_blocking") is True:
        reasons.append("template_build_error_blocking=true")
    return reasons


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


def atomic_to_bdag(value: str | int | Decimal | None) -> Decimal:
    try:
        return Decimal(str(value or "0")) / ATOMS_PER_BDAG
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


def host_pressure_swap_active(
    pressure: dict[str, Any] | None,
    swap_warn_percent: float = HOST_PRESSURE_SWAP_USED_WARN_PERCENT,
    memory_warn_percent: float = HOST_PRESSURE_MEMORY_AVAILABLE_WARN_PERCENT,
    memory_psi_warn_avg10: float = HOST_PRESSURE_SWAP_MEMORY_PSI_WARN_AVG10,
) -> bool:
    if not isinstance(pressure, dict):
        return False
    swap_used_percent = safe_float(pressure.get("swap_used_percent"))
    if swap_used_percent is None or swap_used_percent < swap_warn_percent:
        return False
    memory_available_percent = safe_float(pressure.get("memory_available_percent"))
    if pressure.get("memory_warning_active"):
        return True
    if memory_available_percent is not None and memory_available_percent <= memory_warn_percent:
        return True

    memory_some = safe_float(pressure.get("memory_some_avg10"))
    memory_full = safe_float(pressure.get("memory_full_avg10"))
    if memory_some is None and memory_full is None:
        return memory_available_percent is None
    return bool((memory_some or 0.0) >= memory_psi_warn_avg10 or (memory_full or 0.0) >= memory_psi_warn_avg10)


def host_pressure_warning_messages(pressure: dict[str, Any]) -> list[str]:
    messages: list[str] = []
    memory_available_percent = safe_float(pressure.get("memory_available_percent"))
    if pressure.get("memory_warning_active") and memory_available_percent is not None:
        messages.append(
            "host RAM available is low "
            f"({memory_available_percent:.2f}% <= {HOST_PRESSURE_MEMORY_AVAILABLE_WARN_PERCENT:.2f}%)"
        )
    swap_used_percent = safe_float(pressure.get("swap_used_percent"))
    if host_pressure_swap_active(pressure) and swap_used_percent is not None:
        messages.append(
            "host swap pressure is active "
            f"({swap_used_percent:.2f}% >= {HOST_PRESSURE_SWAP_USED_WARN_PERCENT:.2f}%)"
        )
    samples = pressure.get("samples") if isinstance(pressure.get("samples"), list) else []
    if not pressure.get("iowait_warning_active"):
        return messages
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
    messages.append(
        "host IO wait is sustained across recent dashboard samples "
        f"({detail}, threshold={HOST_PRESSURE_IOWAIT_WARN_PERCENT:.2f}%)"
    )
    return messages


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
        "memory_full_avg10": None,
        "memory_total_bytes": None,
        "memory_available_bytes": None,
        "memory_available_percent": None,
        "memory_available_warn_percent": HOST_PRESSURE_MEMORY_AVAILABLE_WARN_PERCENT,
        "memory_warning_active": False,
        "swap_total_bytes": None,
        "swap_free_bytes": None,
        "swap_used_bytes": None,
        "swap_used_percent": None,
        "swap_used_warn_percent": HOST_PRESSURE_SWAP_USED_WARN_PERCENT,
        "swap_warning_active": False,
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
        meminfo = parse_meminfo(Path("/proc/meminfo").read_text(encoding="utf-8"))
    except OSError:
        meminfo = {}
    mem_total = meminfo.get("MemTotal")
    mem_available = meminfo.get("MemAvailable")
    if mem_total and mem_available is not None and mem_total > 0:
        memory_available_percent = round(max(0.0, mem_available * 100.0 / mem_total), 2)
        pressure["memory_total_bytes"] = mem_total
        pressure["memory_available_bytes"] = mem_available
        pressure["memory_available_percent"] = memory_available_percent
        pressure["memory_warning_active"] = memory_available_percent <= HOST_PRESSURE_MEMORY_AVAILABLE_WARN_PERCENT
    swap_total = meminfo.get("SwapTotal")
    swap_free = meminfo.get("SwapFree")
    if swap_total is not None and swap_free is not None:
        swap_used = max(0, swap_total - swap_free)
        pressure["swap_total_bytes"] = swap_total
        pressure["swap_free_bytes"] = swap_free
        pressure["swap_used_bytes"] = swap_used
        if swap_total > 0:
            swap_used_percent = round(max(0.0, swap_used * 100.0 / swap_total), 2)
            pressure["swap_used_percent"] = swap_used_percent
            pressure["swap_warning_active"] = host_pressure_swap_active(pressure)

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
    memory_available_percent = safe_float(pressure.get("memory_available_percent"))
    swap_pressure_high = host_pressure_swap_active(
        pressure,
        ADAPTIVE_SWAP_USED_WARN_PERCENT,
        ADAPTIVE_MEMORY_AVAILABLE_WARN_PERCENT,
    )
    swap_pressure_moderate = host_pressure_swap_active(
        pressure,
        max(ADAPTIVE_SWAP_USED_WARN_PERCENT / 2, 1.0),
        max(ADAPTIVE_MEMORY_AVAILABLE_WARN_PERCENT * 2, ADAPTIVE_MEMORY_AVAILABLE_WARN_PERCENT),
    )
    if (
        bool(pressure.get("iowait_warning_active"))
        or bool(pressure.get("memory_warning_active"))
        or swap_pressure_high
        or (iowait is not None and iowait >= ADAPTIVE_IOWAIT_WARN_PERCENT)
        or (io_some is not None and io_some >= ADAPTIVE_IO_SOME_AVG10_WARN)
        or (cpu_some is not None and cpu_some >= ADAPTIVE_CPU_SOME_AVG10_WARN)
        or (chain_rpc_latency is not None and chain_rpc_latency >= ADAPTIVE_CHAIN_RPC_WARN_MS)
        or (
            memory_available_percent is not None
            and memory_available_percent <= ADAPTIVE_MEMORY_AVAILABLE_WARN_PERCENT
        )
    ):
        return "high"
    if (
        (iowait is not None and iowait >= ADAPTIVE_IOWAIT_WARN_PERCENT / 2)
        or (io_some is not None and io_some >= ADAPTIVE_IO_SOME_AVG10_WARN / 2)
        or (cpu_some is not None and cpu_some >= ADAPTIVE_CPU_SOME_AVG10_WARN / 2)
        or (chain_rpc_latency is not None and chain_rpc_latency >= ADAPTIVE_CHAIN_RPC_WARN_MS / 2)
        or (
            memory_available_percent is not None
            and memory_available_percent <= max(ADAPTIVE_MEMORY_AVAILABLE_WARN_PERCENT * 2, ADAPTIVE_MEMORY_AVAILABLE_WARN_PERCENT)
        )
        or swap_pressure_moderate
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


def write_dashboard_plot_rebuild_state(state: dict[str, Any]) -> dict[str, Any]:
    now_epoch = time.time()
    payload = {
        "schema_version": 1,
        "updated_at": now_iso(),
        "updated_at_epoch": now_epoch,
        **state,
    }
    write_json_file(DASHBOARD_HISTORY_REBUILD_STATE_FILE, payload, mode=0o600)
    return payload


def read_dashboard_plot_rebuild_state(now_epoch: float | None = None) -> dict[str, Any]:
    payload = read_json_file(DASHBOARD_HISTORY_REBUILD_STATE_FILE, {})
    if not isinstance(payload, dict) or not payload:
        return {"status": "idle", "active": False, "path": str(DASHBOARD_HISTORY_REBUILD_STATE_FILE)}
    now_value = time.time() if now_epoch is None else now_epoch
    try:
        updated_epoch = float(payload.get("updated_at_epoch") or 0)
    except (TypeError, ValueError):
        updated_epoch = 0
    age_seconds = max(0.0, now_value - updated_epoch) if updated_epoch > 0 else None
    status = str(payload.get("status") or "unknown")
    active = status == "running" and age_seconds is not None and age_seconds <= DASHBOARD_HISTORY_REBUILD_ACTIVE_STALE_SECONDS
    result = dict(payload)
    result["active"] = active
    result["age_seconds"] = round(age_seconds, 3) if age_seconds is not None else None
    result["stale"] = status == "running" and not active
    result["path"] = str(DASHBOARD_HISTORY_REBUILD_STATE_FILE)
    result["active_stale_seconds"] = DASHBOARD_HISTORY_REBUILD_ACTIVE_STALE_SECONDS
    return result


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
    sample_include_logs = bool(snapshot.get("include_logs"))
    if include_logs and not sample_include_logs:
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
        "include_logs": sample_include_logs,
        "requested_include_logs": include_logs,
        "age_seconds": round(age, 3),
        "max_age_seconds": sampler_max_age,
    }
    return result


def read_sync_coordinator_state() -> dict[str, Any]:
    state = read_json_file(SYNC_COORDINATOR_STATE_FILE, {})
    return state if isinstance(state, dict) else {}


def planned_sync_service(state: dict[str, Any] | None = None) -> str:
    return ""


def docker_compose_project_name() -> str:
    configured = os.environ.get("BDAG_COMPOSE_PROJECT_NAME") or os.environ.get("COMPOSE_PROJECT_NAME")
    if configured:
        return configured
    raw_project_root = os.environ.get("BDAG_PROJECT_ROOT")
    if raw_project_root:
        name = Path(raw_project_root).expanduser().name
        if name:
            return name
    return PROJECT_ROOT.name


def docker_compose_command(*args: str) -> list[str]:
    command = [
        "docker",
        "compose",
        "-p",
        docker_compose_project_name(),
    ]
    if POOL_ENV_FILE.exists():
        command.extend(["--env-file", str(POOL_ENV_FILE)])
    command.extend([
        "-f",
        str(PROJECT_ROOT / "docker-compose.yml"),
        *args,
    ])
    return command


def compose_service_name(name: str) -> str:
    result = run(["docker", "inspect", "-f", '{{ index .Config.Labels "com.docker.compose.service" }}', name], timeout=10)
    service = result.stdout.strip() if result.ok else ""
    if service and service != "<no value>":
        return service
    container_to_service = {
        "postgres": "postgres",
        "node": "node",
        "pool": "pool",
        "dashboard": "dashboard",
    }
    if name in container_to_service:
        return container_to_service[name]
    project_names = unique_names([docker_compose_project_name(), "pool-stack-docker"])
    for project in project_names:
        for sep in ("-", "_"):
            prefix = f"{project}{sep}"
            suffix = f"{sep}1"
            if name.startswith(prefix) and name.endswith(suffix):
                candidate = name[len(prefix) : -len(suffix)]
                if candidate:
                    return candidate
    return name

_COMPOSE_CONTAINER_NAME_CACHE: dict[str, str] = {}


def compose_container_name(name: str) -> str:
    cached = _COMPOSE_CONTAINER_NAME_CACHE.get(name)
    if cached:
        return cached
    project_names = unique_names([docker_compose_project_name(), "pool-stack-docker"])
    for project in project_names:
        result = run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--filter",
                f"label=com.docker.compose.service={name}",
                "--format",
                "{{.Names}}\t{{.Status}}",
            ],
            timeout=8,
        )
        if not result.ok:
            continue
        rows = []
        for line in result.stdout.splitlines():
            parts = line.split("\t", 1)
            container_name = parts[0].strip()
            status = parts[1].strip() if len(parts) > 1 else ""
            if container_name:
                rows.append((container_name, status))
        if rows:
            chosen = next((container for container, status in rows if status.startswith("Up ")), rows[0][0])
            _COMPOSE_CONTAINER_NAME_CACHE[name] = chosen
            return chosen
    return name


def stack_start_services(*, include_pool: bool = True) -> list[str]:
    configured = split_env_list("BDAG_START_SERVICES", "")
    services = configured or STACK_SERVICES or unique_names([POOL_DB_CONTAINER, *NODES, *POOL_CONTAINERS])
    result = unique_names([compose_service_name(service) for service in services])
    if include_pool:
        return result
    return [service for service in result if not pool_start_gate.is_pool_target(service, POOL_CONTAINER)]


def docker_compose_start_command(*, include_pool: bool = True) -> list[str]:
    services = stack_start_services(include_pool=include_pool)
    if not services:
        if not include_pool:
            return []
        return docker_compose_command("up", "-d")
    if not include_pool:
        return docker_compose_command("up", "-d", "--no-deps", *services)
    return docker_compose_command("up", "-d", *services)


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
    five_minute_max = max(DASHBOARD_HISTORY_FIVE_MINUTE_SECONDS, hot_max + DASHBOARD_HISTORY_FIVE_MINUTE_STEP_SECONDS)
    fifteen_minute_max = max(
        DASHBOARD_HISTORY_FIFTEEN_MINUTE_SECONDS,
        five_minute_max + DASHBOARD_HISTORY_FIFTEEN_MINUTE_STEP_SECONDS,
    )
    thirty_minute_max = max(
        DASHBOARD_HISTORY_THIRTY_MINUTE_SECONDS,
        fifteen_minute_max + DASHBOARD_HISTORY_THIRTY_MINUTE_STEP_SECONDS,
    )
    two_hour_max = max(DASHBOARD_HISTORY_TWO_HOUR_SECONDS, thirty_minute_max + DASHBOARD_HISTORY_TWO_HOUR_STEP_SECONDS)
    return [
        DashboardHistoryTier("minute", "ram", 0, hot_max, DASHBOARD_HISTORY_HOT_STEP_SECONDS),
        DashboardHistoryTier("five_minute", "disk", hot_max, five_minute_max, DASHBOARD_HISTORY_FIVE_MINUTE_STEP_SECONDS),
        DashboardHistoryTier(
            "fifteen_minute",
            "disk",
            five_minute_max,
            fifteen_minute_max,
            DASHBOARD_HISTORY_FIFTEEN_MINUTE_STEP_SECONDS,
        ),
        DashboardHistoryTier(
            "thirty_minute",
            "disk",
            fifteen_minute_max,
            thirty_minute_max,
            DASHBOARD_HISTORY_THIRTY_MINUTE_STEP_SECONDS,
        ),
        DashboardHistoryTier("two_hour", "disk", thirty_minute_max, two_hour_max, DASHBOARD_HISTORY_TWO_HOUR_STEP_SECONDS),
    ]


def dashboard_history_bucket_seconds_for_age(age_seconds: float) -> int:
    for tier in dashboard_history_tiers():
        if age_seconds > tier.max_age_seconds:
            continue
        if tier.min_age_seconds and age_seconds <= tier.min_age_seconds:
            continue
        return max(1, tier.step_seconds)
    return max(1, dashboard_history_tiers()[-1].step_seconds)


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


def merge_unique_strings(*values: Any, limit: int | None = None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        items = value if isinstance(value, list) else [value]
        for item in items:
            text = str(item or "").strip()
            if text and text not in seen:
                result.append(text)
                seen.add(text)
                if limit is not None and len(result) >= limit:
                    return result
    return result


def local_ipv4_addresses() -> list[str]:
    addresses: list[str] = []

    def add(value: str) -> None:
        if value.startswith("127.") or value == "0.0.0.0":
            return
        if is_docker_bridge_ipv4(value):
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


def configured_asic_lan_networks() -> list[ipaddress.IPv4Network]:
    raw_values = [
        os.environ.get("BDAG_ASIC_LAN_CIDRS", ""),
        os.environ.get("BDAG_MINER_SCAN_TARGET", ""),
    ]
    networks: list[ipaddress.IPv4Network] = []
    for raw in raw_values:
        for token in re.split(r"[,;\s]+", str(raw or "")):
            token = token.strip()
            if not token:
                continue
            try:
                if "/" in token:
                    network = ipaddress.ip_network(token, strict=False)
                else:
                    network = ipaddress.ip_network(f"{token}/32", strict=False)
            except ValueError:
                continue
            if network.version == 4:
                networks.append(network)
    return networks


def is_lan_ipv4(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if address.version != 4 or is_docker_bridge_ipv4(value):
        return False
    configured_networks = configured_asic_lan_networks()
    if configured_networks:
        return any(address in network for network in configured_networks)
    return (address.is_private or address.is_link_local) and not is_docker_bridge_ipv4(value)


def docker_bridge_networks() -> list[ipaddress.IPv4Network]:
    networks: list[ipaddress.IPv4Network] = []
    raw = os.environ.get("BDAG_DOCKER_BRIDGE_CIDRS", DEFAULT_DOCKER_BRIDGE_CIDRS)
    for token in re.split(r"[,;\s]+", raw):
        token = token.strip()
        if not token:
            continue
        try:
            network = ipaddress.ip_network(token, strict=False)
        except ValueError:
            continue
        if network.version == 4:
            networks.append(network)
    return networks


def is_docker_bridge_ipv4(value: str) -> bool:
    if env_bool("BDAG_ALLOW_DOCKER_BRIDGE_ASIC_IPS", False):
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.version == 4 and any(address in network for network in docker_bridge_networks())


def is_docker_bridge_pool_log_client(ip: str, mac: str = "") -> bool:
    return is_docker_bridge_ipv4(ip)


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
    if str(item.get("earnings_scope") or "") == "payment-wallet-chain-rewards":
        return True
    if item.get("managed") or item.get("configured") or item.get("connected"):
        return True
    if item.get("credit_workers") and (safe_int(item.get("shares"), 0) > 0 or safe_int(item.get("credited_blocks"), 0) > 0):
        return True
    return False


def is_local_asic_earnings_miner(item: dict[str, Any]) -> bool:
    if str(item.get("earnings_scope") or "") == "payment-wallet-chain-rewards":
        return False
    mac = normalize_mac(item.get("mac"))
    identity = str(item.get("identity_key") or item.get("device_id") or "").strip().lower()
    if identity.startswith("mac:"):
        mac = mac or normalize_mac(identity[4:])
    return bool(mac)


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


_NEIGHBOR_MACS_CACHE_EPOCH = 0.0
_NEIGHBOR_MACS_CACHE: dict[str, str] = {}


def read_neighbor_macs(*, use_cache: bool = True) -> dict[str, str]:
    global _NEIGHBOR_MACS_CACHE_EPOCH, _NEIGHBOR_MACS_CACHE
    now = time.time()
    if (
        use_cache
        and NEIGHBOR_MAC_CACHE_SECONDS > 0
        and _NEIGHBOR_MACS_CACHE
        and now - _NEIGHBOR_MACS_CACHE_EPOCH <= NEIGHBOR_MAC_CACHE_SECONDS
    ):
        return dict(_NEIGHBOR_MACS_CACHE)

    result = run(["ip", "neigh", "show"], timeout=5)
    if not result.ok:
        return dict(_NEIGHBOR_MACS_CACHE) if use_cache else {}
    neighbors: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if not parts or not is_ipv4(parts[0]) or is_docker_bridge_ipv4(parts[0]):
            continue
        if "lladdr" not in parts:
            continue
        index = parts.index("lladdr")
        if index + 1 >= len(parts):
            continue
        mac = normalize_mac(parts[index + 1])
        if mac:
            neighbors[parts[0]] = mac
    if use_cache:
        _NEIGHBOR_MACS_CACHE_EPOCH = now
        _NEIGHBOR_MACS_CACHE = dict(neighbors)
    return neighbors


def miner_scan_target_ips() -> set[str]:
    try:
        return set(parse_scan_targets(default_miner_scan_target()))
    except Exception:
        return set()


def miner_dhcp_lease_paths() -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for pattern in MINER_DHCP_LEASE_FILE_PATTERNS:
        expanded = glob.glob(str(Path(pattern).expanduser()))
        if not expanded and not any(char in pattern for char in "*?["):
            expanded = [pattern]
        for item in expanded:
            path = Path(item).expanduser()
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            paths.append(path)
    return paths


def parse_dnsmasq_lease_line(line: str, now_epoch: int | None = None) -> dict[str, Any] | None:
    parts = line.split()
    if len(parts) < 3:
        return None
    try:
        expires_epoch = int(parts[0])
    except ValueError:
        return None
    mac = normalize_mac(parts[1])
    ip = parts[2]
    if not mac or not is_lan_ipv4(ip) or is_docker_bridge_pool_log_client(ip, mac):
        return None
    now_value = seconds_since_epoch() if now_epoch is None else now_epoch
    if expires_epoch > 0 and expires_epoch < now_value - 86400:
        return None
    return {
        "ip": ip,
        "mac": mac,
        "hostname": "" if len(parts) < 4 or parts[3] == "*" else parts[3],
        "lease_expires_epoch": expires_epoch,
        "lease_active": expires_epoch == 0 or expires_epoch >= now_value,
        "device_id": f"mac:{mac}",
        "device_type": "asic",
        "discovered_by": "dhcp-lease",
        "sources": ["dhcp-lease"],
    }


def read_miner_dhcp_leases() -> list[dict[str, Any]]:
    leases: list[dict[str, Any]] = []
    for path in miner_dhcp_lease_paths():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            lease = parse_dnsmasq_lease_line(line)
            if lease:
                lease["lease_file"] = str(path)
                leases.append(lease)
    return leases


def miner_lan_hint_candidates(registry: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    target_ips = miner_scan_target_ips()
    by_identity: dict[str, dict[str, Any]] = {}

    def add(item: Mapping[str, Any], source: str) -> None:
        ip = str(item.get("ip") or "")
        mac = normalize_mac(item.get("mac"))
        if not ip or not is_lan_ipv4(ip) or not mac:
            return
        if target_ips and ip not in target_ips:
            return
        if is_docker_bridge_pool_log_client(ip, mac):
            return
        key = f"mac:{mac}"
        existing = by_identity.get(key, {})
        merged = merge_miner_records(dict(existing), dict(item)) if existing else dict(item)
        merged.update(
            {
                "ip": ip,
                "mac": mac,
                "device_id": key,
                "device_type": "asic",
                "discovered_by": merged.get("discovered_by") or source,
                "sources": merge_unique_strings(merged.get("sources"), source),
                "ip_history": merge_unique_strings(merged.get("ip_history"), ip),
            }
        )
        by_identity[key] = merged

    for item in (registry or {}).get("miners", []) if isinstance(registry, dict) else []:
        if isinstance(item, Mapping):
            add(item, str(item.get("discovered_by") or "registry"))
    for lease in read_miner_dhcp_leases():
        add(lease, "dhcp-lease")
    for ip, mac in read_neighbor_macs().items():
        add({"ip": ip, "mac": mac, "discovered_by": "arp-neighbor"}, "arp-neighbor")
    return sorted(by_identity.values(), key=lambda item: (str(item.get("mac") or ""), str(item.get("ip") or "")))


def augment_miner_registry_with_lan_hints(registry: dict[str, Any]) -> dict[str, Any]:
    miners = [dict(item) for item in registry.get("miners", []) if isinstance(item, dict)]
    existing_by_mac = {normalize_mac(item.get("mac")): item for item in miners if normalize_mac(item.get("mac"))}
    existing_by_ip = {str(item.get("ip") or ""): item for item in miners if item.get("ip")}
    defaults = default_miner_pool_settings()
    changed = False
    for hint in miner_lan_hint_candidates({"miners": miners}):
        ip = str(hint.get("ip") or "")
        mac = normalize_mac(hint.get("mac"))
        if not ip or not mac:
            continue
        if retired_miner_identity_decision(hint, ip, mac).get("retired"):
            continue
        item = existing_by_mac.get(mac) or existing_by_ip.get(ip)
        if item is None:
            item = {
                "ip": ip,
                "mac": mac,
                "device_id": f"mac:{mac}",
                "device_type": "asic",
                "discovered_by": hint.get("discovered_by") or "lan-hint",
                "sources": [],
                "managed": False,
                "last_configured_ok": False,
            }
            miners.append(item)
            changed = True
        old_ip = str(item.get("ip") or "")
        if item.get("ip") != ip:
            item["ip"] = ip
            changed = True
        if normalize_mac(item.get("mac")) != mac:
            item["mac"] = mac
            item["device_id"] = f"mac:{mac}"
            changed = True
        for key in ("hostname", "lease_expires_epoch", "lease_active", "lease_file"):
            if hint.get(key) not in (None, "", []):
                item[key] = hint[key]
        before_sources = list(item.get("sources") or [])
        item["sources"] = merge_unique_strings(item.get("sources"), hint.get("sources"), "lan-hint")
        item["ip_history"] = merge_unique_strings(item.get("ip_history"), old_ip, ip)
        item["expected_pool_url"] = item.get("expected_pool_url") or defaults["pool_url"]
        item["expected_worker_user"] = item.get("expected_worker_user") or defaults["worker_user"]
        if item.get("sources") != before_sources:
            changed = True
        existing_by_mac[mac] = item
        existing_by_ip[ip] = item
    if changed:
        assign_miner_display_names(miners)
    return {**registry, "miners": miners}


def augment_miner_registry_with_expected_macs(registry: dict[str, Any]) -> dict[str, Any]:
    expected = expected_asic_macs()
    if not expected:
        return registry
    miners = [dict(item) for item in registry.get("miners", []) if isinstance(item, dict)]
    by_mac = {normalize_mac(item.get("mac")): item for item in miners if normalize_mac(item.get("mac"))}
    defaults = default_miner_pool_settings()
    changed = False
    for mac in expected:
        item = by_mac.get(mac)
        if item is None:
            item = {
                "ip": "",
                "mac": mac,
                "device_id": f"mac:{mac}",
                "device_type": "asic",
                "discovered_by": "expected-mac",
                "sources": ["expected-mac"],
                "managed": True,
                "last_configured_ok": False,
            }
            miners.append(item)
            by_mac[mac] = item
            changed = True
        before = dict(item)
        item["mac"] = mac
        item["device_id"] = f"mac:{mac}"
        item["device_type"] = "asic"
        item["managed"] = True
        item["expected_pool_url"] = item.get("expected_pool_url") or defaults["pool_url"]
        item["expected_worker_user"] = item.get("expected_worker_user") or defaults["worker_user"]
        item["sources"] = merge_unique_strings(item.get("sources"), "expected-mac")
        if item != before:
            changed = True
    if changed:
        assign_miner_display_names(miners)
    return {**registry, "miners": miners}


def mac_for_ip(ip: str, neighbors: dict[str, str] | None = None) -> str:
    if not is_lan_ipv4(ip):
        return ""
    if neighbors is None:
        neighbors = read_neighbor_macs()
    return normalize_mac(neighbors.get(ip))


def miner_mac_from_payload(miner: dict[str, Any], ip: str, neighbors: dict[str, str] | None = None) -> str:
    for key in ("mac", "mac_address", "macAddress", "ethaddr", "hwaddr", "name"):
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


def miner_activity_epoch(item: Mapping[str, Any], epoch_keys: tuple[str, ...], timestamp_keys: tuple[str, ...]) -> int:
    values: list[int] = []
    for key in epoch_keys:
        epoch = safe_int(item.get(key), 0)
        if epoch > 0:
            values.append(epoch)
    for key in timestamp_keys:
        raw = item.get(key)
        if not raw:
            continue
        epoch = _pool_log_epoch(str(raw))
        if epoch is not None and epoch > 0:
            values.append(int(epoch))
    return max(values or [0])


def miner_activity_has_timestamp(
    item: Mapping[str, Any],
    epoch_keys: tuple[str, ...],
    timestamp_keys: tuple[str, ...],
) -> bool:
    return any(safe_int(item.get(key), 0) > 0 for key in epoch_keys) or any(bool(item.get(key)) for key in timestamp_keys)


def miner_activity_is_fresh(
    item: Mapping[str, Any],
    now_epoch: int,
    epoch_keys: tuple[str, ...],
    timestamp_keys: tuple[str, ...],
    stale_seconds: int = POOL_CONNECTED_STALE_SECONDS,
) -> bool:
    if not item:
        return False
    epoch = miner_activity_epoch(item, epoch_keys, timestamp_keys)
    if epoch > 0:
        return now_epoch - epoch <= stale_seconds
    return not miner_activity_has_timestamp(item, epoch_keys, timestamp_keys)


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
    result["last_ports"] = merge_unique_strings(
        existing.get("last_ports"),
        incoming.get("last_ports"),
        limit=MINER_REGISTRY_MAX_PORTS,
    )
    incoming_extranonces = merge_unique_strings(
        incoming.get("last_pool_job_extranonces"),
        limit=MINER_REGISTRY_MAX_JOB_EXTRANONCES,
    )
    result["last_pool_job_extranonces"] = (
        incoming_extranonces
        if incoming_extranonces
        else merge_unique_strings(
            existing.get("last_pool_job_extranonces"),
            limit=MINER_REGISTRY_MAX_JOB_EXTRANONCES,
        )
    )
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


def expected_asic_macs() -> list[str]:
    """Return the canonical MAC-only expected ASIC lane list."""
    return split_mac_env_list("BDAG_ASIC_EXPECTED_MACS", "")


def expected_asic_mac_set() -> set[str]:
    return set(expected_asic_macs())


def is_expected_asic_mac(value: Any) -> bool:
    mac = normalize_mac(value)
    return bool(mac and mac in expected_asic_mac_set())


def asic_mac_override_entries(registry: dict[str, Any] | None = None) -> list[tuple[str, str]]:
    """Return verified host-visible ASIC IP to MAC mappings for the pool.

    The IP is only a current connection route into the ASIC. The durable lane
    identity is the MAC, and rows without a proven MAC are excluded so the pool
    never promotes an IP address into an ASIC identity.
    """
    if registry is None:
        registry = read_miner_registry()
    by_ip: dict[str, str] = {}
    neighbors = read_neighbor_macs()
    now_epoch = seconds_since_epoch()
    expected_macs = expected_asic_mac_set()
    miners = registry.get("miners") if isinstance(registry, dict) else []
    for row in miners if isinstance(miners, list) else []:
        if not isinstance(row, dict):
            continue
        ip = str(row.get("ip") or "")
        if not is_lan_ipv4(ip) or is_docker_bridge_ipv4(ip):
            continue
        recent_pool_route = bool(
            safe_int(row.get("last_pool_seen_epoch"), 0) > 0
            and now_epoch - safe_int(row.get("last_pool_seen_epoch"), 0) <= POOL_CONNECTED_STALE_SECONDS
        )
        active_or_configured = bool(
            row.get("managed")
            or row.get("configured")
            or row.get("last_configured_ok")
            or row.get("connected")
            or row.get("pool_active")
            or row.get("work_pool_active")
            or recent_pool_route
            or normalize_mac(row.get("mac")) in expected_macs
        )
        if not active_or_configured:
            continue
        mac = normalize_mac(neighbors.get(ip)) or normalize_mac(row.get("mac"))
        if not mac:
            continue
        device_type = str(row.get("device_type") or "").strip().lower()
        if device_type and device_type != "asic" and not bool(row.get("managed") or row.get("last_configured_ok")):
            continue
        by_ip[ip] = mac
    return sorted(by_ip.items(), key=lambda item: ipaddress.ip_address(item[0]))


def pool_asic_mac_overrides_value(registry: dict[str, Any] | None = None) -> str:
    return ",".join(f"{ip}={mac}" for ip, mac in asic_mac_override_entries(registry))


def pool_asic_mac_override_diagnostics(registry: dict[str, Any] | None = None) -> dict[str, Any]:
    if registry is None:
        registry = read_miner_registry()
    override_entries = asic_mac_override_entries(registry)
    override_ips = {ip for ip, _mac in override_entries}
    unresolved: list[dict[str, Any]] = []
    miners = registry.get("miners") if isinstance(registry, dict) else []
    for row in miners if isinstance(miners, list) else []:
        if not isinstance(row, dict):
            continue
        ip = str(row.get("ip") or "")
        if not is_lan_ipv4(ip) or ip in override_ips or is_docker_bridge_ipv4(ip):
            continue
        device_type = str(row.get("device_type") or "").strip().lower()
        if device_type != "asic" and not bool(row.get("managed") or row.get("last_configured_ok")):
            continue
        if normalize_mac(row.get("mac")):
            continue
        unresolved.append(
            {
                "ip": ip,
                "display_label": miner_display_label(row),
                "managed": bool(row.get("managed")),
                "configured": bool(row.get("last_configured_ok") or row.get("configured")),
                "issue": "asic_mac_unresolved",
            }
        )
    return {
        "override_value": ",".join(f"{ip}={mac}" for ip, mac in override_entries),
        "override_count": len(override_entries),
        "overrides": [{"ip": ip, "mac": mac, "lane_id": f"mac:{mac}"} for ip, mac in override_entries],
        "unresolved_count": len(unresolved),
        "unresolved": unresolved,
        "identity_basis": "mac",
    }


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
            duplicate_old_asic_route = ip in asic_known_ips and ip not in asic_current_ips
            if stale or duplicate_old_asic_route:
                continue
        pruned.append(item)
    return pruned


def default_miner_scan_target() -> str:
    configured = os.environ.get("BDAG_MINER_SCAN_TARGET")
    if configured:
        return configured
    configured_cidrs = os.environ.get("BDAG_ASIC_LAN_CIDRS")
    if configured_cidrs:
        return configured_cidrs
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


def read_miner_registry(*, augment_lan_hints: bool = True) -> dict[str, Any]:
    registry = read_json_file(MINER_REGISTRY_FILE, {"updated_at": None, "miners": []})
    if not isinstance(registry, dict):
        return {"updated_at": None, "miners": []}
    miners = registry.get("miners")
    if not isinstance(miners, list):
        registry["miners"] = []
    registry = augment_miner_registry_with_expected_macs(registry)
    if not augment_lan_hints:
        return registry
    return augment_miner_registry_with_lan_hints(registry)


def read_miner_registry_without_lan_hints() -> dict[str, Any]:
    try:
        return read_miner_registry(augment_lan_hints=False)
    except TypeError:
        # Some unit tests monkeypatch read_miner_registry with a zero-argument
        # fixture. Preserve that test seam while production callers use the
        # bounded no-augmentation path above.
        return read_miner_registry()


def save_miner_registry(miners: list[dict[str, Any]]) -> dict[str, Any]:
    neighbors = read_neighbor_macs()
    by_identity: dict[str, dict[str, Any]] = {}
    for miner in sorted(miners, key=miner_observation_epoch):
        ip = str(miner.get("ip", ""))
        if not is_ipv4(ip):
            continue
        item = dict(miner)
        if is_docker_bridge_ipv4(ip):
            continue
        mac = miner_mac_from_payload(item, ip, neighbors)
        retirement_decision = retired_miner_identity_decision(item, ip, mac)
        if retirement_decision.get("retired"):
            continue
        if retirement_decision.get("conflict") and not mac:
            # A no-MAC pool-log observation at a retired miner's last known IP
            # is not a stable identity. Keep it out of the local managed
            # registry until a MAC proves this is a different physical ASIC.
            continue
        if mac:
            item["mac"] = mac
            item["device_id"] = f"mac:{mac}"
        item["ip_history"] = merge_unique_strings(item.get("ip_history"), ip)
        likely_asic = str(item.get("device_type") or "").lower() == "asic" or bool(
            item.get("managed") or item.get("last_configured_ok")
        )
        if not mac and likely_asic:
            continue
        key = f"mac:{mac}" if mac else f"stratum:{ip}"
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
        device_id = f"mac:{mac}" if mac else ""
        item.update(
            {
                "ip": ip,
                "mac": mac or item.get("mac", ""),
                "device_id": device_id,
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
        device_id = f"mac:{mac}" if mac else ""
        item.update(
            {
                "ip": ip,
                "mac": mac or item.get("mac", ""),
                "device_id": device_id,
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


def get_miner_settings(ip: str, timeout: float = MINER_HTTP_TIMEOUT) -> dict[str, Any]:
    try:
        response = miner_request(ip, "/mcb/setting", timeout=timeout)
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
    settings = get_miner_settings(ip, timeout=timeout)
    identity_payload = {**settings, **status}
    active_pool = next((pool for pool in pools if pool.get("active")), pools[0] if pools else {})
    mac = miner_mac_from_payload(identity_payload, ip)
    return {
        "ip": ip,
        "mac": mac,
        "device_id": f"mac:{mac}" if mac else "",
        "name": settings.get("name", ""),
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
            discovered_for_guard: dict[str, Any] = {}
            try:
                discovered_for_guard = discover_miner(ip, timeout=MINER_HTTP_TIMEOUT)
            except Exception:
                discovered_for_guard = {}
            guard_mac = normalize_mac(discovered_for_guard.get("mac")) or mac_for_ip(ip)
            guard_decision = retired_miner_identity_decision({**discovered_for_guard, "ip": ip, "mac": guard_mac}, ip, guard_mac)
            if guard_decision.get("retired"):
                results.append(
                    {
                        "ip": ip,
                        "mac": guard_mac,
                        "status": "skipped",
                        "reason": "retired-miner-mac",
                        "retired_name": guard_decision.get("retired_name") or "",
                    }
                )
                continue
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
    skipped = [item for item in results if item.get("status") == "skipped"]
    status = (
        "failed"
        if len(failed) == len(results) and results
        else "skipped"
        if len(skipped) == len(results) and results
        else "partial"
        if failed or partial or skipped
        else "ok"
    )
    return {"status": status, "finished_at": now_iso(), "results": results}


def docker_inspect(names: list[str]) -> dict[str, dict[str, Any]]:
    requested_to_actual = {name: compose_container_name(name) for name in names}
    actual_names = unique_names(list(requested_to_actual.values()))
    result = run(["docker", "inspect", *actual_names], timeout=12)
    payload: list[dict[str, Any]] = []
    if result.ok:
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = []
    else:
        for name in actual_names:
            single = run(["docker", "inspect", name], timeout=8)
            if not single.ok:
                continue
            try:
                payload.extend(json.loads(single.stdout))
            except json.JSONDecodeError:
                continue

    payload_names = {str(item.get("Name", "")).lstrip("/") for item in payload}
    for requested, actual in list(requested_to_actual.items()):
        if actual in payload_names:
            continue
        if _COMPOSE_CONTAINER_NAME_CACHE.get(requested) == actual:
            _COMPOSE_CONTAINER_NAME_CACHE.pop(requested, None)
        refreshed_actual = compose_container_name(requested)
        requested_to_actual[requested] = refreshed_actual
        if refreshed_actual in payload_names or refreshed_actual in actual_names:
            continue
        refreshed = run(["docker", "inspect", refreshed_actual], timeout=8)
        actual_names.append(refreshed_actual)
        if not refreshed.ok:
            continue
        try:
            refreshed_payload = json.loads(refreshed.stdout)
        except json.JSONDecodeError:
            continue
        payload.extend(refreshed_payload)
        payload_names.update(str(item.get("Name", "")).lstrip("/") for item in refreshed_payload)

    inspected: dict[str, dict[str, Any]] = {}
    for item in payload:
        name = str(item.get("Name", "")).lstrip("/")
        state = item.get("State") or {}
        config = item.get("Config") or {}
        labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
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
        record = {
            "name": name,
            "runtime_name": name,
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
        }
        inspected[name] = record
        service_label = str(labels.get("com.docker.compose.service") or "")
        for requested, actual in requested_to_actual.items():
            if actual == name or requested == service_label:
                inspected[requested] = dict(record)
    return inspected


def docker_top(name: str) -> str:
    return run(["docker", "top", compose_container_name(name)], timeout=8).stdout


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
    result = run(["docker", "logs", "-n", str(lines), compose_container_name(name)], timeout=12)
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


def discover_observer_node_services() -> list[str]:
    configured = list(OBSERVER_NODES)
    result = run(["docker", "ps", "-a", "--format", "{{.Names}}"], timeout=8)
    if not result.ok:
        return configured
    discovered = [
        line.strip()
        for line in result.stdout.splitlines()
        if re.fullmatch(r"bdag-observer-node-\d+", line.strip())
    ]
    return unique_names([*configured, *discovered])


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
    graph_sync_lines = [
        line
        for line in recent
        if "Syncing graph state" in line or "The sync of graph state has ended" in line
    ]
    template_freeze_lines = [
        line
        for line in recent
        if "TEMPLATE FREEZE DETECTED" in line or "Same parent hash for" in line
    ]
    last_template_freeze_line = template_freeze_lines[-1] if template_freeze_lines else None
    freeze_age_match = FREEZE_RE.search(last_template_freeze_line or "")
    template_freeze_age_seconds = float(freeze_age_match.group(1)) if freeze_age_match else None
    orphan_error_lines = [
        line
        for line in recent
        if "already have block (orphan)" in line
    ]
    dag_tip_damage_lines = [
        line
        for line in recent
        if "The dag data was damaged" in line
        or "Can't find tip:" in line
        or "Tip is missing block data" in line
    ]
    busy_syncing_lines = [
        line
        for line in recent
        if "node busy syncing" in line.lower()
    ]
    graph_sync_churn = bool(
        len(graph_sync_lines) >= NODE_GRAPH_SYNC_CHURN_COUNT
        and len(imported_lines) == 0
    )
    template_freeze = bool(
        template_freeze_age_seconds is not None
        and template_freeze_age_seconds >= POOL_TEMPLATE_FREEZE_SECONDS
    )
    not_dag_block_lines = [line for line in recent if NODE_NOT_DAG_BLOCK_RE.search(line)]
    irreparable_sync_lines = [line for line in recent if NODE_IRREPARABLE_SYNC_RE.search(line)]
    missing_trie_lines = [line for line in recent if NODE_MISSING_TRIE_RE.search(line)]
    rawdb_not_found_lines = [line for line in recent if NODE_RAWDB_PEBBLE_NOT_FOUND_RE.search(line)]
    rawdb_freezer_missing_header_lines = [
        line for line in recent if NODE_RAWDB_FREEZER_MISSING_HEADER_RE.search(line)
    ]
    dag_empty_block_lines = [line for line in recent if NODE_DAG_EMPTY_BLOCK_RE.search(line)]
    dag_order_missing_lines = [line for line in recent if NODE_DAG_ORDER_MISSING_RE.search(line)]
    state_history_truncate_lines = [line for line in recent if NODE_STATE_HISTORY_TRUNCATE_RE.search(line)]
    rawdb_not_found_storm = bool(
        len(rawdb_not_found_lines) >= CHAIN_STATE_RAWDB_NOT_FOUND_RESTORE_WARNINGS
        and len(imported_lines) == 0
    )
    rawdb_freezer_missing_header_storm = bool(
        len(rawdb_freezer_missing_header_lines) >= CHAIN_STATE_RAWDB_FREEZER_MISSING_HEADER_WARNINGS
        and len(imported_lines) == 0
    )
    dag_empty_block_storm = bool(
        len(dag_empty_block_lines) >= NODE_DAG_EMPTY_BLOCK_STORM_COUNT
        and len(imported_lines) == 0
    )
    blocker_hash = ""
    for line in reversed(irreparable_sync_lines + not_dag_block_lines):
        match = NODE_IRREPARABLE_SYNC_RE.search(line) or NODE_NOT_DAG_BLOCK_RE.search(line)
        if match:
            blocker_hash = match.group(1)
            break
    chain_state_blocker = bool(irreparable_sync_lines or not_dag_block_lines)
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
        or NODE_DAG_ORDER_MISSING_RE.search(line)
        or "Irreparable error" in line
        or "Not DAG block:" in line
        or "The dag data was damaged" in line
        or "Can't find tip:" in line
        or (rawdb_not_found_storm and NODE_RAWDB_PEBBLE_NOT_FOUND_RE.search(line))
        or (rawdb_freezer_missing_header_storm and NODE_RAWDB_FREEZER_MISSING_HEADER_RE.search(line))
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
        "dag_tip_damage": bool(dag_tip_damage_lines),
        "dag_tip_damage_lines": dag_tip_damage_lines[-5:],
        "chain_state_blocker": chain_state_blocker,
        "chain_state_blocker_hash": blocker_hash,
        "chain_state_blocker_lines": (irreparable_sync_lines + not_dag_block_lines)[-5:],
        "missing_trie_node_warnings": len(missing_trie_lines),
        "missing_trie_node_lines": missing_trie_lines[-5:],
        "rawdb_pebble_not_found_warnings": len(rawdb_not_found_lines),
        "rawdb_pebble_not_found_storm": rawdb_not_found_storm,
        "rawdb_pebble_not_found_threshold": CHAIN_STATE_RAWDB_NOT_FOUND_RESTORE_WARNINGS,
        "rawdb_pebble_not_found_lines": rawdb_not_found_lines[-5:],
        "rawdb_freezer_missing_header_warnings": len(rawdb_freezer_missing_header_lines),
        "rawdb_freezer_missing_header_storm": rawdb_freezer_missing_header_storm,
        "rawdb_freezer_missing_header_threshold": CHAIN_STATE_RAWDB_FREEZER_MISSING_HEADER_WARNINGS,
        "rawdb_freezer_missing_header_lines": rawdb_freezer_missing_header_lines[-5:],
        "dag_empty_block_warnings": len(dag_empty_block_lines),
        "dag_empty_block_storm": dag_empty_block_storm,
        "dag_empty_block_threshold": NODE_DAG_EMPTY_BLOCK_STORM_COUNT,
        "dag_empty_block_lines": dag_empty_block_lines[-5:],
        "dag_order_missing": bool(dag_order_missing_lines),
        "dag_order_missing_lines": dag_order_missing_lines[-5:],
        "state_history_truncate_failure": bool(state_history_truncate_lines),
        "state_history_truncate_lines": state_history_truncate_lines[-5:],
        "p2p_error_lines": (invalid_peer_lines + p2p_stream_lines)[-5:],
        "node_graph_sync_count": len(graph_sync_lines),
        "node_graph_sync_churn": graph_sync_churn,
        "node_graph_sync_churn_threshold": NODE_GRAPH_SYNC_CHURN_COUNT,
        "node_graph_sync_churn_lines": graph_sync_lines[-5:],
        "node_template_freeze_count": len(template_freeze_lines),
        "node_template_freeze_age_seconds": template_freeze_age_seconds,
        "node_template_frozen": template_freeze,
        "node_template_freeze_lines": template_freeze_lines[-5:],
        "mining_template_error_count": len(template_error_lines),
        "mining_template_hard_error_count": len(template_hard_error_lines),
        "mining_template_transient_tx_error_count": len(template_transient_tx_error_lines),
        "mining_template_nonce_too_low_count": len(template_nonce_too_low_lines),
        "mining_template_error_lines": template_error_lines[-5:],
        "mining_template_hard_error_lines": template_hard_error_lines[-5:],
        "mining_template_failing": len(template_hard_error_lines) >= 3,
        "node_busy_syncing": bool(busy_syncing_lines or graph_sync_churn or template_freeze),
        "node_busy_syncing_lines": (busy_syncing_lines + graph_sync_lines + template_freeze_lines)[-5:],
        "critical": bool(critical_lines),
        "critical_lines": critical_lines[-5:],
        "tail": recent[-24:],
    }


def chain_data_restore_hard_reasons(node: str, info: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    if info.get("chain_state_blocker"):
        blocker_hash = info.get("chain_state_blocker_hash") or "unknown block"
        reasons.append(
            f"{node} chain state is stuck on irreparable sync block {blocker_hash}"
        )
    if info.get("dag_tip_damage"):
        reasons.append(f"{node} DAG tip/block data is damaged")
    if info.get("dag_order_missing"):
        reasons.append(
            f"{node} DAG order index is missing block data; restore or resync from a verified source"
        )
    if info.get("state_history_truncate_failure"):
        reasons.append(
            f"{node} EVM state history freezer is inconsistent; restore or resync from a verified source"
        )
    rawdb_not_found_count = safe_int(info.get("rawdb_pebble_not_found_warnings"), 0)
    if info.get("rawdb_pebble_not_found_storm"):
        reasons.append(
            f"{node} raw chain database is repeatedly missing Pebble keys "
            f"({rawdb_not_found_count} RAWDB not-found warning(s)); restore or resync from a verified source"
        )
    rawdb_freezer_missing_header_count = safe_int(info.get("rawdb_freezer_missing_header_warnings"), 0)
    if info.get("rawdb_freezer_missing_header_storm"):
        reasons.append(
            f"{node} raw chain freezer is repeatedly missing block headers "
            f"({rawdb_freezer_missing_header_count} freezer warning(s)); restore or resync from a verified source"
        )
    return reasons


def chain_data_restore_candidate_reasons(node: str, info: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    missing_trie_count = safe_int(info.get("missing_trie_node_warnings"), 0)
    if missing_trie_count >= CHAIN_STATE_MISSING_TRIE_RESTORE_WARNINGS:
        reasons.append(
            f"{node} reported {missing_trie_count} missing-trie state warning(s); "
            "treating as diagnostic unless imports stall or hard chain-state corruption is also present"
        )
    peer_ahead = safe_int(info.get("peer_ahead_blocks"), 0)
    if (
        info.get("orphan_block_error_storm")
        and peer_ahead >= CHAIN_STATE_ORPHAN_STORM_RESTORE_PEER_AHEAD_BLOCKS
    ):
        reasons.append(
            f"{node} has repeated orphan-only sync errors while {peer_ahead} blocks behind peers"
        )
    return reasons


def parse_pool_log(log: str) -> dict[str, Any]:
    lines = [line for line in strip_ansi(log).splitlines() if line.strip()]
    recent = lines[-600:]
    text = "\n".join(recent)
    initial_download_lines = [line for line in recent if "Client in initial download" in line]
    last_initial_download_line = initial_download_lines[-1] if initial_download_lines else None
    last_initial_download_age_seconds = _pool_log_age_seconds(last_initial_download_line)
    initial_download = bool(
        last_initial_download_line
        and (
            last_initial_download_age_seconds is None
            or last_initial_download_age_seconds <= POOL_INITIAL_DOWNLOAD_RECENT_SECONDS
        )
    )
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
        if "[SUBMIT-STALL]" in line
    ]
    submit_stall_events = [_parse_submit_stall_event(line) for line in submit_stall_event_lines]
    submit_stall_recovery_events = [
        event
        for event in submit_stall_events
        if event.get("action") == "invalidated"
    ]
    expired_job_reconnect_lines = [
        line
        for line in recent
        if "[RECOVERY] reconnecting stale miner" in line and "expired-job recovery attempts" in line
    ]
    last_expired_job_reconnect_line = expired_job_reconnect_lines[-1] if expired_job_reconnect_lines else None
    last_expired_job_reconnect_epoch = _pool_log_epoch(last_expired_job_reconnect_line)
    auth_after_expired_reconnect_lines = [
        line
        for line in recent
        if last_expired_job_reconnect_epoch is not None
        and AUTH_ACCEPT_RE.search(line)
        and (_pool_log_epoch(line) or 0) >= last_expired_job_reconnect_epoch
    ]
    valid_share_after_expired_reconnect_lines = [
        line
        for line in recent
        if last_expired_job_reconnect_epoch is not None
        and VALID_SHARE_RE.search(line)
        and (_pool_log_epoch(line) or 0) >= last_expired_job_reconnect_epoch
    ]
    timeout_after_expired_reconnect_lines = [
        line
        for line in recent
        if last_expired_job_reconnect_epoch is not None
        and "read error:" in line
        and "i/o timeout" in line
        and (_pool_log_epoch(line) or 0) >= last_expired_job_reconnect_epoch
    ]
    last_expired_job_reauthorize_line = (
        auth_after_expired_reconnect_lines[-1] if auth_after_expired_reconnect_lines else None
    )
    last_expired_job_timeout_line = (
        timeout_after_expired_reconnect_lines[-1] if timeout_after_expired_reconnect_lines else None
    )
    expired_job_reconnect_failed_no_share = bool(
        last_expired_job_reconnect_line
        and auth_after_expired_reconnect_lines
        and not valid_share_after_expired_reconnect_lines
        and timeout_after_expired_reconnect_lines
    )
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
        "initial_download_count": len(initial_download_lines),
        "last_initial_download_age_seconds": last_initial_download_age_seconds,
        "initial_download_recent_seconds": POOL_INITIAL_DOWNLOAD_RECENT_SECONDS,
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
        "expired_job_reconnect_count": len(expired_job_reconnect_lines),
        "expired_job_reconnect_failed_no_share": expired_job_reconnect_failed_no_share,
        "expired_job_reconnect_failure_reason": (
            "stale-client expired-job reconnect reauthorized, produced no valid shares, then timed out"
            if expired_job_reconnect_failed_no_share
            else ""
        ),
        "expired_job_reconnect_last_at": _parse_log_timestamp(last_expired_job_reconnect_line),
        "expired_job_reconnect_last_age_seconds": _pool_log_age_seconds(last_expired_job_reconnect_line),
        "expired_job_reconnect_last_line": last_expired_job_reconnect_line,
        "expired_job_reauthorize_after_reconnect_count": len(auth_after_expired_reconnect_lines),
        "expired_job_reauthorize_last_at": _parse_log_timestamp(last_expired_job_reauthorize_line),
        "expired_job_client_timeout_after_reconnect_count": len(timeout_after_expired_reconnect_lines),
        "expired_job_client_timeout_last_at": _parse_log_timestamp(last_expired_job_timeout_line),
        "expired_job_client_timeout_last_line": last_expired_job_timeout_line,
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
RECOVERY_JOB_TO_CLIENT_RE = re.compile(
    r"resending current job to ((?:\d{1,3}\.){3}\d{1,3}):([0-9]+).*?\(job=([^\s)]+)"
)
CLIENT_ADDR_RE = re.compile(r"\bclient=((?:\d{1,3}\.){3}\d{1,3}):([0-9]+)")
STRATUM_ACCEPT_RE = re.compile(
    r"\[STRATUM\]\s+accepted client\s+addr=((?:\d{1,3}\.){3}\d{1,3}):([0-9]+).*?"
    r"(?:\blane=mac:([0-9A-Fa-f:.-]+)|\bmac=([0-9A-Fa-f:.-]+))"
)
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
    for name in NODES:
        ip = docker_container_ip(name)
        if valid_ipv4(ip):
            urls.append((name, f"http://{ip}:{NODE_MINING_RPC_PORT}"))
    return urls


def mining_rpc_call(url: str, method: str, params: list[Any], timeout: float = NODE_TEMPLATE_PROBE_TIMEOUT) -> Any:
    credentials = f"{NODE_MINING_RPC_USER}:{NODE_MINING_RPC_PASS}".encode("utf-8")
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
    elif "invalidated" in line:
        action = "invalidated"

    epoch = _pool_log_epoch(line)
    return {
        "action": action,
        "reason": _parse_key_value_from_log(line, "reason"),
        "backend": _parse_key_value_from_log(line, "backend"),
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
    pool_has_recent_paid_work: bool,
) -> dict[str, Any]:
    source = selected_source_health if isinstance(selected_source_health, dict) else {}
    job_health = source_job_health if isinstance(source_job_health, dict) else {}
    checks: dict[str, bool] = {}
    for key in (
        "node_mineable",
        "node_submit_ready",
        "node_p2p_mining_fresh",
        "healthy",
        "ws_connected",
        "ws_connected_observed",
        "template_delivery_effective",
    ):
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
    contradiction = bool(pool_has_recent_paid_work and (node_unready or job_unready))
    hard_unready = bool((node_unready or job_unready) and not pool_has_recent_paid_work)
    return {
        "version": 1,
        "selected_backend": selected_backend,
        "pool_has_recent_mining": pool_has_recent_paid_work,
        "pool_has_recent_paid_work": pool_has_recent_paid_work,
        "job_health_ok": job_ok,
        "checks": checks,
        "contradiction": contradiction,
        "hard_unready": hard_unready,
        "advisory_degraded": bool(contradiction),
        "truth_basis": "only recent accepted block submission may override readiness; accepted shares alone are not paid mining",
    }


def selected_backend_source_degradation(selected_source_degraded: bool, pool_has_recent_paid_work: bool) -> dict[str, bool]:
    degraded = bool(selected_source_degraded)
    recent_paid = bool(pool_has_recent_paid_work)
    return {
        "degraded": degraded,
        "hard": bool(degraded and not recent_paid),
        "advisory": bool(degraded and recent_paid),
    }


def annotate_template_delivery_state(source_backend_health: dict[str, dict[str, Any]]) -> None:
    for row in source_backend_health.values():
        if not isinstance(row, dict):
            continue
        if "ws_connected" in row and "ws_connected_observed" not in row:
            row["ws_connected_observed"] = bool(row.get("ws_connected"))
        template_ages = [
            value
            for value in (
                safe_float(row.get("template_age_seconds")),
                safe_float(row.get("node_template_age_seconds")),
                safe_float(row.get("node_last_template_build_age_seconds")),
            )
            if value is not None
        ]
        freshest_template_age = min(template_ages) if template_ages else None
        template_fresh = bool(
            freshest_template_age is not None
            and freshest_template_age <= POOL_TEMPLATE_DELIVERY_FRESH_SECONDS
        )
        health_ready = bool(
            row.get("healthy") is True
            and row.get("node_mineable") is True
            and row.get("node_submit_ready") is True
            and row.get("node_p2p_mining_fresh") is not False
        )
        effective = bool(row.get("ws_connected") or (template_fresh and health_ready))
        row["template_delivery_effective"] = effective
        row["template_delivery_fresh_age_seconds"] = (
            round(freshest_template_age, 3) if freshest_template_age is not None else None
        )
        if row.get("ws_connected"):
            row["template_delivery_mode"] = "websocket"
            row["template_delivery_reason"] = "backend reports websocket template stream connected"
        elif effective:
            row["template_delivery_mode"] = "fresh-template-fallback"
            row["template_delivery_reason"] = (
                "backend websocket metric is disconnected, but fresh templates and submit readiness are observed"
            )
        else:
            row["template_delivery_mode"] = "polling-or-stale"
            row["template_delivery_reason"] = "backend websocket metric is disconnected and fresh templates are not proven"


def collect_pool_prometheus_metrics(containers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "generated_at": now_iso(),
        "status": "unavailable",
        "error": "",
        "containers": {},
        "active_connections": None,
        "selected_backend": "",
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

        try:
            raw_job_state = fetch_text_url(
                f"http://{endpoint}/health/job-state",
                {"accept": "application/json", "user-agent": HTTP_USER_AGENT},
                timeout=POOL_METRICS_TIMEOUT,
            )
            job_state = json.loads(raw_job_state)
            if isinstance(job_state, dict):
                row["job_state_status"] = "ok"
                source_job_health["job_state_available"] = True
                source_job_health["status"] = str(job_state.get("status") or "")
                source_job_health["reason_code"] = str(job_state.get("reason_code") or "")
                source_job_health["active_connections"] = safe_int(job_state.get("active_connections"), 0)
                source_job_health["authorized_miners"] = safe_int(job_state.get("authorized_connections"), 0)
                source_job_health["subscribed_miners"] = safe_int(job_state.get("subscribed_connections"), 0)
                source_job_health["ready_miners"] = safe_int(job_state.get("ready_connections"), 0)
                source_job_health["current_template_seq"] = safe_int(job_state.get("current_template_seq"), 0)
                source_job_health["current_parent"] = str(job_state.get("current_parent") or "")
                source_job_health["last_broadcast_age_ms"] = safe_int(job_state.get("last_broadcast_age_ms"), 0)
                clients = source_job_health.setdefault("clients", [])
                if isinstance(clients, list):
                    clients.extend(
                        dict(client)
                        for client in (job_state.get("clients") or [])
                        if isinstance(client, dict)
                    )
            else:
                row["job_state_status"] = "invalid"
        except Exception as exc:  # noqa: BLE001 - job-state augments Prometheus and must not hide metrics.
            row["job_state_status"] = "unavailable"
            row["job_state_error"] = str(exc)
        payload["containers"][name] = row
        if source_backend_health and not template_backend_source:
            template_backend_source = endpoint

    source_job_health.update(source_job_health_lane_summary(source_job_health))
    annotate_template_delivery_state(source_backend_health)
    payload["status"] = "ok" if any_ok else "unavailable"
    payload["error"] = "; ".join(errors[:3])
    payload["active_connections"] = active_connections
    payload["selected_backend"] = selected_backend
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
    recent_worker_clients: dict[str, list[dict[str, str]]] = {}
    ambiguous_worker_clients: set[str] = set()
    miners: dict[str, dict[str, Any]] = {}
    legacy_authorized_client_count = 0
    unattributed_valid_shares = 0
    unattributed_blocks = 0
    neighbors = read_neighbor_macs()
    registered_by_ip: dict[str, dict[str, Any]] = {}
    registered_by_mac: dict[str, dict[str, Any]] = {}

    for row in read_miner_registry_without_lan_hints().get("miners", []):
        if not isinstance(row, dict):
            continue
        registered = dict(row)
        ip = str(registered.get("ip") or "")
        mac = normalize_mac(registered.get("mac")) or mac_for_ip(ip, neighbors)
        if mac:
            registered["mac"] = mac
            registered["device_id"] = f"mac:{mac}"
            registered_by_mac[mac] = registered
        if is_ipv4(ip):
            registered_by_ip[ip] = registered

    def registered_for_ip(ip: str) -> tuple[dict[str, Any], str]:
        registered = registered_by_ip.get(ip, {})
        mac = normalize_mac(registered.get("mac")) or mac_for_ip(ip, neighbors)
        if mac and mac in registered_by_mac:
            registered = registered_by_mac[mac]
        return registered, mac

    bridge_alias_candidates = [
        item
        for item in miner_lan_hint_candidates({"miners": list(registered_by_ip.values())})
        if is_lan_ipv4(str(item.get("ip") or ""))
        and normalize_mac(item.get("mac"))
        and not is_docker_bridge_pool_log_client(str(item.get("ip") or ""), normalize_mac(item.get("mac")))
    ]
    bridge_alias_ip = str(bridge_alias_candidates[0].get("ip") or "") if len(bridge_alias_candidates) == 1 else ""

    def canonical_client_ip(ip: str) -> str:
        if bridge_alias_ip and is_docker_bridge_pool_log_client(ip):
            return bridge_alias_ip
        return ip

    def identity_key_for_route(ip: str, mac_hint: str = "") -> str:
        ip = canonical_client_ip(ip)
        hinted_mac = normalize_mac(mac_hint)
        if hinted_mac:
            return f"mac:{hinted_mac}"
        _registered, mac = registered_for_ip(ip)
        if mac:
            return f"mac:{mac}"
        if is_lan_ipv4(ip):
            return ""
        return f"stratum:{ip}"

    def client_identity_key(client: dict[str, str] | None) -> str:
        if not client:
            return ""
        return str(client.get("identity_key") or identity_key_for_route(str(client.get("ip") or "")))

    def client_from_identity(ip: str, port: str = "", mac_hint: str = "") -> dict[str, str] | None:
        ip = canonical_client_ip(ip)
        mac = normalize_mac(mac_hint)
        identity_key = identity_key_for_route(ip, mac_hint=mac)
        if not identity_key:
            return None
        result = {"ip": ip, "port": port, "identity_key": identity_key}
        if mac:
            result["mac"] = mac
        return result

    def note_worker_client(worker: str, ip: str, port: str = "", priority: int = 1) -> None:
        incoming = client_from_identity(ip, port)
        if not incoming:
            return
        current = worker_to_client.get(worker)
        current_priority = worker_client_priority.get(worker, -1)
        if current and client_identity_key(current) != client_identity_key(incoming):
            current_is_bridge = is_docker_bridge_pool_log_client(str(current.get("ip") or ""))
            incoming_is_bridge = is_docker_bridge_pool_log_client(ip)
            if incoming_is_bridge and not current_is_bridge and priority <= current_priority:
                return
            if current_is_bridge and not incoming_is_bridge:
                ambiguous_worker_clients.discard(worker)
                worker_to_client[worker] = incoming
                worker_client_priority[worker] = max(priority, current_priority)
                return
            if priority < current_priority:
                return
            if priority == current_priority:
                ambiguous_worker_clients.add(worker)
            else:
                ambiguous_worker_clients.discard(worker)
        if priority < current_priority:
            return
        worker_to_client[worker] = incoming
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
        return client_from_identity(str(ip), str(port or ""))

    def client_from_line(line: str) -> dict[str, str] | None:
        match = CLIENT_ADDR_RE.search(line)
        if not match:
            return None
        return client_from_addr(match.group(1), match.group(2))

    def note_job_client(job_id: str, client: dict[str, str]) -> None:
        if not job_id or not client:
            return
        job_to_client[job_id] = client
        extranonce = job_extranonce(job_id)
        if extranonce:
            # The extranonce/job suffix is a volatile pool lane identifier. A
            # fresh pool log line must override cached registry hints after a
            # miner reconnects and receives a new lane suffix.
            extranonce_to_client[extranonce] = client

    def remember_recent_worker_client(worker: str, client: dict[str, str]) -> None:
        if not worker or not client:
            return
        key = client_identity_key(client)
        recent = [
            item
            for item in recent_worker_clients.get(worker, [])
            if client_identity_key(item) != key or str(item.get("port") or "") != str(client.get("port") or "")
        ]
        recent.append(client)
        recent_worker_clients[worker] = recent[-8:]

    def recent_client_for_unknown_job(job_id: str, worker: str = "") -> dict[str, str] | None:
        extranonce = job_extranonce(job_id)
        if not worker or not extranonce or extranonce in extranonce_to_client:
            return None
        candidates = recent_worker_clients.get(worker) or []
        if not candidates:
            return None
        # Legacy pool logs sometimes omit client= on submit/share lines after
        # a miner reconnects. Use only the most recent authorize hint, and only
        # for a previously unseen volatile job suffix. Once mapped, subsequent
        # shares and blocks stay keyed by that MAC identity.
        client = candidates[-1]
        note_job_client(job_id, client)
        return client

    def note_legacy_extranonce_client(client: dict[str, str]) -> None:
        nonlocal legacy_authorized_client_count
        if not client:
            return
        legacy_authorized_client_count += 1
        # Older pool images omit subscribe/job-notify client details but encode
        # each connection's little-endian extranonce suffix in the job id.
        # The suffix is reused after reconnects, so fresh authorize order must
        # replace cached registry hints instead of preserving stale ownership.
        extranonce = f"{legacy_authorized_client_count:02x}000000"
        extranonce_to_client[extranonce] = client

    def note_item_extranonce(item: dict[str, Any], job_id: str) -> None:
        extranonce = job_extranonce(job_id)
        if not extranonce:
            return
        values = item.setdefault("job_extranonces", [])
        if extranonce not in values:
            values.append(extranonce)

    def client_for_job_or_worker(job_id: str, worker: str = "") -> dict[str, str] | None:
        return (
            job_to_client.get(job_id)
            or extranonce_to_client.get(job_extranonce(job_id))
            or recent_client_for_unknown_job(job_id, worker)
            or (client_for_worker(worker) if worker else None)
        )

    registry_extranonce_clients: dict[str, dict[str, str]] = {}
    registry_extranonce_ambiguous: set[str] = set()

    for registered in registered_by_ip.values():
        ip = str(registered.get("ip") or "")
        if not is_ipv4(ip):
            continue
        priority = 2 if normalize_mac(registered.get("mac")) or registered.get("display_name") else 1
        for worker in merge_unique_strings(registered.get("last_workers"), registered.get("expected_worker_user")):
            note_worker_client(worker, ip, priority=priority)
        client = client_from_identity(ip)
        if not client:
            continue
        for extranonce in merge_unique_strings(registered.get("last_pool_job_extranonces")):
            if re.fullmatch(r"[0-9a-fA-F]{8}", str(extranonce or "")):
                suffix = str(extranonce).lower()
                if suffix in registry_extranonce_ambiguous:
                    continue
                existing_client = registry_extranonce_clients.get(suffix)
                if existing_client and client_identity_key(existing_client) != client_identity_key(client):
                    registry_extranonce_ambiguous.add(suffix)
                    registry_extranonce_clients.pop(suffix, None)
                else:
                    registry_extranonce_clients[suffix] = client

    for suffix, client in registry_extranonce_clients.items():
        extranonce_to_client.setdefault(suffix, client)

    def miner_for_ip(ip: str, mac_hint: str = "") -> dict[str, Any] | None:
        ip = canonical_client_ip(ip)
        registered, mac = registered_for_ip(ip)
        hinted_mac = normalize_mac(mac_hint)
        if hinted_mac:
            mac = hinted_mac
            registered = registered_by_mac.get(mac, registered)
        if mac:
            identity_key = f"mac:{mac}"
        elif is_lan_ipv4(ip):
            return None
        else:
            identity_key = f"stratum:{ip}"
        storage_key = identity_key or f"stratum:{ip}"
        item = miners.setdefault(
            storage_key,
            {
                "ip": ip,
                "mac": mac,
                "device_id": f"mac:{mac}" if mac else "",
                "identity_key": identity_key,
                "identity_unresolved": bool(is_lan_ipv4(ip) and not mac),
                "identity_issue": "asic_mac_unresolved" if is_lan_ipv4(ip) and not mac else "",
                "display_name": registered.get("display_name") or "",
                "display_label": miner_display_label({**registered, "mac": mac}) if mac or registered else "",
                "ip_history": merge_unique_strings(registered.get("ip_history"), ip),
                "device_type": "asic" if mac and is_lan_ipv4(ip) else "stratum",
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
                "job_extranonces": [],
            },
        )
        item["ip"] = ip
        item["ip_history"] = merge_unique_strings(item.get("ip_history"), registered.get("ip_history"), ip)
        if mac:
            item["mac"] = mac
            item["device_id"] = f"mac:{mac}"
            item["identity_key"] = identity_key
        if registered.get("display_name") and not item.get("display_name"):
            item["display_name"] = registered.get("display_name")
        if mac or registered:
            item["display_label"] = miner_display_label({**registered, **item, "mac": mac or item.get("mac")})
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
        stratum_accept = STRATUM_ACCEPT_RE.search(line)
        if stratum_accept:
            ip, port, lane_mac, field_mac = stratum_accept.groups()
            mac = normalize_mac(lane_mac or field_mac)
            if not mac:
                continue
            client = client_from_identity(ip, port, mac_hint=mac)
            if not client:
                continue
            item = miner_for_ip(ip, mac_hint=mac)
            if item is None:
                continue
            note_port(item, port)
            note_seen(item, line)
            continue

        pushdif = PUSHDIF_RE.search(line)
        if pushdif:
            ip, port, difficulty = pushdif.groups()
            item = miner_for_ip(ip)
            if item is None:
                continue
            note_port(item, port)
            item["last_difficulty"] = difficulty
            note_seen(item, line, "last_job_at")
            continue

        auth = AUTH_ACCEPT_RE.search(line)
        if auth:
            ip, port, worker = auth.groups()
            note_worker_client(worker, ip, port=port, priority=1)
            legacy_client = client_from_identity(ip, port)
            if not legacy_client:
                continue
            if is_docker_bridge_pool_log_client(ip):
                legacy_client = client_for_worker(worker) or legacy_client
            remember_recent_worker_client(worker, legacy_client)
            note_legacy_extranonce_client(legacy_client)
            item = miner_for_ip(ip)
            if item is None:
                continue
            note_port(item, port)
            note_worker(item, worker)
            note_seen(item, line)
            continue

        subscribe = SUBSCRIBE_ACCEPT_RE.search(line)
        if subscribe:
            ip, port, extranonce = subscribe.groups()
            client = client_from_identity(ip, port)
            if not client:
                continue
            extranonce_to_client[extranonce.lower()] = client
            item = miner_for_ip(ip)
            if item is None:
                continue
            note_port(item, port)
            note_seen(item, line)
            continue

        notify = JOB_NOTIFY_DETAIL_RE.search(line)
        if notify:
            ip, port, job_id = notify.groups()
            client = client_from_identity(ip, port)
            if not client:
                continue
            note_job_client(job_id, client)
            item = miner_for_ip(ip)
            if item is None:
                continue
            note_port(item, port)
            item["jobs"] += 1
            note_seen(item, line, "last_job_at")
            continue

        legacy_notify = JOB_NOTIFY_RE.search(line)
        if legacy_notify:
            ip, job_id = legacy_notify.groups()
            client = client_from_identity(ip, "")
            if not client:
                continue
            note_job_client(job_id, client)
            item = miner_for_ip(ip)
            if item is None:
                continue
            item["jobs"] += 1
            note_seen(item, line, "last_job_at")
            continue

        recovery_resend = RECOVERY_JOB_TO_CLIENT_RE.search(line)
        if recovery_resend:
            ip, port, job_id = recovery_resend.groups()
            client = client_from_identity(ip, port)
            if not client:
                continue
            note_job_client(job_id, client)
            item = miner_for_ip(ip)
            if item is None:
                continue
            note_port(item, port)
            note_item_extranonce(item, job_id)
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
            if item is None:
                continue
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
                unattributed_valid_shares += valid_share_line_weight(line)
                continue
            note_job_client(job_id, client)
            item = miner_for_ip(client["ip"])
            if item is None:
                unattributed_valid_shares += valid_share_line_weight(line)
                continue
            note_port(item, client.get("port"))
            note_item_extranonce(item, job_id)
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
                unattributed_blocks += 1
                continue
            item = miner_for_ip(client["ip"])
            if item is None:
                unattributed_blocks += 1
                continue
            note_port(item, client.get("port"))
            note_item_extranonce(item, job_id)
            item["blocks_found"] += 1
            note_seen(item, line, "last_block_at")

    for item in miners.values():
        observed_submits = int(item.get("shares", 0) or 0) + int(item.get("blocks_found", 0) or 0)
        if int(item.get("submits", 0) or 0) < observed_submits:
            item["submits"] = observed_submits

    return {
        "generated_at": now_iso(),
        "miners": sorted(
            miners.values(),
            key=lambda item: (
                0 if normalize_mac(item.get("mac")) else 1,
                normalize_mac(item.get("mac")) or str(item.get("identity_key") or ""),
                int(ipaddress.ip_address(item["ip"])),
            ),
        ),
        "unattributed_valid_shares": unattributed_valid_shares,
        "unattributed_blocks": unattributed_blocks,
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
    activity = parse_pool_activity(log)
    if (
        lines < POOL_ACTIVITY_BOOTSTRAP_LOG_LINES
        and (safe_int(activity.get("unattributed_valid_shares"), 0) > 0 or safe_int(activity.get("unattributed_blocks"), 0) > 0)
    ):
        activity = parse_pool_activity(docker_logs_many(POOL_CONTAINERS, lines=POOL_ACTIVITY_BOOTSTRAP_LOG_LINES))
        activity["bootstrap_log_lines"] = POOL_ACTIVITY_BOOTSTRAP_LOG_LINES
    return activity


def upsert_pool_activity_miners(activity: dict[str, Any]) -> dict[str, Any]:
    """Persist miners seen passively in the stratum pool logs."""
    registry = read_miner_registry_without_lan_hints()
    registry_had_bridge_pseudo_miners = any(is_docker_bridge_pseudo_miner(item) for item in registry.get("miners", []))
    existing_miners = [dict(item) for item in registry.get("miners", []) if not is_docker_bridge_pseudo_miner(item)]
    existing = {str(item.get("ip")): dict(item) for item in existing_miners if item.get("ip")}
    existing_by_mac = {
        normalize_mac(item.get("mac")): dict(item)
        for item in existing_miners
        if normalize_mac(item.get("mac"))
    }
    neighbors = read_neighbor_macs()
    defaults = default_miner_pool_settings()
    changed = False
    bridge_alias_candidates = [
        item
        for item in miner_lan_hint_candidates(registry)
        if is_lan_ipv4(str(item.get("ip") or "")) and normalize_mac(item.get("mac"))
    ]
    bridge_alias_ip = str(bridge_alias_candidates[0].get("ip") or "") if len(bridge_alias_candidates) == 1 else ""

    for miner in activity.get("miners", []):
        ip = str(miner.get("ip", ""))
        if bridge_alias_ip and is_docker_bridge_pool_log_client(ip):
            ip = bridge_alias_ip
        if not is_ipv4(ip):
            continue

        mac = miner_mac_from_payload(miner, ip, neighbors)
        discovered = None
        if not mac and is_lan_ipv4(ip):
            discovered = discover_miner(ip, timeout=MINER_SCAN_TIMEOUT)
            if discovered:
                mac = miner_mac_from_payload(discovered, ip, neighbors)
        if is_docker_bridge_pool_log_client(ip, mac):
            continue
        item = existing_by_mac.get(mac) if mac else None
        item = dict(item or existing.get(ip, {"ip": ip}))
        previous_ip = str(item.get("ip") or "")
        workers = merge_unique_strings(item.get("last_workers"), miner.get("workers"))
        ports = merge_unique_strings(
            item.get("last_ports"),
            miner.get("ports"),
            limit=MINER_REGISTRY_MAX_PORTS,
        )
        incoming_job_extranonces = merge_unique_strings(
            miner.get("job_extranonces"),
            limit=MINER_REGISTRY_MAX_JOB_EXTRANONCES,
        )
        job_extranonces = (
            incoming_job_extranonces
            if incoming_job_extranonces
            else merge_unique_strings(
                item.get("last_pool_job_extranonces"),
                limit=MINER_REGISTRY_MAX_JOB_EXTRANONCES,
            )
        )
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
        if discovered:
            item["device_type"] = "asic"
            if not item.get("discovered_by") or item.get("discovered_by") == "pool-log":
                item["discovered_by"] = "asic-api"
            for key in ("name", "model", "hardware", "firmware", "mcbversion"):
                if discovered.get(key):
                    item[key] = discovered[key]
            if discovered.get("pool_count") is not None:
                item["last_pool_count"] = discovered["pool_count"]
            item["last_seen_at"] = now_iso()
            item["last_seen_epoch"] = now_epoch

        item.update(
            {
                "ip": ip,
                "mac": mac or item.get("mac", ""),
                "device_id": f"mac:{mac}" if mac else "",
                "identity_key": f"mac:{mac}" if mac else "",
                "identity_unresolved": bool(is_lan_ipv4(ip) and not mac),
                "identity_issue": "asic_mac_unresolved" if is_lan_ipv4(ip) and not mac else "",
                "ip_history": merge_unique_strings(item.get("ip_history"), previous_ip, ip),
                "sources": merge_unique_strings(item.get("sources"), "pool-log", "asic-api" if discovered else None),
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
                "last_pool_job_extranonces": job_extranonces,
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

        if mac and previous_ip and previous_ip != ip:
            existing.pop(previous_ip, None)
        existing[ip] = item
        if mac:
            existing_by_mac[mac] = item
        changed = True

    return save_miner_registry(list(existing.values())) if changed or registry_had_bridge_pseudo_miners else registry


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


def collect_miner_health_from_registry(reason: str = "pool_container_not_running") -> dict[str, Any]:
    """Return bounded MAC-identity miner visibility without reading pool logs.

    The status sampler uses this when the pool is intentionally stopped, for
    example during catch-up pause. Miner demand remains visible via managed MAC
    rows, but old pool logs and live ASIC HTTP probes stay off the fast safety
    path.
    """
    registry = read_miner_registry_without_lan_hints()
    defaults = default_miner_pool_settings()
    health: list[dict[str, Any]] = []
    for registered in registry.get("miners", []):
        if not isinstance(registered, dict):
            continue
        ip = str(registered.get("ip") or "")
        if not is_ipv4(ip) or is_docker_bridge_ipv4(ip):
            continue
        mac = normalize_mac(registered.get("mac"))
        device_type = str(registered.get("device_type") or ("asic" if mac else "stratum"))
        managed = bool(registered.get("managed"))
        configured = bool(registered.get("configured") or registered.get("last_configured_ok") or managed)
        if not (managed or configured):
            continue
        last_known_jobs = int(registered.get("last_jobs_window", 0) or 0)
        last_known_submits = int(registered.get("last_submits_window", 0) or 0)
        last_known_shares = int(registered.get("last_shares_window", 0) or 0)
        last_known_share_work = int(registered.get("last_share_work_window", 0) or 0)
        last_known_blocks = int(registered.get("last_blocks_window", 0) or 0)
        health.append(
            {
                "ip": ip,
                "mac": mac,
                "device_id": f"mac:{mac}" if mac else "",
                "identity_key": f"mac:{mac}" if mac else "",
                "identity_unresolved": bool(is_lan_ipv4(ip) and not mac and device_type == "asic"),
                "identity_issue": "asic_mac_unresolved" if is_lan_ipv4(ip) and not mac and device_type == "asic" else "",
                "display_name": registered.get("display_name") or "",
                "display_label": miner_display_label({**registered, "mac": mac}),
                "managed": managed,
                "device_type": device_type,
                "discovered_by": registered.get("discovered_by") or "registry",
                "auto_discovered": bool(registered.get("auto_discovered")),
                "status": "paused" if managed or configured else "inactive",
                "configured": configured,
                "connected": False,
                "pool_active": False,
                "work_pool_active": False,
                "api_error": "",
                "debug_error": "",
                "issue": reason,
                "model": registered.get("model", ""),
                "hardware": registered.get("hardware", ""),
                "firmware": registered.get("firmware", ""),
                "debug": {"available": False},
                "expected_pool_url": registered.get("expected_pool_url") or defaults["pool_url"],
                "expected_worker_user": registered.get("expected_worker_user") or defaults["worker_user"],
                "workers": merge_unique_strings(registered.get("last_workers")),
                "ports": merge_unique_strings(registered.get("last_ports"), limit=MINER_REGISTRY_MAX_PORTS),
                "jobs": 0,
                "submits": 0,
                "shares": 0,
                "share_work": 0,
                "work_percent": "0.00",
                "relevant_for_work_share": bool(managed or configured),
                "low_difficulty_flood": False,
                "share_difficulty": 0,
                "blocks_found": 0,
                "last_known_jobs": last_known_jobs,
                "last_known_submits": last_known_submits,
                "last_known_shares": last_known_shares,
                "last_known_share_work": last_known_share_work,
                "last_known_blocks_found": last_known_blocks,
                "last_known_share_difficulty": registered.get("last_share_difficulty_window", 0),
                "last_difficulty": registered.get("last_difficulty"),
                "last_job_at": registered.get("last_job_at"),
                "last_submit_at": registered.get("last_submit_at"),
                "last_submit_epoch": registered.get("last_submit_epoch"),
                "last_submit_age_seconds": None,
                "last_share_at": registered.get("last_share_at"),
                "last_share_epoch": registered.get("last_share_epoch"),
                "last_share_age_seconds": None,
                "last_block_at": registered.get("last_block_at"),
                "last_pool_seen_at": registered.get("last_pool_seen_at"),
                "last_pool_seen_epoch": registered.get("last_pool_seen_epoch"),
                "last_pool_seen_age_seconds": None,
                "expected_work_lane": bool(managed or configured),
                "expected_work_percent": "0.00",
                "work_ratio_to_expected": None,
                "lane_status": "paused" if managed or configured else "not-tracked",
            }
        )
    health.sort(
        key=lambda item: (
            0 if normalize_mac(item.get("mac")) else 1,
            normalize_mac(item.get("mac")) or str(item.get("identity_key") or ""),
            int(ipaddress.ip_address(item["ip"])),
        )
    )
    counts = miner_health_count_summary(health)
    hidden_inactive = sum(
        1
        for registered in registry.get("miners", [])
        if isinstance(registered, dict)
        and is_ipv4(str(registered.get("ip") or ""))
        and not is_docker_bridge_ipv4(str(registered.get("ip") or ""))
        and not (
            bool(registered.get("managed"))
            or bool(registered.get("configured") or registered.get("last_configured_ok") or registered.get("managed"))
        )
    )
    return {
        "generated_at": now_iso(),
        "registry_updated_at": registry.get("updated_at"),
        "status_source": "registry_only",
        "source_reason": reason,
        **counts,
        "expected_lane_count": sum(1 for item in health if item.get("expected_work_lane")),
        "expected_lane_percent": "0.00",
        "imbalanced_lane_count": 0,
        "hidden_inactive_count": hidden_inactive,
        "hidden_inactive": hidden_inactive,
        "work_total": 0,
        "total_work_includes_all_rows": False,
        "failures": [],
        "warnings": [],
        "miners": health,
    }


def pool_worker_user_matches(actual: Any, expected: Any) -> bool:
    return str(actual or "").lower() == str(expected or "").lower()


def collect_miner_health(source_job_health: Mapping[str, Any] | None = None) -> dict[str, Any]:
    defaults = default_miner_pool_settings()
    activity = collect_pool_activity(lines=POOL_ACTIVITY_LOG_LINES)
    registry = upsert_pool_activity_miners(activity)
    miners = registry.get("miners", [])
    activity_by_ip = {item["ip"]: item for item in activity["miners"]}
    activity_by_identity = {
        str(item.get("identity_key") or ""): item
        for item in activity["miners"]
        if item.get("identity_key")
    }
    health: list[dict[str, Any]] = []
    failures: list[str] = []
    warnings: list[str] = []
    asic_identity = pool_asic_mac_override_diagnostics(registry)
    unresolved_asic_identity = asic_identity.get("unresolved") or []
    if unresolved_asic_identity:
        sample = ", ".join(
            str(item.get("ip") or "")
            for item in unresolved_asic_identity[:4]
            if isinstance(item, dict) and item.get("ip")
        )
        suffix = f"; +{len(unresolved_asic_identity) - 4} more" if len(unresolved_asic_identity) > 4 else ""
        warnings.append(
            "ASIC MAC identity unresolved for observed LAN miner route(s): "
            f"{sample}{suffix}; IP addresses remain observations only and are not used as ASIC lanes"
        )
    now_epoch = seconds_since_epoch()
    pool_lane_summary = source_job_health_lane_summary(source_job_health)
    pool_job_state_available = bool(pool_lane_summary.get("job_state_available"))
    active_lane_ids = set(pool_lane_summary.get("active_lane_ids") or [])
    authorized_lane_ids = set(pool_lane_summary.get("authorized_lane_ids") or [])
    ready_lane_ids = set(pool_lane_summary.get("ready_lane_ids") or [])
    clients_by_lane = pool_lane_summary.get("clients_by_lane")
    if not isinstance(clients_by_lane, dict):
        clients_by_lane = {}

    for registered in miners:
        ip = str(registered.get("ip", ""))
        if not is_ipv4(ip):
            continue
        if is_docker_bridge_ipv4(ip):
            continue
        expected_url = registered.get("expected_pool_url") or defaults["pool_url"]
        expected_user = registered.get("expected_worker_user") or defaults["worker_user"]
        registered_identity = miner_identity_key(registered)
        activity_item = activity_by_identity.get(registered_identity, {}) if registered_identity else {}
        if not activity_item:
            activity_item = activity_by_ip.get(ip, {})
        device_type = str(registered.get("device_type") or ("asic" if registered.get("model") else "stratum"))
        discovered_by = str(registered.get("discovered_by") or "")
        pool_log_lan_candidate = bool(activity_item or registered.get("auto_discovered") or discovered_by == "pool-log")
        api_expected = is_lan_ipv4(ip) and (device_type == "asic" or pool_log_lan_candidate)
        api_error = ""
        debug_error = ""
        discovered = None
        cgminer_devs: dict[str, Any] = {}
        configured = False
        pool_active = False
        last_pool_seen_epoch = int(registered.get("last_pool_seen_epoch", 0) or 0)
        last_submit_epoch = int(registered.get("last_submit_epoch", 0) or 0)
        last_share_epoch = int(registered.get("last_share_epoch", 0) or 0)
        workers = merge_unique_strings(activity_item.get("workers"), registered.get("last_workers"))
        ports = merge_unique_strings(
            activity_item.get("ports"),
            registered.get("last_ports"),
            limit=MINER_REGISTRY_MAX_PORTS,
        )
        activity_current_fresh = miner_activity_is_fresh(
            activity_item,
            now_epoch,
            ("last_seen_epoch", "last_pool_seen_epoch"),
            ("last_seen_at", "last_job_at", "last_submit_at", "last_share_at", "last_block_at"),
        )
        activity_submit_fresh = miner_activity_is_fresh(
            activity_item,
            now_epoch,
            ("last_submit_epoch",),
            ("last_submit_at", "last_share_at", "last_block_at"),
        )
        activity_share_fresh = miner_activity_is_fresh(
            activity_item,
            now_epoch,
            ("last_share_epoch",),
            ("last_share_at", "last_block_at"),
        )
        pool_window_fresh = bool(
            not activity_current_fresh
            and last_pool_seen_epoch
            and now_epoch - last_pool_seen_epoch <= POOL_CONNECTED_STALE_SECONDS
        )
        submit_window_fresh = bool(
            not activity_submit_fresh
            and last_submit_epoch
            and now_epoch - last_submit_epoch <= POOL_CONNECTED_STALE_SECONDS
        )
        share_window_fresh = bool(
            not activity_share_fresh
            and last_share_epoch
            and now_epoch - last_share_epoch <= POOL_CONNECTED_STALE_SECONDS
        )
        connected = activity_current_fresh or pool_window_fresh
        managed = bool(registered.get("managed"))
        configured_record = bool(registered.get("configured") or registered.get("managed") or registered.get("last_configured_ok"))
        current_submits = int(activity_item.get("submits", 0) or 0) if activity_submit_fresh else 0
        current_shares = int(activity_item.get("shares", 0) or 0) if activity_share_fresh else 0
        current_blocks_found = int(activity_item.get("blocks_found", 0) or 0) if activity_share_fresh else 0
        current_share_work = int(activity_item.get("share_work", 0) or 0) if activity_share_fresh else 0
        current_share_difficulty = activity_item.get("share_difficulty", 0) if activity_share_fresh else 0
        current_jobs = activity_item.get("jobs", 0) if activity_current_fresh else 0
        if pool_window_fresh:
            current_jobs = max(int(current_jobs or 0), int(registered.get("last_jobs_window", 0) or 0))
        if submit_window_fresh:
            current_submits = max(current_submits, int(registered.get("last_submits_window", 0) or 0))
        if share_window_fresh:
            current_shares = max(current_shares, int(registered.get("last_shares_window", 0) or 0))
            current_blocks_found = max(current_blocks_found, int(registered.get("last_blocks_window", 0) or 0))
            current_share_work = max(current_share_work, int(registered.get("last_share_work_window", 0) or 0))
            if not current_share_difficulty:
                current_share_difficulty = registered.get("last_share_difficulty_window", 0)
        has_recent_shares = current_shares > 0
        has_recent_blocks = current_blocks_found > 0
        expected_worker_seen = str(expected_user).lower() in {str(worker).lower() for worker in workers}
        current_pool_activity = (
            activity_current_fresh or submit_window_fresh or share_window_fresh
        ) and expected_url == defaults["pool_url"] and (
            expected_worker_seen or current_submits > 0 or has_recent_shares or has_recent_blocks
        )
        primary_pool_log = configured_record and is_known_primary_pool_log_miner({**registered, "last_workers": workers})
        pre_api_relevant = (
            managed
            or configured_record
            or current_pool_activity
            or has_recent_shares
            or has_recent_blocks
            or primary_pool_log
        )
        if api_expected and pre_api_relevant:
            try:
                discovered = discover_miner(ip, timeout=MINER_HTTP_TIMEOUT)
                if discovered and discovered.get("model"):
                    device_type = "asic"
                    discovered_by = discovered_by if discovered_by and discovered_by != "pool-log" else "asic-api"
                pools = discovered.get("pools", []) if discovered else []
                configured = any(
                    str(pool.get("url", "")) == expected_url and pool_worker_user_matches(pool.get("user", ""), expected_user)
                    for pool in pools
                )
                pool_active = any(
                    str(pool.get("url", "")) == expected_url
                    and pool_worker_user_matches(pool.get("user", ""), expected_user)
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
        lane_id = miner_identity_key({**registered, "mac": mac}) or (f"mac:{mac}" if mac else "")
        pool_lane_expected = bool(pool_job_state_available and device_type == "asic" and mac and (managed or configured_record))
        source_lane_clients = clients_by_lane.get(lane_id, []) if lane_id else []
        pool_lane_seen = bool(pool_lane_expected and lane_id in active_lane_ids)
        pool_lane_authorized = bool(pool_lane_expected and lane_id in authorized_lane_ids)
        pool_lane_ready = bool(pool_lane_expected and lane_id in ready_lane_ids)
        if pool_lane_expected:
            connected = pool_lane_seen
            if not pool_lane_authorized:
                pool_active = False
                work_pool_active = False
        pool_log_recent = bool(activity_current_fresh or pool_window_fresh)
        retirement_decision = retired_miner_identity_decision({**registered, **activity_item}, ip, mac)
        if retirement_decision.get("conflict") and pool_log_recent:
            label = miner_display_label({**registered, "mac": mac})
            retired_name = retirement_decision.get("retired_name") or "retired miner"
            warnings.append(
                f"{label} mac={mac or 'unknown-mac'} observed_ip={ip} is active in pool logs but shares "
                f"an observed retired-miner IP for {retired_name}; "
                "keeping it active because only MAC address can retire an ASIC"
            )
        if is_pool_log_only_miner(registered) or device_type == "stratum" or discovered_by == "pool-log":
            expected_worker_seen = str(expected_user).lower() in {str(worker).lower() for worker in workers}
            if connected and expected_url == defaults["pool_url"] and expected_worker_seen:
                configured = bool(configured or configured_record)
                pool_active = True
        pool_seen_age = now_epoch - last_pool_seen_epoch if last_pool_seen_epoch else None
        submit_age = now_epoch - last_submit_epoch if last_submit_epoch else None
        share_age = now_epoch - last_share_epoch if last_share_epoch else None
        expected_worker_seen = str(expected_user).lower() in {str(worker).lower() for worker in workers}
        current_pool_activity = (
            activity_current_fresh or submit_window_fresh or share_window_fresh
        ) and expected_url == defaults["pool_url"] and (
            expected_worker_seen or current_submits > 0 or has_recent_shares or has_recent_blocks
        )
        work_pool_active = bool(
            (managed or configured_record or current_pool_activity)
            and (current_pool_activity or pool_active or has_recent_shares or has_recent_blocks)
        )
        if pool_lane_expected and not pool_lane_authorized:
            pool_active = False
            work_pool_active = False
        primary_pool_log = configured_record and is_known_primary_pool_log_miner({**registered, "last_workers": workers})
        relevant = managed or configured_record or work_pool_active or has_recent_shares or has_recent_blocks or primary_pool_log
        if not relevant:
            continue
        shares = current_shares
        share_work_int = int(current_share_work or 0)
        share_difficulty = current_share_difficulty
        blocks_found = current_blocks_found
        last_share_at = activity_item.get("last_share_at") or registered.get("last_share_at")
        last_job_at = activity_item.get("last_job_at") or registered.get("last_job_at")
        last_submit_at = activity_item.get("last_submit_at") or registered.get("last_submit_at")
        issue = api_error or debug_error
        last_difficulty = activity_item.get("last_difficulty") or registered.get("last_difficulty")
        last_difficulty_value = safe_decimal(last_difficulty)
        submits = current_submits
        low_difficulty_flood = bool(
            last_difficulty_value is not None
            and last_difficulty_value > 0
            and last_difficulty_value < MINER_LOW_DIFF_THRESHOLD
            and current_submits >= MINER_LOW_DIFF_MIN_SUBMITS
        )

        status = "inactive"
        if managed:
            if pool_lane_expected and not pool_lane_authorized:
                status = "down" if (api_error or debug_error) else "degraded"
                label = miner_display_label({**registered, "mac": mac})
                message = (
                    f"{label} mac={mac or 'unknown-mac'} observed_ip={ip} is configured/managed "
                    "but is absent from the pool's current authorized Stratum MAC lanes"
                    + (f": {api_error or debug_error}" if (api_error or debug_error) else "")
                )
                if api_error or debug_error:
                    failures.append(message)
                else:
                    warnings.append(message)
                issue = issue or message
            elif (api_error or debug_error) and not has_recent_shares and not has_recent_blocks and not connected:
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
                "device_id": f"mac:{mac}" if mac else "",
                "identity_key": miner_identity_key({**registered, "mac": mac}),
                "identity_unresolved": bool(is_lan_ipv4(ip) and not mac and device_type == "asic"),
                "identity_issue": "asic_mac_unresolved" if is_lan_ipv4(ip) and not mac and device_type == "asic" else "",
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
                "pool_lane_expected": pool_lane_expected,
                "pool_lane_seen": pool_lane_seen,
                "pool_lane_authorized": pool_lane_authorized,
                "pool_lane_ready": pool_lane_ready,
                "pool_lane_id": lane_id,
                "pool_lane_clients": source_lane_clients,
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
                "jobs": current_jobs,
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
    total_work_includes_all_rows = False
    if not total_work:
        total_work = sum(item["share_work"] for item in health if item.get("share_work", 0) > 0)
        total_work_includes_all_rows = True
    expected_lane_rows = [
        item
        for item in health
        if item.get("relevant_for_work_share")
        and (item.get("configured") or item.get("managed"))
        and miner_identity_key(item)
    ]
    expected_lane_ids = {miner_identity_key(item) for item in expected_lane_rows}
    expected_lane_count = len(expected_lane_rows)
    expected_lane_percent = Decimal("100") / Decimal(expected_lane_count) if expected_lane_count > 0 else Decimal("0")
    imbalanced_lanes: list[str] = []
    for item in health:
        share_work_int = int(item.get("share_work", 0) or 0)
        include_in_work_percent = bool(item.get("relevant_for_work_share") or total_work_includes_all_rows)
        if total_work > 0 and share_work_int > 0 and include_in_work_percent:
            item["work_percent"] = percent_to_str((Decimal(share_work_int) / Decimal(total_work)) * Decimal("100"))
        lane_id = miner_identity_key(item)
        expected_lane = bool(lane_id and lane_id in expected_lane_ids)
        item["expected_work_lane"] = expected_lane
        item["expected_work_percent"] = percent_to_str(expected_lane_percent) if expected_lane_count > 0 and expected_lane else "0.00"
        item["work_ratio_to_expected"] = None
        item["lane_status"] = "not-tracked"
        if expected_lane_count > 0 and expected_lane:
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
        "asic_identity": asic_identity,
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


def max_catchup_lag_blocks(
    sync_progress: Mapping[str, Any],
    node_details: Mapping[str, Any],
    selected_source_health: Mapping[str, Any] | None = None,
) -> int:
    values: list[int] = []
    for key in ("remaining_blocks", "peer_ahead_blocks"):
        value = safe_int(sync_progress.get(key), -1)
        if value >= 0:
            values.append(value)
    progress_nodes = sync_progress.get("nodes") if isinstance(sync_progress.get("nodes"), dict) else {}
    for info in progress_nodes.values():
        if not isinstance(info, dict):
            continue
        for key in ("remaining_blocks", "peer_ahead_blocks"):
            value = safe_int(info.get(key), -1)
            if value >= 0:
                values.append(value)
    for info in node_details.values():
        if not isinstance(info, dict):
            continue
        for key in ("remaining_blocks", "peer_ahead_blocks"):
            value = safe_int(info.get(key), -1)
            if value >= 0:
                values.append(value)
    if isinstance(selected_source_health, Mapping):
        value = safe_int(selected_source_health.get("node_p2p_best_peer_lead_blocks"), -1)
        if value >= 0:
            values.append(value)
    return max(values) if values else 0


def catchup_io_pressure_reasons(host_pressure: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(host_pressure, Mapping):
        return []
    reasons: list[str] = []
    iowait = safe_float(host_pressure.get("iowait_percent"))
    io_some = safe_float(host_pressure.get("io_some_avg10"))
    io_full = safe_float(host_pressure.get("io_full_avg10"))
    if bool(host_pressure.get("iowait_warning_active")):
        reasons.append("sustained_iowait_warning")
    if iowait is not None and iowait >= CATCHUP_IOWAIT_WARN_PERCENT:
        reasons.append(f"iowait_percent={iowait:.2f}>={CATCHUP_IOWAIT_WARN_PERCENT:.2f}")
    if io_some is not None and io_some >= CATCHUP_IO_SOME_AVG10_WARN:
        reasons.append(f"io_some_avg10={io_some:.2f}>={CATCHUP_IO_SOME_AVG10_WARN:.2f}")
    if io_full is not None and io_full >= CATCHUP_IO_FULL_AVG10_WARN:
        reasons.append(f"io_full_avg10={io_full:.2f}>={CATCHUP_IO_FULL_AVG10_WARN:.2f}")
    return reasons


def build_catchup_policy(
    sync_progress: Mapping[str, Any],
    node_details: Mapping[str, Any],
    containers: Mapping[str, Any],
    selected_source_health: Mapping[str, Any] | None = None,
    host_pressure: Mapping[str, Any] | None = None,
    mining_ready: bool | None = None,
    pool_has_recent_paid_work: bool = False,
) -> dict[str, Any]:
    lag = max_catchup_lag_blocks(sync_progress, node_details, selected_source_health)
    status = str(sync_progress.get("status") or "").lower()
    peer_catchup = lag > 0
    io_pressure_reasons = catchup_io_pressure_reasons(host_pressure)
    backend_unready_reasons = selected_backend_unready_reasons(selected_source_health or {})
    mining_ready_for_policy = bool(mining_ready) if mining_ready is not None else not bool(
        backend_unready_reasons
    )
    node_sync_busy = any(
        bool(info.get("node_busy_syncing"))
        or bool(info.get("importing"))
        or (
            info.get("last_import_age_seconds") is not None
            and safe_int(info.get("last_import_age_seconds"), NODE_IMPORT_STALE_SECONDS + 1) <= NODE_IMPORT_STALE_SECONDS
        )
        for info in node_details.values()
        if isinstance(info, Mapping)
    )
    backend_unready_under_pressure = bool(
        io_pressure_reasons
        and backend_unready_reasons
        and not mining_ready_for_policy
        and not pool_has_recent_paid_work
    )
    io_pressure_lag_active = bool(peer_catchup and lag >= CATCHUP_IO_PRESSURE_MIN_LAG_BLOCKS)
    io_pressure_active = bool(
        CATCHUP_IO_PRESSURE_PAUSE_ENABLED
        and io_pressure_reasons
        and not mining_ready_for_policy
        and io_pressure_lag_active
    )
    lag_threshold_active = bool(lag > CATCHUP_PAUSE_THRESHOLD_BLOCKS and (status != "synced" or not mining_ready_for_policy))
    backend_sync_active = bool(
        status == "syncing"
        and node_sync_busy
        and not mining_ready_for_policy
        and not pool_has_recent_paid_work
    )
    pause_candidate_active = bool(io_pressure_active or lag_threshold_active or backend_sync_active)
    recent_paid_work_suppressed = bool(pool_has_recent_paid_work and pause_candidate_active)
    active = bool(CATCHUP_PAUSE_ENABLED and pause_candidate_active and not recent_paid_work_suppressed)
    pool_running = bool((containers.get(POOL_CONTAINER) or {}).get("running")) if isinstance(containers, Mapping) else False
    trigger = (
        "io_pressure"
        if io_pressure_active
        else ("lag_threshold" if lag_threshold_active else ("backend_syncing" if backend_sync_active else ""))
    ) if active else ""
    summary = (
        (
            f"catch-up pause active: chain node is I/O-bound while {lag} blocks behind peers; "
            "mining work is intentionally paused"
            if lag > 0
            else "catch-up pause active: backend is not ready while the host is I/O-bound; mining work is intentionally paused"
        )
        if trigger == "io_pressure"
        else (
            f"catch-up pause active: chain node is {lag} blocks behind peers "
            f"(threshold {CATCHUP_PAUSE_THRESHOLD_BLOCKS}); mining work is intentionally paused"
        )
        if trigger == "lag_threshold"
        else (
            "catch-up pause active: chain node is importing or busy syncing while mining templates are not ready; "
            "mining work is intentionally paused"
        )
        if active
        else ""
    )
    user_message = (
        "The pool is deliberately prioritizing chain catch-up because the node is I/O-bound while behind peers. "
        "Mining/template work is paused so disk, CPU, and network capacity go to block import instead of stale jobs. "
        "Leave miners configured for this pool; they are not the problem while this state is active."
        if trigger == "io_pressure" and lag > 0
        else (
            "The pool is deliberately pausing mining/template work because the backend is not ready while the host "
            "is I/O-bound. This prevents miners from hammering stale or invalid work. Leave miners configured for "
            "this pool; they are not the problem while this state is active."
            if trigger == "io_pressure"
            else (
                "The pool is deliberately prioritizing chain catch-up. Mining/template work is paused so the "
                "node can spend disk, CPU, and network capacity importing blocks instead of handing stale jobs "
                "to miners. Leave miners configured for this pool; they are not the problem while this state is active."
                if active
                else ""
            )
        )
    )
    if not active:
        next_step = ""
    elif trigger == "io_pressure":
        next_step = (
            "The sampler will allow mining again when I/O pressure drops, peer lag is inside the safe window, "
            "and backend template checks are ready."
        )
    elif trigger == "lag_threshold":
        next_step = (
            f"The sampler will allow mining again when peer lag is at or below "
            f"{CATCHUP_PAUSE_THRESHOLD_BLOCKS} blocks and backend template checks are ready."
        )
    else:
        next_step = "The sampler will allow mining again when chain RPC/template checks are healthy and import pressure clears."
    return {
        "enabled": CATCHUP_PAUSE_ENABLED,
        "active": active,
        "trigger": trigger,
        "lag_blocks": lag,
        "threshold_blocks": CATCHUP_PAUSE_THRESHOLD_BLOCKS,
        "io_pressure_pause_enabled": CATCHUP_IO_PRESSURE_PAUSE_ENABLED,
        "io_pressure_active": io_pressure_active,
        "io_pressure_reasons": io_pressure_reasons,
        "io_pressure_min_lag_blocks": CATCHUP_IO_PRESSURE_MIN_LAG_BLOCKS,
        "io_pressure_iowait_warn_percent": CATCHUP_IOWAIT_WARN_PERCENT,
        "io_pressure_io_some_avg10_warn": CATCHUP_IO_SOME_AVG10_WARN,
        "io_pressure_io_full_avg10_warn": CATCHUP_IO_FULL_AVG10_WARN,
        "mining_ready": mining_ready_for_policy,
        "backend_unready_under_pressure": backend_unready_under_pressure,
        "backend_unready_reasons": backend_unready_reasons,
        "backend_sync_active": backend_sync_active,
        "node_sync_busy": node_sync_busy,
        "lag_threshold_active": lag_threshold_active,
        "pool_has_recent_paid_work": bool(pool_has_recent_paid_work),
        "recent_paid_work_suppressed": recent_paid_work_suppressed,
        "pool_pause_recommended": active,
        "pool_pause_active": bool(active and not pool_running),
        "pool_running": pool_running,
        "node_cache_target_mb": CATCHUP_NODE_CACHE_MB,
        "summary": summary,
        "user_message": user_message,
        "next_step": next_step,
    }


def catchup_template_stall_nodes(
    managed_node_details: Mapping[str, Any],
    sync_progress: Mapping[str, Any],
    sync_progress_health: Mapping[str, Any],
    catchup_policy: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    if not catchup_policy.get("active"):
        return {}
    lag = safe_int(catchup_policy.get("lag_blocks"), -1)
    if lag <= CATCHUP_PAUSE_THRESHOLD_BLOCKS:
        return {}
    active_nodes = set(str(node) for node in (sync_progress_health.get("active_nodes") or []))
    progress_nodes = sync_progress.get("nodes") if isinstance(sync_progress.get("nodes"), Mapping) else {}
    stalled: dict[str, dict[str, Any]] = {}
    for node, info in managed_node_details.items():
        if not isinstance(info, Mapping) or node in active_nodes:
            continue
        if info.get("importing") or not info.get("node_template_frozen"):
            continue
        node_progress = progress_nodes.get(node) if isinstance(progress_nodes.get(node), Mapping) else {}
        remaining = safe_int(node_progress.get("remaining_blocks"), safe_int(sync_progress.get("remaining_blocks"), lag))
        if remaining <= CATCHUP_PAUSE_THRESHOLD_BLOCKS:
            continue
        stalled[node] = {
            "remaining_blocks": remaining,
            "template_freeze_age_seconds": info.get("node_template_freeze_age_seconds"),
            "template_freeze_count": info.get("node_template_freeze_count") or 0,
            "lines": info.get("node_template_freeze_lines") or [],
        }
    return stalled


def node_import_blocks_mining(
    sync_progress: Mapping[str, Any],
    node_name: str,
    node_info: Mapping[str, Any],
) -> bool:
    importing = bool(node_info.get("importing"))
    recent_import = bool(
        node_info.get("last_import_age_seconds") is not None
        and safe_int(node_info.get("last_import_age_seconds"), NODE_IMPORT_STALE_SECONDS + 1) <= NODE_IMPORT_STALE_SECONDS
    )
    if not (importing or recent_import):
        return False

    progress_nodes = sync_progress.get("nodes") if isinstance(sync_progress.get("nodes"), Mapping) else {}
    node_progress = progress_nodes.get(node_name) if isinstance(progress_nodes.get(node_name), Mapping) else {}
    remaining = safe_int(node_progress.get("remaining_blocks"), safe_int(sync_progress.get("remaining_blocks"), -1))
    peer_ahead = max(
        safe_int(node_progress.get("peer_ahead_blocks"), -1),
        safe_int(sync_progress.get("peer_ahead_blocks"), -1),
        safe_int(node_info.get("peer_ahead_blocks"), -1),
    )
    if remaining > 0 or peer_ahead > 0:
        return True

    status = str(node_progress.get("status") or sync_progress.get("status") or "").strip().lower()
    rpc_error = str(node_progress.get("chain_rpc_error") or sync_progress.get("chain_rpc_error") or "").strip()
    if status in {"synced", "ok"} and remaining == 0 and not rpc_error:
        return False
    return status in {"syncing", "unknown", "waiting_for_status_sample", ""} or bool(rpc_error)


def collect_status(include_logs: bool = True) -> dict[str, Any]:
    ensure_runtime()
    docker_error = docker_access_error()
    observer_nodes = list(OBSERVER_NODES)
    display_nodes = unique_names([*NODES, *observer_nodes])
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
    latest_action = read_latest_action()
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
            "stale_after_seconds": STATUS_PAYLOAD_STALE_AFTER_SECONDS,
            "stale_sources": ["docker"],
            "mode": "unknown",
            "can_mine": False,
            "can_accept_shares": False,
            "can_submit_blocks": False,
            "truth_sources": {
                "chain_block_count": "getBlockCount chain RPC",
                "chain_main_height": "getMainChainHeight diagnostic",
                "template_height": "diagnostic_only",
                "node_log_height": "diagnostic_only",
            },
            "blocking_failures": [f"docker access unavailable: {docker_error}"],
            "degraded_reasons": [],
            "repair_actions_recent": latest_action,
            "project_root": str(PROJECT_ROOT),
            "runtime_dir": str(RUNTIME_DIR),
            "pool_env_file": str(POOL_ENV_FILE),
            "stack_services": SERVICES,
            "node_services": display_nodes,
            "managed_node_services": NODES,
            "observer_node_services": observer_nodes,
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
                "needs_chain_sync_repair": False,
            },
            "pool": empty_pool,
            "pool_metrics": {
                "generated_at": now_iso(),
                "status": "unavailable",
                "error": f"docker access unavailable: {docker_error}",
                "containers": {},
                "active_connections": None,
                "selected_backend": "",
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
                "template_conversion_stall": {},
            },
            "pool_health": {
                **empty_pool,
                "connected_miners": 0,
                "managed_miners": 0,
                "needs_pool_repair": False,
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
            "latest_action": latest_action,
        }
    observer_nodes = discover_observer_node_services()
    display_nodes = unique_names([*NODES, *observer_nodes])
    services_for_status = unique_names([*SERVICES, *observer_nodes])
    inspected = docker_inspect(services_for_status)
    containers: dict[str, dict[str, Any]] = {}
    node_details: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    stack_failures: list[str] = []
    warnings: list[str] = []
    sync_warnings: list[str] = []
    maintenance_warnings: list[str] = []
    sync_coordinator = read_sync_coordinator_state()
    planned_sync_service_name = planned_sync_service(sync_coordinator)
    planned_pause_leader = str(sync_coordinator.get("active_node") or "")

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
        hard_restore_reasons = chain_data_restore_hard_reasons(node, parsed) if managed_node else []
        if hard_restore_reasons:
            stack_failures.append(
                f"{'; '.join(hard_restore_reasons)}; "
                "restore or resync node data before mining"
            )
        elif managed_node and parsed["critical"]:
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
        if managed_node and parsed["dag_empty_block_storm"]:
            add_sync_warning(
                f"{node} is logging repeated DAG empty-block lookups "
                f"({parsed['dag_empty_block_warnings']} recent warnings, no recent imports)"
            )
        if managed_node and parsed["mining_template_failing"]:
            add_sync_warning(
                f"{node} cannot create fresh mining templates "
                f"({parsed['mining_template_error_count']} recent errors)"
            )
        if managed_node and parsed["node_busy_syncing"]:
            if parsed.get("node_graph_sync_churn"):
                add_sync_warning(
                    f"{node} is churning graph-state sync without recent imports; mining template updates are blocked"
                )
            elif parsed.get("node_template_frozen"):
                add_sync_warning(
                    f"{node} reports frozen mining templates; mining template updates are blocked"
                )
            else:
                add_sync_warning(f"{node} reports node busy syncing; mining template updates are blocked")

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
            "dag_tip_damage": parsed["dag_tip_damage"],
            "dag_tip_damage_lines": parsed["dag_tip_damage_lines"],
            "chain_state_blocker": parsed["chain_state_blocker"],
            "chain_state_blocker_hash": parsed["chain_state_blocker_hash"],
            "chain_state_blocker_lines": parsed["chain_state_blocker_lines"],
            "missing_trie_node_warnings": parsed["missing_trie_node_warnings"],
            "missing_trie_node_lines": parsed["missing_trie_node_lines"],
            "rawdb_pebble_not_found_warnings": parsed["rawdb_pebble_not_found_warnings"],
            "rawdb_pebble_not_found_storm": parsed["rawdb_pebble_not_found_storm"],
            "rawdb_pebble_not_found_threshold": parsed["rawdb_pebble_not_found_threshold"],
            "rawdb_pebble_not_found_lines": parsed["rawdb_pebble_not_found_lines"],
            "rawdb_freezer_missing_header_warnings": parsed["rawdb_freezer_missing_header_warnings"],
            "rawdb_freezer_missing_header_storm": parsed["rawdb_freezer_missing_header_storm"],
            "rawdb_freezer_missing_header_threshold": parsed["rawdb_freezer_missing_header_threshold"],
            "rawdb_freezer_missing_header_lines": parsed["rawdb_freezer_missing_header_lines"],
            "dag_empty_block_warnings": parsed["dag_empty_block_warnings"],
            "dag_empty_block_storm": parsed["dag_empty_block_storm"],
            "dag_empty_block_threshold": parsed["dag_empty_block_threshold"],
            "dag_empty_block_lines": parsed["dag_empty_block_lines"],
            "p2p_error_lines": parsed["p2p_error_lines"],
            "node_graph_sync_count": parsed["node_graph_sync_count"],
            "node_graph_sync_churn": parsed["node_graph_sync_churn"],
            "node_graph_sync_churn_threshold": parsed["node_graph_sync_churn_threshold"],
            "node_graph_sync_churn_lines": parsed["node_graph_sync_churn_lines"],
            "node_template_freeze_count": parsed["node_template_freeze_count"],
            "node_template_freeze_age_seconds": parsed["node_template_freeze_age_seconds"],
            "node_template_frozen": parsed["node_template_frozen"],
            "node_template_freeze_lines": parsed["node_template_freeze_lines"],
            "mining_template_error_count": parsed["mining_template_error_count"],
            "mining_template_hard_error_count": parsed["mining_template_hard_error_count"],
            "mining_template_transient_tx_error_count": parsed["mining_template_transient_tx_error_count"],
            "mining_template_nonce_too_low_count": parsed["mining_template_nonce_too_low_count"],
            "mining_template_error_lines": parsed["mining_template_error_lines"],
            "mining_template_hard_error_lines": parsed["mining_template_hard_error_lines"],
            "mining_template_failing": parsed["mining_template_failing"],
            "node_busy_syncing": parsed["node_busy_syncing"],
            "node_busy_syncing_lines": parsed["node_busy_syncing_lines"],
            "critical": parsed["critical"],
            "critical_lines": parsed["critical_lines"],
            "tail": parsed["tail"],
            "planned_sync_pause": node == planned_sync_service_name,
            "sync_pause_leader": planned_pause_leader if node == planned_sync_service_name else "",
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
        "template_conversion_stall": {},
    }
    pool["metrics"] = pool_metrics
    pool["selected_backend"] = pool_metrics.get("selected_backend") or ""
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
    selected_source_peer_lead_blocks = safe_int(
        selected_source_health.get("node_p2p_best_peer_lead_blocks"),
        0,
    )
    if selected_source_peer_lead_blocks > 0:
        if len(NODES) == 1:
            node_details.setdefault(NODES[0], {})
            node_details[NODES[0]]["peer_ahead_blocks"] = max(
                safe_int(node_details[NODES[0]].get("peer_ahead_blocks"), 0),
                selected_source_peer_lead_blocks,
            )
            node_details[NODES[0]]["peer_ahead_blocks_source"] = (
                "pool_rpc_backend_node_health_p2p_best_peer_lead_blocks"
            )
        add_sync_warning(
            "selected pool backend is still catching up by "
            f"{selected_source_peer_lead_blocks} blocks according to pool backend health"
        )
    pool["source_job_health"] = source_job_health
    pool["source_backend_health"] = source_backend_health
    pool["selected_backend_source_health"] = selected_source_health
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
    block_outcomes = (
        pool_loss_ledger.get("block_outcomes")
        if isinstance(pool_loss_ledger.get("block_outcomes"), dict)
        else {}
    )
    ledger_block_total = safe_int(block_outcomes.get("total"), 0)
    ledger_block_accepted = safe_int(block_outcomes.get("accepted"), 0)
    if ledger_block_total:
        pool["block_submit_failure_count"] = max(
            safe_int(pool.get("block_submit_failure_count"), 0),
            max(0, ledger_block_total - ledger_block_accepted),
        )
    if ledger_block_total >= POOL_BLOCK_SUBMIT_ZERO_SUCCESS_ERROR_COUNT and ledger_block_accepted == 0:
        pool["block_submit_zero_success_storm"] = True
    if pool.get("rpc_refused_recent") and not any("bdag child" in item for item in stack_failures):
        add_sync_warning("pool recently saw RPC connection refused")

    if include_logs and running_pool_containers:
        miner_health = collect_miner_health(source_job_health)
    elif include_logs:
        miner_health = collect_miner_health_from_registry("pool_container_not_running")
    else:
        miner_health = collect_miner_health_from_registry("logs_disabled")
    scan_connected_miners = safe_int(miner_health.get("connected_count"), 0)
    connected_miners = effective_connected_miner_count(miner_health, pool_metrics, source_job_health)
    miner_health["connected_count_effective"] = connected_miners
    miner_health["connected_count_source"] = (
        "miner-health" if connected_miners == scan_connected_miners else "pool-metrics"
    )
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
        "node_template_frozen": any(
            bool(item.get("node_template_frozen"))
            for item in managed_node_details.values()
            if isinstance(item, Mapping)
        ),
        "node_template_frozen_nodes": {
            node: {
                "template_freeze_age_seconds": info.get("node_template_freeze_age_seconds"),
                "template_freeze_count": info.get("node_template_freeze_count") or 0,
                "lines": info.get("node_template_freeze_lines") or [],
            }
            for node, info in managed_node_details.items()
            if isinstance(info, Mapping) and info.get("node_template_frozen")
        },
        "dag_empty_block_storm": any(
            bool(item.get("dag_empty_block_storm"))
            for item in managed_node_details.values()
            if isinstance(item, Mapping)
        ),
        "dag_empty_block_storm_nodes": {
            node: {
                "warnings": info.get("dag_empty_block_warnings") or 0,
                "threshold": info.get("dag_empty_block_threshold") or NODE_DAG_EMPTY_BLOCK_STORM_COUNT,
                "lines": info.get("dag_empty_block_lines") or [],
            }
            for node, info in managed_node_details.items()
            if isinstance(info, Mapping) and info.get("dag_empty_block_storm")
        },
        "rawdb_freezer_missing_header_storm": any(
            bool(item.get("rawdb_freezer_missing_header_storm"))
            for item in managed_node_details.values()
            if isinstance(item, Mapping)
        ),
        "rawdb_freezer_missing_header_storm_nodes": {
            node: {
                "warnings": info.get("rawdb_freezer_missing_header_warnings") or 0,
                "threshold": info.get("rawdb_freezer_missing_header_threshold")
                or CHAIN_STATE_RAWDB_FREEZER_MISSING_HEADER_WARNINGS,
                "lines": info.get("rawdb_freezer_missing_header_lines") or [],
            }
            for node, info in managed_node_details.items()
            if isinstance(info, Mapping) and info.get("rawdb_freezer_missing_header_storm")
        },
        "needs_chain_sync_repair": False,
        "planned_sync_service": planned_sync_service_name,
        "planned_pause_leader": planned_pause_leader,
    }
    chain_blocker_nodes = {
        node: {
            "hash": info.get("chain_state_blocker_hash") or "",
            "lines": info.get("chain_state_blocker_lines") or [],
            "missing_trie_node_warnings": info.get("missing_trie_node_warnings") or 0,
        }
        for node, info in managed_node_details.items()
        if info.get("chain_state_blocker")
    }
    chain_restore_nodes = {
        node: {
            "reasons": reasons,
            "hash": info.get("chain_state_blocker_hash") or "",
            "chain_state_blocker_lines": info.get("chain_state_blocker_lines") or [],
            "dag_tip_damage_lines": info.get("dag_tip_damage_lines") or [],
            "missing_trie_node_warnings": info.get("missing_trie_node_warnings") or 0,
            "missing_trie_node_lines": info.get("missing_trie_node_lines") or [],
            "rawdb_pebble_not_found_warnings": info.get("rawdb_pebble_not_found_warnings") or 0,
            "rawdb_pebble_not_found_lines": info.get("rawdb_pebble_not_found_lines") or [],
            "rawdb_freezer_missing_header_warnings": info.get("rawdb_freezer_missing_header_warnings") or 0,
            "rawdb_freezer_missing_header_lines": info.get("rawdb_freezer_missing_header_lines") or [],
        }
        for node, info in managed_node_details.items()
        if (reasons := chain_data_restore_hard_reasons(node, info))
    }
    chain_restore_candidate_nodes = {
        node: {
            "reasons": reasons,
            "orphan_block_errors": info.get("orphan_block_errors") or 0,
            "orphan_block_error_lines": info.get("orphan_block_error_lines") or [],
            "peer_ahead_blocks": info.get("peer_ahead_blocks"),
            "missing_trie_node_warnings": info.get("missing_trie_node_warnings") or 0,
            "missing_trie_node_lines": info.get("missing_trie_node_lines") or [],
        }
        for node, info in managed_node_details.items()
        if (reasons := chain_data_restore_candidate_reasons(node, info))
    }
    if chain_blocker_nodes:
        sync_health["chain_state_blocker"] = True
        sync_health["chain_state_blocker_nodes"] = chain_blocker_nodes
        sync_health["needs_chain_data_restore"] = True
    if chain_restore_nodes:
        sync_health["chain_data_restore_required"] = True
        sync_health["chain_data_restore_nodes"] = chain_restore_nodes
        sync_health["needs_chain_data_restore"] = True
    if chain_restore_candidate_nodes:
        sync_health["chain_data_restore_candidate"] = True
        sync_health["chain_data_restore_candidate_nodes"] = chain_restore_candidate_nodes
    pool_has_recent_share_activity = any(
        pool.get(field) is not None and int(pool.get(field) or 0) <= max_age
        for field, max_age in (
            ("last_submit_age_seconds", 30),
            ("last_valid_share_age_seconds", 60),
        )
    )
    pool_has_recent_paid_work = bool(
        safe_int(pool.get("block_submit_success_count"), 0) > 0
        and pool.get("last_block_submit_age_seconds") is not None
        and int(pool.get("last_block_submit_age_seconds") or 0) <= 60
    )
    pool_initial_download_transient = bool(
        pool.get("initial_download")
        and pool_has_recent_paid_work
        and not pool.get("share_stall")
    )
    source_job_health_ok_raw = source_job_health.get("ok") if isinstance(source_job_health, dict) else None
    source_job_health_ok = None if source_job_health_ok_raw is None else bool(source_job_health_ok_raw)
    selected_source_unready_reasons = selected_backend_unready_reasons(selected_source_health)
    selected_source_degraded = bool(selected_source_unready_reasons)
    source_job_hard_degraded = bool(source_job_health_ok is False and not pool_has_recent_paid_work)
    selected_source_degradation = selected_backend_source_degradation(
        selected_source_degraded,
        pool_has_recent_paid_work,
    )
    source_selected_backend_hard_degraded = bool(selected_source_degradation["hard"])
    source_selected_backend_advisory_degraded = bool(selected_source_degradation["advisory"])
    source_health_transient_degraded = bool(
        source_job_health_ok is False
        and pool_has_recent_paid_work
    )
    pool["source_job_health_ok"] = source_job_health_ok
    pool["source_job_hard_degraded"] = source_job_hard_degraded
    pool["source_selected_backend_degraded"] = selected_source_degraded
    pool["source_selected_backend_hard_degraded"] = source_selected_backend_hard_degraded
    pool["source_selected_backend_advisory_degraded"] = source_selected_backend_advisory_degraded
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
    sync_health["pool_has_recent_share_activity"] = pool_has_recent_share_activity
    sync_health["pool_has_recent_paid_work"] = pool_has_recent_paid_work
    sync_health["pool_has_recent_mining"] = pool_has_recent_paid_work
    if connected_miners > 0 and pool_has_recent_paid_work and sync_warnings:
        advisory_sync_warnings = [
            item for item in sync_warnings
            if is_recent_mining_sync_noise(item)
        ]
        if advisory_sync_warnings:
            sync_warnings = [
                item for item in sync_warnings
                if not is_recent_mining_sync_noise(item)
            ]
            warnings = [
                item for item in warnings
                if not is_recent_mining_sync_noise(item)
            ]
            for item in advisory_sync_warnings:
                add_maintenance_warning(f"{item}; accepted block submission remains fresh")
    readiness_contract = selected_backend_readiness_contract(
        str(pool.get("selected_backend") or ""),
        selected_source_health,
        source_job_health,
        pool_has_recent_paid_work,
    )
    pool["selected_backend_readiness_contract"] = readiness_contract
    source_advisory_suppressed = bool(
        connected_miners > 0
        and pool_has_recent_paid_work
        and (
            readiness_contract.get("contradiction")
            or source_selected_backend_advisory_degraded
            or source_health_transient_degraded
            or pool_initial_download_transient
        )
    )
    pool["source_health_advisory_suppressed"] = source_advisory_suppressed
    if source_advisory_suppressed:
        pool["source_health_suppressed_reasons"] = selected_source_unready_reasons
    if pool_initial_download_needs_repair:
        add_sync_warning("pool is waiting for node sync to finish")
    elif pool_initial_download_transient and not source_advisory_suppressed:
        add_maintenance_warning(
            "pool saw a transient initial-download template response while accepted block submission stayed fresh"
        )
    if connected_miners > 0 and pool_loss_ledger.get("warnings"):
        ledger_warnings = [str(item) for item in pool_loss_ledger.get("warnings", []) if item]
        if ledger_warnings:
            add_maintenance_warning("pool efficiency loss ledger: " + "; ".join(ledger_warnings[:3]))
    if connected_miners > 0 and readiness_contract.get("contradiction") and not source_advisory_suppressed:
        backend = readiness_contract.get("selected_backend") or "selected backend"
        checks = readiness_contract.get("checks") if isinstance(readiness_contract.get("checks"), dict) else {}
        add_maintenance_warning(
            f"selected backend readiness contradiction: {backend} reports "
            f"mineable={checks.get('node_mineable')} submit_ready={checks.get('node_submit_ready')} "
            "while accepted block submission remains recent"
        )
    if connected_miners > 0 and source_job_hard_degraded:
        add_sync_warning("pool source job health reports not-ok and accepted block submission is stale")
    elif connected_miners > 0 and source_job_health_ok is False and not source_advisory_suppressed:
        add_maintenance_warning("pool source job health is advisory-degraded while accepted block submission remains fresh")
    if connected_miners > 0 and source_selected_backend_hard_degraded:
        backend = pool.get("selected_backend") or "selected backend"
        add_sync_warning(
            f"pool source health says {backend} is not ready for mining "
            f"({', '.join(selected_source_unready_reasons)})"
        )
    elif connected_miners > 0 and source_selected_backend_advisory_degraded and not source_advisory_suppressed:
        backend = pool.get("selected_backend") or "selected backend"
        add_maintenance_warning(
            f"pool source health says {backend} is degraded, but accepted block submission remains fresh "
            f"({', '.join(selected_source_unready_reasons)})"
        )
    elif connected_miners > 0 and source_health_transient_degraded and not source_advisory_suppressed:
        backend = pool.get("selected_backend") or "selected backend"
        add_maintenance_warning(f"pool source health says {backend} is degraded, but accepted block submission remains fresh")
    if connected_miners > 0 and pool.get("share_stall"):
        age = pool.get("last_valid_share_age_seconds")
        age_text = f"{age}s" if age is not None else "unknown"
        add_sync_warning(
            f"pool has not accepted a valid share for {age_text} "
            f"while {connected_miners} miner(s) are connected"
        )
    effective_job_stall = bool(connected_miners > 0 and pool.get("job_stall") and not pool_has_recent_paid_work)
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
    elif pool.get("expired_job_reconnect_failed_no_share"):
        add_sync_warning(
            "pool stale-client expired-job reconnect recovery failed: miners re-authorized, "
            "no valid shares followed, and the client timed out"
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
        backend_to = str(recovery.get("backend_to") or pool.get("selected_backend") or "active backend")
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

    miner_failures = [str(item) for item in miner_health.get("failures", []) if item]
    blocking_miner_failures = (
        miner_failures
        if miner_failures_block_stack(
            miner_failures,
            connected_miners,
            pool_has_recent_share_activity,
            pool_has_recent_paid_work,
            source_job_health_ok,
        )
        else []
    )
    advisory_miner_failures = [
        item for item in miner_failures
        if item not in blocking_miner_failures
    ]
    for item in advisory_miner_failures:
        add_maintenance_warning(f"miner repair required but active mining continues: {item}")
    failures = stack_failures + blocking_miner_failures
    miner_warnings = miner_health.get("warnings", [])
    warnings.extend(miner_warnings)
    maintenance_warnings.extend(miner_warnings)
    pool_health = {
        **pool,
        "rpc_refused_raw": bool(pool.get("rpc_refused")),
        "rpc_refused": bool(pool.get("rpc_refused_recent") and connected_miners > 0),
        "connected_miners": connected_miners,
        "managed_miners": managed_miners,
        "rpc_template_failing": False,
        "node_template_probe_failing": bool(template_probe_health.get("failing_nodes")),
        "share_stall": bool(pool.get("share_stall") and connected_miners > 0),
        "job_stall": effective_job_stall,
        "needs_pool_repair": bool(
            pool_initial_download_needs_repair
            or (pool.get("rpc_refused_recent") and connected_miners > 0)
            or (
                template_probe_health.get("failing_nodes")
                and connected_miners > 0
                and not pool_has_recent_paid_work
            )
            or (pool.get("pool_template_frozen") and connected_miners > 0)
            or (pool.get("duplicate_block_storm") and connected_miners > 0)
            or (pool.get("stale_job_candidate_storm") and connected_miners > 0)
            or (pool.get("block_submit_error_storm") and connected_miners > 0)
            or (pool.get("accepted_job_expired_storm") and connected_miners > 0)
            or pool.get("expired_job_reconnect_failed_no_share")
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
    sync_health["needs_chain_sync_repair"] = bool(sync_warnings and not failures) or pool_health["needs_pool_repair"]

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
    sync_progress = sync_progress_for_display_nodes(collect_sync_progress(), display_nodes)
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
    busy_syncing_nodes = [
        node
        for node in managed_node_details
        if bool(node_details.get(node, {}).get("node_busy_syncing"))
    ]
    active_import_nodes = [
        node
        for node in managed_node_details
        if node_import_blocks_mining(sync_progress, node, node_details.get(node, {}))
    ]
    sync_blocked_nodes = unique_names([*busy_syncing_nodes, *active_import_nodes])
    if sync_blocked_nodes and sync_progress.get("status") != "syncing":
        sync_progress = dict(sync_progress)
        sync_progress["status"] = "syncing"
        sync_progress["error"] = "node busy syncing" if busy_syncing_nodes else "node importing blocks"
        sync_progress["source"] = "nodes:busy-syncing" if busy_syncing_nodes else "nodes:importing"
        nodes_progress = dict(sync_progress.get("nodes") or {})
        for node in sync_blocked_nodes:
            node_progress = nodes_progress.get(node)
            if isinstance(node_progress, dict):
                updated_node_progress = dict(node_progress)
                updated_node_progress["status"] = "syncing"
                updated_node_progress["error"] = "node busy syncing" if node in busy_syncing_nodes else "node importing blocks"
                nodes_progress[node] = updated_node_progress
        sync_progress["nodes"] = nodes_progress
    sync_health["node_busy_syncing"] = bool(busy_syncing_nodes)
    if busy_syncing_nodes:
        sync_health["node_busy_syncing_nodes"] = busy_syncing_nodes
    sync_health["node_importing"] = bool(active_import_nodes)
    if active_import_nodes:
        sync_health["node_importing_nodes"] = active_import_nodes
    sync_progress_nodes = sync_progress.get("nodes") if isinstance(sync_progress.get("nodes"), Mapping) else {}
    chain_rpc_unavailable = bool(
        str(sync_progress.get("status") or "").strip().lower() in {"", "unknown", "waiting_for_status_sample"}
        and (
            str(sync_progress.get("error") or sync_progress.get("chain_rpc_error") or "").strip()
            or any(
                str(node_progress.get("chain_rpc_error") or node_progress.get("error") or "").strip()
                for node_progress in sync_progress_nodes.values()
                if isinstance(node_progress, Mapping)
            )
        )
    )
    template_probe_unavailable = bool(
        template_probe_health.get("all_nodes_failing")
        or template_probe_health.get("all_nodes_ready") is False
        or template_probe_health.get("failing_nodes")
    )
    node_readiness_unavailable = bool(
        (chain_rpc_unavailable or template_probe_unavailable)
        and not pool_has_recent_paid_work
    )
    if node_readiness_unavailable and sync_progress.get("status") != "syncing":
        sync_progress = dict(sync_progress)
        sync_progress["status"] = "syncing"
        sync_progress["error"] = "node chain RPC/template readiness unavailable"
        sync_progress["source"] = "nodes:readiness-unavailable"
        nodes_progress = dict(sync_progress.get("nodes") or {})
        for node in managed_node_details:
            node_progress = nodes_progress.get(node)
            if isinstance(node_progress, dict):
                updated_node_progress = dict(node_progress)
                updated_node_progress["status"] = "syncing"
                updated_node_progress["error"] = "node chain RPC/template readiness unavailable"
                nodes_progress[node] = updated_node_progress
        sync_progress["nodes"] = nodes_progress
        add_sync_warning("node chain RPC/template readiness is unavailable; mining work is intentionally paused")
    sync_health["node_readiness_unavailable"] = node_readiness_unavailable
    sync_health["chain_rpc_unavailable"] = chain_rpc_unavailable
    sync_health["template_probe_unavailable"] = template_probe_unavailable
    catchup_mining_ready = bool(
        sync_progress.get("status") == "synced"
        and (not selected_source_unready_reasons or pool_has_recent_paid_work)
        and source_job_health_ok is not False
        and not pool_initial_download_needs_repair
    )
    catchup_policy = build_catchup_policy(
        sync_progress,
        node_details,
        containers,
        selected_source_health,
        host_pressure,
        mining_ready=catchup_mining_ready,
        pool_has_recent_paid_work=pool_has_recent_paid_work,
    )
    if catchup_policy.get("active"):
        pool_down_message = f"{POOL_CONTAINER} is not running"
        stack_failures = [item for item in stack_failures if item != pool_down_message]
        failures = stack_failures + blocking_miner_failures
        summary = str(catchup_policy.get("summary") or "")
        if summary:
            warnings = [item for item in warnings if item != summary]
            sync_warnings = [item for item in sync_warnings if item != summary]
            warnings.insert(0, summary)
            sync_warnings.insert(0, summary)
        sync_health["catchup_pause_active"] = True
        sync_health["catchup_pause_threshold_blocks"] = catchup_policy.get("threshold_blocks")
        sync_health["catchup_pause_lag_blocks"] = catchup_policy.get("lag_blocks")
        sync_health["catchup_pause_pool_stopped"] = catchup_policy.get("pool_pause_active")
        pool["catchup_pause_active"] = True
        pool["catchup_pause_reason"] = summary
        pool_health["catchup_pause_active"] = True
        pool_health["catchup_pause_reason"] = summary
    no_miner_node_only = bool(
        connected_miners == 0
        and managed_miners == 0
        and any(containers.get(node, {}).get("running") for node in NODES)
    )
    no_miner_sync_only = bool(no_miner_node_only and sync_progress.get("status") == "syncing")
    node_readiness_sync_only = bool(connected_miners == 0 and sync_progress.get("status") == "syncing")
    no_miner_after_expired_reconnect_failure = bool(
        no_miner_node_only and pool.get("expired_job_reconnect_failed_no_share")
    )
    if no_miner_node_only and not no_miner_after_expired_reconnect_failure:
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
        pool_health["needs_pool_repair"] = False
        sync_health["needs_chain_sync_repair"] = False
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
    elif no_miner_after_expired_reconnect_failure:
        add_maintenance_warning(
            "no miners are connected after failed expired-job reconnect recovery; "
            "pool restart/reconnect repair is required"
        )
    sync_progress_health = observe_sync_progress_health(sync_progress)
    active_sync_progress_nodes = sync_progress_health.get("active_nodes") or []
    if active_sync_progress_nodes:
        sync_health["nodes_with_recent_imports"] = max(
            int(sync_health.get("nodes_with_recent_imports") or 0),
            int(sync_progress_health.get("active_node_count") or 0),
        )
        hard_pool_needs_repair = bool(pool_health.get("needs_pool_repair"))
        if sync_progress.get("status") == "syncing" and pool_health.get("initial_download"):
            pool_health["initial_download_needs_repair"] = False
            pool_health["needs_pool_repair"] = hard_pool_needs_repair
        if sync_progress.get("status") == "syncing" and not failures and not pool_health["needs_pool_repair"]:
            sync_health["needs_chain_sync_repair"] = False
    sync_health["sync_progress_health"] = sync_progress_health
    stalled_template_nodes = catchup_template_stall_nodes(
        managed_node_details,
        sync_progress,
        sync_progress_health,
        catchup_policy,
    )
    if stalled_template_nodes:
        sync_health["catchup_template_stall"] = True
        sync_health["catchup_template_stall_nodes"] = stalled_template_nodes
        sync_health["needs_chain_sync_repair"] = True
    dag_empty_block_nodes = {
        node: info
        for node, info in (sync_health.get("dag_empty_block_storm_nodes") or {}).items()
        if node not in active_sync_progress_nodes
    }
    if dag_empty_block_nodes:
        sync_health["dag_empty_block_storm_nodes"] = dag_empty_block_nodes
        sync_health["needs_chain_sync_repair"] = True

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

    mode = (
        "catchup_pause"
        if catchup_policy.get("active")
        else "mining"
        if connected_miners > 0
        else ("sync_only_no_miners" if no_miner_sync_only or node_readiness_sync_only else "ready_no_miners")
    )
    can_accept_shares = bool(connected_miners > 0 and containers.get(POOL_CONTAINER, {}).get("running") and not failures)
    can_submit_blocks = bool(can_accept_shares and not pool_health.get("needs_pool_repair") and not sync_warnings)
    can_mine = bool(can_accept_shares and can_submit_blocks)
    truth_sources = {
        "chain_block_count": "getBlockCount chain RPC",
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
        "stale_after_seconds": STATUS_PAYLOAD_STALE_AFTER_SECONDS,
        "stale_sources": [],
        "mode": mode,
        "can_mine": can_mine,
        "can_accept_shares": can_accept_shares,
        "can_submit_blocks": can_submit_blocks,
        "truth_sources": truth_sources,
        "blocking_failures": failures,
        "degraded_reasons": sync_warnings + maintenance_warnings,
        "repair_actions_recent": latest_action,
        "project_root": str(PROJECT_ROOT),
        "runtime_dir": str(RUNTIME_DIR),
        "pool_env_file": str(POOL_ENV_FILE),
        "stack_services": SERVICES,
        "node_services": display_nodes,
        "managed_node_services": NODES,
        "observer_node_services": observer_nodes,
        "pool_container": POOL_CONTAINER,
        "pool_containers": POOL_CONTAINERS,
        "pool_db_container": POOL_DB_CONTAINER,
        "overall": overall,
        "status_reason": status_reason,
        "containers": containers,
        "nodes": node_details,
        "sync_progress": sync_progress,
        "sync_health": sync_health,
        "sync_coordinator": sync_coordinator,
        "catchup_policy": catchup_policy,
        "rpc_template_health": template_probe_health,
        "pool": pool,
        "pool_metrics": pool_metrics,
        "pool_health": pool_health,
        "failures": failures,
        "stack_failures": stack_failures,
        "miner_failures": miner_failures,
        "blocking_miner_failures": blocking_miner_failures,
        "advisory_miner_failures": advisory_miner_failures,
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
        "latest_action": read_latest_action(),
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


def _sync_priority_add_lag(values: list[int], value: Any) -> None:
    parsed = safe_int(value, -1)
    if parsed >= 0:
        values.append(parsed)


def _sync_priority_block_gap(info: Mapping[str, Any]) -> int:
    current = safe_int(
        info.get("evm_block_count")
        if info.get("evm_block_count") is not None
        else info.get("current_block")
        if info.get("current_block") is not None
        else info.get("latest_block"),
        -1,
    )
    highest = safe_int(
        info.get("evm_reference_block_count")
        if info.get("evm_reference_block_count") is not None
        else info.get("highest_block"),
        -1,
    )
    if current >= 0 and highest >= current:
        return highest - current
    return -1


def _sync_priority_lag_blocks(payload: Mapping[str, Any], sync_progress: Mapping[str, Any]) -> int:
    values: list[int] = []
    policy = payload.get("catchup_policy") if isinstance(payload.get("catchup_policy"), Mapping) else {}
    if isinstance(policy, Mapping):
        _sync_priority_add_lag(values, policy.get("lag_blocks"))
    for key in (
        "remaining_blocks",
        "peer_ahead_blocks",
        "evm_lag_to_reference",
        "evm_lag_to_chain",
        "reference_lag_blocks",
        "chain_tip_lag_blocks",
    ):
        _sync_priority_add_lag(values, sync_progress.get(key))
    _sync_priority_add_lag(values, _sync_priority_block_gap(sync_progress))
    for group in (sync_progress.get("nodes"), payload.get("nodes")):
        if not isinstance(group, Mapping):
            continue
        for info in group.values():
            if not isinstance(info, Mapping):
                continue
            for key in (
                "remaining_blocks",
                "peer_ahead_blocks",
                "evm_lag_to_reference",
                "evm_lag_to_chain",
                "reference_lag_blocks",
                "node_p2p_best_peer_lead_blocks",
            ):
                _sync_priority_add_lag(values, info.get(key))
            alignment = info.get("public_chain_alignment")
            if isinstance(alignment, Mapping):
                _sync_priority_add_lag(values, alignment.get("reference_lag_blocks"))
            _sync_priority_add_lag(values, _sync_priority_block_gap(info))
    return max(values) if values else -1


def sync_priority_decision(task: str, status: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return whether chain catch-up should preempt optional local work."""
    if not SYNC_PRIORITY_ENABLED:
        return {
            "enabled": False,
            "active": False,
            "task": task,
            "reasons": [],
            "lag_blocks": -1,
            "min_lag_blocks": SYNC_PRIORITY_MIN_LAG_BLOCKS,
            "defer_dashboard_samplers": False,
        }

    try:
        payload = status if isinstance(status, dict) else collect_status_cached(include_logs=False)
    except Exception as exc:  # noqa: BLE001 - priority probing must not take down callers.
        return {
            "enabled": True,
            "active": False,
            "task": task,
            "reasons": [],
            "lag_blocks": -1,
            "min_lag_blocks": SYNC_PRIORITY_MIN_LAG_BLOCKS,
            "defer_dashboard_samplers": False,
            "error": str(exc),
        }

    sync_progress = payload.get("sync_progress") if isinstance(payload.get("sync_progress"), dict) else {}
    host_pressure = payload.get("host_pressure") if isinstance(payload.get("host_pressure"), dict) else {}
    catchup_policy = payload.get("catchup_policy") if isinstance(payload.get("catchup_policy"), dict) else {}
    sync_status = str(sync_progress.get("status") or "unknown").strip().lower()
    mode = str(payload.get("mode") or "unknown").strip().lower()
    lag_blocks = _sync_priority_lag_blocks(payload, sync_progress)
    payload_nodes = payload.get("nodes") if isinstance(payload.get("nodes"), Mapping) else {}
    importing = sync_status == "syncing" or any(
        isinstance(info, Mapping) and bool(info.get("importing"))
        for info in payload_nodes.values()
    )
    io_pressure_reasons = catchup_policy.get("io_pressure_reasons")
    if not isinstance(io_pressure_reasons, list):
        io_pressure_reasons = catchup_io_pressure_reasons(host_pressure)

    reasons: list[str] = []
    if bool(catchup_policy.get("active")):
        reasons.append(f"catchup_policy_active:{catchup_policy.get('trigger') or 'active'}")
    if mode == "catchup_pause":
        reasons.append("stack_mode=catchup_pause")
    if sync_status == "syncing":
        lag_text = "unknown" if lag_blocks < 0 else str(lag_blocks)
        reasons.append(f"sync_status=syncing lag={lag_text}")
    if lag_blocks >= SYNC_PRIORITY_MIN_LAG_BLOCKS and sync_status != "synced":
        reasons.append(f"lag_blocks={lag_blocks}>={SYNC_PRIORITY_MIN_LAG_BLOCKS}")
    if importing and lag_blocks >= SYNC_PRIORITY_MIN_LAG_BLOCKS:
        reasons.append("node_importing_while_behind")
    if bool(catchup_policy.get("io_pressure_active")) and lag_blocks >= SYNC_PRIORITY_MIN_LAG_BLOCKS:
        reasons.append("catchup_io_pressure_active")
    elif io_pressure_reasons and importing and lag_blocks >= SYNC_PRIORITY_MIN_LAG_BLOCKS:
        reasons.append("catchup_io_pressure:" + ",".join(str(item) for item in io_pressure_reasons[:3]))

    active = bool(reasons)
    return {
        "enabled": True,
        "active": active,
        "task": task,
        "reasons": reasons,
        "lag_blocks": lag_blocks,
        "min_lag_blocks": SYNC_PRIORITY_MIN_LAG_BLOCKS,
        "sync_status": sync_status,
        "mode": mode,
        "importing": importing,
        "catchup_policy_active": bool(catchup_policy.get("active")),
        "io_pressure_reasons": io_pressure_reasons,
        "defer_dashboard_samplers": bool(active and SYNC_PRIORITY_DEFER_DASHBOARD_SAMPLERS),
        "shared_status_cache": payload.get("shared_status_cache"),
    }


def _background_task_selected(task: str, selected: set[str]) -> bool:
    normalized = str(task or "").strip()
    return "*" in selected or normalized in selected


def _pool_ready_payload_requires_log_truth(payload: Mapping[str, Any]) -> bool:
    mode = str(payload.get("mode") or "unknown").strip().lower()
    if mode in {"down", "degraded", "syncing", "unknown", "ready_no_miners"}:
        return True
    return any(payload.get(key) is False for key in ("can_mine", "can_accept_shares", "can_submit_blocks"))


def background_pool_ready_payload(payload: dict[str, Any], status_supplied: bool) -> dict[str, Any]:
    """Use recent log-derived sampler truth before accepting a cheap no-log no-miner state."""
    if status_supplied or not _pool_ready_payload_requires_log_truth(payload):
        return payload
    sampled = read_status_sampler_payload(
        include_logs=True,
        max_age_seconds=BACKGROUND_MAINTENANCE_POOL_READY_STATUS_MAX_AGE_SECONDS,
    )
    if not isinstance(sampled, dict):
        return payload
    selected = dict(sampled)
    selected["background_pool_ready_status_source"] = {
        "selected": "status_sampler_with_logs",
        "max_age_seconds": BACKGROUND_MAINTENANCE_POOL_READY_STATUS_MAX_AGE_SECONDS,
        "previous_mode": payload.get("mode"),
        "previous_can_mine": payload.get("can_mine"),
        "previous_can_accept_shares": payload.get("can_accept_shares"),
        "previous_can_submit_blocks": payload.get("can_submit_blocks"),
        "reason": "no_logs_status_cannot_prove_stratum_lane_activity",
    }
    return selected


def background_maintenance_decision(task: str, status: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return whether optional background work should run on this tick."""
    task_is_lazy = _background_task_selected(task, BACKGROUND_MAINTENANCE_LAZY_TASKS)
    pool_ready_required = _background_task_selected(task, BACKGROUND_MAINTENANCE_POOL_READY_TASKS)
    sync_priority_exempt = _background_task_selected(task, BACKGROUND_MAINTENANCE_SYNC_PRIORITY_EXEMPT_TASKS)
    io_pressure_exempt = _background_task_selected(task, BACKGROUND_MAINTENANCE_IO_PRESSURE_EXEMPT_TASKS)
    profile = host_runtime_profile()
    if not BACKGROUND_MAINTENANCE_BACKOFF_ENABLED:
        return {
            "allowed": True,
            "task": task,
            "reasons": [],
            "backoff_enabled": False,
            "host_profile": profile,
            "task_is_lazy": task_is_lazy,
            "pool_ready_required": pool_ready_required,
            "sync_priority_exempt": sync_priority_exempt,
            "io_pressure_exempt": io_pressure_exempt,
        }

    status_supplied = isinstance(status, dict)
    payload = status if status_supplied else collect_status_cached(include_logs=False)
    if pool_ready_required:
        payload = background_pool_ready_payload(payload, status_supplied)
    sync_progress = payload.get("sync_progress") if isinstance(payload.get("sync_progress"), dict) else {}
    host_pressure = payload.get("host_pressure") if isinstance(payload.get("host_pressure"), dict) else {}
    reasons: list[str] = []
    sync_priority = sync_priority_decision(task, payload)
    if sync_priority.get("active") and not sync_priority_exempt:
        priority_reason = "; ".join(str(item) for item in sync_priority.get("reasons", []) if item)
        reasons.append(f"sync priority active: {priority_reason or 'chain catch-up needs host capacity'}")
    sync_status = str(sync_progress.get("status") or "unknown")
    remaining_blocks = _sync_remaining_blocks(sync_progress)
    chain_rpc_latency_ms = _sync_chain_rpc_latency_ms(sync_progress)
    if sync_status == "syncing" and not sync_priority_exempt and (
        remaining_blocks < 0 or remaining_blocks > BACKGROUND_MAINTENANCE_SYNC_BACKOFF_BLOCKS
    ):
        remaining_text = "unknown" if remaining_blocks < 0 else str(remaining_blocks)
        reasons.append(
            "chain catch-up has priority "
            f"status={sync_status} remaining={remaining_text} "
            f"threshold={BACKGROUND_MAINTENANCE_SYNC_BACKOFF_BLOCKS}"
        )

    iowait = safe_float(host_pressure.get("iowait_percent"))
    if bool(host_pressure.get("iowait_warning_active")) and not io_pressure_exempt:
        reasons.append("host IO wait warning is active")
    elif iowait is not None and not io_pressure_exempt and iowait >= BACKGROUND_MAINTENANCE_IOWAIT_WARN_PERCENT:
        reasons.append(
            f"host iowait {iowait:.2f}% >= {BACKGROUND_MAINTENANCE_IOWAIT_WARN_PERCENT:.2f}%"
        )

    io_some = safe_float(host_pressure.get("io_some_avg10"))
    if io_some is not None and not io_pressure_exempt and io_some >= BACKGROUND_MAINTENANCE_IO_SOME_AVG10_WARN:
        reasons.append(
            f"host io pressure avg10 {io_some:.2f} >= {BACKGROUND_MAINTENANCE_IO_SOME_AVG10_WARN:.2f}"
        )

    io_full = safe_float(host_pressure.get("io_full_avg10"))
    if io_full is not None and not io_pressure_exempt and io_full >= BACKGROUND_MAINTENANCE_IO_FULL_AVG10_WARN:
        reasons.append(
            f"host io full pressure avg10 {io_full:.2f} >= {BACKGROUND_MAINTENANCE_IO_FULL_AVG10_WARN:.2f}"
        )

    cpu_some = safe_float(host_pressure.get("cpu_some_avg10"))
    if cpu_some is not None and cpu_some >= BACKGROUND_MAINTENANCE_CPU_SOME_AVG10_WARN:
        reasons.append(
            f"host cpu pressure avg10 {cpu_some:.2f} >= {BACKGROUND_MAINTENANCE_CPU_SOME_AVG10_WARN:.2f}"
        )
    memory_available_percent = safe_float(host_pressure.get("memory_available_percent"))
    if bool(host_pressure.get("memory_warning_active")):
        reasons.append("host RAM available warning is active")
    elif (
        memory_available_percent is not None
        and memory_available_percent <= BACKGROUND_MAINTENANCE_MEMORY_AVAILABLE_WARN_PERCENT
    ):
        reasons.append(
            "host RAM available "
            f"{memory_available_percent:.2f}% <= {BACKGROUND_MAINTENANCE_MEMORY_AVAILABLE_WARN_PERCENT:.2f}%"
        )
    swap_used_percent = safe_float(host_pressure.get("swap_used_percent"))
    if host_pressure_swap_active(
        host_pressure,
        BACKGROUND_MAINTENANCE_SWAP_USED_WARN_PERCENT,
        BACKGROUND_MAINTENANCE_MEMORY_AVAILABLE_WARN_PERCENT,
    ):
        reasons.append(
            f"host swap pressure is active {swap_used_percent:.2f}% >= "
            f"{BACKGROUND_MAINTENANCE_SWAP_USED_WARN_PERCENT:.2f}%"
        )
    if (
        chain_rpc_latency_ms is not None
        and not sync_priority_exempt
        and chain_rpc_latency_ms >= BACKGROUND_MAINTENANCE_CHAIN_RPC_WARN_MS
    ):
        reasons.append(
            f"chain RPC latency {chain_rpc_latency_ms:.1f}ms >= {BACKGROUND_MAINTENANCE_CHAIN_RPC_WARN_MS:.1f}ms"
        )

    loadavg_1m = safe_float(host_pressure.get("loadavg_1m"))
    cpu_count = max(1, safe_int(profile.get("cpu_count"), os.cpu_count() or 1))
    loadavg_warn = round(cpu_count * BACKGROUND_MAINTENANCE_LOADAVG_PER_CPU_WARN, 2)
    if task_is_lazy and loadavg_1m is not None and loadavg_1m >= loadavg_warn:
        reasons.append(
            f"host loadavg_1m {loadavg_1m:.2f} >= lazy threshold {loadavg_warn:.2f}"
        )

    if pool_ready_required:
        overall = str(payload.get("overall") or "unknown").strip().lower()
        mode = str(payload.get("mode") or "unknown").strip().lower()
        if overall != "ok":
            reasons.append(f"pool status is not ok: overall={overall}")
        if mode in {"down", "degraded", "syncing", "unknown", "ready_no_miners"}:
            reasons.append(f"pool mode is not ready for archive work: mode={mode}")
        for key in ("can_mine", "can_accept_shares", "can_submit_blocks"):
            if payload.get(key) is False:
                reasons.append(f"pool {key}=false")

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
        "io_full_avg10": io_full,
        "io_full_avg10_warn": BACKGROUND_MAINTENANCE_IO_FULL_AVG10_WARN,
        "cpu_some_avg10": cpu_some,
        "cpu_some_avg10_warn": BACKGROUND_MAINTENANCE_CPU_SOME_AVG10_WARN,
        "memory_available_percent": memory_available_percent,
        "memory_available_warn_percent": BACKGROUND_MAINTENANCE_MEMORY_AVAILABLE_WARN_PERCENT,
        "swap_used_percent": swap_used_percent,
        "swap_used_warn_percent": BACKGROUND_MAINTENANCE_SWAP_USED_WARN_PERCENT,
        "chain_rpc_latency_ms": chain_rpc_latency_ms,
        "chain_rpc_latency_warn_ms": BACKGROUND_MAINTENANCE_CHAIN_RPC_WARN_MS,
        "loadavg_1m": loadavg_1m,
        "loadavg_1m_warn": loadavg_warn if task_is_lazy else None,
        "task_is_lazy": task_is_lazy,
        "pool_ready_required": pool_ready_required,
        "sync_priority_exempt": sync_priority_exempt,
        "io_pressure_exempt": io_pressure_exempt,
        "host_profile": profile,
        "adaptive_concurrency": adaptive_worker_budgets({**host_pressure, "chain_rpc_latency_ms": chain_rpc_latency_ms}),
        "shared_status_cache": payload.get("shared_status_cache"),
        "background_pool_ready_status_source": payload.get("background_pool_ready_status_source"),
    }


def pool_db_json(sql: str) -> Any:
    result = run(
        [
            "docker",
            "exec",
            compose_container_name(POOL_DB_CONTAINER),
            "psql",
            "-U",
            POOL_DB_USER,
            "-d",
            POOL_DB_NAME,
            "-t",
            "-A",
            "-c",
            sql,
        ],
        timeout=20,
    )
    if not result.ok:
        raise RuntimeError((result.stderr or result.stdout or f"{POOL_DB_CONTAINER} query failed").strip())
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
        return {"error": "unexpected postgres response"}

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


def named_url_from_env(name: str, default_source: str) -> list[tuple[str, str]]:
    value = os.environ.get(name, "").strip()
    if not value:
        return []
    if "=" in value:
        source, url = value.split("=", 1)
        return [(source.strip() or default_source, url.strip())]
    return [(default_source, value)]


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


def node_rpc_endpoint() -> tuple[str, str] | None:
    configured = named_url_from_env("BDAG_NODE_RPC_URL", NODE_SERVICE)
    if configured:
        source, url = configured[0]
        url = url.strip()
        if valid_url(url):
            return source.strip() or NODE_SERVICE, url

    for source, url in mining_rpc_urls():
        if valid_url(url):
            return source, url
    return None


def docker_container_ip(name: str) -> str:
    ip = run(
        ["docker", "inspect", compose_container_name(name), "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"],
        timeout=8,
    ).stdout.strip()
    return ip if valid_ipv4(ip) else ""


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

    for name in NODES:
        ip = docker_container_ip(name)
        if ip:
            urls.append((name, f"http://{ip}:{NODE_EVM_RPC_PORT}"))
    return urls


def global_chain_rpc_urls() -> list[tuple[str, str]]:
    configured = (
        named_urls_from_env("BDAG_GLOBAL_CHAIN_RPC_URLS", [])
        or named_url_from_env("BDAG_NODE_RPC_URL", NODE_SERVICE)
    )
    urls: list[tuple[str, str]] = []
    for source, url in configured:
        normalized = _host_url_for_dashboard(url.strip())
        if valid_url(normalized):
            urls.append((source.strip() or "configured-chain", normalized))
    if not urls:
        urls.extend(mining_rpc_urls())
    if GLOBAL_CHAIN_PEER_RPC_ENABLED:
        urls.extend(peer_chain_rpc_urls())
    return _dedupe_rpc_urls(urls)


def _dedupe_rpc_urls(sources: list[tuple[str, str]]) -> list[tuple[str, str]]:
    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source, url in sources:
        normalized = url.strip()
        if not valid_url(normalized) or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append((source.strip() or f"rpc-{len(deduped) + 1}", normalized))
    return deduped


PEER_MULTIADDR_HOST_RE = re.compile(r"/(?:ip4|dns|dns4)/([^/]+)/tcp/[0-9]+")


def peer_host_from_multiaddr(value: str) -> str:
    match = PEER_MULTIADDR_HOST_RE.search(value.strip())
    return match.group(1).strip() if match else ""


def peer_chain_rpc_urls(limit: int | None = None) -> list[tuple[str, str]]:
    peer_limit = GLOBAL_CHAIN_PEER_RPC_LIMIT if limit is None else max(0, limit)
    if peer_limit <= 0:
        return []

    hosts: list[tuple[str, str]] = []
    seen_hosts: set[str] = set()

    def add_host(host: object, source: str) -> None:
        text = str(host or "").strip()
        if not text or text in seen_hosts or any(ch in text for ch in "/?#[]"):
            return
        if any(ord(ch) < 32 or ch.isspace() for ch in text):
            return
        seen_hosts.add(text)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-")[:48] or f"peer-{len(hosts) + 1}"
        hosts.append((f"{source}-{safe}", text))

    discovery = read_json_file(PEER_DISCOVERY_FILE, {})
    if isinstance(discovery, Mapping):
        peers = discovery.get("peers")
        if isinstance(peers, list):
            for peer in peers:
                if not isinstance(peer, Mapping):
                    continue
                status = str(peer.get("status") or "").strip().lower()
                if status and status != "tcp-open":
                    continue
                add_host(peer.get("host"), "live-peer")

    for path, source in (
        (LIVE_PEERS_FILE, "live-peer"),
        (CHAIN_PEERSTORE_CANDIDATES_FILE, "peerstore-peer"),
    ):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            host = peer_host_from_multiaddr(line)
            if host:
                add_host(host, source)
            if len(hosts) >= peer_limit:
                break
        if len(hosts) >= peer_limit:
            break

    return _dedupe_rpc_urls(
        [(source, f"http://{host}:{GLOBAL_CHAIN_PEER_RPC_PORT}") for source, host in hosts[:peer_limit]]
    )


def chain_rpc_timeout_for_source(source_name: str, default: float) -> float:
    if source_name.startswith(("live-peer-", "peerstore-peer-")):
        return min(default, GLOBAL_CHAIN_PEER_RPC_TIMEOUT)
    return default


def public_evm_rpc_urls() -> list[tuple[str, str]]:
    configured = named_urls_from_env("BDAG_PUBLIC_RPC_URLS", PUBLIC_EVM_RPC_DEFAULTS)
    return _dedupe_rpc_urls([(source, url) for source, url in configured])


def evm_reference_rpc_urls() -> list[tuple[str, str]]:
    configured = named_urls_from_env("BDAG_EVM_REFERENCE_RPC_URLS", [])
    if configured:
        return _dedupe_rpc_urls([(source, url) for source, url in configured])
    return public_evm_rpc_urls()


def local_evm_balance_rpc_urls() -> list[tuple[str, str]]:
    if not LOCAL_EVM_BALANCE_PROBE_ENABLED:
        return []
    return global_evm_rpc_urls()


def local_evm_balance_probe_pause_from_status(status: Mapping[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    overall = str(status.get("overall") or "").strip().lower()
    mode = str(status.get("mode") or "").strip().lower()
    if overall == "syncing":
        reasons.append("status is syncing")
    if mode == "catchup_pause":
        reasons.append("catch-up pause is active")

    sync_progress = status.get("sync_progress") if isinstance(status.get("sync_progress"), Mapping) else {}
    sync_status = str(sync_progress.get("status") or "").strip().lower()
    remaining_blocks = safe_int(sync_progress.get("remaining_blocks"), 0)
    if sync_status == "syncing" and remaining_blocks > 0:
        reasons.append(f"node is {remaining_blocks} blocks behind")

    sync_health = status.get("sync_health") if isinstance(status.get("sync_health"), Mapping) else {}
    if bool(sync_health.get("catchup_pause_active")):
        reasons.append("sync health reports catch-up pause")
    if bool(sync_health.get("chain_data_restore_candidate")):
        reasons.append("chain-state restore candidate is under observation")
    if bool(sync_health.get("needs_chain_data_restore") or sync_health.get("chain_data_restore_required")):
        reasons.append("chain-state restore is required")

    nodes = status.get("nodes") if isinstance(status.get("nodes"), Mapping) else {}
    for node_name, node in nodes.items():
        if not isinstance(node, Mapping):
            continue
        if bool(node.get("node_busy_syncing")):
            reasons.append(f"{node_name} reports busy syncing")
        if bool(node.get("node_template_frozen")):
            reasons.append(f"{node_name} reports frozen mining templates")

    deduped = unique_names(reasons)
    return {
        "paused": bool(deduped),
        "reason": "; ".join(deduped),
        "reasons": deduped,
    }


def local_evm_balance_probe_pause() -> dict[str, Any]:
    if not LOCAL_EVM_BALANCE_PROBE_ENABLED:
        reason = "local EVM balance probes are disabled"
        return {"paused": True, "reason": reason, "reasons": [reason]}
    if not LOCAL_EVM_BALANCE_PROBE_PAUSE_DURING_SYNC:
        return {"paused": False, "reason": "", "reasons": []}
    status = read_status_sampler_payload(
        include_logs=False,
        max_age_seconds=LOCAL_EVM_BALANCE_PROBE_STATUS_MAX_AGE_SECONDS,
    )
    if not isinstance(status, dict):
        return {"paused": False, "reason": "", "reasons": []}
    pause = local_evm_balance_probe_pause_from_status(status)
    pause["status_generated_at"] = status.get("generated_at")
    pause["status_overall"] = status.get("overall")
    pause["status_mode"] = status.get("mode")
    return pause


def local_evm_rpc_pause_skipped_source(source: str, pause: Mapping[str, Any]) -> dict[str, Any]:
    reason = str(pause.get("reason") or "local EVM balance probes are paused")
    return {
        "source": source,
        "type": "local-rpc",
        "status": "skipped",
        "error": reason,
        "pause_reason": reason,
    }


def filter_local_evm_rpc_urls(
    sources: list[tuple[str, str]],
    local_sources: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    local_urls = {url for _source, url in local_sources}
    return [(source, url) for source, url in sources if url not in local_urls]


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


def rpc_url_port(url: str) -> int | None:
    try:
        return urllib.parse.urlsplit(url).port
    except ValueError:
        return None


def sibling_evm_rpc_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return f"http://127.0.0.1:{NODE_EVM_RPC_PORT}"
    hostname = parsed.hostname or "127.0.0.1"
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    return urllib.parse.urlunsplit(
        (
            parsed.scheme or "http",
            f"{host}:{NODE_EVM_RPC_PORT}",
            parsed.path or "",
            parsed.query,
            parsed.fragment,
        )
    )


def rpc_method_unavailable(error: str) -> bool:
    text = str(error).lower()
    return "-32601" in text or "method" in text and ("not exist" in text or "not available" in text)


def eth_syncing_details(url: str, timeout: float) -> dict[str, Any]:
    try:
        result = json_rpc_call(url, "eth_syncing", [], timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - sync detail is diagnostic only.
        return {"eth_syncing_error": str(exc)}
    if result is False:
        return {"eth_syncing": False, "chain_syncing": False}
    if isinstance(result, dict):
        current = result.get("currentBlock") or result.get("current_block")
        highest = result.get("highestBlock") or result.get("highest_block")
        details: dict[str, Any] = {"eth_syncing": result, "chain_syncing": True}
        try:
            details["sync_current_block"] = parse_rpc_quantity(current)
        except Exception:
            pass
        try:
            details["sync_highest_block"] = parse_rpc_quantity(highest)
        except Exception:
            pass
        return details
    return {"eth_syncing": result, "chain_syncing": bool(result)}


def normalize_eth_address(value: Any) -> str:
    address = str(value or "").strip().lower()
    return address if valid_eth_address(address) else ""


def evm_block_miner(header: Mapping[str, Any] | None) -> str:
    if not isinstance(header, Mapping):
        return ""
    for key in ("miner", "author", "coinbase"):
        address = normalize_eth_address(header.get(key))
        if address:
            return address
    return ""


def evm_block_hash(header: Mapping[str, Any] | None) -> str:
    if not isinstance(header, Mapping):
        return ""
    value = str(header.get("hash") or "").strip().lower()
    return value if value.startswith("0x") else ""


def evm_block_header_summary(height: int, header: Mapping[str, Any] | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "height": height,
        "miner": evm_block_miner(header),
        "hash": evm_block_hash(header),
        "timestamp": None,
    }
    if isinstance(header, Mapping):
        try:
            payload["timestamp"] = parse_rpc_quantity(header.get("timestamp"))
        except Exception:
            payload["timestamp"] = None
    return payload


def evm_public_alignment_sample_heights(local_block: int, reference_block: int, sample_count: int) -> list[int]:
    highest = min(int(local_block), int(reference_block))
    if highest < 0:
        return []
    offsets = [0, 1, 2, 5, 10, 25, 50, 100]
    heights: list[int] = []
    for offset in offsets:
        if len(heights) >= sample_count:
            break
        height = highest - offset
        if height >= 0 and height not in heights:
            heights.append(height)
    next_height = highest - 1
    while len(heights) < sample_count and next_height >= 0:
        if next_height not in heights:
            heights.append(next_height)
        next_height -= 1
    return heights


def evm_public_chain_alignment(
    *,
    local_url: str,
    reference_source: str,
    reference_url: str,
    local_block: int,
    reference_block: int,
    reference_lag: int,
    mining_address: str,
    timeout: float,
) -> dict[str, Any]:
    alignment: dict[str, Any] = {
        "enabled": bool(reference_url and reference_url != local_url),
        "local_url": local_url,
        "reference_source": reference_source,
        "reference_url": reference_url,
        "local_block": local_block,
        "reference_block": reference_block,
        "reference_lag_blocks": reference_lag,
        "min_reference_lag_blocks": EVM_PUBLIC_ALIGNMENT_MIN_REFERENCE_LAG,
        "min_samples": EVM_PUBLIC_ALIGNMENT_MIN_SAMPLES,
        "sample_heights": [],
        "local_samples": [],
        "reference_samples": [],
        "sample_errors": [],
        "compared_count": 0,
        "reference_sample_count": 0,
        "hash_mismatch_count": 0,
        "miner_mismatch_count": 0,
        "timestamp_mismatch_count": 0,
        "local_miners": [],
        "reference_miners": [],
        "local_solo_miner": False,
        "local_only_miner": "",
        "reference_has_other_miners": False,
        "hash_divergence_suspected": False,
        "solo_mining_suspected": False,
        "public_chain_diverged": False,
        "reason": "",
    }
    pool_address = normalize_eth_address(read_env_value("MINING_ADDRESS") or mining_address)
    if not alignment["enabled"]:
        alignment["reason"] = "no independent public EVM reference was available"
        return alignment

    heights = evm_public_alignment_sample_heights(
        int(local_block),
        int(reference_block),
        EVM_PUBLIC_ALIGNMENT_SAMPLE_BLOCKS,
    )
    alignment["sample_heights"] = heights
    local_samples: list[dict[str, Any]] = []
    reference_samples: list[dict[str, Any]] = []
    errors: list[str] = []
    for height in heights:
        local_summary: dict[str, Any] | None = None
        reference_summary: dict[str, Any] | None = None
        try:
            local_summary = evm_block_header_summary(height, fetch_block_header(local_url, height, timeout=timeout))
            local_samples.append(local_summary)
        except Exception as exc:  # noqa: BLE001 - alignment is a safety diagnostic.
            errors.append(f"local:{height}: {exc}")
        try:
            reference_summary = evm_block_header_summary(height, fetch_block_header(reference_url, height, timeout=timeout))
            reference_samples.append(reference_summary)
        except Exception as exc:  # noqa: BLE001 - public references are best-effort.
            errors.append(f"{reference_source}:{height}: {exc}")
        if local_summary and reference_summary:
            if local_summary.get("hash") and reference_summary.get("hash") and local_summary["hash"] != reference_summary["hash"]:
                alignment["hash_mismatch_count"] += 1
            if (
                local_summary.get("miner")
                and reference_summary.get("miner")
                and local_summary["miner"] != reference_summary["miner"]
            ):
                alignment["miner_mismatch_count"] += 1
            if (
                local_summary.get("timestamp") is not None
                and reference_summary.get("timestamp") is not None
                and local_summary["timestamp"] != reference_summary["timestamp"]
            ):
                alignment["timestamp_mismatch_count"] += 1
    alignment["local_samples"] = local_samples
    alignment["reference_samples"] = reference_samples
    alignment["sample_errors"] = errors[:6]
    local_miners = sorted({str(item.get("miner") or "") for item in local_samples if item.get("miner")})
    reference_miners = sorted({str(item.get("miner") or "") for item in reference_samples if item.get("miner")})
    alignment["local_miners"] = local_miners
    alignment["reference_miners"] = reference_miners
    compared_heights = {
        int(item.get("height"))
        for item in local_samples
        if isinstance(item.get("height"), int)
    } & {
        int(item.get("height"))
        for item in reference_samples
        if isinstance(item.get("height"), int)
    }
    compared_count = len(compared_heights)
    alignment["compared_count"] = compared_count
    alignment["reference_sample_count"] = len(reference_samples)
    local_only_miner = local_miners[0] if len(local_miners) == 1 else ""
    local_solo_miner = bool(pool_address and local_only_miner and local_only_miner == pool_address)
    reference_has_other_miners = bool(local_only_miner and any(miner != local_only_miner for miner in reference_miners))
    alignment["local_solo_miner"] = local_solo_miner
    alignment["local_only_miner"] = local_only_miner
    alignment["reference_has_other_miners"] = reference_has_other_miners
    enough_samples = compared_count >= min(EVM_PUBLIC_ALIGNMENT_MIN_SAMPLES, max(1, len(heights))) or (
        len(local_samples) >= min(EVM_PUBLIC_ALIGNMENT_MIN_SAMPLES, max(1, len(heights)))
        and len(reference_samples) >= 1
        and reference_has_other_miners
    )
    lag_is_unsafe = int(reference_lag) >= EVM_PUBLIC_ALIGNMENT_MIN_REFERENCE_LAG
    hash_divergence_threshold = min(EVM_PUBLIC_ALIGNMENT_MIN_SAMPLES, max(1, compared_count))
    hash_divergence_suspected = bool(
        compared_count > 0
        and lag_is_unsafe
        and int(alignment["hash_mismatch_count"]) >= hash_divergence_threshold
        and (int(alignment["miner_mismatch_count"]) > 0 or int(alignment["timestamp_mismatch_count"]) > 0)
    )
    solo_mining_suspected = bool(
        enough_samples
        and lag_is_unsafe
        and local_solo_miner
        and reference_has_other_miners
    )
    alignment["hash_divergence_suspected"] = hash_divergence_suspected
    alignment["solo_mining_suspected"] = solo_mining_suspected
    alignment["public_chain_diverged"] = bool(hash_divergence_suspected or solo_mining_suspected)
    if hash_divergence_suspected:
        alignment["reason"] = (
            "same-height local EVM block identity differs from an ahead public reference; "
            "the local node must not mine until it rejoins the public chain"
        )
    elif solo_mining_suspected:
        alignment["reason"] = (
            "local EVM headers are only from the configured mining address while "
            "an ahead public reference shows other miners at the sampled heights"
        )
    elif not enough_samples:
        alignment["reason"] = "not enough comparable local/public EVM headers"
    elif not lag_is_unsafe:
        alignment["reason"] = "public EVM reference lag is below the unsafe threshold"
    elif int(alignment["hash_mismatch_count"]) >= hash_divergence_threshold:
        alignment["reason"] = (
            "local/public EVM block hashes differ but miner and timestamp samples align; "
            "hash mismatch is diagnostic for this node build while catch-up lag controls mining readiness"
        )
    elif not local_solo_miner:
        alignment["reason"] = "local sampled EVM headers are not a solo signature for the configured mining address"
    elif not reference_has_other_miners:
        alignment["reason"] = "public sampled EVM headers did not show other miners"
    return alignment


def evm_rpc_lag_snapshot(source: str, node_rpc_url: str, chain_block_count: int, timeout: float) -> dict[str, Any]:
    evm_url = node_rpc_url if rpc_url_port(node_rpc_url) == NODE_EVM_RPC_PORT else sibling_evm_rpc_url(node_rpc_url)
    snapshot: dict[str, Any] = {
        "evm_rpc_url": evm_url,
        "evm_rpc_source": "eth_blockNumber",
        "evm_block_count": None,
        "evm_gap_to_chain_count": None,
        "evm_reference_source": "",
        "evm_reference_url": "",
        "evm_reference_block_count": None,
        "evm_reference_errors": [],
        "evm_reference_external_observed": False,
        "evm_reference_external_source": "",
        "evm_reference_external_url": "",
        "evm_reference_external_block_count": None,
        "evm_lag_to_reference": None,
        "evm_lag_to_chain": None,
        "evm_rpc_error": "",
        "public_chain_alignment": {},
        "public_chain_diverged": False,
        "solo_mining_suspected": False,
        "canonical_mining_safety": {
            "schema": "stack_evm_public_reference_v1",
            "safe": False,
            "reason": "EVM RPC has not been sampled yet",
        },
    }
    try:
        evm_block = parse_rpc_quantity(json_rpc_call(evm_url, "eth_blockNumber", [], timeout=timeout))
    except Exception as exc:  # noqa: BLE001 - EVM lag is a readiness diagnostic.
        snapshot["evm_rpc_error"] = f"eth_blockNumber failed for {source}: {exc}"
        return snapshot
    snapshot["evm_block_count"] = evm_block
    snapshot["evm_gap_to_chain_count"] = max(0, int(chain_block_count) - int(evm_block))

    best_source = source
    best_url = evm_url
    best_block = evm_block
    external_source = ""
    external_url = ""
    external_block: int | None = None
    reference_errors: list[str] = []
    for ref_source, ref_url in evm_reference_rpc_urls():
        if ref_url == evm_url:
            continue
        try:
            ref_block = parse_rpc_quantity(json_rpc_call(ref_url, "eth_blockNumber", [], timeout=timeout))
        except Exception as exc:  # noqa: BLE001 - reference sources are best-effort.
            reference_errors.append(f"{ref_source}: {exc}")
            continue
        if external_block is None or ref_block > external_block:
            external_source = ref_source
            external_url = ref_url
            external_block = ref_block
        if ref_block > best_block:
            best_source = ref_source
            best_url = ref_url
            best_block = ref_block
    if external_block is not None:
        best_source = external_source
        best_url = external_url
        best_block = external_block
    snapshot["evm_reference_source"] = best_source
    snapshot["evm_reference_url"] = best_url
    snapshot["evm_reference_block_count"] = best_block
    snapshot["evm_reference_errors"] = reference_errors[:5]
    snapshot["evm_reference_external_observed"] = external_block is not None
    snapshot["evm_reference_external_source"] = external_source
    snapshot["evm_reference_external_url"] = external_url
    snapshot["evm_reference_external_block_count"] = external_block
    snapshot["evm_lag_to_reference"] = max(0, int(best_block) - int(evm_block))
    # Compatibility field for older dashboard consumers. The DAG/order gap is
    # exposed as evm_gap_to_chain_count; readiness uses EVM-to-EVM reference lag.
    snapshot["evm_lag_to_chain"] = snapshot["evm_lag_to_reference"]
    reference_lag = safe_int(snapshot.get("evm_lag_to_reference"), 0)
    alignment: dict[str, Any] = {}
    if external_url:
        if reference_lag >= EVM_PUBLIC_ALIGNMENT_MIN_REFERENCE_LAG or EVM_PUBLIC_ALIGNMENT_ALWAYS_SAMPLE:
            alignment = evm_public_chain_alignment(
                local_url=evm_url,
                reference_source=external_source,
                reference_url=external_url,
                local_block=evm_block,
                reference_block=int(external_block),
                reference_lag=reference_lag,
                mining_address=read_env_value("MINING_ADDRESS") or "",
                timeout=timeout,
            )
        else:
            alignment = {
                "enabled": True,
                "local_url": evm_url,
                "reference_source": external_source,
                "reference_url": external_url,
                "local_block": evm_block,
                "reference_block": external_block,
                "reference_lag_blocks": reference_lag,
                "min_reference_lag_blocks": EVM_PUBLIC_ALIGNMENT_MIN_REFERENCE_LAG,
                "sample_heights": [],
                "local_samples": [],
                "reference_samples": [],
                "compared_count": 0,
                "reference_sample_count": 0,
                "hash_mismatch_count": 0,
                "solo_mining_suspected": False,
                "public_chain_diverged": False,
                "reason": "public EVM reference lag is below the unsafe threshold",
            }
    elif reference_errors:
        alignment = {
            "enabled": False,
            "reason": "no independent public EVM reference was reachable",
            "sample_errors": reference_errors[:5],
        }
    else:
        alignment = {
            "enabled": False,
            "reason": "no independent public EVM reference was configured",
        }
    snapshot["public_chain_alignment"] = alignment
    snapshot["public_chain_diverged"] = bool(alignment.get("public_chain_diverged"))
    snapshot["solo_mining_suspected"] = bool(alignment.get("solo_mining_suspected"))

    compared_count = safe_int(alignment.get("compared_count"), 0)
    sample_heights = alignment.get("sample_heights") if isinstance(alignment.get("sample_heights"), list) else []
    required_samples = min(EVM_PUBLIC_ALIGNMENT_MIN_SAMPLES, max(1, len(sample_heights)))
    hash_mismatch_count = safe_int(alignment.get("hash_mismatch_count"), 0)
    reference_lag_below_unsafe_threshold = bool(
        external_url
        and alignment.get("enabled")
        and reference_lag < EVM_PUBLIC_ALIGNMENT_MIN_REFERENCE_LAG
        and not snapshot["public_chain_diverged"]
    )
    safety_safe = bool(
        external_url
        and alignment.get("enabled")
        and not snapshot["public_chain_diverged"]
        and (reference_lag_below_unsafe_threshold or compared_count >= required_samples)
    )
    if safety_safe:
        if hash_mismatch_count:
            safety_reason = (
                "local/public EVM miner and timestamp samples align; block-hash mismatch is diagnostic "
                "for this node build while catch-up lag controls mining readiness"
            )
        elif reference_lag_below_unsafe_threshold:
            safety_reason = str(alignment.get("reason") or "public EVM reference lag is below the unsafe threshold")
        else:
            safety_reason = "local EVM headers match an independent public reference at sampled heights"
    elif not external_url:
        safety_reason = str(alignment.get("reason") or "no independent public EVM reference was available")
    elif snapshot["public_chain_diverged"]:
        safety_reason = str(alignment.get("reason") or "public chain divergence detected")
    elif hash_mismatch_count:
        safety_reason = f"local/public EVM hash mismatch count={hash_mismatch_count}"
    else:
        safety_reason = str(alignment.get("reason") or "not enough same-height public EVM samples")
    snapshot["canonical_mining_safety"] = {
        "schema": "stack_evm_public_reference_v1",
        "safe": safety_safe,
        "reason": safety_reason,
        "external_reference_observed": bool(external_url),
        "reference_source": external_source,
        "reference_url": external_url,
        "local_block": evm_block,
        "reference_block": external_block,
        "reference_lag_blocks": reference_lag,
        "compared_count": compared_count,
        "required_samples": required_samples,
        "hash_mismatch_count": hash_mismatch_count,
        "public_chain_diverged": snapshot["public_chain_diverged"],
        "solo_mining_suspected": snapshot["solo_mining_suspected"],
    }
    return snapshot


def node_chain_rpc_snapshot(source: str, url: str, timeout: float = NODE_CHAIN_RPC_TIMEOUT) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "chain_rpc_source": "unavailable",
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
    port = rpc_url_port(url)
    method_order = ["getBlockCount"]
    total_attempts = 0

    for method in method_order:
        method_errors: list[str] = []
        for attempt in range(NODE_CHAIN_RPC_RETRIES):
            start = time.monotonic()
            total_attempts += 1
            try:
                value = mining_rpc_call(url, method, [], timeout=timeout)
                snapshot["chain_block_count"] = parse_rpc_quantity(value)
                snapshot["chain_rpc_source"] = method
                snapshot["chain_rpc_latency_ms"] = round((time.monotonic() - start) * 1000, 1)
                snapshot["chain_rpc_attempts"] = total_attempts
                errors.extend(method_errors)
                break
            except Exception as exc:
                snapshot["chain_rpc_latency_ms"] = round((time.monotonic() - start) * 1000, 1)
                snapshot["chain_rpc_attempts"] = total_attempts
                detail = str(exc)
                method_errors.append(detail)
                errors.append(f"{method}: {detail}")
                if attempt + 1 < NODE_CHAIN_RPC_RETRIES:
                    time.sleep(0.2)
        if snapshot["chain_block_count"] is not None:
            break
        if method == "getBlockCount" and not all(rpc_method_unavailable(error) for error in method_errors):
            break

    if snapshot["chain_block_count"] is None and port != NODE_EVM_RPC_PORT and errors and rpc_method_unavailable(errors[-1]):
        start = time.monotonic()
        total_attempts += 1
        try:
            main_height = parse_rpc_quantity(mining_rpc_call(url, "getMainChainHeight", [], timeout=timeout))
            snapshot["chain_main_height"] = main_height
            snapshot["chain_main_height_source"] = "getMainChainHeight"
            snapshot["chain_rpc_latency_ms"] = round((time.monotonic() - start) * 1000, 1)
            snapshot["chain_rpc_attempts"] = total_attempts
        except Exception as exc:
            snapshot["chain_rpc_latency_ms"] = round((time.monotonic() - start) * 1000, 1)
            snapshot["chain_rpc_attempts"] = total_attempts
            errors.append(f"getMainChainHeight: {exc}")

    if snapshot["chain_block_count"] is None:
        detail = errors[-1] if errors else "unknown error"
        primary = method_order[0] if method_order else "chain RPC"
        if len(method_order) == 1:
            snapshot["chain_rpc_error"] = f"{primary} failed for {source} after {NODE_CHAIN_RPC_RETRIES} attempt(s): {detail.split(': ', 1)[-1]}"
        else:
            snapshot["chain_rpc_error"] = f"chain RPC height methods failed for {source} after {total_attempts} attempt(s): {detail}"
        return snapshot

    if snapshot["chain_main_height"] is None:
        try:
            snapshot["chain_main_height"] = parse_rpc_quantity(
                mining_rpc_call(url, "getMainChainHeight", [], timeout=timeout)
            )
            snapshot["chain_main_height_source"] = "getMainChainHeight"
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
        evm_lag = evm_rpc_lag_snapshot(source, url, current, timeout)

        sync_current = safe_int(chain.get("sync_current_block"), None)
        sync_highest = safe_int(chain.get("sync_highest_block"), None)
        if chain.get("chain_syncing") is True:
            progress_current = sync_current if sync_current is not None else current
            remaining = max(0, sync_highest - progress_current) if sync_highest is not None else None
            percent = (
                round(max(0.0, min(100.0, (progress_current / max(1, sync_highest)) * 100)), 2)
                if sync_highest is not None
                else None
            )
            return {
                "status": "syncing",
                "percent": percent,
                "current_block": progress_current,
                "highest_block": sync_highest,
                "starting_block": None,
                "remaining_blocks": remaining,
                "source": source,
                "error": "",
                "current_block_source": chain.get("chain_rpc_source"),
                **chain,
                **evm_lag,
            }

        evm_block = safe_int(evm_lag.get("evm_block_count"), None)
        evm_reference = safe_int(evm_lag.get("evm_reference_block_count"), None)
        evm_remaining = safe_int(evm_lag.get("evm_lag_to_reference"), 0)
        if evm_block is not None and evm_reference is not None and evm_remaining > EVM_SYNC_LAG_THRESHOLD_BLOCKS:
            return {
                "status": "syncing",
                "percent": round(max(0.0, min(100.0, (evm_block / max(1, evm_reference)) * 100)), 2),
                "current_block": evm_block,
                "highest_block": evm_reference,
                "starting_block": None,
                "remaining_blocks": evm_remaining,
                "source": f"{source}:evm-head-lag",
                "error": "",
                "current_block_source": "eth_blockNumber",
                **chain,
                **evm_lag,
            }

        native = native_sync_progress(source)
        if native:
            native.update(chain)
            native.update(evm_lag)
            native["current_block"] = current
            native["highest_block"] = None
            native["current_block_source"] = chain.get("chain_rpc_source")
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
            "current_block_source": chain.get("chain_rpc_source"),
            **chain,
            **evm_lag,
        }
    except Exception as exc:
        return unknown_sync_progress(source, str(exc))


def collect_sync_progress() -> dict[str, Any]:
    endpoint = node_rpc_endpoint()
    if endpoint is None:
        return {
            **unknown_sync_progress(NODE_SERVICE, "node RPC URL unavailable"),
            "nodes": {node: unknown_sync_progress(node, "node RPC URL unavailable") for node in NODES},
        }

    source, url = endpoint
    per_node = {source: node_sync_progress(source, url)}
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
        "chain_rpc_source": ",".join(
            unique_names(
                [
                    str(item.get("chain_rpc_source") or "")
                    for item in known
                    if item.get("chain_rpc_source")
                ]
            )
        ) or "unavailable",
        "starting_block": min(starting_values) if starting_values else None,
        "remaining_blocks": max(remaining_values) if remaining_values else (0 if status == "synced" else None),
        "source": "nodes",
        "error": error,
        "nodes": per_node,
    }


def sync_progress_for_display_nodes(sync_progress: dict[str, Any], display_nodes: list[str]) -> dict[str, Any]:
    progress_nodes = sync_progress.get("nodes") if isinstance(sync_progress.get("nodes"), dict) else {}
    managed_display_nodes = [node for node in display_nodes if node in NODES]
    if len(managed_display_nodes) != 1 or len(progress_nodes) != 1:
        return sync_progress

    display_node = managed_display_nodes[0]
    source, progress = next(iter(progress_nodes.items()))
    if source == display_node or not isinstance(progress, dict):
        return sync_progress

    aligned_progress = dict(progress)
    aligned_progress["configured_source"] = source
    aligned_progress["source"] = display_node
    aligned = dict(sync_progress)
    aligned["nodes"] = {display_node: aligned_progress}
    if aligned.get("source") == source:
        aligned["configured_source"] = source
        aligned["source"] = display_node
    return aligned


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


def freshest_evm_rpc_source(
    rpc_sources: list[tuple[str, str]],
    timeout: float = 6.0,
) -> tuple[str, str, int, list[str]] | None:
    best: tuple[str, str, int] | None = None
    errors: list[str] = []
    for source_name, source_url in rpc_sources:
        try:
            latest_hex = json_rpc_call(source_url, "eth_blockNumber", [], timeout=timeout)
            latest_block = parse_rpc_quantity(latest_hex)
        except Exception as exc:  # noqa: BLE001 - each source is independent.
            errors.append(f"{source_name}: {exc}")
            continue
        if best is None or latest_block > best[2]:
            best = (source_name, source_url, latest_block)
    if best is None:
        return None
    return best[0], best[1], best[2], errors


def parse_global_block_epoch(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if text.startswith(("0x", "0X")):
        try:
            return int(text, 16)
        except ValueError:
            return None
    if re.fullmatch(r"[0-9]+", text):
        return int(text)
    parsed = parse_chain_timestamp(text)
    return int(parsed.timestamp()) if parsed else None


def chain_order_block_hash(block: Mapping[str, Any], order: int) -> str:
    for key in ("hash", "Hash", "blockHash", "BlockHash", "block_hash", "blockhash"):
        value = str(block.get(key) or "").strip()
        if value:
            return value
    raise RuntimeError(f"missing block hash for order {order}")


def fetch_chain_order_reference(url: str, order: int, timeout: float = 8.0) -> dict[str, Any]:
    result = mining_rpc_call(url, "getBlockByOrder", [order, True, False], timeout=timeout)
    if not isinstance(result, dict):
        raise RuntimeError(f"getBlockByOrder response for order {order} was not a JSON object")
    response_order = result.get("order", result.get("Order", result.get("mainOrder", result.get("MainOrder", result.get("main_order")))))
    resolved_order = safe_int(response_order, order)
    if order >= 0 and response_order is not None and resolved_order != order:
        raise RuntimeError(f"getBlockByOrder returned order {response_order!r} for requested order {order}")
    if resolved_order < 0:
        raise RuntimeError(f"getBlockByOrder returned invalid order {response_order!r} for requested order {order}")
    return {
        "order": resolved_order,
        "hash": chain_order_block_hash(result, resolved_order),
        "block": result,
    }


def fetch_chain_order_tip(url: str, timeout: float = 8.0) -> tuple[int, str]:
    errors: list[str] = []
    try:
        reference = fetch_chain_order_reference(url, -1, timeout=timeout)
        latest_order = safe_int(reference.get("order"), -1)
        if latest_order >= 0:
            return latest_order, "getBlockByOrder(-1)"
        errors.append(f"getBlockByOrder(-1) returned invalid order {reference.get('order')!r}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"getBlockByOrder(-1): {exc}")
    try:
        latest_order = parse_rpc_quantity(mining_rpc_call(url, "getBlockTotal", [], timeout=timeout))
        if latest_order >= 0:
            fetch_chain_order_reference(url, latest_order, timeout=timeout)
            return latest_order, "getBlockTotal"
        errors.append(f"getBlockTotal returned negative order tip {latest_order}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"getBlockTotal: {exc}")
    raise RuntimeError("; ".join(errors) or "unable to resolve latest chain order")


def fetch_chain_order_header(
    url: str,
    source_name: str,
    order: int,
    timeout: float = 8.0,
) -> dict[str, Any]:
    reference = fetch_chain_order_reference(url, order, timeout=timeout)
    block_hash = str(reference.get("hash") or "")
    block = reference.get("block") if isinstance(reference.get("block"), dict) else {}
    if not block_hash:
        raise RuntimeError(f"empty block hash for order {order}")
    header: dict[str, Any] = {}
    header_error = ""
    try:
        result = mining_rpc_call(url, "getBlockHeader", [block_hash, True], timeout=timeout)
        if isinstance(result, dict):
            header = result
    except Exception as exc:  # noqa: BLE001 - reward/timestamp can degrade independently.
        header_error = str(exc)

    coinbase = mining_rpc_call(url, "getCoinbaseAddress", [block_hash], timeout=timeout)
    miner = str(coinbase or "").strip().lower()
    if not valid_eth_address(miner):
        raise RuntimeError(f"invalid coinbase address for order {order}: {coinbase!r}")

    epoch = parse_global_block_epoch(header.get("time"))
    if epoch is None:
        epoch = parse_global_block_epoch(block.get("timestamp", block.get("Timestamp", block.get("time", block.get("Time")))))
        if header.get("reward") is None:
            for key in ("reward", "Reward"):
                if key in block:
                    header["reward"] = block.get(key)
                    break
    if epoch is None:
        raise RuntimeError(f"missing block timestamp for order {order}")

    reward_bdag = atomic_to_bdag(header.get("reward")) if header.get("reward") is not None else None
    return {
        "order": order,
        "hash": block_hash,
        "miner": miner,
        "timestamp_epoch": epoch,
        "reward_atoms": header.get("reward"),
        "reward_bdag": reward_bdag,
        "header_error": header_error,
        "_rpc_source": source_name,
    }


def probe_global_chain_block_count() -> tuple[int | None, str, str, list[str]]:
    errors: list[str] = []
    candidates: list[tuple[int, str, str]] = []
    for source_name, source_url in global_chain_rpc_urls() or [("local-chain", f"http://127.0.0.1:{NODE_MINING_RPC_PORT}")]:
        try:
            block_count = parse_rpc_quantity(
                mining_rpc_call(source_url, "getBlockCount", [], timeout=chain_rpc_timeout_for_source(source_name, 8.0))
            )
            if block_count > 0:
                candidates.append((block_count, source_name, source_url))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{source_name}: {exc}")
    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates:
        return None, "", "", errors
    block_count, source_name, source_url = candidates[0]
    return block_count, source_name, source_url, errors


def latest_pool_db_block_height() -> tuple[int | None, str, dict[str, Any], list[str]]:
    try:
        summary = pool_db_json(
            """
            SELECT json_build_object(
              'latest_height', max(height),
              'last_block_at', max(created_at)::text,
              'last_block_epoch', floor(extract(epoch FROM max(created_at)))::bigint,
              'block_count', count(*)
            )
            FROM blocks;
            """
        ) or {}
    except Exception as exc:  # noqa: BLE001 - pool DB may be unavailable during startup.
        return None, "postgres", {}, [f"postgres: {exc}"]
    if not isinstance(summary, dict):
        return None, "postgres", {}, ["postgres: latest block query returned non-object payload"]
    height = safe_int(summary.get("latest_height"), None)
    if height is None or height <= 0:
        return None, "postgres", summary, ["postgres: no block height recorded"]
    epoch = safe_int(summary.get("last_block_epoch"), None)
    age = max(0, seconds_since_epoch() - epoch) if epoch is not None else None
    summary["last_block_age_seconds"] = age
    if (
        GLOBAL_POOL_HEIGHT_MAX_AGE_SECONDS > 0
        and age is not None
        and age > GLOBAL_POOL_HEIGHT_MAX_AGE_SECONDS
    ):
        return None, "postgres", summary, [
            f"postgres: latest block height {height} is stale ({age}s old)"
        ]
    return height, "postgres", summary, []


def mining_template_params() -> list[Any]:
    pool_address = read_env_value("MINING_ADDRESS") or ""
    params: list[Any] = [[], 10]
    if valid_eth_address(pool_address):
        params.append(pool_address)
    return params


def probe_global_display_block_height() -> tuple[int | None, str, dict[str, Any], list[str]]:
    errors: list[str] = []
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    pool_height, pool_source, pool_meta, pool_errors = latest_pool_db_block_height()
    errors.extend(pool_errors)
    if pool_height is not None:
        candidates.append((pool_height, pool_source, pool_meta))

    rpc_sources = global_chain_rpc_urls() or [("local-chain", f"http://127.0.0.1:{NODE_MINING_RPC_PORT}")]
    for source_name, source_url in rpc_sources:
        try:
            timeout = chain_rpc_timeout_for_source(
                source_name,
                min(5.0, max(1.0, NODE_CHAIN_RPC_TIMEOUT)),
            )
            template = mining_rpc_call(
                source_url,
                "getBlockTemplate",
                mining_template_params(),
                timeout=timeout,
            )
            if not isinstance(template, dict):
                raise RuntimeError(f"getBlockTemplate returned {template!r}")
            height = safe_int(template.get("height"), None)
            if height is None or height <= 0:
                raise RuntimeError(f"getBlockTemplate returned invalid height {template.get('height')!r}")
            candidates.append((
                height,
                f"{source_name}:getBlockTemplate",
                {
                    "rpc_url": source_url,
                    "template_height": height,
                    "template_blues": template.get("blues"),
                    "template_curtime": template.get("curtime"),
                },
            ))
        except Exception as exc:  # noqa: BLE001 - fall through to other height sources.
            errors.append(f"{source_name}: getBlockTemplate: {exc}")

    for source_name, source_url in rpc_sources:
        try:
            height = parse_rpc_quantity(
                mining_rpc_call(source_url, "getMainChainHeight", [], timeout=chain_rpc_timeout_for_source(source_name, 4.0))
            )
            if height > 0:
                candidates.append((
                    height,
                    f"{source_name}:getMainChainHeight",
                    {"rpc_url": source_url, "main_chain_height": height},
                ))
        except Exception as exc:  # noqa: BLE001 - diagnostic only.
            errors.append(f"{source_name}: getMainChainHeight: {exc}")

    if not candidates:
        return None, "", {}, errors
    height, source, metadata = max(candidates, key=lambda item: item[0])
    return height, source, metadata, errors[:20]


def dashboard_history_iso_from_epoch(epoch: int | float) -> str:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")


def dashboard_history_rebuild_sample_epochs(
    latest_epoch: int,
    max_age_seconds: int | None = None,
) -> list[int]:
    max_age = max_age_seconds if max_age_seconds is not None else dashboard_history_tiers()[-1].max_age_seconds
    epochs: set[int] = set()
    for tier in dashboard_history_tiers():
        if tier.min_age_seconds >= max_age:
            continue
        tier_max_age = min(max_age, tier.max_age_seconds)
        first_age = 0 if tier.min_age_seconds == 0 else tier.min_age_seconds + tier.step_seconds
        age = first_age
        while age <= tier_max_age:
            epochs.add(int(latest_epoch - age))
            age += tier.step_seconds
    return sorted(epoch for epoch in epochs if epoch <= latest_epoch)


def estimate_chain_seconds_per_order(
    rpc_url: str,
    rpc_name: str,
    latest_order: int,
    latest_epoch: int,
    lookback_orders: int = DASHBOARD_HISTORY_REBUILD_LOOKBACK_ORDERS,
    timeout: float = DASHBOARD_HISTORY_REBUILD_RPC_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    baseline_order = max(0, latest_order - max(1, lookback_orders))
    baseline_header = fetch_chain_order_header(rpc_url, rpc_name, baseline_order, timeout=timeout)
    baseline_epoch = safe_int(baseline_header.get("timestamp_epoch"), latest_epoch)
    order_delta = max(1, latest_order - baseline_order)
    time_delta = max(1, latest_epoch - baseline_epoch)
    return {
        "baseline_order": baseline_order,
        "baseline_epoch": baseline_epoch,
        "seconds_per_order": max(0.001, time_delta / order_delta),
        "baseline_header": baseline_header,
    }


def estimate_chain_order_for_epoch(
    target_epoch: int,
    latest_order: int,
    latest_epoch: int,
    seconds_per_order: float,
) -> int:
    age_seconds = max(0, latest_epoch - target_epoch)
    estimated = latest_order - int(round(age_seconds / max(0.001, seconds_per_order)))
    return max(0, min(latest_order, estimated))


def fetch_chain_order_headers_for_history(
    rpc_url: str,
    rpc_name: str,
    orders: list[int],
    workers: int = DASHBOARD_HISTORY_REBUILD_RPC_WORKERS,
    timeout: float = DASHBOARD_HISTORY_REBUILD_RPC_TIMEOUT_SECONDS,
    progress=None,
) -> tuple[dict[int, dict[str, Any]], list[str]]:
    unique_orders = sorted({order for order in orders if order >= 0})
    headers: dict[int, dict[str, Any]] = {}
    if not unique_orders:
        return headers, []

    worker_count = max(1, min(workers, len(unique_orders)))
    batch_size = max(worker_count * 64, 512)
    completed = 0
    total = len(unique_orders)
    remaining = unique_orders
    final_errors: dict[int, str] = {}
    for attempt in range(4):
        failed: dict[int, str] = {}
        attempt_workers = max(1, min(worker_count, len(remaining)))
        if attempt:
            attempt_workers = max(1, min(attempt_workers, max(1, worker_count // 2)))
            time.sleep(min(2.0 * attempt, 6.0))
        for offset in range(0, len(remaining), batch_size):
            batch = remaining[offset : offset + batch_size]
            with ThreadPoolExecutor(max_workers=attempt_workers) as pool:
                future_map = {
                    pool.submit(fetch_chain_order_header, rpc_url, rpc_name, order, timeout): order
                    for order in batch
                }
                for future in as_completed(future_map):
                    order = future_map[future]
                    try:
                        headers[order] = future.result()
                    except Exception as exc:  # noqa: BLE001 - caller reports partial rebuild details.
                        failed[order] = str(exc)
                    if not attempt:
                        completed += 1
            if progress is not None and not attempt:
                progress(completed, total, len(failed))
        if not failed:
            final_errors = {}
            break
        remaining = sorted(failed)
        final_errors = failed
    return headers, [f"{order}: {error}" for order, error in sorted(final_errors.items())]


def global_history_snapshot_from_chain_headers(
    headers: list[dict[str, Any]],
    sample_epoch: int,
    sample_order: int,
    rpc_name: str,
    price: dict[str, Any],
    requested_blocks: int,
    fetch_errors: list[str] | None = None,
) -> dict[str, Any]:
    ordered_headers = sorted(
        [header for header in headers if isinstance(header, dict)],
        key=lambda item: safe_int(item.get("order"), 0),
    )
    total_blocks = len(ordered_headers)
    unknown_blocks = max(0, requested_blocks - total_blocks)
    reward_values = [item["reward_bdag"] for item in ordered_headers if isinstance(item.get("reward_bdag"), Decimal)]
    known_reward_count = len(reward_values)
    known_reward_total = sum(reward_values, Decimal("0"))
    avg_reward_bdag = known_reward_total / Decimal(known_reward_count) if known_reward_count else None
    missing_reward_blocks = max(0, total_blocks - known_reward_count)
    total_reward_estimate = (
        known_reward_total + (avg_reward_bdag * Decimal(missing_reward_blocks))
        if avg_reward_bdag is not None
        else None
    )

    cluster_map: dict[str, dict[str, Any]] = {}
    first_seen_epoch: int | None = None
    last_seen_epoch: int | None = None
    zero_address_blocks = 0
    zero_address_reward_bdag = Decimal("0")
    for header in ordered_headers:
        miner = str(header.get("miner") or "").lower()
        if not miner:
            continue
        epoch = safe_int(header.get("timestamp_epoch"), 0)
        first_seen_epoch = epoch if first_seen_epoch is None else min(first_seen_epoch, epoch)
        last_seen_epoch = epoch if last_seen_epoch is None else max(last_seen_epoch, epoch)
        reward_bdag = header.get("reward_bdag")
        if miner == ZERO_ETH_ADDRESS:
            zero_address_blocks += 1
            if isinstance(reward_bdag, Decimal):
                zero_address_reward_bdag += reward_bdag
            continue
        order = safe_int(header.get("order"), 0)
        entry = cluster_map.setdefault(
            miner,
            {
                "address": miner,
                "blocks": 0,
                "reward_bdag": Decimal("0"),
                "reward_count": 0,
                "first_height": order,
                "last_height": order,
                "first_seen_epoch": epoch,
                "last_seen_epoch": epoch,
                "rpc_sources": [],
                "header_errors": [],
            },
        )
        entry["blocks"] += 1
        if isinstance(reward_bdag, Decimal):
            entry["reward_bdag"] += reward_bdag
            entry["reward_count"] += 1
        entry["first_height"] = min(entry["first_height"], order)
        entry["last_height"] = max(entry["last_height"], order)
        entry["first_seen_epoch"] = min(entry["first_seen_epoch"], epoch)
        entry["last_seen_epoch"] = max(entry["last_seen_epoch"], epoch)
        entry["rpc_sources"].append(str(header.get("_rpc_source") or rpc_name))
        if header.get("header_error"):
            entry["header_errors"].append(str(header["header_error"]))

    window_seconds = max(1, (last_seen_epoch or sample_epoch) - (first_seen_epoch or sample_epoch))
    scan_window_hours = Decimal(str(window_seconds)) / Decimal("3600")
    avg_block_seconds = window_seconds / max(1, total_blocks - 1) if total_blocks > 1 else None
    share_denominator = max(1, requested_blocks)
    enriched_clusters: list[dict[str, Any]] = []
    for rank, cluster in enumerate(
        sorted(cluster_map.values(), key=lambda item: (item["blocks"], item["last_seen_epoch"]), reverse=True),
        start=1,
    ):
        blocks = int(cluster["blocks"])
        known_bdag = cluster["reward_bdag"]
        missing_cluster_rewards = max(0, blocks - int(cluster.get("reward_count", 0) or 0))
        est_bdag = known_bdag + (avg_reward_bdag * Decimal(missing_cluster_rewards)) if avg_reward_bdag is not None else None
        est_bdag_hour, est_usd_hour, est_zar_hour = _pool_earning_rates_from_cluster(
            {
                "estimated_bdag": decimal_to_str(est_bdag, places=2) if est_bdag is not None else None,
                "estimated_usd": fiat_value(est_bdag, price, "usd") if est_bdag is not None else None,
                "estimated_zar": fiat_value(est_bdag, price, "zar") if est_bdag is not None else None,
            },
            scan_window_hours,
        )
        share = Decimal(blocks) / Decimal(share_denominator)
        enriched_clusters.append(
            {
                "rank": rank,
                "address": cluster["address"],
                "address_short": short_eth_address(cluster["address"]),
                "pool_name": "",
                "source": "chain-rpc-history-rebuild",
                "local_pool": False,
                "blocks": blocks,
                "shares": blocks,
                "credit_blocks": blocks,
                "found_blocks": blocks,
                "share_percent": decimal_to_str(share * Decimal("100"), places=2),
                "credited_bdag": decimal_to_str(known_bdag, places=2),
                "known_reward_bdag": decimal_to_str(known_bdag, places=2),
                "reward_missing_blocks": missing_cluster_rewards,
                "reward_estimated": missing_cluster_rewards > 0,
                "estimated_bdag": decimal_to_str(est_bdag, places=2) if est_bdag is not None else None,
                "estimated_usd": fiat_value(est_bdag, price, "usd") if est_bdag is not None else None,
                "estimated_zar": fiat_value(est_bdag, price, "zar") if est_bdag is not None else None,
                "estimated_wallet_bdag": decimal_to_str(est_bdag, places=2) if est_bdag is not None else None,
                "estimated_bdag_avg_hour": est_bdag_hour,
                "estimated_usd_avg_hour": est_usd_hour,
                "estimated_zar_avg_hour": est_zar_hour,
                "estimated_bdag_recent_hour": est_bdag_hour,
                "estimated_usd_recent_hour": est_usd_hour,
                "estimated_zar_recent_hour": est_zar_hour,
                "first_seen_at": datetime.fromtimestamp(cluster["first_seen_epoch"], tz=timezone.utc).isoformat(),
                "last_seen_at": datetime.fromtimestamp(cluster["last_seen_epoch"], tz=timezone.utc).isoformat(),
                "avg_block_seconds": decimal_to_str(Decimal(str(avg_block_seconds)), places=1)
                if avg_block_seconds is not None and blocks > 1
                else None,
                "location": "Unknown",
                "location_confidence": "not-collected-during-history-rebuild",
                "rpc_sources": unique_names(cluster["rpc_sources"]),
                "header_errors": unique_names(cluster["header_errors"])[:3],
            }
        )

    sample_iso = dashboard_history_iso_from_epoch(sample_epoch)
    payload = {
        "status": "ok" if unknown_blocks == 0 and not fetch_errors else "degraded",
        "source": "on-chain-rpc-rebuild",
        "source_truth": GLOBAL_STATS_SOURCE_TRUTH,
        "source_contract": DASHBOARD_CHAIN_HISTORY_SOURCE_CONTRACT,
        "schema_version": GLOBAL_CACHE_SCHEMA_VERSION,
        "rpc_kind": "blockdag-chain-rpc",
        "height_method": "getBlockByOrder-reconstructed",
        "generated_at": sample_iso,
        "updated_at": sample_iso,
        "updated_at_epoch": int(sample_epoch),
        "rpc_source": rpc_name,
        "chain_block_count": sample_order + 1,
        "latest_block": sample_order + 1,
        "latest_order": sample_order,
        "latest_order_method": "estimated-from-block-timestamps",
        "requested_blocks": requested_blocks,
        "fetched_blocks": total_blocks,
        "unknown_blocks": unknown_blocks,
        "partial_scan": unknown_blocks > 0,
        "head_only": False,
        "maintenance_deferred": False,
        "scan_start_block": safe_int(ordered_headers[0].get("order"), sample_order) if ordered_headers else sample_order,
        "scan_end_block": safe_int(ordered_headers[-1].get("order"), sample_order) if ordered_headers else sample_order,
        "scan_start_order": safe_int(ordered_headers[0].get("order"), sample_order) if ordered_headers else sample_order,
        "scan_end_order": safe_int(ordered_headers[-1].get("order"), sample_order) if ordered_headers else sample_order,
        "scan_window_seconds": window_seconds,
        "scan_window_hours": decimal_to_str(scan_window_hours, places=2),
        "avg_block_seconds": decimal_to_str(Decimal(str(avg_block_seconds)), places=1) if avg_block_seconds is not None else None,
        "avg_reward_bdag": decimal_to_str(avg_reward_bdag, places=2) if avg_reward_bdag is not None else None,
        "estimated_total_reward_bdag": decimal_to_str(total_reward_estimate, places=2) if total_reward_estimate is not None else None,
        "estimated_total_reward_usd": fiat_value(total_reward_estimate, price, "usd") if total_reward_estimate is not None else None,
        "estimated_total_reward_zar": fiat_value(total_reward_estimate, price, "zar") if total_reward_estimate is not None else None,
        "unique_miners": len(enriched_clusters),
        "chain_unique_miners": len(enriched_clusters),
        "clusters": enriched_clusters,
        "chain_clusters": enriched_clusters,
        "local_pool_clusters": [],
        "peer_location": {"observations": []},
        "fetch_errors": (fetch_errors or [])[:20],
        "zero_address_blocks": zero_address_blocks,
        "attributed_blocks": max(0, requested_blocks - zero_address_blocks - unknown_blocks),
        "unattributed_reward_bdag": decimal_to_str(zero_address_reward_bdag, places=2),
        "reward_source": "getBlockHeader.reward atomic units",
        "reward_known_blocks": known_reward_count,
        "reward_missing_blocks": missing_reward_blocks,
        "history_rebuild": True,
    }
    return annotate_global_pool_labels(payload)


def payment_wallet_earnings_snapshot_from_chain_headers(
    headers: list[dict[str, Any]],
    sample_epoch: int,
    wallet_address: str | None,
    price: dict[str, Any],
    requested_blocks: int,
) -> dict[str, Any] | None:
    if not is_spendable_eth_address(wallet_address):
        return None
    wallet = str(wallet_address).strip().lower()
    ordered_headers = sorted(
        [header for header in headers if isinstance(header, dict)],
        key=lambda item: safe_int(item.get("order"), 0),
    )
    first_epoch = min((safe_int(header.get("timestamp_epoch"), sample_epoch) for header in ordered_headers), default=sample_epoch)
    last_epoch = max((safe_int(header.get("timestamp_epoch"), sample_epoch) for header in ordered_headers), default=sample_epoch)
    window_seconds = max(1, last_epoch - first_epoch)
    window_hours = Decimal(str(window_seconds)) / Decimal("3600")
    wallet_headers = [header for header in ordered_headers if str(header.get("miner") or "").lower() == wallet]
    reward_bdag = sum(
        (header.get("reward_bdag") for header in wallet_headers if isinstance(header.get("reward_bdag"), Decimal)),
        Decimal("0"),
    )
    reward_per_hour = reward_bdag / window_hours if window_hours > 0 else Decimal("0")
    wallet_blocks = len(wallet_headers)
    work_percent = Decimal(wallet_blocks) / Decimal(max(1, requested_blocks)) * Decimal("100")
    sample_iso = dashboard_history_iso_from_epoch(sample_epoch)
    miner = {
        "identity_key": f"wallet:{wallet}",
        "display_label": f"Payment wallet {short_eth_address(wallet)}",
        "device_type": "payment-wallet",
        "workers": [wallet],
        "credit_workers": [wallet],
        "credit_scope": "chain-rpc-rebuild",
        "earnings_scope": "payment-wallet-chain-rewards",
        "history_source": "chain-rpc-history-rebuild",
        "shares": wallet_blocks,
        "share_work": wallet_blocks,
        "work_percent": decimal_to_str(work_percent, places=2),
        "blocks_found": wallet_blocks,
        "hashrate_available": False,
        "hashrate_source": "not-reconstructable-from-chain-rpc",
        "estimated_bdag_avg_hour": decimal_to_str(reward_per_hour),
        "estimated_bdag_1h": decimal_to_str(reward_per_hour),
        "estimated_usd_avg_hour": fiat_value(reward_per_hour, price, "usd"),
        "estimated_usd_1h": fiat_value(reward_per_hour, price, "usd"),
        "estimated_zar_avg_hour": fiat_value(reward_per_hour, price, "zar"),
        "estimated_zar_1h": fiat_value(reward_per_hour, price, "zar"),
        "estimated_wallet_bdag_recent_hour": decimal_to_str(reward_per_hour),
        "estimated_wallet_bdag_avg_hour": decimal_to_str(reward_per_hour),
        "estimated_wallet_bdag_1h": decimal_to_str(reward_per_hour),
        "estimated_wallet_usd_recent_hour": fiat_value(reward_per_hour, price, "usd"),
        "estimated_wallet_usd_avg_hour": fiat_value(reward_per_hour, price, "usd"),
        "estimated_wallet_usd_1h": fiat_value(reward_per_hour, price, "usd"),
        "estimated_wallet_zar_recent_hour": fiat_value(reward_per_hour, price, "zar"),
        "estimated_wallet_zar_avg_hour": fiat_value(reward_per_hour, price, "zar"),
        "estimated_wallet_zar_1h": fiat_value(reward_per_hour, price, "zar"),
    }
    return {
        "generated_at": sample_iso,
        "total_bdag": None,
        "credit_balance_check": {
            "wallet_bdag": None,
            "source_truth": "chain-rpc coinbase rewards only; not historical wallet balance",
            "payment_wallet_address": wallet,
        },
        "miner_estimates": [miner],
        "history_source": "chain-rpc-history-rebuild",
        "bucket_seconds": window_seconds,
        "requested_blocks": requested_blocks,
        "fetched_blocks": len(ordered_headers),
    }


def payment_wallet_rate_bdag_hour(snapshot: dict[str, Any]) -> Decimal | None:
    for miner in snapshot.get("miner_estimates") or []:
        if not isinstance(miner, dict):
            continue
        if str(miner.get("earnings_scope") or "") != "payment-wallet-chain-rewards":
            continue
        rate = decimal_value(
            miner.get("estimated_wallet_bdag_avg_hour")
            or miner.get("estimated_wallet_bdag_recent_hour")
            or miner.get("estimated_bdag_avg_hour")
        )
        if rate is not None and rate >= 0:
            return rate
    return None


def annotate_rebuilt_wallet_24h_earnings(
    rows: list[dict[str, Any]],
    price: dict[str, Any],
    min_coverage_hours: Decimal = Decimal("23"),
) -> dict[str, Any]:
    points: list[tuple[float, dict[str, Any], Decimal]] = []
    for row in rows:
        epoch = history_snapshot_epoch(row)
        rate = payment_wallet_rate_bdag_hour(row)
        if epoch is None or rate is None:
            continue
        points.append((epoch, row, rate))
    points.sort(key=lambda item: item[0])
    epochs = [item[0] for item in points]
    annotated = 0
    insufficient = 0
    for index, (epoch, row, _rate) in enumerate(points):
        cutoff = epoch - 86400
        start_index = bisect.bisect_right(epochs, cutoff, 0, index + 1) - 1
        integration: list[tuple[float, Decimal]] = []
        if start_index >= 0:
            integration.append((cutoff, points[start_index][2]))
            first_after = start_index + 1
        else:
            first_after = 0
        for point_epoch, _point_row, point_rate in points[first_after:index + 1]:
            if point_epoch >= cutoff:
                integration.append((point_epoch, point_rate))
        if len(integration) < 2:
            insufficient += 1
            continue
        earned = Decimal("0")
        for left, right in zip(integration, integration[1:]):
            delta_seconds = Decimal(str(max(0.0, right[0] - left[0])))
            earned += right[1] * (delta_seconds / Decimal("3600"))
        coverage_hours = Decimal(str(max(0.0, integration[-1][0] - integration[0][0]))) / Decimal("3600")
        if coverage_hours < min_coverage_hours:
            insufficient += 1
            continue
        row["earnings_24h"] = {
            "status": "ok",
            "source": "chain-rpc-history-rebuild-rolling-rate",
            "source_truth": "payment wallet coinbase rewards integrated from rebuilt local chain RPC samples",
            "fallback_used": False,
            "bdag": decimal_to_str(earned),
            "usd": fiat_value(earned, price, "usd"),
            "zar": fiat_value(earned, price, "zar"),
            "sample_count": len(integration),
            "coverage_hours": decimal_to_str(coverage_hours, places=2),
        }
        hourly = row.get("hourly_averages") if isinstance(row.get("hourly_averages"), dict) else {}
        hourly = dict(hourly)
        hourly["wallet_24h_bdag"] = decimal_to_str(earned)
        hourly["wallet_24h_avg_bdag_hour"] = decimal_to_str(earned / Decimal("24"))
        hourly["wallet_24h_source"] = "chain-rpc-history-rebuild-rolling-rate"
        row["hourly_averages"] = hourly
        annotated += 1
    return {
        "annotated_rows": annotated,
        "insufficient_rows": insufficient,
        "source": "chain-rpc-history-rebuild-rolling-rate",
    }


def local_asic_miners_from_earnings_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    miners: list[dict[str, Any]] = []
    for miner in snapshot.get("miner_estimates") or []:
        if not isinstance(miner, dict) or not is_earnings_wallet_miner(miner):
            continue
        if not is_local_asic_earnings_miner(miner):
            continue
        compacted = compact_miner_estimate_for_history(miner)
        mac = normalize_mac(compacted.get("mac"))
        if not mac:
            continue
        compacted["mac"] = mac
        compacted["device_id"] = compacted.get("device_id") or f"mac:{mac}"
        compacted["identity_key"] = compacted.get("identity_key") or f"mac:{mac}"
        miners.append(compacted)
    return miners


def miner_history_merge_key(miner: dict[str, Any]) -> str:
    mac = normalize_mac(miner.get("mac"))
    if mac:
        return f"mac:{mac}"
    identity = str(miner.get("identity_key") or miner.get("device_id") or "").strip().lower()
    if identity.startswith("mac:") or identity.startswith("wallet:"):
        return identity
    return identity


def nearest_rebuild_epoch(epoch: float, rebuild_epochs: list[float], latest_epoch: float) -> float | None:
    if not rebuild_epochs:
        return None
    index = bisect.bisect_left(rebuild_epochs, epoch)
    candidates = []
    if index < len(rebuild_epochs):
        candidates.append(rebuild_epochs[index])
    if index > 0:
        candidates.append(rebuild_epochs[index - 1])
    if not candidates:
        return None
    target = min(candidates, key=lambda item: abs(item - epoch))
    age = max(0.0, latest_epoch - epoch)
    max_distance = max(60, dashboard_history_bucket_seconds_for_age(age))
    return target if abs(target - epoch) <= max_distance else None


def merge_rebuilt_earnings_with_preserved_asic_history(
    rebuilt_rows: list[dict[str, Any]],
    existing_rows: list[dict[str, Any]],
    latest_epoch: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not rebuilt_rows or not existing_rows:
        return rebuilt_rows, {
            "enabled": DASHBOARD_HISTORY_REBUILD_PRESERVE_ASIC_HISTORY,
            "preserved_rows": 0,
            "preserved_miners": 0,
            "preserved_macs": [],
        }
    rebuild_epochs = [epoch for epoch in (history_snapshot_epoch(row) for row in rebuilt_rows) if epoch is not None]
    if not rebuild_epochs:
        return rebuilt_rows, {
            "enabled": DASHBOARD_HISTORY_REBUILD_PRESERVE_ASIC_HISTORY,
            "preserved_rows": 0,
            "preserved_miners": 0,
            "preserved_macs": [],
        }
    rebuild_epochs = sorted(set(rebuild_epochs))
    latest = latest_epoch if latest_epoch is not None else max(rebuild_epochs)
    cutoff = latest - max(3600, EARNINGS_DASHBOARD_HISTORY_SECONDS)
    by_epoch = {history_snapshot_epoch(row): dict(row) for row in rebuilt_rows if history_snapshot_epoch(row) is not None}
    preserved_rows = 0
    preserved_miners = 0
    preserved_macs: set[str] = set()
    for snapshot in existing_rows:
        if not isinstance(snapshot, dict):
            continue
        epoch = history_snapshot_epoch(snapshot)
        if epoch is None or epoch < cutoff or epoch > latest + 3600:
            continue
        miners = local_asic_miners_from_earnings_snapshot(snapshot)
        if not miners:
            continue
        target_epoch = nearest_rebuild_epoch(epoch, rebuild_epochs, latest)
        if target_epoch is None:
            continue
        target = by_epoch.get(target_epoch)
        if target is None:
            continue
        merged_by_key: dict[str, dict[str, Any]] = {
            miner_history_merge_key(miner): dict(miner)
            for miner in target.get("miner_estimates") or []
            if isinstance(miner, dict)
        }
        for miner in miners:
            key = miner_history_merge_key(miner)
            if not key:
                continue
            merged_by_key[key] = miner
            mac = normalize_mac(miner.get("mac"))
            if mac:
                preserved_macs.add(mac)
            preserved_miners += 1
        target["miner_estimates"] = list(merged_by_key.values())
        target["preserved_asic_history"] = {
            "source": "upgrade-preserved-local-asic-history",
            "identity": "mac",
            "merged_at": now_iso(),
        }
        by_epoch[target_epoch] = target
        preserved_rows += 1
    return (
        [by_epoch[epoch] for epoch in sorted(by_epoch)],
        {
            "enabled": DASHBOARD_HISTORY_REBUILD_PRESERVE_ASIC_HISTORY,
            "preserved_rows": preserved_rows,
            "preserved_miners": preserved_miners,
            "preserved_mac_count": len(preserved_macs),
            "preserved_macs": sorted(preserved_macs),
        },
    )


def backup_dashboard_plot_history(target_dir: Path | None = None) -> dict[str, str]:
    ensure_runtime()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = target_dir or RUNTIME_DIR / "dashboard-history-rebuild-backups" / stamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    copied: dict[str, str] = {"backup_dir": str(backup_dir)}
    for label, path in (
        ("global_history", GLOBAL_HISTORY_FILE),
        ("earnings_snapshots", EARNINGS_SNAPSHOT_FILE),
    ):
        if path.exists():
            destination = backup_dir / path.name
            shutil.copy2(path, destination)
            copied[label] = str(destination)
    disk_history_dir = DASHBOARD_HISTORY_DISK_DIR
    if disk_history_dir.exists():
        destination = backup_dir / "dashboard-history"
        shutil.copytree(disk_history_dir, destination, dirs_exist_ok=True)
        copied["dashboard_history_disk"] = str(destination)
    ram_dir = dashboard_history_ram_dir()
    if ram_dir.exists():
        destination = backup_dir / "dashboard-history-ram"
        shutil.copytree(ram_dir, destination, dirs_exist_ok=True)
        copied["dashboard_history_ram"] = str(destination)
    return copied


def rebuild_dashboard_plot_history_from_chain(
    hours: int = 720,
    window_blocks: int = DASHBOARD_HISTORY_REBUILD_BLOCK_WINDOW,
    workers: int = DASHBOARD_HISTORY_REBUILD_RPC_WORKERS,
    install: bool = False,
    progress=None,
) -> dict[str, Any]:
    ensure_runtime()
    max_age_seconds = max(3600, int(hours) * 3600)
    block_count, rpc_name, rpc_url, probe_errors = probe_global_chain_block_count()
    if block_count is None or not rpc_url:
        raise RuntimeError("unable to find a local chain RPC with getBlockCount")
    latest_order, latest_order_method = fetch_chain_order_tip(rpc_url, timeout=DASHBOARD_HISTORY_REBUILD_RPC_TIMEOUT_SECONDS)
    latest_header = fetch_chain_order_header(rpc_url, rpc_name, latest_order, timeout=DASHBOARD_HISTORY_REBUILD_RPC_TIMEOUT_SECONDS)
    latest_epoch = safe_int(latest_header.get("timestamp_epoch"), seconds_since_epoch())
    rate = estimate_chain_seconds_per_order(
        rpc_url,
        rpc_name,
        latest_order,
        latest_epoch,
        lookback_orders=min(DASHBOARD_HISTORY_REBUILD_LOOKBACK_ORDERS, max(1, latest_order)),
        timeout=DASHBOARD_HISTORY_REBUILD_RPC_TIMEOUT_SECONDS,
    )
    genesis_header = fetch_chain_order_header(rpc_url, rpc_name, 0, timeout=DASHBOARD_HISTORY_REBUILD_RPC_TIMEOUT_SECONDS)
    genesis_epoch = safe_int(genesis_header.get("timestamp_epoch"), latest_epoch)
    sample_epochs = [
        epoch for epoch in dashboard_history_rebuild_sample_epochs(latest_epoch, max_age_seconds=max_age_seconds)
        if epoch >= genesis_epoch
    ]
    if latest_epoch not in sample_epochs:
        sample_epochs.append(latest_epoch)
        sample_epochs.sort()

    seconds_per_order = float(rate["seconds_per_order"])
    sample_orders = {
        epoch: estimate_chain_order_for_epoch(epoch, latest_order, latest_epoch, seconds_per_order)
        for epoch in sample_epochs
    }
    sample_windows: dict[int, list[int]] = {}
    all_orders: list[int] = []
    for epoch, order in sample_orders.items():
        start = max(0, order - max(1, int(window_blocks)) + 1)
        orders = list(range(start, order + 1))
        sample_windows[epoch] = orders
        all_orders.extend(orders)

    header_map, fetch_errors = fetch_chain_order_headers_for_history(
        rpc_url,
        rpc_name,
        all_orders,
        workers=max(1, int(workers)),
        timeout=DASHBOARD_HISTORY_REBUILD_RPC_TIMEOUT_SECONDS,
        progress=progress,
    )
    price = fetch_cmc_price()
    wallet = read_env_value("MINING_ADDRESS")

    global_rows: list[dict[str, Any]] = []
    earnings_rows: list[dict[str, Any]] = []
    partial_samples = 0
    for epoch in sample_epochs:
        orders = sample_windows[epoch]
        headers = [header_map[order] for order in orders if order in header_map]
        missing = [order for order in orders if order not in header_map]
        sample_errors = [f"{order}: missing from rebuild fetch" for order in missing[:20]]
        if missing:
            partial_samples += 1
        global_rows.append(
            global_history_snapshot_from_chain_headers(
                headers,
                sample_epoch=epoch,
                sample_order=sample_orders[epoch],
                rpc_name=rpc_name,
                price=price,
                requested_blocks=len(orders),
                fetch_errors=sample_errors,
            )
        )
        earnings = payment_wallet_earnings_snapshot_from_chain_headers(
            headers,
            sample_epoch=epoch,
            wallet_address=wallet,
            price=price,
            requested_blocks=len(orders),
        )
        if earnings is not None:
            earnings_rows.append(earnings)
    wallet_24h_rebuild = annotate_rebuilt_wallet_24h_earnings(earnings_rows, price)

    backups: dict[str, str] = {}
    tier_counts: dict[str, Any] = {}
    preserved_asic_history: dict[str, Any] = {
        "enabled": bool(DASHBOARD_HISTORY_REBUILD_PRESERVE_ASIC_HISTORY),
        "preserved_rows": 0,
        "preserved_miners": 0,
        "preserved_macs": [],
    }
    if install:
        existing_earnings_rows: list[dict[str, Any]] = []
        if DASHBOARD_HISTORY_REBUILD_PRESERVE_ASIC_HISTORY:
            tier_rows, _any_file, _hot_file_exists = load_dashboard_history_tiers("earnings")
            existing_earnings_rows = read_jsonl_file(EARNINGS_SNAPSHOT_FILE) + tier_rows
        backups = backup_dashboard_plot_history()
        if existing_earnings_rows:
            earnings_rows, preserved_asic_history = merge_rebuilt_earnings_with_preserved_asic_history(
                earnings_rows,
                existing_earnings_rows,
                latest_epoch=latest_epoch,
            )
        write_jsonl_file(GLOBAL_HISTORY_FILE, global_rows, mode=0o600)
        write_jsonl_file(EARNINGS_SNAPSHOT_FILE, earnings_rows, mode=0o600)
        global_history, global_source_count = rebuild_dashboard_history_from_source(
            "global",
            GLOBAL_HISTORY_FILE,
            compact_global_snapshot_for_history,
            global_snapshot_has_plot_data,
        )
        earnings_history, earnings_source_count = rebuild_dashboard_history_from_source(
            "earnings",
            EARNINGS_SNAPSHOT_FILE,
            compact_earnings_snapshot,
            earnings_snapshot_has_plot_data,
        )
        tier_counts = {
            "global_source_count": global_source_count,
            "global_chart_rows": len(global_history),
            "earnings_source_count": earnings_source_count,
            "earnings_chart_rows": len(earnings_history),
        }

    return {
        "status": "ok" if not fetch_errors and partial_samples == 0 else "degraded",
        "generated_at": now_iso(),
        "install": install,
        "hours": hours,
        "window_blocks": window_blocks,
        "workers": workers,
        "rpc_source": rpc_name,
        "rpc_url": rpc_url,
        "probe_errors": probe_errors,
        "latest_block_count": block_count,
        "latest_order": latest_order,
        "latest_order_method": latest_order_method,
        "latest_epoch": latest_epoch,
        "latest_at": dashboard_history_iso_from_epoch(latest_epoch),
        "genesis_epoch": genesis_epoch,
        "genesis_at": dashboard_history_iso_from_epoch(genesis_epoch),
        "sample_count": len(sample_epochs),
        "header_order_count": len({order for order in all_orders}),
        "fetched_header_count": len(header_map),
        "fetch_error_count": len(fetch_errors),
        "fetch_errors": fetch_errors[:30],
        "partial_samples": partial_samples,
        "seconds_per_order_estimate": seconds_per_order,
        "rate_baseline_order": rate["baseline_order"],
        "rate_baseline_at": dashboard_history_iso_from_epoch(rate["baseline_epoch"]),
        "global_rows": len(global_rows),
        "earnings_rows": len(earnings_rows),
        "wallet_24h_rebuild": wallet_24h_rebuild,
        "preserved_asic_history": preserved_asic_history,
        "payment_wallet": wallet,
        "history_files": {
            "global": str(GLOBAL_HISTORY_FILE),
            "earnings": str(EARNINGS_SNAPSHOT_FILE),
            "disk_history_dir": str(DASHBOARD_HISTORY_DISK_DIR),
            "ram_history_dir": str(dashboard_history_ram_dir()),
        },
        "backups": backups,
        "tier_counts": tier_counts,
    }


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
    container_name = compose_container_name(name)
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        result = run(["docker", "exec", container_name, "cat", path], timeout=8)
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
    keys = [
        "schema_version",
        "status",
        "source",
        "source_truth",
        "source_contract",
        "height_method",
        "generated_at",
        "updated_at",
        "latest_block",
        "chain_block_count",
        "latest_order",
        "requested_blocks",
        "fetched_blocks",
        "unknown_blocks",
        "partial_scan",
        "head_only",
        "maintenance_deferred",
        "deferred_scan",
        "scan_window_hours",
        "avg_blocks_per_second",
        "max_transactions_per_block",
        "max_avg_block_transactions_per_second",
        "fetch_errors",
    ]
    compacted = {key: snapshot.get(key) for key in keys if key in snapshot}
    compacted["generated_at"] = compacted.get("generated_at") or compacted.get("updated_at")
    compacted["clusters"] = [
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
    ]
    return compacted


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


def is_valid_global_chain_snapshot(snapshot: Mapping[str, Any] | None) -> bool:
    if not isinstance(snapshot, Mapping):
        return False
    if snapshot.get("status") != "ok":
        return False
    if safe_int(snapshot.get("schema_version"), 0) != GLOBAL_CACHE_SCHEMA_VERSION:
        return False
    if str(snapshot.get("source_truth") or "") != GLOBAL_STATS_SOURCE_TRUTH:
        return False
    source_contract = str(snapshot.get("source_contract") or "")
    if source_contract not in {"blockdag-mining-rpc-v1", "blockdag-mining-rpc-history-v1"}:
        return False
    height_method = str(snapshot.get("height_method") or "")
    if source_contract == "blockdag-mining-rpc-v1" and height_method != "getBlockCount":
        return False
    if source_contract == "blockdag-mining-rpc-history-v1" and height_method != "getBlockByOrder-reconstructed":
        return False
    latest_block = safe_int(snapshot.get("latest_block"), None)
    chain_block_count = safe_int(snapshot.get("chain_block_count"), None)
    latest_order = safe_int(snapshot.get("latest_order"), None)
    if latest_block is None or chain_block_count is None:
        return False
    if latest_order is None or latest_order < 0:
        return False
    if chain_block_count > 0 and latest_order > chain_block_count:
        return False
    requested_blocks = safe_int(snapshot.get("requested_blocks"), None)
    fetched_blocks = safe_int(snapshot.get("fetched_blocks"), None)
    if requested_blocks is None or fetched_blocks is None or requested_blocks <= 0 or fetched_blocks != requested_blocks:
        return False
    if safe_int(snapshot.get("unknown_blocks"), 0) != 0:
        return False
    if bool(snapshot.get("partial_scan")) or bool(snapshot.get("head_only")) or bool(snapshot.get("maintenance_deferred")):
        return False
    if snapshot.get("fetch_errors"):
        return False
    return True


def is_valid_global_evm_fallback_snapshot(snapshot: Mapping[str, Any] | None) -> bool:
    if not isinstance(snapshot, Mapping):
        return False
    if snapshot.get("status") not in {"ok", "degraded"}:
        return False
    if str(snapshot.get("source_contract") or "") != "evm-rpc-fallback-v1":
        return False
    if bool(snapshot.get("head_only")):
        return False
    requested_blocks = safe_int(snapshot.get("requested_blocks"), 0)
    fetched_blocks = safe_int(snapshot.get("fetched_blocks"), 0)
    return requested_blocks > 1 and fetched_blocks > 0


def read_valid_global_history(limit: int | None = None) -> list[dict[str, Any]]:
    return [row for row in read_global_history(limit=limit) if is_valid_global_chain_snapshot(row)]


def local_pool_chain_rate_from_global_snapshot(snapshot: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not is_valid_global_chain_snapshot(snapshot):
        return None
    primary_wallet = str(read_env_value("MINING_ADDRESS") or "").lower()
    clusters = snapshot.get("clusters") if isinstance(snapshot, Mapping) else None
    if not isinstance(clusters, list):
        return None
    selected: Mapping[str, Any] | None = None
    for cluster in clusters:
        if not isinstance(cluster, Mapping):
            continue
        address = str(cluster.get("address") or "").lower()
        if bool(cluster.get("local_pool")) or (primary_wallet and address == primary_wallet):
            selected = cluster
            break
    if selected is None:
        return None
    bdag_hour = decimal_value(
        selected.get("estimated_bdag_recent_hour")
        or selected.get("estimated_bdag_avg_hour")
        or selected.get("estimated_wallet_bdag_recent_hour")
        or selected.get("estimated_wallet_bdag_avg_hour")
    )
    if bdag_hour is None or bdag_hour <= 0:
        return None
    return {
        "bdag_hour": bdag_hour,
        "usd_hour": decimal_value(selected.get("estimated_usd_recent_hour") or selected.get("estimated_usd_avg_hour")),
        "zar_hour": decimal_value(selected.get("estimated_zar_recent_hour") or selected.get("estimated_zar_avg_hour")),
        "snapshot_at": snapshot.get("generated_at") or snapshot.get("updated_at"),
        "scan_window_hours": snapshot.get("scan_window_hours"),
        "scan_window_blocks": snapshot.get("fetched_blocks") or snapshot.get("requested_blocks"),
        "avg_block_seconds": snapshot.get("avg_block_seconds"),
        "local_blocks": selected.get("blocks"),
        "local_share_percent": selected.get("share_percent"),
        "source_contract": snapshot.get("source_contract"),
    }


def latest_local_pool_chain_rate_from_global_cache() -> dict[str, Any] | None:
    cached = read_json_file(GLOBAL_CACHE_FILE, {})
    rate = local_pool_chain_rate_from_global_snapshot(cached if isinstance(cached, Mapping) else None)
    if rate is not None:
        rate["cache_source"] = str(GLOBAL_CACHE_FILE)
        return rate
    history = read_valid_global_history(limit=GLOBAL_HISTORY_LIMIT)
    for snapshot in reversed(history):
        rate = local_pool_chain_rate_from_global_snapshot(snapshot)
        if rate is not None:
            rate["cache_source"] = "global-history"
            return rate
    return None


def local_pool_chain_rates_by_epoch() -> list[tuple[float, dict[str, Any]]]:
    rates: list[tuple[float, dict[str, Any]]] = []
    for snapshot in read_valid_global_history(limit=GLOBAL_HISTORY_LIMIT):
        if not isinstance(snapshot, dict):
            continue
        rate = local_pool_chain_rate_from_global_snapshot(snapshot)
        if rate is None:
            continue
        parsed = parse_earnings_timestamp(snapshot.get("generated_at") or snapshot.get("updated_at"))
        if parsed is None:
            continue
        rates.append((parsed.timestamp(), rate))
    rates.sort(key=lambda item: item[0])
    return rates


def nearest_local_pool_chain_rate(
    epoch: float | None,
    rates: list[tuple[float, dict[str, Any]]],
    max_distance_seconds: int = 10 * 60,
) -> dict[str, Any] | None:
    if epoch is None or not rates:
        return None
    best_epoch, best_rate = min(rates, key=lambda item: abs(item[0] - epoch))
    if abs(best_epoch - epoch) > max_distance_seconds:
        return None
    return best_rate


def apply_local_pool_chain_rate_to_miner_estimates(
    miner_estimates: list[dict[str, Any]],
    rate: dict[str, Any] | None,
    price: dict[str, Any],
) -> bool:
    if not rate:
        return False
    bdag_hour = decimal_value(rate.get("bdag_hour"))
    if bdag_hour is None or bdag_hour <= 0:
        return False
    eligible = [miner for miner in miner_estimates if isinstance(miner, dict) and is_earnings_wallet_miner(miner)]
    total_work = sum(safe_int(miner.get("share_work"), 0) for miner in eligible if safe_int(miner.get("share_work"), 0) > 0)
    total_percent = Decimal("0")
    if total_work <= 0:
        total_percent = sum((decimal_value(miner.get("work_percent")) or Decimal("0")) for miner in eligible)
    if total_work <= 0 and total_percent <= 0:
        return False

    usd_hour = decimal_value(rate.get("usd_hour"))
    zar_hour = decimal_value(rate.get("zar_hour"))
    changed = False
    for miner in eligible:
        if total_work > 0:
            work = safe_int(miner.get("share_work"), 0)
            share = Decimal(work) / Decimal(total_work) if work > 0 else Decimal("0")
        else:
            percent = decimal_value(miner.get("work_percent")) or Decimal("0")
            share = percent / total_percent if total_percent > 0 else Decimal("0")
        if share <= 0:
            continue
        miner_bdag_hour = bdag_hour * share
        miner_usd_hour = usd_hour * share if usd_hour is not None else None
        miner_zar_hour = zar_hour * share if zar_hour is not None else None
        miner["estimated_wallet_bdag_recent_hour"] = decimal_to_str(miner_bdag_hour)
        miner["estimated_wallet_bdag_avg_hour"] = decimal_to_str(miner_bdag_hour)
        miner["estimated_wallet_bdag_1h"] = decimal_to_str(miner_bdag_hour)
        miner["estimated_wallet_usd_recent_hour"] = (
            decimal_to_str(miner_usd_hour) if miner_usd_hour is not None else fiat_value(miner_bdag_hour, price, "usd")
        )
        miner["estimated_wallet_usd_avg_hour"] = miner["estimated_wallet_usd_recent_hour"]
        miner["estimated_wallet_usd_1h"] = miner["estimated_wallet_usd_recent_hour"]
        miner["estimated_wallet_zar_recent_hour"] = (
            decimal_to_str(miner_zar_hour) if miner_zar_hour is not None else fiat_value(miner_bdag_hour, price, "zar")
        )
        miner["estimated_wallet_zar_avg_hour"] = miner["estimated_wallet_zar_recent_hour"]
        miner["estimated_wallet_zar_1h"] = miner["estimated_wallet_zar_recent_hour"]
        miner["estimated_wallet_rate_source"] = "chain-confirmed-local-pool-global-scan"
        miner["estimated_wallet_rate_basis"] = "local_pool_bdag_per_hour_allocated_by_live_share_work"
        miner["estimated_wallet_scan_window_hours"] = rate.get("scan_window_hours")
        miner["estimated_wallet_scan_window_blocks"] = rate.get("scan_window_blocks")
        miner["estimated_wallet_avg_block_seconds"] = rate.get("avg_block_seconds")
        miner["estimated_wallet_global_snapshot_at"] = rate.get("snapshot_at")
        miner["estimated_wallet_local_pool_bdag_hour"] = decimal_to_str(bdag_hour)
        miner["estimated_wallet_work_share_percent"] = percent_to_str(share * Decimal("100"))
        changed = True
    return changed


def apply_local_pool_chain_rates_to_earnings_history(
    history: list[dict[str, Any]],
    price: dict[str, Any],
) -> list[dict[str, Any]]:
    if not history:
        return []
    rates = local_pool_chain_rates_by_epoch()
    if not rates:
        return history
    updated: list[dict[str, Any]] = []
    for snapshot in history:
        if not isinstance(snapshot, dict):
            updated.append(snapshot)
            continue
        parsed = parse_earnings_timestamp(snapshot.get("generated_at"))
        rate = nearest_local_pool_chain_rate(parsed.timestamp() if parsed is not None else None, rates)
        if rate is None:
            updated.append(snapshot)
            continue
        clone = dict(snapshot)
        miners = [dict(miner) for miner in (snapshot.get("miner_estimates") or []) if isinstance(miner, dict)]
        if apply_local_pool_chain_rate_to_miner_estimates(miners, rate, price):
            clone["miner_estimates"] = miners
            clone["asic_allocation_rate_source"] = "chain-confirmed-local-pool-global-scan"
            clone["asic_allocation_rate_basis"] = "local_pool_bdag_per_hour_allocated_by_live_share_work"
            clone["asic_allocation_chain_rate"] = {
                "local_pool_bdag_hour": decimal_to_str(decimal_value(rate.get("bdag_hour")) or Decimal("0")),
                "snapshot_at": rate.get("snapshot_at"),
                "scan_window_hours": rate.get("scan_window_hours"),
                "scan_window_blocks": rate.get("scan_window_blocks"),
                "avg_block_seconds": rate.get("avg_block_seconds"),
            }
        updated.append(clone)
    return updated


def record_global_snapshot(snapshot: dict[str, Any]) -> None:
    ensure_runtime()
    try:
        append_jsonl_file(GLOBAL_HISTORY_FILE, snapshot, mode=0o600)
    except OSError:
        return
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


def write_global_cache(payload: dict[str, Any]) -> None:
    try:
        write_json_file(GLOBAL_CACHE_FILE, payload, mode=0o600)
    except OSError:
        return


def global_scan_tip_count(payload: Mapping[str, Any]) -> int | None:
    """Return the chain block count implied by the scanned production window."""
    scan_order = safe_int(payload.get("scan_end_order"), None)
    if scan_order is not None:
        return max(0, scan_order + 1)
    scan_block = safe_int(payload.get("scan_end_block"), None)
    if scan_block is not None:
        return max(0, scan_block)
    return safe_int(payload.get("chain_block_count"), safe_int(payload.get("latest_block"), None))


def refresh_global_chain_head(payload: dict[str, Any]) -> dict[str, Any]:
    """Add a live displayed block height without changing scan-window data."""
    is_evm_fallback = str(payload.get("source_contract") or "") == "evm-rpc-fallback-v1"
    evm_latest_block = safe_int(payload.get("evm_latest_block"), safe_int(payload.get("scan_end_block"), None))
    try:
        block_count, source_name, _source_url, errors = probe_global_chain_block_count()
    except Exception as exc:  # noqa: BLE001 - live tip freshness is best-effort.
        block_count = None
        source_name = ""
        errors = [str(exc)]
    try:
        display_height, display_source, display_metadata, display_errors = probe_global_display_block_height()
        errors.extend(display_errors)
    except Exception as exc:  # noqa: BLE001 - display freshness is best-effort.
        display_height = None
        display_source = ""
        display_metadata = {}
        errors.append(str(exc))

    if block_count is None and display_height is None and not (is_evm_fallback and evm_latest_block is not None):
        if errors:
            existing_errors = list(payload.get("head_probe_errors") or [])
            payload["head_probe_errors"] = [*existing_errors, *errors][:20]
        return payload

    has_scan_tip = payload.get("scan_end_order") is not None or payload.get("scan_end_block") is not None
    scanned_tip_count = global_scan_tip_count(payload)
    if not has_scan_tip:
        scanned_tip = safe_int(payload.get("latest_block"), 0)
        if scanned_tip is not None:
            payload["scan_end_block"] = scanned_tip
            scanned_tip_count = max(0, scanned_tip)
    if block_count is not None:
        if is_evm_fallback:
            payload["native_chain_block_count"] = block_count
            payload["native_chain_block_count_source"] = source_name or "getBlockCount"
        else:
            payload["chain_block_count"] = block_count
            payload["chain_block_count_source"] = source_name or "getBlockCount"
    if is_evm_fallback and evm_latest_block is not None:
        if display_height is not None:
            payload["native_display_latest_block"] = display_height
            payload["native_display_latest_block_source"] = display_source or "live-height"
            payload["native_display_latest_block_metadata"] = display_metadata
        display_height = evm_latest_block
        display_source = str(payload.get("rpc_source") or "evm-rpc-fallback")
        display_metadata = {
            "fallback_height_domain": "evm-json-rpc",
            "native_display_latest_block": payload.get("native_display_latest_block"),
        }
        payload["chain_block_count"] = evm_latest_block
        payload["chain_block_count_source"] = "evm-rpc-fallback"
    elif display_height is None:
        display_height = block_count
        display_source = source_name or "getBlockCount"
        display_metadata = {"fallback": "getBlockCount"}
    payload["chain_latest_block"] = display_height
    payload["display_latest_block"] = display_height
    payload["chain_latest_block_source"] = display_source or "live-height"
    payload["chain_latest_block_updated_at"] = now_iso()
    lag_tip_count = evm_latest_block if is_evm_fallback else block_count
    payload["chain_tip_lag_blocks"] = max(0, lag_tip_count - (scanned_tip_count or 0)) if lag_tip_count is not None else 0
    payload["latest_block"] = display_height
    payload["display_latest_block_metadata"] = display_metadata
    return payload


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


def miner_worker_identity_score(miner: dict[str, Any]) -> tuple[int, int, int, int, int, int]:
    return (
        safe_int(miner.get("last_shares_window"), 0),
        1 if bool(miner.get("last_configured_ok")) else 0,
        safe_int(miner.get("last_share_epoch"), 0),
        safe_int(miner.get("last_blocks_window"), 0),
        safe_int(miner.get("last_submit_epoch"), 0),
        safe_int(miner.get("last_pool_seen_epoch"), 0),
    )


def local_worker_identity_for_shared_miners(miners: list[dict[str, Any]]) -> dict[str, Any]:
    identities: dict[str, dict[str, Any]] = {}
    for miner in miners:
        identity = miner_identity_key(miner)
        if not identity:
            continue
        identities.setdefault(identity, miner)
    unique_miners = list(identities.values())
    if len(unique_miners) <= 1:
        miner = unique_miners[0] if unique_miners else {}
        label = miner_display_label(miner) if miner else "Local pool"
        return {
            "pool_name": label,
            "display_name": miner.get("display_name") or miner.get("name") or "",
            "display_label": label,
            "device_type": miner.get("device_type") or "",
            "ip": miner.get("ip") or "",
            "mac": normalize_mac(miner.get("mac")),
            "identity_key": miner_identity_key(miner),
            "local_miner_count": len(unique_miners),
            "local_asic_count": 1 if str(miner.get("device_type") or "").lower() == "asic" else 0,
        }

    asic_count = sum(1 for miner in unique_miners if str(miner.get("device_type") or "").lower() == "asic")
    count_label = f"{asic_count} ASICs" if asic_count == len(unique_miners) else f"{len(unique_miners)} miners"
    return {
        "pool_name": f"Local pool ({count_label})",
        "display_name": "Local pool",
        "display_label": f"Local pool ({count_label})",
        "device_type": "pool",
        "ip": "",
        "mac": "",
        "identity_key": "",
        "local_miner_count": len(unique_miners),
        "local_asic_count": asic_count,
        "local_macs": sorted(
            mac for mac in (normalize_mac(miner.get("mac")) for miner in unique_miners) if mac
        ),
    }


def local_worker_identity_map() -> dict[str, dict[str, Any]]:
    registry = read_miner_registry()
    worker_miners: dict[str, list[dict[str, Any]]] = {}
    for miner in registry.get("miners", []) or []:
        if not isinstance(miner, dict):
            continue
        worker_values = merge_unique_strings(miner.get("last_workers"), miner.get("workers"))
        for worker in worker_values:
            if not is_spendable_eth_address(worker):
                continue
            worker_miners.setdefault(str(worker).lower(), []).append(miner)
    workers: dict[str, dict[str, Any]] = {}
    for worker_key, miners in worker_miners.items():
        workers[worker_key] = local_worker_identity_for_shared_miners(
            sorted(miners, key=miner_worker_identity_score, reverse=True)
        )
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
      SELECT lower(c.miner_address) AS miner_address,
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
      GROUP BY lower(c.miner_address)
      ORDER BY count(DISTINCT c.block_hash) DESC, sum(c.amount) DESC
    ) t;
    """
    try:
        rows = pool_db_json(sql) or []
    except Exception as exc:  # noqa: BLE001
        return [{"source": "local-postgres", "status": "failed", "error": str(exc), "local_pool": True}]
    if not isinstance(rows, list):
        return []

    identities = local_worker_identity_map()
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
        clusters.append(
            {
                "rank": None,
                "address": address,
                "address_short": short_eth_address(address),
                "pool_name": pool_name,
                "pool_label": f"{pool_name} ({short_eth_address(address)})",
                "source": "local-postgres",
                "local_pool": True,
                "nodes": list(NODES),
                "rpc_sources": ["local-postgres"],
                "workers": [address],
                "shares": credit_count,
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
                "estimated_usd_avg_hour": fiat_value(hourly_bdag, price, "usd") if hourly_bdag is not None else None,
                "estimated_zar_avg_hour": fiat_value(hourly_bdag, price, "zar") if hourly_bdag is not None else None,
                "estimated_bdag_recent_hour": decimal_to_str(hourly_bdag) if hourly_bdag is not None else None,
                "estimated_usd_recent_hour": fiat_value(hourly_bdag, price, "usd") if hourly_bdag is not None else None,
                "estimated_zar_recent_hour": fiat_value(hourly_bdag, price, "zar") if hourly_bdag is not None else None,
                "first_seen_at": row.get("first_seen_at") or row.get("first_credit_at"),
                "last_seen_at": row.get("last_seen_at") or row.get("last_credit_at"),
                "location": "local pool",
                "location_confidence": "postgres",
                "identity_key": identity.get("identity_key") or "",
                "ip": identity.get("ip") or "",
                "mac": identity.get("mac") or "",
                "device_type": identity.get("device_type") or "",
                "local_miner_count": identity.get("local_miner_count") or 0,
                "local_asic_count": identity.get("local_asic_count") or 0,
                "local_macs": identity.get("local_macs") or [],
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
            # Global production rows are chain-confirmed only. Local pool DB rows
            # can annotate a matching chain address, but must not create
            # dashboard-visible block production that the chain did not report.
            continue
        existing["local_pool"] = True
        existing["source"] = "on-chain+local-postgres"
        for key in (
            "pool_name",
            "pool_label",
            "nodes",
            "workers",
            "identity_key",
            "ip",
            "mac",
            "device_type",
            "local_miner_count",
            "local_asic_count",
            "local_macs",
        ):
            if local.get(key) not in (None, "", []):
                existing[key] = local[key]
        for local_key, display_key in (
            ("shares", "local_shares"),
            ("credit_blocks", "local_credit_blocks"),
            ("credited_bdag", "local_credited_bdag"),
            ("found_blocks", "local_found_blocks"),
            ("estimated_wallet_bdag", "local_estimated_wallet_bdag"),
        ):
            if local.get(local_key) not in (None, "", []):
                existing[display_key] = local[local_key]
    merged.sort(key=lambda item: (int(item.get("blocks", 0) or 0), str(item.get("last_seen_at") or "")), reverse=True)
    for rank, cluster in enumerate(merged, start=1):
        cluster["rank"] = rank
    return merged


def collect_global_blockchain() -> dict[str, Any]:
    cached = read_json_file(GLOBAL_CACHE_FILE, {})
    cached_at = int(cached.get("updated_at_epoch", 0) or 0) if isinstance(cached, dict) else 0
    cached_valid = is_valid_global_chain_snapshot(cached)
    invalid_cache_error = ""
    if isinstance(cached, dict) and cached.get("status") == "ok" and not cached_valid:
        invalid_cache_error = (
            "ignored stale global cache with unsupported source/schema "
            f"source_truth={cached.get('source_truth') or cached.get('source') or 'unknown'} "
            f"schema={cached.get('schema_version') or 'missing'}"
        )

    def lightweight_global_head(
        error: str,
        errors: list[str],
        history: list[dict[str, Any]],
        maintenance_decision: dict[str, Any],
    ) -> dict[str, Any] | None:
        fetch_errors = list(errors)
        block_count, source_name, _source_url, probe_errors = probe_global_chain_block_count()
        fetch_errors.extend(probe_errors)
        if block_count is None:
            return None
        latest_order = max(0, block_count - 1)
        return annotate_global_pool_labels(
            {
                "status": "deferred",
                "source": "on-chain-head",
                "source_truth": GLOBAL_STATS_SOURCE_TRUTH,
                "source_contract": "blockdag-mining-rpc-v1",
                "schema_version": GLOBAL_CACHE_SCHEMA_VERSION,
                "rpc_kind": "blockdag-chain-rpc",
                "height_method": "getBlockCount",
                "updated_at": now_iso(),
                "updated_at_epoch": seconds_since_epoch(),
                "rpc_source": source_name,
                "chain_block_count": block_count,
                "latest_block": block_count,
                "latest_order": latest_order,
                "requested_blocks": 0,
                "fetched_blocks": 0,
                "global_rpc_worker_count": 0,
                "adaptive_concurrency": maintenance_decision.get("adaptive_concurrency", {}),
                "scan_start_block": None,
                "scan_end_block": None,
                "scan_start_order": None,
                "scan_end_order": None,
                "scan_window_seconds": 0,
                "scan_window_hours": "0.00",
                "avg_block_seconds": None,
                "unique_miners": 0,
                "chain_unique_miners": 0,
                "clusters": [],
                "chain_clusters": [],
                "local_pool_clusters": [],
                "peer_location": {"observations": []},
                "fetch_errors": fetch_errors[:20],
                "history": history,
                "cache_hit": False,
                "cache": {
                    "hit": False,
                    "ttl_seconds": GLOBAL_CACHE_TTL_SECONDS,
                    "max_tip_lag_blocks": GLOBAL_CACHE_MAX_TIP_LAG_BLOCKS,
                },
                "head_only": True,
                "maintenance_deferred": True,
                "maintenance_decision": maintenance_decision,
                "error": error,
            }
        )

    def evm_fallback_global(
        error: str,
        errors: list[str],
        history: list[dict[str, Any]],
        chain_block_count: int | None = None,
    ) -> dict[str, Any] | None:
        freshest = freshest_evm_rpc_source(public_evm_rpc_urls(), timeout=6.0)
        if freshest is None:
            return None
        rpc_name, rpc_url, evm_latest_block, latest_errors = freshest
        requested_count = min(max(GLOBAL_EVM_FALLBACK_BLOCK_WINDOW, 1), evm_latest_block + 1)
        start_block = max(0, evm_latest_block - requested_count + 1)
        block_numbers = list(range(start_block, evm_latest_block + 1))
        worker_count = min(GLOBAL_EVM_FALLBACK_RPC_WORKERS, len(block_numbers))
        headers: list[dict[str, Any]] = []
        fetch_errors = [*errors, *latest_errors]

        def load_block(block_number: int) -> dict[str, Any]:
            header = fetch_block_header(rpc_url, block_number, timeout=10.0)
            header["_rpc_source"] = rpc_name
            return header

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            future_map = {pool.submit(load_block, number): number for number in block_numbers}
            for future in as_completed(future_map):
                number = future_map[future]
                try:
                    headers.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    fetch_errors.append(f"{number}: {exc}")
        if not headers:
            return None

        headers.sort(key=lambda item: safe_int(str(item.get("number") or "0"), 0))
        cluster_map: dict[str, dict[str, Any]] = {}
        first_seen_epoch: int | None = None
        last_seen_epoch: int | None = None
        zero_address_blocks = 0
        for header in headers:
            miner = str(header.get("miner") or header.get("author") or header.get("coinbase") or "").lower()
            if not miner:
                continue
            try:
                epoch = int(str(header.get("timestamp") or "0"), 16)
                height = int(str(header.get("number") or "0"), 16)
            except (TypeError, ValueError):
                continue
            first_seen_epoch = epoch if first_seen_epoch is None else min(first_seen_epoch, epoch)
            last_seen_epoch = epoch if last_seen_epoch is None else max(last_seen_epoch, epoch)
            if not is_spendable_eth_address(miner):
                zero_address_blocks += 1
                continue
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

        total_blocks = len(headers)
        window_seconds = max(1, (last_seen_epoch or 0) - (first_seen_epoch or 0))
        scan_window_hours = Decimal(str(window_seconds)) / Decimal("3600") if window_seconds > 0 else None
        avg_block_seconds = window_seconds / max(1, total_blocks - 1) if total_blocks > 1 else None
        enriched_clusters: list[dict[str, Any]] = []
        clusters = sorted(cluster_map.values(), key=lambda item: (item["blocks"], item["last_seen_epoch"]), reverse=True)
        for rank, cluster in enumerate(clusters, start=1):
            blocks = int(cluster["blocks"])
            share = Decimal(blocks) / Decimal(max(1, total_blocks))
            enriched_clusters.append(
                {
                    "rank": rank,
                    "address": cluster["address"],
                    "address_short": short_eth_address(cluster["address"]),
                    "pool_name": "",
                    "source": "evm-rpc-fallback",
                    "local_pool": False,
                    "blocks": blocks,
                    "shares": blocks,
                    "credit_blocks": blocks,
                    "found_blocks": blocks,
                    "share_percent": decimal_to_str(share * Decimal("100"), places=2),
                    "first_height": cluster["first_height"],
                    "last_height": cluster["last_height"],
                    "first_seen_at": datetime.fromtimestamp(cluster["first_seen_epoch"], tz=timezone.utc).isoformat(),
                    "last_seen_at": datetime.fromtimestamp(cluster["last_seen_epoch"], tz=timezone.utc).isoformat(),
                    "avg_block_seconds": decimal_to_str(Decimal(str(avg_block_seconds)), places=1) if avg_block_seconds is not None and blocks > 1 else None,
                    "rpc_sources": unique_names(cluster["rpc_sources"]),
                }
            )
        local_pool_clusters = collect_local_pool_global_clusters(window_seconds, total_blocks, scan_window_hours, {})
        display_clusters = merge_global_local_pool_clusters(enriched_clusters, local_pool_clusters)
        latest_block = evm_latest_block
        payload: dict[str, Any] = {
            "status": "degraded",
            "source": "on-chain-evm-fallback",
            "source_truth": "evm-rpc:eth_blockNumber/eth_getBlockByNumber fallback",
            "source_contract": "evm-rpc-fallback-v1",
            "schema_version": GLOBAL_CACHE_SCHEMA_VERSION,
            "rpc_kind": "evm-json-rpc",
            "height_method": "eth_blockNumber",
            "updated_at": now_iso(),
            "updated_at_epoch": seconds_since_epoch(),
            "rpc_source": rpc_name,
            "chain_block_count": evm_latest_block,
            "native_chain_block_count": chain_block_count,
            "latest_block": latest_block,
            "latest_order": evm_latest_block,
            "evm_latest_block": evm_latest_block,
            "requested_blocks": requested_count,
            "fetched_blocks": total_blocks,
            "unknown_blocks": 0,
            "partial_scan": False,
            "global_rpc_worker_count": worker_count,
            "adaptive_concurrency": {},
            "scan_start_block": start_block,
            "scan_end_block": evm_latest_block,
            "scan_start_order": None,
            "scan_end_order": None,
            "scan_window_seconds": window_seconds,
            "scan_window_hours": decimal_to_str(Decimal(str(window_seconds)) / Decimal("3600"), places=2),
            "avg_block_seconds": decimal_to_str(Decimal(str(avg_block_seconds)), places=1) if avg_block_seconds is not None else None,
            "unique_miners": len(display_clusters),
            "chain_unique_miners": len(enriched_clusters),
            "clusters": display_clusters,
            "chain_clusters": enriched_clusters,
            "local_pool_clusters": local_pool_clusters,
            "peer_location": {"observations": []},
            "fetch_errors": fetch_errors[:20],
            "zero_address_blocks": zero_address_blocks,
            "history": history,
            "cache_hit": False,
            "cache": {"hit": False, "ttl_seconds": GLOBAL_CACHE_TTL_SECONDS},
            "error": f"{error}; using EVM header fallback",
        }
        payload = annotate_global_pool_labels(payload)
        write_global_cache(payload)
        return payload

    def stale_or_failed(
        error: str,
        errors: list[str] | None = None,
        maintenance_decision: dict[str, Any] | None = None,
        chain_block_count: int | None = None,
    ) -> dict[str, Any]:
        history = read_valid_global_history(limit=GLOBAL_HISTORY_LIMIT)
        fetch_errors = list(errors or [])
        if invalid_cache_error:
            fetch_errors.insert(0, invalid_cache_error)
        if GLOBAL_EVM_FALLBACK_ENABLED and maintenance_decision is None:
            fallback = evm_fallback_global(error, fetch_errors, history, chain_block_count)
            if fallback is not None:
                return refresh_global_chain_head(fallback)
        if cached_valid:
            cache_meta = dict(cached.get("cache") or {}) if isinstance(cached.get("cache"), dict) else {}
            cache_meta.update(
                {
                    "hit": True,
                    "age_seconds": max(0, seconds_since_epoch() - cached_at),
                    "ttl_seconds": GLOBAL_CACHE_TTL_SECONDS,
                    "max_tip_lag_blocks": GLOBAL_CACHE_MAX_TIP_LAG_BLOCKS,
                }
            )
            return annotate_global_pool_labels(
                refresh_global_chain_head({
                    **cached,
                    "status": "stale",
                    "stale": True,
                    "cache_hit": True,
                    "cache": cache_meta,
                    "error": error,
                    "fetch_errors": fetch_errors or cached.get("fetch_errors", []),
                    "history": history,
                })
            )
        if maintenance_decision is not None:
            head = lightweight_global_head(error, errors or [], history, maintenance_decision)
            if head is not None:
                return refresh_global_chain_head(head)
        return refresh_global_chain_head(
            {
                "status": "failed",
                "source": "on-chain",
                "source_truth": GLOBAL_STATS_SOURCE_TRUTH,
                "schema_version": GLOBAL_CACHE_SCHEMA_VERSION,
                "source_contract": "blockdag-mining-rpc-v1",
                "error": error,
                "fetch_errors": fetch_errors,
                "chain_block_count": chain_block_count,
                "latest_block": chain_block_count,
                "clusters": [],
                "history": history,
                "cache": {
                    "hit": False,
                    "ttl_seconds": GLOBAL_CACHE_TTL_SECONDS,
                    "max_tip_lag_blocks": GLOBAL_CACHE_MAX_TIP_LAG_BLOCKS,
                },
            }
        )

    maintenance_decision = background_maintenance_decision("global_blockchain_scan")
    if not maintenance_decision.get("allowed", True):
        reason = "; ".join(str(item) for item in maintenance_decision.get("reasons", []) if item)
        result = stale_or_failed(
            f"global blockchain scan deferred: {reason}",
            [reason] if reason else [],
            maintenance_decision,
        )
        result["maintenance_deferred"] = True
        result["maintenance_decision"] = maintenance_decision
        return result

    if cached_valid and seconds_since_epoch() - cached_at <= GLOBAL_CACHE_TTL_SECONDS:
        live_count, live_source, _live_url, live_errors = probe_global_chain_block_count()
        if live_count is not None:
            cached_head_count = safe_int(cached.get("chain_block_count"), safe_int(cached.get("latest_block"), 0))
            cached_scan_count = global_scan_tip_count(cached) or cached_head_count
            if max(cached_head_count, cached_scan_count) > live_count:
                cached_valid = False
                invalid_cache_error = (
                    f"ignored global cache ahead of live chain tip cached={max(cached_head_count, cached_scan_count)} "
                    f"live={live_count}; refreshing from chain RPC"
                )
            cache_tip_lag = max(0, live_count - cached_scan_count)
            if cached_valid and cache_tip_lag == 0:
                cache_meta = dict(cached.get("cache") or {}) if isinstance(cached.get("cache"), dict) else {}
                cache_meta.update(
                    {
                        "hit": True,
                        "age_seconds": max(0, seconds_since_epoch() - cached_at),
                        "ttl_seconds": GLOBAL_CACHE_TTL_SECONDS,
                        "max_tip_lag_blocks": GLOBAL_CACHE_MAX_TIP_LAG_BLOCKS,
                        "tip_lag_blocks": cache_tip_lag,
                    }
                )
                return annotate_global_pool_labels(
                    {
                        **cached,
                        "cache_hit": True,
                        "cache": cache_meta,
                        "cache_tip_lag_blocks": cache_tip_lag,
                        "cache_validated_by": live_source,
                        "history": read_valid_global_history(limit=GLOBAL_HISTORY_LIMIT),
                    }
                )
            if cached_valid:
                invalid_cache_error = (
                    f"global cache tip lag {cache_tip_lag} blocks exceeds "
                    "0 for authoritative display; refreshing from chain RPC"
                )
        else:
            invalid_cache_error = "unable to validate global cache freshness from chain RPC; refusing ok cache"

    rpc_sources = global_chain_rpc_urls() or [("local-chain", f"http://127.0.0.1:{NODE_MINING_RPC_PORT}")]
    latest_errors: list[str] = []
    candidates: list[tuple[int, str, str]] = []
    for source_name, source_url in rpc_sources:
        try:
            block_count = parse_rpc_quantity(
                mining_rpc_call(source_url, "getBlockCount", [], timeout=chain_rpc_timeout_for_source(source_name, 8.0))
            )
            if block_count > 0:
                candidates.append((block_count, source_name, source_url))
        except Exception as exc:  # noqa: BLE001
            latest_errors.append(f"{source_name}: {exc}")
    candidates.sort(key=lambda item: item[0], reverse=True)
    latest_block_count = candidates[0][0] if candidates else None
    if latest_block_count is None:
        return stale_or_failed("unable to fetch latest global block height from chain RPC getBlockCount", latest_errors)

    order_probe_errors: list[str] = []
    selected_source: tuple[int, str, str, int, str, int, int, int, dict[str, Any]] | None = None
    for candidate_block_count, candidate_name, candidate_url in candidates:
        try:
            candidate_order, candidate_order_method = fetch_chain_order_tip(
                candidate_url,
                timeout=chain_rpc_timeout_for_source(candidate_name, GLOBAL_CHAIN_ORDER_RPC_TIMEOUT),
            )
        except Exception as exc:  # noqa: BLE001
            order_probe_errors.append(f"{candidate_name}: {exc}")
            continue
        candidate_requested_count = min(max(GLOBAL_BLOCK_WINDOW, 1), candidate_order + 1)
        candidate_start_order = max(0, candidate_order - candidate_requested_count + 1)
        candidate_preflight_order = candidate_order
        try:
            candidate_preflight_header = fetch_chain_order_header(
                candidate_url,
                candidate_name,
                candidate_preflight_order,
                timeout=chain_rpc_timeout_for_source(candidate_name, GLOBAL_CHAIN_ORDER_RPC_TIMEOUT),
            )
        except Exception as exc:  # noqa: BLE001
            if candidate_order > 0 and "Order is too big" in str(exc):
                candidate_order -= 1
                candidate_requested_count = min(max(GLOBAL_BLOCK_WINDOW, 1), candidate_order + 1)
                candidate_start_order = max(0, candidate_order - candidate_requested_count + 1)
                candidate_preflight_order = candidate_order
                try:
                    candidate_preflight_header = fetch_chain_order_header(
                        candidate_url,
                        candidate_name,
                        candidate_preflight_order,
                        timeout=chain_rpc_timeout_for_source(candidate_name, GLOBAL_CHAIN_ORDER_RPC_TIMEOUT),
                    )
                except Exception as retry_exc:  # noqa: BLE001
                    order_probe_errors.append(f"{candidate_name}: {candidate_preflight_order}: {retry_exc}")
                    continue
            else:
                order_probe_errors.append(f"{candidate_name}: {candidate_preflight_order}: {exc}")
                continue
        selected_source = (
            candidate_block_count,
            candidate_name,
            candidate_url,
            candidate_order,
            candidate_order_method,
            candidate_requested_count,
            candidate_start_order,
            candidate_preflight_order,
            candidate_preflight_header,
        )
        break

    if selected_source is None:
        return stale_or_failed(
            "unable to resolve latest global chain order from chain RPC",
            latest_errors + order_probe_errors,
            chain_block_count=latest_block_count,
        )
    latest_errors.extend(order_probe_errors)
    (
        selected_block_count,
        rpc_name,
        rpc_url,
        latest_order,
        latest_order_method,
        requested_count,
        start_order,
        preflight_order,
        preflight_header,
    ) = selected_source
    headers: list[dict[str, Any]] = [preflight_header]
    preflight_orders = {preflight_order}
    primary_sources = [(rpc_name, rpc_url), *[(name, url) for _, name, url in candidates if url != rpc_url]]

    def fetch_order_from_sources(order: int, timeout: float) -> dict[str, Any]:
        source_errors: list[str] = []
        for source_name, source_url in primary_sources:
            try:
                return fetch_chain_order_header(
                    source_url,
                    source_name,
                    order,
                    timeout=chain_rpc_timeout_for_source(source_name, timeout),
                )
            except Exception as exc:  # noqa: BLE001
                source_errors.append(f"{source_name}: {exc}")
        raise RuntimeError("; ".join(source_errors) or f"failed to fetch chain block order {order}")

    if requested_count >= GLOBAL_CHAIN_PREFLIGHT_SAMPLE_MIN_BLOCKS:
        sample_offsets = (1, 2, 4, 8)
        for sample_order in sorted({latest_order - offset for offset in sample_offsets if latest_order - offset >= start_order}, reverse=True):
            if sample_order in preflight_orders:
                continue
            try:
                headers.append(fetch_order_from_sources(sample_order, timeout=GLOBAL_CHAIN_ORDER_RPC_TIMEOUT))
                preflight_orders.add(sample_order)
            except Exception as exc:  # noqa: BLE001
                return stale_or_failed(
                    "unable to complete chain order preflight sample",
                    latest_errors + [f"{sample_order}: {exc}"],
                    chain_block_count=latest_block_count,
                )
    requested_orders = list(range(start_order, latest_order + 1))
    requested_orders = [order for order in requested_orders if order not in preflight_orders]

    def load_block(order: int) -> dict[str, Any]:
        return fetch_order_from_sources(order, timeout=GLOBAL_CHAIN_BLOCK_RPC_TIMEOUT)

    fetch_errors: list[str] = []
    global_pressure = {
        "iowait_percent": maintenance_decision.get("iowait_percent"),
        "io_some_avg10": maintenance_decision.get("io_some_avg10"),
        "cpu_some_avg10": maintenance_decision.get("cpu_some_avg10"),
        "chain_rpc_latency_ms": maintenance_decision.get("chain_rpc_latency_ms"),
    }
    global_worker_count = adaptive_worker_count("global_rpc", GLOBAL_RPC_WORKERS, len(requested_orders), global_pressure)
    with ThreadPoolExecutor(max_workers=global_worker_count) as pool:
        future_map = {pool.submit(load_block, order): order for order in requested_orders}
        for future in as_completed(future_map):
            order = future_map[future]
            try:
                headers.append(future.result())
            except Exception as exc:  # noqa: BLE001
                fetch_errors.append(f"{order}: {exc}")

    headers.sort(key=lambda item: safe_int(item.get("order"), 0))
    if not headers:
        return stale_or_failed("unable to fetch chain block headers by order", latest_errors + fetch_errors, chain_block_count=latest_block_count)

    total_blocks = len(headers)
    unknown_blocks = max(0, requested_count - total_blocks)
    partial_scan = unknown_blocks > 0
    if partial_scan and cached_valid:
        return stale_or_failed(
            f"global chain scan partial: fetched {total_blocks}/{requested_count} requested blocks; keeping last trusted cache",
            latest_errors + fetch_errors,
        )
    reward_values = [item["reward_bdag"] for item in headers if isinstance(item.get("reward_bdag"), Decimal)]
    known_reward_count = len(reward_values)
    known_reward_total = sum(reward_values, Decimal("0"))
    avg_reward_bdag = known_reward_total / Decimal(known_reward_count) if known_reward_count else None
    missing_reward_blocks = max(0, total_blocks - known_reward_count)
    total_reward_estimate = None
    if avg_reward_bdag is not None:
        total_reward_estimate = known_reward_total + (avg_reward_bdag * Decimal(missing_reward_blocks))
    price = fetch_cmc_price()
    peer_location = collect_peer_location_guess()

    cluster_map: dict[str, dict[str, Any]] = {}
    first_seen_epoch: int | None = None
    last_seen_epoch: int | None = None
    zero_address_blocks = 0
    zero_address_reward_bdag = Decimal("0")
    for header in headers:
        miner = str(header.get("miner") or "").lower()
        if not miner:
            continue
        reward_bdag = header.get("reward_bdag")
        epoch = safe_int(header.get("timestamp_epoch"), 0)
        first_seen_epoch = epoch if first_seen_epoch is None else min(first_seen_epoch, epoch)
        last_seen_epoch = epoch if last_seen_epoch is None else max(last_seen_epoch, epoch)
        if miner == ZERO_ETH_ADDRESS:
            zero_address_blocks += 1
            if isinstance(reward_bdag, Decimal):
                zero_address_reward_bdag += reward_bdag
            continue
        height = safe_int(header.get("order"), 0)
        entry = cluster_map.setdefault(
            miner,
            {
                "address": miner,
                "blocks": 0,
                "reward_bdag": Decimal("0"),
                "reward_count": 0,
                "first_height": height,
                "last_height": height,
                "first_seen_epoch": epoch,
                "last_seen_epoch": epoch,
                "rpc_sources": [],
                "header_errors": [],
            },
        )
        entry["blocks"] += 1
        if isinstance(reward_bdag, Decimal):
            entry["reward_bdag"] += reward_bdag
            entry["reward_count"] += 1
        entry["first_height"] = min(entry["first_height"], height)
        entry["last_height"] = max(entry["last_height"], height)
        entry["first_seen_epoch"] = min(entry["first_seen_epoch"], epoch)
        entry["last_seen_epoch"] = max(entry["last_seen_epoch"], epoch)
        entry["rpc_sources"].append(str(header.get("_rpc_source") or rpc_name))
        if header.get("header_error"):
            entry["header_errors"].append(str(header["header_error"]))

    clusters = sorted(cluster_map.values(), key=lambda item: (item["blocks"], item["last_seen_epoch"]), reverse=True)
    unique_miners = len(clusters)
    window_seconds = max(1, (last_seen_epoch or 0) - (first_seen_epoch or 0))
    avg_block_seconds = window_seconds / max(1, total_blocks - 1) if total_blocks > 1 else None
    total_reward_estimate_bdag = decimal_to_str(total_reward_estimate, places=2) if total_reward_estimate is not None else None
    scan_window_hours = Decimal(str(window_seconds)) / Decimal("3600") if window_seconds > 0 else None
    enriched_clusters: list[dict[str, Any]] = []
    share_denominator = max(1, requested_count)
    for rank, cluster in enumerate(clusters, start=1):
        blocks = int(cluster["blocks"])
        share = Decimal(blocks) / Decimal(share_denominator)
        missing_cluster_rewards = max(0, blocks - int(cluster.get("reward_count", 0) or 0))
        known_bdag = cluster["reward_bdag"]
        est_bdag = None
        if avg_reward_bdag is not None:
            est_bdag = known_bdag + (avg_reward_bdag * Decimal(missing_cluster_rewards))
        est_bdag_hour, est_usd_hour, est_zar_hour = _pool_earning_rates_from_cluster(
            {
                "estimated_bdag": decimal_to_str(est_bdag, places=2) if est_bdag is not None else None,
                "estimated_usd": fiat_value(est_bdag, price, "usd") if est_bdag is not None else None,
                "estimated_zar": fiat_value(est_bdag, price, "zar") if est_bdag is not None else None,
            },
            scan_window_hours,
        )
        invalid_payout = cluster["address"] == ZERO_ETH_ADDRESS
        enriched_clusters.append(
            {
                "rank": rank,
                "address": cluster["address"],
                "address_short": short_eth_address(cluster["address"]),
                "pool_name": "ZERO ADDRESS" if invalid_payout else "",
                "source": "chain-rpc",
                "local_pool": False,
                "blocks": blocks,
                "shares": blocks,
                "credit_blocks": blocks,
                "found_blocks": blocks,
                "share_percent": decimal_to_str(share * Decimal("100"), places=2),
                "credited_bdag": decimal_to_str(known_bdag, places=2),
                "known_reward_bdag": decimal_to_str(known_bdag, places=2),
                "reward_missing_blocks": missing_cluster_rewards,
                "reward_estimated": missing_cluster_rewards > 0,
                "estimated_bdag": decimal_to_str(est_bdag, places=2) if est_bdag is not None else None,
                "estimated_usd": fiat_value(est_bdag, price, "usd") if est_bdag is not None else None,
                "estimated_zar": fiat_value(est_bdag, price, "zar") if est_bdag is not None else None,
                "estimated_wallet_bdag": decimal_to_str(est_bdag, places=2) if est_bdag is not None else None,
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
                "invalid_payout": invalid_payout,
                "header_errors": unique_names(cluster["header_errors"])[:3],
            }
        )

    local_pool_clusters = collect_local_pool_global_clusters(
        window_seconds,
        share_denominator,
        scan_window_hours,
        price,
    )
    display_clusters = merge_global_local_pool_clusters(enriched_clusters, local_pool_clusters)

    payload = {
        "status": "degraded" if partial_scan else "ok",
        "source": "on-chain",
        "source_truth": GLOBAL_STATS_SOURCE_TRUTH,
        "source_contract": "blockdag-mining-rpc-v1",
        "schema_version": GLOBAL_CACHE_SCHEMA_VERSION,
        "rpc_kind": "blockdag-chain-rpc",
        "height_method": "getBlockCount",
        "updated_at": now_iso(),
        "updated_at_epoch": seconds_since_epoch(),
        "rpc_source": rpc_name,
        "selected_chain_block_count": selected_block_count,
        "chain_block_count": latest_block_count,
        "latest_block": latest_block_count,
        "latest_order": latest_order,
        "latest_order_method": latest_order_method,
        "requested_blocks": requested_count,
        "fetched_blocks": total_blocks,
        "unknown_blocks": unknown_blocks,
        "partial_scan": partial_scan,
        "global_rpc_worker_count": global_worker_count,
        "adaptive_concurrency": adaptive_worker_budgets(global_pressure),
        "scan_start_block": start_order,
        "scan_end_block": latest_order,
        "scan_start_order": start_order,
        "scan_end_order": latest_order,
        "scan_window_seconds": window_seconds,
        "scan_window_hours": decimal_to_str(Decimal(str(window_seconds)) / Decimal("3600"), places=2),
        "avg_block_seconds": decimal_to_str(Decimal(str(avg_block_seconds)), places=1) if avg_block_seconds is not None else None,
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
        "rpc_probe_errors": latest_errors[:20],
        "rpc_order_probe_errors": order_probe_errors[:20],
        "zero_address_blocks": zero_address_blocks,
        "attributed_blocks": max(0, requested_count - zero_address_blocks - unknown_blocks),
        "unattributed_reward_bdag": decimal_to_str(zero_address_reward_bdag, places=2),
        "reward_source": "getBlockHeader.reward atomic units",
        "reward_known_blocks": known_reward_count,
        "reward_missing_blocks": missing_reward_blocks,
        "cache": {
            "hit": False,
            "schema_version": GLOBAL_CACHE_SCHEMA_VERSION,
            "ttl_seconds": GLOBAL_CACHE_TTL_SECONDS,
            "max_tip_lag_blocks": GLOBAL_CACHE_MAX_TIP_LAG_BLOCKS,
        },
    }
    if partial_scan:
        payload["error"] = f"global chain scan partial: fetched {total_blocks}/{requested_count} requested blocks"
        payload["history"] = read_valid_global_history(limit=GLOBAL_HISTORY_LIMIT)
        payload = annotate_global_pool_labels(payload)
        return payload
    record_global_snapshot(
        {
            "status": "ok",
            "schema_version": payload["schema_version"],
            "source_truth": payload["source_truth"],
            "source_contract": payload["source_contract"],
            "height_method": payload["height_method"],
            "generated_at": payload["updated_at"],
            "latest_block": payload["latest_block"],
            "chain_block_count": payload["chain_block_count"],
            "latest_order": payload["latest_order"],
            "latest_order_method": payload.get("latest_order_method"),
            "requested_blocks": payload["requested_blocks"],
            "fetched_blocks": payload["fetched_blocks"],
            "unknown_blocks": payload["unknown_blocks"],
            "partial_scan": False,
            "head_only": False,
            "maintenance_deferred": False,
            "fetch_errors": [],
            "scan_window_hours": payload["scan_window_hours"],
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
    payload["history"] = read_valid_global_history(limit=GLOBAL_HISTORY_LIMIT)
    payload = annotate_global_pool_labels(payload)
    write_global_cache(payload)
    return payload


def collect_global_pool_earnings_window(block_window: int = 600) -> dict[str, Any]:
    """Compatibility wrapper for collector builds that expose this narrow route.

    The stack global collector already includes local-pool earnings clusters in
    the normal global payload. Keep the collector API route available without
    reintroducing a second, older EVM-only implementation.
    """
    payload = dict(collect_global_blockchain())
    payload["requested_window"] = max(1, int(block_window or 600))
    payload["source_route"] = "collect_global_blockchain"
    return payload


def collect_wallet_balances(address: str | None = None) -> dict[str, Any]:
    wallet = address or read_env_value("MINING_ADDRESS")
    if not wallet:
        return {"address": None, "sources": []}

    sources: list[dict[str, Any]] = []
    local_sources = local_evm_balance_rpc_urls()
    local_evm_pause = local_evm_balance_probe_pause()
    if bool(local_evm_pause.get("paused")):
        for name, _url in local_sources:
            sources.append(local_evm_rpc_pause_skipped_source(name, local_evm_pause))
    else:
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
            ("blockdag-engineering-rpc", "https://rpc.blockdag.engineering"),
            ("bdagscan-rpc", "https://rpc.bdagscan.com"),
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
            "paused": bool(local_evm_pause.get("paused")),
            "reason": local_evm_pause.get("reason"),
            "skipped_source_count": len(local_sources) if bool(local_evm_pause.get("paused")) else 0,
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

    local_evm_pause = local_evm_balance_probe_pause()
    local_rpc_sources = [(source, url, "evm-rpc") for source, url in local_evm_balance_rpc_urls()]
    rpc_sources = [] if bool(local_evm_pause.get("paused")) else list(local_rpc_sources)
    rpc_sources.extend(
        (source, url, "public-rpc")
        for source, url in named_urls_from_env(
            "BDAG_PUBLIC_RPC_URLS",
            [
                ("blockdag-engineering-rpc", "https://rpc.blockdag.engineering"),
                ("bdagscan-rpc", "https://rpc.bdagscan.com"),
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
            "paused": bool(local_evm_pause.get("paused")),
            "reason": local_evm_pause.get("reason"),
            "skipped_source_count": len(local_rpc_sources) if bool(local_evm_pause.get("paused")) else 0,
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
                ("blockdag-engineering-rpc", "https://rpc.blockdag.engineering"),
                ("bdagscan-rpc", "https://rpc.bdagscan.com"),
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
                "reason": "local EVM balance probes are disabled" if not LOCAL_EVM_BALANCE_PROBE_ENABLED else None,
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

    local_evm_pause = local_evm_balance_probe_pause()
    local_sources = local_evm_balance_rpc_urls()
    latest_sources = evm_reference_rpc_urls() if bool(local_evm_pause.get("paused")) else local_sources
    if not latest_sources:
        source_kind = "reference" if bool(local_evm_pause.get("paused")) else "local"
        return {
            "status": "failed",
            "hours": hours,
            "address": address,
            "local_evm_rpc": {
                "paused": bool(local_evm_pause.get("paused")),
                "reason": local_evm_pause.get("reason"),
            },
            "error": f"no {source_kind} EVM RPC sources",
        }
    latest_source = freshest_evm_rpc_source(latest_sources, timeout=8.0)
    if latest_source is None:
        source_kind = "reference" if bool(local_evm_pause.get("paused")) else "local"
        return {
            "status": "failed",
            "hours": hours,
            "address": address,
            "local_evm_rpc": {
                "paused": bool(local_evm_pause.get("paused")),
                "reason": local_evm_pause.get("reason"),
            },
            "error": f"no {source_kind} EVM RPC returned eth_blockNumber",
        }
    latest_name, latest_url, latest_block, latest_errors = latest_source
    try:
        latest_timestamp = rpc_block_timestamp(latest_url, latest_block)
        target_timestamp = latest_timestamp - (hours * 3600)
        start_block = first_block_at_or_after(latest_url, latest_block, target_timestamp)
        start_timestamp = rpc_block_timestamp(latest_url, start_block)
        latest_balance = json_rpc_balance_at(latest_url, address, latest_block, timeout=8.0)
    except Exception as exc:  # noqa: BLE001
        errors = [*latest_errors, str(exc)]
        return {
            "status": "failed",
            "hours": hours,
            "address": address,
            "source": latest_name,
            "local_evm_rpc": {
                "paused": bool(local_evm_pause.get("paused")),
                "reason": local_evm_pause.get("reason"),
            },
            "error": "; ".join(errors[-3:]),
        }

    start_balance = None
    start_source = None
    start_errors: list[str] = []
    archive_sources = archive_rpc_urls()
    if bool(local_evm_pause.get("paused")):
        archive_sources = filter_local_evm_rpc_urls(archive_sources, local_sources)
    for source, url in archive_sources:
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
        "latest_balance_source": latest_name,
        "start_block": start_block,
        "start_block_time": datetime.fromtimestamp(start_timestamp, timezone.utc).isoformat(),
        "start_balance_source": start_source,
        "transfer_source": transfer_source,
        "local_evm_rpc": {
            "paused": bool(local_evm_pause.get("paused")),
            "reason": local_evm_pause.get("reason"),
            "latest_source_scope": "reference-rpc" if bool(local_evm_pause.get("paused")) else "local-rpc",
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


def fiat_value(amount_bdag: Decimal, price: dict[str, Any], currency: str) -> str | None:
    value = price.get(currency.lower())
    if value is None:
        return None
    try:
        return decimal_to_str(amount_bdag * Decimal(str(value)), places=2)
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
    registry_by_identity = {
        miner_identity_key(item): item
        for item in registry.get("miners", [])
        if miner_identity_key(item)
    }

    def registered_for_activity(item: dict[str, Any]) -> dict[str, Any]:
        identity = str(item.get("identity_key") or "")
        if identity and identity in registry_by_identity:
            return registry_by_identity[identity]
        return registry_by_ip.get(str(item.get("ip") or ""), {})

    active_activity_miners = [
        item
        for item in activity.get("miners", [])
        if not is_retired_miner_identity(
            {**item, **registered_for_activity(item)},
            str(item.get("ip") or ""),
            normalize_mac((registered_for_activity(item) or {}).get("mac")) or normalize_mac(item.get("mac")),
        )
    ]
    earnings_activity_miners = [
        item
        for item in active_activity_miners
        if is_configured_miner_record(registered_for_activity(item))
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
    worker_to_identities: dict[str, set[str]] = {}
    for item in earnings_activity_miners:
        activity_ip = str(item.get("ip") or "")
        registered = registered_for_activity(item)
        activity_mac = normalize_mac((registered or {}).get("mac")) or normalize_mac(item.get("mac"))
        if is_docker_bridge_pool_log_client(activity_ip, activity_mac):
            continue
        identity = str(item.get("identity_key") or miner_identity_key({**registered, **item}))
        if not identity:
            continue
        for worker in item.get("workers", []):
            worker_to_identities.setdefault(str(worker), set()).add(identity)
    total_work = sum(int(item.get("share_work", 0) or 0) for item in earnings_activity_miners)
    total_bdag = wei_to_bdag(credit_totals.get("totals", {}).get("total_wei"))
    recent_bdag = wei_to_bdag(credit_totals.get("recent_1h", {}).get("total_wei"))
    estimates: list[dict[str, Any]] = []
    for item in earnings_activity_miners:
        activity_ip = str(item.get("ip") or "")
        registered = registered_for_activity(item)
        registered_mac = normalize_mac(registered.get("mac")) or normalize_mac(item.get("mac"))
        if is_docker_bridge_pool_log_client(activity_ip, registered_mac):
            continue
        work = int(item.get("share_work", 0) or 0)
        hashrate = hashrate_by_ip.get(activity_ip, {})
        workers = merge_unique_strings(item.get("workers"), registered.get("last_workers"))
        unique_workers = [worker for worker in workers if len(worker_to_identities.get(worker, set())) <= 1]
        shared_workers = [worker for worker in workers if len(worker_to_identities.get(worker, set())) > 1]
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
        configured = is_configured_miner_record(registered)
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
                "device_type": "asic" if hashrate.get("available") else registered.get("device_type") or item.get("device_type") or "stratum",
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
    return dashboard_history_bucket_seconds_for_age(age_seconds)


def compact_miner_estimate_for_history(miner: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "ip",
        "mac",
        "device_id",
        "identity_key",
        "display_name",
        "display_label",
        "device_type",
        "discovered_by",
        "sources",
        "status",
        "managed",
        "configured",
        "connected",
        "pool_active",
        "work_pool_active",
        "expected_work_lane",
        "expected_work_percent",
        "lane_status",
        "workers",
        "credit_workers",
        "shared_workers",
        "credit_scope",
        "earnings_scope",
        "history_source",
        "shares",
        "share_work",
        "work_percent",
        "blocks_found",
        "credited_blocks",
        "credited_bdag_total",
        "credited_bdag_paid",
        "credited_bdag_pending",
        "last_credit_at",
        "last_share_at",
        "active_asics",
        "target_active_asics",
        "connected_miners",
        "hashrate",
        "av_hashrate",
        "hashrate_ghs",
        "av_hashrate_ghs",
        "hashrate_available",
        "hashrate_source",
        "hashrate_error",
        "estimated_bdag_avg_hour",
        "estimated_bdag_1h",
        "estimated_bdag_total",
        "estimated_usd_avg_hour",
        "estimated_usd_1h",
        "estimated_usd_total",
        "estimated_zar_avg_hour",
        "estimated_zar_1h",
        "estimated_zar_total",
        "estimated_wallet_bdag_recent_hour",
        "estimated_wallet_bdag_avg_hour",
        "estimated_wallet_bdag_1h",
        "estimated_wallet_usd_recent_hour",
        "estimated_wallet_usd_avg_hour",
        "estimated_wallet_usd_1h",
        "estimated_wallet_zar_recent_hour",
        "estimated_wallet_zar_avg_hour",
        "estimated_wallet_zar_1h",
        "estimated_wallet_rate_source",
        "estimated_wallet_rate_basis",
        "estimated_wallet_scan_window_hours",
        "estimated_wallet_scan_window_blocks",
        "estimated_wallet_avg_block_seconds",
        "estimated_wallet_global_snapshot_at",
        "estimated_wallet_local_pool_bdag_hour",
        "estimated_wallet_work_share_percent",
    ]
    compacted = {key: miner.get(key) for key in keys if key in miner}
    mac = normalize_mac(compacted.get("mac"))
    identity = str(compacted.get("identity_key") or compacted.get("device_id") or "").strip().lower()
    if not mac and identity.startswith("mac:"):
        mac = normalize_mac(identity[4:])
    if mac:
        compacted["mac"] = mac
        compacted.setdefault("device_id", f"mac:{mac}")
        compacted.setdefault("identity_key", f"mac:{mac}")
    return compacted


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
    compacted = {
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
    if isinstance(snapshot.get("hourly_averages"), dict):
        hourly = snapshot["hourly_averages"]
        compacted["hourly_averages"] = {
            key: hourly.get(key)
            for key in ("wallet_24h_bdag", "wallet_24h_avg_bdag_hour", "wallet_24h_source")
            if key in hourly
        }
    if isinstance(snapshot.get("earnings_24h"), dict):
        earnings_24h = snapshot["earnings_24h"]
        compacted["earnings_24h"] = {
            key: earnings_24h.get(key)
            for key in ("status", "source", "source_truth", "bdag", "usd", "zar", "sample_count", "coverage_hours")
            if key in earnings_24h
        }
    if snapshot.get("history_source"):
        compacted["history_source"] = snapshot.get("history_source")
    if snapshot.get("preserved_asic_history"):
        compacted["preserved_asic_history"] = snapshot.get("preserved_asic_history")
    return compacted


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
                "history_source": "postgres-derived-credits",
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
            "credit_scope": "postgres-derived",
            "shares": None,
            "share_work": None,
            "blocks_found": int(row.get("credit_count", 0) or 0),
            "estimated_bdag_avg_hour": decimal_to_str(rate_bdag),
            "estimated_bdag_1h": decimal_to_str(rate_bdag),
            "estimated_usd_avg_hour": fiat_value(rate_bdag, price, "usd"),
            "estimated_usd_1h": fiat_value(rate_bdag, price, "usd"),
            "estimated_zar_avg_hour": fiat_value(rate_bdag, price, "zar"),
            "estimated_zar_1h": fiat_value(rate_bdag, price, "zar"),
            "history_source": "postgres-derived-credits",
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
            "wallet_bdag": None,
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
    wallet_24h_source = "unavailable"
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
                wallet_24h_source = "wallet-balance-history"
        elif wallet_runtime_hours is not None and wallet_runtime_hours < Decimal("24"):
            wallet_24h_source = "insufficient-wallet-balance-history"

    wallet_net_recent_bdag_hour = wallet_recent_bdag_hour
    wallet_net_24h_bdag = wallet_24h_bdag
    if current_earned_24h_bdag is not None:
        wallet_24h_bdag = current_earned_24h_bdag
        wallet_recent_bdag_hour = current_recent_bdag
        wallet_24h_source = current_earned_24h_source or "postgres-credits-24h"

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
    recent_bdag = decimal_value(onchain_1h.get("earned_bdag")) if onchain_1h.get("status") == "ok" else None
    if recent_bdag is None:
        recent_bdag = Decimal("0")
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
    derived_history = (
        derived_credit_history_for_dashboard(price, miner_estimates)
        if include_history and EARNINGS_DERIVED_HISTORY_RUNTIME_FALLBACK_ENABLED and not history
        else []
    )
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
        current_earned_24h_source=onchain_24h.get("source") if onchain_24h.get("status") == "ok" else None,
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

    chain_rate = latest_local_pool_chain_rate_from_global_cache()
    chain_rate_applied = apply_local_pool_chain_rate_to_miner_estimates(miner_estimates, chain_rate, price)
    history_for_response = (
        apply_local_pool_chain_rates_to_earnings_history(history, price)
        if include_history
        else []
    )

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
            "status": onchain_24h.get("status"),
            "source": onchain_24h.get("source") if onchain_24h.get("status") == "ok" else "on-chain-unavailable",
            "source_truth": "on-chain balance window reconciled with native transfers",
            "fallback_used": False,
            "bdag": decimal_to_str(recent_24h_bdag) if recent_24h_bdag is not None else None,
            "usd": fiat_value(recent_24h_bdag, price, "usd") if recent_24h_bdag is not None else None,
            "zar": fiat_value(recent_24h_bdag, price, "zar") if recent_24h_bdag is not None else None,
            "credit_count": credits.get("recent_24h", {}).get("credit_count"),
            "first_credit_at": credits.get("recent_24h", {}).get("first_credit_at"),
            "last_credit_at": credits.get("recent_24h", {}).get("last_credit_at"),
            "onchain_reconciliation": onchain_24h,
            "db_credit_fallback_bdag": decimal_to_str(db_recent_24h_bdag),
            "db_credit_diagnostic_bdag": decimal_to_str(db_recent_24h_bdag),
        },
        "credit_balance_check": credit_balance_check,
        "hourly_averages": hourly_averages,
        "asic_allocation_rate_source": "chain-confirmed-local-pool-global-scan" if chain_rate_applied else "wallet-credit-hourly-estimate",
        "asic_allocation_rate_basis": "local_pool_bdag_per_hour_allocated_by_live_share_work" if chain_rate_applied else "wallet-credit-hourly-estimate",
        "asic_allocation_chain_rate": {
            "applied": bool(chain_rate_applied),
            "local_pool_bdag_hour": decimal_to_str(decimal_value(chain_rate.get("bdag_hour")) or Decimal("0")) if chain_rate else None,
            "snapshot_at": chain_rate.get("snapshot_at") if chain_rate else None,
            "scan_window_hours": chain_rate.get("scan_window_hours") if chain_rate else None,
            "scan_window_blocks": chain_rate.get("scan_window_blocks") if chain_rate else None,
            "avg_block_seconds": chain_rate.get("avg_block_seconds") if chain_rate else None,
            "cache_source": chain_rate.get("cache_source") if chain_rate else None,
        },
        "miner_estimates": miner_estimates,
        "total_usd": fiat_value(total_bdag, price, "usd"),
        "total_zar": fiat_value(total_bdag, price, "zar"),
        "wallet_24h_usd": fiat_value(decimal_value(hourly_averages.get("wallet_24h_bdag")) or Decimal("0"), price, "usd") if hourly_averages.get("wallet_24h_bdag") is not None else None,
        "wallet_24h_zar": fiat_value(decimal_value(hourly_averages.get("wallet_24h_bdag")) or Decimal("0"), price, "zar") if hourly_averages.get("wallet_24h_bdag") is not None else None,
        "wallet_total_usd": fiat_value(wallet_bdag, price, "usd") if wallet_bdag is not None else None,
        "wallet_total_zar": fiat_value(wallet_bdag, price, "zar") if wallet_bdag is not None else None,
        "snapshot_log": str(EARNINGS_SNAPSHOT_FILE),
        "history": history_for_response,
        "history_sample_count": history_sample_count,
        "history_derived_sample_count": len(derived_history),
        "history_total_sample_count": len(history_for_response),
        "history_derivation_source": "postgres credits" if derived_history else "",
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
    global _DOCKER_USE_SUDO_CACHE
    effective_command = command
    if command_uses_docker(command) and docker_sudo_fallback_enabled() and (
        docker_use_sudo_requested() or _DOCKER_USE_SUDO_CACHE is True
    ):
        effective_command = sudo_docker_command(command)
    start = time.time()
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n[{now_iso()}] $ {' '.join(effective_command)}\n")
        log.flush()
        try:
            proc = subprocess.run(
                effective_command,
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
        if (
            command_uses_docker(command)
            and effective_command == command
            and code != 0
            and docker_sudo_fallback_enabled()
        ):
            fallback_command = sudo_docker_command(command)
            log.write(f"\n[{now_iso()}] retry with sudo fallback: {' '.join(fallback_command)}\n")
            log.flush()
            try:
                proc = subprocess.run(
                    fallback_command,
                    cwd=PROJECT_ROOT,
                    text=True,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=timeout,
                    check=False,
                )
                fallback_code = proc.returncode
            except subprocess.TimeoutExpired:
                fallback_code = 124
                log.write(f"\n[{now_iso()}] sudo fallback timed out after {timeout}s\n")
            if fallback_code == 0:
                _DOCKER_USE_SUDO_CACHE = True
                effective_command = fallback_command
                code = fallback_code
        elapsed = round(time.time() - start, 3)
        log.write(f"\n[{now_iso()}] exit={code} elapsed={elapsed}s\n")
    return CommandResult(command=effective_command, returncode=code, stdout="", stderr="", elapsed=elapsed)


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

    steps = [configured_command("BDAG_STOP_COMMAND", docker_compose_command("stop"))]
    for step in steps:
        if not step:
            continue
        result = run_logged(step, log_path, timeout=120)
        if not result.ok:
            return False

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for node_dir in NODE_DATA_DIRS:
        backup_node_dir(node_dir, log_path)

    restore_command = configured_command("BDAG_RESTORE_NODE_COMMAND", [])
    if not restore_command:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(
                f"[{now_iso()}] clean restore blocked: BDAG_RESTORE_NODE_COMMAND is not configured. "
                "Configure an explicit verified IPFS/rawdatadir restore command before destructive restore.\n"
            )
        return False

    for step in (
        restore_command,
        gated_stack_start_command(log_path),
    ):
        if not step:
            continue
        result = run_logged(step, log_path, timeout=1800)
        if not result.ok:
            return False
    return True


def gated_stack_start_command(log_path: Path) -> list[str]:
    status = pool_start_gate.read_latest_status_payload()
    decision = pool_start_gate.pool_start_decision(status)
    if decision.allowed:
        return configured_command("BDAG_START_COMMAND", docker_compose_start_command(include_pool=True))

    configured = configured_command("BDAG_START_COMMAND", [])
    if configured:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(
                f"[{now_iso()}] configured BDAG_START_COMMAND suppressed because pool start is unsafe: "
                f"{decision.reason}\n"
            )
    command = docker_compose_start_command(include_pool=False)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"[{now_iso()}] starting non-pool stack services only: {decision.reason}\n")
    return command


def start_stack(log_path: Path) -> bool:
    command = gated_stack_start_command(log_path)
    if not command:
        return False
    ok = run_logged(command, log_path, timeout=180).ok
    return ok and stop_planned_sync_service(log_path)


def restart_stack(log_path: Path) -> bool:
    stop_command = configured_command("BDAG_STOP_COMMAND", docker_compose_command("stop"))
    start_command = gated_stack_start_command(log_path)
    down = run_logged(stop_command, log_path, timeout=180) if stop_command else CommandResult(stop_command, 0, "", "", 0)
    up = run_logged(start_command, log_path, timeout=180) if start_command else CommandResult(start_command, 1, "", "", 0)
    return down.ok and up.ok and stop_planned_sync_service(log_path)


def stop_planned_sync_service(log_path: Path) -> bool:
    return True


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
        "- confirm pool, node, postgres, and dashboard containers are running",
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

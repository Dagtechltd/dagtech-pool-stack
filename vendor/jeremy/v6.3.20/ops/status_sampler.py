#!/usr/bin/env python3
"""Write a shared atomic BlockDAG stack status sample for local agents."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import automation_control
from incident_journal import append_incident
import pool_start_gate
from pool_ops import (
    EARNINGS_SNAPSHOT_EXPECTED_INTERVAL_SECONDS,
    LOG_DIR,
    POOL_ACTIVITY_BOOTSTRAP_LOG_LINES,
    POOL_CONTAINER,
    POOL_ENV_FILE,
    PROJECT_ROOT,
    RUNTIME_DIR,
    STATUS_SAMPLER_FILE,
    collect_pool_activity,
    collect_status_cached,
    detect_total_memory_bytes,
    compose_service_name,
    docker_compose_command,
    ensure_runtime,
    env_bool,
    now_iso,
    pool_asic_mac_override_diagnostics,
    pool_asic_mac_overrides_value,
    read_env_file_value,
    read_miner_registry,
    read_neighbor_macs,
    read_latest_earnings_snapshot_info,
    record_earnings_snapshot,
    run,
    save_miner_registry,
    split_env_list,
    upsert_pool_activity_miners,
    write_json_file,
    write_status_sampler_payload,
)


def env_float(name: str, default: float, minimum: float | None = None) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def env_int(name: str, default: int, minimum: int | None = None) -> int:
    try:
        value = int(float(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


DEFAULT_INTERVAL_SECONDS = env_float("BDAG_STATUS_SAMPLER_INTERVAL_SECONDS", 10.0, minimum=1.0)
DEFAULT_EARNINGS_SNAPSHOT_INTERVAL_SECONDS = env_float(
    "BDAG_STATUS_SAMPLER_EARNINGS_SNAPSHOT_INTERVAL_SECONDS",
    float(EARNINGS_SNAPSHOT_EXPECTED_INTERVAL_SECONDS),
    minimum=0.0,
)
MINING_IMPERATIVE_REPAIR_ENABLED = env_bool("BDAG_MINING_IMPERATIVE_REPAIR_ENABLED", True)
MINING_IMPERATIVE_REPAIR_INTERVAL_SECONDS = env_float(
    "BDAG_MINING_IMPERATIVE_REPAIR_INTERVAL_SECONDS",
    30.0,
    minimum=5.0,
)
MINING_IMPERATIVE_GUARD_UNITS = split_env_list(
    "BDAG_MINING_IMPERATIVE_GUARD_UNITS",
    "bdag-stack-sentinel.timer,bdag-watchdog.service",
)
MINING_IMPERATIVE_START_POOL_ENABLED = env_bool("BDAG_MINING_IMPERATIVE_START_POOL_ENABLED", True)
MINING_IMPERATIVE_START_IDLE_SYNCED_POOL = env_bool("BDAG_MINING_IMPERATIVE_START_IDLE_SYNCED_POOL", False)
MINING_IMPERATIVE_POOL_START_STABLE_SAFE_SECONDS = env_int(
    "BDAG_MINING_IMPERATIVE_POOL_START_STABLE_SAFE_SECONDS",
    90,
    minimum=0,
)
POOL_START_STABILITY_FILE = STATUS_SAMPLER_FILE.parent / "mining-imperative-pool-start-stability.json"
MINING_IMPERATIVE_MINER_TRACKING_REPAIR_ENABLED = env_bool(
    "BDAG_MINING_IMPERATIVE_MINER_TRACKING_REPAIR_ENABLED",
    True,
)
MINING_IMPERATIVE_MINER_ACTIVITY_REPAIR_ENABLED = env_bool(
    "BDAG_MINING_IMPERATIVE_MINER_ACTIVITY_REPAIR_ENABLED",
    True,
)
MINING_IMPERATIVE_ASIC_MAC_OVERRIDES_REPAIR_ENABLED = env_bool(
    "BDAG_MINING_IMPERATIVE_ASIC_MAC_OVERRIDES_REPAIR_ENABLED",
    True,
)
MINING_IMPERATIVE_MINER_ACTIVITY_STALE_SECONDS = env_int(
    "BDAG_MINING_IMPERATIVE_MINER_ACTIVITY_STALE_SECONDS",
    180,
    minimum=30,
)
MINING_IMPERATIVE_NODE_MINING_REPAIR_ENABLED = env_bool(
    "BDAG_MINING_IMPERATIVE_NODE_MINING_REPAIR_ENABLED",
    True,
)
MINING_IMPERATIVE_NODE_COMMAND_LINE_REPAIR_ENABLED = env_bool(
    "BDAG_MINING_IMPERATIVE_NODE_COMMAND_LINE_REPAIR_ENABLED",
    False,
)
MINING_IMPERATIVE_CHAIN_STATE_RESTORE_ENABLED = env_bool(
    "BDAG_MINING_IMPERATIVE_CHAIN_STATE_RESTORE_ENABLED",
    True,
)
CHAIN_STATE_SELF_HEAL_UNIT = os.environ.get(
    "BDAG_CHAIN_STATE_SELF_HEAL_UNIT",
    "bdag-chain-state-self-heal.service",
).strip()
CHAIN_STATE_IMPORT_WATCH_FILE = STATUS_SAMPLER_FILE.parent / "chain-state-import-watch.json"
CHAIN_STATE_MISSING_TRIE_RESTORE_WARNINGS = env_int(
    "BDAG_CHAIN_STATE_MISSING_TRIE_RESTORE_WARNINGS",
    3,
    minimum=1,
)
CHAIN_STATE_ACTIVE_MINING_DEFER_SECONDS = env_int(
    "BDAG_CHAIN_STATE_ACTIVE_MINING_DEFER_SECONDS",
    180,
    minimum=0,
)
CHAIN_STATE_STALLED_IMPORT_RESTORE_ENABLED = env_bool(
    "BDAG_CHAIN_STATE_STALLED_IMPORT_RESTORE_ENABLED",
    True,
)
CHAIN_STATE_STALLED_IMPORT_RESTORE_SECONDS = env_int(
    "BDAG_CHAIN_STATE_STALLED_IMPORT_RESTORE_SECONDS",
    900,
    minimum=60,
)
CHAIN_STATE_STALLED_IMPORT_RESTORE_PEER_AHEAD_BLOCKS = env_int(
    "BDAG_CHAIN_STATE_STALLED_IMPORT_RESTORE_PEER_AHEAD_BLOCKS",
    1000,
    minimum=1,
)
CHAIN_STATE_STALLED_IMPORT_RESTORE_GAP_GROWTH_BLOCKS = env_int(
    "BDAG_CHAIN_STATE_STALLED_IMPORT_RESTORE_GAP_GROWTH_BLOCKS",
    60,
    minimum=0,
)
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
NODE_MINING_REQUIRED_BOOL_FLAGS = ("--miner",)
NODE_MINING_UNSAFE_BYPASS_FLAGS = (
    "--allowminingwhennearlysynced",
    "--allowsubmitwhennotsynced",
)
NODE_MINING_CONSTRAINED_ASSIGNMENTS = {
    "--maxinbound": "1",
}
CATCHUP_PAUSE_ENABLED = env_bool("BDAG_CATCHUP_PAUSE_ENABLED", True)
CATCHUP_PAUSE_THRESHOLD_BLOCKS = env_int("BDAG_CATCHUP_PAUSE_THRESHOLD_BLOCKS", 300, minimum=1)
CATCHUP_NODE_RECREATE_ENABLED = env_bool("BDAG_CATCHUP_NODE_RECREATE_ENABLED", True)
CATCHUP_NODE_CACHE_MB = env_int("BDAG_CATCHUP_NODE_CACHE_MB", 1024, minimum=0)
CATCHUP_NODE_CACHE_MIN_MB = env_int("BDAG_CATCHUP_NODE_CACHE_MIN_MB", 512, minimum=256)
CATCHUP_NODE_CACHE_MEMORY_PERCENT = env_float("BDAG_CATCHUP_NODE_CACHE_MEMORY_PERCENT", 15.0, minimum=5.0)
CATCHUP_IO_PRESSURE_PAUSE_ENABLED = env_bool("BDAG_CATCHUP_IO_PRESSURE_PAUSE_ENABLED", True)
CATCHUP_IO_PRESSURE_MIN_LAG_BLOCKS = env_int("BDAG_CATCHUP_IO_PRESSURE_MIN_LAG_BLOCKS", 25, minimum=1)
CATCHUP_IOWAIT_WARN_PERCENT = env_float("BDAG_CATCHUP_IOWAIT_WARN_PERCENT", 15.0, minimum=0.0)
CATCHUP_IO_SOME_AVG10_WARN = env_float("BDAG_CATCHUP_IO_SOME_AVG10_WARN", 20.0, minimum=0.0)
CATCHUP_IO_FULL_AVG10_WARN = env_float("BDAG_CATCHUP_IO_FULL_AVG10_WARN", 10.0, minimum=0.0)
LOG_FILE = LOG_DIR / "status-sampler.log"


def log(message: str) -> None:
    ensure_runtime()
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now_iso()}] {message}\n")


def record_incident(
    event_type: str,
    severity: str,
    message: str,
    details: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    try:
        append_incident(
            event_type,
            severity,
            "status-sampler",
            message,
            details,
            status=payload,
            action=details,
        )
    except Exception as exc:  # noqa: BLE001 - repair must not fail because incident logging failed.
        log(f"mining imperative incident logging failed event={event_type} error={exc}")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def config_value(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is not None:
        return value
    for path in (POOL_ENV_FILE, PROJECT_ROOT / ".env"):
        try:
            file_value = read_env_file_value(path, name)
        except OSError:
            file_value = None
        if file_value is not None:
            return file_value
    return default


def set_env_file_value(path: Any, key: str, value: str) -> bool:
    env_path = path if hasattr(path, "read_text") else PROJECT_ROOT / str(path)
    if not env_path.exists():
        return False
    lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
    changed = False
    found = False
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        prefix = "export " if stripped.startswith("export ") else ""
        assignment = stripped[7:].strip() if prefix else stripped
        if assignment.startswith(f"{key}="):
            found = True
            replacement = f"{prefix}{key}={value}" if prefix else f"{key}={value}"
            output.append(replacement)
            changed = changed or line != replacement
        else:
            output.append(line)
    if not found:
        output.append(f"{key}={value}")
        changed = True
    if not changed:
        return False
    tmp = env_path.with_suffix(env_path.suffix + ".tmp")
    tmp.write_text("\n".join(output) + "\n", encoding="utf-8")
    os.replace(tmp, env_path)
    return True


def set_runtime_env_value(key: str, value: str) -> list[str]:
    changed_paths: list[str] = []
    seen: set[Any] = set()
    ops_env_file = Path(os.environ.get("BDAG_OPS_ENV_FILE") or RUNTIME_DIR / "ops.env")
    if not ops_env_file.is_absolute():
        ops_env_file = PROJECT_ROOT / ops_env_file
    for path in (PROJECT_ROOT / ".env", POOL_ENV_FILE, ops_env_file):
        if path in seen:
            continue
        seen.add(path)
        if set_env_file_value(path, key, value):
            changed_paths.append(str(path))
    os.environ[key] = value
    return changed_paths


def env_enabled_value(value: str | None, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def configured_mining_address() -> str:
    for key in ("POOL_COINBASE_ADDRESS", "MINING_POOL_ADDRESS", "MINING_ADDRESS"):
        value = config_value(key).strip()
        if value:
            return value
    return ""


def valid_mining_address(address: str) -> bool:
    return bool(re.fullmatch(r"0x[a-fA-F0-9]{40}", address or "")) and address.lower() != ZERO_ADDRESS


def node_args_have_mining_address(args: str, address: str) -> bool:
    address_lower = address.lower()
    for word in args.replace("'", " ").replace('"', " ").split():
        if word.startswith("--miningaddr=") and word.split("=", 1)[1].lower() == address_lower:
            return True
    return False


def node_args_words(args: str) -> list[str]:
    words: list[str] = []
    for word in args.replace("'", " ").replace('"', " ").split():
        if not word:
            continue
        if word.startswith("--node-args="):
            embedded = word.split("=", 1)[1].strip()
            if embedded:
                words.append(embedded)
            continue
        words.append(word)
    return words


def node_args_have_bool_flag(args: str, flag: str) -> bool:
    for word in node_args_words(args):
        if word == flag:
            return True
        if word.startswith(f"{flag}="):
            return word.split("=", 1)[1].strip().lower() not in {"0", "false", "no", "off"}
    return False


def node_args_assignment_value(args: str, flag: str) -> str | None:
    for word in node_args_words(args):
        if word.startswith(f"{flag}="):
            return word.split("=", 1)[1].strip()
    return None


def node_mining_runtime_args(address: str) -> str:
    parts = [
        *NODE_MINING_REQUIRED_BOOL_FLAGS,
        f"--miningaddr={address}",
    ]
    existing_args = config_value("BDAG_NODE_MINING_ARGS") or config_value("NODE_ARGS_APPEND")
    existing_maxinbound = node_args_assignment_value(existing_args, "--maxinbound")
    if constrained_storage_profile() or existing_maxinbound is not None:
        # A USB-backed ASIC router should mine and relay blocks, not serve as a
        # catch-up source for other peers while it is trying to convert shares
        # into accepted blocks. Keep one inbound slot because this node build
        # treats a zero inbound budget as an unusable P2P server.
        for key, value in NODE_MINING_CONSTRAINED_ASSIGNMENTS.items():
            wanted = existing_maxinbound if key == "--maxinbound" and existing_maxinbound is not None else value
            parts.append(f"{key}={wanted}")
    return " ".join(parts)


def node_mining_args_are_safe_and_complete(args: str, address: str) -> bool:
    if not node_args_have_mining_address(args, address):
        return False
    for flag in NODE_MINING_UNSAFE_BYPASS_FLAGS:
        if node_args_have_bool_flag(args, flag):
            return False
    for flag in NODE_MINING_REQUIRED_BOOL_FLAGS:
        if not node_args_have_bool_flag(args, flag):
            return False
    if constrained_storage_profile():
        for flag, wanted in NODE_MINING_CONSTRAINED_ASSIGNMENTS.items():
            if node_args_assignment_value(args, flag) != wanted:
                return False
    return True


def mining_imperative_enabled() -> bool:
    return MINING_IMPERATIVE_REPAIR_ENABLED and env_bool("BDAG_MINING_IMPERATIVE_REPAIR_ENABLED", True)


def systemctl_user(*args: str):
    return run(["systemctl", "--user", *args], timeout=30)


def ensure_user_unit(unit: str, payload: dict[str, Any]) -> bool:
    if not unit:
        return False
    enabled = systemctl_user("is-enabled", unit)
    active = systemctl_user("is-active", unit)
    enabled_text = enabled.stdout.strip()
    active_text = active.stdout.strip()
    if enabled.ok and enabled_text in {"enabled", "static", "generated", "linked"} and active.ok and active_text == "active":
        return False

    action = ["enable", "--now", unit] if not enabled.ok or enabled_text in {"", "disabled", "indirect"} else ["start", unit]
    if not automation_repair_mutation_allowed(
        automation_control.ACTION_SYSTEMD_START,
        target=unit,
        reason=f"repair mining guard unit with systemctl --user {' '.join(action)}",
        payload=payload,
        event_type="mining_imperative_user_unit_start_blocked",
        message=f"Mining imperative left {unit} unchanged because automation control blocked systemd start",
        severity="warning",
    ):
        return False
    result = systemctl_user(*action)
    details = {
        "unit": unit,
        "action": " ".join(action),
        "enabled_before": enabled_text,
        "active_before": active_text,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }
    if result.ok:
        log(
            "mining imperative repaired user unit "
            f"unit={unit} action={' '.join(action)} enabled_before={enabled_text} active_before={active_text}"
        )
        record_incident(
            "mining_imperative_user_unit_repaired",
            "warning",
            f"Mining imperative guard repaired {unit}",
            details,
            payload,
        )
        return True
    log(f"mining imperative could not repair user unit unit={unit} rc={result.returncode} stderr={result.stderr.strip()}")
    record_incident(
        "mining_imperative_user_unit_repair_failed",
        "critical",
        f"Mining imperative guard could not repair {unit}",
        details,
        payload,
    )
    return False


def chain_ready_for_mining(payload: dict[str, Any]) -> bool:
    sync = dict_value(payload.get("sync_progress"))
    if str(sync.get("status") or "").lower() == "synced":
        return True
    remaining = sync.get("remaining_blocks")
    if remaining is not None and safe_int(remaining, 1) <= 0 and sync.get("chain_block_count") is not None:
        return True
    return payload.get("overall") == "ok" and not payload.get("sync_warnings")


def canonical_safety_payloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []

    def append(value: Any) -> None:
        if isinstance(value, dict):
            payloads.append(value)

    append(payload.get("canonical_mining_safety"))
    sync_health = dict_value(payload.get("sync_health"))
    append(sync_health.get("canonical_mining_safety"))
    sync = dict_value(payload.get("sync_progress"))
    append(sync.get("canonical_mining_safety"))
    for node in dict_value(sync.get("nodes")).values():
        if isinstance(node, dict):
            append(node.get("canonical_mining_safety"))
    for node in dict_value(payload.get("nodes")).values():
        if isinstance(node, dict):
            append(node.get("canonical_mining_safety"))
    return payloads


def public_chain_divergence_reasons(payload: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    sync_health = dict_value(payload.get("sync_health"))
    if sync_health.get("public_chain_divergence"):
        reasons.append("sync health reports public-chain divergence")
    divergence_nodes = sync_health.get("public_chain_divergence_nodes")
    if isinstance(divergence_nodes, dict) and divergence_nodes:
        reasons.append(f"public-chain divergence nodes={','.join(str(key) for key in divergence_nodes)}")
    elif isinstance(divergence_nodes, list) and divergence_nodes:
        reasons.append(f"public-chain divergence nodes={','.join(str(item) for item in divergence_nodes)}")

    sync = dict_value(payload.get("sync_progress"))
    if sync.get("public_chain_diverged"):
        reasons.append("sync progress reports public-chain divergence")
    if sync.get("solo_mining_suspected"):
        reasons.append("sync progress reports solo-mining suspicion")
    for name, node in dict_value(sync.get("nodes")).items():
        if not isinstance(node, dict):
            continue
        if node.get("public_chain_diverged"):
            reasons.append(f"{name} reports public-chain divergence")
        if node.get("solo_mining_suspected"):
            reasons.append(f"{name} reports solo-mining suspicion")
    for name, node in dict_value(payload.get("nodes")).items():
        if not isinstance(node, dict):
            continue
        if node.get("public_chain_diverged"):
            reasons.append(f"{name} reports public-chain divergence")
        if node.get("solo_mining_suspected"):
            reasons.append(f"{name} reports solo-mining suspicion")

    for safety in canonical_safety_payloads(payload):
        if safety.get("safe") is True:
            continue
        reason = str(safety.get("reason") or safety.get("status") or "").lower()
        if any(token in reason for token in ("public-chain", "diverg", "solo")):
            reasons.append(f"canonical mining safety proof is unsafe: {safety.get('reason') or safety.get('status')}")
    return sorted(set(str(item) for item in reasons if item))


def catchup_lag_blocks(payload: dict[str, Any]) -> int:
    values: list[int] = []
    policy = dict_value(payload.get("catchup_policy"))
    policy_lag = safe_int(policy.get("lag_blocks"), -1)
    if policy_lag >= 0:
        values.append(policy_lag)

    sync = dict_value(payload.get("sync_progress"))
    for key in ("remaining_blocks", "peer_ahead_blocks"):
        value = safe_int(sync.get(key), -1)
        if value >= 0:
            values.append(value)
    for info in dict_value(sync.get("nodes")).values():
        if not isinstance(info, dict):
            continue
        for key in ("remaining_blocks", "peer_ahead_blocks"):
            value = safe_int(info.get(key), -1)
            if value >= 0:
                values.append(value)

    for info in dict_value(payload.get("nodes")).values():
        if not isinstance(info, dict):
            continue
        value = safe_int(info.get("peer_ahead_blocks"), -1)
        if value >= 0:
            values.append(value)

    selected_health = dict_value(
        dict_value(payload.get("pool_metrics")).get("selected_backend_source_health")
    ) or dict_value(dict_value(payload.get("pool")).get("selected_backend_source_health"))
    value = safe_int(selected_health.get("node_p2p_best_peer_lead_blocks"), -1)
    if value >= 0:
        values.append(value)
    return max(values) if values else 0


def catchup_io_pressure_reasons(payload: dict[str, Any]) -> list[str]:
    host_pressure = dict_value(payload.get("host_pressure"))
    reasons: list[str] = []
    iowait = safe_float(host_pressure.get("iowait_percent")) if host_pressure.get("iowait_percent") is not None else None
    io_some = safe_float(host_pressure.get("io_some_avg10")) if host_pressure.get("io_some_avg10") is not None else None
    io_full = safe_float(host_pressure.get("io_full_avg10")) if host_pressure.get("io_full_avg10") is not None else None
    memory_available = (
        safe_float(host_pressure.get("memory_available_percent"))
        if host_pressure.get("memory_available_percent") is not None
        else None
    )
    memory_available_warn = safe_float(host_pressure.get("memory_available_warn_percent"), 0.0) or 0.0
    swap_used = safe_float(host_pressure.get("swap_used_percent")) if host_pressure.get("swap_used_percent") is not None else None
    swap_used_warn = safe_float(host_pressure.get("swap_used_warn_percent"), 0.0) or 0.0
    if bool(host_pressure.get("iowait_warning_active")):
        reasons.append("sustained_iowait_warning")
    if iowait is not None and iowait >= CATCHUP_IOWAIT_WARN_PERCENT:
        reasons.append(f"iowait_percent={iowait:.2f}>={CATCHUP_IOWAIT_WARN_PERCENT:.2f}")
    if io_some is not None and io_some >= CATCHUP_IO_SOME_AVG10_WARN:
        reasons.append(f"io_some_avg10={io_some:.2f}>={CATCHUP_IO_SOME_AVG10_WARN:.2f}")
    if io_full is not None and io_full >= CATCHUP_IO_FULL_AVG10_WARN:
        reasons.append(f"io_full_avg10={io_full:.2f}>={CATCHUP_IO_FULL_AVG10_WARN:.2f}")
    if bool(host_pressure.get("memory_warning_active")):
        reasons.append("memory_available_warning")
    elif memory_available is not None and memory_available_warn > 0 and memory_available <= memory_available_warn:
        reasons.append(f"memory_available_percent={memory_available:.2f}<={memory_available_warn:.2f}")
    if bool(host_pressure.get("swap_warning_active")):
        reasons.append("swap_used_warning")
    elif swap_used is not None and swap_used_warn > 0 and swap_used >= swap_used_warn:
        reasons.append(f"swap_used_percent={swap_used:.2f}>={swap_used_warn:.2f}")
    return reasons


def catchup_policy_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    policy = dict_value(payload.get("catchup_policy"))
    sync_health = dict_value(payload.get("sync_health"))
    sync_progress = dict_value(payload.get("sync_progress"))
    threshold = safe_int(policy.get("threshold_blocks"), CATCHUP_PAUSE_THRESHOLD_BLOCKS)
    lag = catchup_lag_blocks(payload)
    io_pressure_reasons = policy.get("io_pressure_reasons")
    if not isinstance(io_pressure_reasons, list):
        io_pressure_reasons = catchup_io_pressure_reasons(payload)
    io_pressure_enabled = bool(policy.get("io_pressure_pause_enabled", CATCHUP_IO_PRESSURE_PAUSE_ENABLED))
    io_min_lag = safe_int(policy.get("io_pressure_min_lag_blocks"), CATCHUP_IO_PRESSURE_MIN_LAG_BLOCKS)
    mining_ready = bool(policy.get("mining_ready", payload.get("can_mine") is True))
    sync_health = dict_value(payload.get("sync_health"))
    pool = dict_value(payload.get("pool"))
    pool_has_recent_paid_work = bool(
        sync_health.get("pool_has_recent_paid_work")
        or sync_health.get("pool_has_recent_mining")
        or (
            safe_int(pool.get("block_submit_success_count"), 0) > 0
            and pool.get("last_block_submit_age_seconds") is not None
            and safe_int(pool.get("last_block_submit_age_seconds"), 999999) <= 60
        )
        or bool(policy.get("pool_has_recent_paid_work"))
    )
    sync_error_text = str(sync_progress.get("error") or sync_progress.get("chain_rpc_error") or "").lower()
    node_readiness_unavailable = bool(
        sync_health.get("node_readiness_unavailable")
        or sync_health.get("chain_rpc_unavailable")
        or sync_health.get("template_probe_unavailable")
        or str(sync_progress.get("source") or "") == "nodes:readiness-unavailable"
        or "readiness unavailable" in sync_error_text
    )
    backend_unready_under_pressure = bool(
        policy.get("backend_unready_under_pressure")
        or (io_pressure_reasons and not mining_ready and payload.get("can_mine") is False)
    )
    if pool_has_recent_paid_work:
        backend_unready_under_pressure = False
    io_pressure_lag_active = lag >= io_min_lag
    io_pressure_candidate_active = bool(
        io_pressure_enabled
        and io_pressure_reasons
        and not mining_ready
        and io_pressure_lag_active
    )
    io_pressure_active = bool(io_pressure_candidate_active and not pool_has_recent_paid_work)
    lag_threshold_active = bool(lag > threshold and (not chain_ready_for_mining(payload) or not mining_ready))
    node_sync_busy = bool(
        policy.get("node_sync_busy")
        or sync_health.get("node_busy_syncing")
        or sync_health.get("node_importing")
        or sync_progress.get("status") == "syncing"
    )
    node_readiness_candidate_active = bool(
        sync_progress.get("status") == "syncing"
        and node_readiness_unavailable
        and not mining_ready
    )
    backend_sync_candidate_active = bool(
        (policy.get("backend_sync_active") or (sync_progress.get("status") == "syncing" and node_sync_busy))
        and not node_readiness_candidate_active
        and not mining_ready
    )
    pause_candidate_active = bool(
        io_pressure_candidate_active
        or lag_threshold_active
        or node_readiness_candidate_active
        or backend_sync_candidate_active
    )
    recent_paid_work_suppressed = bool(pool_has_recent_paid_work and pause_candidate_active)
    active = bool(CATCHUP_PAUSE_ENABLED and pause_candidate_active and not recent_paid_work_suppressed)
    trigger = str(policy.get("trigger") or "")
    if active and node_readiness_candidate_active and trigger in {"", "backend_syncing"} and lag <= 0:
        trigger = "node_readiness_unavailable"
    elif not trigger and active:
        trigger = (
            "io_pressure"
            if io_pressure_active
            else (
                "lag_threshold"
                if lag_threshold_active
                else ("node_readiness_unavailable" if node_readiness_candidate_active else "backend_syncing")
            )
        )
    if not active:
        trigger = ""
    return {
        **policy,
        "enabled": bool(policy.get("enabled", CATCHUP_PAUSE_ENABLED)),
        "active": active,
        "trigger": trigger,
        "lag_blocks": lag,
        "threshold_blocks": threshold,
        "io_pressure_pause_enabled": io_pressure_enabled,
        "io_pressure_active": io_pressure_active,
        "io_pressure_reasons": io_pressure_reasons,
        "io_pressure_min_lag_blocks": io_min_lag,
        "backend_unready_under_pressure": backend_unready_under_pressure,
        "backend_sync_active": bool(backend_sync_candidate_active and not pool_has_recent_paid_work),
        "node_readiness_unavailable": node_readiness_unavailable,
        "node_readiness_candidate_active": bool(node_readiness_candidate_active and not pool_has_recent_paid_work),
        "node_sync_busy": node_sync_busy,
        "pool_has_recent_paid_work": pool_has_recent_paid_work,
        "lag_threshold_active": lag_threshold_active,
        "mining_ready": mining_ready,
        "recent_paid_work_suppressed": recent_paid_work_suppressed,
    }


def catchup_pause_active(payload: dict[str, Any]) -> bool:
    return bool(catchup_policy_from_payload(payload).get("active"))


def catchup_policy_containment(policy: dict[str, Any]) -> str:
    if policy.get("trigger") == "node_readiness_unavailable":
        return "node_readiness_unavailable"
    return "catchup_pause"


def catchup_policy_stop_reason(policy: dict[str, Any], payload: dict[str, Any]) -> str:
    summary = str(policy.get("summary") or "").strip()
    if summary:
        return summary

    trigger = str(policy.get("trigger") or "")
    lag = safe_int(policy.get("lag_blocks"), -1)
    threshold = safe_int(policy.get("threshold_blocks"), CATCHUP_PAUSE_THRESHOLD_BLOCKS)
    sync_health = dict_value(payload.get("sync_health"))

    if trigger == "node_readiness_unavailable":
        unavailable = []
        if sync_health.get("chain_rpc_unavailable"):
            unavailable.append("chain RPC")
        if sync_health.get("template_probe_unavailable"):
            unavailable.append("template probe")
        detail = "/".join(unavailable) if unavailable else "chain RPC/template"
        return f"node {detail} readiness unavailable; mining work is intentionally paused"

    if trigger == "io_pressure":
        if lag > 0:
            return f"node is I/O-bound while {lag} blocks behind peers"
        return "backend is not ready while the host is I/O-bound"

    if trigger == "lag_threshold" and lag > 0:
        return f"node is {lag} blocks behind peers (pause threshold {threshold})"

    if trigger == "backend_syncing":
        if lag > 0:
            return f"node is {lag} blocks behind peers (pause threshold {threshold})"
        return "node is importing or busy syncing while mining templates are not ready"

    if lag > 0:
        return f"node is {lag} blocks behind peers (pause threshold {threshold})"
    return "chain node is not ready for mining templates"


def catchup_policy_allows_node_runtime_adjustment(policy: dict[str, Any]) -> bool:
    if policy.get("trigger") == "node_readiness_unavailable":
        return safe_int(policy.get("lag_blocks"), 0) > 0 and bool(policy.get("node_sync_busy"))
    return True


def chain_state_reason_is_missing_trie(reason: Any) -> bool:
    text = str(reason or "").lower()
    return "missing-trie" in text or "missing trie" in text or "missing_trie" in text


def chain_state_restore_reason_details(payload: dict[str, Any]) -> list[dict[str, str]]:
    details: list[dict[str, str]] = []
    defer_missing_trie = status_payload_has_recent_paid_work(payload, CHAIN_STATE_ACTIVE_MINING_DEFER_SECONDS)
    sync_health = dict_value(payload.get("sync_health"))
    if sync_health.get("needs_chain_data_restore") or sync_health.get("chain_data_restore_required"):
        restore_nodes = dict_value(sync_health.get("chain_data_restore_nodes"))
        for node, info in restore_nodes.items():
            node_reasons = info.get("reasons") if isinstance(info, dict) else None
            if isinstance(node_reasons, list) and node_reasons:
                details.extend(
                    {"kind": "chain_data_restore", "reason": str(item)}
                    for item in node_reasons
                    if item and not (defer_missing_trie and chain_state_reason_is_missing_trie(item))
                )
            else:
                reason = f"{node} requires chain data restore"
                if not (defer_missing_trie and chain_state_reason_is_missing_trie(reason)):
                    details.append({"kind": "chain_data_restore", "reason": reason})
        if not restore_nodes:
            reason = "status reports chain data restore is required"
            if not (defer_missing_trie and chain_state_reason_is_missing_trie(reason)):
                details.append(
                    {
                        "kind": "chain_data_restore",
                        "reason": reason,
                    }
                )
    if sync_health.get("chain_state_blocker"):
        blocker_nodes = dict_value(sync_health.get("chain_state_blocker_nodes"))
        for node, info in blocker_nodes.items():
            block_hash = info.get("hash") if isinstance(info, dict) else ""
            if block_hash:
                reason = f"{node} is stuck on irreparable sync block {block_hash}"
            else:
                reason = f"{node} is stuck on an irreparable sync block"
            details.append({"kind": "chain_state_blocker", "reason": reason})

    nodes = dict_value(payload.get("nodes"))
    for node, info in nodes.items():
        if not isinstance(info, dict):
            continue
        if info.get("chain_state_blocker"):
            block_hash = info.get("chain_state_blocker_hash") or "unknown block"
            details.append(
                {
                    "kind": "chain_state_blocker",
                    "reason": f"{node} is stuck on irreparable sync block {block_hash}",
                }
            )
        if info.get("dag_tip_damage"):
            details.append({"kind": "dag_tip_damage", "reason": f"{node} DAG tip/block data is damaged"})
    unique: dict[tuple[str, str], dict[str, str]] = {}
    for item in details:
        unique[(item["kind"], item["reason"])] = item
    return [unique[key] for key in sorted(unique)]


def chain_state_restore_candidate_details(payload: dict[str, Any]) -> list[dict[str, str]]:
    details: list[dict[str, str]] = []
    sync_health = dict_value(payload.get("sync_health"))
    defer_missing_trie = status_payload_has_recent_paid_work(payload, CHAIN_STATE_ACTIVE_MINING_DEFER_SECONDS)
    restore_nodes = dict_value(sync_health.get("chain_data_restore_nodes"))
    for node, info in restore_nodes.items():
        node_reasons = info.get("reasons") if isinstance(info, dict) else None
        if not isinstance(node_reasons, list):
            continue
        for reason in node_reasons:
            if defer_missing_trie and chain_state_reason_is_missing_trie(reason):
                details.append(
                    {
                        "kind": "missing_trie_candidate",
                        "reason": f"{reason}; deferring restore because accepted block submission is fresh",
                    }
                )
    candidate_nodes = dict_value(sync_health.get("chain_data_restore_candidate_nodes"))
    for node, info in candidate_nodes.items():
        node_reasons = info.get("reasons") if isinstance(info, dict) else None
        if isinstance(node_reasons, list) and node_reasons:
            details.extend({"kind": "chain_data_restore_candidate", "reason": str(item)} for item in node_reasons if item)
        else:
            details.append({"kind": "chain_data_restore_candidate", "reason": f"{node} has chain-state restore candidate signals"})

    nodes = dict_value(payload.get("nodes"))
    for node, info in nodes.items():
        if not isinstance(info, dict):
            continue
        missing_trie = safe_int(info.get("missing_trie_node_warnings"), 0)
        if missing_trie >= CHAIN_STATE_MISSING_TRIE_RESTORE_WARNINGS:
            details.append(
                {
                    "kind": "missing_trie_candidate",
                    "reason": (
                        f"{node} has {missing_trie} missing-trie state warning(s); "
                        "waiting for import stall or hard chain-state corruption before restore"
                    ),
                }
            )
    unique: dict[tuple[str, str], dict[str, str]] = {}
    for item in details:
        unique[(item["kind"], item["reason"])] = item
    return [unique[key] for key in sorted(unique)]


def chain_state_restore_hard_reasons(payload: dict[str, Any]) -> list[str]:
    return [item["reason"] for item in chain_state_restore_reason_details(payload)]


def status_payload_has_recent_paid_work(payload: dict[str, Any], max_age_seconds: int) -> bool:
    sync_health = dict_value(payload.get("sync_health"))
    if sync_health.get("pool_has_recent_paid_work") or sync_health.get("pool_has_recent_mining"):
        return True
    pool = dict_value(payload.get("pool"))
    success_count = safe_int(pool.get("block_submit_success_count"), 0)
    last_age = safe_int(pool.get("last_block_submit_age_seconds"), 999999)
    return bool(success_count > 0 and last_age <= max_age_seconds)


def sync_progress_height(payload: dict[str, Any]) -> int:
    sync = dict_value(payload.get("sync_progress"))
    values: list[int] = []
    for key in ("chain_block_count", "latest_block", "height"):
        value = safe_int(sync.get(key), -1)
        if value >= 0:
            values.append(value)
    for info in dict_value(sync.get("nodes")).values():
        if not isinstance(info, dict):
            continue
        for key in ("chain_block_count", "latest_block", "height"):
            value = safe_int(info.get(key), -1)
            if value >= 0:
                values.append(value)
    for info in dict_value(payload.get("nodes")).values():
        if not isinstance(info, dict):
            continue
        for key in ("chain_block_count", "latest_block"):
            value = safe_int(info.get(key), -1)
            if value >= 0:
                values.append(value)
    return max(values) if values else -1


def read_import_watch_state() -> dict[str, Any]:
    try:
        with CHAIN_STATE_IMPORT_WATCH_FILE.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def update_stalled_import_watch(payload: dict[str, Any]) -> dict[str, Any]:
    now_epoch = time.time()
    sync = dict_value(payload.get("sync_progress"))
    status = str(sync.get("status") or payload.get("mode") or "").lower()
    height = sync_progress_height(payload)
    lag = catchup_lag_blocks(payload)
    candidate = bool(
        CHAIN_STATE_STALLED_IMPORT_RESTORE_ENABLED
        and status in {"syncing", "catchup_pause"}
        and height >= 0
        and lag >= CHAIN_STATE_STALLED_IMPORT_RESTORE_PEER_AHEAD_BLOCKS
    )
    previous = read_import_watch_state()
    previous_height = safe_int(previous.get("height"), -1)
    if not candidate or previous_height != height:
        state = {
            "schema_version": 1,
            "updated_at": now_iso(),
            "epoch": now_epoch,
            "status": status,
            "height": height,
            "lag_blocks": lag,
            "first_stalled_epoch": now_epoch if candidate else 0,
            "stalled_seconds": 0,
            "min_lag_blocks": lag if candidate else 0,
            "max_lag_blocks": lag if candidate else 0,
            "gap_growth_blocks": 0,
            "restore_required": False,
            "reason": "height changed or stall candidate inactive",
        }
        write_json_file(CHAIN_STATE_IMPORT_WATCH_FILE, state, mode=0o600)
        return state

    first_stalled_epoch = safe_float(previous.get("first_stalled_epoch"), now_epoch)
    min_lag = min(safe_int(previous.get("min_lag_blocks"), lag), lag)
    max_lag = max(safe_int(previous.get("max_lag_blocks"), lag), lag)
    stalled_seconds = max(0, int(now_epoch - first_stalled_epoch))
    gap_growth = max(0, max_lag - min_lag)
    restore_required = bool(
        stalled_seconds >= CHAIN_STATE_STALLED_IMPORT_RESTORE_SECONDS
        and gap_growth >= CHAIN_STATE_STALLED_IMPORT_RESTORE_GAP_GROWTH_BLOCKS
    )
    reason = (
        f"chain height has stayed at {height} for {stalled_seconds}s while peer lag "
        f"grew by {gap_growth} block(s) to {lag}"
    )
    state = {
        "schema_version": 1,
        "updated_at": now_iso(),
        "epoch": now_epoch,
        "status": status,
        "height": height,
        "lag_blocks": lag,
        "first_stalled_epoch": first_stalled_epoch,
        "stalled_seconds": stalled_seconds,
        "min_lag_blocks": min_lag,
        "max_lag_blocks": max_lag,
        "gap_growth_blocks": gap_growth,
        "restore_required": restore_required,
        "reason": reason,
    }
    write_json_file(CHAIN_STATE_IMPORT_WATCH_FILE, state, mode=0o600)
    return state


def chain_state_restore_decision(payload: dict[str, Any]) -> dict[str, Any]:
    if not MINING_IMPERATIVE_CHAIN_STATE_RESTORE_ENABLED:
        return {"should_repair": False, "reasons": [], "stalled_import": {}, "disabled": True}
    details = chain_state_restore_reason_details(payload)
    reasons = [item["reason"] for item in details]
    candidate_details = chain_state_restore_candidate_details(payload)
    candidate_reasons = [item["reason"] for item in candidate_details]
    stalled_import = update_stalled_import_watch(payload)
    if reasons:
        return {
            "should_repair": True,
            "reasons": reasons,
            "reason_details": details,
            "candidate_reasons": candidate_reasons,
            "candidate_details": candidate_details,
            "stalled_import": stalled_import,
            "hard": True,
        }
    if stalled_import.get("restore_required"):
        return {
            "should_repair": True,
            "reasons": [str(stalled_import.get("reason") or "sustained stalled import")],
            "candidate_reasons": candidate_reasons,
            "candidate_details": candidate_details,
            "stalled_import": stalled_import,
            "hard": False,
        }
    if candidate_reasons:
        return {
            "should_repair": False,
            "reasons": candidate_reasons,
            "reason_details": candidate_details,
            "stalled_import": stalled_import,
            "hard": False,
            "deferred": True,
            "defer_reason": "chain-state restore candidate requires corroboration before destructive restore",
        }
    return {"should_repair": False, "reasons": [], "stalled_import": stalled_import, "hard": False}


def start_chain_state_self_heal(payload: dict[str, Any], decision: dict[str, Any]) -> bool:
    if not CHAIN_STATE_SELF_HEAL_UNIT:
        log("chain-state self-heal unit is not configured")
        return False
    if not automation_repair_mutation_allowed(
        automation_control.ACTION_SYSTEMD_START,
        target=CHAIN_STATE_SELF_HEAL_UNIT,
        reason="start chain-state self-heal after restore-required node state",
        payload=payload,
        event_type="chain_state_self_heal_start_blocked",
        message="Chain-state self-heal was not started because automation control blocked systemd start",
    ):
        return False
    result = systemctl_user("start", "--no-block", CHAIN_STATE_SELF_HEAL_UNIT)
    details = {
        "unit": CHAIN_STATE_SELF_HEAL_UNIT,
        "decision": decision,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }
    if result.ok:
        log(f"started chain-state self-heal unit={CHAIN_STATE_SELF_HEAL_UNIT}")
        record_incident(
            "chain_state_self_heal_started",
            "critical",
            "Started chain-state self-heal because node import is corrupt or stuck",
            details,
            payload,
        )
        return True
    log(
        "failed to start chain-state self-heal "
        f"unit={CHAIN_STATE_SELF_HEAL_UNIT} rc={result.returncode} stderr={result.stderr.strip()}"
    )
    record_incident(
        "chain_state_self_heal_start_failed",
        "critical",
        "Could not start chain-state self-heal after detecting restore-required node state",
        details,
        payload,
    )
    return False


def status_payload_has_miner_demand(payload: dict[str, Any]) -> bool:
    miner_health = dict_value(payload.get("miner_health"))
    if safe_int(miner_health.get("connected_count")) > 0 or safe_int(miner_health.get("managed_count")) > 0:
        return True

    pool = dict_value(payload.get("pool"))
    pool_metrics = dict_value(payload.get("pool_metrics")) or dict_value(pool.get("metrics"))
    if safe_float(pool_metrics.get("active_connections")) > 0:
        return True

    source_job_health = dict_value(pool.get("source_job_health")) or dict_value(pool_metrics.get("source_job_health"))
    return (
        safe_int(source_job_health.get("authorized_miners")) > 0
        or safe_int(source_job_health.get("ready_miners")) > 0
    )


def status_payload_has_tracking_gap(payload: dict[str, Any]) -> bool:
    if not MINING_IMPERATIVE_MINER_TRACKING_REPAIR_ENABLED:
        return False
    miner_health = dict_value(payload.get("miner_health"))
    if safe_int(miner_health.get("tracked_count")) > 0:
        return False
    return status_payload_has_miner_demand(payload) or asic_lan_neighbor_present()


def recent_age_seconds(value: Any, max_age: int = MINING_IMPERATIVE_MINER_ACTIVITY_STALE_SECONDS) -> bool:
    if value in (None, ""):
        return False
    return safe_float(value, default=max_age + 1) <= max_age


def pool_has_recent_share_evidence(payload: dict[str, Any]) -> bool:
    pool = dict_value(payload.get("pool"))
    pool_health = dict_value(payload.get("pool_health"))
    pool_metrics = dict_value(payload.get("pool_metrics")) or dict_value(pool.get("metrics"))
    source_job_health = dict_value(pool.get("source_job_health")) or dict_value(pool_metrics.get("source_job_health"))
    if (
        safe_int(pool_health.get("valid_share_count")) > 0
        or safe_int(pool.get("valid_share_count")) > 0
        or safe_int(pool_metrics.get("valid_share_count")) > 0
        or safe_int(source_job_health.get("valid_share_count")) > 0
    ):
        return True
    for key in ("last_valid_share_age_seconds", "valid_share_age_seconds", "last_share_age_seconds"):
        if recent_age_seconds(pool_health.get(key)) or recent_age_seconds(pool.get(key)) or recent_age_seconds(pool_metrics.get(key)):
            return True
    return False


def miner_row_has_visible_share_evidence(row: dict[str, Any]) -> bool:
    if safe_int(row.get("shares")) > 0 or safe_int(row.get("share_work")) > 0:
        return True
    if safe_int(row.get("last_shares_window")) > 0 or safe_int(row.get("last_share_work_window")) > 0:
        return recent_age_seconds(row.get("last_share_age_seconds"))
    return False


def status_payload_has_miner_activity_visibility_gap(payload: dict[str, Any]) -> bool:
    if not MINING_IMPERATIVE_MINER_ACTIVITY_REPAIR_ENABLED:
        return False
    miner_health = dict_value(payload.get("miner_health"))
    if safe_int(miner_health.get("tracked_count")) <= 0:
        return False
    miners = miner_health.get("miners") if isinstance(miner_health.get("miners"), list) else []
    managed_connected = [
        row
        for row in miners
        if isinstance(row, dict)
        and row.get("managed")
        and row.get("connected")
        and str(row.get("device_type") or "").lower() != "stratum"
    ]
    if not managed_connected:
        return False
    if any(miner_row_has_visible_share_evidence(row) for row in managed_connected):
        return False
    return status_payload_has_miner_demand(payload) and pool_has_recent_share_evidence(payload)


def pool_container_env_value(key: str) -> str | None:
    result = run(
        docker_compose_command(
            "exec",
            "-T",
            POOL_CONTAINER,
            "sh",
            "-lc",
            f'printf "%s" "${{{key}:-}}"',
        ),
        timeout=20,
    )
    if not result.ok:
        return None
    return result.stdout.strip()


def desired_asic_lan_cidrs_value() -> str:
    configured = config_value("BDAG_ASIC_LAN_CIDRS")
    if configured:
        return configured
    scan_target = config_value("BDAG_MINER_SCAN_TARGET")
    if "/" in scan_target:
        return scan_target
    return ""


def status_payload_needs_asic_mac_override_repair(payload: dict[str, Any]) -> bool:
    if not MINING_IMPERATIVE_ASIC_MAC_OVERRIDES_REPAIR_ENABLED:
        return False
    if not (status_payload_has_miner_demand(payload) or asic_lan_neighbor_present()):
        return False
    desired = pool_asic_mac_overrides_value()
    desired_lan_cidrs = desired_asic_lan_cidrs_value()
    if not desired:
        diagnostics = pool_asic_mac_override_diagnostics()
        if diagnostics.get("unresolved_count"):
            log(
                "mining imperative cannot derive POOL_ASIC_MAC_OVERRIDES because ASIC MAC identity is unresolved "
                f"unresolved={diagnostics.get('unresolved')}"
            )
        if not desired_lan_cidrs:
            return False
    configured = config_value("POOL_ASIC_MAC_OVERRIDES")
    configured_lan_cidrs = config_value("BDAG_ASIC_LAN_CIDRS")
    if desired and configured != desired:
        return True
    if desired_lan_cidrs and configured_lan_cidrs != desired_lan_cidrs:
        return True
    if pool_container_running(payload):
        container_value = pool_container_env_value("POOL_ASIC_MAC_OVERRIDES")
        container_lan_cidrs = pool_container_env_value("BDAG_ASIC_LAN_CIDRS")
        return (
            (desired and container_value is not None and container_value != desired)
            or (
                desired_lan_cidrs
                and container_lan_cidrs is not None
                and container_lan_cidrs != desired_lan_cidrs
            )
        )
    return False


def asic_lan_neighbor_present() -> bool:
    cidrs = split_env_list("BDAG_ASIC_LAN_CIDRS", "")
    if not cidrs:
        target = os.environ.get("BDAG_MINER_SCAN_TARGET", "")
        cidrs = [target] if "/" in target else []
    networks = []
    for cidr in cidrs:
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            log(f"mining imperative ignored invalid ASIC LAN CIDR {cidr!r}")
    if not networks:
        return False

    for ip_text, mac in read_neighbor_macs().items():
        if not mac:
            continue
        try:
            address = ipaddress.ip_address(ip_text)
        except ValueError:
            continue
        if any(address in network for network in networks):
            return True
    return False


def constrained_storage_profile() -> bool:
    topology = (config_value("BDAG_DETECTED_NETWORK_TOPOLOGY") or config_value("BDAG_NETWORK_TOPOLOGY")).strip().lower()
    storage_profile = config_value("BDAG_STORAGE_PROFILE").strip().lower()
    return bool(
        topology == "asic-router"
        or storage_profile == "usb-chain-internal-runtime"
        or storage_profile == "single-usb-constrained"
    )

def node_services_for_recreate() -> list[str]:
    configured = config_value("BDAG_NODE_SERVICES") or config_value("BDAG_NODE_SERVICE", "node")
    services: list[str] = []
    for item in configured.replace(" ", ",").split(","):
        name = item.strip()
        if not name:
            continue
        service = compose_service_name(name) or name
        if service not in services:
            services.append(service)
    return services or ["node"]


def node_service_for_recreate() -> str:
    return node_services_for_recreate()[0]


def node_command_line(node_service: str) -> str | None:
    result = run(
        docker_compose_command(
            "exec",
            "-T",
            node_service,
            "sh",
            "-lc",
            "ps -eo args | awk '/[b]dag/{print; exit}'",
        ),
        timeout=20,
    )
    if not result.ok:
        return None
    command_line = result.stdout.strip()
    return command_line or None


def node_mining_template_support_should_repair(payload: dict[str, Any]) -> bool:
    if not MINING_IMPERATIVE_NODE_MINING_REPAIR_ENABLED:
        return False
    if catchup_pause_active(payload):
        return False
    if not (status_payload_has_miner_demand(payload) or asic_lan_neighbor_present()):
        return False
    address = configured_mining_address()
    if not valid_mining_address(address):
        return False
    modules = {item.strip().lower() for item in config_value("BDAG_NODE_MODULES", "Blockdag").split(",")}
    args = config_value("BDAG_NODE_MINING_ARGS")
    if not env_enabled_value(config_value("BDAG_ENABLE_NODE_MINING"), False):
        return True
    if modules and ("blockdag" not in modules or "miner" in modules):
        return True
    if not node_mining_args_are_safe_and_complete(args, address):
        return True
    append_args = config_value("NODE_ARGS_APPEND")
    if append_args and not node_mining_args_are_safe_and_complete(append_args, address):
        return True
    if pool_has_recent_share_evidence(payload) or status_payload_has_recent_paid_work(
        payload,
        CHAIN_STATE_ACTIVE_MINING_DEFER_SECONDS,
    ):
        return False
    if not MINING_IMPERATIVE_NODE_COMMAND_LINE_REPAIR_ENABLED:
        return False
    for service in node_services_for_recreate():
        command_line = node_command_line(service)
        if command_line and not node_mining_args_are_safe_and_complete(command_line, address):
            return True
    return False


def payload_node_tail_lines(payload: dict[str, Any]) -> list[str]:
    nodes = payload.get("nodes") if isinstance(payload.get("nodes"), dict) else {}
    lines: list[str] = []
    for row in nodes.values():
        if not isinstance(row, dict):
            continue
        tail = row.get("tail") if isinstance(row.get("tail"), list) else []
        lines.extend(str(line) for line in tail)
    return lines


def control_decision_payload(decision: Any) -> dict[str, Any]:
    if hasattr(decision, "as_dict"):
        return decision.as_dict()
    try:
        return dict(vars(decision))
    except TypeError:
        return {"decision": str(decision)}


def automation_repair_mutation_allowed(
    action: str,
    *,
    target: str,
    reason: str,
    payload: dict[str, Any],
    event_type: str,
    message: str,
    severity: str = "critical",
) -> bool:
    decision = automation_control.check_mutation_allowed(
        action,
        actor="status-sampler",
        target=target,
        reason=reason,
    )
    if decision.allowed:
        return True
    details = {
        "action": action,
        "target": target,
        "requested_reason": reason,
        "control_decision": control_decision_payload(decision),
    }
    log(f"mining imperative suppressed {action} for {target}: {decision.reason}")
    record_incident(event_type, severity, message, details, payload)
    return False


def recreate_node_service(payload: dict[str, Any], reason: str) -> tuple[bool, list[dict[str, Any]]]:
    node_results = []
    ok = True
    for service in node_services_for_recreate():
        if not automation_repair_mutation_allowed(
            automation_control.ACTION_CONTAINER_RECREATE,
            target=service,
            reason=reason,
            payload=payload,
            event_type="mining_imperative_node_recreate_blocked",
            message=f"Mining imperative left {service} unchanged because automation control blocked recreate",
        ):
            node_results.append({"service": service, "returncode": None, "ok": False, "blocked": True})
            ok = False
            continue
        result = run(
            docker_compose_command("up", "-d", "--no-deps", "--force-recreate", "--no-build", "--pull", "never", service),
            timeout=240,
        )
        node_results.append({"service": service, "returncode": result.returncode, "ok": result.ok})
        ok = ok and result.ok
    return ok, node_results


def remove_peer_ids_from_csv(value: str, peer_ids: list[str]) -> str:
    peers = [item.strip() for item in value.split(",") if item.strip()]
    if not peers or not peer_ids:
        return value
    kept = [peer for peer in peers if not any(peer_id in peer for peer_id in peer_ids)]
    return ",".join(kept)


def repair_missing_tracked_miners(payload: dict[str, Any]) -> bool:
    activity = collect_pool_activity(lines=POOL_ACTIVITY_BOOTSTRAP_LOG_LINES)
    registry = upsert_pool_activity_miners(activity)
    if not registry.get("miners"):
        hinted = read_miner_registry()
        if hinted.get("miners"):
            registry = save_miner_registry(hinted.get("miners", []))
    count = len(registry.get("miners") or [])
    action = {
        "tracked_count_after": count,
        "activity_miners": len(activity.get("miners") or []),
        "unattributed_valid_shares": activity.get("unattributed_valid_shares"),
        "unattributed_blocks": activity.get("unattributed_blocks"),
    }
    if count > 0:
        log(f"mining imperative repaired tracked-miner registry count={count}")
        record_incident(
            "mining_imperative_tracked_miners_repaired",
            "critical",
            "Mining imperative repaired missing tracked miners from LAN/pool evidence",
            action,
            payload,
        )
        return True
    log("mining imperative could not repair missing tracked miners")
    record_incident(
        "mining_imperative_tracked_miners_repair_failed",
        "critical",
        "Mining imperative could not repair missing tracked miners despite miner demand",
        action,
        payload,
    )
    return False


def repair_miner_activity_visibility(payload: dict[str, Any]) -> bool:
    activity = collect_pool_activity(lines=POOL_ACTIVITY_BOOTSTRAP_LOG_LINES)
    registry = upsert_pool_activity_miners(activity)
    miners = registry.get("miners") if isinstance(registry.get("miners"), list) else []
    now_epoch = time.time()
    fresh_rows = [
        row
        for row in miners
        if isinstance(row, dict)
        and (
            safe_int(row.get("last_shares_window")) > 0
            or safe_int(row.get("last_share_work_window")) > 0
            or safe_int(row.get("last_submits_window")) > 0
        )
        and (
            (
                safe_int(row.get("last_share_epoch")) > 0
                and now_epoch - safe_int(row.get("last_share_epoch")) <= MINING_IMPERATIVE_MINER_ACTIVITY_STALE_SECONDS
            )
            or (
                safe_int(row.get("last_submit_epoch")) > 0
                and now_epoch - safe_int(row.get("last_submit_epoch")) <= MINING_IMPERATIVE_MINER_ACTIVITY_STALE_SECONDS
            )
            or (
                safe_int(row.get("last_pool_seen_epoch")) > 0
                and now_epoch - safe_int(row.get("last_pool_seen_epoch")) <= MINING_IMPERATIVE_MINER_ACTIVITY_STALE_SECONDS
            )
        )
    ]
    action = {
        "tracked_count_after": len(miners),
        "activity_miners": len(activity.get("miners") or []),
        "fresh_registry_activity_rows": len(fresh_rows),
        "unattributed_valid_shares": activity.get("unattributed_valid_shares"),
        "unattributed_blocks": activity.get("unattributed_blocks"),
    }
    if action["activity_miners"] > 0 or action["fresh_registry_activity_rows"] > 0:
        log(
            "mining imperative repaired miner activity visibility "
            f"activity_miners={action['activity_miners']} fresh_rows={action['fresh_registry_activity_rows']}"
        )
        record_incident(
            "mining_imperative_miner_activity_visibility_repaired",
            "warning",
            "Mining imperative refreshed miner share attribution after tracked miners had no visible share rows",
            action,
            payload,
        )
        return True
    log("mining imperative could not repair miner activity visibility")
    record_incident(
        "mining_imperative_miner_activity_visibility_repair_failed",
        "warning",
        "Mining imperative found pool share evidence but could not refresh per-miner share attribution",
        action,
        payload,
    )
    return False


def repair_pool_asic_mac_overrides(payload: dict[str, Any]) -> bool:
    diagnostics = pool_asic_mac_override_diagnostics()
    desired = str(diagnostics.get("override_value") or "")
    desired_lan_cidrs = desired_asic_lan_cidrs_value()
    if not desired and not desired_lan_cidrs:
        record_incident(
            "mining_imperative_asic_mac_overrides_unresolved",
            "warning",
            "ASIC MAC override map could not be generated because one or more ASIC MAC identities are unresolved",
            diagnostics,
            payload,
        )
        return False
    configured = config_value("POOL_ASIC_MAC_OVERRIDES")
    configured_lan_cidrs = config_value("BDAG_ASIC_LAN_CIDRS")
    container_value = pool_container_env_value("POOL_ASIC_MAC_OVERRIDES") if pool_container_running(payload) else None
    container_lan_cidrs = pool_container_env_value("BDAG_ASIC_LAN_CIDRS") if pool_container_running(payload) else None
    changed_paths: list[str] = []
    if desired and configured != desired:
        if not automation_repair_mutation_allowed(
            automation_control.ACTION_CONFIG_EDIT,
            target="POOL_ASIC_MAC_OVERRIDES",
            reason="write MAC-only ASIC LAN lane override map for pool container",
            payload=payload,
            event_type="mining_imperative_config_edit_blocked",
            message="Mining imperative could not write ASIC MAC override config because automation control blocked config edits",
        ):
            return False
        changed_paths = set_runtime_env_value("POOL_ASIC_MAC_OVERRIDES", desired)
    if desired_lan_cidrs and configured_lan_cidrs != desired_lan_cidrs:
        if not automation_repair_mutation_allowed(
            automation_control.ACTION_CONFIG_EDIT,
            target="BDAG_ASIC_LAN_CIDRS",
            reason="write ASIC LAN CIDR scope for pool MAC-only lane enforcement",
            payload=payload,
            event_type="mining_imperative_config_edit_blocked",
            message="Mining imperative could not write ASIC LAN CIDR config because automation control blocked config edits",
        ):
            return False
        changed_paths.extend(set_runtime_env_value("BDAG_ASIC_LAN_CIDRS", desired_lan_cidrs))

    recreate_result = None
    pool_env_stale = (
        (desired and container_value != desired)
        or (desired_lan_cidrs and container_lan_cidrs != desired_lan_cidrs)
    )
    if pool_container_running(payload) and pool_env_stale:
        if not automation_repair_mutation_allowed(
            automation_control.ACTION_CONTAINER_RECREATE,
            target=POOL_CONTAINER,
            reason="recreate pool to load MAC-only ASIC LAN identity environment",
            payload=payload,
            event_type="mining_imperative_pool_recreate_blocked",
            message="Mining imperative could not recreate pool for ASIC MAC identity config because automation control blocked container recreate",
        ):
            return False
        recreate_result = run(
            docker_compose_command("up", "-d", "--no-deps", "--force-recreate", "--no-build", "--pull", "never", POOL_CONTAINER),
            timeout=180,
        )

    action = {
        **diagnostics,
        "configured_value_before": configured,
        "container_value_before": container_value,
        "desired_lan_cidrs": desired_lan_cidrs,
        "configured_lan_cidrs_before": configured_lan_cidrs,
        "container_lan_cidrs_before": container_lan_cidrs,
        "changed_env_paths": sorted(set(changed_paths)),
        "pool_recreate": recreate_result.as_dict() if recreate_result else None,
    }
    if recreate_result is None or recreate_result.ok:
        log(
            "mining imperative reconciled ASIC MAC identity environment "
            f"override_count={diagnostics.get('override_count')} pool_recreated={bool(recreate_result)}"
        )
        record_incident(
            "mining_imperative_asic_mac_overrides_reconciled",
            "warning",
            "Reconciled MAC-only ASIC lane identity environment for the pool container",
            action,
            payload,
        )
        return True

    log(f"mining imperative failed to recreate {POOL_CONTAINER} for ASIC MAC identity environment")
    record_incident(
        "mining_imperative_asic_mac_overrides_recreate_failed",
        "critical",
        "Could not recreate pool after writing MAC-only ASIC lane identity environment",
        action,
        payload,
    )
    return False


def write_text_if_changed(path: Any, text: str) -> bool:
    if not path.exists():
        return False
    current = path.read_text(encoding="utf-8", errors="replace")
    if current == text:
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True


def node_conf_path() -> Any:
    return PROJECT_ROOT / "node.conf"


def node_conf_cache_mb() -> int:
    path = node_conf_path()
    if not path.exists():
        return 0
    match = re.search(r"(?m)^cache=(\d+)\s*$", path.read_text(encoding="utf-8", errors="replace"))
    return safe_int(match.group(1), 0) if match else 0


def update_node_conf_cache(cache_mb: int, evm_cache_mb: int | None = None) -> list[str]:
    path = node_conf_path()
    if not path.exists() or cache_mb <= 0:
        return []
    original = path.read_text(encoding="utf-8", errors="replace")
    text, count = re.subn(r"(?m)^cache=\d+\s*$", f"cache={cache_mb}", original, count=1)
    if count == 0:
        text, count = re.subn(r"(?m)^cache\.database=", f"cache={cache_mb}\ncache.database=", text, count=1)
        if count == 0:
            text = text.rstrip() + f"\ncache={cache_mb}\n"
    if evm_cache_mb is None:
        evm_cache_mb = safe_int(config_value("BDAG_EVM_CACHE_MB"), 0)
    if evm_cache_mb > 0:
        if re.search(r"--cache\s+\d+", text):
            text = re.sub(r"--cache\s+\d+", f"--cache {evm_cache_mb}", text)
        elif re.search(r'(?m)^evmenv="', text):
            text = re.sub(
                r'(?m)^evmenv="([^"]*)"',
                lambda match: f'evmenv="{match.group(1).rstrip()} --cache {evm_cache_mb}"',
                text,
                count=1,
            )
    return [str(path)] if write_text_if_changed(path, text) else []


def update_node_conf_mining(enabled: bool, address: str = "") -> list[str]:
    path = node_conf_path()
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    if enabled:
        if address:
            if re.search(r"(?m)^#?\s*miningaddr=.*$", text):
                text = re.sub(r"(?m)^#?\s*miningaddr=.*$", f"miningaddr={address}", text, count=1)
            else:
                text = text.rstrip() + f"\nminingaddr={address}\n"
    else:
        text = re.sub(r"(?m)^miningaddr=.*$", "miningaddr=", text)
        text = re.sub(r"(?m)^modules=miner\s*$", "# modules=miner disabled during catch-up pause", text)
        text = re.sub(r"(?m)^miner=true\s*$", "# miner=true disabled during catch-up pause", text)
    return [str(path)] if write_text_if_changed(path, text) else []


def catchup_target_node_cache_mb() -> int:
    if CATCHUP_NODE_CACHE_MB <= 0:
        return 0
    memory_bytes = detect_total_memory_bytes()
    if not memory_bytes:
        return CATCHUP_NODE_CACHE_MB
    memory_mb = max(0, int(memory_bytes / (1024 * 1024)))
    percent_target = int(memory_mb * (CATCHUP_NODE_CACHE_MEMORY_PERCENT / 100.0))
    return max(256, min(CATCHUP_NODE_CACHE_MB, max(CATCHUP_NODE_CACHE_MIN_MB, percent_target)))


def apply_catchup_node_runtime(payload: dict[str, Any], policy: dict[str, Any]) -> bool:
    if not automation_repair_mutation_allowed(
        automation_control.ACTION_CONFIG_EDIT,
        target="catchup-node-runtime",
        reason=f"catch-up cache adjustment lag={policy.get('lag_blocks')} threshold={policy.get('threshold_blocks')}",
        payload=payload,
        event_type="catchup_pause_config_edit_blocked",
        message="Catch-up pause could not adjust node runtime because automation control blocked config edits",
    ):
        return False
    changed_paths: list[str] = []
    env_updates: dict[str, str] = {}

    target_cache = catchup_target_node_cache_mb()
    if target_cache > 0:
        if safe_int(config_value("BDAG_NODE_CACHE_MB"), 0) < target_cache:
            changed_paths.extend(set_runtime_env_value("BDAG_NODE_CACHE_MB", str(target_cache)))
            env_updates["BDAG_NODE_CACHE_MB"] = str(target_cache)
        current_conf_cache = node_conf_cache_mb()
        if current_conf_cache and current_conf_cache < target_cache:
            changed_paths.extend(update_node_conf_cache(target_cache))

    changed_paths = sorted(set(changed_paths))
    if not changed_paths:
        return False

    node_results: list[dict[str, Any]] = []
    ok = True
    node_service = node_service_for_recreate()
    containers = dict_value(payload.get("containers"))
    node_container = containers.get(node_service)
    node_state_known = isinstance(node_container, dict)
    node_running = bool(dict_value(node_container).get("running")) if node_state_known else False
    if CATCHUP_NODE_RECREATE_ENABLED and node_state_known and not node_running:
        ok, node_results = recreate_node_service(payload, "recreate node after catch-up runtime adjustment")
    elif CATCHUP_NODE_RECREATE_ENABLED:
        log(
            "catch-up pause deferred node recreate after cache config adjustment "
            f"lag={policy.get('lag_blocks')} threshold={policy.get('threshold_blocks')}"
        )
    action = {
        "policy": policy,
        "changed_env": env_updates,
        "changed_paths": changed_paths,
        "node_recreate_results": node_results,
        "node_recreate_deferred": bool(CATCHUP_NODE_RECREATE_ENABLED and (node_running or not node_state_known)),
        "node_state_known": node_state_known,
        "target_cache_mb": target_cache,
    }
    if ok:
        log(
            "catch-up pause adjusted node cache runtime "
            f"lag={policy.get('lag_blocks')} threshold={policy.get('threshold_blocks')} paths={','.join(changed_paths)}"
        )
        record_incident(
            "catchup_pause_node_runtime_adjusted",
            "warning",
            "Catch-up pause raised node cache settings without disabling mining/template support",
            action,
            payload,
        )
        return True
    log("catch-up pause failed to recreate node after runtime adjustment")
    record_incident(
        "catchup_pause_node_runtime_adjust_failed",
        "critical",
        "Catch-up pause could not recreate the node after runtime adjustment",
        action,
        payload,
    )
    return False


def repair_node_mining_template_support(payload: dict[str, Any]) -> bool:
    gate = pool_start_gate.pool_start_decision(payload)
    if not gate.allowed:
        action = {
            "blocked_reason": gate.reason,
            "status_source": gate.status_source,
        }
        log(f"mining imperative left node mining/template support unchanged by pool start gate: {gate.reason}")
        record_incident(
            "mining_imperative_node_mining_gate_blocked",
            "warning",
            "Mining imperative did not enable node mining/template support because canonical pool gate is unsafe",
            action,
            payload,
        )
        return False

    address = configured_mining_address()
    if not valid_mining_address(address):
        action = {"address_present": bool(address), "address_zero": address.lower() == ZERO_ADDRESS}
        log("mining imperative cannot enable node mining template support without a valid payout address")
        record_incident(
            "mining_imperative_node_mining_address_missing",
            "critical",
            "Cannot enable node mining template support without a valid non-zero payout address",
            action,
            payload,
        )
        return False

    if not automation_repair_mutation_allowed(
        automation_control.ACTION_CONFIG_EDIT,
        target="node-mining-template-config",
        reason="enable node mining/template support for attached ASIC demand",
        payload=payload,
        event_type="mining_imperative_config_edit_blocked",
        message="Mining imperative could not enable node mining/template support because automation control blocked config edits",
    ):
        return False
    changed_paths = []
    changed_paths.extend(set_runtime_env_value("BDAG_ENABLE_NODE_MINING", "1"))
    changed_paths.extend(set_runtime_env_value("BDAG_NODE_MODULES", "Blockdag"))
    runtime_args = node_mining_runtime_args(address)
    changed_paths.extend(
        set_runtime_env_value(
            "BDAG_NODE_MINING_ARGS",
            runtime_args,
        )
    )
    changed_paths.extend(set_runtime_env_value("NODE_ARGS_APPEND", runtime_args))
    changed_paths.extend(update_node_conf_mining(True, address))
    ok, node_results = recreate_node_service(payload, "recreate node after enabling mining/template support")
    action = {
        "changed_env_paths": sorted(set(changed_paths)),
        "node_recreate_results": node_results,
        "mining_address_configured": True,
    }
    if ok:
        log("mining imperative enabled node miner/template support for attached ASIC demand")
        record_incident(
            "mining_imperative_node_mining_enabled",
            "critical",
            "Enabled node miner/template support because miner demand is present",
            action,
            payload,
        )
        return True
    log("mining imperative failed to recreate node after enabling miner/template support")
    record_incident(
        "mining_imperative_node_mining_enable_failed",
        "critical",
        "Could not recreate node after enabling miner/template support",
        action,
        payload,
    )
    return False


def pool_container_running(payload: dict[str, Any]) -> bool:
    containers = dict_value(payload.get("containers"))
    container = dict_value(containers.get(POOL_CONTAINER))
    return bool(container.get("running"))


def clear_pool_start_stability() -> None:
    try:
        POOL_START_STABILITY_FILE.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        log(f"could not clear pool start stability state: {exc}")


def pool_start_stability_blocked(payload: dict[str, Any]) -> tuple[bool, str]:
    required = MINING_IMPERATIVE_POOL_START_STABLE_SAFE_SECONDS
    if required <= 0:
        return False, ""

    now = time.time()
    state: dict[str, Any] = {}
    try:
        state = json.loads(POOL_START_STABILITY_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        state = {}

    status_key = "|".join(
        str(payload.get(name) or "")
        for name in ("overall", "mode", "status_reason")
    )
    sync_progress = dict_value(payload.get("sync_progress"))
    status_key = "|".join(
        [
            status_key,
            str(sync_progress.get("status") or ""),
            str(sync_progress.get("remaining_blocks") or ""),
        ]
    )
    if state.get("status_key") != status_key:
        state = {
            "schema_version": 1,
            "status": "allowed",
            "status_key": status_key,
            "allowed_since": now,
            "updated_at": now_iso(),
        }
    else:
        state["updated_at"] = now_iso()
    write_json_file(POOL_START_STABILITY_FILE, state, mode=0o600)

    try:
        allowed_since = float(state.get("allowed_since"))
    except (TypeError, ValueError):
        allowed_since = now
    elapsed = max(0.0, now - allowed_since)
    if elapsed < required:
        return (
            True,
            f"pool start gate is safe, but waiting for a stable-safe window ({elapsed:.0f}/{required}s)",
        )
    return False, ""


def stop_pool_container(payload: dict[str, Any], reason: str, *, containment: str = "catchup_pause") -> bool:
    control = automation_control.check_mutation_allowed(
        automation_control.ACTION_CONTAINMENT_STOP,
        actor="status-sampler",
        target=POOL_CONTAINER,
        reason=reason,
    )
    containment_label = containment.replace("_", " ")
    event_prefix = containment if containment else "containment"
    control_payload = control.as_dict() if hasattr(control, "as_dict") else dict(vars(control))
    if not control.allowed:
        action = {
            "reason": reason,
            "container": POOL_CONTAINER,
            "containment": containment,
            "control_decision": control_payload,
            "method": "automation-control",
        }
        log(f"{containment_label} stop suppressed by automation control for {POOL_CONTAINER}: {control.reason}")
        record_incident(
            f"{event_prefix}_pool_stop_blocked",
            "warning",
            f"{containment_label} stop suppressed by automation control: {control.reason}",
            action,
            payload,
        )
        return False
    compose = run(docker_compose_command("stop", POOL_CONTAINER), timeout=120)
    action = {
        "reason": reason,
        "container": POOL_CONTAINER,
        "containment": containment,
        "control_decision": control_payload,
        "method": "docker compose stop",
        **compose.as_dict(),
    }
    if compose.ok:
        log(f"{containment_label} stopped {POOL_CONTAINER}: {reason}")
        record_incident(
            f"{event_prefix}_stopped_pool",
            "warning",
            f"{containment_label} stopped {POOL_CONTAINER}: {reason}",
            action,
            payload,
        )
        return True

    stop = run(["docker", "stop", POOL_CONTAINER], timeout=60)
    action = {
        "reason": reason,
        "container": POOL_CONTAINER,
        "containment": containment,
        "control_decision": control_payload,
        "method": "docker stop",
        "compose_stop": compose.as_dict(),
        "docker_stop": stop.as_dict(),
    }
    if stop.ok:
        log(f"{containment_label} stopped {POOL_CONTAINER} with docker stop: {reason}")
        record_incident(
            f"{event_prefix}_stopped_pool",
            "warning",
            f"{containment_label} stopped {POOL_CONTAINER}: {reason}",
            action,
            payload,
        )
        return True

    log(
        f"{containment_label} could not stop {POOL_CONTAINER}: {reason}; "
        f"compose_rc={compose.returncode} docker_stop_rc={stop.returncode}"
    )
    record_incident(
        f"{event_prefix}_pool_stop_failed",
        "critical",
        f"{containment_label} could not stop {POOL_CONTAINER}: {reason}",
        action,
        payload,
    )
    return False


def start_pool_container(payload: dict[str, Any], reason: str) -> bool:
    allowed, block_reason, _decision = pool_start_gate.pool_start_allowed_by_control(
        action=automation_control.ACTION_ASIC_POOL_START,
        actor="status-sampler",
        target=POOL_CONTAINER,
        reason=reason,
        status=payload,
    )
    if not allowed:
        clear_pool_start_stability()
        action = {
            "reason": reason,
            "container": POOL_CONTAINER,
            "blocked_reason": block_reason,
            "method": "pool_start_gate",
        }
        log(f"mining imperative left {POOL_CONTAINER} stopped by pool start gate: {block_reason}")
        record_incident(
            "mining_imperative_pool_start_blocked",
            "warning",
            f"Mining imperative left {POOL_CONTAINER} stopped: {block_reason}",
            action,
            payload,
        )
        return False

    stable_blocked, stable_reason = pool_start_stability_blocked(payload)
    if stable_blocked:
        action = {
            "reason": reason,
            "container": POOL_CONTAINER,
            "blocked_reason": stable_reason,
            "method": "pool_start_stability_gate",
        }
        log(f"mining imperative left {POOL_CONTAINER} stopped by pool start stability gate: {stable_reason}")
        record_incident(
            "mining_imperative_pool_start_blocked",
            "warning",
            f"Mining imperative left {POOL_CONTAINER} stopped: {stable_reason}",
            action,
            payload,
        )
        return False

    compose = run(
        docker_compose_command("up", "-d", "--no-deps", "--no-build", "--pull", "never", POOL_CONTAINER),
        timeout=180,
    )
    action = {
        "reason": reason,
        "container": POOL_CONTAINER,
        "method": "docker compose up --no-deps --no-build --pull never",
        **compose.as_dict(),
    }
    if compose.ok:
        log(f"mining imperative started {POOL_CONTAINER}: {reason}")
        record_incident(
            "mining_imperative_started_pool",
            "critical",
            f"Mining imperative started {POOL_CONTAINER}: {reason}",
            action,
            payload,
        )
        return True

    log(
        f"mining imperative could not start {POOL_CONTAINER}: {reason}; "
        f"compose_rc={compose.returncode}"
    )
    record_incident(
        "mining_imperative_pool_start_failed",
        "critical",
        f"Mining imperative could not start {POOL_CONTAINER}: {reason}",
        action,
        payload,
    )
    return False


def mining_imperative_repair(payload: dict[str, Any]) -> dict[str, Any]:
    if not mining_imperative_enabled():
        return {"enabled": False, "actions": []}

    actions: list[str] = []
    catchup_policy = catchup_policy_from_payload(payload)
    catchup_active = bool(catchup_policy.get("active"))
    for unit in MINING_IMPERATIVE_GUARD_UNITS:
        if ensure_user_unit(unit, payload):
            actions.append(f"repaired_unit:{unit}")

    divergence_reasons = public_chain_divergence_reasons(payload)
    if divergence_reasons:
        clear_pool_start_stability()
        reason = "; ".join(divergence_reasons)
        if pool_container_running(payload) and stop_pool_container(payload, reason, containment="public_chain_divergence"):
            actions.append(f"stopped_container:{POOL_CONTAINER}:public_chain_divergence")
        else:
            log(f"mining imperative held {POOL_CONTAINER} for public-chain divergence: {reason}")
        return {"enabled": True, "actions": actions}

    chain_restore_decision = chain_state_restore_decision(payload)
    if chain_restore_decision.get("should_repair"):
        clear_pool_start_stability()
        if start_chain_state_self_heal(payload, chain_restore_decision):
            actions.append("started_chain_state_self_heal")
        return {"enabled": True, "actions": actions}
    if chain_restore_decision.get("deferred"):
        reason = chain_restore_decision.get("defer_reason") or "chain-state restore deferred"
        details = "; ".join(chain_restore_decision.get("reasons") or [])
        log(f"{reason}: {details}")

    if status_payload_has_tracking_gap(payload):
        if repair_missing_tracked_miners(payload):
            actions.append("repaired_tracked_miners")

    if status_payload_needs_asic_mac_override_repair(payload):
        if repair_pool_asic_mac_overrides(payload):
            actions.append("repaired_pool_asic_mac_overrides")

    if status_payload_has_miner_activity_visibility_gap(payload):
        if repair_miner_activity_visibility(payload):
            actions.append("repaired_miner_activity_visibility")

    if catchup_active:
        clear_pool_start_stability()
        reason = catchup_policy_stop_reason(catchup_policy, payload)
        containment = catchup_policy_containment(catchup_policy)
        if pool_container_running(payload) and stop_pool_container(payload, reason, containment=containment):
            actions.append(f"stopped_container:{POOL_CONTAINER}:{containment}")
        if (
            catchup_policy_allows_node_runtime_adjustment(catchup_policy)
            and apply_catchup_node_runtime(payload, catchup_policy)
        ):
            actions.append("applied_catchup_node_runtime")

    if not catchup_active and node_mining_template_support_should_repair(payload):
        if repair_node_mining_template_support(payload):
            actions.append("enabled_node_mining_template_support")

    if MINING_IMPERATIVE_START_POOL_ENABLED and not pool_container_running(payload):
        miner_demand = status_payload_has_miner_demand(payload)
        lan_candidate = asic_lan_neighbor_present()
        chain_ready = chain_ready_for_mining(payload)
        should_start = (
            not catchup_active
            and (miner_demand or lan_candidate or (MINING_IMPERATIVE_START_IDLE_SYNCED_POOL and chain_ready))
        )
        if should_start:
            reasons = []
            if miner_demand:
                reasons.append("miner demand is visible in status metrics")
            if lan_candidate:
                reasons.append("ASIC LAN neighbor is present")
            if chain_ready:
                reasons.append("chain is ready")
            if start_pool_container(payload, "; ".join(reasons) or "mining service is required"):
                actions.append(f"started_container:{POOL_CONTAINER}")
        elif catchup_active:
            reason = catchup_policy_stop_reason(catchup_policy, payload)
            containment = catchup_policy_containment(catchup_policy).replace("_", " ")
            log(
                f"mining imperative left {POOL_CONTAINER} stopped for {containment}: {reason}"
            )
        else:
            clear_pool_start_stability()
            log(
                f"mining imperative left {POOL_CONTAINER} stopped: "
                "no miner demand or ASIC LAN neighbor; idle synced pool autostart is disabled"
            )

    return {"enabled": True, "actions": actions}


def write_error_state(error: Exception) -> None:
    write_json_file(
        STATUS_SAMPLER_FILE,
        {
            "schema_version": 1,
            "updated_at": now_iso(),
            "epoch": time.time(),
            "status": "failed",
            "error": str(error),
        },
        mode=0o600,
    )


def sample_once(include_logs: bool) -> dict[str, Any]:
    # max_age_seconds=0 is the explicit hard-bypass path: do not read either
    # the shared sampler file or the short shared cache while producing a sample.
    payload = collect_status_cached(include_logs=include_logs, max_age_seconds=0)
    write_status_sampler_payload(payload, include_logs=include_logs)
    log(
        "sampled "
        f"overall={payload.get('overall')} mode={payload.get('mode')} "
        f"fresh={payload.get('fresh')} include_logs={include_logs}"
    )
    return payload


def maybe_record_earnings_snapshot(
    now_epoch: float,
    last_attempt_epoch: float,
    interval_seconds: float,
    enabled: bool,
) -> float:
    if not enabled or interval_seconds <= 0:
        return last_attempt_epoch
    if last_attempt_epoch and now_epoch - last_attempt_epoch < interval_seconds:
        return last_attempt_epoch

    info = read_latest_earnings_snapshot_info()
    latest_epoch = info.get("latest_epoch")
    try:
        latest_age = now_epoch - float(latest_epoch) if latest_epoch is not None else None
    except (TypeError, ValueError):
        latest_age = None
    if latest_age is not None and latest_age < interval_seconds:
        return last_attempt_epoch

    try:
        snapshot = record_earnings_snapshot()
    except Exception as exc:  # noqa: BLE001 - status sampling must not die on plot history failures.
        log(f"earnings snapshot failed: {exc}")
        return now_epoch
    miners = snapshot.get("miner_estimates")
    miner_count = len(miners) if isinstance(miners, list) else 0
    log(f"earnings snapshot recorded generated_at={snapshot.get('generated_at')} miners={miner_count}")
    return now_epoch


def run_loop(interval_seconds: float, include_logs: bool, earnings_snapshot_interval_seconds: float, record_earnings: bool) -> int:
    ensure_runtime()
    last_earnings_attempt_epoch = 0.0
    last_mining_repair_epoch = 0.0
    while True:
        started = time.time()
        try:
            payload = sample_once(include_logs=include_logs)
            now_epoch = time.time()
            if now_epoch - last_mining_repair_epoch >= MINING_IMPERATIVE_REPAIR_INTERVAL_SECONDS:
                repair = mining_imperative_repair(payload)
                if repair.get("actions"):
                    log(f"mining imperative repair actions={','.join(repair['actions'])}")
                    if any(
                        action in repair["actions"]
                        for action in (
                            "repaired_tracked_miners",
                            "repaired_pool_asic_mac_overrides",
                            "repaired_miner_activity_visibility",
                        )
                    ):
                        payload = sample_once(include_logs=include_logs)
                last_mining_repair_epoch = now_epoch
            last_earnings_attempt_epoch = maybe_record_earnings_snapshot(
                time.time(),
                last_earnings_attempt_epoch,
                earnings_snapshot_interval_seconds,
                record_earnings,
            )
        except Exception as exc:  # noqa: BLE001 - sampler must keep trying.
            log(f"sample failed: {exc}")
            try:
                write_error_state(exc)
            except Exception as write_exc:  # noqa: BLE001
                log(f"failed to write error state: {write_exc}")
        elapsed = time.time() - started
        time.sleep(max(1.0, interval_seconds - elapsed))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true", help="keep sampling until the service is stopped")
    parser.add_argument("--interval-seconds", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument(
        "--earnings-snapshot-interval-seconds",
        type=float,
        default=DEFAULT_EARNINGS_SNAPSHOT_INTERVAL_SECONDS,
        help="append miner/earnings plot snapshots when the valid history is older than this interval; 0 disables",
    )
    parser.add_argument(
        "--no-earnings-snapshots",
        action="store_true",
        help="do not append miner/earnings plot snapshots from the status sampler",
    )
    parser.add_argument("--no-logs", action="store_true", help="omit container log tails from each sample")
    parser.add_argument("--json", action="store_true", help="print the sampled payload")
    args = parser.parse_args()

    include_logs = not args.no_logs
    if args.loop:
        return run_loop(
            max(1.0, args.interval_seconds),
            include_logs,
            max(0.0, args.earnings_snapshot_interval_seconds),
            not args.no_earnings_snapshots,
        )
    try:
        payload = sample_once(include_logs=include_logs)
    except Exception as exc:  # noqa: BLE001
        log(f"sample failed: {exc}")
        write_error_state(exc)
        raise
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

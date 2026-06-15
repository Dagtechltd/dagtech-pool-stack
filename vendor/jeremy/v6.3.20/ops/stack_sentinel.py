#!/usr/bin/env python3
"""Last-resort liveness sentinel for the local BlockDAG mining stack."""

from __future__ import annotations

import fcntl
import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import automation_control
from incident_journal import append_incident
from guard_core import automation_mutation_allowed, should_emit, stable_failure_signature, start_unit
import pool_start_gate
from stack_status_source import StackStatusUnavailable, collect_stack_status
from pool_ops import (
    LOG_DIR,
    NODES,
    POOL_CONTAINER,
    POOL_DB_CONTAINER,
    POOL_ENV_FILE,
    PROJECT_ROOT,
    RUNTIME_DIR,
    SERVICES,
    docker_inspect,
    ensure_runtime,
    now_iso,
    run_logged,
)


STATE_FILE = RUNTIME_DIR / "stack-sentinel-state.json"
SYNC_COORDINATOR_STATE_FILE = RUNTIME_DIR / "sync-coordinator-state.json"
LOG_FILE = LOG_DIR / "stack-sentinel.log"
LOCK_FILE = RUNTIME_DIR / "stack-sentinel.lock"
STATUS_URL = (
    os.environ.get("BDAG_SENTINEL_STATUS_URL")
    or os.environ.get("BDAG_SENTINEL_COLLECTOR_URL")
    or "http://127.0.0.1:9280/api/status"
)
STATUS_TIMEOUT = float(os.environ.get("BDAG_SENTINEL_STATUS_TIMEOUT", "20"))
INCIDENT_COOLDOWN_SECONDS = int(os.environ.get("BDAG_SENTINEL_INCIDENT_COOLDOWN_SECONDS", "300"))
SHARE_STALE_SECONDS = int(os.environ.get("BDAG_SENTINEL_SHARE_STALE_SECONDS", "180"))
NODE_LOG_LOOKBACK_SECONDS = int(os.environ.get("BDAG_SENTINEL_NODE_LOG_LOOKBACK_SECONDS", "300"))
NODE_LOG_SCAN_TIMEOUT_SECONDS = float(os.environ.get("BDAG_SENTINEL_NODE_LOG_SCAN_TIMEOUT_SECONDS", "12"))
ZERO_STATE_ROOT_WARN_COUNT = int(os.environ.get("BDAG_SENTINEL_ZERO_STATE_ROOT_WARN_COUNT", "3"))
ZERO_STATE_ROOT_CRITICAL_COUNT = int(os.environ.get("BDAG_SENTINEL_ZERO_STATE_ROOT_CRITICAL_COUNT", "20"))
POOL_START_STABLE_SAFE_SECONDS = int(os.environ.get("BDAG_SENTINEL_POOL_START_STABLE_SAFE_SECONDS", "90"))
DESKTOP_NOTIFY_ENABLED = os.environ.get("BDAG_SENTINEL_DESKTOP_NOTIFY", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

def split_csv_env(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


def unique_names(names: list[str]) -> list[str]:
    result: list[str] = []
    for name in names:
        if name and name not in result:
            result.append(name)
    return result


USER_SERVICES = split_csv_env(
    "BDAG_SENTINEL_USER_SERVICES",
    "bdag-status-sampler.service,bdag-watchdog.service,bdag-p2p-guard.service",
)
USER_TIMERS = split_csv_env(
    "BDAG_SENTINEL_USER_TIMERS",
    "bdag-stack-sentinel.timer,bdag-sync-coordinator.timer,bdag-chain-restore-guard.timer,"
    "bdag-local-peers.timer,bdag-mining-30min-guard.timer",
)

SENTINEL_OBSERVE_ONLY_STATES = {
    automation_control.STATE_TRANSITION_HOLD,
    automation_control.STATE_REPAIR_HOLD,
    automation_control.STATE_CONTROLLED_STOP,
    automation_control.STATE_CHAIN_INCIDENT,
}


def log(message: str) -> None:
    ensure_runtime()
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now_iso()}] {message}\n")


def timeout_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def read_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(state: dict[str, Any]) -> None:
    ensure_runtime()
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def acquire_run_lock() -> Any | None:
    ensure_runtime()
    handle = LOCK_FILE.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.write(f"{os.getpid()} {now_iso()}\n")
    handle.flush()
    return handle


def automation_action_for_container(service: str, recreate: bool = False) -> str:
    if service == POOL_CONTAINER:
        return automation_control.ACTION_ASIC_POOL_RESTART if recreate else automation_control.ACTION_ASIC_POOL_START
    return automation_control.ACTION_CONTAINER_RECREATE if recreate else automation_control.ACTION_CONTAINER_START


def status_api() -> tuple[dict[str, Any] | None, str]:
    try:
        return collect_stack_status(include_logs=True, collector_url=STATUS_URL, timeout=STATUS_TIMEOUT), ""
    except (StackStatusUnavailable, OSError, TimeoutError, json.JSONDecodeError) as exc:
        return None, str(exc)


def observe_only_control_state() -> tuple[bool, str]:
    control, status, reason = automation_control.read_control_state()
    if control is None or status != "ok":
        return False, reason
    state = str(control.get("state") or "")
    if state in SENTINEL_OBSERVE_ONLY_STATES:
        owner_unit = str(control.get("owner_unit") or control.get("owner") or "unknown")
        hold_reason = str(control.get("reason") or "")
        detail = f"automation control {state} owned by {owner_unit}"
        if hold_reason:
            detail = f"{detail}: {hold_reason}"
        return True, detail
    return False, ""


def compose_command(*args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--env-file",
        str(POOL_ENV_FILE),
        "-f",
        str(PROJECT_ROOT / "docker-compose.yml"),
        *args,
    ]


def compose_service_name(name: str) -> str:
    result = subprocess.run(
        ["docker", "inspect", "-f", '{{ index .Config.Labels "com.docker.compose.service" }}', name],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    service = result.stdout.strip() if result.returncode == 0 else ""
    if service and service != "<no value>":
        return service
    return name


def start_container(service: str, reason: str, state: dict[str, Any], now: int) -> bool:
    action = automation_action_for_container(service, recreate=False)
    if not automation_mutation_allowed(
        actor="sentinel",
        action=action,
        target=service,
        reason=reason,
        state=state,
        now=now,
        log=log,
        incident_source="stack-sentinel",
    ):
        return False
    if pool_start_gate.is_pool_target(service, POOL_CONTAINER):
        decision = pool_start_gate.pool_start_decision(pool_start_gate.read_latest_status_payload())
        if not decision.allowed:
            clear_pool_start_stability(state)
            state["pool_start_blocked_reason"] = decision.reason
            append_incident(
                "sentinel_pool_start_blocked",
                "warning",
                "stack-sentinel",
                f"Stack sentinel left {service} stopped: {decision.reason}",
                {"pool_container": service, "reason": decision.reason},
            )
            log(f"left {service} stopped by pool start gate: {decision.reason}")
            return False
        stable_blocked, stable_reason = pool_start_stability_blocked(state, now)
        if stable_blocked:
            state["pool_start_blocked_reason"] = stable_reason
            append_incident(
                "sentinel_pool_start_blocked",
                "warning",
                "stack-sentinel",
                f"Stack sentinel left {service} stopped: {stable_reason}",
                {"pool_container": service, "reason": stable_reason},
            )
            log(f"left {service} stopped by pool start stability gate: {stable_reason}")
            return False

    log_path = LOG_DIR / f"sentinel-start-{service}-{now}.log"
    compose_target = compose_service_name(service)
    result = run_logged(compose_command("start", compose_target), log_path, timeout=120)
    if not result.ok:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{now_iso()}] compose start failed for {service}; using direct docker start fallback\n")
        result = run_logged(["docker", "start", service], log_path, timeout=120)
    details = {"service": service, "reason": reason, "log_path": str(log_path), **result.as_dict()}
    if result.ok:
        append_incident(
            "sentinel_started_container",
            "warning",
            "stack-sentinel",
            f"Stack sentinel started {service}: {reason}",
            details,
        )
        log(f"started {service}: {reason}")
        return True
    append_incident(
        "sentinel_container_start_failed",
        "critical",
        "stack-sentinel",
        f"Stack sentinel could not start {service}: {reason}",
        details,
    )
    log(f"failed to start {service}: {reason} rc={result.returncode}")
    return False


def recreate_container(service: str, reason: str, state: dict[str, Any], now: int) -> bool:
    action = automation_action_for_container(service, recreate=True)
    if not automation_mutation_allowed(
        actor="sentinel",
        action=action,
        target=service,
        reason=reason,
        state=state,
        now=now,
        log=log,
        incident_source="stack-sentinel",
    ):
        return False
    if pool_start_gate.is_pool_target(service, POOL_CONTAINER):
        decision = pool_start_gate.pool_start_decision(pool_start_gate.read_latest_status_payload())
        if not decision.allowed:
            clear_pool_start_stability(state)
            state["pool_start_blocked_reason"] = decision.reason
            append_incident(
                "sentinel_pool_start_blocked",
                "warning",
                "stack-sentinel",
                f"Stack sentinel left {service} stopped: {decision.reason}",
                {"pool_container": service, "reason": decision.reason},
            )
            log(f"left {service} stopped by pool start gate: {decision.reason}")
            return False
        stable_blocked, stable_reason = pool_start_stability_blocked(state, now)
        if stable_blocked:
            state["pool_start_blocked_reason"] = stable_reason
            append_incident(
                "sentinel_pool_start_blocked",
                "warning",
                "stack-sentinel",
                f"Stack sentinel left {service} stopped: {stable_reason}",
                {"pool_container": service, "reason": stable_reason},
            )
            log(f"left {service} stopped by pool start stability gate: {stable_reason}")
            return False

    log_path = LOG_DIR / f"sentinel-recreate-{service}-{now}.log"
    compose_target = compose_service_name(service)
    result = run_logged(
        compose_command("up", "-d", "--no-deps", "--force-recreate", "--no-build", "--pull", "never", compose_target),
        log_path,
        timeout=180,
    )
    if not result.ok:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{now_iso()}] compose recreate failed for {service}; using direct docker start fallback\n")
        result = run_logged(["docker", "start", service], log_path, timeout=120)
    details = {"service": service, "reason": reason, "log_path": str(log_path), **result.as_dict()}
    if result.ok:
        append_incident(
            "sentinel_recreated_container",
            "warning",
            "stack-sentinel",
            f"Stack sentinel recreated {service}: {reason}",
            details,
        )
        log(f"recreated {service}: {reason}")
        return True
    append_incident(
        "sentinel_container_recreate_failed",
        "critical",
        "stack-sentinel",
        f"Stack sentinel could not recreate {service}: {reason}",
        details,
    )
    log(f"failed to recreate {service}: {reason} rc={result.returncode}")
    return False


def notify_user(title: str, body: str) -> None:
    if not DESKTOP_NOTIFY_ENABLED:
        log(f"desktop notification suppressed title={title!r}")
        return
    command = ["notify-send", title, body]
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":0")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
    subprocess.run(command, env=env, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def pool_start_blocked_by_status(status: dict[str, Any] | None) -> tuple[bool, str]:
    decision = pool_start_gate.pool_start_decision(status)
    return (not decision.allowed), decision.reason


def clear_pool_start_stability(state: dict[str, Any]) -> None:
    state.pop("pool_start_allowed_since", None)
    state.pop("pool_start_allowed_reason", None)


def pool_start_stability_blocked(state: dict[str, Any], now: int) -> tuple[bool, str]:
    if POOL_START_STABLE_SAFE_SECONDS <= 0:
        return False, ""
    try:
        allowed_since = int(state.get("pool_start_allowed_since"))
    except (TypeError, ValueError):
        allowed_since = now
        state["pool_start_allowed_since"] = allowed_since
    elapsed = max(0, now - allowed_since)
    state["pool_start_allowed_reason"] = "status safe; waiting stable pool-start window"
    if elapsed < POOL_START_STABLE_SAFE_SECONDS:
        return (
            True,
            (
                "pool start gate is safe, but waiting for a stable-safe window "
                f"({elapsed}/{POOL_START_STABLE_SAFE_SECONDS}s)"
            ),
        )
    return False, ""


def pool_start_blocked_by_status_with_stability(
    status: dict[str, Any] | None,
    state: dict[str, Any],
    now: int,
) -> tuple[bool, str]:
    decision = pool_start_gate.pool_start_decision(status)
    if not decision.allowed:
        clear_pool_start_stability(state)
        return True, decision.reason
    return pool_start_stability_blocked(state, now)


def inspect_and_repair_containers(status: dict[str, Any] | None, state: dict[str, Any], now: int) -> None:
    inspected = docker_inspect(SERVICES)
    critical_services = unique_names(
        [POOL_DB_CONTAINER, *NODES, POOL_CONTAINER]
    )
    stopped = [name for name in critical_services if not inspected.get(name, {}).get("running")]
    restarting = [
        name
        for name in critical_services
        if inspected.get(name, {}).get("status") == "restarting"
    ]
    state["stopped_containers"] = stopped
    state["restarting_containers"] = restarting
    pool_start_blocked, pool_start_blocked_reason = pool_start_blocked_by_status_with_stability(status, state, now)
    actionable_stopped = list(stopped)
    if pool_start_blocked and POOL_CONTAINER in actionable_stopped:
        actionable_stopped.remove(POOL_CONTAINER)
        state["pool_start_blocked_reason"] = pool_start_blocked_reason
        if should_emit(state, "pool_start_blocked", pool_start_blocked_reason, now, INCIDENT_COOLDOWN_SECONDS):
            append_incident(
                "sentinel_pool_start_blocked",
                "warning",
                "stack-sentinel",
                f"Stack sentinel left {POOL_CONTAINER} stopped: {pool_start_blocked_reason}",
                {"pool_container": POOL_CONTAINER, "reason": pool_start_blocked_reason},
            )
        log(f"left {POOL_CONTAINER} stopped: {pool_start_blocked_reason}")
    else:
        state.pop("pool_start_blocked_reason", None)
    state["actionable_stopped_containers"] = actionable_stopped
    if stopped and should_emit(state, "stopped_containers", ",".join(stopped), now, INCIDENT_COOLDOWN_SECONDS):
        append_incident(
            "sentinel_stopped_containers",
            "critical",
            "stack-sentinel",
            "Critical BlockDAG container(s) are stopped",
            {
                "stopped": stopped,
                "containers": inspected,
            },
        )
        notify_user("BlockDAG mining stack needs attention", f"Stopped containers: {', '.join(stopped)}")

    if POOL_DB_CONTAINER in stopped:
        start_container(POOL_DB_CONTAINER, "database container is stopped", state, now)
    for node in NODES:
        if node in stopped:
            start_container(node, "node container is stopped", state, now)
    if POOL_CONTAINER in stopped:
        if pool_start_blocked:
            log(f"skipping pool start for {POOL_CONTAINER}: {pool_start_blocked_reason}")
        else:
            start_container(POOL_CONTAINER, "ASIC pool container is stopped", state, now)

    if not status:
        return
    pool_health = status.get("pool_health") if isinstance(status.get("pool_health"), dict) else {}
    miner_health = status.get("miner_health") if isinstance(status.get("miner_health"), dict) else {}
    last_share_age = pool_health.get("last_valid_share_age_seconds")
    connected = int(miner_health.get("connected_count") or 0)
    if connected > 0 and isinstance(last_share_age, int) and last_share_age > SHARE_STALE_SECONDS:
        signature = f"{connected}:{last_share_age // 60}"
        if should_emit(state, "share_stale", signature, now, INCIDENT_COOLDOWN_SECONDS):
            append_incident(
                "sentinel_share_stale",
                "critical",
                "stack-sentinel",
                f"No accepted pool share for {last_share_age}s while {connected} miner(s) are connected",
                {"last_valid_share_age_seconds": last_share_age, "connected_miners": connected},
            )
            notify_user("BlockDAG share flow stalled", f"No accepted share for {last_share_age}s")


def check_node_log_red_flags(state: dict[str, Any], now: int) -> None:
    for node in NODES:
        try:
            result = subprocess.run(
                ["docker", "logs", "--since", f"{NODE_LOG_LOOKBACK_SECONDS}s", node],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=NODE_LOG_SCAN_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_text = timeout_text(exc.stdout)
            stderr_text = timeout_text(exc.stderr)
            signature = f"{node}:{NODE_LOG_LOOKBACK_SECONDS}:{NODE_LOG_SCAN_TIMEOUT_SECONDS}"
            if should_emit(state, f"node_log_scan_timeout_{node}", signature, now, INCIDENT_COOLDOWN_SECONDS):
                message = (
                    f"{node} docker log scan timed out after {NODE_LOG_SCAN_TIMEOUT_SECONDS:.1f}s "
                    f"over a {NODE_LOG_LOOKBACK_SECONDS}s lookback"
                )
                append_incident(
                    "node_log_scan_timeout",
                    "warning",
                    "stack-sentinel",
                    message,
                    {
                        "node": node,
                        "lookback_seconds": NODE_LOG_LOOKBACK_SECONDS,
                        "timeout_seconds": NODE_LOG_SCAN_TIMEOUT_SECONDS,
                        "stdout_bytes": len(stdout_text),
                        "stderr_bytes": len(stderr_text),
                    },
                )
                log(message)
            continue
        text = f"{result.stdout}\n{result.stderr}"
        zero_state_count = text.count("Zero state root hash")
        if zero_state_count < ZERO_STATE_ROOT_WARN_COUNT:
            continue
        severity = "critical" if zero_state_count >= ZERO_STATE_ROOT_CRITICAL_COUNT else "warning"
        # The count changes minute to minute during a reorg storm; rate-limit by node
        # and severity so the incident log stays useful instead of becoming the fault.
        signature = f"{node}:{severity}"
        if should_emit(state, f"zero_state_root_{node}", signature, now, INCIDENT_COOLDOWN_SECONDS):
            message = f"{node} logged {zero_state_count} zero-state-root warning(s) in the last {NODE_LOG_LOOKBACK_SECONDS}s"
            details = {
                "node": node,
                "zero_state_root_count": zero_state_count,
                "lookback_seconds": NODE_LOG_LOOKBACK_SECONDS,
                "returncode": result.returncode,
            }
            append_incident("node_zero_state_root_warnings", severity, "stack-sentinel", message, details)
            log(message)
            if severity == "critical":
                notify_user("BlockDAG node red-flag logs", message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="evaluate incidents without starting or repairing services")
    args = parser.parse_args(argv)
    ensure_runtime()
    lock_handle = acquire_run_lock()
    if lock_handle is None:
        log("another stack sentinel run is active; skipping this tick")
        return 0

    now = int(time.time())
    state = read_state()

    try:
        observe_only, observe_reason = observe_only_control_state()
        state["observe_only"] = observe_only
        if observe_only:
            state["observe_only_reason"] = observe_reason
            if should_emit(state, "observe_only", observe_reason, now, INCIDENT_COOLDOWN_SECONDS):
                append_incident(
                    "sentinel_observe_only",
                    "warning",
                    "stack-sentinel",
                    f"Stack sentinel is observing only: {observe_reason}",
                    {"reason": observe_reason},
                )
            log(f"observe-only mode: {observe_reason}")
        else:
            state.pop("observe_only_reason", None)

        if not args.dry_run and not observe_only:
            for unit in [*USER_SERVICES, *USER_TIMERS]:
                start_unit(unit, state, now, log=log, incident_source="stack-sentinel", cooldown_seconds=INCIDENT_COOLDOWN_SECONDS)
        elif observe_only:
            log("observe-only: skipped user service and timer start checks")
        else:
            log("dry-run: skipped user service and timer start checks")

        status, error = status_api()
        state["stack_status_ok"] = status is not None
        state["stack_status_error"] = error
        if status is None and should_emit(
            state,
            "stack_status_unavailable",
            error or "unknown",
            now,
            INCIDENT_COOLDOWN_SECONDS,
        ):
            append_incident(
                "sentinel_stack_status_unavailable",
                "critical",
                "stack-sentinel",
                "Stack status is unavailable to the stack sentinel",
                {"url": STATUS_URL, "error": error},
            )
            notify_user("BlockDAG stack status unavailable", error[:160] or "status source timed out")
        elif status is not None:
            overall = str(status.get("overall") or "")
            failures = status.get("failures") if isinstance(status.get("failures"), list) else []
            if overall == "down":
                signature = stable_failure_signature(failures)
                if should_emit(state, "stack_overall_down", signature, now, INCIDENT_COOLDOWN_SECONDS):
                    state["stack_overall_down_signature"] = signature
                    append_incident(
                        "sentinel_stack_overall_down",
                        "critical",
                        "stack-sentinel",
                        "Stack status is down",
                        {
                            "overall": overall,
                            "failures": failures,
                            "miner_failures": status.get("miner_failures"),
                            "stack_failures": status.get("stack_failures"),
                        },
                    )
                    notify_user("BlockDAG mining degradation", signature[:220])

        if not args.dry_run and not observe_only:
            inspect_and_repair_containers(status, state, now)
        elif observe_only:
            log("observe-only: skipped container repair actions")
        else:
            log("dry-run: skipped container repair actions")
        check_node_log_red_flags(state, now)
        state["updated_at"] = now_iso()
        write_state(state)
        return 0
    finally:
        lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())

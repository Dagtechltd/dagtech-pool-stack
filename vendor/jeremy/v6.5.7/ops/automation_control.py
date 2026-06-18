#!/usr/bin/env python3
"""Shared fail-closed automation control for BlockDAG ops mutators."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import time
from typing import Any


PROJECT_ROOT = (
    Path(os.environ.get("BDAG_PROJECT_ROOT") or Path(__file__).resolve().parents[1]).expanduser().resolve()
)
RUNTIME_DIR = Path(os.environ.get("BDAG_RUNTIME_DIR") or PROJECT_ROOT / "ops" / "runtime").expanduser()
if not RUNTIME_DIR.is_absolute():
    RUNTIME_DIR = PROJECT_ROOT / RUNTIME_DIR
RUNTIME_DIR = RUNTIME_DIR.resolve()

DEFAULT_STATE_PATH = RUNTIME_DIR / "automation-control.json"
DEFAULT_LOCK_PATH = RUNTIME_DIR / "automation-control.lock"
DEFAULT_EVENT_PATH = RUNTIME_DIR / "automation-control-events.jsonl"

SCHEMA_VERSION = 1
STATE_NORMAL = "normal"
STATE_TRANSITION_HOLD = "transition_hold"
STATE_REPAIR_HOLD = "repair_hold"
STATE_CONTROLLED_STOP = "controlled_stop"
STATE_CHAIN_INCIDENT = "chain_incident"
VALID_STATES = {
    STATE_NORMAL,
    STATE_TRANSITION_HOLD,
    STATE_REPAIR_HOLD,
    STATE_CONTROLLED_STOP,
    STATE_CHAIN_INCIDENT,
}
BLOCKING_STATES = {STATE_REPAIR_HOLD, STATE_CONTROLLED_STOP, STATE_CHAIN_INCIDENT}

ACTION_READ_STATUS = "read_status"
ACTION_WRITE_INCIDENT = "write_incident"
ACTION_STACK_START = "stack_start"
ACTION_STACK_RESTART = "stack_restart"
ACTION_STACK_CLEAN_RESTORE = "stack_clean_restore"
ACTION_NODE_RESTART = "node_restart"
ACTION_CONTAINER_START = "container_start"
ACTION_CONTAINER_RECREATE = "container_recreate"
ACTION_CONTAINER_RESTART = "container_restart"
ACTION_ASIC_MINER_OPEN_RESTART = "asic_miner_open_restart"
ACTION_ASIC_MINER_RESTART = "asic_miner_restart"
ACTION_ASIC_POOL_START = "asic_pool_start"
ACTION_ASIC_POOL_RESTART = "asic_pool_restart"
ACTION_CONFIG_EDIT = "config_edit"
ACTION_SYSTEMD_START = "systemd_start"
ACTION_SYSTEMD_RESTART = "systemd_restart"
ACTION_CONTAINMENT_STOP = "containment_stop"

HIGH_RISK_ACTIONS = {
    ACTION_STACK_START,
    ACTION_STACK_RESTART,
    ACTION_STACK_CLEAN_RESTORE,
    ACTION_NODE_RESTART,
    ACTION_CONTAINER_START,
    ACTION_CONTAINER_RECREATE,
    ACTION_CONTAINER_RESTART,
    ACTION_ASIC_MINER_OPEN_RESTART,
    ACTION_ASIC_MINER_RESTART,
    ACTION_ASIC_POOL_START,
    ACTION_ASIC_POOL_RESTART,
    ACTION_CONFIG_EDIT,
    ACTION_SYSTEMD_START,
    ACTION_SYSTEMD_RESTART,
}
LOW_RISK_ACTIONS = {ACTION_READ_STATUS, ACTION_WRITE_INCIDENT}
CONTAINMENT_ACTIONS = {ACTION_CONTAINMENT_STOP}


@dataclass(frozen=True)
class ControlDecision:
    allowed: bool
    action: str
    actor: str
    target: str
    control_state: str
    control_status: str
    reason: str
    control_path: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "action": self.action,
            "actor": self.actor,
            "target": self.target,
            "control_state": self.control_state,
            "control_status": self.control_status,
            "reason": self.reason,
            "control_path": self.control_path,
        }


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_iso_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def ensure_runtime(path: Path = RUNTIME_DIR) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _with_lock(lock_path: Path):
    ensure_runtime(lock_path.parent)
    handle = lock_path.open("a+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _state_path(path: Path | None = None) -> Path:
    return path or DEFAULT_STATE_PATH


def _lock_path(path: Path | None = None) -> Path:
    return path or DEFAULT_LOCK_PATH


def _event_path(path: Path | None = None) -> Path:
    return path or DEFAULT_EVENT_PATH


def _read_text_with_retry(path: Path) -> tuple[str | None, str]:
    for attempt in range(2):
        try:
            return path.read_text(encoding="utf-8"), ""
        except FileNotFoundError:
            return None, "missing"
        except OSError as exc:
            if attempt == 0:
                time.sleep(0.02)
                continue
            return None, f"read_error:{exc}"
    return None, "read_error"


def validate_control_state(raw: Any, now: datetime | None = None) -> tuple[dict[str, Any] | None, str, str]:
    if not isinstance(raw, dict):
        return None, "schema_invalid", "control state is not an object"
    if raw.get("schema_version") != SCHEMA_VERSION:
        return None, "schema_invalid", "schema_version must be 1"
    state = raw.get("state")
    if state not in VALID_STATES:
        return None, "schema_invalid", "state is missing or invalid"
    owner = raw.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        return None, "schema_invalid", "owner is missing or invalid"
    reason = raw.get("reason")
    if reason is not None and not isinstance(reason, str):
        return None, "schema_invalid", "reason must be a string when present"
    allowed = raw.get("allowed_mutations", [])
    if allowed is not None and (
        not isinstance(allowed, list) or any(not isinstance(item, str) for item in allowed)
    ):
        return None, "schema_invalid", "allowed_mutations must be a string list"
    expires_at = raw.get("expires_at")
    if expires_at is not None:
        if not isinstance(expires_at, str):
            return None, "schema_invalid", "expires_at must be a string or null"
        parsed = parse_iso_datetime(expires_at)
        if parsed is None:
            return None, "schema_invalid", "expires_at is not ISO-8601"
        if parsed <= (now or datetime.now(timezone.utc)):
            return None, "expired", "automation control is expired"
    return raw, "ok", "ok"


def read_control_state(
    *,
    state_path: Path | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any] | None, str, str]:
    path = _state_path(state_path)
    text, read_status = _read_text_with_retry(path)
    if text is None:
        return None, read_status, "automation control file is missing" if read_status == "missing" else read_status
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, "corrupt", f"automation control JSON is corrupt: {exc.msg}"
    return validate_control_state(raw, now=now)


def write_control_state(
    state: dict[str, Any],
    *,
    state_path: Path | None = None,
    lock_path: Path | None = None,
    now: datetime | None = None,
) -> None:
    path = _state_path(state_path)
    lock = _lock_path(lock_path)
    valid, status, reason = validate_control_state(state, now=now)
    if valid is None:
        raise ValueError(f"invalid automation control state: {status}: {reason}")
    ensure_runtime(path.parent)
    handle = _with_lock(lock)
    try:
        temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
        with temp_path.open("w", encoding="utf-8") as temp:
            temp.write(payload)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_path, path)
        try:
            dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def default_normal_control_state(
    *,
    owner: str,
    owner_unit: str,
    reason: str,
    correlation_id: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = (now or datetime.now(timezone.utc)).astimezone().isoformat(timespec="seconds")
    return {
        "schema_version": SCHEMA_VERSION,
        "state": STATE_NORMAL,
        "owner": owner,
        "owner_unit": owner_unit,
        "pid": os.getpid(),
        "reason": reason,
        "correlation_id": correlation_id or f"{owner_unit}-{int(time.time())}",
        "created_at": timestamp,
        "updated_at": timestamp,
        "expires_at": None,
        "allowed_mutations": [],
        "suppressed_count": 0,
        "last_transition": {"from": "missing", "to": STATE_NORMAL, "at": timestamp, "by": owner_unit},
    }


def ensure_normal_control_state(
    *,
    state_path: Path | None = None,
    lock_path: Path | None = None,
    owner: str = "installer",
    owner_unit: str = "automation-control",
    reason: str = "Provision default automation control state",
    correlation_id: str = "",
    repair_invalid: bool = False,
    now: datetime | None = None,
) -> tuple[bool, str, str]:
    path = _state_path(state_path)
    control, status, status_reason = read_control_state(state_path=path, now=now)
    if control is not None and status == "ok":
        return False, status, str(path)
    if status != "missing" and not repair_invalid:
        return False, status, str(path)
    state = default_normal_control_state(
        owner=owner,
        owner_unit=owner_unit,
        reason=reason,
        correlation_id=correlation_id,
        now=now,
    )
    previous = status
    state["last_transition"] = {
        "from": previous,
        "to": STATE_NORMAL,
        "at": state["updated_at"],
        "by": owner_unit,
        "reason": status_reason,
    }
    write_control_state(state, state_path=path, lock_path=lock_path, now=now)
    return True, previous, str(path)


def append_control_event(
    event: dict[str, Any],
    *,
    event_path: Path | None = None,
    lock_path: Path | None = None,
) -> None:
    path = _event_path(event_path)
    lock = _lock_path(lock_path)
    ensure_runtime(path.parent)
    handle = _with_lock(lock)
    try:
        payload = {"generated_at": now_iso(), **event}
        with path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
            out.flush()
            os.fsync(out.fileno())
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _transition_hold_allows(control: dict[str, Any], action: str, actor: str, target: str) -> bool:
    allowed = control.get("allowed_mutations")
    if not isinstance(allowed, list):
        return False
    if target:
        tokens = {
            f"{action}:{target}",
            f"{actor}:{action}:{target}",
            f"{action}:*",
            f"{actor}:{action}:*",
        }
    else:
        tokens = {
            action,
            f"{actor}:{action}",
        }
    return any(item in tokens for item in allowed)


def is_high_risk_action(action: str) -> bool:
    if action in LOW_RISK_ACTIONS or action in CONTAINMENT_ACTIONS:
        return False
    return True


def check_mutation_allowed(
    action: str,
    *,
    actor: str,
    target: str = "",
    reason: str = "",
    state_path: Path | None = None,
    event_path: Path | None = None,
    lock_path: Path | None = None,
    now: datetime | None = None,
    log_denial: bool = True,
) -> ControlDecision:
    path = _state_path(state_path)
    control, status, status_reason = read_control_state(state_path=path, now=now)
    high_risk = is_high_risk_action(action)

    if action in LOW_RISK_ACTIONS or action in CONTAINMENT_ACTIONS:
        control_state = str(control.get("state") if control else "invalid")
        return ControlDecision(
            True,
            action,
            actor,
            target,
            control_state,
            status,
            "allowed low-risk or containment action",
            str(path),
        )

    if status != "ok" or control is None:
        allowed = not high_risk
        decision = ControlDecision(
            allowed,
            action,
            actor,
            target,
            "invalid",
            status,
            status_reason if high_risk else "allowed non-high-risk action despite invalid control",
            str(path),
        )
        if not allowed and log_denial:
            maybe_log_denial_event(decision, requested_reason=reason, event_path=event_path, lock_path=lock_path)
        return decision

    state = str(control.get("state"))
    if state == STATE_NORMAL:
        return ControlDecision(True, action, actor, target, state, status, "normal control state", str(path))

    if state == STATE_TRANSITION_HOLD:
        allowed = high_risk and _transition_hold_allows(control, action, actor, target)
        if not high_risk:
            allowed = True
        decision = ControlDecision(
            allowed,
            action,
            actor,
            target,
            state,
            status,
            "transition_hold allow-list matched" if allowed else "transition_hold does not allow this mutation",
            str(path),
        )
        if not allowed and log_denial:
            maybe_log_denial_event(decision, requested_reason=reason, event_path=event_path, lock_path=lock_path)
        return decision

    if state in BLOCKING_STATES and high_risk:
        decision = ControlDecision(
            False,
            action,
            actor,
            target,
            state,
            status,
            f"automation control state {state} denies high-risk mutation",
            str(path),
        )
        if log_denial:
            maybe_log_denial_event(decision, requested_reason=reason, event_path=event_path, lock_path=lock_path)
        return decision

    return ControlDecision(True, action, actor, target, state, status, "allowed non-high-risk mutation", str(path))


def maybe_log_denial_event(
    decision: ControlDecision,
    *,
    requested_reason: str,
    event_path: Path | None = None,
    lock_path: Path | None = None,
) -> None:
    try:
        log_denial_event(
            decision,
            requested_reason=requested_reason,
            event_path=event_path,
            lock_path=lock_path,
        )
    except OSError:
        pass


def log_denial_event(
    decision: ControlDecision,
    *,
    requested_reason: str,
    event_path: Path | None = None,
    lock_path: Path | None = None,
) -> None:
    append_control_event(
        {
            "event_type": "automation_control_denied",
            "severity": "critical",
            "decision": decision.as_dict(),
            "requested_reason": requested_reason,
        },
        event_path=event_path,
        lock_path=lock_path,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the BlockDAG automation control gate.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    ensure_parser = subparsers.add_parser(
        "ensure-normal",
        help="Create a normal automation-control file only when it is missing.",
    )
    ensure_parser.add_argument("--state-path", type=Path, default=None)
    ensure_parser.add_argument("--lock-path", type=Path, default=None)
    ensure_parser.add_argument("--owner", default="installer")
    ensure_parser.add_argument("--owner-unit", default="automation-control")
    ensure_parser.add_argument("--reason", default="Provision default automation control state")
    ensure_parser.add_argument("--correlation-id", default="")
    ensure_parser.add_argument(
        "--repair-invalid",
        action="store_true",
        help="Replace an invalid/expired control file with normal state. Use only during explicit recovery.",
    )
    args = parser.parse_args(argv)

    if args.command == "ensure-normal":
        created, previous_status, path = ensure_normal_control_state(
            state_path=args.state_path,
            lock_path=args.lock_path,
            owner=args.owner,
            owner_unit=args.owner_unit,
            reason=args.reason,
            correlation_id=args.correlation_id,
            repair_invalid=args.repair_invalid,
        )
        print(
            json.dumps(
                {
                    "created": created,
                    "previous_status": previous_status,
                    "path": path,
                },
                sort_keys=True,
            )
        )
        return 0
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

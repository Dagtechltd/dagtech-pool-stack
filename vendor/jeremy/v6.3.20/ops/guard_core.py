#!/usr/bin/env python3
"""Shared guard helpers for the stack watchdog and stack sentinel."""

from __future__ import annotations

import re
import subprocess
from typing import Any, Callable

import automation_control
from incident_journal import append_incident
from pool_ops import now_iso


FAILURE_AGE_RE = re.compile(r"\bfor \d+s\b")


def should_emit(state: dict[str, Any], key: str, signature: str, now: int, cooldown_seconds: int) -> bool:
    last_signature = str(state.get(f"{key}_signature") or "")
    last_epoch = int(state.get(f"{key}_epoch", 0) or 0)
    if last_signature == signature and now - last_epoch < cooldown_seconds:
        return False
    state[f"{key}_signature"] = signature
    state[f"{key}_epoch"] = now
    state[f"{key}_at"] = now_iso()
    return True


def stable_failure_signature(failures: list[Any]) -> str:
    parts = []
    for item in failures[:8]:
        parts.append(FAILURE_AGE_RE.sub("for Ns", str(item)))
    return " | ".join(parts) or "overall-down"


def automation_mutation_allowed(
    *,
    actor: str,
    action: str,
    target: str,
    reason: str,
    state: dict[str, Any] | None = None,
    now: int | None = None,
    log: Callable[[str], None],
    incident_source: str,
    cooldown_seconds: int = 300,
) -> bool:
    decision = automation_control.check_mutation_allowed(
        action,
        actor=actor,
        target=target,
        reason=reason,
    )
    if decision.allowed:
        return True
    message = f"{actor} suppressed {action} for {target}: {decision.reason}"
    if state is None or now is None or should_emit(
        state,
        f"automation_control_{action}_{target}",
        decision.reason,
        now,
        cooldown_seconds,
    ):
        append_incident(
            "automation_control_suppressed",
            "critical",
            incident_source,
            message,
            decision.as_dict(),
        )
    log(message)
    return False


def systemctl_user(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def unit_active(unit: str) -> bool:
    result = systemctl_user("is-active", unit)
    return result.returncode == 0 and result.stdout.strip() == "active"


def start_unit(
    unit: str,
    state: dict[str, Any],
    now: int,
    *,
    log: Callable[[str], None],
    incident_source: str,
    cooldown_seconds: int = 300,
) -> None:
    if unit_active(unit):
        return
    if not automation_mutation_allowed(
        actor="sentinel",
        action=automation_control.ACTION_SYSTEMD_START,
        target=unit,
        reason="Stack sentinel user unit start",
        state=state,
        now=now,
        log=log,
        incident_source=incident_source,
        cooldown_seconds=cooldown_seconds,
    ):
        return
    result = systemctl_user("start", unit)
    details = {
        "unit": unit,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }
    event_type = "sentinel_started_unit" if result.returncode == 0 else "sentinel_unit_start_failed"
    severity = "warning" if result.returncode == 0 else "critical"
    message = f"Stack sentinel {'started' if result.returncode == 0 else 'could not start'} {unit}"
    if should_emit(state, event_type + "_" + unit.replace(".", "_"), str(result.returncode), now, cooldown_seconds):
        append_incident(event_type, severity, incident_source, message, details)
    log(f"{message} rc={result.returncode}")

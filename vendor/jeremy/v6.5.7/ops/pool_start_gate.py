#!/usr/bin/env python3
"""Single source of truth for deciding whether the ASIC pool may start."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import time
from typing import Any

import automation_control


PROJECT_ROOT = (
    Path(os.environ.get("BDAG_PROJECT_ROOT") or Path(__file__).resolve().parents[1]).expanduser().resolve()
)
RUNTIME_DIR = Path(os.environ.get("BDAG_RUNTIME_DIR") or PROJECT_ROOT / "ops" / "runtime").expanduser()
if not RUNTIME_DIR.is_absolute():
    RUNTIME_DIR = PROJECT_ROOT / RUNTIME_DIR
RUNTIME_DIR = RUNTIME_DIR.resolve()

STATUS_SAMPLER_FILE = RUNTIME_DIR / "status-sampler.json"
DEFAULT_STATUS_MAX_AGE_SECONDS = float(os.environ.get("BDAG_POOL_START_GATE_STATUS_MAX_AGE_SECONDS", "180"))
REQUIRE_CANONICAL_SAFETY = str(
    os.environ.get("BDAG_POOL_START_GATE_REQUIRE_CANONICAL_SAFETY", "1")
).strip().lower() not in {"0", "false", "no", "off"}
UNSAFE_MODES = {"catchup_pause", "syncing", "unknown", "waiting_for_status_sample"}
READY_DOWN_MODES = {"synced", "mining", "ready_no_miners"}


@dataclass(frozen=True)
class PoolStartGateDecision:
    allowed: bool
    reasons: tuple[str, ...]
    status_source: str
    status_age_seconds: float | None = None

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sync_progress_lag_blocks(status: dict[str, Any]) -> int:
    sync_progress = status.get("sync_progress")
    if not isinstance(sync_progress, dict):
        return 0
    values: list[int] = []
    for key in ("remaining_blocks", "peer_ahead_blocks"):
        value = _safe_int(sync_progress.get(key))
        if value is not None and value >= 0:
            values.append(value)
    nodes = sync_progress.get("nodes")
    if isinstance(nodes, dict):
        for node in nodes.values():
            if not isinstance(node, dict):
                continue
            for key in ("remaining_blocks", "peer_ahead_blocks"):
                value = _safe_int(node.get(key))
                if value is not None and value >= 0:
                    values.append(value)
    return max(values) if values else 0


def _unwrap_status_payload(raw: Any) -> tuple[dict[str, Any] | None, str, float | None]:
    if not isinstance(raw, dict):
        return None, "invalid", None
    if isinstance(raw.get("payload"), dict):
        age = _safe_float(raw.get("epoch"))
        return dict(raw["payload"]), "status-sampler", age
    return dict(raw), "direct", None


def read_latest_status_payload(
    *,
    status_path: Path | None = None,
    max_age_seconds: float | None = DEFAULT_STATUS_MAX_AGE_SECONDS,
) -> dict[str, Any] | None:
    path = status_path or STATUS_SAMPLER_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    payload, _source, sampled_epoch = _unwrap_status_payload(raw)
    if payload is None:
        return None
    if sampled_epoch is not None and max_age_seconds is not None:
        age = max(0.0, time.time() - sampled_epoch)
        if age > max_age_seconds:
            payload = dict(payload)
            payload["fresh"] = False
            payload["pool_start_gate_stale_age_seconds"] = round(age, 3)
    return payload


def is_pool_target(target: str, pool_container: str | None = None) -> bool:
    token = str(target or "").strip().lower()
    if not token:
        return False
    known = {
        "pool",
        "asic-pool",
        str(pool_container or os.environ.get("BDAG_POOL_CONTAINER") or "").strip().lower(),
    }
    if token in {item for item in known if item}:
        return True
    return bool(re.search(r"(^|[-_])pool[-_]1$", token))


def _canonical_safety_payloads(status: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    payloads: list[tuple[str, dict[str, Any]]] = []

    def append(scope: str, value: Any) -> None:
        if isinstance(value, dict):
            payloads.append((scope, value))

    append("status", status.get("canonical_mining_safety"))
    sync_health = status.get("sync_health")
    if isinstance(sync_health, dict):
        append("sync_health", sync_health.get("canonical_mining_safety"))
    sync_progress = status.get("sync_progress")
    if isinstance(sync_progress, dict):
        append("sync_progress", sync_progress.get("canonical_mining_safety"))
        nodes = sync_progress.get("nodes")
        if isinstance(nodes, dict):
            for name, node in nodes.items():
                if isinstance(node, dict):
                    append(f"node:{name}", node.get("canonical_mining_safety"))
        elif isinstance(nodes, list):
            for index, node in enumerate(nodes):
                if isinstance(node, dict):
                    append(f"node:{index}", node.get("canonical_mining_safety"))
    return payloads


def canonical_safety_proven(status: dict[str, Any]) -> tuple[bool, str]:
    payloads = _canonical_safety_payloads(status)
    if not payloads:
        return False, "canonical public-chain safety proof is missing"
    safe_scopes = [scope for scope, payload in payloads if payload.get("safe") is True]
    if safe_scopes:
        return True, f"canonical public-chain safety proof accepted from {', '.join(safe_scopes)}"
    details: list[str] = []
    for scope, payload in payloads[:4]:
        reason = str(payload.get("reason") or payload.get("status") or "unsafe").strip()
        details.append(f"{scope}: {reason}")
    suffix = f": {'; '.join(details)}" if details else ""
    return False, f"canonical public-chain safety proof is unsafe{suffix}"


def pool_start_decision(status: dict[str, Any] | None, *, status_source: str = "direct") -> PoolStartGateDecision:
    if not isinstance(status, dict):
        return PoolStartGateDecision(False, ("stack status unavailable; cannot prove pool start is safe",), status_source)

    status, source, sampled_epoch = _unwrap_status_payload(status)
    if status is None:
        return PoolStartGateDecision(False, ("stack status unavailable; cannot prove pool start is safe",), source)

    reasons: list[str] = []
    age_seconds = _safe_float(status.get("age_seconds"))
    stale_after = _safe_float(status.get("stale_after_seconds"))
    if sampled_epoch is not None:
        age_seconds = max(0.0, time.time() - sampled_epoch)
    if status.get("fresh") is False:
        reasons.append("stack status is stale; cannot prove pool start is safe")
    elif age_seconds is not None and stale_after is not None and age_seconds > stale_after:
        reasons.append("stack status is stale; cannot prove pool start is safe")

    sync_health = status.get("sync_health") if isinstance(status.get("sync_health"), dict) else {}
    catchup_policy = status.get("catchup_policy") if isinstance(status.get("catchup_policy"), dict) else {}
    if sync_health.get("public_chain_divergence") or sync_health.get("public_chain_divergence_nodes"):
        reasons.append("public-chain divergence containment is active")
    if catchup_policy.get("active") or sync_health.get("catchup_pause_active"):
        reasons.append("chain catch-up pause is active")

    sync_progress = status.get("sync_progress") if isinstance(status.get("sync_progress"), dict) else {}
    sync_status = str(sync_progress.get("status") or "").strip().lower()
    sync_lag = _sync_progress_lag_blocks(status)
    if sync_status in {"syncing", "catchup_pause"}:
        if sync_lag > 0:
            reasons.append(f"sync progress is {sync_status} with {sync_lag} block(s) remaining")
        else:
            reasons.append(f"sync progress is {sync_status}")

    mode = str(status.get("mode") or "").strip().lower()
    overall = str(status.get("overall") or "").strip().lower()
    if mode in UNSAFE_MODES:
        reasons.append(f"status mode is not safe for pool start: {mode}")
    if overall == "syncing":
        reasons.append("overall stack status is syncing")
    if overall == "down" and mode not in READY_DOWN_MODES:
        reasons.append(f"overall stack status is down with non-ready mode: {mode or 'unknown'}")

    rpc_template = status.get("rpc_template_health")
    if isinstance(rpc_template, dict) and rpc_template.get("all_nodes_ready") is False:
        reasons.append("node template health is not ready")

    if REQUIRE_CANONICAL_SAFETY:
        safe, canonical_reason = canonical_safety_proven(status)
        if not safe:
            reasons.append(canonical_reason)

    status_reason = str(status.get("status_reason") or "")
    degraded_reasons = status.get("degraded_reasons")
    reason_text = (status_reason + " " + " ".join(str(item) for item in degraded_reasons or [])).lower()
    text_blockers = (
        ("public-chain divergence", "public-chain divergence is reported in status"),
        ("catch-up pause active", "chain catch-up pause is reported in status"),
        ("node_syncing", "node template health reports node_syncing"),
        ("node busy syncing", "node log reports node busy syncing"),
        ("bdag pool syncing", "node log reports bdag pool syncing"),
        ("client in initial download", "node reports initial download"),
    )
    for needle, message in text_blockers:
        if needle in reason_text and message not in reasons:
            reasons.append(message)

    if reasons:
        return PoolStartGateDecision(False, tuple(reasons), source, age_seconds)
    return PoolStartGateDecision(True, (), source, age_seconds)


def pool_start_allowed_by_control(
    *,
    action: str,
    actor: str,
    target: str,
    reason: str,
    status: dict[str, Any] | None,
) -> tuple[bool, str, automation_control.ControlDecision | None]:
    decision = automation_control.check_mutation_allowed(
        action,
        actor=actor,
        target=target,
        reason=reason,
    )
    if not decision.allowed:
        return False, decision.reason, decision
    gate = pool_start_decision(status)
    if not gate.allowed:
        return False, gate.reason, decision
    return True, "", decision

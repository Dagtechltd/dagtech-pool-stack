#!/usr/bin/env python3
"""Pure mining-health triage helpers for watchdog-style repair actors."""

from __future__ import annotations

from typing import Any


PRIMARY_ISSUE_ORDER = (
    "docker_unavailable",
    "pool_start_blocked",
    "stack_down",
    "node_orphan_error_storm",
    "pool_submit_path_self_healed",
    "pool_submit_path_stall",
    "asic_hashrate_issue",
    "asic_degraded",
    "share_stall",
    "pool_template_frozen",
    "duplicate_block_storm",
    "unknown",
)


def build_mining_health_triage(**payload: Any) -> dict[str, Any]:
    triage = dict(payload)
    stack_failures = list(triage.get("stack_failures") or [])
    pool_start_blocked = bool(triage.get("pool_start_blocked"))
    docker_access_error = triage.get("docker_access_error")
    orphan_nodes = list(triage.get("orphan_nodes") or [])
    submit_path_self_healed_recently = bool(triage.get("submit_path_self_healed_recently"))
    submit_path_zero_success_storm = bool(triage.get("submit_path_zero_success_storm"))
    accepted_job_expired_storm = bool(triage.get("accepted_job_expired_storm"))
    expired_job_reconnect_failed = bool(triage.get("expired_job_reconnect_failed"))
    hashrate_issue_asics = list(triage.get("hashrate_issue_asics") or [])
    degraded_asics = list(triage.get("degraded_asics") or [])
    share_stall = bool(triage.get("share_stall"))
    pool_template_frozen = bool(triage.get("pool_template_frozen"))
    duplicate_block_storm = bool(triage.get("duplicate_block_storm"))

    if docker_access_error:
        primary_issue = "docker_unavailable"
    elif pool_start_blocked:
        primary_issue = "pool_start_blocked"
    elif stack_failures:
        primary_issue = "stack_down"
    elif orphan_nodes:
        primary_issue = "node_orphan_error_storm"
    elif submit_path_self_healed_recently:
        primary_issue = "pool_submit_path_self_healed"
    elif submit_path_zero_success_storm or accepted_job_expired_storm or expired_job_reconnect_failed:
        primary_issue = "pool_submit_path_stall"
    elif hashrate_issue_asics:
        primary_issue = "asic_hashrate_issue"
    elif degraded_asics:
        primary_issue = "asic_degraded"
    elif share_stall:
        primary_issue = "share_stall"
    elif pool_template_frozen:
        primary_issue = "pool_template_frozen"
    elif duplicate_block_storm:
        primary_issue = "duplicate_block_storm"
    else:
        primary_issue = "unknown"

    triage["primary_issue"] = primary_issue
    triage["primary_issue_rank"] = PRIMARY_ISSUE_ORDER.index(primary_issue) if primary_issue in PRIMARY_ISSUE_ORDER else len(PRIMARY_ISSUE_ORDER)
    triage["has_actionable_issue"] = primary_issue != "unknown"
    triage["needs_repair"] = primary_issue not in {"unknown", "pool_submit_path_self_healed"}
    return triage

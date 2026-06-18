#!/usr/bin/env python3
"""Thirty-minute proof-of-health check for the BlockDAG ASIC mining path."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from incident_journal import append_incident
from pool_ops import LOG_DIR, RUNTIME_DIR, ensure_runtime, now_iso
from stack_status_source import StackStatusUnavailable, collect_stack_status


def load_runtime_env_defaults() -> None:
    env_path = RUNTIME_DIR / "ops.env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"')


load_runtime_env_defaults()


STATUS_URL = (
    os.environ.get("BDAG_MINING_GUARD_STATUS_URL")
    or os.environ.get("BDAG_MINING_GUARD_COLLECTOR_URL")
    or "http://127.0.0.1:9280/api/status"
)
STATUS_TIMEOUT = float(os.environ.get("BDAG_MINING_GUARD_STATUS_TIMEOUT", "20"))
EXPECTED_ASIC_IP = os.environ.get("BDAG_EXPECTED_ASIC_IP", "").strip()
EXPECTED_WALLET = os.environ.get("BDAG_EXPECTED_MINING_WALLET", "").strip()
MINING_GUARD_ENABLED = os.environ.get(
    "BDAG_MINING_GUARD_ENABLED",
    os.environ.get("BDAG_ENABLE_NODE_MINING", "0"),
).strip().lower() in {"1", "true", "yes", "on"}
INCIDENT_COOLDOWN_SECONDS = int(os.environ.get("BDAG_MINING_GUARD_INCIDENT_COOLDOWN_SECONDS", "1800"))
SHARE_STALE_SECONDS = int(os.environ.get("BDAG_MINING_GUARD_SHARE_STALE_SECONDS", "900"))
SYNC_PROGRESS_LOOKBACK_SECONDS = int(os.environ.get("BDAG_MINING_GUARD_SYNC_PROGRESS_LOOKBACK_SECONDS", "2700"))
SOURCE_BRANCH = os.environ.get("BDAG_MINING_GUARD_SOURCE_BRANCH", "develop").strip() or "develop"
SOURCE_REPOS = [
    Path(item).expanduser()
    for item in os.environ.get(
        "BDAG_MINING_GUARD_SOURCE_REPOS",
        os.pathsep.join(
            [
                "/home/jeremy/blockdag-source/pool-stack-docker",
                "/home/jeremy/blockdag-source/pool",
                "/home/jeremy/blockdag-source/dashboard",
            ]
        ),
    ).split(os.pathsep)
    if item.strip()
]

STATE_FILE = RUNTIME_DIR / "mining-30min-guard-state.json"
HISTORY_FILE = RUNTIME_DIR / "mining-30min-guard-history.jsonl"
LOG_FILE = LOG_DIR / "mining-30min-guard.log"


def log(message: str) -> None:
    ensure_runtime()
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now_iso()}] {message}\n")


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_runtime()
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def append_history(payload: dict[str, Any]) -> None:
    ensure_runtime()
    with HISTORY_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def run_command(args: list[str], *, timeout: int = 30) -> dict[str, Any]:
    try:
        result = subprocess.run(
            args,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {"returncode": -1, "stdout": "", "stderr": str(exc)}


def git_value(repo: Path, *args: str, timeout: int = 15) -> str:
    result = run_command(["git", "-C", str(repo), *args], timeout=timeout)
    return str(result.get("stdout") or "").strip()


def source_repo_triage(repo: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {"path": str(repo), "branch_target": SOURCE_BRANCH}
    if not repo.exists():
        payload.update({"available": False, "error": "path-missing"})
        return payload
    inside = git_value(repo, "rev-parse", "--is-inside-work-tree")
    if inside != "true":
        payload.update({"available": False, "error": "not-a-git-worktree"})
        return payload

    fetch = run_command(["git", "-C", str(repo), "fetch", "--quiet", "origin", SOURCE_BRANCH], timeout=45)
    head = git_value(repo, "rev-parse", "--short=12", "HEAD")
    remote_ref = f"origin/{SOURCE_BRANCH}"
    remote = git_value(repo, "rev-parse", "--short=12", remote_ref)
    behind = git_value(repo, "rev-list", "--count", f"HEAD..{remote_ref}")
    ahead = git_value(repo, "rev-list", "--count", f"{remote_ref}..HEAD")
    status = git_value(repo, "status", "--short")
    branch = git_value(repo, "branch", "--show-current")
    upstream_commits = git_value(repo, "log", "--oneline", "--decorate", "-8", f"HEAD..{remote_ref}")
    payload.update(
        {
            "available": True,
            "fetch_returncode": fetch.get("returncode"),
            "fetch_error": fetch.get("stderr"),
            "branch": branch or "(detached)",
            "head": head,
            "remote_ref": remote_ref,
            "remote_head": remote,
            "behind_count": as_int(behind, -1),
            "ahead_count": as_int(ahead, -1),
            "dirty": bool(status),
            "status_short": status.splitlines()[:20],
            "recent_upstream_commits": upstream_commits.splitlines()[:8],
        }
    )
    return payload


def source_freshness_triage() -> dict[str, Any]:
    return {
        "generated_at": now_iso(),
        "policy": "fetch-only; background guard records upstream state but does not edit, build, commit, push, or restart",
        "repos": [source_repo_triage(repo) for repo in SOURCE_REPOS],
    }


def as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def status_api() -> tuple[dict[str, Any] | None, str]:
    try:
        return collect_stack_status(include_logs=True, collector_url=STATUS_URL, timeout=STATUS_TIMEOUT), ""
    except (StackStatusUnavailable, OSError, TimeoutError, json.JSONDecodeError) as exc:
        return None, str(exc)


def fallback_status() -> tuple[dict[str, Any] | None, str]:
    status, error = status_api()
    if status is not None:
        return status, ""
    return None, error


def find_expected_miner(status: dict[str, Any]) -> dict[str, Any]:
    miners = ((status.get("miner_health") or {}).get("miners") or [])
    if not EXPECTED_ASIC_IP:
        return miners[0] if len(miners) == 1 and isinstance(miners[0], dict) else {}
    for miner in miners:
        if isinstance(miner, dict) and str(miner.get("ip") or "") == EXPECTED_ASIC_IP:
            return miner
    return {}


def wallet_seen(miner: dict[str, Any]) -> bool:
    if not EXPECTED_WALLET:
        return True
    wallet = EXPECTED_WALLET.lower()
    candidates: list[str] = []
    for key in ("expected_worker_user", "worker", "username", "user"):
        if miner.get(key):
            candidates.append(str(miner.get(key)))
    for key in ("workers", "last_workers", "recent_workers"):
        value = miner.get(key)
        if isinstance(value, list):
            candidates.extend(str(item) for item in value)
    return any(wallet in item.lower() for item in candidates)


def miner_hashrate(miner: dict[str, Any]) -> float | None:
    debug = miner.get("debug") if isinstance(miner.get("debug"), dict) else {}
    for key in ("hashrate", "av_hashrate"):
        value = as_float(debug.get(key))
        if value is not None:
            return value
    return None


def build_sample(status: dict[str, Any] | None, error: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    now = int(time.time())
    sample: dict[str, Any] = {
        "generated_at": now_iso(),
        "generated_epoch": now,
        "guard_state": "critical" if status is None else "ok",
        "problems": [],
        "status_error": error,
    }
    if status is None:
        sample["problems"].append("status-unavailable")
        return sample

    pool = status.get("pool_health") if isinstance(status.get("pool_health"), dict) else {}
    sync = status.get("sync_progress") if isinstance(status.get("sync_progress"), dict) else {}
    sync_health = status.get("sync_health") if isinstance(status.get("sync_health"), dict) else {}
    miner_health = status.get("miner_health") if isinstance(status.get("miner_health"), dict) else {}
    miner = find_expected_miner(status)
    hashrate = miner_hashrate(miner)
    expected_asic_required = bool(EXPECTED_ASIC_IP)

    current_block = as_int(sync.get("current_block"), -1)
    highest_block = as_int(sync.get("highest_block"), -1)
    remaining_blocks = as_int(sync.get("remaining_blocks"), max(0, highest_block - current_block))
    sync_status = str(sync.get("status") or "unknown")
    active_imports = as_int(sync_health.get("nodes_with_recent_imports"), 0)
    previous_sample = (state or {}).get("last_sample") if isinstance((state or {}).get("last_sample"), dict) else {}
    previous_current_block = as_int(previous_sample.get("current_block"), -1)
    previous_epoch = as_int(previous_sample.get("generated_epoch"), 0)
    height_progress_since_last_check = bool(
        previous_current_block >= 0
        and current_block > previous_current_block
        and previous_epoch > 0
        and now - previous_epoch <= SYNC_PROGRESS_LOOKBACK_SECONDS
    )
    effective_active_imports = active_imports
    if active_imports <= 0 and height_progress_since_last_check:
        effective_active_imports = 1
    initial_download = bool(pool.get("initial_download"))
    job_notify_count = as_int(pool.get("job_notify_count"), 0)
    connected_miners = as_int(pool.get("connected_miners"), as_int(miner_health.get("connected_count"), 0))
    last_valid_share_age = pool.get("last_valid_share_age_seconds")
    last_job_notify_age = pool.get("last_job_notify_age_seconds")
    recent_valid_share = isinstance(last_valid_share_age, (int, float)) and int(last_valid_share_age) <= SHARE_STALE_SECONDS
    recent_job_notify = isinstance(last_job_notify_age, (int, float)) and int(last_job_notify_age) <= 120
    transient_initial_download = bool(sync_health.get("pool_initial_download_transient")) or (
        initial_download
        and sync_status == "synced"
        and remaining_blocks == 0
        and connected_miners > 0
        and (recent_valid_share or recent_job_notify)
    )
    effective_initial_download = initial_download and not transient_initial_download

    sample.update(
        {
            "overall": status.get("overall"),
            "sync_status": sync_status,
            "current_block": current_block,
            "highest_block": highest_block,
            "remaining_blocks": remaining_blocks,
            "sync_percent": sync.get("percent"),
            "nodes_with_recent_imports": active_imports,
            "effective_nodes_with_recent_imports": effective_active_imports,
            "height_progress_since_last_check": height_progress_since_last_check,
            "previous_current_block": previous_current_block if previous_current_block >= 0 else None,
            "pool_selected_backend": pool.get("selected_backend"),
            "pool_initial_download": initial_download,
            "pool_initial_download_transient": transient_initial_download,
            "pool_connected_miners": connected_miners,
            "pool_job_notify_count": job_notify_count,
            "pool_last_job_notify_age_seconds": last_job_notify_age,
            "pool_valid_share_count": pool.get("valid_share_count"),
            "pool_last_valid_share_age_seconds": last_valid_share_age,
            "mining_guard_enabled": MINING_GUARD_ENABLED,
            "asic_ip": EXPECTED_ASIC_IP,
            "expected_asic_required": expected_asic_required,
            "asic_present": bool(miner),
            "asic_configured": bool(miner.get("configured")) if miner else False,
            "asic_connected": bool(miner.get("connected")) if miner else False,
            "asic_pool_active": bool(miner.get("pool_active")) if miner else False,
            "asic_hashrate": hashrate,
            "wallet_seen_for_asic": wallet_seen(miner) if miner else False,
        }
    )

    problems: list[str] = []
    critical = False

    if str(status.get("overall") or "") == "down":
        problems.append("status-overall-down")
        critical = True

    if sync_status != "synced" or remaining_blocks > 0 or effective_initial_download:
        problems.append("node-not-synced")
        if effective_active_imports <= 0:
            critical = True
            problems.append("sync-not-importing")

    if MINING_GUARD_ENABLED:
        if expected_asic_required and not miner:
            problems.append("expected-asic-missing")
            critical = True
        elif expected_asic_required:
            if not miner.get("configured"):
                problems.append("expected-asic-not-configured")
                critical = True
            if not miner.get("connected"):
                problems.append("expected-asic-not-connected")
                critical = True
            if not wallet_seen(miner):
                problems.append("expected-wallet-not-seen")
                critical = True

        if connected_miners <= 0:
            problems.append("pool-has-no-connected-miners")
            critical = True

        if job_notify_count <= 0 and not recent_valid_share:
            problems.append("pool-has-not-issued-jobs")
            if not effective_initial_download:
                critical = True

        if sync_status == "synced" and job_notify_count > 0 and connected_miners > 0:
            if isinstance(last_valid_share_age, (int, float)) and int(last_valid_share_age) > SHARE_STALE_SECONDS:
                problems.append("valid-share-stale")
                critical = True
            elif last_valid_share_age is None:
                problems.append("no-valid-share-recorded")
                critical = True
            if hashrate is not None and hashrate <= 0:
                problems.append("asic-zero-hashrate")
                critical = True

    sample["problems"] = problems
    if critical:
        sample["guard_state"] = "critical"
    elif problems:
        sample["guard_state"] = "warning"
    else:
        sample["guard_state"] = "ok"
    return sample


def should_emit(state: dict[str, Any], sample: dict[str, Any]) -> bool:
    if sample.get("guard_state") == "ok":
        return False
    now = int(sample.get("generated_epoch") or time.time())
    signature = json.dumps(
        {
            "guard_state": sample.get("guard_state"),
            "problems": sample.get("problems"),
            "sync_status": sample.get("sync_status"),
            "pool_initial_download": sample.get("pool_initial_download"),
            "asic_connected": sample.get("asic_connected"),
        },
        sort_keys=True,
    )
    last_signature = str(state.get("last_incident_signature") or "")
    last_epoch = int(state.get("last_incident_epoch", 0) or 0)
    if signature == last_signature and now - last_epoch < INCIDENT_COOLDOWN_SECONDS:
        return False
    state["last_incident_signature"] = signature
    state["last_incident_epoch"] = now
    return True


def run_once(dry_run: bool = False) -> dict[str, Any]:
    ensure_runtime()
    state = read_json(STATE_FILE)
    status, error = fallback_status()
    sample = build_sample(status, error, state)
    sample["dry_run"] = dry_run
    source_triage: dict[str, Any] = {}
    if sample.get("guard_state") != "ok":
        source_triage = source_freshness_triage()
        sample["source_freshness"] = source_triage
    if should_emit(state, sample):
        append_incident(
            "mining_30min_guard",
            "critical" if sample.get("guard_state") == "critical" else "warning",
            "mining-30min-guard",
            "Thirty-minute mining guard detected a condition that prevents full ASIC mining",
            {
                "guard_state": sample.get("guard_state"),
                "problems": sample.get("problems"),
                "sync_status": sample.get("sync_status"),
                "current_block": sample.get("current_block"),
                "highest_block": sample.get("highest_block"),
                "remaining_blocks": sample.get("remaining_blocks"),
                "pool_selected_backend": sample.get("pool_selected_backend"),
                "pool_initial_download": sample.get("pool_initial_download"),
                "pool_connected_miners": sample.get("pool_connected_miners"),
                "pool_job_notify_count": sample.get("pool_job_notify_count"),
                "expected_asic_required": sample.get("expected_asic_required"),
                "asic_ip": sample.get("asic_ip"),
                "asic_configured": sample.get("asic_configured"),
                "asic_connected": sample.get("asic_connected"),
                "asic_pool_active": sample.get("asic_pool_active"),
                "asic_hashrate": sample.get("asic_hashrate"),
                "wallet_seen_for_asic": sample.get("wallet_seen_for_asic"),
                "source_freshness": source_triage,
                "repair_policy": (
                    "Active Codex sessions must inspect the fetched source state, "
                    "use an existing upstream fix when present, otherwise implement "
                    "a tested source fix before deploy/restart/commit/push."
                ),
            },
            status=status,
        )
    state.update(
        {
            "updated_at": sample.get("generated_at"),
            "last_guard_state": sample.get("guard_state"),
            "last_problems": sample.get("problems"),
            "last_sample": sample,
        }
    )
    write_json(STATE_FILE, state)
    append_history(sample)
    log(
        "sample "
        f"state={sample.get('guard_state')} sync={sample.get('sync_status')} "
        f"remaining={sample.get('remaining_blocks')} jobs={sample.get('pool_job_notify_count')} "
        f"asic_connected={sample.get('asic_connected')} problems={sample.get('problems')}"
    )
    return sample


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one check and print JSON")
    parser.add_argument("--dry-run", action="store_true", help="evaluate triage without acting on the stack")
    args = parser.parse_args()
    sample = run_once(dry_run=args.dry_run)
    print(json.dumps(sample, indent=2, sort_keys=True, default=str))
    return 0 if sample.get("guard_state") in {"ok", "warning", "critical"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

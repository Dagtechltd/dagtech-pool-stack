#!/usr/bin/env python3
"""IPFS restore-point freshness guard for BlockDAG chain data."""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import automation_control
from incident_journal import append_incident
from pool_ops import LOG_DIR, PROJECT_ROOT, RUNTIME_DIR, ensure_runtime, now_iso


STATE_FILE = RUNTIME_DIR / "chain-restore-guard-state.json"
HEALTH_FILE = RUNTIME_DIR / "chain-restore-health.json"
LOG_FILE = LOG_DIR / "chain-restore-guard.log"
STATUS_URL = os.environ.get("BDAG_RESTORE_GUARD_STATUS_URL", "http://127.0.0.1:8088/api/status")
STATUS_TIMEOUT = float(os.environ.get("BDAG_RESTORE_GUARD_STATUS_TIMEOUT", "20"))
MAX_RESTORE_AGE_SECONDS = int(os.environ.get("BDAG_RESTORE_POINT_MAX_AGE_SECONDS", str(6 * 3600)))
INCIDENT_COOLDOWN_SECONDS = int(os.environ.get("BDAG_RESTORE_GUARD_INCIDENT_COOLDOWN_SECONDS", "1800"))


def resolve_path(value: str | None, default: Path) -> Path:
    if not value:
        return default
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def configured_status_files() -> dict[str, Path]:
    return {
        "ipfs_segment_writer": resolve_path(
            os.environ.get("BDAG_IPFS_SEGMENT_STATUS_FILE"),
            PROJECT_ROOT / "ops/runtime/ipfs-content/segment-writer-status.json",
        ),
        "rawdatadir_content_index": resolve_path(
            os.environ.get("BDAG_IPFS_RAWDATADIR_CONTENT_INDEX_PATH"),
            PROJECT_ROOT / "ops/runtime/ipfs-content/rawdatadir-content-index.json",
        ),
        "rawdatadir_content_seal": resolve_path(
            os.environ.get("BDAG_RAWDATADIR_SIDECAR_CONTENT_STATUS_FILE"),
            PROJECT_ROOT / "ops/runtime/rawdatadir-sidecar-content-status.json",
        ),
        "ipfs_content_sidecar": resolve_path(
            os.environ.get("BDAG_IPFS_CONTENT_STATUS_FILE"),
            PROJECT_ROOT / "ops/runtime/ipfs-content-sidecar-status.json",
        ),
        "rawdatadir_sidecar_safe": resolve_path(
            os.environ.get("BDAG_RAWDATADIR_SIDECAR_SAFE_STATUS"),
            PROJECT_ROOT / "ops/runtime/rawdatadir-sidecar-safe-status.json",
        ),
        "rawdatadir_ipfs_restore": resolve_path(
            os.environ.get("BDAG_IPFS_RAWDATADIR_RESTORE_STATUS_FILE"),
            PROJECT_ROOT / "ops/runtime/ipfs-content/rawdatadir-restore-status.json",
        ),
        "ipfs_restore_drill": resolve_path(
            os.environ.get("BDAG_IPFS_RESTORE_STATUS_FILE"),
            PROJECT_ROOT / "ops/runtime/ipfs-content/restore-drill-status.json",
        ),
    }


def configured_timers() -> list[str]:
    raw = os.environ.get(
        "BDAG_RESTORE_GUARD_IPFS_TIMERS",
        "bdag-rawdatadir-sidecar.timer,bdag-rawdatadir-sidecar-verify.timer,bdag-ipfs-content-sidecar.timer",
    )
    return [item.strip() for item in raw.split(",") if item.strip()]


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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def should_emit(state: dict[str, Any], key: str, signature: str, now: int) -> bool:
    last_signature = str(state.get(f"{key}_signature") or "")
    last_epoch = int(state.get(f"{key}_epoch", 0) or 0)
    if signature == last_signature and now - last_epoch < INCIDENT_COOLDOWN_SECONDS:
        return False
    state[f"{key}_signature"] = signature
    state[f"{key}_epoch"] = now
    state[f"{key}_at"] = now_iso()
    return True


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


def start_unit(unit: str) -> subprocess.CompletedProcess[str]:
    return systemctl_user("start", unit)


def unit_start_allowed(unit: str, reason: str) -> automation_control.ControlDecision:
    return automation_control.check_mutation_allowed(
        automation_control.ACTION_SYSTEMD_START,
        actor="chain-restore-guard",
        target=unit,
        reason=reason,
    )


def status_api() -> tuple[dict[str, Any] | None, str]:
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=STATUS_TIMEOUT) as response:
            return json.loads(response.read(4_000_000).decode("utf-8", "replace")), ""
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return None, str(exc)


def status_file_info(name: str, path: Path, now: int) -> dict[str, Any]:
    payload = read_json(path)
    exists = path.exists()
    mtime = int(path.stat().st_mtime) if exists else None
    result: dict[str, Any] = {
        "name": name,
        "path": str(path),
        "exists": exists,
        "age_seconds": max(0, now - mtime) if mtime else None,
        "state": payload.get("state") or payload.get("status"),
    }
    for key in (
        "document_type",
        "latest_index_cid",
        "index_cid",
        "current_rawdatadir_index_cid",
        "artifact_cid",
        "artifact_type",
        "network",
        "chain_id",
        "raw_artifact_cid",
        "accepted_head",
        "last_published_order",
        "last_order",
        "tip_order",
        "tip_hash",
        "state_root",
        "reason",
        "reasons",
        "manifest_verification",
    ):
        if key in payload:
            result[key] = payload[key]
    return result


def verification_state(info: dict[str, Any]) -> str:
    verification = info.get("manifest_verification")
    if isinstance(verification, dict):
        return str(verification.get("state") or "").strip().lower()
    return ""


def raw_state_checkpoint_status(files: dict[str, dict[str, Any]], now: int) -> dict[str, Any]:
    sidecar = files.get("ipfs_content_sidecar") or {}
    index = files.get("rawdatadir_content_index") or {}
    reasons: list[str] = []
    sidecar_age = sidecar.get("age_seconds")
    index_age = index.get("age_seconds")
    sidecar_fresh = isinstance(sidecar_age, int) and sidecar_age <= MAX_RESTORE_AGE_SECONDS
    index_fresh = isinstance(index_age, int) and index_age <= MAX_RESTORE_AGE_SECONDS

    sidecar_state = str(sidecar.get("state") or "").strip().lower()
    if sidecar_state != "published":
        reasons.append(f"ipfs_content_sidecar_not_published:{sidecar_state or 'missing'}")
    if not sidecar_fresh:
        reasons.append("ipfs_content_sidecar_stale_or_missing")
    if not index_fresh:
        reasons.append("rawdatadir_content_index_stale_or_missing")

    artifact_cid = str(sidecar.get("artifact_cid") or index.get("artifact_cid") or "").strip()
    index_cid = str(sidecar.get("index_cid") or index.get("index_cid") or index.get("current_rawdatadir_index_cid") or "").strip()
    if not artifact_cid:
        reasons.append("rawdatadir_artifact_cid_missing")
    if not index_cid:
        reasons.append("rawdatadir_index_cid_missing")

    artifact_type = str(index.get("artifact_type") or "").strip()
    if artifact_type and artifact_type != "raw_datadir_checkpoint":
        reasons.append(f"rawdatadir_content_index_wrong_artifact_type:{artifact_type}")
    network = str(index.get("network") or "").strip().lower()
    if network and network != "mainnet":
        reasons.append(f"rawdatadir_content_index_non_mainnet:{network}")

    verification = verification_state(sidecar) or verification_state(index)
    if verification != "verified":
        reasons.append(f"rawdatadir_manifest_not_verified:{verification or 'missing'}")

    return {
        "ready": not reasons,
        "state": "ready" if not reasons else "not_ready",
        "reasons": sorted(set(reasons)),
        "artifact_cid": artifact_cid,
        "index_cid": index_cid,
        "tip_order": index.get("tip_order") or sidecar.get("tip_order"),
        "tip_hash": index.get("tip_hash") or sidecar.get("tip_hash"),
        "state_root": index.get("state_root") or sidecar.get("state_root"),
        "sidecar_status_age_seconds": sidecar_age,
        "index_age_seconds": index_age,
        "max_restore_age_seconds": MAX_RESTORE_AGE_SECONDS,
        "trust_model": (
            "raw state checkpoints are trusted recovery candidates only when the content sidecar "
            "published a fresh raw_datadir_checkpoint index with a verified trusted Ed25519 manifest signature"
        ),
    }


def restore_status(now: int) -> dict[str, Any]:
    files = {
        name: status_file_info(name, path, now)
        for name, path in configured_status_files().items()
    }
    raw_checkpoint = raw_state_checkpoint_status(files, now)
    fresh = ["rawdatadir_state_checkpoint"] if raw_checkpoint["ready"] else []
    required_restore_files = {"ipfs_content_sidecar", "rawdatadir_content_index"}
    stale = {
        name: info
        for name, info in files.items()
        if name in required_restore_files
        if not isinstance(info.get("age_seconds"), int) or int(info["age_seconds"]) > MAX_RESTORE_AGE_SECONDS
    }
    if not raw_checkpoint["ready"]:
        stale["rawdatadir_state_checkpoint"] = raw_checkpoint
    return {
        "fresh": bool(fresh),
        "fresh_sources": fresh,
        "stale_or_missing": stale,
        "raw_state_checkpoint": raw_checkpoint,
        "files": files,
        "max_restore_age_seconds": MAX_RESTORE_AGE_SECONDS,
    }


def ensure_ipfs_timers(state: dict[str, Any], now: int) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for timer in configured_timers():
        if unit_active(timer):
            results[timer] = {"active": True}
            continue
        decision = unit_start_allowed(timer, "ensure IPFS recovery timer is active")
        if not decision.allowed:
            results[timer] = {
                "active": False,
                "started": False,
                "returncode": None,
                "stderr": decision.reason,
                "control_decision": decision.as_dict(),
            }
            if should_emit(state, f"{timer}_automation_blocked", decision.reason, now):
                append_incident(
                    "restore_guard_ipfs_timer_start_blocked",
                    "warning",
                    "chain-restore-guard",
                    f"Restore guard start of {timer} was blocked by automation control",
                    {"timer": timer, "control_decision": decision.as_dict()},
                )
            continue
        result = start_unit(timer)
        results[timer] = {
            "active": False,
            "started": result.returncode == 0,
            "returncode": result.returncode,
            "stderr": result.stderr.strip(),
        }
        if should_emit(state, f"{timer}_inactive", str(result.returncode), now):
            append_incident(
                "restore_guard_started_ipfs_timer" if result.returncode == 0 else "restore_guard_ipfs_timer_start_failed",
                "warning" if result.returncode == 0 else "critical",
                "chain-restore-guard",
                f"Restore guard {'started' if result.returncode == 0 else 'could not start'} {timer}",
                {"timer": timer, "returncode": result.returncode, "stderr": result.stderr.strip()},
            )
    return results


def main() -> int:
    ensure_runtime()
    now = int(time.time())
    state = read_json(STATE_FILE)

    status, status_error = status_api()
    ipfs_restore = restore_status(now)
    timers = ensure_ipfs_timers(state, now)

    stale = ipfs_restore["stale_or_missing"]
    if stale and should_emit(state, "ipfs_restore_status_stale", ",".join(sorted(stale)), now):
        append_incident(
            "restore_point_stale",
            "warning",
            "chain-restore-guard",
            "IPFS restore metadata is stale or missing",
            {"stale_or_missing": stale},
        )
        log(f"IPFS restore metadata stale or missing: {','.join(sorted(stale))}")

    payload = {
        "generated_at": now_iso(),
        "status_api_error": status_error,
        "sync_status": (status or {}).get("sync_progress", {}).get("status") if isinstance(status, dict) else None,
        "restore_transport": "ipfs",
        "ipfs_restore": ipfs_restore,
        "timers": timers,
    }
    write_json(HEALTH_FILE, payload)
    write_json(STATE_FILE, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

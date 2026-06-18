#!/usr/bin/env python3
"""Restore-point freshness guard for BlockDAG chain data."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from incident_journal import append_incident
from pool_ops import LOG_DIR, PROJECT_ROOT, RUNTIME_DIR, ensure_runtime, now_iso


STATE_FILE = RUNTIME_DIR / "chain-restore-guard-state.json"
HEALTH_FILE = RUNTIME_DIR / "chain-restore-health.json"
LOG_FILE = LOG_DIR / "chain-restore-guard.log"
SNAPSHOT_ROOT = PROJECT_ROOT / "data-restore"
LATEST_SNAPSHOT = SNAPSHOT_ROOT / "latest-hourly"
STATUS_URL = os.environ.get("BDAG_RESTORE_GUARD_STATUS_URL", "http://127.0.0.1:8088/api/status")
STATUS_TIMEOUT = float(os.environ.get("BDAG_RESTORE_GUARD_STATUS_TIMEOUT", "20"))
MAX_PUBLISHED_AGE_SECONDS = int(os.environ.get("BDAG_RESTORE_POINT_MAX_AGE_SECONDS", str(6 * 3600)))
MAX_STAGE_AGE_SECONDS = int(os.environ.get("BDAG_RESTORE_STAGE_MAX_AGE_SECONDS", str(90 * 60)))
INCIDENT_COOLDOWN_SECONDS = int(os.environ.get("BDAG_RESTORE_GUARD_INCIDENT_COOLDOWN_SECONDS", "1800"))
SNAPSHOT_START_COOLDOWN_SECONDS = int(os.environ.get("BDAG_RESTORE_GUARD_SNAPSHOT_START_COOLDOWN_SECONDS", "3600"))
STAMP_RE = re.compile(r"bdag-(node[12])-hourly-(\d{8}T\d{6}Z)")


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


def start_unit_no_block(unit: str) -> subprocess.CompletedProcess[str]:
    return systemctl_user("start", "--no-block", unit)


def status_api() -> tuple[dict[str, Any] | None, str]:
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=STATUS_TIMEOUT) as response:
            return json.loads(response.read(4_000_000).decode("utf-8", "replace")), ""
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return None, str(exc)


def snapshot_stamp(path: Path) -> tuple[str, int | None]:
    match = STAMP_RE.search(path.name)
    if not match:
        return "", None
    stamp = match.group(2)
    try:
        parsed = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return stamp, None
    return stamp, int(parsed.timestamp())


def newest_file_mtime(path: Path) -> int | None:
    if not path.exists():
        return None
    newest = int(path.stat().st_mtime)
    for root, _, files in os.walk(path):
        for name in files:
            try:
                mtime = int((Path(root) / name).stat().st_mtime)
            except OSError:
                continue
            if mtime > newest:
                newest = mtime
    return newest


def published_snapshot_info(now: int) -> dict[str, Any]:
    if not LATEST_SNAPSHOT.exists():
        target = ""
        if LATEST_SNAPSHOT.is_symlink():
            try:
                target = os.readlink(LATEST_SNAPSHOT)
            except OSError:
                target = ""
        return {
            "exists": False,
            "path": str(LATEST_SNAPSHOT),
            "broken_symlink": bool(LATEST_SNAPSHOT.is_symlink()),
            "target": target,
        }
    resolved = LATEST_SNAPSHOT.resolve()
    stamp, stamp_epoch = snapshot_stamp(resolved)
    manifest_path = Path(str(resolved) + ".manifest.json")
    manifest = read_json(manifest_path)
    source_epoch = stamp_epoch or int(resolved.stat().st_mtime)
    return {
        "exists": True,
        "path": str(resolved),
        "stamp": stamp,
        "stamp_epoch": stamp_epoch,
        "age_seconds": max(0, now - source_epoch),
        "manifest_path": str(manifest_path),
        "manifest_exists": manifest_path.exists(),
        "manifest": manifest,
    }


def stage_info(now: int) -> dict[str, Any]:
    root = SNAPSHOT_ROOT / ".hourly-stage"
    result: dict[str, Any] = {}
    nodes = [item.strip() for item in os.environ.get("BDAG_CHAIN_RESTORE_STAGE_NODES", "node").split(",") if item.strip()]
    for node in nodes:
        path = root / node
        mtime = newest_file_mtime(path)
        result[node] = {
            "exists": path.exists(),
            "path": str(path),
            "latest_file_epoch": mtime,
            "latest_file_age_seconds": max(0, now - mtime) if mtime else None,
        }
    return result


def stack_is_safe_for_snapshot(status: dict[str, Any] | None) -> tuple[bool, str]:
    if not isinstance(status, dict):
        return False, "status API unavailable"
    failures = status.get("failures")
    if failures:
        return False, f"stack failures are present: {failures}"
    sync = status.get("sync_progress") if isinstance(status.get("sync_progress"), dict) else {}
    if sync.get("status") != "synced":
        return False, f"sync status is {sync.get('status')}"
    nodes = status.get("nodes") if isinstance(status.get("nodes"), dict) else {}
    heights = [
        int(info.get("latest_block"))
        for info in nodes.values()
        if isinstance(info, dict) and info.get("latest_block") is not None
    ]
    if len(heights) >= 2 and max(heights) - min(heights) > 5:
        return False, f"node height gap is {max(heights) - min(heights)} blocks"
    return True, "stack synced and safe"


def maybe_start_snapshot(
    state: dict[str, Any],
    now: int,
    published: dict[str, Any],
    status: dict[str, Any] | None,
) -> dict[str, Any]:
    latest_age = published.get("age_seconds")
    if isinstance(latest_age, int) and latest_age <= MAX_PUBLISHED_AGE_SECONDS:
        return {"started": False, "reason": "published restore point is fresh"}
    safe, reason = stack_is_safe_for_snapshot(status)
    if not safe:
        return {"started": False, "reason": f"snapshot not safe now: {reason}"}
    if unit_active("bdag-hourly-snapshot.service"):
        return {"started": False, "reason": "snapshot service already active"}
    last_start = int(state.get("last_snapshot_start_epoch", 0) or 0)
    if now - last_start < SNAPSHOT_START_COOLDOWN_SECONDS:
        return {
            "started": False,
            "reason": f"snapshot start cooldown {SNAPSHOT_START_COOLDOWN_SECONDS - (now - last_start)}s",
        }
    result = start_unit_no_block("bdag-hourly-snapshot.service")
    state["last_snapshot_start_epoch"] = now
    state["last_snapshot_start_at"] = now_iso()
    details = {
        "latest_age_seconds": latest_age,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }
    if result.returncode == 0:
        append_incident(
            "restore_guard_started_snapshot",
            "warning",
            "chain-restore-guard",
            "Restore guard started a chain snapshot because the latest published restore point is stale",
            details,
        )
        log("started bdag-hourly-snapshot.service because restore point is stale")
        return {"started": True, "reason": "latest published restore point stale", **details}
    append_incident(
        "restore_guard_snapshot_start_failed",
        "critical",
        "chain-restore-guard",
        "Restore guard could not start chain snapshot service",
        details,
    )
    log(f"failed to start bdag-hourly-snapshot.service rc={result.returncode}")
    return {"started": False, "reason": "snapshot start failed", **details}


def main() -> int:
    ensure_runtime()
    now = int(time.time())
    state = read_json(STATE_FILE)

    for timer in ("bdag-chain-presync.timer", "bdag-hourly-snapshot.timer"):
        if not unit_active(timer):
            result = start_unit(timer)
            if should_emit(state, f"{timer}_inactive", str(result.returncode), now):
                append_incident(
                    "restore_guard_started_timer" if result.returncode == 0 else "restore_guard_timer_start_failed",
                    "warning" if result.returncode == 0 else "critical",
                    "chain-restore-guard",
                    f"Restore guard {'started' if result.returncode == 0 else 'could not start'} {timer}",
                    {"timer": timer, "returncode": result.returncode, "stderr": result.stderr.strip()},
                )

    status, status_error = status_api()
    published = published_snapshot_info(now)
    stage = stage_info(now)
    action = maybe_start_snapshot(state, now, published, status)

    if published.get("broken_symlink") and should_emit(state, "published_snapshot_broken", str(published.get("target") or ""), now):
        append_incident(
            "restore_point_broken",
            "critical",
            "chain-restore-guard",
            "Latest published chain restore point symlink is broken",
            {"published": published},
        )
        log(f"latest published restore symlink is broken: {published.get('path')} -> {published.get('target')}")

    stale_stage = {
        node: info
        for node, info in stage.items()
        if not isinstance(info.get("latest_file_age_seconds"), int)
        or int(info["latest_file_age_seconds"]) > MAX_STAGE_AGE_SECONDS
    }
    latest_age = published.get("age_seconds")
    if isinstance(latest_age, int) and latest_age > MAX_PUBLISHED_AGE_SECONDS:
        hours = round(latest_age / 3600, 2)
        if should_emit(state, "published_snapshot_stale", str(published.get("stamp") or published.get("path")), now):
            append_incident(
                "restore_point_stale",
                "critical",
                "chain-restore-guard",
                f"Latest published chain restore point is stale ({hours}h old)",
                {"published": published, "max_age_seconds": MAX_PUBLISHED_AGE_SECONDS},
            )
    if stale_stage and should_emit(state, "stage_stale", ",".join(sorted(stale_stage)), now):
        append_incident(
            "restore_stage_stale",
            "warning",
            "chain-restore-guard",
            "Warm chain restore stage is stale or missing for at least one node",
            {"stale_stage": stale_stage, "max_age_seconds": MAX_STAGE_AGE_SECONDS},
        )

    health = {
        "generated_at": now_iso(),
        "status_ok": status is not None,
        "status_error": status_error,
        "stack_overall": status.get("overall") if isinstance(status, dict) else None,
        "sync_progress": status.get("sync_progress") if isinstance(status, dict) else None,
        "published_restore_point": published,
        "stage": stage,
        "action": action,
        "thresholds": {
            "max_published_age_seconds": MAX_PUBLISHED_AGE_SECONDS,
            "max_stage_age_seconds": MAX_STAGE_AGE_SECONDS,
        },
    }
    write_json(HEALTH_FILE, health)
    state["updated_at"] = now_iso()
    state["last_health_file"] = str(HEALTH_FILE)
    write_json(STATE_FILE, state)
    log(
        "restore health checked: "
        f"published_age={published.get('age_seconds')} action={action.get('reason')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

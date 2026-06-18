#!/usr/bin/env python3
"""Record the newest available BlockDAG chain snapshot candidate.

This checker is intentionally read-only. It makes recovery prefer the newest
available data only when the manifest says the data is restore-safe; unsafe warm
copies are recorded and rejected instead of being retried against live nodes.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from pool_ops import LOG_DIR, PROJECT_ROOT, RUNTIME_DIR, ensure_runtime, now_iso


STATE_FILE = RUNTIME_DIR / "latest-chain-candidate-state.json"
LOG_FILE = LOG_DIR / "latest-chain-candidate.log"
DEFAULT_PATTERNS = [
    str(Path.home() / "Downloads" / "blockdag-chain-snapshots" / "*.manifest.json"),
    str(PROJECT_ROOT / "data-restore" / "hourly" / "*.manifest.json"),
    str(PROJECT_ROOT / "data-restore" / "*.manifest.json"),
]
MIN_GAIN_BLOCKS = int(os.environ.get("BDAG_LATEST_CHAIN_MIN_GAIN_BLOCKS", "5000"))
MAX_SYNC_REMAINING_BLOCKS = int(os.environ.get("BDAG_LATEST_CHAIN_MAX_SYNC_REMAINING_BLOCKS", "5"))


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


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_epoch(value: Any, fallback: int) -> int:
    if not value:
        return fallback
    text = str(value).strip()
    for candidate in (text, text.replace("Z", "+0000")):
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
            try:
                return int(datetime.strptime(candidate, fmt).timestamp())
            except ValueError:
                continue
    return fallback


def sibling_payload_path(manifest_path: Path) -> Path:
    name = manifest_path.name
    suffix = ".manifest.json"
    if name.endswith(suffix):
        return manifest_path.with_name(name[: -len(suffix)])
    return manifest_path


def manifest_payload_path(manifest_path: Path, manifest: dict[str, Any]) -> Path:
    published = manifest.get("published_path")
    if published:
        path = Path(str(published)).expanduser()
        if path.exists():
            return path
    return sibling_payload_path(manifest_path)


def candidate_height(manifest: dict[str, Any]) -> int:
    height = safe_int(manifest.get("source_latest_block"))
    if height > 0:
        return height
    node_heights = manifest.get("node_heights")
    if isinstance(node_heights, dict):
        values = [safe_int(value) for value in node_heights.values()]
        return max(values or [0])
    return 0


def candidate_rejections(manifest_path: Path, manifest: dict[str, Any], payload_path: Path, height: int) -> list[str]:
    reasons: list[str] = []
    if not payload_path.exists():
        reasons.append("payload_path_missing")
    if height <= 0:
        reasons.append("source_height_unknown")

    restore_safe = manifest.get("restore_safe")
    if restore_safe is False:
        reasons.append("manifest_restore_safe_false")
    if restore_safe is not True:
        if manifest.get("published_from_online_warm_copy") is True:
            reasons.append("online_warm_copy")
        if manifest.get("consistent_final_stopped_sync") is not True:
            reasons.append("no_final_stopped_sync")

    sync_status = str(manifest.get("sync_status") or "").lower()
    if sync_status and sync_status != "synced":
        reasons.append(f"sync_status_{sync_status}")
    remaining = manifest.get("sync_remaining_blocks")
    if remaining is not None and safe_int(remaining, 999999999) > MAX_SYNC_REMAINING_BLOCKS:
        reasons.append(f"sync_remaining_{safe_int(remaining)}")
    stack_overall = str(manifest.get("stack_overall") or "").lower()
    if stack_overall and stack_overall != "ok":
        reasons.append(f"stack_overall_{stack_overall}")

    if not manifest:
        reasons.append("manifest_unreadable")
    if manifest_path.name.endswith(".tar.gz.manifest.json") and not payload_path.exists():
        reasons.append("archive_missing")
    return reasons


def discover_candidates(patterns: list[str]) -> list[dict[str, Any]]:
    seen: set[Path] = set()
    candidates: list[dict[str, Any]] = []
    for pattern in patterns:
        for raw_path in glob.glob(pattern):
            manifest_path = Path(raw_path).expanduser().resolve()
            if manifest_path in seen:
                continue
            seen.add(manifest_path)
            manifest = read_json(manifest_path)
            try:
                fallback_epoch = int(manifest_path.stat().st_mtime)
            except OSError:
                fallback_epoch = int(time.time())
            payload_path = manifest_payload_path(manifest_path, manifest)
            height = candidate_height(manifest)
            epoch = parse_epoch(manifest.get("generated_at"), fallback_epoch)
            rejections = candidate_rejections(manifest_path, manifest, payload_path, height)
            candidates.append(
                {
                    "manifest_path": str(manifest_path),
                    "payload_path": str(payload_path),
                    "generated_at": manifest.get("generated_at"),
                    "generated_epoch": epoch,
                    "source_latest_block": height,
                    "sync_status": manifest.get("sync_status"),
                    "sync_remaining_blocks": manifest.get("sync_remaining_blocks"),
                    "stack_overall": manifest.get("stack_overall"),
                    "restore_safe": manifest.get("restore_safe"),
                    "published_from_online_warm_copy": manifest.get("published_from_online_warm_copy"),
                    "consistent_final_stopped_sync": manifest.get("consistent_final_stopped_sync"),
                    "source_node_service": manifest.get("source_node_service"),
                    "safe_to_restore": not rejections,
                    "rejections": rejections,
                }
            )
    return candidates


def current_sync_status() -> dict[str, Any]:
    try:
        from pool_ops import collect_sync_progress

        return collect_sync_progress()
    except Exception as exc:  # noqa: BLE001 - checker should not disturb recovery.
        return {"status": "unknown", "error": str(exc)}


def build_state(patterns: list[str]) -> dict[str, Any]:
    candidates = discover_candidates(patterns)
    candidates.sort(key=lambda item: (safe_int(item.get("generated_epoch")), safe_int(item.get("source_latest_block"))), reverse=True)
    safe_candidates = [item for item in candidates if item.get("safe_to_restore")]
    safe_candidates.sort(key=lambda item: (safe_int(item.get("source_latest_block")), safe_int(item.get("generated_epoch"))), reverse=True)
    sync = current_sync_status()
    current_height = safe_int(sync.get("current_block"))
    remaining = safe_int(sync.get("remaining_blocks"), -1)
    latest = candidates[0] if candidates else None
    best_safe = safe_candidates[0] if safe_candidates else None

    action = "no_candidates"
    reason = "no manifest candidates found"
    if latest and not latest.get("safe_to_restore"):
        action = "reject_latest_candidate"
        reason = "latest manifest is not restore-safe: " + ",".join(latest.get("rejections") or [])
    if best_safe:
        gain = safe_int(best_safe.get("source_latest_block")) - current_height if current_height else None
        if gain is None:
            action = "safe_candidate_available_current_height_unknown"
            reason = "safe candidate exists but current importer height is unknown"
        elif gain >= MIN_GAIN_BLOCKS and (remaining < 0 or remaining >= MIN_GAIN_BLOCKS):
            action = "newer_safe_candidate_available"
            reason = f"safe candidate is {gain} block(s) ahead of the current importer"
        elif action == "no_candidates" or (latest and latest.get("safe_to_restore")):
            action = "current_importer_is_best_use"
            reason = f"best safe candidate gain is {gain} block(s), below threshold {MIN_GAIN_BLOCKS}"

    return {
        "generated_at": now_iso(),
        "policy": "prefer the newest chain data only after the manifest is restore-safe; reject unsafe warm copies",
        "scan_patterns": patterns,
        "current_sync": sync,
        "current_height": current_height,
        "remaining_blocks": remaining,
        "latest_candidate": latest,
        "best_safe_candidate": best_safe,
        "candidate_count": len(candidates),
        "safe_candidate_count": len(safe_candidates),
        "decision": {
            "action": action,
            "reason": reason,
            "min_gain_blocks": MIN_GAIN_BLOCKS,
            "max_sync_remaining_blocks": MAX_SYNC_REMAINING_BLOCKS,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one check and exit")
    parser.add_argument("--quiet", action="store_true", help="do not print JSON to stdout")
    parser.add_argument(
        "--patterns",
        default=os.environ.get("BDAG_LATEST_CHAIN_MANIFEST_PATTERNS", os.pathsep.join(DEFAULT_PATTERNS)),
        help="os.pathsep-separated manifest glob patterns",
    )
    args = parser.parse_args()
    patterns = [item for item in args.patterns.split(os.pathsep) if item]
    state = build_state(patterns)
    write_json(STATE_FILE, state)
    decision = state.get("decision") or {}
    log(f"decision action={decision.get('action')} reason={decision.get('reason')}")
    if not args.quiet:
        print(json.dumps(state, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Repair and prune BlockDAG hourly chain restore snapshots by name timestamp."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(os.environ.get("BDAG_PROJECT_ROOT", "/home/jeremy/blockdag-asic-pool"))
SNAPSHOT_DIR = Path(os.environ.get("BDAG_SNAPSHOT_DIR", PROJECT_ROOT / "data-restore" / "hourly"))
LATEST_LINK = PROJECT_ROOT / "data-restore" / "latest-hourly"
LATEST_MANIFEST_LINK = PROJECT_ROOT / "data-restore" / "latest-hourly.manifest.json"
RUNTIME_DIR = PROJECT_ROOT / "ops" / "runtime"
SNAPSHOT_STAMP_RE = re.compile(r"hourly-(\d{8}T\d{6}Z)")


def snapshot_sort_key(path: Path) -> tuple[str, str]:
    match = SNAPSHOT_STAMP_RE.search(path.name)
    stamp = match.group(1) if match else ""
    return stamp, path.name


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def snapshot_dirs() -> list[Path]:
    return sorted(
        [path for path in SNAPSHOT_DIR.glob("bdag-node*-hourly-*") if path.is_dir()],
        key=snapshot_sort_key,
        reverse=True,
    )


def archive_files() -> list[Path]:
    return sorted(
        [path for path in SNAPSHOT_DIR.glob("bdag-node*-hourly-*.tar.gz") if path.is_file()],
        key=snapshot_sort_key,
        reverse=True,
    )


def manifest_files() -> list[Path]:
    return sorted(SNAPSHOT_DIR.glob("bdag-node*-hourly-*.manifest.json"), key=snapshot_sort_key, reverse=True)


def dir_manifest(path: Path) -> Path:
    return path.with_name(f"{path.name}.manifest.json")


def archive_manifest(path: Path) -> Path:
    return path.with_name(f"{path.name}.manifest.json")


def path_size(path: Path) -> str:
    result = os.popen(f"du -sh {str(path)!r} 2>/dev/null").read().strip()
    return result.split("\t", 1)[0] if result else ""


def current_latest() -> dict[str, Any]:
    target = os.readlink(LATEST_LINK) if LATEST_LINK.is_symlink() else ""
    manifest_target = os.readlink(LATEST_MANIFEST_LINK) if LATEST_MANIFEST_LINK.is_symlink() else ""
    return {
        "latest_link": str(LATEST_LINK),
        "latest_target": target,
        "latest_exists": LATEST_LINK.exists(),
        "latest_manifest_link": str(LATEST_MANIFEST_LINK),
        "latest_manifest_target": manifest_target,
        "latest_manifest_exists": LATEST_MANIFEST_LINK.exists(),
    }


def desired_latest() -> tuple[Path | None, str]:
    dirs = snapshot_dirs()
    if dirs:
        return dirs[0], "directory"
    archives = archive_files()
    if archives:
        return archives[0], "archive"
    return None, "none"


def orphan_manifests() -> list[Path]:
    out: list[Path] = []
    for manifest in manifest_files():
        name = manifest.name.removesuffix(".manifest.json")
        base = SNAPSHOT_DIR / name
        archive_base = SNAPSHOT_DIR / name.removesuffix(".tar.gz")
        if not base.exists() and not archive_base.exists() and not (SNAPSHOT_DIR / f"{name}.tar.gz").exists():
            out.append(manifest)
    return out


def build_plan(retain: int) -> dict[str, Any]:
    dirs = snapshot_dirs()
    archives = archive_files()
    remove_dirs = dirs[retain:]
    remove_archives = archives[retain:]
    latest_path, latest_kind = desired_latest()
    return {
        "generated_at": now_iso(),
        "project_root": str(PROJECT_ROOT),
        "snapshot_dir": str(SNAPSHOT_DIR),
        "retain": retain,
        "current_latest": current_latest(),
        "latest_candidate": str(latest_path) if latest_path else "",
        "latest_candidate_kind": latest_kind,
        "directory_count": len(dirs),
        "archive_count": len(archives),
        "manifest_count": len(manifest_files()),
        "remove_directories": [{"path": str(path), "size": path_size(path)} for path in remove_dirs],
        "remove_archives": [{"path": str(path), "size": path_size(path)} for path in remove_archives],
        "remove_orphan_manifests": [str(path) for path in orphan_manifests()],
    }


def apply_plan(plan: dict[str, Any]) -> None:
    for item in plan["remove_directories"]:
        path = Path(item["path"])
        if path.exists():
            shutil.rmtree(path)
        manifest = dir_manifest(path)
        if manifest.exists():
            manifest.unlink()
    for item in plan["remove_archives"]:
        path = Path(item["path"])
        if path.exists():
            path.unlink()
        manifest = archive_manifest(path)
        if manifest.exists():
            manifest.unlink()
    for item in plan["remove_orphan_manifests"]:
        path = Path(item)
        if path.exists():
            path.unlink()

    latest_path = Path(plan["latest_candidate"]) if plan.get("latest_candidate") else None
    if latest_path and latest_path.exists():
        target = Path("hourly") / latest_path.name
        LATEST_LINK.unlink(missing_ok=True)
        LATEST_LINK.symlink_to(target)
        manifest = archive_manifest(latest_path) if latest_path.name.endswith(".tar.gz") else dir_manifest(latest_path)
        if manifest.exists():
            LATEST_MANIFEST_LINK.unlink(missing_ok=True)
            LATEST_MANIFEST_LINK.symlink_to(Path("hourly") / manifest.name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retain", type=int, default=int(os.environ.get("BDAG_SNAPSHOT_RETAIN", "12")))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args()

    plan = build_plan(args.retain)
    if args.apply:
        apply_plan(plan)
        plan["applied_at"] = now_iso()
        plan["after"] = build_plan(args.retain)

    if args.write_report:
        report_dir = RUNTIME_DIR / "snapshot-prune"
        report_dir.mkdir(parents=True, exist_ok=True)
        path = report_dir / f"snapshot-prune-{time.strftime('%Y%m%d-%H%M%S')}.json"
        path.write_text(json.dumps(plan, indent=2, sort_keys=True), encoding="utf-8")
        plan["report_path"] = str(path)

    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

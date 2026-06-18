#!/usr/bin/env python3
"""Verify that the raw-datadir sidecar is safe to consume as the latest copy."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("BDAG_PROJECT_ROOT") or Path(__file__).resolve().parents[1]).resolve()
REQUESTED_NETWORK = (os.environ.get("BDAG_RAWDATADIR_NETWORK") or os.environ.get("BDAG_FASTSNAP_NETWORK") or "mainnet").strip().lower()
if REQUESTED_NETWORK != "mainnet":
    raise SystemExit(f"raw datadir sidecar verifier refuses non-mainnet network: {REQUESTED_NETWORK}")
NETWORK = "mainnet"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def resolve_path(value: str | None, default: Path) -> Path:
    if not value:
        return default.resolve()
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        tmp = Path(handle.name)
    tmp.replace(path)


def newest_mtime(paths: list[Path]) -> float | None:
    values: list[float] = []
    for path in paths:
        try:
            values.append(path.stat().st_mtime)
        except OSError:
            pass
    return max(values) if values else None


def shutil_available(command: str) -> bool:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if directory and os.access(Path(directory) / command, os.X_OK):
            return True
    return False


def fuser_holds(path: Path) -> bool:
    if not path.exists() or not shutil_available("fuser"):
        return False
    result = subprocess.run(["fuser", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return result.returncode == 0


def collect_unsafe_paths(sidecar_dir: Path) -> list[str]:
    exact = (
        "network.key",
        "bdageth/nodekey",
        "keystore",
        "bdageth/keystore",
        "peerstore",
        "nodes",
        "bdageth/nodes",
        ".rsync-partial",
        "snapshot.bdsnap",
        "artifact.manifest.json",
    )
    unsafe: list[str] = []
    for rel in exact:
        if (sidecar_dir / rel).exists():
            unsafe.append(rel)
    for pattern in ("LOCK", "*.ipc", "*.sock", ".rsync-partial"):
        for path in sidecar_dir.rglob(pattern):
            unsafe.append(path.relative_to(sidecar_dir).as_posix())
    return sorted(set(unsafe))


def verify(sidecar_dir: Path, source_dir: Path | None, max_age_seconds: int | None) -> dict[str, Any]:
    reasons: list[str] = []
    if not sidecar_dir.exists():
        reasons.append("sidecar_dir_missing")
    if not (sidecar_dir / "BdagChain").is_dir():
        reasons.append("missing_BdagChain")

    current = sidecar_dir / "BdagChain" / "CURRENT"
    if not current.exists():
        reasons.append("missing_BdagChain_CURRENT")
    manifests = list((sidecar_dir / "BdagChain").glob("MANIFEST-*")) if (sidecar_dir / "BdagChain").is_dir() else []
    if not manifests:
        reasons.append("missing_BdagChain_manifest")

    unsafe_paths = collect_unsafe_paths(sidecar_dir) if sidecar_dir.exists() else []
    if unsafe_paths:
        reasons.append("unsafe_ephemeral_or_private_paths_present")

    held_locks = [item for item in unsafe_paths if (item.endswith("/LOCK") or item == "LOCK") and fuser_holds(sidecar_dir / item)]
    if held_locks:
        reasons.append("sidecar_lock_held_by_process")

    marker_paths = [current, *manifests]
    sidecar_newest = newest_mtime(marker_paths)
    source_newest = None
    if source_dir:
        source_manifests = list((source_dir / "BdagChain").glob("MANIFEST-*")) if (source_dir / "BdagChain").is_dir() else []
        source_newest = newest_mtime([source_dir / "BdagChain" / "CURRENT", *source_manifests])
    age_seconds = int(time.time() - sidecar_newest) if sidecar_newest else None
    if max_age_seconds is not None and age_seconds is not None and age_seconds > max_age_seconds:
        reasons.append(f"sidecar_age_seconds_{age_seconds}_gt_{max_age_seconds}")

    safe = not reasons
    return {
        "document_type": "bdag_rawdatadir_sidecar_safe_status_v1",
        "generated_at": now_iso(),
        "safe": safe,
        "usable": safe,
        "network": NETWORK,
        "project_root": str(ROOT),
        "sidecar_dir": str(sidecar_dir),
        "source_dir": str(source_dir) if source_dir else None,
        "latest_safe_dir": str(sidecar_dir) if safe else None,
        "reasons": reasons,
        "unsafe_paths": unsafe_paths[:200],
        "unsafe_path_count": len(unsafe_paths),
        "sidecar_newest_mtime": sidecar_newest,
        "source_newest_mtime": source_newest,
        "sidecar_age_seconds": age_seconds,
        "policy": "safe means structure exists, rsync/private/identity/lock/socket state is absent, and optional age policy passes; consensus trust still comes from manifest and normal chain validation",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sidecar-dir", default=os.environ.get("BDAG_RAWDATADIR_SIDECAR_DIR"))
    parser.add_argument("--source-dir", default=os.environ.get("BDAG_RAWDATADIR_SIDECAR_SOURCE"))
    parser.add_argument("--status-file", default=os.environ.get("BDAG_RAWDATADIR_SIDECAR_SAFE_STATUS"))
    parser.add_argument("--max-age-seconds", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    sidecar_dir = resolve_path(args.sidecar_dir, ROOT / "data-restore/rawdatadir-sidecar" / NETWORK)
    source_dir = resolve_path(args.source_dir, ROOT / "data/node" / NETWORK) if args.source_dir else None
    status_file = resolve_path(args.status_file, ROOT / "ops/runtime/rawdatadir-sidecar-safe-status.json")
    payload = verify(sidecar_dir, source_dir, args.max_age_seconds)
    if not args.no_write:
        atomic_write_json(status_file, payload)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["safe"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

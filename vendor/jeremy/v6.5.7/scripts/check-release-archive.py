#!/usr/bin/env python3
"""Reject release archives that accidentally include VCS metadata or live data."""

from __future__ import annotations

import argparse
import os
import sys
import tarfile
import zipfile
from pathlib import Path


DENY_COMPONENTS = {
    ".git",
    ".github",
    ".pytest_cache",
    "__pycache__",
    "runtime",
    "data",
}
DENY_BASENAMES = {
    ".env",
    "node.conf",
    "latest.bdsnap",
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
}
DENY_SUFFIXES = (".pyc", ".pyo")

LOCAL_SKIP_COMPONENTS = {
    ".git",
    ".pytest_cache",
    "__pycache__",
}
LOCAL_SKIP_SUFFIXES = DENY_SUFFIXES


def iter_members(path: Path) -> list[str]:
    if path.is_dir():
        members: list[str] = []
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in LOCAL_SKIP_COMPONENTS]
            for name in dirs:
                full = Path(root, name)
                members.append(full.relative_to(path).as_posix())
            for name in files:
                if name.endswith(LOCAL_SKIP_SUFFIXES):
                    continue
                full = Path(root, name)
                members.append(full.relative_to(path).as_posix())
        return members
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            return zf.namelist()
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as tf:
            return tf.getnames()
    raise ValueError(f"unsupported archive format: {path}")


def blocked_reason(member: str) -> str | None:
    normalized = member.strip("/").replace("\\", "/")
    if not normalized:
        return None
    parts = [part for part in normalized.split("/") if part]
    basename = parts[-1]
    if any(part in DENY_COMPONENTS or part.startswith("runtime-") for part in parts):
        return "VCS metadata, cache, or mutable runtime/data directory"
    if basename.endswith(DENY_SUFFIXES):
        return "Python bytecode/cache file"
    if basename in DENY_BASENAMES or basename.endswith(".bdsnap.part") or basename.endswith(".tmp"):
        return "mutable host config, snapshot, or temporary file"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()

    failed = False
    for path in args.paths:
        try:
            members = iter_members(path)
        except Exception as exc:  # noqa: BLE001 - CLI reports the path and reason.
            print(f"{path}: {exc}", file=sys.stderr)
            failed = True
            continue
        blocked = [(member, reason) for member in members if (reason := blocked_reason(member))]
        if blocked:
            failed = True
            print(f"{path}: blocked release members:", file=sys.stderr)
            for member, reason in blocked[:50]:
                print(f"  {member}: {reason}", file=sys.stderr)
            if len(blocked) > 50:
                print(f"  ... {len(blocked) - 50} more", file=sys.stderr)
        else:
            print(f"{path}: release archive metadata/data audit passed ({len(members)} members)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

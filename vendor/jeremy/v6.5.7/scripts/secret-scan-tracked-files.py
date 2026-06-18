#!/usr/bin/env python3
"""Fail if tracked files contain obvious private secrets."""

from __future__ import annotations

import re
import subprocess
import sys
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

SECRET_PATTERNS = [
    ("private_key", re.compile(rb"-----BEGIN (?:OPENSSH|RSA|DSA|EC|PRIVATE) PRIVATE KEY-----")),
    ("age_secret_key", re.compile(rb"AGE-SECRET-KEY-[A-Z0-9]+")),
    ("github_token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9_]{30,}\b")),
    ("aws_access_key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("raw_signed_url", re.compile(rb"https?://[^\s\"']+[?&](?:X-Amz-Signature|sig|signature)=")),
]

ALLOWLIST = {
    "scripts/secret-scan-tracked-files.py",
}


def scan_files() -> list[Path]:
    try:
        raw = subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "-z"], stderr=subprocess.DEVNULL)
        return [ROOT / item.decode("utf-8") for item in raw.split(b"\0") if item]
    except (subprocess.CalledProcessError, FileNotFoundError):
        ignored_names = {".git", "__pycache__", "data", "data-restore", "data-repair", "release-downloads"}
        ignored_rels = {"ops/runtime"}
        files: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            current = Path(dirpath)
            rel_dir = current.relative_to(ROOT).as_posix()
            dirnames[:] = [
                name
                for name in dirnames
                if name not in ignored_names
                and not name.startswith("data-")
                and f"{rel_dir}/{name}".lstrip("./") not in ignored_rels
                and not f"{rel_dir}/{name}".lstrip("./").startswith("ops/runtime-")
            ]
            for filename in filenames:
                files.append(current / filename)
        return files


def main() -> int:
    violations: list[str] = []
    for path in scan_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWLIST or not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            violations.append(f"{rel}:read_failed:{exc}")
            continue
        for pattern_id, pattern in SECRET_PATTERNS:
            if pattern.search(data):
                violations.append(f"{rel}:{pattern_id}")
                break

    if violations:
        print("tracked secret scan failed", file=sys.stderr)
        for item in violations[:50]:
            print(f"- {item}", file=sys.stderr)
        if len(violations) > 50:
            print(f"- truncated:{len(violations) - 50}", file=sys.stderr)
        return 1
    print("tracked secret scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Replay a recorded stack status payload through guard entrypoints safely."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent


def run_script(script: str, env: dict[str, str], args: list[str]) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(ROOT / script), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    return {
        "script": script,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status-file", required=True, help="recorded /api/status JSON payload")
    parser.add_argument("--runtime-dir", help="isolated runtime directory; defaults to a temp dir")
    parser.add_argument(
        "--mode",
        choices=["all", "watchdog", "sentinel", "mining"],
        default="all",
        help="which guard entrypoints to replay",
    )
    args = parser.parse_args(argv)

    status_file = Path(args.status_file).expanduser().resolve()
    if not status_file.exists():
        raise SystemExit(f"status file not found: {status_file}")

    runtime_dir = Path(args.runtime_dir).expanduser().resolve() if args.runtime_dir else Path(
        tempfile.mkdtemp(prefix="stack-triage-")
    )
    runtime_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["BDAG_STATUS_SOURCE_FIXTURE"] = str(status_file)
    env["BDAG_RUNTIME_DIR"] = str(runtime_dir)

    plan = {
        "all": [
            ("watchdog.py", ["--once", "--dry-run"]),
            ("stack_sentinel.py", ["--dry-run"]),
            ("mining_guard_30min.py", ["--once", "--dry-run"]),
        ],
        "watchdog": [("watchdog.py", ["--once", "--dry-run"])],
        "sentinel": [("stack_sentinel.py", ["--dry-run"])],
        "mining": [("mining_guard_30min.py", ["--once", "--dry-run"])],
    }

    results = []
    for script, script_args in plan[args.mode]:
        results.append(run_script(script, env, script_args))

    payload = {
        "status_file": str(status_file),
        "runtime_dir": str(runtime_dir),
        "mode": args.mode,
        "results": results,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if all(item["returncode"] == 0 for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Capture a live stack status payload to disk for replay testing."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from stack_status_source import collect_stack_status


def default_output_path() -> Path:
    stamp = os.environ.get("BDAG_STATUS_CAPTURE_STAMP")
    if not stamp:
        from datetime import datetime, timezone

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path.cwd() / f"stack-status-{stamp}.json"


def capture_status(include_logs: bool, timeout: float) -> dict[str, Any]:
    return collect_stack_status(include_logs=include_logs, timeout=timeout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=default_output_path(), help="path to write the captured JSON")
    parser.add_argument("--timeout", type=float, default=5.0, help="status-source timeout in seconds")
    parser.add_argument("--include-logs", action="store_true", help="include recent logs in the captured payload")
    parser.add_argument("--no-include-logs", dest="include_logs", action="store_false", help="exclude logs from capture")
    parser.set_defaults(include_logs=True)
    args = parser.parse_args(argv)

    payload = capture_status(include_logs=args.include_logs, timeout=args.timeout)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(str(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

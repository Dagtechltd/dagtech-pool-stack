#!/usr/bin/env python3
"""Write shared BlockDAG status and plot-history samples for local agents."""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any

from pool_ops import (
    EARNINGS_SNAPSHOT_EXPECTED_INTERVAL_SECONDS,
    LOG_DIR,
    STATUS_SAMPLER_FILE,
    collect_status_cached,
    ensure_runtime,
    now_iso,
    read_latest_earnings_snapshot_info,
    record_earnings_snapshot,
    write_json_file,
    write_status_sampler_payload,
)


def env_float(name: str, default: float, minimum: float | None = None) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


DEFAULT_INTERVAL_SECONDS = env_float("BDAG_STATUS_SAMPLER_INTERVAL_SECONDS", 10.0, minimum=1.0)
DEFAULT_EARNINGS_SNAPSHOT_INTERVAL_SECONDS = env_float(
    "BDAG_STATUS_SAMPLER_EARNINGS_SNAPSHOT_INTERVAL_SECONDS",
    float(EARNINGS_SNAPSHOT_EXPECTED_INTERVAL_SECONDS),
    minimum=0.0,
)
LOG_FILE = LOG_DIR / "status-sampler.log"


def log(message: str) -> None:
    ensure_runtime()
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now_iso()}] {message}\n")


def write_error_state(error: Exception) -> None:
    write_json_file(
        STATUS_SAMPLER_FILE,
        {
            "schema_version": 1,
            "updated_at": now_iso(),
            "epoch": time.time(),
            "status": "failed",
            "error": str(error),
        },
        mode=0o600,
    )


def sample_once(include_logs: bool) -> dict[str, Any]:
    # max_age_seconds=0 is the explicit hard-bypass path: do not read either
    # the shared sampler file or the short shared cache while producing a sample.
    payload = collect_status_cached(include_logs=include_logs, max_age_seconds=0)
    write_status_sampler_payload(payload, include_logs=include_logs)
    log(
        "sampled "
        f"overall={payload.get('overall')} mode={payload.get('mode')} "
        f"fresh={payload.get('fresh')} include_logs={include_logs}"
    )
    return payload


def maybe_record_earnings_snapshot(
    now_epoch: float,
    last_attempt_epoch: float,
    interval_seconds: float,
    enabled: bool,
) -> float:
    if not enabled or interval_seconds <= 0:
        return last_attempt_epoch
    if last_attempt_epoch and now_epoch - last_attempt_epoch < interval_seconds:
        return last_attempt_epoch

    info = read_latest_earnings_snapshot_info()
    latest_epoch = info.get("latest_epoch")
    try:
        latest_age = now_epoch - float(latest_epoch) if latest_epoch is not None else None
    except (TypeError, ValueError):
        latest_age = None
    if latest_age is not None and latest_age < interval_seconds:
        return last_attempt_epoch

    try:
        snapshot = record_earnings_snapshot()
    except Exception as exc:  # noqa: BLE001 - status sampling must not die on plot history failures.
        log(f"earnings snapshot failed: {exc}")
        return now_epoch
    miners = snapshot.get("miner_estimates")
    miner_count = len(miners) if isinstance(miners, list) else 0
    log(f"earnings snapshot recorded generated_at={snapshot.get('generated_at')} miners={miner_count}")
    return now_epoch


def run_loop(interval_seconds: float, include_logs: bool, earnings_snapshot_interval_seconds: float, record_earnings: bool) -> int:
    ensure_runtime()
    last_earnings_attempt_epoch = 0.0
    while True:
        started = time.time()
        try:
            sample_once(include_logs=include_logs)
            last_earnings_attempt_epoch = maybe_record_earnings_snapshot(
                time.time(),
                last_earnings_attempt_epoch,
                earnings_snapshot_interval_seconds,
                record_earnings,
            )
        except Exception as exc:  # noqa: BLE001 - sampler must keep trying.
            log(f"sample failed: {exc}")
            try:
                write_error_state(exc)
            except Exception as write_exc:  # noqa: BLE001
                log(f"failed to write error state: {write_exc}")
        elapsed = time.time() - started
        time.sleep(max(1.0, interval_seconds - elapsed))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true", help="keep sampling until the service is stopped")
    parser.add_argument("--interval-seconds", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument(
        "--earnings-snapshot-interval-seconds",
        type=float,
        default=DEFAULT_EARNINGS_SNAPSHOT_INTERVAL_SECONDS,
        help="append miner/earnings plot snapshots when the valid history is older than this interval; 0 disables",
    )
    parser.add_argument(
        "--no-earnings-snapshots",
        action="store_true",
        help="do not append miner/earnings plot snapshots from the status sampler",
    )
    parser.add_argument("--no-logs", action="store_true", help="omit container log tails from each sample")
    parser.add_argument("--json", action="store_true", help="print the sampled payload")
    args = parser.parse_args()

    include_logs = not args.no_logs
    if args.loop:
        return run_loop(
            max(1.0, args.interval_seconds),
            include_logs,
            max(0.0, args.earnings_snapshot_interval_seconds),
            not args.no_earnings_snapshots,
        )
    try:
        payload = sample_once(include_logs=include_logs)
    except Exception as exc:  # noqa: BLE001
        log(f"sample failed: {exc}")
        write_error_state(exc)
        raise
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

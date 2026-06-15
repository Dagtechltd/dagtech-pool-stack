#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path


POOL_SUBMIT_HARDENING_FLAGS = (
    ("POOL_SUBMIT_STALE_BLOCK_CANDIDATES", "${POOL_SUBMIT_STALE_BLOCK_CANDIDATES:-false}"),
    ("POOL_SUBMIT_BLOCK_HEADER_V2_ENABLED", "${POOL_SUBMIT_BLOCK_HEADER_V2_ENABLED:-true}"),
    ("POOL_AUTO_TUNE_BLOCK_CANDIDATE_JOB_AGE", "${POOL_AUTO_TUNE_BLOCK_CANDIDATE_JOB_AGE:-false}"),
    ("POOL_STALE_RACE_CLIENT_RESEND_THRESHOLD", "${POOL_STALE_RACE_CLIENT_RESEND_THRESHOLD:-3}"),
    ("POOL_STALE_RACE_RECOVERY_COOLDOWN_SECONDS", "${POOL_STALE_RACE_RECOVERY_COOLDOWN_SECONDS:-15}"),
    ("POOL_EXPIRED_JOB_CLIENT_RECONNECT_THRESHOLD", "${POOL_EXPIRED_JOB_CLIENT_RECONNECT_THRESHOLD:-3}"),
    ("POOL_EXPIRED_JOB_CLIENT_RECONNECT_WINDOW_SECONDS", "${POOL_EXPIRED_JOB_CLIENT_RECONNECT_WINDOW_SECONDS:-120}"),
    ("POOL_EXPIRED_JOB_CLIENT_RECONNECT_COOLDOWN_SECONDS", "${POOL_EXPIRED_JOB_CLIENT_RECONNECT_COOLDOWN_SECONDS:-60}"),
)


@dataclass(frozen=True)
class MigrationResult:
    text: str
    changed: bool
    inserted_count: int


def _service_ranges(lines: list[str]) -> list[tuple[str, int, int]]:
    ranges: list[tuple[str, int, int]] = []
    in_services = False
    current_name = ""
    current_start = -1

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not line.startswith(" ") and stripped == "services:":
            in_services = True
            continue
        if not in_services:
            continue
        if line and not line.startswith(" ") and stripped.endswith(":"):
            if current_start >= 0:
                ranges.append((current_name, current_start, index))
            break
        if line.startswith("  ") and not line.startswith("    ") and stripped.endswith(":"):
            if current_start >= 0:
                ranges.append((current_name, current_start, index))
            current_name = stripped[:-1]
            current_start = index

    if in_services and current_start >= 0:
        ranges.append((current_name, current_start, len(lines)))
    return ranges


def _pool_service(name: str) -> bool:
    return name in {"pool", "asic-pool"}


def _environment_range(lines: list[str], start: int, end: int) -> tuple[int, int] | None:
    env_start: int | None = None
    for index in range(start + 1, end):
        line = lines[index]
        stripped = line.strip()
        if line.startswith("    ") and not line.startswith("      "):
            if env_start is None:
                if stripped == "environment:":
                    env_start = index + 1
                continue
            return env_start, index
    if env_start is None:
        return None
    return env_start, end


def _line_has_key(line: str, key: str) -> bool:
    return line.strip().startswith(f"{key}:")


def ensure_pool_env_flags(text: str, flags: tuple[tuple[str, str], ...]) -> MigrationResult:
    if not flags:
        return MigrationResult(text=text, changed=False, inserted_count=0)

    lines = text.splitlines()
    trailing_newline = text.endswith("\n")
    inserted_count = 0
    flag_keys = tuple(key for key, _ in flags)

    for name, start, end in reversed(_service_ranges(lines)):
        if not _pool_service(name):
            continue
        env_range = _environment_range(lines, start, end)
        if env_range is None:
            continue
        env_start, env_end = env_range
        missing = [
            (key, value)
            for key, value in flags
            if not any(_line_has_key(lines[index], key) for index in range(env_start, env_end))
        ]
        if not missing:
            continue

        insert_at = env_start
        for index in range(env_start, env_end):
            if any(_line_has_key(lines[index], key) for key in flag_keys):
                insert_at = index + 1
        if insert_at == env_start:
            for index in range(env_start, env_end):
                if _line_has_key(lines[index], "NODE_RPC_URL"):
                    insert_at = index + 1
                    break
        if insert_at == env_start:
            for index in range(env_start, env_end):
                if _line_has_key(lines[index], "NODE_RPC_URL"):
                    insert_at = index + 1
                    break

        indent = "      "
        if env_start < env_end:
            anchor_index = min(max(insert_at - 1, env_start), env_end - 1)
            anchor = lines[anchor_index]
            if anchor.strip():
                indent = anchor[: len(anchor) - len(anchor.lstrip())]
        for key, value in reversed(missing):
            lines.insert(insert_at, f"{indent}{key}: {value}")
            inserted_count += 1

    migrated = "\n".join(lines)
    if trailing_newline:
        migrated += "\n"
    return MigrationResult(text=migrated, changed=inserted_count > 0, inserted_count=inserted_count)


def ensure_pool_submit_hardening_flags(text: str) -> MigrationResult:
    return ensure_pool_env_flags(text, POOL_SUBMIT_HARDENING_FLAGS)


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply idempotent live runtime compose migrations.")
    parser.add_argument("--ensure-pool-submit-hardening", action="store_true")
    parser.add_argument("compose_file", type=Path)
    args = parser.parse_args()

    if not args.ensure_pool_submit_hardening:
        parser.error("one migration flag is required")

    text = args.compose_file.read_text(encoding="utf-8")
    result = ensure_pool_submit_hardening_flags(text)
    required_keys = tuple(key for key, _ in POOL_SUBMIT_HARDENING_FLAGS)

    missing_keys = [key for key in required_keys if f"{key}:" not in result.text]
    if missing_keys:
        raise SystemExit(f"could not insert {', '.join(missing_keys)}; no eligible pool service was found")
    if result.changed:
        args.compose_file.write_text(result.text, encoding="utf-8")
        print(f"inserted {result.inserted_count} pool submit hardening setting(s)")
    else:
        print("pool submit hardening settings already present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

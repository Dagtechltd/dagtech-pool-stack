#!/usr/bin/env python3
"""Boot-time status verifier and Codex handoff writer for the BlockDAG stack."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("BDAG_PROJECT_ROOT", str(DEFAULT_PROJECT_ROOT))
os.environ.setdefault("BDAG_RUNTIME_DIR", str(DEFAULT_PROJECT_ROOT / "ops" / "runtime"))

from pool_ops import RUNTIME_DIR, collect_status, make_handoff, now_iso  # noqa: E402


BOOT_HANDOFF_JSON = RUNTIME_DIR / "codex-boot-handoff.json"
BOOT_HANDOFF_MD = RUNTIME_DIR / "codex-boot-handoff.md"


def atomic_write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(body, encoding="utf-8")
    temp.replace(path)


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def uptime_since() -> str:
    result = subprocess.run(["uptime", "-s"], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    return result.stdout.strip()


def dashboard_status(url: str, timeout: float) -> dict:
    with urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def status_snapshot(dashboard_url: str, timeout: float) -> tuple[dict, str]:
    try:
        return dashboard_status(dashboard_url, timeout), "dashboard"
    except (OSError, TimeoutError, URLError, json.JSONDecodeError):
        return collect_status(include_logs=False), "direct"


def node_stack_running(status: dict) -> bool:
    containers = status.get("containers") if isinstance(status.get("containers"), dict) else {}
    postgres = containers.get("postgres") if isinstance(containers.get("postgres"), dict) else {}
    node = containers.get("node") if isinstance(containers.get("node"), dict) else {}
    return bool(postgres.get("running") and node.get("running"))


def acceptable_status(status: dict) -> bool:
    failures = status.get("blocking_failures") or status.get("failures") or []
    overall = str(status.get("overall") or "")
    if failures:
        return False
    if not node_stack_running(status):
        return False
    return overall in {"ok", "syncing", "pool_start_blocked", "degraded"}


def run_boot_repair(project_root: Path, reason: str) -> dict:
    command = [sys.executable, str(project_root / "ops" / "watchdog.py"), "--boot-repair"]
    started = time.time()
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-4000:],
        "stderr_tail": result.stderr[-4000:],
        "elapsed_seconds": round(time.time() - started, 3),
        "reason": reason,
    }


def codex_resume_command(project_root: Path, session_id: str) -> str:
    if not session_id:
        return ""
    return " ".join(
        shlex.quote(part)
        for part in [
            "codex",
            "resume",
            "--cd",
            str(project_root),
            "--ask-for-approval",
            "never",
            "--sandbox",
            "danger-full-access",
            "--dangerously-bypass-approvals-and-sandbox",
            session_id,
        ]
    )


def extract_resume_session_id(parts: list[str]) -> str:
    options_with_values = {
        "-a",
        "-C",
        "-c",
        "-i",
        "-m",
        "-p",
        "-s",
        "--add-dir",
        "--ask-for-approval",
        "--cd",
        "--config",
        "--image",
        "--local-provider",
        "--model",
        "--profile",
        "--remote",
        "--remote-auth-token-env",
        "--sandbox",
    }
    long_options_with_values = {item for item in options_with_values if item.startswith("--")}
    try:
        index = parts.index("resume") + 1
    except ValueError:
        return ""
    while index < len(parts):
        part = parts[index]
        if part in options_with_values:
            index += 2
            continue
        if any(part.startswith(f"{option}=") for option in long_options_with_values):
            index += 1
            continue
        if part.startswith("-"):
            index += 1
            continue
        return part
    return ""


def discover_codex_resume_command(project_root: Path, session_id: str) -> str:
    if session_id:
        return codex_resume_command(project_root, session_id)
    result = subprocess.run(
        ["pgrep", "-af", r"codex"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    for line in result.stdout.splitlines():
        discovered_session_id = extract_resume_session_id(line.split())
        if discovered_session_id:
            return codex_resume_command(project_root, discovered_session_id)
    return ""


def write_handoff(summary: dict) -> None:
    handoff_path = make_handoff()
    summary["codex_handoff_path"] = str(handoff_path)
    atomic_write(BOOT_HANDOFF_JSON, json.dumps(summary, indent=2, sort_keys=True) + "\n")
    status = summary.get("status", {})
    lines = [
        "# Codex Boot Handoff",
        "",
        f"Generated: {summary.get('generated_at')}",
        f"Boot ID: {summary.get('boot_id')}",
        f"Boot Time: {summary.get('uptime_since')}",
        f"Dashboard: {summary.get('dashboard_url')}",
        f"Status source: {summary.get('status_source')}",
        f"Overall: {status.get('overall')}",
        f"Mode: {status.get('mode')}",
        f"Blocking failures: {status.get('blocking_failures') or []}",
        f"Status reason: {status.get('status_reason')}",
        f"Codex resume command: {summary.get('codex_resume_command') or 'not recorded'}",
        "",
        "Note: an interactive terminal process cannot survive a host reboot. Resume the Codex session with the command above, then read:",
        f"- {handoff_path}",
        f"- {BOOT_HANDOFF_JSON}",
        f"- {RUNTIME_DIR / 'latest-action.json'}",
    ]
    atomic_write(BOOT_HANDOFF_MD, "\n".join(lines).rstrip() + "\n")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Verify BlockDAG stack after boot and write a Codex handoff")
    parser.add_argument("--dashboard-url", default=os.environ.get("BDAG_CODEX_BOOT_DASHBOARD_URL", "http://127.0.0.1:8088/api/status"))
    parser.add_argument("--wait-seconds", type=float, default=float(os.environ.get("BDAG_CODEX_BOOT_VERIFY_WAIT_SECONDS", "300")))
    parser.add_argument("--interval-seconds", type=float, default=float(os.environ.get("BDAG_CODEX_BOOT_VERIFY_INTERVAL_SECONDS", "10")))
    parser.add_argument("--repair", action="store_true", default=os.environ.get("BDAG_CODEX_BOOT_VERIFY_REPAIR", "1").lower() not in {"0", "false", "no", "off"})
    parser.add_argument("--session-id", default=os.environ.get("BDAG_CODEX_RESUME_SESSION_ID", ""))
    args = parser.parse_args(argv)

    project_root = Path(os.environ.get("BDAG_PROJECT_ROOT", DEFAULT_PROJECT_ROOT)).expanduser()
    deadline = time.time() + max(1.0, args.wait_seconds)
    attempts: list[dict] = []
    repair_result: dict | None = None
    status: dict = {}
    source = ""

    while True:
        status, source = status_snapshot(args.dashboard_url, timeout=5)
        attempts.append(
            {
                "at": now_iso(),
                "source": source,
                "overall": status.get("overall"),
                "mode": status.get("mode"),
                "blocking_failures": status.get("blocking_failures") or status.get("failures") or [],
                "node_stack_running": node_stack_running(status),
            }
        )
        if acceptable_status(status):
            break
        if args.repair and repair_result is None:
            reason = "; ".join(str(item) for item in attempts[-1]["blocking_failures"]) or "boot verifier status not acceptable"
            repair_result = run_boot_repair(project_root, reason)
        if time.time() >= deadline:
            break
        time.sleep(max(1.0, args.interval_seconds))

    ok = acceptable_status(status)
    summary = {
        "generated_at": now_iso(),
        "boot_id": boot_id(),
        "uptime_since": uptime_since(),
        "dashboard_url": args.dashboard_url,
        "status_source": source,
        "ok": ok,
        "manual_prompt_required_for_stack": not ok,
        "interactive_terminal_survives_reboot": False,
        "codex_resume_command": discover_codex_resume_command(project_root, args.session_id),
        "attempts": attempts,
        "repair_result": repair_result,
        "status": {
            "generated_at": status.get("generated_at"),
            "overall": status.get("overall"),
            "mode": status.get("mode"),
            "can_mine": status.get("can_mine"),
            "blocking_failures": status.get("blocking_failures") or status.get("failures") or [],
            "status_reason": status.get("status_reason"),
            "containers": {
                name: {
                    "name": item.get("name"),
                    "running": item.get("running"),
                    "status": item.get("status"),
                }
                for name, item in (status.get("containers") or {}).items()
                if isinstance(item, dict)
            },
        },
    }
    write_handoff(summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

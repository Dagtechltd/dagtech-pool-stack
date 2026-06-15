#!/usr/bin/env python3
"""Start a Codex resume terminal after boot and verify the pool first."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path


DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_DIR = DEFAULT_PROJECT_ROOT / "ops" / "runtime"


def read_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(payload, encoding="utf-8")
    temp.replace(path)


def acquire_run_lock(runtime_dir: Path):
    lock_path = runtime_dir / "codex-auto-resume.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("w", encoding="utf-8")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    return lock_file


def launch_marker_path(runtime_dir: Path) -> Path:
    return runtime_dir / "codex-auto-resume-launch.json"


def read_launch_marker(runtime_dir: Path) -> dict:
    path = launch_marker_path(runtime_dir)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_launch_marker(runtime_dir: Path, payload: dict) -> None:
    atomic_write(launch_marker_path(runtime_dir), json.dumps(payload, indent=2, sort_keys=True) + "\n")


def session_slug(session_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", session_id).strip("-")
    return cleaned[:48] or "unknown"


def codex_resume_args(codex_bin: str, project_root: Path, session_id: str) -> list[str]:
    return [
        codex_bin,
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


def display_codex_resume_command(project_root: Path, session_id: str) -> str:
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


def matching_codex_processes(session_id: str) -> list[str]:
    result = subprocess.run(
        ["pgrep", "-af", "codex"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    current_pid = str(os.getpid())
    matches = []
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=1)
        if fields and fields[0] == current_pid:
            continue
        if " resume " in f" {line} " and session_id in line:
            matches.append(line)
    return matches


def discover_codex_session_id() -> str:
    result = subprocess.run(
        ["pgrep", "-af", "codex"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    for line in result.stdout.splitlines():
        session_id = extract_resume_session_id(line.split())
        if session_id:
            return session_id
    return ""


def choose_backend(preferred: str, *, visible: bool) -> str:
    choices = [preferred] if preferred else []
    if visible:
        choices.extend(["ptyxis", "gnome-terminal", "kgx", "xterm"])
    else:
        choices.extend(["tmux", "screen"])
    for item in choices:
        if item and shutil.which(item):
            return item
    return ""


def run_pool_check(project_root: Path, env: dict[str, str], wait_seconds: float, interval_seconds: float) -> dict:
    command = [
        sys.executable,
        str(project_root / "ops" / "codex_boot_handoff.py"),
        "--repair",
        "--wait-seconds",
        str(wait_seconds),
        "--interval-seconds",
        str(interval_seconds),
    ]
    started = time.time()
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False)
    parsed: dict = {}
    if result.stdout.strip():
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError:
            parsed = {}
    return {
        "command": command,
        "returncode": result.returncode,
        "elapsed_seconds": round(time.time() - started, 3),
        "stdout_tail": result.stdout[-4000:],
        "stderr_tail": result.stderr[-4000:],
        "parsed": parsed,
    }


def terminal_environment(env: dict[str, str]) -> dict[str, str]:
    merged = dict(env)
    if not merged.get("DBUS_SESSION_BUS_ADDRESS") and merged.get("XDG_RUNTIME_DIR"):
        merged["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={merged['XDG_RUNTIME_DIR']}/bus"
    return merged


def start_terminal_backend(
    backend: str,
    session_name: str,
    project_root: Path,
    codex_bin: str,
    session_id: str,
    log_path: Path,
    env: dict[str, str],
    *,
    run_check_in_terminal: bool = False,
    wait_seconds: float = 300.0,
    interval_seconds: float = 10.0,
) -> subprocess.CompletedProcess[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    resume_args = codex_resume_args(codex_bin, project_root, session_id)
    resume_command = " ".join(shlex.quote(part) for part in resume_args)
    pool_check_command = ""
    if run_check_in_terminal:
        handoff = project_root / "ops" / "codex_boot_handoff.py"
        pool_check_command = (
            "echo '[bdag] normal desktop boot is live; checking pool and stack now' | "
            f"tee -a {shlex.quote(str(log_path))}; "
            "set -o pipefail; "
            f"{shlex.quote(sys.executable)} {shlex.quote(str(handoff))} --repair "
            f"--wait-seconds {shlex.quote(str(wait_seconds))} "
            f"--interval-seconds {shlex.quote(str(interval_seconds))} 2>&1 | "
            f"tee -a {shlex.quote(str(log_path))}; "
            "pool_check_rc=$?; set +o pipefail; "
            f"echo \"[bdag] pool check return code: ${{pool_check_rc}}\" | tee -a {shlex.quote(str(log_path))}; "
        )
    shell_command = (
        f"cd {shlex.quote(str(project_root))} && "
        f"export BDAG_PROJECT_ROOT={shlex.quote(str(project_root))} "
        f"BDAG_RUNTIME_DIR={shlex.quote(env.get('BDAG_RUNTIME_DIR', str(DEFAULT_RUNTIME_DIR)))} && "
        f"{pool_check_command}"
        f"echo '[bdag] resuming Codex session {shlex.quote(session_id)} from {shlex.quote(str(project_root))}'; "
        f"{resume_command}; "
        "codex_rc=$?; "
        f"echo \"[bdag] Codex exited with return code ${{codex_rc}}\" | tee -a {shlex.quote(str(log_path))}; "
        "echo; echo '[bdag] Codex exited. Press Enter to close this terminal.'; read -r _"
    )
    if backend == "ptyxis":
        command = ["ptyxis", "--new-window", "--title", "BlockDAG Codex Resume", "--working-directory", str(project_root), "--", "bash", "-lc", shell_command]
    elif backend == "gnome-terminal":
        command = ["gnome-terminal", "--title", "BlockDAG Codex Resume", "--working-directory", str(project_root), "--", "bash", "-lc", shell_command]
    elif backend == "kgx":
        command = ["kgx", "--title", "BlockDAG Codex Resume", "--working-directory", str(project_root), "--", "bash", "-lc", shell_command]
    elif backend == "xterm":
        command = ["xterm", "-T", "BlockDAG Codex Resume", "-e", "bash", "-lc", shell_command]
    elif backend == "tmux":
        command = ["tmux", "new-session", "-d", "-s", session_name, shell_command]
    elif backend == "screen":
        command = ["screen", "-dmS", session_name, "bash", "-lc", shell_command]
    else:
        raise ValueError(f"unsupported terminal backend: {backend}")
    if backend in {"ptyxis", "gnome-terminal", "kgx", "xterm"}:
        try:
            proc = subprocess.Popen(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            return subprocess.CompletedProcess(command, 127, "", str(exc))
        try:
            stdout, stderr = proc.communicate(timeout=2)
            return subprocess.CompletedProcess(command, proc.returncode or 0, stdout or "", stderr or "")
        except subprocess.TimeoutExpired:
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
            return subprocess.CompletedProcess(command, 0, "", "")
    return subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False)


def write_summary(runtime_dir: Path, summary: dict) -> None:
    json_path = runtime_dir / "codex-auto-resume.json"
    md_path = runtime_dir / "codex-auto-resume.md"
    atomic_write(json_path, json.dumps(summary, indent=2, sort_keys=True) + "\n")
    lines = [
        "# Codex Auto Resume",
        "",
        f"Generated: {summary.get('generated_at')}",
        f"Boot ID: {summary.get('boot_id')}",
        f"Status: {summary.get('status')}",
        f"Terminal backend: {summary.get('terminal_backend') or 'none'}",
        f"Terminal session: {summary.get('terminal_session') or 'none'}",
        f"Visible terminal: {summary.get('visible_terminal')}",
        f"Codex resume command: {summary.get('codex_resume_command')}",
        f"Pool check return code: {(summary.get('pool_check') or {}).get('returncode')}",
        "",
        "Attach to the detached terminal:",
    ]
    if summary.get("visible_terminal"):
        lines.append("- use the visible desktop terminal titled BlockDAG Codex Resume")
    elif summary.get("terminal_backend") == "screen":
        lines.append(f"- screen -r {summary.get('terminal_session')}")
    elif summary.get("terminal_backend") == "tmux":
        lines.append(f"- tmux attach -t {summary.get('terminal_session')}")
    else:
        lines.append("- no detached terminal was started")
    atomic_write(md_path, "\n".join(lines).rstrip() + "\n")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Auto-start Codex resume in a visible desktop terminal after boot")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--backend", default="")
    parser.add_argument("--detached", action="store_true", default=os.environ.get("BDAG_CODEX_AUTO_RESUME_VISIBLE", "1").lower() in {"0", "false", "no", "off"})
    parser.add_argument("--wait-seconds", type=float, default=None)
    parser.add_argument("--interval-seconds", type=float, default=None)
    parser.add_argument("--skip-pool-check", action="store_true")
    args = parser.parse_args(argv)

    project_root = Path(os.environ.get("BDAG_PROJECT_ROOT", DEFAULT_PROJECT_ROOT)).expanduser()
    runtime_dir = Path(os.environ.get("BDAG_RUNTIME_DIR", DEFAULT_RUNTIME_DIR)).expanduser()
    read_env_file(runtime_dir / "ops.env")
    if not args.backend:
        args.backend = os.environ.get("BDAG_CODEX_AUTO_RESUME_BACKEND", "ptyxis")
    if args.wait_seconds is None:
        args.wait_seconds = float(
            os.environ.get(
                "BDAG_CODEX_AUTO_RESUME_CHECK_WAIT_SECONDS",
                os.environ.get("BDAG_CODEX_BOOT_VERIFY_WAIT_SECONDS", "60"),
            )
        )
    if args.interval_seconds is None:
        args.interval_seconds = float(
            os.environ.get(
                "BDAG_CODEX_AUTO_RESUME_CHECK_INTERVAL_SECONDS",
                os.environ.get("BDAG_CODEX_BOOT_VERIFY_INTERVAL_SECONDS", "10"),
            )
        )
    env = terminal_environment(dict(os.environ))
    env["PATH"] = f"{Path.home() / '.npm-global/bin'}:{env.get('PATH') or '/usr/local/bin:/usr/bin:/bin'}"
    env.setdefault("BDAG_PROJECT_ROOT", str(project_root))
    env.setdefault("BDAG_RUNTIME_DIR", str(runtime_dir))
    env.setdefault("TERM", "xterm-256color")

    session_id = args.session_id or env.get("BDAG_CODEX_RESUME_SESSION_ID", "") or discover_codex_session_id()
    codex_bin = env.get("BDAG_CODEX_BIN") or shutil.which("codex", path=env.get("PATH")) or ""
    session_name = f"bdag-codex-{session_slug(session_id)}"
    log_path = runtime_dir / "logs" / "codex-auto-resume.log"
    lock_file = acquire_run_lock(runtime_dir)
    visible = not args.detached
    if args.skip_pool_check:
        pool_check = None
    elif visible:
        pool_check = {
            "mode": "inside_visible_terminal",
            "wait_seconds": args.wait_seconds,
            "interval_seconds": args.interval_seconds,
        }
    else:
        pool_check = run_pool_check(project_root, env, args.wait_seconds, args.interval_seconds)
    existing = matching_codex_processes(session_id) if session_id else []
    current_boot_id = boot_id()

    summary = {
        "generated_at": now_iso(),
        "boot_id": current_boot_id,
        "status": "pending",
        "session_id": session_id,
        "codex_resume_command": display_codex_resume_command(project_root, session_id),
        "codex_bin": codex_bin,
        "terminal_backend": "",
        "terminal_session": "",
        "visible_terminal": not args.detached,
        "existing_processes": existing,
        "pool_check": pool_check,
        "log_path": str(log_path),
    }

    if not session_id:
        summary["status"] = "failed_missing_session_id"
        write_summary(runtime_dir, summary)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 2
    if not codex_bin:
        summary["status"] = "failed_missing_codex_binary"
        write_summary(runtime_dir, summary)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 3
    if existing:
        summary["status"] = "already_running"
        write_summary(runtime_dir, summary)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    marker = read_launch_marker(runtime_dir)
    if visible and marker.get("boot_id") == current_boot_id and marker.get("session_id") == session_id:
        summary["status"] = "already_launched_this_boot"
        summary["launch_marker"] = marker
        write_summary(runtime_dir, summary)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    backend = choose_backend(args.backend, visible=not args.detached)
    if not backend:
        summary["status"] = "failed_missing_terminal_backend"
        write_summary(runtime_dir, summary)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 4

    result = start_terminal_backend(
        backend,
        session_name,
        project_root,
        codex_bin,
        session_id,
        log_path,
        env,
        run_check_in_terminal=visible and not args.skip_pool_check,
        wait_seconds=args.wait_seconds,
        interval_seconds=args.interval_seconds,
    )
    summary.update(
        {
            "status": "started" if result.returncode == 0 else "failed_start",
            "terminal_backend": backend,
            "terminal_session": session_name,
            "start_returncode": result.returncode,
            "start_stdout": result.stdout,
            "start_stderr": result.stderr,
        }
    )
    if result.returncode == 0:
        write_launch_marker(
            runtime_dir,
            {
                "boot_id": current_boot_id,
                "generated_at": now_iso(),
                "session_id": session_id,
                "terminal_backend": backend,
                "terminal_session": session_name,
                "visible_terminal": visible,
            },
        )
    write_summary(runtime_dir, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    lock_file.close()
    return 0 if result.returncode == 0 else 5


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

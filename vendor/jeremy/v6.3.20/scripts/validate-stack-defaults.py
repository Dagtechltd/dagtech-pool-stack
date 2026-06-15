#!/usr/bin/env python3
"""Validate projections of stack-owned deployment defaults."""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from pathlib import Path


ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise ValueError(f"{path}:{lineno}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not ENV_KEY_RE.match(key):
            raise ValueError(f"{path}:{lineno}: invalid env key {key!r}")
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        if key in values:
            raise ValueError(f"{path}:{lineno}: duplicate default for {key}")
        values[key] = value
    return values


def env_assignments(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in re.finditer(r"(?m)^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", text):
        key = match.group(1)
        value = match.group(2).strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        found[key] = value
    return found


def assert_projection_matches(
    *,
    errors: list[str],
    defaults: dict[str, str],
    root: Path,
    rel_path: str,
) -> None:
    path = root / rel_path
    if not path.exists():
        errors.append(f"missing projection file: {rel_path}")
        return
    assignments = env_assignments(path.read_text(encoding="utf-8"))
    for key, expected in defaults.items():
        if key not in assignments:
            continue
        actual = assignments[key]
        if actual != expected:
            errors.append(f"{rel_path}: {key}={actual!r}, expected {expected!r}")


def assert_compose_fallbacks_match(errors: list[str], defaults: dict[str, str], root: Path) -> None:
    rel_path = "docker-compose.yml"
    text = (root / rel_path).read_text(encoding="utf-8")
    for key, expected in defaults.items():
        for match in re.finditer(r"\$\{" + re.escape(key) + r":-([^}]*)\}", text):
            actual = match.group(1)
            if actual != expected:
                errors.append(f"{rel_path}: ${{{key}:-{actual}}}, expected fallback {expected!r}")


def assert_shell_defaults_match(
    *,
    errors: list[str],
    defaults: dict[str, str],
    root: Path,
    rel_path: str,
) -> None:
    path = root / rel_path
    text = path.read_text(encoding="utf-8")
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "set_env_value" not in stripped and "ensure_env_value" not in stripped and "ensure_stack_default_env_value" not in stripped:
            continue
        try:
            parts = shlex.split(stripped, comments=False, posix=True)
        except ValueError:
            continue
        if not parts:
            continue
        command = parts[0]
        if command in {"set_env_value", "ensure_env_value"}:
            if (command == "set_env_value" and len(parts) < 4) or (
                command == "ensure_env_value" and len(parts) < 3
            ):
                continue
            key = parts[2] if command == "set_env_value" else parts[1]
            value = parts[3] if command == "set_env_value" else parts[2]
        elif command == "ensure_stack_default_env_value":
            if len(parts) < 2:
                continue
            key = parts[1]
            value = parts[2] if len(parts) > 2 else defaults.get(key, "")
        else:
            continue
        if key not in defaults:
            continue
        if value.startswith("$") or "$(" in value or "${" in value:
            continue
        expected = defaults[key]
        if value != expected:
            errors.append(f"{rel_path}:{lineno}: {key} default {value!r}, expected {expected!r}")


def assert_required_hooks(errors: list[str], root: Path) -> None:
    hooks = {
        "ops/install-dashboard.sh": (
            "BDAG_STACK_DEFAULTS_FILE",
            "append_stack_defaults_to_env_file",
            "ensure_stack_default_env_value",
        ),
        "ops/release-install.sh": (
            "BDAG_STACK_DEFAULTS_FILE",
            "stack_default",
        ),
        "ops/install-p2p-services.sh": ("stack-defaults.env",),
        "ops/pool_ops.py": ("stack-defaults.env",),
        "ops/maintain-rawdatadir-sidecar.sh": ("stack-defaults.env",),
    }
    for rel_path, required in hooks.items():
        path = root / rel_path
        if not path.exists():
            errors.append(f"missing hook target: {rel_path}")
            continue
        text = path.read_text(encoding="utf-8")
        for needle in required:
            if needle not in text:
                errors.append(f"{rel_path}: missing stack-defaults hook {needle!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".", help="stack repository root")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    defaults_path = root / "ops" / "config" / "stack-defaults.env"
    errors: list[str] = []
    if not defaults_path.exists():
        errors.append("missing ops/config/stack-defaults.env")
        defaults: dict[str, str] = {}
    else:
        try:
            defaults = parse_env_file(defaults_path)
        except ValueError as exc:
            errors.append(str(exc))
            defaults = {}

    for rel_path in (".env.example", ".env.cpu.example", "ops/portable.env.example"):
        assert_projection_matches(errors=errors, defaults=defaults, root=root, rel_path=rel_path)
    assert_compose_fallbacks_match(errors, defaults, root)
    for rel_path in ("ops/install-dashboard.sh", "ops/release-install.sh"):
        assert_shell_defaults_match(errors=errors, defaults=defaults, root=root, rel_path=rel_path)
    assert_required_hooks(errors, root)

    if errors:
        for error in errors:
            print(f"stack-defaults validation failed: {error}", file=sys.stderr)
        return 1
    print(f"stack defaults validated: {len(defaults)} canonical values")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

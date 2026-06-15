#!/usr/bin/env python3
"""Smoke-test the release installer entrypoints in an isolated temp payload root."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RELEASE_DIR = ROOT / "scripts" / "release"
INSTALLERS_DIR = RELEASE_DIR / "installers"


def host_arch() -> str:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64", "x64"}:
        return "amd64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    raise ValueError(f"unsupported CPU architecture: {platform.machine()}")


def payload_target() -> str:
    return f"linux-{host_arch()}"


def copy_payload_root(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / ".env.example", dest / ".env.example")
    shutil.copy2(RELEASE_DIR / "install.sh", dest / "install.sh")
    shutil.copy2(RELEASE_DIR / "install.ps1", dest / "install.ps1")
    shutil.copy2(RELEASE_DIR / "install.cmd", dest / "install.cmd")
    shutil.copytree(INSTALLERS_DIR, dest / "installers", dirs_exist_ok=True)
    (dest / "release-payload.env").write_text(
        "\n".join(
            [
                f"BDAG_RELEASE_PAYLOAD_TARGET={payload_target()}",
                f"BDAG_RELEASE_PAYLOAD_ARCH={host_arch()}",
                f"DOCKER_PLATFORM=linux/{host_arch()}",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )


def expected_docker_platform(package_root: Path) -> str:
    """The installer writes the payload's platform, not the host's, so a
    cross-built payload (e.g. arm64 packaged on an amd64 runner) must be
    checked against release-payload.env."""
    metadata_path = package_root / "release-payload.env"
    metadata: dict[str, str] = {}
    if metadata_path.exists():
        for line in metadata_path.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            metadata[key.strip()] = value.strip()
    if metadata.get("DOCKER_PLATFORM"):
        return metadata["DOCKER_PLATFORM"]
    if metadata.get("BDAG_RELEASE_PAYLOAD_ARCH"):
        return f"linux/{metadata['BDAG_RELEASE_PAYLOAD_ARCH']}"
    return f"linux/{host_arch()}"


def run_command(args: list[str], cwd: Path, env: dict[str, str]) -> dict[str, Any]:
    result = subprocess.run(
        args,
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return {
        "args": args,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", type=Path, help="existing payload root to smoke-test")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a short summary")
    args = parser.parse_args(argv)

    cleanup = None
    package_root = args.package_root
    if package_root is None:
        cleanup = tempfile.TemporaryDirectory(prefix="stack-release-smoke-")
        package_root = Path(cleanup.name)
        copy_payload_root(package_root)

    env = os.environ.copy()
    env["BDAG_INSTALL_TEST_WRITE_ENV_ONLY"] = "1"
    env["BDAG_NO_PAUSE"] = "1"

    if os.name == "nt":
        result = run_command(["cmd", "/c", "install.cmd"], package_root, env)
    else:
        result = run_command(["bash", "install.sh"], package_root, env)

    env_path = package_root / ".env"
    env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    expected_platform = f"DOCKER_PLATFORM={expected_docker_platform(package_root)}"
    ok = result["returncode"] == 0 and expected_platform in env_text
    payload: dict[str, Any] = {
        "package_root": str(package_root),
        "host_arch": host_arch(),
        "expected_platform": expected_platform,
        "env_written": env_path.exists(),
        "env_contains_expected_platform": expected_platform in env_text,
        "result": result,
        "ok": ok,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"release smoke: {'ok' if ok else 'failed'} "
            f"platform={expected_platform} rc={result['returncode']}"
        )
    if cleanup is not None:
        cleanup.cleanup()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

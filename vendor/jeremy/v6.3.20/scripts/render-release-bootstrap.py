#!/usr/bin/env python3
"""Render pinned release bootstrap installers.

The generated scripts intentionally pin a single GitHub release tag. They only
select between runtime-architecture payload zips attached to that same tag.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import stat


ARCH_ALIASES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "arm64": "arm64",
    "aarch64": "arm64",
}

OS_ALIASES = {
    "linux": "linux",
    "darwin": "macos",
    "macos": "macos",
    "windows": "windows",
    "win32nt": "windows",
}


def normalize_arch(arch: str) -> str:
    key = arch.strip().lower()
    if key not in ARCH_ALIASES:
        raise ValueError(f"unsupported CPU architecture: {arch}")
    return ARCH_ALIASES[key]


def normalize_os(os_name: str) -> str:
    key = os_name.strip().lower()
    if key.startswith(("mingw", "msys", "cygwin")):
        return "windows"
    if key not in OS_ALIASES:
        raise ValueError(f"unsupported operating system: {os_name}")
    return OS_ALIASES[key]


def select_payload_target(os_name: str, arch: str) -> str:
    normalized_os = normalize_os(os_name)
    normalized_arch = normalize_arch(arch)
    if normalized_os not in {"linux", "macos", "windows"}:
        raise ValueError(f"unsupported operating system: {os_name}")
    return f"linux-{normalized_arch}"


def render_shell(version: str, repository: str, package_name: str) -> str:
    return f"""#!/usr/bin/env sh
set -eu

VERSION='{version}'
REPOSITORY='{repository}'
PACKAGE_NAME='{package_name}'
DOWNLOAD_BASE='https://github.com/'"$REPOSITORY"'/releases/download/'"$VERSION"

OS_NAME=$(uname -s 2>/dev/null || echo unknown)
ARCH_NAME=$(uname -m 2>/dev/null || echo unknown)

case "$OS_NAME" in
  Linux) ;;
  Darwin)
    echo "macOS is not supported in this release yet. Only Linux is currently supported." >&2
    exit 1
    ;;
  MINGW*|MSYS*|CYGWIN*)
    echo "Windows is not supported in this release yet. Only Linux is currently supported." >&2
    exit 1
    ;;
  *)
    echo "Unsupported operating system: $OS_NAME" >&2
    exit 1
    ;;
esac

case "$ARCH_NAME" in
  x86_64|amd64) PAYLOAD_TARGET='linux-amd64' ;;
  arm64|aarch64) PAYLOAD_TARGET='linux-arm64' ;;
  *)
    echo "Unsupported CPU architecture: $ARCH_NAME" >&2
    exit 1
    ;;
esac

ASSET="$PACKAGE_NAME-$VERSION-$PAYLOAD_TARGET.zip"
ROOT="$PACKAGE_NAME-$VERSION-$PAYLOAD_TARGET"
URL="$DOWNLOAD_BASE/$ASSET"
INSTALL_DIR="${{BDAG_INSTALL_DIR:-$ROOT}}"
ZIP_PATH="$ASSET"

if [ "$INSTALL_DIR" != "$ROOT" ]; then
  echo "BDAG_INSTALL_DIR is not supported by this pinned bootstrap; remove it and re-run." >&2
  exit 1
fi

if [ -e "$ROOT" ]; then
  echo "Refusing to overwrite existing directory: $ROOT" >&2
  exit 1
fi

require_command() {{
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command missing: $1" >&2
    exit 1
  fi
}}

print_docker_install_instructions() {{
  cat >&2 <<'DOCKER_INSTRUCTIONS'

Install Docker Engine first, then re-run this installer.

Quick install (most Linux distros):

  curl -fsSL https://get.docker.com | sh

Then enable the daemon and let your user run docker without sudo:

  sudo systemctl enable --now docker
  sudo usermod -aG docker "$USER"
  newgrp docker   # or log out and back in

Verify everything works:

  docker run --rm hello-world
  docker compose version

Notes:
  - Avoid your distro's docker.io package; it is often outdated.
  - Membership in the docker group is root-equivalent on this host. On a
    multi-admin box, skip the usermod step and run the installer with a
    user that can sudo docker instead.
DOCKER_INSTRUCTIONS
}}

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: Docker is not installed." >&2
  print_docker_install_instructions
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Error: Docker is installed but the Docker Compose v2 plugin is missing." >&2
  echo "Install/update Docker Engine (includes docker-compose-plugin):" >&2
  print_docker_install_instructions
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Error: Docker is installed but this user cannot reach the Docker daemon." >&2
  cat >&2 <<'DOCKER_ACCESS'

Fix daemon access, then re-run this installer:

  sudo systemctl enable --now docker     # make sure the daemon is running
  sudo usermod -aG docker "$USER"        # allow docker without sudo
  newgrp docker                          # or log out and back in
DOCKER_ACCESS
  exit 1
fi

require_command curl
require_command unzip

echo "Downloading $ASSET"
rm -f "$ZIP_PATH" "$ZIP_PATH.part"
curl --fail --location --show-error --progress-bar -o "$ZIP_PATH.part" "$URL"
mv "$ZIP_PATH.part" "$ZIP_PATH"

echo "Extracting $ASSET"
unzip -q "$ZIP_PATH"
rm -f "$ZIP_PATH"

if [ ! -f "$ROOT/install.sh" ]; then
  echo "Payload did not contain expected installer: $ROOT/install.sh" >&2
  exit 1
fi

chmod +x "$ROOT/install.sh" "$ROOT/installers/"*.sh 2>/dev/null || true
exec sh "$ROOT/install.sh" "$@"
"""


def render_powershell(version: str, repository: str, package_name: str) -> str:
    return f"""#Requires -Version 5.1
$ErrorActionPreference = 'Stop'

$Version = '{version}'
$Repository = '{repository}'
$PackageName = '{package_name}'
$DownloadBase = "https://github.com/$Repository/releases/download/$Version"

$platform = [System.Environment]::OSVersion.Platform.ToString()
if ($platform -notlike 'Win*') {{
    throw "This bootstrap is for Windows. On Linux or macOS, run install.sh from the same release."
}}

switch ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()) {{
    'X64'   {{ $PayloadTarget = 'linux-amd64' }}
    'Arm64' {{ $PayloadTarget = 'linux-arm64' }}
    default {{ throw "Unsupported CPU architecture: $([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture)" }}
}}

$Asset = "$PackageName-$Version-$PayloadTarget.zip"
$Root = "$PackageName-$Version-$PayloadTarget"
$Url = "$DownloadBase/$Asset"
$ZipPath = Join-Path (Get-Location) $Asset

if ($env:BDAG_INSTALL_DIR) {{
    throw "BDAG_INSTALL_DIR is not supported by this pinned bootstrap; remove it and re-run."
}}
if (Test-Path $Root) {{
    throw "Refusing to overwrite existing directory: $Root"
}}

Write-Host "Downloading $Asset"
Remove-Item -Path $ZipPath, "$ZipPath.part" -ErrorAction SilentlyContinue
Invoke-WebRequest -Uri $Url -OutFile "$ZipPath.part" -UseBasicParsing
Move-Item -Path "$ZipPath.part" -Destination $ZipPath -Force

Write-Host "Extracting $Asset"
Expand-Archive -Path $ZipPath -DestinationPath (Get-Location) -Force
Remove-Item -Path $ZipPath -ErrorAction SilentlyContinue

$Installer = Join-Path $Root 'install.ps1'
if (-not (Test-Path $Installer)) {{
    throw "Payload did not contain expected installer: $Installer"
}}

& $Installer @args
exit $LASTEXITCODE
"""


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--package-name", default="pool-stack-docker")
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_executable(
        args.out_dir / "install.sh",
        render_shell(args.version, args.repository, args.package_name),
    )
    (args.out_dir / "install.ps1").write_text(
        render_powershell(args.version, args.repository, args.package_name),
        encoding="utf-8",
        newline="\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Provision a local signing identity for the BlockDAG IPFS segment writer."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("BDAG_PROJECT_ROOT") or Path(__file__).resolve().parents[1]).resolve()
ENV_FILE = Path(os.environ.get("BDAG_ENV_FILE") or ROOT / ".env")
OPS_DIR = ROOT / "ops"
sys.path.insert(0, str(OPS_DIR))
import ipfs_segment_trust  # type: ignore  # noqa: E402


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def atomic_write_env(path: Path, values: dict[str, str]) -> None:
    existing = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []
    remaining = []
    keys = set(values)
    for line in existing:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in keys:
            continue
        remaining.append(line)
    for key in sorted(values):
        remaining.append(f"{key}={values[key]}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(remaining).rstrip() + "\n", encoding="utf-8")
    tmp.replace(path)


def resolve_key_path(env: dict[str, str], env_file: Path, override: str = "") -> Path:
    configured = override or env.get("BDAG_IPFS_SEGMENT_SIGNING_KEY_FILE") or "./ops/runtime/ipfs-content/segment-writer.key"
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = env_file.parent / path
    return path.resolve()


def read_key_seed(path: Path) -> str:
    if not path.exists():
        return ""
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            if key.strip() in {"BDAG_IPFS_SEGMENT_SIGNING_KEY_HEX", "SIGNING_KEY_HEX"}:
                return value.strip().strip('"').strip("'")
            continue
        return line
    return ""


def write_key_seed(path: Path, seed_hex: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("# BlockDAG IPFS segment writer Ed25519 seed. Keep private.\n")
            handle.write(f"BDAG_IPFS_SEGMENT_SIGNING_KEY_HEX={seed_hex}\n")
    finally:
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass


def generate_seed_hex() -> str:
    return os.urandom(32).hex()


def default_writer_id(public_hex: str) -> str:
    return f"bdag-writer-{public_hex[:16]}"


def append_signer(existing: str, writer_id: str, public_hex: str) -> str:
    mapping = ipfs_segment_trust.trusted_signers({"BDAG_IPFS_SEGMENT_TRUSTED_SIGNERS": existing})
    mapping[writer_id] = public_hex
    return ",".join(f"{key}={mapping[key]}" for key in sorted(mapping))


def append_roster(existing: str, writer_id: str) -> str:
    writers = []
    seen: set[str] = set()
    for raw in existing.replace("\n", ",").split(","):
        writer = raw.strip()
        if not writer:
            continue
        writer = writer.split("=", 1)[0].strip()
        if writer and writer not in seen:
            seen.add(writer)
            writers.append(writer)
    if writer_id and writer_id not in seen:
        writers.append(writer_id)
    return ",".join(sorted(writers))


def ensure_identity(env_file: Path, key_file_override: str = "", write_env: bool = True) -> dict[str, Any]:
    env = load_env(env_file)
    key_path = resolve_key_path(env, env_file, key_file_override)
    seed_hex = read_key_seed(key_path)
    created_key = False
    if not seed_hex:
        seed_hex = generate_seed_hex()
        write_key_seed(key_path, seed_hex)
        created_key = True
    private_key = ipfs_segment_trust.load_private_key(seed_hex)
    public_hex = ipfs_segment_trust.public_key_hex(private_key)
    writer_id = env.get("BDAG_IPFS_SEGMENT_WRITER_ID") or env.get("BDAG_IPFS_WRITER_ID") or default_writer_id(public_hex)
    trusted = append_signer(env.get("BDAG_IPFS_SEGMENT_TRUSTED_SIGNERS", ""), writer_id, public_hex)
    rawdatadir_trusted = append_signer(env.get("BDAG_RAWDATADIR_TRUSTED_SIGNERS", ""), writer_id, public_hex)
    roster = append_roster(env.get("BDAG_IPFS_SEGMENT_WRITER_ROSTER", ""), writer_id)
    relative_key = os.path.relpath(key_path, env_file.parent)
    relative_key_value = f"./{relative_key}" if not relative_key.startswith(".") else relative_key
    updates = {
        "BDAG_IPFS_SEGMENT_WRITER_ID": writer_id,
        "BDAG_IPFS_SEGMENT_WRITER_ROSTER": roster,
        "BDAG_IPFS_SEGMENT_SIGNING_KEY_FILE": relative_key_value,
        "BDAG_IPFS_SEGMENT_TRUSTED_SIGNERS": trusted,
        "BDAG_IPFS_SEGMENT_REQUIRE_SIGNATURES": env.get("BDAG_IPFS_SEGMENT_REQUIRE_SIGNATURES") or "1",
        "BDAG_IPFS_RESTORE_REQUIRE_SIGNATURES": env.get("BDAG_IPFS_RESTORE_REQUIRE_SIGNATURES") or "1",
        "BDAG_RAWDATADIR_SIGNING_KEY_FILE": env.get("BDAG_RAWDATADIR_SIGNING_KEY_FILE") or relative_key_value,
        "BDAG_RAWDATADIR_SIGNING_KEY_ID": env.get("BDAG_RAWDATADIR_SIGNING_KEY_ID") or writer_id,
        "BDAG_RAWDATADIR_TRUSTED_SIGNERS": rawdatadir_trusted,
        "BDAG_RAWDATADIR_REQUIRE_TRUSTED_SIGNER": env.get("BDAG_RAWDATADIR_REQUIRE_TRUSTED_SIGNER") or "1",
    }
    if write_env:
        atomic_write_env(env_file, updates)
    return {
        "env_file": str(env_file),
        "key_file": str(key_path),
        "created_key": created_key,
        "writer_id": writer_id,
        "public_key_hex": public_hex,
        "trusted_signers": trusted,
        "rawdatadir_trusted_signers": rawdatadir_trusted,
        "writer_roster": roster,
        "env_updates": updates,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=str(ENV_FILE), help="stack .env path to update")
    parser.add_argument("--key-file", default="", help="override signing key path")
    parser.add_argument("--no-write-env", action="store_true", help="derive/provision the key without writing .env")
    parser.add_argument("--json", action="store_true", help="print identity metadata as JSON")
    args = parser.parse_args(argv)
    payload = ensure_identity(
        Path(args.env_file).expanduser().resolve(),
        key_file_override=args.key_file,
        write_env=not args.no_write_env,
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"writer_id={payload['writer_id']}")
        print(f"public_key_hex={payload['public_key_hex']}")
        print(f"key_file={payload['key_file']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

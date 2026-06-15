#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("BDAG_PROJECT_ROOT", Path(__file__).resolve().parents[1])).resolve()
ENV_FILE = Path(os.environ.get("BDAG_ENV_FILE", ROOT / ".env"))
STACK_DEFAULTS_FILE = Path(os.environ.get("BDAG_STACK_DEFAULTS_FILE", ROOT / "ops" / "config" / "stack-defaults.env"))
STATUS_FILE = Path(
    os.environ.get(
        "BDAG_RAWDATADIR_SIDECAR_SAFETY_STATUS",
        ROOT / "ops" / "runtime" / "rawdatadir-sidecar-safety-status.json",
    )
)
UNSAFE_FSTYPES = {
    "vfat",
    "exfat",
    "ntfs",
    "ntfs3",
    "fuseblk",
    "fuse",
    "nfs",
    "nfs4",
    "cifs",
    "smb3",
    "tmpfs",
    "ramfs",
}
PUBLIC_EVM_RPC_DEFAULTS = [
    ("bdagscan-rpc", "https://rpc.bdagscan.com"),
    ("blockdag-engineering-rpc", "https://rpc.blockdag.engineering"),
]
LOW_IO_USB_STORAGE_PROFILES = {
    "single-usb-constrained",
    "usb-chain-internal-runtime",
}


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for path in (STACK_DEFAULTS_FILE, ENV_FILE):
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            env[key] = value
    env.update(os.environ)
    return env


def split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]


def as_int(value: str | None, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def as_float(value: str | None, default: float) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def bool_mode(value: str | None) -> bool | None:
    if value is None or str(value).strip() == "":
        return None
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return None


def resolve_path(raw: str | Path) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def named_urls(raw: str, defaults: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    for index, part in enumerate(split_csv(raw), start=1):
        if "=" in part:
            name, url = part.split("=", 1)
        else:
            name, url = f"rpc-{index}", part
        name = name.strip() or f"rpc-{index}"
        url = url.strip()
        if url.startswith(("http://", "https://")):
            values.append((name, url))
    if values:
        return values
    return list(defaults or [])


def evm_reference_urls(env: dict[str, str]) -> list[tuple[str, str]]:
    for key in ("BDAG_RAWDATADIR_EVM_REFERENCE_RPC_URLS", "BDAG_EVM_REFERENCE_RPC_URLS", "BDAG_PUBLIC_RPC_URLS"):
        urls = named_urls(env.get(key, ""))
        if urls:
            return urls
    return list(PUBLIC_EVM_RPC_DEFAULTS)


def json_rpc_quantity(url: str, method: str, timeout: float = 5.0) -> int:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": []}).encode()
    request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        decoded = json.loads(response.read().decode())
    if decoded.get("error"):
        raise RuntimeError(decoded["error"])
    value = decoded.get("result")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 16) if value.lower().startswith("0x") else int(value)
    raise ValueError(f"{method} returned no quantity")


def docker_container_ip(service: str) -> str:
    if not service or not shutil.which("docker"):
        return ""
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}", service],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""


def local_evm_rpc_candidates(env: dict[str, str], local_url: str) -> list[str]:
    candidates = [local_url]
    parsed = urllib.parse.urlsplit(local_url)
    if parsed.scheme not in {"http", "https"}:
        return candidates
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return candidates
    container_ip = docker_container_ip(active_node_service(env))
    if not container_ip:
        return candidates
    netloc = container_ip
    if parsed.port:
        netloc = f"{container_ip}:{parsed.port}"
    fallback = urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, parsed.fragment))
    if fallback not in candidates:
        candidates.append(fallback)
    return candidates


def source_evm_sync_sample(env: dict[str, str]) -> dict[str, Any]:
    local_url = env.get("BDAG_RAWDATADIR_EVM_RPC_URL") or env.get("BDAG_EVM_RPC_URL") or "http://127.0.0.1:18545"
    max_lag = as_int(env.get("BDAG_RAWDATADIR_MAX_EVM_REFERENCE_LAG") or env.get("BDAG_AUTOPUBLISH_EVM_LAG_LIMIT"), 1000)
    timeout = as_float(env.get("BDAG_RAWDATADIR_EVM_REFERENCE_TIMEOUT_SECONDS"), 5.0)
    payload: dict[str, Any] = {
        "local_evm_rpc_url": local_url,
        "local_evm_rpc_candidates": [],
        "local_evm_block": None,
        "reference_source": "",
        "reference_url": "",
        "reference_evm_block": None,
        "lag_to_reference": None,
        "max_lag": max_lag,
        "fresh": False,
        "errors": [],
    }
    for candidate_url in local_evm_rpc_candidates(env, local_url):
        payload["local_evm_rpc_candidates"].append(candidate_url)
        try:
            local_block = json_rpc_quantity(candidate_url, "eth_blockNumber", timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - try each local candidate.
            payload["errors"].append(f"local-evm:{candidate_url}: {exc}")
            continue
        payload["local_evm_rpc_url"] = candidate_url
        payload["local_evm_block"] = local_block
        break
    else:
        return payload

    best: tuple[str, str, int] | None = None
    for source, url in evm_reference_urls(env):
        if url == local_url:
            continue
        try:
            block = json_rpc_quantity(url, "eth_blockNumber", timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - try every configured reference.
            payload["errors"].append(f"{source}: {exc}")
            continue
        if best is None or block > best[2]:
            best = (source, url, block)

    if best is None:
        payload["errors"].append("no usable external EVM reference RPC")
        return payload
    payload["reference_source"] = best[0]
    payload["reference_url"] = best[1]
    payload["reference_evm_block"] = best[2]
    lag = max(0, best[2] - local_block)
    payload["lag_to_reference"] = lag
    payload["fresh"] = lag <= max_lag
    return payload


def nearest_existing(path: Path) -> Path:
    cur = path
    while not cur.exists() and cur != cur.parent:
        cur = cur.parent
    return cur


def run_json(args: list[str]) -> Any | None:
    try:
        result = subprocess.run(args, check=True, capture_output=True, text=True, timeout=3)
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def mount_info(path: Path) -> dict[str, Any]:
    probe = nearest_existing(path)
    payload = run_json(["findmnt", "-J", "-T", str(probe), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"])
    filesystems = payload.get("filesystems") if isinstance(payload, dict) else None
    if isinstance(filesystems, list) and filesystems:
        return dict(filesystems[0])
    return {"target": "", "source": "", "fstype": "", "options": ""}


def disk_name_for_source(source: str) -> str:
    if not source.startswith("/dev/"):
        return ""
    try:
        result = subprocess.run(
            ["lsblk", "-no", "PKNAME", source],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        parent = result.stdout.strip().splitlines()
        if parent and parent[0].strip():
            return parent[0].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(source).name


def block_device_facts(source: str) -> dict[str, Any]:
    disk = disk_name_for_source(source)
    facts: dict[str, Any] = {"disk": disk, "transport": "", "removable": None, "hotplug": None, "rotational": None}
    if not disk:
        return facts
    try:
        result = subprocess.run(
            ["lsblk", "-dnJ", "-o", "NAME,TRAN,RM,HOTPLUG,ROTA", f"/dev/{disk}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        payload = json.loads(result.stdout or "{}")
        devices = payload.get("blockdevices") or []
        if devices:
            dev = devices[0]
            facts["transport"] = str(dev.get("tran") or "")
            facts["removable"] = bool(dev.get("rm")) if dev.get("rm") is not None else None
            facts["hotplug"] = bool(dev.get("hotplug")) if dev.get("hotplug") is not None else None
            facts["rotational"] = bool(dev.get("rota")) if dev.get("rota") is not None else None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        pass
    removable_path = Path("/sys/class/block") / disk / "removable"
    if removable_path.exists():
        try:
            facts["removable"] = removable_path.read_text(encoding="utf-8").strip() == "1"
        except OSError:
            pass
    try:
        device_realpath = str((Path("/sys/class/block") / disk / "device").resolve())
        facts["sysfs_device"] = device_realpath
        if "usb" in device_realpath.lower():
            facts["transport"] = facts["transport"] or "usb"
    except OSError:
        pass
    return facts


def classify_path(name: str, path: Path) -> dict[str, Any]:
    mount = mount_info(path)
    source = str(mount.get("source") or "")
    fstype = str(mount.get("fstype") or "")
    facts = block_device_facts(source)
    path_text = str(path)
    transport = str(facts.get("transport") or "").lower()
    reasons: list[str] = []
    if transport == "usb" or facts.get("removable") or facts.get("hotplug"):
        reasons.append("usb_or_removable")
    if fstype.lower() in UNSAFE_FSTYPES or fstype.lower().startswith("fuse."):
        reasons.append(f"unsafe_fstype:{fstype}")
    if path_text.startswith(("/media/", "/run/media/")):
        reasons.append("removable_mount_path")
    if source and not source.startswith("/dev/"):
        reasons.append(f"non_block_source:{source}")
    return {
        "name": name,
        "path": str(path),
        "mount": mount,
        "device": facts,
        "unsafe": bool(reasons),
        "unsafe_reasons": reasons,
    }


def dir_size_bytes(path: Path) -> int | None:
    commands = [["du", "-sb", str(path)]]
    if os.name == "posix" and os.geteuid() != 0 and shutil.which("sudo"):
        commands.append(["sudo", "-n", "du", "-sb", str(path)])
    for command in commands:
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            return int(result.stdout.split()[0])
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            continue
    return None


def total_memory_bytes() -> int | None:
    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return None
    for line in meminfo.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2:
                return int(parts[1]) * 1024
    return None


def active_node_service(env: dict[str, str]) -> str:
    services = split_csv(env.get("BDAG_NODE_SERVICE", "node"))
    return services[0] if services else "node"


def env_path(env: dict[str, str], key: str, default: str | Path) -> Path:
    value = env.get(key)
    return resolve_path(value if value else default)


def node_data_dir(env: dict[str, str], service: str) -> Path:
    if service == "node" or service.endswith("node-1"):
        return env_path(env, "BDAG_NODE_DATA_DIR", env.get("BDAG_DATA_DIR") or "./data/node")
    return env_path(env, "BDAG_NODE_DATA_DIR", env.get("BDAG_DATA_DIR") or "./data/node")


def build_payload(full: bool) -> dict[str, Any]:
    env = load_env()
    requested_network = (env.get("BDAG_RAWDATADIR_NETWORK") or "mainnet").strip().lower()
    network = "mainnet"
    service = active_node_service(env)
    data_dir = node_data_dir(env, service)
    source_dir = env_path(env, "BDAG_RAWDATADIR_SIDECAR_SOURCE", data_dir / network)
    sidecar_dir = env_path(
        env, "BDAG_RAWDATADIR_SIDECAR_DIR", ROOT / "data-restore" / "btrfs-checkpoints" / "rawdatadir-sidecar" / network
    )
    artifact_base = env_path(env, "BDAG_RAWDATADIR_ARTIFACT_BASE", ROOT / "data-restore" / "btrfs-checkpoints" / "rawdatadir-artifacts")
    tmp_dir = env_path(env, "BDAG_RAWDATADIR_TMPDIR", artifact_base / "tmp")
    mode = (env.get("BDAG_RAWDATADIR_SIDECAR_MODE") or "auto").strip().lower()
    storage_profile = (env.get("BDAG_STORAGE_PROFILE") or "").strip().lower()
    network_topology = (env.get("BDAG_DETECTED_NETWORK_TOPOLOGY") or env.get("BDAG_NETWORK_TOPOLOGY") or "").strip().lower()

    paths = [
        classify_path("active_node_datadir", data_dir),
        classify_path("source_datadir", source_dir),
        classify_path("sidecar_dir", sidecar_dir),
        classify_path("artifact_base", artifact_base),
        classify_path("tmp_dir", tmp_dir),
        classify_path("docker_root", Path("/var/lib/docker")),
    ]
    reasons: list[str] = []
    if requested_network != "mainnet":
        reasons.append(f"non-mainnet raw datadir network is unsupported:{requested_network}")
    if bool_mode(mode) is False:
        reasons.append("sidecar_mode_disabled")
    if storage_profile in LOW_IO_USB_STORAGE_PROFILES:
        reasons.append(f"storage_profile_usb_low_io:{storage_profile}")
    for item in paths:
        if item["unsafe"]:
            reasons.append(f"{item['name']}:{','.join(item['unsafe_reasons'])}")

    min_ram_gib = as_float(env.get("BDAG_RAWDATADIR_MIN_RAM_GIB"), 8.0)
    memory = total_memory_bytes()
    if memory is not None and memory < min_ram_gib * 1024**3:
        reasons.append(f"insufficient_ram:{memory / 1024**3:.1f}GiB<{min_ram_gib:.1f}GiB")
    min_cpu = as_int(env.get("BDAG_RAWDATADIR_MIN_CPU_COUNT"), 4)
    cpu_count = os.cpu_count() or 1
    if cpu_count < min_cpu:
        reasons.append(f"insufficient_cpu:{cpu_count}<{min_cpu}")

    usage = shutil.disk_usage(nearest_existing(artifact_base))
    source_size = dir_size_bytes(source_dir) if full else None
    min_free_gib = as_float(env.get("BDAG_RAWDATADIR_MIN_FREE_GIB"), 100.0)
    multiplier = as_float(env.get("BDAG_RAWDATADIR_FREE_SPACE_MULTIPLIER"), 2.5)
    required_free = int(min_free_gib * 1024**3)
    if source_size is not None:
        required_free = max(required_free, int(source_size * multiplier))
    elif full:
        reasons.append("source_size_unavailable")
    if usage.free < required_free:
        reasons.append(
            f"insufficient_disk:{usage.free / 1024**3:.1f}GiB<{required_free / 1024**3:.1f}GiB"
        )

    evm_sync = source_evm_sync_sample(env)
    if as_bool(env.get("BDAG_RAWDATADIR_REQUIRE_EVM_REFERENCE_FRESH"), True):
        if evm_sync["local_evm_block"] is None:
            reasons.append("local_evm_unavailable")
        elif evm_sync["reference_evm_block"] is None:
            reasons.append("evm_reference_unavailable")
        elif not evm_sync["fresh"]:
            reasons.append(f"evm_lag_to_reference:{evm_sync['lag_to_reference']}>{evm_sync['max_lag']}")

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "project_root": str(ROOT),
        "mode": mode,
        "storage_profile": storage_profile,
        "network_topology": network_topology,
        "active_node_service": service,
        "network": network,
        "safe": not reasons,
        "reasons": reasons,
        "paths": paths,
        "evm_sync": evm_sync,
        "source_size_bytes": source_size,
        "artifact_free_bytes": usage.free,
        "artifact_required_free_bytes": required_free,
        "cpu_count": cpu_count,
        "memory_bytes": memory,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--full", action="store_true", help="include a du -sb source-size check")
    parser.add_argument("--status-file", default=str(STATUS_FILE))
    args = parser.parse_args()

    payload = build_payload(full=args.full)
    status_file = Path(args.status_file)
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["safe"] else 2


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Discover local BlockDAG node peers and update node-specific addpeer lists."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"
RUNTIME_DIR = Path(os.environ.get("BDAG_RUNTIME_DIR", PROJECT_ROOT / "ops" / "runtime"))
RUNTIME_ENV_FILE = RUNTIME_DIR / "ops.env"
SYNC_COORDINATOR_STATE_FILE = RUNTIME_DIR / "sync-coordinator-state.json"
DEFERRED_APPLY_FILE = RUNTIME_DIR / "local-peers-deferred-apply"
CHAIN_PEERSTORE_CANDIDATES_FILE = RUNTIME_DIR / "chain-peerstore-candidates.txt"
LIVE_PEERS_FILE = RUNTIME_DIR / "live-peers-current.txt"
PEER_DISCOVERY_FILE = RUNTIME_DIR / "peer-discovery-current.json"
DEFAULT_ACTIVE_NODE_SERVICE = "node"
NODE_SPECS = {
    "node": {"port": 8151, "env": "BDAG_NODE_PEER_ADDRESSES"},
}
NODE_PEER_ID_ENV = {
    "node": ("BDAG_LOCAL_NODE_PEER_ID", "BDAG_NODE_PEER_ID"),
}
PEER_RE = re.compile(r"Node started p2p server.*?/p2p/([A-Za-z0-9]+)")
ADDR_RE = re.compile(r"/ip4/[^,\s]+/tcp/(\d+)/p2p/([A-Za-z0-9]+)")
PEER_RE_FULL = re.compile(r"/(?:ip4|ip6|dns|dns4|dns6)/([^/]+)/tcp/(\d+)/p2p/([^,\s]+)")
PEERSTORE_LOG_RE = re.compile(r"Try to connect from peer store:\{([^:]+): \[([^\]]*)\]}")
PEER_LATENCY_TIMEOUT = float(os.environ.get("BDAG_LOCAL_PEER_LATENCY_TIMEOUT", "0.75"))
PEER_LATENCY_WORKERS = int(os.environ.get("BDAG_LOCAL_PEER_LATENCY_WORKERS", "16"))
COLLECTOR_STATUS_URL = os.environ.get("BDAG_COLLECTOR_STATUS_URL", "http://127.0.0.1:9280/api/status")
CHAIN_PEERSTORE_LOG_TAIL = os.environ.get("BDAG_CHAIN_PEERSTORE_LOG_TAIL", "8000")
DASHBOARD_STATUS_URL = os.environ.get("BDAG_DASHBOARD_STATUS_URL", "http://127.0.0.1:8088/api/status")
ACTIVE_MINING_RECENT_SECONDS = int(os.environ.get("BDAG_LOCAL_PEERS_ACTIVE_MINING_RECENT_SECONDS", "300"))
DEFAULT_ASIC_LAN_CIDRS = ""
TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
AUTO_VALUES = {"", "auto", "detect"}
DOCKER_ACCESS_ERROR_MARKERS = (
    "permission denied while trying to connect to the docker api",
    "cannot connect to the docker daemon",
    "is the docker daemon running",
    "connect: permission denied",
)
_DOCKER_USE_SUDO_CACHE: bool | None = None
# Migration input only. New releases should configure ordinary node peer
# variables with complete P2P multiaddrs.
LEGACY_PEER_SOURCE_KEYS = (
    "BDAG_P2P_LAN_PEERS",
    "LAN_PEER_ADDRESSES",
    "BDAG_P2P_VPN_PEERS",
    "VPN_PEER_ADDRESSES",
    "ZEROTIER_PEER_ADDRESSES",
    "DISCOVERED_ZEROTIER_PEER_ADDRESSES",
    "DISCOVERED_LAN_PEER_ADDRESSES",
    "BDAG_P2P_PUBLIC_PEERS",
    "EXTRA_PEER_ADDRESSES",
)
GENERIC_PEER_KEYS = (
    "BOOTSTRAP_PEER_ADDRESSES",
    "PEER_ADDRESSES",
    "LOCAL_PEER_ADDRESSES",
)
GENERATED_PEER_KEYS = ("BDAG_NODE_PEER_ADDRESSES",)
DEFAULT_NODE_PEER_LIMIT = 8
DEFAULT_STABLE_P2P_PORTS = "8150,8151,8152,8154"
PEER_ROSTER_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}


class PeerCandidates:
    def __init__(
        self,
        peers: list[str],
        rejected_non_p2p: list[str],
        source_peers: dict[str, list[str]] | None = None,
    ) -> None:
        self.peers = peers
        self.rejected_non_p2p = rejected_non_p2p
        self.source_peers = source_peers or {}

    @property
    def source_counts(self) -> dict[str, int]:
        return {source: len(peers) for source, peers in sorted(self.source_peers.items())}


def docker_top_has_bdag_child(output: str) -> bool:
    for line in output.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        command = parts[1]
        if command in {"bdag", "blockdag-node"} or command.endswith(("/bdag", "/blockdag-node")):
            return True
    return False


def command_uses_docker(command: list[str]) -> bool:
    return bool(command) and command[0] == "docker"


def sudo_docker_command(command: list[str]) -> list[str]:
    return ["sudo", "-n", *command]


def docker_sudo_fallback_enabled() -> bool:
    return os.environ.get("BDAG_DOCKER_SUDO_FALLBACK", "1").strip().lower() not in {"0", "false", "no", "off"}


def docker_use_sudo_requested() -> bool:
    return os.environ.get("BDAG_DOCKER_USE_SUDO", "0").strip().lower() in TRUE_VALUES


def docker_result_looks_like_access_error(proc: subprocess.CompletedProcess[str]) -> bool:
    text = f"{proc.stderr or ''}\n{proc.stdout or ''}".lower()
    return proc.returncode == 127 or any(marker in text for marker in DOCKER_ACCESS_ERROR_MARKERS)


def run_process(command: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    global _DOCKER_USE_SUDO_CACHE
    effective = command
    if command_uses_docker(command) and docker_sudo_fallback_enabled() and (
        docker_use_sudo_requested() or _DOCKER_USE_SUDO_CACHE is True
    ):
        effective = sudo_docker_command(command)
    proc = subprocess.run(
        effective,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if (
        command_uses_docker(command)
        and effective == command
        and proc.returncode != 0
        and docker_sudo_fallback_enabled()
        and docker_result_looks_like_access_error(proc)
    ):
        fallback = subprocess.run(
            sudo_docker_command(command),
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if fallback.returncode == 0:
            _DOCKER_USE_SUDO_CACHE = True
            return fallback
    elif command_uses_docker(command) and proc.returncode == 0:
        _DOCKER_USE_SUDO_CACHE = effective[0] == "sudo"
    return proc


def run(command: list[str], timeout: int = 20) -> str:
    proc = run_process(command, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"{command[0]} failed").strip())
    return proc.stdout


def docker_compose_container_id(service: str) -> str:
    proc = run_process(
        ["docker", "compose", "ps", "-q", service],
        timeout=10,
    )
    return proc.stdout.strip().splitlines()[-1] if proc.returncode == 0 and proc.stdout.strip() else ""


def docker_targets(container_or_service: str) -> list[str]:
    targets = [container_or_service]
    compose_id = docker_compose_container_id(container_or_service)
    if compose_id and compose_id not in targets:
        targets.append(compose_id)
    return targets


def docker_logs(container_or_service: str, tail: str = "5000", timeout: int = 20) -> str:
    errors: list[str] = []
    for target in docker_targets(container_or_service):
        proc = run_process(
            ["docker", "logs", "--tail", tail, target],
            timeout=timeout,
        )
        if proc.returncode == 0:
            return proc.stdout + proc.stderr
        errors.append((proc.stderr or proc.stdout or f"docker logs failed for {target}").strip())

    proc = run_process(
        ["docker", "compose", "logs", "--no-color", "--tail", tail, container_or_service],
        timeout=timeout,
    )
    if proc.returncode == 0:
        return proc.stdout + proc.stderr
    errors.append((proc.stderr or proc.stdout or f"docker compose logs failed for {container_or_service}").strip())
    raise RuntimeError("; ".join(error for error in errors if error))


def node_process_running(container: str) -> bool:
    for target in docker_targets(container):
        try:
            output = run(["docker", "top", target, "-eo", "pid,comm,args"], timeout=10)
        except Exception:
            continue
        if docker_top_has_bdag_child(output):
            return True
    return False


def wait_for_node(container: str, timeout: int = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if node_process_running(container):
            return
        time.sleep(3)
    raise RuntimeError(f"{container} did not show a running bdag process within {timeout}s")


def container_running(container: str) -> bool:
    for target in docker_targets(container):
        proc = run_process(
            ["docker", "inspect", "-f", "{{.State.Running}}", target],
            timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip() == "true":
            return True
    return False


def stop_inactive_nodes(active_nodes: list[str]) -> None:
    for node in NODE_SPECS:
        if node in active_nodes or not container_running(node):
            continue
        print(f"stopping inactive {node}; not the configured BDAG_NODE_SERVICE")
        run(["docker", "compose", "stop", node], timeout=120)


def read_env(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(errors="replace").splitlines() if path.exists() else []
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return lines, values


def read_env_values(path: Path) -> dict[str, str]:
    _, values = read_env(path)
    return values


def write_env(path: Path, lines: list[str], updates: dict[str, str]) -> None:
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                output.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        output.append(line)
    missing = [key for key in updates if key not in seen]
    if missing:
        if output and output[-1].strip():
            output.append("")
        output.append("# Local node peer discovery")
        for key in missing:
            output.append(f"{key}={updates[key]}")
    path.write_text("\n".join(output) + "\n")


def env_value(values: dict[str, str], key: str, default: str = "") -> str:
    return values.get(key) or os.environ.get(key) or default


def peer_source_value(values: dict[str, str], key: str) -> str:
    """Return peer-source config from the explicit stack config only.

    Peer launch inputs must not merge in arbitrary shell/systemd environment
    leftovers because generated peers can otherwise reseed themselves and grow
    the node launch list over time.
    """

    return values.get(key, "")


def truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in TRUE_VALUES


def split_peer_csv(value: str) -> list[str]:
    peers: list[str] = []
    for peer in re.split(r"[\s,]+", value or ""):
        peer = peer.strip()
        if peer:
            peers.append(peer)
    return peers


def peer_values(values: dict[str, str], keys: tuple[str, ...]) -> list[str]:
    peers: list[str] = []
    for key in keys:
        peers.extend(split_peer_csv(env_value(values, key)))
    return peers


def read_peer_file(path: Path) -> list[str]:
    try:
        text = path.read_text(errors="replace")
    except FileNotFoundError:
        return []
    except OSError:
        return []
    return split_peer_csv(text.replace("\n", ","))


def config_path(values: dict[str, str], key: str, default: Path) -> Path:
    raw = env_value(values, key, "")
    path = Path(raw).expanduser() if raw else default
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        tmp = Path(handle.name)
    tmp.replace(path)


def ipfs_peer_roster_enabled(values: dict[str, str]) -> bool:
    raw = env_value(values, "BDAG_IPFS_PEER_ROSTER_ENABLED", "1").strip().lower()
    return raw not in PEER_ROSTER_FALSE_VALUES


def peer_roster_status_file(values: dict[str, str]) -> Path:
    return config_path(
        values,
        "BDAG_IPFS_PEER_ROSTER_STATUS_FILE",
        RUNTIME_DIR / "ipfs-content" / "peer-roster-status.json",
    )


def peer_roster_index_path(values: dict[str, str]) -> Path:
    return config_path(
        values,
        "BDAG_IPFS_PEER_ROSTER_INDEX_PATH",
        RUNTIME_DIR / "ipfs-content" / "peer-roster.json",
    )


def discovery_file(values: dict[str, str]) -> Path:
    return config_path(values, "BDAG_IPFS_CONTENT_DISCOVERY_FILE", PROJECT_ROOT / "ops" / "ipfs-content-discovery.json")


def write_peer_roster_status(values: dict[str, str], state: str, **extra: object) -> None:
    payload: dict[str, object] = {
        "document_type": "bdag_ipfs_peer_roster_status_v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "state": state,
        "trust_model": "signed peer hints only; chain consensus and template readiness remain authoritative",
    }
    payload.update(extra)
    atomic_write_json(peer_roster_status_file(values), payload)


def normalized_ipfs_trust_env(values: dict[str, str]) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key.startswith("BDAG_") or key == "IPFS_PATH"}
    env.update(values)
    require = env_value(values, "BDAG_IPFS_PEER_ROSTER_REQUIRE_SIGNATURES", "1")
    env["BDAG_IPFS_SEGMENT_REQUIRE_SIGNATURES"] = require
    env["BDAG_IPFS_RESTORE_REQUIRE_SIGNATURES"] = require
    for key in ("BDAG_IPFS_SEGMENT_SIGNING_KEY_FILE", "BDAG_RAWDATADIR_SIGNING_KEY_FILE"):
        raw = env.get(key, "").strip()
        if raw:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                env[key] = str((PROJECT_ROOT / path).resolve())
    return env


def ipfs_command(values: dict[str, str], args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    binary = env_value(values, "BDAG_IPFS_BINARY", "ipfs")
    child_env = os.environ.copy()
    child_env.update({key: value for key, value in values.items() if key.startswith("BDAG_") or key == "IPFS_PATH"})
    return subprocess.run(
        [binary, *args],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        env=child_env,
    )


def parse_ipfs_add_cid(stdout: str) -> str:
    lines = [line.strip().split()[0] for line in stdout.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def ipfs_cat_json(values: dict[str, str], ref: str) -> dict[str, object]:
    timeout = safe_int(env_value(values, "BDAG_IPFS_PEER_ROSTER_IPFS_TIMEOUT", "20"), 20)
    proc = ipfs_command(values, ["cat", ref], timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"ipfs cat failed for {ref}").strip())
    payload = json.loads(proc.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError(f"IPFS peer roster {ref} is not a JSON object")
    return payload


def verify_peer_roster_payload(values: dict[str, str], payload: dict[str, object], *, context: str) -> None:
    if payload.get("document_type") != "bdag_ipfs_peer_roster_v1":
        raise RuntimeError(f"{context} has unsupported document_type {payload.get('document_type')!r}")
    if str(payload.get("network") or "").strip().lower() != "mainnet":
        raise RuntimeError(f"{context} is not a mainnet peer roster")
    sys.path.insert(0, str(PROJECT_ROOT / "ops"))
    import ipfs_segment_trust  # type: ignore

    ipfs_segment_trust.verify_payload_signature(
        payload,
        normalized_ipfs_trust_env(values),
        signature_field="roster_signatures",
        context=context,
    )


def peer_roster_refs(values: dict[str, str]) -> list[str]:
    refs: list[str] = []
    for key in ("BDAG_IPFS_PEER_ROSTER_CID", "BDAG_IPFS_PEER_ROSTER_DEFAULT_CID", "BDAG_IPFS_PEER_ROSTER_IPNS"):
        raw = env_value(values, key, "").strip()
        if raw:
            refs.append(raw if raw.startswith(("/ipfs/", "/ipns/")) else f"/ipfs/{raw}")
    path = discovery_file(values)
    try:
        discovery = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        discovery = {}
    if isinstance(discovery, dict):
        for key in ("current_peer_roster_cid", "peer_roster_latest_cid"):
            raw = str(discovery.get(key) or "").strip()
            if raw:
                refs.append(raw if raw.startswith(("/ipfs/", "/ipns/")) else f"/ipfs/{raw}")
        raw_ipns = str(discovery.get("current_peer_roster_ipns") or "").strip()
        if raw_ipns:
            refs.append(raw_ipns if raw_ipns.startswith("/ipns/") else f"/ipns/{raw_ipns}")
    seen: set[str] = set()
    result: list[str] = []
    for ref in refs:
        if ref and ref not in seen:
            result.append(ref)
            seen.add(ref)
    return result


def ipfs_peer_roster_candidates(values: dict[str, str]) -> list[str]:
    if not ipfs_peer_roster_enabled(values):
        return []
    peers: list[str] = []
    errors: list[str] = []
    for ref in peer_roster_refs(values):
        try:
            payload = ipfs_cat_json(values, ref)
            verify_peer_roster_payload(values, payload, context=f"IPFS peer roster {ref}")
        except Exception as exc:
            errors.append(f"{ref}: {exc}")
            continue
        for item in payload.get("peers") or []:
            if not isinstance(item, dict):
                continue
            multiaddr = str(item.get("multiaddr") or "").strip()
            if multiaddr and peer_parts(multiaddr):
                peers.append(multiaddr)
        if peers:
            write_peer_roster_status(values, "consumed", source=ref, peer_count=len(peers), errors=errors[:5])
            return peers
    if errors:
        write_peer_roster_status(values, "consume_failed", errors=errors[:5])
    return []


def extract_peerstore_log_peers(logs: str) -> list[str]:
    peers: list[str] = []
    seen: set[str] = set()
    for match in PEERSTORE_LOG_RE.finditer(logs):
        peer_id = match.group(1).strip()
        raw_addrs = match.group(2)
        if not peer_id:
            continue
        for addr in raw_addrs.split():
            if not addr.startswith(("/ip4/", "/ip6/", "/dns/", "/dns4/", "/dns6/")):
                continue
            full = f"{addr}/p2p/{peer_id}"
            if peer_parts(full) and full not in seen:
                seen.add(full)
                peers.append(full)
    return peers


def node_peerstore_log_candidates(values: dict[str, str], active_nodes: list[str] | None = None) -> list[str]:
    if not env_enabled("BDAG_CHAIN_PEERSTORE_PEER_EXTRACTION_ENABLED", True):
        return []
    nodes = active_nodes or configured_active_nodes(values)
    peers: list[str] = []
    seen: set[str] = set()
    for node in nodes:
        if node not in NODE_SPECS:
            continue
        try:
            logs = docker_logs(node, tail=CHAIN_PEERSTORE_LOG_TAIL, timeout=20)
        except Exception:
            continue
        for peer in extract_peerstore_log_peers(logs):
            if peer not in seen:
                seen.add(peer)
                peers.append(peer)
    return peers


def peer_parts(peer: str) -> tuple[str, int, str] | None:
    match = PEER_RE_FULL.search(peer)
    if not match:
        return None
    host, port_text, peer_id = match.groups()
    try:
        port = int(port_text)
    except ValueError:
        return None
    return host, port, peer_id


def stable_p2p_ports(values: dict[str, str]) -> set[int]:
    raw = env_value(values, "BDAG_NODE_PEER_STABLE_PORTS", DEFAULT_STABLE_P2P_PORTS)
    ports: set[int] = set()
    for token in re.split(r"[\s,]+", raw):
        token = token.strip()
        if not token:
            continue
        try:
            port = int(token)
        except ValueError:
            continue
        if 1 <= port <= 65535:
            ports.add(port)
    return ports or {8150, 8151, 8152, 8154}


def node_peer_limit(values: dict[str, str]) -> int:
    raw = env_value(values, "BDAG_NODE_PEER_LIMIT", str(DEFAULT_NODE_PEER_LIMIT))
    try:
        limit = int(raw)
    except ValueError:
        return DEFAULT_NODE_PEER_LIMIT
    return max(1, min(limit, 128))


def public_or_dns_peer_host(host: str) -> bool:
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return ip.version == 4 and ip.is_global


def curated_node_launch_peers(
    peers: list[str],
    values: dict[str, str],
    local_peer_ids: set[str] | None = None,
) -> list[str]:
    """Return the small stable peer set used to launch the node.

    Raw peer observations can be large and noisy. The node launch list must stay
    bounded and stable: one public/DNS listener address per peer ID, on known
    P2P listener ports only.
    """
    local_peer_ids = local_peer_ids or set()
    stable_ports = stable_p2p_ports(values)
    limit = node_peer_limit(values)
    selected: list[str] = []
    seen_peer_ids: set[str] = set()
    for peer in peers:
        parts = peer_parts(peer)
        if not parts:
            continue
        host, port, peer_id = parts
        if peer_id in local_peer_ids or peer_id in seen_peer_ids:
            continue
        if port not in stable_ports:
            continue
        if not public_or_dns_peer_host(host):
            continue
        selected.append(peer)
        seen_peer_ids.add(peer_id)
        if len(selected) >= limit:
            break
    return selected


def parse_networks(raw: str, default: str = "") -> list[ipaddress.IPv4Network]:
    networks: list[ipaddress.IPv4Network] = []
    for item in re.split(r"[\s,]+", raw or default):
        item = item.strip()
        if not item:
            continue
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError:
            continue
        if network.version == 4:
            networks.append(network)
    return networks


def asic_lan_networks(values: dict[str, str]) -> list[ipaddress.IPv4Network]:
    return parse_networks(env_value(values, "BDAG_ASIC_LAN_CIDRS", DEFAULT_ASIC_LAN_CIDRS))


def interface_ipv4_addresses() -> dict[str, list[str]]:
    try:
        output = run(["ip", "-br", "addr"], timeout=5)
    except Exception:
        return {}
    result: dict[str, list[str]] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        iface = parts[0]
        for token in parts[2:]:
            if "/" not in token:
                continue
            ip_text = token.split("/", 1)[0]
            try:
                ip = ipaddress.ip_address(ip_text)
            except ValueError:
                continue
            if ip.version == 4 and not ip.is_loopback and not ip.is_link_local:
                result.setdefault(iface, []).append(str(ip))
    return result


def interface_ipv4_networks() -> dict[str, list[ipaddress.IPv4Network]]:
    try:
        output = run(["ip", "-br", "addr"], timeout=5)
    except Exception:
        return {}
    result: dict[str, list[ipaddress.IPv4Network]] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        iface = parts[0]
        for token in parts[2:]:
            if "/" not in token:
                continue
            try:
                interface = ipaddress.ip_interface(token)
            except ValueError:
                continue
            if interface.version == 4 and not interface.ip.is_loopback and not interface.ip.is_link_local:
                result.setdefault(iface, []).append(interface.network)
    return result


def vpn_or_container_interface(iface: str) -> bool:
    return iface.startswith(("zt", "wg", "tun", "tap", "tailscale", "docker", "br-", "veth"))


def local_lan_networks(values: dict[str, str]) -> list[ipaddress.IPv4Network]:
    asic_networks = asic_lan_networks(values)
    networks: list[ipaddress.IPv4Network] = []
    for iface, iface_networks in interface_ipv4_networks().items():
        if vpn_or_container_interface(iface):
            continue
        for network in iface_networks:
            if any(network.subnet_of(asic_network) or asic_network.subnet_of(network) for asic_network in asic_networks):
                continue
            if network.is_private:
                networks.append(network)
    return networks


def default_route_interface() -> str:
    try:
        output = run(["ip", "route"], timeout=5)
    except Exception:
        return ""
    for line in output.splitlines():
        parts = line.split()
        if not parts or parts[0] != "default":
            continue
        try:
            return parts[parts.index("dev") + 1]
        except (ValueError, IndexError):
            return ""
    return ""


def ip_in_networks(address: str, networks: list[ipaddress.IPv4Network]) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.version == 4 and any(ip in network for network in networks)


def detect_network_topology(values: dict[str, str]) -> str:
    explicit = env_value(values, "BDAG_NETWORK_TOPOLOGY", "auto").strip().lower()
    if explicit not in AUTO_VALUES:
        return explicit
    if truthy(env_value(values, "BDAG_ASIC_LAN_ENABLED")):
        return "asic-router"

    default_iface = default_route_interface()
    asic_iface = env_value(values, "BDAG_ASIC_LAN_INTERFACE", "").strip()
    networks = asic_lan_networks(values)
    for iface, addresses in interface_ipv4_addresses().items():
        if default_iface and iface == default_iface:
            continue
        if asic_iface and iface != asic_iface:
            continue
        if any(ip_in_networks(address, networks) for address in addresses):
            return "asic-router"
    return "standard"


def sort_peers_by_latency(peers: list[str]) -> list[str]:
    indexed = list(enumerate(unique_csv(peers).split(","))) if peers else []
    scores: dict[int, tuple[bool, float]] = {}
    with ThreadPoolExecutor(max_workers=max(1, PEER_LATENCY_WORKERS)) as executor:
        futures = {executor.submit(peer_tcp_latency, peer): index for index, peer in indexed}
        for future in as_completed(futures):
            scores[futures[future]] = future.result()
    indexed.sort(key=lambda item: (0 if scores.get(item[0], (False, float("inf")))[0] else 1, scores.get(item[0], (False, float("inf")))[1], item[0]))
    return [peer for _, peer in indexed]


def p2p_peer_candidates(values: dict[str, str]) -> PeerCandidates:
    seen: set[str] = set()
    peers: list[str] = []
    rejected_non_p2p: list[str] = []
    source_peers: dict[str, list[str]] = {}

    def add(peer: str, source: str) -> None:
        peer = peer.strip()
        if not peer or peer in seen:
            return
        seen.add(peer)
        if not peer_parts(peer):
            rejected_non_p2p.append(peer)
            return
        peers.append(peer)
        source_peers.setdefault(source, []).append(peer)

    for key in GENERIC_PEER_KEYS:
        for peer in split_peer_csv(peer_source_value(values, key)):
            add(peer, key)

    for key in GENERATED_PEER_KEYS:
        for peer in split_peer_csv(peer_source_value(values, key)):
            if not peer_parts(peer):
                add(peer, key)

    for key in LEGACY_PEER_SOURCE_KEYS:
        for peer in split_peer_csv(peer_source_value(values, key)):
            add(peer, key)

    for peer in ipfs_peer_roster_candidates(values):
        add(peer, "ipfs-peer-roster")

    for peer in read_peer_file(LIVE_PEERS_FILE):
        add(peer, "runtime-live-peers")

    for peer in read_peer_file(CHAIN_PEERSTORE_CANDIDATES_FILE):
        add(peer, "chain-peerstore-candidate-file")

    for peer in node_peerstore_log_candidates(values):
        add(peer, "chain-peerstore-startup-log")

    sorted_peers = sort_peers_by_latency(peers)
    return PeerCandidates(sorted_peers, rejected_non_p2p, source_peers)


def write_deferred_apply(reason: str) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    DEFERRED_APPLY_FILE.write_text(reason + "\n")


def clear_deferred_apply() -> None:
    try:
        DEFERRED_APPLY_FILE.unlink()
    except FileNotFoundError:
        pass


def configured_p2p_port(values: dict[str, str]) -> int:
    raw = env_value(values, "P2P_PORT", "8150")
    try:
        port = int(raw)
    except ValueError:
        return 8150
    if 1 <= port <= 65535:
        return port
    return 8150


def fallback_peer_ids(values: dict[str, str]) -> dict[str, str]:
    p2p_port = configured_p2p_port(values)
    by_port = {str(p2p_port): "node"}
    result: dict[str, str] = {}
    for node, keys in NODE_PEER_ID_ENV.items():
        for key in keys:
            peer_id = values.get(key, "").strip()
            if peer_id:
                result[node] = peer_id
                break
    for value in (values.get("LOCAL_PEER_ADDRESSES", ""),):
        for port, peer_id in ADDR_RE.findall(value):
            node = by_port.get(port)
            if node and node not in result:
                result[node] = peer_id
    return result


def peer_tcp_latency(peer: str) -> tuple[bool, float]:
    match = PEER_RE_FULL.search(peer)
    if not match:
        return False, float("inf")
    host, port_text, _ = match.groups()
    try:
        port = int(port_text)
    except ValueError:
        return False, float("inf")
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=PEER_LATENCY_TIMEOUT):
            pass
        return True, (time.monotonic() - started) * 1000
    except OSError:
        return False, float("inf")


def peer_reachability_results(peers: list[str]) -> list[dict[str, object]]:
    unique = unique_csv(peers).split(",") if peers else []

    def probe(peer: str) -> dict[str, object]:
        parts = peer_parts(peer)
        if not parts:
            return {"multiaddr": peer, "status": "unparsed"}
        host, port, peer_id = parts
        live, latency = peer_tcp_latency(peer)
        result: dict[str, object] = {
            "multiaddr": peer,
            "host": host,
            "port": port,
            "peer_id": peer_id,
            "status": "tcp-open" if live else "closed",
        }
        if live:
            result["rtt_ms"] = round(latency, 1)
        return result

    with ThreadPoolExecutor(max_workers=max(1, PEER_LATENCY_WORKERS)) as executor:
        return list(executor.map(probe, unique))


def write_peer_discovery_artifacts(peer_candidates: PeerCandidates, remote_candidate_peers: list[str]) -> dict[str, object]:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    chain_sources = [
        "chain-peerstore-startup-log",
        "chain-peerstore-candidate-file",
    ]
    chain_peers: list[str] = []
    for source in chain_sources:
        chain_peers.extend(peer_candidates.source_peers.get(source, []))
    chain_peer_text = "\n".join(unique_csv(chain_peers).split(",")) if chain_peers else ""
    CHAIN_PEERSTORE_CANDIDATES_FILE.write_text((chain_peer_text + "\n") if chain_peer_text else "")

    reachability = peer_reachability_results(remote_candidate_peers)
    live_peers = [str(item["multiaddr"]) for item in reachability if item.get("status") == "tcp-open"]
    LIVE_PEERS_FILE.write_text(("\n".join(live_peers) + "\n") if live_peers else "")

    manifest = {
        "document_type": "bdag_peer_discovery_manifest_v1",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "chain_peerstore_extraction": env_enabled("BDAG_CHAIN_PEERSTORE_PEER_EXTRACTION_ENABLED", True),
        "source_counts": peer_candidates.source_counts,
        "candidate_count": len(peer_candidates.peers),
        "remote_candidate_count": len(remote_candidate_peers),
        "rejected_non_p2p_count": len(peer_candidates.rejected_non_p2p),
        "live_tcp_open_count": len(live_peers),
        "live_peers_file": str(LIVE_PEERS_FILE),
        "chain_peerstore_candidates_file": str(CHAIN_PEERSTORE_CANDIDATES_FILE),
        "note": "tcp-open proves reachability only; startup readiness must still verify node peer handshakes, sync freshness, and mining template health.",
        "peers": sorted(reachability, key=lambda item: (item.get("status") != "tcp-open", str(item.get("host", "")), int(item.get("port", 0) or 0), str(item.get("multiaddr", "")))),
    }
    PEER_DISCOVERY_FILE.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def max_peer_roster_peers(values: dict[str, str]) -> int:
    return max(1, min(safe_int(env_value(values, "BDAG_IPFS_PEER_ROSTER_MAX_PEERS", "64"), 64), 256))


def peer_roster_publish_enabled(values: dict[str, str]) -> bool:
    raw = env_value(values, "BDAG_IPFS_PEER_ROSTER_PUBLISH_IPFS", "1").strip().lower()
    return raw not in PEER_ROSTER_FALSE_VALUES


def update_peer_roster_discovery(values: dict[str, str], cid: str, roster: dict[str, object]) -> None:
    path = discovery_file(values)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        discovery = loaded if isinstance(loaded, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        discovery = {}
    if not discovery:
        discovery = {"document_type": "bdag_ipfs_content_discovery_v1", "network": "mainnet"}
    discovery["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    discovery["current_peer_roster_cid"] = cid
    discovery["peer_roster_latest_cid"] = cid
    discovery["current_peer_roster_uri"] = f"ipfs://{cid}"
    discovery["current_peer_roster"] = {
        "document_type": roster.get("document_type"),
        "network": roster.get("network"),
        "generated_at": roster.get("generated_at"),
        "peer_count": len(roster.get("peers") or []),
        "writer": roster.get("writer"),
        "cid": cid,
    }
    discovery["peer_roster_trust_model"] = (
        "Peer rosters are signed bootstrap hints only. Consumers must verify "
        "the roster signature before use, TCP-probe candidates, and still rely "
        "on live chain consensus for block validity."
    )
    atomic_write_json(path, discovery)


def build_signed_peer_roster(values: dict[str, str], discovery: dict[str, object]) -> dict[str, object]:
    sys.path.insert(0, str(PROJECT_ROOT / "ops"))
    import ipfs_segment_trust  # type: ignore

    candidate_multiaddrs: list[str] = []
    source_by_multiaddr: dict[str, object] = {}
    for item in discovery.get("peers") or []:
        if not isinstance(item, dict) or item.get("status") != "tcp-open":
            continue
        multiaddr = str(item.get("multiaddr") or "").strip()
        if not peer_parts(multiaddr):
            continue
        candidate_multiaddrs.append(multiaddr)
        source_by_multiaddr[multiaddr] = item.get("source") or "local-peer-discovery"

    roster_values = dict(values)
    roster_values["BDAG_NODE_PEER_LIMIT"] = env_value(values, "BDAG_IPFS_PEER_ROSTER_MAX_PEERS", "64")
    public_multiaddrs = curated_node_launch_peers(candidate_multiaddrs, roster_values)
    peer_rows: list[dict[str, object]] = []
    for multiaddr in public_multiaddrs:
        parts = peer_parts(multiaddr)
        if not parts:
            continue
        host, port, peer_id = parts
        peer_rows.append(
            {
                "multiaddr": multiaddr,
                "host": host,
                "port": port,
                "peer_id": peer_id,
                "source": source_by_multiaddr.get(multiaddr, "local-peer-discovery"),
                "publication_filter": "public_or_dns_stable_p2p_port_one_address_per_peer_id",
                "observed_status": "tcp-open",
            }
        )
    previous = ""
    try:
        loaded = json.loads(discovery_file(values).read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            previous = str(loaded.get("current_peer_roster_cid") or loaded.get("peer_roster_latest_cid") or "")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        previous = ""
    payload: dict[str, object] = {
        "document_type": "bdag_ipfs_peer_roster_v1",
        "schema_version": 1,
        "network": "mainnet",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_discovery_file": str(PEER_DISCOVERY_FILE),
        "previous_peer_roster_cid": previous,
        "peer_count": len(peer_rows),
        "max_peers": max_peer_roster_peers(values),
        "writer": {
            "writer_id": env_value(values, "BDAG_IPFS_SEGMENT_WRITER_ID", ""),
            "local_node_peer_id": env_value(values, "BDAG_LOCAL_NODE_PEER_ID", ""),
        },
        "trust_model": (
            "This signed roster is a bootstrap hint, not chain authority. "
            "A consumer must verify this signature, probe peers, and validate "
            "chain data against consensus and chain anchors."
        ),
        "peers": peer_rows,
    }
    return ipfs_segment_trust.sign_payload(
        payload,
        normalized_ipfs_trust_env(values),
        signature_field="roster_signatures",
    )


def publish_peer_roster(values: dict[str, str], discovery: dict[str, object]) -> None:
    if not ipfs_peer_roster_enabled(values):
        write_peer_roster_status(values, "disabled", reasons=["BDAG_IPFS_PEER_ROSTER_ENABLED is disabled"])
        return
    try:
        roster = build_signed_peer_roster(values, discovery)
    except Exception as exc:
        write_peer_roster_status(values, "waiting_for_signing_identity", reasons=[str(exc)])
        return

    index_path = peer_roster_index_path(values)
    atomic_write_json(index_path, roster)
    if not peer_roster_publish_enabled(values):
        write_peer_roster_status(values, "ready", peer_count=len(roster.get("peers") or []), index_path=str(index_path))
        return

    add_args = split_peer_csv(env_value(values, "BDAG_IPFS_PEER_ROSTER_ADD_ARGS", "--cid-version=1 --pin=true --quieter"))
    timeout = safe_int(env_value(values, "BDAG_IPFS_PEER_ROSTER_IPFS_TIMEOUT", "20"), 20)
    proc = ipfs_command(values, ["add", *add_args, str(index_path)], timeout=timeout)
    if proc.returncode != 0:
        write_peer_roster_status(
            values,
            "publish_failed",
            peer_count=len(roster.get("peers") or []),
            index_path=str(index_path),
            reasons=[(proc.stderr or proc.stdout or "ipfs add failed").strip()[-1000:]],
        )
        return
    cid = parse_ipfs_add_cid(proc.stdout)
    if not cid:
        write_peer_roster_status(values, "publish_failed", peer_count=len(roster.get("peers") or []), index_path=str(index_path), reasons=["ipfs add returned no CID"])
        return
    update_peer_roster_discovery(values, cid, roster)
    write_peer_roster_status(
        values,
        "published",
        peer_count=len(roster.get("peers") or []),
        index_path=str(index_path),
        peer_roster_cid=cid,
        peer_roster_uri=f"ipfs://{cid}",
    )


def latest_peer_id(container: str, fallback: str | None = None) -> str:
    if fallback:
        return fallback
    try:
        logs = docker_logs(container, tail="5000", timeout=20)
    except Exception as exc:
        if fallback:
            return fallback
        raise RuntimeError(str(exc)) from exc
    matches = PEER_RE.findall(logs)
    if not matches:
        if fallback:
            return fallback
        raise RuntimeError(f"could not find local peer ID in recent logs for {container}")
    return matches[-1]


def local_ipv4_addresses() -> list[str]:
    try:
        output = run(["hostname", "-I"], timeout=5)
    except Exception:
        output = ""
    result: list[str] = []
    for token in output.split():
        try:
            ip = ipaddress.ip_address(token)
        except ValueError:
            continue
        if ip.version != 4 or ip.is_loopback or ip.is_link_local:
            continue
        result.append(str(ip))
    return result


def choose_local_ip(explicit: str | None = None, values: dict[str, str] | None = None) -> str:
    if explicit:
        return explicit
    values = values or {}
    configured = env_value(values, "BDAG_P2P_ADVERTISE_IP")
    if configured:
        return configured

    networks = asic_lan_networks(values)
    interfaces = interface_ipv4_addresses()
    preferred_iface = env_value(values, "BDAG_P2P_INTERFACE")
    if preferred_iface:
        for address in interfaces.get(preferred_iface, []):
            if not ip_in_networks(address, networks):
                return address

    default_iface = default_route_interface()
    if default_iface:
        for address in interfaces.get(default_iface, []):
            if not ip_in_networks(address, networks):
                return address

    addresses = local_ipv4_addresses()
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_private and not ip_in_networks(address, networks):
            return address
    if addresses:
        for address in addresses:
            if not ip_in_networks(address, networks):
                return address
    raise RuntimeError("could not determine a host IPv4 address for local P2P")


def unique_csv(items: list[str]) -> str:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        item = item.strip()
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return ",".join(result)


def without_peer_ids(peers: list[str], peer_ids: set[str]) -> list[str]:
    if not peer_ids:
        return peers
    result: list[str] = []
    for peer in peers:
        parts = peer_parts(peer)
        if parts and parts[2] in peer_ids:
            continue
        result.append(peer)
    return result


def without_inactive_local_node_peers(peers: list[str], active_nodes: list[str], host_ip: str) -> list[str]:
    inactive_nodes = {node for node in NODE_SPECS if node not in active_nodes}
    if not inactive_nodes:
        return peers
    local_hosts = set(local_ipv4_addresses()) | {host_ip, "127.0.0.1", "localhost"}
    active_ports = {NODE_SPECS[node]["port"] for node in active_nodes if node in NODE_SPECS}
    inactive_ports = {NODE_SPECS[node]["port"] for node in inactive_nodes if NODE_SPECS[node]["port"] not in active_ports}
    inactive_dns_hosts = set(inactive_nodes)
    result: list[str] = []
    for peer in peers:
        parts = peer_parts(peer)
        if not parts:
            result.append(peer)
            continue
        host, port, _peer_id = parts
        if port in inactive_ports and (host in local_hosts or host in inactive_dns_hosts):
            continue
        result.append(peer)
    return result


def csv_set(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def update_value_changed(key: str, current: str | None, new: str) -> bool:
    if key.endswith("_PEER_ADDRESSES") or key == "LOCAL_PEER_ADDRESSES":
        return csv_set(current or "") != csv_set(new)
    return (current or "") != new


def env_enabled(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def safe_int(value: object, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def fetch_dashboard_status() -> dict[str, object]:
    request = urllib.request.Request(COLLECTOR_STATUS_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=3) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    return payload if isinstance(payload, dict) else {}


def active_mining_recreate_guard_reason() -> str:
    if not env_enabled("BDAG_LOCAL_PEERS_DEFER_NODE_RECREATE_WHILE_MINING", True):
        return ""
    try:
        status = fetch_dashboard_status()
    except Exception:
        return ""
    pool = status.get("pool") if isinstance(status.get("pool"), dict) else {}
    active_connections = safe_int(pool.get("metrics_active_connections"), 0)
    recent_share_age = safe_int(pool.get("last_valid_share_age_seconds"), 999999)
    recent_submit_age = safe_int(pool.get("last_submit_age_seconds"), 999999)
    recent_work = min(recent_share_age, recent_submit_age) <= ACTIVE_MINING_RECENT_SECONDS
    if active_connections <= 0 or not (status.get("can_accept_shares") or status.get("can_mine") or recent_work):
        return ""
    return (
        f"active mining detected: {active_connections} stratum connection(s), "
        f"last_valid_share_age_seconds={recent_share_age}, "
        f"last_submit_age_seconds={recent_submit_age}"
    )


def configured_active_nodes(pool_values: dict[str, str]) -> list[str]:
    runtime_values = read_env_values(RUNTIME_ENV_FILE)
    raw = (
        os.environ.get("BDAG_NODE_SERVICE")
        or runtime_values.get("BDAG_NODE_SERVICE")
        or pool_values.get("BDAG_NODE_SERVICE")
        or ""
    )
    if not raw:
        return [DEFAULT_ACTIVE_NODE_SERVICE]
    node = raw.strip()
    return [node] if node in NODE_SPECS else []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-ip", help="host IPv4 address to advertise in local P2P multiaddrs")
    parser.add_argument("--env-file", type=Path, default=ENV_FILE, help="Pool stack .env file to update")
    parser.add_argument("--compose-file", type=Path, default=PROJECT_ROOT / "docker-compose.yml", help="Compose file used when --apply restarts node containers")
    parser.add_argument("--apply", action="store_true", help="Restart node containers sequentially if peer lists changed")
    parser.add_argument("--force-apply", action="store_true", help="Restart node containers sequentially even if peer lists did not change")
    args = parser.parse_args()

    env_file = args.env_file
    compose_file = args.compose_file
    lines, values = read_env(env_file)
    active_nodes = configured_active_nodes(values)
    topology = detect_network_topology(values)
    peer_candidates = p2p_peer_candidates(values)
    candidate_peers = peer_candidates.peers
    host_ip = choose_local_ip(args.host_ip, values)

    fallback_peers = fallback_peer_ids(values)
    peers: dict[str, str] = {}
    for node in active_nodes:
        peers[node] = latest_peer_id(node, fallback=fallback_peers.get(node))
    inactive_local_peer_ids = {
        peer_id
        for node, peer_id in fallback_peers.items()
        if node not in active_nodes and peer_id
    }
    local_peer_ids = {peer_id for peer_id in peers.values() if peer_id} | inactive_local_peer_ids
    remote_candidate_peers = without_inactive_local_node_peers(
        without_peer_ids(candidate_peers, local_peer_ids),
        active_nodes,
        host_ip,
    )
    peer_discovery = write_peer_discovery_artifacts(peer_candidates, remote_candidate_peers)
    publish_peer_roster(values, peer_discovery)

    p2p_port = configured_p2p_port(values)
    local_addrs = {
        node: f"/ip4/{host_ip}/tcp/{p2p_port}/p2p/{peers[node]}"
        for node in NODE_SPECS
        if node in peers
    }
    updates: dict[str, str] = {}
    primary_node = "node" if "node" in active_nodes else ""
    if primary_node:
        candidate_node_peers = without_inactive_local_node_peers(
            without_peer_ids(
                list(candidate_peers),
                inactive_local_peer_ids | {peers.get(primary_node, "")},
            ),
            active_nodes,
            host_ip,
        )
        node_peers = curated_node_launch_peers(
            candidate_node_peers,
            values,
            inactive_local_peer_ids | {peers.get(primary_node, "")},
        )
        updates["BDAG_NODE_PEER_ADDRESSES"] = unique_csv(node_peers)
    if local_addrs:
        updates["LOCAL_PEER_ADDRESSES"] = unique_csv([local_addrs[node] for node in active_nodes if node in local_addrs])
    for node, peer_id in peers.items():
        peer_id_keys = NODE_PEER_ID_ENV.get(node, ())
        if peer_id_keys:
            updates[peer_id_keys[0]] = peer_id
    for node in NODE_SPECS:
        if node in active_nodes:
            continue
        for peer_id_key in NODE_PEER_ID_ENV.get(node, ()):
            if peer_id_key in values:
                updates[peer_id_key] = ""
    updates["BDAG_NETWORK_TOPOLOGY"] = env_value(values, "BDAG_NETWORK_TOPOLOGY", "auto") or "auto"
    updates["BDAG_DETECTED_NETWORK_TOPOLOGY"] = topology
    for legacy_key in (
        "BDAG_P2P_LAN_PEERS",
        "LAN_PEER_ADDRESSES",
        "BDAG_P2P_VPN_PEERS",
        "VPN_PEER_ADDRESSES",
        "ZEROTIER_PEER_ADDRESSES",
        "DISCOVERED_ZEROTIER_PEER_ADDRESSES",
        "DISCOVERED_LAN_PEER_ADDRESSES",
        "BDAG_P2P_PUBLIC_PEERS",
        "EXTRA_PEER_ADDRESSES",
    ):
        if legacy_key in values:
            updates[legacy_key] = ""

    changed = any(update_value_changed(key, values.get(key), value) for key, value in updates.items())
    if changed:
        write_env(env_file, lines, updates)
        print(f"updated {env_file}")
    else:
        print("local peer configuration already current")
    print(f"host_ip={host_ip}")
    print(f"network_topology={topology}")
    print(f"active_nodes={','.join(active_nodes)}")
    print(f"p2p_candidates={len(candidate_peers)} remote_p2p_candidates={len(remote_candidate_peers)} rejected_non_p2p={len(peer_candidates.rejected_non_p2p)}")
    print(f"peer_source_counts={json.dumps(peer_candidates.source_counts, sort_keys=True)}")
    print(f"peer_discovery_manifest={PEER_DISCOVERY_FILE}")
    for node, addr in local_addrs.items():
        print(f"{node}={addr}")

    apply_needed = args.force_apply or (args.apply and (changed or DEFERRED_APPLY_FILE.exists()))
    if args.apply and not args.force_apply and apply_needed:
        guard_reason = active_mining_recreate_guard_reason()
        if guard_reason:
            write_deferred_apply(guard_reason)
            print(f"deferring container recreation: {guard_reason}")
            return 0
    if args.apply or args.force_apply:
        stop_inactive_nodes(active_nodes)
    if len(active_nodes) == 1 and args.apply and not args.force_apply and apply_needed:
        write_deferred_apply("peer config updated without recreating the only production node")
        print("not recreating the active production node automatically; use --force-apply for an explicit restart")
        return 0
    if apply_needed:
        for node in active_nodes:
            print(f"recreating {node} to apply local peers")
            run([
                "docker",
                "compose",
                "--env-file",
                str(env_file),
                "-f",
                str(compose_file),
                "up",
                "-d",
                "--force-recreate",
                "--no-deps",
                node,
            ], timeout=120)
            wait_for_node(node)
        clear_deferred_apply()
    return 0


if __name__ == "__main__":
    sys.exit(main())

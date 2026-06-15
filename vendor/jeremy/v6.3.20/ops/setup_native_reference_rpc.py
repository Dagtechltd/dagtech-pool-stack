#!/usr/bin/env python3
"""Provision and validate an independent native BlockDAG reference RPC.

IPFS chain-order publication must compare local bytes against an independent
native BlockDAG RPC. Public EVM JSON-RPC endpoints are not enough because they
do not expose the chain-order methods needed to verify raw segment contents.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Mapping


ROOT = Path(__file__).resolve().parents[1]
OPS_DIR = ROOT / "ops"
sys.path.insert(0, str(OPS_DIR))

import chain_integrity_gate  # noqa: E402


RpcCall = Callable[[str, str, list[Any], float, Mapping[str, str]], Any]


def now_iso() -> str:
    return chain_integrity_gate.now_iso()


def env_quote(value: str) -> str:
    if value and all((not ch.isspace()) and ch not in '"\\$`#' for ch in value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"'


def set_env_value(path: Path, key: str, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = key + "="
    encoded = prefix + env_quote(value)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []
    updated: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith(prefix):
            if not replaced:
                updated.append(encoded)
                replaced = True
            continue
        updated.append(line)
    if not replaced:
        updated.append(encoded)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(updated).rstrip() + "\n", encoding="utf-8")
    os.replace(tmp, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def url_without_query(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.username or parsed.password:
        raise ValueError("reference_rpc_url_must_not_embed_credentials")
    host = (parsed.hostname or "").lower()
    if not parsed.scheme or not host:
        raise ValueError("reference_rpc_url_must_include_scheme_and_host")
    port = parsed.port
    netloc = host if port is None else f"{host}:{port}"
    path = parsed.path.rstrip("/") or ""
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def same_endpoint(left: str, right: str) -> bool:
    if not left or not right:
        return False
    try:
        return url_without_query(left) == url_without_query(right)
    except ValueError:
        return left.strip().rstrip("/") == right.strip().rstrip("/")


def validate_native_reference_rpc(
    reference_url: str,
    *,
    source_url: str = "",
    timeout: float = 8.0,
    env: Mapping[str, str] | None = None,
    rpc: RpcCall = chain_integrity_gate.rpc_call,
) -> dict[str, Any]:
    env = dict(env or {})
    checks: list[dict[str, Any]] = []
    reasons: list[str] = []

    reference_url = reference_url.strip()
    source_url = source_url.strip()
    if not reference_url:
        return {"ok": False, "state": "missing", "reasons": ["reference_rpc_url_missing"], "checks": checks}

    try:
        normalized_reference = url_without_query(reference_url)
    except ValueError as exc:
        return {"ok": False, "state": "invalid", "reasons": [str(exc)], "checks": checks}

    if source_url and same_endpoint(normalized_reference, source_url):
        return {
            "ok": False,
            "state": "not_independent",
            "reasons": ["reference_rpc_must_be_independent"],
            "checks": [{"name": "independence", "state": "rejected", "source_url": chain_integrity_gate.redacted_url(source_url), "reference_url": chain_integrity_gate.redacted_url(normalized_reference)}],
        }
    checks.append({"name": "independence", "state": "ok", "reference_url": chain_integrity_gate.redacted_url(normalized_reference)})

    try:
        reference_tip = chain_integrity_gate.fetch_order_record(normalized_reference, -1, timeout, env, rpc)
    except Exception as exc:  # noqa: BLE001 - report exact native-method failure.
        return {
            "ok": False,
            "state": "native_method_unavailable",
            "reasons": [f"reference_getBlockByOrder_unavailable:{exc}"],
            "checks": checks,
            "reference_url": chain_integrity_gate.redacted_url(normalized_reference),
        }
    checks.append({"name": "reference_getBlockByOrder", "state": "ok", "order": reference_tip.get("order"), "hash": reference_tip.get("hash")})

    try:
        reference_genesis = chain_integrity_gate.fetch_genesis_hash(normalized_reference, timeout, env, rpc)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "state": "reference_unready",
            "reasons": [f"reference_genesis_unavailable:{exc}"],
            "checks": checks,
            "reference_url": chain_integrity_gate.redacted_url(normalized_reference),
        }
    reference_network = chain_integrity_gate.fetch_network_identity(normalized_reference, timeout, env, rpc)
    checks.append({"name": "reference_chain_identity", "state": "ok", "genesis_hash": reference_genesis, "network": reference_network})

    details: dict[str, Any] = {
        "ok": True,
        "state": "validated",
        "reasons": reasons,
        "checks": checks,
        "reference_url": normalized_reference,
        "reference_order": reference_tip.get("order"),
        "reference_hash": reference_tip.get("hash"),
        "reference_genesis_hash": reference_genesis,
        "reference_network": reference_network,
    }

    if source_url:
        try:
            normalized_source = url_without_query(source_url)
        except ValueError as exc:
            return {"ok": False, "state": "invalid_source", "reasons": [str(exc)], "checks": checks}
        try:
            source_tip = chain_integrity_gate.fetch_order_record(normalized_source, -1, timeout, env, rpc)
            source_genesis = chain_integrity_gate.fetch_genesis_hash(normalized_source, timeout, env, rpc)
            source_network = chain_integrity_gate.fetch_network_identity(normalized_source, timeout, env, rpc)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "state": "source_unready",
                "reasons": [f"source_native_rpc_unavailable:{exc}"],
                "checks": checks,
                "source_url": chain_integrity_gate.redacted_url(normalized_source),
                "reference_url": chain_integrity_gate.redacted_url(normalized_reference),
            }
        checks.append({"name": "source_chain_identity", "state": "ok", "order": source_tip.get("order"), "hash": source_tip.get("hash"), "genesis_hash": source_genesis, "network": source_network})
        if source_genesis != reference_genesis:
            return {
                "ok": False,
                "state": "mismatch",
                "reasons": ["genesis_hash_mismatch"],
                "checks": checks,
                "source_genesis_hash": source_genesis,
                "reference_genesis_hash": reference_genesis,
            }
        if source_network and reference_network and source_network != reference_network:
            return {
                "ok": False,
                "state": "mismatch",
                "reasons": ["network_identity_mismatch"],
                "checks": checks,
                "source_network": source_network,
                "reference_network": reference_network,
            }
        details.update(
            {
                "source_url": normalized_source,
                "source_order": source_tip.get("order"),
                "source_hash": source_tip.get("hash"),
                "source_genesis_hash": source_genesis,
                "source_network": source_network,
            }
        )

    return details


def ensure_ssh_key(key_path: Path) -> None:
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if key_path.exists():
        return
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", f"bdag-native-reference-rpc@{os.uname().nodename}", "-f", str(key_path)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    key_path.chmod(0o600)
    pub = key_path.with_suffix(key_path.suffix + ".pub")
    if pub.exists():
        pub.chmod(0o644)


def tunnel_exec_start(
    *,
    ssh_target: str,
    local_bind: str,
    local_port: int,
    remote_host: str,
    remote_port: int,
    key_path: Path,
    known_hosts: Path,
) -> str:
    forward = f"{local_bind}:{local_port}:{remote_host}:{remote_port}"
    cmd = [
        "/usr/bin/ssh",
        "-N",
        "-L",
        forward,
        "-i",
        str(key_path),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        ssh_target,
    ]
    return " ".join(shlex.quote(item) for item in cmd)


def tunnel_unit_text(
    *,
    ssh_target: str,
    local_bind: str,
    local_port: int,
    remote_host: str,
    remote_port: int,
    key_path: Path,
    known_hosts: Path,
) -> str:
    return f"""[Unit]
Description=BlockDAG native reference RPC SSH tunnel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={tunnel_exec_start(ssh_target=ssh_target, local_bind=local_bind, local_port=local_port, remote_host=remote_host, remote_port=remote_port, key_path=key_path, known_hosts=known_hosts)}
Restart=always
RestartSec=10s
Nice=15
IOSchedulingClass=best-effort
IOSchedulingPriority=7

[Install]
WantedBy=default.target
"""


def write_tunnel_service(unit_path: Path, unit_text: str) -> None:
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(unit_text, encoding="utf-8")


def apply_validated_env(env_file: Path, reference_url: str, result: Mapping[str, Any], *, tunnel_target: str = "", unit_path: Path | None = None) -> None:
    keys = {
        "BDAG_NATIVE_REFERENCE_RPC_URL": reference_url,
        "BDAG_CHAIN_REFERENCE_RPC_URL": reference_url,
        "BDAG_IPFS_SEGMENT_REFERENCE_RPC_URL": reference_url,
        "BDAG_IPFS_RESTORE_CHAIN_REFERENCE_RPC_URL": reference_url,
        "BDAG_NATIVE_REFERENCE_RPC_SETUP_STATUS": "validated",
        "BDAG_NATIVE_REFERENCE_RPC_SETUP_AT": now_iso(),
        "BDAG_NATIVE_REFERENCE_RPC_REFERENCE_ORDER": str(result.get("reference_order") or ""),
        "BDAG_NATIVE_REFERENCE_RPC_REFERENCE_HASH": str(result.get("reference_hash") or ""),
        "BDAG_NATIVE_REFERENCE_RPC_REFERENCE_GENESIS_HASH": str(result.get("reference_genesis_hash") or ""),
    }
    if tunnel_target:
        keys["BDAG_NATIVE_REFERENCE_RPC_SSH_TARGET"] = tunnel_target
    if unit_path:
        keys["BDAG_NATIVE_REFERENCE_RPC_TUNNEL_SERVICE"] = str(unit_path)
    for key, value in keys.items():
        set_env_value(env_file, key, value)


def apply_failed_env(env_file: Path, reasons: list[str]) -> None:
    set_env_value(env_file, "BDAG_NATIVE_REFERENCE_RPC_SETUP_STATUS", "failed")
    set_env_value(env_file, "BDAG_NATIVE_REFERENCE_RPC_SETUP_AT", now_iso())
    set_env_value(env_file, "BDAG_NATIVE_REFERENCE_RPC_SETUP_REASON", "; ".join(reasons))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    runtime = ROOT / "ops/runtime/native-reference-rpc"
    parser.add_argument("--env-file", default=str(ROOT / "ops/runtime/ops.env"))
    parser.add_argument("--reference-rpc-url", default=os.environ.get("BDAG_NATIVE_REFERENCE_RPC_URL") or os.environ.get("BDAG_CHAIN_REFERENCE_RPC_URL") or "")
    parser.add_argument("--source-rpc-url", default=os.environ.get("BDAG_CHAIN_SOURCE_RPC_URL") or "http://127.0.0.1:38131")
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("BDAG_NATIVE_REFERENCE_RPC_TIMEOUT_SECONDS", "8")))
    parser.add_argument("--ssh-target", default=os.environ.get("BDAG_NATIVE_REFERENCE_RPC_SSH_TARGET", ""))
    parser.add_argument("--remote-rpc-host", default=os.environ.get("BDAG_NATIVE_REFERENCE_RPC_REMOTE_HOST", "127.0.0.1"))
    parser.add_argument("--remote-rpc-port", type=int, default=int(os.environ.get("BDAG_NATIVE_REFERENCE_RPC_REMOTE_PORT", "38131")))
    parser.add_argument("--local-bind", default=os.environ.get("BDAG_NATIVE_REFERENCE_RPC_LOCAL_BIND", "127.0.0.1"))
    parser.add_argument("--local-port", type=int, default=int(os.environ.get("BDAG_NATIVE_REFERENCE_RPC_LOCAL_PORT", "38141")))
    parser.add_argument("--key", default=os.environ.get("BDAG_NATIVE_REFERENCE_RPC_KEY_PATH", str(runtime / "id_ed25519")))
    parser.add_argument("--known-hosts", default=os.environ.get("BDAG_NATIVE_REFERENCE_RPC_KNOWN_HOSTS", str(runtime / "known_hosts")))
    parser.add_argument("--unit-path", default=os.environ.get("BDAG_NATIVE_REFERENCE_RPC_TUNNEL_UNIT", str(Path.home() / ".config/systemd/user/bdag-native-reference-rpc-tunnel.service")))
    parser.add_argument("--install-public-key", action="store_true")
    parser.add_argument("--print-public-key", action="store_true")
    parser.add_argument("--start-tunnel", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv or sys.argv[1:]))
    env_file = Path(args.env_file)
    key_path = Path(args.key)
    known_hosts = Path(args.known_hosts)
    unit_path = Path(args.unit_path)

    if not args.reference_rpc_url and args.ssh_target:
        args.reference_rpc_url = f"http://{args.local_bind}:{args.local_port}"

    if not args.reference_rpc_url and not args.ssh_target:
        payload = {"ok": True, "state": "skipped", "reasons": ["native_reference_rpc_not_configured"]}
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        return 1 if args.strict else 0

    if args.ssh_target:
        ensure_ssh_key(key_path)
        known_hosts.parent.mkdir(parents=True, exist_ok=True)
        if args.print_public_key:
            print(key_path.with_suffix(key_path.suffix + ".pub").read_text(encoding="utf-8").strip())
            return 0
        if args.install_public_key:
            subprocess.run(["ssh-copy-id", "-i", str(key_path.with_suffix(key_path.suffix + ".pub")), args.ssh_target], check=True)
        unit = tunnel_unit_text(
            ssh_target=args.ssh_target,
            local_bind=args.local_bind,
            local_port=args.local_port,
            remote_host=args.remote_rpc_host,
            remote_port=args.remote_rpc_port,
            key_path=key_path,
            known_hosts=known_hosts,
        )
        if not args.validate_only:
            write_tunnel_service(unit_path, unit)
            if args.start_tunnel:
                subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
                subprocess.run(["systemctl", "--user", "enable", "--now", unit_path.name], check=False)

    result = validate_native_reference_rpc(
        args.reference_rpc_url,
        source_url=args.source_rpc_url,
        timeout=args.timeout,
        env=os.environ,
    )
    if result.get("ok"):
        if not args.validate_only:
            apply_validated_env(
                env_file,
                str(result["reference_url"]),
                result,
                tunnel_target=args.ssh_target,
                unit_path=unit_path if args.ssh_target else None,
            )
    else:
        reasons = [str(item) for item in result.get("reasons", [])] or [str(result.get("state") or "failed")]
        if not args.validate_only:
            apply_failed_env(env_file, reasons)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") else (1 if args.strict else 0)


if __name__ == "__main__":
    raise SystemExit(main())

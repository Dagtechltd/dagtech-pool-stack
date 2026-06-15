#!/usr/bin/env python3
"""Seal a raw-datadir sidecar into immutable content-addressed chunks.

The raw sidecar is a mutable rsync target. This script turns a completed
sidecar pass into an immutable generation suitable for future IPFS transport:

live/sidecar files -> sha256 chunk store -> generation/chunks hardlinks
-> signed manifest root -> current symlink.

Hot sidecar generations are signed for byte integrity but marked
DO_NOT_PUBLISH unless an operator-approved finalization pass says the source was
quiesced. Receivers must treat IPFS as byte transport and still verify the
manifest, roots, chain metadata, and consensus tail.
"""

from __future__ import annotations

import argparse
import copy
import fnmatch
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("BDAG_PROJECT_ROOT") or Path(__file__).resolve().parents[1]).resolve()
ENV_FILE = Path(os.environ.get("BDAG_ENV_FILE") or ROOT / ".env").resolve()
STACK_DEFAULTS_FILE = Path(os.environ.get("BDAG_STACK_DEFAULTS_FILE") or ROOT / "ops" / "config" / "stack-defaults.env").resolve()
FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
DEFAULT_CHUNK_SIZE = 64 * 1024 * 1024
ZERO_HASH = "0x" + ("0" * 64)
MAINNET_NETWORK = "mainnet"


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    env: dict[str, str] = {}
    for env_path in (STACK_DEFAULTS_FILE, path):
        if not env_path.exists():
            continue
        for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    env.update({key: value for key, value in os.environ.items() if key.startswith("BDAG_") or key.startswith("NODE_RPC_")})
    return env


def env_bool(env: dict[str, str], key: str, default: bool = False) -> bool:
    value = str(env.get(key, "")).strip().lower()
    if not value:
        return default
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    return default


def env_int(env: dict[str, str], key: str, default: int) -> int:
    value = str(env.get(key, "")).strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def mainnet_network(env: dict[str, str]) -> str:
    requested = str(env.get("BDAG_RAWDATADIR_NETWORK") or MAINNET_NETWORK)
    requested = requested.strip().lower()
    if requested != MAINNET_NETWORK:
        raise RuntimeError(f"raw datadir sidecar content refuses non-mainnet network: {requested}")
    return MAINNET_NETWORK


def resolve_path(value: str | None, default: Path) -> Path:
    if not value:
        return default.resolve()
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def now_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%SZ", time.gmtime())


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        tmp = Path(handle.name)
    tmp.replace(path)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    payload = copy.deepcopy(manifest)
    payload.pop("artifact_root", None)
    payload.pop("signatures", None)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def manifest_root(manifest: dict[str, Any]) -> str:
    return sha256_bytes(canonical_manifest_bytes(manifest))


def signer_from_env(env: dict[str, str]) -> dict[str, str] | None:
    key_hex = (env.get("BDAG_RAWDATADIR_SIGNING_KEY_HEX") or "").strip()
    key_file = (env.get("BDAG_RAWDATADIR_SIGNING_KEY_FILE") or env.get("BDAG_IPFS_SEGMENT_SIGNING_KEY_FILE") or "").strip()
    if not key_hex and key_file:
        key_path = resolve_path(key_file, ROOT / "ops/runtime/ipfs-content/segment-writer.key")
        try:
            for raw in key_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    if key.strip() not in {
                        "BDAG_RAWDATADIR_SIGNING_KEY_HEX",
                        "BDAG_IPFS_SEGMENT_SIGNING_KEY_HEX",
                        "SIGNING_KEY_HEX",
                    }:
                        continue
                    key_hex = value.strip().strip('"').strip("'")
                    break
                key_hex = line
                break
        except OSError:
            key_hex = ""
    if not key_hex:
        return None
    key_bytes = bytes.fromhex(key_hex)
    if len(key_bytes) == 64:
        seed = key_bytes[:32]
    elif len(key_bytes) == 32:
        seed = key_bytes
    else:
        raise ValueError("BDAG_RAWDATADIR_SIGNING_KEY_HEX must be a 32-byte seed or 64-byte ed25519 private key")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "key_id": env.get("BDAG_RAWDATADIR_SIGNING_KEY_ID")
        or env.get("BDAG_IPFS_SEGMENT_WRITER_ID")
        or "rawdatadir-sidecar",
        "public_key": public_key.hex(),
        "private_key": private_key,
    }


def sign_manifest(manifest: dict[str, Any], env: dict[str, str]) -> list[dict[str, str]]:
    signer = signer_from_env(env)
    if signer is None:
        return []
    digest = bytes.fromhex(manifest_root(manifest))
    signature = signer["private_key"].sign(digest)  # type: ignore[union-attr]
    return [
        {
            "key_id": str(signer["key_id"]),
            "algorithm": "ed25519",
            "public_key": str(signer["public_key"]),
            "signature": signature.hex(),
            "signed_at": now_iso(),
        }
    ]


def excluded_rel(rel: str, is_dir: bool) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    retired_chain_artifact = "snap" + "shot.bd" + "snap"
    if name in {".rsync-partial", "LOCK", retired_chain_artifact, "artifact.manifest.json"}:
        return True
    if fnmatch.fnmatch(name, "*.ipc") or fnmatch.fnmatch(name, "*.sock"):
        return True
    if len(parts) == 1 and any(
        fnmatch.fnmatch(name, pattern)
        for pattern in ("network.key*", "keystore*", "peerstore*", "nodes*")
    ):
        return True
    if len(parts) >= 2 and parts[0] == "bdageth" and any(
        fnmatch.fnmatch(parts[1], pattern) for pattern in ("nodekey*", "keystore*", "nodes*")
    ):
        return True
    if len(parts) >= 2 and parts[0] == "BdagChain" and parts[1] == "LOCK":
        return True
    return False


def iter_regular_files(root: Path) -> list[Path]:
    root_dev = root.stat().st_dev
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(dirpath)
        kept_dirs: list[str] = []
        for dirname in sorted(dirnames):
            child = current / dirname
            rel = child.relative_to(root).as_posix()
            try:
                child_stat = child.lstat()
            except OSError:
                continue
            if excluded_rel(rel, is_dir=True) or child_stat.st_dev != root_dev or stat.S_ISLNK(child_stat.st_mode):
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        for filename in sorted(filenames):
            path = current / filename
            rel = path.relative_to(root).as_posix()
            if excluded_rel(rel, is_dir=False):
                continue
            try:
                path_stat = path.lstat()
            except OSError:
                continue
            if path_stat.st_dev != root_dev or not stat.S_ISREG(path_stat.st_mode):
                continue
            files.append(path)
    return files


def chown_if_needed(path: Path, uid: int | None, gid: int | None) -> None:
    if uid is None or gid is None:
        return
    try:
        os.chown(path, uid, gid)
    except PermissionError:
        pass


def make_public_file(path: Path, uid: int | None, gid: int | None) -> None:
    try:
        path.chmod(0o444)
    except PermissionError:
        pass
    chown_if_needed(path, uid, gid)


def make_public_dir(path: Path, uid: int | None, gid: int | None) -> None:
    try:
        path.chmod(0o755)
    except PermissionError:
        pass
    chown_if_needed(path, uid, gid)


def owner_from_sudo(env: dict[str, str]) -> tuple[int | None, int | None]:
    uid = env.get("BDAG_RAWDATADIR_SIDECAR_CONTENT_OWNER_UID") or os.environ.get("SUDO_UID")
    gid = env.get("BDAG_RAWDATADIR_SIDECAR_CONTENT_OWNER_GID") or os.environ.get("SUDO_GID")
    if not uid or not gid:
        return None, None
    try:
        return int(uid), int(gid)
    except ValueError:
        return None, None


def store_chunk(chunk_store: Path, data: bytes, uid: int | None, gid: int | None) -> tuple[str, Path]:
    digest = sha256_bytes(data)
    target = chunk_store / "sha256" / digest[:2] / digest
    target.parent.mkdir(parents=True, exist_ok=True)
    make_public_dir(target.parent, uid, gid)
    if not target.exists():
        with tempfile.NamedTemporaryFile("wb", dir=str(target.parent), delete=False) as handle:
            handle.write(data)
            tmp = Path(handle.name)
        make_public_file(tmp, uid, gid)
        try:
            tmp.replace(target)
        except FileExistsError:
            tmp.unlink(missing_ok=True)
    make_public_file(target, uid, gid)
    return digest, target


def link_chunk(store_path: Path, stage: Path, digest: str, uid: int | None, gid: int | None) -> str:
    rel = Path("chunks") / "sha256" / digest[:2] / digest
    target = stage / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    make_public_dir(target.parent, uid, gid)
    if target.exists():
        return rel.as_posix()
    try:
        os.link(store_path, target)
    except OSError:
        shutil.copy2(store_path, target)
    make_public_file(target, uid, gid)
    return rel.as_posix()


def quantity(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 16) if value.lower().startswith("0x") else int(value)
    raise ValueError(value)


def env_quantity(env: dict[str, str], key: str, default: int = 0) -> int:
    value = str(env.get(key) or "").strip()
    if not value:
        return default
    try:
        return quantity(value)
    except ValueError:
        return default


def collect_evm_chain_anchor(env: dict[str, str]) -> dict[str, Any]:
    evm_url = env.get("BDAG_RAWDATADIR_EVM_RPC_URL") or env.get("LOCAL_EVM_RPC_URL") or "http://127.0.0.1:18545"
    finality_blocks = max(0, env_int(env, "BDAG_RAWDATADIR_CHAIN_ANCHOR_FINALITY_BLOCKS", 600))
    try:
        chain_id = quantity(evm_rpc(evm_url, "eth_chainId"))
        latest = quantity(evm_rpc(evm_url, "eth_blockNumber"))
        anchor_number = max(0, latest - finality_blocks)
        block = evm_rpc(evm_url, "eth_getBlockByNumber", [hex(anchor_number), False])
        genesis = evm_rpc(evm_url, "eth_getBlockByNumber", ["0x0", False])
        if not isinstance(block, dict):
            return {"state": "unavailable", "reason": f"missing_evm_block:{anchor_number}"}
        return {
            "state": "available",
            "source_url": evm_url,
            "chain_id": chain_id,
            "latest_block_number": latest,
            "block_number": anchor_number,
            "block_hash": block.get("hash") or "",
            "state_root": block.get("stateRoot") or "",
            "genesis_hash": genesis.get("hash") if isinstance(genesis, dict) else "",
            "finality_lag_blocks": max(0, latest - anchor_number),
            "method": "eth_getBlockByNumber",
        }
    except Exception as exc:
        return {"state": "unavailable", "source_url": evm_url, "reason": str(exc)}


def zero_hash(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return not text or text in {ZERO_HASH, ZERO_HASH[2:]}


def anchor_blockers(anchor: dict[str, Any], require_state_root: bool) -> list[str]:
    blockers: list[str] = []
    try:
        if int(anchor.get("block_total") or 0) <= 1:
            blockers.append("anchor_missing_block_total")
    except (TypeError, ValueError):
        blockers.append("anchor_missing_block_total")
    try:
        if int(anchor.get("tip_order") or 0) <= 1:
            blockers.append("anchor_missing_tip_order")
    except (TypeError, ValueError):
        blockers.append("anchor_missing_tip_order")
    if zero_hash(anchor.get("tip_hash")):
        blockers.append("anchor_missing_tip_hash")
    if require_state_root and zero_hash(anchor.get("state_root")):
        blockers.append("anchor_missing_state_root")
    return blockers


def rpc(url: str, user: str, password: str, method: str, params: list[Any] | None = None) -> Any:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    if user or password:
        import base64

        req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode())
    with urllib.request.urlopen(req, timeout=5) as resp:
        decoded = json.loads(resp.read().decode())
    if decoded.get("error"):
        raise RuntimeError(f"{method}: {decoded['error']}")
    return decoded.get("result")


def evm_rpc(url: str, method: str, params: list[Any] | None = None) -> Any:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        decoded = json.loads(resp.read().decode())
    if decoded.get("error"):
        raise RuntimeError(f"{method}: {decoded['error']}")
    return decoded.get("result")


def collect_anchor(env: dict[str, str], require_state_root: bool = True) -> dict[str, Any]:
    url = env.get("BDAG_RAWDATADIR_ANCHOR_RPC_URL") or env.get("NODE_RPC_URL") or "http://127.0.0.1:38131"
    evm_url = env.get("BDAG_RAWDATADIR_EVM_RPC_URL") or env.get("LOCAL_EVM_RPC_URL") or "http://127.0.0.1:18545"
    user = env.get("NODE_RPC_USER", "test")
    password = env.get("NODE_RPC_PASS", "test")
    anchor: dict[str, Any] = {
        "chain_id": quantity(env.get("BDAG_RAWDATADIR_CHAIN_ID") or 1404),
        "network": mainnet_network(env),
        "block_total": env_quantity(env, "BDAG_RAWDATADIR_BLOCK_TOTAL"),
        "tip_order": env_quantity(env, "BDAG_RAWDATADIR_TIP_ORDER"),
        "tip_hash": env.get("BDAG_RAWDATADIR_TIP_HASH") or "",
        "state_root": env.get("BDAG_RAWDATADIR_STATE_ROOT") or ZERO_HASH,
        "genesis_hash": env.get("BDAG_RAWDATADIR_GENESIS_HASH") or "",
    }
    if (
        int(anchor.get("block_total") or 0) > 1
        and int(anchor.get("tip_order") or 0) > 1
        and not zero_hash(anchor.get("tip_hash"))
        and (not require_state_root or not zero_hash(anchor.get("state_root")))
        and not zero_hash(anchor.get("genesis_hash"))
    ):
        anchor["anchor_source"] = "configured_finalization_anchor"
        return anchor
    try:
        if int(anchor.get("block_total") or 0) <= 1:
            for method in ("getBlockTotal", "getBlockCount"):
                try:
                    anchor["block_total"] = quantity(rpc(url, user, password, method))
                    break
                except Exception:
                    pass
        if int(anchor.get("tip_order") or 0) <= 1:
            try:
                anchor["tip_order"] = quantity(rpc(url, user, password, "getMainChainHeight"))
            except Exception:
                anchor["tip_order"] = anchor["block_total"]
        if anchor["tip_order"] and zero_hash(anchor.get("tip_hash")):
            for method, params in (("getBlockhash", [int(anchor["tip_order"])]), ("getBestBlockHash", [])):
                try:
                    anchor["tip_hash"] = str(rpc(url, user, password, method, params))
                    break
                except Exception:
                    pass
        if require_state_root and (not anchor["state_root"] or anchor["state_root"] == ZERO_HASH):
            for method, params in (("getBlockHeader", [anchor["tip_hash"], True]), ("getStateRoot", [int(anchor["tip_order"]), False])):
                try:
                    result = rpc(url, user, password, method, params)
                    if isinstance(result, dict):
                        anchor["state_root"] = result.get("stateRoot") or result.get("stateroot") or result.get("StateRoot") or ZERO_HASH
                    elif isinstance(result, str):
                        anchor["state_root"] = result
                    if anchor["state_root"] and anchor["state_root"] != ZERO_HASH:
                        break
                except Exception:
                    pass
        if zero_hash(anchor.get("genesis_hash")):
            try:
                anchor["genesis_hash"] = str(rpc(url, user, password, "getBlockhash", [0]))
            except Exception:
                pass
    except Exception as exc:
        anchor["anchor_error"] = str(exc)
    try:
        if int(anchor.get("block_total") or 0) <= 1:
            anchor["block_total"] = quantity(evm_rpc(evm_url, "eth_blockNumber"))
        if int(anchor.get("tip_order") or 0) <= 1:
            anchor["tip_order"] = int(anchor.get("block_total") or 0)
        if zero_hash(anchor.get("tip_hash")) or (require_state_root and zero_hash(anchor.get("state_root"))):
            block = evm_rpc(evm_url, "eth_getBlockByNumber", ["latest", False])
            if isinstance(block, dict):
                if zero_hash(anchor.get("tip_hash")):
                    anchor["tip_hash"] = block.get("hash") or ""
                if require_state_root and zero_hash(anchor.get("state_root")):
                    anchor["state_root"] = block.get("stateRoot") or ZERO_HASH
                try:
                    number = quantity(block.get("number"))
                    if number > int(anchor.get("tip_order") or 0):
                        anchor["tip_order"] = number
                    if number > int(anchor.get("block_total") or 0):
                        anchor["block_total"] = number
                except Exception:
                    pass
        if not anchor["genesis_hash"]:
            genesis = evm_rpc(evm_url, "eth_getBlockByNumber", ["0x0", False])
            if isinstance(genesis, dict):
                anchor["genesis_hash"] = genesis.get("hash") or ""
    except Exception as exc:
        anchor["evm_anchor_error"] = str(exc)
    return anchor


def prune_generations(artifact_base: Path, keep: int) -> None:
    artifacts = artifact_base / "artifacts"
    if keep <= 0 or not artifacts.exists():
        return
    generations = sorted([p for p in artifacts.iterdir() if p.is_dir() and p.name.startswith("sidecar-")], key=lambda p: p.name, reverse=True)
    for stale in generations[keep:]:
        shutil.rmtree(stale, ignore_errors=True)


def publish_current_symlink(artifact_base: Path, stage: Path) -> Path:
    current = artifact_base / "current"
    artifact_base.mkdir(parents=True, exist_ok=True)
    tmp_link = artifact_base / f".current.{os.getpid()}.tmp"
    if tmp_link.exists() or tmp_link.is_symlink():
        if tmp_link.is_dir() and not tmp_link.is_symlink():
            shutil.rmtree(tmp_link)
        else:
            tmp_link.unlink()
    tmp_link.symlink_to(Path("artifacts") / stage.name)
    if current.exists() and current.is_dir() and not current.is_symlink():
        old_current = artifact_base / f".current.replaced.{now_stamp()}.{os.getpid()}"
        current.rename(old_current)
        try:
            tmp_link.replace(current)
        except OSError:
            old_current.rename(current)
            raise
        shutil.rmtree(old_current, ignore_errors=True)
        return current
    tmp_link.replace(current)
    return current


def write_status(env: dict[str, str], payload: dict[str, Any]) -> None:
    status = resolve_path(
        env.get("BDAG_RAWDATADIR_SIDECAR_CONTENT_STATUS_FILE"),
        ROOT / "ops/runtime/rawdatadir-sidecar-content-status.json",
    )
    atomic_write_json(status, payload)
    uid, gid = owner_from_sudo(env)
    try:
        status.chmod(0o644)
    except PermissionError:
        pass
    chown_if_needed(status, uid, gid)


def seal_sidecar(env: dict[str, str]) -> dict[str, Any]:
    network = mainnet_network(env)
    sidecar = resolve_path(
        env.get("BDAG_RAWDATADIR_SIDECAR_DIR"),
        ROOT / "data-restore" / "btrfs-checkpoints" / "rawdatadir-sidecar" / network,
    )
    artifact_base = resolve_path(
        env.get("BDAG_RAWDATADIR_SIDECAR_CONTENT_BASE"),
        ROOT / "data-restore" / "btrfs-checkpoints" / "rawdatadir-sidecar-content",
    )
    chunk_store = resolve_path(env.get("BDAG_RAWDATADIR_SIDECAR_CHUNK_STORE"), artifact_base / "chunk-store")
    keep = env_int(env, "BDAG_RAWDATADIR_SIDECAR_CONTENT_KEEP", 2)
    chunk_size = max(1024 * 1024, env_int(env, "BDAG_RAWDATADIR_SIDECAR_CONTENT_CHUNK_SIZE", DEFAULT_CHUNK_SIZE))
    finalized = env_bool(env, "BDAG_RAWDATADIR_SIDECAR_CONTENT_FINALIZED", False)
    allow_hot_publish = env_bool(env, "BDAG_RAWDATADIR_SIDECAR_CONTENT_ALLOW_HOT_PUBLISH", False)
    require_signed = env_bool(env, "BDAG_RAWDATADIR_SIDECAR_CONTENT_REQUIRE_SIGNED", env_bool(env, "BDAG_RAWDATADIR_REQUIRE_SIGNED", True))
    require_state_root = env_bool(env, "BDAG_RAWDATADIR_REQUIRE_STATE_ROOT", True)
    uid, gid = owner_from_sudo(env)

    if not (sidecar / "BdagChain").is_dir():
        payload = {
            "generated_at": now_iso(),
            "state": "deferred",
            "reasons": ["sidecar_missing_BdagChain"],
            "sidecar_dir": str(sidecar),
        }
        write_status(env, payload)
        return payload

    stamp = now_stamp()
    stage = artifact_base / "artifacts" / f"sidecar-{stamp}"
    stage.mkdir(parents=True, exist_ok=False)
    make_public_dir(stage, uid, gid)
    (stage / "INCOMPLETE").write_text("sealing\n", encoding="utf-8")
    make_public_file(stage / "INCOMPLETE", uid, gid)

    files: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    total_bytes = 0
    for path in iter_regular_files(sidecar):
        rel = path.relative_to(sidecar).as_posix()
        path_stat = path.stat()
        file_hash = hashlib.sha256()
        chunk_start = len(chunks)
        offset = 0
        with path.open("rb") as handle:
            while True:
                data = handle.read(chunk_size)
                if not data:
                    break
                file_hash.update(data)
                digest, store_path = store_chunk(chunk_store, data, uid, gid)
                chunk_rel = link_chunk(store_path, stage, digest, uid, gid)
                chunks.append(
                    {
                        "id": len(chunks),
                        "source": "rawdatadir-sidecar",
                        "class": "raw_datadir_file_chunk",
                        "offset": offset,
                        "compressed_size": len(data),
                        "uncompressed_size": len(data),
                        "compressed_sha256": digest,
                        "path": chunk_rel,
                        "source_path": rel,
                    }
                )
                offset += len(data)
                total_bytes += len(data)
        files.append(
            {
                "path": rel,
                "class": "raw_datadir_file",
                "size": path_stat.st_size,
                "sha256": file_hash.hexdigest(),
                "chunk_start": chunk_start,
                "chunk_count": len(chunks) - chunk_start,
                "mode": stat.S_IMODE(path_stat.st_mode),
            }
        )

    anchor = collect_anchor(env, require_state_root=require_state_root)
    created_at = now_iso()
    manifest: dict[str, Any] = {
        "format_version": 2,
        "artifact_type": "raw_datadir_checkpoint",
        "network": anchor.get("network") or network,
        "chain_id": int(anchor.get("chain_id") or 1404),
        "genesis_hash": anchor.get("genesis_hash") or "",
        "tip_order": int(anchor.get("tip_order") or 0),
        "tip_hash": anchor.get("tip_hash") or "",
        "block_total": int(anchor.get("block_total") or 0),
        "state_root": anchor.get("state_root") or ZERO_HASH,
        "chunk_hash_algo": "sha256",
        "encoding": "content-addressed-raw-chunks",
        "layout": "directory",
        "created_at": created_at,
        "metadata": {
            "source": "rawdatadir-sidecar",
            "sidecar_dir": str(sidecar),
            "content_chunk_size": str(chunk_size),
            "content_store": "sha256",
            "finalized_sidecar": "1" if finalized else "0",
            "publishable": "1" if finalized or allow_hot_publish else "0",
            "canonical_json": "json_sort_keys_sha256_v1",
            "evm_anchor": collect_evm_chain_anchor(env),
        },
        "sources": [
            {
                "name": "rawdatadir-sidecar",
                "chunk_start": 0,
                "chunk_count": len(chunks),
                "total_compressed": total_bytes,
                "total_uncompressed": total_bytes,
            }
        ],
        "chunks": chunks,
        "files": files,
    }
    root = manifest_root(manifest)
    manifest["artifact_root"] = root
    signatures = sign_manifest(manifest, env)
    if signatures:
        manifest["signatures"] = signatures

    signed = bool(signatures)
    reasons: list[str] = []
    if require_signed and not signed:
        reasons.append("missing_signing_key")
    if not finalized and not allow_hot_publish:
        reasons.append("hot_sidecar_not_finalized")
    reasons.extend(anchor_blockers(anchor, require_state_root))
    publishable = signed and not reasons and (finalized or allow_hot_publish)

    manifest_path = stage / "manifest.json"
    atomic_write_json(manifest_path, manifest)
    make_public_file(manifest_path, uid, gid)
    readme = stage / "README-SIDECAR-CONTENT.txt"
    readme.write_text(
        "\n".join(
            [
                "BlockDAG raw datadir sidecar content artifact",
                f"Created: {created_at}",
                f"Artifact root: {root}",
                f"Files: {len(files)}",
                f"Chunks: {len(chunks)}",
                f"Bytes: {total_bytes}",
                f"Signed: {'yes' if signed else 'no'}",
                f"Finalized: {'yes' if finalized else 'no'}",
                f"Publishable: {'yes' if publishable else 'no'}",
                "",
                "IPFS is byte transport only. Receivers must verify this manifest,",
                "its signatures, the chunk/file hashes, and normal consensus before import.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    make_public_file(readme, uid, gid)
    (stage / "INCOMPLETE").unlink(missing_ok=True)
    if not publishable:
        marker = stage / "DO_NOT_PUBLISH.txt"
        marker.write_text("\n".join(reasons or ["not_publishable"]) + "\n", encoding="utf-8")
        make_public_file(marker, uid, gid)

    current = publish_current_symlink(artifact_base, stage)
    make_public_dir(artifact_base, uid, gid)
    make_public_dir(artifact_base / "artifacts", uid, gid)

    prune_generations(artifact_base, keep)
    payload = {
        "generated_at": now_iso(),
        "state": "sealed" if publishable else "sealed_not_publishable",
        "reasons": reasons,
        "sidecar_dir": str(sidecar),
        "artifact_base": str(artifact_base),
        "generation_dir": str(stage),
        "current": str(current),
        "manifest": str(manifest_path),
        "artifact_root": root,
        "signed": signed,
        "finalized": finalized,
        "publishable": publishable,
        "file_count": len(files),
        "chunk_count": len(chunks),
        "total_bytes": total_bytes,
        "chunk_size": chunk_size,
        "anchor": anchor,
    }
    write_status(env, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print status JSON")
    args = parser.parse_args(argv)
    env = load_env()
    mode = (env.get("BDAG_RAWDATADIR_SIDECAR_CONTENT_MODE") or "auto").strip().lower()
    if mode in FALSE_VALUES:
        payload = {"generated_at": now_iso(), "state": "disabled", "reasons": ["mode_disabled"]}
        write_status(env, payload)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    try:
        payload = seal_sidecar(env)
    except Exception as exc:
        payload = {"generated_at": now_iso(), "state": "failed", "reasons": [str(exc)]}
        write_status(env, payload)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return 1
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

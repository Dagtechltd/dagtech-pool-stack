#!/usr/bin/env python3
"""Lazy IPFS publisher for finalized BlockDAG raw-datadir sidecar content.

This process is deliberately not a snapshot builder. It only advertises an
already-finalized, signed raw-datadir generation after resource pressure clears.
IPFS is treated as an untrusted byte distribution plane; the signed
sidecar manifest and normal consensus validation remain authoritative.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("BDAG_PROJECT_ROOT") or Path(__file__).resolve().parents[1]).resolve()
ENV_FILE = Path(os.environ.get("BDAG_ENV_FILE") or ROOT / ".env")
STACK_DEFAULTS_FILE = Path(os.environ.get("BDAG_STACK_DEFAULTS_FILE") or ROOT / "ops" / "config" / "stack-defaults.env")
OPS_DIR = ROOT / "ops"
RESTORE_MODULE_PATH = OPS_DIR / "restore-rawdatadir-segment-artifact.py"

FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}


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
            value = value.strip().strip('"').strip("'")
            env[key.strip()] = value
    env.update({key: value for key, value in os.environ.items() if key.startswith("BDAG_") or key in {"IPFS_PATH"}})
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


def resolve_path(value: str | None, default: Path) -> Path:
    if not value:
        return default.resolve()
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        tmp = Path(handle.name)
    tmp.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_command(command: list[str], timeout: int, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    child_env = os.environ.copy()
    child_env.update(env)
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=child_env,
        check=False,
    )


def status_base(env: dict[str, str]) -> Path:
    return resolve_path(env.get("BDAG_IPFS_CONTENT_STATUS_FILE"), ROOT / "ops/runtime/ipfs-content-sidecar-status.json")


def status_payload(state: str, env: dict[str, str], **extra: Any) -> dict[str, Any]:
    payload = {
        "generated_at": now_iso(),
        "state": state,
        "mode": env.get("BDAG_IPFS_CONTENT_SIDECAR_MODE", "auto"),
        "project_root": str(ROOT),
        "trust_model": "ipfs_is_untrusted_transport_manifest_and_consensus_are_authoritative",
    }
    payload.update(extra)
    return payload


def write_status(env: dict[str, str], state: str, **extra: Any) -> dict[str, Any]:
    payload = status_payload(state, env, **extra)
    atomic_write_json(status_base(env), payload)
    return payload


def background_maintenance_allowed(env: dict[str, str]) -> dict[str, Any]:
    if env_bool(env, "BDAG_IPFS_CONTENT_SKIP_MAINTENANCE_DECISION", False):
        return {"allowed": True, "reasons": [], "skipped": True}
    try:
        sys.path.insert(0, str(OPS_DIR))
        from pool_ops import background_maintenance_decision, collect_status_cached  # type: ignore

        return background_maintenance_decision(
            "ipfs_content_sidecar",
            collect_status_cached(include_logs=False),
        )
    except Exception as exc:  # pragma: no cover - exercised by integration use.
        return {"allowed": False, "reasons": [f"maintenance gate unavailable: {exc}"], "error": str(exc)}


def artifact_paths(env: dict[str, str]) -> tuple[Path, Path]:
    sidecar_content_base = resolve_path(
        env.get("BDAG_RAWDATADIR_SIDECAR_CONTENT_BASE") or env.get("BDAG_RAWDATADIR_ARTIFACT_BASE"),
        ROOT / "data-restore" / "btrfs-checkpoints" / "rawdatadir-sidecar-content",
    )
    artifact_dir = resolve_path(env.get("BDAG_IPFS_CONTENT_ARTIFACT_DIR"), sidecar_content_base / "current")
    manifest = resolve_path(env.get("BDAG_IPFS_CONTENT_ARTIFACT_MANIFEST"), artifact_dir / "manifest.json")
    return artifact_dir, manifest


def rawdatadir_index_path(env: dict[str, str]) -> Path:
    """Return the raw checkpoint index path.

    Chain-order segment indexes and raw-datadir checkpoint indexes have
    different schemas and restore semantics. Keep their local files separate so
    one writer cannot overwrite the other writer's source of truth.
    """

    return resolve_path(
        env.get("BDAG_IPFS_RAWDATADIR_CONTENT_INDEX_PATH") or env.get("BDAG_IPFS_CONTENT_INDEX_PATH"),
        ROOT / "ops/runtime/ipfs-content/rawdatadir-content-index.json",
    )


def env_int(env: dict[str, str], key: str, default: int) -> int:
    value = str(env.get(key, "")).strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def rawdatadir_trusted_signers(env: dict[str, str]) -> str:
    return str(
        env.get("BDAG_RAWDATADIR_TRUSTED_SIGNERS")
        or env.get("BDAG_IPFS_RAWDATADIR_TRUSTED_SIGNERS")
        or env.get("BDAG_IPFS_SEGMENT_TRUSTED_SIGNERS")
        or env.get("BDAG_IPFS_SEGMENT_WRITER_ROSTER")
        or ""
    ).strip()


def load_restore_module() -> Any:
    spec = importlib.util.spec_from_file_location("restore_rawdatadir_segment_artifact", RESTORE_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load rawdatadir restore verifier from {RESTORE_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_rawdatadir_manifest(
    artifact_dir: Path,
    manifest: dict[str, Any],
    env: dict[str, str],
) -> dict[str, Any]:
    verifier = load_restore_module()
    args = argparse.Namespace(
        local_artifact_dir=str(artifact_dir),
        remote_artifact_dir=None,
        remote=None,
        ssh_control_socket=None,
        network="mainnet",
        min_tip_order=env_int(env, "BDAG_IPFS_RAWDATADIR_CONTENT_MIN_TIP_ORDER", 0),
        allow_unsigned=env_bool(env, "BDAG_IPFS_CONTENT_ALLOW_UNSIGNED_ARTIFACT", False),
        trusted_signers=rawdatadir_trusted_signers(env),
        require_trusted_signer=env_bool(env, "BDAG_RAWDATADIR_REQUIRE_TRUSTED_SIGNER", True),
        reference_evm_rpc_url=str(
            env.get("BDAG_RAWDATADIR_CHAIN_ANCHOR_REFERENCE_EVM_URL")
            or env.get("BDAG_IPFS_RAWDATADIR_RESTORE_REFERENCE_EVM_RPC_URL")
            or env.get("BDAG_PUBLIC_EVM_RPC_URL")
            or ""
        ),
        require_chain_anchor=env_bool(env, "BDAG_RAWDATADIR_REQUIRE_CHAIN_ANCHOR", True),
        chain_anchor_timeout=env_int(env, "BDAG_RAWDATADIR_CHAIN_ANCHOR_TIMEOUT", 8),
        chain_anchor_finality_blocks=env_int(env, "BDAG_RAWDATADIR_CHAIN_ANCHOR_FINALITY_BLOCKS", 600),
        allow_test_unsafe_metadata=env_bool(env, "BDAG_RAWDATADIR_ALLOW_TEST_UNSAFE_METADATA", False),
    )
    verification = verifier.validate_manifest(manifest, args)
    return {
        "state": "verified",
        "artifact_root": verification.get("artifact_root"),
        "verified_signature_key_ids": verification.get("verified_signature_key_ids") or [],
        "trusted_signers_configured": bool(rawdatadir_trusted_signers(env)),
        "require_trusted_signer": bool(args.require_trusted_signer),
        "chain_anchor": verification.get("chain_anchor"),
    }


def verification_blocker(reason: str) -> str:
    lowered = reason.lower()
    if "trusted raw-datadir signer roster is empty" in lowered:
        return "trusted_rawdatadir_signers_missing"
    if "no valid ed25519 signature from a trusted" in lowered:
        return "manifest_signature_untrusted"
    if "no signature material" in lowered or "missing artifact_root" in lowered:
        return "manifest_unsigned"
    if "artifact metadata is not publishable" in lowered:
        return f"manifest_not_publishable:{reason}"
    if "chain anchor" in lowered or "evm " in lowered:
        return f"manifest_chain_anchor_untrusted:{reason}"
    return f"manifest_unverified:{reason}"


def artifact_publish_blockers(artifact_dir: Path, manifest_path: Path, manifest: dict[str, Any], env: dict[str, str]) -> list[str]:
    blockers: list[str] = []
    if not artifact_dir.exists():
        blockers.append("artifact_dir_missing")
    if not manifest_path.exists():
        blockers.append("manifest_missing")
    if manifest:
        network = str(manifest.get("network") or "").strip()
        if network.lower() != "mainnet":
            blockers.append(f"manifest_non_mainnet_network:{network or 'missing'}")
    for marker_dir in (artifact_dir, artifact_dir.parent):
        if (marker_dir / "DO_NOT_PUBLISH.txt").exists() or (marker_dir / "DO_NOT_PUBLISH").exists():
            blockers.append(f"do_not_publish_marker:{marker_dir}")
            break
    artifact_type = str(manifest.get("artifact_type") or manifest.get("type") or "")
    if artifact_type and artifact_type != "raw_datadir_checkpoint":
        blockers.append(f"unsupported_artifact_type:{artifact_type}")
    if manifest and not manifest.get("signatures") and not env_bool(env, "BDAG_IPFS_CONTENT_ALLOW_UNSIGNED_ARTIFACT", False):
        blockers.append("manifest_unsigned")
    if manifest:
        try:
            verify_rawdatadir_manifest(artifact_dir, manifest, env)
        except Exception as exc:  # noqa: BLE001 - reason is surfaced as a publish blocker.
            blocker = verification_blocker(str(exc))
            if blocker not in blockers:
                blockers.append(blocker)
    elif not env_bool(env, "BDAG_IPFS_CONTENT_ALLOW_UNSIGNED_ARTIFACT", False):
        blockers.append("manifest_unsigned")
    return blockers


def waiting_state_for_blockers(blockers: list[str]) -> str:
    if any(item.startswith("do_not_publish_marker:") for item in blockers):
        return "waiting_for_safe_artifact"
    if any(item in {"manifest_unsigned", "trusted_rawdatadir_signers_missing", "manifest_signature_untrusted"} for item in blockers):
        return "waiting_for_signed_artifact"
    return "waiting_for_artifact"


def load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def ipfs_binary(env: dict[str, str]) -> str:
    return env.get("BDAG_IPFS_BINARY") or "ipfs"


def parse_cid(stdout: str) -> str:
    lines = [line.strip().split()[0] for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("ipfs add returned no CID")
    return lines[-1]


def ipfs_add(path: Path, env: dict[str, str], timeout_key: str) -> str:
    # Keep the ipfs add invocation explicit and deterministic enough that all
    # cooperating nodes derive the same CID for the same finalized directory.
    add_args = shlex.split(
        env.get(
            "BDAG_IPFS_CONTENT_ADD_ARGS",
            "--recursive --cid-version=1 --raw-leaves --pin=true --quieter",
        )
    )
    command = [ipfs_binary(env), "add", *add_args, str(path)]
    result = run_command(command, int(env.get(timeout_key, "3600")), env)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "ipfs add failed").strip())
    return parse_cid(result.stdout)


def ipfs_pin_present(cid: str, env: dict[str, str]) -> bool:
    result = run_command(
        [ipfs_binary(env), "pin", "ls", "--type=recursive", cid],
        int(env.get("BDAG_IPFS_CONTENT_PIN_CHECK_TIMEOUT", "60")),
        env,
    )
    return result.returncode == 0


def build_latest_index(
    artifact_cid: str,
    manifest: dict[str, Any],
    manifest_path: Path,
    manifest_sha256: str,
) -> dict[str, Any]:
    return {
        "document_type": "bdag_ipfs_content_index_v1",
        "generated_at": now_iso(),
        "artifact_cid": artifact_cid,
        "artifact_manifest_path": str(manifest_path),
        "artifact_manifest_sha256": manifest_sha256,
        "artifact_type": manifest.get("artifact_type") or manifest.get("type") or "raw_datadir_checkpoint",
        "network": manifest.get("network"),
        "chain_id": manifest.get("chain_id"),
        "genesis": manifest.get("genesis") or manifest.get("genesis_hash"),
        "tip_hash": manifest.get("tip_hash") or manifest.get("tip"),
        "tip_order": manifest.get("tip_order") or manifest.get("block_total") or manifest.get("main_order"),
        "state_root": manifest.get("state_root") or manifest.get("evm_state_root"),
        "manifest_signatures": manifest.get("signatures") or [],
        "trust_model": "CID locates bytes only; receivers must verify manifest signatures, roots, and consensus before import.",
    }


def publish_ipns_enabled(env: dict[str, str]) -> bool:
    value = str(
        env.get("BDAG_IPFS_RAWDATADIR_CONTENT_PUBLISH_IPNS")
        or env.get("BDAG_IPFS_CONTENT_PUBLISH_IPNS")
        or ""
    ).strip().lower()
    if value in FALSE_VALUES:
        return False
    if value in TRUE_VALUES:
        return True
    if value == "auto":
        return bool(
            str(
                env.get("BDAG_IPFS_RAWDATADIR_CONTENT_IPNS_KEY")
                or env.get("BDAG_IPFS_CONTENT_IPNS_KEY")
                or ""
            ).strip()
        )
    return False


def publish_ipns(index_cid: str, env: dict[str, str]) -> dict[str, Any] | None:
    if not publish_ipns_enabled(env):
        return None
    key = env.get("BDAG_IPFS_RAWDATADIR_CONTENT_IPNS_KEY") or env.get("BDAG_IPFS_CONTENT_IPNS_KEY")
    command = [ipfs_binary(env), "name", "publish"]
    if key:
        command.extend(["--key", key])
    ttl = env.get("BDAG_IPFS_CONTENT_IPNS_TTL")
    if ttl:
        command.extend(["--ttl", ttl])
    lifetime = env.get("BDAG_IPFS_CONTENT_IPNS_LIFETIME")
    if lifetime:
        command.extend(["--lifetime", lifetime])
    command.append(f"/ipfs/{index_cid}")
    result = run_command(command, int(env.get("BDAG_IPFS_CONTENT_IPNS_TIMEOUT", "300")), env)
    return {
        "command": command[:3] + ["..."],
        "ok": result.returncode == 0,
        "stdout": result.stdout.strip()[-1000:],
        "stderr": result.stderr.strip()[-1000:],
    }


def load_existing_index(index_path: Path) -> dict[str, Any]:
    if not index_path.exists():
        return {}
    try:
        return json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def current_index_cid(existing: dict[str, Any], env: dict[str, str]) -> str:
    for key in ("index_cid", "current_rawdatadir_index_cid", "rawdatadir_latest_index_cid"):
        value = str(existing.get(key) or "").strip()
        if value:
            return value
    discovery_path = resolve_path(env.get("BDAG_IPFS_CONTENT_DISCOVERY_FILE"), ROOT / "ops/ipfs-content-discovery.json")
    if discovery_path.exists():
        try:
            discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
            value = str(
                discovery.get("current_rawdatadir_index_cid")
                or discovery.get("rawdatadir_latest_index_cid")
                or discovery.get("current_content_index_cid")
                or ""
            ).strip()
            if value:
                return value
        except json.JSONDecodeError:
            pass
    return str(env.get("BDAG_IPFS_RAWDATADIR_CONTENT_DEFAULT_INDEX_CID") or "").strip()


def republish_current_ipns(existing: dict[str, Any], env: dict[str, str]) -> tuple[str, dict[str, Any] | None]:
    index_cid = current_index_cid(existing, env)
    if not index_cid or not publish_ipns_enabled(env):
        return index_cid, None
    if not ipfs_pin_present(index_cid, env):
        return index_cid, {
            "ok": False,
            "skipped": "index_cid_not_recursively_pinned",
            "index_cid": index_cid,
        }
    return index_cid, publish_ipns(index_cid, env)


def update_discovery(raw_index_cid: str, artifact_cid: str, manifest: dict[str, Any], env: dict[str, str]) -> None:
    path = resolve_path(env.get("BDAG_IPFS_CONTENT_DISCOVERY_FILE"), ROOT / "ops/ipfs-content-discovery.json")
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except json.JSONDecodeError:
            data = {}
    if not data:
        data = {
            "document_type": "bdag_ipfs_content_discovery_v1",
            "network": "mainnet",
        }
    data["updated_at"] = now_iso()
    data["rawdatadir_latest_index_cid"] = raw_index_cid
    data["rawdatadir_latest_index_uri"] = f"ipfs://{raw_index_cid}"
    data["current_rawdatadir_index_cid"] = raw_index_cid
    data["current_rawdatadir_artifact_cid"] = artifact_cid
    data["current_rawdatadir_content"] = {
        "document_type": "bdag_ipfs_content_index_v1",
        "artifact_type": manifest.get("artifact_type") or manifest.get("type") or "raw_datadir_checkpoint",
        "network": manifest.get("network"),
        "chain_id": manifest.get("chain_id"),
        "tip_order": manifest.get("tip_order") or manifest.get("block_total") or manifest.get("main_order"),
        "tip_hash": manifest.get("tip_hash") or manifest.get("tip"),
        "state_root": manifest.get("state_root") or manifest.get("evm_state_root"),
        "artifact_cid": artifact_cid,
        "index_cid": raw_index_cid,
        "updated_at": now_iso(),
    }
    data["rawdatadir_trust_model"] = (
        "Raw checkpoint CIDs are restore candidates only. Consumers must verify the "
        "content-index schema, manifest root, trusted Ed25519 signature, chunk/file "
        "hashes, mainnet metadata, and normal chain consensus before import."
    )
    atomic_write_json(path, data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="evaluate gates without calling ipfs")
    parser.add_argument("--json", action="store_true", help="print final status JSON")
    args = parser.parse_args(argv)

    env = load_env()
    mode = (env.get("BDAG_IPFS_CONTENT_SIDECAR_MODE") or "auto").strip().lower()
    if mode in FALSE_VALUES:
        payload = write_status(env, "disabled", reasons=["mode_disabled"])
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    decision = background_maintenance_allowed(env)
    if not decision.get("allowed", False):
        payload = write_status(env, "deferred", reasons=decision.get("reasons") or ["background_maintenance_denied"], maintenance_decision=decision)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    index_path = rawdatadir_index_path(env)
    existing = load_existing_index(index_path)
    artifact_dir, manifest_path = artifact_paths(env)
    manifest: dict[str, Any] = {}
    if manifest_path.exists():
        manifest = load_manifest(manifest_path)
    blockers = artifact_publish_blockers(artifact_dir, manifest_path, manifest, env)
    if blockers:
        index_cid = ""
        ipns = None
        if env_bool(env, "BDAG_IPFS_CONTENT_REPUBLISH_IPNS_WHILE_WAITING", True):
            index_cid, ipns = republish_current_ipns(existing, env)
        payload = write_status(
            env,
            waiting_state_for_blockers(blockers),
            reasons=blockers,
            action="waiting_republish_current_ipns" if ipns else "waiting",
            index_cid=index_cid,
            ipns=ipns,
            retry_policy="timer_will_retry_after_pressure_or_artifact_state_changes",
            artifact_dir=str(artifact_dir),
            artifact_manifest=str(manifest_path),
        )
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    manifest_verification = verify_rawdatadir_manifest(artifact_dir, manifest, env)
    manifest_sha = sha256_file(manifest_path)
    existing_cid = str(existing.get("artifact_cid") or "")
    if existing.get("artifact_manifest_sha256") == manifest_sha and existing_cid and ipfs_pin_present(existing_cid, env):
        index_cid, ipns = republish_current_ipns(existing, env)
        payload = write_status(
            env,
            "published",
            action="already_pinned",
            artifact_cid=existing_cid,
            index_cid=index_cid,
            ipns=ipns,
            artifact_manifest_sha256=manifest_sha,
            manifest_verification=manifest_verification,
            artifact_dir=str(artifact_dir),
            artifact_manifest=str(manifest_path),
        )
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.dry_run:
        payload = write_status(
            env,
            "ready",
            action="dry_run",
            artifact_manifest_sha256=manifest_sha,
            manifest_verification=manifest_verification,
            artifact_dir=str(artifact_dir),
            artifact_manifest=str(manifest_path),
        )
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    try:
        artifact_cid = ipfs_add(artifact_dir, env, "BDAG_IPFS_CONTENT_ADD_TIMEOUT")
        if not ipfs_pin_present(artifact_cid, env):
            raise RuntimeError(f"artifact CID {artifact_cid} was not present as a recursive pin after add")
        index = build_latest_index(artifact_cid, manifest, manifest_path, manifest_sha)
        index["manifest_verification"] = manifest_verification
        atomic_write_json(index_path, index)
        index_cid = ipfs_add(index_path, env, "BDAG_IPFS_CONTENT_INDEX_ADD_TIMEOUT")
        index["index_cid"] = index_cid
        atomic_write_json(index_path, index)
        update_discovery(index_cid, artifact_cid, manifest, env)
        ipns = publish_ipns(index_cid, env)
    except Exception as exc:
        payload = write_status(
            env,
            "failed",
            reasons=[str(exc)],
            artifact_manifest_sha256=manifest_sha,
            manifest_verification=manifest_verification,
            artifact_dir=str(artifact_dir),
            artifact_manifest=str(manifest_path),
        )
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return 1

    payload = write_status(
        env,
        "published",
        action="ipfs_add_pin",
        artifact_cid=artifact_cid,
        index_cid=index_cid,
        ipns=ipns,
        artifact_manifest_sha256=manifest_sha,
        manifest_verification=manifest_verification,
        artifact_dir=str(artifact_dir),
        artifact_manifest=str(manifest_path),
        latest_index_path=str(index_path),
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

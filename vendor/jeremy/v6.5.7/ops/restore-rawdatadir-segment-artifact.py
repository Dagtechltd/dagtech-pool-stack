#!/usr/bin/env python3
"""Restore a raw datadir from a signed segmented artifact manifest.

The transport is intentionally pluggable. In production the chunk bytes should
come from IPFS by CID/path; during incident recovery this tool can fetch the same
manifest-addressed chunks over SSH while preserving the same trust boundary:
manifest first, chunk hash/size checks, file hash checks, then node consensus.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import fnmatch
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, BinaryIO

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


ROOT = Path(os.environ.get("BDAG_PROJECT_ROOT") or Path(__file__).resolve().parents[1]).resolve()
CHUNK_SIZE = 1024 * 1024
SHA256_HEX_LEN = 64
ED25519_PUBLIC_KEY_LEN = 32
ED25519_SIGNATURE_LEN = 64
SIGNABLE_MANIFEST_FIELDS = (
    ("format_version", True),
    ("artifact_type", True),
    ("network", True),
    ("chain_id", True),
    ("genesis_hash", False),
    ("start_order", False),
    ("end_order", False),
    ("tip_order", True),
    ("tip_hash", True),
    ("tip_id", False),
    ("block_total", True),
    ("state_root", True),
    ("parent_artifact_root", False),
    ("chunk_hash_algo", True),
    ("encoding", False),
    ("compression", False),
    ("layout", False),
    ("created_at", True),
    ("expires_at", False),
    ("min_node_version", False),
    ("metadata", False),
    ("sources", True),
    ("chunks", True),
    ("files", False),
)
SOURCE_FIELDS = (
    "name",
    "chunk_start",
    "chunk_count",
    "total_records",
    "total_compressed",
    "total_uncompressed",
)
CHUNK_FIELDS = (
    "id",
    "source",
    "class",
    "offset",
    "path",
    "compressed_size",
    "uncompressed_size",
    "compressed_sha256",
    "uncompressed_sha256",
    "records",
)
FILE_FIELDS = (
    "path",
    "class",
    "size",
    "sha256",
    "chunk_start",
    "chunk_count",
    "mode",
)
FALSE_VALUES = {"0", "false", "no", "off", "disabled", "failed", "fail", "invalid"}
TRUE_VALUES = {"1", "true", "yes", "on", "enabled", "passed", "pass", "valid", "ok"}
SIGNATURE_KEYS = ("signature", "signature_hex", "signatureHex", "sig", "value")
PUBLIC_KEY_KEYS = ("public_key", "publicKey", "signing_public_key", "signingPublicKey")
UNSAFE_MARKERS = ("DO_NOT_PUBLISH", "DO_NOT_PUBLISH.txt")
UNSAFE_METADATA_SCOPE_NORMALIZED = {
    "metadata",
    "validation",
    "restorevalidation",
    "candidatevalidation",
    "filesafety",
    "artifacttrust",
    "trust",
    "source",
}
UNSAFE_EXACT = {
    "LOCK",
    "BdagChain/LOCK",
    "bdageth/LOCK",
    "bdageth/chaindata/LOCK",
    "network.key",
    "bdageth/nodekey",
    "keystore",
    "bdageth/keystore",
    "peerstore",
    "nodes",
    "bdageth/nodes",
    "bdageth/transactions.rlp",
    "geth.ipc",
    "bdag.ipc",
}
UNSAFE_PATTERNS = (
    ".shutdown.lock.tmp*",
    "*.ipc",
    "*.sock",
)
PRESERVE_PATHS = (
    "network.key",
    "bdageth/nodekey",
    "keystore",
    "bdageth/keystore",
    "peerstore",
)


class RestoreError(RuntimeError):
    """Raised when artifact verification or reconstruction fails."""


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def canonical_json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")


def canonical_ordered_dict(value: dict[str, Any], field_order: tuple[str, ...] = ()) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for field in field_order:
        if field in value and value[field] not in ("", None, [], {}):
            output[field] = canonical_manifest_value("", value[field])
    for field in sorted(value):
        if field not in output and value[field] not in ("", None, [], {}):
            output[field] = canonical_manifest_value("", value[field])
    return output


def canonical_manifest_value(field: str, value: Any) -> Any:
    if field == "sources" and isinstance(value, list):
        return [canonical_ordered_dict(item, SOURCE_FIELDS) if isinstance(item, dict) else item for item in value]
    if field == "chunks" and isinstance(value, list):
        return [canonical_ordered_dict(item, CHUNK_FIELDS) if isinstance(item, dict) else item for item in value]
    if field == "files" and isinstance(value, list):
        return [canonical_ordered_dict(item, FILE_FIELDS) if isinstance(item, dict) else item for item in value]
    if isinstance(value, dict):
        return canonical_ordered_dict(value)
    if isinstance(value, list):
        return [canonical_manifest_value("", item) for item in value]
    return value


def signable_manifest_payload(manifest: dict[str, Any]) -> bytes:
    """Return the canonical bytes used for artifact_root and Ed25519 signing.

    This mirrors corechain FastArtifact V2's Go manifest canonicalization:
    signatures and artifact_root are excluded, struct fields are serialized in
    manifest order, optional empty fields are omitted, and maps are stable.
    """

    payload: dict[str, Any] = {}
    for field, required in SIGNABLE_MANIFEST_FIELDS:
        if field not in manifest:
            continue
        value = manifest[field]
        if required or value not in ("", None, [], {}):
            payload[field] = canonical_manifest_value(field, value)
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def manifest_digest(manifest: dict[str, Any]) -> bytes:
    return hashlib.sha256(signable_manifest_payload(manifest)).digest()


def compute_artifact_root(manifest: dict[str, Any]) -> str:
    return manifest_digest(manifest).hex()


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=str(path.parent), delete=False) as handle:
        handle.write(canonical_json_bytes(payload))
        tmp = Path(handle.name)
    tmp.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative_path(value: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise RestoreError("empty relative path")
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts:
        raise RestoreError(f"unsafe relative path: {raw}")
    return path


def is_unsafe_restore_path(rel: str) -> bool:
    normalized = rel.strip("/")
    if normalized in UNSAFE_EXACT:
        return True
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in UNSAFE_PATTERNS)


def ssh_base(remote: str, control_socket: str | None) -> list[str]:
    command = ["ssh"]
    if control_socket:
        command.extend(["-S", control_socket, "-o", "BatchMode=yes"])
    command.append(remote)
    return command


def remote_cat_command(remote: str, control_socket: str | None, path: str) -> list[str]:
    return [*ssh_base(remote, control_socket), "cat " + shlex.quote(path)]


def remote_marker_test_command(remote: str, control_socket: str | None, artifact_dir: str) -> list[str]:
    quoted = [shlex.quote(str(Path(artifact_dir) / name)) for name in UNSAFE_MARKERS]
    script = " || ".join(f"test -e {path}" for path in quoted)
    return [*ssh_base(remote, control_socket), script]


def read_manifest(args: argparse.Namespace) -> tuple[dict[str, Any], bytes]:
    if args.local_artifact_dir:
        path = Path(args.local_artifact_dir).resolve() / "manifest.json"
        raw = path.read_bytes()
    else:
        if not args.remote or not args.remote_artifact_dir:
            raise RestoreError("set --local-artifact-dir or both --remote and --remote-artifact-dir")
        path = str(Path(args.remote_artifact_dir) / "manifest.json")
        result = subprocess.run(
            remote_cat_command(args.remote, args.ssh_control_socket, path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise RestoreError(f"failed to fetch remote manifest: {result.stderr.decode(errors='replace')[-500:]}")
        raw = result.stdout
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RestoreError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise RestoreError("manifest root is not an object")
    return manifest, raw


def parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in TRUE_VALUES:
            return True
        if lowered in FALSE_VALUES:
            return False
    return None


def normalize_key(value: Any) -> str:
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def unsafe_marker_blockers(args: argparse.Namespace) -> list[str]:
    if args.local_artifact_dir:
        artifact_dir = Path(args.local_artifact_dir).resolve()
        return [f"do_not_publish_marker:{name}" for name in UNSAFE_MARKERS if (artifact_dir / name).exists()]
    if not args.remote_artifact_dir:
        return []
    if not args.remote:
        raise RestoreError("remote marker check requires --remote")
    result = subprocess.run(
        remote_marker_test_command(args.remote, args.ssh_control_socket, args.remote_artifact_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode == 0:
        return ["do_not_publish_marker:remote_artifact_dir"]
    if result.returncode == 1:
        return []
    raise RestoreError(f"failed to check remote DO_NOT_PUBLISH marker: {result.stderr.decode(errors='replace')[-500:]}")


def iter_metadata_scopes(value: Any, include_current: bool = True) -> Any:
    if isinstance(value, dict):
        if include_current:
            yield value
        for key, child in value.items():
            if normalize_key(key) in UNSAFE_METADATA_SCOPE_NORMALIZED:
                yield from iter_metadata_scopes(child, include_current=True)
            elif isinstance(child, list):
                for item in child:
                    yield from iter_metadata_scopes(item, include_current=False)
    elif isinstance(value, list):
        for item in value:
            yield from iter_metadata_scopes(item, include_current=include_current)


def unsafe_metadata_blockers(manifest: dict[str, Any], args: argparse.Namespace) -> list[str]:
    if args.allow_test_unsafe_metadata:
        return []
    blockers = unsafe_marker_blockers(args)
    for scope in iter_metadata_scopes(manifest):
        for key, value in scope.items():
            normalized = normalize_key(key)
            parsed = parse_bool(value)
            if normalized == "donotpublish" and parsed is not False:
                blockers.append("metadata:DO_NOT_PUBLISH")
            elif normalized == "publishable" and parsed is False:
                blockers.append("metadata:publishable=0")
            elif normalized == "finalizedsidecar" and parsed is False:
                blockers.append("metadata:finalized_sidecar=0")
    return sorted(set(blockers))


def decode_key_material(value: Any, field_name: str, expected_len: int) -> bytes:
    text = str(value or "").strip()
    if not text:
        raise RestoreError(f"signature entry missing {field_name}")
    if text.startswith("0x"):
        text = text[2:]
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        try:
            raw = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RestoreError(f"signature entry has invalid {field_name} encoding") from exc
    if len(raw) != expected_len:
        raise RestoreError(f"signature entry {field_name} has length {len(raw)}, expected {expected_len}")
    return raw


def signature_field(signature: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in signature:
            return signature[name]
    return ""


def verify_manifest_artifact_root(manifest: dict[str, Any], args: argparse.Namespace) -> str:
    computed = compute_artifact_root(manifest)
    declared = str(manifest.get("artifact_root") or "").strip().lower()
    if not declared:
        if args.allow_unsigned:
            return computed
        raise RestoreError("manifest missing artifact_root")
    if len(declared) != SHA256_HEX_LEN:
        raise RestoreError("manifest artifact_root is not a sha256 hex digest")
    try:
        int(declared, 16)
    except ValueError as exc:
        raise RestoreError("manifest artifact_root is not valid hex") from exc
    if declared != computed:
        raise RestoreError(f"manifest artifact_root mismatch: declared={declared} computed={computed}")
    return computed


def verify_manifest_signatures(manifest: dict[str, Any], args: argparse.Namespace) -> list[str]:
    signatures = manifest.get("signatures")
    if not signatures:
        if args.allow_unsigned:
            return []
        raise RestoreError("manifest has no signature material; pass --allow-unsigned only for explicit test fixtures")
    if not isinstance(signatures, list):
        raise RestoreError("manifest signatures must be an array")
    digest = manifest_digest(manifest)
    verified: list[str] = []
    for index, item in enumerate(signatures):
        if not isinstance(item, dict):
            raise RestoreError(f"signature entry {index} is not an object")
        key_id = str(item.get("key_id") or item.get("keyId") or item.get("id") or "").strip()
        if not key_id:
            raise RestoreError(f"signature entry {index} missing key_id")
        algorithm = str(item.get("algorithm") or "ed25519").strip().lower()
        if algorithm != "ed25519":
            continue
        public_key = decode_key_material(signature_field(item, PUBLIC_KEY_KEYS), "public_key", ED25519_PUBLIC_KEY_LEN)
        signature = decode_key_material(signature_field(item, SIGNATURE_KEYS), "signature", ED25519_SIGNATURE_LEN)
        try:
            Ed25519PublicKey.from_public_bytes(public_key).verify(signature, digest)
        except InvalidSignature:
            continue
        verified.append(key_id)
    if not verified:
        raise RestoreError("manifest has no valid Ed25519 signature")
    return sorted(set(verified))


def validate_manifest(manifest: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    artifact_type = str(manifest.get("artifact_type") or manifest.get("type") or "")
    if artifact_type != "raw_datadir_checkpoint":
        raise RestoreError(f"unsupported artifact_type={artifact_type!r}")
    if args.network and str(manifest.get("network") or "") != args.network:
        raise RestoreError(f"manifest network mismatch: {manifest.get('network')!r} != {args.network!r}")
    if args.min_tip_order and int(manifest.get("tip_order") or 0) < args.min_tip_order:
        raise RestoreError(f"manifest tip_order below minimum: {manifest.get('tip_order')} < {args.min_tip_order}")
    files = manifest.get("files")
    chunks = manifest.get("chunks")
    if not isinstance(files, list) or not isinstance(chunks, list) or not files or not chunks:
        raise RestoreError("manifest must contain non-empty files and chunks arrays")
    metadata_blockers = unsafe_metadata_blockers(manifest, args)
    if metadata_blockers:
        raise RestoreError("artifact metadata is not publishable: " + ", ".join(metadata_blockers))
    artifact_root = verify_manifest_artifact_root(manifest, args)
    verified_signature_key_ids = verify_manifest_signatures(manifest, args)
    return {
        "artifact_root": artifact_root,
        "verified_signature_key_ids": verified_signature_key_ids,
    }


def open_chunk(args: argparse.Namespace, chunk_path: str) -> tuple[BinaryIO, subprocess.Popen[bytes] | None]:
    safe_relative_path(chunk_path)
    if args.local_artifact_dir:
        path = Path(args.local_artifact_dir).resolve() / chunk_path
        return path.open("rb"), None
    assert args.remote and args.remote_artifact_dir
    remote_path = str(Path(args.remote_artifact_dir) / chunk_path)
    proc = subprocess.Popen(
        remote_cat_command(args.remote, args.ssh_control_socket, remote_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.stdout is None:
        raise RestoreError("failed to open remote chunk pipe")
    return proc.stdout, proc


def stream_chunk(
    args: argparse.Namespace,
    chunk: dict[str, Any],
    output: BinaryIO,
    file_digest: hashlib._Hash,
) -> int:
    path = str(chunk.get("path") or "")
    expected_hash = str(chunk.get("compressed_sha256") or chunk.get("sha256") or "")
    expected_size = int(chunk.get("compressed_size") or chunk.get("uncompressed_size") or -1)
    if not expected_hash:
        raise RestoreError(f"chunk {chunk.get('id')} has no expected hash")
    digest = hashlib.sha256()
    total = 0
    handle, proc = open_chunk(args, path)
    stderr = b""
    try:
        while True:
            data = handle.read(CHUNK_SIZE)
            if not data:
                break
            total += len(data)
            digest.update(data)
            file_digest.update(data)
            output.write(data)
    finally:
        handle.close()
    if proc is not None:
        stderr = proc.stderr.read() if proc.stderr is not None else b""
        rc = proc.wait()
        if rc != 0:
            raise RestoreError(f"chunk fetch failed path={path} rc={rc}: {stderr.decode(errors='replace')[-500:]}")
    actual_hash = digest.hexdigest()
    if total != expected_size:
        raise RestoreError(f"chunk size mismatch path={path} actual={total} expected={expected_size}")
    if actual_hash != expected_hash:
        raise RestoreError(f"chunk hash mismatch path={path} actual={actual_hash} expected={expected_hash}")
    return total


def copy_preserved_identity(preserve_from: Path, target: Path) -> list[str]:
    copied: list[str] = []
    if not preserve_from.exists():
        return copied
    for rel in PRESERVE_PATHS:
        src = preserve_from / rel
        dst = target / rel
        if not src.exists():
            continue
        if dst.exists():
            if dst.is_dir() and not dst.is_symlink():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir() and not src.is_symlink():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        copied.append(rel)
    return copied


def remove_ephemeral_paths(target: Path) -> list[str]:
    removed: list[str] = []
    for rel in sorted(UNSAFE_EXACT):
        path = target / rel
        if path.exists() and rel not in PRESERVE_PATHS:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed.append(rel)
    for pattern in UNSAFE_PATTERNS:
        for path in target.rglob(pattern):
            rel = path.relative_to(target).as_posix()
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed.append(rel)
    return sorted(set(removed))


def reconstruct(manifest: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    target = Path(args.target_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    files = manifest["files"]
    chunks = manifest["chunks"]
    restored = 0
    skipped = []
    bytes_written = 0
    started = time.time()

    for file_index, entry in enumerate(files, start=1):
        rel = safe_relative_path(str(entry.get("path") or ""))
        rel_posix = rel.as_posix()
        if args.skip_unsafe and is_unsafe_restore_path(rel_posix):
            skipped.append(rel_posix)
            continue
        output_path = target / rel
        expected_size = int(entry.get("size") or 0)
        expected_hash = str(entry.get("sha256") or "")
        if output_path.exists() and expected_hash and output_path.stat().st_size == expected_size:
            if sha256_file(output_path) == expected_hash:
                restored += 1
                continue
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = output_path.with_name(output_path.name + ".part")
        if tmp_path.exists():
            tmp_path.unlink()
        file_digest = hashlib.sha256()
        chunk_start = int(entry.get("chunk_start") or 0)
        chunk_count = int(entry.get("chunk_count") or 0)
        with tmp_path.open("wb") as handle:
            for chunk in chunks[chunk_start : chunk_start + chunk_count]:
                bytes_written += stream_chunk(args, chunk, handle, file_digest)
        actual_hash = file_digest.hexdigest()
        actual_size = tmp_path.stat().st_size
        if actual_size != expected_size:
            tmp_path.unlink(missing_ok=True)
            raise RestoreError(f"file size mismatch path={rel_posix} actual={actual_size} expected={expected_size}")
        if expected_hash and actual_hash != expected_hash:
            tmp_path.unlink(missing_ok=True)
            raise RestoreError(f"file hash mismatch path={rel_posix} actual={actual_hash} expected={expected_hash}")
        if "mode" in entry:
            try:
                tmp_path.chmod(int(entry["mode"]) & 0o777)
            except OSError:
                pass
        tmp_path.replace(output_path)
        restored += 1
        if args.progress_every and (restored % args.progress_every == 0):
            elapsed = max(time.time() - started, 0.001)
            print(
                json.dumps(
                    {
                        "event": "restore_progress",
                        "files_restored": restored,
                        "files_total": len(files),
                        "bytes_written": bytes_written,
                        "mb_per_second": round(bytes_written / elapsed / 1024 / 1024, 2),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
        if args.max_files and file_index >= args.max_files:
            break

    preserved = copy_preserved_identity(Path(args.preserve_from).resolve(), target) if args.preserve_from else []
    removed = remove_ephemeral_paths(target)
    return {
        "target_dir": str(target),
        "files_total": len(files),
        "files_restored": restored,
        "files_skipped_unsafe": len(skipped),
        "skipped_unsafe_sample": skipped[:50],
        "bytes_written": bytes_written,
        "preserved_identity_paths": preserved,
        "removed_ephemeral_paths": removed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--local-artifact-dir")
    source.add_argument("--remote-artifact-dir")
    parser.add_argument("--remote", help="SSH remote, for example jeremy@192.168.68.65")
    parser.add_argument("--ssh-control-socket")
    parser.add_argument("--target-dir", required=True)
    parser.add_argument("--preserve-from")
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--network", default="mainnet")
    parser.add_argument("--min-tip-order", type=int, default=0)
    parser.add_argument("--allow-unsigned", action="store_true")
    parser.add_argument(
        "--allow-test-unsafe-metadata",
        action="store_true",
        help="test-only override for DO_NOT_PUBLISH/publishable=0/finalized_sidecar=0 artifact metadata",
    )
    parser.add_argument("--skip-unsafe", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument("--max-files", type=int, default=0, help="test-only limit")
    args = parser.parse_args(argv)
    if str(args.network or "").strip().lower() != "mainnet":
        raise RestoreError(f"raw datadir restore refuses non-mainnet network: {args.network!r}")

    started = time.time()
    manifest, raw = read_manifest(args)
    verification = validate_manifest(manifest, args)
    manifest_sha = hashlib.sha256(raw).hexdigest()
    result = reconstruct(manifest, args)
    payload = {
        "document_type": "bdag_rawdatadir_segment_restore_report_v1",
        "generated_at": now_iso(),
        "ok": True,
        "duration_seconds": round(time.time() - started, 3),
        "project_root": str(ROOT),
        "source": {
            "mode": "local" if args.local_artifact_dir else "ssh_segment_fetch",
            "remote": args.remote if args.remote_artifact_dir else None,
            "artifact_dir": args.local_artifact_dir or args.remote_artifact_dir,
        },
        "manifest": {
            "sha256": manifest_sha,
            "artifact_type": manifest.get("artifact_type"),
            "network": manifest.get("network"),
            "chain_id": manifest.get("chain_id"),
            "genesis_hash": manifest.get("genesis_hash"),
            "tip_order": manifest.get("tip_order"),
            "tip_hash": manifest.get("tip_hash"),
            "state_root": manifest.get("state_root"),
            "signatures_present": bool(manifest.get("signatures")),
            "artifact_root": verification["artifact_root"],
            "verified_signature_key_ids": verification["verified_signature_key_ids"],
            "do_not_publish_marker_observed": None,
        },
        "restore": result,
        "trust_model": "chunks are byte transport; manifest hashes and node consensus validation remain authoritative",
    }
    atomic_write_json(Path(args.status_file).resolve(), payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RestoreError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        raise SystemExit(1)

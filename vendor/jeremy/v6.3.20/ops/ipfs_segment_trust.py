#!/usr/bin/env python3
"""Signature helpers for BlockDAG IPFS segment indexes and manifests.

The transport CID proves bytes. These signatures bind those bytes to a known
writer identity so restore consumers can reject unsigned or unauthorized IPFS
objects before any chain-data mutation is considered.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


SIGNATURE_FIELDS = {
    "signature_status",
    "signatures",
    "index_signatures",
    "manifest_signatures",
    "roster_signatures",
}
FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}


def canonical_json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def env_bool(env: Mapping[str, str], key: str, default: bool = False) -> bool:
    value = str(env.get(key, "")).strip().lower()
    if not value:
        return default
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    return default


def unsigned_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in SIGNATURE_FIELDS}


def signing_key_hex(env: Mapping[str, str]) -> str:
    value = str(env.get("BDAG_IPFS_SEGMENT_SIGNING_KEY_HEX") or "").strip()
    if value:
        return value
    path_value = str(env.get("BDAG_IPFS_SEGMENT_SIGNING_KEY_FILE") or "").strip()
    if not path_value:
        return ""
    path = Path(path_value).expanduser()
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, candidate = line.split("=", 1)
                if key.strip() not in {"BDAG_IPFS_SEGMENT_SIGNING_KEY_HEX", "SIGNING_KEY_HEX"}:
                    continue
                return candidate.strip().strip('"').strip("'")
            return line
    except OSError:
        return ""
    return ""


def load_private_key(seed_hex: str) -> Ed25519PrivateKey:
    raw = bytes.fromhex(seed_hex)
    if len(raw) == 32:
        return Ed25519PrivateKey.from_private_bytes(raw)
    if len(raw) == 64:
        return Ed25519PrivateKey.from_private_bytes(raw[:32])
    raise ValueError("BDAG_IPFS_SEGMENT_SIGNING_KEY_HEX must be a 32-byte Ed25519 seed or 64-byte seed+public hex")


def public_key_hex(private_key: Ed25519PrivateKey) -> str:
    return private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def _parse_signer_pairs(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in value.replace("\n", ",").split(","):
        item = raw.strip()
        if not item:
            continue
        if "=" in item:
            writer_id, pub = item.split("=", 1)
        elif ":" in item:
            writer_id, pub = item.split(":", 1)
        else:
            continue
        writer_id = writer_id.strip()
        pub = pub.strip().lower()
        if writer_id and pub:
            result[writer_id] = pub
    return result


def trusted_signers(env: Mapping[str, str]) -> dict[str, str]:
    """Parse signer trust anchors from writer_id=public_hex pairs.

    BDAG_IPFS_SEGMENT_TRUSTED_SIGNERS remains the explicit source of trust.
    BDAG_IPFS_SEGMENT_WRITER_ROSTER may also carry writer_id=public_hex entries
    so election and trust can share one roster file/env value.
    """

    result: dict[str, str] = {}
    for key in (
        "BDAG_IPFS_SEGMENT_WRITER_ROSTER",
        "BDAG_IPFS_WRITER_ROSTER",
        "BDAG_IPFS_SEGMENT_TRUSTED_SIGNERS",
        "BDAG_IPFS_TRUSTED_SIGNERS",
    ):
        result.update(_parse_signer_pairs(str(env.get(key) or "").strip()))
    return result


def signature_required(env: Mapping[str, str], *, restore: bool = False) -> bool:
    key = "BDAG_IPFS_RESTORE_REQUIRE_SIGNATURES" if restore else "BDAG_IPFS_SEGMENT_REQUIRE_SIGNATURES"
    fallback = env_bool(env, "BDAG_IPFS_SEGMENT_REQUIRE_SIGNATURES", True)
    return env_bool(env, key, fallback)


def sign_payload(payload: Mapping[str, Any], env: Mapping[str, str], *, signature_field: str) -> dict[str, Any]:
    seed_hex = signing_key_hex(env)
    if not seed_hex:
        if signature_required(env):
            raise RuntimeError("IPFS segment signing is required but no BDAG_IPFS_SEGMENT_SIGNING_KEY_HEX/FILE is configured")
        signed = dict(payload)
        signed["signature_status"] = "unsigned_allowed_by_policy"
        return signed

    key = load_private_key(seed_hex)
    unsigned = unsigned_payload(payload)
    signed_bytes = canonical_json_bytes(unsigned)
    writer_id = str(env.get("BDAG_IPFS_SEGMENT_WRITER_ID") or env.get("BDAG_IPFS_WRITER_ID") or "").strip()
    if not writer_id:
        writer_id = str(unsigned.get("writer", {}).get("writer_id") if isinstance(unsigned.get("writer"), Mapping) else "")
    if not writer_id:
        writer_id = "local_writer"
    pub = public_key_hex(key)
    signature = {
        "algorithm": "ed25519",
        "canonicalization": "json-sort-keys-no-signature-fields-v1",
        "writer_id": writer_id,
        "public_key_hex": pub,
        "signed_payload_sha256": sha256_bytes(signed_bytes),
        "signature_hex": key.sign(signed_bytes).hex(),
    }
    signed = dict(unsigned)
    signed[signature_field] = [signature]
    signed["signature_status"] = "signed"
    return signed


def verify_payload_signature(
    payload: Mapping[str, Any],
    env: Mapping[str, str],
    *,
    signature_field: str,
    context: str,
) -> dict[str, Any]:
    signatures = payload.get(signature_field)
    if not isinstance(signatures, list) or not signatures:
        if signature_required(env, restore=True):
            raise RuntimeError(f"{context} is missing required {signature_field}")
        return {"state": "unsigned_allowed_by_policy", "signatures_verified": 0}

    trust = trusted_signers(env)
    unsigned = unsigned_payload(payload)
    signed_bytes = canonical_json_bytes(unsigned)
    digest = sha256_bytes(signed_bytes)
    verified = []
    errors: list[str] = []
    for idx, raw in enumerate(signatures):
        if not isinstance(raw, Mapping):
            errors.append(f"signature[{idx}] is not an object")
            continue
        if raw.get("algorithm") != "ed25519":
            errors.append(f"signature[{idx}] unsupported algorithm {raw.get('algorithm')!r}")
            continue
        writer_id = str(raw.get("writer_id") or "").strip()
        public_hex = str(raw.get("public_key_hex") or "").strip().lower()
        signature_hex = str(raw.get("signature_hex") or "").strip().lower()
        if not writer_id or not public_hex or not signature_hex:
            errors.append(f"signature[{idx}] missing writer_id/public_key_hex/signature_hex")
            continue
        if raw.get("signed_payload_sha256") != digest:
            errors.append(f"signature[{idx}] signed_payload_sha256 mismatch")
            continue
        expected_public = trust.get(writer_id)
        if not expected_public:
            errors.append(f"signature[{idx}] writer {writer_id!r} is not trusted")
            continue
        if expected_public.lower() != public_hex:
            errors.append(f"signature[{idx}] public key for writer {writer_id!r} is not the configured trust anchor")
            continue
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex)).verify(
                bytes.fromhex(signature_hex),
                signed_bytes,
            )
        except (InvalidSignature, ValueError) as exc:
            errors.append(f"signature[{idx}] invalid: {exc}")
            continue
        verified.append({"writer_id": writer_id, "public_key_hex": public_hex, "signed_payload_sha256": digest})

    if not verified:
        raise RuntimeError(f"{context} signature verification failed: {'; '.join(errors) or 'no valid signatures'}")
    return {"state": "verified", "signatures_verified": len(verified), "verified_signers": verified}

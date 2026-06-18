#!/usr/bin/env python3
"""Validate BlockDAG restore candidates against fail-closed policy.

This is intentionally a policy/schema slice. It reads manifests, optional
metadata, and bounded filesystem safety markers. It does not boot a node, open a
live database, run Docker, or mutate candidate data.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("BDAG_PROJECT_ROOT") or Path(__file__).resolve().parents[1]).resolve()
POLICY_VERSION = "restore_candidate_policy_v1"

FALSE_VALUES = {"0", "false", "no", "off", "disabled", "failed", "fail", "invalid"}
TRUE_VALUES = {"1", "true", "yes", "on", "enabled", "passed", "pass", "valid", "ok"}
ZERO_HASH_CHARS = {"0", "x"}

MARKER_NAMES = ("DO_NOT_PUBLISH", "DO_NOT_PUBLISH.txt")
SUPPORTED_ARTIFACT_TYPES = {"chain_checkpoint", "chain_archive", "restore_candidate"}
UNSAFE_RELATIVE_PATHS = (
    "network.key",
    "nodekey",
    "bdageth/nodekey",
    "keystore",
    "bdageth/keystore",
    "peerstore",
    "bdageth/peerstore",
    "nodes",
    "bdageth/nodes",
    ".rsync-partial",
    "BdagChain/LOCK",
    "LOCK",
)

HARD_TEXT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("unknown ancestor", "unknown_ancestor"),
    ("bad block", "bad_block"),
    ("chain is stateless", "stateless_genesis_after_restore"),
    ("stateless genesis", "stateless_genesis_after_restore"),
    ("genesis block reached", "stateless_genesis_after_restore"),
    ("head state missing", "missing_head_state"),
    ("missing head state", "missing_head_state"),
    ("block state missing", "missing_block_state"),
    ("missing trie", "missing_block_state"),
    ("missing state", "missing_block_state"),
    ("zero state root", "zero_state_root"),
    ("zero-state-root", "zero_state_root"),
    ("network mismatch", "network_or_genesis_mismatch"),
    ("genesis mismatch", "network_or_genesis_mismatch"),
    ("repairing", "silent_startup_repair_observed"),
    ("rewinding", "silent_startup_repair_observed"),
)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def resolve_path(value: str | None, default: Path | None = None) -> Path | None:
    if not value:
        return default.resolve() if default is not None else None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def read_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def append_unique(values: list[str], item: str) -> None:
    if item and item not in values:
        values.append(item)


def normalize_key(value: Any) -> str:
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def metadata_as_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    parsed: dict[str, Any] = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and "=" in item:
                key, value = item.split("=", 1)
                parsed[key.strip()] = value.strip()
            elif isinstance(item, dict):
                parsed.update(item)
    return parsed


def flattened_sources(manifest: dict[str, Any], metadata: dict[str, Any]) -> list[dict[str, Any]]:
    sources = [manifest, metadata]
    for payload in (manifest, metadata):
        if not isinstance(payload, dict):
            continue
        for key in (
            "metadata",
            "validation",
            "restore_validation",
            "candidate_validation",
            "file_safety",
            "artifact_trust",
            "trust",
            "source",
            "anchors",
            "trial",
            "consensus",
        ):
            value = payload.get(key)
            if isinstance(value, dict):
                sources.append(value)
            elif key == "metadata":
                parsed = metadata_as_dict(value)
                if parsed:
                    sources.append(parsed)
    return sources


def lookup_value(payloads: list[dict[str, Any]], keys: set[str]) -> tuple[Any, bool]:
    normalized = {normalize_key(key) for key in keys}
    for payload in payloads:
        for key, value in payload.items():
            if normalize_key(key) in normalized:
                return value, True
    return None, False


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


def bool_field(payloads: list[dict[str, Any]], keys: set[str]) -> tuple[bool, bool]:
    value, present = lookup_value(payloads, keys)
    parsed = parse_bool(value)
    if parsed is None:
        return False, present
    return parsed, present


def int_value(value: Any) -> int:
    try:
        if isinstance(value, str) and value.strip().lower().startswith("0x"):
            return int(value, 16)
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def numeric_field(payloads: list[dict[str, Any]], keys: set[str]) -> int:
    value, _ = lookup_value(payloads, keys)
    return int_value(value)


def string_field(payloads: list[dict[str, Any]], keys: set[str]) -> str:
    value, _ = lookup_value(payloads, keys)
    return str(value or "").strip()


def is_zero_hash(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return True
    return bool(text) and set(text) <= ZERO_HASH_CHARS and any(ch == "0" for ch in text)


def manifest_path_for(candidate: Path) -> Path | None:
    if candidate.is_file():
        return candidate
    ordered = [
        candidate / "manifest.json",
        candidate / "artifact.manifest.json",
        candidate / "restore-candidate.json",
        candidate / "candidate-metadata.json",
        candidate / ".restore-candidate.json",
    ]
    for path in ordered:
        if path.is_file():
            return path
    matches = sorted(candidate.glob("*.manifest.json"))
    if matches:
        return matches[0]
    current = candidate / "current" / "manifest.json"
    if current.is_file():
        return current
    return None


def metadata_path_for(value: str | None, candidate: Path) -> Path | None:
    explicit = resolve_path(value) if value else None
    if explicit:
        return explicit
    if candidate.is_file():
        return None
    for name in (
        "restore-candidate-metadata.json",
        "candidate-metadata.json",
        "restore-validation.json",
    ):
        path = candidate / name
        if path.is_file():
            return path
    return None


def do_not_publish_markers(candidate: Path) -> list[str]:
    probes = [candidate]
    if candidate.parent != candidate:
        probes.append(candidate.parent)
    markers: list[str] = []
    for base in probes:
        for name in MARKER_NAMES:
            path = base / name
            if path.exists():
                markers.append(str(path))
    return markers


def collect_unsafe_paths(candidate: Path) -> list[str]:
    unsafe: list[str] = []
    for rel in UNSAFE_RELATIVE_PATHS:
        if (candidate / rel).exists():
            unsafe.append(rel)
    return sorted(set(unsafe))


def artifact_archive_names(manifest: dict[str, Any], metadata: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for payload in (metadata_as_dict(manifest.get("metadata")), metadata_as_dict(metadata.get("metadata")), manifest, metadata):
        if not isinstance(payload, dict):
            continue
        for key in ("archive", "payload", "payload_path", "artifact_file"):
            value = str(payload.get(key) or "").strip()
            if value:
                names.append(value)
    return names


def manifest_file_entries(manifest: dict[str, Any]) -> list[str]:
    entries: list[str] = []
    for key in ("files", "chunks", "artifacts"):
        value = manifest.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, str):
                entries.append(item)
            elif isinstance(item, dict):
                for name_key in ("path", "name", "file", "relative_path"):
                    raw = item.get(name_key)
                    if raw:
                        entries.append(str(raw))
                        break
    return entries


def relative_child_exists(base: Path, entry: str) -> bool:
    entry_path = Path(entry)
    if entry_path.is_absolute() or ".." in entry_path.parts:
        return False
    return (base / entry_path).exists()


def artifact_content_complete(candidate: Path, manifest_path: Path | None, manifest: dict[str, Any], metadata: dict[str, Any]) -> bool:
    if manifest_path is None or not manifest:
        return False
    base = manifest_path.parent if manifest_path else candidate
    entries = manifest_file_entries(manifest)
    if entries:
        return all(relative_child_exists(base, entry) for entry in entries)
    archive_names = artifact_archive_names(manifest, metadata)
    if archive_names:
        return all(relative_child_exists(base, name) for name in archive_names)
    if any(base.glob("*.tar.zst")):
        return True
    return False


def artifact_type_value(manifest: dict[str, Any]) -> str:
    return str(manifest.get("artifact_type") or manifest.get("type") or "").strip()


def has_signature_material(value: Any, under_signature_key: bool = False) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if under_signature_key and item:
                return True
            if lowered in {"signature", "signature_hex", "signaturehex", "sig"} and item:
                return True
            if lowered in {"signatures", "signature_set", "signaturelist"} and has_signature_material(item, True):
                return True
            if has_signature_material(item, under_signature_key or "signature" in lowered):
                return True
    elif isinstance(value, list):
        return any(has_signature_material(item, under_signature_key) for item in value)
    elif under_signature_key and value:
        return True
    return False


def signed_manifest_status(payloads: list[dict[str, Any]], manifest: dict[str, Any]) -> tuple[bool, bool, bool]:
    explicit, present = bool_field(
        payloads,
        {
            "signed_manifest_valid",
            "manifest_signature_valid",
            "signature_valid",
            "signatures_valid",
        },
    )
    has_material = has_signature_material(manifest.get("signatures") or manifest.get("signature"))
    return explicit and has_material, present, has_material


def state_root_nonzero(payloads: list[dict[str, Any]]) -> tuple[bool, str]:
    explicit, present = bool_field(
        payloads,
        {"state_root_nonzero_expected_blocks", "state_root_valid", "state_root_nonzero"},
    )
    if present:
        return explicit, "explicit"
    state_root = string_field(payloads, {"state_root", "evm_state_root", "stateroot"})
    height = max(
        numeric_field(payloads, {"tip_order", "tipOrder", "main_order", "mainOrder", "height"}),
        numeric_field(payloads, {"block_total", "blockTotal", "block_count", "blockCount", "blocks"}),
    )
    if height > 1:
        return bool(state_root and not is_zero_hash(state_root)), "inferred_from_state_root"
    return False, "missing_height_or_state_root"


def recursive_text_values(value: Any, remaining: int = 200) -> list[str]:
    if remaining <= 0:
        return []
    if isinstance(value, dict):
        texts: list[str] = []
        for key, item in value.items():
            texts.extend(recursive_text_values(key, remaining - len(texts)))
            texts.extend(recursive_text_values(item, remaining - len(texts)))
            if len(texts) >= remaining:
                break
        return texts[:remaining]
    if isinstance(value, list):
        texts = []
        for item in value:
            texts.extend(recursive_text_values(item, remaining - len(texts)))
            if len(texts) >= remaining:
                break
        return texts[:remaining]
    if isinstance(value, (str, int, float, bool)):
        return [str(value)]
    return []


def hard_text_blockers(*payloads: dict[str, Any]) -> list[str]:
    haystack = "\n".join(text.lower() for payload in payloads for text in recursive_text_values(payload))
    reasons: list[str] = []
    for needle, reason in HARD_TEXT_PATTERNS:
        if needle in haystack:
            append_unique(reasons, reason)
    return reasons


def explicit_mismatch_blockers(payloads: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    for keys, reason in (
        ({"network_match", "network_matches", "network_valid"}, "network_or_genesis_mismatch"),
        ({"genesis_match", "genesis_matches", "genesis_valid"}, "network_or_genesis_mismatch"),
        ({"unknown_ancestor"}, "unknown_ancestor"),
        ({"bad_block"}, "bad_block"),
        ({"startup_repair_observed", "silent_startup_repair_observed", "rewind_observed"}, "silent_startup_repair_observed"),
        ({"stateless_genesis_after_restore"}, "stateless_genesis_after_restore"),
        ({"missing_head_state"}, "missing_head_state"),
    ):
        value, present = bool_field(payloads, keys)
        if present:
            if reason == "network_or_genesis_mismatch" and value is False:
                append_unique(reasons, reason)
            elif reason != "network_or_genesis_mismatch" and value is True:
                append_unique(reasons, reason)
    return reasons


def field_reason(name: str, value: bool, present: bool) -> str:
    if name == "independent_anchor_match":
        return "independent_anchor_mismatch" if present else "independent_anchor_validation_missing"
    if name == "offline_db_open":
        return "offline_db_open_failed" if present else "offline_db_open_missing"
    if name == "restore_trial_passed":
        return "restore_trial_failed" if present else "restore_trial_missing"
    if name == "consensus_validated":
        return "consensus_validation_failed" if present else "consensus_validation_missing"
    if name == "mineable_validated":
        return "mineable_validation_failed" if present else "mineable_validation_missing"
    return f"{name}_missing_or_false"


def validate_candidate(
    candidate: Path,
    candidate_type: str,
    metadata_path: Path | None = None,
    reference_rpc_url: str = "",
    require_mineable: bool = False,
) -> dict[str, Any]:
    candidate_type = candidate_type or "artifact"
    manifest_path = manifest_path_for(candidate)
    metadata_path = metadata_path_for(str(metadata_path) if metadata_path else None, candidate)
    manifest = read_json(manifest_path)
    metadata = read_json(metadata_path)
    payloads = flattened_sources(manifest, metadata)

    markers = do_not_publish_markers(candidate)
    unsafe_paths = collect_unsafe_paths(candidate) if candidate.exists() else []
    file_safe_explicit, file_safe_present = bool_field(payloads, {"file_safe", "files_safe", "safe_files"})
    structural_file_safe = candidate.exists() and not unsafe_paths
    file_safe = structural_file_safe and (file_safe_explicit if file_safe_present else True)

    structural_complete = artifact_content_complete(candidate, manifest_path, manifest, metadata)
    content_explicit, content_present = bool_field(
        payloads,
        {"content_complete", "contentComplete", "filesystem_complete", "complete"},
    )
    if content_present:
        content_complete = candidate.exists() and content_explicit
    else:
        content_complete = structural_complete and (content_explicit if content_present else True)

    signed_valid, signed_explicit, signature_material = signed_manifest_status(payloads, manifest)
    finalized_source, finalized_present = bool_field(
        payloads,
        {"finalized_source", "source_finalized", "consistent_final_stopped_sync", "final_stopped_sync", "finalized"},
    )
    active_single, _ = bool_field(
        payloads,
        {"source_was_active_single_mining_node", "active_single_mining_node", "active_mining_node_source"},
    )
    independent_anchor, independent_present = bool_field(
        payloads,
        {
            "independent_anchor_match",
            "independent_anchor_validated",
            "reference_anchor_match",
            "external_anchor_match",
            "consensus_anchor_match",
        },
    )
    offline_db_open, offline_present = bool_field(payloads, {"offline_db_open", "db_open", "database_opened"})
    state_root_ok, state_root_source = state_root_nonzero(payloads)
    restore_trial, restore_trial_present = bool_field(
        payloads,
        {"restore_trial_passed", "trial_restore_passed", "disposable_restore_passed"},
    )
    consensus_validated, consensus_present = bool_field(
        payloads,
        {"consensus_validated", "consensus_validation_passed", "reference_consensus_validated"},
    )
    mineable_validated, mineable_present = bool_field(
        payloads,
        {"mineable_validated", "mineable_validation_passed", "submit_readiness_validated"},
    )

    blockers: list[str] = []
    if not candidate.exists():
        append_unique(blockers, "candidate_missing")
    if not file_safe:
        append_unique(blockers, "file_safety_failed")
    if not content_complete:
        append_unique(blockers, "content_incomplete")
    if not signed_valid:
        if not signature_material:
            append_unique(blockers, "unsigned_manifest")
        elif signed_explicit:
            append_unique(blockers, "signed_manifest_validation_failed")
        else:
            append_unique(blockers, "signed_manifest_validation_missing")
    if markers:
        append_unique(blockers, "do_not_publish_present")
    artifact_type = artifact_type_value(manifest)
    if artifact_type and artifact_type not in SUPPORTED_ARTIFACT_TYPES:
        append_unique(blockers, f"unsupported_artifact_type:{artifact_type}")
    if not finalized_source:
        append_unique(blockers, field_reason("finalized_source", finalized_source, finalized_present))
    if active_single:
        append_unique(blockers, "active_single_mining_node_source")
    if not independent_anchor:
        append_unique(blockers, field_reason("independent_anchor_match", independent_anchor, independent_present))
    if not offline_db_open:
        append_unique(blockers, field_reason("offline_db_open", offline_db_open, offline_present))
    if not state_root_ok:
        append_unique(blockers, "state_root_zero_or_missing")
    if not restore_trial:
        append_unique(blockers, field_reason("restore_trial_passed", restore_trial, restore_trial_present))
    if not consensus_validated:
        append_unique(blockers, field_reason("consensus_validated", consensus_validated, consensus_present))
    if require_mineable and not mineable_validated:
        append_unique(blockers, field_reason("mineable_validated", mineable_validated, mineable_present))
    for reason in explicit_mismatch_blockers(payloads):
        append_unique(blockers, reason)
    for reason in hard_text_blockers(manifest, metadata):
        append_unique(blockers, reason)

    do_not_publish_absent = not markers
    payload = {
        "schema_version": 1,
        "policy_version": POLICY_VERSION,
        "generated_at": now_iso(),
        "candidate": str(candidate),
        "candidate_type": candidate_type,
        "manifest_path": str(manifest_path) if manifest_path else None,
        "metadata_path": str(metadata_path) if metadata_path else None,
        "reference_rpc_url_configured": bool(reference_rpc_url),
        "reference_rpc_policy": "not_contacted_by_policy_schema_slice",
        "file_safe": file_safe,
        "content_complete": content_complete,
        "signed_manifest_valid": signed_valid,
        "do_not_publish_absent": do_not_publish_absent,
        "finalized_source": finalized_source,
        "source_was_active_single_mining_node": active_single,
        "independent_anchor_match": independent_anchor,
        "offline_db_open": offline_db_open,
        "state_root_nonzero_expected_blocks": state_root_ok,
        "restore_trial_passed": restore_trial,
        "consensus_validated": consensus_validated,
        "mineable_validated": mineable_validated,
        "promotable": not blockers,
        "blocking_reasons": blockers,
        "evidence": {
            "manifest_loaded": bool(manifest),
            "manifest_signature_present": signature_material,
            "metadata_loaded": bool(metadata),
            "file_safe_explicit": file_safe_present,
            "content_complete_explicit": content_present,
            "state_root_source": state_root_source,
            "do_not_publish_markers": markers,
            "unsafe_paths": unsafe_paths[:200],
            "unsafe_path_count": len(unsafe_paths),
            "structural_file_safe": structural_file_safe,
            "structural_content_complete": structural_complete,
            "require_mineable": require_mineable,
        },
    }
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, help="Restore candidate directory or manifest path")
    parser.add_argument("--type", default="artifact", choices=("artifact",), help="Restore candidate type")
    parser.add_argument("--metadata", help="Optional JSON metadata/validation evidence file")
    parser.add_argument("--reference-rpc-url", default=os.environ.get("BDAG_CHAIN_REFERENCE_RPC_URL", ""))
    parser.add_argument("--require-mineable", action="store_true", help="Also require explicit mineable validation evidence")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    candidate = resolve_path(args.candidate)
    metadata = resolve_path(args.metadata) if args.metadata else None
    assert candidate is not None
    payload = validate_candidate(
        candidate,
        args.type,
        metadata_path=metadata,
        reference_rpc_url=args.reference_rpc_url,
        require_mineable=args.require_mineable,
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        state = "promotable" if payload["promotable"] else "blocked"
        print(f"{state}: {payload['candidate']}")
        for reason in payload["blocking_reasons"]:
            print(f"- {reason}")
    return 0 if payload["promotable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

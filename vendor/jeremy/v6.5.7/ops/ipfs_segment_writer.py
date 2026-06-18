#!/usr/bin/env python3
"""Low-priority append-only IPFS segment writer for BlockDAG chain-order data.

This local writer publishes verified finalized order-range segments. IPFS/IPNS
are byte transport only; normal chain consensus remains authoritative.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(os.environ.get("BDAG_PROJECT_ROOT") or Path(__file__).resolve().parents[1]).resolve()
ENV_FILE = Path(os.environ.get("BDAG_ENV_FILE") or ROOT / ".env")
OPS_DIR = ROOT / "ops"
MAINNET_NETWORK = "mainnet"

FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}


class RetryableDefer(RuntimeError):
    """A transient condition that should be retried by the next timer tick."""


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    env.update({key: value for key, value in os.environ.items() if key.startswith("BDAG_") or key in {"IPFS_PATH"}})
    return env


def env_bool(env: Mapping[str, str], key: str, default: bool = False) -> bool:
    value = str(env.get(key, "")).strip().lower()
    if not value:
        return default
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    return default


def env_int(env: Mapping[str, str], key: str, default: int) -> int:
    value = str(env.get(key, "")).strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def mainnet_network(env: Mapping[str, str]) -> str:
    requested = str(env.get("BDAG_NETWORK") or MAINNET_NETWORK).strip().lower()
    if requested != MAINNET_NETWORK:
        raise RuntimeError(f"IPFS segment writer refuses non-mainnet network: {requested}")
    return MAINNET_NETWORK


def resolve_path(value: str | None, default: Path) -> Path:
    if not value:
        return default.resolve()
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def canonical_json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=str(path.parent), delete=False) as handle:
        handle.write(data)
        tmp = Path(handle.name)
    tmp.replace(path)


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_bytes(path, canonical_json_bytes(payload))


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_command(command: list[str], timeout: int, env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
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


def is_timeout_exception(exc: BaseException) -> bool:
    if isinstance(exc, subprocess.TimeoutExpired):
        return True
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text


def is_retryable_exception(exc: BaseException) -> bool:
    return isinstance(exc, RetryableDefer) or is_timeout_exception(exc)


def ipfs_binary(env: Mapping[str, str]) -> str:
    return str(env.get("BDAG_IPFS_BINARY") or "ipfs")


def parse_cid(stdout: str) -> str:
    lines = [line.strip().split()[0] for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("ipfs add returned no CID")
    return lines[-1]


def ipfs_add(path: Path, env: Mapping[str, str], timeout_key: str = "BDAG_IPFS_SEGMENT_IPFS_TIMEOUT") -> str:
    add_args = shlex.split(
        str(env.get("BDAG_IPFS_SEGMENT_ADD_ARGS") or "--cid-version=1 --raw-leaves --pin=true --quieter")
    )
    result = run_command([ipfs_binary(env), "add", *add_args, str(path)], env_int(env, timeout_key, 600), env)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "ipfs add failed").strip())
    return parse_cid(result.stdout)


def ipfs_cat_sha256(cid: str, env: Mapping[str, str]) -> str:
    result = run_command(
        [ipfs_binary(env), "cat", f"/ipfs/{cid}"],
        env_int(env, "BDAG_IPFS_SEGMENT_IPFS_TIMEOUT", 600),
        env,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or f"ipfs cat failed for {cid}").strip())
    return hashlib.sha256(result.stdout.encode("utf-8")).hexdigest()


def ipfs_pin_present(cid: str, env: Mapping[str, str]) -> bool:
    result = run_command(
        [ipfs_binary(env), "pin", "ls", "--type=recursive", cid],
        env_int(env, "BDAG_IPFS_SEGMENT_IPFS_TIMEOUT", 600),
        env,
    )
    return result.returncode == 0


def ipfs_peer_id(env: Mapping[str, str]) -> str:
    result = run_command([ipfs_binary(env), "id", "-f", "<id>"], 30, env)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def publish_ipns(index_cid: str, env: Mapping[str, str]) -> dict[str, Any] | None:
    if not env_bool(env, "BDAG_IPFS_SEGMENT_PUBLISH_IPNS", False):
        return None
    command = [ipfs_binary(env), "name", "publish"]
    key = str(env.get("BDAG_IPFS_SEGMENT_IPNS_KEY") or env.get("BDAG_IPFS_CONTENT_IPNS_KEY") or "self").strip()
    if key:
        command.extend(["--key", key])
    ttl = str(env.get("BDAG_IPFS_SEGMENT_IPNS_TTL") or env.get("BDAG_IPFS_CONTENT_IPNS_TTL") or "1m").strip()
    if ttl:
        command.extend(["--ttl", ttl])
    lifetime = str(env.get("BDAG_IPFS_SEGMENT_IPNS_LIFETIME") or env.get("BDAG_IPFS_CONTENT_IPNS_LIFETIME") or "8760h").strip()
    if lifetime:
        command.extend(["--lifetime", lifetime])
    command.append(f"/ipfs/{index_cid}")
    result = run_command(command, env_int(env, "BDAG_IPFS_SEGMENT_IPNS_TIMEOUT", 300), env)
    return {
        "command": command[:3] + ["..."],
        "ok": result.returncode == 0,
        "stdout": result.stdout.strip()[-1000:],
        "stderr": result.stderr.strip()[-1000:],
    }


def status_path(env: Mapping[str, str]) -> Path:
    return resolve_path(env.get("BDAG_IPFS_SEGMENT_STATUS_FILE"), ROOT / "ops/runtime/ipfs-content/segment-writer-status.json")


def write_status(env: Mapping[str, str], state: str, **extra: Any) -> dict[str, Any]:
    payload = {
        "generated_at": now_iso(),
        "state": state,
        "mode": env.get("BDAG_IPFS_SEGMENT_WRITER_MODE", "auto"),
        "project_root": str(ROOT),
        "trust_model": "ipfs_is_untrusted_transport_chain_consensus_and_segment_validation_are_authoritative",
    }
    payload.update(extra)
    atomic_write_json(status_path(env), payload)
    return payload


def maintenance_allowed(env: Mapping[str, str]) -> dict[str, Any]:
    if env_bool(env, "BDAG_IPFS_SEGMENT_SKIP_MAINTENANCE_DECISION", False):
        return {"allowed": True, "reasons": [], "skipped": True}
    try:
        sys.path.insert(0, str(OPS_DIR))
        from pool_ops import background_maintenance_decision, collect_status_cached  # type: ignore

        return background_maintenance_decision(
            "ipfs_segment_writer",
            collect_status_cached(include_logs=False),
        )
    except Exception as exc:  # pragma: no cover - integration fallback.
        return {"allowed": False, "reasons": [f"maintenance gate unavailable: {exc}"], "error": str(exc)}


def import_pool_ops() -> Any:
    sys.path.insert(0, str(OPS_DIR))
    import pool_ops  # type: ignore

    return pool_ops


def segment_dir(env: Mapping[str, str]) -> Path:
    return resolve_path(env.get("BDAG_IPFS_SEGMENT_DIR"), ROOT / "ops/runtime/ipfs-content/segments")


def latest_index_path(env: Mapping[str, str]) -> Path:
    return resolve_path(env.get("BDAG_IPFS_SEGMENT_INDEX_PATH") or env.get("BDAG_IPFS_CONTENT_LATEST_INDEX_PATH"), ROOT / "ops/runtime/ipfs-content/latest-index.json")


def discovery_path(env: Mapping[str, str]) -> Path:
    return resolve_path(env.get("BDAG_IPFS_CONTENT_DISCOVERY_FILE"), ROOT / "ops/ipfs-content-discovery.json")


def current_head(index: Mapping[str, Any]) -> dict[str, Any] | None:
    head = index.get("current_head")
    return head if isinstance(head, dict) else None


def segments(index: Mapping[str, Any]) -> list[dict[str, Any]]:
    values = index.get("segments")
    if not isinstance(values, list):
        return []
    return [item for item in values if isinstance(item, dict)]


def deprecated_tip_order(index: Mapping[str, Any]) -> int | None:
    for item in index.get("deprecated_content") or []:
        if isinstance(item, dict):
            value = item.get("tip_order")
            if isinstance(value, int):
                return value
    return None


def choose_next_range(index: Mapping[str, Any], latest_order: int, env: Mapping[str, str]) -> tuple[int, int, int, str]:
    finality_lag = max(0, env_int(env, "BDAG_IPFS_SEGMENT_FINALITY_LAG_ORDERS", 600))
    orders_per_segment = max(1, env_int(env, "BDAG_IPFS_SEGMENT_ORDERS_PER_SEGMENT", 300))
    safe_tip = latest_order - finality_lag
    if safe_tip < 1:
        return 0, 0, safe_tip, "safe_tip_not_available"

    head = current_head(index)
    if head and isinstance(head.get("end_order"), int):
        start = int(head["end_order"]) + 1
        reason = "append_after_current_head"
    else:
        configured = env_int(env, "BDAG_IPFS_SEGMENT_START_ORDER", 0)
        policy = str(env.get("BDAG_IPFS_SEGMENT_START_POLICY") or "live_tail").strip().lower()
        if configured > 0:
            start = configured
            reason = "configured_start_order"
        elif policy == "after_deprecated":
            start = (deprecated_tip_order(index) or 0) + 1
            reason = "after_deprecated_tip"
        else:
            start = max(1, safe_tip - orders_per_segment + 1)
            reason = "live_tail_start"
    if start > safe_tip:
        return start, 0, safe_tip, "waiting_for_next_finalized_range"
    return start, min(start + orders_per_segment - 1, safe_tip), safe_tip, reason


def normalize_block(block: Mapping[str, Any]) -> dict[str, Any]:
    wanted = (
        "hash",
        "txsvalid",
        "confirmations",
        "version",
        "weight",
        "height",
        "txRoot",
        "order",
        "stateRoot",
        "bits",
        "difficulty",
        "pow",
        "timestamp",
        "parentroot",
        "parents",
        "children",
    )
    return {key: block.get(key) for key in wanted if key in block}


def fetch_block_record(pool_ops: Any, url: str, order: int, env: Mapping[str, str]) -> dict[str, Any]:
    timeout = env_int(env, "BDAG_IPFS_SEGMENT_RPC_TIMEOUT", 8)
    attempts = max(1, env_int(env, "BDAG_IPFS_SEGMENT_BLOCK_RPC_RETRIES", 2) + 1)
    last_error: BaseException | None = None
    for attempt in range(attempts):
        try:
            reference = pool_ops.fetch_chain_order_reference(url, order, timeout=timeout)
            block = reference["block"]
            raw_hex = pool_ops.mining_rpc_call(url, "getBlockByOrder", [order, False, True], timeout=timeout)
            if not isinstance(raw_hex, str) or not raw_hex:
                raise RuntimeError(f"getBlockByOrder raw response for order {order} was not a non-empty hex string")
            return {
                "order": order,
                "hash": reference["hash"],
                "header": normalize_block(block),
                "raw_block_hex": raw_hex,
                "raw_block_sha256": hashlib.sha256(raw_hex.encode("ascii")).hexdigest(),
            }
        except Exception as exc:  # noqa: BLE001 - retry transient RPC timeouts only.
            last_error = exc
            if attempt + 1 < attempts and is_timeout_exception(exc):
                time.sleep(min(2.0, 0.25 * (attempt + 1)))
                continue
            raise
    raise RuntimeError(str(last_error) if last_error else f"failed to fetch order {order}")


def fetch_segment_blocks(pool_ops: Any, url: str, start: int, end: int, env: Mapping[str, str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    rpc_per_second = max(0, env_int(env, "BDAG_IPFS_SEGMENT_MAX_RPC_PER_SECOND", 25))
    delay = 1.0 / rpc_per_second if rpc_per_second else 0.0
    for order in range(start, end + 1):
        records.append(fetch_block_record(pool_ops, url, order, env))
        if delay:
            time.sleep(delay)
    return records


def next_segment_id(index: Mapping[str, Any]) -> int:
    values = segments(index)
    if not values:
        return 1
    return max(int(item.get("segment_id") or 0) for item in values) + 1


def select_rpc_source(
    pool_ops: Any,
    env: Mapping[str, str],
    min_order: int = 0,
) -> tuple[str, str, int, str]:
    errors: list[str] = []
    for name, url in pool_ops.mining_rpc_urls():
        try:
            latest_order, method = pool_ops.fetch_chain_order_tip(url, timeout=env_int(env, "BDAG_IPFS_SEGMENT_RPC_TIMEOUT", 8))
            if min_order and latest_order < min_order:
                errors.append(f"{name}: tip order {latest_order} is behind current index head {min_order}")
                continue
            return name, url, latest_order, method
        except Exception as exc:  # noqa: BLE001 - try next source.
            errors.append(f"{name}: {exc}")
    message = "; ".join(errors) or "no mining RPC sources available"
    if min_order and "behind current index head" in message:
        raise RetryableDefer(message)
    raise RuntimeError(message)


def chain_reference_rpc_url(env: Mapping[str, str]) -> str:
    return str(env.get("BDAG_CHAIN_REFERENCE_RPC_URL") or env.get("BDAG_IPFS_SEGMENT_REFERENCE_RPC_URL") or "").strip()


def normal_publish_requires_preflight(env: Mapping[str, str]) -> bool:
    return True


def run_preflight(
    env: Mapping[str, str],
    index_path: Path,
    source_url: str,
    reference_url: str,
    start: int,
    end: int,
) -> dict[str, Any]:
    sys.path.insert(0, str(OPS_DIR))
    import chain_integrity_gate  # type: ignore

    config = {
        "workflow": "ipfs_segment_writer",
        "source_rpc_url": source_url,
        "reference_rpc_url": reference_url,
        "index": str(index_path),
        "start_order": start,
        "end_order": end,
    }
    return chain_integrity_gate.evaluate_chain_integrity(config, env=env)


def require_trusted_preflight(
    env: Mapping[str, str],
    index_path: Path,
    source_url: str,
    reference_url: str,
    start: int,
    end: int,
) -> dict[str, Any]:
    if not normal_publish_requires_preflight(env):
        return {"state": "skipped", "trusted": True, "reasons": ["preflight_requirement_disabled"]}
    preflight = run_preflight(env, index_path, source_url, reference_url, start, end)
    if preflight.get("state") != "trusted":
        state = str(preflight.get("state") or "failed")
        reasons = preflight.get("reasons")
        if not isinstance(reasons, list) or not reasons:
            reasons = [state]
        raise RetryableDefer(
            "chain integrity preflight not trusted: "
            f"{state}: {'; '.join(str(item) for item in reasons)}"
        )
    return preflight


def add_checked_json(path: Path, payload: Any, env: Mapping[str, str]) -> tuple[str, str, int]:
    raw = canonical_json_bytes(payload)
    atomic_write_bytes(path, raw)
    expected_sha = sha256_bytes(raw)
    cid = ipfs_add(path, env)
    if not ipfs_pin_present(cid, env):
        raise RuntimeError(f"CID {cid} is not recursively pinned after ipfs add")
    actual_sha = ipfs_cat_sha256(cid, env)
    if actual_sha != expected_sha:
        raise RuntimeError(f"safe-copy check failed for {cid}: {actual_sha} != {expected_sha}")
    return cid, expected_sha, len(raw)


def first_last_timestamps(blocks: list[dict[str, Any]]) -> tuple[Any, Any]:
    first = blocks[0].get("header", {}).get("timestamp") if blocks else None
    last = blocks[-1].get("header", {}).get("timestamp") if blocks else None
    return first, last


def build_segment(
    pool_ops: Any,
    source_name: str,
    source_url: str,
    start: int,
    end: int,
    index: Mapping[str, Any],
    env: Mapping[str, str],
) -> dict[str, Any]:
    blocks = fetch_segment_blocks(pool_ops, source_url, start, end, env)
    if len(blocks) != end - start + 1:
        raise RuntimeError(f"expected {end - start + 1} blocks, fetched {len(blocks)}")
    seg_id = next_segment_id(index)
    seg_name = f"segment-{seg_id:012d}-{start}-{end}"
    out_dir = segment_dir(env)
    payload_path = out_dir / f"{seg_name}.payload.json"
    manifest_path = out_dir / f"{seg_name}.manifest.json"

    payload = {
        "document_type": "bdag_chain_order_segment_payload_v1",
        "network": mainnet_network(env),
        "segment_id": seg_id,
        "start_order": start,
        "end_order": end,
        "block_count": len(blocks),
        "build_algorithm": "getBlockByOrder_verbose_header_plus_raw_block_hex_v1",
        "blocks": blocks,
    }
    payload_cid, payload_sha, payload_size = add_checked_json(payload_path, payload, env)
    first_ts, last_ts = first_last_timestamps(blocks)
    head = current_head(index)
    previous_manifest_cid = str(head.get("manifest_cid") or "") if head else ""
    base_anchor_order = start - 1
    base_anchor_hash = ""
    if base_anchor_order >= 1:
        try:
            base_anchor_hash = pool_ops.fetch_chain_order_reference(
                source_url,
                base_anchor_order,
                timeout=env_int(env, "BDAG_IPFS_SEGMENT_RPC_TIMEOUT", 8),
            )["hash"]
        except Exception:
            base_anchor_hash = ""

    manifest = {
        "document_type": "bdag_ipfs_segment_manifest_v1",
        "network": mainnet_network(env),
        "generated_at": now_iso(),
        "segment_id": seg_id,
        "start_order": start,
        "end_order": end,
        "block_count": len(blocks),
        "start_hash": blocks[0]["hash"],
        "end_hash": blocks[-1]["hash"],
        "start_timestamp": first_ts,
        "end_timestamp": last_ts,
        "previous_segment_manifest_cid": previous_manifest_cid or None,
        "base_anchor_order": base_anchor_order,
        "base_anchor_hash": base_anchor_hash or None,
        "payload_cid": payload_cid,
        "payload_sha256": payload_sha,
        "payload_size_bytes": payload_size,
        "payload_format": "bdag_chain_order_segment_payload_v1",
        "source": {
            "rpc_source": source_name,
            "rpc_method": "getBlockByOrder",
        },
        "writer": {
            "mode": "local_writer",
            "kubo_peer_id": ipfs_peer_id(env),
            "ipns_name": env.get("BDAG_IPFS_CONTENT_LATEST_IPNS", ""),
        },
        "election": {
            "phase": "local_writer",
            "rule": "this deployment writes verified finalized segments from its local node",
            "fallback": "timer retries after maintenance pressure clears",
        },
        "trust_model": "CID and sha256 verify bytes; receivers must still verify chain consensus and segment continuity.",
    }
    manifest_cid, manifest_sha, manifest_size = add_checked_json(manifest_path, manifest, env)
    return {
        "segment_id": seg_id,
        "start_order": start,
        "end_order": end,
        "block_count": len(blocks),
        "start_hash": blocks[0]["hash"],
        "end_hash": blocks[-1]["hash"],
        "start_timestamp": first_ts,
        "end_timestamp": last_ts,
        "payload_cid": payload_cid,
        "payload_sha256": payload_sha,
        "payload_size_bytes": payload_size,
        "manifest_cid": manifest_cid,
        "manifest_sha256": manifest_sha,
        "manifest_path": str(manifest_path),
        "payload_path": str(payload_path),
        "writer_mode": "local_writer",
    }


def update_index(index: dict[str, Any], segment_record: dict[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    existing_segments = segments(index)
    now = now_iso()
    if not index:
        index = {
            "document_type": "bdag_ipfs_segment_index_v1",
            "network": mainnet_network(env),
            "append_only_model": {
                "immutable_segments": True,
                "old_segments_never_change": True,
                "latest_index_changes_on_append": True,
                "stable_latest_pointer": env.get("BDAG_IPFS_CONTENT_LATEST_IPNS", ""),
            },
            "deprecated_content": [],
        }
    first_start = segment_record["start_order"] if not existing_segments else existing_segments[0].get("start_order")
    index.update(
        {
            "generated_at": now,
            "status": "active_single_writer_segments",
            "index_sequence": len(existing_segments) + 1,
            "chain_data_status": "live_tail_segments_publishing",
            "trust_model": "IPFS and IPNS are byte transport only. Segment payload CIDs, sha256, manifest links, order continuity, and normal consensus validation are authoritative.",
            "current_head": {
                "segment_id": segment_record["segment_id"],
                "start_order": segment_record["start_order"],
                "end_order": segment_record["end_order"],
                "end_hash": segment_record["end_hash"],
                "manifest_cid": segment_record["manifest_cid"],
                "payload_cid": segment_record["payload_cid"],
                "updated_at": now,
            },
            "history_completeness": {
                "complete_from_order": first_start,
                "backfill_required_before_order": first_start if first_start and int(first_start) > 1 else None,
                "note": "Phase-1 writer starts from configured/current tail. Earlier history must be backfilled or supplied by a verified snapshot before full bootstrap use.",
            },
            "notes": [
                "This phase-1 index contains live-tail chain-order segments written by the local single writer.",
                "Earlier history before history_completeness.complete_from_order is not complete yet and must be backfilled or supplied by a verified snapshot before full bootstrap use.",
                "New nodes should resolve the stable IPNS pointer, fetch this index, verify every segment CID/sha256/order link, and then rely on normal chain consensus.",
            ],
            "publisher_policy": {
                "phase": "phase_1_single_local_writer",
                "current_writer": "this_pool_local_node",
                "future_policy": "deterministic finalized-PoW-winner roster with fallback slots",
            },
        }
    )
    index["segments"] = [*existing_segments, segment_record]
    return index


def update_discovery(index_cid: str, index: Mapping[str, Any], env: Mapping[str, str]) -> None:
    path = discovery_path(env)
    data = load_json(path)
    if not data:
        return
    data["updated_at"] = now_iso()
    data["current_latest_index_cid"] = index_cid
    data["current_latest_index_uri"] = f"ipfs://{index_cid}"
    policy = data.get("ipns_publish_policy")
    if isinstance(policy, dict):
        policy["published_value"] = f"/ipfs/{index_cid}"
        policy["last_verified_nocache_at"] = now_iso()
        policy["republish_timer"] = "bdag-ipfs-segment-writer.timer"
        policy["republish_interval"] = "5m plus up to 60s randomized delay when background maintenance is allowed"
    data["current_content"] = {
        "document_type": "bdag_ipfs_segment_index_v1",
        "status": index.get("status"),
        "segments": len(segments(index)),
        "current_head": index.get("current_head"),
        "history_completeness": index.get("history_completeness"),
    }
    atomic_write_json(path, data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="evaluate the next segment without writing IPFS data")
    parser.add_argument("--preflight", action="store_true", help="validate the bounded next segment against an independent reference without Kubo/IPNS mutation")
    parser.add_argument("--source-rpc-url", default="", help="direct source backend RPC URL for --preflight")
    parser.add_argument("--reference-rpc-url", default="", help="independent reference RPC URL for --preflight")
    parser.add_argument("--start-order", type=int, default=0, help="explicit start order for --preflight")
    parser.add_argument("--end-order", type=int, default=0, help="explicit end order for --preflight")
    parser.add_argument("--index", default="", help="segment index path override for --preflight")
    parser.add_argument("--json", action="store_true", help="print final status JSON")
    args = parser.parse_args(argv)

    env = load_env()
    mode = str(env.get("BDAG_IPFS_SEGMENT_WRITER_MODE") or "auto").strip().lower()
    if mode in FALSE_VALUES:
        payload = write_status(env, "disabled", reasons=["mode_disabled"])
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if not args.preflight:
        decision = maintenance_allowed(env)
        if not decision.get("allowed", False):
            payload = write_status(env, "deferred", reasons=decision.get("reasons") or ["background_maintenance_denied"], maintenance_decision=decision)
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            return 0

    try:
        pool_ops = import_pool_ops()
        index_path = resolve_path(args.index, latest_index_path(env)) if args.index else latest_index_path(env)
        index = load_json(index_path)
        head = current_head(index)
        min_order = int(head.get("end_order") or 0) if head else 0
        explicit_range = bool(args.start_order and args.end_order)
        source_name = "configured"
        tip_method = ""
        source_url = str(args.source_rpc_url or env.get("BDAG_CHAIN_SOURCE_RPC_URL") or "").strip()
        latest_order = 0
        if args.preflight and source_url and explicit_range:
            tip_method = "not_sampled_explicit_preflight_range"
        elif args.preflight and source_url:
            latest_order, tip_method = pool_ops.fetch_chain_order_tip(
                source_url,
                timeout=env_int(env, "BDAG_IPFS_SEGMENT_RPC_TIMEOUT", 8),
            )
        else:
            source_name, source_url, latest_order, tip_method = select_rpc_source(
                pool_ops,
                env,
                min_order=min_order,
            )
        if explicit_range:
            start, end = args.start_order, args.end_order
            finality_lag = max(0, env_int(env, "BDAG_IPFS_SEGMENT_FINALITY_LAG_ORDERS", 600))
            safe_tip = latest_order - finality_lag if latest_order > 0 else None
            range_reason = "explicit_preflight_range" if args.preflight else "explicit_range"
        else:
            start, end, safe_tip, range_reason = choose_next_range(index, latest_order, env)
        if end <= 0:
            payload = write_status(
                env,
                "waiting_for_finalized_range",
                latest_order=latest_order,
                safe_tip=safe_tip,
                next_start_order=start,
                reason=range_reason,
                rpc_source=source_name,
                tip_method=tip_method,
            )
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        if args.preflight:
            reference_url = str(args.reference_rpc_url or chain_reference_rpc_url(env)).strip()
            preflight = run_preflight(env, index_path, source_url, reference_url, start, end)
            payload = write_status(
                env,
                str(preflight.get("state") or "failed"),
                action="preflight",
                rpc_source=source_name,
                source_url=preflight.get("source_url") or "",
                reference_url=preflight.get("reference_url") or "",
                latest_order=latest_order,
                safe_tip=safe_tip,
                next_start_order=start,
                next_end_order=end,
                range_reason=range_reason,
                chain_integrity=preflight,
                mutation_policy="no_ipfs_add_pin_cat_ipns_or_index_write_in_preflight",
            )
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            return 0 if preflight.get("state") == "trusted" or str(preflight.get("state") or "").startswith("deferred_") else 1
        if args.dry_run:
            payload = write_status(
                env,
                "ready",
                action="dry_run",
                rpc_source=source_name,
                latest_order=latest_order,
                safe_tip=safe_tip,
                next_start_order=start,
                next_end_order=end,
                range_reason=range_reason,
            )
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            return 0

        reference_url = chain_reference_rpc_url(env)
        publish_preflight: dict[str, Any] = {}
        publish_preflights: list[dict[str, Any]] = []
        max_segments = max(1, env_int(env, "BDAG_IPFS_SEGMENT_MAX_SEGMENTS_PER_RUN", 1))
        written: list[dict[str, Any]] = []
        current_index = dict(index)
        current_start, current_end = start, end
        for _ in range(max_segments):
            if current_end <= 0:
                break
            publish_preflight = require_trusted_preflight(env, index_path, source_url, reference_url, current_start, current_end)
            publish_preflights.append(publish_preflight)
            record = build_segment(pool_ops, source_name, source_url, current_start, current_end, current_index, env)
            current_index = update_index(current_index, record, env)
            atomic_write_json(index_path, current_index)
            written.append(record)
            current_start, current_end, safe_tip, range_reason = choose_next_range(current_index, latest_order, env)
        index_cid, index_sha, index_size = add_checked_json(index_path, current_index, env)
        update_discovery(index_cid, current_index, env)
        ipns = publish_ipns(index_cid, env)
        state = "published" if written else "waiting_for_finalized_range"
        payload = write_status(
            env,
            state,
            action="segment_append",
            segments_written=len(written),
            written_segments=written,
            index_cid=index_cid,
            index_sha256=index_sha,
            index_size_bytes=index_size,
            ipns=ipns,
            latest_order=latest_order,
            safe_tip=safe_tip,
            next_start_order=current_start,
            next_end_order=current_end,
            rpc_source=source_name,
            tip_method=tip_method,
            chain_integrity=publish_preflight,
            chain_integrity_preflights=publish_preflights,
        )
    except Exception as exc:
        transient_retry = is_retryable_exception(exc)
        payload = write_status(
            env,
            "deferred" if transient_retry else "failed",
            reasons=[str(exc)],
            exception_type=type(exc).__name__,
            retrying=transient_retry,
        )
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if transient_retry else 1

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

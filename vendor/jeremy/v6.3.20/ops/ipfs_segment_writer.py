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
sys.path.insert(0, str(OPS_DIR))
import ipfs_segment_trust  # type: ignore  # noqa: E402

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


def index_from_discovery(discovery: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    content = discovery.get("current_content")
    if not isinstance(content, Mapping):
        return {}
    if content.get("document_type") != "bdag_ipfs_segment_index_v1":
        return {}
    head = content.get("current_head")
    if not isinstance(head, Mapping) or not isinstance(head.get("end_order"), int):
        return {}
    history = content.get("history_completeness")
    index = {
        "document_type": "bdag_ipfs_segment_index_v1",
        "network": mainnet_network(env),
        "status": content.get("status") or "active_deterministic_writer_segments",
        "current_head": dict(head),
        "segments": [],
        "recovered_from_discovery": True,
        "recovered_from_discovery_at": now_iso(),
        "recovered_latest_index_cid": discovery.get("current_latest_index_cid") or "",
    }
    if isinstance(history, Mapping):
        index["history_completeness"] = dict(history)
    return index


def load_index_with_discovery(index_path: Path, env: Mapping[str, str], *, use_discovery: bool = True) -> dict[str, Any]:
    index = load_json(index_path)
    if current_head(index):
        return index
    if not use_discovery:
        return index
    discovery = load_json(discovery_path(env))
    latest_index_cid = str(discovery.get("current_latest_index_cid") or "").strip()
    if latest_index_cid:
        previous_index = ipfs_cat_json(latest_index_cid, env)
        if current_head(previous_index):
            previous_index["recovered_from_discovery_cid"] = latest_index_cid
            previous_index["recovered_from_discovery_at"] = now_iso()
            return previous_index
    discovery_index = index_from_discovery(discovery, env)
    if discovery_index:
        return discovery_index
    return index


def discovered_latest_index_cid(env: Mapping[str, str]) -> str:
    discovery = load_json(discovery_path(env))
    return str(discovery.get("current_latest_index_cid") or "").strip()


def published_index_cid(index: Mapping[str, Any], env: Mapping[str, str]) -> str:
    for key in ("index_cid", "recovered_from_discovery_cid", "recovered_latest_index_cid"):
        value = str(index.get(key) or "").strip()
        if value:
            return value
    return discovered_latest_index_cid(env)


def attach_previous_index_link(
    index: Mapping[str, Any],
    previous_index_cid: str,
    previous_index: Mapping[str, Any],
    reason: str,
) -> dict[str, Any]:
    linked = dict(index)
    linked["append_only_index_policy"] = {
        "immutable_index_cids": True,
        "latest_pointer_is_mutable_discovery_only": True,
        "verification_rule": (
            "Resolve the latest IPNS pointer, then recursively verify previous_index_cid links, "
            "segment manifests, payload CIDs, sha256 values, order continuity, and normal chain consensus."
        ),
    }
    previous_index_cid = str(previous_index_cid or "").strip()
    if not previous_index_cid:
        return linked
    previous_head = current_head(previous_index) or {}
    linked["previous_index_cid"] = previous_index_cid
    linked["previous_index_link"] = {
        "document_type": "bdag_ipfs_segment_previous_index_link_v1",
        "index_cid": previous_index_cid,
        "linked_at": now_iso(),
        "reason": reason or "segment_append",
        "previous_current_head": dict(previous_head),
        "previous_history_completeness": dict(previous_index.get("history_completeness") or {})
        if isinstance(previous_index.get("history_completeness"), Mapping)
        else {},
    }
    return linked


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


def ipfs_cat_json(cid: str, env: Mapping[str, str]) -> dict[str, Any]:
    try:
        result = run_command(
            [ipfs_binary(env), "cat", f"/ipfs/{cid}"],
            env_int(env, "BDAG_IPFS_SEGMENT_IPFS_TIMEOUT", 600),
            env,
        )
    except Exception:
        return {}
    if result.returncode != 0:
        return {}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


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
    publish_mode = str(env.get("BDAG_IPFS_SEGMENT_PUBLISH_IPNS") or "auto").strip().lower()
    key = str(env.get("BDAG_IPFS_SEGMENT_IPNS_KEY") or env.get("BDAG_IPFS_CONTENT_IPNS_KEY") or "").strip()
    if publish_mode in FALSE_VALUES:
        return None
    if publish_mode == "auto" and not key:
        return {
            "ok": False,
            "skipped": True,
            "reason": "auto_publish_waiting_for_ipns_key",
            "index_cid": index_cid,
        }
    command = [ipfs_binary(env), "name", "publish"]
    command.extend(["--key", key or "self"])
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
    policy = str(env.get("BDAG_IPFS_SEGMENT_START_POLICY") or "live_tail").strip().lower()
    safe_tip = latest_order - finality_lag
    if safe_tip < 1:
        return 0, 0, safe_tip, "safe_tip_not_available"

    head = current_head(index)
    if head and isinstance(head.get("end_order"), int):
        head_end = int(head["end_order"])
        stale_reset_enabled = env_bool(env, "BDAG_IPFS_SEGMENT_STALE_HEAD_RESET_ENABLED", True)
        stale_reset_lag = max(
            orders_per_segment,
            env_int(env, "BDAG_IPFS_SEGMENT_STALE_HEAD_MAX_LAG_ORDERS", 3600),
        )
        if policy == "live_tail" and stale_reset_enabled and safe_tip - head_end > stale_reset_lag:
            start = max(1, safe_tip - orders_per_segment + 1)
            return start, min(start + orders_per_segment - 1, safe_tip), safe_tip, "stale_head_live_tail_reset"
        start = head_end + 1
        reason = "append_after_current_head"
    else:
        configured = env_int(env, "BDAG_IPFS_SEGMENT_START_ORDER", 0)
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


def reset_index_for_live_tail_epoch(
    index: Mapping[str, Any],
    start: int,
    end: int,
    latest_order: int,
    safe_tip: int | None,
    env: Mapping[str, str],
) -> dict[str, Any]:
    """Start a near-tip epoch when the stored live-tail seed is stale."""

    deprecated = [dict(item) for item in index.get("deprecated_content") or [] if isinstance(item, Mapping)]
    head = current_head(index)
    old_segments = segments(index)
    if head or old_segments:
        deprecated.append(
            {
                "type": "superseded_stale_live_tail_epoch",
                "superseded_at": now_iso(),
                "reason": "stale_head_live_tail_reset",
                "previous_current_head": dict(head or {}),
                "previous_segment_count": len(old_segments),
                "previous_history_completeness": dict(index.get("history_completeness") or {})
                if isinstance(index.get("history_completeness"), Mapping)
                else {},
                "previous_index_cid": index.get("recovered_from_discovery_cid")
                or index.get("recovered_latest_index_cid")
                or index.get("index_cid")
                or "",
            }
        )
    return {
        "document_type": "bdag_ipfs_segment_index_v1",
        "network": mainnet_network(env),
        "status": "active_deterministic_writer_segments",
        "chain_data_status": "live_tail_epoch_reset_pending",
        "append_only_model": {
            "immutable_segments": True,
            "old_segments_never_change": True,
            "latest_index_changes_on_append": True,
            "stable_latest_pointer": env.get("BDAG_IPFS_CONTENT_LATEST_IPNS", ""),
        },
        "deprecated_content": deprecated,
        "segments": [],
        "history_completeness": {
            "complete_from_order": start,
            "backfill_required_before_order": start if start > 1 else None,
            "note": (
                "Current live-tail epoch was reset near the finalized tip because the previous seed head was stale. "
                "Earlier history remains required for full bootstrap and must be supplied by verified backfill or a signed checkpoint."
            ),
        },
        "live_tail_epoch": {
            "reset_at": now_iso(),
            "start_order": start,
            "planned_end_order": end,
            "latest_order_at_reset": latest_order,
            "safe_tip_at_reset": safe_tip,
            "stale_head_reset_enabled": True,
            "stale_head_max_lag_orders": max(
                env_int(env, "BDAG_IPFS_SEGMENT_ORDERS_PER_SEGMENT", 300),
                env_int(env, "BDAG_IPFS_SEGMENT_STALE_HEAD_MAX_LAG_ORDERS", 3600),
            ),
        },
    }


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
    ids = [int(item.get("segment_id") or 0) for item in values]
    head = current_head(index)
    if head and isinstance(head.get("segment_id"), int):
        ids.append(int(head["segment_id"]))
    return max(ids, default=0) + 1


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


def public_rpc_urls(env: Mapping[str, str]) -> list[tuple[str, str]]:
    configured = str(env.get("BDAG_PUBLIC_RPC_URLS") or "").strip()
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in configured.replace("\n", ",").split(","):
        item = raw.strip()
        if not item:
            continue
        if "=" in item:
            name, url = item.split("=", 1)
        else:
            name, url = item, item
        name = name.strip() or url.strip()
        url = url.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        result.append((name, url))
    return result


def explicit_chain_reference_rpc_url(env: Mapping[str, str]) -> str:
    return str(env.get("BDAG_CHAIN_REFERENCE_RPC_URL") or env.get("BDAG_IPFS_SEGMENT_REFERENCE_RPC_URL") or "").strip()


def chain_reference_rpc_candidates(env: Mapping[str, str], source_url: str = "") -> list[str]:
    explicit = explicit_chain_reference_rpc_url(env)
    source = source_url.strip().rstrip("/")
    if explicit:
        candidate = explicit.strip()
        return [candidate] if candidate and candidate.rstrip("/") != source else []
    result: list[str] = []
    seen: set[str] = set()
    for _name, url in public_rpc_urls(env):
        candidate = url.strip()
        if not candidate or candidate.rstrip("/") == source or candidate in seen:
            continue
        seen.add(candidate)
        result.append(candidate)
    return result


def chain_reference_rpc_url(env: Mapping[str, str], source_url: str = "") -> str:
    candidates = chain_reference_rpc_candidates(env, source_url)
    return candidates[0] if candidates else ""


def parse_writer_roster(value: str | None) -> list[str]:
    if not value:
        return []
    seen: set[str] = set()
    roster: list[str] = []
    for raw in value.replace("\n", ",").split(","):
        writer = raw.strip()
        if "=" in writer:
            writer = writer.split("=", 1)[0].strip()
        elif ":" in writer:
            writer = writer.split(":", 1)[0].strip()
        if not writer or writer in seen:
            continue
        seen.add(writer)
        roster.append(writer)
    return sorted(roster)


def local_writer_id(env: Mapping[str, str]) -> str:
    configured = str(env.get("BDAG_IPFS_SEGMENT_WRITER_ID") or env.get("BDAG_IPFS_WRITER_ID") or "").strip()
    if configured:
        return configured
    return ipfs_peer_id(env)


def writer_election(
    env: Mapping[str, str],
    start: int,
    end: int,
    previous_manifest_cid: str = "",
) -> dict[str, Any]:
    """Return deterministic segment-writer eligibility for this finalized range."""

    rule = str(env.get("BDAG_IPFS_SEGMENT_WRITER_ELECTION_RULE") or "rendezvous_sha256_v1").strip()
    roster = parse_writer_roster(str(env.get("BDAG_IPFS_SEGMENT_WRITER_ROSTER") or env.get("BDAG_IPFS_WRITER_ROSTER") or ""))
    writer_id = local_writer_id(env)
    if not roster:
        return {
            "allowed": True,
            "mode": "bootstrap_single_writer",
            "rule": rule,
            "local_writer_id": writer_id,
            "selected_writer_id": writer_id or "local_bootstrap_writer",
            "roster_size": 0,
            "reason": "writer_roster_empty_bootstrap_seed_allows_local_writer",
        }
    if not writer_id:
        return {
            "allowed": False,
            "mode": "deterministic_roster",
            "rule": rule,
            "local_writer_id": "",
            "selected_writer_id": "",
            "roster_size": len(roster),
            "reason": "local_writer_id_unavailable",
        }
    seed = f"{MAINNET_NETWORK}|{start}|{end}|{previous_manifest_cid or '-'}"
    scores = [
        {
            "writer_id": candidate,
            "score": hashlib.sha256(f"{rule}|{seed}|{candidate}".encode("utf-8")).hexdigest(),
        }
        for candidate in roster
    ]
    selected = max(scores, key=lambda item: (item["score"], item["writer_id"]))["writer_id"]
    return {
        "allowed": selected == writer_id,
        "mode": "deterministic_roster",
        "rule": rule,
        "local_writer_id": writer_id,
        "selected_writer_id": selected,
        "roster_size": len(roster),
        "range": {"start_order": start, "end_order": end},
        "reason": "local_writer_selected" if selected == writer_id else "another_writer_selected",
    }


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


def preflight_reasons(preflight: Mapping[str, Any]) -> list[str]:
    reasons = preflight.get("reasons")
    if isinstance(reasons, list) and reasons:
        return [str(item) for item in reasons]
    state = str(preflight.get("state") or "failed")
    return [state]


def preflight_not_trusted_message(preflight: Mapping[str, Any]) -> str:
    state = str(preflight.get("state") or "failed")
    return f"{state}: {'; '.join(preflight_reasons(preflight))}"


def reference_unavailable_preflight(preflight: Mapping[str, Any]) -> bool:
    return str(preflight.get("state") or "").startswith("deferred_reference")


def require_trusted_preflight_from_candidates(
    env: Mapping[str, str],
    index_path: Path,
    source_url: str,
    reference_urls: list[str],
    start: int,
    end: int,
) -> dict[str, Any]:
    if not normal_publish_requires_preflight(env):
        return {"state": "skipped", "trusted": True, "reasons": ["preflight_requirement_disabled"]}
    if not reference_urls:
        return require_trusted_preflight(env, index_path, source_url, "", start, end)

    attempts: list[dict[str, Any]] = []
    last_deferred: Mapping[str, Any] | None = None
    for reference_url in reference_urls:
        preflight = run_preflight(env, index_path, source_url, reference_url, start, end)
        attempt = {
            "reference_url": reference_url,
            "state": str(preflight.get("state") or "failed"),
            "reasons": preflight_reasons(preflight),
        }
        attempts.append(attempt)
        if preflight.get("state") == "trusted":
            result = dict(preflight)
            result["selected_reference_url"] = reference_url
            result["reference_attempts"] = attempts
            return result
        if reference_unavailable_preflight(preflight):
            last_deferred = preflight
            continue
        raise RetryableDefer("chain integrity preflight not trusted: " + preflight_not_trusted_message(preflight))

    detail = preflight_not_trusted_message(last_deferred or {"state": "deferred_reference_unavailable"})
    raise RetryableDefer(
        "chain integrity preflight not trusted after "
        f"{len(attempts)} reference attempt(s): {detail}"
    )


def bootstrap_local_publish_allowed(env: Mapping[str, str], election: Mapping[str, Any]) -> bool:
    return (
        env_bool(env, "BDAG_IPFS_SEGMENT_BOOTSTRAP_LOCAL_PUBLISH", False)
        and env_bool(env, "BDAG_IPFS_SEGMENT_BOOTSTRAP_UNTRUSTED_PUBLISH", False)
        and election.get("mode") == "bootstrap_single_writer"
        and int(election.get("roster_size") or 0) == 0
    )


def publication_integrity_gate(
    env: Mapping[str, str],
    index_path: Path,
    source_url: str,
    reference_url: str,
    start: int,
    end: int,
    election: Mapping[str, Any],
) -> dict[str, Any]:
    if reference_url:
        return require_trusted_preflight(env, index_path, source_url, reference_url, start, end)
    candidate_urls = chain_reference_rpc_candidates(env, source_url)
    if candidate_urls:
        return require_trusted_preflight_from_candidates(env, index_path, source_url, candidate_urls, start, end)
    if bootstrap_local_publish_allowed(env, election):
        return {
            "state": "bootstrap_local_reference_absent",
            "trusted": False,
            "reasons": [
                "no independent native reference RPC configured",
                "bootstrap seed publication is immutable transport only; receivers must verify segment continuity and chain consensus before restore",
            ],
            "source_url": source_url,
            "reference_url": "",
            "range": {"start_order": start, "end_order": end},
            "mutation_policy": "allowed_for_bootstrap_seed_without_roster_only",
        }
    return require_trusted_preflight(env, index_path, source_url, reference_url, start, end)


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
    election: Mapping[str, Any] | None = None,
    publication_integrity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if ipfs_segment_trust.signature_required(env) and not ipfs_segment_trust.signing_key_hex(env):
        raise RuntimeError(
            "IPFS segment signing is required but no BDAG_IPFS_SEGMENT_SIGNING_KEY_HEX/FILE is configured"
        )
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

    election_payload = dict(election or writer_election(env, start, end, previous_manifest_cid))
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
            "mode": election_payload.get("mode") or "bootstrap_single_writer",
            "kubo_peer_id": ipfs_peer_id(env),
            "writer_id": election_payload.get("local_writer_id") or "",
            "ipns_name": env.get("BDAG_IPFS_CONTENT_LATEST_IPNS", ""),
        },
        "election": election_payload,
        "publication_integrity": dict(publication_integrity or {}),
        "trust_model": "CID and sha256 verify bytes; receivers must still verify chain consensus and segment continuity.",
    }
    manifest = ipfs_segment_trust.sign_payload(manifest, env, signature_field="manifest_signatures")
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
        "writer_mode": election_payload.get("mode") or "bootstrap_single_writer",
        "writer_election": election_payload,
        "publication_integrity": dict(publication_integrity or {}),
        "manifest_signature_status": manifest.get("signature_status"),
        "manifest_signatures": manifest.get("manifest_signatures") or [],
    }


def update_index(index: dict[str, Any], segment_record: dict[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    existing_segments = segments(index)
    if existing_segments:
        last = existing_segments[-1]
        expected_start = int(last.get("end_order") or 0) + 1
        if int(segment_record.get("start_order") or 0) != expected_start:
            raise RuntimeError(
                "refusing non-contiguous IPFS segment append: "
                f"start_order {segment_record.get('start_order')} != expected {expected_start}"
            )
        expected_segment_id = int(last.get("segment_id") or 0) + 1
        if int(segment_record.get("segment_id") or 0) != expected_segment_id:
            raise RuntimeError(
                "refusing non-monotonic IPFS segment append: "
                f"segment_id {segment_record.get('segment_id')} != expected {expected_segment_id}"
            )
    else:
        head = current_head(index)
        if head and isinstance(head.get("end_order"), int):
            expected_start = int(head["end_order"]) + 1
            if int(segment_record.get("start_order") or 0) != expected_start:
                raise RuntimeError(
                    "refusing non-contiguous IPFS segment append after current head: "
                    f"start_order {segment_record.get('start_order')} != expected {expected_start}"
                )
            if isinstance(head.get("segment_id"), int):
                expected_segment_id = int(head["segment_id"]) + 1
                if int(segment_record.get("segment_id") or 0) != expected_segment_id:
                    raise RuntimeError(
                        "refusing non-monotonic IPFS segment append after current head: "
                        f"segment_id {segment_record.get('segment_id')} != expected {expected_segment_id}"
                    )
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
    existing_history = index.get("history_completeness") if isinstance(index.get("history_completeness"), Mapping) else {}
    existing_complete_from = existing_history.get("complete_from_order") if isinstance(existing_history, Mapping) else None
    first_start = (
        existing_complete_from
        if isinstance(existing_complete_from, int) and existing_complete_from > 0
        else segment_record["start_order"] if not existing_segments else existing_segments[0].get("start_order")
    )
    index.update(
        {
            "generated_at": now,
            "status": "active_deterministic_writer_segments",
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
                "note": "Phase-1 writer starts from configured/current tail. Earlier history must be backfilled through verified IPFS segments or supplied by a signed checkpoint before full bootstrap use.",
            },
            "notes": [
                "This index contains live-tail chain-order segments written by the deterministic elected writer, or by the bootstrap seed when no roster is configured.",
                "Earlier history before history_completeness.complete_from_order is not complete yet and must be backfilled through verified IPFS segments or supplied by a signed checkpoint before full bootstrap use.",
                "New nodes should resolve the stable IPNS pointer, fetch this index, verify every segment CID/sha256/order link, and then rely on normal chain consensus.",
            ],
            "publisher_policy": {
                "phase": "deterministic_roster_or_bootstrap_seed",
                "rule": env.get("BDAG_IPFS_SEGMENT_WRITER_ELECTION_RULE", "rendezvous_sha256_v1"),
                "configured_roster_size": len(parse_writer_roster(env.get("BDAG_IPFS_SEGMENT_WRITER_ROSTER"))),
                "writer_roster": parse_writer_roster(env.get("BDAG_IPFS_SEGMENT_WRITER_ROSTER")),
                "trusted_writer_ids": sorted(ipfs_segment_trust.trusted_signers(env)),
                "cadence": "timer attempts every five minutes; chain finality and maintenance gates decide whether a segment is publishable",
                "conflict_policy": "only the elected roster writer should publish each immutable finalized range; identical honest content still verifies to the same canonical bytes",
                "signature_requirement": "ed25519 signatures required unless explicitly disabled for non-production drills",
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
    previous_index_cid = str(index.get("previous_index_cid") or "").strip()
    if previous_index_cid:
        data["previous_latest_index_cid"] = previous_index_cid
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
        "previous_index_cid": previous_index_cid,
        "append_only_index_policy": index.get("append_only_index_policy"),
    }
    atomic_write_json(path, data)


def custom_index_discovery_enabled(env: Mapping[str, str]) -> bool:
    return env_bool(env, "BDAG_IPFS_SEGMENT_UPDATE_DISCOVERY_FOR_CUSTOM_INDEX", False)


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
        index = load_index_with_discovery(index_path, env, use_discovery=not bool(args.index))
        previous_index_cid = "" if args.index else published_index_cid(index, env)
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
        if explicit_range and not args.preflight and safe_tip is not None and end > safe_tip:
            payload = write_status(
                env,
                "waiting_for_finalized_range",
                latest_order=latest_order,
                safe_tip=safe_tip,
                next_start_order=start,
                next_end_order=end,
                reason="explicit_range_exceeds_safe_tip",
                rpc_source=source_name,
                tip_method=tip_method,
            )
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        if args.preflight:
            reference_url = str(args.reference_rpc_url or chain_reference_rpc_url(env, source_url)).strip()
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
            election = writer_election(env, start, end, str((head or {}).get("manifest_cid") or ""))
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
                writer_election=election,
                stale_head_live_tail_reset=range_reason == "stale_head_live_tail_reset",
            )
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            return 0

        reference_url = explicit_chain_reference_rpc_url(env)
        publish_preflight: dict[str, Any] = {}
        publish_preflights: list[dict[str, Any]] = []
        max_segments = max(1, env_int(env, "BDAG_IPFS_SEGMENT_MAX_SEGMENTS_PER_RUN", 1))
        written: list[dict[str, Any]] = []
        live_tail_epoch_reset = range_reason == "stale_head_live_tail_reset"
        current_index = (
            reset_index_for_live_tail_epoch(index, start, end, latest_order, safe_tip, env)
            if live_tail_epoch_reset
            else dict(index)
        )
        current_start, current_end = start, end
        current_election: dict[str, Any] = {}
        for _ in range(max_segments):
            if current_end <= 0:
                break
            current_head_record = current_head(current_index)
            previous_manifest_cid = str(current_head_record.get("manifest_cid") or "") if current_head_record else ""
            current_election = writer_election(env, current_start, current_end, previous_manifest_cid)
            if not current_election.get("allowed", False):
                if not written:
                    payload = write_status(
                        env,
                        "deferred",
                        reasons=[str(current_election.get("reason") or "writer_election_deferred")],
                        writer_election=current_election,
                        latest_order=latest_order,
                        safe_tip=safe_tip,
                        next_start_order=current_start,
                        next_end_order=current_end,
                        rpc_source=source_name,
                        tip_method=tip_method,
                    )
                    if args.json:
                        print(json.dumps(payload, indent=2, sort_keys=True))
                    return 0
                break
            publish_preflight = publication_integrity_gate(
                env,
                index_path,
                source_url,
                reference_url,
                current_start,
                current_end,
                current_election,
            )
            publish_preflights.append(publish_preflight)
            record = build_segment(
                pool_ops,
                source_name,
                source_url,
                current_start,
                current_end,
                current_index,
                env,
                current_election,
                publish_preflight,
            )
            current_index = update_index(current_index, record, env)
            atomic_write_json(index_path, current_index)
            written.append(record)
            current_start, current_end, safe_tip, range_reason = choose_next_range(current_index, latest_order, env)
        if written:
            current_index = attach_previous_index_link(
                current_index,
                previous_index_cid,
                index,
                "stale_head_live_tail_reset" if live_tail_epoch_reset else "segment_append",
            )
            current_index = ipfs_segment_trust.sign_payload(current_index, env, signature_field="index_signatures")
            atomic_write_json(index_path, current_index)
        index_cid, index_sha, index_size = add_checked_json(index_path, current_index, env)
        if args.index and not custom_index_discovery_enabled(env):
            ipns = {
                "published": False,
                "reason": "custom_index_discovery_disabled",
                "policy": "custom --index paths are candidate/backfill workspaces unless BDAG_IPFS_SEGMENT_UPDATE_DISCOVERY_FOR_CUSTOM_INDEX=1",
            }
        else:
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
            current_head=current_index.get("current_head"),
            previous_index_cid=current_index.get("previous_index_cid"),
            previous_index_link=current_index.get("previous_index_link"),
            append_only_index_policy=current_index.get("append_only_index_policy"),
            ipns=ipns,
            latest_order=latest_order,
            safe_tip=safe_tip,
            next_start_order=current_start,
            next_end_order=current_end,
            rpc_source=source_name,
            tip_method=tip_method,
            writer_election=current_election,
            chain_integrity=publish_preflight,
            chain_integrity_preflights=publish_preflights,
            stale_head_live_tail_reset=live_tail_epoch_reset,
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

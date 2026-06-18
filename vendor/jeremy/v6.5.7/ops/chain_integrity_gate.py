#!/usr/bin/env python3
"""Fail-closed chain integrity gate for IPFS/rawdatadir publication paths.

IPFS and rawdatadir artifacts are byte transport only. This gate checks a
bounded chain-order range against a direct source backend and an independent
reference before any caller is allowed to mutate Kubo, IPNS, or public artifact
state.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


ROOT = Path(os.environ.get("BDAG_PROJECT_ROOT") or Path(__file__).resolve().parents[1]).resolve()
ENV_FILE = Path(os.environ.get("BDAG_ENV_FILE") or ROOT / ".env")

FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}

TRUSTED = "trusted"
DEFERRED_PRESSURE = "deferred_pressure"
DEFERRED_INCIDENT = "deferred_incident"
DEFERRED_REFERENCE_UNAVAILABLE = "deferred_reference_unavailable"
REJECTED_MISMATCH = "rejected_mismatch"
REJECTED_SOURCE_UNREADY = "rejected_source_unready"
REJECTED_INDEX_GAP = "rejected_index_gap"

CHAIN_INCIDENT_TERMS = (
    "zero state root",
    "zero-state-root",
    "bad block",
    "unknown ancestor",
    "head state missing",
    "stateless",
    "state sync",
    "chain_incident",
    "node_zero_state_root_warnings",
)

DEFAULT_LOCK_RELATIVE_PATHS = (
    "ops/runtime/rawdatadir-sidecar.lock",
    "ops/runtime/rawdatadir-artifact.lock",
    "ops/runtime/rawdatadir-publish.lock",
    "ops/runtime/repair.lock",
    "ops/runtime/hourly-chain-snapshot.lock",
)

RpcCall = Callable[[str, str, list[Any], float, Mapping[str, str]], Any]
DashboardFetcher = Callable[[str, float], tuple[dict[str, Any], float]]


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    env.update({key: value for key, value in os.environ.items() if key.startswith("BDAG_") or key in {"NODE_RPC_USER", "NODE_RPC_PASS"}})
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


def env_float(env: Mapping[str, str], key: str, default: float) -> float:
    value = str(env.get(key, "")).strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def resolve_path(value: str | None, default: Path) -> Path:
    if not value:
        return default.resolve()
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        return {}


def canonical_json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = canonical_json_bytes(payload)
    with tempfile.NamedTemporaryFile("wb", dir=str(path.parent), delete=False) as handle:
        handle.write(raw)
        tmp = Path(handle.name)
    tmp.replace(path)


def parse_rpc_quantity(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("0x", "0X")):
            return int(text, 16)
        return int(text)
    raise ValueError(f"invalid RPC quantity: {value!r}")


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return parse_rpc_quantity(value)
    except Exception:
        return default


def redacted_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _request_url_and_auth(url: str, env: Mapping[str, str]) -> tuple[str, str | None]:
    parsed = urllib.parse.urlsplit(url)
    user = urllib.parse.unquote(parsed.username or "")
    password = urllib.parse.unquote(parsed.password or "")
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    clean_url = urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))
    if not user and not password:
        user = str(env.get("NODE_RPC_USER") or env.get("BDAG_NODE_RPC_USER") or "").strip()
        password = str(env.get("NODE_RPC_PASS") or env.get("BDAG_NODE_RPC_PASS") or "").strip()
    if not user and not password:
        return clean_url, None
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return clean_url, f"Basic {token}"


def rpc_call(url: str, method: str, params: list[Any], timeout: float, env: Mapping[str, str]) -> Any:
    request_url, authorization = _request_url_and_auth(url, env)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "accept": "application/json",
        "user-agent": "BlockDAGChainIntegrityGate/1.0",
    }
    if authorization:
        headers["authorization"] = authorization
    request = urllib.request.Request(request_url, data=body, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read(1_000_000).decode("utf-8", "replace"))
    if payload.get("error") is not None and "result" not in payload:
        raise RuntimeError(str(payload.get("error") or payload))
    return payload.get("result")


def fetch_dashboard_status(url: str, timeout: float) -> tuple[dict[str, Any], float]:
    started = time.monotonic()
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.loads(response.read(2_000_000).decode("utf-8", "replace"))
    elapsed = time.monotonic() - started
    return payload if isinstance(payload, dict) else {}, elapsed


def first_present(payload: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in payload and payload.get(name) not in (None, ""):
            return payload.get(name)
    return None


def normalize_value(value: Any, sort_lists: bool = False) -> Any:
    if isinstance(value, str):
        text = value.strip()
        return text.lower() if text.startswith(("0x", "0X")) else text
    if isinstance(value, list):
        normalized = [normalize_value(item) for item in value]
        if sort_lists:
            return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True))
        return normalized
    if isinstance(value, dict):
        return {str(key): normalize_value(val) for key, val in sorted(value.items())}
    return value


def block_hash(block: Mapping[str, Any], order: int) -> str:
    value = first_present(block, ("hash", "Hash", "blockHash", "BlockHash", "block_hash", "blockhash"))
    if value in (None, ""):
        raise RuntimeError(f"missing block hash for order {order}")
    return str(normalize_value(value))


HEADER_FIELDS: dict[str, tuple[str, ...]] = {
    "state_root": ("stateRoot", "StateRoot", "state_root", "stateroot", "evm_state_root"),
    "tx_root": ("txRoot", "TxRoot", "tx_root", "merkleroot", "merkleRoot"),
    "parent_root": ("parentroot", "parentRoot", "ParentRoot", "parent_root"),
    "parents": ("parents", "Parents"),
    "height": ("height", "Height", "mainHeight", "main_height"),
    "order": ("order", "Order", "mainOrder", "MainOrder", "main_order"),
    "bits": ("bits", "Bits"),
    "difficulty": ("difficulty", "Difficulty"),
    "pow": ("pow", "Pow", "PoW"),
    "version": ("version", "Version"),
}


def comparable_header(block: Mapping[str, Any]) -> dict[str, Any]:
    header: dict[str, Any] = {}
    for canonical, aliases in HEADER_FIELDS.items():
        value = first_present(block, aliases)
        if value not in (None, ""):
            header[canonical] = normalize_value(value, sort_lists=(canonical == "parents"))
    return header


def fetch_order_record(
    url: str,
    order: int,
    timeout: float,
    env: Mapping[str, str],
    rpc: RpcCall = rpc_call,
) -> dict[str, Any]:
    result = rpc(url, "getBlockByOrder", [order, True, False], timeout, env)
    if not isinstance(result, dict):
        raise RuntimeError(f"getBlockByOrder response for order {order} was not an object")
    response_order = first_present(result, ("order", "Order", "mainOrder", "MainOrder", "main_order"))
    resolved_order = safe_int(response_order, order)
    if order >= 0 and response_order is not None and resolved_order != order:
        raise RuntimeError(f"getBlockByOrder returned order {response_order!r} for requested order {order}")
    if resolved_order < 0:
        raise RuntimeError(f"getBlockByOrder returned invalid order {response_order!r}")
    return {
        "order": resolved_order,
        "hash": block_hash(result, resolved_order),
        "header": comparable_header(result),
    }


def fetch_tip(url: str, timeout: float, env: Mapping[str, str], rpc: RpcCall = rpc_call) -> tuple[int, str]:
    errors: list[str] = []
    try:
        record = fetch_order_record(url, -1, timeout, env, rpc)
        latest = safe_int(record.get("order"), -1)
        if latest >= 0:
            return latest, "getBlockByOrder(-1)"
        errors.append(f"getBlockByOrder(-1) returned invalid order {record.get('order')!r}")
    except Exception as exc:  # noqa: BLE001 - fall back to older methods.
        errors.append(f"getBlockByOrder(-1): {exc}")
    for method in ("getBlockTotal", "getBlockCount"):
        try:
            latest = parse_rpc_quantity(rpc(url, method, [], timeout, env))
            if latest >= 0:
                return latest, method
            errors.append(f"{method} returned negative tip {latest}")
        except Exception as exc:  # noqa: BLE001 - collect all source details.
            errors.append(f"{method}: {exc}")
    raise RuntimeError("; ".join(errors) or "unable to resolve chain tip")


def fetch_genesis_hash(url: str, timeout: float, env: Mapping[str, str], rpc: RpcCall = rpc_call) -> str:
    for method in ("getBlockhash", "getBlockHash"):
        try:
            value = rpc(url, method, [0], timeout, env)
            if value not in (None, ""):
                return str(normalize_value(value))
        except Exception:
            pass
    return fetch_order_record(url, 0, timeout, env, rpc)["hash"]


def fetch_network_identity(url: str, timeout: float, env: Mapping[str, str], rpc: RpcCall = rpc_call) -> str:
    for method in ("getBlockDagInfo", "getBlockDAGInfo", "getNetworkInfo"):
        try:
            result = rpc(url, method, [], timeout, env)
        except Exception:
            continue
        if isinstance(result, dict):
            value = first_present(result, ("network", "Network", "chain", "chainName", "chain_id", "chainId"))
            if value not in (None, ""):
                return str(normalize_value(value))
        elif result not in (None, ""):
            return str(normalize_value(result))
    return ""


def compare_records(order: int, source: Mapping[str, Any], reference: Mapping[str, Any]) -> list[str]:
    mismatches: list[str] = []
    if str(source.get("hash") or "") != str(reference.get("hash") or ""):
        mismatches.append(f"order_{order}_hash:{source.get('hash')}!={reference.get('hash')}")
    source_header = source.get("header") if isinstance(source.get("header"), dict) else {}
    reference_header = reference.get("header") if isinstance(reference.get("header"), dict) else {}
    for key in sorted(set(source_header) & set(reference_header)):
        if source_header.get(key) != reference_header.get(key):
            mismatches.append(f"order_{order}_{key}:{source_header.get(key)!r}!={reference_header.get(key)!r}")
    return mismatches


def index_segments(index: Mapping[str, Any]) -> list[dict[str, Any]]:
    values = index.get("segments")
    if not isinstance(values, list):
        return []
    return [item for item in values if isinstance(item, dict)]


def index_head_order(index: Mapping[str, Any]) -> int:
    head = index.get("current_head")
    if isinstance(head, dict):
        value = safe_int(head.get("end_order"), 0)
        if value > 0:
            return value
    values = index_segments(index)
    if not values:
        return 0
    return max(safe_int(item.get("end_order"), 0) for item in values)


def validate_index_shape(index: Mapping[str, Any]) -> list[str]:
    if not index:
        return []
    values = index_segments(index)
    head = index.get("current_head")
    if head and not values:
        return ["current_head_without_segments"]
    previous_end = 0
    for idx, segment in enumerate(values):
        start = safe_int(segment.get("start_order"), -1)
        end = safe_int(segment.get("end_order"), -1)
        if start <= 0 or end < start:
            return [f"segment_{idx}_invalid_range:{start}-{end}"]
        if previous_end and start != previous_end + 1:
            return [f"segment_{idx}_gap:{previous_end}->{start}"]
        if not segment.get("start_hash") or not segment.get("end_hash"):
            return [f"segment_{idx}_missing_endpoint_hash"]
        previous_end = end
    if isinstance(head, dict) and values:
        last = values[-1]
        if safe_int(head.get("end_order"), -1) != safe_int(last.get("end_order"), -2):
            return ["current_head_end_order_mismatch"]
        head_hash = str(normalize_value(head.get("end_hash") or ""))
        last_hash = str(normalize_value(last.get("end_hash") or ""))
        if head_hash and last_hash and head_hash != last_hash:
            return ["current_head_end_hash_mismatch"]
    return []


def validate_index_against_chain(
    index: Mapping[str, Any],
    source_url: str,
    reference_url: str,
    timeout: float,
    env: Mapping[str, str],
    rpc: RpcCall,
) -> tuple[str, list[str], dict[str, Any]]:
    values = index_segments(index)
    if not values:
        return TRUSTED, [], {"validated_segments": 0}
    max_segments = max(1, env_int(env, "BDAG_CHAIN_INTEGRITY_MAX_INDEX_SEGMENTS_VALIDATE", 64))
    if len(values) > max_segments and not env_bool(env, "BDAG_CHAIN_INTEGRITY_ALLOW_LARGE_INDEX_VALIDATION", False):
        return REJECTED_INDEX_GAP, [f"index_segment_count_{len(values)}_exceeds_validation_cap_{max_segments}"], {
            "validated_segments": 0,
            "segment_count": len(values),
        }
    checked_orders: dict[int, dict[str, Any]] = {}
    for idx, segment in enumerate(values):
        for key in ("start", "end"):
            order = safe_int(segment.get(f"{key}_order"), -1)
            expected = str(normalize_value(segment.get(f"{key}_hash") or ""))
            if order <= 0 or not expected:
                return REJECTED_INDEX_GAP, [f"segment_{idx}_{key}_endpoint_unverifiable"], {"segment": idx}
            if order not in checked_orders:
                try:
                    source_record = fetch_order_record(source_url, order, timeout, env, rpc)
                except Exception as exc:  # noqa: BLE001
                    return REJECTED_SOURCE_UNREADY, [f"source_index_endpoint_{order}: {exc}"], {"segment": idx, "order": order}
                try:
                    reference_record = fetch_order_record(reference_url, order, timeout, env, rpc)
                except Exception as exc:  # noqa: BLE001
                    return DEFERRED_REFERENCE_UNAVAILABLE, [f"reference_index_endpoint_{order}: {exc}"], {"segment": idx, "order": order}
                mismatches = compare_records(order, source_record, reference_record)
                if mismatches:
                    return REJECTED_MISMATCH, mismatches, {"segment": idx, "order": order}
                checked_orders[order] = source_record
            actual = str(checked_orders[order].get("hash") or "")
            if actual != expected:
                return REJECTED_INDEX_GAP, [f"segment_{idx}_{key}_hash_mismatch:{expected}!={actual}"], {
                    "segment": idx,
                    "order": order,
                }
    return TRUSTED, [], {"validated_segments": len(values), "validated_endpoint_orders": sorted(checked_orders)}


def is_forbidden_source_url(url: str, env: Mapping[str, str]) -> str:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    port = parsed.port
    denied_hosts = {
        item.strip().lower()
        for item in str(env.get("BDAG_CHAIN_INTEGRITY_DENY_SOURCE_HOSTS") or "").split(",")
        if item.strip()
    }
    if host in denied_hosts:
        return f"source_url_uses_forbidden_router_host:{host}"
    return ""


def artifact_publish_blockers(
    artifact_dir: Path | None,
    manifest_path: Path | None,
    env: Mapping[str, str],
    require_signed: bool,
) -> list[str]:
    blockers: list[str] = []
    if artifact_dir is None and manifest_path is None:
        return blockers
    if artifact_dir is None:
        blockers.append("artifact_dir_missing")
    elif not artifact_dir.exists():
        blockers.append("artifact_dir_missing")
    if manifest_path is None:
        blockers.append("manifest_missing")
        manifest: dict[str, Any] = {}
    elif not manifest_path.exists():
        blockers.append("manifest_missing")
        manifest = {}
    else:
        manifest = load_json(manifest_path)
    for marker_dir in [item for item in (artifact_dir, artifact_dir.parent if artifact_dir else None) if item is not None]:
        if (marker_dir / "DO_NOT_PUBLISH.txt").exists() or (marker_dir / "DO_NOT_PUBLISH").exists():
            blockers.append(f"do_not_publish_marker:{marker_dir}")
            break
    if require_signed and not manifest.get("signatures") and not env_bool(env, "BDAG_CHAIN_INTEGRITY_ALLOW_UNSIGNED_ARTIFACT", False):
        blockers.append("manifest_unsigned")
    artifact_type = str(manifest.get("artifact_type") or manifest.get("type") or "")
    if artifact_type and artifact_type != "raw_datadir_checkpoint":
        blockers.append(f"unsupported_artifact_type:{artifact_type}")
    return blockers


def parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def automation_control_gate(env: Mapping[str, str]) -> tuple[str, list[str], dict[str, Any]]:
    path = resolve_path(env.get("BDAG_AUTOMATION_CONTROL_FILE"), ROOT / "ops/runtime/automation-control.json")
    if not path.exists():
        return DEFERRED_INCIDENT, ["automation_control_missing"], {"path": str(path)}
    payload = load_json(path)
    if not payload:
        return DEFERRED_INCIDENT, ["automation_control_unreadable"], {"path": str(path)}
    state = str(payload.get("state") or "").strip()
    if state != "normal":
        expires_at = parse_time(payload.get("expires_at"))
        expired = bool(expires_at and expires_at < datetime.now(timezone.utc))
        return DEFERRED_INCIDENT, [f"automation_control_state_{state or 'missing'}"], {
            "path": str(path),
            "state": state,
            "expired": expired,
            "owner": payload.get("owner"),
            "reason": payload.get("reason"),
        }
    return TRUSTED, [], {"path": str(path), "state": state}


def lock_is_held(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        with path.open("r") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            finally:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
    except OSError:
        return True
    return False


def active_job_gate(env: Mapping[str, str]) -> tuple[str, list[str], dict[str, Any]]:
    configured = str(env.get("BDAG_CHAIN_INTEGRITY_ACTIVE_LOCKS") or "").strip()
    if configured:
        paths = [resolve_path(item.strip(), ROOT / item.strip()) for item in configured.split(",") if item.strip()]
    else:
        paths = [ROOT / item for item in DEFAULT_LOCK_RELATIVE_PATHS]
    active = [str(path) for path in paths if lock_is_held(path)]
    if active:
        return DEFERRED_PRESSURE, ["active_restore_sidecar_or_artifact_job"], {"active_locks": active}
    return TRUSTED, [], {"checked_locks": [str(path) for path in paths]}


def recent_chain_incident_gate(env: Mapping[str, str]) -> tuple[str, list[str], dict[str, Any]]:
    path = resolve_path(env.get("BDAG_CHAIN_INTEGRITY_INCIDENTS_FILE"), ROOT / "ops/runtime/logs/incidents.jsonl")
    if not path.exists():
        return TRUSTED, [], {"path": str(path), "checked": False}
    ttl_seconds = max(0, env_int(env, "BDAG_CHAIN_INTEGRITY_CHAIN_INCIDENT_TTL_SECONDS", 24 * 3600))
    now = datetime.now(timezone.utc)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
    except OSError as exc:
        return DEFERRED_INCIDENT, [f"incident_log_unreadable:{exc}"], {"path": str(path)}
    for line in reversed(lines):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = json.dumps(item, sort_keys=True).lower()
        if not any(term in text for term in CHAIN_INCIDENT_TERMS):
            continue
        stamp = parse_time(item.get("generated_at") or item.get("time") or item.get("created_at"))
        if stamp is None or ttl_seconds <= 0 or (now - stamp).total_seconds() <= ttl_seconds:
            return DEFERRED_INCIDENT, ["recent_chain_corruption_incident"], {
                "path": str(path),
                "event_type": item.get("event_type"),
                "component": item.get("component"),
                "generated_at": item.get("generated_at"),
                "id": item.get("id"),
            }
    return TRUSTED, [], {"path": str(path), "checked": True}


def pressure_samples(status: Mapping[str, Any]) -> list[dict[str, Any]]:
    host_pressure = status.get("host_pressure") if isinstance(status.get("host_pressure"), dict) else {}
    samples = host_pressure.get("samples") if isinstance(host_pressure.get("samples"), list) else []
    parsed = [item for item in samples if isinstance(item, dict)]
    if parsed:
        return parsed
    return [host_pressure] if host_pressure else []


def first_float(payload: Mapping[str, Any], names: tuple[str, ...]) -> float | None:
    for name in names:
        if name in payload and payload.get(name) is not None:
            try:
                return float(payload.get(name))
            except (TypeError, ValueError):
                return None
    return None


def dashboard_pressure_gate(
    env: Mapping[str, str],
    dashboard_fetcher: DashboardFetcher = fetch_dashboard_status,
) -> tuple[str, list[str], dict[str, Any]]:
    url = str(env.get("BDAG_DASHBOARD_STATUS_URL") or "http://127.0.0.1:8088/api/status").strip()
    timeout = env_float(env, "BDAG_CHAIN_INTEGRITY_DASHBOARD_TIMEOUT_SECONDS", 2.0)
    try:
        status, elapsed = dashboard_fetcher(url, timeout)
    except Exception as exc:  # noqa: BLE001 - dashboard health is part of the gate.
        return DEFERRED_PRESSURE, [f"dashboard_status_unavailable:{exc}"], {"url": redacted_url(url)}
    if elapsed > timeout:
        return DEFERRED_PRESSURE, [f"dashboard_status_slow:{elapsed:.3f}s"], {"url": redacted_url(url), "elapsed_seconds": elapsed}
    required = max(1, env_int(env, "BDAG_CHAIN_INTEGRITY_PRESSURE_SAMPLE_COUNT", 3))
    samples = pressure_samples(status)
    if len(samples) < required:
        return DEFERRED_PRESSURE, [f"insufficient_pressure_samples:{len(samples)}/{required}"], {
            "url": redacted_url(url),
            "elapsed_seconds": elapsed,
            "sample_count": len(samples),
            "required_samples": required,
        }
    io_some_limit = env_float(env, "BDAG_CHAIN_INTEGRITY_IO_SOME_AVG10_MAX", 5.0)
    io_full_limit = env_float(env, "BDAG_CHAIN_INTEGRITY_IO_FULL_AVG10_MAX", 2.0)
    cpu_some_limit = env_float(env, "BDAG_CHAIN_INTEGRITY_CPU_SOME_AVG10_MAX", 20.0)
    rpc_p95_limit = env_float(env, "BDAG_CHAIN_INTEGRITY_CHAIN_RPC_P95_MS_MAX", 500.0)
    violations: list[str] = []
    for sample in samples[-required:]:
        io_some = first_float(sample, ("io_some_avg10", "some_avg10"))
        io_full = first_float(sample, ("io_full_avg10", "full_avg10"))
        cpu_some = first_float(sample, ("cpu_some_avg10",))
        rpc_p95 = first_float(sample, ("chain_rpc_p95_ms", "chain_rpc_latency_p95_ms", "chain_rpc_latency_ms"))
        if io_some is not None and io_some >= io_some_limit:
            violations.append(f"io_some_avg10_{io_some:.2f}_ge_{io_some_limit:.2f}")
        if io_full is not None and io_full >= io_full_limit:
            violations.append(f"io_full_avg10_{io_full:.2f}_ge_{io_full_limit:.2f}")
        if cpu_some is not None and cpu_some >= cpu_some_limit:
            violations.append(f"cpu_some_avg10_{cpu_some:.2f}_ge_{cpu_some_limit:.2f}")
        if rpc_p95 is not None and rpc_p95 >= rpc_p95_limit:
            violations.append(f"chain_rpc_p95_ms_{rpc_p95:.1f}_ge_{rpc_p95_limit:.1f}")
    if violations:
        return DEFERRED_PRESSURE, violations, {"url": redacted_url(url), "elapsed_seconds": elapsed, "sample_count": len(samples)}
    return TRUSTED, [], {"url": redacted_url(url), "elapsed_seconds": elapsed, "sample_count": len(samples)}


def environment_gates(
    env: Mapping[str, str],
    dashboard_fetcher: DashboardFetcher = fetch_dashboard_status,
) -> tuple[str, list[str], dict[str, Any]]:
    if env_bool(env, "BDAG_CHAIN_INTEGRITY_SKIP_ENVIRONMENT_GATES", False):
        return TRUSTED, [], {"skipped": True}
    details: dict[str, Any] = {}
    for name, gate in (
        ("automation_control", lambda: automation_control_gate(env)),
        ("chain_incident", lambda: recent_chain_incident_gate(env)),
        ("active_jobs", lambda: active_job_gate(env)),
        ("dashboard_pressure", lambda: dashboard_pressure_gate(env, dashboard_fetcher)),
    ):
        state, reasons, gate_details = gate()
        details[name] = gate_details
        if state != TRUSTED:
            return state, reasons, details
    return TRUSTED, [], details


def terminal_payload(
    workflow: str,
    state: str,
    reasons: list[str],
    checks: list[dict[str, Any]],
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "document_type": "bdag_chain_integrity_gate_v1",
        "generated_at": now_iso(),
        "workflow": workflow,
        "state": state,
        "trusted": state == TRUSTED,
        "reasons": reasons,
        "checks": checks,
        "trust_model": "IPFS/rawdatadir bytes are untrusted until direct source and independent reference chain evidence match.",
    }
    payload.update(extra)
    return payload


def evaluate_chain_integrity(
    config: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
    rpc: RpcCall = rpc_call,
    dashboard_fetcher: DashboardFetcher = fetch_dashboard_status,
) -> dict[str, Any]:
    env = dict(env or load_env())
    workflow = str(config.get("workflow") or "ipfs_segment_writer")
    timeout = env_float(env, "BDAG_CHAIN_INTEGRITY_RPC_TIMEOUT_SECONDS", env_float(env, "BDAG_IPFS_SEGMENT_RPC_TIMEOUT", 8.0))
    checks: list[dict[str, Any]] = []

    source_url = str(config.get("source_rpc_url") or env.get("BDAG_CHAIN_SOURCE_RPC_URL") or "").strip()
    reference_url = str(config.get("reference_rpc_url") or env.get("BDAG_CHAIN_REFERENCE_RPC_URL") or "").strip()
    start_order = safe_int(config.get("start_order"), 0)
    end_order = safe_int(config.get("end_order"), 0)
    max_orders = max(1, env_int(env, "BDAG_CHAIN_INTEGRITY_MAX_SEGMENT_ORDERS", 128))
    index_path = resolve_path(str(config.get("index") or env.get("BDAG_IPFS_SEGMENT_INDEX_PATH") or env.get("BDAG_IPFS_CONTENT_LATEST_INDEX_PATH") or ""), ROOT / "ops/runtime/ipfs-content/latest-index.json")
    index = load_json(index_path)

    artifact_dir = config.get("artifact_dir")
    artifact_manifest = config.get("artifact_manifest")
    require_signed = bool(config.get("require_signed_manifest", False))
    if artifact_dir or artifact_manifest:
        blockers = artifact_publish_blockers(
            resolve_path(str(artifact_dir), ROOT / str(artifact_dir)) if artifact_dir else None,
            resolve_path(str(artifact_manifest), ROOT / str(artifact_manifest)) if artifact_manifest else None,
            env,
            require_signed,
        )
        checks.append({"name": "artifact_publish_blockers", "state": TRUSTED if not blockers else REJECTED_SOURCE_UNREADY, "blockers": blockers})
        if blockers:
            return terminal_payload(workflow, REJECTED_SOURCE_UNREADY, blockers, checks)

    if start_order and end_order and end_order < start_order:
        reasons = [f"invalid_range:{start_order}-{end_order}"]
        checks.append({"name": "segment_range", "state": REJECTED_SOURCE_UNREADY, "max_orders": max_orders})
        return terminal_payload(workflow, REJECTED_SOURCE_UNREADY, reasons, checks)
    if start_order and end_order and (end_order - start_order + 1) > max_orders:
        reasons = [f"segment_order_count_{end_order - start_order + 1}_exceeds_cap_{max_orders}"]
        checks.append({"name": "segment_range", "state": REJECTED_SOURCE_UNREADY, "max_orders": max_orders})
        return terminal_payload(workflow, REJECTED_SOURCE_UNREADY, reasons, checks)
    checks.append({"name": "segment_range", "state": TRUSTED, "start_order": start_order, "end_order": end_order, "max_orders": max_orders})

    index_reasons = validate_index_shape(index)
    checks.append({"name": "index_shape", "state": TRUSTED if not index_reasons else REJECTED_INDEX_GAP, "path": str(index_path)})
    if index_reasons:
        return terminal_payload(workflow, REJECTED_INDEX_GAP, index_reasons, checks, index_path=str(index_path))

    env_state, env_reasons, env_details = environment_gates(env, dashboard_fetcher)
    checks.append({"name": "environment", "state": env_state, "details": env_details})
    if env_state != TRUSTED:
        return terminal_payload(workflow, env_state, env_reasons, checks, index_path=str(index_path))

    if not source_url:
        checks.append({"name": "source_rpc", "state": REJECTED_SOURCE_UNREADY})
        return terminal_payload(workflow, REJECTED_SOURCE_UNREADY, ["source_rpc_url_missing"], checks, index_path=str(index_path))
    forbidden = is_forbidden_source_url(source_url, env)
    checks.append({"name": "direct_source_rpc", "state": TRUSTED if not forbidden else REJECTED_SOURCE_UNREADY, "source_url": redacted_url(source_url)})
    if forbidden:
        return terminal_payload(workflow, REJECTED_SOURCE_UNREADY, [forbidden], checks, index_path=str(index_path), source_url=redacted_url(source_url))
    if not reference_url:
        checks.append({"name": "reference_rpc", "state": DEFERRED_REFERENCE_UNAVAILABLE})
        return terminal_payload(workflow, DEFERRED_REFERENCE_UNAVAILABLE, ["reference_rpc_url_missing"], checks, index_path=str(index_path), source_url=redacted_url(source_url))
    if redacted_url(reference_url) == redacted_url(source_url):
        checks.append({"name": "reference_rpc", "state": DEFERRED_REFERENCE_UNAVAILABLE})
        return terminal_payload(workflow, DEFERRED_REFERENCE_UNAVAILABLE, ["reference_rpc_must_be_independent"], checks, index_path=str(index_path), source_url=redacted_url(source_url), reference_url=redacted_url(reference_url))

    head_order = index_head_order(index)
    try:
        source_tip, source_tip_method = fetch_tip(source_url, timeout, env, rpc)
    except Exception as exc:  # noqa: BLE001
        checks.append({"name": "source_tip", "state": REJECTED_SOURCE_UNREADY, "source_url": redacted_url(source_url)})
        return terminal_payload(workflow, REJECTED_SOURCE_UNREADY, [f"source_tip_unavailable:{exc}"], checks, index_path=str(index_path), source_url=redacted_url(source_url))
    try:
        reference_tip, reference_tip_method = fetch_tip(reference_url, timeout, env, rpc)
    except Exception as exc:  # noqa: BLE001
        checks.append({"name": "reference_tip", "state": DEFERRED_REFERENCE_UNAVAILABLE, "reference_url": redacted_url(reference_url)})
        return terminal_payload(workflow, DEFERRED_REFERENCE_UNAVAILABLE, [f"reference_tip_unavailable:{exc}"], checks, index_path=str(index_path), source_url=redacted_url(source_url), reference_url=redacted_url(reference_url), source_tip_order=source_tip)
    tips = {
        "source_tip_order": source_tip,
        "source_tip_method": source_tip_method,
        "reference_tip_order": reference_tip,
        "reference_tip_method": reference_tip_method,
        "index_head_order": head_order,
    }
    checks.append({"name": "tips", "state": TRUSTED, **tips})
    min_required = max(head_order, end_order)
    if min_required and source_tip < min_required:
        return terminal_payload(workflow, REJECTED_SOURCE_UNREADY, [f"source_tip_{source_tip}_behind_required_{min_required}"], checks, index_path=str(index_path), source_url=redacted_url(source_url), reference_url=redacted_url(reference_url), **tips)
    if min_required and reference_tip < min_required:
        return terminal_payload(workflow, DEFERRED_REFERENCE_UNAVAILABLE, [f"reference_tip_{reference_tip}_behind_required_{min_required}"], checks, index_path=str(index_path), source_url=redacted_url(source_url), reference_url=redacted_url(reference_url), **tips)

    try:
        source_genesis = fetch_genesis_hash(source_url, timeout, env, rpc)
    except Exception as exc:  # noqa: BLE001
        return terminal_payload(workflow, REJECTED_SOURCE_UNREADY, [f"source_genesis_unavailable:{exc}"], checks, index_path=str(index_path), source_url=redacted_url(source_url), reference_url=redacted_url(reference_url), **tips)
    try:
        reference_genesis = fetch_genesis_hash(reference_url, timeout, env, rpc)
    except Exception as exc:  # noqa: BLE001
        return terminal_payload(workflow, DEFERRED_REFERENCE_UNAVAILABLE, [f"reference_genesis_unavailable:{exc}"], checks, index_path=str(index_path), source_url=redacted_url(source_url), reference_url=redacted_url(reference_url), **tips)
    genesis_state = TRUSTED if source_genesis == reference_genesis else REJECTED_MISMATCH
    checks.append({"name": "genesis", "state": genesis_state, "source_genesis": source_genesis, "reference_genesis": reference_genesis})
    if genesis_state != TRUSTED:
        return terminal_payload(workflow, REJECTED_MISMATCH, ["genesis_hash_mismatch"], checks, index_path=str(index_path), source_url=redacted_url(source_url), reference_url=redacted_url(reference_url), **tips)

    source_network = fetch_network_identity(source_url, timeout, env, rpc)
    reference_network = fetch_network_identity(reference_url, timeout, env, rpc)
    if source_network and reference_network and source_network != reference_network:
        checks.append({"name": "network_identity", "state": REJECTED_MISMATCH, "source_network": source_network, "reference_network": reference_network})
        return terminal_payload(workflow, REJECTED_MISMATCH, ["network_identity_mismatch"], checks, index_path=str(index_path), source_url=redacted_url(source_url), reference_url=redacted_url(reference_url), **tips)
    checks.append({"name": "network_identity", "state": TRUSTED, "source_network": source_network, "reference_network": reference_network})

    index_state, index_chain_reasons, index_details = validate_index_against_chain(index, source_url, reference_url, timeout, env, rpc)
    checks.append({"name": "index_chain_validation", "state": index_state, **index_details})
    if index_state != TRUSTED:
        return terminal_payload(workflow, index_state, index_chain_reasons, checks, index_path=str(index_path), source_url=redacted_url(source_url), reference_url=redacted_url(reference_url), **tips)

    records: list[dict[str, Any]] = []
    if start_order and end_order:
        for order in range(start_order, end_order + 1):
            try:
                source_record = fetch_order_record(source_url, order, timeout, env, rpc)
            except Exception as exc:  # noqa: BLE001
                return terminal_payload(workflow, REJECTED_SOURCE_UNREADY, [f"source_order_{order}_unavailable:{exc}"], checks, index_path=str(index_path), source_url=redacted_url(source_url), reference_url=redacted_url(reference_url), **tips)
            try:
                reference_record = fetch_order_record(reference_url, order, timeout, env, rpc)
            except Exception as exc:  # noqa: BLE001
                return terminal_payload(workflow, DEFERRED_REFERENCE_UNAVAILABLE, [f"reference_order_{order}_unavailable:{exc}"], checks, index_path=str(index_path), source_url=redacted_url(source_url), reference_url=redacted_url(reference_url), **tips)
            mismatches = compare_records(order, source_record, reference_record)
            if mismatches:
                checks.append({"name": "segment_order_mapping", "state": REJECTED_MISMATCH, "order": order})
                return terminal_payload(workflow, REJECTED_MISMATCH, mismatches, checks, index_path=str(index_path), source_url=redacted_url(source_url), reference_url=redacted_url(reference_url), **tips)
            records.append(source_record)
        canonical_payload = {
            "document_type": "bdag_chain_integrity_preflight_segment_v1",
            "workflow": workflow,
            "network": "mainnet",
            "genesis_hash": source_genesis,
            "start_order": start_order,
            "end_order": end_order,
            "block_count": len(records),
            "blocks": records,
        }
        segment_preflight = {
            "canonical_payload_sha256": hashlib.sha256(canonical_json_bytes(canonical_payload)).hexdigest(),
            "canonicalization": "bdag_chain_integrity_preflight_segment_v1",
            "start_order": start_order,
            "end_order": end_order,
            "block_count": len(records),
            "start_hash": records[0]["hash"] if records else None,
            "end_hash": records[-1]["hash"] if records else None,
        }
        checks.append({"name": "segment_order_mapping", "state": TRUSTED, **segment_preflight})
    else:
        segment_preflight = None

    return terminal_payload(
        workflow,
        TRUSTED,
        [],
        checks,
        index_path=str(index_path),
        source_url=redacted_url(source_url),
        reference_url=redacted_url(reference_url),
        source_genesis=source_genesis,
        reference_genesis=reference_genesis,
        source_network=source_network,
        reference_network=reference_network,
        segment_preflight=segment_preflight,
        **tips,
    )


def exit_code_for_state(state: str) -> int:
    if state == TRUSTED:
        return 0
    if state.startswith("deferred_"):
        return 2
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", default="ipfs_segment_writer")
    parser.add_argument("--source-rpc-url", default="")
    parser.add_argument("--reference-rpc-url", default="")
    parser.add_argument("--index", default="")
    parser.add_argument("--start-order", type=int, default=0)
    parser.add_argument("--end-order", type=int, default=0)
    parser.add_argument("--artifact-dir", default="")
    parser.add_argument("--artifact-manifest", default="")
    parser.add_argument("--require-signed-manifest", action="store_true")
    parser.add_argument("--skip-environment-gates", action="store_true")
    parser.add_argument("--status-file", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    env = load_env()
    if args.skip_environment_gates:
        env["BDAG_CHAIN_INTEGRITY_SKIP_ENVIRONMENT_GATES"] = "1"
    config = {
        "workflow": args.workflow,
        "source_rpc_url": args.source_rpc_url,
        "reference_rpc_url": args.reference_rpc_url,
        "index": args.index,
        "start_order": args.start_order,
        "end_order": args.end_order,
        "artifact_dir": args.artifact_dir,
        "artifact_manifest": args.artifact_manifest,
        "require_signed_manifest": args.require_signed_manifest,
    }
    payload = evaluate_chain_integrity(config, env=env)
    if args.status_file:
        atomic_write_json(resolve_path(args.status_file, ROOT / args.status_file), payload)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"{payload['state']}: {', '.join(payload.get('reasons') or ['ok'])}")
    return exit_code_for_state(str(payload.get("state") or ""))


if __name__ == "__main__":
    raise SystemExit(main())

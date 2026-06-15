#!/usr/bin/env python3
"""Fail-closed mining node readiness and topology gate."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_TIMEOUT_SECONDS = 1.5
DEFAULT_SAMPLE_COUNT = 3
DEFAULT_SAMPLE_INTERVAL_SECONDS = 10.0
DEFAULT_MAX_REFERENCE_LAG = 120
DEFAULT_LOG_LOOKBACK_MINUTES = 15
DEFAULT_POW_TYPE = 10
JSON_RPC_CONTENT_TYPE = "application/json"

# Policy markers consumed by the governance suite:
# direct BlockDAG node JSON-RPC endpoints
# unexpected_extra_service
# unexpected_extra_backend
# unexpected_extra_running_container
# unexpected_extra_eligible_node

HARD_TEMPLATE_ERROR_CODES = {
    "bdag_pool_syncing",
    "template_parent_stale",
    "stale_parent",
    "missing_state",
    "missing_trie",
    "missing_state_root",
    "head_state_missing",
    "block_state_missing",
    "chain_stateless",
    "unknown_ancestor",
}

HARD_LOG_PATTERNS = (
    re.compile(r"BAD BLOCK", re.IGNORECASE),
    re.compile(r"unknown ancestor", re.IGNORECASE),
    re.compile(r"Chain is stateless", re.IGNORECASE),
    re.compile(r"Head state missing,\s*repairing", re.IGNORECASE),
    re.compile(r"Block state missing", re.IGNORECASE),
    re.compile(r"Genesis block reached", re.IGNORECASE),
    re.compile(r"node busy syncing", re.IGNORECASE),
    re.compile(r"bdag pool syncing", re.IGNORECASE),
    re.compile(r"missing (trie|state|state-root|state root)", re.IGNORECASE),
    re.compile(r"stale parent", re.IGNORECASE),
    re.compile(r"template_parent_stale", re.IGNORECASE),
)

class RpcCallError(Exception):
    """Typed JSON-RPC transport or response error."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(detail or kind)
        self.kind = kind
        self.detail = detail or kind


@dataclass(frozen=True)
class Backend:
    name: str
    url: str


def normalize_key(value: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value))
    value = value.replace("-", "_").replace(".", "_")
    return re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").lower()


def normalize_code(value: Any) -> str:
    return normalize_key(str(value or "").strip())


def parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "ready", "ok"}:
            return True
        if lowered in {"0", "false", "no", "off", "not_ready", "fail", "failed"}:
            return False
    return None


def parse_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def flatten_mapping(value: Any, prefix: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    if not isinstance(value, dict):
        return flattened
    for key, item in value.items():
        normalized = normalize_key(str(key))
        flattened[normalized] = item
        if prefix:
            flattened[f"{prefix}_{normalized}"] = item
        if isinstance(item, dict):
            flattened.update(flatten_mapping(item, normalized))
    return flattened


def first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        normalized = normalize_key(key)
        if normalized in mapping:
            return mapping[normalized]
    return None


def first_number(mapping: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        parsed = parse_number(first_present(mapping, key))
        if parsed is not None:
            return parsed
    return None


def positive_int(value: float | int | None) -> int | None:
    if value is None or value <= 0:
        return None
    return int(value)


def parse_backend_arg(value: str) -> Backend:
    if "=" not in value:
        raise argparse.ArgumentTypeError(f"backend must be name=url, got {value!r}")
    name, url = value.split("=", 1)
    name = name.strip()
    url = url.strip()
    if not name:
        raise argparse.ArgumentTypeError("backend name is empty")
    if not url.startswith(("http://", "https://")):
        raise argparse.ArgumentTypeError(f"backend {name!r} URL must be http(s), got {url!r}")
    return Backend(name=name, url=url)


def canonical_node_name(value: str) -> str:
    value = value.strip()
    if value == "node" or re.fullmatch(r".*[-_]node[-_]1", value):
        return "node"
    return value


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
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))


def request_url_and_auth(url: str) -> tuple[str, str | None]:
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
        user = os.environ.get("NODE_RPC_USER", os.environ.get("BDAG_NODE_RPC_USER", "")).strip()
        password = os.environ.get("NODE_RPC_PASS", os.environ.get("BDAG_NODE_RPC_PASS", "")).strip()
    if not user and not password:
        return clean_url, None
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return clean_url, f"Basic {token}"


def read_env_file_value(path: Path, key: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].strip()
        if not stripped.startswith(f"{key}="):
            continue
        value = stripped.split("=", 1)[1].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value
    return ""


def default_mining_address(explicit: str | None = None) -> str:
    for value in (
        explicit,
        os.environ.get("MINING_ADDRESS"),
        os.environ.get("BDAG_MINING_ADDRESS"),
        os.environ.get("BDAG_POOL_MINING_ADDRESS"),
    ):
        if value and str(value).strip():
            return str(value).strip()
    for path in (Path.cwd() / ".env", Path.cwd() / "asic-pool" / ".env"):
        value = read_env_file_value(path, "MINING_ADDRESS")
        if value:
            return value
    return ""


def get_block_template_params(pow_type: int = DEFAULT_POW_TYPE, mining_address: str = "") -> list[Any]:
    params: list[Any] = [[], int(pow_type)]
    if mining_address:
        params.append(mining_address)
    return params


def _classify_transport_error(error: BaseException) -> str:
    if isinstance(error, socket.timeout):
        return "timeout"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, urllib.error.HTTPError):
        return f"http_{error.code}"
    if isinstance(error, urllib.error.URLError):
        reason = getattr(error, "reason", None)
        if isinstance(reason, ConnectionRefusedError):
            return "connection_refused"
        if isinstance(reason, socket.timeout):
            return "timeout"
        if isinstance(reason, OSError) and getattr(reason, "errno", None) in {111, 61}:
            return "connection_refused"
        return "connection_error"
    if isinstance(error, ConnectionRefusedError):
        return "connection_refused"
    return "transport_error"


def json_rpc_call(
    url: str,
    method: str,
    params: list[Any] | dict[str, Any] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Any:
    request_url, authorization = request_url_and_auth(url)
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": [] if params is None else params,
        }
    ).encode("utf-8")
    headers = {"Content-Type": JSON_RPC_CONTENT_TYPE}
    if authorization:
        headers["Authorization"] = authorization
    request = urllib.request.Request(
        request_url,
        data=payload,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        raise RpcCallError(_classify_transport_error(exc), str(exc)) from exc
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RpcCallError("invalid_json", str(exc)) from exc
    if not isinstance(decoded, dict):
        raise RpcCallError("invalid_jsonrpc_response", "response is not an object")
    if decoded.get("error") is not None:
        error = decoded.get("error")
        if isinstance(error, dict):
            code = error.get("code", "")
            message = str(error.get("message") or "")
            kind = "method_not_found" if code == -32601 else "jsonrpc_error"
            raise RpcCallError(kind, f"{code}:{message}".strip(":"))
        raise RpcCallError("jsonrpc_error", str(error))
    return decoded.get("result")


def extract_height(result: Any) -> int | None:
    if isinstance(result, (int, float)) and not isinstance(result, bool):
        return positive_int(result)
    mapping = flatten_mapping(result)
    return positive_int(
        first_number(
            mapping,
            "height",
            "block_count",
            "blockcount",
            "latest_block",
            "latestBlock",
            "chain_head_block",
            "blocks",
        )
    )


def extract_main_order(result: Any) -> int | None:
    mapping = flatten_mapping(result)
    return positive_int(
        first_number(
            mapping,
            "main_order",
            "mainOrder",
            "best_main_order",
            "bestMainOrder",
            "order",
            "blue_score",
        )
    )


def normalized_health(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    return flatten_mapping(result)


def get_health_bool(health: dict[str, Any], key: str, *aliases: str) -> bool | None:
    for candidate in (key, *aliases):
        value = parse_bool(first_present(health, candidate))
        if value is not None:
            return value
    return None


def add_bool_predicate(
    failures: list[str],
    warnings: list[str],
    health: dict[str, Any],
    key: str,
    *,
    aliases: tuple[str, ...] = (),
    required: bool = True,
    required_after_incident: bool = False,
    after_chain_incident: bool = False,
) -> None:
    value = get_health_bool(health, key, *aliases)
    if value is True:
        return
    if value is False:
        failures.append(f"{normalize_key(key)}_false")
        return
    if required or (required_after_incident and after_chain_incident):
        failures.append(f"{normalize_key(key)}_missing")
    else:
        warnings.append(f"{normalize_key(key)}_missing")


def template_error_codes(health: dict[str, Any]) -> list[str]:
    codes: list[str] = []
    for key in (
        "last_template_build_error_code",
        "lastTemplateBuildErrorCode",
        "reason_code",
        "reasonCode",
        "template_error_code",
        "templateErrorCode",
    ):
        value = first_present(health, key)
        if value is None:
            continue
        if isinstance(value, list):
            codes.extend(normalize_code(item) for item in value if normalize_code(item))
        else:
            code = normalize_code(value)
            if code:
                codes.append(code)
    return codes


def probe_reference(
    reference_rpc_url: str | None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if not reference_rpc_url:
        return {"configured": False, "available": False, "height": None, "main_order": None, "failures": []}
    result: dict[str, Any] = {
        "configured": True,
        "available": False,
        "height": None,
        "main_order": None,
        "failures": [],
    }
    try:
        block_count_result = json_rpc_call(reference_rpc_url, "getBlockCount", timeout=timeout)
        result["height"] = extract_height(block_count_result)
    except RpcCallError as exc:
        result["failures"].append(f"reference getBlockCount failed: {exc.kind}")
        return result
    try:
        health_result = json_rpc_call(reference_rpc_url, "getTemplateHealth", timeout=timeout)
        result["main_order"] = extract_main_order(health_result)
    except RpcCallError:
        result["main_order"] = None
    if result["height"] is not None:
        result["available"] = True
    return result


def probe_backend_once(
    backend: Backend,
    *,
    reference_rpc_url: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    after_chain_incident: bool = False,
    max_reference_lag: int = DEFAULT_MAX_REFERENCE_LAG,
    allow_reference_unavailable: bool = False,
    pow_type: int = DEFAULT_POW_TYPE,
    mining_address: str = "",
) -> dict[str, Any]:
    sample: dict[str, Any] = {
        "backend": backend.name,
        "url": redacted_url(backend.url),
        "ok": False,
        "status": "fail",
        "rpc_ok": False,
        "height": None,
        "main_order": None,
        "template_health_available": False,
        "get_block_template_ok": False,
        "reference": None,
        "failures": [],
        "warnings": [],
    }
    failures: list[str] = sample["failures"]
    warnings: list[str] = sample["warnings"]

    try:
        block_count_result = json_rpc_call(backend.url, "getBlockCount", timeout=timeout)
    except RpcCallError as exc:
        failures.append(f"getBlockCount failed: {exc.kind}")
        return sample

    height = extract_height(block_count_result)
    sample["height"] = height
    if height is None:
        failures.append("height_missing_or_non_positive")
    else:
        sample["rpc_ok"] = True

    health: dict[str, Any] = {}
    try:
        health_result = json_rpc_call(backend.url, "getTemplateHealth", timeout=timeout)
        health = normalized_health(health_result)
        if not health:
            failures.append("template_health_invalid")
        else:
            sample["template_health_available"] = True
    except RpcCallError as exc:
        if exc.kind == "method_not_found" and after_chain_incident:
            failures.append("template_health_missing_after_chain_incident")
        elif exc.kind == "method_not_found":
            failures.append("template_health_missing")
        else:
            failures.append(f"getTemplateHealth failed: {exc.kind}")

    if health:
        main_order = extract_main_order(health)
        sample["main_order"] = main_order
        if main_order is None:
            failures.append("main_order_missing_or_non_positive")

        add_bool_predicate(
            failures,
            warnings,
            health,
            "is_current",
            aliases=("chain_current",),
            required=True,
            after_chain_incident=after_chain_incident,
        )
        add_bool_predicate(
            failures,
            warnings,
            health,
            "mineable_now",
            required=True,
            after_chain_incident=after_chain_incident,
        )
        add_bool_predicate(
            failures,
            warnings,
            health,
            "submit_ready",
            required=True,
            after_chain_incident=after_chain_incident,
        )
        add_bool_predicate(
            failures,
            warnings,
            health,
            "get_block_template_ready",
            required=True,
            after_chain_incident=after_chain_incident,
        )
        for optional_key in ("template_usable", "sync_allowed"):
            optional_value = get_health_bool(health, optional_key)
            if optional_value is False:
                failures.append(f"{optional_key}_false")

        add_bool_predicate(
            failures,
            warnings,
            health,
            "p2p_mining_fresh",
            required=False,
            required_after_incident=True,
            after_chain_incident=after_chain_incident,
        )

        peer_lead = first_number(health, "p2p_best_peer_lead_blocks", "p2pBestPeerLeadBlocks")
        if peer_lead is not None and peer_lead > 12:
            failures.append(f"p2p_best_peer_lead_blocks_{int(peer_lead)}_gt_12")

        submit_no_synced = get_health_bool(health, "submit_no_synced")
        if after_chain_incident and submit_no_synced is True:
            failures.append("submit_no_synced_true_after_chain_incident")

        for code in template_error_codes(health):
            if code in HARD_TEMPLATE_ERROR_CODES:
                failures.append(f"blocking_template_error:{code}")

    try:
        template_result = json_rpc_call(
            backend.url,
            "getBlockTemplate",
            get_block_template_params(pow_type, mining_address),
            timeout=timeout,
        )
        if not isinstance(template_result, dict):
            failures.append("getBlockTemplate returned non-object")
        elif not template_result:
            failures.append("getBlockTemplate_empty")
        else:
            sample["get_block_template_ok"] = True
            template_mapping = flatten_mapping(template_result)
            template_order = extract_main_order(template_result)
            if sample["main_order"] is not None and template_order is not None:
                if template_order < int(sample["main_order"]):
                    failures.append(
                        f"getBlockTemplate_order_{template_order}_lt_health_main_order_{sample['main_order']}"
                    )
            health_parent = first_present(health, "template_parent", "templateParent", "parent")
            template_parent = first_present(
                template_mapping,
                "parent",
                "template_parent",
                "previousblockhash",
                "previous_block_hash",
            )
            if health_parent and template_parent and str(health_parent) != str(template_parent):
                failures.append("getBlockTemplate_parent_mismatch")
    except RpcCallError as exc:
        failures.append(f"getBlockTemplate failed: {exc.kind}")

    reference = probe_reference(reference_rpc_url, timeout=timeout)
    sample["reference"] = reference
    if reference["configured"] and not reference["available"] and not allow_reference_unavailable:
        failures.append("reference_unavailable")
        sample["status"] = "deferred"
    elif reference["available"]:
        reference_height = reference.get("height")
        if height is not None and reference_height is not None:
            lag = int(reference_height) - int(height)
            if lag > max_reference_lag:
                failures.append(f"reference_height_lag_{lag}_gt_{max_reference_lag}")
        reference_main_order = reference.get("main_order")
        main_order = sample.get("main_order")
        if main_order is not None and reference_main_order is not None:
            lag = int(reference_main_order) - int(main_order)
            if lag > max_reference_lag:
                failures.append(f"reference_main_order_lag_{lag}_gt_{max_reference_lag}")

    if not failures:
        sample["ok"] = True
        sample["status"] = "pass"
    elif sample["status"] != "deferred":
        sample["status"] = "fail"
    return sample


def evaluate_backend(
    backend: Backend,
    *,
    reference_rpc_url: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    sample_count: int = DEFAULT_SAMPLE_COUNT,
    sample_interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
    after_chain_incident: bool = False,
    max_reference_lag: int = DEFAULT_MAX_REFERENCE_LAG,
    allow_reference_unavailable: bool = False,
    pow_type: int = DEFAULT_POW_TYPE,
    mining_address: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    started_at = time.time() if now is None else now
    result: dict[str, Any] = {
        "backend": backend.name,
        "url": redacted_url(backend.url),
        "ready": False,
        "status": "not_ready",
        "sample_count": sample_count,
        "samples": [],
        "failures": [],
        "warnings": [],
        "height": None,
        "main_order": None,
        "started_at_epoch": started_at,
        "completed_at_epoch": None,
    }
    previous_height: int | None = None
    previous_main_order: int | None = None

    for index in range(max(1, sample_count)):
        sample = probe_backend_once(
            backend,
            reference_rpc_url=reference_rpc_url,
            timeout=timeout,
            after_chain_incident=after_chain_incident,
            max_reference_lag=max_reference_lag,
            allow_reference_unavailable=allow_reference_unavailable,
            pow_type=pow_type,
            mining_address=mining_address,
        )
        sample["sample_index"] = index + 1
        height = sample.get("height")
        main_order = sample.get("main_order")
        if sample.get("ok"):
            if previous_height is not None and height is not None and int(height) < previous_height:
                sample["ok"] = False
                sample["status"] = "fail"
                sample["failures"].append(f"height_regressed_{height}_lt_{previous_height}")
            if previous_main_order is not None and main_order is not None and int(main_order) < previous_main_order:
                sample["ok"] = False
                sample["status"] = "fail"
                sample["failures"].append(f"main_order_regressed_{main_order}_lt_{previous_main_order}")
        if height is not None:
            previous_height = int(height)
            result["height"] = int(height)
        if main_order is not None:
            previous_main_order = int(main_order)
            result["main_order"] = int(main_order)
        result["samples"].append(sample)
        result["warnings"].extend(sample.get("warnings") or [])
        if sample.get("failures"):
            result["failures"].extend(sample["failures"])
        if index + 1 < sample_count and sample_interval_seconds > 0:
            time.sleep(sample_interval_seconds)

    result["completed_at_epoch"] = time.time()
    if len(result["samples"]) == sample_count and all(sample.get("ok") for sample in result["samples"]):
        result["ready"] = True
        result["status"] = "ready"
    return result


def validate_topology(
    *,
    node_service: str | None = None,
    backend_name: str | None = None,
    running_containers: list[str] | None = None,
    eligible_backend: str | None = None,
    strict_routing: bool = False,
) -> dict[str, Any]:
    normalized_service = canonical_node_name(node_service or "node")
    normalized_backend = canonical_node_name(backend_name or "")
    normalized_running = sorted({canonical_node_name(item) for item in (running_containers or []) if item})
    normalized_eligible = canonical_node_name(eligible_backend or "")
    failures: list[str] = []

    if normalized_service != "node":
        failures.append(f"unexpected_extra_service:{normalized_service}")
    if normalized_backend and normalized_backend != "node":
        failures.append(f"unexpected_extra_backend:{normalized_backend}")
    if normalized_eligible and normalized_eligible != "node":
        failures.append(f"unexpected_extra_eligible_node:{normalized_eligible}")
    for node in normalized_running:
        if node != "node":
            failures.append(f"unexpected_extra_running_container:{node}")

    if running_containers is not None and "node" in normalized_running and normalized_service != "node":
        failures.append("running_container_not_declared:node")

    if strict_routing and normalized_backend and normalized_backend != normalized_eligible:
        failures.append(f"pool_routes_ineligible_backend:{normalized_backend}")

    return {
        "ok": not failures,
        "failures": failures,
        "topology": "single_node",
        "node_service": normalized_service,
        "backend": normalized_backend,
        "running_containers": normalized_running,
        "eligible_backend": normalized_eligible,
        "strict_routing": strict_routing,
    }


def read_log_source(source: str, *, lookback_minutes: int = DEFAULT_LOG_LOOKBACK_MINUTES) -> tuple[str, str | None]:
    if source == "-":
        return sys.stdin.read(), None
    if source.startswith("file:"):
        path = Path(source[5:])
        return path.read_text(encoding="utf-8", errors="replace"), None
    path = Path(source)
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace"), None
    if source.startswith("docker:"):
        container = source.split(":", 1)[1].strip()
        if not container:
            return "", "empty_docker_log_source"
        since = f"{max(1, lookback_minutes)}m"
        try:
            completed = subprocess.run(
                ["docker", "logs", "--since", since, container],
                check=False,
                capture_output=True,
                text=True,
                timeout=min(20, max(5, lookback_minutes)),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return "", f"docker_log_source_failed:{type(exc).__name__}"
        if completed.returncode != 0:
            return completed.stdout + completed.stderr, f"docker_logs_returned_{completed.returncode}"
        return completed.stdout + completed.stderr, None
    return "", f"unknown_log_source:{source}"


def scan_bad_log_text(text: str, *, lookback_minutes: int = DEFAULT_LOG_LOOKBACK_MINUTES) -> dict[str, Any]:
    hard_matches: list[str] = []
    for pattern in HARD_LOG_PATTERNS:
        if pattern.search(text):
            hard_matches.append(pattern.pattern)
    zero_state_root_count = len(re.findall(r"Zero state root hash", text, flags=re.IGNORECASE))
    zero_state_root_rate = zero_state_root_count / max(1, lookback_minutes)
    failures = [f"hard_log_veto:{pattern}" for pattern in sorted(set(hard_matches))]
    warnings: list[str] = []
    if zero_state_root_count:
        if zero_state_root_rate > 60 or hard_matches:
            failures.append(f"zero_state_root_burst:{zero_state_root_count}")
        else:
            warnings.append(f"zero_state_root_warning:{zero_state_root_count}")
    return {
        "ok": not failures,
        "failures": failures,
        "warnings": warnings,
        "zero_state_root_count": zero_state_root_count,
        "zero_state_root_per_minute": zero_state_root_rate,
    }


def collect_log_vetoes(
    sources: list[str],
    *,
    lookback_minutes: int = DEFAULT_LOG_LOOKBACK_MINUTES,
) -> dict[str, Any]:
    combined_failures: list[str] = []
    combined_warnings: list[str] = []
    source_results: list[dict[str, Any]] = []
    for source in sources:
        text, error = read_log_source(source, lookback_minutes=lookback_minutes)
        scan = scan_bad_log_text(text, lookback_minutes=lookback_minutes)
        if error:
            scan["warnings"].append(error)
        combined_failures.extend(scan["failures"])
        combined_warnings.extend(scan["warnings"])
        source_results.append({"source": source, **scan})
    return {
        "ok": not combined_failures,
        "failures": combined_failures,
        "warnings": combined_warnings,
        "sources": source_results,
    }


def evaluate_gate(
    backend: Backend,
    *,
    reference_rpc_url: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    sample_count: int = DEFAULT_SAMPLE_COUNT,
    sample_interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
    after_chain_incident: bool = False,
    max_reference_lag: int = DEFAULT_MAX_REFERENCE_LAG,
    allow_reference_unavailable: bool = False,
    log_sources: list[str] | None = None,
    log_lookback_minutes: int = DEFAULT_LOG_LOOKBACK_MINUTES,
    node_service: str | None = None,
    running_containers: list[str] | None = None,
    strict_routing: bool = False,
    pow_type: int = DEFAULT_POW_TYPE,
    mining_address: str | None = None,
) -> dict[str, Any]:
    resolved_mining_address = default_mining_address(mining_address)
    backend_result = evaluate_backend(
        backend,
        reference_rpc_url=reference_rpc_url,
        timeout=timeout,
        sample_count=sample_count,
        sample_interval_seconds=sample_interval_seconds,
        after_chain_incident=after_chain_incident,
        max_reference_lag=max_reference_lag,
        allow_reference_unavailable=allow_reference_unavailable,
        pow_type=pow_type,
        mining_address=resolved_mining_address,
    )
    eligible_backend = backend.name if backend_result.get("ready") else ""

    topology = validate_topology(
        node_service=node_service,
        backend_name=backend.name,
        running_containers=running_containers,
        eligible_backend=eligible_backend,
        strict_routing=strict_routing,
    )
    logs = collect_log_vetoes(log_sources or [], lookback_minutes=log_lookback_minutes)
    failures: list[str] = []
    if not eligible_backend:
        failures.append("node_not_ready")
    failures.extend(logs["failures"])
    failures.extend(topology["failures"])

    return {
        "ok": not failures,
        "status": "ready" if not failures else "not_ready",
        "eligible_backend": eligible_backend,
        "failures": failures,
        "backend": backend_result,
        "topology": topology,
        "logs": logs,
        "policy": {
            "single_node_backend_only": True,
            "sample_count": sample_count,
            "sample_interval_seconds": sample_interval_seconds,
            "timeout_seconds": timeout,
            "after_chain_incident": after_chain_incident,
            "max_reference_lag": max_reference_lag,
            "allow_reference_unavailable": allow_reference_unavailable,
            "pow_type": pow_type,
            "mining_address_present": bool(resolved_mining_address),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=parse_backend_arg, required=True, help="Direct node backend name=url")
    parser.add_argument(
        "--reference-rpc-url",
        default=os.environ.get("BDAG_CHAIN_REFERENCE_RPC_URL") or None,
        help="Independent reference node JSON-RPC URL",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLE_COUNT)
    parser.add_argument("--sample-interval", type=float, default=DEFAULT_SAMPLE_INTERVAL_SECONDS)
    parser.add_argument("--max-reference-lag", type=int, default=DEFAULT_MAX_REFERENCE_LAG)
    parser.add_argument("--pow-type", type=int, default=DEFAULT_POW_TYPE)
    parser.add_argument("--mining-address", default=None)
    parser.add_argument(
        "--after-chain-incident",
        action="store_true",
        default=os.environ.get("BDAG_CHAIN_INCIDENT", "").strip().lower() in {"1", "true", "yes", "on"},
        help="Fail closed on old nodes missing getTemplateHealth and stricter post-incident predicates",
    )
    parser.add_argument(
        "--allow-reference-unavailable",
        action="store_true",
        help="Do not defer when the optional reference URL is configured but unavailable",
    )
    parser.add_argument("--log-source", action="append", default=[], help="file:/path, plain path, -, or docker:container")
    parser.add_argument("--log-lookback-minutes", type=int, default=DEFAULT_LOG_LOOKBACK_MINUTES)
    parser.add_argument("--node-service", default=os.environ.get("BDAG_NODE_SERVICE") or "node")
    parser.add_argument("--running-container", action="append", default=[])
    parser.add_argument("--strict-routing", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = evaluate_gate(
        args.backend,
        reference_rpc_url=args.reference_rpc_url,
        timeout=args.timeout,
        sample_count=args.samples,
        sample_interval_seconds=args.sample_interval,
        after_chain_incident=args.after_chain_incident,
        max_reference_lag=args.max_reference_lag,
        allow_reference_unavailable=args.allow_reference_unavailable,
        log_sources=args.log_source,
        log_lookback_minutes=args.log_lookback_minutes,
        node_service=args.node_service,
        running_containers=args.running_container,
        strict_routing=args.strict_routing,
        pow_type=args.pow_type,
        mining_address=args.mining_address,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"mining readiness: {result['status']}")
        if result["eligible_backend"]:
            print("eligible backend: " + str(result["eligible_backend"]))
        if result["failures"]:
            print("failures:")
            for failure in result["failures"]:
                print(f"  - {failure}")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

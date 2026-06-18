#!/usr/bin/env python3
"""Release/install readiness gates for a BlockDAG pool-stack deployment.

The checks are intentionally read-only:
  - verify the pool Postgres schema exists,
  - verify the node is synced or explicitly mineable,
  - verify peer sanity while filtering self/loopback peers,
  - verify getBlockTemplate returns a usable mining template.
"""

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
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RPC_URL = "http://127.0.0.1:38131"
DEFAULT_SCHEMA_FILE = ROOT / "sql" / "pool-schema.sql"
REQUIRED_SCHEMA = {
    "miners": {"address", "joined_at", "last_active"},
    "blocks": {"hash", "height", "reward", "fees", "status", "created_at"},
    "credits": {
        "id",
        "block_hash",
        "miner_address",
        "amount",
        "is_paid",
        "created_at",
    },
    "block_submissions": {
        "id",
        "candidate_hash",
        "node_block_hash",
        "height",
        "backend",
        "template_seq",
        "accepted",
        "outcome",
        "message",
        "created_at",
    },
    "payouts": {"id", "tx_hash", "amount", "created_at"},
}
REQUIRED_INDEXES = {
    "credits_block_miner_unique": {
        "table": "credits",
        "columns": ("block_hash", "miner_address"),
        "unique": True,
    },
    "block_submissions_created_at_idx": {
        "table": "block_submissions",
        "columns": ("created_at",),
        "unique": False,
    },
    "block_submissions_outcome_created_idx": {
        "table": "block_submissions",
        "columns": ("outcome", "created_at"),
        "unique": False,
    },
}


class CheckError(RuntimeError):
    pass


class RPCError(CheckError):
    def __init__(self, method: str, code: Any, message: str) -> None:
        super().__init__(f"{method} RPC error {code}: {message}")
        self.method = method
        self.code = code
        self.message = message


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    skipped: bool = False


def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def rpc_call(
    url: str,
    user: str,
    password: str,
    method: str,
    params: list[Any] | None = None,
    timeout: float = 5.0,
) -> Any:
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if user or password:
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except (TimeoutError, socket.timeout) as exc:
        raise CheckError(f"{method} timed out after {timeout:.1f}s") from exc
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise CheckError(f"{method} HTTP {exc.code}: {body[:240]}") from exc
    except urllib.error.URLError as exc:
        raise CheckError(f"{method} connection failed: {exc.reason}") from exc

    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CheckError(f"{method} returned invalid JSON: {raw[:240]}") from exc
    if decoded.get("error") not in (None, {}, []):
        err = decoded["error"]
        if isinstance(err, dict):
            raise RPCError(method, err.get("code"), str(err.get("message", err)))
        raise RPCError(method, "unknown", str(err))
    if "result" not in decoded:
        raise CheckError(f"{method} response did not include result")
    return decoded["result"]


def method_not_found(err: RPCError) -> bool:
    return err.code == -32601 or "method not found" in err.message.lower()


def check_postgres_schema(args: argparse.Namespace, env: dict[str, str]) -> CheckResult:
    if args.skip_postgres:
        return CheckResult("postgres_schema", True, "skipped by request", skipped=True)

    if args.schema_file and not Path(args.schema_file).exists():
        raise CheckError(f"schema file is missing: {args.schema_file}")

    query = """
WITH required(table_name, column_name) AS (
  VALUES
    ('miners','address'), ('miners','joined_at'), ('miners','last_active'),
    ('blocks','hash'), ('blocks','height'), ('blocks','reward'), ('blocks','fees'),
    ('blocks','status'), ('blocks','created_at'),
    ('credits','id'), ('credits','block_hash'), ('credits','miner_address'),
    ('credits','amount'), ('credits','is_paid'), ('credits','created_at'),
    ('block_submissions','id'), ('block_submissions','candidate_hash'),
    ('block_submissions','node_block_hash'), ('block_submissions','height'),
    ('block_submissions','backend'), ('block_submissions','template_seq'),
    ('block_submissions','accepted'), ('block_submissions','outcome'),
    ('block_submissions','message'), ('block_submissions','created_at'),
    ('payouts','id'), ('payouts','tx_hash'), ('payouts','amount'), ('payouts','created_at')
)
SELECT table_name || '.' || column_name
FROM required r
WHERE NOT EXISTS (
  SELECT 1
  FROM information_schema.columns c
  WHERE c.table_schema = 'public'
    AND c.table_name = r.table_name
    AND c.column_name = r.column_name
)
UNION ALL
SELECT 'index:' || r.index_name
FROM (
  VALUES
    ('credits_block_miner_unique', 'credits', '%(block_hash, miner_address)%')
) AS r(index_name, table_name, column_pattern)
WHERE NOT EXISTS (
  SELECT 1
  FROM pg_indexes i
  WHERE i.schemaname = 'public'
    AND i.tablename = r.table_name
    AND i.indexname = r.index_name
    AND i.indexdef ILIKE 'CREATE UNIQUE INDEX%'
    AND i.indexdef LIKE r.column_pattern
)
UNION ALL
SELECT 'index:' || r.index_name
FROM (
  VALUES
    ('block_submissions_created_at_idx', 'block_submissions', '%(created_at)%'),
    ('block_submissions_outcome_created_idx', 'block_submissions', '%(outcome, created_at)%')
) AS r(index_name, table_name, column_pattern)
WHERE NOT EXISTS (
  SELECT 1
  FROM pg_indexes i
  WHERE i.schemaname = 'public'
    AND i.tablename = r.table_name
    AND i.indexname = r.index_name
    AND i.indexdef LIKE r.column_pattern
)
ORDER BY 1;
""".strip()

    postgres_user = env.get("POSTGRES_USER", "bdag_pool")
    postgres_db = env.get("POSTGRES_DB", "bdagpool")
    postgres_password = env.get("POSTGRES_PASSWORD", "")
    if args.pg_url:
        cmd = ["psql", args.pg_url, "-v", "ON_ERROR_STOP=1", "-Atc", query]
        run_env = os.environ.copy()
    else:
        cmd = [
            "docker",
            "compose",
            "exec",
            "-T",
            "-e",
            f"PGPASSWORD={postgres_password}",
            args.postgres_service,
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            postgres_user,
            "-d",
            postgres_db,
            "-Atc",
            query,
        ]
        run_env = os.environ.copy()
        run_env.update(env)

    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        env=run_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.replace(postgres_password, "[redacted]")
        raise CheckError(f"Postgres schema query failed: {stderr.strip()}")
    missing = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if missing:
        return CheckResult("postgres_schema", False, "missing " + ", ".join(missing))
    table_count = len(REQUIRED_SCHEMA)
    column_count = sum(len(cols) for cols in REQUIRED_SCHEMA.values())
    index_count = len(REQUIRED_INDEXES)
    return CheckResult(
        "postgres_schema",
        True,
        f"{table_count} required tables, {column_count} required columns, and {index_count} required indexes present",
    )


def check_sync_or_mineable(args: argparse.Namespace) -> CheckResult:
    try:
        health = rpc_call(
            args.rpc_url,
            args.rpc_user,
            args.rpc_pass,
            "getTemplateHealth",
            timeout=args.timeout,
        )
        if not isinstance(health, dict):
            raise CheckError("getTemplateHealth result is not an object")
        blocking_reasons: list[str] = []
        if health.get("last_template_build_error_blocking") is True:
            code = health.get("last_template_build_error_code") or "template_build_error"
            blocking_reasons.append(f"blocking template build error: {code}")
        if health.get("submit_ready") is False:
            blocking_reasons.append("submit_ready=false")
        if health.get("get_block_template_ready") is False:
            reason = health.get("get_block_template_reason_code") or "unknown"
            blocking_reasons.append(f"get_block_template_ready=false:{reason}")
        if health.get("p2p_mining_fresh") is False:
            reason = health.get("p2p_mining_fresh_reason_code") or "unknown"
            blocking_reasons.append(f"p2p_mining_fresh=false:{reason}")
        if blocking_reasons:
            return CheckResult(
                "node_mineable_or_synced",
                False,
                "; ".join(blocking_reasons),
            )
        mineable = bool(health.get("mineable_now") or health.get("submit_ready"))
        sync_allowed = bool(health.get("sync_allowed"))
        chain_current = bool(health.get("chain_current") or health.get("p2p_current"))
        if mineable:
            return CheckResult(
                "node_mineable_or_synced",
                True,
                "template health reports mineable/submit-ready",
            )
        if chain_current and sync_allowed:
            return CheckResult(
                "node_mineable_or_synced",
                True,
                "template health reports current and sync-allowed",
            )
        reason = health.get("reason_code") or health.get("sync_reason_code") or "unknown"
        return CheckResult(
            "node_mineable_or_synced",
            False,
            f"template health is not mineable/current enough: {reason}",
        )
    except RPCError as exc:
        if not method_not_found(exc):
            raise

    current = rpc_call(
        args.rpc_url, args.rpc_user, args.rpc_pass, "isCurrent", timeout=args.timeout
    )
    if current is True:
        return CheckResult(
            "node_mineable_or_synced",
            True,
            "getTemplateHealth unavailable; isCurrent returned true",
        )
    return CheckResult(
        "node_mineable_or_synced",
        False,
        f"getTemplateHealth unavailable and isCurrent returned {current!r}",
    )


def parse_peer_host(address: str) -> str:
    if not address:
        return ""
    match = re.search(r"/(?:ip4|ip6|dns4|dns6|dns)/([^/]+)", address)
    if match:
        return match.group(1).strip("[]").lower()
    if ":" in address:
        return address.rsplit(":", 1)[0].strip("[]").lower()
    return address.lower()


def is_loopback_or_unspecified(host: str) -> bool:
    host = host.strip().lower()
    if host in {"localhost", "::1", "0:0:0:0:0:0:0:1", "0.0.0.0", "::"}:
        return True
    return host.startswith("127.") or host.startswith("0.")


def check_peer_sanity(args: argparse.Namespace, node_info: dict[str, Any]) -> CheckResult:
    peers = rpc_call(
        args.rpc_url,
        args.rpc_user,
        args.rpc_pass,
        "getPeerInfo",
        params=[True],
        timeout=args.timeout,
    )
    if not isinstance(peers, list):
        raise CheckError("getPeerInfo result is not a list")
    self_id = str(node_info.get("ID") or node_info.get("id") or "")
    sane = []
    rejected = {"self": 0, "invalid": 0, "loopback": 0, "inactive": 0}
    for peer in peers:
        if not isinstance(peer, dict):
            continue
        if self_id and str(peer.get("id", "")) == self_id:
            rejected["self"] += 1
            continue
        host = parse_peer_host(str(peer.get("address", "")))
        if not host:
            rejected["invalid"] += 1
            continue
        if host and is_loopback_or_unspecified(host):
            rejected["loopback"] += 1
            continue
        if peer.get("active") is False or peer.get("state") is False:
            rejected["inactive"] += 1
            continue
        sane.append(peer)
    if len(sane) < args.min_peers:
        return CheckResult(
            "peer_sanity",
            False,
            (
                f"{len(sane)} sane peers, need {args.min_peers}; "
                f"filtered self={rejected['self']} invalid={rejected['invalid']} "
                f"loopback={rejected['loopback']} inactive={rejected['inactive']}"
            ),
        )
    return CheckResult(
        "peer_sanity",
        True,
        f"{len(sane)} sane peers after self/loopback filtering",
    )


def check_get_block_template(args: argparse.Namespace) -> CheckResult:
    params: list[Any] = [[], args.pow_type]
    if args.mining_address:
        params.append(args.mining_address)
    template = rpc_call(
        args.rpc_url,
        args.rpc_user,
        args.rpc_pass,
        "getBlockTemplate",
        params=params,
        timeout=args.timeout,
    )
    if not isinstance(template, dict):
        raise CheckError("getBlockTemplate result is not an object")
    required = [
        "height",
        "previousblockhash",
        "txroot",
        "stateroot",
        "pow_diff_reference",
        "coinbase_address",
    ]
    missing = [key for key in required if key not in template or template[key] in ("", None)]
    nbits = template.get("pow_diff_reference", {})
    if not isinstance(nbits, dict) or not nbits.get("nbits"):
        missing.append("pow_diff_reference.nbits")
    if missing:
        return CheckResult(
            "get_block_template",
            False,
            "template missing " + ", ".join(sorted(set(missing))),
        )
    return CheckResult(
        "get_block_template",
        True,
        f"height={template.get('height')} parent={str(template.get('previousblockhash'))[:16]}...",
    )


def check_mining_rpc_stability(
    args: argparse.Namespace, node_info: dict[str, Any]
) -> CheckResult:
    if args.stability_samples < 1:
        raise CheckError("--stability-samples must be >= 1")
    if args.stability_interval < 0:
        raise CheckError("--stability-interval must be >= 0")
    if args.stability_samples == 1:
        return CheckResult(
            "mining_rpc_stability",
            True,
            "single sample requested",
            skipped=True,
        )

    for sample in range(2, args.stability_samples + 1):
        if args.stability_interval:
            time.sleep(args.stability_interval)
        try:
            sample_results = [
                check_sync_or_mineable(args),
                check_peer_sanity(args, node_info),
                check_get_block_template(args),
            ]
        except CheckError as exc:
            return CheckResult(
                "mining_rpc_stability",
                False,
                f"sample {sample}/{args.stability_samples} failed: {exc}",
            )
        failed = [result for result in sample_results if not result.ok]
        if failed:
            detail = "; ".join(f"{result.name}: {result.detail}" for result in failed)
            return CheckResult(
                "mining_rpc_stability",
                False,
                f"sample {sample}/{args.stability_samples} failed: {detail}",
            )
    return CheckResult(
        "mining_rpc_stability",
        True,
        (
            f"{args.stability_samples} mining RPC samples stable "
            f"at {args.stability_interval:.1f}s interval"
        ),
    )


def run_checks(args: argparse.Namespace) -> list[CheckResult]:
    env = os.environ.copy()
    env.update(load_env_file(Path(args.env_file)))
    args.rpc_url = args.rpc_url or env.get("BDAG_RPC_URL") or DEFAULT_RPC_URL
    args.rpc_user = args.rpc_user if args.rpc_user is not None else env.get("NODE_RPC_USER", "test")
    args.rpc_pass = args.rpc_pass if args.rpc_pass is not None else env.get("NODE_RPC_PASS", "test")
    args.mining_address = args.mining_address or env.get("MINING_POOL_ADDRESS", "")
    args.schema_file = args.schema_file or str(DEFAULT_SCHEMA_FILE)

    results: list[CheckResult] = []
    results.append(check_postgres_schema(args, env))
    node_info: dict[str, Any] = {}
    try:
        raw_node_info = rpc_call(
            args.rpc_url, args.rpc_user, args.rpc_pass, "getNodeInfo", timeout=args.timeout
        )
        if not isinstance(raw_node_info, dict):
            raise CheckError("getNodeInfo result is not an object")
        node_info = raw_node_info
        results.append(
            CheckResult(
                "node_rpc",
                True,
                f"network={node_info.get('network', 'unknown')} connections={node_info.get('connections', 'unknown')}",
            )
        )
    except CheckError as exc:
        results.append(
            CheckResult(
                "node_rpc",
                True,
                f"getNodeInfo unavailable; continuing with functional mining RPC checks: {exc}",
                skipped=True,
            )
        )
    results.append(check_sync_or_mineable(args))
    results.append(check_peer_sanity(args, node_info))
    results.append(check_get_block_template(args))
    results.append(check_mining_rpc_stability(args, node_info))
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run read-only BlockDAG release/install readiness gates."
    )
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    parser.add_argument("--rpc-url", default=None)
    parser.add_argument("--rpc-user", default=None)
    parser.add_argument("--rpc-pass", default=None)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--min-peers", type=int, default=1)
    parser.add_argument("--stability-samples", type=int, default=3)
    parser.add_argument("--stability-interval", type=float, default=1.0)
    parser.add_argument("--pow-type", type=int, default=10)
    parser.add_argument("--mining-address", default=None)
    parser.add_argument("--skip-postgres", action="store_true")
    parser.add_argument("--postgres-service", default="postgres")
    parser.add_argument("--pg-url", default=None, help="Optional direct psql URL.")
    parser.add_argument("--schema-file", default=None)
    parser.add_argument("--json", action="store_true", help="Emit JSON summary.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        results = run_checks(args)
    except CheckError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            print(f"FAIL readiness: {exc}", file=sys.stderr)
        return 1

    ok = all(result.ok for result in results)
    if args.json:
        print(
            json.dumps(
                {
                    "ok": ok,
                    "checks": [result.__dict__ for result in results],
                },
                indent=2,
            )
        )
    else:
        for result in results:
            status = "SKIP" if result.skipped else ("PASS" if result.ok else "FAIL")
            print(f"{status} {result.name}: {result.detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

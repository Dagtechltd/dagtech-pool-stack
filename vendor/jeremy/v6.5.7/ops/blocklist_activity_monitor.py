#!/usr/bin/env python3
"""Periodic blocked-address transaction monitor for BlockDAG EVM RPC."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get("BDAG_RUNTIME_DIR", ROOT / "ops" / "runtime")).expanduser()
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_runtime(*paths: Path) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    (RUNTIME_DIR / "logs").mkdir(parents=True, exist_ok=True)
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_runtime(path)
    data = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as handle:
        handle.write(data)
        temp_name = handle.name
    Path(temp_name).replace(path)


def parse_addresses(path: Path) -> tuple[list[str], list[str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SystemExit(f"address file unavailable: {path}: {exc}") from exc

    addresses: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for line in lines:
        value = line.split("#", 1)[0].strip()
        if not value:
            continue
        if not ADDRESS_RE.match(value):
            invalid.append(value)
            continue
        key = value.lower()
        if key not in seen:
            seen.add(key)
            addresses.append(value)
    return addresses, invalid


class RpcClient:
    def __init__(self, url: str, timeout: float) -> None:
        self.url = url
        self.timeout = timeout
        self.next_id = 1

    def _post(self, payload: Any) -> Any:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=data,
            headers={"content-type": "application/json", "accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except OSError:
                body = ""
            raise RuntimeError(f"RPC HTTP {exc.code}: {body[:300]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"RPC connection failed: {exc}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"RPC returned invalid JSON: {body[:300]}") from exc

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        request_id = self.next_id
        self.next_id += 1
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or []}
        response = self._post(payload)
        if not isinstance(response, dict):
            raise RuntimeError(f"RPC returned non-object response for {method}")
        if response.get("error"):
            raise RuntimeError(f"RPC {method} error: {response['error']}")
        return response.get("result")

    def batch(self, calls: list[tuple[str, list[Any]]]) -> list[Any]:
        if not calls:
            return []
        if len(calls) == 1:
            method, params = calls[0]
            return [self.call(method, params)]

        payload: list[dict[str, Any]] = []
        ids: list[int] = []
        for method, params in calls:
            request_id = self.next_id
            self.next_id += 1
            ids.append(request_id)
            payload.append({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})

        try:
            response = self._post(payload)
        except RuntimeError:
            midpoint = len(calls) // 2
            return self.batch(calls[:midpoint]) + self.batch(calls[midpoint:])

        if not isinstance(response, list):
            raise RuntimeError("RPC batch returned non-array response")
        by_id = {item.get("id"): item for item in response if isinstance(item, dict)}
        results: list[Any] = []
        for request_id in ids:
            item = by_id.get(request_id)
            if item is None:
                raise RuntimeError(f"RPC batch missing response id {request_id}")
            if item.get("error"):
                raise RuntimeError(f"RPC batch item error: {item['error']}")
            results.append(item.get("result"))
        return results


def hex_block(value: int) -> str:
    return hex(value)


def hex_to_int(value: Any) -> int:
    if isinstance(value, str):
        return int(value, 16)
    if isinstance(value, int):
        return value
    raise ValueError(f"cannot parse integer from {value!r}")


def compact_tx(tx: dict[str, Any], block: dict[str, Any], matched: list[str], rpc_url: str) -> dict[str, Any]:
    return {
        "generated_at": now_iso(),
        "rpc_url": rpc_url,
        "block_number": hex_to_int(block.get("number")),
        "block_hash": block.get("hash"),
        "block_timestamp": hex_to_int(block.get("timestamp")) if block.get("timestamp") else None,
        "tx_hash": tx.get("hash"),
        "transaction_index": hex_to_int(tx.get("transactionIndex")) if tx.get("transactionIndex") else None,
        "from": tx.get("from"),
        "to": tx.get("to"),
        "nonce": tx.get("nonce"),
        "value": tx.get("value"),
        "input_prefix": str(tx.get("input") or "")[:74],
        "matched_addresses": matched,
    }


def scan_blocks(
    rpc: RpcClient,
    *,
    rpc_url: str,
    addresses: list[str],
    start_block: int,
    end_block: int,
    batch_size: int,
    seen_alerts: set[str],
    alerts_file: Path,
) -> tuple[int, int, list[str]]:
    watched = {address.lower(): address for address in addresses}
    tx_count = 0
    new_alert_count = 0
    new_alert_hashes: list[str] = []
    ensure_runtime(alerts_file)

    with alerts_file.open("a", encoding="utf-8") as alert_handle:
        for batch_start in range(start_block, end_block + 1, batch_size):
            batch_end = min(end_block, batch_start + batch_size - 1)
            calls = [
                ("eth_getBlockByNumber", [hex_block(block_number), True])
                for block_number in range(batch_start, batch_end + 1)
            ]
            blocks = rpc.batch(calls)
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                for tx in block.get("transactions") or []:
                    if not isinstance(tx, dict):
                        continue
                    tx_count += 1
                    matches: list[str] = []
                    from_addr = str(tx.get("from") or "").lower()
                    to_addr = str(tx.get("to") or "").lower()
                    if from_addr in watched:
                        matches.append(watched[from_addr])
                    if to_addr in watched and to_addr != from_addr:
                        matches.append(watched[to_addr])
                    if not matches:
                        continue
                    tx_hash = str(tx.get("hash") or "")
                    if tx_hash and tx_hash in seen_alerts:
                        continue
                    alert = compact_tx(tx, block, matches, rpc_url)
                    alert_handle.write(json.dumps(alert, sort_keys=True, default=str) + "\n")
                    alert_handle.flush()
                    new_alert_count += 1
                    if tx_hash:
                        new_alert_hashes.append(tx_hash)
                        seen_alerts.add(tx_hash)
    return tx_count, new_alert_count, new_alert_hashes


def prune_seen_hashes(existing: list[str], new_hashes: list[str], limit: int) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in [*existing, *new_hashes]:
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        merged.append(value)
    return merged[-max(1, limit) :]


def recent_alert_hashes(path: Path, limit: int) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 2_000_000))
            data = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    hashes: list[str] = []
    for line in data.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        tx_hash = row.get("tx_hash")
        if isinstance(tx_hash, str) and tx_hash:
            hashes.append(tx_hash)
    return hashes[-max(1, limit) :]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rpc-url",
        default=os.environ.get("BDAG_BLOCKLIST_MONITOR_RPC_URL", "https://rpc.blockdag.engineering"),
        help="EVM JSON-RPC URL",
    )
    parser.add_argument(
        "--addresses-file",
        type=Path,
        default=Path(os.environ.get("BDAG_BLOCKLIST_MONITOR_ADDRESSES", RUNTIME_DIR / "blocklist-monitor-addresses.txt")),
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(os.environ.get("BDAG_BLOCKLIST_MONITOR_STATE", RUNTIME_DIR / "blocklist-monitor-state.json")),
    )
    parser.add_argument(
        "--alerts-file",
        type=Path,
        default=Path(os.environ.get("BDAG_BLOCKLIST_MONITOR_ALERTS", RUNTIME_DIR / "blocklist-monitor-alerts.jsonl")),
    )
    parser.add_argument(
        "--summary-file",
        type=Path,
        default=Path(os.environ.get("BDAG_BLOCKLIST_MONITOR_SUMMARY", RUNTIME_DIR / "blocklist-monitor-summary.json")),
    )
    parser.add_argument("--initial-lookback-blocks", type=int, default=int(os.environ.get("BDAG_BLOCKLIST_MONITOR_INITIAL_LOOKBACK_BLOCKS", "5000")))
    parser.add_argument("--max-blocks-per-run", type=int, default=int(os.environ.get("BDAG_BLOCKLIST_MONITOR_MAX_BLOCKS_PER_RUN", "10000")))
    parser.add_argument("--overlap-blocks", type=int, default=int(os.environ.get("BDAG_BLOCKLIST_MONITOR_OVERLAP_BLOCKS", "12")))
    parser.add_argument("--batch-size", type=int, default=int(os.environ.get("BDAG_BLOCKLIST_MONITOR_BATCH_SIZE", "20")))
    parser.add_argument("--seen-hash-limit", type=int, default=int(os.environ.get("BDAG_BLOCKLIST_MONITOR_SEEN_HASH_LIMIT", "20000")))
    parser.add_argument("--timeout-seconds", type=float, default=float(os.environ.get("BDAG_BLOCKLIST_MONITOR_TIMEOUT_SECONDS", "30")))
    parser.add_argument("--jitter-seconds", type=int, default=int(os.environ.get("BDAG_BLOCKLIST_MONITOR_JITTER_SECONDS", "0")))
    parser.add_argument("--start-block", type=int, help="scan this block as the first block instead of using state")
    parser.add_argument("--end-block", type=int, help="scan this block as the final block instead of the latest block")
    parser.add_argument("--no-state-update", action="store_true", help="scan without updating the persistent cursor")
    parser.add_argument("--json", action="store_true", help="print JSON summary")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.jitter_seconds > 0:
        time.sleep(random.randint(0, args.jitter_seconds))

    ensure_runtime(args.addresses_file, args.state_file, args.alerts_file, args.summary_file)
    addresses, invalid = parse_addresses(args.addresses_file)
    if invalid:
        raise SystemExit(f"invalid address entries in {args.addresses_file}: {', '.join(invalid[:8])}")
    if not addresses:
        raise SystemExit(f"no addresses configured in {args.addresses_file}")

    state = read_json(args.state_file)
    seen_hashes = prune_seen_hashes(
        [item for item in state.get("seen_alert_hashes", []) if isinstance(item, str)],
        recent_alert_hashes(args.alerts_file, args.seen_hash_limit),
        args.seen_hash_limit,
    )
    seen_alerts = set(seen_hashes)
    rpc = RpcClient(args.rpc_url, args.timeout_seconds)

    summary: dict[str, Any] = {
        "generated_at": now_iso(),
        "rpc_url": args.rpc_url,
        "address_count": len(addresses),
        "addresses": addresses,
        "errors": [],
    }
    try:
        latest_block = hex_to_int(rpc.call("eth_blockNumber"))
        if args.start_block is not None:
            start_block = max(0, args.start_block)
        else:
            last_scanned = state.get("last_scanned_block")
            if isinstance(last_scanned, int):
                start_block = max(0, last_scanned - max(0, args.overlap_blocks) + 1)
            else:
                start_block = max(0, latest_block - max(1, args.initial_lookback_blocks) + 1)
        if args.end_block is not None:
            end_block = max(start_block, min(latest_block, args.end_block))
        else:
            end_block = min(latest_block, start_block + max(1, args.max_blocks_per_run) - 1)

        summary.update(
            {
                "latest_block": latest_block,
                "start_block": start_block,
                "end_block": end_block,
                "caught_up": end_block >= latest_block,
                "scanned_block_count": max(0, end_block - start_block + 1),
            }
        )
        if end_block >= start_block:
            tx_count, new_alert_count, new_alert_hashes = scan_blocks(
                rpc,
                rpc_url=args.rpc_url,
                addresses=addresses,
                start_block=start_block,
                end_block=end_block,
                batch_size=max(1, args.batch_size),
                seen_alerts=seen_alerts,
                alerts_file=args.alerts_file,
            )
        else:
            tx_count, new_alert_count, new_alert_hashes = 0, 0, []
        seen_hashes = prune_seen_hashes(seen_hashes, new_alert_hashes, args.seen_hash_limit)
        if not args.no_state_update:
            state_payload = {
                "updated_at": now_iso(),
                "rpc_url": args.rpc_url,
                "address_count": len(addresses),
                "last_scanned_block": end_block if end_block >= start_block else state.get("last_scanned_block"),
                "latest_block": latest_block,
                "seen_alert_hashes": seen_hashes,
                "seen_alert_hash_limit": args.seen_hash_limit,
            }
            atomic_write_json(args.state_file, state_payload)
        summary.update({"tx_count": tx_count, "new_alert_count": new_alert_count})
    except Exception as exc:
        summary["errors"].append(str(exc))
        atomic_write_json(args.summary_file, summary)
        if args.json:
            print(json.dumps(summary, indent=2, sort_keys=True, default=str))
        else:
            print(f"blocklist monitor failed: {exc}", file=sys.stderr)
        return 1

    atomic_write_json(args.summary_file, summary)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    else:
        print(
            "blocklist monitor: "
            f"blocks={summary.get('start_block')}..{summary.get('end_block')} "
            f"latest={summary.get('latest_block')} txs={summary.get('tx_count')} "
            f"new_alerts={summary.get('new_alert_count')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

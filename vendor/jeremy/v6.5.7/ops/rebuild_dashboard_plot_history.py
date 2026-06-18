#!/usr/bin/env python3
"""Rebuild dashboard plot history from the local BlockDAG chain RPC."""

from __future__ import annotations

import argparse
import html
import json
import os
import pathlib
import sys
import time
from typing import Any

OPS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


REPORT_DIR = pool_ops.RUNTIME_DIR / "reports"


def render_html_report(payload: dict[str, Any]) -> str:
    status = html.escape(str(payload.get("status") or "unknown"))
    rows = []
    for key in (
        "generated_at",
        "install",
        "hours",
        "window_blocks",
        "workers",
        "rpc_source",
        "latest_block_count",
        "latest_order",
        "latest_at",
        "genesis_at",
        "sample_count",
        "header_order_count",
        "fetched_header_count",
        "fetch_error_count",
        "partial_samples",
        "global_rows",
        "earnings_rows",
        "payment_wallet",
    ):
        rows.append(
            "<tr>"
            f"<th>{html.escape(key)}</th>"
            f"<td><code>{html.escape(str(payload.get(key)))}</code></td>"
            "</tr>"
        )
    metadata = {
        "schema_version": 1,
        "report_type": "dashboard-rpc-history-rebuild",
        "generated_at": payload.get("generated_at"),
        "status": payload.get("status"),
        "source_contract": pool_ops.DASHBOARD_CHAIN_HISTORY_SOURCE_CONTRACT,
    }
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dashboard RPC History Rebuild</title>
  <style>
    body {{ margin:0; background:#0f1720; color:#e5edf5; font:14px/1.55 system-ui,sans-serif; }}
    main {{ max-width:1180px; margin:0 auto; padding:28px 20px 56px; }}
    h1 {{ margin:0 0 8px; font-size:28px; }}
    h2 {{ margin-top:28px; }}
    table {{ width:100%; border-collapse:collapse; background:#141d27; }}
    th,td {{ border:1px solid #314154; padding:9px 10px; text-align:left; vertical-align:top; }}
    th {{ width:240px; background:#1c2836; }}
    code,pre {{ background:#08111b; color:#d5f2ff; border:1px solid #314154; border-radius:6px; }}
    code {{ padding:1px 5px; }}
    pre {{ padding:14px; overflow:auto; }}
    .muted {{ color:#a8b4c2; }}
    .ok {{ color:#6ee7a8; }}
    .warn {{ color:#facc15; }}
  </style>
  <script type="application/json" id="agent-metadata">{json.dumps(metadata, sort_keys=True).replace("</", "<\\/")}</script>
</head>
<body>
<main>
  <h1>Dashboard RPC History Rebuild</h1>
  <p class="muted">Rebuilt Global and Wallet plot source rows from the local chain RPC, then compacted them into the dashboard RAM/disk history tiers.</p>
  <p>Status: <strong class="{'ok' if payload.get('status') == 'ok' else 'warn'}">{status}</strong></p>
  <table>{''.join(rows)}</table>
  <h2>Files</h2>
  <pre>{html.escape(json.dumps(payload.get("history_files", {}), indent=2, sort_keys=True))}</pre>
  <h2>Backups</h2>
  <pre>{html.escape(json.dumps(payload.get("backups", {}), indent=2, sort_keys=True))}</pre>
  <h2>Tier Counts</h2>
  <pre>{html.escape(json.dumps(payload.get("tier_counts", {}), indent=2, sort_keys=True))}</pre>
  <h2>Full Payload</h2>
  <pre>{html.escape(json.dumps(payload, indent=2, sort_keys=True, default=str))}</pre>
</main>
</body>
</html>
"""


def write_report(payload: dict[str, Any]) -> dict[str, str]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    json_path = REPORT_DIR / f"dashboard-rpc-history-rebuild-{stamp}.json"
    html_path = REPORT_DIR / f"dashboard-rpc-history-rebuild-{stamp}.html"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    html_path.write_text(render_html_report(payload), encoding="utf-8")
    return {"json": str(json_path), "html": str(html_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=720, help="history span to rebuild; default is 720 hours")
    parser.add_argument("--window-blocks", type=int, default=pool_ops.DASHBOARD_HISTORY_REBUILD_BLOCK_WINDOW)
    parser.add_argument("--workers", type=int, default=pool_ops.DASHBOARD_HISTORY_REBUILD_RPC_WORKERS)
    parser.add_argument("--install", action="store_true", help="replace live source history files and rebuild tiers")
    parser.add_argument("--dry-run", action="store_true", help="fetch and build in memory without replacing live history")
    parser.add_argument("--write-report", action="store_true", help="write JSON and HTML reports")
    args = parser.parse_args()

    if args.install and args.dry_run:
        parser.error("--install and --dry-run are mutually exclusive")

    last_progress = {"at": 0.0}
    started_at = pool_ops.now_iso()

    def write_state(status: str, phase: str, **extra: Any) -> None:
        pool_ops.write_dashboard_plot_rebuild_state(
            {
                "status": status,
                "phase": phase,
                "started_at": started_at,
                "install": args.install and not args.dry_run,
                "dry_run": args.dry_run,
                "hours": args.hours,
                "window_blocks": args.window_blocks,
                "workers": args.workers,
                "log_file": os.environ.get("BDAG_DASHBOARD_HISTORY_REBUILD_LOG_FILE", ""),
                **extra,
            }
        )

    def progress(done: int, total: int, errors: int) -> None:
        now = time.time()
        if now - last_progress["at"] < 10 and done < total:
            return
        last_progress["at"] = now
        percent = round((float(done) / float(total)) * 100.0, 2) if total else 0.0
        progress_payload = {"progress": done, "total": total, "errors": errors, "percent": percent}
        write_state("running", "fetching sampled block headers", **progress_payload)
        print(json.dumps(progress_payload), flush=True)

    write_state("running", "probing local chain RPC and planning samples", progress=0, total=0, errors=0, percent=0.0)
    try:
        payload = pool_ops.rebuild_dashboard_plot_history_from_chain(
            hours=args.hours,
            window_blocks=args.window_blocks,
            workers=args.workers,
            install=args.install and not args.dry_run,
            progress=progress,
        )
        if args.write_report:
            payload["reports"] = write_report(payload)
        write_state(
            str(payload.get("status") or "unknown"),
            "complete",
            finished_at=pool_ops.now_iso(),
            progress=payload.get("fetched_header_count"),
            total=payload.get("header_order_count"),
            errors=payload.get("fetch_error_count"),
            percent=100.0 if payload.get("fetch_error_count") == 0 else None,
            latest_order=payload.get("latest_order"),
            latest_at=payload.get("latest_at"),
            sample_count=payload.get("sample_count"),
            global_rows=payload.get("global_rows"),
            earnings_rows=payload.get("earnings_rows"),
            partial_samples=payload.get("partial_samples"),
            wallet_24h_rebuild=payload.get("wallet_24h_rebuild"),
            preserved_asic_history=payload.get("preserved_asic_history"),
            history_files=payload.get("history_files"),
            tier_counts=payload.get("tier_counts"),
            reports=payload.get("reports"),
        )
    except Exception as exc:  # noqa: BLE001 - state file must explain failed rebuilds.
        write_state("failed", "failed", finished_at=pool_ops.now_iso(), error=str(exc))
        raise
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0 if payload.get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Evaluate paid-block conversion evidence before release promotion.

The gate is intentionally evidence-file driven. It does not query or mutate the
live stack, so it can be used by release jobs, canary handoffs, and reviewers.
Shares, container liveness, and dashboard overall=ok are not promotion proof.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

from pool_ops import RUNTIME_DIR, now_iso


REPORT_DIR = RUNTIME_DIR / "reports"
DEFAULT_MIN_SECONDS = 3600.0
DEFAULT_MIN_MINER_HOURS = 1.0
DEFAULT_MIN_ACCEPTED_SUBMITS = 1.0
DEFAULT_MIN_ACCEPTED_PER_MINER_HOUR = 1.0
DEFAULT_MIN_CONFIRMED_PAID_BLOCKS = 1.0
DEFAULT_MAX_LOCAL_DROP_RATIO = 0.05
DEFAULT_MAX_SHARE_REJECT_RATIO = 0.25
DEFAULT_MAX_LOCAL_REJECTS_PER_ACCEPTED = 0.05
PASSING_PAID_STATES = {"mining_paid_ok"}
CONFIRMED_KEYS = {
    "confirmed_blue_paid_blocks",
    "confirmed_paid_blocks",
    "chain_confirmed_paid_blocks",
    "paid_confirmed_blocks",
    "confirmed_onchain_paid_blocks",
    "blue_paid_blocks",
}


def number(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def first_number(*values: Any) -> float | None:
    for value in values:
        parsed = number(value)
        if parsed is not None:
            return parsed
    return None


def recursive_first_number(value: Any, keys: set[str]) -> float | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in keys:
                parsed = number(item)
                if parsed is not None:
                    return parsed
        for item in value.values():
            parsed = recursive_first_number(item, keys)
            if parsed is not None:
                return parsed
    elif isinstance(value, list):
        for item in value:
            parsed = recursive_first_number(item, keys)
            if parsed is not None:
                return parsed
    return None


def html_escape(value: Any) -> str:
    return (
        str(value if value is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def repo_failures(source_repos: dict[str, Any], allow_dirty_repos: bool) -> list[str]:
    if allow_dirty_repos:
        return []
    failures: list[str] = []
    for name, state in sorted(source_repos.items()):
        if not isinstance(state, dict) or not state.get("exists", True):
            continue
        if state.get("dirty"):
            failures.append(f"source repo {name} is dirty")
    return failures


def backend_ready_failure(state: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    mineable = state.get("node_mineable")
    submit_ready = state.get("node_submit_ready")
    if mineable == 0 or mineable is False:
        failures.append("selected backend is not mineable")
    if submit_ready == 0 or submit_ready is False:
        failures.append("selected backend is not submit-ready")
    return failures


def baseline_evidence(path: Path, payload: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    paid_state = payload.get("paid_mining_state") if isinstance(payload.get("paid_mining_state"), dict) else {}
    backend = summary.get("selected_backend_state") if isinstance(summary.get("selected_backend_state"), dict) else {}
    measured_seconds = first_number(summary.get("measured_seconds"))
    miner_hours = first_number(summary.get("miner_hours"))
    active_miners = first_number(summary.get("active_miners_end"))
    accepted = first_number(summary.get("accepted_submit_delta"))
    accepted_per_hour = first_number(summary.get("accepted_submit_per_miner_hour"))
    local_drop_ratio = first_number(summary.get("local_candidate_drop_ratio"))
    share_reject_ratio = first_number(summary.get("share_reject_ratio"))
    confirmed = recursive_first_number(payload, CONFIRMED_KEYS)
    failures: list[str] = []

    state = str(paid_state.get("state") or "unknown")
    if state not in PASSING_PAID_STATES:
        failures.append(f"paid_mining_state {state!r} is not release-passing")
    failures.extend(backend_ready_failure(backend))
    failures.extend(repo_failures(payload.get("source_repos") or {}, args.allow_dirty_repos))
    failures.extend(numeric_gate("measured_seconds", measured_seconds, args.min_seconds, "min"))
    if not args.allow_no_miners and (active_miners is None or active_miners <= 0):
        failures.append("no active miners in evidence window")
    failures.extend(numeric_gate("miner_hours", miner_hours, args.min_miner_hours, "min"))
    failures.extend(numeric_gate("accepted_submit_delta", accepted, args.min_accepted_submits, "min"))
    failures.extend(
        numeric_gate(
            "accepted_submit_per_miner_hour",
            accepted_per_hour,
            args.min_accepted_per_miner_hour,
            "min",
        )
    )
    failures.extend(numeric_gate("local_candidate_drop_ratio", local_drop_ratio, args.max_local_drop_ratio, "max"))
    failures.extend(numeric_gate("share_reject_ratio", share_reject_ratio, args.max_share_reject_ratio, "max"))
    if args.require_chain_confirmation:
        failures.extend(
            numeric_gate(
                "confirmed_paid_blocks",
                confirmed,
                args.min_confirmed_paid_blocks,
                "min",
                missing_message="missing confirmed paid-chain evidence",
            )
        )

    return {
        "path": str(path),
        "kind": "paid_conversion_baseline",
        "gate_passed": not failures,
        "failures": failures,
        "metrics": {
            "paid_mining_state": state,
            "measured_seconds": measured_seconds,
            "miner_hours": miner_hours,
            "active_miners": active_miners,
            "accepted_submit_delta": accepted,
            "accepted_submit_per_miner_hour": accepted_per_hour,
            "confirmed_paid_blocks": confirmed,
            "local_candidate_drop_ratio": local_drop_ratio,
            "share_reject_ratio": share_reject_ratio,
            "selected_backend_state": backend,
        },
    }


def ab_summary_evidence(path: Path, payload: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    measured_seconds = first_number(payload.get("measured_seconds"))
    miner_hours = first_number(payload.get("miner_hours"))
    connected_min = first_number(payload.get("connected_miners_min"))
    accepted = first_number(payload.get("accepted_blocks"))
    accepted_per_hour = first_number(payload.get("accepted_blocks_per_miner_hour"))
    rejected_local_per_accepted = first_number(payload.get("rejected_local_per_accepted"))
    confirmed = recursive_first_number(payload, CONFIRMED_KEYS)
    failures: list[str] = []

    if payload.get("eligible_for_compare") is False:
        failures.append("A/B summary is not eligible_for_compare")
    for flag in payload.get("quality_flags") or []:
        failures.append(f"quality flag: {flag}")
    failures.extend(numeric_gate("measured_seconds", measured_seconds, args.min_seconds, "min"))
    if not args.allow_no_miners and (connected_min is None or connected_min <= 0):
        failures.append("no connected miners for full included A/B window")
    failures.extend(numeric_gate("miner_hours", miner_hours, args.min_miner_hours, "min"))
    failures.extend(numeric_gate("accepted_blocks", accepted, args.min_accepted_submits, "min"))
    failures.extend(
        numeric_gate(
            "accepted_blocks_per_miner_hour",
            accepted_per_hour,
            args.min_accepted_per_miner_hour,
            "min",
        )
    )
    failures.extend(
        numeric_gate(
            "rejected_local_per_accepted",
            rejected_local_per_accepted,
            args.max_local_rejects_per_accepted,
            "max",
        )
    )
    if args.require_chain_confirmation:
        failures.extend(
            numeric_gate(
                "confirmed_paid_blocks",
                confirmed,
                args.min_confirmed_paid_blocks,
                "min",
                missing_message="missing confirmed paid-chain evidence",
            )
        )

    return {
        "path": str(path),
        "kind": "miner_normalized_ab_summary",
        "gate_passed": not failures,
        "failures": failures,
        "metrics": {
            "measured_seconds": measured_seconds,
            "miner_hours": miner_hours,
            "connected_miners_min": connected_min,
            "accepted_blocks": accepted,
            "accepted_blocks_per_miner_hour": accepted_per_hour,
            "confirmed_paid_blocks": confirmed,
            "rejected_local_per_accepted": rejected_local_per_accepted,
            "quality_flags": payload.get("quality_flags") or [],
        },
    }


def numeric_gate(
    label: str,
    value: float | None,
    threshold: float,
    direction: str,
    *,
    missing_message: str | None = None,
) -> list[str]:
    if value is None:
        return [missing_message or f"{label} is missing"]
    if direction == "min" and value < threshold:
        return [f"{label} {value:g} < required {threshold:g}"]
    if direction == "max" and value > threshold:
        return [f"{label} {value:g} > allowed {threshold:g}"]
    return []


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return payload


def infer_kind(payload: dict[str, Any], explicit_kind: str) -> str:
    if explicit_kind != "auto":
        return explicit_kind
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    if metadata.get("document_type") == "paid_block_conversion_baseline":
        return "baseline"
    if "accepted_blocks_per_miner_hour" in payload or "eligible_for_compare" in payload:
        return "ab-summary"
    if "paid_mining_state" in payload and "summary" in payload:
        return "baseline"
    return "unknown"


def evaluate_evidence(paths: list[Path], args: argparse.Namespace) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    failures: list[str] = []
    for path in paths:
        try:
            payload = load_json(path)
        except Exception as exc:  # noqa: BLE001 - gate should report all input failures.
            records.append({"path": str(path), "kind": "unreadable", "gate_passed": False, "failures": [str(exc)]})
            failures.append(f"{path}: {exc}")
            continue
        kind = infer_kind(payload, args.kind)
        if kind == "baseline":
            record = baseline_evidence(path, payload, args)
        elif kind == "ab-summary":
            record = ab_summary_evidence(path, payload, args)
        else:
            record = {"path": str(path), "kind": "unknown", "gate_passed": False, "failures": ["unknown evidence kind"]}
        records.append(record)
        failures.extend(f"{path}: {item}" for item in record.get("failures", []))

    if not records:
        failures.append("no evidence files supplied")
    return {
        "document_type": "paid_conversion_release_gate",
        "generated_at": now_iso(),
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "gate_passed": not failures,
        "failures": failures,
        "criteria": {
            "min_seconds": args.min_seconds,
            "min_miner_hours": args.min_miner_hours,
            "min_accepted_submits": args.min_accepted_submits,
            "min_accepted_per_miner_hour": args.min_accepted_per_miner_hour,
            "require_chain_confirmation": args.require_chain_confirmation,
            "min_confirmed_paid_blocks": args.min_confirmed_paid_blocks,
            "max_local_drop_ratio": args.max_local_drop_ratio,
            "max_share_reject_ratio": args.max_share_reject_ratio,
            "max_local_rejects_per_accepted": args.max_local_rejects_per_accepted,
            "allow_dirty_repos": args.allow_dirty_repos,
            "allow_no_miners": args.allow_no_miners,
        },
        "evidence": records,
    }


def render_html(payload: dict[str, Any]) -> str:
    status = "PASS" if payload.get("gate_passed") else "FAIL"
    rows = "\n".join(
        "<tr>"
        f"<td>{html_escape(item.get('path'))}</td>"
        f"<td>{html_escape(item.get('kind'))}</td>"
        f"<td>{html_escape('pass' if item.get('gate_passed') else 'fail')}</td>"
        f"<td>{html_escape('; '.join(item.get('failures') or []))}</td>"
        "</tr>"
        for item in payload.get("evidence") or []
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Paid Conversion Release Gate - {status}</title>
  <style>
    body {{ margin:0; background:#0d1117; color:#e6edf3; font:14px/1.55 system-ui,sans-serif; }}
    main {{ max-width:1180px; margin:0 auto; padding:28px 20px 56px; }}
    table {{ width:100%; border-collapse:collapse; background:#161b22; }}
    th,td {{ border:1px solid #30363d; padding:9px 10px; text-align:left; vertical-align:top; }}
    th {{ background:#1f2937; }}
    code,pre {{ background:#090d13; border:1px solid #30363d; color:#d8f7ff; border-radius:6px; }}
    code {{ padding:1px 5px; }}
    pre {{ padding:12px; overflow:auto; }}
    .pass {{ color:#56d364; }} .fail {{ color:#ff7b72; }}
  </style>
  <script type="application/json" id="agent-metadata">{json.dumps(payload, sort_keys=True)}</script>
</head>
<body>
<main>
  <h1>Paid Conversion Release Gate: <span class="{html_escape(status.lower())}">{html_escape(status)}</span></h1>
  <p>This gate rejects release promotion when paid mining is not proven by accepted submits and confirmed chain-paid evidence.</p>
  <table><thead><tr><th>Evidence</th><th>Kind</th><th>Gate</th><th>Failures</th></tr></thead><tbody>{rows}</tbody></table>
  <h2>Full Payload</h2>
  <pre>{html_escape(json.dumps(payload, indent=2, sort_keys=True, default=str))}</pre>
</main>
</body>
</html>
"""


def write_reports(payload: dict[str, Any], output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = output_dir / f"paid-conversion-release-gate-{stamp}.json"
    html_path = output_dir / f"paid-conversion-release-gate-{stamp}.html"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    html_path.write_text(render_html(payload), encoding="utf-8")
    return {"json": str(json_path), "html": str(html_path)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", nargs="*", type=Path, help="baseline JSON or A/B summary JSON")
    parser.add_argument("--kind", choices=["auto", "baseline", "ab-summary"], default="auto")
    parser.add_argument("--min-seconds", type=float, default=DEFAULT_MIN_SECONDS)
    parser.add_argument("--min-miner-hours", type=float, default=DEFAULT_MIN_MINER_HOURS)
    parser.add_argument("--min-accepted-submits", type=float, default=DEFAULT_MIN_ACCEPTED_SUBMITS)
    parser.add_argument("--min-accepted-per-miner-hour", type=float, default=DEFAULT_MIN_ACCEPTED_PER_MINER_HOUR)
    parser.add_argument("--max-local-drop-ratio", type=float, default=DEFAULT_MAX_LOCAL_DROP_RATIO)
    parser.add_argument("--max-share-reject-ratio", type=float, default=DEFAULT_MAX_SHARE_REJECT_RATIO)
    parser.add_argument("--max-local-rejects-per-accepted", type=float, default=DEFAULT_MAX_LOCAL_REJECTS_PER_ACCEPTED)
    parser.add_argument("--min-confirmed-paid-blocks", type=float, default=DEFAULT_MIN_CONFIRMED_PAID_BLOCKS)
    parser.add_argument("--allow-missing-chain-confirmation", dest="require_chain_confirmation", action="store_false")
    parser.add_argument("--allow-dirty-repos", action="store_true")
    parser.add_argument("--allow-no-miners", action="store_true")
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=REPORT_DIR)
    parser.set_defaults(require_chain_confirmation=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = evaluate_evidence(args.evidence, args)
    if args.write_report:
        payload["reports"] = write_reports(payload, args.output_dir)
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0 if payload["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

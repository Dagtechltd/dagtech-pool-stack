#!/usr/bin/env python3
"""Prepare or publish redacted BlockDAG incident reports.

The stack should always preserve local incidents. Public GitHub reporting is
opt-in because operators must choose the destination repo and provide auth.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from incident_journal import read_recent_incidents
from pool_ops import RUNTIME_DIR, ensure_runtime, now_iso


REPORT_DIR = Path(os.environ.get("BDAG_INCIDENT_REPORT_DIR", RUNTIME_DIR / "incident-reports"))
STATE_FILE = Path(os.environ.get("BDAG_INCIDENT_REPORT_STATE", RUNTIME_DIR / "incident-reporter-state.json"))
REPORT_REPO = os.environ.get("BDAG_INCIDENT_REPORT_REPO", "").strip()
REPORT_ENABLED = os.environ.get("BDAG_INCIDENT_REPORT_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
MIN_SEVERITY = os.environ.get("BDAG_INCIDENT_REPORT_MIN_SEVERITY", "critical").lower()
MAX_REPORTS_PER_RUN = int(os.environ.get("BDAG_INCIDENT_REPORT_MAX_PER_RUN", "3"))
GH_BIN = os.environ.get("BDAG_GH_BIN", "gh")

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2, "fatal": 3}
HEX_SECRET_RE = re.compile(r"(?i)(0x)?[0-9a-f]{48,}")


def severity_rank(value: str) -> int:
    return SEVERITY_ORDER.get(str(value or "").lower(), 0)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): redact(v) for k, v in value.items() if "private" not in str(k).lower()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return HEX_SECRET_RE.sub("[redacted-hex]", value)
    return value


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"reported_ids": []}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"reported_ids": []}
    if not isinstance(data, dict):
        return {"reported_ids": []}
    data.setdefault("reported_ids", [])
    return data


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def report_body(incident: dict[str, Any]) -> str:
    clean = redact(incident)
    details = json.dumps(clean, indent=2, sort_keys=True, default=str)
    return "\n".join(
        [
            "## Automated BlockDAG Incident Report",
            "",
            f"- Generated: {now_iso()}",
            f"- Incident ID: `{clean.get('id')}`",
            f"- Severity: `{clean.get('severity')}`",
            f"- Component: `{clean.get('component')}`",
            f"- Event type: `{clean.get('event_type')}`",
            "",
            "### Message",
            "",
            str(clean.get("message") or ""),
            "",
            "### Redacted Payload",
            "",
            "```json",
            details,
            "```",
        ]
    )


def write_local_report(incident: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    incident_id = str(incident.get("id") or "unknown").replace("/", "-")
    path = REPORT_DIR / f"incident-{incident_id}.md"
    path.write_text(report_body(incident), encoding="utf-8")
    return path


def publish_issue(incident: dict[str, Any], body_path: Path) -> bool:
    if not REPORT_ENABLED or not REPORT_REPO:
        return False
    title = (
        f"[auto-incident] {incident.get('severity', 'unknown')} "
        f"{incident.get('component', 'stack')}: {incident.get('event_type', 'event')}"
    )
    result = subprocess.run(
        [
            GH_BIN,
            "-R",
            REPORT_REPO,
            "issue",
            "create",
            "--title",
            title[:240],
            "--body-file",
            str(body_path),
            "--label",
            "incident",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=45,
        check=False,
    )
    return result.returncode == 0


def main() -> int:
    ensure_runtime()
    state = load_state()
    reported = {str(item) for item in state.get("reported_ids", [])}
    incidents = read_recent_incidents(200)
    selected = [
        incident
        for incident in incidents
        if str(incident.get("id")) not in reported
        and severity_rank(str(incident.get("severity") or "")) >= severity_rank(MIN_SEVERITY)
    ][:MAX_REPORTS_PER_RUN]
    outputs: list[dict[str, Any]] = []
    for incident in selected:
        body_path = write_local_report(incident)
        published = publish_issue(incident, body_path)
        reported.add(str(incident.get("id")))
        outputs.append({"id": incident.get("id"), "path": str(body_path), "published": published})
    state["reported_ids"] = sorted(reported)[-1000:]
    state["last_run_at"] = now_iso()
    state["last_outputs"] = outputs
    save_state(state)
    print(json.dumps({"generated_at": now_iso(), "outputs": outputs}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

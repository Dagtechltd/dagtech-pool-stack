#!/usr/bin/env python3
"""Create a deterministic provenance manifest for a pool-stack release."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA_FILE = ROOT / "sql" / "pool-schema.sql"
SENSITIVE_KEY_PARTS = ("PASS", "PASSWORD", "SECRET", "TOKEN", "KEY", "PRIVATE", "SEED")


def run(cmd: list[str], cwd: Path = ROOT, timeout: float = 10.0) -> tuple[bool, str]:
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    return proc.returncode == 0, proc.stdout.strip()


def sha256_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        values[key] = "[redacted]" if any(part in key.upper() for part in SENSITIVE_KEY_PARTS) else value
    return values


def git_info(path: Path) -> dict[str, Any]:
    ok_head, head = run(["git", "rev-parse", "HEAD"], cwd=path)
    ok_branch, branch = run(["git", "branch", "--show-current"], cwd=path)
    ok_remote, remote = run(["git", "config", "--get", "remote.origin.url"], cwd=path)
    ok_status, status = run(["git", "status", "--porcelain"], cwd=path)
    return {
        "path": str(path),
        "head": head if ok_head else "",
        "branch": branch if ok_branch else "",
        "remote": remote if ok_remote else "",
        "dirty": bool(status) if ok_status else None,
        "status": status.splitlines() if status else [],
    }


def image_info(images: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for image in images:
        ok, raw = run(["docker", "image", "inspect", image], timeout=20)
        if not ok:
            rows.append({"image": image, "available": False, "error": raw[-500:]})
            continue
        try:
            decoded = json.loads(raw)[0]
        except (json.JSONDecodeError, IndexError, TypeError) as exc:
            rows.append({"image": image, "available": False, "error": str(exc)})
            continue
        rows.append(
            {
                "image": image,
                "available": True,
                "id": decoded.get("Id"),
                "repo_digests": decoded.get("RepoDigests") or [],
                "created": decoded.get("Created"),
            }
        )
    return rows


def render_html(manifest: dict[str, Any]) -> str:
    rows = "\n".join(
        f"<tr><th>{html.escape(str(key))}</th><td><pre>{html.escape(json.dumps(value, indent=2, sort_keys=True))}</pre></td></tr>"
        for key, value in manifest.items()
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BlockDAG Release Provenance</title>
  <style>
    body {{ margin:0; background:#0d1117; color:#eef3f8; font:14px/1.55 system-ui,sans-serif; }}
    main {{ max-width:1120px; margin:0 auto; padding:28px 24px 56px; }}
    h1 {{ margin:0 0 8px; }}
    p {{ color:#a8b3c4; }}
    table {{ width:100%; border-collapse:collapse; border:1px solid #303b4d; background:#151b24; }}
    th, td {{ border-bottom:1px solid #303b4d; padding:10px; text-align:left; vertical-align:top; }}
    th {{ width:220px; background:#1d2633; }}
    pre {{ margin:0; white-space:pre-wrap; word-break:break-word; color:#d7f5ff; }}
  </style>
  <script type="application/json" id="agent-metadata">{html.escape(json.dumps(manifest, sort_keys=True))}</script>
</head>
<body><main>
  <h1>BlockDAG Release Provenance</h1>
  <p>Deterministic release evidence: source refs, schema hash, redacted feature flags, optional image IDs, and snapshot checksums.</p>
  <table>{rows}</table>
</main></body></html>
"""


def build_manifest(args: argparse.Namespace) -> dict[str, Any]:
    env_file = Path(args.env_file) if args.env_file else ROOT / ".env"
    schema_file = Path(args.schema_file) if args.schema_file else DEFAULT_SCHEMA_FILE
    snapshot_files = [Path(item) for item in args.snapshot]
    return {
        "generated_at_unix": int(time.time()),
        "root": str(ROOT),
        "git": git_info(ROOT),
        "schema": {
            "path": str(schema_file),
            "sha256": sha256_file(schema_file),
        },
        "env": {
            "path": str(env_file),
            "values": load_env_file(env_file),
        },
        "images": image_info(args.image),
        "snapshots": [
            {
                "path": str(path),
                "exists": path.exists(),
                "size_bytes": path.stat().st_size if path.exists() else 0,
                "sha256": sha256_file(path),
            }
            for path in snapshot_files
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--schema-file", default=None)
    parser.add_argument("--image", action="append", default=[])
    parser.add_argument("--snapshot", action="append", default=[])
    parser.add_argument("--output-json", default="release-provenance.json")
    parser.add_argument("--output-html", default="release-provenance.html")
    args = parser.parse_args()

    manifest = build_manifest(args)
    json_path = Path(args.output_json)
    html_path = Path(args.output_html)
    json_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    html_path.write_text(render_html(manifest), encoding="utf-8")
    print(json_path)
    print(html_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

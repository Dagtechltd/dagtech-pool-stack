#!/usr/bin/env python3
"""Probe a DGX-hosted OpenAI-compatible local model endpoint."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILES = [
    PROJECT_ROOT / "ops" / "runtime" / "dgx-llm.env",
    Path.home() / ".codex" / "dgx-llm.env",
]


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_config() -> dict[str, str]:
    config: dict[str, str] = {}
    for path in ENV_FILES:
        config.update(read_env_file(path))
    for key in ("DGX_LLM_BASE_URL", "DGX_LLM_MODEL", "DGX_LLM_API_KEY"):
        if os.environ.get(key):
            config[key] = os.environ[key]
    return config


def request_json(url: str, api_key: str | None = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"accept": "application/json", "user-agent": "BlockDAGCodexDGXProbe/1.0"}
    if payload is not None:
        headers["content-type"] = "application/json"
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read(2_000_000).decode("utf-8", "replace"))


def main() -> int:
    config = load_config()
    base_url = (config.get("DGX_LLM_BASE_URL") or "").rstrip("/")
    model = config.get("DGX_LLM_MODEL") or ""
    api_key = config.get("DGX_LLM_API_KEY")
    if not base_url or not model:
        print("DGX_LLM_BASE_URL and DGX_LLM_MODEL are required.")
        print("Create ops/runtime/dgx-llm.env or ~/.codex/dgx-llm.env with:")
        print("DGX_LLM_BASE_URL=http://<dgx-lan-ip>:8000/v1")
        print("DGX_LLM_MODEL=<served-model-name>")
        print("DGX_LLM_API_KEY=<shared-secret>")
        return 2

    try:
        models = request_json(f"{base_url}/models", api_key)
        model_ids = [item.get("id") for item in models.get("data", []) if item.get("id")]
        print(f"models_ok=true count={len(model_ids)}")
        if model_ids:
            print("models=" + ", ".join(model_ids[:10]))

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with exactly: dgx-ok"}],
            "temperature": 0,
            "max_tokens": 16,
        }
        completion = request_json(f"{base_url}/chat/completions", api_key, payload)
        content = completion.get("choices", [{}])[0].get("message", {}).get("content", "")
        print(f"chat_ok=true reply={content.strip()!r}")
        return 0
    except urllib.error.URLError as exc:
        print(f"dgx_probe_failed={exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - diagnostics should report any endpoint shape mismatch.
        print(f"dgx_probe_failed={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

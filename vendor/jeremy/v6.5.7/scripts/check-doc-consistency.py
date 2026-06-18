#!/usr/bin/env python3
"""Check duplicated release documentation for drift."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
RELEASE_DOWNLOADS = ROOT / "release-downloads" / "index.html"
INSTALL_COMMAND = "docker compose build && docker compose up -d --no-build --pull never postgres node dashboard"
INSTALL_COMMAND_HTML = INSTALL_COMMAND.replace("&", "&amp;")
STALE_INSTALL_RE = re.compile(
    r"docker compose build (?:&&|&amp;&amp;) docker compose up -d --no-build --pull never(?! postgres node dashboard)"
)


def fail(message: str) -> None:
    raise SystemExit(f"doc consistency check failed: {message}")


def main() -> int:
    readme = README.read_text(encoding="utf-8")
    release_html = RELEASE_DOWNLOADS.read_text(encoding="utf-8")

    if INSTALL_COMMAND not in readme:
        fail(f"{README} does not mention {INSTALL_COMMAND!r}")
    if INSTALL_COMMAND_HTML not in release_html:
        fail(f"{RELEASE_DOWNLOADS} does not mention {INSTALL_COMMAND_HTML!r}")

    for path, text in ((README, readme), (RELEASE_DOWNLOADS, release_html)):
        stale = STALE_INSTALL_RE.search(text)
        if stale:
            fail(f"{path} still contains stale install command at byte {stale.start()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

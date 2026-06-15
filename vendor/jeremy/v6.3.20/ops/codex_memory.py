#!/usr/bin/env python3
"""Codex context memory store with provenance and compressed payloads.

This service tails the local Codex history file, captures selected markdown
notes, and stores the raw context in SQLite with searchable summaries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


HOME = Path.home()
DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(os.environ.get("CODEX_PROJECT_ROOT", DEFAULT_PROJECT_ROOT)).expanduser().resolve()

DB_PATH = Path(
    os.environ.get(
        "CODEX_MEMORY_DB",
        HOME / ".codex" / "memories" / "context-store" / "context.sqlite",
    )
).expanduser().resolve()
HISTORY_FILE = Path(
    os.environ.get(
        "CODEX_HISTORY_FILE",
        HOME / ".codex" / "history.jsonl",
    )
).expanduser().resolve()
SNAPSHOT_DIR = Path(
    os.environ.get(
        "CODEX_MEMORY_SNAPSHOT_DIR",
        HOME / ".codex" / "memories" / "snapshots",
    )
).expanduser().resolve()
WATCH_INTERVAL_SECONDS = max(2, int(os.environ.get("CODEX_MEMORY_INTERVAL", "10")))
WATCH_ROOTS = [
    Path(item).expanduser().resolve()
    for item in re.split(r"[,;]", os.environ.get(
        "CODEX_MEMORY_WATCH_DIRS",
        f"{HOME / '.codex' / 'memories'},{PROJECT_ROOT / 'ops' / 'runtime'}",
    ))
    if item.strip()
]
MAX_SUMMARY = 320
MAX_TITLE = 120

SOURCE_HISTORY = "history"
SOURCE_DOC = "doc"
SOURCE_NOTE = "note"


@dataclass
class SourceState:
    path: str
    kind: str
    cursor: str | None
    sha256: str | None
    mtime: float | None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def connect() -> sqlite3.Connection:
    ensure_parent(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sources (
            path TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            cursor TEXT,
            sha256 TEXT,
            mtime REAL,
            last_seen_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            turn_count INTEGER NOT NULL DEFAULT 0,
            summary TEXT NOT NULL DEFAULT '',
            context_blob BLOB,
            provenance_json TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            session_id TEXT PRIMARY KEY,
            snapshot_path TEXT NOT NULL,
            snapshot_sha256 TEXT NOT NULL,
            entry_count INTEGER NOT NULL,
            summary TEXT NOT NULL,
            snapshot_blob BLOB NOT NULL,
            provenance_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            source_kind TEXT NOT NULL,
            source_path TEXT NOT NULL,
            source_line INTEGER,
            source_offset INTEGER,
            turn_ts INTEGER,
            turn_iso TEXT,
            turn_role TEXT NOT NULL DEFAULT 'unknown',
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            raw_blob BLOB NOT NULL,
            raw_sha256 TEXT NOT NULL,
            provenance_json TEXT NOT NULL,
            refs_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_entries_session_id ON entries(session_id);
        CREATE INDEX IF NOT EXISTS idx_entries_source_path ON entries(source_path);
        CREATE INDEX IF NOT EXISTS idx_entries_turn_ts ON entries(turn_ts);

        CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
            title,
            summary,
            provenance,
            refs,
            session_id,
            content='entries',
            content_rowid='id'
        );

        CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
            INSERT INTO entries_fts(rowid, title, summary, provenance, refs, session_id)
            VALUES (new.id, new.title, new.summary, new.provenance_json, new.refs_json, new.session_id);
        END;

        CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
            INSERT INTO entries_fts(entries_fts, rowid, title, summary, provenance, refs, session_id)
            VALUES ('delete', old.id, old.title, old.summary, old.provenance_json, old.refs_json, old.session_id);
        END;

        CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE ON entries BEGIN
            INSERT INTO entries_fts(entries_fts, rowid, title, summary, provenance, refs, session_id)
            VALUES ('delete', old.id, old.title, old.summary, old.provenance_json, old.refs_json, old.session_id);
            INSERT INTO entries_fts(rowid, title, summary, provenance, refs, session_id)
            VALUES (new.id, new.title, new.summary, new.provenance_json, new.refs_json, new.session_id);
        END;
        """
    )
    conn.commit()


def compress_payload(value: Any) -> bytes:
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return zlib.compress(raw, level=6)


def decompress_payload(blob: bytes | None) -> Any:
    if not blob:
        return None
    return json.loads(zlib.decompress(blob).decode("utf-8"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def sanitize_one_line(text: str, limit: int = MAX_SUMMARY) -> str:
    value = re.sub(r"\s+", " ", text.strip())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def detect_turn_role(text: str) -> str:
    lowered = text.lower()
    if lowered.startswith("user:") or lowered.startswith("i "):
        return "user"
    if lowered.startswith("assistant:"):
        return "assistant"
    if lowered.startswith("tool:") or lowered.startswith("function:"):
        return "tool"
    if text.startswith("{") or text.startswith("["):
        return "json"
    return "note"


def extract_refs(text: str) -> list[str]:
    refs: list[str] = []
    patterns = [
        r"https?://[^\s\]\)\"']+",
        r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
        r"(?:~|/)[A-Za-z0-9_./:-]+",
        r"\b[A-Za-z]:\\[^\s]+",
        r"\b(?:make|python3|python|docker|systemctl|journalctl|curl|gh|git|npm|yarn|cargo|go)\b[^\n]*",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, text, flags=re.IGNORECASE | re.MULTILINE):
            candidate = match.strip().rstrip(".,;")
            if candidate and candidate not in refs:
                refs.append(candidate)
    return refs[:12]


def summarize_text(text: str, refs: list[str]) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    lead = sanitize_one_line(lines[0], limit=220)
    if len(lines) > 1:
        tail = sanitize_one_line(" ".join(lines[1:3]), limit=120)
        if tail and tail != lead:
            lead = f"{lead} {tail}"
    if refs:
        ref_text = ", ".join(refs[:4])
        lead = f"{lead} | refs: {ref_text}"
    return sanitize_one_line(lead, limit=MAX_SUMMARY)


def title_from_text(text: str, source_path: Path) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
        return sanitize_one_line(stripped, limit=MAX_TITLE)
    return source_path.stem[:MAX_TITLE]


def read_source_state(conn: sqlite3.Connection, path: Path) -> SourceState | None:
    row = conn.execute("SELECT path, kind, cursor, sha256, mtime FROM sources WHERE path = ?", (str(path),)).fetchone()
    if not row:
        return None
    return SourceState(path=row["path"], kind=row["kind"], cursor=row["cursor"], sha256=row["sha256"], mtime=row["mtime"])


def write_source_state(
    conn: sqlite3.Connection,
    path: Path,
    kind: str,
    cursor: str | None = None,
    sha256: str | None = None,
    mtime: float | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO sources(path, kind, cursor, sha256, mtime, last_seen_at)
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            kind=excluded.kind,
            cursor=excluded.cursor,
            sha256=excluded.sha256,
            mtime=excluded.mtime,
            last_seen_at=excluded.last_seen_at
        """,
        (str(path), kind, cursor, sha256, mtime, utc_now()),
    )


def upsert_session(
    conn: sqlite3.Connection,
    session_id: str,
    timestamp: str,
    summary_piece: str,
    provenance: dict[str, Any],
) -> None:
    row = conn.execute(
        "SELECT turn_count, summary, context_blob, provenance_json, first_seen_at FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        context = [summary_piece] if summary_piece else []
        conn.execute(
            """
            INSERT INTO sessions(session_id, first_seen_at, last_seen_at, turn_count, summary, context_blob, provenance_json, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                timestamp,
                timestamp,
                1,
                summary_piece,
                compress_payload(context),
                json.dumps([provenance], ensure_ascii=False),
                utc_now(),
            ),
        )
        return

    current_context = decompress_payload(row["context_blob"]) or []
    if summary_piece:
        current_context.append(summary_piece)
    current_context = current_context[-40:]
    summary = " | ".join(item for item in current_context[-8:] if item)
    provenance_list = json.loads(row["provenance_json"] or "[]")
    provenance_list.append(provenance)
    provenance_list = provenance_list[-40:]
    conn.execute(
        """
        UPDATE sessions
        SET last_seen_at = ?,
            turn_count = ?,
            summary = ?,
            context_blob = ?,
            provenance_json = ?,
            updated_at = ?
        WHERE session_id = ?
        """,
        (
            timestamp,
            int(row["turn_count"]) + 1,
            summary,
            compress_payload(current_context),
            json.dumps(provenance_list, ensure_ascii=False),
            utc_now(),
            session_id,
        ),
    )


def render_session_snapshot(session: sqlite3.Row, entries: list[sqlite3.Row]) -> str:
    lines: list[str] = [
        "# Codex Handoff Snapshot",
        "",
        f"- Session: {session['session_id']}",
        f"- First seen: {session['first_seen_at']}",
        f"- Last seen: {session['last_seen_at']}",
        f"- Turn count: {session['turn_count']}",
        f"- Entry count: {len(entries)}",
        f"- Summary: {session['summary'] or 'n/a'}",
        "",
        "## Provenance",
        "",
        "```json",
        session["provenance_json"] or "[]",
        "```",
        "",
        "## Recent Turns",
        "",
    ]
    for row in entries[-12:]:
        lines.append(f"- [{row['turn_iso'] or row['created_at']}] {row['title']}")
        if row["summary"]:
            lines.append(f"  - {row['summary']}")
        if row["provenance_json"]:
            lines.append(f"  - provenance: {row['provenance_json']}")
    lines.extend(
        [
            "",
            "## Retrieval",
            "",
            f"- Search this session: `python3 {PROJECT_ROOT / 'ops' / 'codex_memory.py'} session {session['session_id']}`",
            f"- Search terms: `python3 {PROJECT_ROOT / 'ops' / 'codex_memory.py'} search \"<topic>\"`",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def write_session_snapshot(conn: sqlite3.Connection, session_id: str) -> Path | None:
    session = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    if not session:
        return None
    entries = session_entries(conn, session_id)
    if not entries:
        return None

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_text = render_session_snapshot(session, entries)
    snapshot_path = SNAPSHOT_DIR / f"{session_id}.md"
    snapshot_path.write_text(snapshot_text, encoding="utf-8")
    snapshot_path.chmod(0o600)

    payload = compress_payload(snapshot_text)
    provenance = {
        "source": "session-snapshot",
        "path": str(snapshot_path),
        "session_id": session_id,
        "entry_ids": [row["id"] for row in entries[-12:]],
    }
    digest = sha256_text(snapshot_text)
    conn.execute(
        """
        INSERT INTO snapshots(
            session_id, snapshot_path, snapshot_sha256, entry_count, summary,
            snapshot_blob, provenance_json, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            snapshot_path=excluded.snapshot_path,
            snapshot_sha256=excluded.snapshot_sha256,
            entry_count=excluded.entry_count,
            summary=excluded.summary,
            snapshot_blob=excluded.snapshot_blob,
            provenance_json=excluded.provenance_json,
            updated_at=excluded.updated_at
        """,
        (
            session_id,
            str(snapshot_path),
            digest,
            len(entries),
            session["summary"] or "",
            payload,
            json.dumps(provenance, ensure_ascii=False),
            session["first_seen_at"],
            utc_now(),
        ),
    )
    return snapshot_path


def ensure_missing_snapshots(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """
        SELECT sessions.session_id
        FROM sessions
        LEFT JOIN snapshots ON snapshots.session_id = sessions.session_id
        WHERE snapshots.session_id IS NULL
        ORDER BY sessions.last_seen_at ASC
        """
    ).fetchall()
    created = 0
    for row in rows:
        if write_session_snapshot(conn, row["session_id"]):
            created += 1
    return created


def insert_entry(
    conn: sqlite3.Connection,
    *,
    session_id: str | None,
    source_kind: str,
    source_path: Path,
    source_line: int | None,
    source_offset: int | None,
    turn_ts: int | None,
    turn_iso: str | None,
    turn_role: str,
    title: str,
    summary: str,
    raw_payload: Any,
    provenance: dict[str, Any],
    refs: list[str],
) -> None:
    raw_json = json.dumps(raw_payload, ensure_ascii=False, separators=(",", ":"))
    conn.execute(
        """
        INSERT INTO entries(
            session_id, source_kind, source_path, source_line, source_offset,
            turn_ts, turn_iso, turn_role, title, summary, raw_blob,
            raw_sha256, provenance_json, refs_json, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            source_kind,
            str(source_path),
            source_line,
            source_offset,
            turn_ts,
            turn_iso,
            turn_role,
            title,
            summary,
            zlib.compress(raw_json.encode("utf-8"), level=6),
            sha256_text(raw_json),
            json.dumps(provenance, ensure_ascii=False),
            json.dumps(refs, ensure_ascii=False),
            utc_now(),
        ),
    )


def ingest_history_file(conn: sqlite3.Connection, history_file: Path = HISTORY_FILE) -> int:
    if not history_file.exists():
        return 0
    state = read_source_state(conn, history_file)
    offset = 0
    if state and state.cursor:
        try:
            offset = int(state.cursor)
        except ValueError:
            offset = 0
    total_size = history_file.stat().st_size
    if offset > total_size:
        offset = 0

    ingested = 0
    touched_sessions: set[str] = set()
    line_no = 0
    with history_file.open("rb") as handle:
        if offset:
            handle.seek(offset)
            with history_file.open("rb") as counter:
                while counter.tell() < offset:
                    if not counter.readline():
                        break
                    line_no += 1
        while True:
            start_offset = handle.tell()
            raw = handle.readline()
            if not raw:
                break
            line_no += 1
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = {"text": text}
            session_id = str(payload.get("session_id") or "unknown")
            ts = payload.get("ts")
            turn_ts = int(ts) if isinstance(ts, (int, float, str)) and str(ts).isdigit() else None
            turn_iso = (
                datetime.fromtimestamp(turn_ts, tz=timezone.utc).isoformat(timespec="seconds")
                if turn_ts is not None
                else None
            )
            message_text = str(payload.get("text") or payload.get("message") or text)
            refs = extract_refs(message_text)
            summary = summarize_text(message_text, refs)
            title = title_from_text(message_text, history_file)
            provenance = {
                "source": "history.jsonl",
                "path": str(history_file),
                "line": line_no,
                "offset": start_offset,
                "session_id": session_id,
                "ts": ts,
            }
            if turn_iso is None:
                turn_iso = utc_now()
            upsert_session(conn, session_id, turn_iso, summary, provenance)
            insert_entry(
                conn,
                session_id=session_id,
                source_kind=SOURCE_HISTORY,
                source_path=history_file,
                source_line=line_no,
                source_offset=start_offset,
                turn_ts=turn_ts,
                turn_iso=turn_iso,
                turn_role=detect_turn_role(message_text),
                title=title,
                summary=summary,
                raw_payload=payload,
                provenance=provenance,
                refs=refs,
            )
            touched_sessions.add(session_id)
            ingested += 1

    write_source_state(conn, history_file, SOURCE_HISTORY, cursor=str(total_size), sha256=None, mtime=history_file.stat().st_mtime)
    for session_id in touched_sessions:
        write_session_snapshot(conn, session_id)
    conn.commit()
    return ingested


def markdown_title(lines: list[str], source_path: Path) -> str:
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
        return sanitize_one_line(stripped, limit=MAX_TITLE)
    return source_path.stem[:MAX_TITLE]


def ingest_markdown_file(conn: sqlite3.Connection, path: Path) -> int:
    if not path.exists() or not path.is_file():
        return 0
    text = path.read_text(encoding="utf-8", errors="replace")
    digest = sha256_text(text)
    state = read_source_state(conn, path)
    if state and state.sha256 == digest:
        return 0

    lines = [line for line in text.splitlines() if line.strip()]
    refs = extract_refs(text)
    summary = summarize_text(text, refs)
    title = markdown_title(lines, path)
    provenance = {
        "source": "markdown",
        "path": str(path),
        "sha256": digest,
        "mtime": path.stat().st_mtime,
    }
    session_id = f"doc:{digest[:16]}"
    turn_iso = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
    upsert_session(conn, session_id, turn_iso, summary, provenance)
    insert_entry(
        conn,
        session_id=session_id,
        source_kind=SOURCE_DOC,
        source_path=path,
        source_line=1,
        source_offset=0,
        turn_ts=int(path.stat().st_mtime),
        turn_iso=turn_iso,
        turn_role="document",
        title=title,
        summary=summary,
        raw_payload={"path": str(path), "text": text},
        provenance=provenance,
        refs=refs,
    )
    write_source_state(conn, path, SOURCE_DOC, cursor=digest, sha256=digest, mtime=path.stat().st_mtime)
    write_session_snapshot(conn, session_id)
    conn.commit()
    return 1


def collect_markdown_files() -> list[Path]:
    result: list[Path] = []
    for root in WATCH_ROOTS:
        if not root.exists():
            continue
        if root.is_file() and root.suffix.lower() == ".md":
            result.append(root)
            continue
        if root.is_dir():
            for path in root.rglob("*.md"):
                if path.is_file():
                    try:
                        if path.resolve().is_relative_to(SNAPSHOT_DIR):
                            continue
                    except AttributeError:
                        snapshot_root = str(SNAPSHOT_DIR.resolve())
                        if str(path.resolve()).startswith(snapshot_root):
                            continue
                    result.append(path)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in sorted(result):
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def bootstrap(conn: sqlite3.Connection) -> dict[str, int]:
    init_db(conn)
    ingested_history = ingest_history_file(conn)
    ingested_docs = 0
    for path in collect_markdown_files():
        try:
            ingested_docs += ingest_markdown_file(conn, path)
        except OSError:
            continue
    ingested_snapshots = ensure_missing_snapshots(conn)
    conn.commit()
    return {"history": ingested_history, "docs": ingested_docs, "snapshots": ingested_snapshots}


def search(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[sqlite3.Row]:
    init_db(conn)
    query = query.strip()
    if not query:
        return conn.execute(
            "SELECT * FROM entries ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    tokens = re.findall(r"[A-Za-z0-9_./:-]+", query)
    if tokens:
        fts_query = " AND ".join(f'"{token}"' for token in tokens[:12])
        try:
            return conn.execute(
                """
                SELECT entries.*
                FROM entries
                JOIN entries_fts ON entries_fts.rowid = entries.id
                WHERE entries_fts MATCH ?
                ORDER BY bm25(entries_fts)
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            pass
    like = f"%{query}%"
    return conn.execute(
        """
        SELECT *
        FROM entries
        WHERE title LIKE ? OR summary LIKE ? OR provenance_json LIKE ? OR refs_json LIKE ? OR session_id LIKE ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (like, like, like, like, like, limit),
    ).fetchall()


def session_entries(conn: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM entries WHERE session_id = ? ORDER BY id ASC",
        (session_id,),
    ).fetchall()


def render_entry(row: sqlite3.Row) -> str:
    preview = ""
    try:
        payload = decompress_payload(row["raw_blob"])
        if isinstance(payload, dict) and "text" in payload:
            preview = sanitize_one_line(str(payload["text"]), 220)
        elif isinstance(payload, dict) and "payload" in payload:
            preview = sanitize_one_line(json.dumps(payload["payload"], ensure_ascii=False), 220)
        else:
            preview = sanitize_one_line(str(payload), 220)
    except Exception:
        preview = ""
    parts = [
        f"[{row['id']}] {row['session_id']} {row['turn_iso'] or ''}".strip(),
        f"  {row['title']}",
        f"  {row['summary']}",
    ]
    if preview:
        parts.append(f"  preview: {preview}")
    parts.append(f"  provenance: {row['provenance_json']}")
    return "\n".join(parts)


def print_search_results(rows: Iterable[sqlite3.Row]) -> None:
    rows = list(rows)
    if not rows:
        print("no matches")
        return
    for row in rows:
        print(render_entry(row))
        print()


def print_session(conn: sqlite3.Connection, session_id: str) -> None:
    init_db(conn)
    session = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    if not session:
        print("session not found")
        return
    snapshot = conn.execute("SELECT * FROM snapshots WHERE session_id = ?", (session_id,)).fetchone()
    print(f"session: {session['session_id']}")
    print(f"first_seen: {session['first_seen_at']}")
    print(f"last_seen: {session['last_seen_at']}")
    print(f"turn_count: {session['turn_count']}")
    print(f"summary: {session['summary']}")
    print(f"provenance: {session['provenance_json']}")
    if snapshot:
        print(f"snapshot: {snapshot['snapshot_path']}")
    print()
    for row in session_entries(conn, session_id):
        print(render_entry(row))
        print()


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    init_db(conn)
    counts = {
        "entries": conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0],
        "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
        "sources": conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
        "snapshots": conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],
    }
    last = conn.execute("SELECT MAX(created_at) FROM entries").fetchone()[0]
    return {"db_path": str(DB_PATH), "history_file": str(HISTORY_FILE), "last_ingest": last, **counts}


def watch(conn: sqlite3.Connection, interval: int) -> None:
    bootstrap(conn)
    while True:
        try:
            bootstrap(conn)
        except Exception as exc:  # pragma: no cover - keep the service alive
            print(f"[codex-memory] ingest error: {exc}", file=sys.stderr)
        time.sleep(interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex context memory store")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("bootstrap", help="ingest history and markdown sources once")
    sub.add_parser("once", help="alias for bootstrap")

    watch_parser = sub.add_parser("watch", help="ingest continuously")
    watch_parser.add_argument("--interval", type=int, default=WATCH_INTERVAL_SECONDS)

    search_parser = sub.add_parser("search", help="search memory entries")
    search_parser.add_argument("query", nargs="?", default="")
    search_parser.add_argument("--limit", type=int, default=10)

    session_parser = sub.add_parser("session", help="show a session by id")
    session_parser.add_argument("session_id")

    sub.add_parser("status", help="show store status")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    conn = connect()
    init_db(conn)

    if args.command in {"bootstrap", "once"}:
        result = bootstrap(conn)
        print(json.dumps({"status": "ok", **result, **status(conn)}, indent=2))
        return 0
    if args.command == "watch":
        watch(conn, args.interval)
        return 0
    if args.command == "search":
        rows = search(conn, args.query, args.limit)
        print_search_results(rows)
        return 0
    if args.command == "session":
        print_session(conn, args.session_id)
        return 0
    if args.command == "status":
        print(json.dumps(status(conn), indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

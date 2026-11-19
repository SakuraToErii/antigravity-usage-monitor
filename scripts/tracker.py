#!/usr/bin/env python3
"""Local SQLite ledger & transcript.jsonl ingestion engine for Antigravity App / CLI / IDE.

Supports both local transcripts and SSH-synced remote Linux host transcripts.
Never touches OAuth tokens or makes any Google/Antigravity network calls.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pricing import DATA_DIR, calculate_step_cost

DB_PATH = DATA_DIR / "usage.db"
CONFIG_PATH = DATA_DIR / "config.json"

_MODEL_CHANGE_RE = re.compile(
    r"The user changed setting `Model Selection` from .*? to ([^.\n]+)\.",
    re.IGNORECASE,
)

# Standard local transcript roots for all Antigravity surfaces
DEFAULT_BRAIN_ROOTS = [
    ("app", Path.home() / ".gemini" / "antigravity" / "brain"),
    ("cli", Path.home() / ".gemini" / "antigravity-cli" / "brain"),
    ("ide", Path.home() / ".gemini" / "antigravity-ide" / "brain"),
]


def detect_surface(transcript_path: str) -> str:
    p = str(transcript_path).replace("\\", "/")
    if "/antigravity-cli/" in p:
        return "cli"
    if "/antigravity-ide/" in p:
        return "ide"
    if "/antigravity/" in p:
        return "app"
    return "other"


def extract_conversation_id(transcript_path: str, fallback: str = "") -> str:
    if fallback:
        return fallback
    parts = Path(transcript_path).parts
    if "brain" in parts:
        idx = parts.index("brain")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return Path(transcript_path).parent.name


def parse_iso_utc(ts_str: str) -> Optional[datetime]:
    if not ts_str:
        return None
    try:
        s = ts_str.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def get_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=10000;")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS usage_steps (
            surface TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            host TEXT NOT NULL DEFAULT 'local',
            ts_epoch REAL NOT NULL,
            created_at TEXT NOT NULL,
            raw_model TEXT NOT NULL,
            canonical_model TEXT NOT NULL,
            display_model TEXT NOT NULL,
            pool TEXT NOT NULL,
            input_tokens INTEGER NOT NULL,
            cache_read_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            input_cost REAL NOT NULL,
            cache_read_cost REAL NOT NULL,
            output_cost REAL NOT NULL,
            total_cost REAL NOT NULL,
            cache_savings REAL NOT NULL,
            priced INTEGER NOT NULL,
            PRIMARY KEY (surface, conversation_id, step_index)
        );

        CREATE INDEX IF NOT EXISTS idx_usage_steps_ts ON usage_steps(ts_epoch);
        CREATE INDEX IF NOT EXISTS idx_usage_steps_pool_ts ON usage_steps(pool, ts_epoch);

        CREATE TABLE IF NOT EXISTS file_state (
            file_path TEXT PRIMARY KEY,
            mtime REAL NOT NULL,
            size INTEGER NOT NULL,
            last_model TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS remote_sync_state (
            host TEXT PRIMARY KEY,
            last_attempt_epoch REAL,
            last_success_epoch REAL,
            status TEXT NOT NULL DEFAULT 'idle',
            error TEXT NOT NULL DEFAULT '',
            files_found INTEGER NOT NULL DEFAULT 0,
            files_updated INTEGER NOT NULL DEFAULT 0,
            steps_synced INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    # Seamless migration for existing DBs created before the `host` column existed
    cols = {row[1] for row in conn.execute("PRAGMA table_info(usage_steps)").fetchall()}
    if "host" not in cols:
        conn.execute("ALTER TABLE usage_steps ADD COLUMN host TEXT NOT NULL DEFAULT 'local'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_steps_host_ts ON usage_steps(host, ts_epoch)")
    conn.commit()


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(cfg: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


_UPSERT_SQL = """
INSERT INTO usage_steps (
    surface, conversation_id, step_index, host, ts_epoch, created_at,
    raw_model, canonical_model, display_model, pool,
    input_tokens, cache_read_tokens, output_tokens,
    input_cost, cache_read_cost, output_cost, total_cost, cache_savings, priced
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(surface, conversation_id, step_index) DO UPDATE SET
    host=CASE WHEN usage_steps.host = 'local' THEN 'local' ELSE excluded.host END,
    ts_epoch=excluded.ts_epoch,
    created_at=excluded.created_at,
    raw_model=excluded.raw_model,
    canonical_model=excluded.canonical_model,
    display_model=excluded.display_model,
    pool=excluded.pool,
    input_tokens=excluded.input_tokens,
    cache_read_tokens=excluded.cache_read_tokens,
    output_tokens=excluded.output_tokens,
    input_cost=excluded.input_cost,
    cache_read_cost=excluded.cache_read_cost,
    output_cost=excluded.output_cost,
    total_cost=excluded.total_cost,
    cache_savings=excluded.cache_savings,
    priced=excluded.priced
"""


def _build_step_row(
    surface: str,
    conv_id: str,
    step_index: int,
    host: str,
    created_at: str,
    fallback_mtime: float,
    raw_model: str,
    in_tok: int,
    cache_tok: int,
    out_tok: int,
) -> Tuple[Any, ...]:
    dt = parse_iso_utc(created_at)
    if dt is None:
        dt = datetime.fromtimestamp(fallback_mtime, tz=timezone.utc)
        created_at = dt.isoformat().replace("+00:00", "Z")

    cost = calculate_step_cost(raw_model, in_tok, cache_tok, out_tok, event_dt=dt)
    return (
        surface,
        conv_id,
        int(step_index),
        host,
        dt.timestamp(),
        created_at,
        raw_model,
        cost["canonical_id"],
        cost["display_name"],
        cost["pool"],
        int(in_tok),
        int(cache_tok),
        int(out_tok),
        cost["input_cost"],
        cost["cache_read_cost"],
        cost["output_cost"],
        cost["total_cost"],
        cost["cache_savings"],
        1 if cost["matched"] else 0,
    )


def upsert_remote_file_steps(
    conn: sqlite3.Connection,
    host: str,
    surface: str,
    conv_id: str,
    remote_path: str,
    mtime: float,
    size: int,
    last_model: str,
    steps: Sequence[Sequence[Any]],
) -> int:
    """Upsert extracted steps from a remote SSH host transcript file and record file_state."""
    rows_to_upsert: List[Tuple[Any, ...]] = []
    for item in steps:
        if len(item) < 6:
            continue
        step_index, created_at, raw_model, in_tok, cache_tok, out_tok = item[:6]
        if int(in_tok) == 0 and int(cache_tok) == 0 and int(out_tok) == 0:
            continue
        rows_to_upsert.append(
            _build_step_row(
                surface=surface or "other",
                conv_id=conv_id,
                step_index=int(step_index),
                host=host,
                created_at=str(created_at or ""),
                fallback_mtime=float(mtime),
                raw_model=str(raw_model or "unknown"),
                in_tok=int(in_tok),
                cache_tok=int(cache_tok),
                out_tok=int(out_tok),
            )
        )

    if rows_to_upsert:
        conn.executemany(_UPSERT_SQL, rows_to_upsert)

    file_key = f"ssh://{host}{remote_path}"
    conn.execute(
        """
        INSERT INTO file_state (file_path, mtime, size, last_model)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(file_path) DO UPDATE SET
            mtime=excluded.mtime,
            size=excluded.size,
            last_model=excluded.last_model
        """,
        (file_key, float(mtime), int(size), str(last_model or "")),
    )
    return len(rows_to_upsert)


def ingest_transcript(
    transcript_path: str,
    conversation_id: str = "",
    hint_model: str = "",
    host: str = "local",
    force: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Ingest or update all PLANNER_RESPONSE steps from a single transcript.jsonl file.

    Returns the number of rows upserted.
    """
    path = Path(transcript_path).expanduser()
    if not path.is_file():
        return 0

    stat = path.stat()
    file_key = str(path.resolve())
    surface = detect_surface(file_key)
    conv_id = extract_conversation_id(file_key, conversation_id)

    own_conn = conn is None
    if own_conn:
        conn = get_connection()

    try:
        if not force:
            row = conn.execute(
                "SELECT mtime, size FROM file_state WHERE file_path = ?",
                (file_key,),
            ).fetchone()
            if row and abs(row[0] - stat.st_mtime) < 1e-6 and row[1] == stat.st_size:
                return 0

        current_model = ""
        clean_hint = "" if not hint_model or hint_model.lower() == "auto" else hint_model
        rows_to_upsert: List[Tuple[Any, ...]] = []

        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue

                step_type = entry.get("type", "")
                if step_type == "USER_INPUT":
                    content = entry.get("content", "")
                    if isinstance(content, str) and "Model Selection" in content:
                        matches = _MODEL_CHANGE_RE.findall(content)
                        if matches:
                            current_model = matches[-1].strip()
                    continue

                if step_type != "PLANNER_RESPONSE":
                    continue

                in_tok = int(entry.get("input_tokens") or 0)
                cache_tok = int(entry.get("cache_read_tokens") or 0)
                out_tok = int(entry.get("output_tokens") or 0)
                if in_tok == 0 and cache_tok == 0 and out_tok == 0:
                    continue

                step_index = int(entry.get("step_index") or 0)
                created_at = str(entry.get("created_at") or "")
                step_model = str(entry.get("model") or "").strip()
                raw_model = step_model or current_model or clean_hint or "unknown"
                if step_model:
                    current_model = step_model

                rows_to_upsert.append(
                    _build_step_row(
                        surface=surface,
                        conv_id=conv_id,
                        step_index=step_index,
                        host=host,
                        created_at=created_at,
                        fallback_mtime=stat.st_mtime,
                        raw_model=raw_model,
                        in_tok=in_tok,
                        cache_tok=cache_tok,
                        out_tok=out_tok,
                    )
                )

        if rows_to_upsert:
            conn.executemany(_UPSERT_SQL, rows_to_upsert)

        conn.execute(
            """
            INSERT INTO file_state (file_path, mtime, size, last_model)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(file_path) DO UPDATE SET
                mtime=excluded.mtime,
                size=excluded.size,
                last_model=excluded.last_model
            """,
            (file_key, stat.st_mtime, stat.st_size, current_model),
        )
        conn.commit()
        return len(rows_to_upsert)
    finally:
        if own_conn:
            conn.close()


def reprice_db(conn: Optional[sqlite3.Connection] = None) -> int:
    """Re-calculate costs for ALL rows in usage_steps (local + synced remote) using current pricing."""
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT surface, conversation_id, step_index, ts_epoch, raw_model,
                   input_tokens, cache_read_tokens, output_tokens
            FROM usage_steps
            """
        ).fetchall()
        updates = []
        for surface, conv_id, step_index, ts_epoch, raw_model, in_tok, cache_tok, out_tok in rows:
            dt = datetime.fromtimestamp(float(ts_epoch), tz=timezone.utc)
            cost = calculate_step_cost(raw_model, in_tok, cache_tok, out_tok, event_dt=dt)
            updates.append(
                (
                    cost["canonical_id"],
                    cost["display_name"],
                    cost["pool"],
                    cost["input_cost"],
                    cost["cache_read_cost"],
                    cost["output_cost"],
                    cost["total_cost"],
                    cost["cache_savings"],
                    1 if cost["matched"] else 0,
                    surface,
                    conv_id,
                    step_index,
                )
            )
        if updates:
            conn.executemany(
                """
                UPDATE usage_steps
                SET canonical_model=?, display_model=?, pool=?,
                    input_cost=?, cache_read_cost=?, output_cost=?,
                    total_cost=?, cache_savings=?, priced=?
                WHERE surface=? AND conversation_id=? AND step_index=?
                """,
                updates,
            )
            conn.commit()
        return len(updates)
    finally:
        if own_conn:
            conn.close()


def discover_transcripts() -> List[Path]:
    """Find all local Antigravity transcript.jsonl files across App, CLI, and IDE."""
    found: List[Path] = []
    for _, root in DEFAULT_BRAIN_ROOTS:
        if not root.exists():
            continue
        try:
            for conv_dir in root.iterdir():
                if not conv_dir.is_dir():
                    continue
                for candidate in (
                    conv_dir / ".system_generated" / "logs" / "transcript.jsonl",
                    conv_dir / "transcript.jsonl",
                ):
                    if candidate.is_file():
                        found.append(candidate)
                        break
        except PermissionError:
            continue
    return found


def scan_all_transcripts(force: bool = False) -> Tuple[int, int]:
    """Scan all local App/CLI/IDE transcripts and sync them to SQLite.

    Returns (files_scanned, steps_upserted).
    """
    files = discover_transcripts()
    conn = get_connection()
    total_steps = 0
    try:
        for p in files:
            total_steps += ingest_transcript(str(p), force=force, conn=conn)
    finally:
        conn.close()
    return len(files), total_steps


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sync Antigravity local transcript logs to SQLite")
    parser.add_argument("--scan-all", action="store_true", help="Scan all App/CLI/IDE transcript files")
    parser.add_argument("--force", action="store_true", help="Re-parse unchanged files (recalculates prices)")
    parser.add_argument("--reprice-db", action="store_true", help="Reprice all rows in SQLite (local + remote)")
    parser.add_argument("--file", type=str, default="", help="Ingest a single transcript.jsonl file")
    args = parser.parse_args()

    if args.file:
        n = ingest_transcript(args.file, force=args.force)
        print(f"Ingested {n} steps from {args.file}")
    elif args.reprice_db:
        n = reprice_db()
        print(f"Repriced {n} rows in {DB_PATH}")
    else:
        fcount, scount = scan_all_transcripts(force=args.force)
        print(f"Scanned {fcount} transcript files, synced {scount} steps -> {DB_PATH}")

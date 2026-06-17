"""SQLite persistence layer.

Stores everything in the /config volume so it survives container restarts:
  - settings        (UI-editable overrides of env defaults)
  - ratings_cache   (TMDb / arr rating lookups)
  - scans           (one row per dry run)
  - scan_items      (candidate items found by a scan)
  - action_log      (audit trail of every delete/unmonitor/etc.)
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

_LOCK = threading.Lock()
_DB_PATH: Optional[Path] = None


def init_db(config_dir: str) -> None:
    global _DB_PATH
    cfg = Path(config_dir)
    cfg.mkdir(parents=True, exist_ok=True)
    _DB_PATH = cfg / "mediacleanuparr.db"
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS ratings_cache (
                imdb_id    TEXT PRIMARY KEY,
                rt_score   INTEGER,
                source     TEXT,
                fetched_at REAL,
                raw        TEXT
            );

            CREATE TABLE IF NOT EXISTS scans (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL,
                scope      TEXT,
                threshold  INTEGER,
                status     TEXT,
                summary    TEXT
            );

            CREATE TABLE IF NOT EXISTS scan_items (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id          INTEGER,
                media_type       TEXT,
                arr_id           INTEGER,
                tmdb_id          INTEGER,
                tvdb_id          INTEGER,
                title            TEXT,
                year             INTEGER,
                score            INTEGER,
                rating_source    TEXT,
                path             TEXT,
                size_bytes       INTEGER,
                proposed_action  TEXT,
                prevent_redl     INTEGER,
                reason           TEXT,
                requested_by     TEXT,
                selected         INTEGER DEFAULT 0,
                FOREIGN KEY (scan_id) REFERENCES scans(id)
            );

            CREATE TABLE IF NOT EXISTS action_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          REAL,
                scan_id     INTEGER,
                media_type  TEXT,
                arr_id      INTEGER,
                title       TEXT,
                action      TEXT,
                success     INTEGER,
                detail      TEXT
            );

            CREATE TABLE IF NOT EXISTS exclusions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                media_type  TEXT,
                tmdb_id     INTEGER,
                tvdb_id     INTEGER,
                title       TEXT,
                created_at  REAL
            );
            """
        )
        # Migrate databases created by older versions: CREATE TABLE IF NOT
        # EXISTS won't add new columns to an existing table, so add them here.
        _ensure_columns(conn, "scan_items",
                        {"tmdb_id": "INTEGER", "tvdb_id": "INTEGER", "requested_by": "TEXT"})
        conn.commit()


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    if _DB_PATH is None:
        raise RuntimeError("Database not initialized; call init_db() first.")
    conn = sqlite3.connect(str(_DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ----------------------------- settings -----------------------------------

def get_setting(key: str) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


def all_settings() -> dict[str, str]:
    with _connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}


# --------------------------- ratings cache --------------------------------

def get_cached_rating(imdb_id: str, max_age_seconds: float) -> Optional[dict[str, Any]]:
    if not imdb_id:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM ratings_cache WHERE imdb_id = ?", (imdb_id,)
        ).fetchone()
        if not row:
            return None
        if max_age_seconds and (time.time() - (row["fetched_at"] or 0)) > max_age_seconds:
            return None
        return dict(row)


def put_cached_rating(imdb_id: str, rt_score: Optional[int], source: str, raw: Any) -> None:
    if not imdb_id:
        return
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO ratings_cache(imdb_id, rt_score, source, fetched_at, raw) "
            "VALUES(?, ?, ?, ?, ?) ON CONFLICT(imdb_id) DO UPDATE SET "
            "rt_score=excluded.rt_score, source=excluded.source, "
            "fetched_at=excluded.fetched_at, raw=excluded.raw",
            (imdb_id, rt_score, source, time.time(), json.dumps(raw) if raw is not None else None),
        )
        conn.commit()


# ------------------------------- scans ------------------------------------

def create_scan(scope: str, threshold: int) -> int:
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO scans(created_at, scope, threshold, status, summary) "
            "VALUES(?, ?, ?, ?, ?)",
            (time.time(), scope, threshold, "running", None),
        )
        conn.commit()
        return int(cur.lastrowid)


def finish_scan(scan_id: int, status: str, summary: dict[str, Any]) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "UPDATE scans SET status = ?, summary = ? WHERE id = ?",
            (status, json.dumps(summary), scan_id),
        )
        conn.commit()


def get_scan(scan_id: int) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["summary"] = json.loads(d["summary"]) if d.get("summary") else None
        return d


def latest_scan() -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        d = dict(row)
        d["summary"] = json.loads(d["summary"]) if d.get("summary") else None
        return d


def add_scan_item(scan_id: int, item: dict[str, Any]) -> int:
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            """INSERT INTO scan_items
               (scan_id, media_type, arr_id, tmdb_id, tvdb_id, title, year, score,
                rating_source, path, size_bytes, proposed_action, prevent_redl, reason,
                requested_by, selected)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                scan_id,
                item.get("media_type"),
                item.get("arr_id"),
                item.get("tmdb_id"),
                item.get("tvdb_id"),
                item.get("title"),
                item.get("year"),
                item.get("score"),
                item.get("rating_source"),
                item.get("path"),
                item.get("size_bytes"),
                item.get("proposed_action"),
                1 if item.get("prevent_redl") else 0,
                item.get("reason"),
                item.get("requested_by"),
                1 if item.get("selected") else 0,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_scan_items(scan_id: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM scan_items WHERE scan_id = ? ORDER BY score IS NULL, score ASC, title ASC",
            (scan_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_items_by_ids(item_ids: list[int]) -> list[dict[str, Any]]:
    if not item_ids:
        return []
    placeholders = ",".join("?" for _ in item_ids)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM scan_items WHERE id IN ({placeholders})", item_ids
        ).fetchall()
        return [dict(r) for r in rows]


def set_item_selected(item_id: int, selected: bool) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "UPDATE scan_items SET selected = ? WHERE id = ?",
            (1 if selected else 0, item_id),
        )
        conn.commit()


# ----------------------------- action log ---------------------------------

def log_action(
    scan_id: Optional[int],
    media_type: str,
    arr_id: Optional[int],
    title: str,
    action: str,
    success: bool,
    detail: str,
) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO action_log(ts, scan_id, media_type, arr_id, title, action, success, detail) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (time.time(), scan_id, media_type, arr_id, title, action, 1 if success else 0, detail),
        )
        conn.commit()


def recent_actions(limit: int = 200) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM action_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# --------------------------- exclusions ----------------------------------
def add_exclusion(media_type: str, tmdb_id: Any, tvdb_id: Any, title: str) -> None:
    import time as _t
    with _LOCK, _connect() as conn:
        # Avoid duplicates on the same (media_type, tmdb_id, tvdb_id).
        existing = conn.execute(
            """SELECT 1 FROM exclusions WHERE media_type = ?
               AND IFNULL(tmdb_id,-1) = IFNULL(?,-1)
               AND IFNULL(tvdb_id,-1) = IFNULL(?,-1)""",
            (media_type, tmdb_id, tvdb_id),
        ).fetchone()
        if existing:
            return
        conn.execute(
            "INSERT INTO exclusions (media_type, tmdb_id, tvdb_id, title, created_at) VALUES (?,?,?,?,?)",
            (media_type, tmdb_id, tvdb_id, title, _t.time()),
        )
        conn.commit()


def remove_exclusion(media_type: str, tmdb_id: Any, tvdb_id: Any) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """DELETE FROM exclusions WHERE media_type = ?
               AND IFNULL(tmdb_id,-1) = IFNULL(?,-1)
               AND IFNULL(tvdb_id,-1) = IFNULL(?,-1)""",
            (media_type, tmdb_id, tvdb_id),
        )
        conn.commit()


def remove_exclusion_by_id(excl_id: int) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM exclusions WHERE id = ?", (excl_id,))
        conn.commit()


def list_exclusions() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM exclusions ORDER BY title COLLATE NOCASE ASC").fetchall()
        return [dict(r) for r in rows]


def excluded_keys() -> set[str]:
    """Fast lookup set: 'movie:tmdb:<id>', 'tv:tmdb:<id>', 'tv:tvdb:<id>'."""
    keys: set[str] = set()
    with _connect() as conn:
        for r in conn.execute("SELECT media_type, tmdb_id, tvdb_id FROM exclusions"):
            mt = r["media_type"]
            if r["tmdb_id"] is not None:
                keys.add(f"{mt}:tmdb:{r['tmdb_id']}")
            if r["tvdb_id"] is not None:
                keys.add(f"{mt}:tvdb:{r['tvdb_id']}")
    return keys

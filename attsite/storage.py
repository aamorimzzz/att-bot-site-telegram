"""Persistência em SQLite (um arquivo só, sem servidor)."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       INTEGER NOT NULL,
    url           TEXT    NOT NULL,
    name          TEXT,
    interval      INTEGER NOT NULL,
    selector      TEXT,
    mode          TEXT    NOT NULL DEFAULT 'texto',
    ignore_regex  TEXT,
    keywords      TEXT,
    notify        TEXT    NOT NULL DEFAULT 'tudo',
    user_agent    TEXT,
    paused        INTEGER NOT NULL DEFAULT 0,
    created_at    REAL    NOT NULL,
    next_check    REAL    NOT NULL DEFAULT 0,
    last_check    REAL,
    last_change   REAL,
    last_status   TEXT,
    error_count   INTEGER NOT NULL DEFAULT 0,
    etag          TEXT,
    last_modified TEXT,
    content       TEXT,
    last_diff     TEXT,
    checks        INTEGER NOT NULL DEFAULT 0,
    changes       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_watches_chat ON watches(chat_id);
CREATE INDEX IF NOT EXISTS idx_watches_due ON watches(paused, next_check);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

UPDATABLE = {
    "chat_id", "url", "name", "interval", "selector", "mode", "ignore_regex", "keywords",
    "notify", "user_agent", "paused", "next_check", "last_check", "last_change",
    "last_status", "error_count", "etag", "last_modified", "content", "last_diff",
    "checks", "changes",
}

# Campos que entram no /export e são aceitos no /import
EXPORT_FIELDS = ("url", "name", "interval", "selector", "mode", "ignore_regex", "keywords",
                 "notify", "user_agent", "paused")


class Storage:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ------------------------------------------------------------------ watches
    def add_watch(self, chat_id: int, url: str, interval: int, *, selector: str | None = None,
                  name: str | None = None, mode: str = "texto", ignore_regex: str | None = None,
                  keywords: str | None = None, notify: str = "tudo", user_agent: str | None = None,
                  paused: bool = False, next_check: float | None = None) -> int:
        now = time.time()
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO watches (chat_id, url, name, interval, selector, mode, ignore_regex,
                       keywords, notify, user_agent, paused, created_at, next_check)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (chat_id, url, name, interval, selector, mode, ignore_regex, keywords, notify,
                 user_agent, int(bool(paused)), now, now + interval if next_check is None else next_check),
            )
            return int(cur.lastrowid)

    def get(self, watch_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone()
        return dict(row) if row else None

    def list_chat(self, chat_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM watches WHERE chat_id=? ORDER BY id", (chat_id,)).fetchall()
        return [dict(r) for r in rows]

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM watches ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def find(self, chat_id: int, url: str, selector: str | None) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM watches WHERE chat_id=? AND url=? AND IFNULL(selector,'')=IFNULL(?,'')",
                (chat_id, url, selector),
            ).fetchone()
        return dict(row) if row else None

    def count_chat(self, chat_id: int) -> int:
        with self._lock:
            return int(self.conn.execute("SELECT COUNT(*) FROM watches WHERE chat_id=?", (chat_id,)).fetchone()[0])

    def due(self, now: float, limit: int = 200) -> list[int]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM watches WHERE paused=0 AND next_check<=? ORDER BY next_check LIMIT ?",
                (now, limit),
            ).fetchall()
        return [int(r[0]) for r in rows]

    def update(self, watch_id: int, **fields: Any) -> None:
        if not fields:
            return
        bad = set(fields) - UPDATABLE
        if bad:
            raise ValueError(f"campos inválidos: {bad}")
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self.conn.execute(f"UPDATE watches SET {cols} WHERE id=?", (*fields.values(), watch_id))

    def delete(self, watch_id: int) -> bool:
        with self._lock:
            return self.conn.execute("DELETE FROM watches WHERE id=?", (watch_id,)).rowcount > 0

    def pause_chat(self, chat_id: int) -> int:
        with self._lock:
            return self.conn.execute("UPDATE watches SET paused=1 WHERE chat_id=?", (chat_id,)).rowcount

    def stats(self) -> dict[str, int]:
        with self._lock:
            row = self.conn.execute(
                """SELECT COUNT(*), COALESCE(SUM(paused=0),0), COUNT(DISTINCT chat_id),
                          COALESCE(SUM(error_count>0 AND paused=0),0), COALESCE(SUM(checks),0),
                          COALESCE(SUM(changes),0)
                   FROM watches"""
            ).fetchone()
        keys = ("total", "active", "chats", "with_errors", "checks", "changes")
        return dict(zip(keys, (int(v or 0) for v in row)))

    # ----------------------------------------------------------------- settings
    def get_setting(self, key: str) -> str | None:
        with self._lock:
            row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str | None) -> None:
        with self._lock:
            if value is None:
                self.conn.execute("DELETE FROM settings WHERE key=?", (key,))
            else:
                self.conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )

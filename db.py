"""SQLite-backed conversation history and session management."""

import sqlite3
import time
from contextlib import contextmanager
from typing import Optional

from config import DATA_DIR, ensure_data_dir
import os

DB_PATH = os.path.join(DATA_DIR, "history.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    model TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


@contextmanager
def get_conn():
    ensure_data_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def create_session(name: str, model: str) -> int:
    now = time.time()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO sessions (name, model, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, model, now, now),
        )
        return cur.lastrowid


def list_sessions() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC"
        ).fetchall()


def rename_session(session_id: int, name: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE sessions SET name = ? WHERE id = ?", (name, session_id))


def touch_session(session_id: int, model: Optional[str] = None) -> None:
    with get_conn() as conn:
        if model:
            conn.execute(
                "UPDATE sessions SET updated_at = ?, model = ? WHERE id = ?",
                (time.time(), model, session_id),
            )
        else:
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (time.time(), session_id),
            )


def delete_session(session_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


def clear_session_messages(session_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))


def add_message(session_id: int, role: str, content: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (session_id, role, content, time.time()),
        )
        return cur.lastrowid


def load_messages(session_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()


def set_memory(key: str, value: str) -> None:
    now = time.time()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO memories (key, value, created_at, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, value, now, now),
        )


def get_memory(key: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM memories WHERE key = ?", (key,)).fetchone()


def delete_memory(key: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM memories WHERE key = ?", (key,))
        return cur.rowcount > 0


def search_memories(query: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        pattern = f"%{query}%"
        return conn.execute(
            "SELECT * FROM memories WHERE key LIKE ? OR value LIKE ? ORDER BY updated_at DESC",
            (pattern, pattern),
        ).fetchall()


def list_memories() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM memories ORDER BY updated_at DESC").fetchall()

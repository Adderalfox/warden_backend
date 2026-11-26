"""Tiny SQLite layer for user accounts (stdlib only)."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from app.core.config import settings

_BACKEND_ROOT = Path(__file__).resolve().parents[2]


def resolve_path(path: str) -> str:
    if path == ":memory:":
        return path
    p = Path(path)
    return str(p if p.is_absolute() else _BACKEND_ROOT / p)


class Database:
    def __init__(self, path: str) -> None:
        self.path = resolve_path(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    email          TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    name           TEXT NOT NULL,
                    password_hash  TEXT NOT NULL,
                    token_version  INTEGER NOT NULL DEFAULT 0,
                    created_at     REAL NOT NULL,
                    last_login_at  REAL
                );
                """
            )
            self._conn.commit()

    # --- helpers ---
    def one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def run(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.lastrowid or 0

    # --- users ---
    def count_users(self) -> int:
        return int(self.one("SELECT COUNT(*) AS n FROM users")["n"])

    def create_user(self, email: str, name: str, password_hash: str) -> int:
        return self.run(
            "INSERT INTO users (email, name, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (email.strip(), name.strip(), password_hash, time.time()),
        )

    def user_by_email(self, email: str) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM users WHERE email = ?", (email.strip(),))

    def user_by_id(self, user_id: int) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM users WHERE id = ?", (user_id,))

    def touch_login(self, user_id: int) -> None:
        self.run("UPDATE users SET last_login_at = ? WHERE id = ?", (time.time(), user_id))

    def update_name(self, user_id: int, name: str) -> None:
        self.run("UPDATE users SET name = ? WHERE id = ?", (name.strip(), user_id))

    def set_password(self, user_id: int, password_hash: str) -> int:
        """Store a new hash and bump token_version (invalidates every existing session)."""
        self.run(
            "UPDATE users SET password_hash = ?, token_version = token_version + 1 WHERE id = ?",
            (password_hash, user_id),
        )
        return int(self.user_by_id(user_id)["token_version"])


_db: Optional[Database] = None


def db() -> Database:
    """Process-wide handle; re-opened if WARDEN_DATABASE_PATH changes (tests)."""
    global _db
    want = resolve_path(settings.database_path)
    if _db is None or _db.path != want:
        _db = Database(settings.database_path)
    return _db


def reset_db() -> None:
    global _db
    _db = None


def public_user(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"] if "last_login_at" in row.keys() else None,
    }

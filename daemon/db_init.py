"""Daemon local DB initialization helpers.

This module initializes a lightweight local SQLite database used by the
daemon process for deployment readiness and operation metadata.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def ensure_daemon_db_initialized(db_path: str) -> dict:
    """Initialize daemon SQLite DB (idempotent).

    Returns a small status payload suitable for API responses.
    """
    path = Path(db_path)
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daemon_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS r2_upload_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT,
                upload_type TEXT,
                object_key TEXT NOT NULL,
                original_name TEXT NOT NULL,
                content_type TEXT NOT NULL,
                content_length INTEGER NOT NULL,
                etag TEXT,
                deleted_at TEXT,
                uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        migrations = [
            "ALTER TABLE r2_upload_logs ADD COLUMN guild_id TEXT",
            "ALTER TABLE r2_upload_logs ADD COLUMN upload_type TEXT",
            "ALTER TABLE r2_upload_logs ADD COLUMN original_name TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE r2_upload_logs ADD COLUMN deleted_at TEXT",
        ]
        for migration in migrations:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        conn.execute(
            """
            INSERT INTO daemon_meta (key, value)
            VALUES ('schema_version', '2')
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = datetime('now')
            """
        )
        conn.commit()

    return {
        "ok": True,
        "db_path": str(path),
        "schema_version": "2",
    }

"""
SQL schema definitions and one-time initialisation.

All tables use ``CREATE TABLE IF NOT EXISTS`` — safe to call on every start.
"""

from mods.logger import setup_logger
from mods.storage import DataStore

logger = setup_logger(__name__)

# Each string is a standalone DDL statement executed via the D1 REST API.
_TABLES_DDL: list[str] = [
    # ------------------------------------------------------------------
    # guilds — basic guild metadata
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS guilds (
        guild_id   TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        owner_id   TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # ------------------------------------------------------------------
    # channels — current channel structure
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS channels (
        channel_id TEXT PRIMARY KEY,
        guild_id   TEXT NOT NULL,
        name       TEXT NOT NULL,
        type       INTEGER NOT NULL,
        position   INTEGER NOT NULL DEFAULT 0,
        parent_id  TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (guild_id) REFERENCES guilds (guild_id) ON DELETE CASCADE
    )
    """,
    # ------------------------------------------------------------------
    # roles — current role structure
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS roles (
        role_id     TEXT PRIMARY KEY,
        guild_id    TEXT NOT NULL,
        name        TEXT NOT NULL,
        permissions TEXT NOT NULL,
        position    INTEGER NOT NULL DEFAULT 0,
        color       INTEGER NOT NULL DEFAULT 0,
        hoist       INTEGER NOT NULL DEFAULT 0,
        mentionable INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (guild_id) REFERENCES guilds (guild_id) ON DELETE CASCADE
    )
    """,
    # ------------------------------------------------------------------
    # structure_snapshots — historical snapshots with timestamps
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS structure_snapshots (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id      TEXT NOT NULL,
        target_type   TEXT NOT NULL,
        target_id     TEXT NOT NULL,
        snapshot_data TEXT NOT NULL,
        timestamp     INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        FOREIGN KEY (guild_id) REFERENCES guilds (guild_id) ON DELETE CASCADE
    )
    """,
    # ------------------------------------------------------------------
    # encrypted_messages — message content, author name & avatar
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS encrypted_messages (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id        TEXT NOT NULL UNIQUE,
        channel_id        TEXT NOT NULL,
        guild_id          TEXT NOT NULL,
        author_id         TEXT NOT NULL,
        author_name       TEXT NOT NULL,
        author_avatar     TEXT,
        encrypted_content TEXT NOT NULL,
        nonce             TEXT NOT NULL,
        timestamp         INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        FOREIGN KEY (guild_id) REFERENCES guilds (guild_id) ON DELETE CASCADE
    )
    """,
    # ------------------------------------------------------------------
    # recovery_approvers — admins who accepted the approver role
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS recovery_approvers (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id    TEXT NOT NULL,
        user_id     TEXT NOT NULL,
        approved_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (guild_id) REFERENCES guilds (guild_id) ON DELETE CASCADE,
        UNIQUE (guild_id, user_id)
    )
    """,
    # ------------------------------------------------------------------
    # temp_cache — short-lived event log for anomaly detection
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS temp_cache (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id   TEXT NOT NULL,
        event_type TEXT NOT NULL,
        target_id  TEXT NOT NULL,
        old_data   TEXT,
        new_data   TEXT,
        timestamp  INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
    )
    """,
]


async def init_schema(store: DataStore) -> None:
    """Create all application tables in D1 (idempotent).

    Calls ``PRAGMA foreign_keys = ON`` first, then executes every DDL
    statement in :data:`_TABLES_DDL`.
    """
    if not store.d1_available:
        logger.warning("D1 not available — schema initialisation skipped")
        return

    await store.enable_foreign_keys()

    for ddl in _TABLES_DDL:
        await store.execute(ddl)

    logger.info("Schema initialised (%d tables)", len(_TABLES_DDL))

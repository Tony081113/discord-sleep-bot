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
        pinned        INTEGER NOT NULL DEFAULT 0,
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
        attachment_names  TEXT,
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
    # ------------------------------------------------------------------
    # guild_thresholds — per-guild anomaly detection thresholds
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS guild_thresholds (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id        TEXT NOT NULL,
        event_type      TEXT NOT NULL,
        threshold_value INTEGER NOT NULL,
        window_seconds  INTEGER NOT NULL DEFAULT 300,
        updated_by      TEXT,
        updated_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        FOREIGN KEY (guild_id) REFERENCES guilds (guild_id) ON DELETE CASCADE,
        UNIQUE (guild_id, event_type)
    )
    """,
    # ------------------------------------------------------------------
    # guild_defense_state — per-guild defense toggle with auto-resume
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS guild_defense_state (
        guild_id       TEXT PRIMARY KEY,
        is_enabled     INTEGER NOT NULL DEFAULT 1,
        disabled_until INTEGER,
        updated_by     TEXT,
        updated_at     INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        FOREIGN KEY (guild_id) REFERENCES guilds (guild_id) ON DELETE CASCADE
    )
    """,
    # ------------------------------------------------------------------
    # app_settings — global runtime settings
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS app_settings (
        setting_key   TEXT PRIMARY KEY,
        setting_value TEXT,
        updated_by    TEXT,
        updated_at    INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
    )
    """,
    # ------------------------------------------------------------------
    # guild_r2_settings — per-guild R2 quota overrides
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS guild_r2_settings (
        guild_id           TEXT PRIMARY KEY,
        quota_mb           REAL,
        updated_by         TEXT,
        updated_at         INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        FOREIGN KEY (guild_id) REFERENCES guilds (guild_id) ON DELETE CASCADE
    )
    """,
    # ------------------------------------------------------------------
    # recovery_requests — recovery approval workflow
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS recovery_requests (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id        TEXT NOT NULL,
        event_type      TEXT NOT NULL,
        event_count     INTEGER NOT NULL DEFAULT 0,
        status          TEXT NOT NULL DEFAULT 'pending',
        requested_by    TEXT,
        approved_by     TEXT,
        result_channels INTEGER DEFAULT 0,
        result_roles    INTEGER DEFAULT 0,
        result_messages INTEGER DEFAULT 0,
        alert_msg_ids   TEXT DEFAULT NULL,
        attacker_ids    TEXT DEFAULT NULL,
        created_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        resolved_at     INTEGER,
        FOREIGN KEY (guild_id) REFERENCES guilds (guild_id) ON DELETE CASCADE
    )
    """,
    # ------------------------------------------------------------------
    # maintenance_logs — transparent maintenance notes
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS maintenance_logs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id    TEXT,
        content     TEXT NOT NULL,
        author_id   TEXT NOT NULL,
        author_name TEXT NOT NULL,
        created_at  INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
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

    # ---------------------------------------------------------------------------
    # Migrations — ADD COLUMN is idempotent on success; ignore if already exists.
    # ---------------------------------------------------------------------------
    _MIGRATIONS = [
        "ALTER TABLE structure_snapshots ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE recovery_requests ADD COLUMN alert_msg_ids TEXT DEFAULT NULL",
        "ALTER TABLE recovery_requests ADD COLUMN attacker_ids TEXT DEFAULT NULL",
        "ALTER TABLE encrypted_messages ADD COLUMN attachment_names TEXT",
    ]
    for migration in _MIGRATIONS:
        try:
            await store.execute(migration)
            logger.info("Migration applied: %s", migration[:60])
        except Exception as exc:  # noqa: BLE001
            # Ignore only already-exists style errors; log others for visibility.
            msg = str(exc).lower()
            benign = (
                "duplicate column name" in msg
                or "already exists" in msg
            )
            if not benign:
                logger.warning("Migration failed: %s | err=%s", migration, exc)

    # 強化檢查：D1 若曾發生靜默 migration 失敗，啟動時主動補 attachment_names 欄位。
    try:
        cols = await store.fetchall("PRAGMA table_info(encrypted_messages)")
        col_names = {str(r.get("name", "")) for r in cols}
        if "attachment_names" not in col_names:
            await store.execute(
                "ALTER TABLE encrypted_messages ADD COLUMN attachment_names TEXT"
            )
            logger.info("Schema heal applied: encrypted_messages.attachment_names")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Schema heal failed for encrypted_messages.attachment_names: %s",
            exc,
        )

    logger.info("Schema initialised (%d tables)", len(_TABLES_DDL))

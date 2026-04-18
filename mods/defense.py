"""防禦系統狀態管理工具。"""

from __future__ import annotations

import time
from typing import Any

from mods.logger import setup_logger

logger = setup_logger(__name__)

DEFAULT_DISABLE_SECONDS = 3600
MIN_DISABLE_SECONDS = 60
MAX_DISABLE_SECONDS = 86400


def _normalize_duration(duration_seconds: int) -> int:
    """將停用秒數限制在安全範圍。"""
    return max(MIN_DISABLE_SECONDS, min(MAX_DISABLE_SECONDS, duration_seconds))


async def get_defense_state(store, guild_id: str) -> dict[str, Any]:
    """取得防禦狀態；若逾時則自動恢復並回傳最新狀態。"""
    rows = await store.fetchall(
        "SELECT is_enabled, disabled_until FROM guild_defense_state WHERE guild_id = ?",
        [guild_id],
    )

    if not rows:
        return {
            "enabled": True,
            "disabled_until": None,
            "remaining_seconds": 0,
            "auto_restored": False,
        }

    row = rows[0]
    enabled = bool(row.get("is_enabled", 1))
    disabled_until = row.get("disabled_until")
    now = int(time.time())

    if not enabled and disabled_until:
        disabled_until = int(disabled_until)
        if now >= disabled_until:
            await store.execute(
                "UPDATE guild_defense_state "
                "SET is_enabled = 1, disabled_until = NULL, updated_by = ?, "
                "updated_at = strftime('%s','now') "
                "WHERE guild_id = ?",
                ["system:auto_restore", guild_id],
            )
            logger.info("Defense auto-restored guild=%s", guild_id)
            return {
                "enabled": True,
                "disabled_until": None,
                "remaining_seconds": 0,
                "auto_restored": True,
            }

        return {
            "enabled": False,
            "disabled_until": disabled_until,
            "remaining_seconds": max(0, disabled_until - now),
            "auto_restored": False,
        }

    return {
        "enabled": enabled,
        "disabled_until": None,
        "remaining_seconds": 0,
        "auto_restored": False,
    }


async def set_defense_disabled(
    store,
    guild_id: str,
    updated_by: str,
    duration_seconds: int = DEFAULT_DISABLE_SECONDS,
) -> dict[str, Any]:
    """停用防禦系統一段時間。"""
    duration = _normalize_duration(duration_seconds)
    disabled_until = int(time.time()) + duration

    await store.execute(
        "INSERT INTO guild_defense_state (guild_id, is_enabled, disabled_until, updated_by) "
        "VALUES (?, 0, ?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET "
        "is_enabled = 0, disabled_until = excluded.disabled_until, "
        "updated_by = excluded.updated_by, updated_at = strftime('%s','now')",
        [guild_id, disabled_until, updated_by],
    )

    logger.warning(
        "Defense disabled guild=%s by=%s until=%s duration=%s",
        guild_id,
        updated_by,
        disabled_until,
        duration,
    )
    return {
        "enabled": False,
        "disabled_until": disabled_until,
        "remaining_seconds": duration,
        "auto_restored": False,
    }


async def set_defense_enabled(store, guild_id: str, updated_by: str) -> dict[str, Any]:
    """立即啟用防禦系統。"""
    await store.execute(
        "INSERT INTO guild_defense_state (guild_id, is_enabled, disabled_until, updated_by) "
        "VALUES (?, 1, NULL, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET "
        "is_enabled = 1, disabled_until = NULL, "
        "updated_by = excluded.updated_by, updated_at = strftime('%s','now')",
        [guild_id, updated_by],
    )

    logger.info("Defense enabled guild=%s by=%s", guild_id, updated_by)
    return {
        "enabled": True,
        "disabled_until": None,
        "remaining_seconds": 0,
        "auto_restored": False,
    }

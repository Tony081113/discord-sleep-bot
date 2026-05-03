from __future__ import annotations

import os

from mods.storage import get_storage

_DEFAULT_GUILD_QUOTA_MB = 100.0
_DEFAULT_SETTING_KEY = "r2_default_quota_mb"


def _fallback_default_quota_mb() -> float:
    raw = os.getenv("GUILD_R2_QUOTA_MB", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return _DEFAULT_GUILD_QUOTA_MB


async def ensure_guild_registered(guild_id: str, guild_name: str, owner_id: str) -> None:
    store = get_storage()
    await store.execute(
        "INSERT INTO guilds (guild_id, name, owner_id) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET name = excluded.name, owner_id = excluded.owner_id",
        [guild_id, guild_name, owner_id],
    )


async def get_default_guild_quota_mb() -> float:
    store = get_storage()
    rows = await store.fetchall(
        "SELECT setting_value FROM app_settings WHERE setting_key = ?",
        [_DEFAULT_SETTING_KEY],
    )
    if rows:
        try:
            return float(rows[0]["setting_value"])
        except (TypeError, ValueError, KeyError):
            pass
    return _fallback_default_quota_mb()


async def set_default_guild_quota_mb(quota_mb: float, updated_by: str) -> None:
    store = get_storage()
    await store.execute(
        "INSERT INTO app_settings (setting_key, setting_value, updated_by, updated_at) "
        "VALUES (?, ?, ?, strftime('%s','now')) "
        "ON CONFLICT(setting_key) DO UPDATE SET "
        "setting_value = excluded.setting_value, "
        "updated_by = excluded.updated_by, "
        "updated_at = excluded.updated_at",
        [_DEFAULT_SETTING_KEY, str(quota_mb), updated_by],
    )


async def get_guild_quota_mb(guild_id: str) -> float:
    store = get_storage()
    rows = await store.fetchall(
        "SELECT quota_mb FROM guild_r2_settings WHERE guild_id = ?",
        [guild_id],
    )
    if rows and rows[0].get("quota_mb") is not None:
        try:
            return float(rows[0]["quota_mb"])
        except (TypeError, ValueError, KeyError):
            pass
    return await get_default_guild_quota_mb()


async def get_guild_quota_details(guild_id: str) -> dict[str, float | bool]:
    store = get_storage()
    rows = await store.fetchall(
        "SELECT quota_mb FROM guild_r2_settings WHERE guild_id = ?",
        [guild_id],
    )
    default_quota_mb = await get_default_guild_quota_mb()
    if rows and rows[0].get("quota_mb") is not None:
        try:
            quota_mb = float(rows[0]["quota_mb"])
            return {
                "quota_mb": quota_mb,
                "default_quota_mb": default_quota_mb,
                "is_override": True,
            }
        except (TypeError, ValueError, KeyError):
            pass
    return {
        "quota_mb": default_quota_mb,
        "default_quota_mb": default_quota_mb,
        "is_override": False,
    }


async def set_guild_quota_mb(guild_id: str, quota_mb: float, updated_by: str) -> None:
    store = get_storage()
    await store.execute(
        "INSERT INTO guild_r2_settings (guild_id, quota_mb, updated_by, updated_at) "
        "VALUES (?, ?, ?, strftime('%s','now')) "
        "ON CONFLICT(guild_id) DO UPDATE SET "
        "quota_mb = excluded.quota_mb, "
        "updated_by = excluded.updated_by, "
        "updated_at = excluded.updated_at",
        [guild_id, quota_mb, updated_by],
    )


async def clear_guild_quota_override(guild_id: str) -> None:
    store = get_storage()
    await store.execute("DELETE FROM guild_r2_settings WHERE guild_id = ?", [guild_id])
"""媒體儲存 Cog（Phase R2）。

職責：
1. /upload-file  — 上傳附件到 Cloudflare R2。
2. /upload-avatar — 儲存成員頭像到 Cloudflare R2。
3. /r2-usage     — 查詢 R2 全域與此伺服器的使用量。
"""

from __future__ import annotations

import os
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from mods.db_mod_daemon import get_daemon_bridge
from mods.logger import setup_logger

logger = setup_logger(__name__)

_BYTES_PER_MB = 1024 * 1024
_MAX_FILE_SIZE_MB = int(os.getenv("R2_MAX_FILE_SIZE_MB", "25"))


def _get_guild_quota_mb(guild_id: str) -> float | None:
    """從 .env 讀取每個群組的 R2 配額（MB）。

    優先查 GUILD_R2_QUOTA_OVERRIDES（格式：guild_id:MB,guild_id:MB），
    其次用 GUILD_R2_QUOTA_MB 作為預設值；兩者都沒設定則回傳 None（無限制）。
    """
    overrides_raw = os.getenv("GUILD_R2_QUOTA_OVERRIDES", "").strip()
    if overrides_raw:
        for pair in overrides_raw.split(","):
            pair = pair.strip()
            if ":" not in pair:
                continue
            gid, mb = pair.split(":", 1)
            if gid.strip() == guild_id:
                try:
                    return float(mb.strip())
                except ValueError:
                    pass

    default = os.getenv("GUILD_R2_QUOTA_MB", "").strip()
    if default:
        try:
            return float(default)
        except ValueError:
            pass
    return None


async def _fetch_bytes(url: str) -> bytes:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()


def _quota_error_message(exc: Exception) -> str | None:
    s = str(exc)
    if "r2_locked" in s:
        return "❌ R2 全域儲存空間已達上限，暫停寫入。請聯絡開發者。"
    if "guild_quota_exceeded" in s:
        return "❌ 此伺服器的 R2 儲存配額已滿。"
    return None


class MediaCog(commands.Cog, name="Media"):
    """上傳檔案與頭像到 Cloudflare R2。"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # /upload-file
    # ------------------------------------------------------------------

    @app_commands.command(name="upload-file", description="上傳檔案到 R2 儲存空間")
    @app_commands.describe(attachment="要上傳的檔案")
    @app_commands.guild_only()
    async def upload_file(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        guild_id = str(interaction.guild_id)
        bridge = get_daemon_bridge()

        if not bridge.enabled():
            await interaction.followup.send("❌ R2 daemon 未啟用，請聯絡管理員。", ephemeral=True)
            return

        if attachment.size > _MAX_FILE_SIZE_MB * _BYTES_PER_MB:
            await interaction.followup.send(
                f"❌ 檔案超過上限 {_MAX_FILE_SIZE_MB} MB。", ephemeral=True
            )
            return

        try:
            data = await _fetch_bytes(attachment.url)
        except Exception as exc:
            logger.error("Failed to download attachment: %s", exc)
            await interaction.followup.send("❌ 下載附件失敗，請稍後再試。", ephemeral=True)
            return

        guild_quota_mb = _get_guild_quota_mb(guild_id)

        try:
            result = await bridge.upload_file(
                guild_id=guild_id,
                filename=attachment.filename,
                data=data,
                content_type=attachment.content_type or "application/octet-stream",
                guild_quota_mb=guild_quota_mb,
            )
        except Exception as exc:
            msg = _quota_error_message(exc)
            if msg:
                await interaction.followup.send(msg, ephemeral=True)
            else:
                logger.error("R2 file upload failed: %s", exc)
                await interaction.followup.send("❌ 上傳失敗，請稍後再試。", ephemeral=True)
            return

        size_mb = len(data) / _BYTES_PER_MB
        await interaction.followup.send(
            f"✅ 上傳成功！\nKey: `{result.get('key')}`\n大小: {size_mb:.2f} MB",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /upload-avatar
    # ------------------------------------------------------------------

    @app_commands.command(name="upload-avatar", description="儲存成員頭像到 R2")
    @app_commands.describe(member="要儲存頭像的成員（預設為自己）")
    @app_commands.guild_only()
    async def upload_avatar(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        guild_id = str(interaction.guild_id)
        target = member or interaction.user
        bridge = get_daemon_bridge()

        if not bridge.enabled():
            await interaction.followup.send("❌ R2 daemon 未啟用，請聯絡管理員。", ephemeral=True)
            return

        if not target.avatar:
            await interaction.followup.send("❌ 該成員沒有頭像。", ephemeral=True)
            return

        avatar_url = str(target.avatar.url)
        ext = avatar_url.split(".")[-1].split("?")[0] or "webp"
        user_id = str(target.id)
        guild_quota_mb = _get_guild_quota_mb(guild_id)

        try:
            data = await _fetch_bytes(avatar_url)
        except Exception as exc:
            logger.error("Failed to download avatar: %s", exc)
            await interaction.followup.send("❌ 下載頭像失敗，請稍後再試。", ephemeral=True)
            return

        try:
            result = await bridge.upload_avatar(
                guild_id=guild_id,
                user_id=user_id,
                data=data,
                ext=ext,
                guild_quota_mb=guild_quota_mb,
            )
        except Exception as exc:
            msg = _quota_error_message(exc)
            if msg:
                await interaction.followup.send(msg, ephemeral=True)
            else:
                logger.error("R2 avatar upload failed: %s", exc)
                await interaction.followup.send("❌ 上傳失敗，請稍後再試。", ephemeral=True)
            return

        size_kb = len(data) / 1024
        await interaction.followup.send(
            f"✅ 頭像已儲存！\nKey: `{result.get('key')}`\n大小: {size_kb:.1f} KB",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /r2-usage
    # ------------------------------------------------------------------

    @app_commands.command(name="r2-usage", description="查詢 R2 儲存空間使用量")
    @app_commands.guild_only()
    async def r2_usage(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        guild_id = str(interaction.guild_id)
        bridge = get_daemon_bridge()

        if not bridge.enabled():
            await interaction.followup.send("❌ R2 daemon 未啟用。", ephemeral=True)
            return

        try:
            total_info = await bridge.get_usage()
            guild_info = await bridge.get_guild_usage(guild_id=guild_id)
        except Exception as exc:
            logger.error("Failed to get R2 usage: %s", exc)
            await interaction.followup.send("❌ 查詢失敗，請稍後再試。", ephemeral=True)
            return

        total_gb: float = total_info.get("total_gb", 0)
        locked: bool = total_info.get("locked", False)
        guild_mb: float = guild_info.get("used_mb", 0)
        guild_quota_mb = _get_guild_quota_mb(guild_id)

        global_quota_gb = float(os.getenv("R2_GLOBAL_QUOTA_GB", "10"))
        color = discord.Color.red() if locked else discord.Color.blurple()
        embed = discord.Embed(title="☁️ R2 儲存空間使用量", color=color)
        embed.add_field(
            name="全域使用量",
            value=f"{total_gb:.3f} GB / {global_quota_gb:.0f} GB",
            inline=False,
        )

        if guild_quota_mb is not None:
            embed.add_field(
                name="此伺服器使用量",
                value=f"{guild_mb:.1f} MB / {guild_quota_mb:.0f} MB",
                inline=False,
            )
        else:
            embed.add_field(
                name="此伺服器使用量",
                value=f"{guild_mb:.1f} MB（無配額限制）",
                inline=False,
            )

        if locked:
            embed.add_field(
                name="⚠️ 狀態",
                value=f"R2 已鎖定：{total_info.get('lock_reason', '')}",
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MediaCog(bot))

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
from mods.r2_settings import get_guild_quota_details, get_guild_quota_mb

logger = setup_logger(__name__)

_BYTES_PER_MB = 1024 * 1024
_MAX_FILE_SIZE_MB = int(os.getenv("R2_MAX_FILE_SIZE_MB", "25"))

_DEV_IDS = {
    str(uid.strip())
    for uid in os.getenv("BOT_ADMIN_ID", "").split(",")
    if uid.strip()
}


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

    def _is_developer(self, user_id: int) -> bool:
        return str(user_id) in _DEV_IDS

    def _can_manage_r2(self, interaction: discord.Interaction) -> bool:
        if self._is_developer(interaction.user.id):
            return True
        perms = getattr(interaction.user, "guild_permissions", None)
        return bool(perms and perms.administrator)

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

        guild_quota_mb = await get_guild_quota_mb(guild_id)

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
                await interaction.followup.send(
                    f"❌ {attachment.filename}：{msg.removeprefix('❌ ').strip()}",
                    ephemeral=True,
                )
            else:
                logger.error("R2 file upload failed: %s", exc)
                await interaction.followup.send(
                    f"❌ {attachment.filename}：上傳失敗，請稍後再試。",
                    ephemeral=True,
                )
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
        guild_quota_mb = await get_guild_quota_mb(guild_id)

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
    async def r2_usage(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        if interaction.guild_id is None:
            await interaction.followup.send("請到伺服器內使用 `/r2-usage` 查看剩餘額度。", ephemeral=True)
            return

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

        total_gb: float = float(total_info.get("current_quota_gb", total_info.get("total_gb", 0)) or 0)
        global_quota_gb: float = float(total_info.get("total_quota_gb", os.getenv("R2_GLOBAL_QUOTA_GB", "10")) or 0)
        remaining_global_gb: float = float(total_info.get("remaining_quota_gb", max(0.0, global_quota_gb - total_gb)) or 0)
        locked: bool = total_info.get("locked", False)
        guild_mb: float = guild_info.get("used_mb", 0)
        quota_info = await get_guild_quota_details(guild_id)
        guild_quota_mb = float(quota_info["quota_mb"])

        color = discord.Color.red() if locked else discord.Color.blurple()
        embed = discord.Embed(title="☁️ R2 儲存空間使用量", color=color)
        embed.add_field(
            name="全域使用量",
            value=f"{total_gb:.3f} GB / {global_quota_gb:.0f} GB",
            inline=False,
        )
        embed.add_field(
            name="全域剩餘額度",
            value=f"{remaining_global_gb:.3f} GB",
            inline=False,
        )

        quota_label = f"{guild_mb:.1f} MB / {guild_quota_mb:.0f} MB"
        if quota_info["is_override"]:
            quota_label += f"\n此伺服器已覆寫預設 {float(quota_info['default_quota_mb']):.0f} MB"
        embed.add_field(
            name="此伺服器使用量",
            value=quota_label,
            inline=False,
        )

        remaining_mb = max(0.0, guild_quota_mb - guild_mb)
        embed.add_field(
            name="剩餘額度",
            value=f"{remaining_mb:.1f} MB",
            inline=False,
        )

        if locked:
            embed.add_field(
                name="⚠️ 狀態",
                value=f"R2 已鎖定：{total_info.get('lock_reason', '')}",
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="r2-files", description="查看此伺服器 R2 檔案大小排行")
    @app_commands.describe(limit="最多顯示幾筆（1-20）")
    @app_commands.guild_only()
    async def r2_files(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 20] = 10) -> None:
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("❌ 此指令只能在伺服器內使用。", ephemeral=True)
            return
        if not self._can_manage_r2(interaction):
            await interaction.followup.send("❌ 只有群管理員或開發者可以查看檔案排行。", ephemeral=True)
            return

        bridge = get_daemon_bridge()
        if not bridge.enabled():
            await interaction.followup.send("❌ R2 daemon 未啟用。", ephemeral=True)
            return

        try:
            result = await bridge.list_guild_objects(guild_id=str(guild.id), upload_type="files", limit=limit)
        except Exception as exc:
            logger.error("Failed to list R2 files guild=%s: %s", guild.id, exc)
            await interaction.followup.send("❌ 讀取檔案排行失敗，請稍後再試。", ephemeral=True)
            return

        objects = result.get("objects", [])
        if not objects:
            await interaction.followup.send("目前此伺服器沒有已上傳的 R2 檔案。", ephemeral=True)
            return

        lines = []
        for index, item in enumerate(objects, start=1):
            size_mb = float(item.get("size_bytes", 0)) / _BYTES_PER_MB
            lines.append(
                f"{index}. {item.get('name', 'unknown')}\n"
                f"   {size_mb:.2f} MB | key: `{item.get('key', '')}`"
            )

        embed = discord.Embed(title="R2 檔案大小排行", color=discord.Color.orange())
        embed.description = "\n".join(lines)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="r2-delete", description="刪除此伺服器 R2 檔案")
    @app_commands.describe(object_key="要刪除的完整 object key，可先用 /r2-files 查看")
    @app_commands.guild_only()
    async def r2_delete(self, interaction: discord.Interaction, object_key: str) -> None:
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("❌ 此指令只能在伺服器內使用。", ephemeral=True)
            return
        if not self._can_manage_r2(interaction):
            await interaction.followup.send("❌ 只有群管理員或開發者可以刪除檔案。", ephemeral=True)
            return

        bridge = get_daemon_bridge()
        if not bridge.enabled():
            await interaction.followup.send("❌ R2 daemon 未啟用。", ephemeral=True)
            return

        object_key = object_key.strip()
        if not object_key.startswith(f"{guild.id}/files/"):
            await interaction.followup.send("❌ 只能刪除此伺服器 files 目錄下的物件。", ephemeral=True)
            return

        try:
            result = await bridge.delete_guild_object(guild_id=str(guild.id), object_key=object_key)
        except Exception as exc:
            logger.error("Failed to delete R2 file guild=%s key=%s: %s", guild.id, object_key, exc)
            await interaction.followup.send("❌ 刪除失敗，請稍後再試。", ephemeral=True)
            return

        await interaction.followup.send(
            f"✅ 已刪除檔案：{result.get('name', object_key.rsplit('/', 1)[-1])}",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MediaCog(bot))

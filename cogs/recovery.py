"""復原模組（Phase 4）。

職責：
1. 由核准者按鈕觸發復原流程。
2. 依 5 分鐘前快照還原頻道與身分組。
3. 透過 Webhook 還原訊息（保留原暱稱與頭像）。
"""

import asyncio
import json
from typing import Any

import discord
from discord.ext import commands

from mods.crypto import decrypt
from mods.logger import setup_logger
from mods.rate_limit import rate_limited_call, CHANNEL_OP_DELAY, ROLE_OP_DELAY
from mods.storage import get_storage

logger = setup_logger(__name__)

_RECOVERY_LOOKBACK = 300  # seconds (5 minutes)
_WEBHOOK_NAME = "SleepBot Recovery"
_MAX_RESTORE_MESSAGES = 100
_WEBHOOK_SEND_DELAY = 0.6  # seconds between webhook sends (rate-limit safety)


class RecoveryCog(commands.Cog, name="Recovery"):
    """負責伺服器結構與訊息的復原。"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # -------------------------------------------------------- interaction

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """接收按鈕互動，攔截 execute_recovery 事件。"""
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")
        if custom_id.startswith("execute_recovery:"):
            await self._handle_recovery(interaction, custom_id)

    # -------------------------------------------------------- main handler

    async def _handle_recovery(
        self, interaction: discord.Interaction, custom_id: str
    ) -> None:
        """驗證核准者身份後，執行完整復原流程。"""
        parts = custom_id.split(":")
        if len(parts) < 2:
            logger.warning("Invalid recovery custom_id=%s", custom_id)
            return
        guild_id = parts[1]
        request_id = parts[2] if len(parts) > 2 else None
        logger.info(
            "Recovery requested custom_id=%s guild=%s request_id=%s user=%s",
            custom_id,
            guild_id,
            request_id,
            interaction.user.id,
        )
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            logger.warning(
                "Recovery guild not found guild=%s request_id=%s user=%s",
                guild_id,
                request_id,
                interaction.user.id,
            )
            await interaction.response.send_message(
                "\u274c 找不到伺服器。", ephemeral=True
            )
            return

        # 檢查是否為核准者
        store = get_storage()
        approvers = await store.fetchall(
            "SELECT user_id FROM recovery_approvers WHERE guild_id = ?",
            [guild_id],
        )
        if str(interaction.user.id) not in {r["user_id"] for r in approvers}:
            logger.warning(
                "Recovery denied guild=%s request_id=%s user=%s approvers=%s",
                guild_id,
                request_id,
                interaction.user.id,
                len(approvers),
            )
            await interaction.response.send_message(
                "\u274c 你不是已註冊的核准者。", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        logger.info(
            "Recovery approved for execution guild=%s request_id=%s user=%s",
            guild_id,
            request_id,
            interaction.user.id,
        )

        try:
            ch, ro, ms = await self._execute_recovery(store, guild, request_id)

            # 若有對應請求，更新請求狀態
            if request_id:
                try:
                    await store.execute(
                        "UPDATE recovery_requests "
                        "SET status='approved', approved_by=?, "
                        "result_channels=?, result_roles=?, result_messages=?, "
                        "resolved_at=strftime('%s','now') "
                        "WHERE id=? AND status='pending'",
                        [str(interaction.user.id), ch, ro, ms, request_id],
                    )
                    logger.info(
                        "Recovery request marked approved request_id=%s guild=%s user=%s channels=%s roles=%s messages=%s",
                        request_id,
                        guild_id,
                        interaction.user.id,
                        ch,
                        ro,
                        ms,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "Failed to update recovery request request_id=%s guild=%s: %s",
                        request_id,
                        guild_id,
                        exc,
                        exc_info=True,
                    )

            await interaction.followup.send(
                f"\u2705 復原完成！\n"
                f"\U0001f4c1 頻道復原：{ch}\n"
                f"\U0001f3f7\ufe0f 身分組復原：{ro}\n"
                f"\U0001f4ac 訊息還原：{ms}",
                ephemeral=True,
            )
        except Exception as exc:
            logger.error(
                "Recovery failed guild=%s request_id=%s user=%s: %s",
                guild_id,
                request_id,
                interaction.user.id,
                exc,
                exc_info=True,
            )
            if request_id:
                try:
                    await store.execute(
                        "UPDATE recovery_requests "
                        "SET status='failed', approved_by=?, resolved_at=strftime('%s','now') "
                        "WHERE id=? AND status='pending'",
                        [str(interaction.user.id), request_id],
                    )
                    logger.info(
                        "Recovery request marked failed request_id=%s guild=%s user=%s",
                        request_id,
                        guild_id,
                        interaction.user.id,
                    )
                except Exception as update_exc:  # noqa: BLE001
                    logger.error(
                        "Failed to mark recovery request failed request_id=%s guild=%s: %s",
                        request_id,
                        guild_id,
                        update_exc,
                        exc_info=True,
                    )
            await interaction.followup.send(
                "\u274c 復原失敗，請稍後重試或聯繫系統管理員。", ephemeral=True
            )

    # -------------------------------------------------------- 復原主流程

    async def _execute_recovery(
        self, store, guild: discord.Guild, request_id: str | None = None
    ) -> tuple[int, int, int]:
        """執行三段式復原：頻道、身分組、訊息。"""
        guild_id = str(guild.id)

        channel_snaps = await self._get_pre_attack_snapshots(
            store, guild_id, "channel"
        )
        role_snaps = await self._get_pre_attack_snapshots(
            store, guild_id, "role"
        )

        logger.info(
            "Recovery snapshots loaded guild=%s request_id=%s channel_snapshots=%s role_snapshots=%s",
            guild_id,
            request_id,
            len(channel_snaps),
            len(role_snaps),
        )

        restored_ch = await self._restore_channels(guild, channel_snaps)
        restored_ro = await self._restore_roles(guild, role_snaps)
        restored_ms = await self._restore_messages(store, guild)

        logger.info(
            "Recovery finished guild=%s request_id=%s channels=%d roles=%d messages=%d",
            guild_id, request_id, restored_ch, restored_ro, restored_ms,
        )
        return restored_ch, restored_ro, restored_ms

    async def _get_pre_attack_snapshots(
        self, store, guild_id: str, target_type: str
    ) -> list[dict[str, Any]]:
        """取得每個目標在攻擊前（至少 _RECOVERY_LOOKBACK 秒前）的最新快照。"""
        return await store.fetchall(
            """
            SELECT s1.target_id, s1.snapshot_data, s1.timestamp
            FROM structure_snapshots s1
            INNER JOIN (
                SELECT target_id, MAX(timestamp) AS max_ts
                FROM structure_snapshots
                WHERE guild_id = ? AND target_type = ?
                  AND timestamp <= (strftime('%s', 'now') - ?)
                GROUP BY target_id
            ) s2 ON s1.target_id = s2.target_id AND s1.timestamp = s2.max_ts
            WHERE s1.guild_id = ? AND s1.target_type = ?
            """,
            [guild_id, target_type, _RECOVERY_LOOKBACK, guild_id, target_type],
        )

    # -------------------------------------------------------- 頻道復原

    async def _restore_channels(
        self, guild: discord.Guild, snapshots: list[dict]
    ) -> int:
        """依快照重建或修正頻道設定。"""
        restored = 0
        current = {str(ch.id): ch for ch in guild.channels}

        logger.info(
            "Restoring channels guild=%s snapshots=%s current_channels=%s",
            guild.id,
            len(snapshots),
            len(current),
        )

        for row in snapshots:
            data = json.loads(row["snapshot_data"])
            channel_id = data["channel_id"]
            existing = current.get(channel_id)

            try:
                if existing is None:
                    logger.info(
                        "Recreating missing channel guild=%s channel_id=%s name=%s",
                        guild.id,
                        channel_id,
                        data.get("name"),
                    )
                    await rate_limited_call(self._recreate_channel, guild, data)
                else:
                    logger.info(
                        "Updating existing channel guild=%s channel_id=%s name=%s",
                        guild.id,
                        channel_id,
                        data.get("name"),
                    )
                    await rate_limited_call(self._update_channel, guild, existing, data)
                restored += 1
                await asyncio.sleep(CHANNEL_OP_DELAY)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Restore channel failed guild=%s channel_id=%s name=%s: %s",
                    guild.id,
                    channel_id,
                    data.get("name"),
                    exc,
                    exc_info=True,
                )

        return restored

    async def _recreate_channel(
        self, guild: discord.Guild, data: dict
    ) -> None:
        """依快照資料重建單一頻道。"""
        ch_type = discord.ChannelType(data["type"])
        overwrites = self._build_overwrites(
            guild, data.get("permission_overwrites", [])
        )
        category = None
        if data.get("parent_id"):
            category = guild.get_channel(int(data["parent_id"]))

        kwargs: dict[str, Any] = {
            "name": data["name"],
            "overwrites": overwrites,
            "position": data.get("position", 0),
        }
        if category:
            kwargs["category"] = category

        if ch_type == discord.ChannelType.text:
            kwargs["topic"] = data.get("topic")
            kwargs["nsfw"] = data.get("nsfw", False)
            kwargs["slowmode_delay"] = data.get("slowmode_delay", 0)
            await guild.create_text_channel(**kwargs)
        elif ch_type == discord.ChannelType.voice:
            await guild.create_voice_channel(**kwargs)
        elif ch_type == discord.ChannelType.category:
            await guild.create_category(**kwargs)
        else:
            await guild.create_text_channel(**kwargs)

        logger.info("Recreated channel '%s' in guild %s", data["name"], guild.id)

    async def _update_channel(
        self, guild: discord.Guild, channel, data: dict
    ) -> None:
        """將既有頻道調整回快照設定。"""
        overwrites = self._build_overwrites(
            guild, data.get("permission_overwrites", [])
        )
        edit_kwargs: dict[str, Any] = {
            "name": data["name"],
            "position": data.get("position", 0),
            "overwrites": overwrites,
        }
        if isinstance(channel, discord.TextChannel):
            edit_kwargs["topic"] = data.get("topic")
            edit_kwargs["nsfw"] = data.get("nsfw", False)
            edit_kwargs["slowmode_delay"] = data.get("slowmode_delay", 0)
        await channel.edit(**edit_kwargs)

    def _build_overwrites(
        self, guild: discord.Guild, ow_list: list[dict]
    ) -> dict:
        """將快照中的 allow/deny 權限還原成 Discord PermissionOverwrite。"""
        overwrites: dict = {}
        for ow in ow_list:
            target = (
                guild.get_role(int(ow["id"]))
                if ow["type"] == "role"
                else guild.get_member(int(ow["id"]))
            )
            if target:
                allow = discord.Permissions(int(ow["allow"]))
                deny = discord.Permissions(int(ow["deny"]))
                overwrites[target] = discord.PermissionOverwrite.from_pair(
                    allow, deny
                )
        return overwrites

    # -------------------------------------------------------- 身分組復原

    async def _restore_roles(
        self, guild: discord.Guild, snapshots: list[dict]
    ) -> int:
        """依快照重建或修正身分組設定。"""
        restored = 0
        current = {str(r.id): r for r in guild.roles}

        logger.info(
            "Restoring roles guild=%s snapshots=%s current_roles=%s",
            guild.id,
            len(snapshots),
            len(current),
        )

        for row in snapshots:
            data = json.loads(row["snapshot_data"])
            role_id = data["role_id"]
            existing = current.get(role_id)

            try:
                if existing is None:
                    logger.info(
                        "Recreating missing role guild=%s role_id=%s name=%s",
                        guild.id,
                        role_id,
                        data.get("name"),
                    )
                    await rate_limited_call(guild.create_role,
                        name=data["name"],
                        permissions=discord.Permissions(int(data["permissions"])),
                        color=discord.Color(data["color"]),
                        hoist=data["hoist"],
                        mentionable=data["mentionable"],
                    )
                    logger.info(
                        "Recreated role '%s' in guild %s", data["name"], guild.id
                    )
                else:
                    if existing.is_default():
                        logger.info(
                            "Skipping default role during recovery guild=%s role_id=%s",
                            guild.id,
                            role_id,
                        )
                        continue
                    logger.info(
                        "Updating existing role guild=%s role_id=%s name=%s",
                        guild.id,
                        role_id,
                        data.get("name"),
                    )
                    await rate_limited_call(
                        existing.edit,
                        name=data["name"],
                        permissions=discord.Permissions(int(data["permissions"])),
                        color=discord.Color(data["color"]),
                        hoist=data["hoist"],
                        mentionable=data["mentionable"],
                    )
                restored += 1
                await asyncio.sleep(ROLE_OP_DELAY)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Restore role failed guild=%s role_id=%s name=%s: %s",
                    guild.id,
                    role_id,
                    data.get("name"),
                    exc,
                    exc_info=True,
                )

        return restored

    # -------------------------------------------------------- 訊息復原

    async def _restore_messages(
        self, store, guild: discord.Guild
    ) -> int:
        """將舊頻道加密訊息重送到新頻道，附還原時間標記。"""
        guild_id = str(guild.id)

        # 找出近 5 分鐘內被刪除的頻道
        deleted = await store.fetchall(
            """
            SELECT DISTINCT target_id, old_data FROM temp_cache
            WHERE guild_id = ? AND event_type = 'channel_delete'
              AND timestamp >= (strftime('%s', 'now') - ?)
            """,
            [guild_id, _RECOVERY_LOOKBACK],
        )
        if not deleted:
            logger.info("No deleted channels found for message restore guild=%s", guild_id)
            return 0

        logger.info(
            "Restoring messages guild=%s deleted_channels=%s",
            guild_id,
            len(deleted),
        )

        restored = 0
        for event in deleted:
            old_ch_id = event["target_id"]
            ch_data = json.loads(event["old_data"]) if event["old_data"] else {}
            ch_name = ch_data.get("name", "unknown")

            # 以名稱找回剛重建的新頻道
            new_channel = discord.utils.get(guild.text_channels, name=ch_name)
            if new_channel is None:
                logger.warning("No recreated channel '%s' for message restore", ch_name)
                continue

            # 讀取舊頻道加密訊息
            messages = await store.fetchall(
                """
                SELECT * FROM encrypted_messages
                WHERE channel_id = ? AND guild_id = ?
                ORDER BY timestamp ASC
                """,
                [old_ch_id, guild_id],
            )
            if not messages:
                logger.info(
                    "No stored messages to restore guild=%s old_channel=%s name=%s",
                    guild_id,
                    old_ch_id,
                    ch_name,
                )
                continue

            # 訊息數量上限保護
            messages = messages[-_MAX_RESTORE_MESSAGES:]
            logger.info(
                "Preparing message restore guild=%s old_channel=%s new_channel=%s messages=%s",
                guild_id,
                old_ch_id,
                new_channel.id,
                len(messages),
            )

            # 建立或重用 webhook
            webhook = await self._get_or_create_webhook(new_channel)
            if webhook is None:
                continue

            for msg in messages:
                try:
                    content = decrypt(msg["encrypted_content"], msg["nonce"])
                    ts = msg["timestamp"]
                    await rate_limited_call(
                        webhook.send,
                        content=f"{content}\n\n*(由系統於 <t:{ts}:f> 還原)*",
                        username=msg["author_name"],
                        avatar_url=msg.get("author_avatar"),
                    )
                    restored += 1
                    await asyncio.sleep(_WEBHOOK_SEND_DELAY)
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "Restore message failed guild=%s channel=%s message=%s author=%s: %s",
                        guild_id,
                        new_channel.id,
                        msg["message_id"],
                        msg["author_id"],
                        exc,
                        exc_info=True,
                    )

        return restored

    async def _get_or_create_webhook(
        self, channel: discord.TextChannel
    ) -> discord.Webhook | None:
        """取得既有復原 webhook；不存在時建立新 webhook。"""
        try:
            webhooks = await channel.webhooks()
            webhook = next(
                (w for w in webhooks if w.name == _WEBHOOK_NAME), None
            )
            if webhook is None:
                webhook = await channel.create_webhook(name=_WEBHOOK_NAME)
            return webhook
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Webhook setup failed guild=%s channel=%s name=%s: %s",
                channel.guild.id,
                channel.id,
                channel.name,
                exc,
                exc_info=True,
            )
            return None


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RecoveryCog(bot))

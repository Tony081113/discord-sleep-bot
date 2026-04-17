"""
Recovery cog — Phase 4.

Handles:
1. Recovery execution triggered by an approver button click.
2. Channel and role restoration from snapshots ≥ 5 minutes old.
3. Message restoration via webhooks (original name + avatar).
"""

import asyncio
import json
from typing import Any

import discord
from discord.ext import commands

from mods.crypto import decrypt
from mods.logger import setup_logger
from mods.storage import get_storage

logger = setup_logger(__name__)

_RECOVERY_LOOKBACK = 300  # seconds (5 minutes)
_WEBHOOK_NAME = "SleepBot Recovery"
_MAX_RESTORE_MESSAGES = 100
_WEBHOOK_SEND_DELAY = 0.5  # seconds between webhook sends (rate-limit safety)


class RecoveryCog(commands.Cog, name="Recovery"):
    """Server structure and message recovery."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # -------------------------------------------------------- interaction

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")
        if custom_id.startswith("execute_recovery:"):
            await self._handle_recovery(interaction, custom_id)

    # -------------------------------------------------------- main handler

    async def _handle_recovery(
        self, interaction: discord.Interaction, custom_id: str
    ) -> None:
        parts = custom_id.split(":")
        if len(parts) < 2:
            return
        guild_id = parts[1]
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            await interaction.response.send_message(
                "\u274c 找不到伺服器。", ephemeral=True
            )
            return

        # Verify approver status
        store = get_storage()
        approvers = await store.fetchall(
            "SELECT user_id FROM recovery_approvers WHERE guild_id = ?",
            [guild_id],
        )
        if str(interaction.user.id) not in {r["user_id"] for r in approvers}:
            await interaction.response.send_message(
                "\u274c 你不是已註冊的核准者。", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            ch, ro, ms = await self._execute_recovery(store, guild)
            await interaction.followup.send(
                f"\u2705 復原完成！\n"
                f"\U0001f4c1 頻道復原：{ch}\n"
                f"\U0001f3f7\ufe0f 身分組復原：{ro}\n"
                f"\U0001f4ac 訊息還原：{ms}",
                ephemeral=True,
            )
        except Exception as exc:
            logger.error(
                "Recovery failed for guild %s: %s", guild_id, exc, exc_info=True
            )
            await interaction.followup.send(
                f"\u274c 復原失敗：{exc}", ephemeral=True
            )

    # -------------------------------------------------------- recovery logic

    async def _execute_recovery(
        self, store, guild: discord.Guild
    ) -> tuple[int, int, int]:
        guild_id = str(guild.id)

        channel_snaps = await self._get_pre_attack_snapshots(
            store, guild_id, "channel"
        )
        role_snaps = await self._get_pre_attack_snapshots(
            store, guild_id, "role"
        )

        restored_ch = await self._restore_channels(guild, channel_snaps)
        restored_ro = await self._restore_roles(guild, role_snaps)
        restored_ms = await self._restore_messages(store, guild)

        logger.info(
            "Recovery for guild %s: channels=%d roles=%d messages=%d",
            guild_id, restored_ch, restored_ro, restored_ms,
        )
        return restored_ch, restored_ro, restored_ms

    async def _get_pre_attack_snapshots(
        self, store, guild_id: str, target_type: str
    ) -> list[dict[str, Any]]:
        """Latest snapshot per target from ≥ ``_RECOVERY_LOOKBACK`` ago."""
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

    # -------------------------------------------------------- channel restore

    async def _restore_channels(
        self, guild: discord.Guild, snapshots: list[dict]
    ) -> int:
        restored = 0
        current = {str(ch.id): ch for ch in guild.channels}

        for row in snapshots:
            data = json.loads(row["snapshot_data"])
            channel_id = data["channel_id"]
            existing = current.get(channel_id)

            try:
                if existing is None:
                    await self._recreate_channel(guild, data)
                else:
                    await self._update_channel(guild, existing, data)
                restored += 1
            except Exception as exc:  # noqa: BLE001
                logger.error("Restore channel %s failed: %s", channel_id, exc)

        return restored

    async def _recreate_channel(
        self, guild: discord.Guild, data: dict
    ) -> None:
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

    # -------------------------------------------------------- role restore

    async def _restore_roles(
        self, guild: discord.Guild, snapshots: list[dict]
    ) -> int:
        restored = 0
        current = {str(r.id): r for r in guild.roles}

        for row in snapshots:
            data = json.loads(row["snapshot_data"])
            role_id = data["role_id"]
            existing = current.get(role_id)

            try:
                if existing is None:
                    await guild.create_role(
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
                        continue
                    await existing.edit(
                        name=data["name"],
                        permissions=discord.Permissions(int(data["permissions"])),
                        color=discord.Color(data["color"]),
                        hoist=data["hoist"],
                        mentionable=data["mentionable"],
                    )
                restored += 1
            except Exception as exc:  # noqa: BLE001
                logger.error("Restore role %s failed: %s", role_id, exc)

        return restored

    # -------------------------------------------------------- message restore

    async def _restore_messages(
        self, store, guild: discord.Guild
    ) -> int:
        guild_id = str(guild.id)

        # Channels deleted in the last 5 minutes
        deleted = await store.fetchall(
            """
            SELECT DISTINCT target_id, old_data FROM temp_cache
            WHERE guild_id = ? AND event_type = 'channel_delete'
              AND timestamp >= (strftime('%s', 'now') - ?)
            """,
            [guild_id, _RECOVERY_LOOKBACK],
        )
        if not deleted:
            return 0

        restored = 0
        for event in deleted:
            old_ch_id = event["target_id"]
            ch_data = json.loads(event["old_data"]) if event["old_data"] else {}
            ch_name = ch_data.get("name", "unknown")

            # Find the newly-recreated channel by name
            new_channel = discord.utils.get(guild.text_channels, name=ch_name)
            if new_channel is None:
                logger.warning("No recreated channel '%s' for message restore", ch_name)
                continue

            # Fetch encrypted messages for the old channel
            messages = await store.fetchall(
                """
                SELECT * FROM encrypted_messages
                WHERE channel_id = ? AND guild_id = ?
                ORDER BY timestamp ASC
                """,
                [old_ch_id, guild_id],
            )
            if not messages:
                continue

            # Cap message count
            messages = messages[-_MAX_RESTORE_MESSAGES:]

            # Create or reuse webhook
            webhook = await self._get_or_create_webhook(new_channel)
            if webhook is None:
                continue

            for msg in messages:
                try:
                    content = decrypt(msg["encrypted_content"], msg["nonce"])
                    ts = msg["timestamp"]
                    await webhook.send(
                        content=f"{content}\n\n*(由系統於 <t:{ts}:f> 還原)*",
                        username=msg["author_name"],
                        avatar_url=msg.get("author_avatar"),
                    )
                    restored += 1
                    await asyncio.sleep(_WEBHOOK_SEND_DELAY)
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "Restore message %s failed: %s", msg["message_id"], exc
                    )

        return restored

    async def _get_or_create_webhook(
        self, channel: discord.TextChannel
    ) -> discord.Webhook | None:
        try:
            webhooks = await channel.webhooks()
            webhook = next(
                (w for w in webhooks if w.name == _WEBHOOK_NAME), None
            )
            if webhook is None:
                webhook = await channel.create_webhook(name=_WEBHOOK_NAME)
            return webhook
        except Exception as exc:  # noqa: BLE001
            logger.error("Webhook setup in %s failed: %s", channel.name, exc)
            return None


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RecoveryCog(bot))

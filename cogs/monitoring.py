"""
Monitoring cog — Phase 3.

Handles:
1. Encrypted message persistence (on_message).
2. Structure change detection (channels, roles).
3. Anomaly detection and alerting to recovery approvers.
"""

import json
from typing import Optional

import discord
from discord.ext import commands

from mods.crypto import encrypt
from mods.logger import setup_logger
from mods.storage import get_storage

logger = setup_logger(__name__)

# ---------------------------------------------------------------------------
# Anomaly detection thresholds  (events within a 5-minute window)
# ---------------------------------------------------------------------------

_ANOMALY_WINDOW = 300  # seconds

_THRESHOLD: dict[str, int] = {
    "channel_delete": 3,
    "channel_update": 5,
    "role_delete": 3,
    "role_update": 5,
    "admin_perm_remove": 2,
}

_EVENT_LABELS: dict[str, str] = {
    "channel_delete": "大量頻道被刪除",
    "role_delete": "大量身分組被刪除",
    "admin_perm_remove": "管理員權限被移除",
    "channel_update": "大量頻道被修改",
    "role_update": "大量身分組被修改",
}


# ---------------------------------------------------------------------------
# Serialisation helpers (mirrors onboarding.py)
# ---------------------------------------------------------------------------

def _channel_to_dict(channel: discord.abc.GuildChannel) -> dict:
    data = {
        "channel_id": str(channel.id),
        "name": channel.name,
        "type": channel.type.value,
        "position": channel.position,
        "parent_id": str(channel.category_id) if channel.category_id else None,
    }
    overwrites = []
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        overwrites.append({
            "id": str(target.id),
            "type": "role" if isinstance(target, discord.Role) else "member",
            "allow": str(allow.value),
            "deny": str(deny.value),
        })
    data["permission_overwrites"] = overwrites
    if isinstance(channel, discord.TextChannel):
        data["topic"] = channel.topic
        data["nsfw"] = channel.nsfw
        data["slowmode_delay"] = channel.slowmode_delay
    return data


def _role_to_dict(role: discord.Role) -> dict:
    return {
        "role_id": str(role.id),
        "name": role.name,
        "permissions": str(role.permissions.value),
        "position": role.position,
        "color": role.color.value,
        "hoist": role.hoist,
        "mentionable": role.mentionable,
    }


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class MonitoringCog(commands.Cog, name="Monitoring"):
    """Message encryption, structure monitoring, and anomaly detection."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ----------------------------------------------------------- on_message

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Encrypt and store every non-bot guild message."""
        if message.author.bot or message.guild is None or not message.content:
            return

        store = get_storage()
        guild_id = str(message.guild.id)
        encrypted_content, nonce = encrypt(message.content)
        avatar_url = (
            message.author.display_avatar.url
            if message.author.display_avatar
            else None
        )

        try:
            await store.execute(
                """
                INSERT OR IGNORE INTO encrypted_messages
                    (message_id, channel_id, guild_id, author_id,
                     author_name, author_avatar, encrypted_content, nonce)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    str(message.id),
                    str(message.channel.id),
                    guild_id,
                    str(message.author.id),
                    message.author.display_name,
                    avatar_url,
                    encrypted_content,
                    nonce,
                ],
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to store message %s: %s", message.id, exc)

    # ------------------------------------------------------ channel events

    @commands.Cog.listener()
    async def on_guild_channel_delete(
        self, channel: discord.abc.GuildChannel
    ) -> None:
        guild_id = str(channel.guild.id)
        store = get_storage()
        await self._record_event(
            store, guild_id, "channel_delete", str(channel.id),
            json.dumps(_channel_to_dict(channel), ensure_ascii=False), None,
        )
        await self._check_and_alert(store, channel.guild, "channel_delete")

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel,
    ) -> None:
        guild_id = str(after.guild.id)
        store = get_storage()
        old_data = json.dumps(_channel_to_dict(before), ensure_ascii=False)
        new_data = json.dumps(_channel_to_dict(after), ensure_ascii=False)

        await self._record_event(
            store, guild_id, "channel_update", str(after.id), old_data, new_data,
        )

        # New snapshot
        await store.execute(
            "INSERT INTO structure_snapshots "
            "(guild_id, target_type, target_id, snapshot_data) VALUES (?, ?, ?, ?)",
            [guild_id, "channel", str(after.id), new_data],
        )

        # Update channels table
        await store.execute(
            """
            INSERT INTO channels (channel_id, guild_id, name, type, position, parent_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                name = excluded.name, type = excluded.type,
                position = excluded.position, parent_id = excluded.parent_id
            """,
            [
                str(after.id), guild_id, after.name,
                after.type.value, after.position,
                str(after.category_id) if after.category_id else None,
            ],
        )

    # --------------------------------------------------------- role events

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        guild_id = str(role.guild.id)
        store = get_storage()
        await self._record_event(
            store, guild_id, "role_delete", str(role.id),
            json.dumps(_role_to_dict(role), ensure_ascii=False), None,
        )
        await self._check_and_alert(store, role.guild, "role_delete")

    @commands.Cog.listener()
    async def on_guild_role_update(
        self, before: discord.Role, after: discord.Role
    ) -> None:
        guild_id = str(after.guild.id)
        store = get_storage()
        old_data = json.dumps(_role_to_dict(before), ensure_ascii=False)
        new_data = json.dumps(_role_to_dict(after), ensure_ascii=False)

        await self._record_event(
            store, guild_id, "role_update", str(after.id), old_data, new_data,
        )

        # Admin permission removal → extra event type
        if before.permissions.administrator and not after.permissions.administrator:
            await self._record_event(
                store, guild_id, "admin_perm_remove", str(after.id),
                old_data, new_data,
            )
            await self._check_and_alert(store, after.guild, "admin_perm_remove")

        # New snapshot
        await store.execute(
            "INSERT INTO structure_snapshots "
            "(guild_id, target_type, target_id, snapshot_data) VALUES (?, ?, ?, ?)",
            [guild_id, "role", str(after.id), new_data],
        )

        # Update roles table
        await store.execute(
            """
            INSERT INTO roles
                (role_id, guild_id, name, permissions, position, color, hoist, mentionable)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(role_id) DO UPDATE SET
                name = excluded.name, permissions = excluded.permissions,
                position = excluded.position, color = excluded.color,
                hoist = excluded.hoist, mentionable = excluded.mentionable
            """,
            [
                str(after.id), guild_id, after.name,
                str(after.permissions.value), after.position,
                after.color.value, int(after.hoist), int(after.mentionable),
            ],
        )

    # ------------------------------------------------------- member events

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        """Detect admin permission loss via role changes on a member."""
        if (
            before.guild_permissions.administrator
            and not after.guild_permissions.administrator
        ):
            guild_id = str(after.guild.id)
            store = get_storage()
            await self._record_event(
                store, guild_id, "admin_perm_remove", str(after.id),
                json.dumps({"user_id": str(after.id), "had_admin": True}, ensure_ascii=False),
                json.dumps({"user_id": str(after.id), "had_admin": False}, ensure_ascii=False),
            )
            await self._check_and_alert(store, after.guild, "admin_perm_remove")

    # ------------------------------------------------------------ helpers

    async def _record_event(
        self,
        store,
        guild_id: str,
        event_type: str,
        target_id: str,
        old_data: Optional[str],
        new_data: Optional[str],
    ) -> None:
        try:
            await store.execute(
                """
                INSERT INTO temp_cache
                    (guild_id, event_type, target_id, old_data, new_data)
                VALUES (?, ?, ?, ?, ?)
                """,
                [guild_id, event_type, target_id, old_data, new_data],
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to record event %s: %s", event_type, exc)

    async def _check_and_alert(
        self, store, guild: discord.Guild, event_type: str
    ) -> None:
        guild_id = str(guild.id)
        threshold = _THRESHOLD.get(event_type, 999)

        try:
            rows = await store.fetchall(
                """
                SELECT COUNT(*) AS cnt FROM temp_cache
                WHERE guild_id = ? AND event_type = ?
                  AND timestamp >= (strftime('%s', 'now') - ?)
                """,
                [guild_id, event_type, _ANOMALY_WINDOW],
            )
            count = rows[0]["cnt"] if rows else 0
        except Exception as exc:  # noqa: BLE001
            logger.error("Anomaly check failed: %s", exc)
            return

        if count < threshold:
            return

        logger.warning(
            "ANOMALY in guild %s: %s count=%d (threshold=%d)",
            guild_id, event_type, count, threshold,
        )
        await self._alert_approvers(store, guild, event_type, count)

    async def _alert_approvers(
        self, store, guild: discord.Guild, event_type: str, count: int
    ) -> None:
        guild_id = str(guild.id)
        try:
            approvers = await store.fetchall(
                "SELECT user_id FROM recovery_approvers WHERE guild_id = ?",
                [guild_id],
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to fetch approvers: %s", exc)
            return

        if not approvers:
            logger.warning("No approvers registered for guild %s", guild_id)
            return

        label = _EVENT_LABELS.get(event_type, event_type)
        embed = discord.Embed(
            title="\U0001f6a8 伺服器異常警報",
            description=(
                f"**{guild.name}** 偵測到異常活動：\n\n"
                f"\U0001f4cb 類型：**{label}**\n"
                f"\U0001f4ca 5 分鐘內事件數：**{count}**\n\n"
                f"如需將伺服器復原至 5 分鐘前的狀態，請點擊下方按鈕。"
            ),
            color=discord.Color.red(),
        )

        view = discord.ui.View(timeout=1800)  # 30 minutes
        recovery_btn = discord.ui.Button(
            label="執行復原",
            style=discord.ButtonStyle.danger,
            custom_id=f"execute_recovery:{guild_id}",
            emoji="\U0001f504",
        )

        async def _recovery_callback(interaction: discord.Interaction) -> None:
            # Delegate to recovery cog via on_interaction
            cog = self.bot.get_cog("Recovery")
            if cog:
                await cog._handle_recovery(interaction, f"execute_recovery:{guild_id}")
            else:
                await interaction.response.send_message(
                    "\u274c 復原模組未載入。", ephemeral=True
                )

        recovery_btn.callback = _recovery_callback
        view.add_item(recovery_btn)

        sent = 0
        for row in approvers:
            user = self.bot.get_user(int(row["user_id"]))
            if user is None:
                try:
                    user = await self.bot.fetch_user(int(row["user_id"]))
                except discord.NotFound:
                    continue
            try:
                await user.send(embed=embed, view=view)
                sent += 1
            except discord.Forbidden:
                logger.warning("Cannot DM approver %s", row["user_id"])

        logger.info(
            "Anomaly alert sent to %d/%d approvers (guild=%s)",
            sent, len(approvers), guild_id,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MonitoringCog(bot))

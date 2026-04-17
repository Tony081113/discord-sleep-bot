"""
Onboarding cog — Phase 2.

Handles:
1. Initial structure snapshot when the bot joins a guild.
2. Admin approval DM flow with interactive buttons.
3. Fallback channel message if DM delivery fails.
"""

import json

import discord
from discord.ext import commands

from mods.logger import setup_logger
from mods.storage import get_storage

logger = setup_logger(__name__)

# Discord official DM troubleshooting page
_DM_HELP_URL = "https://support.discord.com/hc/en-us/articles/217916488"


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _channel_to_dict(channel: discord.abc.GuildChannel) -> dict:
    """Serialise a channel into a JSON-safe dict for snapshot storage."""
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
    """Serialise a role into a JSON-safe dict for snapshot storage."""
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

class OnboardingCog(commands.Cog, name="Onboarding"):
    """Guild join initialisation and admin approval flow."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ---------------------------------------------------------------- events

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Scan the guild, store a snapshot, and request admin approval."""
        logger.info("Joined guild '%s' (id=%s)", guild.name, guild.id)
        store = get_storage()
        guild_id = str(guild.id)

        # 1. Upsert guild record
        await store.execute(
            """
            INSERT INTO guilds (guild_id, name, owner_id)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                name     = excluded.name,
                owner_id = excluded.owner_id
            """,
            [guild_id, guild.name, str(guild.owner_id)],
        )

        # 2. Store channels + snapshots
        for channel in guild.channels:
            ch_data = _channel_to_dict(channel)
            await store.execute(
                """
                INSERT INTO channels (channel_id, guild_id, name, type, position, parent_id)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    name = excluded.name, type = excluded.type,
                    position = excluded.position, parent_id = excluded.parent_id
                """,
                [
                    str(channel.id), guild_id, channel.name,
                    channel.type.value, channel.position,
                    str(channel.category_id) if channel.category_id else None,
                ],
            )
            await store.execute(
                "INSERT INTO structure_snapshots (guild_id, target_type, target_id, snapshot_data) VALUES (?, ?, ?, ?)",
                [guild_id, "channel", str(channel.id), json.dumps(ch_data, ensure_ascii=False)],
            )

        # 3. Store roles + snapshots
        for role in guild.roles:
            r_data = _role_to_dict(role)
            await store.execute(
                """
                INSERT INTO roles (role_id, guild_id, name, permissions, position, color, hoist, mentionable)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(role_id) DO UPDATE SET
                    name = excluded.name, permissions = excluded.permissions,
                    position = excluded.position, color = excluded.color,
                    hoist = excluded.hoist, mentionable = excluded.mentionable
                """,
                [
                    str(role.id), guild_id, role.name,
                    str(role.permissions.value), role.position,
                    role.color.value, int(role.hoist), int(role.mentionable),
                ],
            )
            await store.execute(
                "INSERT INTO structure_snapshots (guild_id, target_type, target_id, snapshot_data) VALUES (?, ?, ?, ?)",
                [guild_id, "role", str(role.id), json.dumps(r_data, ensure_ascii=False)],
            )

        logger.info(
            "Initial snapshot stored for '%s': %d channels, %d roles",
            guild.name, len(guild.channels), len(guild.roles),
        )

        # 4. Admin approval DMs
        admins = [
            m for m in guild.members
            if m.guild_permissions.administrator and not m.bot
        ]
        logger.info("Found %d administrator(s) in '%s'", len(admins), guild.name)

        for admin in admins:
            await self._send_approval_dm(admin, guild)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Fallback handler for button interactions (survives bot restarts)."""
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")
        if custom_id.startswith("accept_approver:"):
            await self._handle_accept_approver(interaction, custom_id)
        elif custom_id.startswith("retry_dm:"):
            await self._handle_retry_dm(interaction, custom_id)

    # ------------------------------------------------------------- helpers

    async def _send_approval_dm(
        self, member: discord.Member, guild: discord.Guild
    ) -> None:
        """Send a DM asking the admin to accept the recovery approver role."""
        guild_id = str(guild.id)

        view = discord.ui.View(timeout=3600)  # 1 hour
        btn = discord.ui.Button(
            label="接受擔任核准者",
            style=discord.ButtonStyle.success,
            custom_id=f"accept_approver:{guild_id}",
        )

        async def _accept_callback(interaction: discord.Interaction) -> None:
            await self._handle_accept_approver(
                interaction, f"accept_approver:{guild_id}"
            )

        btn.callback = _accept_callback
        view.add_item(btn)

        embed = discord.Embed(
            title="\U0001f6e1\ufe0f 伺服器復原核准者邀請",
            description=(
                f"**{guild.name}** 已啟用伺服器結構保護。\n"
                f"身為管理員，你被邀請擔任「復原核准者」。\n\n"
                f"當偵測到異常（如大量頻道被刪除、權限被竄改），\n"
                f"你將收到警報並可一鍵同意執行復原。\n\n"
                f"\u23f0 此邀請將在 **1 小時後** 過期。"
            ),
            color=discord.Color.blue(),
        )

        try:
            await member.send(embed=embed, view=view)
            logger.info("Sent approval DM to %s (guild=%s)", member, guild.name)
        except discord.Forbidden:
            logger.warning(
                "Cannot DM %s — sending fallback in guild channel", member
            )
            await self._send_fallback_channel_message(guild, member)

    async def _send_fallback_channel_message(
        self, guild: discord.Guild, member: discord.Member
    ) -> None:
        """Send a fallback message in the guild when DM cannot be delivered."""
        channel = guild.system_channel or next(
            (
                ch
                for ch in guild.text_channels
                if ch.permissions_for(guild.me).send_messages
            ),
            None,
        )
        if channel is None:
            logger.error(
                "No text channel available in guild '%s' for fallback", guild.name
            )
            return

        view = discord.ui.View(timeout=None)

        retry_btn = discord.ui.Button(
            label="重新嘗試發送 DM",
            style=discord.ButtonStyle.primary,
            custom_id=f"retry_dm:{guild.id}:{member.id}",
        )

        async def _retry_callback(interaction: discord.Interaction) -> None:
            await self._handle_retry_dm(
                interaction, f"retry_dm:{guild.id}:{member.id}"
            )

        retry_btn.callback = _retry_callback
        view.add_item(retry_btn)

        view.add_item(
            discord.ui.Button(
                label="為什麼我收不到私訊？",
                style=discord.ButtonStyle.link,
                url=_DM_HELP_URL,
            )
        )

        await channel.send(
            f"\u26a0\ufe0f 無法向 {member.mention} 發送私訊。\n"
            f"請先開啟伺服器的私訊設定，然後點選下方按鈕重新嘗試。",
            view=view,
        )

    # -------------------------------------------------------- button handlers

    async def _handle_accept_approver(
        self, interaction: discord.Interaction, custom_id: str
    ) -> None:
        parts = custom_id.split(":")
        if len(parts) < 2:
            return
        guild_id = parts[1]
        user_id = str(interaction.user.id)

        store = get_storage()
        await store.execute(
            """
            INSERT INTO recovery_approvers (guild_id, user_id)
            VALUES (?, ?)
            ON CONFLICT(guild_id, user_id) DO NOTHING
            """,
            [guild_id, user_id],
        )

        await interaction.response.send_message(
            "\u2705 你已成功接受擔任復原核准者！當偵測到異常時，你將收到警報通知。",
            ephemeral=True,
        )
        logger.info(
            "User %s accepted approver role for guild %s", user_id, guild_id
        )

    async def _handle_retry_dm(
        self, interaction: discord.Interaction, custom_id: str
    ) -> None:
        parts = custom_id.split(":")
        if len(parts) < 3:
            return
        guild_id, member_id = parts[1], parts[2]
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            await interaction.response.send_message(
                "\u274c 找不到伺服器。", ephemeral=True
            )
            return
        member = guild.get_member(int(member_id))
        if member is None:
            await interaction.response.send_message(
                "\u274c 找不到該成員。", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await self._send_approval_dm(member, guild)
            await interaction.followup.send(
                "\u2705 已重新發送私訊！", ephemeral=True
            )
        except Exception as exc:
            await interaction.followup.send(
                f"\u274c 發送失敗：{exc}", ephemeral=True
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OnboardingCog(bot))

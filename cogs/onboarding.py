"""導入模組（Phase 2）。

職責：
1. Bot 加入伺服器時建立初始結構快照。
2. 提供管理員核准者的私訊互動流程。
3. 私訊失敗時提供伺服器內備援通知。
4. 提供管理員手動接受核准者指令。
"""

import asyncio
import json

import discord
from discord import app_commands
from discord.ext import commands

from mods.logger import setup_logger
from mods.rate_limit import dm_sleep
from mods.storage import get_storage

logger = setup_logger(__name__)

# Discord 官方私訊疑難排解頁面
_DM_HELP_URL = "https://support.discord.com/hc/en-us/articles/217916488"


# ---------------------------------------------------------------------------
# 序列化工具
# ---------------------------------------------------------------------------

def _channel_to_dict(channel: discord.abc.GuildChannel) -> dict:
    """將頻道轉成可安全寫入 JSON 的快照資料。"""
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
    """將身分組轉成可安全寫入 JSON 的快照資料。"""
    return {
        "role_id": str(role.id),
        "name": role.name,
        "permissions": str(role.permissions.value),
        "position": role.position,
        "color": role.color.value,
        "hoist": role.hoist,
        "mentionable": role.mentionable,
        "members": [str(m.id) for m in role.members],
    }


def _guild_to_dict(guild: discord.Guild) -> dict:
    """將伺服器名稱/縮圖/橫幅轉為快照資料。"""
    return {
        "guild_id": str(guild.id),
        "name": guild.name,
        "icon_url": str(guild.icon.url) if guild.icon else None,
        "banner_url": str(guild.banner.url) if guild.banner else None,
    }


def _member_to_dict(member: discord.Member) -> dict:
    """將成員暱稱快照化，供還原時復原 nick。"""
    return {
        "user_id": str(member.id),
        "nick": member.nick,
    }


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class OnboardingCog(commands.Cog, name="Onboarding"):
    """處理入群初始化與管理員核准流程。"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------- slash commands

    # -------------------------------------------------------- DM verify helpers

    async def _send_verify_dm(
        self, user: discord.User, guild_id: str
    ) -> bool:
        """送出含驗證按鈕的私訊；成功送達回傳 True。"""
        embed = discord.Embed(
            title="🔔 復原核准者驗證",
            description=(
                "你正在申請成為 **SleepBot** 的復原核准者。\n\n"
                "點擊下方按鈕即可完成驗證。日後伺服器偵測到異常時，"
                "復原請求會透過此私訊管道通知你。"
            ),
            color=discord.Color.blurple(),
        )
        view = discord.ui.View(timeout=None)
        btn = discord.ui.Button(
            label="✅ 確認成為核准者",
            style=discord.ButtonStyle.success,
            custom_id=f"verify_approver:{guild_id}:{user.id}",
        )
        view.add_item(btn)
        try:
            await user.send(embed=embed, view=view)
            return True
        except discord.Forbidden:
            return False
        except discord.HTTPException:
            return False

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """集中處理 onboarding 相關按鈕互動入口。"""
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")

        if custom_id.startswith("verify_approver:"):
            await self._handle_verify_approver(interaction, custom_id)
        elif custom_id.startswith("resend_approver_dm:"):
            await self._handle_resend_approver_dm(interaction, custom_id)
        elif custom_id.startswith("accept_approver:"):
            await self._handle_accept_approver(interaction, custom_id)
        elif custom_id.startswith("retry_dm:"):
            await self._handle_retry_dm(interaction, custom_id)

    async def _handle_verify_approver(
        self, interaction: discord.Interaction, custom_id: str
    ) -> None:
        """當使用者點擊私訊驗證按鈕時，註冊為核准者。"""
        parts = custom_id.split(":")
        if len(parts) < 3:
            logger.warning("verify_approver: invalid custom_id=%s", custom_id)
            return
        guild_id, user_id = parts[1], parts[2]
        logger.info(
            "verify_approver: guild=%s user=%s interactor=%s",
            guild_id, user_id, interaction.user.id,
        )
        if str(interaction.user.id) != user_id:
            await interaction.response.send_message("❌ 這個按鈕不屬於你。", ephemeral=True)
            return
        try:
            await interaction.response.defer()
            store = get_storage()
            await store.execute(
                """
                INSERT INTO recovery_approvers (guild_id, user_id)
                VALUES (?, ?)
                ON CONFLICT(guild_id, user_id) DO NOTHING
                """,
                [guild_id, user_id],
            )
            # 驗證成功後嘗試編輯原訊息，避免按鈕被重複點擊。
            try:
                await interaction.message.edit(
                    content="✅ 驗證成功！你已成為此伺服器的復原核准者。\n日後復原請求將透過私訊通知你。",
                    embed=None,
                    view=discord.ui.View(),
                )
            except Exception as edit_exc:
                logger.warning("Could not edit verify DM message: %s", edit_exc)
            await interaction.followup.send(
                "✅ 驗證成功！你已成為此伺服器的復原核准者。",
            )
            logger.info("User %s verified as approver for guild %s via DM button", user_id, guild_id)
        except Exception as exc:
            logger.exception("Failed to verify approver: %s", exc)
            try:
                await interaction.followup.send("❌ 驗證失敗，請稍後重試。", ephemeral=True)
            except Exception:
                pass
    async def _handle_resend_approver_dm(
        self, interaction: discord.Interaction, custom_id: str
    ) -> None:
        """點擊重新發送按鈕時，補發驗證私訊。"""
        parts = custom_id.split(":")
        if len(parts) < 3:
            return
        guild_id, user_id = parts[1], parts[2]
        if str(interaction.user.id) != user_id:
            await interaction.response.send_message("❌ 這個按鈕不屬於你。", ephemeral=True)
            return
        sent = await self._send_verify_dm(interaction.user, guild_id)
        if sent:
            await interaction.response.send_message(
                "📨 已重新發送驗證私訊，請查看 DM 並點擊按鈕完成驗證。",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "❌ 仍然無法發送私訊，請確認已開啟「允許伺服器成員傳送私訊」後再試。",
                ephemeral=True,
            )

    # -------------------------------------------------------- slash commands

    @app_commands.command(name="accept-approver", description="接受『復原核准者』角色")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def accept_approver(self, interaction: discord.Interaction) -> None:
        """允許管理員手動接受復原核准者身份。"""
        try:
            guild_id = str(interaction.guild_id)
            user_id = str(interaction.user.id)
            store = get_storage()
            guild = interaction.guild

            await interaction.response.defer(ephemeral=True)

            # 先確保 guild 基本資料存在，後續查詢才有一致主檔。
            if guild:
                await store.execute(
                    """
                    INSERT INTO guilds (guild_id, name, owner_id)
                    VALUES (?, ?, ?)
                    ON CONFLICT(guild_id) DO UPDATE SET name = excluded.name
                    """,
                    [guild_id, guild.name, str(guild.owner_id) if guild.owner_id else user_id],
                )

            # 已是核准者就直接返回，避免重複建立紀錄。
            existing = await store.fetchone(
                "SELECT 1 FROM recovery_approvers WHERE guild_id = ? AND user_id = ?",
                [guild_id, user_id],
            )
            if existing:
                await interaction.followup.send(
                    "ℹ️ 你已經是此伺服器的復原核准者了，無需重複驗證。",
                    ephemeral=True,
                )
                return

            # 以私訊完成核准者身分驗證。
            sent = await self._send_verify_dm(interaction.user, guild_id)

            if sent:
                # 私訊成功時同時提供「重送」按鈕，降低遺漏訊息情境。
                view = discord.ui.View(timeout=None)
                resend_btn = discord.ui.Button(
                    label="📨 沒收到？重新發送",
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"resend_approver_dm:{guild_id}:{user_id}",
                )
                view.add_item(resend_btn)
                await interaction.followup.send(
                    "📩 已發送驗證私訊！請查看 DM 並點擊按鈕完成驗證。\n"
                    "若沒有收到私訊，請點擊下方按鈕重試。",
                    view=view,
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "❌ 無法向你發送私訊，請先開啟「允許伺服器成員傳送私訊」後再重試。",
                    ephemeral=True,
                )
            logger.info(
                "User %s requested approver verification for guild %s (dm_sent=%s)",
                user_id, guild_id, sent,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to accept approver role: %s", exc)
            try:
                await interaction.followup.send(
                    "❌ 無法接受核准者角色，請稍後重試或聯繫系統管理員。",
                    ephemeral=True,
                )
            except Exception:
                pass

    @app_commands.command(name="remove-approver", description="（測試用）移除自己的復原核准者身份")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def remove_approver(self, interaction: discord.Interaction) -> None:
        """允許管理員移除自己的核准者身份（測試用途）。"""
        try:
            guild_id = str(interaction.guild_id)
            user_id = str(interaction.user.id)
            store = get_storage()

            existing = await store.fetchone(
                "SELECT 1 FROM recovery_approvers WHERE guild_id = ? AND user_id = ?",
                [guild_id, user_id],
            )

            if existing:
                await store.execute(
                    "DELETE FROM recovery_approvers WHERE guild_id = ? AND user_id = ?",
                    [guild_id, user_id],
                )

            if existing:
                await interaction.response.send_message(
                    "\u2705 已移除你的復原核准者身份。",
                    ephemeral=True,
                )
                logger.info(
                    "User %s removed approver role for guild %s via /remove-approver",
                    user_id,
                    guild_id,
                )
            else:
                await interaction.response.send_message(
                    "\u2139\ufe0f 你目前不是此伺服器的核准者，無需移除。",
                    ephemeral=True,
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to remove approver role: %s", exc)
            await interaction.response.send_message(
                "\u274c 移除失敗，請稍後重試或聯繫系統管理員。",
                ephemeral=True,
            )

    # ---------------------------------------------------------------- events

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        """掃描伺服器、保存初始快照，並發起管理員核准流程。"""
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
        await store.execute(
            "INSERT INTO structure_snapshots (guild_id, target_type, target_id, snapshot_data) "
            "VALUES (?, ?, ?, ?)",
            [guild_id, "guild", guild_id, json.dumps(_guild_to_dict(guild), ensure_ascii=False)],
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

        # 3.5 Store member nick snapshots
        for member in guild.members:
            m_data = _member_to_dict(member)
            await store.execute(
                "INSERT INTO structure_snapshots (guild_id, target_type, target_id, snapshot_data) VALUES (?, ?, ?, ?)",
                [guild_id, "member", str(member.id), json.dumps(m_data, ensure_ascii=False)],
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
            await dm_sleep()

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """機器人離開或被踢出伺服器時，清除所有相關資料。"""
        guild_id = str(guild.id)
        logger.info("Removed from guild '%s' (id=%s) — purging data", guild.name, guild_id)
        store = get_storage()
        try:
            # guilds 表的 ON DELETE CASCADE 會自動清除所有外鍵關聯資料。
            await store.execute(
                "DELETE FROM guilds WHERE guild_id = ?",
                [guild_id],
            )
            # temp_cache 與 maintenance_logs 無外鍵約束，須手動刪除。
            await store.execute(
                "DELETE FROM temp_cache WHERE guild_id = ?",
                [guild_id],
            )
            await store.execute(
                "DELETE FROM maintenance_logs WHERE guild_id = ?",
                [guild_id],
            )
            logger.info("Purged all data for guild=%s", guild_id)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to purge data for guild=%s: %s",
                guild_id,
                exc,
                exc_info=True,
            )

    # ------------------------------------------------------------- helpers

    async def _send_approval_dm(
        self, member: discord.Member, guild: discord.Guild
    ) -> None:
        """發送邀請私訊，請管理員接受核准者角色。"""
        guild_id = str(guild.id)

        view = discord.ui.View(timeout=3600)  # 1 hour
        btn = discord.ui.Button(
            label="接受擔任核准者",
            style=discord.ButtonStyle.success,
            custom_id=f"accept_approver:{guild_id}",
        )
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
            logger.info("✓ Sent approval DM to %s (guild=%s)", member, guild.name)
        except discord.Forbidden as e:
            logger.warning(
                "⚠ Cannot DM %s (Forbidden) — sending fallback in guild channel (guild=%s)",
                member, guild.name
            )
            await self._send_fallback_channel_message(guild, member)
        except discord.HTTPException as e:
            logger.warning(
                "⚠ Cannot DM %s (HTTPException: %s) — sending fallback (guild=%s)",
                member, e, guild.name
            )
            await self._send_fallback_channel_message(guild, member)
        except Exception as e:
            logger.error(
                "✗ Unexpected error sending DM to %s: %s (guild=%s)",
                member, e, guild.name, exc_info=True
            )
            await self._send_fallback_channel_message(guild, member)

    async def _send_fallback_channel_message(
        self, guild: discord.Guild, member: discord.Member
    ) -> None:
        """當私訊失敗時，改在伺服器可發話頻道提示管理員。"""
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
        """處理邀請按鈕：將使用者寫入 recovery_approvers。"""
        async def _send_ephemeral(content: str) -> None:
            if interaction.response.is_done():
                await interaction.followup.send(content, ephemeral=True)
            else:
                await interaction.response.send_message(content, ephemeral=True)

        parts = custom_id.split(":")
        if len(parts) < 2:
            return
        guild_id = parts[1]
        user_id = str(interaction.user.id)

        store = get_storage()
        existing = await store.fetchone(
            "SELECT 1 FROM recovery_approvers WHERE guild_id = ? AND user_id = ?",
            [guild_id, user_id],
        )

        # 申請已收到（或已通過）時：收回按鈕；若仍被點擊則回覆已通過。
        if existing:
            try:
                if interaction.message is not None:
                    await interaction.message.edit(view=discord.ui.View())
            except Exception:
                pass
            await _send_ephemeral("✅ 已收到並通過申請。")
            return

        await store.execute(
            """
            INSERT INTO recovery_approvers (guild_id, user_id)
            VALUES (?, ?)
            ON CONFLICT(guild_id, user_id) DO NOTHING
            """,
            [guild_id, user_id],
        )

        try:
            if interaction.message is not None:
                await interaction.message.edit(view=discord.ui.View())
        except Exception:
            pass

        await _send_ephemeral(
            "\u2705 你已成功接受擔任復原核准者！當偵測到異常時，你將收到警報通知。",
        )
        logger.info(
            "User %s accepted approver role for guild %s", user_id, guild_id
        )

    async def _handle_retry_dm(
        self, interaction: discord.Interaction, custom_id: str
    ) -> None:
        """處理重送按鈕：重新嘗試發送核准邀請私訊。"""
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

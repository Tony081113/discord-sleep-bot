"""系統管理斜線指令。

提供：
- /ping   ：顯示 Discord API 延遲
- /status ：顯示 Redis / D1 連線與延遲
- /panel  ：顯示管理面板網址
- /defense：切換防禦系統啟停
- /message-log-status：顯示訊息保留狀態
"""

from time import perf_counter
import json
import os

import discord
from discord import app_commands
from discord.ext import commands

from mods.defense import (
    DefenseStorageError,
    DEFAULT_DISABLE_SECONDS,
    get_defense_state,
    set_defense_disabled,
    set_defense_enabled,
)
from mods.logger import setup_logger
from mods.storage import get_storage

logger = setup_logger(__name__)

_MESSAGE_RETENTION_SECONDS = 14 * 24 * 60 * 60
_MESSAGE_MAX_PER_GUILD = 50000


class SystemCommandsCog(commands.Cog, name="SystemCommands"):
    """營運與維運用斜線指令。"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="ping", description="顯示 API 延遲")
    async def ping(self, interaction: discord.Interaction) -> None:
        """顯示 WebSocket 與互動回應延遲。"""
        # 先 defer，避免互動逾時並可精準計算互動回應耗時。
        start = perf_counter()
        await interaction.response.defer(ephemeral=True)
        interaction_ms = (perf_counter() - start) * 1000
        ws_ms = self.bot.latency * 1000

        embed = discord.Embed(title="Pong!", color=discord.Color.green())
        embed.add_field(name="Gateway 延遲", value=f"{ws_ms:.1f} ms", inline=True)
        embed.add_field(name="Interaction 延遲", value=f"{interaction_ms:.1f} ms", inline=True)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="status", description="顯示 Redis / D1 狀態")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def status(self, interaction: discord.Interaction) -> None:
        """顯示資料庫連線狀態與 ping 結果。"""
        await interaction.response.defer(ephemeral=True)

        # 讀取統一儲存層，內含 Redis 與 D1 連線資訊。
        store = get_storage()

        # Redis 連線/延遲檢查。
        redis_conn = "已連線" if store.redis_available else "未連線"
        redis_ping_text = "不適用"
        if store.redis_available:
            try:
                assert store._redis is not None
                start = perf_counter()
                await store._redis.ping()
                redis_ping_text = f"{(perf_counter() - start) * 1000:.1f} ms"
            except Exception as exc:  # noqa: BLE001
                redis_conn = f"錯誤（{exc}）"

        # D1 連線/延遲檢查。
        d1_conn = "已連線" if store.d1_available else "未連線"
        d1_ping_text = "不適用"
        if store.d1_available:
            try:
                start = perf_counter()
                await store.execute("SELECT 1 AS ok")
                d1_ping_text = f"{(perf_counter() - start) * 1000:.1f} ms"
            except Exception as exc:  # noqa: BLE001
                d1_conn = f"錯誤（{exc}）"

        embed = discord.Embed(title="儲存系統狀態", color=discord.Color.blurple())
        embed.add_field(
            name="Redis",
            value=f"連線狀態：{redis_conn}\n延遲：{redis_ping_text}",
            inline=False,
        )
        embed.add_field(
            name="Cloudflare D1",
            value=f"連線狀態：{d1_conn}\n延遲：{d1_ping_text}",
            inline=False,
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="panel", description="取得管理面板網址")
    async def panel(self, interaction: discord.Interaction) -> None:
        """顯示 Web 管理面板網址。"""
        port = os.getenv("WEB_PORT", "8080")
        url = (os.getenv("WEB_BASE_URL") or "").rstrip("/") or f"http://localhost:{port}"
        embed = discord.Embed(
            title="\U0001f319 SleepBot 管理面板",
            description=(
                f"[點擊開啟管理面板]({url})\n\n"
                f"在面板上你可以：\n"
                f"• 核准或拒絕復原請求\n"
                f"• 手動執行伺服器復原\n"
                f"• 調整異常偵測門檻"
            ),
            color=discord.Color.from_str("#8b7ec8"),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="defense", description="暫停或啟用自動防禦系統")
    @app_commands.describe(action="要執行的操作")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="查看狀態", value="status"),
            app_commands.Choice(name="暫時關閉（1 小時）", value="disable"),
            app_commands.Choice(name="立即啟用", value="enable"),
        ]
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def defense(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
    ) -> None:
        """管理防禦系統狀態。"""
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("❌ 此指令只能在伺服器內使用。", ephemeral=True)
            return

        store = get_storage()
        guild_id = str(guild.id)
        user_id = str(interaction.user.id)

        try:
            if action.value == "disable":
                state = await set_defense_disabled(
                    store,
                    guild_id,
                    user_id,
                    duration_seconds=DEFAULT_DISABLE_SECONDS,
                )
                until = state["disabled_until"]
                await interaction.followup.send(
                    (
                        "🛑 防禦系統已暫時關閉。\n"
                        f"將於 <t:{until}:R> 自動恢復（<t:{until}:f>）。\n"
                        "你也可以隨時用 `/defense` 選擇「立即啟用」。"
                    ),
                    ephemeral=True,
                )
                return

            if action.value == "enable":
                await set_defense_enabled(store, guild_id, user_id)
                await interaction.followup.send(
                    "✅ 防禦系統已重新啟用。",
                    ephemeral=True,
                )
                return

            state = await get_defense_state(store, guild_id)
            if state["enabled"]:
                await interaction.followup.send("✅ 目前防禦系統為啟用中。", ephemeral=True)
                return

            until = state["disabled_until"]
            await interaction.followup.send(
                (
                    "🛑 目前防禦系統為暫停中。\n"
                    f"預計 <t:{until}:R> 自動恢復（<t:{until}:f>）。"
                ),
                ephemeral=True,
            )
        except DefenseStorageError:
            logger.warning(
                "Defense command storage failed guild=%s action=%s user=%s",
                guild_id,
                action.value,
                user_id,
                exc_info=True,
            )
            await interaction.followup.send(
                "❌ 防禦操作失敗：儲存服務暫時不可用，請稍後再試。",
                ephemeral=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Defense command failed guild=%s action=%s user=%s: %s",
                guild_id,
                action.value,
                user_id,
                exc,
                exc_info=True,
            )
            await interaction.followup.send(
                "❌ 防禦操作失敗，請稍後重試。",
                ephemeral=True,
            )

    @app_commands.command(name="message-log-status", description="查看訊息記錄保留狀態")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def message_log_status(self, interaction: discord.Interaction) -> None:
        """顯示訊息記錄容量、14 天保留與是否超過上限。"""
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("❌ 此指令只能在伺服器內使用。", ephemeral=True)
            return

        store = get_storage()
        guild_id = str(guild.id)

        try:
            total_rows = await store.fetchall(
                "SELECT COUNT(*) AS cnt FROM encrypted_messages WHERE guild_id = ?",
                [guild_id],
            )
            recent_rows = await store.fetchall(
                """
                SELECT COUNT(*) AS cnt
                FROM encrypted_messages
                WHERE guild_id = ?
                  AND timestamp >= (strftime('%s','now') - ?)
                """,
                [guild_id, _MESSAGE_RETENTION_SECONDS],
            )
            range_rows = await store.fetchall(
                """
                SELECT MIN(timestamp) AS oldest_ts, MAX(timestamp) AS newest_ts
                FROM encrypted_messages
                WHERE guild_id = ?
                """,
                [guild_id],
            )

            total = int(total_rows[0]["cnt"] if total_rows else 0)
            in_retention = int(recent_rows[0]["cnt"] if recent_rows else 0)
            overflow = max(0, total - _MESSAGE_MAX_PER_GUILD)
            oldest_ts = int(range_rows[0]["oldest_ts"]) if range_rows and range_rows[0]["oldest_ts"] else None
            newest_ts = int(range_rows[0]["newest_ts"]) if range_rows and range_rows[0]["newest_ts"] else None

            embed = discord.Embed(title="訊息記錄狀態", color=discord.Color.blurple())
            embed.add_field(name="總訊息數", value=f"{total}", inline=True)
            embed.add_field(name="14 天內", value=f"{in_retention}", inline=True)
            embed.add_field(name="超過上限 (50000)", value=f"{overflow}", inline=True)
            embed.add_field(
                name="保留規則",
                value="僅保留 14 天內，且每伺服器最多 50000 則",
                inline=False,
            )
            embed.add_field(
                name="最早記錄",
                value=(f"<t:{oldest_ts}:f>" if oldest_ts else "無"),
                inline=True,
            )
            embed.add_field(
                name="最新記錄",
                value=(f"<t:{newest_ts}:f>" if newest_ts else "無"),
                inline=True,
            )

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "message-log-status failed guild=%s: %s",
                guild_id,
                exc,
                exc_info=True,
            )
            await interaction.followup.send(
                "❌ 讀取訊息記錄狀態失敗，請稍後重試。",
                ephemeral=True,
            )

    @app_commands.command(name="force-snapshot", description="立即重新快照伺服器頻道、身分組與成員暱稱")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def force_snapshot(self, interaction: discord.Interaction) -> None:
        """強制更新此伺服器的結構快照（頻道、身分組、成員），以確保還原資料為最新狀態。"""
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("❌ 此指令只能在伺服器內使用。", ephemeral=True)
            return

        store = get_storage()
        guild_id = str(guild.id)
        ch_count = ro_count = mb_count = 0

        try:
            for channel in guild.channels:
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
                await store.execute(
                    "INSERT INTO structure_snapshots (guild_id, target_type, target_id, snapshot_data) VALUES (?, ?, ?, ?)",
                    [guild_id, "channel", str(channel.id), json.dumps(data, ensure_ascii=False)],
                )
                ch_count += 1

            for role in guild.roles:
                data = {
                    "role_id": str(role.id),
                    "name": role.name,
                    "permissions": str(role.permissions.value),
                    "position": role.position,
                    "color": role.color.value,
                    "hoist": role.hoist,
                    "mentionable": role.mentionable,
                    "members": [str(m.id) for m in role.members],
                }
                await store.execute(
                    "INSERT INTO structure_snapshots (guild_id, target_type, target_id, snapshot_data) VALUES (?, ?, ?, ?)",
                    [guild_id, "role", str(role.id), json.dumps(data, ensure_ascii=False)],
                )
                ro_count += 1

            for member in guild.members:
                data = {"user_id": str(member.id), "nick": member.nick}
                await store.execute(
                    "INSERT INTO structure_snapshots (guild_id, target_type, target_id, snapshot_data) VALUES (?, ?, ?, ?)",
                    [guild_id, "member", str(member.id), json.dumps(data, ensure_ascii=False)],
                )
                mb_count += 1

            logger.info(
                "Force snapshot completed guild=%s channels=%d roles=%d members=%d user=%s",
                guild_id, ch_count, ro_count, mb_count, interaction.user.id,
            )
            await interaction.followup.send(
                f"✅ 快照已更新：{ch_count} 個頻道、{ro_count} 個身分組、{mb_count} 位成員。",
                ephemeral=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Force snapshot failed guild=%s: %s", guild_id, exc, exc_info=True)
            await interaction.followup.send(f"❌ 快照失敗：{exc}", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SystemCommandsCog(bot))
    logger.info("SystemCommands cog loaded")

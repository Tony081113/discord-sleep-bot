"""系統管理斜線指令。

提供：
- /ping   ：顯示 Discord API 延遲
- /status ：顯示 Redis / D1 連線與延遲
- /panel  ：顯示管理面板網址
"""

from time import perf_counter
import os

import discord
from discord import app_commands
from discord.ext import commands

from mods.logger import setup_logger
from mods.storage import get_storage

logger = setup_logger(__name__)


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


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SystemCommandsCog(bot))
    logger.info("SystemCommands cog loaded")

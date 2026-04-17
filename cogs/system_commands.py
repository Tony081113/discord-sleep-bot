"""System slash commands.

Provides:
- /ping   : show Discord API latency
- /status : show Redis / D1 connection status and ping
"""

from time import perf_counter

import discord
from discord import app_commands
from discord.ext import commands

from mods.logger import setup_logger
from mods.storage import get_storage

logger = setup_logger(__name__)


class SystemCommandsCog(commands.Cog, name="SystemCommands"):
    """Operational slash commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="ping", description="顯示 API 延遲")
    async def ping(self, interaction: discord.Interaction) -> None:
        """Show WebSocket and interaction response latency."""
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
        """Show DB connectivity and ping results."""
        await interaction.response.defer(ephemeral=True)

        store = get_storage()

        # Redis status
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

        # D1 status
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


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SystemCommandsCog(bot))
    logger.info("SystemCommands cog loaded")

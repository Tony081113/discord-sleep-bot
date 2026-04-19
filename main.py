#要避免在還原時被429，且還原前要先把搗亂的人踢出(如果發現發訊者不在群內，可能是User install bot)
"""
Discord bot entry point.

Responsibilities
----------------
1. Load configuration from .env
2. Initialise mods (logger, unified storage: Redis + D1)
3. Attach Redis handler to all loggers
4. Log into Discord
5. Load all cogs from the ``cogs/`` directory
6. Start the bot
"""

import asyncio
import json
import os
import pathlib

import discord
from discord.ext import commands
from dotenv import load_dotenv

# Load environment variables as early as possible
load_dotenv()

from mods.logger import attach_redis, setup_logger, get_configured_logger_names
from mods.schema import init_schema
from mods.storage import init_storage, close_storage, get_storage

logger = setup_logger(__name__)

# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True

bot = commands.Bot(command_prefix=">>", intents=INTENTS)
_app_commands_synced = False


# ---------------------------------------------------------------------------
# Owner-only prefix commands
# ---------------------------------------------------------------------------

@bot.command(name="reload")
async def cmd_reload(ctx: commands.Context, cog: str = "") -> None:
    """Reload one cog (``>>reload cogs.monitoring``) or all cogs (``>>reload``)."""
    admin_id = os.getenv("BOT_ADMIN_ID", "").strip()
    if not admin_id or str(ctx.author.id) != admin_id:
        return
    cogs_dir = pathlib.Path(__file__).parent / "cogs"
    targets: list[str] = []
    if cog:
        targets = [cog if "." in cog else f"cogs.{cog}"]
    else:
        targets = [
            f"cogs.{f.stem}"
            for f in sorted(cogs_dir.glob("*.py"))
            if not f.name.startswith("_")
        ]

    ok, fail = [], []
    for module in targets:
        try:
            if module in bot.extensions:
                await bot.reload_extension(module)
            else:
                await bot.load_extension(module)
            ok.append(module)
        except Exception as exc:  # noqa: BLE001
            fail.append(f"{module}: {exc}")
            logger.error("Reload failed for '%s': %s", module, exc, exc_info=True)

    lines = []
    if ok:
        lines.append("\u2705 " + ", ".join(ok))
    if fail:
        lines += [f"\u274c {f}" for f in fail]
    await ctx.send("\n".join(lines) or "nothing to reload")


@bot.command(name="commit")
async def cmd_commit(ctx: commands.Context) -> None:
    """Force-save current guild recovery snapshots (``>>commit``)."""
    admin_id = os.getenv("BOT_ADMIN_ID", "").strip()
    if not admin_id or str(ctx.author.id) != admin_id:
        return

    guild = ctx.guild
    if guild is None:
        await ctx.send("❌ 這個指令只能在伺服器內使用。")
        return

    store = get_storage()
    if not store.redis_available:
        await ctx.send(
            "⚠️ commit skipped: Redis unavailable "
            f"(redis={store.redis_available})"
        )
        return

    guild_id = str(guild.id)
    ch_count = ro_count = mb_count = 0

    try:
        guild_data = {
            "guild_id": guild_id,
            "name": guild.name,
            "icon_url": str(guild.icon.url) if guild.icon else None,
            "banner_url": str(guild.banner.url) if guild.banner else None,
        }

        redis = getattr(store, "_redis", None)
        if redis is None:
            await ctx.send("⚠️ commit skipped: Redis client unavailable")
            return

        redis_batch: list[str] = []
        redis_prefix = f"sync:recovery:commit:{guild_id}"
        redis_batch.append(f"{redis_prefix}:guild:{guild_id}")
        redis_batch.append(json.dumps(guild_data, ensure_ascii=False))

        channel_payloads: list[tuple[str, str]] = []
        role_payloads: list[tuple[str, str]] = []
        member_payloads: list[tuple[str, str]] = []

        for channel in guild.channels:
            ch_data = {
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
            ch_data["permission_overwrites"] = overwrites
            if isinstance(channel, discord.TextChannel):
                ch_data["topic"] = channel.topic
                ch_data["nsfw"] = channel.nsfw
                ch_data["slowmode_delay"] = channel.slowmode_delay

            channel_payloads.append((
                str(channel.id),
                json.dumps(ch_data, ensure_ascii=False),
            ))
            redis_batch.append(f"{redis_prefix}:channel:{channel.id}")
            redis_batch.append(channel_payloads[-1][1])
            ch_count += 1

        for role in guild.roles:
            role_data = {
                "role_id": str(role.id),
                "name": role.name,
                "permissions": str(role.permissions.value),
                "position": role.position,
                "color": role.color.value,
                "hoist": role.hoist,
                "mentionable": role.mentionable,
                "members": [str(member.id) for member in role.members],
            }
            role_payloads.append((
                str(role.id),
                json.dumps(role_data, ensure_ascii=False),
            ))
            redis_batch.append(f"{redis_prefix}:role:{role.id}")
            redis_batch.append(role_payloads[-1][1])
            ro_count += 1

        for member in guild.members:
            member_data = {
                "user_id": str(member.id),
                "nick": member.nick,
            }
            member_payloads.append((
                str(member.id),
                json.dumps(member_data, ensure_ascii=False),
            ))
            redis_batch.append(f"{redis_prefix}:member:{member.id}")
            redis_batch.append(member_payloads[-1][1])
            mb_count += 1

        chunk_size = 100
        for i in range(0, len(redis_batch), chunk_size):
            chunk = redis_batch[i : i + chunk_size]
            pairs = {chunk[j]: chunk[j + 1] for j in range(0, len(chunk), 2)}
            await redis.mset(pairs)
        for i in range(0, len(redis_batch), 2):
            await redis.expire(redis_batch[i], 3600)

        total_snapshots = len(redis_batch) // 2

        logger.info(
            "Manual commit snapshot staged to Redis guild=%s channels=%d roles=%d members=%d user=%s",
            guild_id,
            ch_count,
            ro_count,
            mb_count,
            ctx.author.id,
        )
        logger.info(
            "Commit summary guild=%s snapshots_total=%d channels=%d roles=%d members=%d prefix=%s user=%s",
            guild_id,
            total_snapshots,
            ch_count,
            ro_count,
            mb_count,
            redis_prefix,
            ctx.author.id,
        )
        await ctx.send(
            f"✅ commit complete: 已寫入 Redis，後續由守護同步寫入（頻道 {ch_count}、身分組 {ro_count}、成員 {mb_count}）。"
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Manual commit failed: %s", exc, exc_info=True)
        await ctx.send(f"❌ commit failed: {exc}")


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

@bot.event
async def on_ready() -> None:
    await sync_app_commands_once()
    logger.info("Logged in as %s (id=%d)", bot.user, bot.user.id)
    logger.info("Guilds: %d", len(bot.guilds))


@bot.event
async def on_error(event: str, *args, **kwargs) -> None:
    logger.exception("Unhandled exception in event '%s'", event)


# ---------------------------------------------------------------------------
# Start-up / shut-down hooks
# ---------------------------------------------------------------------------

async def load_cogs() -> None:
    """Discover and load all cog files from the ``cogs/`` directory."""
    cogs_dir = pathlib.Path(__file__).parent / "cogs"
    loaded = 0
    for cog_file in sorted(cogs_dir.glob("*.py")):
        if cog_file.name.startswith("_"):
            continue
        module = f"cogs.{cog_file.stem}"
        try:
            await bot.load_extension(module)
            logger.info("Loaded cog: %s", module)
            loaded += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load cog '%s': %s", module, exc, exc_info=True)
    logger.info("Cogs loaded: %d", loaded)


async def init_mods() -> None:
    """Initialise all mods in the correct order."""
    try:
        store = await init_storage()
        logger.info(
            "Storage initialised (redis=%s, d1=%s)",
            store.redis_available,
            store.d1_available,
        )

        # Create / verify all D1 tables
        await init_schema(store)

        # Attach a synchronous Redis handler to all configured loggers.
        # A synchronous client is required because logging.Handler.emit
        # cannot await coroutines.
        if store.redis_available:
            sync_redis = store.get_sync_redis_client()
            for name in get_configured_logger_names():
                attach_redis(name, sync_redis)

    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to initialise storage: %s", exc, exc_info=True)


async def shutdown_mods() -> None:
    """Gracefully shut down all mods."""
    try:
        await close_storage()
    except Exception as exc:  # noqa: BLE001
        logger.error("Error closing storage: %s", exc)


async def sync_app_commands_once() -> None:
    """Sync slash commands once per process start.

    Tries guild-scoped sync for every guild the bot is in (fastest propagation).
    Falls back to global sync if all guild syncs fail (e.g. missing
    ``applications.commands`` scope — bot was invited without it).
    """
    global _app_commands_synced

    if _app_commands_synced:
        return

    guild_synced = 0
    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            logger.info(
                "Synced %d app command(s) to guild '%s' (id=%d)",
                len(synced),
                guild.name,
                guild.id,
            )
            guild_synced += 1
        except discord.Forbidden:
            logger.warning(
                "Cannot sync commands to guild '%s' (id=%d) — "
                "bot may be missing applications.commands scope",
                guild.name,
                guild.id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to sync app commands to guild '%s' (id=%d): %s",
                guild.name,
                guild.id,
                exc,
                exc_info=True,
            )

    if guild_synced == 0:
        # No guild accepted the sync; fall back to global (propagates in ~1 hour).
        try:
            synced = await bot.tree.sync()
            logger.warning(
                "Guild sync failed for all guilds — fell back to global sync "
                "(%d commands, may take up to 1 hour to propagate)",
                len(synced),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Global app command sync also failed: %s", exc, exc_info=True)
            return

    _app_commands_synced = True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise ValueError("DISCORD_TOKEN is not set in the environment / .env file")

    try:
        async with bot:
            await init_mods()
            await load_cogs()
            await bot.start(token)
    finally:
        await shutdown_mods()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")

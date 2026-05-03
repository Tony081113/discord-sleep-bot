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

_RECOVERY_SHUTDOWN_WAIT_SECONDS = max(
    0,
    int(os.getenv("RECOVERY_SHUTDOWN_WAIT_SECONDS", "180")),
)


async def _wait_or_queue_recoveries_before_shutdown(bot: commands.Bot) -> None:
    """關機前先等復原任務；逾時未完成則排到下次開機。"""
    recovery_cog = bot.get_cog("Recovery")
    if recovery_cog is None:
        return
    if not hasattr(recovery_cog, "wait_for_all_recoveries"):
        return

    completed, active = await recovery_cog.wait_for_all_recoveries(
        _RECOVERY_SHUTDOWN_WAIT_SECONDS
    )
    if completed:
        logger.info("Shutdown wait: all recovery tasks completed")
        return

    if not active:
        return

    queued_total = 0
    if hasattr(recovery_cog, "persist_recovery_resume_queue"):
        queued_total = recovery_cog.persist_recovery_resume_queue(active)
    logger.warning(
        "Shutdown timeout: %d recovery task(s) queued for next startup (queue_size=%d)",
        len(active),
        queued_total,
    )


class SleepBot(commands.Bot):
    """Bot with graceful shutdown that coordinates recovery workflows."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._closing_with_recovery_wait = False

    async def close(self) -> None:
        if self._closing_with_recovery_wait:
            await super().close()
            return

        self._closing_with_recovery_wait = True
        try:
            await _wait_or_queue_recoveries_before_shutdown(self)
        except Exception as exc:  # noqa: BLE001
            logger.error("Graceful recovery shutdown check failed: %s", exc, exc_info=True)
        finally:
            await super().close()


bot = SleepBot(command_prefix=">>", intents=INTENTS)
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

        pending_pairs: list[tuple[str, str]] = []
        pending_chunk_pairs = 50
        total_snapshots = 0

        async def _flush_pending() -> None:
            nonlocal total_snapshots
            if not pending_pairs:
                return
            await redis.mset(dict(pending_pairs))
            pipe = redis.pipeline(transaction=False)
            for key, _ in pending_pairs:
                pipe.expire(key, 3600)
            await pipe.execute()
            total_snapshots += len(pending_pairs)
            pending_pairs.clear()

        def _queue_snapshot(key: str, payload: dict[str, object]) -> None:
            pending_pairs.append((key, json.dumps(payload, ensure_ascii=False)))

        redis_prefix = f"sync:recovery:commit:{guild_id}"
        _queue_snapshot(f"{redis_prefix}:guild:{guild_id}", guild_data)

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

            _queue_snapshot(f"{redis_prefix}:channel:{channel.id}", ch_data)
            if len(pending_pairs) >= pending_chunk_pairs:
                await _flush_pending()
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
            _queue_snapshot(f"{redis_prefix}:role:{role.id}", role_data)
            if len(pending_pairs) >= pending_chunk_pairs:
                await _flush_pending()
            ro_count += 1

        for member in guild.members:
            member_data = {
                "user_id": str(member.id),
                "nick": member.nick,
            }
            _queue_snapshot(f"{redis_prefix}:member:{member.id}", member_data)
            if len(pending_pairs) >= pending_chunk_pairs:
                await _flush_pending()
            mb_count += 1

        await _flush_pending()

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
# Background tasks
# ---------------------------------------------------------------------------

_reload_task: asyncio.Task | None = None


async def monitor_reload_queue() -> None:
    """Monitor Redis queue for reload requests from web.py.
    
    Web should push cogs.web reload requests to 'bot:reload_queue' 
    instead of doing it directly, to avoid reloading while serving requests.
    """
    store = get_storage()
    if not store.redis_available:
        logger.warning("Redis unavailable — reload queue monitoring disabled")
        return

    redis = getattr(store, "_redis", None)
    if redis is None:
        logger.warning("Redis client unavailable — reload queue monitoring disabled")
        return

    logger.info("Started reload queue monitoring")
    while True:
        try:
            await asyncio.sleep(2)  # Poll every 2 seconds
            # Pop one reload request from the queue
            module = await redis.lpop("bot:reload_queue")
            if not module:
                continue

            module = module.decode() if isinstance(module, bytes) else str(module)
            module = module.strip()
            if not module:
                continue

            logger.info("Processing queued reload request: %s", module)
            try:
                if module in bot.extensions:
                    await bot.reload_extension(module)
                else:
                    await bot.load_extension(module)
                logger.info("Successfully reloaded: %s", module)
            except Exception as exc:  # noqa: BLE001
                logger.error("Reload failed for '%s': %s", module, exc, exc_info=True)

        except asyncio.CancelledError:
            break
        except Exception as exc:  # noqa: BLE001
            logger.error("Reload queue monitor error: %s", exc, exc_info=True)
            await asyncio.sleep(5)  # Back off on error

    logger.info("Reload queue monitoring stopped")


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

@bot.event
async def on_ready() -> None:
    global _reload_task
    
    await sync_app_commands_once()
    logger.info("Logged in as %s (id=%d)", bot.user, bot.user.id)
    logger.info("Guilds: %d", len(bot.guilds))
    
    # Start reload queue monitor if not already running
    if _reload_task is None or _reload_task.done():
        _reload_task = asyncio.create_task(monitor_reload_queue())


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

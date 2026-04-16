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
import os
import pathlib

import discord
from discord.ext import commands
from dotenv import load_dotenv

# Load environment variables as early as possible
load_dotenv()

from mods.logger import attach_redis, setup_logger, get_configured_logger_names
from mods.storage import init_storage, close_storage

logger = setup_logger(__name__)

# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

INTENTS = discord.Intents.default()
INTENTS.message_content = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

@bot.event
async def on_ready() -> None:
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise ValueError("DISCORD_TOKEN is not set in the environment / .env file")

    async with bot:
        await init_mods()
        await load_cogs()
        await bot.start(token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    finally:
        asyncio.run(shutdown_mods())

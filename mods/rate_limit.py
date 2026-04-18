"""
Rate-limit helpers for Discord API calls.

Provides reusable utilities so that bulk operations (channel/role
restoration, DM blasts, etc.) stay well under Discord's per-route
and global rate limits without relying solely on discord.py's
built-in 429 retry logic.
"""

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

import discord

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Delays (seconds) tuned to stay comfortably under Discord rate limits.
CHANNEL_OP_DELAY = 1.0     # channel create / edit  (per-guild route)
ROLE_OP_DELAY = 1.0        # role create / edit      (per-guild route)
DM_SEND_DELAY = 0.8        # user DM sends           (global DM route)
WEBHOOK_SEND_DELAY = 0.5   # webhook execute          (per-webhook route)

_MAX_RETRIES = 3


async def rate_limited_call(
    coro_fn: Callable[..., Awaitable[T]],
    *args,
    retry: int = _MAX_RETRIES,
    **kwargs,
) -> T:
    """Call *coro_fn* with automatic retry on ``HTTPException`` 429.

    discord.py normally handles 429 transparently, but in edge cases
    (shared rate limits, Cloudflare bans) an explicit retry with
    back-off gives extra safety.
    """
    last_exc: Exception | None = None
    for attempt in range(1, retry + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except discord.HTTPException as exc:
            if exc.status == 429:
                retry_after = getattr(exc, "retry_after", None)
                wait = retry_after if retry_after else 2 ** attempt
                logger.warning(
                    "429 rate-limited (attempt %d/%d), retrying after %.2fs: %s",
                    attempt, retry, wait, exc,
                )
                await asyncio.sleep(wait)
                last_exc = exc
            else:
                raise
    # All retries exhausted — re-raise the last 429 so the caller can
    # decide what to do.
    raise last_exc  # type: ignore[misc]

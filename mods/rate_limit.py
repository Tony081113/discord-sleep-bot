"""
Rate-limit helpers for Discord API calls.

Provides reusable utilities so that bulk operations (channel/role
restoration, DM blasts, etc.) stay well under Discord's per-route
and global rate limits without relying solely on discord.py's
built-in 429 retry logic.

Rate-limit telemetry can be collected and queried via get_ratelimit_stats().
"""

import asyncio
import collections
import logging
import os
import time
from typing import Awaitable, Callable, TypeVar

import discord

logger = logging.getLogger(__name__)

T = TypeVar("T")

def _env_float(name: str, default: float, min_value: float, max_value: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid env %s=%r, fallback=%s", name, raw, default)
        return default
    return max(min_value, min(max_value, value))


def _env_int(name: str, default: int, min_value: int, max_value: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid env %s=%r, fallback=%s", name, raw, default)
        return default
    return max(min_value, min(max_value, value))


# Baseline delays (seconds). These are floors; dynamic limiter can increase them.
CHANNEL_OP_DELAY = _env_float("RATE_CHANNEL_OP_DELAY", 1.0, 0.05, 5.0)
ROLE_OP_DELAY = _env_float("RATE_ROLE_OP_DELAY", 1.0, 0.05, 5.0)
DM_SEND_DELAY = _env_float("RATE_DM_SEND_DELAY", 0.8, 0.05, 5.0)
WEBHOOK_SEND_DELAY = _env_float("RATE_WEBHOOK_SEND_DELAY", 0.5, 0.05, 5.0)

_MAX_RETRIES = _env_int("RATE_MAX_RETRIES", 3, 1, 10)
_MAX_DYNAMIC_DELAY = _env_float("RATE_MAX_DYNAMIC_DELAY", 15.0, 1.0, 60.0)


class _RateLimitStats:
    """In-memory telemetry for rate-limit 429 events (per-minute sliding window)."""

    def __init__(self, window_seconds: int = 60) -> None:
        self.window_seconds = window_seconds
        # (timestamp, limit_key, scope, bucket) -> count
        self.events: collections.deque = collections.deque()

    def record_429(self, limit_key: str, scope: str, bucket: str | None) -> None:
        """Record a 429 event with scope and bucket information."""
        now = time.time()
        self.events.append((now, limit_key, scope, bucket or ""))

    def get_stats(self, minutes: int = 1) -> dict:
        """Get aggregated 429 stats for the last N minutes.
        
        Returns a dict with:
          - total_429s: total count
          - by_scope: {scope_name: count}
          - by_bucket: {bucket_id: count} (top 10)
          - by_limit_key: {key: count}
          - timestamps: list of recent (timestamp, key, scope, bucket) tuples
        """
        now = time.time()
        cutoff = now - (minutes * 60)
        
        # Remove old events
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()
        
        total = len(self.events)
        by_scope: dict[str, int] = collections.Counter()
        by_bucket: dict[str, int] = collections.Counter()
        by_limit_key: dict[str, int] = collections.Counter()
        
        for ts, key, scope, bucket in self.events:
            by_limit_key[key] += 1
            by_scope[scope] += 1
            if bucket:
                by_bucket[bucket] += 1
        
        # Sort buckets by count and take top 10
        top_buckets = dict(sorted(by_bucket.items(), key=lambda x: -x[1])[:10])
        
        return {
            "total_429s": total,
            "by_scope": dict(by_scope),
            "by_bucket": top_buckets,
            "by_limit_key": dict(by_limit_key),
            "window_minutes": minutes,
            "collected_at": now,
        }


class _AdaptiveLimiter:
    """In-memory adaptive limiter fed by observed 429 retry hints."""

    def __init__(self) -> None:
        self._next_allowed: dict[str, float] = {}
        self._dynamic_delay: dict[str, float] = {}

    def _base_delay(self, key: str) -> float:
        if "webhook" in key:
            return WEBHOOK_SEND_DELAY
        if "dm" in key:
            return DM_SEND_DELAY
        if "channel" in key:
            return CHANNEL_OP_DELAY
        if "role" in key:
            return ROLE_OP_DELAY
        return 0.1

    async def wait(self, key: str) -> None:
        now = time.monotonic()
        blocked_until = self._next_allowed.get(key, 0.0)
        if blocked_until <= now:
            return
        await asyncio.sleep(blocked_until - now)

    def on_success(self, key: str) -> None:
        base = self._base_delay(key)
        current = self._dynamic_delay.get(key, base)
        if current > base:
            self._dynamic_delay[key] = max(base, current * 0.92)

    def on_429(self, key: str, retry_after: float | None, attempt: int) -> float:
        base = self._base_delay(key)
        current = self._dynamic_delay.get(key, base)
        fallback = min(_MAX_DYNAMIC_DELAY, base * (2 ** attempt))
        suggested = retry_after if retry_after is not None else fallback
        wait = min(_MAX_DYNAMIC_DELAY, max(base, current * 1.35, suggested))
        self._dynamic_delay[key] = wait
        self._next_allowed[key] = time.monotonic() + wait
        return wait

    def mark_wait(self, key: str, seconds: float) -> None:
        wait = max(0.0, min(_MAX_DYNAMIC_DELAY, seconds))
        now = time.monotonic()
        self._next_allowed[key] = max(self._next_allowed.get(key, now), now + wait)

    def current_delay(self, key: str, fallback: float) -> float:
        return max(fallback, self._dynamic_delay.get(key, self._base_delay(key)))


_LIMITER = _AdaptiveLimiter()
_STATS = _RateLimitStats()


def _extract_retry_after(exc: discord.HTTPException) -> float | None:
    if getattr(exc, "retry_after", None):
        try:
            return float(exc.retry_after)
        except (TypeError, ValueError):
            return None

    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None

    for k in ("Retry-After", "X-RateLimit-Reset-After"):
        raw = headers.get(k)
        if not raw:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def get_adaptive_delay(limit_key: str, fallback: float) -> float:
    """Get current adaptive delay for non-request gaps (sleep between operations)."""
    return _LIMITER.current_delay(limit_key, fallback)


def get_ratelimit_stats(minutes: int = 1) -> dict:
    """Get rate-limit telemetry for the last N minutes.
    
    Returns aggregated 429 statistics including scope and bucket hotspots.
    Useful for monitoring and debugging rate-limit issues.
    """
    return _STATS.get_stats(minutes)


async def rate_limited_call(
    coro_fn: Callable[..., Awaitable[T]],
    *args,
    retry: int = _MAX_RETRIES,
    limit_key: str = "default",
    **kwargs,
) -> T:
    """Call *coro_fn* with automatic retry on ``HTTPException`` 429.

    discord.py normally handles 429 transparently, but in edge cases
    (shared rate limits, Cloudflare bans) an explicit retry with
    back-off gives extra safety.
    """
    last_exc: Exception | None = None
    for attempt in range(1, retry + 1):
        await _LIMITER.wait("__global__")
        await _LIMITER.wait(limit_key)
        try:
            result = await coro_fn(*args, **kwargs)
            _LIMITER.on_success(limit_key)
            return result
        except discord.HTTPException as exc:
            if exc.status == 429:
                response = getattr(exc, "response", None)
                headers = getattr(response, "headers", {}) or {}
                bucket = headers.get("X-RateLimit-Bucket")
                scope = headers.get("X-RateLimit-Scope", "unknown")
                is_global = str(headers.get("X-RateLimit-Global", "")).lower() == "true"
                retry_after = _extract_retry_after(exc)
                key = f"{limit_key}|{bucket}" if bucket else limit_key
                wait = _LIMITER.on_429(key, retry_after, attempt)
                _STATS.record_429(limit_key, scope, bucket)
                if is_global or scope == "global":
                    _LIMITER.mark_wait("__global__", wait)
                logger.warning(
                    "429 rate-limited key=%s scope=%s bucket=%s global=%s "
                    "(attempt %d/%d), retry after %.2fs: %s",
                    limit_key,
                    scope,
                    bucket,
                    is_global,
                    attempt,
                    retry,
                    wait,
                    exc,
                )
                await asyncio.sleep(wait)
                last_exc = exc
            else:
                raise
    # All retries exhausted — re-raise the last 429 so the caller can
    # decide what to do.
    raise last_exc  # type: ignore[misc]

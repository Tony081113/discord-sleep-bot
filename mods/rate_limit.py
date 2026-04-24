"""
Rate-limit helpers for Discord API calls.

Provides reusable utilities so that bulk operations (channel/role
restoration, DM blasts, etc.) stay well under Discord's per-route
and global rate limits without relying solely on discord.py's
built-in 429 retry logic.

Rate-limit telemetry can be collected and queried via get_ratelimit_stats().

亦包含 Cloudflare 無效請求防護：10 分鐘內超過 9000 個
4xx 回應時自動中止，避免觸發 CF 封鎖。
"""

import asyncio
import collections
import logging
import os
import time
from collections import deque
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

# ── Cloudflare 無效請求防護 ────────────────────────────────────────────────────
# Discord 每 10 分鐘允許最多 10,000 個無效請求（4xx），超過後 Cloudflare 自動封鎖。
_CF_WINDOW = 600      # 10 分鐘（秒）
_CF_LIMIT  = 9_000    # 在到達 10,000 前的安全停止線


class _InvalidRequestGuard:
    """滑動窗口計數器，追蹤 4xx 回應次數以防 Cloudflare 封鎖。"""

    def __init__(self) -> None:
        self._ts: deque[float] = deque()

    def record(self) -> None:
        now = time.monotonic()
        self._ts.append(now)
        self._prune(now)

    def is_safe(self) -> bool:
        self._prune(time.monotonic())
        return len(self._ts) < _CF_LIMIT

    def _prune(self, now: float) -> None:
        cutoff = now - _CF_WINDOW
        while self._ts and self._ts[0] < cutoff:
            self._ts.popleft()


_cf_guard = _InvalidRequestGuard()


# ── 每資源滑動窗口桶 ──────────────────────────────────────────────────────────


class _ResourceBucket:
    """
    針對單一 (resource_type, resource_id) 的滑動窗口速率桶。

    初始參數保守；遇到 429 時透過 retry_after 收緊，
    若 API 回傳實際 header 數值（remaining / limit / reset_after），
    則立即以真實值覆蓋本地估計。
    """

    def __init__(self, limit: int = 5, window: float = 5.0) -> None:
        self._limit  = limit
        self._window = window
        self._calls: deque[float] = deque()
        self._throttled_until: float = 0.0

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        now  = loop.time()

        # 先等到解除節流（由 429 觸發）
        if self._throttled_until > now:
            await asyncio.sleep(self._throttled_until - now)
            now = loop.time()

        self._prune(loop.time())

        # 若已達窗口上限，等到最早一筆過期
        if len(self._calls) >= self._limit:
            wait = self._calls[0] + self._window - loop.time()
            if wait > 0:
                logger.debug(
                    "Bucket limit=%d/%.1fs reached, sleeping %.3fs",
                    self._limit, self._window, wait,
                )
                await asyncio.sleep(wait)
            self._prune(loop.time())

        self._calls.append(loop.time())

    def throttle(self, retry_after: float) -> None:
        """收到 429 時呼叫；依照 Retry-After 暫停桶並清空記錄。"""
        try:
            loop = asyncio.get_running_loop()
            self._throttled_until = loop.time() + retry_after
        except RuntimeError:
            self._throttled_until = time.monotonic() + retry_after
        self._calls.clear()

    def update_from_response(self, remaining: int, limit: int, reset_after: float) -> None:
        """以 X-RateLimit-* header 的真實值更新桶參數。"""
        if limit > 0:
            self._limit  = limit
        if reset_after > 0:
            self._window = reset_after
        # 讓呼叫歷史反映實際剩餘量
        target_used = max(0, self._limit - remaining)
        while len(self._calls) > target_used:
            self._calls.popleft()

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        while self._calls and self._calls[0] < cutoff:
            self._calls.popleft()


# ── 桶管理器（singleton） ─────────────────────────────────────────────────────


class _BucketManager:
    """
    全域桶登錄：以 (resource_type, resource_id) 為鍵管理獨立桶。

    Discord 的限制與頂層資源（channel_id / guild_id / webhook_id）綁定，
    因此不同資源的配額完全獨立，可以同時進行。
    """

    # 預設 (limit, window_seconds)；採保守起點，實際值由 API 回應更新。
    _DEFAULTS: dict[str, tuple[int, float]] = {
        "channel": (5, 5.0),   # 頻道建立／編輯（per-guild 路由）
        "role":    (5, 5.0),   # 身份組建立／編輯／刪除（per-guild 路由）
        "dm":      (5, 5.0),   # DM 傳送（全域 DM 路由）
        "webhook": (5, 2.0),   # Webhook 執行（per-webhook 路由）
    }

    def __init__(self) -> None:
        self._buckets: dict[tuple[str, int | str], _ResourceBucket] = {}

    def get(self, resource_type: str, resource_id: int | str = 0) -> _ResourceBucket:
        key = (resource_type, resource_id)
        if key not in self._buckets:
            limit, window = self._DEFAULTS.get(resource_type, (5, 5.0))
            self._buckets[key] = _ResourceBucket(limit, window)
        return self._buckets[key]

    async def wait(self, resource_type: str, resource_id: int | str = 0) -> None:
        await self.get(resource_type, resource_id).acquire()

    def on_429(self, resource_type: str, resource_id: int | str, retry_after: float) -> None:
        self.get(resource_type, resource_id).throttle(retry_after)

    def on_headers(
        self,
        resource_type: str,
        resource_id: int | str,
        remaining: int,
        limit: int,
        reset_after: float,
    ) -> None:
        """以真實 X-RateLimit-* header 更新指定資源的桶。"""
        self.get(resource_type, resource_id).update_from_response(remaining, limit, reset_after)


buckets = _BucketManager()


# ── 便利的 sleep 輔助函式（取代舊的 asyncio.sleep(HARDCODED)） ────────────────


async def channel_sleep(guild_id: int) -> None:
    """頻道建立／編輯後的自適應等待（per-guild bucket）。"""
    await buckets.wait("channel", guild_id)


async def role_sleep(guild_id: int) -> None:
    """身份組建立／編輯／刪除後的自適應等待（per-guild bucket）。"""
    await buckets.wait("role", guild_id)


async def dm_sleep() -> None:
    """DM 傳送後的自適應等待（全域 DM bucket）。"""
    await buckets.wait("dm", 0)


async def webhook_sleep(webhook_id: int) -> None:
    """Webhook 執行後的自適應等待（per-webhook bucket）。"""
    await buckets.wait("webhook", webhook_id)


# ── 核心重試包裝器 ────────────────────────────────────────────────────────────


async def rate_limited_call(
    coro_fn: Callable[..., Awaitable[T]],
    *args,
    retry: int = _MAX_RETRIES,
    limit_key: str = "default",
    **kwargs,
) -> T:
    """呼叫 *coro_fn* 並自動處理速率限制與 Cloudflare 防護。

    - **429**：讀取 ``retry_after`` 精確等待，並更新對應資源桶。
    - **401**：立即停止重試（token 無效，重試只會消耗無效請求額度）。
    - **403 / 404**：不重試，直接拋出（避免燒掉 CF 無效請求配額）。
    - **4xx** 全部計入滑動窗口；接近 Cloudflare 上限時自動中止。

    可選關鍵字參數 ``_resource_type`` / ``_resource_id`` 用於將 429
    回饋給對應資源桶；省略時不影響重試邏輯。
    """
    if not _cf_guard.is_safe():
        logger.error(
            "Approaching Cloudflare invalid-request limit – aborting to prevent ban."
        )
        raise RuntimeError("CF invalid-request limit reached; call aborted.")

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
            elif exc.status == 401:
                _cf_guard.record()
                logger.error(
                    "401 Unauthorized – halting retries immediately to protect token."
                )
                raise
            elif exc.status in (403, 404):
                _cf_guard.record()
                logger.warning(
                    "HTTP %d – not retrying to avoid invalid-request burn.",
                    exc.status,
                )
                raise
            else:
                raise
    raise last_exc  # type: ignore[misc]

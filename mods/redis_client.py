"""
Redis client module.

Provides:
  - An async Redis connection pool with automatic reconnection
  - A background task that periodically syncs tracked Redis keys to D1
  - A public API consumed by the rest of the application
"""

import asyncio
import json
import os
from typing import Any, Optional

import redis.asyncio as aioredis
from redis.asyncio.connection import ConnectionPool
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError

from mods.logger import setup_logger

logger = setup_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SYNC_KEY_PREFIX = "sync:"          # Keys with this prefix are synced to D1
_D1_SYNC_TABLE = "redis_cache"      # D1 table used for sync
_DEFAULT_SYNC_INTERVAL = 60         # seconds


# ---------------------------------------------------------------------------
# RedisClient
# ---------------------------------------------------------------------------

class RedisClient:
    """Async Redis client with connection pool and D1 sync capability.

    Usage
    -----
    Instantiate once, call :meth:`connect`, then use the helper methods.
    Pass a :class:`~mods.database.D1Database` instance to enable periodic
    D1 synchronisation.
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        password: Optional[str] = None,
        db: Optional[int] = None,
        *,
        max_connections: int = 10,
        sync_interval: Optional[int] = None,
        db_client=None,
    ) -> None:
        self._host = host or os.getenv("REDIS_HOST", "localhost")
        self._port = int(port or os.getenv("REDIS_PORT", "6379"))
        raw_password = password or os.getenv("REDIS_PASSWORD", "") or None
        self._password = raw_password if raw_password else None
        self._db = int(db if db is not None else os.getenv("REDIS_DB", "0"))
        self._max_connections = max_connections
        self._sync_interval = int(
            sync_interval
            or os.getenv("REDIS_SYNC_INTERVAL", str(_DEFAULT_SYNC_INTERVAL))
        )
        self._db_client = db_client  # mods.database.D1Database instance

        self._pool: Optional[ConnectionPool] = None
        self._redis: Optional[aioredis.Redis] = None
        self._sync_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Initialise the Redis connection pool."""
        self._pool = ConnectionPool(
            host=self._host,
            port=self._port,
            password=self._password,
            db=self._db,
            max_connections=self._max_connections,
            decode_responses=True,
        )
        self._redis = aioredis.Redis(connection_pool=self._pool)
        # Verify the connection is working
        await self._ping_with_retry()
        logger.info(
            "Redis connection pool initialised (host=%s, port=%d, db=%d)",
            self._host,
            self._port,
            self._db,
        )

    async def close(self) -> None:
        """Shut down the connection pool and cancel the sync task."""
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
            logger.info("Redis D1 sync task cancelled")

        if self._redis:
            await self._redis.aclose()
        if self._pool:
            await self._pool.aclose()
        logger.info("Redis connection pool closed")

    def get_sync_client(self):
        """Return a synchronous ``redis.Redis`` client sharing the same connection params.

        Useful for passing to synchronous handlers (e.g. :class:`~mods.logger._RedisLogHandler`)
        that cannot use async I/O.
        """
        import redis as sync_redis  # local import to keep module-level deps minimal
        return sync_redis.Redis(
            host=self._host,
            port=self._port,
            password=self._password,
            db=self._db,
            decode_responses=True,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ping_with_retry(self, retries: int = 3) -> None:
        """Ping Redis, retrying on transient connection failures."""
        for attempt in range(1, retries + 1):
            try:
                assert self._redis is not None
                await self._redis.ping()
                return
            except (RedisConnectionError, RedisError) as exc:
                logger.warning(
                    "Redis ping attempt %d/%d failed: %s", attempt, retries, exc
                )
                if attempt < retries:
                    await asyncio.sleep(1.0 * attempt)
                else:
                    raise

    async def _ensure_connected(self) -> None:
        """Reconnect if the client is not available."""
        if self._redis is None:
            await self.connect()
            return
        try:
            await self._redis.ping()
        except (RedisConnectionError, RedisError):
            logger.warning("Redis connection lost — reconnecting …")
            await self.connect()

    # ------------------------------------------------------------------
    # Public Redis API
    # ------------------------------------------------------------------

    async def get(self, key: str) -> Optional[str]:
        """Return the value at *key*, or *None* if not found."""
        await self._ensure_connected()
        assert self._redis is not None
        return await self._redis.get(key)

    async def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
    ) -> None:
        """Set *key* to *value*, with an optional TTL in seconds."""
        await self._ensure_connected()
        assert self._redis is not None
        serialised = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if ttl:
            await self._redis.setex(key, ttl, serialised)
        else:
            await self._redis.set(key, serialised)

    async def delete(self, key: str) -> int:
        """Delete *key*.  Returns the number of keys removed."""
        await self._ensure_connected()
        assert self._redis is not None
        return await self._redis.delete(key)

    async def exists(self, key: str) -> bool:
        """Return *True* if *key* exists in Redis."""
        await self._ensure_connected()
        assert self._redis is not None
        return bool(await self._redis.exists(key))

    async def keys(self, pattern: str = "*") -> list[str]:
        """Return a list of keys matching *pattern*."""
        await self._ensure_connected()
        assert self._redis is not None
        return await self._redis.keys(pattern)

    async def hset(self, name: str, mapping: dict) -> None:
        """Set multiple fields in a Redis hash."""
        await self._ensure_connected()
        assert self._redis is not None
        await self._redis.hset(name, mapping=mapping)

    async def hgetall(self, name: str) -> dict:
        """Return all fields and values in a Redis hash."""
        await self._ensure_connected()
        assert self._redis is not None
        return await self._redis.hgetall(name)

    async def lpush(self, key: str, *values: Any) -> int:
        """Prepend one or more values to a list."""
        await self._ensure_connected()
        assert self._redis is not None
        return await self._redis.lpush(key, *values)

    async def pipeline(self):
        """Return a Redis pipeline for batched commands."""
        await self._ensure_connected()
        assert self._redis is not None
        return self._redis.pipeline()

    # ------------------------------------------------------------------
    # D1 sync
    # ------------------------------------------------------------------

    def start_sync_task(self, db_client=None) -> None:
        """Start the background task that syncs ``sync:*`` keys to D1.

        Parameters
        ----------
        db_client:
            :class:`~mods.database.D1Database` instance.  If *None*, the
            instance supplied at construction time is used.  A
            :class:`RuntimeError` is raised when neither is available.
        """
        if db_client:
            self._db_client = db_client
        if self._db_client is None:
            raise RuntimeError(
                "A D1Database instance is required to start the sync task."
            )
        if self._sync_task and not self._sync_task.done():
            logger.warning("Redis sync task is already running")
            return
        self._sync_task = asyncio.create_task(self._sync_loop())
        logger.info(
            "Redis → D1 sync task started (interval=%ds)", self._sync_interval
        )

    async def _sync_loop(self) -> None:
        """Periodically sync all ``sync:*`` keys from Redis to D1."""
        while True:
            try:
                await asyncio.sleep(self._sync_interval)
                await self._sync_to_d1()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.error("Redis → D1 sync error: %s", exc, exc_info=True)

    async def _sync_to_d1(self) -> None:
        """Write all ``sync:*`` Redis keys to the D1 ``redis_cache`` table."""
        assert self._db_client is not None
        await self._ensure_connected()
        assert self._redis is not None

        # Ensure the target table exists
        await self._db_client.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_D1_SYNC_TABLE} (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                synced_at TEXT NOT NULL
            )
            """
        )

        pattern = f"{_SYNC_KEY_PREFIX}*"
        keys = await self._redis.keys(pattern)
        if not keys:
            logger.debug("Redis → D1 sync: no keys to sync")
            return

        synced = 0
        for key in keys:
            try:
                value = await self._redis.get(key)
                if value is None:
                    continue
                await self._db_client.execute(
                    f"""
                    INSERT INTO {_D1_SYNC_TABLE} (key, value, synced_at)
                    VALUES (?, ?, datetime('now'))
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        synced_at = excluded.synced_at
                    """,
                    [key, value],
                )
                synced += 1
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to sync key '%s' to D1: %s", key, exc)

        logger.info("Redis → D1 sync complete: %d/%d keys synced", synced, len(keys))


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_redis_instance: Optional[RedisClient] = None


def get_redis() -> RedisClient:
    """Return the shared :class:`RedisClient` instance.

    Raises
    ------
    RuntimeError
        If :func:`init_redis` has not been called yet.
    """
    if _redis_instance is None:
        raise RuntimeError(
            "RedisClient has not been initialised. Call init_redis() first."
        )
    return _redis_instance


async def init_redis(
    host: Optional[str] = None,
    port: Optional[int] = None,
    password: Optional[str] = None,
    db: Optional[int] = None,
    *,
    max_connections: int = 10,
    sync_interval: Optional[int] = None,
    db_client=None,
) -> "RedisClient":
    """Create and connect the shared :class:`RedisClient` instance.

    Returns
    -------
    RedisClient
        The connected instance (also accessible via :func:`get_redis`).
    """
    global _redis_instance
    _redis_instance = RedisClient(
        host=host,
        port=port,
        password=password,
        db=db,
        max_connections=max_connections,
        sync_interval=sync_interval,
        db_client=db_client,
    )
    await _redis_instance.connect()
    return _redis_instance


async def close_redis() -> None:
    """Close the shared Redis connection pool."""
    global _redis_instance
    if _redis_instance is not None:
        await _redis_instance.close()
        _redis_instance = None

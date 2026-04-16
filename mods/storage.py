"""
Unified storage module (Redis + Cloudflare D1).

This single mod manages both Redis (hot cache) and Cloudflare D1 (persistent
SQL store).  It handles:

  - Connecting to each backend automatically on start-up (either backend is
    optional; the mod degrades gracefully when one is unavailable).
  - A cache-aside ``get`` / ``set`` / ``delete`` API: reads hit Redis first
    and fall back to D1; writes go to both.
  - Direct SQL access via ``execute`` / ``fetchall`` / ``fetchone``.
  - A background task that periodically syncs ``sync:*`` Redis keys to D1.

Typical usage
-------------
::

    from mods.storage import init_storage, get_storage, close_storage

    # --- start-up ---
    store = await init_storage()          # reads all config from .env

    # cache-aside read (Redis → D1 fallback)
    value = await store.get("user:42:name")

    # cache-aside write (Redis + D1)
    await store.set("user:42:name", "Alice")

    # raw SQL
    rows = await store.fetchall("SELECT * FROM users WHERE active = ?", [1])

    # --- shutdown ---
    await close_storage()
"""

import asyncio
import json
import os
from typing import Any, Optional

import aiohttp
import redis.asyncio as aioredis
from redis.asyncio.connection import ConnectionPool
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError

from mods.logger import setup_logger

logger = setup_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CF_API_BASE = "https://api.cloudflare.com/client/v4"
_DEFAULT_RETRIES = 3
_RETRY_BACKOFF = 1.0          # seconds between D1 retry attempts
_SYNC_KEY_PREFIX = "sync:"    # Redis keys with this prefix are synced to D1
_D1_SYNC_TABLE = "redis_cache"
_DEFAULT_SYNC_INTERVAL = 60   # seconds


# ---------------------------------------------------------------------------
# DataStore
# ---------------------------------------------------------------------------

class DataStore:
    """Combined Redis + Cloudflare D1 storage backend.

    Parameters
    ----------
    redis_host, redis_port, redis_password, redis_db:
        Redis connection parameters.  Fall back to the corresponding
        ``REDIS_*`` environment variables when not provided.
    redis_max_connections:
        Maximum number of connections in the Redis pool.
    cf_account_id, cf_database_id, cf_api_token:
        Cloudflare D1 credentials.  Fall back to the corresponding
        ``CLOUDFLARE_*`` environment variables when not provided.
    d1_pool_size:
        Maximum concurrent HTTP connections used for D1 requests.
    d1_retries:
        Number of retry attempts for transient D1 errors.
    sync_interval:
        Seconds between Redis → D1 sync runs.  Falls back to
        ``REDIS_SYNC_INTERVAL`` env var.
    """

    def __init__(
        self,
        *,
        # Redis
        redis_host: Optional[str] = None,
        redis_port: Optional[int] = None,
        redis_password: Optional[str] = None,
        redis_db: Optional[int] = None,
        redis_max_connections: int = 10,
        # Cloudflare D1
        cf_account_id: Optional[str] = None,
        cf_database_id: Optional[str] = None,
        cf_api_token: Optional[str] = None,
        d1_pool_size: int = 10,
        d1_retries: int = _DEFAULT_RETRIES,
        # Sync
        sync_interval: Optional[int] = None,
    ) -> None:
        # --- Redis config ---
        self._redis_host = redis_host or os.getenv("REDIS_HOST", "localhost")
        self._redis_port = int(redis_port or os.getenv("REDIS_PORT", "6379"))
        _raw_pw = redis_password or os.getenv("REDIS_PASSWORD", "") or None
        self._redis_password = _raw_pw if _raw_pw else None
        self._redis_db = int(
            redis_db if redis_db is not None else os.getenv("REDIS_DB", "0")
        )
        self._redis_max_connections = redis_max_connections

        # --- D1 config ---
        self._cf_account_id = cf_account_id or os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
        self._cf_database_id = cf_database_id or os.getenv("CLOUDFLARE_D1_DATABASE_ID", "")
        self._cf_api_token = cf_api_token or os.getenv("CLOUDFLARE_API_TOKEN", "")
        self._d1_pool_size = d1_pool_size
        self._d1_retries = d1_retries

        # --- Sync ---
        self._sync_interval = int(
            sync_interval
            or os.getenv("REDIS_SYNC_INTERVAL", str(_DEFAULT_SYNC_INTERVAL))
        )

        # --- Internal state ---
        self._redis_pool: Optional[ConnectionPool] = None
        self._redis: Optional[aioredis.Redis] = None
        self._d1_session: Optional[aiohttp.ClientSession] = None
        self._d1_base_url: str = (
            f"{_CF_API_BASE}/accounts/{self._cf_account_id}"
            f"/d1/database/{self._cf_database_id}/query"
        )
        self._sync_task: Optional[asyncio.Task] = None

        # Availability flags (set during connect)
        self.redis_available: bool = False
        self.d1_available: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to Redis and D1.

        Each backend is attempted independently.  A failure in one does not
        prevent the other from connecting.  Check :attr:`redis_available` and
        :attr:`d1_available` after calling this method.
        """
        await self._connect_redis()
        await self._connect_d1()

        if self.redis_available and self.d1_available:
            self._start_sync_task()

        logger.info(
            "DataStore ready (redis=%s, d1=%s)",
            self.redis_available,
            self.d1_available,
        )

    async def close(self) -> None:
        """Gracefully shut down all connections and background tasks."""
        # Cancel sync task
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
            logger.info("Storage sync task cancelled")

        # Close Redis
        if self._redis:
            await self._redis.aclose()
        if self._redis_pool:
            await self._redis_pool.aclose()
            logger.info("Redis connection pool closed")

        # Close D1
        if self._d1_session and not self._d1_session.closed:
            await self._d1_session.close()
            logger.info("D1 connection pool closed")

        self.redis_available = False
        self.d1_available = False

    # ------------------------------------------------------------------
    # Redis – internal helpers
    # ------------------------------------------------------------------

    async def _connect_redis(self) -> None:
        try:
            self._redis_pool = ConnectionPool(
                host=self._redis_host,
                port=self._redis_port,
                password=self._redis_password,
                db=self._redis_db,
                max_connections=self._redis_max_connections,
                decode_responses=True,
            )
            self._redis = aioredis.Redis(connection_pool=self._redis_pool)
            await self._redis_ping_with_retry()
            self.redis_available = True
            logger.info(
                "Redis connected (host=%s, port=%d, db=%d)",
                self._redis_host,
                self._redis_port,
                self._redis_db,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Redis connection failed: %s", exc)
            self.redis_available = False

    async def _redis_ping_with_retry(self, retries: int = 3) -> None:
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

    async def _ensure_redis(self) -> None:
        """Reconnect to Redis if the connection was lost."""
        if not self.redis_available or self._redis is None:
            await self._connect_redis()
            return
        try:
            await self._redis.ping()
        except (RedisConnectionError, RedisError):
            logger.warning("Redis connection lost — reconnecting …")
            await self._connect_redis()

    def get_sync_redis_client(self):
        """Return a synchronous ``redis.Redis`` client.

        Useful for code that cannot use async I/O (e.g. logging handlers).
        The returned client shares the same host/port/db settings but uses
        its own synchronous connection pool.

        .. note::
            The logging handler requires a **synchronous** client because
            ``logging.Handler.emit`` is a synchronous method and cannot
            ``await`` coroutines.
        """
        import redis as sync_redis  # local import — keep module-level deps clean
        return sync_redis.Redis(
            host=self._redis_host,
            port=self._redis_port,
            password=self._redis_password,
            db=self._redis_db,
            decode_responses=True,
        )

    # ------------------------------------------------------------------
    # D1 – internal helpers
    # ------------------------------------------------------------------

    async def _connect_d1(self) -> None:
        missing = [
            name
            for name, val in [
                ("CLOUDFLARE_ACCOUNT_ID", self._cf_account_id),
                ("CLOUDFLARE_D1_DATABASE_ID", self._cf_database_id),
                ("CLOUDFLARE_API_TOKEN", self._cf_api_token),
            ]
            if not val
        ]
        if missing:
            logger.warning(
                "D1 not configured — missing env vars: %s", ", ".join(missing)
            )
            self.d1_available = False
            return

        try:
            connector = aiohttp.TCPConnector(limit=self._d1_pool_size)
            self._d1_session = aiohttp.ClientSession(
                connector=connector,
                headers={
                    "Authorization": f"Bearer {self._cf_api_token}",
                    "Content-Type": "application/json",
                },
            )
            self.d1_available = True
            logger.info(
                "D1 connected (account=%s, db=%s)",
                self._cf_account_id,
                self._cf_database_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("D1 connection failed: %s", exc)
            self.d1_available = False

    async def _ensure_d1(self) -> None:
        """Re-open the D1 HTTP session if it was closed."""
        if not self.d1_available or not self._d1_session or self._d1_session.closed:
            logger.warning("D1 session closed — reconnecting …")
            await self._connect_d1()

    async def _d1_request(
        self, sql: str, params: Optional[list] = None
    ) -> list[dict[str, Any]]:
        await self._ensure_d1()
        if not self.d1_available:
            raise RuntimeError("D1 is not available")

        payload: dict[str, Any] = {"sql": sql}
        if params:
            payload["params"] = params

        last_exc: Exception = RuntimeError("No attempts made")

        for attempt in range(1, self._d1_retries + 1):
            try:
                assert self._d1_session is not None
                async with self._d1_session.post(
                    self._d1_base_url, json=payload
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if not data.get("success"):
                            errors = data.get("errors", [])
                            raise RuntimeError(f"D1 query failed: {errors}")
                        results = data.get("result", [])
                        return results[0].get("results", []) if results else []
                    else:
                        body = await resp.text()
                        raise RuntimeError(f"D1 HTTP {resp.status}: {body}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                logger.warning(
                    "D1 request attempt %d/%d failed: %s",
                    attempt,
                    self._d1_retries,
                    exc,
                )
                if attempt < self._d1_retries:
                    await asyncio.sleep(_RETRY_BACKOFF * attempt)
                    if self._d1_session and not self._d1_session.closed:
                        await self._d1_session.close()
                    self._d1_session = None
                    self.d1_available = False

        raise last_exc

    # ------------------------------------------------------------------
    # Sync – internal helpers
    # ------------------------------------------------------------------

    def _start_sync_task(self) -> None:
        if self._sync_task and not self._sync_task.done():
            logger.warning("Storage sync task is already running")
            return
        self._sync_task = asyncio.create_task(self._sync_loop())
        logger.info("Redis → D1 sync task started (interval=%ds)", self._sync_interval)

    async def _sync_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._sync_interval)
                await self._sync_redis_to_d1()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.error("Redis → D1 sync error: %s", exc, exc_info=True)

    async def _sync_redis_to_d1(self) -> None:
        """Upsert all ``sync:*`` Redis keys into the D1 ``redis_cache`` table."""
        await self._ensure_redis()
        await self._ensure_d1()
        if not self.redis_available or not self.d1_available:
            logger.warning(
                "Redis → D1 sync skipped (redis=%s, d1=%s)",
                self.redis_available,
                self.d1_available,
            )
            return

        assert self._redis is not None

        # Ensure the target table exists
        await self._d1_request(
            f"""
            CREATE TABLE IF NOT EXISTS {_D1_SYNC_TABLE} (
                key       TEXT PRIMARY KEY,
                value     TEXT NOT NULL,
                synced_at TEXT NOT NULL
            )
            """
        )

        keys = await self._redis.keys(f"{_SYNC_KEY_PREFIX}*")
        if not keys:
            logger.debug("Redis → D1 sync: no keys to sync")
            return

        synced = 0
        for key in keys:
            try:
                value = await self._redis.get(key)
                if value is None:
                    continue
                await self._d1_request(
                    f"""
                    INSERT INTO {_D1_SYNC_TABLE} (key, value, synced_at)
                    VALUES (?, ?, datetime('now'))
                    ON CONFLICT(key) DO UPDATE SET
                        value     = excluded.value,
                        synced_at = excluded.synced_at
                    """,
                    [key, value],
                )
                synced += 1
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to sync key '%s' to D1: %s", key, exc)

        logger.info(
            "Redis → D1 sync complete: %d/%d keys synced", synced, len(keys)
        )

    # ------------------------------------------------------------------
    # Public cache-aside API  (Redis hot cache + D1 persistence)
    # ------------------------------------------------------------------

    async def get(self, key: str) -> Optional[str]:
        """Return the cached value for *key*.

        Look-up order:
        1. Redis (fast, in-memory)
        2. D1 (persistent, if Redis missed or is unavailable)

        Returns *None* when the key is not found in either backend.
        """
        # 1. Try Redis
        if self.redis_available:
            try:
                await self._ensure_redis()
                assert self._redis is not None
                value = await self._redis.get(key)
                if value is not None:
                    return value
            except Exception as exc:  # noqa: BLE001
                logger.warning("Redis get('%s') failed: %s", key, exc)

        # 2. Fall back to D1
        if self.d1_available:
            try:
                row = await self._d1_request(
                    f"SELECT value FROM {_D1_SYNC_TABLE} WHERE key = ?", [key]
                )
                if row:
                    value = row[0].get("value")
                    # Warm Redis cache
                    if self.redis_available and value is not None:
                        try:
                            assert self._redis is not None
                            await self._redis.set(key, value)
                        except Exception:  # noqa: BLE001
                            pass
                    return value
            except Exception as exc:  # noqa: BLE001
                logger.warning("D1 get('%s') failed: %s", key, exc)

        return None

    async def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
        *,
        persist: bool = True,
    ) -> None:
        """Store *value* under *key*.

        Writes to Redis (with optional TTL) and, when *persist* is *True*,
        also upserts the value into the D1 ``redis_cache`` table.

        Parameters
        ----------
        key:
            Storage key.
        value:
            String or JSON-serialisable value.
        ttl:
            Time-to-live in seconds for the Redis entry.
        persist:
            Whether to also write the value to D1 immediately.
        """
        serialised = (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        )

        # Write to Redis
        if self.redis_available:
            try:
                await self._ensure_redis()
                assert self._redis is not None
                if ttl:
                    await self._redis.setex(key, ttl, serialised)
                else:
                    await self._redis.set(key, serialised)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Redis set('%s') failed: %s", key, exc)

        # Write to D1
        if persist and self.d1_available:
            try:
                await self._d1_request(
                    f"""
                    CREATE TABLE IF NOT EXISTS {_D1_SYNC_TABLE} (
                        key       TEXT PRIMARY KEY,
                        value     TEXT NOT NULL,
                        synced_at TEXT NOT NULL
                    )
                    """
                )
                await self._d1_request(
                    f"""
                    INSERT INTO {_D1_SYNC_TABLE} (key, value, synced_at)
                    VALUES (?, ?, datetime('now'))
                    ON CONFLICT(key) DO UPDATE SET
                        value     = excluded.value,
                        synced_at = excluded.synced_at
                    """,
                    [key, serialised],
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("D1 set('%s') failed: %s", key, exc)

    async def delete(self, key: str) -> None:
        """Delete *key* from both Redis and D1."""
        if self.redis_available:
            try:
                await self._ensure_redis()
                assert self._redis is not None
                await self._redis.delete(key)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Redis delete('%s') failed: %s", key, exc)

        if self.d1_available:
            try:
                await self._d1_request(
                    f"DELETE FROM {_D1_SYNC_TABLE} WHERE key = ?", [key]
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("D1 delete('%s') failed: %s", key, exc)

    async def exists(self, key: str) -> bool:
        """Return *True* if *key* is present in Redis or D1."""
        if self.redis_available:
            try:
                await self._ensure_redis()
                assert self._redis is not None
                if await self._redis.exists(key):
                    return True
            except Exception as exc:  # noqa: BLE001
                logger.warning("Redis exists('%s') failed: %s", key, exc)

        if self.d1_available:
            try:
                rows = await self._d1_request(
                    f"SELECT 1 FROM {_D1_SYNC_TABLE} WHERE key = ? LIMIT 1", [key]
                )
                return bool(rows)
            except Exception as exc:  # noqa: BLE001
                logger.warning("D1 exists('%s') failed: %s", key, exc)

        return False

    # ------------------------------------------------------------------
    # Public Redis-only helpers
    # ------------------------------------------------------------------

    async def redis_keys(self, pattern: str = "*") -> list[str]:
        """Return Redis keys matching *pattern*."""
        await self._ensure_redis()
        if not self.redis_available or self._redis is None:
            return []
        return await self._redis.keys(pattern)

    async def hset(self, name: str, mapping: dict) -> None:
        """Set multiple fields in a Redis hash."""
        await self._ensure_redis()
        if not self.redis_available or self._redis is None:
            raise RuntimeError("Redis is not available")
        await self._redis.hset(name, mapping=mapping)

    async def hgetall(self, name: str) -> dict:
        """Return all fields and values from a Redis hash."""
        await self._ensure_redis()
        if not self.redis_available or self._redis is None:
            raise RuntimeError("Redis is not available")
        return await self._redis.hgetall(name)

    async def lpush(self, key: str, *values: Any) -> int:
        """Prepend one or more values to a Redis list."""
        await self._ensure_redis()
        if not self.redis_available or self._redis is None:
            raise RuntimeError("Redis is not available")
        return await self._redis.lpush(key, *values)

    async def pipeline(self):
        """Return a Redis pipeline for batched commands."""
        await self._ensure_redis()
        if not self.redis_available or self._redis is None:
            raise RuntimeError("Redis is not available")
        return self._redis.pipeline()

    # ------------------------------------------------------------------
    # Public D1 SQL API
    # ------------------------------------------------------------------

    async def execute(
        self, sql: str, params: Optional[list] = None
    ) -> list[dict[str, Any]]:
        """Execute a D1 SQL statement and return all result rows.

        Parameters
        ----------
        sql:
            SQL statement with ``?`` placeholders.
        params:
            Positional parameters to bind.

        Returns
        -------
        list[dict]
            Each dict is a row keyed by column name.
        """
        return await self._d1_request(sql, params)

    async def fetchall(
        self, sql: str, params: Optional[list] = None
    ) -> list[dict[str, Any]]:
        """Alias for :meth:`execute` — fetch all matching rows."""
        return await self.execute(sql, params)

    async def fetchone(
        self, sql: str, params: Optional[list] = None
    ) -> Optional[dict[str, Any]]:
        """Return the first D1 result row, or *None* if empty."""
        rows = await self.execute(sql, params)
        return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_store_instance: Optional[DataStore] = None


def get_storage() -> DataStore:
    """Return the shared :class:`DataStore` instance.

    Raises
    ------
    RuntimeError
        If :func:`init_storage` has not been called yet.
    """
    if _store_instance is None:
        raise RuntimeError(
            "DataStore has not been initialised. Call init_storage() first."
        )
    return _store_instance


async def init_storage(**kwargs: Any) -> DataStore:
    """Create, connect, and return the shared :class:`DataStore` instance.

    All keyword arguments are forwarded to :class:`DataStore.__init__`.
    When no arguments are supplied, all configuration is read from
    environment variables / ``.env``.

    Returns
    -------
    DataStore
        The connected instance (also accessible via :func:`get_storage`).
    """
    global _store_instance
    _store_instance = DataStore(**kwargs)
    await _store_instance.connect()
    return _store_instance


async def close_storage() -> None:
    """Close the shared :class:`DataStore` and release all resources."""
    global _store_instance
    if _store_instance is not None:
        await _store_instance.close()
        _store_instance = None

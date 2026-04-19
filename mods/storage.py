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
  - Declarative table-schema definition with column types, constraints, and
    foreign keys via :class:`TableSchema` / :class:`ColumnDef` /
    :class:`ForeignKey` and :meth:`DataStore.define_table`.

Typical usage
-------------
::

    from mods.storage import (
        init_storage, get_storage, close_storage,
        ColumnDef, ForeignKey, TableSchema,
    )

    # --- start-up ---
    store = await init_storage()          # reads all config from .env

    # enable FK enforcement (once per connection)
    await store.enable_foreign_keys()

    # define tables
    await store.define_table(TableSchema(
        name="users",
        columns=[
            ColumnDef("id",       "INTEGER", primary_key=True, autoincrement=True),
            ColumnDef("username", "TEXT",    not_null=True, unique=True),
            ColumnDef("created_at", "TEXT",  default="datetime('now')"),
        ],
    ))

    await store.define_table(TableSchema(
        name="messages",
        columns=[
            ColumnDef("id",      "INTEGER", primary_key=True, autoincrement=True),
            ColumnDef("user_id", "INTEGER", not_null=True),
            ColumnDef("content", "TEXT",    not_null=True),
        ],
        foreign_keys=[
            ForeignKey(column="user_id", ref_table="users", ref_column="id",
                       on_delete="CASCADE"),
        ],
    ))

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
from dataclasses import dataclass, field
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
_RECOVERY_COMMIT_KEY_PREFIX = "sync:recovery:commit:"


# ---------------------------------------------------------------------------
# Schema definition helpers
# ---------------------------------------------------------------------------

@dataclass
class ColumnDef:
    """Definition of a single D1 / SQLite column.

    Parameters
    ----------
    name:
        Column name.
    type:
        SQLite type affinity: ``"TEXT"``, ``"INTEGER"``, ``"REAL"``,
        ``"BLOB"``, or ``"NUMERIC"``.
    primary_key:
        Mark this column as the PRIMARY KEY.
    autoincrement:
        Add ``AUTOINCREMENT`` (only valid for ``INTEGER PRIMARY KEY``).
    not_null:
        Add a ``NOT NULL`` constraint.
    unique:
        Add a ``UNIQUE`` constraint.
    default:
        Raw SQL expression for the column default, e.g. ``"0"``,
        ``"'active'"``, or ``"datetime('now')"``.
    """

    name: str
    type: str
    primary_key: bool = False
    autoincrement: bool = False
    not_null: bool = False
    unique: bool = False
    default: Optional[str] = None

    def to_sql(self) -> str:
        """Return the SQL fragment for this column definition."""
        parts = [self.name, self.type]
        if self.primary_key:
            parts.append("PRIMARY KEY")
            if self.autoincrement:
                parts.append("AUTOINCREMENT")
        if self.not_null:
            parts.append("NOT NULL")
        if self.unique:
            parts.append("UNIQUE")
        if self.default is not None:
            parts.append(f"DEFAULT {self.default}")
        return " ".join(parts)


@dataclass
class ForeignKey:
    """A FOREIGN KEY constraint on one column of a table.

    Parameters
    ----------
    column:
        The local column that holds the foreign key.
    ref_table:
        The referenced (parent) table.
    ref_column:
        The referenced column in the parent table.
    on_delete:
        Action when the referenced row is deleted.
        One of ``"NO ACTION"`` (default), ``"RESTRICT"``, ``"CASCADE"``,
        ``"SET NULL"``, ``"SET DEFAULT"``.
    on_update:
        Action when the referenced key is updated (same options).
    """

    column: str
    ref_table: str
    ref_column: str
    on_delete: str = "NO ACTION"
    on_update: str = "NO ACTION"

    def to_sql(self) -> str:
        """Return the SQL fragment for this FOREIGN KEY constraint."""
        return (
            f"FOREIGN KEY ({self.column}) "
            f"REFERENCES {self.ref_table} ({self.ref_column}) "
            f"ON DELETE {self.on_delete} "
            f"ON UPDATE {self.on_update}"
        )


@dataclass
class TableSchema:
    """Declarative schema for a single D1 / SQLite table.

    Parameters
    ----------
    name:
        Table name.
    columns:
        Ordered list of :class:`ColumnDef` objects.
    foreign_keys:
        List of :class:`ForeignKey` constraints.  Requires FK enforcement
        to be active (call :meth:`DataStore.enable_foreign_keys` before
        DML that needs cascading behaviour).

    Example
    -------
    ::

        TableSchema(
            name="orders",
            columns=[
                ColumnDef("id",      "INTEGER", primary_key=True, autoincrement=True),
                ColumnDef("user_id", "INTEGER", not_null=True),
                ColumnDef("total",   "REAL",    not_null=True, default="0.0"),
                ColumnDef("status",  "TEXT",    not_null=True, default="'pending'"),
            ],
            foreign_keys=[
                ForeignKey("user_id", "users", "id", on_delete="CASCADE"),
            ],
        )
    """

    name: str
    columns: list[ColumnDef]
    foreign_keys: list[ForeignKey] = field(default_factory=list)

    def to_ddl(self) -> str:
        """Return a ``CREATE TABLE IF NOT EXISTS`` statement for this schema."""
        col_parts = [col.to_sql() for col in self.columns]
        fk_parts = [fk.to_sql() for fk in self.foreign_keys]
        all_parts = col_parts + fk_parts
        joined = ",\n    ".join(all_parts)
        return f"CREATE TABLE IF NOT EXISTS {self.name} (\n    {joined}\n)"


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
        self._d1_auth_failed: bool = False
        self._d1_base_url: str = (
            f"{_CF_API_BASE}/accounts/{self._cf_account_id}"
            f"/d1/database/{self._cf_database_id}/query"
        )
        self._sync_task: Optional[asyncio.Task] = None

        # Availability flags (set during connect)
        self.redis_available: bool = False
        self.d1_available: bool = False

        # Set to True once the D1 cache/sync table has been created so we
        # don't issue redundant DDL on every set() call.
        self._cache_table_ready: bool = False

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

        # Start the sync loop unconditionally.  The loop calls _ensure_redis /
        # _ensure_d1 on every iteration and self-skips when either backend is
        # unavailable, so it naturally picks up work once both come online.
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
        if self._d1_auth_failed:
            # Auth failures are not transient; skip reconnect attempts until restart.
            self.d1_available = False
            return

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
                "D1 not configured — %d required env var(s) not set", len(missing)
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

    async def _ensure_cache_table(self) -> None:
        """Create the D1 cache/sync table the first time it is needed.

        Uses a flag so the DDL is only sent once per ``DataStore`` lifetime,
        avoiding unnecessary round-trips on every :meth:`set` call.
        """
        if self._cache_table_ready:
            return
        await self._d1_request(
            f"""
            CREATE TABLE IF NOT EXISTS {_D1_SYNC_TABLE} (
                key       TEXT PRIMARY KEY,
                value     TEXT NOT NULL,
                synced_at TEXT NOT NULL
            )
            """
        )
        self._cache_table_ready = True

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
            # Re-open the session at the start of every attempt so that a
            # previous failure (which closed/nulled the session) is recovered
            # before the assert below, not after it.
            await self._ensure_d1()
            if not self.d1_available:
                raise RuntimeError("D1 is not available")

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
                        if resp.status == 401 or "Authentication error" in body:
                            # Bad/expired API token is not transient; disable D1 to
                            # avoid noisy retry loops from the sync task.
                            self._d1_auth_failed = True
                            self.d1_available = False
                            if self._d1_session and not self._d1_session.closed:
                                await self._d1_session.close()
                            self._d1_session = None
                            logger.error("D1 authentication failed; disabling D1 until restart")
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
                    # Close the stale session so _ensure_d1 opens a fresh one
                    # on the next iteration.  Do NOT set d1_available = False
                    # here — that would suppress reconnection.
                    if self._d1_session and not self._d1_session.closed:
                        await self._d1_session.close()
                    self._d1_session = None

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

        # Ensure the target table exists (no-op after the first call).
        await self._ensure_cache_table()

        synced = 0
        seen = 0
        batch: list[str] = []
        batch_size = 100
        processed_commit_guilds: set[str] = set()

        async def _process_batch(keys_batch: list[str]) -> int:
            batch_synced = 0
            for key in keys_batch:
                try:
                    value = await self._redis.get(key)  # type: ignore[union-attr]
                    if value is None:
                        continue

                    # Commit snapshots are staged in Redis first and then
                    # persisted by the background sync task.
                    if key.startswith(_RECOVERY_COMMIT_KEY_PREFIX):
                        parts = key.split(":", 5)
                        if len(parts) == 6:
                            guild_id = parts[3]
                            target_type = parts[4]
                            target_id = parts[5]
                            if target_type in {"guild", "channel", "role", "member"}:
                                try:
                                    # Validate snapshot payload is JSON before storing.
                                    json.loads(value)
                                    if guild_id not in processed_commit_guilds:
                                        await self._d1_request(
                                            "DELETE FROM structure_snapshots WHERE guild_id = ? AND pinned = 2",
                                            [guild_id],
                                        )
                                        processed_commit_guilds.add(guild_id)
                                    await self._d1_request(
                                        """
                                        INSERT INTO structure_snapshots
                                            (guild_id, target_type, target_id, snapshot_data, timestamp, pinned)
                                        VALUES (?, ?, ?, ?, (strftime('%s', 'now') - 301), 2)
                                        """,
                                        [guild_id, target_type, target_id, value],
                                    )
                                except Exception as exc:  # noqa: BLE001
                                    logger.error(
                                        "Failed to persist recovery commit key '%s': %s",
                                        key,
                                        exc,
                                    )

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
                    batch_synced += 1
                except Exception as exc:  # noqa: BLE001
                    logger.error("Failed to sync key '%s' to D1: %s", key, exc)
            return batch_synced

        async for key in self._redis.scan_iter(
            match=f"{_SYNC_KEY_PREFIX}*", count=batch_size
        ):
            seen += 1
            batch.append(key)
            if len(batch) >= batch_size:
                synced += await _process_batch(batch)
                batch.clear()

        if batch:
            synced += await _process_batch(batch)

        if seen == 0:
            logger.debug("Redis → D1 sync: no keys to sync")
            return

        logger.info("Redis → D1 sync complete: %d/%d keys synced", synced, seen)

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
                await self._ensure_cache_table()
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

    async def enable_foreign_keys(self) -> None:
        """Enable SQLite foreign-key enforcement for the current D1 session.

        Cloudflare D1 is built on SQLite, which disables foreign-key
        enforcement by default.  Call this method once after connecting (or
        whenever you need cascading deletes / updates to take effect).

        .. note::
            D1 currently applies ``PRAGMA`` statements per-query rather than
            per-connection; call this before any DML that relies on FK
            cascading behaviour.
        """
        await self._d1_request("PRAGMA foreign_keys = ON")
        logger.info("D1 foreign-key enforcement enabled")

    async def define_table(self, schema: TableSchema) -> None:
        """Create a D1 table from a :class:`TableSchema` if it does not exist.

        Builds and executes a ``CREATE TABLE IF NOT EXISTS`` DDL statement that
        includes all column definitions and any FOREIGN KEY constraints
        declared in *schema*.

        Parameters
        ----------
        schema:
            The :class:`TableSchema` describing the table to create.

        Example
        -------
        ::

            await store.enable_foreign_keys()

            await store.define_table(TableSchema(
                name="users",
                columns=[
                    ColumnDef("id",       "INTEGER", primary_key=True, autoincrement=True),
                    ColumnDef("username", "TEXT",    not_null=True, unique=True),
                ],
            ))

            await store.define_table(TableSchema(
                name="posts",
                columns=[
                    ColumnDef("id",      "INTEGER", primary_key=True, autoincrement=True),
                    ColumnDef("user_id", "INTEGER", not_null=True),
                    ColumnDef("body",    "TEXT",    not_null=True),
                ],
                foreign_keys=[
                    ForeignKey("user_id", "users", "id", on_delete="CASCADE"),
                ],
            ))
        """
        ddl = schema.to_ddl()
        logger.debug("Defining table '%s':\n%s", schema.name, ddl)
        await self._d1_request(ddl)
        logger.info(
            "Table '%s' defined (%d columns, %d FK%s)",
            schema.name,
            len(schema.columns),
            len(schema.foreign_keys),
            "s" if len(schema.foreign_keys) != 1 else "",
        )


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

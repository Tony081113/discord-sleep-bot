"""
Cloudflare D1 database module.

Wraps the Cloudflare D1 REST API with:
  - An aiohttp connection pool (TCPConnector)
  - Automatic reconnection / retry on transient errors
  - A simple public API consumed by the rest of the application
"""

import asyncio
import os
from typing import Any, Optional

import aiohttp

from mods.logger import setup_logger

logger = setup_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CF_API_BASE = "https://api.cloudflare.com/client/v4"
_DEFAULT_RETRIES = 3
_RETRY_BACKOFF = 1.0  # seconds


# ---------------------------------------------------------------------------
# D1Database
# ---------------------------------------------------------------------------

class D1Database:
    """Async Cloudflare D1 client with a shared aiohttp connection pool.

    Usage
    -----
    Instantiate once at application start, call :meth:`connect`, then use
    :meth:`execute` / :meth:`fetchall` / :meth:`fetchone` throughout the
    application.  Call :meth:`close` on shutdown.
    """

    def __init__(
        self,
        account_id: Optional[str] = None,
        database_id: Optional[str] = None,
        api_token: Optional[str] = None,
        *,
        pool_size: int = 10,
        retries: int = _DEFAULT_RETRIES,
    ) -> None:
        self._account_id = account_id or os.environ["CLOUDFLARE_ACCOUNT_ID"]
        self._database_id = database_id or os.environ["CLOUDFLARE_D1_DATABASE_ID"]
        self._api_token = api_token or os.environ["CLOUDFLARE_API_TOKEN"]
        self._pool_size = pool_size
        self._retries = retries

        self._session: Optional[aiohttp.ClientSession] = None
        self._base_url = (
            f"{_CF_API_BASE}/accounts/{self._account_id}"
            f"/d1/database/{self._database_id}/query"
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Initialise the connection pool."""
        if self._session and not self._session.closed:
            return

        connector = aiohttp.TCPConnector(limit=self._pool_size)
        headers = {
            "Authorization": f"Bearer {self._api_token}",
            "Content-Type": "application/json",
        }
        self._session = aiohttp.ClientSession(
            connector=connector,
            headers=headers,
        )
        logger.info("D1 connection pool initialised (size=%d)", self._pool_size)

    async def close(self) -> None:
        """Close the connection pool."""
        if self._session and not self._session.closed:
            await self._session.close()
            logger.info("D1 connection pool closed")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_connected(self) -> None:
        """Re-open the session if it has been closed."""
        if not self._session or self._session.closed:
            logger.warning("D1 session closed — reconnecting …")
            await self.connect()

    async def _request(
        self, sql: str, params: Optional[list] = None
    ) -> list[dict[str, Any]]:
        """Send a query to D1 and return the rows from the first result set."""
        await self._ensure_connected()

        payload: dict[str, Any] = {"sql": sql}
        if params:
            payload["params"] = params

        last_exc: Exception = RuntimeError("No attempts made")

        for attempt in range(1, self._retries + 1):
            try:
                assert self._session is not None
                async with self._session.post(self._base_url, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if not data.get("success"):
                            errors = data.get("errors", [])
                            raise RuntimeError(f"D1 query failed: {errors}")
                        results = data.get("result", [])
                        return results[0].get("results", []) if results else []
                    else:
                        body = await resp.text()
                        raise RuntimeError(
                            f"D1 HTTP {resp.status}: {body}"
                        )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                logger.warning(
                    "D1 request attempt %d/%d failed: %s",
                    attempt,
                    self._retries,
                    exc,
                )
                if attempt < self._retries:
                    await asyncio.sleep(_RETRY_BACKOFF * attempt)
                    # Force session re-creation on network errors
                    await self._session.close()
                    self._session = None

        raise last_exc

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(
        self, sql: str, params: Optional[list] = None
    ) -> list[dict[str, Any]]:
        """Execute *sql* and return all result rows.

        Parameters
        ----------
        sql:
            SQL statement (use ``?`` placeholders for parameters).
        params:
            Positional parameters to bind.

        Returns
        -------
        list[dict]
            Each dict is a row keyed by column name.
        """
        return await self._request(sql, params)

    async def fetchall(
        self, sql: str, params: Optional[list] = None
    ) -> list[dict[str, Any]]:
        """Alias for :meth:`execute` — fetch all matching rows."""
        return await self.execute(sql, params)

    async def fetchone(
        self, sql: str, params: Optional[list] = None
    ) -> Optional[dict[str, Any]]:
        """Return the first matching row, or *None* if the result set is empty."""
        rows = await self.execute(sql, params)
        return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_db_instance: Optional[D1Database] = None


def get_db() -> D1Database:
    """Return the shared :class:`D1Database` instance.

    Raises
    ------
    RuntimeError
        If :func:`init_db` has not been called yet.
    """
    if _db_instance is None:
        raise RuntimeError(
            "D1Database has not been initialised. Call init_db() first."
        )
    return _db_instance


async def init_db(
    account_id: Optional[str] = None,
    database_id: Optional[str] = None,
    api_token: Optional[str] = None,
    *,
    pool_size: int = 10,
    retries: int = _DEFAULT_RETRIES,
) -> D1Database:
    """Create and connect the shared :class:`D1Database` instance.

    Returns
    -------
    D1Database
        The connected instance (also accessible via :func:`get_db`).
    """
    global _db_instance
    _db_instance = D1Database(
        account_id=account_id,
        database_id=database_id,
        api_token=api_token,
        pool_size=pool_size,
        retries=retries,
    )
    await _db_instance.connect()
    return _db_instance


async def close_db() -> None:
    """Close the shared database connection pool."""
    global _db_instance
    if _db_instance is not None:
        await _db_instance.close()
        _db_instance = None

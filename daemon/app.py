"""R2 guardian daemon service.

Flow:
DB-mod (this repo) -> daemon -> Cloudflare R2

The daemon has its own .env file in daemon/.env and performs token-based
mutual verification with the DB-mod client.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

import aiohttp
import boto3
import time
from aiohttp import web
from botocore.config import Config
from dotenv import load_dotenv

# Support both `python -m daemon.app` and `python daemon/app.py`.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    # When running with: python -m daemon.app
    from daemon.db_init import ensure_daemon_db_initialized
except Exception:  # noqa: BLE001
    # When running with: python daemon/app.py
    from db_init import ensure_daemon_db_initialized
from mods.logger import setup_logger

_DAEMON_ENV = Path(__file__).with_name(".env")
if _DAEMON_ENV.exists():
    load_dotenv(dotenv_path=_DAEMON_ENV)

logger = setup_logger("daemon.r2_guardian")

_BYTES_PER_GB = 1024 ** 3
_DEFAULT_GLOBAL_QUOTA_GB = 10


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _build_s3_client():
    endpoint = _required_env("R2_ENDPOINT")
    access_key = _required_env("R2_ACCESS_KEY_ID")
    secret_key = _required_env("R2_SECRET_ACCESS_KEY")
    region = os.getenv("R2_REGION", "auto")

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(signature_version="s3v4"),
    )


def _token_headers(self_token: str) -> dict[str, str]:
    return {
        "X-DAEMON-TOKEN": self_token,
    }


# ---------------------------------------------------------------------------
# R2 Quota Manager
# ---------------------------------------------------------------------------

class R2QuotaManager:
    """Async R2 usage tracker with global quota enforcement and Discord DM alerts."""

    def __init__(
        self,
        s3_client,
        bucket: str,
        global_quota_gb: float,
        discord_bot_token: str,
        discord_dev_user_id: str,
    ) -> None:
        self._s3 = s3_client
        self._bucket = bucket
        self._global_quota_bytes = int(global_quota_gb * _BYTES_PER_GB)
        self._discord_bot_token = discord_bot_token
        self._discord_dev_user_id = discord_dev_user_id
        self._locked = False
        self._lock_reason: str = ""
        self._last_checked: float = 0.0
        self._cache_ttl: float = 60.0
        self._cached_total: int = 0
        self._check_lock = asyncio.Lock()

    @property
    def locked(self) -> bool:
        return self._locked

    @property
    def lock_reason(self) -> str:
        return self._lock_reason

    def _list_all_objects(self, prefix: str = "") -> int:
        """Synchronously list all objects and return total size in bytes."""
        total = 0
        kwargs: dict[str, Any] = {"Bucket": self._bucket}
        if prefix:
            kwargs["Prefix"] = prefix
        while True:
            resp = self._s3.list_objects_v2(**kwargs)
            for obj in resp.get("Contents", []):
                total += obj.get("Size", 0)
            if not resp.get("IsTruncated"):
                break
            kwargs["ContinuationToken"] = resp["NextContinuationToken"]
        return total

    async def get_total_usage_bytes(self, *, force: bool = False) -> int:
        """Return total R2 bucket usage in bytes (cached for 60 s)."""
        now = time.monotonic()
        if not force and (now - self._last_checked) < self._cache_ttl:
            return self._cached_total
        total = await asyncio.to_thread(self._list_all_objects)
        self._cached_total = total
        self._last_checked = now
        return total

    async def get_guild_usage_bytes(self, guild_id: str) -> int:
        """Return total bytes used by a specific guild (prefix: {guild_id}/)."""
        return await asyncio.to_thread(self._list_all_objects, f"{guild_id}/")

    async def check_and_enforce_global_quota(self) -> bool:
        """Check total usage; lock R2 and notify dev if over quota. Returns True if ok."""
        async with self._check_lock:
            total = await self.get_total_usage_bytes(force=True)
            if total >= self._global_quota_bytes:
                if not self._locked:
                    self._locked = True
                    self._lock_reason = (
                        f"R2 total usage {total / _BYTES_PER_GB:.2f} GB exceeds "
                        f"global quota {self._global_quota_bytes / _BYTES_PER_GB:.1f} GB"
                    )
                    logger.warning("R2 locked: %s", self._lock_reason)
                    asyncio.create_task(self._notify_developer(self._lock_reason))
                return False
            return True

    async def _notify_developer(self, message: str) -> None:
        """Send Discord DM to developer via bot token."""
        if not self._discord_bot_token or not self._discord_dev_user_id:
            logger.warning("Discord DM notification skipped: token or dev user ID not set")
            return
        headers = {
            "Authorization": f"Bot {self._discord_bot_token}",
            "Content-Type": "application/json",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://discord.com/api/v10/users/@me/channels",
                    headers=headers,
                    json={"recipient_id": self._discord_dev_user_id},
                ) as resp:
                    if resp.status not in (200, 201):
                        logger.error(
                            "Failed to open DM channel: status=%d body=%s",
                            resp.status, await resp.text(),
                        )
                        return
                    dm_channel_id = (await resp.json())["id"]
                async with session.post(
                    f"https://discord.com/api/v10/channels/{dm_channel_id}/messages",
                    headers=headers,
                    json={"content": f"\U0001f512 **R2 Guardian Alert**\n{message}"},
                ) as resp:
                    if resp.status not in (200, 201):
                        logger.error(
                            "Failed to send DM: status=%d body=%s",
                            resp.status, await resp.text(),
                        )
                    else:
                        logger.info("Developer notified via Discord DM")
        except Exception as exc:  # noqa: BLE001
            logger.error("Discord DM notification error: %s", exc, exc_info=True)


@web.middleware
async def auth_middleware(request: web.Request, handler):
    # Keep /health public for platform probes.
    if request.path == "/health":
        return await handler(request)

    expected_dbmod_token = request.app["expected_dbmod_token"]
    daemon_self_token = request.app["daemon_self_token"]

    incoming = request.headers.get("X-DBMOD-TOKEN", "")
    if not expected_dbmod_token or incoming != expected_dbmod_token:
        return web.json_response(
            {"ok": False, "error": "unauthorized"},
            status=401,
            headers=_token_headers(daemon_self_token),
        )

    response: web.StreamResponse = await handler(request)
    response.headers["X-DAEMON-TOKEN"] = daemon_self_token
    return response


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "r2-guardian"})


async def handshake(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "r2-guardian", "auth": "dbmod-token-verified"})


async def init_db(request: web.Request) -> web.Response:
    db_path = request.app["daemon_meta_db_path"]
    result = await asyncio.to_thread(ensure_daemon_db_initialized, db_path)
    return web.json_response(result)


async def get_usage(request: web.Request) -> web.Response:
    """Return total R2 usage and lock status."""
    qm: R2QuotaManager = request.app["quota_manager"]
    total = await qm.get_total_usage_bytes(force=True)
    return web.json_response({
        "ok": True,
        "total_bytes": total,
        "total_gb": round(total / _BYTES_PER_GB, 4),
        "locked": qm.locked,
        "lock_reason": qm.lock_reason,
    })


async def get_guild_usage(request: web.Request) -> web.Response:
    """Return R2 usage for a specific guild."""
    payload = await request.json()
    guild_id = str(payload.get("guild_id", "")).strip()
    if not guild_id:
        return web.json_response({"ok": False, "error": "guild_id is required"}, status=400)
    qm: R2QuotaManager = request.app["quota_manager"]
    used = await qm.get_guild_usage_bytes(guild_id)
    return web.json_response({
        "ok": True,
        "guild_id": guild_id,
        "used_bytes": used,
        "used_mb": round(used / (1024 ** 2), 2),
    })


async def put_object(request: web.Request) -> web.Response:
    qm: R2QuotaManager = request.app["quota_manager"]

    # --- global quota lock check ---
    if qm.locked:
        return web.json_response(
            {"ok": False, "error": "r2_locked", "detail": qm.lock_reason},
            status=507,
        )

    payload = await request.json()

    guild_id = str(payload.get("guild_id", "")).strip()
    upload_type = str(payload.get("upload_type", "files")).strip()  # "avatars" or "files"
    raw_key = str(payload.get("key", "")).strip()
    bucket = str(payload.get("bucket") or request.app["r2_bucket"]).strip()
    content_type = str(payload.get("content_type") or "application/octet-stream")

    guild_quota_mb: float | None = None
    if "guild_quota_mb" in payload:
        try:
            guild_quota_mb = float(payload["guild_quota_mb"])
        except (ValueError, TypeError):
            pass

    if not raw_key:
        return web.json_response({"ok": False, "error": "key is required"}, status=400)
    if not bucket:
        return web.json_response({"ok": False, "error": "bucket is required"}, status=400)

    # Canonical key: {guild_id}/{upload_type}/{raw_key} when guild_id is present
    key = f"{guild_id}/{upload_type}/{raw_key}" if guild_id else raw_key

    data: bytes
    if "json_body" in payload:
        data = json.dumps(payload["json_body"], ensure_ascii=False).encode("utf-8")
        if "content_type" not in payload:
            content_type = "application/json; charset=utf-8"
    elif "body_b64" in payload:
        try:
            data = base64.b64decode(str(payload["body_b64"]), validate=True)
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": "invalid body_b64"}, status=400)
    else:
        return web.json_response(
            {"ok": False, "error": "either json_body or body_b64 is required"},
            status=400,
        )

    # --- per-guild quota check ---
    if guild_id and guild_quota_mb is not None:
        guild_used = await qm.get_guild_usage_bytes(guild_id)
        quota_bytes = int(guild_quota_mb * 1024 * 1024)
        if guild_used + len(data) > quota_bytes:
            return web.json_response(
                {
                    "ok": False,
                    "error": "guild_quota_exceeded",
                    "detail": (
                        f"Guild {guild_id} used {guild_used / (1024 ** 2):.1f} MB"
                        f" / {guild_quota_mb:.0f} MB quota"
                    ),
                    "used_bytes": guild_used,
                    "quota_bytes": quota_bytes,
                },
                status=507,
            )

    s3 = request.app["s3_client"]

    def _upload() -> dict[str, Any]:
        result = s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        return {
            "etag": str(result.get("ETag", "")).strip('"'),
            "version_id": result.get("VersionId"),
        }

    uploaded = await asyncio.to_thread(_upload)

    # Background global quota check after every upload (non-blocking)
    asyncio.create_task(qm.check_and_enforce_global_quota())

    return web.json_response(
        {
            "ok": True,
            "bucket": bucket,
            "key": key,
            "content_type": content_type,
            "bytes": len(data),
            **uploaded,
        }
    )


def create_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware])

    app["expected_dbmod_token"] = _required_env("DAEMON_EXPECTED_DBMOD_TOKEN")
    app["daemon_self_token"] = _required_env("DAEMON_SELF_TOKEN")
    app["r2_bucket"] = _required_env("R2_BUCKET")
    app["daemon_meta_db_path"] = os.getenv("DAEMON_META_DB_PATH", "daemon/daemon_state.db")

    s3 = _build_s3_client()
    app["s3_client"] = s3

    global_quota_gb = float(os.getenv("R2_GLOBAL_QUOTA_GB", str(_DEFAULT_GLOBAL_QUOTA_GB)))
    app["quota_manager"] = R2QuotaManager(
        s3_client=s3,
        bucket=app["r2_bucket"],
        global_quota_gb=global_quota_gb,
        discord_bot_token=os.getenv("DISCORD_BOT_TOKEN", "").strip(),
        discord_dev_user_id=os.getenv("DISCORD_DEV_USER_ID", "").strip(),
    )

    app.router.add_get("/health", health)
    app.router.add_post("/v1/handshake", handshake)
    app.router.add_post("/v1/init-db", init_db)
    app.router.add_get("/v1/r2/usage", get_usage)
    app.router.add_post("/v1/r2/guild-usage", get_guild_usage)
    app.router.add_post("/v1/r2/put", put_object)

    return app


def main() -> None:
    host = os.getenv("DAEMON_HOST", "0.0.0.0")
    port = int(os.getenv("DAEMON_PORT", "8091"))

    logger.info("Starting R2 guardian daemon on %s:%d", host, port)
    web.run_app(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()

"""DB-mod to guardian-daemon bridge.

This module provides a thin async client for:
DB-mod -> guardian daemon -> Cloudflare R2

Mutual token verification:
- request header: X-DBMOD-TOKEN
- response header: X-DAEMON-TOKEN (must match expected token)
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

import aiohttp

from mods.logger import setup_logger

logger = setup_logger(__name__)


class DaemonAuthError(RuntimeError):
    """Raised when daemon token verification fails."""


class DaemonBridge:
    """Async client for guardian daemon APIs."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        dbmod_token: str | None = None,
        expected_daemon_token: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("DBMOD_DAEMON_URL", "http://127.0.0.1:8091")).rstrip("/")
        self.dbmod_token = (dbmod_token or os.getenv("DBMOD_DAEMON_TOKEN", "")).strip()
        self.expected_daemon_token = (
            expected_daemon_token or os.getenv("DBMOD_EXPECT_DAEMON_TOKEN", "")
        ).strip()
        self.timeout_seconds = float(timeout_seconds or os.getenv("DBMOD_DAEMON_TIMEOUT", "10"))

    def enabled(self) -> bool:
        return bool(self.base_url and self.dbmod_token and self.expected_daemon_token)

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-DBMOD-TOKEN": self.dbmod_token,
        }

    def _verify_daemon_token(self, response: aiohttp.ClientResponse) -> None:
        token = response.headers.get("X-DAEMON-TOKEN", "").strip()
        if token != self.expected_daemon_token:
            raise DaemonAuthError("daemon token mismatch")

    async def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        url = f"{self.base_url}{path}"

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=self._headers(), data=json.dumps(payload)) as resp:
                self._verify_daemon_token(resp)
                body = await resp.json(content_type=None)
                if resp.status >= 400:
                    raise RuntimeError(f"daemon error {resp.status}: {body}")
                return body

    async def _get_json(self, path: str) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        url = f"{self.base_url}{path}"

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=self._headers()) as resp:
                self._verify_daemon_token(resp)
                body = await resp.json(content_type=None)
                if resp.status >= 400:
                    raise RuntimeError(f"daemon error {resp.status}: {body}")
                return body

    async def handshake(self) -> dict[str, Any]:
        return await self._post_json("/v1/handshake", {})

    async def init_daemon_db(self) -> dict[str, Any]:
        return await self._post_json("/v1/init-db", {})

    async def put_json_to_r2(
        self,
        *,
        key: str,
        payload: Any,
        bucket: str | None = None,
    ) -> dict[str, Any]:
        request_payload: dict[str, Any] = {
            "key": key,
            "json_body": payload,
        }
        if bucket:
            request_payload["bucket"] = bucket
        return await self._post_json("/v1/r2/put", request_payload)

    async def put_bytes_to_r2(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        bucket: str | None = None,
    ) -> dict[str, Any]:
        request_payload: dict[str, Any] = {
            "key": key,
            "body_b64": base64.b64encode(data).decode("ascii"),
            "content_type": content_type,
        }
        if bucket:
            request_payload["bucket"] = bucket
        return await self._post_json("/v1/r2/put", request_payload)

    async def upload_file(
        self,
        *,
        guild_id: str,
        filename: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        bucket: str | None = None,
        guild_quota_mb: float | None = None,
    ) -> dict[str, Any]:
        """Upload a file to R2 under {guild_id}/files/{filename}."""
        request_payload: dict[str, Any] = {
            "key": filename,
            "guild_id": guild_id,
            "upload_type": "files",
            "body_b64": base64.b64encode(data).decode("ascii"),
            "content_type": content_type,
        }
        if bucket:
            request_payload["bucket"] = bucket
        if guild_quota_mb is not None:
            request_payload["guild_quota_mb"] = guild_quota_mb
        return await self._post_json("/v1/r2/put", request_payload)

    async def upload_avatar(
        self,
        *,
        guild_id: str,
        user_id: str,
        data: bytes,
        ext: str = "webp",
        bucket: str | None = None,
        guild_quota_mb: float | None = None,
    ) -> dict[str, Any]:
        """Upload a user avatar to R2 under {guild_id}/avatars/{user_id}.{ext}."""
        request_payload: dict[str, Any] = {
            "key": f"{user_id}.{ext}",
            "guild_id": guild_id,
            "upload_type": "avatars",
            "body_b64": base64.b64encode(data).decode("ascii"),
            "content_type": f"image/{ext}",
        }
        if bucket:
            request_payload["bucket"] = bucket
        if guild_quota_mb is not None:
            request_payload["guild_quota_mb"] = guild_quota_mb
        return await self._post_json("/v1/r2/put", request_payload)

    async def get_usage(self) -> dict[str, Any]:
        """Return total R2 usage and lock status from daemon."""
        return await self._get_json("/v1/r2/usage")

    async def get_guild_usage(self, guild_id: str) -> dict[str, Any]:
        """Return R2 usage for a specific guild."""
        return await self._post_json("/v1/r2/guild-usage", {"guild_id": guild_id})


def get_daemon_bridge() -> DaemonBridge:
    return DaemonBridge()

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

import boto3
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


async def put_object(request: web.Request) -> web.Response:
    payload = await request.json()

    key = str(payload.get("key", "")).strip()
    bucket = str(payload.get("bucket") or request.app["r2_bucket"]).strip()
    content_type = str(payload.get("content_type") or "application/octet-stream")

    if not key:
        return web.json_response({"ok": False, "error": "key is required"}, status=400)
    if not bucket:
        return web.json_response({"ok": False, "error": "bucket is required"}, status=400)

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
    app["s3_client"] = _build_s3_client()

    app.router.add_get("/health", health)
    app.router.add_post("/v1/handshake", handshake)
    app.router.add_post("/v1/init-db", init_db)
    app.router.add_post("/v1/r2/put", put_object)

    return app


def main() -> None:
    host = os.getenv("DAEMON_HOST", "0.0.0.0")
    port = int(os.getenv("DAEMON_PORT", "8091"))

    logger.info("Starting R2 guardian daemon on %s:%d", host, port)
    web.run_app(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()

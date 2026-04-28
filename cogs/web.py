"""Web 管理面板模組。

以 aiohttp 在 Discord Bot 旁啟動管理介面服務，提供：
- Discord OAuth2 登入
- 復原請求核准 / 手動復原觸發
- 各伺服器異常門檻設定
- 維運與審計資訊查詢

必要 .env 參數
----------------------
DISCORD_CLIENT_ID      — OAuth2 應用程式 Client ID
DISCORD_CLIENT_SECRET  — OAuth2 應用程式密鑰
WEB_PORT               — 服務監聽埠（預設 8080）
WEB_SECRET             — Cookie 簽章金鑰，至少 32 字元
WEB_BASE_URL           — 對外網址，例如 https://panel.example.com
"""

import base64
import collections
import functools
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import pathlib
import secrets
import time
from typing import Any

import psutil

import aiohttp
import discord
from aiohttp import web
from aiohttp.abc import AbstractAccessLogger
from discord.ext import commands

from mods.defense import (
    DefenseStorageError,
    DEFAULT_DISABLE_SECONDS,
    get_defense_state,
    set_defense_disabled,
    set_defense_enabled,
)
from mods.logger import setup_logger
from mods.storage import get_storage

logger = setup_logger(__name__)

# ── 系統資源監控（網路 I/O delta 追蹤） ────────────────
_net_prev: dict = {}   # {"bytes_sent": int, "bytes_recv": int, "ts": float}
_bot_proc = psutil.Process()

# ── Discord OAuth2 端點 ─────────────────────────────
_DISCORD_AUTH = "https://discord.com/api/oauth2/authorize"
_DISCORD_TOKEN = "https://discord.com/api/oauth2/token"
_DISCORD_USER = "https://discord.com/api/v10/users/@me"

# ═════════════════════════════════════════════════════════
#  設定值（載入模組時讀取一次） ─────────────────────────
# ═════════════════════════════════════════════════════════
_HOST = os.getenv("WEB_HOST", "0.0.0.0")
_PORT = int(os.getenv("WEB_PORT", "8080"))
_BASE_URL = (os.getenv("WEB_BASE_URL") or "").rstrip("/") or f"http://localhost:{_PORT}"
_SECRET = os.getenv("WEB_SECRET", "")
_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
_DEVELOPER_IDS = set(
    str(uid.strip())
    for uid in os.getenv("BOT_ADMIN_ID", "").split(",")
    if uid.strip()
)
logger.info(f"Loaded _DEVELOPER_IDS: {_DEVELOPER_IDS}")

if not _SECRET or len(_SECRET) < 32:
    _SECRET = secrets.token_hex(32)
    logger.warning(
        "WEB_SECRET not set or too short — generated random key "
        "(sessions will not survive restarts)"
    )

_REDIRECT_URI = f"{_BASE_URL}/auth/callback"
_COOKIE = "sb_sess"
_COOKIE_AGE = 86400 * 7  # 7 days


def _env_bool(name: str, default: bool) -> bool:
    """從環境變數解析布林值。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


_COOKIE_SECURE = _env_bool("WEB_COOKIE_SECURE", _BASE_URL.startswith("https://"))


def _is_developer_user_id(user_id: str | None) -> bool:
    """判斷指定 user_id 是否屬於開發者名單。"""
    return bool(user_id) and str(user_id) in _DEVELOPER_IDS


def _parse_proxy_allowlist(raw: str) -> tuple[ipaddress._BaseNetwork, ...]:
    """解析 WEB_TRUSTED_PROXIES（逗號分隔 IP 或 CIDR）。"""
    networks: list[ipaddress._BaseNetwork] = []
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            if "/" in candidate:
                networks.append(ipaddress.ip_network(candidate, strict=False))
            else:
                addr = ipaddress.ip_address(candidate)
                suffix = "/32" if addr.version == 4 else "/128"
                networks.append(ipaddress.ip_network(f"{candidate}{suffix}", strict=False))
        except ValueError:
            logger.warning("Invalid WEB_TRUSTED_PROXIES entry ignored: %s", candidate)
    return tuple(networks)


_TRUSTED_PROXY_NETWORKS = _parse_proxy_allowlist(
    os.getenv("WEB_TRUSTED_PROXIES", "127.0.0.1,::1")
)

_WEB_DIR = pathlib.Path(__file__).resolve().parent.parent / "web"

# 預設異常門檻（需與 cogs/monitoring.py 同步）
_DEFAULT_THRESHOLDS: dict[str, int] = {
    "channel_create": 8,
    "channel_delete": 3,
    "channel_update": 5,
    "webhook_create": 3,
    "role_create": 10,
    "role_delete": 3,
    "role_update": 5,
    "admin_perm_remove": 2,
    "message_spam": 8,
    "attacker_rejoin": 1,
}
_THRESHOLD_LABELS: dict[str, str] = {
    "channel_create": "頻道建立",
    "channel_delete": "頻道刪除",
    "channel_update": "頻道修改",
    "webhook_create": "Webhook 建立",
    "role_create": "身分組建立",
    "role_delete": "身分組刪除",
    "role_update": "身分組修改",
    "admin_perm_remove": "管理員權限移除",
    "message_spam": "訊息轟炸",
    "attacker_rejoin": "攻擊者重返",
}
_THRESHOLD_WINDOWS: dict[str, int] = {
    "channel_create": 300,
    "channel_delete": 300,
    "channel_update": 300,
    "webhook_create": 300,
    "role_create": 300,
    "role_delete": 300,
    "role_update": 300,
    "admin_perm_remove": 300,
    "message_spam": 10,
    "attacker_rejoin": 120,
}

_THRESHOLD_WINDOW_MIN_SECONDS = 1
_THRESHOLD_WINDOW_MAX_SECONDS = 3600
_THRESHOLD_SCHEMA_READY = False

# 全 API 限速（依危險等級切不同門檻與封鎖時間）
_API_LIMIT_LOW_COUNT = int(os.getenv("API_LIMIT_LOW_COUNT", "120"))
_API_LIMIT_LOW_WINDOW = int(os.getenv("API_LIMIT_LOW_WINDOW_SECONDS", "60"))
_API_LIMIT_MEDIUM_COUNT = int(os.getenv("API_LIMIT_MEDIUM_COUNT", "60"))
_API_LIMIT_MEDIUM_WINDOW = int(os.getenv("API_LIMIT_MEDIUM_WINDOW_SECONDS", "60"))
_API_LIMIT_HIGH_COUNT = int(os.getenv("API_LIMIT_HIGH_COUNT", "20"))
_API_LIMIT_HIGH_WINDOW = int(os.getenv("API_LIMIT_HIGH_WINDOW_SECONDS", "60"))
_API_LIMIT_CRITICAL_COUNT = int(os.getenv("API_LIMIT_CRITICAL_COUNT", "10"))
_API_LIMIT_CRITICAL_WINDOW = int(os.getenv("API_LIMIT_CRITICAL_WINDOW_SECONDS", "60"))

_API_BLOCK_LOW_SECONDS = int(os.getenv("API_BLOCK_LOW_SECONDS", "15"))
_API_BLOCK_MEDIUM_SECONDS = int(os.getenv("API_BLOCK_MEDIUM_SECONDS", "60"))
_API_BLOCK_HIGH_SECONDS = int(os.getenv("API_BLOCK_HIGH_SECONDS", "300"))
_API_BLOCK_CRITICAL_SECONDS = int(os.getenv("API_BLOCK_CRITICAL_SECONDS", "900"))

_API_RATE_PROFILES: dict[str, dict[str, int]] = {
    "low": {
        "count": max(1, _API_LIMIT_LOW_COUNT),
        "window": max(1, _API_LIMIT_LOW_WINDOW),
        "block": max(1, _API_BLOCK_LOW_SECONDS),
    },
    "medium": {
        "count": max(1, _API_LIMIT_MEDIUM_COUNT),
        "window": max(1, _API_LIMIT_MEDIUM_WINDOW),
        "block": max(1, _API_BLOCK_MEDIUM_SECONDS),
    },
    "high": {
        "count": max(1, _API_LIMIT_HIGH_COUNT),
        "window": max(1, _API_LIMIT_HIGH_WINDOW),
        "block": max(1, _API_BLOCK_HIGH_SECONDS),
    },
    "critical": {
        "count": max(1, _API_LIMIT_CRITICAL_COUNT),
        "window": max(1, _API_LIMIT_CRITICAL_WINDOW),
        "block": max(1, _API_BLOCK_CRITICAL_SECONDS),
    },
}


def _api_limit_level(req: web.Request) -> str:
    """依路徑與 HTTP 方法回傳 API 危險等級。"""
    path = req.path
    method = req.method.upper()

    if path.startswith("/api/dev/") and method in {"POST", "PUT", "PATCH", "DELETE"}:
        return "critical"

    if any(seg in path for seg in ("/recovery/approve", "/recovery/reject", "/recovery/manual")):
        return "high"
    if any(seg in path for seg in ("/defense/disable", "/defense/enable", "/thresholds")) and method in {"POST", "PUT", "PATCH", "DELETE"}:
        return "high"

    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        return "medium"

    if path.startswith("/api/dev/"):
        return "medium"

    return "low"


def _api_limit_actor_key(req: web.Request) -> str:
    """限速鍵：優先使用登入 user_id，否則退回 IP。"""
    s = _session(req)
    uid = ""
    if isinstance(s, dict):
        uid = str(s.get("id") or "").strip()
    if uid:
        return f"uid:{uid}"
    ip = str(req.get("real_ip") or req.remote or "-")
    return f"ip:{ip}"


class _ApiRateLimiter:
    """記憶體滑動窗口 API 限速器。"""

    def __init__(self) -> None:
        self._hits: dict[str, collections.deque[float]] = {}
        self._blocked_until: dict[str, float] = {}

    def _prune(self, key: str, now: float, window_seconds: int) -> collections.deque[float]:
        q = self._hits.setdefault(key, collections.deque())
        cutoff = now - window_seconds
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def check_and_record(self, *, key: str, profile_name: str) -> tuple[bool, int, int, int]:
        """回傳 (allowed, retry_after, remaining, limit)。"""
        now = time.time()
        profile = _API_RATE_PROFILES.get(profile_name, _API_RATE_PROFILES["low"])
        limit = int(profile["count"])
        window = int(profile["window"])
        block = int(profile["block"])

        blocked_until = self._blocked_until.get(key, 0.0)
        if blocked_until > now:
            retry_after = max(1, int(blocked_until - now))
            return False, retry_after, 0, limit

        q = self._prune(key, now, window)
        if len(q) >= limit:
            until = now + block
            self._blocked_until[key] = until
            retry_after = max(1, int(block))
            logger.warning(
                "API rate limit hit actor=%s profile=%s count=%s window=%ss block=%ss",
                key,
                profile_name,
                len(q),
                window,
                block,
            )
            return False, retry_after, 0, limit

        q.append(now)
        remaining = max(0, limit - len(q))
        return True, 0, remaining, limit


_API_RATE_LIMITER = _ApiRateLimiter()


# ═════════════════════════════════════════════════════════
#  反向代理信任中間件
# ═════════════════════════════════════════════════════════

@web.middleware
async def _proxy_trust_middleware(
    request: web.Request, handler: web.RequestHandler
) -> web.StreamResponse:
    """從反向代理標頭提取真實 IP / 主機 / 協議。
    
    信任以下標頭：
    - X-Forwarded-For: 客戶端 IP（可能多個，使用第一個）
    - X-Real-IP: 備用的客戶端 IP
    - X-Forwarded-Proto: 原始協議 (http / https)
    - Host: 原始主機名
    """
    remote_ip = request.remote
    trusted = False
    try:
        if remote_ip is not None:
            remote_addr = ipaddress.ip_address(remote_ip)
            trusted = any(remote_addr in net for net in _TRUSTED_PROXY_NETWORKS)
    except ValueError:
        trusted = False

    # 僅信任 allowlist 來源送來的 forwarded 標頭。
    if trusted:
        x_forwarded_for = request.headers.get("X-Forwarded-For", "")
        if x_forwarded_for:
            # X-Forwarded-For 可能包含多個 IP（以逗號分隔）
            real_ip = x_forwarded_for.split(",")[0].strip()
        else:
            real_ip = request.headers.get("X-Real-IP", remote_ip)
        forwarded_proto = request.headers.get(
            "X-Forwarded-Proto", "https" if request.secure else "http"
        )
        forwarded_host = request.headers.get("Host", request.host)
    else:
        real_ip = remote_ip
        forwarded_proto = "https" if request.secure else "http"
        forwarded_host = request.host

    # 在 request 物件上儲存真實 IP（以供後續處理器使用）
    request["real_ip"] = real_ip
    request["forwarded_proto"] = forwarded_proto
    request["forwarded_host"] = forwarded_host
    request["trusted_proxy"] = trusted
    
    return await handler(request)

# ═════════════════════════════════════════════════════════
#  Session 工具（HMAC-SHA256 簽章 Cookie）
# ═════════════════════════════════════════════════════════

def _sign(data: dict) -> str:
    """產生帶有過期時間的簽章 Session Token。"""
    data["exp"] = int(time.time()) + _COOKIE_AGE
    raw = base64.urlsafe_b64encode(
        json.dumps(data, ensure_ascii=False).encode()
    ).decode()
    sig = hmac.new(_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def _verify(token: str) -> dict[str, Any] | None:
    """驗證 Session Token 的簽章與有效期限。"""
    try:
        raw, sig = token.rsplit(".", 1)
        expected = hmac.new(
            _SECRET.encode(), raw.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(base64.urlsafe_b64decode(raw))
        if data.get("exp", 0) < time.time():
            return None
        return data
    except Exception:
        return None


def _session(req: web.Request) -> dict[str, Any] | None:
    """從 Cookie 讀取並解析登入 Session。"""
    tok = req.cookies.get(_COOKIE)
    return _verify(tok) if tok else None


# ═════════════════════════════════════════════════════════
#  認證裝飾器
# ═════════════════════════════════════════════════════════

def _auth(fn):
    """API 認證裝飾器：未登入直接回 401。"""
    @functools.wraps(fn)
    async def wrapper(req: web.Request):
        s = _session(req)
        if not s:
            return web.json_response({"error": "Unauthorized"}, status=401)
        req["s"] = s
        return await fn(req)
    return wrapper


def _require_developer(fn):
    """開發者模式認證裝飾器：非開發者回 403。"""
    @functools.wraps(fn)
    async def wrapper(req: web.Request):
        s = _session(req)
        if not s:
            return web.json_response({"error": "Unauthorized"}, status=401)
        if not _is_developer_user_id(s.get("id")):
            return web.json_response({"error": "Forbidden"}, status=403)
        req["s"] = s
        return await fn(req)
    return wrapper


async def _handle_404(req: web.Request) -> web.Response:
    """404 錯誤頁面處理器。"""
    # 檢查是否為 API 請求
    if req.path.startswith("/api/"):
        return web.json_response(
            {
                "error": "Not Found",
                "message": f"API endpoint '{req.path}' does not exist",
                "status": 404,
            },
            status=404,
        )
    
    # 前端路由返回 404.html
    not_found = _WEB_DIR / "404.html"
    if not_found.exists():
        return web.FileResponse(not_found, status=404)
    
    return web.Response(text="頁面未找到", status=404)


def _is_dotfile_probe(path: str) -> bool:
    """判斷是否為 dotfile 探測路徑（例如 /.env）。"""
    last_segment = path.rstrip("/").rsplit("/", 1)[-1]
    return bool(last_segment) and last_segment.startswith(".")


def _is_blocked_source_path(path: str) -> bool:
    """判斷是否為應拒絕的原始碼/內部目錄路徑探測。"""
    blocked_prefixes = (
        "/cogs",
        "/mods",
        "/docs",
        "/logs",
        "/test",
        "/web",
        "/.git",
    )
    return path == blocked_prefixes[0] or path.startswith(
        tuple(f"{p}/" for p in blocked_prefixes)
    ) or path in blocked_prefixes[1:]


@web.middleware
async def _error_middleware(
    request: web.Request, handler: web.RequestHandler
) -> web.StreamResponse:
    """捕獲未匹配的路由並返回 404 頁面。"""
    # 優先阻擋常見 dotfile 探測請求，避免被任何 fallback 誤回 200。
    if _is_dotfile_probe(request.path) or _is_blocked_source_path(request.path):
        return await _handle_404(request)

    try:
        return await handler(request)
    except web.HTTPNotFound:
        return await _handle_404(request)


@web.middleware
async def _api_rate_limit_middleware(
    request: web.Request, handler: web.RequestHandler
) -> web.StreamResponse:
    """對所有 /api/* 套用限速，並依危險等級給不同封鎖時間。"""
    if not request.path.startswith("/api/"):
        return await handler(request)

    profile_name = _api_limit_level(request)
    actor_key = _api_limit_actor_key(request)
    bucket_key = f"{actor_key}|{profile_name}"

    allowed, retry_after, remaining, limit = _API_RATE_LIMITER.check_and_record(
        key=bucket_key,
        profile_name=profile_name,
    )
    if not allowed:
        return web.json_response(
            {
                "error": "Too Many Requests",
                "message": "API 請求過於頻繁，請稍後再試",
                "danger_level": profile_name,
                "retry_after_seconds": retry_after,
            },
            status=429,
            headers={
                "Retry-After": str(retry_after),
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Policy": f"{profile_name};w={_API_RATE_PROFILES[profile_name]['window']}",
            },
        )

    resp = await handler(request)
    try:
        resp.headers["X-RateLimit-Limit"] = str(limit)
        resp.headers["X-RateLimit-Remaining"] = str(remaining)
        resp.headers["X-RateLimit-Policy"] = (
            f"{profile_name};w={_API_RATE_PROFILES[profile_name]['window']}"
        )
    except Exception:
        pass
    return resp




async def _check_approver(req: web.Request, guild_id: str) -> str | None:
    """若當前登入者是 guild_id 的核准者，回傳其 user_id。

    若 bot 已不在該伺服器（已被踢/伺服器已刪），返回 'GONE' 謚號。
    """
    bot = req.app["bot"]
    if not bot.get_guild(int(guild_id)):
        return "GONE"
    uid = req["s"]["id"]
    store = get_storage()
    rows = await store.fetchall(
        "SELECT user_id FROM recovery_approvers "
        "WHERE guild_id = ? AND user_id = ?",
        [guild_id, uid],
    )
    return uid if rows else None


def _gone():
    """Bot 已不在該伺服器時的統一錯誤回應。"""
    return web.json_response({"error": "機器人已不在此伺服器（可能已被踢出或伺服器已刪除）"}, status=404)


def _deny():
    """統一回傳核准者權限不足錯誤。"""
    return web.json_response({"error": "你不是這個伺服器的核准者"}, status=403)


async def _require_approver(
    req: web.Request, guild_id: str
) -> tuple[str, None] | tuple[None, web.Response]:
    """驗證 bot 在該 guild 且請求者為核准者。

    回傳 (uid, None) 代表通過；回傳 (None, error_response) 代表拒絕。
    """
    bot = req.app["bot"]
    if not bot.get_guild(int(guild_id)):
        return None, _gone()

    uid = str(req["s"].get("id") or "")
    if _is_developer_user_id(uid):
        return uid, None

    result = await _check_approver(req, guild_id)
    if result == "GONE":
        return None, _gone()
    if not result:
        return None, _deny()
    return result, None


class _StatusAwareAccessLogger(AbstractAccessLogger):
    """依 HTTP 狀態碼切換 access log level。"""

    def log(self, request: web.BaseRequest, response: web.StreamResponse, request_time: float) -> None:
        status = response.status
        if status >= 500:
            level = logging.ERROR
        elif status >= 400:
            level = logging.WARNING
        else:
            level = logging.INFO

        remote = request.get("real_ip") or request.remote or "-"
        ts = time.strftime("%d/%b/%Y:%H:%M:%S %z")
        method = request.method
        path_qs = request.path_qs
        version = request.version
        body_len = getattr(response, "body_length", None)
        size = body_len if body_len is not None else "-"
        referer = request.headers.get("Referer", "-")
        user_agent = request.headers.get("User-Agent", "-")

        self.logger.log(
            level,
            '%s [%s] "%s %s HTTP/%s.%s" %s %s "%s" "%s"',
            remote,
            ts,
            method,
            path_qs,
            version.major,
            version.minor,
            status,
            size,
            referer,
            user_agent,
        )


# ═════════════════════════════════════════════════════════
#  認證路由
# ═════════════════════════════════════════════════════════

async def _auth_login(req: web.Request) -> web.Response:
    """導向 Discord OAuth2 授權頁。"""
    if not _CLIENT_ID:
        return web.Response(text="DISCORD_CLIENT_ID 尚未設定", status=500)
    state = secrets.token_urlsafe(16)
    from urllib.parse import urlencode
    url = f"{_DISCORD_AUTH}?{urlencode({'client_id': _CLIENT_ID, 'redirect_uri': _REDIRECT_URI, 'response_type': 'code', 'scope': 'identify', 'state': state})}"
    resp = web.HTTPFound(url)
    resp.set_cookie(
        "_st",
        state,
        max_age=300,
        httponly=True,
        secure=_COOKIE_SECURE,
        samesite="Lax",
    )
    return resp


async def _auth_callback(req: web.Request) -> web.Response:
    """處理 OAuth2 回呼，建立站內 Session Cookie。"""
    state = req.query.get("state", "")
    expected = req.cookies.get("_st", "")
    if not state or not hmac.compare_digest(state, expected):
        return web.Response(text="認證錯誤：state 不匹配", status=400)

    code = req.query.get("code")
    if not code:
        return web.Response(text="認證錯誤：缺少 code", status=400)

    async with aiohttp.ClientSession() as sess:
        # 用 code 交換 access token
        async with sess.post(
            _DISCORD_TOKEN,
            data={
                "client_id": _CLIENT_ID,
                "client_secret": _CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as r:
            if r.status != 200:
                logger.warning("OAuth token exchange failed: %d", r.status)
                return web.Response(text="認證失敗：無法取得 token", status=400)
            tok = await r.json()

        access = tok.get("access_token")
        if not access:
            return web.Response(text="認證失敗：token 資料異常", status=400)

        # 取得 Discord 使用者資訊
        async with sess.get(
            _DISCORD_USER,
            headers={"Authorization": f"Bearer {access}"},
        ) as r:
            if r.status != 200:
                return web.Response(text="認證失敗：無法取得使用者資訊", status=400)
            u = await r.json()

    uid = u["id"]
    avatar_hash = u.get("avatar")
    avatar_url = (
        f"https://cdn.discordapp.com/avatars/{uid}/{avatar_hash}.png"
        if avatar_hash
        else None
    )
    token = _sign({
        "id": uid,
        "username": u.get("global_name") or u.get("username", ""),
        "avatar": avatar_url,
    })

    resp = web.HTTPFound("/")
    resp.set_cookie(
        _COOKIE,
        token,
        max_age=_COOKIE_AGE,
        httponly=True,
        secure=_COOKIE_SECURE,
        samesite="Lax",
        path="/",
    )
    resp.del_cookie("_st")
    return resp


async def _auth_logout(req: web.Request) -> web.Response:
    """登出並清除 Session Cookie。"""
    resp = web.HTTPFound("/")
    resp.del_cookie(_COOKIE, path="/")
    return resp


# ═════════════════════════════════════════════════════════
#  API 路由
# ═════════════════════════════════════════════════════════

@_auth
async def _api_me(req: web.Request) -> web.Response:
    """回傳當前登入者與可管理伺服器清單。"""
    s = req["s"]
    bot = req.app["bot"]
    store = get_storage()
    rows = await store.fetchall(
        "SELECT ra.guild_id, g.name "
        "FROM recovery_approvers ra "
        "LEFT JOIN guilds g ON ra.guild_id = g.guild_id "
        "WHERE ra.user_id = ?",
        [s["id"]],
    )
    all_approver_rows = await store.fetchall(
        "SELECT DISTINCT guild_id FROM recovery_approvers"
    )

    bot_guild_name_map = {
        str(g.id): g.name
        for g in bot.guilds
    }

    def _to_items(guild_ids: set[str]) -> list[dict[str, str]]:
        return [
            {"id": gid, "name": bot_guild_name_map[gid]}
            for gid in sorted(guild_ids, key=lambda gid: bot_guild_name_map[gid].lower())
            if gid in bot_guild_name_map
        ]

    my_ids = {
        str(r["guild_id"])
        for r in rows
        if str(r["guild_id"]) in bot_guild_name_map
    }
    approved_any_ids = {
        str(r["guild_id"])
        for r in all_approver_rows
        if str(r["guild_id"]) in bot_guild_name_map
    }
    bot_only_ids = set(bot_guild_name_map.keys()) - approved_any_ids
    other_approved_ids = approved_any_ids - my_ids

    my_guilds = _to_items(my_ids)
    other_approved_guilds = _to_items(other_approved_ids)
    bot_only_guilds = _to_items(bot_only_ids)
    all_guilds = _to_items(set(bot_guild_name_map.keys()))

    is_developer = _is_developer_user_id(s.get("id"))
    guilds = my_guilds if not is_developer else (
        my_guilds + other_approved_guilds + bot_only_guilds
    )

    return web.json_response({
        "id": s["id"],
        "username": s["username"],
        "avatar": s.get("avatar"),
        "is_developer": is_developer,
        "guilds": guilds,
        "guild_groups": {
            "mine": my_guilds,
            "other_approved": other_approved_guilds,
            "bot_only": bot_only_guilds,
            "all": all_guilds,
            "others": other_approved_guilds,
        },
    })


async def _commit_snapshot_to_redis(guild: discord.Guild, actor_id: str) -> dict[str, int | str]:
    """將指定 guild 的快照寫入 Redis（等同 >>commit 核心流程）。"""
    store = get_storage()
    if not store.redis_available:
        raise RuntimeError("Redis unavailable")

    redis = getattr(store, "_redis", None)
    if redis is None:
        raise RuntimeError("Redis client unavailable")

    guild_id = str(guild.id)
    ch_count = ro_count = mb_count = 0

    guild_data = {
        "guild_id": guild_id,
        "name": guild.name,
        "icon_url": str(guild.icon.url) if guild.icon else None,
        "banner_url": str(guild.banner.url) if guild.banner else None,
    }

    # 分塊即時寫入，避免大型 guild 一次累積所有快照佔用過多記憶體。
    pending_pairs: list[tuple[str, str]] = []
    pending_chunk_pairs = 50
    total_snapshots = 0

    async def _flush_pending() -> None:
        nonlocal total_snapshots
        if not pending_pairs:
            return
        await redis.mset(dict(pending_pairs))
        pipe = redis.pipeline(transaction=False)
        for key, _ in pending_pairs:
            pipe.expire(key, 3600)
        await pipe.execute()
        total_snapshots += len(pending_pairs)
        pending_pairs.clear()

    def _queue_snapshot(key: str, payload: dict[str, Any]) -> None:
        pending_pairs.append((key, json.dumps(payload, ensure_ascii=False)))

    redis_prefix = f"sync:recovery:commit:{guild_id}"
    _queue_snapshot(f"{redis_prefix}:guild:{guild_id}", guild_data)

    for channel in guild.channels:
        ch_data = {
            "channel_id": str(channel.id),
            "name": channel.name,
            "type": channel.type.value,
            "position": channel.position,
            "parent_id": str(channel.category_id) if channel.category_id else None,
        }
        overwrites = []
        for target, overwrite in channel.overwrites.items():
            allow, deny = overwrite.pair()
            overwrites.append(
                {
                    "id": str(target.id),
                    "type": "role" if isinstance(target, discord.Role) else "member",
                    "allow": str(allow.value),
                    "deny": str(deny.value),
                }
            )
        ch_data["permission_overwrites"] = overwrites
        if isinstance(channel, discord.TextChannel):
            ch_data["topic"] = channel.topic
            ch_data["nsfw"] = channel.nsfw
            ch_data["slowmode_delay"] = channel.slowmode_delay

        _queue_snapshot(f"{redis_prefix}:channel:{channel.id}", ch_data)
        if len(pending_pairs) >= pending_chunk_pairs:
            await _flush_pending()
        ch_count += 1

    for role in guild.roles:
        role_data = {
            "role_id": str(role.id),
            "name": role.name,
            "permissions": str(role.permissions.value),
            "position": role.position,
            "color": role.color.value,
            "hoist": role.hoist,
            "mentionable": role.mentionable,
            "members": [str(member.id) for member in role.members],
        }
        _queue_snapshot(f"{redis_prefix}:role:{role.id}", role_data)
        if len(pending_pairs) >= pending_chunk_pairs:
            await _flush_pending()
        ro_count += 1

    for member in guild.members:
        member_data = {
            "user_id": str(member.id),
            "nick": member.nick,
        }
        _queue_snapshot(f"{redis_prefix}:member:{member.id}", member_data)
        if len(pending_pairs) >= pending_chunk_pairs:
            await _flush_pending()
        mb_count += 1

    await _flush_pending()

    logger.info(
        "Web commit snapshot staged guild=%s channels=%d roles=%d members=%d user=%s",
        guild_id,
        ch_count,
        ro_count,
        mb_count,
        actor_id,
    )

    return {
        "guild_id": guild_id,
        "prefix": redis_prefix,
        "channels": ch_count,
        "roles": ro_count,
        "members": mb_count,
        "snapshots_total": total_snapshots,
    }


@_auth
async def _api_overview(req: web.Request) -> web.Response:
    """回傳單一伺服器總覽數據（事件、待復原、成員等）。"""
    gid = req.match_info["gid"]
    _, err = await _require_approver(req, gid)
    if err:
        return err

    store = get_storage()
    bot = req.app["bot"]
    guild = bot.get_guild(int(gid))

    guild_row = await store.fetchall(
        "SELECT name FROM guilds WHERE guild_id = ?", [gid]
    )
    events = await store.fetchall(
        "SELECT COUNT(*) AS cnt FROM temp_cache "
        "WHERE guild_id = ? AND timestamp >= (strftime('%s','now') - 86400)",
        [gid],
    )
    pending = await store.fetchall(
        "SELECT COUNT(*) AS cnt FROM recovery_requests "
        "WHERE guild_id = ? AND status = 'pending'",
        [gid],
    )

    # 僅計算文字與語音頻道（排除分類、討論串等）
    channels = 0
    if guild:
        from discord import ChannelType
        channels = sum(
            1 for ch in guild.channels
            if ch.type in (ChannelType.text, ChannelType.voice)
        )
    
    # 僅計算非預設身分組（排除 @everyone）
    roles = 0
    if guild:
        roles = sum(1 for r in guild.roles if not r.is_default())

    return web.json_response({
        "guild_id": gid,
        "name": guild_row[0]["name"] if guild_row else "未知",
        "channels": channels,
        "roles": roles,
        "members": (guild.member_count or 0) if guild else 0,
        "events_24h": events[0]["cnt"] if events else 0,
        "pending_recoveries": pending[0]["cnt"] if pending else 0,
    })


@_auth
async def _api_events(req: web.Request) -> web.Response:
    """回傳近期事件列表（temp_cache）。"""
    gid = req.match_info["gid"]
    _, err = await _require_approver(req, gid)
    if err:
        return err

    store = get_storage()
    rows = await store.fetchall(
        "SELECT id, event_type, target_id, timestamp FROM temp_cache "
        "WHERE guild_id = ? ORDER BY timestamp DESC LIMIT 50",
        [gid],
    )
    return web.json_response({"events": rows})


@_auth
async def _api_recovery_requests(req: web.Request) -> web.Response:
    """回傳近期復原請求列表。"""
    gid = req.match_info["gid"]
    _, err = await _require_approver(req, gid)
    if err:
        return err

    store = get_storage()
    rows = await store.fetchall(
        "SELECT * FROM recovery_requests "
        "WHERE guild_id = ? ORDER BY created_at DESC LIMIT 30",
        [gid],
    )
    return web.json_response({"requests": rows})


@_auth
async def _api_approve(req: web.Request) -> web.Response:
    """核准指定復原請求並執行復原。"""
    gid = req.match_info["gid"]
    rid = req.match_info["rid"]
    uid, err = await _require_approver(req, gid)
    if err:
        return err

    store = get_storage()
    bot: commands.Bot = req.app["bot"]
    guild = bot.get_guild(int(gid))

    rr = await store.fetchall(
        "SELECT status, approved_by FROM recovery_requests "
        "WHERE id = ? AND guild_id = ?",
        [rid, gid],
    )
    if not rr:
        return web.json_response({"error": "找不到這筆復原請求"}, status=404)

    status = rr[0]["status"]
    if status != "pending":
        return web.json_response(
            {"error": f"此請求目前為 {status}，不可重複同意"},
            status=409,
        )

    await store.execute(
        "UPDATE recovery_requests "
        "SET status='processing', approved_by=? "
        "WHERE id=? AND guild_id=? AND status='pending'",
        [uid, rid, gid],
    )

    recheck = await store.fetchall(
        "SELECT status, approved_by FROM recovery_requests "
        "WHERE id = ? AND guild_id = ?",
        [rid, gid],
    )
    if (
        not recheck
        or recheck[0]["status"] != "processing"
        or str(recheck[0].get("approved_by") or "") != str(uid)
    ):
        return web.json_response(
            {"error": "這筆復原請求已由其他核准者處理"},
            status=409,
        )

    cog = bot.get_cog("Recovery")
    if not cog:
        return web.json_response({"error": "復原模組未載入"}, status=500)

    try:
        ch, ro, ms, *_extra = await cog.run_recovery_with_lock(store, guild, rid)
        await store.execute(
            "UPDATE recovery_requests "
            "SET status='approved', approved_by=?, "
            "result_channels=?, result_roles=?, result_messages=?, "
            "resolved_at=strftime('%s','now') "
            "WHERE id=? AND guild_id=? AND status='processing' AND approved_by=?",
            [uid, ch, ro, ms, rid, gid, uid],
        )
        return web.json_response({"message": "復原完成", "channels": ch, "roles": ro, "messages": ms})
    except Exception as exc:
        await store.execute(
            "UPDATE recovery_requests "
            "SET status='failed', approved_by=?, resolved_at=strftime('%s','now') "
            "WHERE id=? AND guild_id=?",
            [uid, rid, gid],
        )
        logger.error("Web approve recovery failed: %s", exc, exc_info=True)
        return web.json_response({"error": "復原執行失敗，請稍後重試或聯繫系統管理員。"}, status=500)


@_auth
async def _api_reject(req: web.Request) -> web.Response:
    """拒絕指定復原請求。"""
    gid = req.match_info["gid"]
    rid = req.match_info["rid"]
    uid, err = await _require_approver(req, gid)
    if err:
        return err

    store = get_storage()
    before = await store.fetchall(
        "SELECT status FROM recovery_requests WHERE id=? AND guild_id=?",
        [rid, gid],
    )
    if not before:
        return web.json_response({"error": "找不到這筆復原請求"}, status=404)

    if before[0]["status"] != "pending":
        return web.json_response(
            {"error": f"此請求目前為 {before[0]['status']}，不可重複拒絕"},
            status=409,
        )

    await store.execute(
        "UPDATE recovery_requests "
        "SET status='rejected', approved_by=?, resolved_at=strftime('%s','now') "
        "WHERE id=? AND guild_id=? AND status='pending'",
        [uid, rid, gid],
    )

    recheck = await store.fetchall(
        "SELECT status, approved_by FROM recovery_requests WHERE id=? AND guild_id=?",
        [rid, gid],
    )
    if (
        not recheck
        or recheck[0]["status"] != "rejected"
        or str(recheck[0].get("approved_by") or "") != str(uid)
    ):
        return web.json_response(
            {"error": "這筆復原請求已由其他核准者處理"},
            status=409,
        )

    bot: commands.Bot = req.app["bot"]
    cog = bot.get_cog("Recovery")
    if cog:
        rejected_embed = discord.Embed(
            title="❌ 已拒絕還原",
            description=(
                f"**{req['s']['username']}** 於 Web 面板拒絕此復原請求，不會執行還原。"
            ),
            color=discord.Color.light_grey(),
        )
        await cog._edit_alert_dms(
            store,
            gid,
            rid,
            rejected_embed,
            view=discord.ui.View(),
        )

    return web.json_response({"message": "已拒絕復原請求"})


@_auth
async def _api_manual(req: web.Request) -> web.Response:
    """手動觸發一次復原流程。"""
    gid = req.match_info["gid"]
    uid, err = await _require_approver(req, gid)
    if err:
        return err

    bot: commands.Bot = req.app["bot"]
    guild = bot.get_guild(int(gid))

    cog = bot.get_cog("Recovery")
    if not cog:
        return web.json_response({"error": "復原模組未載入"}, status=500)

    if bool(getattr(bot, "_closing_with_recovery_wait", False)):
        if hasattr(cog, "enqueue_recovery_for_next_startup"):
            cog.enqueue_recovery_for_next_startup(
                gid,
                None,
                source="shutdown_manual_api",
            )
        return web.json_response(
            {
                "message": "系統正在關機，這筆手動復原已排程到下次開機自動執行",
                "queued": True,
            },
            status=202,
        )

    if hasattr(cog, "is_recovery_in_progress") and cog.is_recovery_in_progress(gid):
        return web.json_response(
            {"error": "此伺服器目前正在復原中，請等目前流程完成後再試"},
            status=409,
        )

    store = get_storage()
    await store.execute(
        "INSERT INTO recovery_requests "
        "(guild_id, event_type, event_count, status, requested_by) "
        "VALUES (?, 'manual', 0, 'manual', ?)",
        [gid, uid],
    )

    try:
        ch, ro, ms, *extra = await cog.run_recovery_with_lock(store, guild)
        failed_roles = extra[0] if extra else []
        # 更新剛建立的 manual 請求結果。
        latest = await store.fetchall(
            "SELECT id FROM recovery_requests "
            "WHERE guild_id=? AND status='manual' ORDER BY created_at DESC LIMIT 1",
            [gid],
        )
        if latest:
            await store.execute(
                "UPDATE recovery_requests "
                "SET status='completed', result_channels=?, result_roles=?, "
                "result_messages=?, resolved_at=strftime('%s','now') WHERE id=?",
                [ch, ro, ms, latest[0]["id"]],
            )
        return web.json_response(
            {
                "message": "手動復原完成",
                "channels": ch,
                "roles": ro,
                "messages": ms,
                "failed_roles": len(failed_roles or []),
            }
        )
    except Exception as exc:
        logger.error("Manual recovery failed: %s", exc, exc_info=True)
        return web.json_response({"error": "復原執行失敗，請稍後重試或聯繫系統管理員。"}, status=500)


async def _ensure_threshold_schema(store) -> None:
    """確保 guild_thresholds 具備 window_seconds 欄位（向下相容舊資料庫）。"""
    global _THRESHOLD_SCHEMA_READY
    if _THRESHOLD_SCHEMA_READY:
        return
    try:
        await store.execute(
            "ALTER TABLE guild_thresholds ADD COLUMN window_seconds INTEGER"
        )
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if (
            "duplicate" not in msg
            and "already exists" not in msg
            and "exists" not in msg
            and "duplicate column" not in msg
        ):
            logger.debug("Threshold schema alter skipped: %s", exc)
    try:
        await store.execute(
            "UPDATE guild_thresholds SET window_seconds = ? WHERE window_seconds IS NULL",
            [_THRESHOLD_WINDOWS.get("channel_delete", 300)],
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Threshold schema backfill skipped: %s", exc)

    _THRESHOLD_SCHEMA_READY = True


@_auth
async def _api_thresholds_get(req: web.Request) -> web.Response:
    """讀取伺服器異常門檻（含預設值）。"""
    gid = req.match_info["gid"]
    _, err = await _require_approver(req, gid)
    if err:
        return err

    store = get_storage()
    await _ensure_threshold_schema(store)
    rows = await store.fetchall(
        "SELECT event_type, threshold_value, window_seconds FROM guild_thresholds WHERE guild_id = ?",
        [gid],
    )
    db_map = {
        str(r["event_type"]): {
            "value": int(r["threshold_value"]),
            "window_seconds": int(r["window_seconds"] or _THRESHOLD_WINDOWS.get(str(r["event_type"]), 300)),
        }
        for r in rows
    }
    result = []
    keys = sorted(set(_THRESHOLD_LABELS.keys()) | set(_DEFAULT_THRESHOLDS.keys()))
    for et in keys:
        default = _DEFAULT_THRESHOLDS.get(et, 5)
        default_window = _THRESHOLD_WINDOWS.get(et, 300)
        current = db_map.get(et, {"value": default, "window_seconds": default_window})
        result.append({
            "event_type": et,
            "label": _THRESHOLD_LABELS.get(et, et),
            "value": int(current["value"]),
            "default": default,
            "window_seconds": int(current["window_seconds"]),
            "default_window_seconds": default_window,
        })
    return web.json_response({"thresholds": result})


@_auth
async def _api_thresholds_set(req: web.Request) -> web.Response:
    """更新伺服器異常門檻設定。"""
    gid = req.match_info["gid"]
    uid, err = await _require_approver(req, gid)
    if err:
        return err

    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "無效的 JSON"}, status=400)

    thresholds = body.get("thresholds", {})
    if not isinstance(thresholds, dict):
        return web.json_response({"error": "thresholds 必須是物件"}, status=400)

    store = get_storage()
    await _ensure_threshold_schema(store)
    valid_events = set(_THRESHOLD_LABELS.keys()) | set(_DEFAULT_THRESHOLDS.keys())
    for et, payload in thresholds.items():
        if et not in valid_events:
            continue

        # 向後相容：舊版前端只送 int。
        if isinstance(payload, int):
            val = payload
            window_seconds = _THRESHOLD_WINDOWS.get(et, 300)
        elif isinstance(payload, dict):
            val = payload.get("value")
            window_seconds = payload.get("window_seconds", _THRESHOLD_WINDOWS.get(et, 300))
        else:
            return web.json_response(
                {"error": f"「{_THRESHOLD_LABELS.get(et, et)}」格式錯誤"},
                status=400,
            )

        if not isinstance(val, int) or val < 1 or val > 100:
            return web.json_response(
                {"error": f"「{_THRESHOLD_LABELS.get(et, et)}」門檻值需在 1–100 之間"},
                status=400,
            )
        if not isinstance(window_seconds, int) or not (
            _THRESHOLD_WINDOW_MIN_SECONDS <= window_seconds <= _THRESHOLD_WINDOW_MAX_SECONDS
        ):
            return web.json_response(
                {
                    "error": (
                        f"「{_THRESHOLD_LABELS.get(et, et)}」時間窗需在 "
                        f"{_THRESHOLD_WINDOW_MIN_SECONDS}–{_THRESHOLD_WINDOW_MAX_SECONDS} 秒之間"
                    )
                },
                status=400,
            )

        await store.execute(
            "INSERT INTO guild_thresholds (guild_id, event_type, threshold_value, window_seconds, updated_by) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, event_type) DO UPDATE SET "
            "threshold_value=excluded.threshold_value, "
            "window_seconds=excluded.window_seconds, "
            "updated_by=excluded.updated_by, "
            "updated_at=strftime('%s','now')",
            [gid, et, val, window_seconds, uid],
        )
    return web.json_response({"message": "門檻已更新"})


@_auth
async def _api_defense_status(req: web.Request) -> web.Response:
    """讀取伺服器防禦系統啟停狀態。"""
    gid = req.match_info["gid"]
    _, err = await _require_approver(req, gid)
    if err:
        return err

    store = get_storage()
    try:
        defense = await get_defense_state(store, gid)
        return web.json_response({"defense": defense})
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to read defense status gid=%s: %s", gid, exc, exc_info=True)
        return web.json_response(
            {"error": "防禦狀態讀取失敗，請稍後再試"},
            status=503,
        )


@_auth
async def _api_defense_disable(req: web.Request) -> web.Response:
    """暫停防禦系統，預設 1 小時後自動恢復。"""
    gid = req.match_info["gid"]
    uid, err = await _require_approver(req, gid)
    if err:
        return err

    duration = DEFAULT_DISABLE_SECONDS
    try:
        body = await req.json()
        if isinstance(body, dict):
            raw_duration = body.get("duration_seconds")
            if isinstance(raw_duration, int):
                duration = raw_duration
    except Exception:
        # Allow empty body and keep default duration.
        pass

    store = get_storage()
    try:
        defense = await set_defense_disabled(store, gid, uid, duration_seconds=duration)
        return web.json_response(
            {
                "message": "防禦系統已暫時關閉",
                "defense": defense,
            }
        )
    except DefenseStorageError:
        logger.warning(
            "Failed to disable defense gid=%s uid=%s",
            gid,
            uid,
            exc_info=True,
        )
        return web.json_response(
            {"error": "防禦操作失敗：儲存服務暫時不可用"},
            status=503,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to disable defense gid=%s uid=%s: %s", gid, uid, exc, exc_info=True)
        return web.json_response(
            {"error": "防禦操作失敗，請稍後重試"},
            status=500,
        )


@_auth
async def _api_defense_enable(req: web.Request) -> web.Response:
    """立即重新啟用防禦系統。"""
    gid = req.match_info["gid"]
    uid, err = await _require_approver(req, gid)
    if err:
        return err

    store = get_storage()
    try:
        defense = await set_defense_enabled(store, gid, uid)
        return web.json_response(
            {
                "message": "防禦系統已重新啟用",
                "defense": defense,
            }
        )
    except DefenseStorageError:
        logger.warning(
            "Failed to enable defense gid=%s uid=%s",
            gid,
            uid,
            exc_info=True,
        )
        return web.json_response(
            {"error": "防禦操作失敗：儲存服務暫時不可用"},
            status=503,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to enable defense gid=%s uid=%s: %s", gid, uid, exc, exc_info=True)
        return web.json_response(
            {"error": "防禦操作失敗，請稍後重試"},
            status=500,
        )


@_auth
async def _api_logs_get(req: web.Request) -> web.Response:
    """讀取維運日誌。"""
    gid = req.match_info["gid"]
    _, err = await _require_approver(req, gid)
    if err:
        return err

    store = get_storage()
    rows = await store.fetchall(
        "SELECT * FROM maintenance_logs "
        "WHERE guild_id = ? OR guild_id IS NULL "
        "ORDER BY created_at DESC LIMIT 20",
        [gid],
    )
    return web.json_response({"logs": rows})


@_auth
async def _api_logs_post(req: web.Request) -> web.Response:
    """新增一筆維運日誌。"""
    gid = req.match_info["gid"]
    uid, err = await _require_approver(req, gid)
    if err:
        return err

    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "無效的 JSON"}, status=400)

    content = (body.get("content") or "").strip()
    if not content or len(content) > 500:
        return web.json_response({"error": "內容不能為空且不超過 500 字"}, status=400)

    store = get_storage()
    await store.execute(
        "INSERT INTO maintenance_logs "
        "(guild_id, content, author_id, author_name) VALUES (?, ?, ?, ?)",
        [gid, content, uid, req["s"]["username"]],
    )
    return web.json_response({"message": "已新增維護日誌"})


# ── 開發者 API ──

@_require_developer
async def _api_dev_ratelimit_stats(req: web.Request) -> web.Response:
    """取得 Rate-limit telemetry（只限開發者）。"""
    try:
        from mods.rate_limit import get_ratelimit_stats
        minutes = int(req.query.get("minutes", "1"))
        minutes = max(1, min(60, minutes))  # Clamp 1-60
        stats = get_ratelimit_stats(minutes)
        return web.json_response({
            "success": True,
            "stats": stats,
        })
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to get ratelimit stats: %s", exc, exc_info=True)
        return web.json_response(
            {"error": "無法取得統計", "detail": str(exc)},
            status=500,
        )


@_require_developer
async def _api_dev_system_stats(req: web.Request) -> web.Response:
    """回傳系統/程序/Redis 資源使用狀況（只限開發者）。"""
    global _net_prev
    try:
        # ── 系統 RAM ─────────────────────────────────────
        vm = psutil.virtual_memory()
        sys_ram_total_mb = vm.total / 1024 / 1024
        sys_ram_used_mb = vm.used / 1024 / 1024
        sys_ram_percent = vm.percent

        # ── 系統 CPU ─────────────────────────────────────
        sys_cpu_percent = psutil.cpu_percent(interval=None)

        # ── 本程序資源 ──────────────────────────────────
        proc_mem = _bot_proc.memory_info()
        proc_ram_mb = proc_mem.rss / 1024 / 1024
        proc_cpu = _bot_proc.cpu_percent(interval=None)

        # ── 網路 I/O delta ──────────────────────────────
        net_now = psutil.net_io_counters()
        now_ts = time.time()
        sent_ps = recv_ps = 0.0
        if _net_prev:
            dt = now_ts - _net_prev["ts"]
            if dt > 0:
                sent_ps = (net_now.bytes_sent - _net_prev["bytes_sent"]) / dt
                recv_ps = (net_now.bytes_recv - _net_prev["bytes_recv"]) / dt
        _net_prev = {
            "bytes_sent": net_now.bytes_sent,
            "bytes_recv": net_now.bytes_recv,
            "ts": now_ts,
        }

        # ── Redis 記憶體 ────────────────────────────────
        redis_info: dict = {}
        try:
            storage = get_storage()
            if storage.redis_available and storage._redis:
                raw = await storage._redis.info("memory")
                used = raw.get("used_memory", 0)
                maxmem = raw.get("maxmemory", 0)
                redis_info = {
                    "available": True,
                    "used_memory_mb": round(used / 1024 / 1024, 2),
                    "maxmemory_mb": round(maxmem / 1024 / 1024, 2) if maxmem else None,
                    "used_memory_percent": round(used / maxmem * 100, 1) if maxmem else None,
                    "used_memory_human": raw.get("used_memory_human"),
                }
            else:
                redis_info = {"available": False}
        except Exception as redis_exc:  # noqa: BLE001
            redis_info = {"available": False, "error": str(redis_exc)}

        return web.json_response({
            "success": True,
            "timestamp": now_ts,
            "system": {
                "cpu_percent": round(sys_cpu_percent, 1),
                "ram_used_mb": round(sys_ram_used_mb, 1),
                "ram_total_mb": round(sys_ram_total_mb, 1),
                "ram_percent": round(sys_ram_percent, 1),
            },
            "process": {
                "cpu_percent": round(proc_cpu, 1),
                "ram_mb": round(proc_ram_mb, 1),
            },
            "network": {
                "bytes_sent_per_s": round(sent_ps, 1),
                "bytes_recv_per_s": round(recv_ps, 1),
                "bytes_sent_total": net_now.bytes_sent,
                "bytes_recv_total": net_now.bytes_recv,
            },
            "redis": redis_info,
        })
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to get system stats: %s", exc, exc_info=True)
        return web.json_response({"error": str(exc)}, status=500)


@_require_developer
async def _api_dev_info(req: web.Request) -> web.Response:
    """回傳開發者模式資訊。"""
    s = req["s"]
    return web.json_response({
        "success": True,
        "user_id": s.get("id"),
        "username": s.get("username"),
        "is_developer": True,
        "developer_ids": list(_DEVELOPER_IDS),
    })


@_require_developer
async def _api_dev_reload(req: web.Request) -> web.Response:
    """從 Web 端觸發 cog reload（開發者限定）。
    
    cogs.web reload 會被推送到 Redis 隊列，由 main.py 執行，
    避免 web 在服務請求時重載自己。
    """
    bot: commands.Bot = req.app["bot"]
    store = get_storage()

    cog = ""
    try:
        if req.can_read_body:
            body = await req.json()
            if isinstance(body, dict):
                cog = str(body.get("cog") or "").strip()
    except Exception:
        cog = ""

    cogs_dir = pathlib.Path(__file__).resolve().parent
    if cog:
        targets = [cog if "." in cog else f"cogs.{cog}"]
    else:
        targets = [
            f"cogs.{f.stem}"
            for f in sorted(cogs_dir.glob("*.py"))
            if not f.name.startswith("_")
        ]

    ok: list[str] = []
    fail: list[str] = []
    queued: list[str] = []
    
    for module in targets:
        # If it's cogs.web and we didn't explicitly request only cogs.web,
        # queue it to main.py instead of reloading it directly
        if module == "cogs.web" and (not cog or cog.lower() in ("web", "cogs.web")):
            if store.redis_available:
                try:
                    redis = getattr(store, "_redis", None)
                    if redis:
                        await redis.rpush("bot:reload_queue", module)
                        queued.append(module)
                        logger.info("Queued reload for %s to main.py", module)
                        continue
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to queue reload for %s: %s", module, exc)
                    fail.append(f"{module}: queue failed")
                    continue
            else:
                fail.append(f"{module}: Redis unavailable (cannot queue)")
                continue
        
        # Other cogs: reload directly
        try:
            if module in bot.extensions:
                await bot.reload_extension(module)
            else:
                await bot.load_extension(module)
            ok.append(module)
        except Exception as exc:  # noqa: BLE001
            fail.append(f"{module}: {exc}")
            logger.error("Web reload failed for '%s': %s", module, exc, exc_info=True)

    return web.json_response(
        {
            "success": len(fail) == 0,
            "requested": cog or "all",
            "ok": ok,
            "failed": fail,
            "queued": queued,
            "message": ("Reload 完成" if ok else "") + 
                      ((" | " if ok else "") + f"{len(queued)} queued to main.py" if queued else ""),
        },
        status=200 if len(fail) == 0 else 207,
    )


@_require_developer
async def _api_dev_commit(req: web.Request) -> web.Response:
    """從 Web 端對指定 guild 觸發 commit（開發者限定）。"""
    gid = str(req.match_info["gid"])
    bot: commands.Bot = req.app["bot"]
    guild = bot.get_guild(int(gid))
    if guild is None:
        return web.json_response(
            {"error": "找不到該伺服器，或機器人已不在此伺服器"},
            status=404,
        )

    try:
        result = await _commit_snapshot_to_redis(guild, str(req["s"].get("id") or ""))
        return web.json_response(
            {
                "success": True,
                "message": "Commit 完成，已寫入 Redis 快照",
                **result,
            }
        )
    except RuntimeError as exc:
        return web.json_response(
            {"error": f"Commit 失敗：{exc}"},
            status=503,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Web commit failed gid=%s: %s", gid, exc, exc_info=True)
        return web.json_response(
            {"error": "Commit 執行失敗，請稍後重試"},
            status=500,
        )


# ── SPA fallback ────────────────────────────────────────

async def _spa(req: web.Request) -> web.Response:
    """單頁應用程式入口回傳（前端路由 fallback）。"""
    # 排除明顯不是前端路由的路徑
    path = req.path
    if path.startswith(("/api/", "/static/", "/auth/")):
        # 這些路由不應該到達這裡，如果到達則返回 404
        return await _handle_404(req)

    if _is_blocked_source_path(path):
        return await _handle_404(req)

    # 對「像檔案」與 dotfile 的路徑返回 404，避免把 /.env 這類請求誤回首頁。
    last_segment = path.rstrip("/").rsplit("/", 1)[-1]
    if _is_dotfile_probe(path) or "." in last_segment:
        return await _handle_404(req)
    
    index = _WEB_DIR / "index.html"
    if not index.exists():
        return web.Response(text="面板檔案遺失", status=500)
    return web.FileResponse(index)


# ═════════════════════════════════════════════════════════
#  Cog
# ═════════════════════════════════════════════════════════

class WebCog(commands.Cog, name="Web"):
    """Web 管理面板 Cog：負責啟停 aiohttp 伺服器。"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None

    async def cog_load(self) -> None:
        """Cog 載入時建立所有路由並啟動 HTTP 服務。"""
        if not _CLIENT_ID or not _CLIENT_SECRET:
            logger.warning(
                "DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET not set — "
                "OAuth2 login will not work"
            )

        app = web.Application(
            middlewares=[
                _error_middleware,
                _proxy_trust_middleware,
                _api_rate_limit_middleware,
            ]
        )
        app["bot"] = self.bot

        r = app.router
        # 認證相關路由。
        r.add_get("/auth/login", _auth_login)
        r.add_get("/auth/callback", _auth_callback)
        r.add_get("/auth/logout", _auth_logout)

        # 後端 API 路由。
        r.add_get("/api/me", _api_me)
        r.add_get("/api/guilds/{gid}/overview", _api_overview)
        r.add_get("/api/guilds/{gid}/events", _api_events)
        r.add_get("/api/guilds/{gid}/recovery-requests", _api_recovery_requests)
        r.add_post("/api/guilds/{gid}/recovery/approve/{rid}", _api_approve)
        r.add_post("/api/guilds/{gid}/recovery/reject/{rid}", _api_reject)
        r.add_post("/api/guilds/{gid}/recovery/manual", _api_manual)
        r.add_get("/api/guilds/{gid}/thresholds", _api_thresholds_get)
        r.add_put("/api/guilds/{gid}/thresholds", _api_thresholds_set)
        r.add_get("/api/guilds/{gid}/defense-status", _api_defense_status)
        r.add_post("/api/guilds/{gid}/defense/disable", _api_defense_disable)
        r.add_post("/api/guilds/{gid}/defense/enable", _api_defense_enable)
        r.add_get("/api/guilds/{gid}/maintenance-logs", _api_logs_get)
        r.add_post("/api/guilds/{gid}/maintenance-logs", _api_logs_post)

        # 開發者 API 路由。
        r.add_get("/api/dev/ratelimit-stats", _api_dev_ratelimit_stats)
        r.add_get("/api/dev/system-stats", _api_dev_system_stats)
        r.add_get("/api/dev/info", _api_dev_info)
        r.add_post("/api/dev/reload", _api_dev_reload)
        r.add_post("/api/dev/guilds/{gid}/commit", _api_dev_commit)

        # 前端靜態資源路由。
        static_dir = _WEB_DIR / "static"
        if static_dir.is_dir():
            r.add_static("/static", static_dir)

        # SPA fallback（必須最後註冊）。
        r.add_get("/", _spa)
        r.add_get("/{tail:.*}", _spa)

        self.runner = web.AppRunner(
            app,
            access_log=logger,
            access_log_class=_StatusAwareAccessLogger,
        )
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, _HOST, _PORT)
        await self.site.start()
        logger.info("Web panel started on %s:%d (%s)", _HOST, _PORT, _BASE_URL)

    async def cog_unload(self) -> None:
        """Cog 卸載時關閉 HTTP 服務並清理資源。"""
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()
        logger.info("Web panel stopped")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WebCog(bot))

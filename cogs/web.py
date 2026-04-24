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
import functools
import hashlib
import hmac
import json
import os
import pathlib
import secrets
import time
from typing import Any

import psutil

import aiohttp
import discord
from aiohttp import web
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

_WEB_DIR = pathlib.Path(__file__).resolve().parent.parent / "web"

# 預設異常門檻（需與 cogs/monitoring.py 同步）
_DEFAULT_THRESHOLDS: dict[str, int] = {
    "channel_delete": 3,
    "channel_update": 5,
    "role_delete": 3,
    "role_update": 5,
    "admin_perm_remove": 2,
    "message_spam": 8,
}
_THRESHOLD_LABELS: dict[str, str] = {
    "channel_delete": "頻道刪除",
    "channel_update": "頻道修改",
    "role_delete": "身分組刪除",
    "role_update": "身分組修改",
    "admin_perm_remove": "管理員權限移除",
    "message_spam": "訊息轟炸",
}
_THRESHOLD_WINDOWS: dict[str, int] = {
    "channel_delete": 300,
    "channel_update": 300,
    "role_delete": 300,
    "role_update": 300,
    "admin_perm_remove": 300,
    "message_spam": 10,
}


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
    async def wrapper(req: web.Request) -> web.Response:
        s = _session(req)
        if not s:
            return web.json_response({"error": "未登入，請重新登入"}, status=401)
        req["s"] = s
        return await fn(req)
    return wrapper


def _require_developer(fn):
    """需要開發者身份的 API 裝飾器。"""
    @functools.wraps(fn)
    async def wrapper(req: web.Request) -> web.Response:
        s = _session(req)
        if not s:
            return web.json_response({"error": "未登入，請重新登入"}, status=401)
        user_id = str(s.get("id", ""))
        logger.debug(f"Dev check: user_id={user_id!r}, _DEVELOPER_IDS={_DEVELOPER_IDS}")
        if user_id not in _DEVELOPER_IDS:
            logger.warning(f"Non-developer access attempt: {user_id}")
            return web.json_response(
                {"error": "你不是開發者，無法訪問此功能"},
                status=403
            )
        req["s"] = s
        return await fn(req)
    return wrapper


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
    result = await _check_approver(req, guild_id)
    if result == "GONE":
        return None, _gone()
    if not result:
        return None, _deny()
    return result, None


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
    resp.set_cookie("_st", state, max_age=300, httponly=True, samesite="Lax")
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
    resp.set_cookie(_COOKIE, token, max_age=_COOKIE_AGE, httponly=True, samesite="Lax", path="/")
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
    guilds = [
        {"id": r["guild_id"], "name": r["name"] or "未知伺服器"}
        for r in rows
        if bot.get_guild(int(r["guild_id"]))
    ]
    return web.json_response({
        "id": s["id"],
        "username": s["username"],
        "avatar": s.get("avatar"),
        "guilds": guilds,
    })


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
        ch, ro, ms = await cog.run_recovery_with_lock(store, guild, rid)
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

    store = get_storage()
    await store.execute(
        "INSERT INTO recovery_requests "
        "(guild_id, event_type, event_count, status, requested_by) "
        "VALUES (?, 'manual', 0, 'manual', ?)",
        [gid, uid],
    )

    try:
        ch, ro, ms = await cog.run_recovery_with_lock(store, guild)
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
        return web.json_response({"message": "手動復原完成", "channels": ch, "roles": ro, "messages": ms})
    except Exception as exc:
        logger.error("Manual recovery failed: %s", exc, exc_info=True)
        return web.json_response({"error": "復原執行失敗，請稍後重試或聯繫系統管理員。"}, status=500)


@_auth
async def _api_thresholds_get(req: web.Request) -> web.Response:
    """讀取伺服器異常門檻（含預設值）。"""
    gid = req.match_info["gid"]
    _, err = await _require_approver(req, gid)
    if err:
        return err

    store = get_storage()
    rows = await store.fetchall(
        "SELECT event_type, threshold_value FROM guild_thresholds WHERE guild_id = ?",
        [gid],
    )
    db_map = {r["event_type"]: r["threshold_value"] for r in rows}
    result = []
    for et, default in _DEFAULT_THRESHOLDS.items():
        result.append({
            "event_type": et,
            "label": _THRESHOLD_LABELS.get(et, et),
            "value": db_map.get(et, default),
            "default": default,
            "window_seconds": _THRESHOLD_WINDOWS.get(et, 300),
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
    for et, val in thresholds.items():
        if et not in _DEFAULT_THRESHOLDS:
            continue
        if not isinstance(val, int) or val < 1 or val > 100:
            return web.json_response(
                {"error": f"「{_THRESHOLD_LABELS.get(et, et)}」門檻值需在 1–100 之間"},
                status=400,
            )
        await store.execute(
            "INSERT INTO guild_thresholds (guild_id, event_type, threshold_value, updated_by) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, event_type) DO UPDATE SET "
            "threshold_value=excluded.threshold_value, "
            "updated_by=excluded.updated_by, "
            "updated_at=strftime('%s','now')",
            [gid, et, val, uid],
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


# ── SPA fallback ────────────────────────────────────────

async def _spa(req: web.Request) -> web.Response:
    """單頁應用程式入口回傳（前端路由 fallback）。"""
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

        app = web.Application()
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

        # 前端靜態資源路由。
        static_dir = _WEB_DIR / "static"
        if static_dir.is_dir():
            r.add_static("/static", static_dir)

        # SPA fallback（必須最後註冊）。
        r.add_get("/", _spa)
        r.add_get("/{tail:.*}", _spa)

        self.runner = web.AppRunner(app, access_log=logger)
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

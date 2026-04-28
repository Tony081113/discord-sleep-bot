"""監控模組（Phase 3）。

職責：
1. 監聽並加密保存訊息（on_message）。
2. 追蹤伺服器結構變更（頻道、身分組）。
3. 偵測異常事件並通知復原核准者。
"""

import asyncio
import datetime
import json
import logging
import os
import time
from collections import defaultdict, deque
from typing import Any, Optional

import discord
from discord.ext import commands

from mods.crypto import encrypt
from mods.defense import get_defense_state
from mods.logger import setup_logger
from mods.rate_limit import get_adaptive_delay, rate_limited_call, dm_sleep
from mods.storage import get_storage

logger = setup_logger(__name__)


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

# ---------------------------------------------------------------------------
# 異常偵測門檻（5 分鐘視窗）
# ---------------------------------------------------------------------------

_ANOMALY_WINDOW = 300  # seconds
_RECOVERY_LOOKBACK = 300  # seconds — must match recovery.py
_MESSAGE_SPAM_WINDOW = 10  # seconds
_MESSAGE_SPAM_COOLDOWN = 60  # seconds
_BAN_OP_DELAY = 0.25  # seconds
_ALERT_COOLDOWN_SECONDS = 120
_ATTACKER_NEUTRALIZE_COOLDOWN_SECONDS = 90
_ATTACKER_HISTORY_LOOKBACK_SECONDS = 24 * 60 * 60
_ATTACKER_BAN_CONCURRENCY = _env_int(
    "MONITORING_ATTACKER_BAN_CONCURRENCY", default=2, min_value=1, max_value=6
)
_SPAM_CLEANUP_LOOKBACK_SECONDS = 60
_SPAM_CLEANUP_MAX_MESSAGES = 200
_SELF_ACTION_LOOKBACK_SECONDS = 30
_ALERT_DM_CONCURRENCY = 2
_MESSAGE_RETENTION_SECONDS = 14 * 24 * 60 * 60
_MESSAGE_MAX_PER_GUILD = 50000
_MESSAGE_CLEANUP_INTERVAL_SECONDS = 60

_THRESHOLD: dict[str, int] = {
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

_AUDIT_ACTION_MAP: dict[str, discord.AuditLogAction] = {
    "channel_create": discord.AuditLogAction.channel_create,
    "channel_delete": discord.AuditLogAction.channel_delete,
    "channel_update": discord.AuditLogAction.channel_update,
    "webhook_create": discord.AuditLogAction.webhook_create,
    "role_create": discord.AuditLogAction.role_create,
    "role_delete": discord.AuditLogAction.role_delete,
    "role_update": discord.AuditLogAction.role_update,
    "admin_perm_remove": discord.AuditLogAction.role_update,
}

_RELATED_AUDIT_ACTIONS: dict[str, tuple[discord.AuditLogAction, ...]] = {
    # 頻道遭破壞時，常見分工是「一人刪除、一人狂建」，需合併掃描。
    "channel_delete": (
        discord.AuditLogAction.channel_delete,
        discord.AuditLogAction.channel_create,
        discord.AuditLogAction.channel_update,
        discord.AuditLogAction.webhook_create,
        discord.AuditLogAction.webhook_update,
        discord.AuditLogAction.webhook_delete,
        discord.AuditLogAction.role_create,
        discord.AuditLogAction.role_delete,
        discord.AuditLogAction.role_update,
    ),
    "channel_create": (
        discord.AuditLogAction.channel_create,
        discord.AuditLogAction.channel_delete,
        discord.AuditLogAction.channel_update,
        discord.AuditLogAction.webhook_create,
        discord.AuditLogAction.webhook_update,
        discord.AuditLogAction.webhook_delete,
        discord.AuditLogAction.role_create,
        discord.AuditLogAction.role_update,
    ),
    "webhook_create": (
        discord.AuditLogAction.webhook_create,
        discord.AuditLogAction.webhook_update,
        discord.AuditLogAction.webhook_delete,
        discord.AuditLogAction.channel_create,
        discord.AuditLogAction.channel_delete,
    ),
    "role_delete": (
        discord.AuditLogAction.role_delete,
        discord.AuditLogAction.role_create,
        discord.AuditLogAction.role_update,
        discord.AuditLogAction.channel_delete,
        discord.AuditLogAction.channel_create,
    ),
    "role_create": (
        discord.AuditLogAction.role_create,
        discord.AuditLogAction.role_delete,
        discord.AuditLogAction.role_update,
        discord.AuditLogAction.channel_create,
        discord.AuditLogAction.channel_delete,
    ),
    "admin_perm_remove": (
        discord.AuditLogAction.role_update,
        discord.AuditLogAction.role_delete,
        discord.AuditLogAction.role_create,
    ),
}

_BROAD_AUDIT_ACTION_KEYWORDS: tuple[str, ...] = (
    "channel_",
    "role_",
    "guild_update",
    "overwrite_",
    "webhook_",
    "integration_",
    "member_update",
    "member_role_update",
)

_EVENT_LABELS: dict[str, str] = {
    "channel_create": "大量頻道被建立",
    "channel_delete": "大量頻道被刪除",
    "webhook_create": "大量 Webhook 被建立",
    "role_create": "大量身分組被建立",
    "role_delete": "大量身分組被刪除",
    "admin_perm_remove": "管理員權限被移除",
    "channel_update": "大量頻道被修改",
    "role_update": "大量身分組被修改",
    "message_spam": "訊息轟炸",
    "attacker_rejoin": "已知攻擊者重新加入",
}


# ---------------------------------------------------------------------------
# 序列化工具（與 onboarding.py 對齊）
# ---------------------------------------------------------------------------

def _channel_to_dict(channel: discord.abc.GuildChannel) -> dict:
    """將頻道物件轉為可序列化快照。"""
    data = {
        "channel_id": str(channel.id),
        "name": channel.name,
        "type": channel.type.value,
        "position": channel.position,
        "parent_id": str(channel.category_id) if channel.category_id else None,
    }
    overwrites = []
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        overwrites.append({
            "id": str(target.id),
            "type": "role" if isinstance(target, discord.Role) else "member",
            "allow": str(allow.value),
            "deny": str(deny.value),
        })
    data["permission_overwrites"] = overwrites
    if isinstance(channel, discord.TextChannel):
        data["topic"] = channel.topic
        data["nsfw"] = channel.nsfw
        data["slowmode_delay"] = channel.slowmode_delay
    return data


def _role_to_dict(role: discord.Role) -> dict:
    """將身分組物件轉為可序列化快照，含成員清單供還原使用。"""
    return {
        "role_id": str(role.id),
        "name": role.name,
        "permissions": str(role.permissions.value),
        "position": role.position,
        "color": role.color.value,
        "hoist": role.hoist,
        "mentionable": role.mentionable,
        "members": [str(m.id) for m in role.members],
    }


def _guild_to_dict(guild: discord.Guild) -> dict:
    """將伺服器基礎視覺資料轉為快照。"""
    return {
        "guild_id": str(guild.id),
        "name": guild.name,
        "icon_url": str(guild.icon.url) if guild.icon else None,
        "banner_url": str(guild.banner.url) if guild.banner else None,
    }


def _member_to_dict(member: discord.Member) -> dict:
    """將成員暱稱快照化，供還原時復原 nick。"""
    return {
        "user_id": str(member.id),
        "nick": member.nick,
    }


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class MonitoringCog(commands.Cog, name="Monitoring"):
    """負責訊息加密留存、結構監控與異常偵測。"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # 訊息轟炸偵測緩衝：以 (guild_id, author_id) 分桶儲存時間戳。
        self._message_windows: dict[tuple[int, int], deque[float]] = defaultdict(deque)
        # 觸發冷卻：避免同一使用者在短時間內重複觸發。
        self._spam_triggered_at: dict[tuple[int, int], float] = {}
        # 告警鎖：避免同 guild 並發重複建立請求與連發通知。
        self._alert_locks: dict[str, asyncio.Lock] = {}
        # 告警冷卻：避免短時間內重複發送同伺服器異常通知。
        self._last_alert_at: dict[str, float] = {}
        # 已發送的告警訊息 ID：(guild_id, user_id) -> message_id，用於編輯而非重發。
        self._last_alert_msg_ids: dict[tuple[str, str], int] = {}
        # 已在本次 session 發送過告警的 guild，加速 pending-alert 判斷。
        self._guilds_with_alert_msgs: set[str] = set()
        # 近期已處置的攻擊者，避免短時間重複執行相同封鎖流程。
        self._neutralized_attackers_at: dict[tuple[str, int], float] = {}
        # 近期已記錄的 webhook 建立事件，避免 on_webhooks_update 重複寫入。
        self._recent_webhook_create_seen_at: dict[tuple[str, str], float] = {}
        # 訊息清理節流，避免每則訊息都執行重型清理 SQL。
        self._last_message_cleanup_at: dict[str, float] = {}
        # 攻擊者掃描節流：避免同一伺服器在短時間內重複呼叫稽核 API。
        self._attacker_scan_at: dict[str, float] = {}

    def _log_deferred(self, level: int, message: str, *args: Any) -> None:
        """將高頻 log 延後到下一個 event-loop tick，避免塞住當前 async 熱路徑。"""
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon(logger.log, level, message, *args)
        except RuntimeError:
            logger.log(level, message, *args)

    async def _gather_bounded(self, coros: list, limit: int) -> list:
        """以有界併發執行任務，提升吞吐同時控制速率。"""
        if not coros:
            return []
        sem = asyncio.Semaphore(max(1, limit))

        async def _runner(coro):
            async with sem:
                return await coro

        return await asyncio.gather(*(_runner(c) for c in coros), return_exceptions=False)

    # ----------------------------------------------------------- on_message

    def _sanitize_untrusted_text(self, value: str, *, max_len: int = 2000) -> str:
        """清理不可信輸入，降低注入/格式破壞風險。"""
        cleaned = value.replace("\x00", "").replace("\r", "")
        if len(cleaned) > max_len:
            cleaned = cleaned[:max_len]
        return cleaned

    def _extract_attachment_names(self, message: discord.Message) -> list[str]:
        """提取附件檔名，不保存檔案本體。"""
        names: list[str] = []
        for att in message.attachments:
            name = self._sanitize_untrusted_text(att.filename or "unknown", max_len=120)
            if not name:
                name = "unknown"
            names.append(name)
        return names

    async def _insert_encrypted_message_compat(
        self,
        store,
        *,
        message_id: str,
        channel_id: str,
        guild_id: str,
        author_id: str,
        author_name: str,
        author_avatar: str | None,
        encrypted_content: str,
        attachment_names_json: str,
        nonce: str,
    ) -> None:
        """相容寫入 encrypted_messages。

        優先使用 attachment_names 欄位；若資料庫尚未 migration，
        會嘗試補上欄位並重試。最終仍失敗時回退到舊版欄位寫入，
        以避免訊息遺失。
        """
        new_sql = (
            """
            INSERT OR IGNORE INTO encrypted_messages
                (message_id, channel_id, guild_id, author_id,
                 author_name, author_avatar, encrypted_content, attachment_names, nonce)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
        )
        old_sql = (
            """
            INSERT OR IGNORE INTO encrypted_messages
                (message_id, channel_id, guild_id, author_id,
                 author_name, author_avatar, encrypted_content, nonce)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """
        )
        new_args = [
            message_id,
            channel_id,
            guild_id,
            author_id,
            author_name,
            author_avatar,
            encrypted_content,
            attachment_names_json,
            nonce,
        ]
        old_args = [
            message_id,
            channel_id,
            guild_id,
            author_id,
            author_name,
            author_avatar,
            encrypted_content,
            nonce,
        ]

        try:
            await store.execute(new_sql, new_args)
            return
        except Exception as exc:  # noqa: BLE001
            exc_text = str(exc).lower()
            missing_attachment_col = "no column named attachment_names" in exc_text
            if not missing_attachment_col:
                raise

            logger.warning(
                "encrypted_messages missing attachment_names; attempting runtime migration"
            )

            # 嘗試線上補 migration，成功後再重試新版 INSERT。
            try:
                await store.execute(
                    "ALTER TABLE encrypted_messages ADD COLUMN attachment_names TEXT"
                )
                await store.execute(new_sql, new_args)
                logger.info("Runtime migration applied: encrypted_messages.attachment_names")
                return
            except Exception:
                # migration 可能已被其他執行緒套用，直接再試一次新版 INSERT。
                try:
                    await store.execute(new_sql, new_args)
                    return
                except Exception:
                    # 最後回退舊 SQL，確保訊息仍可寫入。
                    await store.execute(old_sql, old_args)
                    logger.warning(
                        "Stored message without attachment_names due to legacy schema"
                    )

    async def _cleanup_message_log_if_needed(self, store, guild_id: str) -> None:
        """按節流執行訊息保留策略：14 天 + 每 guild 最多 50000 則。"""
        now = time.time()
        last = self._last_message_cleanup_at.get(guild_id, 0.0)
        if (now - last) < _MESSAGE_CLEANUP_INTERVAL_SECONDS:
            return
        self._last_message_cleanup_at[guild_id] = now

        try:
            await store.execute(
                "DELETE FROM encrypted_messages WHERE guild_id = ? AND timestamp < (strftime('%s','now') - ?)",
                [guild_id, _MESSAGE_RETENTION_SECONDS],
            )

            await store.execute(
                """
                DELETE FROM encrypted_messages
                WHERE message_id IN (
                    SELECT message_id
                    FROM encrypted_messages
                    WHERE guild_id = ?
                    ORDER BY timestamp DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                [guild_id, _MESSAGE_MAX_PER_GUILD],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Message retention cleanup failed guild=%s: %s",
                guild_id,
                exc,
                exc_info=True,
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """加密並保存所有非機器人的伺服器訊息。"""
        is_webhook_message = message.webhook_id is not None
        if message.guild is None:
            return
        if message.author.bot and not is_webhook_message:
            return

        store = get_storage()
        guild_id = str(message.guild.id)
        attachment_names = self._extract_attachment_names(message)
        safe_content = self._sanitize_untrusted_text(message.content or "", max_len=4000)

        if safe_content or attachment_names:
            encrypted_content, nonce = encrypt(safe_content)
            avatar_url = (
                message.author.display_avatar.url
                if message.author.display_avatar
                else None
            )

            try:
                await self._insert_encrypted_message_compat(
                    store,
                    message_id=str(message.id),
                    channel_id=str(message.channel.id),
                    guild_id=guild_id,
                    author_id=str(message.author.id),
                    author_name=self._sanitize_untrusted_text(
                        message.author.display_name,
                        max_len=80,
                    ),
                    author_avatar=avatar_url,
                    encrypted_content=encrypted_content,
                    attachment_names_json=json.dumps(attachment_names, ensure_ascii=False),
                    nonce=nonce,
                )
                await self._cleanup_message_log_if_needed(store, guild_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to store message %s: %s", message.id, exc)

        # 訊息保存後檢查是否命中轟炸門檻。
        # 防禦流程任何例外都不應中斷 on_message 主事件。
        try:
            await self._detect_message_spam(message, store)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Spam detection pipeline failed guild=%s channel=%s message=%s: %s",
                guild_id,
                message.channel.id,
                message.id,
                exc,
                exc_info=True,
            )

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: discord.abc.GuildChannel) -> None:
        """監聽 webhook 變更，將近期 webhook 建立行為記為異常事件。"""
        guild = channel.guild
        store = get_storage()
        guild_id = str(guild.id)
        now = time.time()
        cutoff = now - _ANOMALY_WINDOW

        stale_keys = [
            key for key, ts in self._recent_webhook_create_seen_at.items() if ts < cutoff
        ]
        for key in stale_keys:
            self._recent_webhook_create_seen_at.pop(key, None)

        created = 0
        try:
            async for entry in guild.audit_logs(
                limit=30,
                action=discord.AuditLogAction.webhook_create,
            ):
                if entry.created_at.timestamp() < cutoff:
                    break
                if not entry.target:
                    continue
                target_id = str(getattr(entry.target, "id", ""))
                if not target_id:
                    continue
                target_channel_id = str(getattr(entry.target, "channel_id", ""))
                if target_channel_id and target_channel_id != str(channel.id):
                    continue

                seen_key = (guild_id, target_id)
                if seen_key in self._recent_webhook_create_seen_at:
                    continue
                self._recent_webhook_create_seen_at[seen_key] = now

                payload = {
                    "webhook_id": target_id,
                    "channel_id": str(channel.id),
                    "channel_name": getattr(channel, "name", None),
                    "executor_id": str(entry.user.id) if entry.user else None,
                    "created_at": entry.created_at.isoformat() if entry.created_at else None,
                }
                await self._record_event(
                    store,
                    guild_id,
                    "webhook_create",
                    target_id,
                    None,
                    json.dumps(payload, ensure_ascii=False),
                )
                created += 1
        except discord.Forbidden:
            logger.warning("No audit log access for webhook updates guild=%s", guild_id)
            return
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed handling webhook update guild=%s channel=%s: %s",
                guild_id,
                channel.id,
                exc,
                exc_info=True,
            )
            return

        if created > 0:
            logger.warning(
                "Webhook create burst detected guild=%s channel=%s created=%s",
                guild_id,
                channel.id,
                created,
            )
            await self._check_and_alert(store, guild, "webhook_create")

    # ------------------------------------------------------ channel events

    @commands.Cog.listener()
    async def on_guild_channel_create(
        self, channel: discord.abc.GuildChannel
    ) -> None:
        """記錄新建頻道事件，確保新建類別/頻道也有可還原快照。"""
        if await self._is_bot_initiated_action(
            channel.guild,
            discord.AuditLogAction.channel_create,
            str(channel.id),
        ):
            logger.info(
                "Skip self channel_create event guild=%s channel=%s",
                channel.guild.id,
                channel.id,
            )
            return

        guild_id = str(channel.guild.id)
        store = get_storage()
        new_data = json.dumps(_channel_to_dict(channel), ensure_ascii=False)

        await self._record_event(
            store, guild_id, "channel_create", str(channel.id), None, new_data,
        )

        await store.execute(
            "INSERT INTO structure_snapshots "
            "(guild_id, target_type, target_id, snapshot_data) VALUES (?, ?, ?, ?)",
            [guild_id, "channel", str(channel.id), new_data],
        )

        await store.execute(
            """
            INSERT INTO channels (channel_id, guild_id, name, type, position, parent_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                name = excluded.name, type = excluded.type,
                position = excluded.position, parent_id = excluded.parent_id
            """,
            [
                str(channel.id), guild_id, channel.name,
                channel.type.value, channel.position,
                str(channel.category_id) if channel.category_id else None,
            ],
        )

        await self._check_and_alert(store, channel.guild, "channel_create")

    @commands.Cog.listener()
    async def on_guild_channel_delete(
        self, channel: discord.abc.GuildChannel
    ) -> None:
        """記錄頻道刪除事件，並檢查是否觸發異常警報。"""
        if await self._is_bot_initiated_action(
            channel.guild,
            discord.AuditLogAction.channel_delete,
            str(channel.id),
        ):
            logger.info(
                "Skip self channel_delete event guild=%s channel=%s",
                channel.guild.id,
                channel.id,
            )
            return

        guild_id = str(channel.guild.id)
        store = get_storage()
        ch_data = json.dumps(_channel_to_dict(channel), ensure_ascii=False)
        # 存入 structure_snapshots，確保還原時能重建被刪除的頻道。
        try:
            await store.execute(
                "INSERT INTO structure_snapshots (guild_id, target_type, target_id, snapshot_data) VALUES (?, ?, ?, ?)",
                [guild_id, "channel", str(channel.id), ch_data],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to snapshot deleted channel guild=%s channel=%s: %s",
                guild_id, channel.id, exc,
            )
        await self._record_event(
            store, guild_id, "channel_delete", str(channel.id),
            ch_data, None,
        )
        await self._check_and_alert(store, channel.guild, "channel_delete")

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel,
    ) -> None:
        """記錄頻道修改事件，更新快照與 channels 表。"""
        if await self._is_bot_initiated_action(
            after.guild,
            discord.AuditLogAction.channel_update,
            str(after.id),
        ):
            logger.info(
                "Skip self channel_update event guild=%s channel=%s",
                after.guild.id,
                after.id,
            )
            return

        guild_id = str(after.guild.id)
        store = get_storage()
        old_data = json.dumps(_channel_to_dict(before), ensure_ascii=False)
        new_data = json.dumps(_channel_to_dict(after), ensure_ascii=False)

        await self._record_event(
            store, guild_id, "channel_update", str(after.id), old_data, new_data,
        )

        # New snapshot
        await store.execute(
            "INSERT INTO structure_snapshots "
            "(guild_id, target_type, target_id, snapshot_data) VALUES (?, ?, ?, ?)",
            [guild_id, "channel", str(after.id), new_data],
        )

        # Update channels table
        await store.execute(
            """
            INSERT INTO channels (channel_id, guild_id, name, type, position, parent_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                name = excluded.name, type = excluded.type,
                position = excluded.position, parent_id = excluded.parent_id
            """,
            [
                str(after.id), guild_id, after.name,
                after.type.value, after.position,
                str(after.category_id) if after.category_id else None,
            ],
        )

    # --------------------------------------------------------- role events

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        """記錄新建身分組事件，確保新建角色也有可還原快照。"""
        if await self._is_bot_initiated_action(
            role.guild,
            discord.AuditLogAction.role_create,
            str(role.id),
        ):
            logger.info(
                "Skip self role_create event guild=%s role=%s",
                role.guild.id,
                role.id,
            )
            return

        guild_id = str(role.guild.id)
        store = get_storage()
        new_data = json.dumps(_role_to_dict(role), ensure_ascii=False)

        await self._record_event(
            store, guild_id, "role_create", str(role.id), None, new_data,
        )

        await store.execute(
            "INSERT INTO structure_snapshots "
            "(guild_id, target_type, target_id, snapshot_data) VALUES (?, ?, ?, ?)",
            [guild_id, "role", str(role.id), new_data],
        )

        await store.execute(
            """
            INSERT INTO roles
                (role_id, guild_id, name, permissions, position, color, hoist, mentionable)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(role_id) DO UPDATE SET
                name = excluded.name, permissions = excluded.permissions,
                position = excluded.position, color = excluded.color,
                hoist = excluded.hoist, mentionable = excluded.mentionable
            """,
            [
                str(role.id), guild_id, role.name,
                str(role.permissions.value), role.position,
                role.color.value, int(role.hoist), int(role.mentionable),
            ],
        )

        await self._check_and_alert(store, role.guild, "role_create")

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        """記錄身分組刪除事件，並檢查是否觸發異常警報。"""
        if await self._is_bot_initiated_action(
            role.guild,
            discord.AuditLogAction.role_delete,
            str(role.id),
        ):
            logger.info(
                "Skip self role_delete event guild=%s role=%s",
                role.guild.id,
                role.id,
            )
            return

        guild_id = str(role.guild.id)
        store = get_storage()
        await self._record_event(
            store, guild_id, "role_delete", str(role.id),
            json.dumps(_role_to_dict(role), ensure_ascii=False), None,
        )
        await self._check_and_alert(store, role.guild, "role_delete")

    @commands.Cog.listener()
    async def on_guild_role_update(
        self, before: discord.Role, after: discord.Role
    ) -> None:
        """記錄身分組修改事件，並同步 roles 快照與資料表。"""
        if await self._is_bot_initiated_action(
            after.guild,
            discord.AuditLogAction.role_update,
            str(after.id),
        ):
            logger.info(
                "Skip self role_update event guild=%s role=%s",
                after.guild.id,
                after.id,
            )
            return

        guild_id = str(after.guild.id)
        store = get_storage()
        old_data = json.dumps(_role_to_dict(before), ensure_ascii=False)
        new_data = json.dumps(_role_to_dict(after), ensure_ascii=False)

        await self._record_event(
            store, guild_id, "role_update", str(after.id), old_data, new_data,
        )

        # 管理員權限被移除時，額外記一筆高風險事件。
        if before.permissions.administrator and not after.permissions.administrator:
            await self._record_event(
                store, guild_id, "admin_perm_remove", str(after.id),
                old_data, new_data,
            )
            await self._check_and_alert(store, after.guild, "admin_perm_remove")

        # 更新最新角色快照，供後續復原使用。
        await store.execute(
            "INSERT INTO structure_snapshots "
            "(guild_id, target_type, target_id, snapshot_data) VALUES (?, ?, ?, ?)",
            [guild_id, "role", str(after.id), new_data],
        )

        # 同步 roles 表，保持管理面板資料一致。
        await store.execute(
            """
            INSERT INTO roles
                (role_id, guild_id, name, permissions, position, color, hoist, mentionable)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(role_id) DO UPDATE SET
                name = excluded.name, permissions = excluded.permissions,
                position = excluded.position, color = excluded.color,
                hoist = excluded.hoist, mentionable = excluded.mentionable
            """,
            [
                str(after.id), guild_id, after.name,
                str(after.permissions.value), after.position,
                after.color.value, int(after.hoist), int(after.mentionable),
            ],
        )

    # ------------------------------------------------------- member events

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        """追蹤管理員權限異常與暱稱快照。"""
        guild_id = str(after.guild.id)
        store = get_storage()

        if before.nick != after.nick:
            try:
                # 儲存變更前暱稱，讓復原回到攻擊前狀態。
                await store.execute(
                    "INSERT INTO structure_snapshots "
                    "(guild_id, target_type, target_id, snapshot_data) VALUES (?, ?, ?, ?)",
                    [
                        guild_id,
                        "member",
                        str(after.id),
                        json.dumps(_member_to_dict(before), ensure_ascii=False),
                    ],
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to snapshot member nick guild=%s user=%s: %s",
                    guild_id,
                    after.id,
                    exc,
                    exc_info=True,
                )

        if (
            before.guild_permissions.administrator
            and not after.guild_permissions.administrator
        ):
            await self._record_event(
                store, guild_id, "admin_perm_remove", str(after.id),
                json.dumps({"user_id": str(after.id), "had_admin": True}, ensure_ascii=False),
                json.dumps({"user_id": str(after.id), "had_admin": False}, ensure_ascii=False),
            )
            await self._check_and_alert(store, after.guild, "admin_perm_remove")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """已知攻擊者（含機器人）重加時立即再次封鎖，避免在 pending 視窗內繞過防禦。"""
        guild = member.guild
        guild_id = str(guild.id)
        store = get_storage()

        try:
            rows = await store.fetchall(
                """
                SELECT attacker_ids
                FROM recovery_requests
                WHERE guild_id = ?
                  AND attacker_ids IS NOT NULL
                  AND attacker_ids != ''
                                    AND created_at >= (strftime('%s', 'now') - ?)
                ORDER BY created_at DESC
                LIMIT 30
                """,
                                [guild_id, _ATTACKER_HISTORY_LOOKBACK_SECONDS],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to query attacker history guild=%s user=%s: %s",
                guild_id,
                member.id,
                exc,
            )
            return

        known_attackers: set[int] = set()
        for row in rows:
            raw = row.get("attacker_ids")
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except Exception:
                continue
            if not isinstance(parsed, list):
                continue
            for item in parsed:
                if str(item).isdigit():
                    known_attackers.add(int(item))

        known_by_request_history = member.id in known_attackers
        known_by_recent_ban = False
        if not known_by_request_history:
            known_by_recent_ban = await self._was_recently_banned_attacker(
                guild,
                member.id,
                lookback_seconds=_ATTACKER_HISTORY_LOOKBACK_SECONDS,
            )

        if not (known_by_request_history or known_by_recent_ban):
            return

        logger.warning(
            "Known attacker rejoined guild=%s user=%s bot=%s source=request:%s recent_ban:%s; rebanning",
            guild_id,
            member.id,
            member.bot,
            known_by_request_history,
            known_by_recent_ban,
        )

        try:
            await self._record_event(
                store,
                guild_id,
                "attacker_rejoin",
                str(member.id),
                None,
                json.dumps(
                    {
                        "user_id": str(member.id),
                        "bot": bool(member.bot),
                        "known_by_request_history": known_by_request_history,
                        "known_by_recent_ban": known_by_recent_ban,
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to record attacker_rejoin event guild=%s user=%s: %s",
                guild_id,
                member.id,
                exc,
            )

        await self._append_attacker_to_pending_request(store, guild_id, member.id)
        await self._ban_user_ids(
            guild=guild,
            user_ids={member.id},
            source_message_id=f"rejoin:{member.id}",
            source_kind="member_join_known_attacker",
        )
        await self._check_and_alert(store, guild, "attacker_rejoin")

    @commands.Cog.listener()
    async def on_guild_update(
        self, before: discord.Guild, after: discord.Guild
    ) -> None:
        """保存伺服器名稱/縮圖/橫幅快照供後續復原。"""
        if await self._is_bot_initiated_action(
            after,
            discord.AuditLogAction.guild_update,
            str(after.id),
        ):
            logger.info("Skip self guild_update event guild=%s", after.id)
            return

        if (
            before.name == after.name
            and before.icon == after.icon
            and before.banner == after.banner
        ):
            return

        guild_id = str(after.id)
        store = get_storage()
        old_data = json.dumps(_guild_to_dict(before), ensure_ascii=False)
        new_data = json.dumps(_guild_to_dict(after), ensure_ascii=False)

        await self._record_event(
            store,
            guild_id,
            "guild_update",
            guild_id,
            old_data,
            new_data,
        )

        await store.execute(
            "INSERT INTO structure_snapshots "
            "(guild_id, target_type, target_id, snapshot_data) VALUES (?, ?, ?, ?)",
            [guild_id, "guild", guild_id, old_data],
        )

        await store.execute(
            "INSERT INTO guilds (guild_id, name, owner_id) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET name = excluded.name",
            [guild_id, after.name, str(after.owner_id) if after.owner_id else "0"],
        )

    # ------------------------------------------------------------ helpers

    async def _detect_message_spam(
        self, message: discord.Message, store
    ) -> None:
        """在短時間視窗內計算單一使用者訊息數，判斷是否轟炸。"""
        guild = message.guild
        if guild is None:
            return

        guild_id = str(guild.id)
        defense_state = await get_defense_state(store, guild_id)
        if not defense_state["enabled"]:
            logger.debug(
                "Defense paused, skip spam detection guild=%s remaining=%s",
                guild_id,
                defense_state["remaining_seconds"],
            )
            return

        threshold, spam_window_seconds = await self._get_threshold_profile(
            store,
            guild_id,
            "message_spam",
        )
        now = time.time()
        key = (guild.id, message.author.id)
        window = self._message_windows[key]
        window.append(now)

        while window and (now - window[0]) > spam_window_seconds:
            window.popleft()

        if len(window) < threshold:
            return

        last_trigger = self._spam_triggered_at.get(key, 0.0)
        if (now - last_trigger) < _MESSAGE_SPAM_COOLDOWN:
            return
        self._spam_triggered_at[key] = now

        await self._handle_message_spam_detected(
            message=message,
            store=store,
            burst_count=len(window),
            window_seconds=spam_window_seconds,
        )

    def _extract_authorizing_integration_owners(
        self, message: discord.Message
    ) -> dict[str, str] | None:
        """從訊息中萃取 authorizing_integration_owners（若有）。"""
        owners: Any = None

        interaction_metadata = getattr(message, "interaction_metadata", None)
        if interaction_metadata is not None:
            owners = getattr(interaction_metadata, "authorizing_integration_owners", None)

        if not owners:
            interaction = getattr(message, "interaction", None)
            if interaction is not None:
                owners = getattr(interaction, "authorizing_integration_owners", None)

        if not owners:
            return None

        try:
            return {
                str(k): str(v)
                for k, v in dict(owners).items()
                if v is not None and str(v).strip()
            }
        except Exception:
            return None

    def _build_spam_message_payload(
        self,
        message: discord.Message,
        burst_count: int,
        owners: dict[str, str] | None,
        window_seconds: int,
    ) -> dict[str, Any]:
        """建立轟炸事件 JSON，用於稽核與後續處置。"""
        return {
            "event": "message_spam",
            "burst_count": burst_count,
            "window_seconds": window_seconds,
            "message": {
                "id": str(message.id),
                "guild_id": str(message.guild.id) if message.guild else None,
                "channel_id": str(message.channel.id),
                "author_id": str(message.author.id),
                "author_name": message.author.display_name,
                "content": message.content,
                "created_at": message.created_at.isoformat() if message.created_at else None,
                "attachments": [a.url for a in message.attachments],
                "embed_count": len(message.embeds),
            },
            "authorizing_integration_owners": owners or {},
        }

    async def _handle_message_spam_detected(
        self,
        message: discord.Message,
        store,
        burst_count: int,
        window_seconds: int,
    ) -> None:
        """命中轟炸時：保存 JSON，並依 authorizing_integration_owners 封鎖。"""
        guild = message.guild
        if guild is None:
            return

        owners = self._extract_authorizing_integration_owners(message)
        payload = self._build_spam_message_payload(
            message,
            burst_count,
            owners,
            window_seconds,
        )

        await self._record_event(
            store=store,
            guild_id=str(guild.id),
            event_type="message_spam",
            target_id=str(message.author.id),
            old_data=json.dumps(payload, ensure_ascii=False),
            new_data=None,
        )

        logger.warning(
            "Message spam detected guild=%s author=%s channel=%s count=%s owners=%s",
            guild.id,
            message.author.id,
            message.channel.id,
            burst_count,
            owners,
        )

        # 立即在背景啟動釘快照 + 復原警報任務，不等待封鎖流程結束。
        asyncio.create_task(
            self._pin_and_alert_spam(store, guild),
            name=f"spam_alert_{guild.id}",
        )

        # 若 JSON 含 authorizing_integration_owners，視為 User Install 路徑：
        # 封 owner 與刪訊息並行。
        if owners:
            await asyncio.gather(
                self._ban_authorizing_owner_ids(guild, owners, str(message.id)),
                self._delete_recent_spam_messages(message, source_kind="user_install"),
            )
            await self._delete_detected_spam_message(message, source_kind="user_install")
        elif message.webhook_id is not None:
            webhook_actor_ids = await self._find_webhook_operator_ids_from_audit(
                guild,
                str(message.webhook_id),
            )
            # 無論是否找到操作者，立即在背景刪除 webhook 本身以阻斷來源。
            asyncio.create_task(
                self._delete_spam_webhook(guild, str(message.webhook_id)),
                name=f"del_webhook_{message.webhook_id}",
            )
            ban_coros: list = [
                self._delete_recent_spam_messages(message, source_kind="webhook_message"),
            ]
            if webhook_actor_ids:
                ban_coros.append(self._ban_user_ids(
                    guild=guild,
                    user_ids=webhook_actor_ids,
                    source_message_id=str(message.id),
                    source_kind="webhook_message_audit",
                ))
            else:
                logger.warning(
                    "Webhook spam found but no executor from audit guild=%s webhook_id=%s message=%s",
                    guild.id,
                    message.webhook_id,
                    message.id,
                )
            await asyncio.gather(*ban_coros)
            await self._delete_detected_spam_message(message, source_kind="webhook_message")
        else:
            # 無 owner 資訊時，封鎖發訊者與刪訊息並行。
            await asyncio.gather(
                self._ban_user_ids(
                    guild=guild,
                    user_ids={message.author.id},
                    source_message_id=str(message.id),
                    source_kind="message_author_fallback",
                ),
                self._delete_recent_spam_messages(message, source_kind="message_author"),
            )
            await self._delete_detected_spam_message(message, source_kind="message_author")

    async def _pin_and_alert_spam(self, store, guild: discord.Guild) -> None:
        """背景任務：釘住攻擊前快照並發送復原警報。"""
        try:
            await self._pin_pre_attack_snapshots(store, str(guild.id))
            await self._check_and_alert(store, guild, "message_spam")
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "pin_and_alert_spam failed guild=%s: %s", guild.id, exc, exc_info=True
            )

    async def _delete_spam_webhook(self, guild: discord.Guild, webhook_id: str) -> None:
        """背景任務：刪除垃圾 webhook，阻斷訊息來源。"""
        try:
            webhook = await rate_limited_call(
                self.bot.fetch_webhook,
                int(webhook_id),
                limit_key="webhook_fetch",
            )
            await rate_limited_call(
                webhook.delete,
                reason="Spam webhook — deleted by defense system",
                limit_key="webhook_ops",
            )
            logger.warning(
                "Deleted spam webhook guild=%s webhook_id=%s", guild.id, webhook_id
            )
        except discord.NotFound:
            logger.info(
                "Spam webhook already gone guild=%s webhook_id=%s", guild.id, webhook_id
            )
        except discord.Forbidden:
            logger.error(
                "Cannot delete webhook guild=%s webhook_id=%s (no permission)",
                guild.id, webhook_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to delete webhook guild=%s webhook_id=%s: %s",
                guild.id, webhook_id, exc, exc_info=True,
            )

    async def _find_webhook_operator_ids_from_audit(
        self,
        guild: discord.Guild,
        webhook_id: str,
    ) -> set[int]:
        """從 webhook 相關稽核事件找出操作者 user ID。"""
        attacker_ids: set[int] = set()
        cutoff = time.time() - _ANOMALY_WINDOW
        for action in (
            discord.AuditLogAction.webhook_create,
            discord.AuditLogAction.webhook_update,
            discord.AuditLogAction.webhook_delete,
        ):
            try:
                async for entry in guild.audit_logs(limit=100, action=action):
                    if entry.created_at.timestamp() < cutoff:
                        break
                    target = getattr(entry, "target", None)
                    target_id = str(getattr(target, "id", ""))
                    if target_id and target_id != webhook_id:
                        continue
                    if entry.user and entry.user.id != (self.bot.user.id if self.bot.user else None):
                        attacker_ids.add(entry.user.id)
            except discord.Forbidden:
                logger.warning(
                    "No audit log access for webhook operators guild=%s", guild.id
                )
                break
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Failed to resolve webhook operators guild=%s webhook=%s action=%s: %s",
                    guild.id,
                    webhook_id,
                    action,
                    exc,
                )
        logger.debug(
            "Webhook operator scan guild=%s webhook=%s attackers=%s",
            guild.id,
            webhook_id,
            sorted(attacker_ids),
        )
        return attacker_ids

    async def _delete_detected_spam_message(
        self,
        message: discord.Message,
        source_kind: str,
    ) -> None:
        """刪除命中轟炸偵測的訊息。"""
        try:
            try:
                await rate_limited_call(
                    message.delete,
                    reason=f"Message spam detected ({source_kind})",
                )
            except TypeError:
                # 某些訊息物件（如 PartialMessage）delete 不接受 reason 參數。
                await rate_limited_call(message.delete)
            logger.warning(
                "Deleted spam message guild=%s channel=%s message=%s source=%s",
                message.guild.id if message.guild else "unknown",
                message.channel.id,
                message.id,
                source_kind,
            )
        except discord.NotFound:
            logger.info(
                "Spam message already deleted channel=%s message=%s source=%s",
                message.channel.id,
                message.id,
                source_kind,
            )
        except discord.Forbidden:
            logger.error(
                "Delete spam message forbidden guild=%s channel=%s message=%s source=%s",
                message.guild.id if message.guild else "unknown",
                message.channel.id,
                message.id,
                source_kind,
            )
        except discord.HTTPException as exc:
            logger.error(
                "Delete spam message failed channel=%s message=%s source=%s: %s",
                message.channel.id,
                message.id,
                source_kind,
                exc,
                exc_info=True,
            )

    async def _delete_recent_spam_messages(
        self,
        message: discord.Message,
        source_kind: str,
    ) -> None:
        """刪除同一波 recent spam 訊息（channel.purge 批次刪除）。"""
        channel = message.channel
        if not isinstance(channel, discord.TextChannel):
            return

        cutoff = discord.utils.utcnow() - datetime.timedelta(
            seconds=_SPAM_CLEANUP_LOOKBACK_SECONDS
        )
        author_id = message.author.id
        webhook_id = message.webhook_id

        def _is_spam(m: discord.Message) -> bool:
            if m.id == message.id:
                return False
            if webhook_id is not None and m.webhook_id == webhook_id:
                return True
            return m.author.id == author_id

        try:
            deleted = await channel.purge(
                limit=_SPAM_CLEANUP_MAX_MESSAGES,
                check=_is_spam,
                after=cutoff,
                reason=f"Spam cleanup ({source_kind})",
                bulk=True,
            )
            if deleted:
                logger.warning(
                    "Spam cleanup deleted=%s channel=%s guild=%s source=%s",
                    len(deleted),
                    channel.id,
                    message.guild.id if message.guild else "unknown",
                    source_kind,
                )
        except discord.Forbidden:
            logger.error(
                "Spam cleanup forbidden channel=%s source=%s",
                channel.id,
                source_kind,
            )
        except discord.HTTPException as exc:
            logger.warning(
                "Spam cleanup failed channel=%s source=%s: %s",
                channel.id,
                source_kind,
                exc,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Spam cleanup crashed channel=%s source=%s: %s",
                channel.id, source_kind, exc, exc_info=True,
            )

    async def _ban_authorizing_owner_ids(
        self,
        guild: discord.Guild,
        owners: dict[str, str],
        source_message_id: str,
    ) -> None:
        """封鎖 authorizing_integration_owners 中可解析的使用者 ID。"""
        owner_ids: set[int] = set()
        for _, raw_id in owners.items():
            if str(raw_id).isdigit():
                owner_ids.add(int(raw_id))

        if not owner_ids:
            logger.warning(
                "No numeric owner IDs found for spam-ban guild=%s owners=%s",
                guild.id,
                owners,
            )
            return

        await self._ban_user_ids(
            guild=guild,
            user_ids=owner_ids,
            source_message_id=source_message_id,
            source_kind="authorizing_integration_owners",
        )

    async def _ban_user_ids(
        self,
        guild: discord.Guild,
        user_ids: set[int],
        source_message_id: str,
        source_kind: str,
    ) -> None:
        """拔除指定使用者的所有身分組、靜音，再封鎖。"""

        async def _neutralize_one(user_id: int) -> None:
            try:
                if self.bot.user and user_id == self.bot.user.id:
                    return
                if user_id == guild.owner_id:
                    logger.warning(
                        "Skipping neutralize for guild owner guild=%s user=%s",
                        guild.id, user_id,
                    )
                    return

                # 僅處理可解析為 Discord 使用者的 ID。
                await rate_limited_call(
                    self.bot.fetch_user,
                    user_id,
                    limit_key="user_fetch",
                )

                member = guild.get_member(user_id)
                if member:
                    # 1. 拔除所有非系統身分組
                    safe_roles = [r for r in member.roles if r.managed or r.is_default()]
                    try:
                        await rate_limited_call(
                            member.edit,
                            roles=safe_roles,
                            reason=f"Attack detected — stripping roles ({source_kind})",
                            limit_key="member_ops",
                        )
                        logger.info(
                            "Stripped roles guild=%s user=%s source=%s",
                            guild.id, user_id, source_kind,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "Could not strip roles guild=%s user=%s: %s",
                            guild.id, user_id, exc,
                        )
                    # 2. 靜音（timeout 最長 28 天）
                    try:
                        # Discord 對 timeout 時間戳很嚴格：採保守值並去除微秒。
                        until = (
                            datetime.datetime.now(datetime.timezone.utc)
                            + datetime.timedelta(days=27, hours=23, minutes=50)
                        ).replace(microsecond=0)
                        await rate_limited_call(
                            member.edit,
                            timed_out_until=until,
                            reason=f"Attack detected — muted pending ban ({source_kind})",
                            limit_key="member_ops",
                        )
                        logger.info(
                            "Timed out guild=%s user=%s source=%s",
                            guild.id, user_id, source_kind,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "Could not timeout guild=%s user=%s: %s",
                            guild.id, user_id, exc,
                        )

                # 3. 封鎖
                target = member or discord.Object(id=user_id)
                await rate_limited_call(
                    guild.ban,
                    target,
                    reason=(
                        f"Attack detected; banned by {source_kind} "
                        f"(source_message_id={source_message_id})"
                    ),
                    limit_key="guild_ban",
                )
                logger.warning(
                    "Banned user from %s guild=%s user=%s source_message=%s",
                    source_kind,
                    guild.id,
                    user_id,
                    source_message_id,
                )
                await asyncio.sleep(get_adaptive_delay("guild_ban", _BAN_OP_DELAY))
            except discord.NotFound:
                logger.warning(
                    "Skip non-user id for spam-ban guild=%s raw_id=%s source_message=%s",
                    guild.id,
                    user_id,
                    source_message_id,
                )
            except discord.Forbidden:
                logger.error(
                    "Ban forbidden guild=%s user=%s source_message=%s",
                    guild.id,
                    user_id,
                    source_message_id,
                )
                asyncio.create_task(
                    self._request_manual_ban(guild, user_id, source_kind),
                    name=f"manual_ban_req_{guild.id}_{user_id}",
                )
            except discord.HTTPException as exc:
                logger.error(
                    "Ban failed guild=%s user=%s source_message=%s: %s",
                    guild.id,
                    user_id,
                    source_message_id,
                    exc,
                    exc_info=True,
                )

        await self._gather_bounded(
            [_neutralize_one(user_id) for user_id in sorted(user_ids)],
            _ATTACKER_BAN_CONCURRENCY,
        )

    async def _request_manual_ban(
        self,
        guild: discord.Guild,
        user_id: int,
        source_kind: str,
    ) -> None:
        """Bot 無法直接封鎖時，委派給位階足夠的審核者或擁有者手動封鎖。"""
        # 擁有者已在 _neutralize_one 開頭跳過，不會到這裡
        if user_id == guild.owner_id:
            return

        member = guild.get_member(user_id)
        target_top_pos = member.top_role.position if member else 0
        target_display = member.display_name if member else str(user_id)

        store = get_storage()
        try:
            rows = await store.fetchall(
                "SELECT user_id FROM recovery_approvers WHERE guild_id = ?",
                [str(guild.id)],
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to fetch approvers for manual ban guild=%s target=%s: %s",
                guild.id, user_id, exc,
            )
            rows = []

        # 找出位階比目標高的審核者
        qualified: list[discord.Member] = [
            m
            for row in rows
            if (m := guild.get_member(int(row["user_id"])))
            and m.top_role.position > target_top_pos
        ]

        embed = discord.Embed(
            title="⚠️ 需要手動封鎖攻擊者",
            description=(
                f"Bot 無法封鎖 **{target_display}** (`{user_id}`)，因為對方位階高於 Bot。\n\n"
                f"偵測來源：`{source_kind}`\n\n"
                f"請手動封鎖此使用者：<@{user_id}>"
            ),
            color=discord.Color.orange(),
        )

        recipients: list[Any] = []
        if qualified:
            recipients = qualified
            logger.warning(
                "Requesting manual ban from %d approver(s) guild=%s target=%s",
                len(qualified), guild.id, user_id,
            )
        else:
            # 無審核者位階足夠：改通知擁有者
            owner: Any = guild.owner
            if owner is None and guild.owner_id:
                try:
                    owner = await rate_limited_call(
                        self.bot.fetch_user,
                        guild.owner_id,
                        limit_key="user_fetch",
                    )
                except Exception:  # noqa: BLE001
                    pass
            if owner:
                embed.description += "\n\n\uff08無審核者位階足夠，已通知伺服器擁有者）"
                recipients = [owner]
                logger.warning(
                    "No qualified approver for manual ban, notifying owner guild=%s target=%s",
                    guild.id, user_id,
                )

        for recipient in recipients:
            try:
                await rate_limited_call(
                    recipient.send,
                    embed=embed,
                    limit_key="dm_send",
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning(
                    "Cannot DM manual ban request guild=%s recipient=%s target=%s: %s",
                    guild.id, recipient.id, user_id, exc,
                )

    async def _find_attackers_from_audit(
        self, guild: discord.Guild, event_type: str
    ) -> set[int]:
        """從近 _ANOMALY_WINDOW 秒內的相關稽核動作聚合攻擊者 ID。"""
        primary_action = _AUDIT_ACTION_MAP.get(event_type)
        actions = _RELATED_AUDIT_ACTIONS.get(event_type)
        if not actions and primary_action:
            actions = (primary_action,)
        if not actions:
            return set()

        attacker_ids: set[int] = set()
        scanned_entries_by_action: dict[str, int] = {}
        scanned_users_by_action: dict[str, list[int]] = {}
        cutoff = time.time() - _ANOMALY_WINDOW
        bot_user_id = self.bot.user.id if self.bot.user else None

        for action in actions:
            scanned_users: set[int] = set()
            scanned_entries = 0
            try:
                async for entry in guild.audit_logs(limit=100, action=action):
                    scanned_entries += 1
                    if entry.created_at.timestamp() < cutoff:
                        break
                    if entry.user:
                        scanned_users.add(entry.user.id)
                        if entry.user.id != bot_user_id:
                            attacker_ids.add(entry.user.id)
            except discord.Forbidden:
                logger.warning(
                    "No audit log access guild=%s event=%s action=%s",
                    guild.id,
                    event_type,
                    action,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Audit log fetch failed guild=%s event=%s action=%s: %s",
                    guild.id,
                    event_type,
                    action,
                    exc,
                )
            scanned_entries_by_action[str(action)] = scanned_entries
            scanned_users_by_action[str(action)] = sorted(scanned_users)

        if len(attacker_ids) <= 1:
            fallback_users: set[int] = set()
            fallback_entries = 0
            try:
                async for entry in guild.audit_logs(limit=200):
                    fallback_entries += 1
                    if entry.created_at.timestamp() < cutoff:
                        break
                    if not entry.user:
                        continue
                    if entry.user.id == bot_user_id:
                        continue
                    if self._is_suspicious_audit_action(entry.action):
                        fallback_users.add(entry.user.id)
            except discord.Forbidden:
                logger.warning(
                    "No broad audit log access guild=%s event=%s",
                    guild.id,
                    event_type,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Broad audit scan failed guild=%s event=%s: %s",
                    guild.id,
                    event_type,
                    exc,
                )

            if fallback_users:
                attacker_ids.update(fallback_users)
            logger.debug(
                "Broad audit fallback guild=%s event=%s entries=%s users=%s merged_attackers=%s",
                guild.id,
                event_type,
                fallback_entries,
                sorted(fallback_users),
                sorted(attacker_ids),
            )

        logger.debug(
            "Audit scan guild=%s event=%s actions=%s entries=%s users=%s attackers=%s",
            guild.id,
            event_type,
            [str(a) for a in actions],
            scanned_entries_by_action,
            scanned_users_by_action,
            sorted(attacker_ids),
        )
        return attacker_ids

    async def _is_bot_initiated_action(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        target_id: str,
    ) -> bool:
        """檢查事件是否由本 bot 在近期稽核紀錄中觸發。"""
        if not self.bot.user:
            return False

        cutoff = time.time() - _SELF_ACTION_LOOKBACK_SECONDS
        try:
            async for entry in guild.audit_logs(limit=20, action=action):
                if entry.created_at.timestamp() < cutoff:
                    break

                entry_target_id = str(getattr(entry.target, "id", ""))
                if entry_target_id and entry_target_id != target_id:
                    continue

                if entry.user and entry.user.id == self.bot.user.id:
                    return True
        except Exception:
            return False

    def _is_suspicious_audit_action(self, action: Any) -> bool:
        """判斷 audit action 是否屬於高風險管理行為。"""
        action_text = str(action)
        return any(keyword in action_text for keyword in _BROAD_AUDIT_ACTION_KEYWORDS)

    def _should_skip_recently_neutralized(
        self,
        guild_id: str,
        user_id: int,
        now: float,
    ) -> bool:
        """短時間內重複命中同一攻擊者時，避免重複處置。"""
        key = (guild_id, user_id)
        last = self._neutralized_attackers_at.get(key, 0.0)
        if (now - last) < _ATTACKER_NEUTRALIZE_COOLDOWN_SECONDS:
            return True
        self._neutralized_attackers_at[key] = now
        return False

    async def _was_recently_banned_attacker(
        self,
        guild: discord.Guild,
        user_id: int,
        *,
        lookback_seconds: int,
    ) -> bool:
        """Fallback heuristic: if a user was recently banned, treat it as known attacker history."""
        cutoff = time.time() - max(60, lookback_seconds)
        try:
            async for entry in guild.audit_logs(limit=100, action=discord.AuditLogAction.ban):
                if entry.created_at.timestamp() < cutoff:
                    break
                target_id = getattr(getattr(entry, "target", None), "id", None)
                if target_id == user_id:
                    return True
        except discord.Forbidden:
            return False
        except Exception:
            return False
        return False

    async def _append_attacker_to_pending_request(
        self,
        store,
        guild_id: str,
        user_id: int,
    ) -> None:
        """Keep pending request attacker_ids updated when an attacker rejoins."""
        try:
            row = await store.fetchone(
                "SELECT id, attacker_ids FROM recovery_requests "
                "WHERE guild_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
                [guild_id],
            )
            if not row:
                return

            request_id = row["id"]
            raw = row.get("attacker_ids")
            attacker_ids: list[str] = []
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        attacker_ids = [str(v) for v in parsed]
                except Exception:
                    attacker_ids = []

            uid = str(user_id)
            if uid in attacker_ids:
                return
            attacker_ids.append(uid)

            await store.execute(
                "UPDATE recovery_requests SET attacker_ids = ? WHERE id = ? AND guild_id = ?",
                [json.dumps(attacker_ids, ensure_ascii=False), request_id, guild_id],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to append attacker to pending request guild=%s user=%s: %s",
                guild_id,
                user_id,
                exc,
            )

    async def _pin_pre_attack_snapshots(
        self, store, guild_id: str
    ) -> None:
        """將攻擊前各目標的最新快照標記為 pinned=1，防止守護程式將其清除。"""
        try:
            await store.execute(
                """
                UPDATE structure_snapshots
                SET pinned = 1
                WHERE guild_id = ? AND id IN (
                    SELECT s1.id
                    FROM structure_snapshots s1
                    INNER JOIN (
                        SELECT target_id, target_type, MAX(timestamp) AS max_ts
                        FROM structure_snapshots
                        WHERE guild_id = ? AND timestamp <= (strftime('%s', 'now') - ?)
                        GROUP BY target_id, target_type
                    ) s2 ON s1.target_id = s2.target_id
                           AND s1.target_type = s2.target_type
                           AND s1.timestamp = s2.max_ts
                    WHERE s1.guild_id = ?
                )
                """,
                [guild_id, guild_id, _RECOVERY_LOOKBACK, guild_id],
            )
            logger.info("Pinned pre-attack snapshots guild=%s", guild_id)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to pin snapshots guild=%s: %s", guild_id, exc, exc_info=True
            )

    async def _record_event(
        self,
        store,
        guild_id: str,
        event_type: str,
        target_id: str,
        old_data: Optional[str],
        new_data: Optional[str],
    ) -> None:
        """將事件寫入 temp_cache，作為異常判斷與復原依據。"""
        try:
            await store.execute(
                """
                INSERT INTO temp_cache
                    (guild_id, event_type, target_id, old_data, new_data)
                VALUES (?, ?, ?, ?, ?)
                """,
                [guild_id, event_type, target_id, old_data, new_data],
            )
            logger.info(
                "Recorded event guild=%s event=%s target=%s old=%s new=%s",
                guild_id,
                event_type,
                target_id,
                bool(old_data),
                bool(new_data),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to record event guild=%s event=%s target=%s: %s",
                guild_id,
                event_type,
                target_id,
                exc,
                exc_info=True,
            )

    async def _get_threshold_profile(
        self,
        store,
        guild_id: str,
        event_type: str,
    ) -> tuple[int, int]:
        """讀取伺服器自訂門檻與視窗秒數，找不到時退回預設值。"""
        default_threshold = int(_THRESHOLD.get(event_type, 999))
        default_window = int(_THRESHOLD_WINDOWS.get(event_type, _ANOMALY_WINDOW))
        try:
            rows = await store.fetchall(
                "SELECT threshold_value, window_seconds FROM guild_thresholds "
                "WHERE guild_id = ? AND event_type = ?",
                [guild_id, event_type],
            )
            if rows:
                value = int(rows[0]["threshold_value"])
                window_seconds = int(rows[0].get("window_seconds") or default_window)
                logger.info(
                    "Loaded custom threshold guild=%s event=%s threshold=%s window=%ss",
                    guild_id,
                    event_type,
                    value,
                    window_seconds,
                )
                return value, max(1, window_seconds)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to load threshold profile guild=%s event=%s: %s; using default",
                guild_id,
                event_type,
                exc,
                exc_info=True,
            )
        logger.debug(
            "Using default threshold profile guild=%s event=%s threshold=%s window=%ss",
            guild_id,
            event_type,
            default_threshold,
            default_window,
        )
        return default_threshold, max(1, default_window)

    async def _get_recent_event_summary(
        self, store, guild_id: str
    ) -> dict[str, int]:
        """彙總近 _ANOMALY_WINDOW 秒內各異常事件數量。"""
        try:
            rows = await store.fetchall(
                """
                SELECT event_type, COUNT(*) AS cnt
                FROM temp_cache
                WHERE guild_id = ?
                  AND timestamp >= (strftime('%s', 'now') - ?)
                GROUP BY event_type
                """,
                [guild_id, _ANOMALY_WINDOW],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to build event summary guild=%s: %s",
                guild_id,
                exc,
                exc_info=True,
            )
            return {}

        summary: dict[str, int] = {}
        for row in rows:
            ev = row.get("event_type")
            cnt = int(row.get("cnt") or 0)
            if not ev or cnt <= 0:
                continue
            summary[str(ev)] = cnt
        return summary

    async def _check_and_alert(
        self, store, guild: discord.Guild, event_type: str
    ) -> None:
        """依時間視窗統計事件量，達門檻時建立請求並發送警報。"""
        guild_id = str(guild.id)
        recovery_cog = self.bot.get_cog("Recovery")
        if recovery_cog and hasattr(recovery_cog, "is_recovery_in_progress"):
            if recovery_cog.is_recovery_in_progress(guild_id):
                logger.debug(
                    "Recovery in progress, skip anomaly alert guild=%s event=%s",
                    guild_id,
                    event_type,
                )
                return

        defense_state = await get_defense_state(store, guild_id)
        if not defense_state["enabled"]:
            logger.debug(
                "Defense paused, skip anomaly alert guild=%s event=%s remaining=%s",
                guild_id,
                event_type,
                defense_state["remaining_seconds"],
            )
            return

        threshold, window_seconds = await self._get_threshold_profile(
            store,
            guild_id,
            event_type,
        )

        self._log_deferred(
            logging.DEBUG,
            "Checking anomaly guild=%s name=%s event=%s threshold=%s window=%s",
            guild_id,
            guild.name,
            event_type,
            threshold,
            window_seconds,
        )

        lock_key = guild_id
        lock = self._alert_locks.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            self._alert_locks[lock_key] = lock

        async with lock:
            try:
                rows = await store.fetchall(
                    """
                    SELECT COUNT(*) AS cnt FROM temp_cache
                    WHERE guild_id = ? AND event_type = ?
                      AND timestamp >= (strftime('%s', 'now') - ?)
                    """,
                                        [guild_id, event_type, window_seconds],
                )
                count = rows[0]["cnt"] if rows else 0
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Anomaly check failed guild=%s event=%s: %s",
                    guild_id,
                    event_type,
                    exc,
                    exc_info=True,
                )
                return

            self._log_deferred(
                logging.DEBUG,
                "Anomaly count guild=%s event=%s count=%s threshold=%s",
                guild_id,
                event_type,
                count,
                threshold,
            )

            if count < threshold:
                self._log_deferred(
                    logging.DEBUG,
                    "Anomaly not triggered guild=%s event=%s count=%s threshold=%s",
                    guild_id,
                    event_type,
                    count,
                    threshold,
                )
                return

            logger.warning(
                "ANOMALY in guild %s: %s count=%d (threshold=%d)",
                guild_id, event_type, count, threshold,
            )

            # 每個伺服器同時間只允許一筆 pending 請求，避免不同事件類型重複告警。
            request_id = None
            existing_pending_request = False
            try:
                existing = await store.fetchall(
                    "SELECT id FROM recovery_requests "
                    "WHERE guild_id = ? AND status = 'pending' "
                    "ORDER BY created_at DESC LIMIT 1",
                    [guild_id],
                )
                if existing:
                    pending_id = existing[0]["id"]
                    existing_pending_request = True
                    await store.execute(
                        "UPDATE recovery_requests "
                        "SET event_count = CASE WHEN event_count > ? THEN event_count ELSE ? END "
                        "WHERE id = ? AND guild_id = ?",
                        [count, count, pending_id, guild_id],
                    )
                    request_id = pending_id
                    logger.info(
                        "Pending recovery request already exists guild=%s event=%s request_id=%s",
                        guild_id,
                        event_type,
                        pending_id,
                    )
                else:
                    await store.execute(
                        "INSERT INTO recovery_requests (guild_id, event_type, event_count) "
                        "VALUES (?, ?, ?)",
                        [guild_id, event_type, count],
                    )
                    row = await store.fetchall(
                        "SELECT id FROM recovery_requests "
                        "WHERE guild_id = ? AND status = 'pending' "
                        "ORDER BY created_at DESC LIMIT 1",
                        [guild_id],
                    )
                    if row:
                        request_id = row[0]["id"]
                        logger.info(
                            "Created recovery request guild=%s event=%s request_id=%s count=%s",
                            guild_id,
                            event_type,
                            request_id,
                            count,
                        )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Failed to create recovery request guild=%s event=%s count=%s: %s",
                    guild_id,
                    event_type,
                    count,
                    exc,
                    exc_info=True,
                )
                return

            if existing_pending_request:
                logger.debug(
                    "Pending request exists; skip repeated alert/request guild=%s event=%s pending_request=%s",
                    guild_id,
                    event_type,
                    request_id,
                )

            # 非 message_spam 事件透過稽核紀錄找出攻擊者並立即處置。
            # message_spam 攻擊者已在 _handle_message_spam_detected 中處理。
            if event_type != "message_spam":
                scan_key = f"{guild_id}:{event_type}"
                scan_now = time.time()
                last_scan = self._attacker_scan_at.get(scan_key, 0.0)
                _ATTACKER_SCAN_COOLDOWN = 30  # 30 秒內不重複掃描稽核紀錄
                if (scan_now - last_scan) < _ATTACKER_SCAN_COOLDOWN:
                    logger.debug(
                        "Skip attacker scan (cooldown) guild=%s event=%s since_last=%.1fs",
                        guild_id, event_type, scan_now - last_scan,
                    )
                    if existing_pending_request:
                        return
                    # 非 pending 情況下跳過掃描，直接進入 alert 流程
                    attacker_ids = set()
                else:
                    self._attacker_scan_at[scan_key] = scan_now
                    attacker_ids = await self._find_attackers_from_audit(guild, event_type)
                if attacker_ids:
                    logger.warning(
                        "Found %d attacker(s) guild=%s event=%s ids=%s",
                        len(attacker_ids), guild_id, event_type, attacker_ids,
                    )
                    # 更新 recovery_requests 紀錄攻擊者清單。
                    if request_id:
                        try:
                            await store.execute(
                                "UPDATE recovery_requests SET attacker_ids = ? WHERE id = ? AND guild_id = ?",
                                [json.dumps([str(i) for i in attacker_ids]), request_id, guild_id],
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.error(
                                "Failed to save attacker_ids request_id=%s: %s", request_id, exc
                            )
                    ban_now = time.time()
                    ids_to_ban: set[int] = set()
                    for uid in attacker_ids:
                        member = guild.get_member(uid)
                        # 對仍在場的機器人攻擊者不使用冷卻，避免快速重加造成防禦空窗。
                        if not (member and member.bot) and self._should_skip_recently_neutralized(guild_id, uid, ban_now):
                            logger.debug(
                                "Skip recently neutralized attacker guild=%s event=%s user=%s",
                                guild_id,
                                event_type,
                                uid,
                            )
                            continue
                        ids_to_ban.add(uid)

                    if ids_to_ban:
                        await self._ban_user_ids(
                            guild=guild,
                            user_ids=ids_to_ban,
                            source_message_id=str(request_id or ""),
                            source_kind=f"anomaly:{event_type}",
                        )

            # 有 pending 請求時，若本次 session 已有傳送過告警訊息的紀錄，則跳過；
            # 否則（例如機器人重啟後遺失 in-memory 紀錄）仍需補發告警。
            if existing_pending_request:
                if guild_id in self._guilds_with_alert_msgs:
                    return

            now = time.time()
            last_alert = self._last_alert_at.get(lock_key, 0.0)
            if (now - last_alert) < _ALERT_COOLDOWN_SECONDS:
                logger.debug(
                    "Anomaly alert throttled guild=%s event=%s since_last=%.1fs cooldown=%ss",
                    guild_id,
                    event_type,
                    now - last_alert,
                    _ALERT_COOLDOWN_SECONDS,
                )
                return
            self._last_alert_at[lock_key] = now

            # 釘住攻擊前快照，防止守護程式擠掉可還原資料。
            await self._pin_pre_attack_snapshots(store, guild_id)

            event_summary = await self._get_recent_event_summary(store, guild_id)
            await self._alert_approvers(
                store,
                guild,
                event_type,
                count,
                request_id,
                event_summary,
            )

    async def _alert_approvers(
        self, store, guild: discord.Guild, event_type: str, count: int,
        request_id: int | None = None,
        event_summary: dict[str, int] | None = None,
    ) -> None:
        """將異常警報透過私訊發給核准者，附上一鍵復原按鈕。"""
        guild_id = str(guild.id)
        try:
            approvers = await store.fetchall(
                "SELECT user_id FROM recovery_approvers WHERE guild_id = ?",
                [guild_id],
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to fetch approvers guild=%s event=%s request_id=%s: %s",
                guild_id,
                event_type,
                request_id,
                exc,
                exc_info=True,
            )
            return

        if not approvers:
            logger.warning(
                "No approvers registered guild=%s event=%s request_id=%s",
                guild_id,
                event_type,
                request_id,
            )
            return

        logger.info(
            "Preparing anomaly alert guild=%s event=%s request_id=%s approvers=%s count=%s",
            guild_id,
            event_type,
            request_id,
            len(approvers),
            count,
        )

        label = _EVENT_LABELS.get(event_type, event_type)
        summary = event_summary or {}
        summary_lines: list[str] = []
        for ev, ev_count in sorted(summary.items(), key=lambda item: item[1], reverse=True):
            ev_label = _EVENT_LABELS.get(ev, ev)
            summary_lines.append(f"- {ev_label}: {ev_count}")
        summary_text = "\n".join(summary_lines) if summary_lines else "- 無可用摘要"

        embed = discord.Embed(
            title="\U0001f6a8 伺服器異常警報",
            description=(
                f"**{guild.name}** 偵測到異常活動：\n\n"
                f"\U0001f4cb 類型：**{label}**\n"
                f"\U0001f4ca 目前時間窗內事件數：**{count}**\n\n"
                f"\U0001f4dd 伺服器異常摘要（目前時間窗）：\n{summary_text}\n\n"
                f"如需將伺服器復原至攻擊前快照狀態，請點擊下方按鈕。"
            ),
            color=discord.Color.red(),
        )

        view = discord.ui.View(timeout=1800)  # 30 minutes
        custom = (
            f"execute_recovery:{guild_id}:{request_id}"
            if request_id
            else f"execute_recovery:{guild_id}"
        )
        recovery_btn = discord.ui.Button(
            label="執行復原",
            style=discord.ButtonStyle.danger,
            custom_id=custom,
            emoji="\U0001f504",
        )
        decline_custom = (
            f"decline_recovery:{guild_id}:{request_id}"
            if request_id
            else f"decline_recovery:{guild_id}"
        )
        decline_btn = discord.ui.Button(
            label="不同意",
            style=discord.ButtonStyle.secondary,
            custom_id=decline_custom,
            emoji="\u274c",
        )

        # 交由 RecoveryCog.on_interaction 統一處理，避免重複 acknowledge。
        view.add_item(recovery_btn)
        view.add_item(decline_btn)

        alert_msg_ids: dict[str, int] = {}
        async def _send_one_alert(row: dict[str, Any]) -> tuple[int, str | None, int | None]:
            user_id = row["user_id"]
            user = self.bot.get_user(int(row["user_id"]))
            if user is None:
                try:
                    user = await rate_limited_call(
                        self.bot.fetch_user,
                        int(row["user_id"]),
                        limit_key="user_fetch",
                    )
                except discord.NotFound:
                    logger.warning(
                        "Approver user not found guild=%s approver=%s event=%s request_id=%s",
                        guild_id,
                        user_id,
                        event_type,
                        request_id,
                    )
                    return (0, None, None)
            try:
                msg_key = (guild_id, user_id)
                prev_msg_id = self._last_alert_msg_ids.get(msg_key)
                edited = False
                if prev_msg_id:
                    try:
                        dm = await user.create_dm()
                        prev_msg = await dm.fetch_message(prev_msg_id)
                        await rate_limited_call(prev_msg.edit, embed=embed, view=view)
                        edited = True
                        logger.info(
                            "Edited existing alert msg guild=%s approver=%s event=%s request_id=%s",
                            guild_id, user_id, event_type, request_id,
                        )
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass  # 訊息已消失，改為重新發送
                if not edited:
                    msg = await rate_limited_call(
                        user.send,
                        embed=embed,
                        view=view,
                        limit_key="dm_send",
                    )
                    self._last_alert_msg_ids[msg_key] = msg.id
                    self._guilds_with_alert_msgs.add(guild_id)
                    logger.info(
                        "Sent anomaly alert guild=%s approver=%s event=%s request_id=%s",
                        guild_id,
                        user_id,
                        event_type,
                        request_id,
                    )
                    await dm_sleep()
                    return (1, user_id, msg.id)

                return (1, user_id, prev_msg_id)
            except discord.Forbidden:
                logger.warning(
                    "Cannot DM approver guild=%s approver=%s event=%s request_id=%s",
                    guild_id,
                    user_id,
                    event_type,
                    request_id,
                )
                return (0, None, None)
            except discord.HTTPException as exc:
                logger.warning(
                    "Failed DM approver guild=%s approver=%s event=%s request_id=%s: %s",
                    guild_id,
                    user_id,
                    event_type,
                    request_id,
                    exc,
                    exc_info=True,
                )
                return (0, None, None)

        results = await self._gather_bounded(
            [_send_one_alert(row) for row in approvers],
            _ALERT_DM_CONCURRENCY,
        )
        sent = 0
        for sent_one, uid, mid in results:
            sent += sent_one
            if uid and mid:
                alert_msg_ids[uid] = mid

        # 將訊息 ID 存入 DB 供 RecoveryCog 後續編輯用。
        if request_id and alert_msg_ids:
            try:
                await store.execute(
                    "UPDATE recovery_requests SET alert_msg_ids = ? WHERE id = ? AND guild_id = ?",
                    [json.dumps(alert_msg_ids), request_id, guild_id],
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Failed to save alert_msg_ids request_id=%s: %s", request_id, exc
                )

        logger.info(
            "Anomaly alert complete guild=%s event=%s request_id=%s sent=%d total=%d",
            guild_id,
            event_type,
            request_id,
            sent,
            len(approvers),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MonitoringCog(bot))

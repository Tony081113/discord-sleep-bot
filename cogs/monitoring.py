"""監控模組（Phase 3）。

職責：
1. 監聽並加密保存訊息（on_message）。
2. 追蹤伺服器結構變更（頻道、身分組）。
3. 偵測異常事件並通知復原核准者。
"""

import asyncio
import datetime
import json
import time
from collections import defaultdict, deque
from typing import Any, Optional

import discord
from discord.ext import commands

from mods.crypto import encrypt
from mods.defense import get_defense_state
from mods.logger import setup_logger
from mods.rate_limit import rate_limited_call, DM_SEND_DELAY
from mods.storage import get_storage

logger = setup_logger(__name__)

# ---------------------------------------------------------------------------
# 異常偵測門檻（5 分鐘視窗）
# ---------------------------------------------------------------------------

_ANOMALY_WINDOW = 300  # seconds
_RECOVERY_LOOKBACK = 300  # seconds — must match recovery.py
_MESSAGE_SPAM_WINDOW = 10  # seconds
_MESSAGE_SPAM_COOLDOWN = 60  # seconds
_BAN_OP_DELAY = 1.0  # seconds
_ALERT_COOLDOWN_SECONDS = 120
_SPAM_CLEANUP_LOOKBACK_SECONDS = 20
_SPAM_CLEANUP_MAX_MESSAGES = 25

_THRESHOLD: dict[str, int] = {
    "channel_delete": 3,
    "channel_update": 5,
    "role_delete": 3,
    "role_update": 5,
    "admin_perm_remove": 2,
    "message_spam": 8,
}

_AUDIT_ACTION_MAP: dict[str, discord.AuditLogAction] = {
    "channel_delete": discord.AuditLogAction.channel_delete,
    "channel_update": discord.AuditLogAction.channel_update,
    "role_delete": discord.AuditLogAction.role_delete,
    "role_update": discord.AuditLogAction.role_update,
    "admin_perm_remove": discord.AuditLogAction.role_update,
}

_EVENT_LABELS: dict[str, str] = {
    "channel_delete": "大量頻道被刪除",
    "role_delete": "大量身分組被刪除",
    "admin_perm_remove": "管理員權限被移除",
    "channel_update": "大量頻道被修改",
    "role_update": "大量身分組被修改",
    "message_spam": "訊息轟炸",
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
        # 告警鎖：避免同 guild/event 並發重複建立請求與連發通知。
        self._alert_locks: dict[tuple[str, str], asyncio.Lock] = {}
        # 告警冷卻：避免短時間內重複發送同類異常通知。
        self._last_alert_at: dict[tuple[str, str], float] = {}
        # 已發送的告警訊息 ID：(guild_id, event_type, user_id) -> message_id，用於編輯而非重發。
        self._last_alert_msg_ids: dict[tuple[str, str, str], int] = {}

    # ----------------------------------------------------------- on_message

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """加密並保存所有非機器人的伺服器訊息。"""
        if message.author.bot or message.guild is None:
            return

        store = get_storage()
        guild_id = str(message.guild.id)
        if message.content:
            encrypted_content, nonce = encrypt(message.content)
            avatar_url = (
                message.author.display_avatar.url
                if message.author.display_avatar
                else None
            )

            try:
                await store.execute(
                    """
                    INSERT OR IGNORE INTO encrypted_messages
                        (message_id, channel_id, guild_id, author_id,
                         author_name, author_avatar, encrypted_content, nonce)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        str(message.id),
                        str(message.channel.id),
                        guild_id,
                        str(message.author.id),
                        message.author.display_name,
                        avatar_url,
                        encrypted_content,
                        nonce,
                    ],
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to store message %s: %s", message.id, exc)

        # 訊息保存後檢查是否命中轟炸門檻。
        await self._detect_message_spam(message, store)

    # ------------------------------------------------------ channel events

    @commands.Cog.listener()
    async def on_guild_channel_delete(
        self, channel: discord.abc.GuildChannel
    ) -> None:
        """記錄頻道刪除事件，並檢查是否觸發異常警報。"""
        guild_id = str(channel.guild.id)
        store = get_storage()
        await self._record_event(
            store, guild_id, "channel_delete", str(channel.id),
            json.dumps(_channel_to_dict(channel), ensure_ascii=False), None,
        )
        await self._check_and_alert(store, channel.guild, "channel_delete")

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel,
    ) -> None:
        """記錄頻道修改事件，更新快照與 channels 表。"""
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
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        """記錄身分組刪除事件，並檢查是否觸發異常警報。"""
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
    async def on_guild_update(
        self, before: discord.Guild, after: discord.Guild
    ) -> None:
        """保存伺服器名稱/縮圖/橫幅快照供後續復原。"""
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

        threshold = await self._get_threshold(store, guild_id, "message_spam")
        now = time.time()
        key = (guild.id, message.author.id)
        window = self._message_windows[key]
        window.append(now)

        while window and (now - window[0]) > _MESSAGE_SPAM_WINDOW:
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
    ) -> dict[str, Any]:
        """建立轟炸事件 JSON，用於稽核與後續處置。"""
        return {
            "event": "message_spam",
            "burst_count": burst_count,
            "window_seconds": _MESSAGE_SPAM_WINDOW,
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
    ) -> None:
        """命中轟炸時：保存 JSON，並依 authorizing_integration_owners 封鎖。"""
        guild = message.guild
        if guild is None:
            return

        owners = self._extract_authorizing_integration_owners(message)
        payload = self._build_spam_message_payload(message, burst_count, owners)

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

        # 若 JSON 含 authorizing_integration_owners，視為 User Install 路徑：
        # 先封 owner，再刪訊息；不要嘗試封鎖發訊者。
        if owners:
            await self._ban_authorizing_owner_ids(guild, owners, str(message.id))
            await self._delete_detected_spam_message(message, source_kind="user_install")
            await self._delete_recent_spam_messages(
                message,
                source_kind="user_install",
            )
        else:
            # 無 owner 資訊時，退回封鎖發訊者，再刪訊息。
            await self._ban_user_ids(
                guild=guild,
                user_ids={message.author.id},
                source_message_id=str(message.id),
                source_kind="message_author_fallback",
            )
            await self._delete_detected_spam_message(message, source_kind="message_author")
            await self._delete_recent_spam_messages(
                message,
                source_kind="message_author",
            )

        # 釘住快照並建立復原請求（與其他事件類型統一流程）。
        await self._pin_pre_attack_snapshots(store, str(guild.id))
        await self._check_and_alert(store, guild, "message_spam")

    async def _delete_detected_spam_message(
        self,
        message: discord.Message,
        source_kind: str,
    ) -> None:
        """刪除命中轟炸偵測的訊息。"""
        try:
            await rate_limited_call(
                message.delete,
                reason=f"Message spam detected ({source_kind})",
            )
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
        """刪除同一波 recent spam 訊息（同頻道、同作者）。"""
        channel = message.channel
        if not isinstance(channel, discord.TextChannel):
            return

        cutoff = discord.utils.utcnow() - datetime.timedelta(
            seconds=_SPAM_CLEANUP_LOOKBACK_SECONDS
        )
        deleted = 0

        try:
            async for item in channel.history(limit=100, after=cutoff):
                if deleted >= _SPAM_CLEANUP_MAX_MESSAGES:
                    break
                if item.id == message.id:
                    continue
                if item.author.id != message.author.id:
                    continue
                try:
                    await rate_limited_call(
                        item.delete,
                        reason=f"Recent spam cleanup ({source_kind})",
                    )
                    deleted += 1
                    await asyncio.sleep(0.25)
                except discord.NotFound:
                    continue
                except discord.Forbidden:
                    logger.error(
                        "Recent spam cleanup forbidden channel=%s source=%s",
                        channel.id,
                        source_kind,
                    )
                    break
                except discord.HTTPException as exc:
                    logger.warning(
                        "Recent spam cleanup failed channel=%s msg=%s source=%s: %s",
                        channel.id,
                        item.id,
                        source_kind,
                        exc,
                    )
            if deleted:
                logger.warning(
                    "Recent spam cleanup done guild=%s channel=%s deleted=%s source=%s",
                    message.guild.id if message.guild else "unknown",
                    channel.id,
                    deleted,
                    source_kind,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Recent spam cleanup crashed channel=%s source=%s: %s",
                channel.id,
                source_kind,
                exc,
                exc_info=True,
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

        for user_id in user_ids:
            try:
                if self.bot.user and user_id == self.bot.user.id:
                    continue
                if user_id == guild.owner_id:
                    logger.warning(
                        "Skipping neutralize for guild owner guild=%s user=%s",
                        guild.id, user_id,
                    )
                    continue

                # 僅處理可解析為 Discord 使用者的 ID。
                await rate_limited_call(self.bot.fetch_user, user_id)

                member = guild.get_member(user_id)
                if member:
                    # 1. 拔除所有非系統身分組
                    safe_roles = [r for r in member.roles if r.managed or r.is_default()]
                    try:
                        await rate_limited_call(
                            member.edit,
                            roles=safe_roles,
                            reason=f"Attack detected — stripping roles ({source_kind})",
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
                        until = discord.utils.utcnow() + datetime.timedelta(days=28)
                        await rate_limited_call(
                            member.edit,
                            timed_out_until=until,
                            reason=f"Attack detected — muted pending ban ({source_kind})",
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
                )
                logger.warning(
                    "Banned user from %s guild=%s user=%s source_message=%s",
                    source_kind,
                    guild.id,
                    user_id,
                    source_message_id,
                )
                await asyncio.sleep(_BAN_OP_DELAY)
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
            except discord.HTTPException as exc:
                logger.error(
                    "Ban failed guild=%s user=%s source_message=%s: %s",
                    guild.id,
                    user_id,
                    source_message_id,
                    exc,
                    exc_info=True,
                )

    async def _find_attackers_from_audit(
        self, guild: discord.Guild, event_type: str
    ) -> set[int]:
        """從稽核紀錄找出近 _ANOMALY_WINDOW 秒內執行異常操作的使用者 ID。"""
        action = _AUDIT_ACTION_MAP.get(event_type)
        if not action:
            return set()
        attacker_ids: set[int] = set()
        cutoff = time.time() - _ANOMALY_WINDOW
        try:
            async for entry in guild.audit_logs(limit=50, action=action):
                if entry.created_at.timestamp() < cutoff:
                    break
                if entry.user and entry.user.id != (self.bot.user.id if self.bot.user else None):
                    attacker_ids.add(entry.user.id)
        except discord.Forbidden:
            logger.warning(
                "No audit log access guild=%s event=%s", guild.id, event_type
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Audit log fetch failed guild=%s event=%s: %s", guild.id, event_type, exc
            )
        return attacker_ids

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

    async def _get_threshold(
        self, store, guild_id: str, event_type: str
    ) -> int:
        """讀取伺服器自訂門檻，找不到時退回預設值。"""
        try:
            rows = await store.fetchall(
                "SELECT threshold_value FROM guild_thresholds "
                "WHERE guild_id = ? AND event_type = ?",
                [guild_id, event_type],
            )
            if rows:
                logger.info(
                    "Loaded custom threshold guild=%s event=%s threshold=%s",
                    guild_id,
                    event_type,
                    rows[0]["threshold_value"],
                )
                return rows[0]["threshold_value"]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to load threshold guild=%s event=%s: %s; using default",
                guild_id,
                event_type,
                exc,
                exc_info=True,
            )
        threshold = _THRESHOLD.get(event_type, 999)
        logger.debug(
            "Using default threshold guild=%s event=%s threshold=%s",
            guild_id,
            event_type,
            threshold,
        )
        return threshold

    async def _check_and_alert(
        self, store, guild: discord.Guild, event_type: str
    ) -> None:
        """依時間視窗統計事件量，達門檻時建立請求並發送警報。"""
        guild_id = str(guild.id)
        defense_state = await get_defense_state(store, guild_id)
        if not defense_state["enabled"]:
            logger.debug(
                "Defense paused, skip anomaly alert guild=%s event=%s remaining=%s",
                guild_id,
                event_type,
                defense_state["remaining_seconds"],
            )
            return

        threshold = await self._get_threshold(store, guild_id, event_type)

        logger.info(
            "Checking anomaly guild=%s name=%s event=%s threshold=%s window=%s",
            guild_id,
            guild.name,
            event_type,
            threshold,
            _ANOMALY_WINDOW,
        )

        lock_key = (guild_id, event_type)
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
                    [guild_id, event_type, _ANOMALY_WINDOW],
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

            logger.info(
                "Anomaly count guild=%s event=%s count=%s threshold=%s",
                guild_id,
                event_type,
                count,
                threshold,
            )

            if count < threshold:
                logger.info(
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

            # 若尚無待處理請求，建立新的 recovery request。
            request_id = None
            try:
                existing = await store.fetchall(
                    "SELECT id FROM recovery_requests "
                    "WHERE guild_id = ? AND event_type = ? AND status = 'pending'",
                    [guild_id, event_type],
                )
                if existing:
                    logger.info(
                        "Pending recovery request already exists guild=%s event=%s request_id=%s",
                        guild_id,
                        event_type,
                        existing[0]["id"],
                    )
                    return  # 同類異常已有待處理請求，避免重複通知。
                await store.execute(
                    "INSERT INTO recovery_requests (guild_id, event_type, event_count) "
                    "VALUES (?, ?, ?)",
                    [guild_id, event_type, count],
                )
                row = await store.fetchall(
                    "SELECT id FROM recovery_requests "
                    "WHERE guild_id = ? AND event_type = ? AND status = 'pending' "
                    "ORDER BY created_at DESC LIMIT 1",
                    [guild_id, event_type],
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

            self._last_alert_at[lock_key] = now
            # 非 message_spam 事件透過稽核紀錄找出攻擊者並立即處置。
            # message_spam 攻擊者已在 _handle_message_spam_detected 中處理。
            if event_type != "message_spam":
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
                    for uid in attacker_ids:
                        await self._ban_user_ids(
                            guild=guild,
                            user_ids={uid},
                            source_message_id=str(request_id or ""),
                            source_kind=f"anomaly:{event_type}",
                        )

            # 釘住攻擊前快照，防止守護程式擠掉可還原資料。
            await self._pin_pre_attack_snapshots(store, guild_id)

            await self._alert_approvers(store, guild, event_type, count, request_id)

    async def _alert_approvers(
        self, store, guild: discord.Guild, event_type: str, count: int,
        request_id: int | None = None,
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
        embed = discord.Embed(
            title="\U0001f6a8 伺服器異常警報",
            description=(
                f"**{guild.name}** 偵測到異常活動：\n\n"
                f"\U0001f4cb 類型：**{label}**\n"
                f"\U0001f4ca 5 分鐘內事件數：**{count}**\n\n"
                f"如需將伺服器復原至 5 分鐘前的狀態，請點擊下方按鈕。"
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

        sent = 0
        alert_msg_ids: dict[str, int] = {}
        for row in approvers:
            user_id = row["user_id"]
            user = self.bot.get_user(int(row["user_id"]))
            if user is None:
                try:
                    user = await rate_limited_call(self.bot.fetch_user, int(row["user_id"]))
                except discord.NotFound:
                    logger.warning(
                        "Approver user not found guild=%s approver=%s event=%s request_id=%s",
                        guild_id,
                        user_id,
                        event_type,
                        request_id,
                    )
                    continue
            try:
                msg_key = (guild_id, event_type, user_id)
                prev_msg_id = self._last_alert_msg_ids.get(msg_key)
                edited = False
                if prev_msg_id:
                    try:
                        dm = await user.create_dm()
                        prev_msg = await dm.fetch_message(prev_msg_id)
                        await rate_limited_call(prev_msg.edit, embed=embed, view=view)
                        edited = True
                        alert_msg_ids[user_id] = prev_msg_id
                        logger.info(
                            "Edited existing alert msg guild=%s approver=%s event=%s request_id=%s",
                            guild_id, user_id, event_type, request_id,
                        )
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass  # 訊息已消失，改為重新發送
                if not edited:
                    msg = await rate_limited_call(user.send, embed=embed, view=view)
                    self._last_alert_msg_ids[msg_key] = msg.id
                    alert_msg_ids[user_id] = msg.id
                    logger.info(
                        "Sent anomaly alert guild=%s approver=%s event=%s request_id=%s",
                        guild_id,
                        user_id,
                        event_type,
                        request_id,
                    )
                    await asyncio.sleep(DM_SEND_DELAY)
                sent += 1
            except discord.Forbidden:
                logger.warning(
                    "Cannot DM approver guild=%s approver=%s event=%s request_id=%s",
                    guild_id,
                    user_id,
                    event_type,
                    request_id,
                )
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

"""復原模組（Phase 4）。

職責：
1. 由核准者按鈕觸發復原流程。
2. 依 5 分鐘前快照還原頻道與身分組。
3. 透過 Webhook 還原訊息（保留原暱稱與頭像）。
"""

import asyncio
import json
import os
import pathlib
import time
from typing import Any

import aiohttp
import discord
from discord.ext import commands

from mods.crypto import decrypt
from mods.defense import get_defense_state, set_defense_disabled, set_defense_enabled
from mods.logger import setup_logger
from mods.rate_limit import (
    CHANNEL_OP_DELAY,
    ROLE_OP_DELAY,
    get_adaptive_delay,
    rate_limited_call,
)
from mods.storage import get_storage

logger = setup_logger(__name__)

_RECOVERY_LOOKBACK = 300  # seconds (5 minutes)
_WEBHOOK_NAME = "SleepBot Recovery"
_MAX_RESTORE_MESSAGES = 100
_FILE_RESTORE_UNSUPPORTED_TEXT = "暫不支持還原"


def _env_int(name: str, default: int, min_value: int, max_value: int) -> int:
    """Read int from env with clamped safety bounds."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid env %s=%r, fallback=%s", name, raw, default)
        return default
    return max(min_value, min(max_value, value))


def _env_float(name: str, default: float, min_value: float, max_value: float) -> float:
    """Read float from env with clamped safety bounds."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid env %s=%r, fallback=%s", name, raw, default)
        return default
    return max(min_value, min(max_value, value))


_WEBHOOK_SEND_DELAY = _env_float(
    "RECOVERY_WEBHOOK_SEND_DELAY", default=0.35, min_value=0.1, max_value=2.0
)


_RECOVERY_DEFENSE_PAUSE_SECONDS = _env_int(
    "RECOVERY_DEFENSE_PAUSE_SECONDS", default=180, min_value=60, max_value=900
)


_ROLE_MEMBER_RESTORE_CONCURRENCY = _env_int(
    "RECOVERY_ROLE_MEMBER_CONCURRENCY", default=4, min_value=1, max_value=8
)
_MESSAGE_RESTORE_CHANNEL_CONCURRENCY = _env_int(
    "RECOVERY_MESSAGE_CHANNEL_CONCURRENCY", default=3, min_value=1, max_value=5
)
_CHANNEL_RESTORE_CONCURRENCY = _env_int(
    "RECOVERY_CHANNEL_RESTORE_CONCURRENCY", default=3, min_value=1, max_value=4
)
_ROLE_RESTORE_CONCURRENCY = _env_int(
    "RECOVERY_ROLE_RESTORE_CONCURRENCY", default=3, min_value=1, max_value=4
)
_CHANNEL_DELETE_CONCURRENCY = _env_int(
    "RECOVERY_CHANNEL_DELETE_CONCURRENCY", default=2, min_value=1, max_value=4
)
_ROLE_DELETE_CONCURRENCY = _env_int(
    "RECOVERY_ROLE_DELETE_CONCURRENCY", default=2, min_value=1, max_value=4
)
_ALERT_EDIT_CONCURRENCY = _env_int(
    "RECOVERY_ALERT_EDIT_CONCURRENCY", default=3, min_value=1, max_value=8
)

_RECOVERY_RESUME_QUEUE_FILE = pathlib.Path(
    os.getenv(
        "RECOVERY_RESUME_QUEUE_FILE",
        str(pathlib.Path(__file__).resolve().parent.parent / "recovery_resume_queue.json"),
    )
)


class RecoveryCog(commands.Cog, name="Recovery"):
    """負責伺服器結構與訊息的復原。"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # 每個 guild 只允許單一還原流程同時執行，避免併發造成 429。
        self._guild_recovery_locks: dict[str, asyncio.Lock] = {}
        # 目前執行中的復原（供關機等待與跨重啟排程）。
        self._active_recoveries: dict[str, dict[str, Any]] = {}
        # 復原階段建立的舊頻道 -> 新頻道映射，供訊息還原精準定位頻道。
        self._last_channel_restore_map: dict[str, str] = {}
        # 啟動後僅恢復一次關機排隊的復原任務。
        self._resume_queue_started = False

    async def run_recovery_with_lock(
        self,
        store,
        guild: discord.Guild,
        request_id: str | None = None,
    ) -> tuple[int, int, int, list[dict]]:
        """在 guild 級別鎖下執行還原，避免同伺服器重複復原互相踩踏。"""
        guild_id = str(guild.id)
        lock = self._guild_recovery_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._guild_recovery_locks[guild_id] = lock

        async with lock:
            self._active_recoveries[guild_id] = {
                "request_id": request_id,
                "started_at": int(time.time()),
            }
            try:
                return await self._execute_recovery(store, guild, request_id)
            finally:
                self._active_recoveries.pop(guild_id, None)

    def is_recovery_in_progress(self, guild_id: str | int) -> bool:
        """回傳指定 guild 是否已有還原流程正在執行。"""
        key = str(guild_id)
        lock = self._guild_recovery_locks.get(key)
        return bool(lock and lock.locked())

    def get_active_recovery_snapshot(self) -> list[dict[str, Any]]:
        """取得目前復原中的 guild 快照。"""
        snapshot: list[dict[str, Any]] = []
        for guild_id, payload in self._active_recoveries.items():
            snapshot.append(
                {
                    "guild_id": guild_id,
                    "request_id": payload.get("request_id"),
                    "started_at": payload.get("started_at"),
                }
            )
        return snapshot

    async def wait_for_all_recoveries(self, timeout_seconds: int) -> tuple[bool, list[dict[str, Any]]]:
        """等待所有進行中復原完成；回傳 (是否全部完成, 剩餘清單)。"""
        deadline = time.monotonic() + max(0, timeout_seconds)
        while self._active_recoveries and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
        return (len(self._active_recoveries) == 0, self.get_active_recovery_snapshot())

    def persist_recovery_resume_queue(self, entries: list[dict[str, Any]]) -> int:
        """把未完成復原寫入本地檔，供下次開機續跑。"""
        if not entries:
            return 0

        queue: list[dict[str, Any]] = []
        if _RECOVERY_RESUME_QUEUE_FILE.exists():
            try:
                queue = json.loads(_RECOVERY_RESUME_QUEUE_FILE.read_text(encoding="utf-8"))
                if not isinstance(queue, list):
                    queue = []
            except Exception:
                queue = []

        queued_at = int(time.time())
        existing = {(str(i.get("guild_id")), str(i.get("request_id") or "")) for i in queue if isinstance(i, dict)}
        for item in entries:
            guild_id = str(item.get("guild_id") or "").strip()
            if not guild_id:
                continue
            request_id = str(item.get("request_id") or "")
            key = (guild_id, request_id)
            if key in existing:
                continue
            queue.append(
                {
                    "guild_id": guild_id,
                    "request_id": request_id or None,
                    "queued_at": queued_at,
                    "source": "shutdown",
                }
            )
            existing.add(key)

        _RECOVERY_RESUME_QUEUE_FILE.write_text(
            json.dumps(queue, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return len(queue)

    def enqueue_recovery_for_next_startup(
        self,
        guild_id: str | int,
        request_id: str | None = None,
        *,
        source: str = "shutdown",
    ) -> bool:
        """將單筆復原任務排入下次開機佇列。"""
        gid = str(guild_id).strip()
        if not gid:
            return False
        before_size = 0
        if _RECOVERY_RESUME_QUEUE_FILE.exists():
            try:
                existing = json.loads(_RECOVERY_RESUME_QUEUE_FILE.read_text(encoding="utf-8"))
                if isinstance(existing, list):
                    before_size = len(existing)
            except Exception:
                before_size = 0
        after_size = self.persist_recovery_resume_queue(
            [
                {
                    "guild_id": gid,
                    "request_id": request_id,
                    "source": source,
                }
            ]
        )
        return after_size > before_size

    async def _resume_recoveries_from_queue(self) -> None:
        """開機後續跑關機前排隊的復原任務。"""
        if not _RECOVERY_RESUME_QUEUE_FILE.exists():
            return

        try:
            raw = _RECOVERY_RESUME_QUEUE_FILE.read_text(encoding="utf-8")
            queue = json.loads(raw)
            if not isinstance(queue, list):
                queue = []
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load recovery resume queue: %s", exc)
            return

        if not queue:
            try:
                _RECOVERY_RESUME_QUEUE_FILE.unlink(missing_ok=True)
            except Exception:
                pass
            return

        store = get_storage()
        remaining: list[dict[str, Any]] = []
        resumed = 0
        for item in queue:
            if not isinstance(item, dict):
                continue
            guild_id = str(item.get("guild_id") or "").strip()
            request_id = item.get("request_id")
            if not guild_id:
                continue

            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                # Bot 目前不在該伺服器，保留到下次開機再試。
                remaining.append(item)
                continue

            if self.is_recovery_in_progress(guild_id):
                remaining.append(item)
                continue

            try:
                logger.warning(
                    "Resuming queued recovery guild=%s request_id=%s",
                    guild_id,
                    request_id,
                )
                await self.run_recovery_with_lock(store, guild, str(request_id) if request_id else None)
                resumed += 1
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Queued recovery failed guild=%s request_id=%s: %s",
                    guild_id,
                    request_id,
                    exc,
                    exc_info=True,
                )
                remaining.append(item)

        if remaining:
            try:
                _RECOVERY_RESUME_QUEUE_FILE.write_text(
                    json.dumps(remaining, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to persist remaining recovery queue: %s", exc)
        else:
            try:
                _RECOVERY_RESUME_QUEUE_FILE.unlink(missing_ok=True)
            except Exception:
                pass

        if resumed:
            logger.warning("Resumed %d queued recovery task(s) after restart", resumed)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Bot ready 後續跑上次關機排隊的復原任務。"""
        if self._resume_queue_started:
            return
        self._resume_queue_started = True
        asyncio.create_task(
            self._resume_recoveries_from_queue(),
            name="recovery_resume_queue",
        )

    async def _gather_bounded(self, coros: list, limit: int) -> list[Any]:
        """執行有界併發：加速還原，同時避免瞬間打滿 API。"""
        if not coros:
            return []
        sem = asyncio.Semaphore(max(1, limit))

        async def _runner(coro):
            async with sem:
                return await coro

        return await asyncio.gather(*(_runner(c) for c in coros), return_exceptions=False)

    # -------------------------------------------------------- interaction

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """接收按鈕互動，攔截 execute_recovery 與 decline_recovery 事件。"""
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")
        if custom_id.startswith("execute_recovery:"):
            await self._handle_recovery(interaction, custom_id)
        elif custom_id.startswith("decline_recovery:"):
            await self._handle_decline(interaction, custom_id)

    # -------------------------------------------------------- main handler

    async def _handle_recovery(
        self, interaction: discord.Interaction, custom_id: str
    ) -> None:
        """驗證核准者身份後，執行完整復原流程。"""

        async def _send_ephemeral(content: str) -> None:
            if interaction.response.is_done():
                await interaction.followup.send(content, ephemeral=True)
            else:
                await interaction.response.send_message(content, ephemeral=True)

        parts = custom_id.split(":")
        if len(parts) < 2:
            logger.warning("Invalid recovery custom_id=%s", custom_id)
            return
        guild_id = parts[1]
        request_id = parts[2] if len(parts) > 2 else None
        logger.info(
            "Recovery requested custom_id=%s guild=%s request_id=%s user=%s",
            custom_id,
            guild_id,
            request_id,
            interaction.user.id,
        )
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            logger.warning(
                "Recovery guild not found guild=%s request_id=%s user=%s",
                guild_id,
                request_id,
                interaction.user.id,
            )
            await _send_ephemeral("\u274c 找不到伺服器。")
            return

        # 檢查是否為核准者
        store = get_storage()
        approvers = await store.fetchall(
            "SELECT user_id FROM recovery_approvers WHERE guild_id = ?",
            [guild_id],
        )
        if str(interaction.user.id) not in {r["user_id"] for r in approvers}:
            logger.warning(
                "Recovery denied guild=%s request_id=%s user=%s approvers=%s",
                guild_id,
                request_id,
                interaction.user.id,
                len(approvers),
            )
            await _send_ephemeral("\u274c 你不是已註冊的核准者。")
            return

        # 關機流程中：不啟動新復原，改排到下次開機。
        if bool(getattr(self.bot, "_closing_with_recovery_wait", False)):
            self.enqueue_recovery_for_next_startup(
                guild_id,
                request_id,
                source="shutdown_interaction",
            )
            await _send_ephemeral("⏳ 系統正在關機，這筆復原已排程到下次開機自動執行。")
            return

        # 有 request_id 時，先鎖定請求狀態，避免重複執行同一筆復原。
        if request_id:
            request_rows = await store.fetchall(
                "SELECT status FROM recovery_requests WHERE id = ? AND guild_id = ?",
                [request_id, guild_id],
            )
            if not request_rows:
                await _send_ephemeral("\u274c 找不到這筆復原請求。")
                return

            current_status = request_rows[0]["status"]
            if current_status != "pending":
                if current_status in {"rejected", "declined"}:
                    rejected_embed = discord.Embed(
                        title="❌ 已拒絕還原",
                        description="此復原請求已被拒絕，不會執行還原。",
                        color=discord.Color.light_grey(),
                    )
                    await self._edit_alert_dms(
                        store,
                        guild_id,
                        request_id,
                        rejected_embed,
                        view=discord.ui.View(),
                    )
                    try:
                        if not interaction.response.is_done():
                            await interaction.response.edit_message(
                                embed=rejected_embed,
                                view=discord.ui.View(),
                            )
                        elif interaction.message:
                            await interaction.message.edit(
                                embed=rejected_embed,
                                view=discord.ui.View(),
                            )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "Failed to refresh rejected recovery message guild=%s request_id=%s: %s",
                            guild_id,
                            request_id,
                            exc,
                        )
                await _send_ephemeral(
                    f"ℹ️ 這筆復原請求目前狀態為「{current_status}」，不可重複執行。"
                )
                return

            await store.execute(
                "UPDATE recovery_requests "
                "SET status='processing', approved_by=? "
                "WHERE id=? AND guild_id=? AND status='pending'",
                [str(interaction.user.id), request_id, guild_id],
            )

            recheck = await store.fetchall(
                "SELECT status, approved_by FROM recovery_requests WHERE id = ? AND guild_id = ?",
                [request_id, guild_id],
            )
            if (
                not recheck
                or recheck[0]["status"] != "processing"
                or str(recheck[0].get("approved_by") or "") != str(interaction.user.id)
            ):
                await _send_ephemeral("ℹ️ 這筆復原請求已由其他人處理或狀態已變更。")
                return

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        logger.info(
            "Recovery approved for execution guild=%s request_id=%s user=%s",
            guild_id,
            request_id,
            interaction.user.id,
        )

        # 告知所有核准者：正在處理中。
        if request_id:
            processing_embed = discord.Embed(
                title="\U0001f504 還原處理中…",
                description=(
                    f"**{interaction.user.display_name}** 已同意，正在執行伺服器還原，請稍候。"
                ),
                color=discord.Color.yellow(),
            )
            await self._edit_alert_dms(
                store, guild_id, request_id, processing_embed, view=discord.ui.View()
            )

        try:
            ch, ro, ms, failed_roles = await self.run_recovery_with_lock(store, guild, request_id)

            # 若有對應請求，更新請求狀態
            if request_id:
                try:
                    await store.execute(
                        "UPDATE recovery_requests "
                        "SET status='approved', approved_by=?, "
                        "result_channels=?, result_roles=?, result_messages=?, "
                        "resolved_at=strftime('%s','now') "
                        "WHERE id=? AND guild_id=?",
                        [str(interaction.user.id), ch, ro, ms, request_id, guild_id],
                    )
                    logger.info(
                        "Recovery request marked approved request_id=%s guild=%s user=%s channels=%s roles=%s messages=%s",
                        request_id,
                        guild_id,
                        interaction.user.id,
                        ch,
                        ro,
                        ms,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "Failed to update recovery request request_id=%s guild=%s: %s",
                        request_id,
                        guild_id,
                        exc,
                        exc_info=True,
                    )

            # 構建復原完成訊息
            msg = f"\u2705 復原完成！\n" \
                  f"\U0001f4c1 頻道復原：{ch}\n" \
                  f"\U0001f3f7\ufe0f 身分組復原：{ro}\n" \
                  f"\U0001f4ac 訊息還原：{ms}"
            
            # 如果有無法復原的身份組，追加通知
            if failed_roles:
                msg += f"\n\n⚠️ **{len(failed_roles)} 個身分組無法復原：**"
                for role in failed_roles[:10]:  # 最多顯示 10 個
                    role_name = role.get("role_name", "未知")
                    reason = role.get("reason", "未知")
                    perms = role.get("permission_names", [])
                    
                    # 只顯示管理員權限或列出具體權限
                    if "administrator" in perms:
                        msg += f"\n• {role_name}：⚙️ 管理員權限已啟用"
                    else:
                        perm_str = ", ".join(perms[:3])
                        if len(perms) > 3:
                            perm_str += f" 等 {len(perms)} 個權限"
                        msg += f"\n• {role_name}：{perm_str}"
                
                if len(failed_roles) > 10:
                    msg += f"\n• ... 及其他 {len(failed_roles) - 10} 個"

            await _send_ephemeral(msg)
            # 通知所有核准者：已還原。
            if request_id:
                desc = f"\U0001f4c1 頻道：**{ch}**　\U0001f3f7\ufe0f 身分組：**{ro}**　\U0001f4ac 訊息：**{ms}**"
                if failed_roles:
                    desc += f"\n⚠️ {len(failed_roles)} 個身分組無法復原"
                
                done_embed = discord.Embed(
                    title="\u2705 伺服器已還原",
                    description=desc,
                    color=discord.Color.green(),
                )
                await self._edit_alert_dms(
                    store, guild_id, request_id, done_embed, view=discord.ui.View()
                )
        except Exception as exc:
            logger.error(
                "Recovery failed guild=%s request_id=%s user=%s: %s",
                guild_id,
                request_id,
                interaction.user.id,
                exc,
                exc_info=True,
            )
            if request_id:
                try:
                    await store.execute(
                        "UPDATE recovery_requests "
                        "SET status='failed', approved_by=?, resolved_at=strftime('%s','now') "
                        "WHERE id=? AND guild_id=?",
                        [str(interaction.user.id), request_id, guild_id],
                    )
                    logger.info(
                        "Recovery request marked failed request_id=%s guild=%s user=%s",
                        request_id,
                        guild_id,
                        interaction.user.id,
                    )
                except Exception as update_exc:  # noqa: BLE001
                    logger.error(
                        "Failed to mark recovery request failed request_id=%s guild=%s: %s",
                        request_id,
                        guild_id,
                        update_exc,
                        exc_info=True,
                    )
            await _send_ephemeral("\u274c 復原失敗，請稍後重試或聯繫系統管理員。")

    # -------------------------------------------------------- 不同意處理

    async def _handle_decline(
        self, interaction: discord.Interaction, custom_id: str
    ) -> None:
        """核准者點擊「不同意」按鈕：標記請求為 declined，不執行任何還原。"""
        parts = custom_id.split(":")
        if len(parts) < 2:
            return
        guild_id = parts[1]
        request_id = parts[2] if len(parts) > 2 else None

        store = get_storage()
        approvers = await store.fetchall(
            "SELECT user_id FROM recovery_approvers WHERE guild_id = ?", [guild_id]
        )
        if str(interaction.user.id) not in {r["user_id"] for r in approvers}:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "\u274c 你不是已註冊的核准者。", ephemeral=True
                )
            return

        if request_id:
            await store.execute(
                "UPDATE recovery_requests "
                "SET status='declined', approved_by=?, resolved_at=strftime('%s','now') "
                "WHERE id=? AND guild_id=? AND status='pending'",
                [str(interaction.user.id), request_id, guild_id],
            )
            logger.info(
                "Recovery declined guild=%s request_id=%s user=%s",
                guild_id, request_id, interaction.user.id,
            )

        declined_embed = discord.Embed(
            title="\u274c 已拒絕還原",
            description=f"**{interaction.user.display_name}** 選擇不執行還原。",
            color=discord.Color.light_grey(),
        )
        # 編輯所有核准者的告警訊息。
        if request_id:
            await self._edit_alert_dms(
                store, guild_id, request_id, declined_embed, view=discord.ui.View()
            )
        # 也直接編輯觸發互動的訊息（可能與上面重複，但確保當前訊息一定被更新）。
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(
                    embed=declined_embed, view=discord.ui.View()
                )
            else:
                await interaction.message.edit(embed=declined_embed, view=discord.ui.View())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to edit decline interaction msg: %s", exc)

    # -------------------------------------------------------- DM 編輯工具

    async def _edit_alert_dms(
        self,
        store,
        guild_id: str,
        request_id: str,
        embed: discord.Embed,
        view: discord.ui.View | None = None,
    ) -> None:
        """依 recovery_requests.alert_msg_ids 批次編輯所有核准者的告警私訊。"""
        try:
            rows = await store.fetchall(
                "SELECT alert_msg_ids FROM recovery_requests WHERE id = ? AND guild_id = ?",
                [request_id, guild_id],
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to fetch alert_msg_ids request_id=%s: %s", request_id, exc)
            return

        if not rows or not rows[0]["alert_msg_ids"]:
            return

        try:
            msg_id_map: dict[str, int] = json.loads(rows[0]["alert_msg_ids"])
        except Exception:
            return

        empty_view = discord.ui.View() if view is None else view

        async def _edit_one(user_id_str: str, msg_id: int) -> None:
            user = self.bot.get_user(int(user_id_str))
            if not user:
                try:
                    user = await self.bot.fetch_user(int(user_id_str))
                except Exception:
                    return
            try:
                dm = await user.create_dm()
                msg = await dm.fetch_message(int(msg_id))
                await msg.edit(embed=embed, view=empty_view)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to edit alert DM user=%s request_id=%s: %s",
                    user_id_str, request_id, exc,
                )

        await self._gather_bounded(
            [_edit_one(user_id_str, msg_id) for user_id_str, msg_id in msg_id_map.items()],
            _ALERT_EDIT_CONCURRENCY,
        )

    # -------------------------------------------------------- 復原主流程

    async def _get_pre_attack_snapshots(
        self,
        store,
        guild_id: str,
        target_type: str,
        anchor_ts: int | None = None,
    ) -> list[dict[str, Any]]:
        """取得每個目標在攻擊前（至少 _RECOVERY_LOOKBACK 秒前）的最新快照。

        若存在手動 commit 基準快照（pinned=2），優先使用該批基準。
        若不存在 5 分鐘前的資料，退回使用最早的快照（防止伺服器剛建立就遭攻擊）。
        """
        # 攻擊前安全截止點：anchor 往前推 _RECOVERY_LOOKBACK，避免撈到攻擊期間寫入的快照。
        if anchor_ts is not None:
            clean_before = anchor_ts - _RECOVERY_LOOKBACK
            rows = await store.fetchall(
                """
                SELECT s1.target_id, s1.snapshot_data, s1.timestamp
                FROM structure_snapshots s1
                INNER JOIN (
                    SELECT target_id, MAX(timestamp) AS max_ts
                    FROM structure_snapshots
                    WHERE guild_id = ? AND target_type = ? AND timestamp <= ?
                    GROUP BY target_id
                ) s2 ON s1.target_id = s2.target_id AND s1.timestamp = s2.max_ts
                WHERE s1.guild_id = ? AND s1.target_type = ?
                """,
                [guild_id, target_type, clean_before, guild_id, target_type],
            )
            logger.info(
                "Snapshot query guild=%s type=%s anchor_ts=%s clean_before=%s rows=%s",
                guild_id, target_type, anchor_ts, clean_before, len(rows),
            )
        else:
            rows = await store.fetchall(
                """
                SELECT s1.target_id, s1.snapshot_data, s1.timestamp
                FROM structure_snapshots s1
                INNER JOIN (
                    SELECT target_id, MAX(timestamp) AS max_ts
                    FROM structure_snapshots
                    WHERE guild_id = ? AND target_type = ?
                      AND timestamp <= (strftime('%s', 'now') - ?)
                    GROUP BY target_id
                ) s2 ON s1.target_id = s2.target_id AND s1.timestamp = s2.max_ts
                WHERE s1.guild_id = ? AND s1.target_type = ?
                """,
                [guild_id, target_type, _RECOVERY_LOOKBACK, guild_id, target_type],
            )
        if rows:
            return rows

        # 有攻擊錨點時，優先使用 temp_cache 的 old_data，避免被污染快照。
        if anchor_ts is not None:
            clean_before = anchor_ts - _RECOVERY_LOOKBACK
            from_cache = await self._get_old_state_fallback_from_temp_cache(
                store,
                guild_id,
                target_type,
                clean_before,
            )
            if from_cache:
                logger.warning(
                    "No snapshot rows before anchor, using temp_cache old_data guild=%s type=%s anchor_ts=%s clean_before=%s rows=%s",
                    guild_id,
                    target_type,
                    anchor_ts,
                    clean_before,
                    len(from_cache),
                )
                return from_cache

            # 降級回退：若沒有「5 分鐘前」資料，改用「攻擊發生前」最近快照。
            relaxed_rows = await store.fetchall(
                """
                SELECT s1.target_id, s1.snapshot_data, s1.timestamp
                FROM structure_snapshots s1
                INNER JOIN (
                    SELECT target_id, MAX(timestamp) AS max_ts
                    FROM structure_snapshots
                    WHERE guild_id = ? AND target_type = ? AND timestamp <= ?
                    GROUP BY target_id
                ) s2 ON s1.target_id = s2.target_id AND s1.timestamp = s2.max_ts
                WHERE s1.guild_id = ? AND s1.target_type = ?
                """,
                [guild_id, target_type, anchor_ts, guild_id, target_type],
            )
            if relaxed_rows:
                logger.warning(
                    "No clean-5min snapshots; using pre-anchor snapshots guild=%s type=%s anchor_ts=%s rows=%s",
                    guild_id,
                    target_type,
                    anchor_ts,
                    len(relaxed_rows),
                )
                return relaxed_rows

            # 最後回退：使用最早快照，避免完全無法復原。
            earliest_rows = await store.fetchall(
                """
                SELECT s1.target_id, s1.snapshot_data, s1.timestamp
                FROM structure_snapshots s1
                INNER JOIN (
                    SELECT target_id, MIN(timestamp) AS min_ts
                    FROM structure_snapshots
                    WHERE guild_id = ? AND target_type = ?
                    GROUP BY target_id
                ) s2 ON s1.target_id = s2.target_id AND s1.timestamp = s2.min_ts
                WHERE s1.guild_id = ? AND s1.target_type = ?
                """,
                [guild_id, target_type, guild_id, target_type],
            )
            if earliest_rows:
                logger.warning(
                    "No pre-anchor snapshots; using earliest snapshots guild=%s type=%s rows=%s",
                    guild_id,
                    target_type,
                    len(earliest_rows),
                )
                return earliest_rows

            logger.warning(
                "No clean pre-attack data found guild=%s type=%s anchor_ts=%s clean_before=%s",
                guild_id,
                target_type,
                anchor_ts,
                clean_before,
            )
            return []

        # 無錨點時，退回各目標最早的一筆（相容舊行為）。
        logger.warning(
            "No pre-attack snapshots found guild=%s type=%s — falling back to earliest",
            guild_id, target_type,
        )
        return await store.fetchall(
            """
            WITH ranked AS (
                SELECT target_id, snapshot_data, timestamp, id,
                       ROW_NUMBER() OVER (
                           PARTITION BY target_id
                           ORDER BY timestamp ASC, id ASC
                       ) AS rn
                FROM structure_snapshots
                WHERE guild_id = ? AND target_type = ?
            )
            SELECT target_id, snapshot_data, timestamp
            FROM ranked
            WHERE rn = 1
            """,
            [guild_id, target_type],
        )

    async def _get_old_state_fallback_from_temp_cache(
        self,
        store,
        guild_id: str,
        target_type: str,
        anchor_ts: int,
    ) -> list[dict[str, Any]]:
        """用 temp_cache.old_data 回補攻擊前狀態，避免還原到污染快照。"""
        event_map: dict[str, tuple[str, ...]] = {
            "channel": ("channel_delete", "channel_update"),
            "role": ("role_delete", "role_update"),
        }
        id_key_map = {
            "channel": "channel_id",
            "role": "role_id",
        }

        events = event_map.get(target_type)
        id_key = id_key_map.get(target_type)
        if not events or not id_key:
            return []

        placeholders = ",".join(["?"] * len(events))
        rows = await store.fetchall(
            f"""
            SELECT target_id, old_data, timestamp
            FROM temp_cache
            WHERE guild_id = ?
              AND event_type IN ({placeholders})
              AND old_data IS NOT NULL
              AND timestamp <= ?
            ORDER BY timestamp DESC
            """,
            [guild_id, *events, anchor_ts],
        )

        seen: set[str] = set()
        normalized: list[dict[str, Any]] = []
        for row in rows:
            raw = row.get("old_data")
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue

            entity_id = str(payload.get(id_key) or row.get("target_id") or "").strip()
            if not entity_id or entity_id in seen:
                continue

            payload[id_key] = entity_id
            seen.add(entity_id)
            normalized.append(
                {
                    "target_id": entity_id,
                    "snapshot_data": json.dumps(payload, ensure_ascii=False),
                    "timestamp": row.get("timestamp"),
                }
            )

        return normalized

    async def _get_recovery_anchor_timestamp(
        self,
        store,
        guild_id: str,
        request_id: str | None,
    ) -> int | None:
        """取得復原錨點時間，優先使用 recovery_requests.created_at。"""
        if request_id:
            rows = await store.fetchall(
                "SELECT created_at FROM recovery_requests WHERE id = ? AND guild_id = ? LIMIT 1",
                [request_id, guild_id],
            )
            if rows:
                ts = rows[0].get("created_at")
                if ts is not None:
                    try:
                        return int(ts)
                    except (TypeError, ValueError):
                        pass
        return None

    async def _get_latest_guild_snapshot(
        self,
        store,
        guild_id: str,
        anchor_ts: int | None = None,
    ) -> dict[str, Any] | None:
        """取得攻擊前最近的 guild 快照（名稱/縮圖/橫幅）。

        若存在手動 commit 基準快照（pinned=2），優先使用該快照。
        若不存在 5 分鐘前的資料，退回使用最早的快照。
        """
        if anchor_ts is not None:
            clean_before = anchor_ts - _RECOVERY_LOOKBACK
            rows = await store.fetchall(
                """
                SELECT snapshot_data
                FROM structure_snapshots
                WHERE guild_id = ? AND target_type = 'guild' AND target_id = ?
                  AND timestamp <= ?
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                [guild_id, guild_id, clean_before],
            )
        else:
            rows = await store.fetchall(
                """
                SELECT snapshot_data
                FROM structure_snapshots
                WHERE guild_id = ? AND target_type = 'guild' AND target_id = ?
                  AND timestamp <= (strftime('%s', 'now') - ?)
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                [guild_id, guild_id, _RECOVERY_LOOKBACK],
            )

        if not rows:
            if anchor_ts is not None:
                relaxed = await store.fetchall(
                    """
                    SELECT snapshot_data
                    FROM structure_snapshots
                    WHERE guild_id = ? AND target_type = 'guild' AND target_id = ?
                      AND timestamp <= ?
                    ORDER BY timestamp DESC
                    LIMIT 1
                    """,
                    [guild_id, guild_id, anchor_ts],
                )
                if relaxed:
                    rows = relaxed
                    logger.warning(
                        "No clean-5min guild snapshot; using pre-anchor snapshot guild=%s anchor_ts=%s",
                        guild_id,
                        anchor_ts,
                    )

            if not rows:
                logger.warning(
                    "No pre-attack guild snapshot found guild=%s — falling back to earliest",
                    guild_id,
                )
                rows = await store.fetchall(
                    """
                    SELECT snapshot_data
                    FROM structure_snapshots
                    WHERE guild_id = ? AND target_type = 'guild' AND target_id = ?
                    ORDER BY timestamp ASC
                    LIMIT 1
                    """,
                    [guild_id, guild_id],
                )
        if rows:
            try:
                return json.loads(rows[0]["snapshot_data"])
            except Exception:
                pass

        # 舊版資料可能把 after-state 寫進 structure_snapshots，
        # 這裡退回用 temp_cache 的 old_data（攻擊前狀態）。
        clean_before = (anchor_ts - _RECOVERY_LOOKBACK) if anchor_ts is not None else None
        fallback_rows = await store.fetchall(
            """
            SELECT old_data
            FROM temp_cache
            WHERE guild_id = ? AND event_type = 'guild_update'
              AND old_data IS NOT NULL
              AND (? IS NULL OR timestamp <= ?)
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            [guild_id, clean_before, clean_before],
        )
        if not fallback_rows:
            return None
        try:
            return json.loads(fallback_rows[0]["old_data"])
        except Exception:
            return None

    async def _fetch_asset_bytes(self, url: str | None) -> bytes | None:
        """下載快照中的圖片資源供 guild.edit 使用。"""
        if not url:
            return None
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, timeout=15) as resp:
                    if resp.status != 200:
                        return None
                    return await resp.read()
        except Exception:
            return None

    async def _restore_guild_profile(
        self, guild: discord.Guild, snapshot: dict[str, Any] | None
    ) -> None:
        """還原伺服器名稱、縮圖與橫幅。"""
        if not snapshot:
            logger.info("No guild profile snapshot found guild=%s", guild.id)
            return

        target_name = snapshot.get("name") or guild.name
        icon_url = snapshot.get("icon_url")
        banner_url = snapshot.get("banner_url")

        icon_bytes = await self._fetch_asset_bytes(icon_url) if icon_url else None
        banner_bytes = await self._fetch_asset_bytes(banner_url) if banner_url else None

        try:
            kwargs: dict[str, Any] = {"name": target_name}
            if icon_url is None:
                # 快照顯示原本沒有縮圖，清除目前縮圖。
                kwargs["icon"] = None
            elif icon_bytes is not None:
                kwargs["icon"] = icon_bytes
            if banner_url is None:
                # 快照顯示原本沒有橫幅，清除目前橫幅。
                kwargs["banner"] = None
            elif banner_bytes is not None:
                kwargs["banner"] = banner_bytes

            await rate_limited_call(guild.edit, limit_key="guild_edit", **kwargs)
            logger.info(
                "Guild profile restored guild=%s name=%s icon=%s banner=%s",
                guild.id,
                target_name,
                bool(icon_bytes),
                bool(banner_bytes),
            )
        except discord.Forbidden:
            logger.warning(
                "Skip guild profile restore due to missing permissions guild=%s",
                guild.id,
            )
        except discord.HTTPException as exc:
            logger.warning(
                "Guild profile restore failed guild=%s: %s",
                guild.id,
                exc,
                exc_info=True,
            )

    # -------------------------------------------------------- 頻道復原

    async def _restore_channels(
        self, guild: discord.Guild, channel_data_list: list[dict]
    ) -> int:
        """依快照重建或修正頻道設定。接受已解析的快照 dict 清單。"""
        restored = 0
        current = {str(ch.id): ch for ch in guild.channels}
        used_channel_ids: set[str] = set()
        category_id_map: dict[str, str] = {}
        channel_restore_map: dict[str, str] = {}

        logger.info(
            "Restoring channels guild=%s snapshots=%s current_channels=%s",
            guild.id,
            len(channel_data_list),
            len(current),
        )

        def _is_category_snapshot(data: dict) -> bool:
            try:
                return int(data.get("type", -1)) == int(discord.ChannelType.category.value)
            except (TypeError, ValueError):
                return False

        def _pick_fallback_channel(data: dict):
            """當快照 ID 已失效時，盡量重用現有同型同名頻道避免重建重複。"""
            ch_type = data.get("type")
            target_name = data.get("name")
            if target_name is None:
                return None

            for ch in guild.channels:
                ch_id = str(ch.id)
                if ch_id in used_channel_ids:
                    continue
                if ch.type.value != ch_type:
                    continue
                if ch.name != target_name:
                    continue
                return ch
            return None

        async def _restore_one_channel(data: dict) -> int:
            channel_id = data["channel_id"]
            existing = current.get(channel_id)
            if existing is None:
                existing = _pick_fallback_channel(data)
            if existing is not None:
                used_channel_ids.add(str(existing.id))
                channel_restore_map[channel_id] = str(existing.id)
                if _is_category_snapshot(data):
                    category_id_map[channel_id] = str(existing.id)

            try:
                if existing is None:
                    logger.info(
                        "Recreating missing channel guild=%s channel_id=%s name=%s",
                        guild.id,
                        channel_id,
                        data.get("name"),
                    )
                    created = await rate_limited_call(
                        self._recreate_channel,
                        guild,
                        data,
                        category_id_map,
                        limit_key="channel_ops",
                    )
                    if (
                        created is not None
                        and _is_category_snapshot(data)
                    ):
                        category_id_map[channel_id] = str(created.id)
                    if created is not None:
                        channel_restore_map[channel_id] = str(created.id)
                else:
                    logger.info(
                        "Updating existing channel guild=%s channel_id=%s name=%s",
                        guild.id,
                        channel_id,
                        data.get("name"),
                    )
                    await rate_limited_call(
                        self._update_channel,
                        guild,
                        existing,
                        data,
                        category_id_map,
                        limit_key="channel_ops",
                    )
                await asyncio.sleep(get_adaptive_delay("channel_ops", CHANNEL_OP_DELAY))
                return 1
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Restore channel failed guild=%s channel_id=%s name=%s: %s",
                    guild.id,
                    channel_id,
                    data.get("name"),
                    exc,
                    exc_info=True,
                )
                return 0

        # 兩階段還原：先完整還原類別，再還原子頻道，避免 parent 尚未建立。
        category_data = [
            d for d in channel_data_list
            if _is_category_snapshot(d)
        ]
        other_data = [
            d for d in channel_data_list
            if not _is_category_snapshot(d)
        ]

        category_results = await self._gather_bounded(
            [_restore_one_channel(data) for data in sorted(category_data, key=lambda d: d.get("position", 0))],
            _CHANNEL_RESTORE_CONCURRENCY,
        )

        # 保底：若仍有快照中的分類不存在，逐一補建，避免子頻道無法回掛。
        existing_categories_by_name = {
            ch.name: ch for ch in guild.channels if isinstance(ch, discord.CategoryChannel)
        }
        for data in sorted(category_data, key=lambda d: d.get("position", 0)):
            cat_id = str(data.get("channel_id", ""))
            mapped_id = category_id_map.get(cat_id)
            if mapped_id and guild.get_channel(int(mapped_id)) is not None:
                continue

            fallback = existing_categories_by_name.get(str(data.get("name", "")))
            if fallback is not None:
                category_id_map[cat_id] = str(fallback.id)
                continue

            try:
                created = await rate_limited_call(
                    self._recreate_channel,
                    guild,
                    data,
                    category_id_map,
                    limit_key="channel_ops",
                )
                if created is not None:
                    category_id_map[cat_id] = str(created.id)
                    existing_categories_by_name[created.name] = created
                    restored += 1
                    await asyncio.sleep(get_adaptive_delay("channel_ops", CHANNEL_OP_DELAY))
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Fallback create category failed guild=%s category_id=%s name=%s: %s",
                    guild.id,
                    cat_id,
                    data.get("name"),
                    exc,
                    exc_info=True,
                )

        # 類別可能剛被建立，重抓一次 current 提供下一階段 parent 查找。
        current = {str(ch.id): ch for ch in guild.channels}

        other_results = await self._gather_bounded(
            [_restore_one_channel(data) for data in sorted(other_data, key=lambda d: d.get("position", 0))],
            _CHANNEL_RESTORE_CONCURRENCY,
        )

        async def _fix_one_parent(data: dict) -> None:
            channel_id = str(data.get("channel_id", "")).strip()
            if not channel_id:
                return
            parent_id = str(data.get("parent_id") or "").strip()
            if not parent_id:
                return

            mapped_channel_id = channel_restore_map.get(channel_id, channel_id)
            channel = guild.get_channel(int(mapped_channel_id))
            if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
                return

            mapped_parent_id = category_id_map.get(parent_id, parent_id)
            parent = guild.get_channel(int(mapped_parent_id))
            if not isinstance(parent, discord.CategoryChannel):
                return

            if channel.category_id == parent.id:
                return

            try:
                await rate_limited_call(
                    channel.edit,
                    category=parent,
                    limit_key="channel_ops",
                )
                logger.info(
                    "Reattached channel to category guild=%s channel=%s(%s) parent=%s(%s)",
                    guild.id,
                    channel.name,
                    channel.id,
                    parent.name,
                    parent.id,
                )
                await asyncio.sleep(get_adaptive_delay("channel_ops", CHANNEL_OP_DELAY))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to reattach channel category guild=%s channel_id=%s parent_id=%s: %s",
                    guild.id,
                    channel_id,
                    parent_id,
                    exc,
                )

        await self._gather_bounded(
            [_fix_one_parent(data) for data in other_data],
            _CHANNEL_RESTORE_CONCURRENCY,
        )

        self._last_channel_restore_map = dict(channel_restore_map)
        restored = sum(category_results) + sum(other_results)
        return restored

    async def _recreate_channel(
        self,
        guild: discord.Guild,
        data: dict,
        category_id_map: dict[str, str] | None = None,
    ):
        """依快照資料重建單一頻道。"""
        category_id_map = category_id_map or {}

        def _resolve_category(parent_id: str | None):
            if not parent_id:
                return None
            normalized_parent_id = str(parent_id).strip()
            mapped_parent_id = category_id_map.get(normalized_parent_id, normalized_parent_id)
            return guild.get_channel(int(mapped_parent_id))

        ch_type = discord.ChannelType(data["type"])
        overwrites = self._build_overwrites(
            guild, data.get("permission_overwrites", [])
        )
        category = _resolve_category(data.get("parent_id"))

        kwargs: dict[str, Any] = {
            "name": data["name"],
            "overwrites": overwrites,
            "position": data.get("position", 0),
        }
        if category:
            kwargs["category"] = category

        if ch_type == discord.ChannelType.text:
            kwargs["topic"] = data.get("topic")
            kwargs["nsfw"] = data.get("nsfw", False)
            kwargs["slowmode_delay"] = data.get("slowmode_delay", 0)
            created = await guild.create_text_channel(**kwargs)
        elif ch_type == discord.ChannelType.voice:
            created = await guild.create_voice_channel(**kwargs)
        elif ch_type == discord.ChannelType.category:
            created = await guild.create_category(**kwargs)
        else:
            created = await guild.create_text_channel(**kwargs)

        logger.info("Recreated channel '%s' in guild %s", data["name"], guild.id)
        return created

    async def _update_channel(
        self,
        guild: discord.Guild,
        channel,
        data: dict,
        category_id_map: dict[str, str] | None = None,
    ) -> None:
        """將既有頻道調整回快照設定。"""
        category_id_map = category_id_map or {}

        def _resolve_category(parent_id: str | None):
            if not parent_id:
                return None
            normalized_parent_id = str(parent_id).strip()
            mapped_parent_id = category_id_map.get(normalized_parent_id, normalized_parent_id)
            return guild.get_channel(int(mapped_parent_id))

        overwrites = self._build_overwrites(
            guild, data.get("permission_overwrites", [])
        )
        edit_kwargs: dict[str, Any] = {
            "name": data["name"],
            "position": data.get("position", 0),
            "overwrites": overwrites,
        }
        if not isinstance(channel, discord.CategoryChannel):
            parent_id = data.get("parent_id")
            edit_kwargs["category"] = _resolve_category(parent_id)
        if isinstance(channel, discord.TextChannel):
            edit_kwargs["topic"] = data.get("topic")
            edit_kwargs["nsfw"] = data.get("nsfw", False)
            edit_kwargs["slowmode_delay"] = data.get("slowmode_delay", 0)
        await channel.edit(**edit_kwargs)

    def _build_overwrites(
        self, guild: discord.Guild, ow_list: list[dict]
    ) -> dict:
        """將快照中的 allow/deny 權限還原成 Discord PermissionOverwrite。"""
        overwrites: dict = {}
        for ow in ow_list:
            target = (
                guild.get_role(int(ow["id"]))
                if ow["type"] == "role"
                else guild.get_member(int(ow["id"]))
            )
            if target:
                allow = discord.Permissions(int(ow["allow"]))
                deny = discord.Permissions(int(ow["deny"]))
                overwrites[target] = discord.PermissionOverwrite.from_pair(
                    allow, deny
                )
        return overwrites

    # -------------------------------------------------------- 身分組復原

    def _check_recovery_safety(
        self,
        guild: discord.Guild,
        channel_data_list: list[dict[str, Any]],
        role_data_list: list[dict[str, Any]],
    ) -> list[str]:
        """檢查是否具備足夠快照，避免空快照觸發破壞性刪除。"""
        issues: list[str] = []

        recoverable_channels = [ch for ch in guild.channels]
        recoverable_roles = [
            r for r in guild.roles
            if not r.is_default() and not r.managed
        ]

        if recoverable_channels and not channel_data_list:
            issues.append("channel snapshots empty")
        if recoverable_roles and not role_data_list:
            issues.append("role snapshots empty")

        return issues

    async def _execute_recovery(
        self, store, guild: discord.Guild, request_id: str | None = None
    ) -> tuple[int, int, int, list[dict]]:
        """完整還原流程（依序）：

        1. 從 D1 預載所有快照資料到記憶體（並嘗試寫入 Redis 熱快取）。
        2. 刪除快照中不存在的多餘身分組／頻道。
        3. 還原：伺服器名稱/橫幅 → 身分組屬性 → 身分組成員 → 頻道 → 訊息。
        4. 解除快照的保留標記（pinned）。
        """
        guild_id = str(guild.id)
        defense_temporarily_disabled = False

        try:
            defense_state = await get_defense_state(store, guild_id)
            if defense_state.get("enabled", True):
                await set_defense_disabled(
                    store,
                    guild_id,
                    updated_by=f"recovery:{request_id or 'manual'}",
                    duration_seconds=_RECOVERY_DEFENSE_PAUSE_SECONDS,
                )
                defense_temporarily_disabled = True
                logger.info(
                    "Defense temporarily disabled for recovery guild=%s request_id=%s seconds=%s",
                    guild_id,
                    request_id,
                    _RECOVERY_DEFENSE_PAUSE_SECONDS,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to disable defense during recovery guild=%s request_id=%s: %s",
                guild_id,
                request_id,
                exc,
                exc_info=True,
            )

        try:
            # ── Step 1: 預載快照資料到記憶體 ────────────────────────────────
            anchor_ts = await self._get_recovery_anchor_timestamp(store, guild_id, request_id)
            guild_snap, channel_snaps, role_snaps, member_snaps = await asyncio.gather(
                self._get_latest_guild_snapshot(store, guild_id, anchor_ts),
                self._get_pre_attack_snapshots(store, guild_id, "channel", anchor_ts),
                self._get_pre_attack_snapshots(store, guild_id, "role", anchor_ts),
                self._get_pre_attack_snapshots(store, guild_id, "member", anchor_ts),
            )

            def _load_snapshot_rows(rows: list[dict[str, Any]], id_key: str) -> list[dict[str, Any]]:
                loaded: list[dict[str, Any]] = []
                seen_ids: set[str] = set()
                for row in rows:
                    raw = row.get("snapshot_data")
                    if not raw:
                        continue
                    try:
                        payload = json.loads(raw)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "Skip invalid snapshot JSON guild=%s id_key=%s target=%s: %s",
                            guild_id,
                            id_key,
                            row.get("target_id"),
                            exc,
                        )
                        continue

                    if not isinstance(payload, dict):
                        continue
                    entity_id = str(payload.get(id_key) or row.get("target_id") or "").strip()
                    if not entity_id or entity_id in seen_ids:
                        continue
                    payload[id_key] = entity_id
                    seen_ids.add(entity_id)
                    loaded.append(payload)
                return loaded

            channel_data_list = _load_snapshot_rows(channel_snaps, "channel_id")
            role_data_list = _load_snapshot_rows(role_snaps, "role_id")
            member_data_list = _load_snapshot_rows(member_snaps, "user_id")

            # 嘗試寫入 Redis（如可用）；失敗時僅使用記憶體快取繼續執行。
            await self._cache_snapshots_to_redis(store, guild_id, request_id, channel_data_list, role_data_list)

            snapshot_channel_ids = {d["channel_id"] for d in channel_data_list}
            snapshot_role_ids = {d["role_id"] for d in role_data_list}

            logger.info(
                "Recovery snapshots loaded guild=%s request_id=%s anchor_ts=%s channels=%s roles=%s members=%s",
                guild_id,
                request_id,
                anchor_ts,
                len(channel_data_list),
                len(role_data_list),
                len(member_data_list),
            )

            safety_issues = self._check_recovery_safety(
                guild,
                channel_data_list,
                role_data_list,
            )
            if safety_issues:
                logger.error(
                    "Recovery safety abort guild=%s request_id=%s issues=%s",
                    guild_id,
                    request_id,
                    ", ".join(safety_issues),
                )
                raise RuntimeError(
                    "Recovery safety abort: clean pre-attack snapshots are insufficient"
                )

            # ── Step 2: 刪除快照中不存在的多餘身分組、頻道 ─────────────────
            await asyncio.gather(
                self._delete_extra_roles(guild, snapshot_role_ids),
                self._delete_extra_channels(guild, snapshot_channel_ids),
            )

            # ── Step 3: 依序還原 ─────────────────────────────────────────────
            # 3a. 伺服器名稱 / 橫幅
            await self._restore_guild_profile(guild, guild_snap)
            # 3b. 身分組屬性
            restored_ro, failed_roles = await self._restore_roles(guild, role_data_list)
            # 3c. 身分組成員 + 成員暱稱（彼此獨立，可並行縮短流程）
            await asyncio.gather(
                self._restore_role_members(guild, role_data_list),
                self._restore_member_nicks(guild, member_data_list),
            )
            # 3d. 頻道
            restored_ch = await self._restore_channels(guild, channel_data_list)
            # 3e. 訊息
            restored_ms = await self._restore_messages(store, guild)

            # ── Step 4: 解除快照保留標記 ─────────────────────────────────────
            await self._unpin_snapshots(store, guild_id)

            logger.info(
                "Recovery finished guild=%s request_id=%s channels=%d roles=%d messages=%d failed_roles=%d",
                guild_id, request_id, restored_ch, restored_ro, restored_ms, len(failed_roles),
            )
            return restored_ch, restored_ro, restored_ms, failed_roles
        finally:
            if defense_temporarily_disabled:
                try:
                    await set_defense_enabled(
                        store,
                        guild_id,
                        updated_by=f"recovery:{request_id or 'manual'}",
                    )
                    logger.info(
                        "Defense re-enabled after recovery guild=%s request_id=%s",
                        guild_id,
                        request_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Failed to re-enable defense after recovery guild=%s request_id=%s: %s",
                        guild_id,
                        request_id,
                        exc,
                        exc_info=True,
                    )

    # -------------------------------------------------------- 快取與輔助工具

    async def _cache_snapshots_to_redis(
        self,
        store,
        guild_id: str,
        request_id: str | None,
        channel_data_list: list[dict],
        role_data_list: list[dict],
    ) -> None:
        """嘗試將快照資料寫入 Redis 熱快取（TTL 1 小時）。

        若 Redis 不可用或寫入失敗，僅記錄警告並繼續使用記憶體快取。
        如資料量過大，分批寫入（每批最多 50 筆）。
        """
        redis = getattr(store, "_redis", None)
        if redis is None:
            logger.debug("Redis unavailable — using in-memory snapshots only")
            return
        prefix = f"recovery:{request_id or guild_id}"
        batch: list = []
        for d in channel_data_list:
            batch.append(f"{prefix}:channel:{d['channel_id']}")
            batch.append(json.dumps(d, ensure_ascii=False))
        for d in role_data_list:
            batch.append(f"{prefix}:role:{d['role_id']}")
            batch.append(json.dumps(d, ensure_ascii=False))
        # 分批 MSET，每次最多 50 個 key-value 對（100 個元素）。
        chunk_size = 100
        try:
            for i in range(0, len(batch), chunk_size):
                chunk = batch[i : i + chunk_size]
                pairs = {chunk[j]: chunk[j + 1] for j in range(0, len(chunk), 2)}
                await redis.mset(pairs)
            # 為每個 key 設定 TTL。
            pipe = redis.pipeline(transaction=False)
            for i in range(0, len(batch), 2):
                pipe.expire(batch[i], 3600)
            await pipe.execute()
            logger.info(
                "Cached %d snapshot(s) to Redis guild=%s request_id=%s",
                len(batch) // 2, guild_id, request_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Redis cache write failed guild=%s request_id=%s: %s — continuing with memory",
                guild_id, request_id, exc,
            )

    async def _delete_extra_channels(
        self, guild: discord.Guild, snapshot_channel_ids: set[str]
    ) -> None:
        """刪除在快照中不存在的多餘頻道（還原前清場）。"""
        if not snapshot_channel_ids:
            logger.error(
                "Skip delete extra channels due to empty snapshot set guild=%s",
                guild.id,
            )
            return

        # 先刪一般頻道再刪分類，避免殘留。
        non_categories = [
            ch for ch in guild.channels
            if str(ch.id) not in snapshot_channel_ids
            and not isinstance(ch, discord.CategoryChannel)
        ]
        categories = [
            ch for ch in guild.channels
            if str(ch.id) not in snapshot_channel_ids
            and isinstance(ch, discord.CategoryChannel)
        ]
        extra_channels = non_categories + categories

        async def _delete_channel(channel) -> None:
            try:
                await rate_limited_call(
                    channel.delete,
                    reason="Recovery: channel not in pre-attack snapshot",
                    limit_key="channel_ops",
                )
                logger.info(
                    "Deleted extra channel guild=%s channel=%s name=%s",
                    guild.id, channel.id, channel.name,
                )
                await asyncio.sleep(get_adaptive_delay("channel_ops", CHANNEL_OP_DELAY))
            except discord.Forbidden:
                logger.warning(
                    "Cannot delete extra channel (forbidden) guild=%s channel=%s",
                    guild.id, channel.id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Failed to delete extra channel guild=%s channel=%s: %s",
                    guild.id, channel.id, exc, exc_info=True,
                )

        await self._gather_bounded(
            [_delete_channel(channel) for channel in extra_channels],
            _CHANNEL_DELETE_CONCURRENCY,
        )

        # 補刪公開討論串（threads），避免「有一個頻道沒刪到」的殘留。
        async def _delete_thread(thread: discord.Thread) -> None:
            try:
                await rate_limited_call(
                    thread.delete,
                    reason="Recovery: thread not in pre-attack snapshot",
                    limit_key="channel_ops",
                )
                logger.info(
                    "Deleted extra thread guild=%s thread=%s name=%s",
                    guild.id, thread.id, thread.name,
                )
                await asyncio.sleep(get_adaptive_delay("channel_ops", CHANNEL_OP_DELAY))
            except discord.Forbidden:
                logger.warning(
                    "Cannot delete extra thread (forbidden) guild=%s thread=%s",
                    guild.id, thread.id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Failed to delete extra thread guild=%s thread=%s: %s",
                    guild.id, thread.id, exc, exc_info=True,
                )

        await self._gather_bounded(
            [_delete_thread(thread) for thread in list(guild.threads)],
            _CHANNEL_DELETE_CONCURRENCY,
        )

    async def _delete_extra_roles(
        self, guild: discord.Guild, snapshot_role_ids: set[str]
    ) -> None:
        """刪除在快照中不存在的多餘身分組（還原前清場）。"""
        if not snapshot_role_ids:
            logger.error(
                "Skip delete extra roles due to empty snapshot set guild=%s",
                guild.id,
            )
            return

        bot_member = guild.me
        targets = []
        for role in list(guild.roles):
            if role.is_default() or role.managed:
                continue
            if bot_member and role >= bot_member.top_role:
                continue
            if str(role.id) in snapshot_role_ids:
                continue
            targets.append(role)

        async def _delete_role(role: discord.Role) -> None:
            try:
                await rate_limited_call(
                    role.delete,
                    reason="Recovery: role not in pre-attack snapshot",
                    limit_key="role_ops",
                )
                logger.info(
                    "Deleted extra role guild=%s role=%s name=%s",
                    guild.id, role.id, role.name,
                )
                await asyncio.sleep(get_adaptive_delay("role_ops", ROLE_OP_DELAY))
            except discord.Forbidden:
                logger.warning(
                    "Cannot delete extra role (forbidden) guild=%s role=%s",
                    guild.id, role.id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Failed to delete extra role guild=%s role=%s: %s",
                    guild.id, role.id, exc, exc_info=True,
                )

        await self._gather_bounded(
            [_delete_role(role) for role in targets],
            _ROLE_DELETE_CONCURRENCY,
        )

    async def _restore_member_nicks(
        self,
        guild: discord.Guild,
        member_data_list: list[dict],
    ) -> None:
        """還原成員暱稱；找不到使用者（404/不在伺服器）時忽略。"""

        async def _restore_one(data: dict) -> None:
            uid = data.get("user_id")
            if not uid or not str(uid).isdigit():
                return
            member = guild.get_member(int(uid))
            if member is None:
                return
            target_nick = data.get("nick")
            if member.nick == target_nick:
                return
            try:
                await rate_limited_call(
                    member.edit,
                    nick=target_nick,
                    reason="Recovery: restore nickname",
                    limit_key="member_ops",
                )
                await asyncio.sleep(get_adaptive_delay("member_ops", ROLE_OP_DELAY))
            except discord.NotFound:
                # 404: 使用者已離開或找不到，忽略。
                return
            except discord.Forbidden:
                logger.warning(
                    "Cannot restore nick guild=%s user=%s",
                    guild.id,
                    uid,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Restore nick failed guild=%s user=%s: %s",
                    guild.id,
                    uid,
                    exc,
                )

        await self._gather_bounded(
            [_restore_one(data) for data in member_data_list],
            _ROLE_MEMBER_RESTORE_CONCURRENCY,
        )

    async def _restore_role_members(
        self, guild: discord.Guild, role_data_list: list[dict]
    ) -> None:
        """依快照將身分組補發給原有成員。

        快照中 members 欄位記錄了快照當時各身分組的成員 ID；
        此步驟確保成員在攻擊期間被移除的身分組能被恢復。
        """
        async def _restore_one(member: discord.Member, role: discord.Role, uid_str: str) -> None:
            try:
                await rate_limited_call(
                    member.add_roles,
                    role,
                    reason="Recovery: restoring role membership",
                    limit_key="role_ops",
                )
                await asyncio.sleep(get_adaptive_delay("role_ops", ROLE_OP_DELAY))
            except discord.Forbidden as exc:
                logger.warning(
                    "Cannot restore role (forbidden) guild=%s role=%s member=%s",
                    guild.id, role.id, uid_str,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Failed to restore role member guild=%s role=%s member=%s: %s",
                    guild.id, role.id, uid_str, exc, exc_info=True,
                )

        jobs = []
        for data in role_data_list:
            role_id = data["role_id"]
            member_ids: list[str] = data.get("members", [])
            if not member_ids:
                continue
            role = guild.get_role(int(role_id))
            if not role:
                continue
            for uid_str in member_ids:
                member = guild.get_member(int(uid_str))
                if not member:
                    continue
                if role in member.roles:
                    continue
                jobs.append(_restore_one(member, role, uid_str))

        await self._gather_bounded(jobs, _ROLE_MEMBER_RESTORE_CONCURRENCY)

    async def _unpin_snapshots(self, store, guild_id: str) -> None:
        """解除此 guild 所有 pinned 快照的保留標記。"""
        try:
            await store.execute(
                "UPDATE structure_snapshots SET pinned = 0 WHERE guild_id = ? AND pinned = 1",
                [guild_id],
            )
            logger.info("Unpinned snapshots guild=%s", guild_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to unpin snapshots guild=%s: %s", guild_id, exc, exc_info=True)

    async def _restore_roles(
        self, guild: discord.Guild, role_data_list: list[dict]
    ) -> tuple[int, list[dict]]:
        """依快照重建或修正身分組設定。接受已解析的快照 dict 清單。
        
        回傳 (restored_count, failed_roles)，其中 failed_roles 包含無法復原的身分組資訊。
        """
        restored = 0
        current = {str(r.id): r for r in guild.roles}
        bot_member = guild.me
        failed_roles = []  # Track roles that failed to restore

        logger.info(
            "Restoring roles guild=%s snapshots=%s current_roles=%s",
            guild.id,
            len(role_data_list),
            len(current),
        )

        async def _restore_one_role(data: dict) -> tuple[int, dict | None]:
            role_id = data["role_id"]
            existing = current.get(role_id)

            try:
                if existing is None:
                    logger.info(
                        "Recreating missing role guild=%s role_id=%s name=%s",
                        guild.id,
                        role_id,
                        data.get("name"),
                    )
                    await rate_limited_call(
                        guild.create_role,
                        name=data["name"],
                        permissions=discord.Permissions(int(data["permissions"])),
                        color=discord.Color(data["color"]),
                        hoist=data["hoist"],
                        mentionable=data["mentionable"],
                        limit_key="role_ops",
                    )
                    logger.info(
                        "Recreated role '%s' in guild %s", data["name"], guild.id
                    )
                    return (1, None)
                else:
                    if existing.is_default():
                        logger.info(
                            "Skipping default role during recovery guild=%s role_id=%s",
                            guild.id,
                            role_id,
                        )
                        return (0, None)
                    if bot_member is not None and existing >= bot_member.top_role:
                        logger.warning(
                            "Cannot restore role (hierarchy) guild=%s role_id=%s role=%s bot_top_role=%s",
                            guild.id,
                            role_id,
                            existing.name,
                            bot_member.top_role.name,
                        )
                        # Record failed role with hierarchy reason
                        perms = discord.Permissions(int(data.get("permissions", 0)))
                        return (0, {
                            "role_id": role_id,
                            "role_name": existing.name,
                            "reason": "hierarchy",  # Role is above bot
                            "permissions": data.get("permissions"),
                            "permission_names": [p[0] for p in perms],
                        })
                    logger.info(
                        "Updating existing role guild=%s role_id=%s name=%s",
                        guild.id,
                        role_id,
                        data.get("name"),
                    )
                    await rate_limited_call(
                        existing.edit,
                        name=data["name"],
                        permissions=discord.Permissions(int(data["permissions"])),
                        color=discord.Color(data["color"]),
                        hoist=data["hoist"],
                        mentionable=data["mentionable"],
                        limit_key="role_ops",
                    )
                    await asyncio.sleep(get_adaptive_delay("role_ops", ROLE_OP_DELAY))
                    return (1, None)
            except discord.Forbidden as exc:
                logger.warning(
                    "Cannot restore role (forbidden) guild=%s role_id=%s name=%s: %s",
                    guild.id,
                    role_id,
                    data.get("name"),
                    exc,
                    exc_info=True,
                )
                # Record failed role with forbidden reason
                perms = discord.Permissions(int(data.get("permissions", 0)))
                return (0, {
                    "role_id": role_id,
                    "role_name": data.get("name"),
                    "reason": "forbidden",
                    "permissions": data.get("permissions"),
                    "permission_names": [p[0] for p in perms],
                })
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Restore role failed guild=%s role_id=%s name=%s: %s",
                    guild.id,
                    role_id,
                    data.get("name"),
                    exc,
                    exc_info=True,
                )
                perms = discord.Permissions(int(data.get("permissions", 0)))
                return (0, {
                    "role_id": role_id,
                    "role_name": data.get("name"),
                    "reason": "error",
                    "permissions": data.get("permissions"),
                    "permission_names": [p[0] for p in perms],
                })

        results = await self._gather_bounded(
            [_restore_one_role(data) for data in role_data_list],
            _ROLE_RESTORE_CONCURRENCY,
        )
        for count, failed_role in results:
            restored += count
            if failed_role:
                failed_roles.append(failed_role)
        
        return (restored, failed_roles)

    # -------------------------------------------------------- 訊息復原

    async def _restore_messages(
        self, store, guild: discord.Guild
    ) -> int:
        """將舊頻道加密訊息重送到新頻道，附還原時間標記。"""
        guild_id = str(guild.id)

        def _build_restored_message(msg: dict[str, Any]) -> str:
            raw_content = decrypt(msg["encrypted_content"], msg["nonce"])
            safe_content = discord.utils.escape_mentions(raw_content or "").replace("\x00", "")

            attachment_names: list[str] = []
            raw_names = msg.get("attachment_names")
            if raw_names:
                try:
                    parsed = json.loads(raw_names)
                    if isinstance(parsed, list):
                        attachment_names = [str(n).replace("\n", " ").replace("\r", " ")[:120] for n in parsed]
                except Exception:
                    attachment_names = []

            lines: list[str] = []
            if safe_content.strip():
                lines.append(safe_content)
            for name in attachment_names:
                lines.append(f"[{name}] {_FILE_RESTORE_UNSUPPORTED_TEXT}")

            if not lines:
                lines.append("[空白訊息]")

            body = "\n".join(lines)
            if len(body) > 1700:
                body = body[:1700] + "\n...(內容過長已截斷)"

            ts = msg["timestamp"]
            return f"{body}\n\n*(由系統於 <t:{ts}:f> 還原)*"

        deleted = await store.fetchall(
            """
            SELECT DISTINCT target_id, old_data FROM temp_cache
            WHERE guild_id = ? AND event_type = 'channel_delete'
              AND timestamp >= (strftime('%s', 'now') - ?)
            """,
            [guild_id, _RECOVERY_LOOKBACK],
        )
        if not deleted:
            logger.info("No deleted channels found for message restore guild=%s", guild_id)
            return 0

        logger.info(
            "Restoring messages guild=%s deleted_channels=%s",
            guild_id,
            len(deleted),
        )

        async def _restore_one_deleted_channel(event: dict[str, Any]) -> int:
            old_ch_id = event["target_id"]
            ch_data = json.loads(event["old_data"]) if event["old_data"] else {}
            ch_name = ch_data.get("name", "unknown")

            ch_type = ch_data.get("type")
            if ch_type != discord.ChannelType.text.value:
                logger.debug(
                    "Skip message restore for non-text channel guild=%s old_channel=%s type=%s name=%s",
                    guild_id,
                    old_ch_id,
                    ch_type,
                    ch_name,
                )
                return 0

            mapped_new_id = self._last_channel_restore_map.get(str(old_ch_id))
            new_channel = None
            if mapped_new_id:
                mapped = guild.get_channel(int(mapped_new_id))
                if isinstance(mapped, discord.TextChannel):
                    new_channel = mapped
            if new_channel is None:
                new_channel = discord.utils.get(guild.text_channels, name=ch_name)
            if new_channel is None:
                logger.warning(
                    "No recreated text channel for message restore guild=%s old_channel=%s name=%s",
                    guild_id,
                    old_ch_id,
                    ch_name,
                )
                return 0

            messages = await store.fetchall(
                """
                SELECT * FROM encrypted_messages
                WHERE channel_id = ? AND guild_id = ?
                ORDER BY timestamp ASC
                """,
                [old_ch_id, guild_id],
            )
            if not messages:
                logger.info(
                    "No stored messages to restore guild=%s old_channel=%s name=%s",
                    guild_id,
                    old_ch_id,
                    ch_name,
                )
                return 0

            messages = messages[-_MAX_RESTORE_MESSAGES:]
            logger.info(
                "Preparing message restore guild=%s old_channel=%s new_channel=%s messages=%s",
                guild_id,
                old_ch_id,
                new_channel.id,
                len(messages),
            )

            webhook = await self._get_or_create_webhook(new_channel)
            if webhook is None:
                return 0

            channel_restored = 0
            for msg in messages:
                try:
                    await rate_limited_call(
                        webhook.send,
                        content=_build_restored_message(msg),
                        username=msg["author_name"],
                        avatar_url=msg.get("author_avatar"),
                        allowed_mentions=discord.AllowedMentions.none(),
                        limit_key="webhook_send",
                    )
                    channel_restored += 1
                    await asyncio.sleep(get_adaptive_delay("webhook_send", _WEBHOOK_SEND_DELAY))
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "Restore message failed guild=%s channel=%s message=%s author=%s: %s",
                        guild_id,
                        new_channel.id,
                        msg["message_id"],
                        msg["author_id"],
                        exc,
                        exc_info=True,
                    )
            return channel_restored

        results = await self._gather_bounded(
            [_restore_one_deleted_channel(event) for event in deleted],
            _MESSAGE_RESTORE_CHANNEL_CONCURRENCY,
        )
        return sum(results)

    async def _get_or_create_webhook(
        self, channel: discord.TextChannel
    ) -> discord.Webhook | None:
        """取得既有復原 webhook；不存在時建立新 webhook。"""
        try:
            webhooks = await channel.webhooks()
            webhook = next(
                (w for w in webhooks if w.name == _WEBHOOK_NAME), None
            )
            if webhook is None:
                webhook = await channel.create_webhook(name=_WEBHOOK_NAME)
            return webhook
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Webhook setup failed guild=%s channel=%s name=%s: %s",
                channel.guild.id,
                channel.id,
                channel.name,
                exc,
                exc_info=True,
            )
            return None


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RecoveryCog(bot))

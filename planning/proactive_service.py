"""活动驱动的主动行为服务（来源：自主规划插件 services/proactive_service.py，移植版）。

三个职责合并在一个 60s 轮询的后台循环里：

1. **活动切换主动发起** —— 活动开始后，在触发窗口内让 bot 主动开口
   （``ctx.maisaka.proactive.trigger``）。不覆盖当天睡醒后的第一个活动
   （由「早间问好」负责），设定的睡眠时段内不触发；每个
   ``(stream_id, activity_name, date)`` 组合只触发一次。

2. **早间问好** —— bot 睡醒后第一个活动开始时，向白名单会话道早安；
   可选「需要激活」：``ctx.message.get_by_time_in_chat`` 判定睡醒后有无消息，
   截止第一个活动结束前 10 分钟仍无人说话则放弃。

3. **按 goal_type 调节聊天频率** —— ``ctx.frequency.set_adjust`` 推给 heartflow。

对整合契约的适配：
- 触发走 ``ctx.maisaka.proactive.trigger``（目标插件声明的能力名）；
- 群/会话白名单解析失败时跳过该条目（宿主能力缺失不炸循环）；
- 决策写入 plugin._decision_logger（core/diag）。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from .config import PlanningProactiveSectionConfig, ScheduleSectionConfig, section_or_default
from .goal_manager import get_goal_manager
from .time_utils import TimezoneManager, parse_time_window

if TYPE_CHECKING:
    from typing import Any as _Plugin

logger = logging.getLogger(__name__)


# goal_type → 频率因子映射（1.0 = 主程序默认；<1 减少回复；>1 增加回复）
_FREQUENCY_FACTOR_BY_GOAL_TYPE: Dict[str, float] = {
    "study": 0.3,           # 学习：低频，不打扰
    "work": 0.3,            # 工作：低频
    "learn_topic": 0.5,     # 学习兴趣话题：偏低
    "sleep": 0.05,          # 睡觉：几乎不说话
    "exercise": 0.7,        # 运动：偏低
    "meal": 1.2,            # 吃饭：略高于平时
    "rest": 1.3,            # 休息：高频
    "daily_routine": 1.0,   # 日常作息：默认
    "entertainment": 1.5,   # 娱乐：高频
    "social_maintenance": 1.5,  # 社交：最高
}

# 主动发起的 intent 模板：按 goal_type 提供给 maisaka 当作主动开口的提示
_PROACTIVE_INTENT_TEMPLATES: Dict[str, str] = {
    "study": "Bot 刚开始学习/写代码，可以自然地在群里说一两句开始投入工作的话",
    "work": "Bot 刚开始工作，可以自然地说一两句进入状态的话",
    "meal": "Bot 刚开始吃饭，可以自然地在群里说一两句关于这顿饭的话",
    "rest": "Bot 刚开始休息，可以自然地说一两句放松/闲下来的话",
    "exercise": "Bot 刚开始运动，可以自然地在群里吼一两句锻炼的话",
    "entertainment": "Bot 刚开始娱乐/看剧/打游戏，可以自然地说一两句兴奋/期待的话",
    "social_maintenance": "Bot 现在心情比较social，可以主动找群友聊聊天",
    "daily_routine": "Bot 刚切换到日常活动，可以自然地说一句近况",
    "learn_topic": "Bot 开始钻研感兴趣的话题，可以自然地分享一点新发现",
}
_DEFAULT_PROACTIVE_INTENT = "Bot 切换到新活动了，可以自然地在群里说一两句近况"

# 早间问好 intent 模板
_MORNING_GREETING_INTENT = (
    "Bot 刚睡醒，新的一天开始了，请自然地向大家道一声早安；"
    "可以顺口提一句今天的第一个安排（{activity_name}），语气符合人设，简短随意"
)

# 约定（pending_commitment）来源的活动用强指令模板，避免陷入工具调用循环
_COMMITMENT_INTENT_TEMPLATE = (
    "你之前答应过对方今天{time_hint}要在私聊里{title}（约定备注：{notes_hint}）。\n"
    "现在到点了，请用符合人设的语气**立即在私聊里发出**这条消息（例如叫醒、问候、"
    "提醒等），不要询问对方、不要等用户先开口、不要陷入工具调用循环。\n"
    "活动名：{activity_name}"
)

# 早间问好观察截止：第一个活动结束前 N 分钟
_MORNING_DEADLINE_MARGIN_MINUTES = 10

# 作息解析默认值：07:00 起床 / 23:00 入睡
_DEFAULT_WAKE_MINUTES = 7 * 60
_DEFAULT_SLEEP_MINUTES = 23 * 60


def _parse_hhmm_minutes(value: Any, default: int) -> int:
    """把 HH:MM 解析为当天分钟数；非法/留空回退默认值。"""
    try:
        parts = str(value or "").strip().split(":")
        hour, minute = int(parts[0]), int(parts[1])
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour * 60 + minute
    except (ValueError, IndexError, TypeError):
        pass
    return default


class ProactiveService:
    """活动驱动的主动行为服务（60s 轮询循环）。"""

    LOOP_INTERVAL_SECONDS = 60.0
    STARTUP_DELAY_SECONDS = 15.0
    RESOLVE_CACHE_TTL_SUCCESS = 600.0  # 解析成功缓存 10 分钟
    RESOLVE_CACHE_TTL_FAILURE = 60.0   # 解析失败缓存 60 秒（允许恢复后重试）
    RANDOM_DELAY_MIN_SECONDS = 30.0    # 独立随机延迟下限，避免活动刚切换就瞬间开口

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._stop_event: asyncio.Event = asyncio.Event()
        self._tz_manager: TimezoneManager = TimezoneManager(
            section_or_default(plugin, "schedule", ScheduleSectionConfig).timezone
        )
        # 已触发的主动发起记录：{(stream_id, activity_name, date_str): trigger_time}
        self._proactive_history: Dict[Tuple[str, str, str], float] = {}
        # 待触发的主动发起：{(stream_id, activity_name, date_str): fire_at_epoch}
        self._pending_triggers: Dict[Tuple[str, str, str], float] = {}
        # 早间问好状态：{(stream_id, date_str): "fired" / "abandoned"}
        self._morning_history: Dict[Tuple[str, str], str] = {}
        # 早间问好待触发：{(stream_id, date_str): fire_at_epoch}
        self._morning_pending: Dict[Tuple[str, str], float] = {}
        # 已应用的频率因子缓存：{stream_id: factor}（避免重复 set_adjust）
        self._current_factor: Dict[str, float] = {}
        # 白名单解析缓存：{raw_entry: (session_id 或 None, expire_at)}
        self._resolved_cache: Dict[str, Tuple[Optional[str], float]] = {}
        # 活动切换判定（M0 事件）：上一次观测到的活动 (name, goal_type, start_minutes)
        self._last_activity_key: Optional[Tuple[str, str, int]] = None
        self._last_activity_info: Optional[Tuple[str, str, int, int, Dict[str, Any]]] = None

    # ------------------------------------------------------------
    # 循环骨架
    # ------------------------------------------------------------

    def _module_enabled(self) -> bool:
        """模块独立总开关：config.plugin.enabled 且 config.schedule.enabled。"""
        plugin_section = getattr(self._plugin.config, "plugin", None)
        cfg = getattr(self._plugin.config, "schedule", None)
        return bool(getattr(plugin_section, "enabled", True)) and bool(getattr(cfg, "enabled", True))

    async def run_loop(self) -> None:
        """后台循环：60 秒检查一次活动状态，处理触发 / 频率调控 / 早间问好。"""
        logger.info("日程主动行为循环已启动")
        try:
            await asyncio.sleep(self.STARTUP_DELAY_SECONDS)
            while not self._stop_event.is_set():
                try:
                    if self._module_enabled():
                        await self._check_and_act()
                except Exception as exc:
                    logger.error(f"主动检查异常: {exc}", exc_info=True)
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.LOOP_INTERVAL_SECONDS,
                    )
                    break
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            logger.debug("主动行为循环被取消")
            raise
        finally:
            logger.info("日程主动行为循环已退出")

    async def stop(self) -> None:
        """通知循环停止。"""
        self._stop_event.set()

    # ------------------------------------------------------------
    # 配置与日志辅助
    # ------------------------------------------------------------

    def _proactive_cfg(self) -> PlanningProactiveSectionConfig:
        return section_or_default(self._plugin, "planning_proactive", PlanningProactiveSectionConfig)

    def _decision_log(self, action: str, trigger_type: str = "", reason: str = "", **fields: Any) -> None:
        """决策日志（走 plugin._decision_logger，缺失时静默）。"""
        decision_logger = getattr(self._plugin, "_decision_logger", None)
        if decision_logger is not None:
            try:
                decision_logger.record(action=action, trigger_type=trigger_type, reason=reason, **fields)
            except Exception:
                pass

    # ------------------------------------------------------------
    # 白名单解析（群聊 + 其他会话，取并集）
    # ------------------------------------------------------------

    async def _resolve_target_streams(self) -> List[str]:
        """解析主动行为生效的会话列表（群聊白名单 ∪ 其他会话白名单）。

        - 群聊：``proactive_group_ids`` 直接填群号；**留空 = 所有群聊生效**；
        - 其他会话：``proactive_other_streams`` 支持 ``session:<id>`` / ``qq:private:<uid>``。
        """
        cfg = self._proactive_cfg()
        streams: List[str] = []
        seen: set = set()

        group_ids = [str(g).strip() for g in (cfg.proactive_group_ids or []) if str(g).strip()]
        if group_ids:
            for gid in group_ids:
                sid = await self._resolve_group_id(gid)
                if sid and sid not in seen:
                    streams.append(sid)
                    seen.add(sid)
        else:
            # 留空 = 所有群聊生效
            for sid in await self._enumerate_group_streams():
                if sid not in seen:
                    streams.append(sid)
                    seen.add(sid)

        for sid in await self._resolve_proactive_streams(list(cfg.proactive_other_streams or [])):
            if sid not in seen:
                streams.append(sid)
                seen.add(sid)

        return streams

    async def _resolve_group_id(self, group_id: str) -> Optional[str]:
        """把群号解析为 session_id（带解析缓存）。"""
        entry = f"group:{group_id}"
        now = time.time()
        cached = self._resolved_cache.get(entry)
        if cached is not None and now < cached[1]:
            return cached[0]

        ctx_obj = getattr(self._plugin, "ctx", None)
        session_id: Optional[str] = None
        if ctx_obj is None or not hasattr(ctx_obj, "chat"):
            logger.warning("无法解析群 %s：ctx.chat 能力不可用", group_id)
        else:
            result = None
            try:
                result = await ctx_obj.chat.get_stream_by_group_id(group_id, "qq")
            except Exception:
                try:  # 降级：枚举全部聊天流按 group_id 匹配
                    streams_raw = await ctx_obj.chat.get_all_streams(platform="qq")
                    if isinstance(streams_raw, list):
                        for s in streams_raw:
                            if isinstance(s, dict) and str(s.get("group_id") or "") == group_id:
                                result = str(s.get("session_id") or s.get("stream_id") or "")
                                break
                except Exception as exc:
                    logger.warning(f"解析群 {group_id} 异常: {exc}")
            session_id = self._extract_session_id(entry, result)

        ttl = self.RESOLVE_CACHE_TTL_SUCCESS if session_id else self.RESOLVE_CACHE_TTL_FAILURE
        self._resolved_cache[entry] = (session_id, now + ttl)
        if session_id:
            logger.info(f"解析群 {group_id} → session_id={session_id}")
        return session_id

    async def _enumerate_group_streams(self) -> List[str]:
        """枚举所有群聊的 session_id（群白名单留空时使用）。"""
        ctx_obj = getattr(self._plugin, "ctx", None)
        if ctx_obj is None or not hasattr(ctx_obj, "chat"):
            return []
        try:
            streams_raw = await ctx_obj.chat.get_all_streams(platform="qq")
        except Exception as exc:
            logger.debug(f"获取聊天流列表失败: {exc}")
            return []
        if isinstance(streams_raw, dict):
            streams_raw = streams_raw.get("streams") or streams_raw.get("data") or []
        if not isinstance(streams_raw, list):
            return []

        result: List[str] = []
        for stream in streams_raw:
            if not isinstance(stream, dict):
                continue
            group_id = str(stream.get("group_id") or "").strip()
            session_id = str(stream.get("session_id") or stream.get("stream_id") or "").strip()
            if group_id and session_id:
                result.append(session_id)
        return result

    @staticmethod
    def _extract_session_id(entry: str, result: Any) -> Optional[str]:
        """从 chat 能力返回值中提取 session_id（遵守 SDK 解包顺序）。"""
        if isinstance(result, dict) and result.get("success") is False:
            logger.warning(f"解析 {entry} 失败: {result.get('error', '主程序拒绝')}")
            return None
        if result is None:
            logger.warning(f"解析 {entry} 失败：主程序未找到对应聊天流")
            return None
        if isinstance(result, dict):
            session_id = str(result.get("session_id") or result.get("stream_id") or "").strip()
            if session_id:
                return session_id
        if isinstance(result, str) and result.strip():
            return result.strip()
        logger.warning(f"解析 {entry} 失败：返回无 session_id 字段")
        return None

    async def _resolve_proactive_streams(self, raw_entries: List[str]) -> List[str]:
        """解析其他会话白名单（session:<id> / qq:private:<uid> / 裸 id）。"""
        resolved: List[str] = []
        seen: set = set()
        now = time.time()
        ctx_obj = getattr(self._plugin, "ctx", None)

        for raw in raw_entries:
            entry = str(raw).strip()
            if not entry:
                continue

            cached = self._resolved_cache.get(entry)
            if cached is not None and now < cached[1]:
                resolved_sid = cached[0]
                if resolved_sid and resolved_sid not in seen:
                    resolved.append(resolved_sid)
                    seen.add(resolved_sid)
                continue

            session_id: Optional[str] = None
            if entry.startswith("session:"):
                session_id = entry[len("session:"):].strip() or None
            elif ":group:" in entry:
                session_id = await self._resolve_via_chat_capability(entry, kind="group", ctx_obj=ctx_obj)
            elif ":private:" in entry:
                session_id = await self._resolve_via_chat_capability(entry, kind="private", ctx_obj=ctx_obj)
            else:
                session_id = entry

            ttl = self.RESOLVE_CACHE_TTL_SUCCESS if session_id else self.RESOLVE_CACHE_TTL_FAILURE
            self._resolved_cache[entry] = (session_id, now + ttl)

            if session_id and session_id not in seen:
                resolved.append(session_id)
                seen.add(session_id)

        return resolved

    async def _resolve_via_chat_capability(
        self,
        entry: str,
        kind: str,
        ctx_obj: Any,
    ) -> Optional[str]:
        """通过 ``ctx.chat`` 把 ``qq:group:<gid>`` / ``qq:private:<uid>`` 解析为 session_id。"""
        if ctx_obj is None or not hasattr(ctx_obj, "chat"):
            logger.warning(f"无法解析 {entry}：ctx.chat 能力不可用")
            return None

        parts = entry.split(":", 2)
        if len(parts) != 3:
            logger.warning(f"配置条目格式错误：{entry}（应为 <platform>:{kind}:<id>）")
            return None
        platform, _, target_id = parts
        platform = platform.strip() or "qq"
        target_id = target_id.strip()
        if not target_id:
            logger.warning(f"配置条目缺失 ID：{entry}")
            return None

        try:
            if kind == "group":
                result = await ctx_obj.chat.get_stream_by_group_id(target_id, platform)
            else:
                result = await ctx_obj.chat.get_stream_by_user_id(target_id)
        except Exception as exc:
            logger.warning(f"解析 {entry} 异常: {exc}")
            return None

        session_id = self._extract_session_id(entry, result)
        if session_id:
            logger.info(f"解析成功 {entry} → session_id={session_id}")
        return session_id

    # ------------------------------------------------------------
    # 主循环逻辑
    # ------------------------------------------------------------

    async def _check_and_act(self) -> None:
        """读当前活动 → 安排/落实 主动发起、早间问好、频率调控。"""
        cfg_p = self._proactive_cfg()
        if not (
            cfg_p.enable_proactive_trigger
            or cfg_p.enable_frequency_modulation
            or cfg_p.enable_morning_greeting
        ):
            return

        streams = await self._resolve_target_streams()
        now = self._tz_manager.get_now()
        now_ts = time.time()
        current_min = now.hour * 60 + now.minute
        date_str = now.strftime("%Y-%m-%d")
        in_sleep = self._in_sleep_window(current_min)

        activity_info = self._find_current_activity()
        first_activity = self._find_first_activity_today()
        # M0 事件：活动切换判定处 → schedule.activity_ended / activity_started（附录 A ★）
        await self._publish_activity_transition(activity_info)

        # 1. 频率调控（每个 stream 独立，因子不变则跳过）
        if cfg_p.enable_frequency_modulation and activity_info is not None:
            _, goal_type, _, _, _ = activity_info
            await self._apply_frequency(streams, goal_type)

        # 睡眠时段：不安排新的主动发起；已排期的到点后也会在落实阶段被放弃
        if not in_sleep:
            # 2. 活动切换主动发起：安排独立随机延迟（不覆盖当天第一个活动）
            if cfg_p.enable_proactive_trigger and activity_info is not None:
                activity_name, _goal_type, start_minutes, _end, _params = activity_info
                is_first_activity = (
                    first_activity is not None
                    and first_activity[0] == activity_name
                    and first_activity[1] == start_minutes
                )
                elapsed = current_min - start_minutes
                if elapsed < 0:
                    elapsed += 24 * 60  # 跨午夜延续：按真实经过时间计算
                if elapsed * 60 <= self._fresh_window_seconds() and not is_first_activity:
                    for stream_id in streams:
                        key = (stream_id, activity_name, date_str)
                        if key in self._proactive_history or key in self._pending_triggers:
                            continue
                        self._pending_triggers[key] = now_ts + self._random_delay_seconds()
                        logger.info(
                            f"安排主动发起: stream={stream_id}, activity={activity_name}, "
                            f"延迟 {int(self._pending_triggers[key] - now_ts)}s"
                        )

            # 3. 早间问好：仅当天第一个活动进行中时安排
            if cfg_p.enable_morning_greeting and activity_info is not None and first_activity is not None:
                activity_name, _goal_type, start_minutes, _end, _params = activity_info
                if activity_name == first_activity[0] and start_minutes == first_activity[1]:
                    for stream_id in streams:
                        mkey = (stream_id, date_str)
                        if mkey in self._morning_history or mkey in self._morning_pending:
                            continue
                        self._morning_pending[mkey] = now_ts + self._random_delay_seconds()
                        logger.info(f"安排早间问好: stream={stream_id}")

        # 4. 清理跨天的残留排期
        for key in [k for k in self._pending_triggers if k[2] != date_str]:
            self._pending_triggers.pop(key, None)
        for mkey in [k for k in self._morning_pending if k[1] != date_str]:
            self._morning_pending.pop(mkey, None)

        # 5. 落实到点的主动发起（重新校验：活动未切换、未进入睡眠时段）
        due_keys = [k for k, fire_at in self._pending_triggers.items() if now_ts >= fire_at]
        for key in due_keys:
            self._pending_triggers.pop(key, None)
            stream_id, activity_name, key_date = key
            if key_date != date_str:
                continue
            if self._in_sleep_window(current_min):
                logger.info(f"主动发起放弃（已进入睡眠时段）: stream={stream_id}, activity={activity_name}")
                continue
            current = self._find_current_activity()
            if current is None or current[0] != activity_name:
                logger.info(f"主动发起放弃（活动已切换）: stream={stream_id}, activity={activity_name}")
                continue
            await self._trigger_proactive(stream_id, activity_name, current[1], current[4])
            self._proactive_history[key] = time.time()

        # 6. 落实早间问好（含「需要激活」判定）
        if cfg_p.enable_morning_greeting and first_activity is not None:
            await self._process_morning_pending(streams, first_activity, date_str, now_ts, in_sleep)

    # ------------------------------------------------------------
    # 早间问好
    # ------------------------------------------------------------

    async def _process_morning_pending(
        self,
        streams: List[str],
        first_activity: Tuple[str, int, int],
        date_str: str,
        now_ts: float,
        in_sleep: bool,
    ) -> None:
        """处理早间问好的待触发队列（含「需要激活」的观察与放弃）。"""
        cfg_p = self._proactive_cfg()
        deadline_ts = self._morning_deadline_ts(first_activity)

        for mkey in list(self._morning_pending.keys()):
            stream_id, key_date = mkey
            if key_date != date_str:
                self._morning_pending.pop(mkey, None)  # 跨天残留，清理
                continue
            fire_at = self._morning_pending.get(mkey)
            if fire_at is None or now_ts < fire_at:
                continue
            self._morning_pending.pop(mkey, None)

            if in_sleep:
                logger.info(f"早间问好放弃（已进入睡眠时段）: stream={stream_id}")
                self._morning_history[mkey] = "abandoned"
                continue

            if cfg_p.morning_greeting_require_activation:
                # 需要激活：有人说话 → 问好；截止（第一个活动结束前 10 分钟）仍无人 → 放弃
                activated = await self._stream_activated_since_wake(stream_id, first_activity)
                if not activated:
                    if deadline_ts is None or now_ts >= deadline_ts:
                        self._morning_history[mkey] = "abandoned"
                        logger.info(
                            f"早间问好放弃（截止仍无人说话）: stream={stream_id}, "
                            f"deadline={'不可用' if deadline_ts is None else '已过'}"
                        )
                        continue
                    # 重新排队：下一个节拍继续观察，直到截止时刻
                    self._morning_pending[mkey] = now_ts + self.LOOP_INTERVAL_SECONDS
                    continue

            await self._trigger_morning_greeting(stream_id, first_activity[0])
            self._morning_history[mkey] = "fired"

    def _morning_deadline_ts(self, first_activity: Tuple[str, int, int]) -> Optional[float]:
        """计算早间问好的观察截止时间戳（第一个活动结束前 10 分钟）。"""
        now = self._tz_manager.get_now()
        try:
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            base = midnight.timestamp()
        except Exception:
            return None
        end_minutes = max(1, first_activity[2] - _MORNING_DEADLINE_MARGIN_MINUTES)
        return base + end_minutes * 60

    async def _stream_activated_since_wake(
        self,
        stream_id: str,
        first_activity: Tuple[str, int, int],
    ) -> bool:
        """检查会话自 bot 睡醒（第一个活动开始）起是否有人发过消息。"""
        ctx_obj = getattr(self._plugin, "ctx", None)
        if ctx_obj is None or not hasattr(ctx_obj, "message"):
            return False
        try:
            now = self._tz_manager.get_now()
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            wake_ts = midnight.timestamp() + first_activity[1] * 60
            msgs = await ctx_obj.message.get_by_time_in_chat(
                stream_id, str(wake_ts), str(time.time()), limit=1,
            )
        except Exception as exc:
            logger.debug(f"查询会话消息失败: {exc}")
            return False
        if isinstance(msgs, dict):
            msgs = msgs.get("messages") or msgs.get("data") or []
        return bool(msgs)

    async def _trigger_morning_greeting(self, stream_id: str, activity_name: str) -> None:
        """对一个会话触发早间问好。"""
        intent_prompt = _MORNING_GREETING_INTENT.format(activity_name=activity_name)
        ok = await self._call_proactive_trigger(
            stream_id=stream_id,
            intent_prompt=intent_prompt,
            reason="planning_v4: 早间问好",
            priority="normal",
        )
        if ok:
            self._decision_log("trigger", trigger_type="morning_greeting", reason=activity_name, stream_id=stream_id)

    # ------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------

    def _in_sleep_window(self, current_min: int) -> bool:
        """判断当前分钟是否处于设定的睡眠时段（入睡 → 次日起床）。

        与是否开启无睡眠模式无关——只要是设定的睡眠时间，主动行为都不触发。
        """
        cfg = section_or_default(self._plugin, "schedule", ScheduleSectionConfig)
        wake_min = _parse_hhmm_minutes(cfg.wake_time, _DEFAULT_WAKE_MINUTES)
        sleep_min = _parse_hhmm_minutes(cfg.sleep_time, _DEFAULT_SLEEP_MINUTES)
        if wake_min == sleep_min:
            return False
        if wake_min < sleep_min:
            # 清醒 [wake, sleep) 同日；睡眠 [sleep, 24:00) ∪ [0, wake)
            return not (wake_min <= current_min < sleep_min)
        # 跨午夜夜猫子：清醒 [wake, 24:00) ∪ [0, sleep)；睡眠 [sleep, wake) 同日
        return sleep_min <= current_min < wake_min

    def _find_current_activity(self) -> Optional[Tuple[str, str, int, int, Dict[str, Any]]]:
        """从 GoalManager 找当前正在进行的活动。

        Returns:
            ``(name, goal_type, start_minutes, end_minutes, parameters)``；没找到时为 None。
        """
        try:
            gm = get_goal_manager()
            goals = gm.get_active_goals(chat_id="global")
            if not goals:
                return None
            now = self._tz_manager.get_now()
            current_min = now.hour * 60 + now.minute

            for goal in goals:
                # 跳过 pending_commitment 等非日程活动
                if goal.goal_type == "pending_commitment":
                    continue
                tw = None
                if goal.parameters and "time_window" in goal.parameters:
                    tw = goal.parameters.get("time_window")
                elif goal.conditions:
                    tw = goal.conditions.get("time_window")
                if not tw:
                    continue
                start, end = parse_time_window(tw)
                if start is None or end is None:
                    continue
                # 跨夜归一化
                if end > 1440 and current_min < 720:
                    end -= 1440
                if start <= current_min < end:
                    return (
                        goal.name,
                        goal.goal_type or "daily_routine",
                        start,
                        end,
                        dict(goal.parameters) if goal.parameters else {},
                    )
        except Exception as exc:
            logger.debug(f"查找当前活动失败: {exc}")
        return None

    def _find_first_activity_today(self) -> Optional[Tuple[str, int, int]]:
        """找今天日程中开始时间最早的活动（即睡醒后的第一个活动）。"""
        try:
            goals = get_goal_manager().get_schedule_goals(chat_id="global")
        except Exception as exc:
            logger.debug(f"查找今日日程失败: {exc}")
            return None
        best: Optional[Tuple[str, int, int]] = None
        for goal in goals:
            tw = None
            if goal.parameters and "time_window" in goal.parameters:
                tw = goal.parameters.get("time_window")
            elif goal.conditions:
                tw = goal.conditions.get("time_window")
            if not tw:
                continue
            start, end = parse_time_window(tw)
            if start is None or end is None:
                continue
            if best is None or start < best[1]:
                best = (goal.name, start, end)
        return best

    def _fresh_window_seconds(self) -> float:
        """触发窗口（秒），来自配置 ``proactive_fresh_window_minutes``。"""
        try:
            minutes = int(self._proactive_cfg().proactive_fresh_window_minutes)
        except (TypeError, ValueError):
            minutes = 10
        return max(1, minutes) * 60.0

    def _random_delay_seconds(self) -> float:
        """生成独立随机延迟：窗口内随机，预留轮询粒度，保证窗口内必然触发。"""
        upper = max(self.RANDOM_DELAY_MIN_SECONDS + 1.0, self._fresh_window_seconds() - self.LOOP_INTERVAL_SECONDS)
        return random.uniform(self.RANDOM_DELAY_MIN_SECONDS, upper)

    # ------------------------------------------------------------
    # 频率调控
    # ------------------------------------------------------------

    async def _apply_frequency(self, streams: List[str], goal_type: str) -> None:
        """按 goal_type 映射频率因子并对每个 stream 应用（因子不变则跳过）。"""
        target_factor = _FREQUENCY_FACTOR_BY_GOAL_TYPE.get(goal_type, 1.0)
        ctx_obj = getattr(self._plugin, "ctx", None)
        if ctx_obj is None or not hasattr(ctx_obj, "frequency"):
            return  # 宿主未提供 frequency 能力（未声明时兼容）

        applied = 0
        failed = 0
        for stream_id in streams:
            if self._current_factor.get(stream_id) == target_factor:
                continue
            try:
                result = await ctx_obj.frequency.set_adjust(stream_id, target_factor)
                if isinstance(result, dict) and not result.get("success", False):
                    failed += 1
                    continue
                self._current_factor[stream_id] = target_factor
                applied += 1
            except Exception:
                failed += 1

        if applied:
            logger.debug(
                f"频率调控: goal_type={goal_type}, factor={target_factor}, 已应用到 {applied} 个会话"
            )
        if failed:
            logger.debug(f"频率调控失败: goal_type={goal_type}, factor={target_factor}, 共 {failed} 个")

    # ------------------------------------------------------------
    # 主动发起
    # ------------------------------------------------------------

    async def _call_proactive_trigger(
        self,
        stream_id: str,
        intent_prompt: str,
        reason: str,
        priority: str = "normal",
    ) -> bool:
        """调用宿主 maisaka.proactive.trigger（带 priority 时失败则降级不带重试）。"""
        ctx_obj = getattr(self._plugin, "ctx", None)
        if ctx_obj is None or not hasattr(ctx_obj, "maisaka"):
            return False
        trigger = getattr(ctx_obj.maisaka, "proactive", None)
        trigger = getattr(trigger, "trigger", None) if trigger is not None else None
        if trigger is None:
            # 兼容旧形态 ctx.maisaka.trigger_proactive
            trigger = getattr(ctx_obj.maisaka, "trigger_proactive", None)
        if trigger is None:
            logger.warning("宿主未提供 maisaka.proactive.trigger 能力，主动发起跳过")
            return False

        # M0 预算接线（§六.2 H）：查询预算 → 发布 proactive.used → 执行触发。
        # 活动切换主动开口与早间问好共用本调用处，两条通道受同一个全渠道预算约束。
        bus = self._event_bus()
        if bus is not None:
            label = str(reason or intent_prompt or "proactive")
            if not bus.allow_proactive("planning", label):
                return False
            try:
                await bus.publish("proactive.used", source="planning",
                                  channel="planning", intent=label)
            except Exception:
                logger.debug("proactive.used 发布失败", exc_info=True)

        result = None
        if priority and priority != "normal":
            try:
                result = await trigger(stream_id=stream_id, intent=intent_prompt, reason=reason, priority=priority)
            except Exception:
                result = None  # 不支持 priority 参数时降级
        if result is None:
            try:
                result = await trigger(stream_id=stream_id, intent=intent_prompt, reason=reason)
            except Exception as exc:
                logger.warning(f"主动触发异常: stream={stream_id}, {exc}")
                return False

        if isinstance(result, dict) and not result.get("success", False):
            logger.warning(f"主动发起被拒: stream={stream_id}, reason={result.get('error')}")
            return False
        return True

    async def _trigger_proactive(
        self,
        stream_id: str,
        activity_name: str,
        goal_type: str,
        activity_params: Dict[str, Any],
    ) -> None:
        """对一个聊天流触发一次主动开口（约定来源走强指令模板 + high 优先级）。"""
        is_commitment = bool(activity_params.get("is_commitment"))
        if is_commitment:
            commit_title = str(activity_params.get("commitment_title") or activity_name).strip()
            commit_time = str(activity_params.get("commitment_time") or "").strip()
            commit_notes = str(activity_params.get("commitment_notes") or "").strip()
            intent_prompt = _COMMITMENT_INTENT_TEMPLATE.format(
                time_hint=f"{commit_time} " if commit_time else "",
                title=commit_title,
                notes_hint=commit_notes or "无",
                activity_name=activity_name,
            )
            reason = f"planning_v4: 约定到期 → {commit_title}"
        else:
            template = _PROACTIVE_INTENT_TEMPLATES.get(goal_type, _DEFAULT_PROACTIVE_INTENT)
            intent_prompt = f"{template}（活动名：{activity_name}）"
            reason = f"planning_v4: 活动切换 → {activity_name}"

        ok = await self._call_proactive_trigger(
            stream_id=stream_id,
            intent_prompt=intent_prompt,
            reason=reason,
            priority="high" if is_commitment else "normal",
        )
        if ok:
            logger.info(
                f"主动发起触发: stream={stream_id}, activity={activity_name}, "
                f"goal_type={goal_type}, commitment={is_commitment}"
            )
            self._decision_log(
                "trigger",
                trigger_type="commitment_due" if is_commitment else "activity_switch",
                reason=reason,
                stream_id=stream_id,
            )

    # ------------------------------------------------------------
    # 事件总线（M0）：活动切换事件（无总线时静默跳过）
    # ------------------------------------------------------------

    def _event_bus(self) -> Any:
        return getattr(self._plugin, "_bus", None)

    async def _publish_activity_transition(
        self,
        activity_info: Optional[Tuple[str, str, int, int, Dict[str, Any]]],
    ) -> None:
        """活动切换判定处：上一个活动结束 + 当前活动开始（附录 A ★）。

        以本循环上次观测到的活动为准（含进程启动后的首次观测）；无切换时静默。
        payload：activity_started = name/goal_type/time_window/stress；
        activity_ended = name/goal_type。
        """
        bus = self._event_bus()
        if bus is None:
            return
        key: Optional[Tuple[str, str, int]] = None
        if activity_info is not None:
            key = (str(activity_info[0]), str(activity_info[1]), int(activity_info[2]))
        if key == self._last_activity_key:
            return
        previous = self._last_activity_info
        self._last_activity_key = key
        self._last_activity_info = activity_info
        try:
            if previous is not None:
                await bus.publish("schedule.activity_ended", source="planning",
                                  name=str(previous[0]), goal_type=str(previous[1]))
            if activity_info is not None:
                name, goal_type, start_minutes, end_minutes, _params = activity_info
                await bus.publish(
                    "schedule.activity_started", source="planning",
                    name=str(name), goal_type=str(goal_type),
                    time_window=[int(start_minutes), int(end_minutes)],
                    stress=self._activity_stress(str(goal_type), int(start_minutes), int(end_minutes)),
                )
        except Exception:
            logger.debug("活动切换事件发布失败", exc_info=True)

    @staticmethod
    def _activity_stress(goal_type: str, start_minutes: int, end_minutes: int) -> bool:
        """高压判定（§6.2 E）：当前为 study/work 且连续同类块 ≥4h。

        连续块 = 时间相邻（间隔 ≤15 分钟）的 study/work 条目合并；取不到日程时
        按当前条目时长兜底。
        """
        if goal_type not in ("study", "work"):
            return False
        try:
            goals = get_goal_manager().get_schedule_goals(chat_id="global")
        except Exception:
            goals = []
        blocks: List[Tuple[int, int]] = []
        for goal in goals:
            if str(getattr(goal, "goal_type", "")) not in ("study", "work"):
                continue
            start, end = parse_time_window((goal.parameters or {}).get("time_window"))
            if start is None or end is None or int(end) <= int(start):
                continue
            blocks.append((int(start), int(end)))
        if not blocks:
            return (end_minutes - start_minutes) >= 240
        blocks.sort()
        cur_start, cur_end = blocks[0]
        for start, end in blocks[1:]:
            if start - cur_end <= 15:  # 相邻/间隔 ≤15 分钟视为连续
                cur_end = max(cur_end, end)
                continue
            if cur_start <= start_minutes < cur_end:
                return (cur_end - cur_start) >= 240
            cur_start, cur_end = start, end
        if cur_start <= start_minutes < cur_end:
            return (cur_end - cur_start) >= 240
        return (end_minutes - start_minutes) >= 240

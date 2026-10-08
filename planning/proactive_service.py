"""活动驱动的主动行为服务（来源：自主规划插件 services/proactive_service.py，移植版）。

三个职责合并在一个 60s 轮询的后台循环里：

1. **活动切换主动发起** —— 活动开始后，在触发窗口内让 bot 主动开口
   （``ctx.maisaka.proactive.trigger``）。不覆盖当天睡醒后的第一个活动，设定的睡眠时段内不触发；
   每个 ``(stream_id, activity_name, date)`` 组合只触发一次。

2. **L2 早晚安漂移（附录 §三，裁决 B1/B3/B5/B6/B7；M11 附录 II §二 三类开关与名单）** ——
   早/晚安只配漂移区间（``morning_drift`` / ``night_drift``，每流每日独立掷点）：早安基准 =
   睡眠窗口结束（``[schedule].wake_time`` 锚点），晚安基准 = 当天最后一个非睡眠日程条目结束
   （无日程回退 ``sleep_time`` 锚点）。到点按「必要性激活」判定触发（早安自基准点起、
   晚安自 ``fire_at`` 起该流有人发过消息——附录 §六.补 B5-补），到期限仍无人则放弃；
   触发矩阵（附录 II §2.2）：目标私聊**必定触发**（``greeting_target_enabled``，不看名单）、
   群聊走问候专用黑白名单 + 必要性激活（``greeting_group_*``）、其他私聊走独立开关 +
   前一日消息数门槛（``greeting_other_private_*``，白名单留空 = 全不发）。
   **例行问候免预算**（§2.3）：默认 ``greeting_exempt_budget=true``，早/晚安触发既不查预算
   闸门也不计主动打扰用量；想念/日常/活动切换仍照旧受预算约束。

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
import re
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from .config import PlanningProactiveSectionConfig, ScheduleSectionConfig, section_or_default
from .goal_manager import get_goal_manager
from .time_utils import TimezoneManager, is_in_time_window, parse_time_window
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

# 早间问好 intent 模板（L2：基准 = 睡眠窗口结束，首个非睡眠日程条目名作补充信息）
_MORNING_GREETING_INTENT = (
    "Bot 刚睡醒，新的一天开始了，请自然地向大家道一声早安；"
    "可以顺口提一句今天的第一个安排（{activity_name}），语气符合人设，简短随意"
)
#: 无今日日程时的早安 intent（不带"第一个安排"这句）
_MORNING_GREETING_INTENT_NO_ACTIVITY = (
    "Bot 刚睡醒，新的一天开始了，请自然地道一声早安；语气符合人设，简短随意"
)
#: 晚安 intent 模板（L2：基准 = 当天最后一个非睡眠日程条目结束，向前漂移）
_NIGHT_GREETING_INTENT = (
    "Bot 准备休息了，今天快结束了，请自然地道一声晚安；"
    "语气符合人设，简短随意、不要追问对方、不要开新话题"
)

# 约定（pending_commitment）来源的活动用强指令模板，避免陷入工具调用循环
_COMMITMENT_INTENT_TEMPLATE = (
    "你之前答应过对方今天{time_hint}要在私聊里{title}（约定备注：{notes_hint}）。\n"
    "现在到点了，请用符合人设的语气**立即在私聊里发出**这条消息（例如叫醒、问候、"
    "提醒等），不要询问对方、不要等用户先开口、不要陷入工具调用循环。\n"
    "活动名：{activity_name}"
)

# ── L2 早晚安漂移（附录 §三；裁决 B1/B3/B5/B6/B7）──────────────────
#: 漂移区间的严格形态（§3.1）：单数字 N 或 "a-b"；负数（"-5-10"/"-10"）与其他写法一律非法（B3）
_DRIFT_PATTERN = re.compile(r"^\d+(-\d+)?$")
#: 漂移区间默认值（分钟）——非法/负数回退此区间
DEFAULT_GREETING_DRIFT: Tuple[int, int] = (10, 30)
#: 问候种类（掷点/记录键的第二维）
GREETING_MORNING = "morning"
GREETING_NIGHT = "night"
#: 覆盖类别（§3.4/B7）：目标私聊（固定生效）/ 群聊（必要性激活）/ 其他私聊（单独开关 + 门槛）
GREETING_CATEGORY_TARGET = "target"
GREETING_CATEGORY_GROUP = "group"
GREETING_CATEGORY_OTHER = "other"
#: 掷点状态机的状态取值（§3.2/§3.3）
GREETING_STATUS_PENDING = "pending"
GREETING_STATUS_FIRED = "fired"
GREETING_STATUS_ABANDONED = "abandoned"
#: 早安无「非睡眠日程条目」时的放弃期限兜底：基准 + 120 分钟（§3.2 期限列的实现裁量）
_MORNING_FALLBACK_DEADLINE_MINUTES = 120
#: 睡眠条目名称兜底关键词：与 emotion/service.py 的 `_is_sleep_item` 同一依据
#: （planning 不 import emotion，避免循环依赖；F14 要求早安期限过滤睡眠条目）
_SLEEP_NAME_KEYWORDS = ("睡觉", "睡眠", "安睡", "午休", "午睡", "小憩", "打盹")
#: [patrol] 静默时段开始的缺省值（配置不可得时的兜底，与 companion/config.py 默认值一致；
#: 缺省窗口为 00:00-07:30，晚安期限只用到起点）
_DEFAULT_SILENCE_START = "00:00"
_DEFAULT_SILENCE_END = "07:30"


def _parse_drift(value: Any, default: Tuple[int, int] = DEFAULT_GREETING_DRIFT) -> Tuple[int, int]:
    """解析漂移区间配置（§3.1，裁决 B3）：``"10-30"`` / ``20`` → ``(a, b)``。

    - 单数字 ``N`` → ``(0, N)``；``"a-b"`` → ``(min, max)``（``a > b`` 自动交换）；
    - 严格匹配 ``^\\d+(-\\d+)?$``（int 先转 str）；负数（``"-5-10"`` / ``"-10"``）、空值、
      其他非法写法一律**回退默认区间**并记 debug 日志——**不收负数**；
    - 模块级纯函数（无 self/无 IO），便于直测与服务层复用。
    """
    raw = "" if value is None else str(value).strip()
    if _DRIFT_PATTERN.match(raw):
        if "-" in raw:
            left, right = raw.split("-", 1)
            a, b = int(left), int(right)
            return (a, b) if a <= b else (b, a)  # a > b 自动交换
        return (0, int(raw))                    # 单数字 N → 0~N 随机
    logger.debug(f"漂移区间非法（{value!r}）→ 回退默认区间 {default[0]}-{default[1]}")
    return default


def _is_sleep_schedule_item(goal: Any) -> bool:
    """日程条目是否为睡眠条目（``goal_type == "sleep"`` 或名称含睡觉类关键词）。

    依据同 ``emotion/service.py`` 的 ``_is_sleep_item``（M5 实测：生产日程的"睡觉"条目是
    ``goal_type=daily_routine`` + 名称「睡觉」，只看 goal_type 会漏判）。
    """
    if str(getattr(goal, "goal_type", "") or "") == "sleep":
        return True
    name = str(getattr(goal, "name", "") or "")
    return any(keyword in name for keyword in _SLEEP_NAME_KEYWORDS)


def _goal_time_window(goal: Any) -> Tuple[Optional[int], Optional[int]]:
    """取 goal 的时间窗口并解析为分钟（先 parameters 后 conditions，与既有取数口径一致）。"""
    tw = None
    if getattr(goal, "parameters", None) and "time_window" in goal.parameters:
        tw = goal.parameters.get("time_window")
    elif getattr(goal, "conditions", None):
        tw = goal.conditions.get("time_window")
    if not tw:
        return None, None
    return parse_time_window(tw)


def _epoch_ts(dt: datetime) -> float:
    """datetime → epoch 秒（tz-aware 按其时区换算；异常回退当前时间）。"""
    try:
        return float(dt.timestamp())
    except (OverflowError, OSError, ValueError):
        return time.time()

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
        # L2 早晚安漂移掷点（§3.2）：{(stream_id, date_str, kind): 掷点条目}
        # 纯内存、不落盘——热重载后允许重掷（附录 §3.2 明示口径）
        self._greeting_rolls: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        # L2 其他私聊早安门槛（§3.4/B7）当日缓存：{(stream_id, date_str): qualified}
        self._greeting_other_gate: Dict[Tuple[str, str], bool] = {}
        # M11 问候名单解析告警去重（§2.4）：{(date_str, 名单签名): True}（同签名每日一条）
        self._greeting_list_warned: Dict[Tuple[str, Tuple[str, ...]], bool] = {}
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
        """读当前活动 → 安排/落实 主动发起、L2 早晚安漂移、频率调控。"""
        cfg_p = self._proactive_cfg()
        greeting_flags = self._greeting_flags(cfg_p)
        if not (
            cfg_p.enable_proactive_trigger
            or cfg_p.enable_frequency_modulation
            or cfg_p.enable_morning_greeting
            or cfg_p.enable_night_greeting
            # M11（附录 II §二）：三类问候开关任一开启（默认目标/群聊为开）即需进入本轮
            or greeting_flags["target_enabled"]
            or greeting_flags["group_enabled"]
            or greeting_flags["other_private_enabled"]
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

            # 3. 活动切换主动发起之外不再排期问候：早/晚安由下面的漂移机制统一驱动（B1 合并）

        # 4. 清理跨天的残留排期
        for key in [k for k in self._pending_triggers if k[2] != date_str]:
            self._pending_triggers.pop(key, None)

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

        # 6. L2 早晚安漂移（§3.2/§3.3）：掷点复用 → 到点激活判定 → 触发/推迟/放弃
        await self._process_greetings(now, date_str)

    # ------------------------------------------------------------
    # L2 早晚安漂移（附录 §三；裁决 B1/B3/B5/B6/B7）
    # ------------------------------------------------------------

    def _schedule_anchors(self) -> Tuple[int, int]:
        """``(wake_min, sleep_min)`` 作息锚点（``[schedule]``，缺省 07:00 / 23:00）。

        F16/B2：这两个锚点定义「新一天」的起止——早安基准取 wake（= 睡眠窗口结束），
        晚安无日程时回退 sleep 锚点（B4 只做兜底优先级，不强制两者同步）。
        """
        cfg = section_or_default(self._plugin, "schedule", ScheduleSectionConfig)
        wake_min = _parse_hhmm_minutes(getattr(cfg, "wake_time", ""), _DEFAULT_WAKE_MINUTES)
        sleep_min = _parse_hhmm_minutes(getattr(cfg, "sleep_time", ""), _DEFAULT_SLEEP_MINUTES)
        return wake_min, sleep_min

    def _patrol_time(self, attr: str, default: str) -> str:
        """读 ``[patrol]`` 时间窗配置（companion 未装配/字段缺失 → 缺省值兜底）。"""
        patrol = getattr(getattr(self._plugin, "config", None), "patrol", None)
        return str(getattr(patrol, attr, "") or default)

    @staticmethod
    def _midnight_ts(now: datetime) -> float:
        """当天 00:00 的绝对时间戳（把日程分钟数换算成绝对时刻的基准）。"""
        return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def _find_first_non_sleep_activity_today(self, date_str: str) -> Optional[Tuple[str, int, int]]:
        """睡眠结束后第一个**非睡眠**日程条目（F14：复用取数口径 + 一条 ``_is_sleep_item`` 过滤）。"""
        return self._find_first_activity_today(date_str, skip_sleep=True)

    def _last_non_sleep_activity_end_today(self, date_str: str) -> Optional[int]:
        """当天最后一个非睡眠日程条目的结束分钟数（跨午夜条目可能 > 1440）。"""
        try:
            goals = get_goal_manager().get_schedule_goals(chat_id="global", date_str=date_str)
        except Exception as exc:
            logger.debug(f"查找今日日程失败: {exc}")
            return None
        latest: Optional[int] = None
        for goal in goals:
            if _is_sleep_schedule_item(goal):
                continue
            _start, end = _goal_time_window(goal)
            if end is None:
                continue
            if latest is None or int(end) > latest:
                latest = int(end)
        return latest

    def _greeting_baseline_ts(self, kind: str, now: datetime, date_str: str) -> float:
        """问候基准点（绝对时间戳，§3.2/B2/B4）。"""
        midnight = self._midnight_ts(now)
        wake_min, sleep_min = self._schedule_anchors()
        if kind == GREETING_MORNING:
            # 早安：睡眠窗口结束 = [schedule].wake_time 锚点（与「新一天起点」同一概念）
            return midnight + wake_min * 60
        # 晚安：当天最后一个非睡眠日程条目结束；无日程 → 回退 sleep_time 锚点（B4）
        end_min = self._last_non_sleep_activity_end_today(date_str)
        if end_min is None:
            return midnight + sleep_min * 60
        return midnight + end_min * 60

    def _greeting_deadline_ts(self, kind: str, now: datetime, date_str: str,
                              baseline_ts: float) -> float:
        """放弃期限（绝对时间戳，§3.2）。"""
        midnight = self._midnight_ts(now)
        if kind == GREETING_MORNING:
            first = self._find_first_non_sleep_activity_today(date_str)
            if first is None:
                # 实现裁量（§3.2 未定义此分支）：无非睡眠条目 → 基准 + 120 分钟；
                # 不给"无日程"留无期限观察，避免早安在任意时刻补发（防打扰）
                return baseline_ts + _MORNING_FALLBACK_DEADLINE_MINUTES * 60
            return midnight + int(first[2]) * 60
        # 晚安：睡眠窗口开始 与 [patrol] silence_start 取先到者
        _wake_min, sleep_min = self._schedule_anchors()
        return min(self._next_daily_anchor_ts(now, sleep_min), self._silence_start_ts(now))

    def _next_daily_anchor_ts(self, now: datetime, minute: int) -> float:
        """把「当天分钟数」锚点落成**今晚**的绝对时刻（§3.2 晚安的基准/期限分量）。

        参照点 = **今天的起床锚点**（``[schedule].wake_time``，F16 的"新一天起点"）：取最近一次
        不早于参照点的该锚点。常规作息（sleep > wake）得到今天的 ``sleep_time``/``silence_start``；
        夜猫子作息（sleep < wake，日程延续到次日凌晨）自动落到**次日凌晨**——保证晚安窗口始终是
        「最后一个日程之后、入睡之前」，不会因为拿今天的凌晨锚点而误判成"窗口已过"。
        """
        wake_min, _sleep_min = self._schedule_anchors()
        midnight = self._midnight_ts(now)
        ts = midnight + int(minute) * 60
        if ts < midnight + wake_min * 60:
            ts += 86400.0
        return ts

    def _silence_start_ts(self, now: datetime) -> float:
        """今晚静默时段开始的绝对时刻（``[patrol].silence_start``，§3.2 期限列）。

        同 :meth:`_next_daily_anchor_ts`（以起床锚点为参照）：默认 ``00:00-07:30`` → 次日 00:00
        （与入睡锚点取 min 后通常由后者先到）；晚间开始的静默窗（如 21:30-07:30）→ 今天 21:30，
        已过即视为窗口已关（不再发晚安）。
        """
        start_min = _parse_hhmm_minutes(
            self._patrol_time("silence_start", _DEFAULT_SILENCE_START), 0)
        return self._next_daily_anchor_ts(now, start_min)

    def _in_silence_window(self, now: datetime) -> bool:
        """当前是否处于 ``[patrol]`` 静默时段（§3.6：合并后静默仍按 silence_* 生效）。

        支持跨午夜窗口（沿用既有 is_in_time_window 语义）；配置非法按不在静默处理
        （与巡检侧"非法配置 fail-safe"口径一致）——此时问候链不因静默推迟。
        """
        return is_in_time_window(
            self._patrol_time("silence_start", _DEFAULT_SILENCE_START),
            self._patrol_time("silence_end", _DEFAULT_SILENCE_END),
            now.strftime("%H:%M"), invalid_result=False)

    def _greeting_drift(self, kind: str) -> Tuple[int, int]:
        """取该 kind 的漂移区间（非法/负数回退默认区间，§3.1/B3）。"""
        cfg_p = self._proactive_cfg()
        attr = "morning_drift" if kind == GREETING_MORNING else "night_drift"
        return _parse_drift(getattr(cfg_p, attr, None))

    def _roll_greeting(self, stream_id: str, category: str, kind: str, date_str: str,
                       now: datetime, now_ts: float) -> Dict[str, Any]:
        """掷点（§3.2）：每 ``(stream_id, date, kind)`` 只掷一次，同流同日重复决策复用同一结果。

        纯内存、不落盘（热重载后允许重掷）；跨午夜条目按绝对时间戳处理。生成时窗口已关
        （``now ≥ deadline``）→ 直接记 ``abandoned``（当天不再评估该流该 kind）。
        """
        key = (stream_id, date_str, kind)
        entry = self._greeting_rolls.get(key)
        if entry is not None:
            return entry

        drift = self._greeting_drift(kind)
        baseline_ts = self._greeting_baseline_ts(kind, now, date_str)
        deadline_ts = self._greeting_deadline_ts(kind, now, date_str, baseline_ts)
        offset = float(random.randint(drift[0], drift[1])) * 60.0   # 漂移区间是分钟 → 秒
        fire_at = baseline_ts + offset if kind == GREETING_MORNING else baseline_ts - offset
        entry = {
            "stream_id": stream_id,
            "category": category,
            "kind": kind,
            "date": date_str,
            "drift": drift,
            "rolled": True,
            "baseline": baseline_ts,
            "deadline": deadline_ts,
            "fire_at": fire_at,
            "status": GREETING_STATUS_PENDING,
            "reason": "",
        }
        if now_ts >= deadline_ts:
            entry["status"] = GREETING_STATUS_ABANDONED
            entry["reason"] = "window_closed"
            logger.debug(
                f"问候窗口已过（不排期）: stream={stream_id}, kind={kind}, date={date_str}"
            )
        else:
            logger.info(
                f"问候掷点: stream={stream_id}, kind={kind}, category={category}, "
                f"drift={drift[0]}-{drift[1]}, fire_at={int(fire_at)}, "
                f"deadline={int(deadline_ts)}"
            )
        self._greeting_rolls[key] = entry
        return entry

    def _greeting_requires_activation(self, category: str, kind: str, cfg_p: Any) -> bool:
        """是否需要「必要性激活」（§3.3/B5/B7）：目标私聊固定生效，其余按开关。

        - 目标私聊：固定生效（B7），激活要求不适用；
        - 群聊：按 ``greeting_require_activation``（早安另兼容旧字段 ``morning_greeting_require_activation``，取或）；
        - 其他私聊：激活条件 = 前一日消息数门槛（§3.4/B7 的「激活条件 = 前一日该流 ≥ N 条消息」），
          即由 ``_morning_other_private_qualified`` 承担，不再叠加「自基准点起有人说话」。
        """
        if category != GREETING_CATEGORY_GROUP:
            return False
        if bool(getattr(cfg_p, "greeting_require_activation", True)):
            return True
        if kind == GREETING_MORNING:
            # 旧字段兼容（取或）：此前显式开启过「早间问好需激活」的用户仍然生效
            return bool(getattr(cfg_p, "morning_greeting_require_activation", False))
        return False

    async def _stream_activated_since_wake(self, stream_id: str, since_ts: float,
                                           until_ts: float) -> bool:
        """自 ``since_ts`` 起该流是否有人发过消息（B5：早安/晚安共用同一查询形态）。

        窗口起点由调用方分派（附录 §六.补 B5-补）：晚安 = ``fire_at``、早安 = ``baseline``
        （见 :meth:`_evaluate_greeting`）。沿用既有调用形态
        ``ctx.message.get_by_time_in_chat(stream_id, 起点ts, now, limit=1)``；
        能力不可用/异常 → False（保守：没人说话就不打扰）。
        """
        ctx_obj = getattr(self._plugin, "ctx", None)
        if ctx_obj is None or not hasattr(ctx_obj, "message"):
            return False
        try:
            msgs = await ctx_obj.message.get_by_time_in_chat(
                stream_id, str(since_ts), str(until_ts), limit=1,
            )
        except Exception as exc:
            logger.debug(f"查询会话消息失败: {exc}")
            return False
        if isinstance(msgs, dict):
            msgs = msgs.get("messages") or msgs.get("data") or []
        return bool(msgs)

    async def _morning_other_private_qualified(self, stream_id: str, date_str: str,
                                               now: datetime, cfg_p: Any) -> bool:
        """其他私聊问候门槛（§3.4/B7；M11 §2.2 早/晚共用）：前一日该流消息数 ≥ 门槛。

        门槛取 ``greeting_other_private_min_messages``（``ge=5`` 硬约束；被取代键
        ``morning_other_private_min_messages`` 的 deprecated 映射在配置层完成——
        显式旧键沿用为新门槛的缺省，新键显式优先）。
        经 ``get_by_time_in_chat`` 取「昨日 00:00 → 今日 00:00」的消息，``limit=门槛`` 即够判定；
        能力不可用/异常/返回形态不可解析 → 按 0 处理（**不发送**）。门槛当日内不变 → 当日缓存。
        """
        cache_key = (stream_id, date_str)
        if cache_key in self._greeting_other_gate:
            return self._greeting_other_gate[cache_key]

        try:
            need = max(1, int(self._greeting_flags(cfg_p)["other_private_min_messages"]))
        except (TypeError, ValueError):
            need = 5

        qualified = False
        ctx_obj = getattr(self._plugin, "ctx", None)
        if ctx_obj is not None and hasattr(ctx_obj, "message"):
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            yesterday_start = midnight - timedelta(days=1)
            try:
                msgs = await ctx_obj.message.get_by_time_in_chat(
                    stream_id, str(yesterday_start.timestamp()), str(midnight.timestamp()),
                    limit=need,
                )
            except Exception as exc:
                logger.debug(f"查询前一日消息失败（按 0 处理）: stream={stream_id}, {exc}")
                msgs = None
            if isinstance(msgs, dict):
                msgs = msgs.get("messages") or msgs.get("data") or []
            if isinstance(msgs, list):
                qualified = len(msgs) >= need

        self._greeting_other_gate[cache_key] = qualified
        return qualified

    @staticmethod
    def _greeting_flags(cfg_p: Any) -> Dict[str, Any]:
        """三类问候的生效取值（M11 附录 II §2.2/§2.6）。

        配置层（``PlanningProactiveSectionConfig.greeting_effective``）统一解析
        「新键显式 > 旧键显式 > 新键默认」与被取代旧键（``enable_night_greeting`` 显式 false =
        晚安全灭）的 deprecated 映射；非 pydantic 配置对象（骨架/测试桩）没有该解析方法 →
        退化为直接读键（新键缺失时旧键归并生效），保证服务可独立运行。
        """
        resolver = getattr(cfg_p, "greeting_effective", None)
        if callable(resolver):
            try:
                return dict(resolver())
            except Exception:
                logger.debug("greeting_effective 解析失败，回落直接读键", exc_info=True)

        other = getattr(cfg_p, "greeting_other_private_enabled", None)
        if other is None:
            other = getattr(cfg_p, "morning_other_private_enabled", False)
        raw_threshold = getattr(cfg_p, "greeting_other_private_min_messages", None)
        if raw_threshold is None:
            raw_threshold = getattr(cfg_p, "morning_other_private_min_messages", 5)
        try:
            threshold = max(5, int(raw_threshold))
        except (TypeError, ValueError):
            threshold = 5
        return {
            "target_enabled": bool(getattr(cfg_p, "greeting_target_enabled", True)),
            "group_enabled": bool(getattr(cfg_p, "greeting_group_enabled", True)),
            "other_private_enabled": bool(other),
            "other_private_min_messages": threshold,
            "night_allowed": True,
        }

    @staticmethod
    def _parse_greeting_list(raw: Any, *, wildcard_allowed: bool) -> Tuple[List[str], bool, List[str]]:
        """解析一份问候专用名单（M11 §2.4，Q2/Q4）：**只收裸群号/QQ 号**（纯数字）。

        返回 ``(合法号列表, 是否通配, 问题清单)``：

        - 带前缀、含非数字、空串、重复项 → 忽略该元素并计入问题清单（调用方汇总一条告警）；
        - 白名单 ``"*"`` → 通配（不过滤）；``*`` 与具体号混用 → 按全放行处理并计入问题；
        - 黑名单不支持通配（``"*"`` 按非法元素处理）。
        """
        values: List[str] = []
        problems: List[str] = []
        wildcard = False
        seen: set = set()
        for item in list(raw or []):
            text = str(item).strip()
            if not text:
                problems.append(f"空串（{item!r}）")
                continue
            if text == "*":
                if wildcard_allowed:
                    wildcard = True
                else:
                    problems.append("黑名单不支持通配 *（按非法元素忽略）")
                continue
            if not text.isdigit():
                problems.append(f"{text}（非法：只收裸号，带前缀或含非数字字符）")
                continue
            if text in seen:
                problems.append(f"{text}（重复项）")
                continue
            seen.add(text)
            values.append(text)
        if wildcard and values:
            problems.append("通配 * 与具体号混用 → 按全放行处理")
        return values, wildcard, problems

    def _note_greeting_list_problem(self, signature: Tuple[str, ...], problems: List[str]) -> None:
        """名单非法元素的**汇总**告警（§2.4）：一条 ``logger.warning`` + 一条决策日志。

        同签名每日一次（不逐条刷屏、也不每轮刷屏）；配置错误在 decision_logs 可见。
        """
        day = self._tz_manager.get_now().strftime("%Y-%m-%d")
        key = (day, signature)
        if key in self._greeting_list_warned:
            return
        # 跨天清理：只保留当天签名（避免内存无界增长）
        self._greeting_list_warned = {k: True for k in self._greeting_list_warned if k[0] == day}
        self._greeting_list_warned[key] = True
        detail = "；".join(problems)
        logger.warning(f"问候名单存在非法/重复元素（已忽略，不逐条刷屏）：{detail}")
        self._decision_log(
            "skip", trigger_type="greeting_list_parse",
            reason=f"问候名单存在非法元素（已忽略）：{detail}",
        )

    def _greeting_lists(self, cfg_p: Any) -> Dict[str, Any]:
        """解析问候专用黑白名单（群聊 + 其他私聊），非法元素汇总告警落决策日志（§2.4）。

        返回 ``group_whitelist`` / ``group_wildcard`` / ``group_blacklist`` /
        ``other_private_whitelist`` / ``other_private_wildcard`` / ``other_private_blacklist``。
        与活动切换的 ``proactive_group_ids`` / ``proactive_other_streams``（支持前缀）**不混用**：
        此处只认裸群号 / 裸 QQ 号。
        """
        raw_group_whitelist = list(getattr(cfg_p, "greeting_group_whitelist", None) or [])
        raw_group_blacklist = list(getattr(cfg_p, "greeting_group_blacklist", None) or [])
        raw_other_whitelist = list(getattr(cfg_p, "greeting_other_private_whitelist", None) or [])
        raw_other_blacklist = list(getattr(cfg_p, "greeting_other_private_blacklist", None) or [])

        group_whitelist, group_wildcard, gw_problems = self._parse_greeting_list(
            raw_group_whitelist, wildcard_allowed=True)
        group_blacklist, _gb_wildcard, gb_problems = self._parse_greeting_list(
            raw_group_blacklist, wildcard_allowed=False)
        other_whitelist, other_wildcard, ow_problems = self._parse_greeting_list(
            raw_other_whitelist, wildcard_allowed=True)
        other_blacklist, _ob_wildcard, ob_problems = self._parse_greeting_list(
            raw_other_blacklist, wildcard_allowed=False)

        problems: List[str] = []
        for label, items in (("群聊白名单", gw_problems), ("群聊黑名单", gb_problems),
                             ("其他私聊白名单", ow_problems), ("其他私聊黑名单", ob_problems)):
            if items:
                problems.append(f"{label}[{len(items)}]：" + "、".join(items))
        if problems:
            signature = tuple(str(x) for x in (raw_group_whitelist + raw_group_blacklist
                                               + raw_other_whitelist + raw_other_blacklist))
            self._note_greeting_list_problem(signature, problems)

        return {
            "group_whitelist": group_whitelist,
            "group_wildcard": group_wildcard,
            "group_blacklist": group_blacklist,
            "other_private_whitelist": other_whitelist,
            "other_private_wildcard": other_wildcard,
            "other_private_blacklist": other_blacklist,
        }

    async def _resolve_greeting_group_streams(self, lists: Dict[str, Any]) -> List[str]:
        """群聊问候候选（§2.2/§2.4）：黑名单命中即剔除；白名单非空（且非通配）时仅白名单。

        名单为空 / 通配 ``["*"]`` → 枚举所有已知群聊（受必要性激活限制）。
        """
        whitelist = list(lists["group_whitelist"])
        blacklist = set(lists["group_blacklist"])
        pairs: List[Tuple[str, Optional[str]]] = []
        if whitelist and not lists["group_wildcard"]:
            for gid in whitelist:
                pairs.append((gid, await self._resolve_group_id(gid)))
        else:
            pairs = await self._enumerate_group_pairs()

        result: List[str] = []
        for gid, sid in pairs:
            if not sid or str(gid) in blacklist:
                continue
            result.append(sid)
        return result

    async def _enumerate_group_pairs(self) -> List[Tuple[str, str]]:
        """枚举所有群聊的 ``(群号, session_id)``（问候黑名单需要群号维度；活动切换沿用既有枚举）。"""
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

        result: List[Tuple[str, str]] = []
        for stream in streams_raw:
            if not isinstance(stream, dict):
                continue
            group_id = str(stream.get("group_id") or "").strip()
            session_id = str(stream.get("session_id") or stream.get("stream_id") or "").strip()
            if group_id and session_id:
                result.append((group_id, session_id))
        return result

    async def _resolve_private_id(self, user_id: str) -> Optional[str]:
        """把裸 QQ 号解析为私聊 session_id（带解析缓存；问候专用名单只收裸号，不带前缀）。"""
        entry = f"greeting-private:{user_id}"
        now = time.time()
        cached = self._resolved_cache.get(entry)
        if cached is not None and now < cached[1]:
            return cached[0]

        ctx_obj = getattr(self._plugin, "ctx", None)
        session_id: Optional[str] = None
        if ctx_obj is None or not hasattr(ctx_obj, "chat"):
            logger.warning(f"无法解析私聊 {user_id}：ctx.chat 能力不可用")
        else:
            result = None
            try:
                result = await ctx_obj.chat.get_stream_by_user_id(user_id)
            except Exception as exc:
                logger.warning(f"解析私聊 {user_id} 异常: {exc}")
            session_id = self._extract_session_id(entry, result)

        ttl = self.RESOLVE_CACHE_TTL_SUCCESS if session_id else self.RESOLVE_CACHE_TTL_FAILURE
        self._resolved_cache[entry] = (session_id, now + ttl)
        return session_id

    async def _enumerate_private_pairs(self) -> List[Tuple[str, str]]:
        """枚举已知私聊流的 ``(QQ 号, session_id)``（其他私聊白名单通配 ``["*"]`` 时使用）。"""
        ctx_obj = getattr(self._plugin, "ctx", None)
        if ctx_obj is None or not hasattr(ctx_obj, "chat"):
            return []
        getter = getattr(ctx_obj.chat, "get_private_streams", None)
        if not callable(getter):
            logger.debug("宿主未提供 get_private_streams 能力，其他私聊通配名单按空处理")
            return []
        try:
            streams_raw = await getter()
        except Exception as exc:
            logger.debug(f"获取私聊流列表失败: {exc}")
            return []
        if isinstance(streams_raw, dict):
            streams_raw = streams_raw.get("streams") or streams_raw.get("data") or []
        if not isinstance(streams_raw, list):
            return []

        result: List[Tuple[str, str]] = []
        for stream in streams_raw:
            if not isinstance(stream, dict):
                continue
            user_id = str(stream.get("user_id") or "").strip()
            session_id = str(stream.get("session_id") or stream.get("stream_id") or "").strip()
            if user_id and session_id:
                result.append((user_id, session_id))
        return result

    async def _resolve_greeting_other_streams(self, lists: Dict[str, Any]) -> List[str]:
        """其他私聊问候候选（§2.2/§2.4，Q4）：白名单**留空 = 全不发**；黑名单命中即剔除。

        白名单非空时仅白名单内的裸 QQ 号参与；通配 ``["*"]`` → 枚举已知私聊流（不过滤）。
        门槛（前一日消息数 ≥ ``greeting_other_private_min_messages``）在到点判定处统一承担。
        """
        whitelist = list(lists["other_private_whitelist"])
        blacklist = set(lists["other_private_blacklist"])
        pairs: List[Tuple[str, Optional[str]]] = []
        if lists["other_private_wildcard"]:
            pairs = await self._enumerate_private_pairs()
        elif whitelist:
            for uid in whitelist:
                pairs.append((uid, await self._resolve_private_id(uid)))
        else:
            return []   # 白名单留空 = 全不发（默认安全的收紧）

        result: List[str] = []
        for uid, sid in pairs:
            if not sid or str(uid) in blacklist:
                continue
            result.append(sid)
        return result

    async def _resolve_greeting_candidates(self, cfg_p: Any) -> List[Tuple[str, str]]:
        """问候候选（M11 §2.2 矩阵）：目标私聊（开关） ∪ 群聊（名单 + 激活） ∪ 其他私聊（开关 + 门槛）。

        - 目标私聊：``greeting_target_enabled``（默认开）——**豁免名单**，不看任何黑白名单；
        - 群聊：``greeting_group_enabled``（默认开）+ 问候专用黑白名单（空 = 全发）；
        - 其他私聊：``greeting_other_private_enabled``（默认关）+ 白名单（空 = 全不发）+ 黑名单。
        与活动切换主动开口的两套旧名单（``proactive_group_ids`` / ``proactive_other_streams``）
        **不混用**（附录 II §2.4/§八.2）。
        """
        candidates: List[Tuple[str, str]] = []
        seen: set = set()
        flags = self._greeting_flags(cfg_p)

        target = str(getattr(self._plugin, "_stream_id", "") or "").strip()
        if target and flags["target_enabled"]:
            candidates.append((target, GREETING_CATEGORY_TARGET))
            seen.add(target)

        lists = (self._greeting_lists(cfg_p)
                 if (flags["group_enabled"] or flags["other_private_enabled"]) else None)
        if flags["group_enabled"] and lists is not None:
            for sid in await self._resolve_greeting_group_streams(lists):
                if sid and sid not in seen:
                    candidates.append((sid, GREETING_CATEGORY_GROUP))
                    seen.add(sid)

        if flags["other_private_enabled"] and lists is not None:
            for sid in await self._resolve_greeting_other_streams(lists):
                if sid and sid not in seen:
                    candidates.append((sid, GREETING_CATEGORY_OTHER))
                    seen.add(sid)

        return candidates

    async def _process_greetings(self, now: datetime, date_str: str) -> None:
        """L2 每轮：候选枚举 → 掷点/复用 → 到点激活判定 → 触发/推迟/放弃（§3.2/§3.3/§3.5）。

        M11（附录 II §二）：早/晚安不再由 ``enable_morning_greeting`` / ``enable_night_greeting``
        总开关决定，而由三类会话各自的开关与名单决定；被取代的 ``enable_night_greeting``
        仅保留"显式 false = 晚安全灭"的 deprecated 语义（见
        :meth:`PlanningProactiveSectionConfig.greeting_effective`）。
        """
        cfg_p = self._proactive_cfg()
        flags = self._greeting_flags(cfg_p)
        if not (flags["target_enabled"] or flags["group_enabled"] or flags["other_private_enabled"]):
            return
        now_ts = _epoch_ts(now)
        for stream_id, category in await self._resolve_greeting_candidates(cfg_p):
            await self._evaluate_greeting(stream_id, category, GREETING_MORNING,
                                          now, now_ts, date_str, cfg_p)
            await self._evaluate_greeting(stream_id, category, GREETING_NIGHT,
                                          now, now_ts, date_str, cfg_p)
        # 跨天残留清理：只保留当天条目（纯内存状态，热重载后重掷）
        for key in [k for k in self._greeting_rolls if k[1] != date_str]:
            self._greeting_rolls.pop(key, None)
        for gkey in [k for k in self._greeting_other_gate if k[1] != date_str]:
            self._greeting_other_gate.pop(gkey, None)

    async def _evaluate_greeting(self, stream_id: str, category: str, kind: str,
                                 now: datetime, now_ts: float, date_str: str,
                                 cfg_p: Any) -> None:
        """单个候选的到点判定：激活 / 推迟下一 tick / 到期限放弃 / 触发（§3.3）。

        激活查询窗按 kind 分派（**附录 §六.补 B5-补（2026-10-07 协调裁决）**）：

        - **晚安** ``[fire_at, now]``：``fire_at = baseline − drift < baseline``，而常见日程形态
          （末条非睡眠条目截断到入睡锚点）下 ``baseline ≈ 期限``——沿用 ``[baseline, now]``
          会**结构性空窗**、群聊晚安永不触发；改自「该问候成为可能（fire_at）起」。
        - **早安** ``[baseline, now]`` **一字不动**（B5 字面：「自基准点起该流有人发过消息」）。
        - 两者都保留 B5「有人说话才跟着问候」语义；放弃语义不变：``[fire_at, 期限]`` 内
          无人发言 → ``abandoned``（由既有 deadline 分支落 reason）。

        M11（附录 II §2.2）：三类开关在此兜底判定（候选枚举已按开关过滤，本处再判一次保证
        直调路径同样生效）；其他私聊早/晚共用 ``greeting_other_private_enabled`` + 前一日
        消息数门槛（旧键 ``morning_other_private_*`` 由配置层 deprecated 映射归并）。
        """
        flags = self._greeting_flags(cfg_p)
        if kind == GREETING_NIGHT and not flags["night_allowed"]:
            # 被取代键 enable_night_greeting 显式 false → 晚安全灭（三类都不发；§2.6/§七）
            return
        if category == GREETING_CATEGORY_TARGET:
            if not flags["target_enabled"]:
                return
        elif category == GREETING_CATEGORY_GROUP:
            if not flags["group_enabled"]:
                return
        else:
            if not flags["other_private_enabled"]:
                return
            if not await self._morning_other_private_qualified(stream_id, date_str, now, cfg_p):
                return

        entry = self._roll_greeting(stream_id, category, kind, date_str, now, now_ts)
        if entry["status"] in (GREETING_STATUS_FIRED, GREETING_STATUS_ABANDONED):
            return
        if now_ts < entry["fire_at"]:
            return

        activated = True
        if self._greeting_requires_activation(category, kind, cfg_p):
            # 附录 §六.补 B5-补：晚安自 fire_at 起算（用 baseline 会结构性空窗），早安维持 baseline
            since_ts = entry["fire_at"] if kind == GREETING_NIGHT else entry["baseline"]
            activated = await self._stream_activated_since_wake(stream_id, since_ts, now_ts)
        if not activated:
            if now_ts >= entry["deadline"]:
                self._abandon_greeting(entry, "deadline")
            return  # 到点无人说话 → 推迟到下一 tick 继续观察
        if now_ts >= entry["deadline"]:
            self._abandon_greeting(entry, "deadline")
            return
        if self._in_silence_window(now):
            # §3.6：合并后静默时段仍生效——静默中不开口（推迟下一 tick；
            # 静默延续到期限的情形由上一判定的 deadline 分支放弃）
            return
        await self._fire_greeting(entry, date_str, now_ts)

    def _abandon_greeting(self, entry: Dict[str, Any], reason: str) -> None:
        """放弃该流该 kind 的当日问候（状态 + 决策日志；当天不再触发）。"""
        entry["status"] = GREETING_STATUS_ABANDONED
        entry["reason"] = reason
        logger.info(
            f"问候放弃（{reason}）: stream={entry['stream_id']}, kind={entry['kind']}, "
            f"date={entry['date']}"
        )
        self._decision_log(
            "skip", trigger_type=f"{entry['kind']}_greeting", reason=reason,
            stream_id=entry["stream_id"], category=entry["category"],
            fire_at=int(entry["fire_at"]), deadline=int(entry["deadline"]),
        )

    async def _fire_greeting(self, entry: Dict[str, Any], date_str: str, now_ts: float) -> None:
        """触发一次问候（§3.5/F9：一律走同一条主动链；M11 §2.3 例行问候默认免预算）。"""
        stream_id, kind = entry["stream_id"], entry["kind"]
        ok = await self._trigger_greeting(stream_id, kind, date_str)
        if not ok:
            # 预算拒绝 / 宿主触发失败：状态不变，下一轮继续尝试（到期限自然放弃）
            return
        entry["status"] = GREETING_STATUS_FIRED
        entry["fired_at"] = now_ts
        if entry["category"] == GREETING_CATEGORY_TARGET:
            # §3.6：目标私聊写 affection 每日标记，patrol 见已发即不再各发一条
            self._mark_greeting_sent_on_affection(kind)
        self._decision_log(
            "trigger", trigger_type=f"{kind}_greeting",
            reason=("planning_v4: 早安问候（L2 漂移）" if kind == GREETING_MORNING
                    else "planning_v4: 晚安问候（L2 漂移）"),
            stream_id=stream_id, category=entry["category"],
            drift=f"{entry['drift'][0]}-{entry['drift'][1]}",
            fire_at=int(entry["fire_at"]), deadline=int(entry["deadline"]),
        )

    async def _trigger_greeting(self, stream_id: str, kind: str, date_str: str) -> bool:
        """走统一主动链触发一次早晚安问候（intent 提示词按 kind 分派）。

        M11（附录 II §2.3）：例行问候默认免主动打扰预算（``greeting_exempt_budget=true``）——
        传 ``exempt_budget=True`` 时闸门与计数一起跳；关闭免预算则照旧走预算。
        """
        cfg_p = self._proactive_cfg()
        exempt_budget = bool(getattr(cfg_p, "greeting_exempt_budget", True))
        if kind == GREETING_MORNING:
            first = self._find_first_non_sleep_activity_today(date_str)
            intent_prompt = (_MORNING_GREETING_INTENT.format(activity_name=first[0])
                             if first is not None else _MORNING_GREETING_INTENT_NO_ACTIVITY)
            reason = "planning_v4: 早安问候（L2 漂移）"
        else:
            intent_prompt = _NIGHT_GREETING_INTENT
            reason = "planning_v4: 晚安问候（L2 漂移）"
        return await self._call_proactive_trigger(
            stream_id=stream_id,
            intent_prompt=intent_prompt,
            reason=reason,
            priority="normal",
            exempt_budget=exempt_budget,
        )

    def _mark_greeting_sent_on_affection(self, kind: str) -> None:
        """目标私聊触发成功 → 写 affection 的 ``set_morning_sent`` / ``set_night_sent``。

        companion 未装配（``plugin._affection`` 缺失）→ getattr 判空静默跳过（§八.2 可空降级）。
        """
        affection = getattr(self._plugin, "_affection", None)
        if affection is None:
            return
        setter = getattr(
            affection,
            "set_morning_sent" if kind == GREETING_MORNING else "set_night_sent",
            None,
        )
        if not callable(setter):
            return
        try:
            setter()
        except Exception:
            logger.debug("写 affection 早晚安标记失败", exc_info=True)

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

    def _find_first_activity_today(self, date_str: Optional[str] = None,
                                   skip_sleep: bool = False) -> Optional[Tuple[str, int, int]]:
        """找指定日期日程中开始时间最早的活动（即睡醒后的第一个活动）。

        ``date_str`` 缺省 = GoalManager 的「今天」（保持既有行为）；L2 早晚安漂移传入显式日期
        （注入时钟下可测）。``skip_sleep=True`` 过滤睡眠条目（F14：早安放弃期限 =
        睡眠结束后第一个**非睡眠**条目的结束）。
        """
        try:
            gm = get_goal_manager()
            goals = (gm.get_schedule_goals(chat_id="global", date_str=date_str)
                     if date_str else gm.get_schedule_goals(chat_id="global"))
        except Exception as exc:
            logger.debug(f"查找今日日程失败: {exc}")
            return None
        best: Optional[Tuple[str, int, int]] = None
        for goal in goals:
            if skip_sleep and _is_sleep_schedule_item(goal):
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
        exempt_budget: bool = False,
    ) -> bool:
        """调用宿主 maisaka.proactive.trigger（带 priority 时失败则降级不带重试）。

        ``exempt_budget=True``（M11 附录 II §2.3：例行问候专用）→ **闸门与计数一起跳**：
        既不查询主动打扰预算，也不发布 ``proactive.used``（只跳闸门会让计数仍被悄悄消耗）。
        默认 ``False``：非问候主动行为照旧「查预算 → 记用量 → 触发」，但计数身份按 stream
        维度判定（M11 §3.3）——**只有发往目标陪伴私聊**的主动打扰才查计数/入账，
        群聊与其他私聊开口不计入、不受限。
        """
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

        # M0 预算接线（§六.2 H）：查询主动打扰预算 → 发布 proactive.used → 执行触发。
        # 活动切换主动开口与早间问好共用本调用处。
        # M11（附录 II §3.3）：预算计数身份 = **目标陪伴私聊**——按 stream 维度传参，
        # 群聊/其他私聊的活动切换开口直接放行且不计入。
        # M11（附录 II §2.3）：例行问候 exempt_budget=True → 闸门与计数一并跳过。
        bus = self._event_bus()
        if bus is not None and not exempt_budget:
            label = str(reason or intent_prompt or "proactive")
            if not bus.allow_proactive("planning", label, stream_id=stream_id):
                return False
            try:
                await bus.publish("proactive.used", source="planning",
                                  channel="planning", intent=label, stream_id=stream_id)
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

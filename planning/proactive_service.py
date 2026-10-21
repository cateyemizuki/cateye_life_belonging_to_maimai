"""活动驱动的主动行为服务（来源：自主规划插件 services/proactive_service.py，移植版）。

三个职责合并在一个 60s 轮询的后台循环里：

1. **问候（主动开口的唯一插件侧入口）** —— 2026-10-20 用户指示：日程主动行为整条移除后，
   插件主动开口只剩早/晚安（强制激活闸，见 :meth:`_greeting_requires_activation`）与
   陪伴 patrol；活动切换只做频率调控与事件发布，不再触发主动发言。

2. **L2 早晚安漂移（M11 三类开关 → 2026-10-10 两开关 + 统一名单；第二轮重做状态机）** ——
   早/晚安只配漂移区间（``morning_drift`` / ``night_drift``，每流每日独立掷点），执行顺序固定为
   **掷点 → 激活观测 → 到点（或已过点则立即）发**：

   - **基准点**（2026-10-21 §24.2 起真实锚点优先）：早安 = 真实睡眠环起床端（日程缺失回退
     ``[schedule].wake_time`` 锚点）；晚安基准 = 当天最后一个非睡眠日程条目结束
     （无日程回退入睡锚点，同经真实锚点解析）。
   - **漂移点（fire_at）**：早安 = 基准 + randint(a,b)（醒来后随机点）；晚安 = 期限 − randint(a,b)
     （睡前随机点；期限 = ``min(真实入睡锚点, 生效静默起点)``，两者都经真实锚点解析——日程缺失
     回退配置，静默/入睡锚点非法 fail-closed 宁不发）——两个方向都保证点落在
     「当天的清醒区间」内，不会跨过入睡界。
   - **激活观测窗** = ``[min(基准, 漂移点), max(期限, 漂移点)]``（两种问候同一公式）：
     早安期限 = 第一段非睡眠日程结束（无日程 → 基准 + 120 分钟）；
     晚安期限 = 入睡锚点（经真实锚点解析；2026-10-20 用户指示后静默窗复用作息，原
     ``[patrol].silence_start`` 键已删除，min 的静默项与入睡项合一；见
     :meth:`_silence_start_ts`，非法 fail-closed 宁不发）。
   - **判定**：该流当日有人发过消息 → 已激活；激活后未到点 → 等待，到点即发；观察到时
     **已过点** → 立即发；观测窗结束仍无人 → 当天不再评估（放弃）。
   - **激活闸强制开启**（2026-10-20 用户指示，不提供配置项）：激活判定 = 当日（本地 00:00
     起，观测起点更早则取观测起点）该流出现过**非 bot、非斜杠命令**的消息；宁不发不错发。
     错过观测窗（如重启跨过整窗）不补发。
   - **静默时段不再拦截问候**（2026-10-10 第二轮）：默认静默窗 00:00-07:30 与起床锚点 07:00
     重叠，会把漂移整段吞掉（每天固定在 07:30 开口 = 漂移失效），而问候本身已由"必要性激活"
     把关（没人说话就不发）；静默窗真源 = ``companion/patrol.py:resolve_silence_window``
     （2026-10-21 §24.2 起：真实作息锚点优先、``[schedule]`` 配置回退、fail-safe 宁不发；
     ``[patrol] silence_*`` 四键已删除，陪伴巡检与晚安期限共用同源）。
   - 候选矩阵：目标陪伴私聊（``[patrol].enabled`` 且**未被私聊黑名单排除**时生效）；
     群聊与其他私聊走**统一名单系统**（``proactive_group_ids`` / ``proactive_private_ids`` +
     各自模式开关），其他私聊另有「前一日消息数 ≥ 门槛」护栏。
     开/关由 ``greeting_morning_enabled`` / ``greeting_night_enabled`` 两个开关统一控制。
   **例行问候始终免预算**：早/晚安触发既不查预算闸门也不计主动打扰用量；
   预算只服务 [patrol] 的巡检与想念（[patrol].daily_max_speak）。

3. **按 goal_type 调节聊天频率** —— ``ctx.frequency.set_adjust`` 推给 heartflow。

对整合契约的适配：
- 触发走 ``ctx.maisaka.proactive.trigger``（目标插件声明的能力名）；
- 名单接受**裸号**（群聊填群号、私聊填 QQ 号）以及带 ``qq:group:`` / ``qq:private:`` /
  ``group:`` / ``private:`` / ``qq:`` 前缀的写法（自动剥前缀取号，B2 轮补：此前前缀写法会被
  当作非法元素静默忽略，黑名单因此"看起来没生效"）；非法元素汇总级告警（不逐条刷屏）；
- 决策写入 plugin._decision_logger（core/diag）；提示词一律读 suite_prompts。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from suite_prompts import (
    PROACTIVE_INTENT_MORNING_DEFAULT,
    PROACTIVE_INTENT_NIGHT_DEFAULT,
    PROACTIVE_REASON_MORNING_DEFAULT,
    PROACTIVE_REASON_NIGHT_DEFAULT,
    prompt,
    render,
)

from .config import (
    LIST_MODE_BLACKLIST,
    LIST_MODE_WHITELIST,
    PlanningProactiveSectionConfig,
    ScheduleAdvancedSectionConfig,
    ScheduleSectionConfig,
    section_or_default,
)
from core.storage import load_json, save_json_atomic

from .activity_properties import resolve_property
from .goal_manager import get_goal_manager
from .schedule_anchors import SOURCE_REAL_SCHEDULE
from .time_utils import TimezoneManager, is_in_day_frame, parse_time_window
if TYPE_CHECKING:
    from typing import Any as _Plugin

logger = logging.getLogger(__name__)


# （2026-10-21 §24.3 任务C，R7 依据 = 用户直派）goal_type → 频率因子映射表
# ``_FREQUENCY_FACTOR_BY_GOAL_TYPE`` 已删除：数值语义迁为``mappings/schedule_properties.json``
# 的内置默认条目（planning/activity_properties.py 单一真源），``_apply_frequency`` 改经
# :func:`activity_properties.resolve_property` 取属性倍率——倍率与关键词词表成为用户
# 改 json 即可调整的映射表；A-21 ③④（频率表缺 free_time/custom）随内置兜底条目收敛。

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
#: [patrol] 静默起点的生效口径（N5，2026-10-10 第三轮）：留空 = 跟随 [schedule].sleep_time。
#: 生效值解析的真源在 companion.patrol.resolve_silence_window（巡检静默闸同源）；
#: 解析不可用时的本地降级见 :meth:`ProactiveService._effective_silence_start`。
#: 注：晚安期限只用到起点，终点不在本模块使用。


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

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        #: P5 余项（裁决⑩ 2026-10-11）：非目标类问候的按流持久化去重标记
        # （延迟加载；热重载后新实例重新读盘，即"重载后不再重发"）
        self._stream_greeting_marks: Optional[Dict[str, Any]] = None
        self._stop_event: asyncio.Event = asyncio.Event()
        # 时区在 [schedule_advanced]（2026-10-10 段拆分）
        self._tz_manager: TimezoneManager = TimezoneManager(
            section_or_default(plugin, "schedule_advanced", ScheduleAdvancedSectionConfig).timezone
        )
        # 2026-10-20 用户指示：原「已触发/待触发的主动发起记录」随活动切换主动发起链一并删除，
        # 仅剩下面的问候掷点与门槛缓存。
        # L2 早晚安漂移掷点（§3.2）：{(stream_id, date_str, kind): 掷点条目}
        # 纯内存、不落盘——热重载后允许重掷（附录 §3.2 明示口径）
        self._greeting_rolls: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        # L2 早/晚安其他私聊门槛（§3.4/B7）当日缓存：{(stream_id, date_str, kind): qualified}
        # （R7 依据 = 用户 2026-10-21 追加三直派③，计划书 §二十五 25.3：键加 kind——
        #  早/晚安各自的统计窗端点在各自主链首次评估时定死，互不共享）
        self._greeting_other_gate: Dict[Tuple[str, str, str], bool] = {}
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
    # 统一名单系统（2026-10-10 合并：群聊名单 + 私聊名单，各带模式开关）
    # ------------------------------------------------------------

    #: 名单元素可接受的前缀写法（剥前缀取号；B2 轮补：此前一律按非法忽略）
    _GROUP_ID_PREFIXES = ("qq:group:", "group:", "qq:group_", "group_")
    _PRIVATE_ID_PREFIXES = ("qq:private:", "private:", "qq:", "user:")

    @classmethod
    def _clean_id_list(cls, raw: Any, kind: str = "") -> Tuple[List[str], List[str]]:
        """清洗一份名单：只收**裸号**（纯数字）；返回 ``(合法号列表, 问题清单)``。

        前缀写法（``qq:group:123`` / ``group:123`` / ``qq:private:789`` / ``qq:789``）会**剥掉
        前缀取号**（``kind`` 决定认哪一组前缀：``"group"`` / ``"private"``）——B2 轮补：
        此前这些写法被当作非法元素**静默忽略**，用户填了黑名单却"看起来没生效"（黑名单变空 =
        全部放行）。跨类前缀（群号写进私聊名单等）仍按非法处理并告警。

        空串、含非数字、重复项、通配 ``*`` 按非法元素处理（调用方汇总成一条告警，不逐条刷屏）；
        通配在新语义下没有意义（黑名单留空即全放行、白名单留空即全不发）。
        """
        prefixes: Tuple[str, ...] = ()
        if kind == "group":
            prefixes = cls._GROUP_ID_PREFIXES
        elif kind == "private":
            prefixes = cls._PRIVATE_ID_PREFIXES
        values: List[str] = []
        problems: List[str] = []
        seen: set = set()
        for item in list(raw or []):
            text = str(item).strip()
            if not text:
                problems.append(f"空串（{item!r}）")
                continue
            number = text
            for prefix in prefixes:
                if text.lower().startswith(prefix):
                    number = text[len(prefix):].strip()
                    break
            if not number.isdigit():
                problems.append(f"{text}（非法：只收裸号或带 qq:group:/qq:private: 前缀的号，"
                                f"不含非数字字符）")
                continue
            if number in seen:
                problems.append(f"{text}（重复项）")
                continue
            seen.add(number)
            values.append(number)
        return values, problems

    def _target_qq(self) -> str:
        """目标陪伴对象 QQ（空/占位返回空串）。"""
        section = getattr(getattr(self._plugin, "config", None), "patrol", None)
        raw = str(getattr(section, "target_qq", "") or "").strip()
        return "" if raw in ("", "0", "123456789") else raw

    def _companion_enabled(self) -> bool:
        """陪伴模块是否启用（目标私聊的早/晚安由陪伴模块开关兜底；读不到按启用）。"""
        section = getattr(getattr(self._plugin, "config", None), "patrol", None)
        return bool(getattr(section, "enabled", True))

    def _note_list_problem(self, signature: Tuple[str, ...], problems: List[str]) -> None:
        """名单非法元素的**汇总**告警：一条 logger.warning + 一条决策日志；同签名每日一次。"""
        day = self._tz_manager.get_now().strftime("%Y-%m-%d")
        key = (day, signature)
        if key in self._greeting_list_warned:
            return
        # 跨天清理：只保留当天签名（避免内存无界增长）
        self._greeting_list_warned = {k: True for k in self._greeting_list_warned if k[0] == day}
        self._greeting_list_warned[key] = True
        detail = "；".join(problems)
        logger.warning(f"主动行为名单存在非法/重复元素（已忽略，不逐条刷屏）：{detail}")
        self._decision_log(
            "skip", trigger_type="proactive_list_parse",
            reason=f"名单存在非法元素（已忽略）：{detail}",
        )

    async def _resolve_group_streams(self) -> List[str]:
        """群聊生效会话：按「群聊名单 + 名单模式」解析。

        - 模式 = 黑名单（默认）：名单内的群**不**生效；留空 = 所有群生效；
        - 模式 = 白名单：只有名单内的群生效；留空 = 都不生效。
        """
        cfg = self._proactive_cfg()
        ids, problems = self._clean_id_list(getattr(cfg, "proactive_group_ids", None), "group")
        mode = str(getattr(cfg, "proactive_group_list_mode", LIST_MODE_BLACKLIST) or LIST_MODE_BLACKLIST)
        if problems:
            self._note_list_problem(tuple(["group"] + ids + problems), [f"群聊名单：{p}" for p in problems])

        if mode == LIST_MODE_WHITELIST:
            if not ids:
                return []
            candidates: List[Tuple[str, Optional[str]]] = [
                (gid, await self._resolve_group_id(gid)) for gid in ids
            ]
        else:
            excluded = set(ids)
            candidates = [
                (gid, sid) for gid, sid in await self._enumerate_group_pairs()
                if gid not in excluded
            ]
        return [sid for _gid, sid in candidates if sid]

    async def _resolve_private_streams(self) -> List[str]:
        """私聊生效会话：按「私聊名单 + 名单模式」解析。

        - 模式 = 白名单（默认）：只有名单内的 QQ 生效；留空 = 都不生效；
        - 模式 = 黑名单：名单内的 QQ 不生效；留空 = 全部生效。

        **目标陪伴对象始终按黑名单处理**（无论模式如何都剔除）：陪伴对象的私聊时段留给
        陪伴模块的巡检与早/晚安，日程侧的活动切换不进去抢话。
        """
        cfg = self._proactive_cfg()
        ids, problems = self._clean_id_list(getattr(cfg, "proactive_private_ids", None), "private")
        mode = str(getattr(cfg, "proactive_private_list_mode", LIST_MODE_WHITELIST) or LIST_MODE_WHITELIST)
        if problems:
            self._note_list_problem(tuple(["private"] + ids + problems),
                                    [f"私聊名单：{p}" for p in problems])
        target_qq = self._target_qq()

        if mode == LIST_MODE_WHITELIST:
            if not ids:
                return []
            pairs: List[Tuple[str, Optional[str]]] = [
                (uid, await self._resolve_private_id(uid)) for uid in ids
            ]
        else:
            excluded = set(ids)
            pairs = [(uid, sid) for uid, sid in await self._enumerate_private_pairs()
                     if uid not in excluded]

        result: List[str] = []
        for uid, sid in pairs:
            if not sid:
                continue
            if target_qq and uid == target_qq:
                continue  # 陪伴对象始终按私聊黑名单处理
            result.append(sid)
        return result

    async def _resolve_target_streams(self) -> List[str]:
        """主动行为生效的会话列表（活动切换用）：群聊生效集 ∪ 私聊生效集。"""
        streams: List[str] = []
        seen: set = set()
        for sid in (await self._resolve_group_streams()) + (await self._resolve_private_streams()):
            if sid and sid not in seen:
                streams.append(sid)
                seen.add(sid)
        return streams

    def _target_greeting_allowed(self) -> bool:
        """目标陪伴私聊是否允许问候（B2：严格遵循统一名单）。

        规则（与"活动切换恒剔除目标"相反——问候恰恰主要服务目标私聊）：

        - 私聊名单模式 = **白名单**（默认）：目标私聊**视为恒在名单内**（名单留空 = 其他私聊
          一概不主动，但目标照常问候，保住默认体验）；
        - 私聊名单模式 = **黑名单**：目标 QQ 写进名单即被排除（"配置为黑名单的聊天流仍触发
          早安"由此修复）。
        """
        cfg = self._proactive_cfg()
        ids, problems = self._clean_id_list(getattr(cfg, "proactive_private_ids", None), "private")
        if problems:
            self._note_list_problem(tuple(["private"] + ids + problems),
                                    [f"私聊名单：{p}" for p in problems])
        mode = str(getattr(cfg, "proactive_private_list_mode", LIST_MODE_WHITELIST)
                   or LIST_MODE_WHITELIST)
        target = self._target_qq()
        if mode == LIST_MODE_BLACKLIST and target and target in set(ids):
            return False
        return True

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

    # ------------------------------------------------------------
    # 主循环逻辑
    # ------------------------------------------------------------

    async def _check_and_act(self) -> None:
        """读当前活动 → 频率调控 + L2 早晚安漂移。

        闸序参照 autonomous_planning_plugin（MIT 参考实现，2026-10-20 用户指示引用）：
        G0 总开关 → G1 会话集合 → G3 睡眠窗（问候链内部）→ 触发即判（无排期池）。
        **2026-10-20 用户指示：日程主动行为（活动切换/约定主动开口，含非目标私聊与
        群聊主动发送）整条移除**——原排期池/触发窗口/游离主动触发已删；插件侧唯一的
        「主动发消息」来源只剩：早/晚安（强制「当日本流有过非 bot 消息」闸）与陪伴
        patrol（仅目标私聊）。频率调控不发送消息，单独保留。
        """
        cfg_p = self._proactive_cfg()
        if not (
            cfg_p.enable_frequency_modulation
            or self._greeting_kind_enabled(GREETING_MORNING)
            or self._greeting_kind_enabled(GREETING_NIGHT)
        ):
            return

        streams = await self._resolve_target_streams()
        now = self._tz_manager.get_now()
        current_min = now.hour * 60 + now.minute
        date_str = now.strftime("%Y-%m-%d")

        activity_info = self._find_current_activity()
        # M0 事件：活动切换判定处 → schedule.activity_ended / activity_started（附录 A ★）
        await self._publish_activity_transition(activity_info)

        # 1. 频率调控（每个 stream 独立，因子不变则跳过；不发送消息、无打扰面）
        if cfg_p.enable_frequency_modulation and activity_info is not None:
            _name, goal_type = activity_info[0], activity_info[1]
            await self._apply_frequency(streams, goal_type, name=_name)

        # 2. L2 早晚安漂移（§3.2/§3.3）：掷点复用 → 强制激活判定 → 触发/推迟/放弃
        await self._process_greetings(now, date_str)

    # 注（2026-10-20 用户指示）：原「活动切换/约定主动发起排期链」已删除——
    # _pending_triggers / _proactive_history / _fresh_window_seconds / _random_delay_seconds
    # / _trigger_proactive（活动与约定分支）与 [planning_proactive].enable_proactive_trigger
    # 配置键、活动/约定 intent 提示词键一并移除（间隙触发移交无对应插件路径；活动切换后不再有插件侧触发）。
    # 问候/频率调控照旧。

    # ------------------------------------------------------------
    # L2 早晚安漂移（附录 §三；裁决 B1/B3/B5/B6/B7）
    # ------------------------------------------------------------

    def _schedule_anchors(self) -> Tuple[int, int]:
        """``(wake_min, sleep_min)`` 作息锚点（``[schedule]``，缺省 07:00 / 23:00）。

        F16/B2：这两个锚点定义「新一天」的起止——早安基准取 wake（= 睡眠窗口结束），
        晚安无日程时回退 sleep 锚点（B4 只做兜底优先级，不强制两者同步）。

        §24.2 任务B（R7 依据 = 用户 2026-10-21 直派）：**真实作息锚点优先**——经
        companion.patrol 的同一 wiring 真源（``real_sleep_anchors_from_plugin``）读
        真实睡眠环（今晚/今晨睡觉条目，读取即解析；赖床顺延后锚点随移）；锚点不可得
        （纯配置对象桩 / ``_schedule_query`` 缺失 / DB 不可用 / 解析异常）→ 现行
        ``[schedule]`` 配置路径不变（§7.9 fail-safe 语义不变）。
        """
        try:
            from companion.patrol import real_sleep_anchors_from_plugin

            real = real_sleep_anchors_from_plugin(self._plugin)
            if real is not None and real.source == SOURCE_REAL_SCHEDULE:
                wake_min = _parse_hhmm_minutes(real.end, -1)
                sleep_min = _parse_hhmm_minutes(real.start, -1)
                if wake_min >= 0 and sleep_min >= 0:
                    return wake_min, sleep_min
        except Exception as exc:  # pragma: no cover - companion 未装配时的降级路径
            logger.debug(f"真实作息锚点解析不可用（{exc}），按配置路径降级")
        cfg = section_or_default(self._plugin, "schedule", ScheduleSectionConfig)
        wake_min = _parse_hhmm_minutes(getattr(cfg, "wake_time", ""), _DEFAULT_WAKE_MINUTES)
        sleep_min = _parse_hhmm_minutes(getattr(cfg, "sleep_time", ""), _DEFAULT_SLEEP_MINUTES)
        return wake_min, sleep_min

    # 注：原 _patrol_time 已随 2026-10-20 静默键移除而删除（唯一调用点是旧静默起点降级分支）。

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
        """观测窗期限（绝对时间戳，§3.2；2026-10-10 第二轮重定义为"激活观测上界"）。

        - **早安** = 睡眠结束后**第一个非睡眠日程条目的结束**（"第一段日程结束前有人说话"）；
          无日程 → 基准 + 120 分钟（实现裁量：不给无日程留无期限观察）；
        - **晚安** = 入睡锚点（``[schedule].sleep_time``；2026-10-20 起静默窗恒复用作息，
          原静默起点项与入睡项合一，见 :meth:`_silence_start_ts`）：
          既是激活观测上界，也是漂移点的硬界（不得在界后道晚安）。

        观测窗下界统一取 ``max(期限, 漂移点)``（见 :meth:`_roll_greeting`）——期限本身
        不再用于"到点前放弃"（旧版把它当放弃线，与静默窗叠加后会把问候整段吞掉）。
        """
        midnight = self._midnight_ts(now)
        if kind == GREETING_MORNING:
            first = self._find_first_non_sleep_activity_today(date_str)
            if first is None:
                # 实现裁量（§3.2 未定义此分支）：无非睡眠条目 → 基准 + 120 分钟；
                # 不给"无日程"留无期限观察，避免早安在任意时刻补发（防打扰）
                return baseline_ts + _MORNING_FALLBACK_DEADLINE_MINUTES * 60
            return midnight + int(first[2]) * 60
        # 晚安：入睡锚点 与 生效静默起点 取先到者（2026-10-21 §24.2：两者都经真实作息锚点
        # 解析——日程缺失回退 [schedule]；静默/入睡锚点非法时 _silence_start_ts 返回 0.0
        # → 观测窗关闭 = 宁不发（fail-closed），不回退成"可打扰"）
        _wake_min, sleep_min = self._schedule_anchors()
        return min(self._next_daily_anchor_ts(now, sleep_min), self._silence_start_ts(now))

    def _next_daily_anchor_ts(self, now: datetime, minute: int) -> float:
        """把「当天分钟数」锚点落成**今晚**的绝对时刻（§3.2 晚安的基准/期限分量）。

        参照点 = **今天的起床锚点**（``[schedule].wake_time``，F16 的"新一天起点"）：取最近一次
        不早于参照点的该锚点。常规作息（sleep > wake）得到今天的 ``sleep_time``/生效静默起点；
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
        """今晚静默时段开始的绝对时刻（**生效**静默起点，§3.2 期限列）。

        静默窗复用作息（2026-10-20 用户指示，原 N5「silence_start 留空跟随」口径随键删除收官）：
        生效起点恒 = ``[schedule].sleep_time``（与 companion/patrol.py 的巡检静默闸、qzone
        空间静默闸共用同一解析，见 :meth:`_effective_silence_start`）。

        同 :meth:`_next_daily_anchor_ts`（以起床锚点为参照）：如 23:00 → 今天 23:00。
        fail-safe：``[schedule].sleep_time`` 非法 → 返回 0（观测窗直接关闭，宁可不发），
        **不**回退成"可打扰"的边界猜测。
        """
        start_text, _start_key = self._effective_silence_start()
        start_min = _parse_hhmm_minutes(start_text, -1)
        if start_min < 0:
            logger.warning(
                f"静默起点 {start_text!r} 不是 HH:MM 格式；今晚晚安观测窗关闭（fail-safe，宁可不发）"
            )
            return 0.0
        return self._next_daily_anchor_ts(now, start_min)

    def _effective_silence_start(self) -> Tuple[str, str]:
        """生效静默起点 ``(HH:MM 原文, 来源配置键)``（单一真源 + 本地降级）。

        主路径 = ``companion.patrol.resolve_silence_window``（2026-10-20 用户指示后恒取
        ``[schedule].sleep_time``——静默窗复用作息；2026-10-21 §24.2 任务B 起插件实例路径
        **优先真实睡眠环锚点**，今晚入睡条目原文随作息动态同步，键留痕仍为
        ``schedule.sleep_time``）。companion 未装配（裁剪部署/桩环境）→ 本地按同一口径读
        ``[schedule].sleep_time``。两条路径都**不**把非法值改写成缺省时刻——非法由
        :meth:`_silence_start_ts` 按 fail-safe 关闭观测窗。
        """
        try:
            from companion.patrol import resolve_silence_window

            window = resolve_silence_window(self._plugin)
            return window.start, window.start_key
        except Exception as exc:  # pragma: no cover - companion 未装配时的降级路径
            logger.debug(f"静默窗生效值解析不可用（{exc}），按本地口径降级解析")
        cfg = section_or_default(self._plugin, "schedule", ScheduleSectionConfig)
        return str(getattr(cfg, "sleep_time", "") or ""), "schedule.sleep_time"

    # 注（2026-10-10 第二轮）：原 ``_in_silence_window`` 已删除——问候不再被静默时段拦截。
    # 原因：默认静默窗 00:00-07:30 与起床锚点 07:00 重叠，会把早安漂移整段吞掉（表现为
    # "每天固定在 07:30 开口、漂移失效"甚至"第一段日程结束在静默内 → 直接放弃不发"）。
    # 问候的必要性由"激活观测"（有人在该流说过话）把关，静默语义继续约束 [patrol] 巡检
    # （companion/patrol.py 的 is_in_time_window）。不要在这里重新引入静默拦截。

    def _greeting_drift(self, kind: str) -> Tuple[int, int]:
        """取该 kind 的漂移区间（非法/负数回退默认区间，§3.1/B3）。"""
        cfg_p = self._proactive_cfg()
        attr = "morning_drift" if kind == GREETING_MORNING else "night_drift"
        return _parse_drift(getattr(cfg_p, attr, None))

    def _roll_greeting(self, stream_id: str, category: str, kind: str, date_str: str,
                       now: datetime, now_ts: float) -> Dict[str, Any]:
        """掷点（§3.2）：每 ``(stream_id, date, kind)`` 只掷一次，同流同日重复决策复用同一结果。

        **漂移点**（2026-10-10 第二轮回正）：

        - 早安 = ``基准 + randint(a,b)``（醒来后 0~N 分钟；基准 = wake 锚点）；
        - 晚安 = ``期限 − randint(a,b)``（睡前 0~N 分钟；期限 = ``min(入睡锚点, 生效静默起点)``）。
          旧版晚安拿"最后一段日程结束"当参考做减法，最后一段恰好结束在入睡锚点时点会落在
          日程中段 = 提前发；锚在期限上则恒为"睡前随机点"，且永不超过入睡界。
        - **观测窗 = ``[min(基准, 漂移点), max(期限, 漂移点)]``**（两种问候同一公式）：窗内
          该流有人说话即"已激活"；窗下界保证漂移点永远落在窗内（旧版窗口比漂移点还早时
          结构性发不出）。

        纯内存、不落盘（热重载后允许重掷）；跨午夜条目按绝对时间戳处理。掷点时观测窗
        已整段关掉（``now ≥ 观测窗上界``）→ 直接记 ``abandoned``（当天不再评估该流该 kind）。
        """
        key = (stream_id, date_str, kind)
        entry = self._greeting_rolls.get(key)
        if entry is not None:
            return entry

        drift = self._greeting_drift(kind)
        baseline_ts = self._greeting_baseline_ts(kind, now, date_str)
        deadline_ts = self._greeting_deadline_ts(kind, now, date_str, baseline_ts)
        offset = float(random.randint(drift[0], drift[1])) * 60.0   # 漂移区间是分钟 → 秒
        # 漂移点：早安 = 基准 + offset（醒来后 0~N 分钟）；晚安 = 期限 − offset
        # （睡前 0~N 分钟；期限 = min(入睡锚点, 生效静默起点)）。两个方向都保证"点"落在
        # 「白天锚点之间」，不会跨过入睡界（旧版拿最后一段日程结束当参考，最后一段恰好
        # 结束在入睡锚点时点会落在日程中间 = 提前发；锚在期限上则恒为睡前随机点）。
        fire_at = baseline_ts + offset if kind == GREETING_MORNING else deadline_ts - offset
        # 观测窗：[min(基准, 漂移点), max(期限, 漂移点)]——两种问候同一公式，且保证漂移点
        # 永远落在窗内（否则会出现"窗口比漂移点还早、结构性发不出"）
        observe_from = min(baseline_ts, fire_at)
        observe_until = max(deadline_ts, fire_at)
        entry = {
            "stream_id": stream_id,
            "category": category,
            "kind": kind,
            "date": date_str,
            "drift": drift,
            "rolled": True,
            "baseline": baseline_ts,
            "deadline": deadline_ts,
            "observe_from": observe_from,
            "observe_until": observe_until,
            "fire_at": fire_at,
            "status": GREETING_STATUS_PENDING,
            "reason": "",
        }
        if now_ts > observe_until:
            entry["status"] = GREETING_STATUS_ABANDONED
            entry["reason"] = "window_closed"
            logger.debug(
                f"问候窗口已过（不排期）: stream={stream_id}, kind={kind}, date={date_str}"
            )
        else:
            logger.info(
                f"问候掷点: stream={stream_id}, kind={kind}, category={category}, "
                f"drift={drift[0]}-{drift[1]}, fire_at={int(fire_at)}, "
                f"observe_until={int(observe_until)}"
            )
        self._greeting_rolls[key] = entry
        return entry

    def _greeting_kind_enabled(self, kind: str) -> bool:
        """早/晚安总开关（2026-10-10 起由两个开关统一控制，取代原三类会话开关矩阵）。"""
        attr = "greeting_morning_enabled" if kind == GREETING_MORNING else "greeting_night_enabled"
        return bool(getattr(self._proactive_cfg(), attr, True))

    def _greeting_requires_activation(self, cfg_p: Any) -> bool:
        """强制激活闸：恒 True（2026-10-20 用户指示）。

        规则：**当日该流有过非 bot 消息才问候**，宁不发不错发；因此不提供配置项——
        原 ``[planning_proactive].greeting_require_activation`` 键已删除（2026-10-20 用户指示）。
        参数 ``cfg_p`` 仅为调用点兼容保留，不再读取。

        沿革：2026-10-10 第二轮回正时本闸由配置控制、且所有会话一视同仁（含目标私聊，
        此前只有群聊需要激活）；2026-10-20 用户指示起强制化，不再有「到点即发」分支。
        """
        return True

    async def _other_private_qualified(self, stream_id: str, date_str: str, kind: str,
                                       now: datetime, cfg_p: Any) -> bool:
        """其他私聊问候门槛：统计窗内该流消息数 ≥ ``greeting_other_private_min_messages``。

        统计窗 = **[昨日 00:00 → 该类问候开始处理的时刻)**（R7 依据 = 用户 2026-10-21
        追加三直派③，计划书 §二十五 25.3）——不再是旧的「[昨日00:00, 今日00:00)」：

        - **早安** = 用户原话口径：端点 = 早安链开始处理该流的时刻（即首次评估时的
          ``now``），用户**当天凌晨发的消息计入**；
        - **晚安**按同公式：端点 = 晚安链开始处理时刻，**当天白天/傍晚消息亦计入**
          （裁量登记，用户可否决为保持旧窗）。

        端点在**首次评估时定死**：判定结果连同三元组键 ``(stream_id, date_str, kind)``
        进当日缓存，后续 tick 直接取缓存值，窗口不随墙钟（后续 tick 的 ``now``）漂移。
        经 ``get_by_time_in_chat``（``limit=门槛`` 即够判定）；能力不可用/异常/返回形态
        不可解析 → 按 0 处理（**不发送**，兜底不变）。
        """
        cache_key = (stream_id, date_str, kind)
        if cache_key in self._greeting_other_gate:
            return self._greeting_other_gate[cache_key]

        try:
            need = max(5, int(getattr(cfg_p, "greeting_other_private_min_messages", 5) or 5))
        except (TypeError, ValueError):
            need = 5

        qualified = False
        ctx_obj = getattr(self._plugin, "ctx", None)
        if ctx_obj is not None and hasattr(ctx_obj, "message"):
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            yesterday_start = midnight - timedelta(days=1)
            window_end = now  # 窗口端点 = 该类问候首次评估的 now（§25.3；缓存吸收后续 tick 时钟漂移）
            try:
                msgs = await ctx_obj.message.get_by_time_in_chat(
                    stream_id, str(yesterday_start.timestamp()), str(window_end.timestamp()),
                    limit=need,
                )
            except Exception as exc:
                logger.debug(f"查询门槛统计窗消息失败（按 0 处理）: stream={stream_id}, {exc}")
                msgs = None
            if isinstance(msgs, dict):
                msgs = msgs.get("messages") or msgs.get("data") or []
            if isinstance(msgs, list):
                qualified = len(msgs) >= need

        self._greeting_other_gate[cache_key] = qualified
        return qualified

    async def _stream_active_today(self, stream_id: str, since_ts: float,
                                   until_ts: float) -> bool:
        """该流在「今日」是否有人发过消息（问候激活闸的唯一判定，2026-10-20 用户指示）。

        口径：

        - **起点** = ``min(since_ts, 当日 00:00)``——当日零点按 ``self._tz_manager.get_now()``
          的本地时区换算成 epoch 秒（不直接用系统时区，避免宿主时区与插件配置不一致）；
          观测起点早于零点（夜猫子作息的晚安漂移点落在凌晨）时仍取更早者，保证不把
          「凌晨已激活」误判成未激活。
        - **查询** = ``ctx.message.get_by_time_in_chat(stream_id, start, until_ts, limit=10,
          filter_mai=True)``；``filter_mai`` 为宿主较新参数，抛 ``TypeError``/其他异常时**重试一次
          不带该参数**（双保险，旧宿主也能用）。
        - **过滤**：取每条消息的 ``processed_plain_text``，剔除空白与以 ``/`` 开头的命令消息
          （bot 自己的发言由 ``filter_mai`` 排除；兜底再靠命令过滤，避免「只剩指令」也算激活）。
        - **fail-closed**：能力不可用、查询异常、返回形态不可解析 → ``False``（宁不发不错发）。
        """
        ctx_obj = getattr(self._plugin, "ctx", None)
        if ctx_obj is None or not hasattr(ctx_obj, "message"):
            return False

        # 当日零点（本地时区）→ epoch 秒
        try:
            local_now = self._tz_manager.get_now()
            midnight_ts = local_now.replace(
                hour=0, minute=0, second=0, microsecond=0
            ).timestamp()
            start_ts = min(float(since_ts), midnight_ts)
        except Exception as exc:
            logger.debug(f"计算今日起点失败（按未激活处理）: {exc}")
            return False

        getter = getattr(ctx_obj.message, "get_by_time_in_chat", None)
        if not callable(getter):
            return False
        msgs: Any = None
        try:
            msgs = await getter(
                stream_id, str(start_ts), str(until_ts), limit=10, filter_mai=True,
            )
        except Exception as exc:
            # 双保险：旧宿主不认识 filter_mai → 不带该参数重试一次
            logger.debug(f"带 filter_mai 查询会话消息失败，改为不带重试: {exc}")
            try:
                msgs = await getter(stream_id, str(start_ts), str(until_ts), limit=10)
            except Exception as exc2:
                logger.debug(f"查询会话消息失败（按未激活处理）: {exc2}")
                return False

        if isinstance(msgs, dict):
            msgs = msgs.get("messages") or msgs.get("data") or []
        if not isinstance(msgs, list):
            return False
        for msg in msgs:
            if isinstance(msg, dict):
                text = msg.get("processed_plain_text")
            else:
                text = getattr(msg, "processed_plain_text", None)
            if not isinstance(text, str):
                continue
            text = text.strip()
            if not text or text.startswith("/"):
                continue
            return True
        return False

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
        """枚举已知私聊流的 ``(QQ 号, session_id)``（私聊名单为**黑名单模式**时使用：
        全量枚举后剔除名单号，见 :meth:`_resolve_greeting_candidates`；白名单模式按名单
        逐号解析，不走本枚举）。"""
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

    async def stream_display_labels(self) -> Dict[str, str]:
        """诊断用「session_id → 显示名」映射（R7 依据 = 用户 2026-10-21 追加三直派①计划书 §二十五）。

        供 ``/lbm_diag streams``（companion/components.py ``_mai_diag_streams_records``，
        §25.2 起三块 = 三节点）把纯 sid 展示位换成可读名称：单次 ``ctx.chat.get_all_streams(platform="qq")`` 全量枚举，
        每流按 ``is_group_session`` 真值或 ``group_id`` 非空判型——群流取 ``group_name``；
        私聊流 ``user_cardname`` 优先、``user_nickname`` 次之（均 strip 后**非空才用**）。
        名称不可得 → 该流**不入映射**（调用方兜底回显原 session_id）。

        枚举解包形态与 :meth:`_enumerate_group_pairs` 同型（dict → streams/data → list）；
        整方法 try 兜底：能力缺失/异常/形态不可解析 → 返回空 dict（调用方全部回显 sid）。
        纯只读，不发送业务消息、不调 LLM（§22.1 诊断契约不变）。
        """
        labels: Dict[str, str] = {}
        try:
            ctx_obj = getattr(self._plugin, "ctx", None)
            if ctx_obj is None or not hasattr(ctx_obj, "chat"):
                return {}
            streams_raw = await ctx_obj.chat.get_all_streams(platform="qq")
            if isinstance(streams_raw, dict):
                streams_raw = streams_raw.get("streams") or streams_raw.get("data") or []
            if not isinstance(streams_raw, list):
                return {}
            for stream in streams_raw:
                if not isinstance(stream, dict):
                    continue
                session_id = str(stream.get("session_id") or stream.get("stream_id") or "").strip()
                if not session_id:
                    continue
                # 群 = is_group_session 真值或 group_id 非空；名称不可得不入映射
                is_group = bool(stream.get("is_group_session")) or bool(
                    str(stream.get("group_id") or "").strip())
                if is_group:
                    display = str(stream.get("group_name") or "").strip()
                else:
                    display = str(stream.get("user_cardname") or "").strip()
                    if not display:
                        display = str(stream.get("user_nickname") or "").strip()
                if display:
                    labels[session_id] = display
        except Exception as exc:
            logger.debug(f"构建流显示名映射失败（诊断按纯 sid 回显）: {exc}")
            return {}
        return labels

    async def _resolve_greeting_candidates(self, cfg_p: Any) -> List[Tuple[str, str]]:
        """问候候选：目标私聊 ∪ 群聊（名单）∪ 其他私聊（名单 + 门槛）。

        2026-10-10 第二轮回正（B2）：**严格遵循统一名单**，不再有任何"隐藏兼容位"开关：

        - **目标陪伴私聊**：陪伴模块（``[patrol].enabled``）启用、目标会话已解析、
          且**未被私聊黑名单排除**（白名单模式下视为恒在名单内；黑名单模式下把目标 QQ
          写进 ``proactive_private_ids`` 即可停掉它的问候）时生效；
        - **群聊**：走统一群聊名单（``proactive_group_ids`` + 模式；默认黑名单 + 留空 = 全群）；
        - **其他私聊**：走统一私聊名单（``proactive_private_ids`` + 模式；默认白名单 + 留空 = 全不发），
          另需前一日消息数门槛（``greeting_other_private_min_messages``）。
        """
        candidates: List[Tuple[str, str]] = []
        seen: set = set()

        target = str(getattr(self._plugin, "_stream_id", "") or "").strip()
        if target and self._companion_enabled() and self._target_greeting_allowed():
            candidates.append((target, GREETING_CATEGORY_TARGET))
            seen.add(target)

        for sid in await self._resolve_group_streams():
            if sid and sid not in seen:
                candidates.append((sid, GREETING_CATEGORY_GROUP))
                seen.add(sid)

        for sid in await self._resolve_private_streams():
            if sid and sid not in seen:
                candidates.append((sid, GREETING_CATEGORY_OTHER))
                seen.add(sid)

        return candidates

    async def _process_greetings(self, now: datetime, date_str: str) -> None:
        """L2 每轮：候选枚举 → 掷点/复用 → 到点激活判定 → 触发/推迟/放弃。

        早/晚安由两个总开关（``greeting_morning_enabled`` / ``greeting_night_enabled``）控制，
        候选名单见 :meth:`_resolve_greeting_candidates`；两类开关全关时本方法直接返回。
        """
        cfg_p = self._proactive_cfg()
        if not (self._greeting_kind_enabled(GREETING_MORNING)
                or self._greeting_kind_enabled(GREETING_NIGHT)):
            return
        now_ts = _epoch_ts(now)
        for stream_id, category in await self._resolve_greeting_candidates(cfg_p):
            for kind in (GREETING_MORNING, GREETING_NIGHT):
                if not self._greeting_kind_enabled(kind):
                    continue
                await self._evaluate_greeting(stream_id, category, kind,
                                              now, now_ts, date_str, cfg_p)
        # 跨天残留清理：当天条目保留；跨天条目只在「已终结（fired/abandoned）或观测窗已整段过去」
        # 时清掉——夜猫子作息的晚安漂移点落在次日凌晨，条目必须活到那一刻（T3 问题表 #1）。
        for key, entry in list(self._greeting_rolls.items()):
            if key[1] == date_str:
                continue
            terminated = entry.get("status") in (GREETING_STATUS_FIRED, GREETING_STATUS_ABANDONED)
            if terminated or now_ts > float(entry.get("observe_until", 0) or 0):
                self._greeting_rolls.pop(key, None)
        # 门槛缓存跨天清理：键含 kind（§25.3 R7 三元组键），按第 2 位日期字段清即可。
        for gkey in [k for k in self._greeting_other_gate if k[1] != date_str]:
            self._greeting_other_gate.pop(gkey, None)

    async def _evaluate_greeting(self, stream_id: str, category: str, kind: str,
                                 now: datetime, now_ts: float, date_str: str,
                                 cfg_p: Any) -> None:
        """单个候选的状态机：**掷点 → 激活观测 → 到点（或已过点立即）发 / 窗关放弃**。

        2026-10-10 第二轮（用户口径重做）：

        - 激活观测窗 = ``[baseline, observe_until]``（``observe_until`` 见 :meth:`_roll_greeting`），
          该流「当日」（本地 00:00 起；观测起点更早则取观测起点，见
          :meth:`_stream_active_today`）有过非 bot、非命令消息即「已激活」——**不分会话类型**
          （目标私聊同样要看激活）；
        - 已激活后：未到 ``fire_at`` → 等待下一 tick；到点（或观察到时已过点）→ **立即发**，
          不再因"超过期限/落在静默时段"二次拦截（满载漂移语义：点就是点）；
        - 观测窗整段结束仍无人说话 → 放弃当天该流该 kind（记 ``deadline``）；
        - **激活闸恒开**（2026-10-20 用户指示，不提供配置项）：不再有「到点即发」分支，
          判定口径见 :meth:`_stream_active_today`（当日非 bot、非命令消息）；若观测窗已整段
          错过（如重启跨过整窗）→ 放弃，不深夜补发。``_advance_greeting`` 里保留的 else
          分支仅为结构兼容，正常路径不会进入。

        其他私聊另有前一日消息数门槛（``greeting_other_private_min_messages``）；
        目标私聊的准入在候选枚举里按统一名单判定（:meth:`_target_greeting_allowed`）。
        """
        if not self._greeting_kind_enabled(kind):
            return  # 总开关（候选枚举已按开关过滤，这里再判一次保证直调路径同样生效）
        if category == GREETING_CATEGORY_TARGET:
            if not self._companion_enabled():
                return
        elif category != GREETING_CATEGORY_GROUP:
            if not await self._other_private_qualified(stream_id, date_str, kind, now, cfg_p):
                return

        # 跨夜存活的历史条目先推进：条目按「掷点日」key 化，夜猫子作息（sleep < wake）的晚安
        # 漂移点落在次日凌晨，今天重掷不会替它走完流程（T3 问题表 #1：旧版跨天清理把它直接丢掉，
        # 表现为晚安永不发送）。
        for key, prev in list(self._greeting_rolls.items()):
            if key[0] == stream_id and key[2] == kind and key[1] != date_str:
                await self._advance_greeting(prev, now_ts, date_str, cfg_p)

        entry = self._roll_greeting(stream_id, category, kind, date_str, now, now_ts)
        await self._advance_greeting(entry, now_ts, date_str, cfg_p)

    async def _advance_greeting(self, entry: Dict[str, Any], now_ts: float,
                                date_str: str, cfg_p: Any) -> None:
        """推进一条掷点条目：已终结 → 跳过；未激活 → 等待/窗关放弃；激活后到点（或已过点）→ 发。

        投递同一条目的调用点有两处（见 :meth:`_evaluate_greeting`）：今天新掷的条目，
        以及**跨夜存活的历史条目**（夜猫子作息 sleep < wake 时晚安漂移点落在次日凌晨，
        条目按掷点日 key 化，今天再掷不会替它走完流程——T3 问题表 #1）。

        激活判定即**最新判定**：每 tick 都按当前时间线重新查一次「今日该流是否有非 bot、
        非命令消息」，触发前必然复核过 → 与参考插件 G9 的「双阶段复核」同形（判定与投递
        之间不再有信任间隙），2026-10-20 用户指示。
        """
        if entry["status"] in (GREETING_STATUS_FIRED, GREETING_STATUS_ABANDONED):
            return
        observe_until = entry.get("observe_until", entry["deadline"])
        if self._greeting_requires_activation(cfg_p):
            activated = await self._stream_active_today(
                entry["stream_id"], entry.get("observe_from", entry["baseline"]), now_ts)
            if not activated:
                if now_ts > observe_until:
                    self._abandon_greeting(entry, "deadline")
                return  # 窗内暂时无人说话 → 推迟到下一 tick 继续观察
            if now_ts < entry["fire_at"]:
                return  # 已激活但未到点 → 等待漂移点
        else:
            if now_ts < entry["fire_at"]:
                return
            if now_ts > observe_until:
                # 不看激活时，观测窗就是"补发窗口"：整段错过（如重启）→ 不深夜补发
                self._abandon_greeting(entry, "window_closed")
                return
        if (self._greeting_deduped_by_persisted_marker(entry["category"], entry["kind"])
                or self._stream_greeting_marked(entry["stream_id"], entry["kind"], entry["date"])):
            # P5（2026-10-09 登记）：热重载/重启后内存掷点清零，但 affection 的按天标记仍在——
            # 目标私聊该 kind 今天已发过 → 视为已触发（与自然触发同形态）+ 落去重行，不再重发。
            # P5 余项（裁决⑩ 2026-10-11）：非目标类（群聊/其他私聊）同样按流落盘去重——
            # 内存掷点被热重载清零后不再向同一群/私聊重发同一天的同 kind 问候。
            # 内存 status 已 FIRED 的在上方短路，不会走到这里（不重复落去重行）。
            entry["status"] = GREETING_STATUS_FIRED
            self._decision_log(
                "skip", trigger_type=f"{entry['kind']}_greeting",
                reason="持久化标记命中，热重载去重",
                stream_id=entry["stream_id"], category=entry["category"],
                fire_at=int(entry["fire_at"]), deadline=int(entry["deadline"]),
            )
            return
        await self._fire_greeting(entry, date_str, now_ts)

    #: 按流问候去重标记的落盘文件名（P5 余项/裁决⑩）
    _GREETING_MARKS_FILE = "greeting_stream_marks.json"

    def _stream_marks_path(self) -> Optional[Path]:
        """去重标记文件路径（data_dir 不可得 → None，调用方降级为纯内存）。"""
        data_dir = getattr(self._plugin, "_data_dir", None)
        return Path(data_dir) / self._GREETING_MARKS_FILE if data_dir else None

    def _load_stream_marks(self) -> Dict[str, Any]:
        """按流去重标记（实例内缓存一次；热重载后新实例重新读盘）。"""
        if self._stream_greeting_marks is None:
            path = self._stream_marks_path()
            data = load_json(path, {}) if path else {}
            marks = data.get("marks") if isinstance(data, dict) else None
            self._stream_greeting_marks = marks if isinstance(marks, dict) else {}
        return self._stream_greeting_marks

    def _stream_greeting_marked(self, stream_id: str, kind: str, date_str: str) -> bool:
        """该流该 kind 在该掷点日是否已发过（持久化标记；文件不可用 → False 可空降级）。"""
        marks = self._load_stream_marks()
        try:
            return bool(marks.get(str(stream_id), {}).get(str(date_str), {}).get(kind))
        except Exception:
            return False

    def _mark_stream_greeting(self, stream_id: str, kind: str, date_str: str) -> None:
        """写按流去重标记并落盘（保留最近 3 个掷点日，防无限增长；失败仅告警不打断）。"""
        marks = self._load_stream_marks()
        try:
            per_stream = marks.setdefault(str(stream_id), {})
            per_day = per_stream.setdefault(str(date_str), {})
            per_day[kind] = True
            try:
                cutoff = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")
            except (TypeError, ValueError):
                cutoff = None
            if cutoff:
                for stream_key in list(marks):
                    per_stream_marks = marks[stream_key]
                    for stale_day in [d for d in list(per_stream_marks) if d < cutoff]:
                        per_stream_marks.pop(stale_day, None)
                    if not per_stream_marks:
                        marks.pop(stream_key, None)
        except Exception:
            logger.debug("写按流问候标记失败（忽略）", exc_info=True)
            return
        path = self._stream_marks_path()
        if path is not None:
            try:
                save_json_atomic(path, {"version": 1, "marks": marks})
            except Exception:
                logger.debug("落盘按流问候标记失败（忽略）", exc_info=True)

    def _greeting_deduped_by_persisted_marker(self, category: str, kind: str) -> bool:
        """TARGET 类问候的跨重载去重：查 affection 的按天标记（``morning_sent``/``night_sent``）。

        P5（2026-10-09 登记）：标记的语义域 =「目标私聊该 kind 今日已发」（**按天全局布尔**，
        非按流；日切清零见 affection），故只对 TARGET 类别查——群聊/其他私聊查全局标记会把
        「目标已发」误判为「该流已发」，直接压制三类矩阵的多群全发语义（既有 128 号用例）。
        companion 未装配（``_affection`` 缺失）或标记接口不可调 → 返回 False（可空降级，同
        :meth:`_mark_greeting_sent_on_affection`）。
        """
        if category != GREETING_CATEGORY_TARGET:
            return False
        affection = getattr(self._plugin, "_affection", None)
        if affection is None:
            return False
        getter = getattr(
            affection,
            "morning_sent" if kind == GREETING_MORNING else "night_sent",
            None,
        )
        if not callable(getter):
            return False
        try:
            return bool(getter())
        except Exception:
            logger.debug("读 affection 早晚安标记失败", exc_info=True)
            return False

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
        """触发一次问候（一律走同一条主动链；例行问候始终免预算）。"""
        stream_id, kind = entry["stream_id"], entry["kind"]
        ok = await self._trigger_greeting(stream_id, kind, date_str)
        if not ok:
            # 宿主触发失败：状态不变，下一轮继续尝试（到期限自然放弃）
            return
        entry["status"] = GREETING_STATUS_FIRED
        entry["fired_at"] = now_ts
        # P5 余项（裁决⑩ 2026-10-11）：所有类别按流落盘去重标记（重载后不重发）
        self._mark_stream_greeting(stream_id, kind, entry.get("date", date_str))
        if entry["category"] == GREETING_CATEGORY_TARGET:
            # 目标私聊写 affection 每日标记，patrol 见已发即不再各发一条
            self._mark_greeting_sent_on_affection(kind)
        self._decision_log(
            "trigger", trigger_type=f"{kind}_greeting",
            reason=self._greeting_reason(kind),
            stream_id=stream_id, category=entry["category"],
            drift=f"{entry['drift'][0]}-{entry['drift'][1]}",
            fire_at=int(entry["fire_at"]), deadline=int(entry["deadline"]),
        )

    def _greeting_reason(self, kind: str) -> str:
        """问候的触发标注（进主动任务块，标明为什么开口；可在 [prompts] 段改文案）。"""
        if kind == GREETING_MORNING:
            return prompt(self._plugin, "proactive_reason_morning")
        return prompt(self._plugin, "proactive_reason_night")

    async def _trigger_greeting(self, stream_id: str, kind: str, date_str: str) -> bool:
        """走统一主动链触发一次早晚安问候（intent 提示词按 kind 分派）。

        **例行问候始终免预算**（2026-10-10：原 ``greeting_exempt_budget`` 开关删除、恒为免预算）——
        闸门与计数一起跳；陪伴侧的巡检/想念才走预算。
        """
        if kind == GREETING_MORNING:
            first = self._find_first_non_sleep_activity_today(date_str)
            if first is not None:
                intent_prompt = render(
                    prompt(self._plugin, "proactive_intent_morning"),
                    fallback=PROACTIVE_INTENT_MORNING_DEFAULT,
                    activity_name=first[0],
                )
            else:
                intent_prompt = prompt(self._plugin, "proactive_intent_morning_no_activity")
            reason = self._greeting_reason(GREETING_MORNING)
        else:
            intent_prompt = prompt(self._plugin, "proactive_intent_night")
            reason = self._greeting_reason(GREETING_NIGHT)
        return await self._call_proactive_trigger(
            stream_id=stream_id,
            intent_prompt=intent_prompt,
            reason=reason,
            priority="normal",
            exempt_budget=True,
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

    # 注（§24.2 任务B，2026-10-21）：原 ``_in_sleep_window`` 已删除——零调用死代码
    # （盘点依据 §24.0；R7 依据 = 计划书 §24.2 用户指示）。真实作息锚点落地后
    # 保留该副本只会成为"配置口径假源"，睡眠环判定统一收敛到
    # planning/schedule_anchors.py（连锁消费统一经 patrol 的真源入口）。

    def _find_current_activity(self) -> Optional[Tuple[str, str, int, int, Dict[str, Any]]]:
        """从 GoalManager 找当前正在进行的活动。

        N1 读取侧（2026-10-10）：跨午夜窗口统一走 ``time_utils.is_in_day_frame``（同一个 24h
        环，唯一实现）——``end > 1440`` 的条目（如 23:00 入睡 + 8h ⇒ 1380→1860）在凌晨
        00:00-11:59 也能被查到"正在进行"；不再各自做 ``if end > 1440 and current_min < 720:
        end -= 1440``（该写法在 00:30 会把窗口错误地折成 [1380, 420) → 恒不命中）。
        返回的 ``end`` 原样保留 >1440（与 ``schedule_items.item_end_minute`` 同一口径）。

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
                if is_in_day_frame(start, end, current_min):
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

    # 注（2026-10-20 用户指示）：原触发窗口/随机延迟助手已随活动切换主动发起链移除。

    # ------------------------------------------------------------
    # 频率调控
    # ------------------------------------------------------------

    async def _apply_frequency(self, streams: List[str], goal_type: str, name: str = "") -> None:
        """按日程属性映射频率因子并对每个 stream 应用（因子不变则跳过）。

        §24.3 任务C（2026-10-21）：因子改经 :func:`activity_properties.resolve_property`
        按「日程属性」取——`goal_type` 精确命中优先、条目 name 含关键词次之、
        ``default_property`` 兜底；每流按 ``_current_factor`` 缓存机制不变。因子值本身
        来自 ``mappings/schedule_properties.json``（装载/热重载读一遍，改 json 即调）；
        本模块不再持有任何频率字面量。
        """
        property_spec = resolve_property(goal_type, name)
        target_factor = property_spec.reply_factor
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
                f"频率调控: goal_type={goal_type}, name={name}, property={property_spec.id}, "
                f"factor={target_factor}, 已应用到 {applied} 个会话"
            )
        if failed:
            logger.debug(
                f"频率调控失败: goal_type={goal_type}, name={name}, property={property_spec.id}, "
                f"factor={target_factor}, 共 {failed} 个"
            )

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
        默认 ``False``：非问候主动行为照旧「查预算 → 记用量 → 触发」，计数身份按 stream
        维度判定（M11 §3.3）——目标流已解析时只有发往目标陪伴私聊的主动打扰才查计数/入账，
        群聊与其他私聊开口不计入、不受限；**目标流不可得/未解析时**预算退回全量口径：
        任何流的开口都会查计数与被拦截（沿用旧语义，判据见 ``core/event_bus._is_target_stream``）。
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
        # M11（附录 II §3.3）：预算计数身份 = **目标陪伴私聊**——按 stream 维度传参。
        # 注意（#26 口径）：目标流不可得/未解析时预算按**全量口径**计数与拦截（沿用旧语义，
        # 见 core/event_bus._is_target_stream 的回退分支），此时群聊/其他私聊的活动切换开口
        # 同样会被计数；目标流已解析后群聊/其他私聊开口才不计入。
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

    # 注（2026-10-20 用户指示）：原 _trigger_proactive（活动切换/约定主动开口的指令模板组装
    # 与触发，'commitment_due' / 'activity_switch' 两类 trigger_type）已删除；
    # 问候链仍经 _call_proactive_trigger，PROACTIVE_REASON_* 中活动/约定文案随键移除。
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

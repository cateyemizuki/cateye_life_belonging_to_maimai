"""活动驱动的主动行为服务（来源：自主规划插件 services/proactive_service.py，移植版）。

三个职责合并在一个 60s 轮询的后台循环里：

1. **问候（主动开口的唯一插件侧入口）**——2026-10-23 §31.1 闸门矩阵（R7 = 用户第二轮
   直派，冻结口径 = 计划书 §三十一）：全部时间锚从「日历日（0 点/date_str）」迁移到
   「bot 日程周期边界」（``planning/cycle_state`` 四变量），三流分守：

   - **群聊**：早安 = 底层硬闸（无条件）——判定窗 ``[上份日程开始, 本份日程开始)``
     窗内非 bot、非斜杠命令消息 ≥1，不过 = 直接跳过（不掷点、不建条目，决策日志留痕）；
     ``greeting_require_activation`` 开 → 额外判定「今天日程第一个条目时段内非 bot 消息
     ≥1」（AND）。晚安 = **硬性连锁**：本群早安本轮已开口（marks v2）才可开口；
   - **其他私聊**：早安阈值 = 判定窗内对方发送 ≥ ``greeting_other_private_min_messages``；
     晚安 = OR（连锁桶 ∨ 判定窗 ``[本轮日程开始, 晚安结束)`` 对方发送 ≥
     ``greeting_private_night_min_messages``）；窗内任意 tick 达标即通过、到点发；
   - **目标私聊**：**无条件**（原激活强制随 §31.1 解除，用户 2026-10-23 裁定），只看
     ``[patrol].enabled`` + 名单 + 既有掷点/到点/去重流程。

   通则（§31.1）：①「不得界后道早安/晚安」——需判定分支补 ``now > observe_until →
   abandon(deadline)`` 钳制；②**补发冷静期**：过掷点才满足条件的补发需 bot 本会话最近
   一条消息（``is_mai=真``，经 ``get_by_time_in_chat``）距今 ≥
   ``schedule_advanced.greeting_min_silence_minutes`` 才开口，窗内一直不满足 → 到期仍弃；
   到点前已满足的正常触发不受冷静期约束；③ 四类 open 判定的统计一律只用非 bot 信息。

   - **marks v2（连锁桶）**：``greeting_stream_marks.json`` 升版 ``version=2``——按流按
     kind 记**最新已发 cycle_start**（唯一值替换）；旧 v1 数据视为不存在（去重语义 =
     同流同 kind 同周期只发一次）；
   - **无日程 fail-safe**：四变量缺失/非法 → ``planning.cycle_state.load_or_anchored``
     回退 ``[schedule]`` 两键构造本周期边界（anchored 标记，连锁按「本轮无早安」处理）；
   - **晚安期限 = current_cycle_end**（替代旧 min(真实入睡锚点, 静默起点)——两项并入
     cycle_end 真源，§31.1；``_silence_start_ts``/``_next_daily_anchor_ts`` 原实现删除）。

2. **L2 早晚安漂移（M11 → §31.1 重做）**——早/晚安只配漂移区间（每流每周期独立掷点），
   执行顺序 = 掷点 → 分支闸门 → 到点（或过点才达标补发）发：

   - **基准点**：早安 = 本份日程开始（= current_cycle_start，经周期四变量）；
     晚安基准同锚（夜火点 = 晚安期限 − 随机漂移）；
   - **期限**：早安 = 本周期日期帧内第一个非睡眠条目结束（无日程 → 基准 + 120 分钟）；
     晚安 = **current_cycle_end**（入睡时刻）；
   - **判定窗**：早安 = ``[prev_cycle_start, cur_cycle_start)``；晚安 =
     ``[cur_cycle_start, 晚安期限)``——**全部经周期变量**，不再看日历 00:00；
   - **静默时段不拦截问候**（2026-10-10 第二轮口径保留）：静默窗真源 =
     ``companion/patrol.py:resolve_silence_window``，约束巡检侧，不约束问候链。

   候选矩阵：目标陪伴私聊（``[patrol].enabled`` 且未被私聊黑名单排除）；群聊与其他私聊
   走统一名单（``proactive_group_ids`` / ``proactive_private_ids`` + 各自模式开关）。
   开/关由 ``greeting_morning_enabled`` / ``greeting_night_enabled`` 统一控制。
   **例行问候始终免预算**：早/晚安触发既不查预算闸门也不计主动打扰用量。

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
from datetime import datetime
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
from .cycle_state import (CycleState, anchored_state, day_cycle_epochs,
                          load_or_anchored)
from .goal_manager import get_goal_manager
from .time_utils import (TimezoneManager, epoch_to_local, is_in_day_frame,
                         parse_time_window, tzinfo_of)
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
#: 覆盖类别（§3.4/B7；2026-10-22 §二十七 分支闸门分离）：目标私聊（激活强制）/
#: 群聊（必要性配置 + 今日非 bot 消息硬门槛）/ 其他私聊（「对方发送」条数阈值恒开口）
GREETING_CATEGORY_TARGET = "target"
GREETING_CATEGORY_GROUP = "group"
GREETING_CATEGORY_OTHER = "other"
#: 掷点状态机的状态取值（§3.2/§3.3）
GREETING_STATUS_PENDING = "pending"
GREETING_STATUS_FIRED = "fired"
GREETING_STATUS_ABANDONED = "abandoned"
#: 诊断快照（§26.2）里放弃原因的中文说明；未知原因原样透出
_GREETING_ABANDON_REASON_CN = {
    "deadline": "观测窗结束仍无人说话",
    "window_closed": "掷点时观测窗已关闭",
}
#: 早安无「非睡眠日程条目」时的放弃期限兜底：基准 + 120 分钟（§3.2 期限列的实现裁量）
_MORNING_FALLBACK_DEADLINE_MINUTES = 120
#: 睡眠条目名称兜底关键词：与 emotion/service.py 的 `_is_sleep_item` 同一依据
#: （planning 不 import emotion，避免循环依赖；F14 要求早安期限过滤睡眠条目）
_SLEEP_NAME_KEYWORDS = ("睡觉", "睡眠", "安睡", "午休", "午睡", "小憩", "打盹")
#: 晚安期限的锚（§31.1，R7 = 用户 2026-10-23 第二轮直派）：**current_cycle_end**（入睡时刻，
#: 跨 0 点照取）。旧 ``min(真实入睡锚点, 生效静默起点)`` 的静默项与入睡项并入 cycle_end
#: 真源（§21 裁决精神沿用）；``[patrol]`` 静默窗真源 = companion/patrol.py（只约束巡检，
#: 不再约束问候链）。无日程 fail-safe = ``cycle_state.anchored_state``（``[schedule]`` 两键推算）。


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

# （2026-10-23 §31.1，R7 = 用户第二轮直派）``_DEFAULT_WAKE/SLEEP_MINUTES`` 与
# ``_parse_hhmm_minutes`` 已随 ``_schedule_anchors``/``_silence_start_ts``/``_next_daily_anchor_ts``
# 一并删除：问候链的时间真源 = 周期四变量（``planning.cycle_state``），锚点回退构造与
# HH:MM 解析都收敛到 ``cycle_state.anchored_state`` / ``cycle_state.day_cycle_epochs``，
# 本模块不再持有第二份作息字面量（``[patrol]`` 静默口径见 companion/patrol.py）。


class ProactiveService:
    """活动驱动的主动行为服务（60s 轮询循环）。"""

    LOOP_INTERVAL_SECONDS = 60.0
    STARTUP_DELAY_SECONDS = 15.0
    RESOLVE_CACHE_TTL_SUCCESS = 600.0  # 解析成功缓存 10 分钟
    RESOLVE_CACHE_TTL_FAILURE = 60.0   # 解析失败缓存 60 秒（允许恢复后重试）

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        #: marks v2（§31.1 连锁桶，R7 = 用户 2026-10-23 第二轮直派）：按流按 kind 记最新
        #: 已发 ``cycle_start``（唯一值替换；旧 v1 数据视为不存在）。延迟加载；热重载后
        #: 新实例重新读盘——"重载后不再重发"（每流每 kind 同周期只发一次）。
        self._stream_greeting_marks: Optional[Dict[str, Any]] = None
        self._stop_event: asyncio.Event = asyncio.Event()
        # 时区在 [schedule_advanced]（2026-10-10 段拆分）
        self._tz_manager: TimezoneManager = TimezoneManager(
            section_or_default(plugin, "schedule_advanced",
                               ScheduleAdvancedSectionConfig).timezone
        )
        # 2026-10-20 用户指示：原「已触发/待触发的主动发起记录」随活动切换主动发起链一并删除，
        # 仅剩下面的问候掷点与门槛缓存。
        # L2 早晚安漂移掷点（§31.1 周期化）：{(stream_id, cycle_start(epoch 秒), kind): 掷点条目}
        # 纯内存、不落盘——热重载后允许重掷；周期键                          = 闸门矩阵的 cycle 身份，
        # 旧按 (stream, date_str, kind) 掷点日键随 §31.1 删除（date_str 只保留为档案字段）。
        self._greeting_rolls: Dict[Tuple[str, float, str], Dict[str, Any]] = {}
        # 群聊硬闸/necessity 拒绝的决策日志节流（§31.1：硬闸不过 = 跳过 + 决策日志留痕，
        # 但 60s 轮询不能逐 tick 刷屏）：{(stream_id, kind, cycle_start): True}（每周期一条）
        self._greeting_gate_logged: Dict[Tuple[str, str, float], bool] = {}
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

    async def _resolve_group_id(self, group_id: str,
                                use_cache: bool = True) -> Optional[str]:
        """把群号解析为 session_id（带解析缓存）。

        ``use_cache=False``（§26.2 诊断快照）→ 不读不写解析缓存。
        """
        entry = f"group:{group_id}"
        now = time.time()
        cached = self._resolved_cache.get(entry) if use_cache else None
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

        if use_cache:
            ttl = (self.RESOLVE_CACHE_TTL_SUCCESS if session_id
                   else self.RESOLVE_CACHE_TTL_FAILURE)
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

        闸序参照麦麦自主规划插件 v4（v4.9.1，AGPL-3.0，本插件 planning/ 的机制复刻来源；
        2026-10-20 用户指示引用；原「MIT 参考实现」为许可笔误，2026-10-22 订正——详见 README 致谢与许可段）：
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

        # 2. L2 早晚安漂移（§3.2/§3.3；2026-10-22 §27.1 分支闸门）：
        #    掷点复用 → 候选硬门槛 → 分支判定（目标激活强制/群聊必要性/其他阈值）→ 触发/推迟/放弃
        await self._process_greetings(now, date_str)

    # 注（2026-10-20 用户指示）：原「活动切换/约定主动发起排期链」已删除——
    # _pending_triggers / _proactive_history / _fresh_window_seconds / _random_delay_seconds
    # / _trigger_proactive（活动与约定分支）与 [planning_proactive].enable_proactive_trigger
    # 配置键、活动/约定 intent 提示词键一并移除（间隙触发移交无对应插件路径；活动切换后不再有插件侧触发）。
    # 问候/频率调控照旧。

    # ------------------------------------------------------------
    # L2 早晚安漂移（§31.1 闸门矩阵；R7 = 用户 2026-10-23 第二轮直派）
    # ------------------------------------------------------------

    def _greeting_tz_name(self) -> str:
        """``[schedule_advanced].timezone``（周期日帧换算统一入口；空串 = 系统本地时区）。"""
        try:
            advanced = section_or_default(self._plugin, "schedule_advanced",
                                          ScheduleAdvancedSectionConfig)
            return str(getattr(advanced, "timezone", "") or "")
        except Exception:
            return ""

    def _schedule_clock_cfg(self) -> Tuple[str, str]:
        """``[schedule]`` 两键 ``(wake_time, sleep_time)`` 原文（无日程 fail-safe 构造用）。"""
        cfg = section_or_default(self._plugin, "schedule", ScheduleSectionConfig)
        return (str(getattr(cfg, "wake_time", "") or ""),
                str(getattr(cfg, "sleep_time", "") or ""))

    def _cycle_ctx(self, now: datetime) -> CycleState:
        """本轮判定用的**周期四变量**（§31.2 读路径 + §31.1 无日程 fail-safe）。

        - ``plugin._data_dir`` 可得 → :func:`cycle_state.load_or_anchored`（已持久化状态
          优先；缺失/非法 → ``[schedule]`` 两键 anchored 内存构造，**不写盘**）；
        - ``_data_dir`` 缺失（测试桩/裁剪部署）→ 直接 :func:`cycle_state.anchored_state`
          内存构造；
        - 两者皆异常 → 出厂锚点（07:00/23:00）兜底构造（``anchored_state`` 内含）。
        时区链 = ``_greeting_tz_name``（与 :func:`day_cycle_epochs` 的日帧换算同一时区）。
        """
        cfg_wake, cfg_sleep = self._schedule_clock_cfg()
        tz_name = self._greeting_tz_name()
        data_dir = getattr(self._plugin, "_data_dir", None)
        try:
            if data_dir is not None:
                return load_or_anchored(str(data_dir), cfg_wake, cfg_sleep, tz_name, now=now)
            return anchored_state(cfg_wake, cfg_sleep, tz_name, now=now)
        except Exception as exc:  # 周期层异常绝不打断主循环（§31.2 写盘 best-effort 精神）
            logger.debug(f"周期四变量读数失败（按出厂锚点兜底）: {exc}")
            return anchored_state("", "", "", now=now)

    def _cycle_pivots(self, cycle: CycleState) -> Tuple[float, float, float]:
        """四变量 → ``(prev_start, cur_start, cur_end)``（epoch 秒；缺省/非法端点按 0 兜底）。"""
        prev_start = float(cycle.previous_cycle_start or 0.0)
        cur_start = float(cycle.current_cycle_start or 0.0)
        cur_end = float(cycle.current_cycle_end or 0.0)
        return prev_start, cur_start, cur_end

    def _cycle_day_str(self, cycle: CycleState) -> str:
        """周期日期 = current_cycle_start 所在日历日（**档案字段**：只作日程取数/展示，
        不承担窗锚——窗锚 = 周期变量本身，§31.1）。

        epoch → 本地 datetime 走 :func:`planning.time_utils.epoch_to_local`（唯一实现，禁止
        手写换算）；不可得 → 决策时刻的日历日兜底。
        """
        tz = tzinfo_of(self._greeting_tz_name())
        local = epoch_to_local(float(cycle.current_cycle_start or 0.0), tz)
        if local is not None:
            return local.strftime("%Y-%m-%d")
        return self._tz_manager.get_now().strftime("%Y-%m-%d")

    def _day_items(self, date_str: str) -> List[Any]:
        """某日日程条目（ScheduleQuery.get_day_schedule 优先 = §31.2 锚点合并口径；
        查询不可得/异常 → GoalManager 回退（测试桩与裁剪部署的现行口径）。"""
        query = getattr(self._plugin, "_schedule_query", None)
        getter = getattr(query, "get_day_schedule", None)
        if callable(getter):
            try:
                return list(getter(date_str) or [])
            except Exception as exc:
                logger.debug(f"ScheduleQuery 读 {date_str} 失败（回退 GoalManager）: {exc}")
        try:
            return list(get_goal_manager().get_schedule_goals(
                chat_id="global", date_str=date_str) or [])
        except Exception as exc:
            logger.debug(f"GoalManager 读 {date_str} 失败（按无日程处理）: {exc}")
            return []

    @staticmethod
    def _item_frame_window(item: Any) -> Tuple[Optional[int], Optional[int]]:
        """日程条目（dict 行或 Goal 对象）→ 时间窗 ``(start_minute, end_minute)``。

        dict 行（ScheduleDB/ScheduleQuery 原子行）直取两字段；Goal 形态经
        :func:`_goal_time_window`（先 parameters 后 conditions 的既有口径）。
        分钟允许 ``end > 1440``（跨 0 点照取，§31.2 Q4）；任一侧缺失/非法 → ``(None, None)``。
        """
        if isinstance(item, dict):
            try:
                start = int(item.get("start_minute"))
                end = int(item.get("end_minute"))
            except (TypeError, ValueError):
                return None, None
            return start, end
        start, end = _goal_time_window(item)
        if start is None or end is None:
            return None, None
        return int(start), int(end)

    @staticmethod
    def _is_sleep_row(item: Any) -> bool:
        """日程条目（dict 行或 Goal 对象）是否睡眠条目（与 :func:`_is_sleep_schedule_item`
        同一依据；dict 行读不到属性 → 手动补 dict 分支）。"""
        if isinstance(item, dict):
            if str(item.get("goal_type") or "") == "sleep":
                return True
            name = str(item.get("name") or "")
            return any(keyword in name for keyword in _SLEEP_NAME_KEYWORDS)
        return _is_sleep_schedule_item(item)

    @staticmethod
    def _item_name(item: Any) -> str:
        """条目显示名（dict 行 / Goal 对象双形态）。"""
        value = item.get("name") if isinstance(item, dict) else getattr(item, "name", None)
        return str(value or "")

    def _first_awake_item(self, date_str: str) -> Optional[Tuple[str, int, int]]:
        """睡眠结束后第一个**非睡眠**条目 ``[名称, start, end]``（F14 过滤；取数 = :meth:`_day_items`）。"""
        best: Optional[Tuple[str, int, int]] = None
        for item in self._day_items(date_str):
            if self._is_sleep_row(item):
                continue
            start, end = self._item_frame_window(item)
            if start is None or end is None or end <= start:
                continue
            if best is None or start < best[1]:
                best = (self._item_name(item), start, end)
        return best

    def _cycle_morning_deadline_ts(self, cycle: CycleState) -> Optional[float]:
        """早安期限 = 周期日帧内**第一个非睡眠条目结束**（§31.1「现值不变、改经周期变量」）。

        绝对 epoch（``day_cycle_epochs``：跨 0 点照取 ``>1440`` 环节点）；日程缺失/无
        非睡眠条目 → ``None``（调用方回退「基准 + 120 分钟」实现裁量）。
        """
        first = self._first_awake_item(self._cycle_day_str(cycle))
        if first is None:
            return None
        epochs = day_cycle_epochs(self._cycle_day_str(cycle), first[1], first[2],
                                  self._greeting_tz_name())
        return epochs[1] if epochs else None

    def _cycle_first_item_window(self, cycle: CycleState
                                 ) -> Optional[Tuple[float, float]]:
        """群聊 necessity 判定窗（§31.1）= **今天日程第一个条目**的 ``[start, end)``。

        「首条」= 周期日期帧内 start_minute 最小的条目（字面取首条，不滤睡眠——日程生成的
        第一个条目通常非睡眠；夜猫子日程首条为凌晨睡眠段时按字面落在该段）。两端经
        :func:`day_cycle_epochs` 落绝对 epoch（end >1440 照取次日）；日程不可得/两端非法
        → ``None``（调用方按必要性不通过处理，宁不发）。
        """
        date_str = self._cycle_day_str(cycle)
        best: Optional[Tuple[int, int]] = None
        for item in self._day_items(date_str):
            start, end = self._item_frame_window(item)
            if start is None or end is None or end <= start:
                continue
            if best is None or start < best[0]:
                best = (start, end)
        if best is None:
            return None
        return day_cycle_epochs(date_str, best[0], best[1], self._greeting_tz_name())

    def _greeting_frames(self, kind: str, cycle: CycleState) -> Tuple[float, float]:
        """kind 的 ``(基准点, 期限)``（epoch 秒；§31.1 窗体经周期四变量）。

        - 早安：基准 = 本份日程开始（``current_cycle_start``，≈起床锚）；期限 = 第一个
          非睡眠条目结束，拿不到 → 基准 + :data:`_MORNING_FALLBACK_DEADLINE_MINUTES`；
        - 晚安：基准同锚（漂移点 = 期限 − 随机漂移）；期限 = **current_cycle_end**（入睡
          时刻，跨 0 点照取）；病态窗（end ≤ start，如拒绝轮换前的告警态兜底）→ 期限 0
          = 观测窗关闭（fail-closed 宁不发）。
        """
        _prev, cur_start, cur_end = self._cycle_pivots(cycle)
        if kind == GREETING_MORNING:
            deadline = self._cycle_morning_deadline_ts(cycle)
            if deadline is None:
                deadline = cur_start + _MORNING_FALLBACK_DEADLINE_MINUTES * 60.0
        else:
            deadline = cur_end if cur_end > cur_start else 0.0
        return cur_start, deadline

    async def _messages_in_window(self, stream_id: str, start_ts: Any, end_ts: Any,
                                  limit: int,
                                  *,
                                  slash_filter: bool,
                                  keep_bot: bool = False) -> Optional[List[Any]]:
        """宿主消息窗查询（§31.1 四类 open 判定的**统一取数口**）。

        ``filter_mai=True``（bot 自身消息不计入）——``keep_bot=True``（§31.1 补发冷静期:
        要找 bot 自身消息）则不带该参数；旧宿主不认识该参数 → 不带它**重试一次**（既有
        双保险）。``slash_filter=True``（硬闸/necessity 判据）剔除空白与以 ``/`` 开头
        的命令消息；``False``（其他私聊阈值计数）按对方普通输入计入（§27.1 裁量保留）。
        能力缺失/查询异常/返回形态不可解析 → ``None``（调用方 fail-closed 宁不发）。
        """
        ctx_obj = getattr(self._plugin, "ctx", None)
        getter = getattr(getattr(ctx_obj, "message", None), "get_by_time_in_chat", None)
        if not callable(getter):
            return None
        try:
            if keep_bot:
                # 补发冷静期：找 bot 自己的消息（is_mai=真），不能让 filter_mai 把它们滤掉
                # （§31.1 通则②；R7 = 用户 2026-10-23 第二轮直派）
                msgs = await getter(str(stream_id), str(float(start_ts)),
                                    str(float(end_ts)), limit=int(limit))
            else:
                try:
                    msgs = await getter(str(stream_id), str(float(start_ts)), str(float(end_ts)),
                                        limit=int(limit), filter_mai=True)
                except Exception as exc:
                    logger.debug(f"带 filter_mai 查询失败，改为不带重试: {exc}")
                    msgs = await getter(str(stream_id), str(float(start_ts)),
                                        str(float(end_ts)), limit=int(limit))
        except Exception as exc:
            logger.debug(f"消息窗查询失败（按取不到处理）: stream={stream_id}, {exc}")
            return None
        if isinstance(msgs, dict):
            msgs = msgs.get("messages") or msgs.get("data") or []
        if not isinstance(msgs, list):
            return None
        if slash_filter:
            kept: List[Any] = []
            for msg in msgs:
                text = (msg.get("processed_plain_text") if isinstance(msg, dict)
                        else getattr(msg, "processed_plain_text", None))
                if isinstance(text, str) and text.strip() and not text.strip().startswith("/"):
                    kept.append(msg)
            return kept
        return msgs

    async def _group_morning_hard_gate(self, stream_id: str, prev_ts: Any,
                                       cur_ts: Any) -> Optional[bool]:
        """群聊早安**底层硬闸**（§31.1；无条件，与 necessity 配置无关）。

        判定窗 = ``[上份日程开始, 本份日程开始)``（经周期四变量，**非日历 00:00**）——
        窗内群聊非 bot、非斜杠命令消息 ≥1。查询异常 → ``None``（fail-closed，调用方按
        不过处理）。
        """
        msgs = await self._messages_in_window(stream_id, prev_ts, cur_ts, limit=1,
                                              slash_filter=True)
        if msgs is None:
            return None
        return bool(msgs)

    async def _group_necessity_gate(self, stream_id: str, cycle: CycleState) -> Optional[bool]:
        """群聊早安**必要性**判定（``greeting_require_activation``，§31.1）。

        判定窗 = 今天日程**第一个条目**时段（:meth:`_cycle_first_item_window`）——窗内
        非 bot、非斜杠命令消息 ≥1。窗口不可得（无日程）或查询异常 → ``None``（按不通过）。
        """
        window = self._cycle_first_item_window(cycle)
        if window is None:
            return None
        msgs = await self._messages_in_window(stream_id, window[0], window[1], limit=1,
                                              slash_filter=True)
        if msgs is None:
            return None
        return bool(msgs)

    async def _priv_threshold_count(self, stream_id: str, kind: str, cycle: CycleState,
                                    need: int) -> Tuple[int, bool]:
        """其他私聊「对方发送」计数（§31.1 阈值判定：早安 ``greeting_other_private_min_messages``
        / 晚安 ``greeting_private_night_min_messages``）。

        判定窗：早安 = ``[prev_cycle_start, cur_cycle_start)``（窗端已过 → 计数恒定，可与
        「过点判据」探测共用同一窗）；晚安 = ``[cur_cycle_start, 晚安期限)``（Deadline 前
        窗内容单调增长 → 每 tick 重查，**不再冻结端点**——旧 B1/B2「首评定死缓存」形态废止）。
        返回 ``(实查条数, 达标)``；查询异常/不可解析 → ``(0, False)``（fail-closed 宁不发）。
        """
        _prev, cur_start, cur_end = self._cycle_pivots(cycle)
        if kind == GREETING_MORNING:
            start_ts, end_ts = _prev, cur_start
        else:
            start_ts, end_ts = cur_start, cur_end
        if end_ts <= start_ts:
            return 0, False
        msgs = await self._messages_in_window(stream_id, start_ts, end_ts,
                                              limit=max(1, int(need)), slash_filter=False)
        if msgs is None:
            return 0, False
        return min(len(msgs), max(1, int(need))), len(msgs) >= max(1, int(need))

    def _greeting_chain_pass(self, stream_id: str, cycle: CycleState) -> bool:
        """**连锁桶**（marks v2）：该流本轮早安已开口（morning 标记的 cycle_start ==
        当前周期起点）→ 群聊晚安与他人私聊晚安的 OR 连锁分支通过。"""
        marks = self._load_stream_marks()
        try:
            recorded = float(marks.get(str(stream_id), {}).get(GREETING_MORNING) or 0.0)
        except (TypeError, ValueError):
            return False
        cur_start = float(cycle.current_cycle_start or 0.0)
        return bool(cur_start) and recorded == cur_start

    def _greeting_chain_state(self, stream_id: str, cycle: CycleState, now_ts: float,
                              cfg_p: Any) -> str:
        """早安连锁桶当前态（§31.3 diag 四类文案的判定依据；也决定晚安是否可激活）。

        - ``"sent"``：本轮早安已开口（marks v2 命中）→ 晚安可走连锁；
        - ``"inflight"``：早安**未开始（连锁待定）**——本轮还在早安观测窗内（期限未过、
          早安总开关开着、掷点未终结），此后仍可能开口；
        - ``"dead"``：本轮早安已确定不开启（观测窗已过且未开口 / 总开关关闭 /
          掷点已放弃）→ 晚安连锁硬性忽略。
        """
        if self._greeting_chain_pass(stream_id, cycle):
            return "sent"
        if not self._greeting_kind_enabled(GREETING_MORNING):
            return "dead"
        cur_start, deadline = self._greeting_frames(GREETING_MORNING, cycle)
        entry = self._greeting_rolls.get((stream_id, float(cycle.current_cycle_start or 0.0),
                                          GREETING_MORNING))
        if entry is not None and entry.get("status") == GREETING_STATUS_ABANDONED:
            return "dead"
        observe_until = deadline
        if entry is not None and entry.get("observe_until"):
            try:
                observe_until = max(deadline, float(entry.get("observe_until")))
            except (TypeError, ValueError):
                pass
        else:
            _lo, _hi = self._greeting_drift(GREETING_MORNING)
            observe_until = max(deadline, cur_start + _hi * 60.0)
        return "inflight" if now_ts <= observe_until else "dead"

    async def _calm_gate(self, stream_id: str, silence_minutes: int,
                         now_ts: float, cycle: CycleState,
                         from_ts: Optional[float] = None
                         ) -> Tuple[bool, float, Optional[float]]:
        """补发冷静期判定（§31.1 通则②）：bot 本会话最近一条消息（``is_mai=真``）距今
        是否 ≥ ``schedule_advanced.greeting_min_silence_minutes``。

        查询窗 = ``(now − N 分钟, now]``（``from_ts`` 供诊断指定更早窗底）；窗内存在
        ``is_mai=真`` 消息 → 距今 < N → 不满足（余量 = N − 实际沉默分钟）。查询异常 →
        ``(False, silence_minutes, None)``（fail-closed 宁不补发）。``N ≤ 0``（ge=0 配置）
        → 无冷静期，恒满足。返回 ``(满足?, 实际沉默分钟, 最近 bot 消息 ts 或 None)``。
        """
        silence = max(0, int(silence_minutes or 0))
        now = now_ts
        if silence <= 0:
            return True, silence, None
        window_start = now - silence * 60.0
        if from_ts is not None:
            window_start = min(window_start, float(from_ts))
        msgs = await self._messages_in_window(stream_id, window_start, now,
                                              limit=50, slash_filter=False,
                                              keep_bot=True)
        if msgs is None:
            return False, 0.0, None
        latest_own: Optional[float] = None
        for msg in msgs:
            if isinstance(msg, dict):
                if not msg.get("is_mai"):
                    continue
                raw = msg.get("timestamp")
            else:
                if not getattr(msg, "is_mai", None):
                    continue
                raw = getattr(msg, "timestamp", None)
            try:
                ts = float(raw)
            except (TypeError, ValueError):
                continue
            if ts > window_start and (latest_own is None or ts > latest_own):
                latest_own = ts
        if latest_own is None:
            return True, silence, None
        quiet = max(0.0, (now - latest_own) / 60.0)
        return (quiet >= silence), quiet, latest_own

    # 注（2026-10-10 第二轮）：原 ``_in_silence_window`` 已删除——问候不再被静默时段拦截。
    # 注（§31.1，R7 = 用户 2026-10-23 第二轮直派）：早晨晚安的静默拦截同样维持不引入——
    # 晚安期限 = current_cycle_end（静默起点项与入睡项已并入 cycle_end 真源）；这里的
    # 静默语义只约束 [patrol] 巡检（companion/patrol.py）。不要在这里重新引入静默拦截。
    # 旧 ``_schedule_anchors`` / ``_midnight_ts`` / ``_greeting_baseline_ts`` /
    # ``_greeting_deadline_ts`` / ``_next_daily_anchor_ts`` / ``_silence_start_ts`` /
    # ``_effective_silence_start`` / ``_stream_active_today`` / ``_group_floor_ok`` 均随
    # 本轮周期化删除（真源全部收敛到上面 :meth:`_cycle_ctx` / :meth:`_greeting_frames`）。

    def _greeting_drift(self, kind: str) -> Tuple[int, int]:
        """取该 kind 的漂移区间（非法/负数回退默认区间，§3.1/B3）。"""
        cfg_p = self._proactive_cfg()
        attr = "morning_drift" if kind == GREETING_MORNING else "night_drift"
        return _parse_drift(getattr(cfg_p, attr, None))

    def _roll_greeting(self, stream_id: str, category: str, kind: str, cycle: CycleState,
                       now: datetime, now_ts: float, date_str: str) -> Dict[str, Any]:
        """掷点（§31.1 周期化）：每 ``(stream_id, cycle_start, kind)`` 只掷一次——同流同周期
        重复决策复用同一结果。

        **漂移点**：

        - 早安 = 基准 + ``randint(a,b)``（基准 = 本份日程开始 = ``current_cycle_start``，
          醒来后 0~N 分钟）；
        - 晚安 = 晚安期限 − ``randint(a,b)``（期限 = **current_cycle_end** = 入睡时刻，跨 0 点
          照取——晚飞泻点恒落在本次日程清醒区间内，不会跨过入睡界）。
        - **观测窗 = ``[min(基准, 漂移点), max(期限, 漂移点)]``**（既有公式保留；窗体仅含掷点，
          判定窗 = §31.1 闸门矩阵的周期窗，二者分离）。

        纯内存、不落盘（热重载后允许重掷）；跨 0 点窗按绝对 epoch 处理（§31.2：不折回）。
        掷点时观测窗已整段关掉（``now ≥ 观测窗上界``）→ 记 ``abandoned(window_closed)``
        （该周期不再评估该流该 kind；structural irreversible case，区别于观测流内越界的
        ``deadline`` 钳制放弃）。
        """
        cycle_key = float(cycle.current_cycle_start or 0.0)
        key = (stream_id, cycle_key, kind)
        entry = self._greeting_rolls.get(key)
        if entry is not None:
            return entry

        drift = self._greeting_drift(kind)
        baseline_ts, deadline_ts = self._greeting_frames(kind, cycle)
        offset = float(random.randint(drift[0], drift[1])) * 60.0   # 漂移区间是分钟 → 秒
        # 漂移点：早安 = 本份日程开始 + offset；晚安 = 入睡时刻 − offset（睡前随机点，
        # §31.1「晚安期限 = 本轮日程结束时间戳」——min(入睡锚点, 静默起点) 两项已并入）。
        fire_at = baseline_ts + offset if kind == GREETING_MORNING else deadline_ts - offset
        # 观测窗：[min(基准, 漂移点), max(期限, 漂移点)]——保证掷点永远落在窗内
        observe_from = min(baseline_ts, fire_at)
        observe_until = max(deadline_ts, fire_at)
        entry = {
            "stream_id": stream_id,
            "category": category,
            "kind": kind,
            "cycle": cycle_key,
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
                f"问候窗口已过（不排期）: stream={stream_id}, kind={kind}, cycle={cycle_key}"
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
        """群聊早安**必要性**开关（§31.1；R7 = 用户 2026-10-23 第二轮直派）。

        读 ``[planning_proactive].greeting_require_activation``（bool，默认 True）；
        读不到/取值异常 → **True**（fail-closed，宁不发不错发）。**只对群聊早安**生效：
        开 = 在底层硬闸之上额外判定「今天日程第一个条目时段内非 bot 消息 ≥1」（AND）；
        关 = 不判（硬闸仍无条件生效）。目标私聊**无条件**、其他私聊走阈值判定，均不经
        本开关。

        沿革：2026-10-10 第二轮回正时本闸由配置控制、且所有会话一视同仁；2026-10-20
        用户指示起强制化并删除配置键；2026-10-22 裁定恢复为真配置键（默认 True）、收窄群聊；
        2026-10-23 §31.1 起语义 = 群聊早安 necessity 判定窗改「今天日程第一个条目时段」。
        """
        try:
            value = getattr(cfg_p, "greeting_require_activation", True)
        except Exception:
            return True
        if value is None:
            return True
        return bool(value)

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

    async def _resolve_private_id(self, user_id: str,
                                  use_cache: bool = True) -> Optional[str]:
        """把裸 QQ 号解析为私聊 session_id（带解析缓存；问候专用名单只收裸号，不带前缀）。

        ``use_cache=False``（§26.2 诊断快照）→ 不读不写解析缓存，纯查询后直接返回。
        """
        entry = f"greeting-private:{user_id}"
        now = time.time()
        cached = self._resolved_cache.get(entry) if use_cache else None
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

        if use_cache:
            ttl = (self.RESOLVE_CACHE_TTL_SUCCESS if session_id
                   else self.RESOLVE_CACHE_TTL_FAILURE)
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
        §25.2 起 records 化、§26.1 起按群聊/私聊切片）把纯 sid 展示位换成可读名称：单次 ``ctx.chat.get_all_streams(platform="qq")`` 全量枚举，
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

    async def _resolve_greeting_candidates(self, cfg_p: Any,
                                           now: Optional[datetime] = None,
                                           date_str: Optional[str] = None
                                           ) -> List[Tuple[str, str]]:
        """问候候选：目标私聊 ∪ 群聊 ∪ 其他私聊（统一名单；**门槛在分支内逐 kind 判定**）。

        2026-10-10 第二轮回正（B2）：严格遵循统一名单，不再有任何"隐藏兼容位"开关：

        - **目标陪伴私聊**：陪伴模块（``[patrol].enabled``）启用、目标会话已解析、
          且**未被私聊黑名单排除**（白名单模式下视为恒在名单内；黑名单模式下把目标 QQ
          写进 ``proactive_private_ids`` 即可停掉它的问候）时生效；
        - **群聊**：统一群聊名单（``proactive_group_ids`` + 模式；默认黑名单 + 留空 = 全群）。
          §31.1（R7 = 用户 2026-10-23 第二轮直派）起群聊**不再在候选解析处过滤**——底层
          硬闸逐 kind 在 :meth:`_evaluate_greeting` 内实判（不过 = 直接跳过、不掷点、
          不建条目、决策日志留痕），静默群保留候选（诊断可达、闸门语义独立）；
        - **其他私聊**：统一私聊名单（``proactive_private_ids`` + 模式；默认白名单 + 留空 =
          全不发），阈值闸门同在分支内逐 kind 判定。

        ``now``/``date_str`` 缺省时按服务时区管理器取；``date_str`` 自 §31.1 起**仅为
        档案字段**（不再参与任何窗锚）。
        """
        if now is None:
            now = self._tz_manager.get_now()
        if date_str is None:
            date_str = now.strftime("%Y-%m-%d")
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
        """L2 每轮（§31.1 周期口径）：候选枚举 → 掷点/复用 → 分支闸门 → 到点/补发/放弃。

        早/晚安由两个总开关（``greeting_morning_enabled`` / ``greeting_night_enabled``）
        控制；两类开关全关时本方法直接返回。``date_str`` 入参**仅为兼容档案字段**——本轮
        的周期日期一律经周期四变量（:meth:`_cycle_ctx` + :meth:`_cycle_day_str`）取数。

        跨周期残留清理：非本周期条目只在「已终结（fired/abandoned）或观测窗已整段过去」
        时清掉——夜猫子日程（入睡期限 >0 点）的晚安条目必须活到期限那一刻（§31.2 跨 0 点
        照取语义接管旧 T3 #1）。
        """
        cfg_p = self._proactive_cfg()
        if not (self._greeting_kind_enabled(GREETING_MORNING)
                or self._greeting_kind_enabled(GREETING_NIGHT)):
            return
        now_ts = _epoch_ts(now)
        cycle = self._cycle_ctx(now)
        cycle_key = float(cycle.current_cycle_start or 0.0)
        date_str = self._cycle_day_str(cycle)
        candidates = await self._resolve_greeting_candidates(cfg_p, now=now, date_str=date_str)
        for stream_id, category in candidates:
            for kind in (GREETING_MORNING, GREETING_NIGHT):
                if not self._greeting_kind_enabled(kind):
                    continue
                await self._evaluate_greeting(stream_id, category, kind,
                                              now, now_ts, date_str, cfg_p, cycle=cycle)
        for key, entry in list(self._greeting_rolls.items()):
            if key[1] == cycle_key:
                continue
            terminated = entry.get("status") in (GREETING_STATUS_FIRED, GREETING_STATUS_ABANDONED)
            if terminated or now_ts > float(entry.get("observe_until", 0) or 0):
                self._greeting_rolls.pop(key, None)
        # 剩余内存缓存按周期清（闸门拒绝日志节流表；marks 连锁桶唯一值不按周期清）。
        for gkey in [k for k in self._greeting_gate_logged if k[2] != cycle_key]:
            self._greeting_gate_logged.pop(gkey, None)

    async def _evaluate_greeting(self, stream_id: str, category: str, kind: str,
                                 now: datetime, now_ts: float, date_str: str,
                                 cfg_p: Any, cycle: Optional[CycleState] = None) -> None:
        """单个候选的状态机（§31.1 闸门矩阵）：**掷点前闸门 → 掷点/复用 → 推进**。

        - **目标私聊**：无条件（原激活强制随 §31.1 解除，用户 2026-10-23 裁定），只看
          ``[patrol].enabled`` + 统一名单 + 既有掷点/到点/去重流程；
        - **群聊早安**：底层硬闸（``[prev.start, cur.start)`` 窗内非 bot ≥1，无条件）不过 =
          直接跳过、**不掷点、不创建条目**（决策日志留痕，每周期每流每 kind 一条）；
          necessity 开 → AND「今天日程第一个条目时段内非 bot 消息 ≥1」（关 = 不判）；
        - **群聊晚安**：硬性连锁（marks v2 连锁桶）——本群早安本轮未开口 → 不掷点；
        - **其他私聊**：早安阈值（窗 ``[prev.start, cur.start)``）；晚安 = OR（连锁桶 ∨
          阈值，窗 ``[cur.start, 晚安期限)``）。
        """
        if not self._greeting_kind_enabled(kind):
            return  # 总开关（候选枚举已按开关过滤，这里再判一次保证直调路径同样生效）
        if cycle is None:
            cycle = self._cycle_ctx(now)
        date_str = self._cycle_day_str(cycle)
        verdict, durable = await self._pre_gate(stream_id, category, kind, cycle, now, now_ts,
                                                cfg_p)
        baseline, deadline = self._greeting_frames(kind, cycle)
        drift_lo, drift_hi = self._greeting_drift(kind)
        earliest_fire = (baseline + drift_lo * 60.0 if kind == GREETING_MORNING
                         else deadline - drift_hi * 60.0)
        if verdict == "skip" and (durable or now_ts < earliest_fire):
            # 硬闸不过（群聊早安，§31.1 = 不掷点/不建条目）；其他私聊阈/连锁未达标的
            # 本周期首过点前静默跳过（掷点已过者不静默——建条目后由钳制负责到期弃）
            return

        entry = self._roll_greeting(stream_id, category, kind, cycle, now, now_ts, date_str)
        await self._advance_greeting(entry, now_ts, date_str, cfg_p, cycle)

    async def _pre_gate(self, stream_id: str, category: str, kind: str, cycle: CycleState,
                        now: datetime, now_ts: float, cfg_p: Any) -> str:
        """掷点前分支闸门（§31.1 闸门矩阵；返回 ``(verdict, durable)``——``durable`` =
        本周期确定不开（硬闸/连锁 dead），调用方不再掷点）。

        后台轮询逐 tick 实查（旧 §27 按日缓存形态废止，§31.1 窗全部经周期变量、不再看
        日历 00:00）；查询异常一律按**不过**处理（fail-closed 宁不发）。群聊早安硬闸拒绝
        记决策日志（每周期每流每 kind 至多一条，避免刷屏）。
        """
        _prev, cur_start, _cur_end = self._cycle_pivots(cycle)
        if kind == GREETING_MORNING:
            # 早安沉默闸（R7 = 用户 2026-10-24 直派）：第一个检查、三流分派之前——
            # bot 在今日第一日程（= 周期起点）前 N 小时内发过消息 → 该流本周期跳过早安
            # （durable = 不掷点不建条目，对齐群聊硬闸语义；查询异常 fail-closed）
            silenced = await self._morning_silence_gate(stream_id, now_ts, cycle, cfg_p)
            if silenced is None:
                self._log_gate_skip(stream_id, kind, cycle,
                                    "早安沉默闸查询失败（morning_silence，fail-closed，宁不发）")
                return "skip", True
            if not silenced:
                hours = self._morning_silence_hours(cfg_p)
                self._log_gate_skip(
                    stream_id, kind, cycle,
                    f"早安沉默闸未过（morning_silence：第一日程前 {hours:g} 小时内 bot 发过消息，"
                    f"窗={self._fmt_span(cur_start - hours * 3600.0)} → {self._fmt_span(cur_start)}，"
                    f"该流跳过早安）")
                return "skip", True
            if category == GREETING_CATEGORY_GROUP:
                hard = await self._group_morning_hard_gate(stream_id, _prev, cur_start)
                if hard is None:
                    self._log_gate_skip(stream_id, kind, cycle,
                                        "群聊早安硬闸查询失败（fail-closed，宁不发）")
                    return "skip", True
                if not hard:
                    self._log_gate_skip(stream_id, kind, cycle,
                                        "群聊早安硬闸不过（判定窗内无非 bot 消息，跳过）")
                    return "skip", True
                if self._greeting_requires_activation(cfg_p):
                    necessity = await self._group_necessity_gate(stream_id, cycle)
                    if necessity is None or not necessity:
                        self._log_gate_skip(
                            stream_id, kind, cycle,
                            "群聊早安必要性不过（今天日程第一个条目时段内无人开口）")
                        return "skip", True
                return "pass", False
            if category == GREETING_CATEGORY_OTHER:
                threshold = await self._priv_threshold_count(
                    stream_id, kind, cycle, self._threshold_need(cfg_p, kind))
                if not threshold[1]:
                    # 阈值未达标：预掷点静默跳过；掷点过后（调用方建条目）由 advance 复查
                    # 到期钳制弃——「窗内一直不满足则到期仍弃」（§31.1）
                    return "skip", False
            return "pass", False  # TARGET = 无条件（判定窗/阈值均不设）
        # 晚安：群聊 = 硬性连锁；其他私聊 = OR（连锁 ∨ 阈值）；目标 = 无条件
        if category == GREETING_CATEGORY_GROUP:
            state = self._greeting_chain_state(stream_id, cycle, now_ts, cfg_p)
            if state == "sent":
                return "pass", False
            # 未 sent：inflight（待定）= 仍可能翻转 → 非既往不裁；dead = 本周期确定不开
            return "skip", state == "dead"
        if category == GREETING_CATEGORY_OTHER:
            threshold = await self._priv_threshold_count(
                stream_id, kind, cycle, self._threshold_need(cfg_p, kind))
            if self._greeting_chain_pass(stream_id, cycle) or threshold[1]:
                return "pass", False
            return "skip", False
        return "pass", False  # TARGET = 无条件

    def _log_gate_skip(self, stream_id: str, kind: str, cycle: CycleState, reason: str) -> None:
        """闸门拒绝的决策日志留痕（每周期每流每 kind 至多一条）。"""
        key = (str(stream_id), str(kind), float(cycle.current_cycle_start or 0.0))
        if self._greeting_gate_logged.get(key):
            return
        self._greeting_gate_logged[key] = True
        self._decision_log("skip", trigger_type=f"{kind}_greeting",
                           reason=reason, stream_id=str(stream_id),
                           category=GREETING_CATEGORY_GROUP)
        logger.info(f"问候闸门拒绝: stream={stream_id}, kind={kind}: {reason}")

    def _threshold_need(self, cfg_p: Any, kind: str) -> int:
        """其他私聊阈值（运行期兜底 ``max(1, …)``；配置层 ``ge=1`` 已拒绝 0/负数）。"""
        attr = ("greeting_other_private_min_messages" if kind == GREETING_MORNING
                else "greeting_private_night_min_messages")
        try:
            raw = getattr(cfg_p, attr, 5)
            return max(1, int(5 if raw is None else raw))
        except (TypeError, ValueError):
            return 5

    def _silence_minutes(self, cfg_p: Any) -> int:
        """补发冷静期分钟数（``[schedule_advanced].greeting_min_silence_minutes``，ge=0）。"""
        try:
            advanced = section_or_default(self._plugin, "schedule_advanced",
                                          ScheduleAdvancedSectionConfig)
            return max(0, int(getattr(advanced, "greeting_min_silence_minutes", 3) or 0))
        except (TypeError, ValueError):
            return 3

    def _morning_silence_hours(self, cfg_p: Any) -> float:
        """早安沉默闸小时数（``[planning_proactive].greeting_morning_silence_hours``，
        R7 = 用户 2026-10-24 直派；ge=0 配置，运行期非法值回退缺省 2.0，0 = 禁用闸门）。"""
        try:
            raw = getattr(cfg_p, "greeting_morning_silence_hours", 2.0)
            value = float(2.0 if raw is None else raw)
        except (TypeError, ValueError):
            return 2.0
        return max(0.0, value)

    async def _morning_silence_gate(self, stream_id: str, now_ts: float, cycle: CycleState,
                                    cfg_p: Any) -> Optional[bool]:
        """早安沉默闸（R7 = 用户 2026-10-24 直派）：bot 至少在今日第一日程（= 周期起点
        ``cur_start``）前沉默 N 小时，否则该流跳过早安。

        判定：``N > 0`` 且 ``now >= cur_start`` 时，查窗 ``[cur_start − N×3600, cur_start]``
        内该流 bot 自身消息（``keep_bot=True`` 不带 filter_mai，逐条按 ``is_mai`` 过滤，
        写法对齐 :meth:`_calm_gate`）——存在 ≥1 条 → 不过。三流统一受此闸（含目标私聊）。
        返回 ``None`` = 查询异常（调用方 fail-closed 宁不发）；``N ≤ 0`` 或周期起点不可得
        或 ``now < cur_start``（尚未进入本周期）→ 恒通过。
        """
        hours = self._morning_silence_hours(cfg_p)
        if hours <= 0:
            return True
        _prev, cur_start, _cur_end = self._cycle_pivots(cycle)
        if cur_start <= 0 or float(now_ts) < cur_start:
            return True
        msgs = await self._messages_in_window(
            stream_id, cur_start - hours * 3600.0, cur_start,
            limit=20, slash_filter=False, keep_bot=True)
        if msgs is None:
            return None
        for msg in msgs:
            if isinstance(msg, dict):
                if msg.get("is_mai"):
                    return False
            elif getattr(msg, "is_mai", None):
                return False
        return True

    async def _advance_greeting(self, entry: Dict[str, Any], now_ts: float,
                                date_str: str, cfg_p: Any,
                                cycle: Optional[CycleState] = None) -> None:
        """推进一条掷点条目（§31.1）：已终结 → 跳过；**越界钳制** → 放弃；闸门未过 → 等待；
        到点 → 发；过点才达标 → 补发冷静期（不满足则窗尾弃）。

        顺序（§31.1 通则①②）：① 越界钳制先行——用户裁定「不得界后道早安/晚安」，所有
        需判定分支统一 ``now > observe_until → abandon(deadline)``（钉死 SU1；含目标类——
        热重载重置的条目同样不界后开口）；② 到点等待（窗口内未达标 = 下一 tick 接续，
        无端点冻结）；③ 持久化去重（marks v2 唯一值 + 目标类 affection 按天标记）；④ 过点
        才达标（``gate_over_point``）→ 补发冷静期（窗内一直不满足 → 下轮钳制
        abandon(deadline)——「窗内一直不满足则到期仍弃」）；⑤ 触发（宿主失败 → 状态保持
        pending，由钳制保证越过观测窗上界后放弃，不再有旧口径"到期限自然放弃"的失实自述）。
        """
        if entry["status"] in (GREETING_STATUS_FIRED, GREETING_STATUS_ABANDONED):
            return
        if cycle is None:
            cycle = self._cycle_ctx(datetime.fromtimestamp(now_ts))
        observe_until = float(entry.get("observe_until", entry["deadline"]) or entry["deadline"])
        # ① 越界钳制（§31.1 通则①「不得界后道早安/晚安」）
        if now_ts > observe_until:
            self._abandon_greeting(entry, "deadline")
            return
        # ② 分支闸门复核（掷点前已过闸门才走到这里；群聊早安 necessity 窗、其他私聊夜窗
        #    可能在掷点后才首次达标——过点判据由数据窗段决定，见 _greeting_fire_gate）
        passed, over_point = await self._greeting_fire_gate(entry, cfg_p, cycle, now_ts)
        if not passed:
            return
        if now_ts < float(entry["fire_at"]):
            return  # 到点前 → 等待漂移点（到点前已满足的正常触发不受冷静期约束）
        if over_point:
            entry["gate_over_point"] = True  # 过点才满足（数据窗判据）→ 冷静期武装
        # ③ 持久化去重（marks v2 唯一值 = 同流同 kind 同周期只发一次；P5 目标类并查
        #    affection 按天标记；内存 status 已 FIRED 的在上方短路，不重复落去重行）
        if (self._greeting_deduped_by_persisted_marker(entry["category"], entry["kind"])
                or self._stream_greeting_marked(entry["stream_id"], entry["kind"],
                                                entry.get("cycle"))):
            if entry["status"] != GREETING_STATUS_FIRED:
                entry["status"] = GREETING_STATUS_FIRED
            self._decision_log(
                "skip", trigger_type=f"{entry['kind']}_greeting",
                reason="持久化标记命中，热重载去重",
                stream_id=entry["stream_id"], category=entry["category"],
                fire_at=int(entry["fire_at"]), deadline=int(entry["deadline"]),
            )
            return
        # ④ 过点才达标 → 补发冷静期（§31.1 通则②；到点前已满足 = 不受该约束）
        if entry.get("gate_over_point"):
            silence = self._silence_minutes(cfg_p)
            ok = (await self._calm_gate(entry["stream_id"], silence, now_ts, cycle))[0]
            if not ok:
                return  # 冷静期不足 → 下一 tick 复查；窗尾仍未满足 → 钳制放弃
        await self._fire_greeting(entry, date_str, now_ts)

    async def _greeting_fire_gate(self, entry: Dict[str, Any], cfg_p: Any,
                                  cycle: CycleState, now_ts: float) -> Tuple[bool, bool]:
        """到点推进时的**实时闸门复核**（返回 ``(通过, 过点才达标)``）。

        预掷点被闸的条目不会创建（§31.1「不掷点」）；已掷点条目在到点推进时回到同闸重查
        （判据 = 最新判定；与参考插件 G9「双阶段复核」同形）：

        - 目标私聊：无条件（恒通过、无冷静期）；
        - 群聊早安：硬闸 +（necessity 开）必要性——necessity 窗（今天日程第一个条目时段）
          可能越过掷点 → 过点才达标判据以**数据窗段**为准（``[窗底, fire_at)`` 段已有人
          开口 = 到点前已满足 → 正常触发；否则 = 过点才满足 → 补发冷静期）；
        - 群聊晚安：连锁桶重查（硬性连锁无补充阈值）；
        - 其他私聊：早安阈值（窗已冻结于 cur.start ≤ 掷点 → 到点前必已满足）／晚安 OR
          （连锁分支恒无冷静期；阈值分支以「``[窗底, fire_at)`` 段计数 ≥ need」为到点前
          已满足判据，否则过点才达标）。
        """
        fire_at = float(entry["fire_at"])
        kind = entry["kind"]
        stream_id = entry["stream_id"]
        category = entry.get("category")
        if category == GREETING_CATEGORY_TARGET:
            return True, False
        if kind == GREETING_MORNING and category == GREETING_CATEGORY_GROUP:
            _prev, cur_start, _cur_end = self._cycle_pivots(cycle)
            hard = await self._group_morning_hard_gate(stream_id, _prev, cur_start)
            if hard is None or not hard:
                return False, False
            if self._greeting_requires_activation(cfg_p):
                necessity = await self._group_necessity_gate(stream_id, cycle)
                if necessity is None or not necessity:
                    return False, False
                # 过点判据（数据窗段）：[窗底, fire_at) 段已有人开口 → 到点前已满足
                necessity_window = self._cycle_first_item_window(cycle)
                pre_ok = False
                if necessity_window is not None and fire_at > necessity_window[0]:
                    pre_ok = await self._window_counted_before(
                        stream_id, (necessity_window[0], fire_at), 1)
                over = not pre_ok
                return True, bool(over and now_ts > fire_at)
            return True, False
        if kind == GREETING_NIGHT and category == GREETING_CATEGORY_GROUP:
            return self._greeting_chain_pass(stream_id, cycle), False
        win_start, win_end = self._threshold_window_bounds(cycle, kind)
        if kind == GREETING_NIGHT and self._greeting_chain_pass(stream_id, cycle):
            return True, False  # 连锁分支通过（本轮早安时刻恒在该晚安掷点前 → 无冷静期）
        threshold = await self._priv_threshold_count(stream_id, kind, cycle,
                                                     self._threshold_need(cfg_p, kind))
        if not threshold[1]:
            return False, False
        # 阈值分支：过点才达标 = fire_at 之前的主窗段计数不足（数据窗判据）
        if win_end > fire_at and now_ts > fire_at:
            pre_ok = await self._window_counted_before(
                stream_id, (win_start, fire_at), self._threshold_need(cfg_p, kind))
            if not pre_ok:
                return True, True
        return True, False

    async def _window_counted_before(self, stream_id: str, window: Any, need: int) -> bool:
        """``[start, end)`` 段内对方发送计数 ≥ need？（过点判据专用；异常 = False）。"""
        start_ts, end_ts = window[0], window[1]
        if end_ts <= start_ts:
            return False
        msgs = await self._messages_in_window(stream_id, start_ts, end_ts,
                                              limit=max(1, int(need)), slash_filter=False)
        if msgs is None:
            return False
        return len(msgs) >= max(1, int(need))

    def _threshold_window_bounds(self, cycle: CycleState, kind: str) -> Tuple[float, float]:
        """其他私聊阈值窗端 ``(start, end)``（与 :meth:`_priv_threshold_count` 同口径直读）。"""
        _prev, cur_start, cur_end = self._cycle_pivots(cycle)
        if kind == GREETING_MORNING:
            return _prev, cur_start
        return cur_start, cur_end


    #: 按流问候去重标记的落盘文件名（P5 余项/裁决⑩；§31.1 marks v2 连锁桶）
    _GREETING_MARKS_FILE = "greeting_stream_marks.json"

    def _stream_marks_path(self) -> Optional[Path]:
        """去重标记文件路径（data_dir 不可得 → None，调用方降级为纯内存）。"""
        data_dir = getattr(self._plugin, "_data_dir", None)
        return Path(data_dir) / self._GREETING_MARKS_FILE if data_dir else None

    def _load_stream_marks(self) -> Dict[str, Any]:
        """marks **v2** 读取（实例内缓存一次；热重载后新实例重新读盘）。

        v2 形态 = ``{"version": 2, "marks": {stream_id: {kind: cycle_start}}}``——按流按
        kind 记**最新已发 cycle_start**（epoch 秒，唯一值替换）。``version≠2``（含旧 v1
        按「掷点日→kind」两层结构）→ **视为不存在**（空表；§31.1：旧 v1 数据直接视为
        不存在，下一次写盘自然升版覆盖）。
        """
        if self._stream_greeting_marks is None:
            path = self._stream_marks_path()
            data = load_json(path, {}) if path else {}
            marks: Any = None
            if isinstance(data, dict) and data.get("version") == 2:
                marks = data.get("marks")
            self._stream_greeting_marks = marks if isinstance(marks, dict) else {}
        return self._stream_greeting_marks

    def _stream_greeting_marked(self, stream_id: str, kind: str,
                                cycle_start: Any) -> bool:
        """该流该 kind 在该**周期**是否已发过（marks v2；文件不可用 → False 可空降级）。

        去重语义 = 同流同 kind 同周期只发一次：记录值 == 当前 ``cycle_start``（epoch 秒，
        唯一值读取）才判命中；跨周期记录不遮蔽新周期（新周期自然可发一次）。
        """
        try:
            marks = self._load_stream_marks()
            recorded = marks.get(str(stream_id), {}).get(str(kind))
            return recorded is not None and float(recorded) == float(cycle_start)
        except Exception:
            return False

    def _mark_stream_greeting(self, stream_id: str, kind: str, cycle_start: Any) -> None:
        """写 marks v2 连锁桶记录并落盘（按流按 kind 记最新已发 cycle_start，唯一值替换；
        失败仅告警不打断）。"""
        marks = self._load_stream_marks()
        try:
            per_stream = marks.setdefault(str(stream_id), {})
            per_stream[str(kind)] = float(cycle_start or 0.0)
        except Exception:
            logger.debug("写按流问候标记失败（忽略）", exc_info=True)
            return
        path = self._stream_marks_path()
        if path is not None:
            try:
                save_json_atomic(path, {"version": 2, "marks": marks})
            except Exception:
                logger.debug("落盘按流问候标记失败（忽略）", exc_info=True)

    def _greeting_deduped_by_persisted_marker(self, category: str, kind: str) -> bool:
        """TARGET 类问候的跨重载去重：查 affection 的按天标记（``morning_sent``/``night_sent``）。

        P5（2026-10-09 登记）：标记的语义域 =「目标私聊该 kind 今日已发」（**按天全局布尔**，
        非按流；日切清零见 affection），故只对 TARGET 类别查——群聊/其他私聊查全局标记会把
        「目标已发」误判为「该流已发」。§31.1（R7）后 TARGET 主去重 = marks v2（周期唯一
        值），本标记保留为按天护栏（patrol 观察共用，见 companion/patrol.py）。
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
        """放弃该流该 kind 的本周期问候（状态 + 决策日志；同周期不再触发）。"""
        entry["status"] = GREETING_STATUS_ABANDONED
        entry["reason"] = reason
        logger.info(
            f"问候放弃（{reason}）: stream={entry['stream_id']}, kind={entry['kind']}, "
            f"cycle={entry.get('cycle')}"
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
            # 宿主触发失败：状态保持 pending，下一 tick 重试——越过观测窗上界后由
            # _advance_greeting 的越界钳制 abandon(deadline)（§31.1 通则①「不得界后」
            # 同样约束重试路径，不再有旧口径「到期限自然放弃」的无限等待形态）。
            return
        entry["status"] = GREETING_STATUS_FIRED
        entry["fired_at"] = now_ts
        # marks v2（§31.1 连锁桶）：所有类别按流按 kind 落盘去重（重载后不重发），
        # 记录 = 最新已发 cycle_start（同周期只发一次）
        self._mark_stream_greeting(stream_id, kind, entry.get("cycle"))
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

    async def _trigger_greeting(self, stream_id: str, kind: str, date_str: str) -> bool:
        """走统一主动链触发一次早晚安问候（intent 提示词按 kind 分派）。

        **例行问候始终免预算**（2026-10-10：原 ``greeting_exempt_budget`` 开关删除、恒为
        免预算）——闸门与计数一起跳；陪伴侧的巡检/想念才走预算。``date_str`` = 周期
        档案日期（marks v2/决策日志外字段；早安 intent 的首个活动名经同周期日期帧取条目）。
        """
        if kind == GREETING_MORNING:
            first = self._first_awake_item(date_str)
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

    # ------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------

    # 注（§24.2 任务B，2026-10-21）：原 ``_in_sleep_window`` 已删除——零调用死代码
    # （盘点依据 §24.0；R7 依据 = 计划书 §24.2 用户指示）。真实作息锚点落地后
    # 保留该副本只会成为"配置口径假源"，睡眠环判定统一收敛到
    # planning/schedule_anchors.py（连锁消费统一经 patrol 的真源入口）。

    # ------------------------------------------------------------
    # 诊断快照：指定流的下一次早/晚安触发状态（§26.2）
    # ------------------------------------------------------------

    def _message_query_available(self) -> bool:
        """宿主是否具备 ``ctx.message.get_by_time_in_chat``（诊断快照的必需能力）。"""
        ctx_obj = getattr(self._plugin, "ctx", None)
        return callable(getattr(getattr(ctx_obj, "message", None),
                                "get_by_time_in_chat", None))

    async def _count_threshold_live(self, stream_id: str, kind: str, cycle: CycleState,
                                    need: int) -> Tuple[int, bool]:
        """其他私聊阈值的**非缓存**实数（§26.2/§31.3 诊断专用小助手，仅读）。

        判定窗与真实链路同源（:meth:`_priv_threshold_count`：早安 ``[prev.start, cur.start)``、
        晚安 ``[cur.start, 晚安期限)``,全部经周期四变量——§31.3「双阈值 M/N 与窗端」）。
        **不写任何缓存**；能力缺失/查询异常 → ``(0, False)``（fail-closed 按 0，宁不发）。
        """
        return await self._priv_threshold_count(stream_id, kind, cycle, need)

    def _peek_stream_greeting_mark(self, stream_id: str, kind: str, cycle_start: Any) -> bool:
        """只读查询 marks v2 连锁桶记录（§26.2 诊断ème：不写盘、不落实例缓存）。"""
        path = self._stream_marks_path()
        data = load_json(path, {}) if path is not None else {}
        marks: Any = None
        if isinstance(data, dict) and data.get("version") == 2:
            marks = data.get("marks")
        if not isinstance(marks, dict):
            return False
        try:
            recorded = marks.get(str(stream_id), {}).get(str(kind))
            return recorded is not None and float(recorded) == float(cycle_start)
        except Exception:
            return False

    def greeting_cycle_span(self) -> Dict[str, Optional[float]]:
        """诊断读口（§31.3，R7 = 用户 2026-10-23 第二轮直派）：周期四变量窗端。

        返回 ``{"prev_start", "cur_start", "cur_end", "anchored"}``——``/lbm_diag streams``
        的检查窗标题/查询窗与流状态窗端全部经本读口取数（无日历 0 点）。
        """
        now = self._tz_manager.get_now()
        cycle = self._cycle_ctx(now)
        _prev, cur_start, cur_end = self._cycle_pivots(cycle)
        prev_start = float(cycle.previous_cycle_start or 0.0)
        return {
            "prev_start": prev_start or None,
            "cur_start": cur_start or None,
            "cur_end": cur_end or None,
            "anchored": bool(getattr(cycle, "anchored", False)),
        }

    def _target_greeting_allowed_readonly(self) -> bool:
        """诊断用目标私聊准入判定（与 :meth:`_target_greeting_allowed` 同口径，纯读）。

        不调用 ``_note_list_problem``（它写 ``_greeting_list_warned`` 并记决策日志）；
        非法名单元素在诊断里按已忽略处理。
        """
        cfg = self._proactive_cfg()
        ids, _problems = self._clean_id_list(
            getattr(cfg, "proactive_private_ids", None), "private")
        mode = str(getattr(cfg, "proactive_private_list_mode", LIST_MODE_WHITELIST)
                   or LIST_MODE_WHITELIST)
        target = self._target_qq()
        if mode == LIST_MODE_BLACKLIST and target and target in set(ids):
            return False
        return True

    def _target_stream_id_readonly(self) -> str:
        """诊断用目标流 id（``plugin._stream_id``；陪伴关/名单排除/未配置 → 空串不入全集）。"""
        target_stream = str(getattr(self._plugin, "_stream_id", "") or "").strip()
        if not target_stream or not self._companion_enabled() \
                or not self._target_greeting_allowed_readonly():
            return ""
        return target_stream

    async def _resolve_group_streams_readonly(self) -> List[str]:
        """诊断用群聊生效集（与 :meth:`_resolve_group_streams` 同口径，不写解析缓存/告警）。"""
        cfg = self._proactive_cfg()
        ids, _problems = self._clean_id_list(
            getattr(cfg, "proactive_group_ids", None), "group")
        mode = str(getattr(cfg, "proactive_group_list_mode", LIST_MODE_BLACKLIST)
                   or LIST_MODE_BLACKLIST)
        if mode == LIST_MODE_WHITELIST:
            if not ids:
                return []
            pairs: List[Tuple[str, Optional[str]]] = [
                (gid, await self._resolve_group_id(gid, use_cache=False)) for gid in ids
            ]
        else:
            excluded = set(ids)
            pairs = [(gid, sid) for gid, sid in await self._enumerate_group_pairs()
                     if gid not in excluded]
        return [sid for _gid, sid in pairs if sid]

    async def _resolve_private_streams_readonly(self) -> List[str]:
        """诊断用其他私聊生效集（与 :meth:`_resolve_private_streams` 同口径，不写解析缓存）。"""
        cfg = self._proactive_cfg()
        ids, _problems = self._clean_id_list(
            getattr(cfg, "proactive_private_ids", None), "private")
        mode = str(getattr(cfg, "proactive_private_list_mode", LIST_MODE_WHITELIST)
                   or LIST_MODE_WHITELIST)
        target_qq = self._target_qq()
        if mode == LIST_MODE_WHITELIST:
            if not ids:
                return []
            pairs: List[Tuple[str, Optional[str]]] = [
                (uid, await self._resolve_private_id(uid, use_cache=False)) for uid in ids
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
                continue
            result.append(sid)
        return result

    async def _resolve_greeting_candidates_readonly(self, cfg_p: Any = None,
                                                    now: Optional[datetime] = None,
                                                    date_str: Optional[str] = None
                                                    ) -> List[Tuple[str, str]]:
        """诊断用问候候选解析（§26.2；与 :meth:`_resolve_greeting_candidates` 同口径，纯读）。

        **纯读**：不写解析缓存/告警；§31.1 起群聊不做门槛预滤（硬闸逐 kind 判，与主链
        同口径）；§31.3 三流分族（R7 = 用户 2026-10-23 第二轮直派）起**目标流也返回入
        集**（``target`` 类）——序号仍以群聊→其他私聊在前、目标流殿后面（跨帧稳定）。
        ``cfg_p`` 仅为调用点兼容保留。
        """
        candidates: List[Tuple[str, str]] = []
        seen: set = set()
        for sid in await self._resolve_group_streams_readonly():
            if sid and sid not in seen:
                candidates.append((sid, GREETING_CATEGORY_GROUP))
                seen.add(sid)
        for sid in await self._resolve_private_streams_readonly():
            if sid and sid not in seen:
                candidates.append((sid, GREETING_CATEGORY_OTHER))
                seen.add(sid)
        target = self._target_stream_id_readonly()
        if target and target not in seen:
            candidates.append((target, GREETING_CATEGORY_TARGET))
            seen.add(target)
        return candidates

    async def _resolve_target_streams_readonly(self) -> List[str]:
        """诊断用频率调控候选枚举（纯读；不写解析缓存/名单告警）。"""
        streams: List[str] = []
        seen: set = set()
        for sid in ((await self._resolve_group_streams_readonly())
                    + (await self._resolve_private_streams_readonly())):
            if sid and sid not in seen:
                streams.append(sid)
                seen.add(sid)
        return streams

    async def _resolve_greeting_streams_universe_readonly(
            self) -> List[Tuple[str, str]]:
        """诊断用问候名单**全集**枚举（§29.1 基底 + §31.3 三流分族，R7 = 用户 2026-10-23
        第二轮直派）。

        顺序 = **群聊（枚举序）→ 其他私聊（枚举序）→ 目标私聊（无条件族）**——群/他族
        序号语义与 §29.1 不变（同序号恒指向同一流、跨帧稳定），目标流**新增**进全集
        （「目标流=无条件」 GH diag 显示面需要；殿后附号保证既有序号零漂移）。
        """
        pairs = self._dedup_category_streams(
            (await self._resolve_group_streams_readonly()),
            (await self._resolve_private_streams_readonly()))
        target = self._target_stream_id_readonly()
        if target and target not in {sid for sid, _cat in pairs}:
            pairs.append((target, GREETING_CATEGORY_TARGET))
        return pairs

    @staticmethod
    def _dedup_category_streams(group_sids: List[str],
                                other_sids: List[str]) -> List[Tuple[str, str]]:
        """群聊流表 → 其他私聊流表的**去重**合并（全集枚举的公用收口；保持枚举序）。"""
        pairs: List[Tuple[str, str]] = []
        seen: set = set()
        for sid, cat in [(sid, GREETING_CATEGORY_GROUP) for sid in group_sids] \
                + [(sid, GREETING_CATEGORY_OTHER) for sid in other_sids]:
            if sid and sid not in seen:
                pairs.append((sid, cat))
                seen.add(sid)
        return pairs

    async def _greeting_category_of(self, stream_id: str, cfg_p: Any,
                                    now: Optional[datetime] = None
                                    ) -> Tuple[Optional[str], bool]:
        """诊断用类别解析：``(category 或 None, 解析是否成功)``（§26.2 同帧口径）。

        §29.1 名单语义 + §31.3 三流分族（R7 = 用户 2026-10-23 第二轮直派）：在只读全集
        枚举（:meth:`_resolve_greeting_streams_universe_readonly`）内判类别——群聊 ``group``、
        其他私聊 ``other``、目标流 ``target``（无条件族入集后回归诊断可见）；不在全集 →
        ``(None, True)``（调用方再判「仅频率调控」与否）；解析抛异常 → ``(None, False)``。
        ``cfg_p``/``now`` 仅为调用点兼容保留。
        """
        try:
            universe = await self._resolve_greeting_streams_universe_readonly()
        except Exception as exc:
            logger.debug(f"诊断问候候选解析失败: {exc}")
            return None, False
        for cand_sid, cand_cat in universe or []:
            if str(cand_sid) == stream_id:
                return str(cand_cat), True
        return None, True

    @staticmethod
    def _fmt_span(ts: Any) -> str:
        """epoch 秒 → ``YYYY-MM-DD HH:MM``（窗端显示；非法/非正 → 空串）。"""
        try:
            value = float(ts)
        except (TypeError, ValueError):
            return ""
        if value <= 0:
            return ""
        try:
            return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError, OSError, OverflowError):
            return ""

    def _greeting_timing_text(self, *, entry: Optional[Dict[str, Any]], now_ts: float,
                              fire_lo: float, fire_hi: float, observe_until: float) -> str:
        """掷点时点诊断文本（四类新文案以外的既有时点档；§26.2 契约保留）。"""
        if now_ts >= fire_hi:
            return "已满足（下一 tick 将发送）"
        if entry is not None or int(fire_lo) == int(fire_hi):
            point = float(entry.get("fire_at") or 0) if entry else fire_lo
            if now_ts >= point:
                return "已满足（下一 tick 将发送）"
            return f"将发送（约 {self._fmt_hhmm(point)}）"
        return (f"将发送（约 {self._fmt_hhmm(fire_lo)}"
                f"–{self._fmt_hhmm(fire_hi)}）")

    async def _greeting_kind_status(self, stream_id: str, category: Optional[str], kind: str,
                                    now: datetime, now_ts: float, date_str: str,
                                    cfg_p: Any,
                                    cycle: Optional[CycleState] = None
                                    ) -> Dict[str, Any]:
        """单个 kind 的触发状态快照（纯读；§26.2 结论行 + 判定关键量；§31.3 重排）。

        结论档（§31.3 新机制，R7 = 用户 2026-10-23 第二轮直派）：

        - 既有档：``不适用（总开关关闭）`` / ``今日已发`` / ``已放弃（原因）`` /
          ``已满足（下一 tick 将发送）`` / ``将发送（约 HH:MM 或区间）`` /
          ``未满足（差门槛 M/N，窗内剩 X 分钟）`` / ``未满足（差必要性激活，窗内剩 X
          分钟）``（旧「差激活」语义承自 §27.1 necessity，R7 重新锚定窗） / ``取不到…``；
        - **四类新文案**（§31.3「用户 Q8 可写」）：``早安未开始（连锁待定）`` /
          ``因早安未开口→晚安连锁硬性忽略`` / ``连锁已通过``（后接时点档） /
          ``数量闸达标 M/N``（后接时点档）；目标流新增「``无条件``」前承；
        - 判定窗/阈值窗/期限全部经周期四变量（:meth:`_greeting_frames` 等），不再看日历
          0 点；``threshold`` 结构（§26.2 契约）保留（``cached`` 恒 None——§31.1 废止
          端点冻结缓存，夜窗逐 tick 重查）。
        """
        enabled = self._greeting_kind_enabled(kind)
        if cycle is None:
            cycle = self._cycle_ctx(now)
        cycle_key = float(cycle.current_cycle_start or 0.0)
        entry = self._greeting_rolls.get((stream_id, cycle_key, kind))
        marked = self._peek_stream_greeting_mark(stream_id, kind, cycle_key)
        if not marked and category == GREETING_CATEGORY_TARGET:
            marked = self._greeting_deduped_by_persisted_marker(category or "", kind)

        need = count = 0
        gate_query_ok = True
        cached_gate: Optional[bool] = None
        threshold_met = True
        # 早安沉默闸（R7 = 用户 2026-10-24 直派）：N>0 时早安全族需要消息查询能力
        silence_hours = self._morning_silence_hours(cfg_p)
        silence_gate: Optional[bool] = None
        silence_gate_evaluated = False   # 仅在结论链 else 分支实查后为真（早退档不显示明细）
        if category == GREETING_CATEGORY_OTHER and self._message_query_available():
            need = self._threshold_need(cfg_p, kind)
            count, gate_query_ok = await self._count_threshold_live(
                stream_id, kind, cycle, need)
            threshold_met = bool(count >= need) if gate_query_ok else False

        activated: Optional[bool] = None
        fire_at: Optional[float] = None
        observe_until: Optional[float] = None
        remaining_minutes = 0
        detail_parts: List[str] = []
        win_start, _cur, cur_end = self._cycle_pivots(cycle)
        cur_start = _cur
        _baseline, kind_deadline = self._greeting_frames(kind, cycle)
        # 判定窗（§31.1）：早安 [prev.start, cur.start) / 晚安 [cur.start, night_deadline)
        judge_lo = win_start if kind == GREETING_MORNING else cur_start
        judge_hi = cur_start if kind == GREETING_MORNING else cur_end
        win_text = (f"窗={self._fmt_span(judge_lo)} → {self._fmt_span(judge_hi)}"
                    )

        if not enabled:
            conclusion = "不适用（早安总开关关闭）" if kind == GREETING_MORNING \
                else "不适用（晚安总开关关闭）"
        elif marked:
            conclusion = "今日已发"
        elif entry is not None and entry.get("status") == GREETING_STATUS_FIRED:
            conclusion = "今日已发"
        elif entry is not None and entry.get("status") == GREETING_STATUS_ABANDONED:
            reason = str(entry.get("reason") or "unknown")
            conclusion = (f"已放弃（{_GREETING_ABANDON_REASON_CN.get(reason, reason)}，"
                          f"原因={reason}）")
        elif not self._message_query_available() and (
                (category == GREETING_CATEGORY_GROUP and kind == GREETING_MORNING)
                or category == GREETING_CATEGORY_OTHER
                or (kind == GREETING_MORNING and silence_hours > 0)):
            # 取不到必需数据（闸窗/需门槛计数的族）：不猜，不落任何状态（宁不发）
            conclusion = "取不到（缺消息查询能力，宁不发）"
        else:
            if entry is not None:
                observe_until = float(entry.get("observe_until")
                                      or entry.get("deadline") or now_ts)
                fire_at = float(entry.get("fire_at") or 0) or None
                fire_lo = fire_hi = fire_at
            else:
                # 尚未掷点：按基准/期限/漂移区间估算（不调用 _roll_greeting，不写掷点表）
                baseline, deadline = _baseline, kind_deadline
                drift_lo, drift_hi = self._greeting_drift(kind)
                if kind == GREETING_MORNING:
                    fire_lo = baseline + drift_lo * 60.0
                    fire_hi = baseline + drift_hi * 60.0
                else:
                    fire_lo = deadline - drift_hi * 60.0
                    fire_hi = deadline - drift_lo * 60.0
                observe_until = max(deadline, fire_hi)
            remaining_minutes = max(0, int((observe_until - now_ts) / 60.0))
            timing = self._greeting_timing_text(entry=entry, now_ts=now_ts,
                                                fire_lo=fire_lo or 0.0,
                                                fire_hi=fire_hi or 0.0,
                                                observe_until=observe_until or 0.0)

            # 早安沉默闸（R7 = 用户 2026-10-24 直派）：只读实查一次；未过 → 新结论档并
            # 短路其余档位（闸在 _pre_gate 是 durable 拒绝，不会有条目/掷点可报）
            silence_gate = None
            if kind == GREETING_MORNING and silence_hours > 0 and self._message_query_available():
                silence_gate = await self._morning_silence_gate(stream_id, now_ts, cycle, cfg_p)
                silence_gate_evaluated = True

            if silence_gate is None and kind == GREETING_MORNING and silence_hours > 0:
                conclusion = "取不到（沉默闸查询失败，宁不发）"
            elif silence_gate is False:
                conclusion = f"沉默闸未过（第一日程前 {silence_hours:g} 小时内 bot 发过消息）"
            elif category == GREETING_CATEGORY_GROUP:
                if kind == GREETING_MORNING:
                    hard = await self._group_morning_hard_gate(stream_id, win_start, cur_start)
                    if hard is None:
                        conclusion = "取不到（硬闸查询失败，宁不发）"
                    elif not hard:
                        conclusion = "硬闸不过（窗内无非 bot 消息）"
                    elif self._greeting_requires_activation(cfg_p):
                        activated = await self._group_necessity_gate(stream_id, cycle)
                        if activated is None or not activated:
                            conclusion = (f"未满足（差必要性激活，"
                                          f"窗内剩 {remaining_minutes} 分钟）")
                        else:
                            conclusion = timing
                    else:
                        conclusion = timing
                else:
                    state = self._greeting_chain_state(stream_id, cycle, now_ts, cfg_p)
                    if state == "sent":
                        conclusion = f"连锁已通过，{timing}"
                    elif state == "inflight":
                        conclusion = "早安未开始（连锁待定）"
                    else:
                        conclusion = "因早安未开口→晚安连锁硬性忽略"
            elif category == GREETING_CATEGORY_OTHER:
                if kind == GREETING_NIGHT and self._greeting_chain_pass(stream_id, cycle):
                    conclusion = f"连锁已通过，{timing}"
                elif not gate_query_ok:
                    conclusion = "取不到（门槛查询失败，按 0 不发送）"
                elif threshold_met:
                    conclusion = f"数量闸达标 {count}/{need}，{timing}"
                else:
                    conclusion = (f"未满足（差门槛 {count}/{need}，"
                                  f"窗内剩 {remaining_minutes} 分钟）")
            else:  # TARGET：无条件（§31.1 激活强制解除）
                conclusion = f"无条件，{timing}"

        if category is not None:
            if activated is not None:
                detail_parts.append("激活=" + ("是" if activated else "否"))
            if category == GREETING_CATEGORY_OTHER:
                query_text = "实查" if gate_query_ok else "实查失败按0"
                detail_parts.append(
                    f"阈值={count}/{need}（对方发送，{query_text}，{win_text}）")
            elif category == GREETING_CATEGORY_TARGET:
                detail_parts.append("门槛=不适用（目标流无门槛/无条件）")
                detail_parts.append(win_text)
            else:
                detail_parts.append(f"硬闸窗={self._fmt_span(win_start)} → "
                                    f"{self._fmt_span(cur_start)}")
                detail_parts.append(
                    "必要性=" + ("开" if self._greeting_requires_activation(cfg_p) else "关"))
                if kind == GREETING_NIGHT:
                    state = self._greeting_chain_state(stream_id, cycle, now_ts, cfg_p)
                    detail_parts.append("连锁=已通过" if state == "sent"
                                        else ("连锁=待定" if state == "inflight"
                                              else "连锁=已失效"))
                detail_parts.append(win_text)
            if entry is not None:
                roll_text = str(entry.get("status") or "unknown")
                if entry.get("reason"):
                    roll_text += f"({entry['reason']})"
                detail_parts.append(f"掷点={roll_text}")
            else:
                detail_parts.append("掷点=未掷点")
            point_ts = fire_at if fire_at is not None else None
            if point_ts is None and observe_until is not None:
                detail_parts.append(f"期限={self._fmt_hhmm(observe_until)}")
            elif point_ts is not None:
                detail_parts.append(f"点={self._fmt_hhmm(point_ts)}")
            # 补发冷静期余量（§31.3「冷静期余量」；过点才达标条目才有此行）
            if entry is not None and entry.get("gate_over_point"):
                silence = self._silence_minutes(cfg_p)
                calm_ok, quiet, _last = await self._calm_gate(
                    stream_id, silence, now_ts, cycle)
                if silence <= 0:
                    detail_parts.append("补发冷静期=0（过点即时补发）")
                elif calm_ok:
                    detail_parts.append(f"补发冷静期=已满足（阈值 {silence} 分钟）")
                else:
                    remain = max(0.0, silence - quiet)
                    detail_parts.append(
                        f"补发冷静期=差 {remain:.0f} 分钟（沉默 {quiet:.0f}/{silence} 分钟）")
            # 早安沉默闸明细（R7 = 用户 2026-10-24 直派；与 _pre_gate 同源取数，N≤0 = 关闭；
            # 只在结论链实查过才显示——「今日已发/已放弃」等早退档不带此行）
            if kind == GREETING_MORNING and silence_hours > 0 and silence_gate_evaluated:
                if silence_gate is None:
                    gate_text = "取不到（宁不发）"
                elif silence_gate:
                    gate_text = "通过"
                else:
                    gate_text = "未过"
                detail_parts.append(
                    f"沉默闸={gate_text}（窗={self._fmt_span(cur_start - silence_hours * 3600.0)}"
                    f" → {self._fmt_span(cur_start)}，N={silence_hours:g}h）")
            detail_parts.append("标记=" + ("是" if marked else "否"))

        return {
            "kind": kind,
            "enabled": enabled,
            "conclusion": conclusion,
            "detail": " / ".join(detail_parts),
            "activated": activated,
            "marked": marked,
            "threshold": {
                "applicable": category == GREETING_CATEGORY_OTHER,
                "need": need,
                "count": count,
                "met": threshold_met,
                "cached": cached_gate,
                "query_ok": gate_query_ok,
            },
            "roll": (dict(entry) if entry is not None else {}),
            "fire_at": fire_at,
            "observe_until": observe_until,
        }

    @staticmethod
    def _fmt_hhmm(ts: float) -> str:
        """epoch 秒 → ``HH:MM``（本地时区；非法时间戳回显空串）。"""
        try:
            return datetime.fromtimestamp(float(ts)).strftime("%H:%M")
        except (TypeError, ValueError, OSError, OverflowError):
            return ""

    async def greeting_trigger_status(self, sid: str,
                                      now: Optional[datetime] = None) -> Dict[str, Any]:
        """指定聊天流的下一次早安/晚安触发状态快照（§26.2 / §31.3 重排；**纯读**）。

        语义（R7 = 用户 2026-10-23 第二轮直派，计划书 §31.1/§31.3）：

        - **类别**：只读全集枚举内判类别（§29.1 名单语义 + §31.3 三流分族）——
          ``group`` / ``other`` / ``target``（**目标流回归诊断可见，「无条件」结论**）；
        - **两类结论** = 既有档（今日已发 / 已放弃 / 已满足 / 将发送 / 未满足差门槛 /
          未满足差必要性激活 / 不适用 / 取不到）+ 四类新文案（早安未开始（连锁待定）/
          因早安未开口→晚安连锁硬性忽略 / 连锁已通过 / 数量闸达标 M/N）+ 无条件前承；
        - **判定窗/阈值窗/期限/连锁桶/marks v2** 全部经周期四变量（``target`` 类无判定窗）；
        - **只读纪律**：不调用掷点/标记写入/Affection 写入路径；只做消息查询与内存读取。

        返回 ``{"stream_id", "date", "category", "resolved", "frequency_only", "note",
        "kinds": {"morning": {...}, "night": {...}}}``；每个 kind 含 ``conclusion`` /
        ``detail`` / ``activated`` / ``marked`` / ``threshold`` / ``roll`` / ``fire_at`` /
        ``observe_until``（§26.2 契约字段全保留）。
        """
        stream_id = str(sid or "").strip()
        now_dt = now or self._tz_manager.get_now()
        date_str = now_dt.strftime("%Y-%m-%d")
        now_ts = _epoch_ts(now_dt)
        cfg_p = self._proactive_cfg()
        cycle = self._cycle_ctx(now_dt)

        category, resolved = await self._greeting_category_of(stream_id, cfg_p, now=now_dt)
        frequency_only = False
        if resolved and category is None:
            try:
                frequency_only = stream_id in (await self._resolve_target_streams_readonly())
            except Exception as exc:
                logger.debug(f"诊断频率调控候选解析失败: {exc}")
                frequency_only = False

        if not resolved:
            note = "取不到（问候候选解析失败，宁不发）"
        elif category is None:
            note = ("不适用（仅频率调控，不在问候候选）" if frequency_only
                    else "不适用（不在问候候选）")
        else:
            note = ""

        kinds: Dict[str, Any] = {}
        for kind in (GREETING_MORNING, GREETING_NIGHT):
            if note:
                kinds[kind] = {
                    "kind": kind,
                    "enabled": False,
                    "conclusion": note,
                    "detail": "",
                    "activated": None,
                    "marked": False,
                    "threshold": {"applicable": False, "need": 0, "count": 0,
                                  "met": False, "cached": None, "query_ok": False},
                    "roll": {},
                    "fire_at": None,
                    "observe_until": None,
                }
            else:
                kinds[kind] = await self._greeting_kind_status(
                    stream_id, category, kind, now_dt, now_ts, date_str, cfg_p,
                    cycle=cycle)

        return {
            "stream_id": stream_id,
            "date": date_str,
            "category": category or "",
            "resolved": resolved,
            "frequency_only": frequency_only,
            "note": note,
            "kinds": kinds,
        }

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

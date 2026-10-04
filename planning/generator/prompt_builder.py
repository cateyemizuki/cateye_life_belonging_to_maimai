"""Prompt Builder Module.

This module provides prompt building functionality for schedule generation.
Separated from BaseScheduleGenerator to follow Single Responsibility Principle.

v4 改造：
    - 全局配置（personality / bot.nickname 等）由 ``config`` 字典中的 ``bot_profile`` 段提供
    - ``bot_profile`` 由 ``ToolsService`` 在构造 ScheduleGenerator 前从插件 ctx 拉取

v4.6.0 改造：
    - 作息语义重构：``wake_time``（起床/睡醒时间）与 ``sleep_time``（入睡时间）成为
      日程的两个锚点——清醒活动排在 [wake_time, sleep_time)，睡眠时段为
      [sleep_time, 次日 wake_time)；入睡早于起床表示跨午夜夜猫子作息
    - 无睡眠模式与提示词框架完全兼容：JSON 示例、无缝衔接演算、时间合理性框架
      均按 wake/sleep + no_sleep_mode 动态生成，不再出现"示例教模型写睡觉、
      正文却禁止睡觉"的自相矛盾

v4.7.0 改造：
    - 无睡眠模式语义变更：睡眠时段不再是"无所事事"，而是**不生成任何日程条目**
      （LLM 生成后由 ScheduleGenerator 后处理销毁该时段日程）。示例、无缝演算、
      时间框架均只覆盖清醒时段 [wake_time, sleep_time)
"""

from typing import Any, Dict, List, Optional, Tuple
import logging

from ..time_utils import TimezoneManager

logger = logging.getLogger(__name__)

# bot_profile 缺失时显式报错暴露配置问题，不再用任何 fallback 默认值
# （主程序正常情况下 bot.nickname / personality.personality 一定能拉到；
#  拿不到说明 IPC 异常或主程序配置缺失，需要在日志里红色报警让用户立刻修）

# 作息解析默认值（配置留空/非法时兜底）：07:00 起床，23:00 入睡
_DEFAULT_WAKE_MINUTES = 7 * 60
_DEFAULT_SLEEP_MINUTES = 23 * 60


#: 情绪五档词中代表"低/高"的两组（与 emotion/vad.py `_level` 的词表一致；§6.2 F 引导约束按此分档）
_EMOTION_LEVEL_LOW = ("很低", "偏低")
_EMOTION_LEVEL_HIGH = ("很高", "偏高")
#: M5 连续低迷软约束的触发门槛（与 emotion/state_machine.LOW_MOOD_MIN_DAYS 同值；prompt 注明用）
LOW_MOOD_MIN_DAYS = 3


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def build_emotion_context(mood: str, levels: Optional[Dict[str, str]] = None,
                          influence: float = 0.6) -> str:
    """拼「睡醒情绪描述 + 引导约束」上下文（M5；§6.2 F / SYNERGY §3.2）。

    - ``influence <= 0`` 或 ``mood`` 为空 → 返回空串（不注入，等价关闭）；
    - ``influence < 0.5`` → 精简模式：只保留 1 条引导约束；``>= 0.5`` → 按三轴命中输出全部约束；
    - ``levels`` 为情绪低分辨率三档词（emotion.overhauled payload 的 ``levels``：
      valence/arousal/dominance → 很低/偏低/中等/偏高/很高）；缺失时退化为通用引导；
    - 只使用低分辨率词与行为约束，**不含任何 VAD 数值**（§6.3-6 LLM 隔离）。
    """
    text = str(mood or "").strip()
    if not text:
        return ""
    try:
        strength = float(influence)
    except (TypeError, ValueError):
        strength = 0.6
    if strength <= 0:
        return ""
    axis = levels if isinstance(levels, dict) else {}
    constraints: List[str] = []
    valence = str(axis.get("valence") or "")
    arousal = str(axis.get("arousal") or "")
    dominance = str(axis.get("dominance") or "")
    if valence in _EMOTION_LEVEL_LOW:
        constraints.append("安排更多 rest / entertainment / social_maintenance 类活动，学习与工作强度调低")
    elif valence in _EMOTION_LEVEL_HIGH:
        constraints.append("可适当增加 entertainment / social_maintenance 类活动，保持轻快节奏")
    if arousal in _EMOTION_LEVEL_HIGH:
        constraints.append("可安排运动或社交把精力用出去，避免整天闷在家里")
    elif arousal in _EMOTION_LEVEL_LOW:
        constraints.append("避免连续高强度条目，给上午留一段缓冲")
    if dominance in _EMOTION_LEVEL_LOW:
        constraints.append("保留一段 relaxation / free_time，避免连续高压条目")
    if not constraints:
        constraints.append("按上述情绪基调调整活动安排（低落时多安排恢复类活动、兴奋时多安排运动与社交）")
    if strength < 0.5:
        constraints = constraints[:1]
    body = "；".join(f"{idx}) {line}" for idx, line in enumerate(constraints, 1))
    return (f"【睡醒情绪底色】\n昨晚睡醒时的情绪：{text}\n"
            f"引导约束：{body}\n"
            "要求：情绪只影响活动类型与强度的微调，不得改变作息锚点、三餐窗口与无缝衔接要求。")


def _parse_hhmm(value: Any, default_minutes: int) -> int:
    """把 HH:MM 解析为当天分钟数；非法/留空回退默认值。"""
    try:
        parts = str(value or "").strip().split(":")
        hour, minute = int(parts[0]), int(parts[1])
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour * 60 + minute
    except (ValueError, IndexError, TypeError):
        pass
    return default_minutes


def _fmt_minutes(minutes: int) -> str:
    """分钟数 → HH:MM（自动回绕 24 小时）。"""
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


class PromptBuilder:
    """提示词构建器 - 单一职责：构建 LLM 提示词

    该类负责：
        1. 构建初始日程生成提示词
        2. 构建带反馈的重试提示词
        3. 整合配置、上下文、Schema 约束

    v4 改造：
        - 全局配置通过 ``config["bot_profile"]`` 字典传入（由插件层在 ``on_load``
          时一次性拉取并缓存），不再运行时调用 ``config_api``
    """

    def __init__(self, config: Dict[str, Any], tz_manager: TimezoneManager):
        """初始化提示词构建器。

        Args:
            config: 配置字典，期望含 ``bot_profile`` 子段：
                ``{"personality": str, "reply_style": str, "interest": str, "bot_name": str}``
            tz_manager: 时区管理器（用于获取当前时间）
        """
        self.config = config
        self.tz_manager = tz_manager

    def _get_cached_config(self) -> tuple[str, str, str, str]:
        """读取由插件层注入的 bot 配置（无 IO，纯字典访问）。

        Returns:
            ``(personality, reply_style, interest, bot_name)`` 元组；
            personality / bot_name 缺失时返回空串并打 ERROR 日志，
            调用方应据此调整 prompt 拼接（避免出现"你是 ，"这种悬挂表述）。
        """
        bot_profile: Dict[str, Any] = self.config.get("bot_profile", {}) or {}
        personality = str(bot_profile.get("personality") or "").strip()
        reply_style = str(bot_profile.get("reply_style") or "").strip()
        interest = str(bot_profile.get("interest") or "").strip()
        bot_name = str(bot_profile.get("bot_name") or "").strip()

        if not personality:
            logger.error(
                "❌ bot_profile.personality 未配置或为空 —— 生成的日程将无法贴合角色人设。"
                "请在主程序 bot 配置中填写 personality.personality 字段。"
            )
        if not bot_name:
            logger.error(
                "❌ bot_profile.bot_name 未配置或为空 —— prompt 中将不出现角色名。"
                "请在主程序 bot 配置中填写 bot.nickname 字段。"
            )
        return personality, reply_style, interest, bot_name

    # ------------------------------------------------------------
    # 作息解析
    # ------------------------------------------------------------

    def _resolve_schedule_bounds(self) -> Tuple[int, int]:
        """解析 (起床分钟, 入睡分钟)。"""
        wake_min = _parse_hhmm(self.config.get("wake_time"), _DEFAULT_WAKE_MINUTES)
        sleep_min = _parse_hhmm(self.config.get("sleep_time"), _DEFAULT_SLEEP_MINUTES)
        return wake_min, sleep_min

    # ------------------------------------------------------------
    # 动态 JSON 示例（按作息锚点生成，保证与正文要求自洽）
    # ------------------------------------------------------------

    def _build_example_items(
        self,
        wake_min: int,
        sleep_min: int,
        no_sleep: bool,
        enable_detailed: bool,
    ) -> List[Dict[str, Any]]:
        """按起床/入睡锚点生成一份示例日程。

        - 清醒时段 [wake, sleep) 按比例铺开：起床/三餐/学习/运动/娱乐，
          单个活动不超过 3.5 小时（超出自动拆成两条）；
        - 睡眠时段 [sleep, 次日 wake)：正常模式为"睡觉"收尾闭环；
          无睡眠模式**不生成任何条目**（该时段留空，日程止于入睡时刻）；
        - 所有清醒活动首尾相接：每个活动结束时间 = 下一个活动开始时间。
        """
        seg = (sleep_min - wake_min) % (24 * 60)
        if seg == 0:
            seg = 24 * 60
        sleep_minutes = 24 * 60 - seg

        desc = "（示例描述：按人设写一段自然叙述）" if enable_detailed else ""

        # 极短清醒时段（< 8 小时）：使用极简示例，避免按比例铺开出现负时长
        if seg < 480:
            items: List[Dict[str, Any]] = []
            add = None  # 占位，下方闭包内重新绑定
            def _add(name: str, start: int, minutes: int, goal_type: str, priority: str) -> None:
                items.append({
                    "name": name,
                    "description": desc,
                    "goal_type": goal_type,
                    "priority": priority,
                    "time_slot": _fmt_minutes(start),
                    "duration_hours": round(minutes / 60, 2),
                })
            if not no_sleep:
                _add("睡觉", sleep_min, sleep_minutes, "daily_routine", "high")
            _add("起床洗漱", wake_min, 30, "daily_routine", "medium")
            _add("早餐", wake_min + 30, 30, "meal", "high")
            flex = seg - 150
            half1 = max(30, flex // 2)
            _add("上午活动", wake_min + 60, half1, "study", "high")
            _add("午餐", wake_min + 60 + half1, 30, "meal", "high")
            half2 = max(30, flex - half1)
            _add("下午活动", wake_min + 90 + half1, half2, "learn_topic", "medium")
            _add("晚餐", wake_min + 90 + half1 + half2, 30, "meal", "high")
            return items

        def add(items: List[Dict[str, Any]], name: str, start: int, minutes: int,
                goal_type: str, priority: str) -> None:
            items.append({
                "name": name,
                "description": desc,
                "goal_type": goal_type,
                "priority": priority,
                "time_slot": _fmt_minutes(start),
                "duration_hours": round(minutes / 60, 2),
            })

        items: List[Dict[str, Any]] = []

        # 睡眠块（锚定入睡时刻，绕时钟闭环的收尾）；无睡眠模式不生成该块
        if not no_sleep:
            add(items, "睡觉", sleep_min, sleep_minutes, "daily_routine", "high")

        # 清醒块（锚定起床时刻）
        add(items, "起床洗漱", wake_min, 30, "daily_routine", "medium")
        add(items, "早餐", wake_min + 30, 30, "meal", "high")

        cursor = wake_min + 60
        remaining = seg - 60

        morning = min(210, max(60, int(remaining * 0.35)))
        add(items, "上午学习", cursor, morning, "study", "high")
        cursor += morning
        remaining -= morning

        add(items, "午餐", cursor, 30, "meal", "high")
        cursor += 30
        remaining -= 30

        if remaining >= 240:
            nap_minutes = 30
            if no_sleep:
                add(items, "午后放空", cursor, nap_minutes, "rest", "low")
            else:
                add(items, "午休", cursor, nap_minutes, "daily_routine", "medium")
            cursor += nap_minutes
            remaining -= nap_minutes

        afternoon = min(210, max(30, int(remaining * 0.35)))
        add(items, "下午学习", cursor, afternoon, "study", "high")
        cursor += afternoon
        remaining -= afternoon

        exercise = min(90, max(30, int(remaining * 0.2)))
        add(items, "运动", cursor, exercise, "exercise", "medium")
        cursor += exercise
        remaining -= exercise

        # 晚餐固定锚定在入睡时刻前 4.5h（默认作息下落在 17-20 点区间）
        dinner_minutes = 30
        fun_minutes = 150
        wind_down_minutes = 90
        flex = remaining - dinner_minutes - fun_minutes - wind_down_minutes
        if flex >= 60:
            if flex > 210:
                half = flex // 2
                add(items, "兴趣活动", cursor, half, "learn_topic", "medium")
                add(items, "自由活动", cursor + half, flex - half, "free_time", "low")
            else:
                add(items, "兴趣活动", cursor, flex, "learn_topic", "medium")
            cursor += flex
        else:
            # 清醒时段太短：放弃兴趣活动，把差额从娱乐里扣掉，保持无缝
            fun_minutes = max(60, fun_minutes + flex)
        add(items, "晚餐", cursor, dinner_minutes, "meal", "high")
        cursor += dinner_minutes
        if no_sleep:
            add(items, "夜间放松", cursor, fun_minutes, "rest", "low")
        else:
            add(items, "娱乐", cursor, fun_minutes, "entertainment", "low")
        cursor += fun_minutes
        if no_sleep:
            add(items, "安静休闲", cursor, wind_down_minutes, "rest", "medium")
        else:
            add(items, "睡前准备", cursor, wind_down_minutes, "daily_routine", "medium")
        # cursor 此时回到 sleep_min，与开头的睡眠块首尾相接

        return items

    def _build_example_text(
        self,
        wake_min: int,
        sleep_min: int,
        no_sleep: bool,
        enable_detailed: bool,
        min_activities: int,
        max_activities: int,
    ) -> str:
        """把示例日程渲染成 prompt 里的 JSON 文本 + 无缝演算说明。"""
        items = self._build_example_items(wake_min, sleep_min, no_sleep, enable_detailed)
        scope_text = "清醒时段无缝衔接" if no_sleep else "全天无缝衔接"
        lines = [f"【JSON格式示例】（已按本角色作息锚点生成，展示{scope_text}）"]
        lines.append("{")
        lines.append('  "schedule_items": [')
        for idx, item in enumerate(items):
            comma = "," if idx < len(items) - 1 else ""
            desc = item["description"]
            lines.append(
                '    {{"name":"{name}","description":{desc},"goal_type":"{gtype}",'
                '"priority":"{prio}","time_slot":"{slot}","duration_hours":{dur}}}{comma}'.format(
                    name=item["name"],
                    desc=('"%s"' % desc) if enable_detailed else '""',
                    gtype=item["goal_type"],
                    prio=item["priority"],
                    slot=item["time_slot"],
                    dur=item["duration_hours"],
                    comma=comma,
                )
            )
        lines.append("  ]")
        lines.append("}")
        lines.append("")
        lines.append(f"（根据实际情况生成{min_activities}-{max_activities}个活动，以上仅为衔接方式示例）")
        lines.append("")
        if no_sleep:
            lines.append("⚠️ 重要：上面示例展示了清醒时段无缝衔接的正确方式！（睡眠时段不生成条目）")
        else:
            lines.append("⚠️ 重要：上面示例展示了全天无缝衔接的正确方式！")
        # 取前三个活动做衔接演算
        for i in range(min(3, len(items) - 1)):
            cur, nxt = items[i], items[i + 1]
            cur_m = int(cur['time_slot'][:2]) * 60 + int(cur['time_slot'][3:])
            nxt_m = int(nxt['time_slot'][:2]) * 60 + int(nxt['time_slot'][3:])
            next_label = f"次日 {nxt['time_slot']}" if nxt_m <= cur_m else nxt['time_slot']
            lines.append(
                f"- {cur['time_slot']} {cur['name']} + {cur['duration_hours']}h = {next_label} "
                f"→ {nxt['name']} ✅ 无缝"
            )
        last = items[-1]
        if no_sleep:
            # 无睡眠模式：日程止于入睡时刻，睡眠时段留空，次日起床开新一天
            sleep_text_ex = _fmt_minutes(sleep_min)
            wake_text_ex = _fmt_minutes(wake_min)
            lines.append(
                f"- {last['time_slot']} {last['name']} + {last['duration_hours']}h = {sleep_text_ex} "
                f"✅ 恰好到入睡时刻收尾"
            )
            lines.append(
                f"- 睡眠时段（{sleep_text_ex} → 次日 {wake_text_ex}）不生成任何日程条目，"
                f"次日 {wake_text_ex} 直接开始新一天 ✅ 完整闭环"
            )
        else:
            first_sleep = items[0]
            lines.append(
                f"- {last['time_slot']} {last['name']} + {last['duration_hours']}h = "
                f"次日 {first_sleep['time_slot']} ✅ 回到 {first_sleep['name']}，完整闭环"
            )
        lines.append("")
        lines.append("⚠️ duration_hours 是活动持续时长（小时），不是重复间隔！")
        return "\n".join(lines)

    # ------------------------------------------------------------
    # 主入口：日程生成提示词
    # ------------------------------------------------------------

    def build_schedule_prompt(
        self,
        schedule_type: str,
        preferences: Dict[str, Any],
        schema: Optional[Dict] = None,
        yesterday_context: Optional[str] = None,
        pending_commitments: Optional[List[Dict[str, Any]]] = None,
        history_context: str = "",
        knowledge_context: str = "",
        emotion_context: Optional[str] = None,
        low_mood_days: int = 0,
    ) -> str:
        """构建日程生成提示词

        Args:
            schedule_type: 日程类型（daily/weekly/monthly；当前生成管线固定按"今天"生成）
            preferences: 用户偏好
            schema: JSON Schema（可选）
            yesterday_context: 最近几天日程上下文（可选）
            pending_commitments: 今日需要纳入的约定列表（可选）
            history_context: 最近聊天背景（可选；跨群拼接）
            knowledge_context: 相关记忆参考（可选）
            emotion_context: 睡醒情绪描述 + 引导约束（可选；M5 §6.2 F，空串=不注入）
            low_mood_days: 连续低迷天数（M5 §6.2 F；≥3 时注明「最近有点低落」并要求安排恢复类活动）

        Returns:
            完整的提示词字符串
        """
        # 使用缓存的全局配置
        personality, reply_style, interest, bot_name = self._get_cached_config()

        # 从配置读取生成参数
        min_activities = self.config.get('min_activities', 8)
        max_activities = self.config.get('max_activities', 15)
        enable_detailed_description = self.config.get('enable_detailed_description', True)
        min_desc_len = self.config.get('min_description_length', 20)
        max_desc_len = self.config.get('max_description_length', 50)

        # 读取自定义prompt配置
        custom_prompt = self.config.get('custom_prompt', '').strip()

        # 作息锚点 + 无睡眠模式
        wake_min, sleep_min = self._resolve_schedule_bounds()
        no_sleep_mode = bool(self.config.get('no_sleep_mode', False))
        wake_text = _fmt_minutes(wake_min)
        sleep_text = _fmt_minutes(sleep_min)
        cross_midnight = sleep_min <= wake_min

        # 使用时区管理器获取时间信息
        today = self.tz_manager.get_now()
        date_str = today.strftime("%Y-%m-%d")
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        weekday = weekday_names[today.weekday()]
        is_weekend = today.weekday() >= 5

        # 最近几天日程上下文（向后兼容：变量名仍叫 yesterday_context，含义是"最近 N 天"摘要）
        yesterday_text = yesterday_context or "最近几天没有具体记录"
        has_real_yesterday = (
            yesterday_context
            and "记不太清" not in yesterday_text
            and "普通的" not in yesterday_text
            and "没有具体记录" not in yesterday_text
        )

        # 核心提示词（bot_name / personality 缺失时不出现悬挂逗号或孤立"你是"）
        if bot_name and personality:
            persona_line = f"你是{bot_name}，{personality}"
        elif bot_name:
            persona_line = f"你是{bot_name}"
        elif personality:
            persona_line = personality  # 没名字但有人设：直接讲人设
        else:
            persona_line = ""  # 全空：跳过 persona 行（已 logger.error）

        # reply_style 单独一段塞进 prompt
        style_block = f"\n\n【表达风格】\n{reply_style}" if reply_style else ""

        prompt_header = persona_line + style_block if persona_line else style_block.lstrip()
        prompt = f"""{prompt_header}

今天是{date_str} {weekday}{"（周末）" if is_weekend else ""}

【最近几天日程参考】
{yesterday_text}
""" if prompt_header else f"""今天是{date_str} {weekday}{"（周末）" if is_weekend else ""}

【最近几天日程参考】
{yesterday_text}
"""

        # 跨群历史背景（动态上下文）
        if history_context:
            prompt += f"""
【最近聊天背景】
以下是 bot 近期在聊天中观察到的事，可作为日程灵感（不要直接复述）：
{history_context}

💡 若上面提到了**特殊事件**（节日、约会、考试、截止、心情转变等），优先把它反映到今日日程中。
"""

        # 知识库参考（动态上下文）
        if knowledge_context:
            prompt += f"""
【相关记忆参考】
{knowledge_context}
"""

        # 今日需要纳入的约定
        if pending_commitments:
            commit_lines: List[str] = ["", "【今天需要纳入的约定】"]
            for item in pending_commitments:
                t = (item or {}).get("time", "")
                title = (item or {}).get("title", "")
                notes = (item or {}).get("notes", "")
                line = f"- {t} {title}".strip()
                if notes:
                    line += f"（{notes}）"
                commit_lines.append(line)
            commit_lines.append("要求：把上述约定安排到合适时间段（与已知作息冲突时优先约定），goal_type 用 social_maintenance 或对应类型。")
            prompt += "\n".join(commit_lines) + "\n"

        # 添加自定义prompt（如果配置了）—— v4.5.0（issue #11）：语义从"一次性
        # 要求"改为"当前生活阶段/长期状态"，与推断链路（auto_scheduler）的口径一致
        if custom_prompt:
            prompt += f"""
【当前生活阶段与今日重点】
{custom_prompt}
"""

        # M5（§6.2 F）：睡醒情绪描述 + 引导约束（由 schedule_generator 按
        # schedule.emotion_influence 拼好传入；空串=关闭/无情绪，不出现该块）
        if emotion_context:
            prompt += f"""
{emotion_context}
"""

        # M5（§6.2 F）：连续低迷软约束——prompt 注明，并要求安排一次恢复类活动
        days = _to_int(low_mood_days, 0)
        if days >= LOW_MOOD_MIN_DAYS:
            prompt += f"""
【最近状态】最近有点低落（已连续 {days} 天情绪偏低）。
请务必在今天安排一次恢复类活动（goal_type 用 rest 或 social_maintenance，如散步、放空、和朋友聊聊天），
学习与工作强度相应调低，不要连续堆叠高压条目。
"""

        # 根据配置决定描述要求
        if enable_detailed_description:
            desc_requirement = f"2. 每个description {min_desc_len}-{max_desc_len}字，用自然叙述风格（像日记）"
        else:
            desc_requirement = "2. description字段填空字符串\"\"即可（不需要描述）"

        # ── v4.6.0：作息锚点硬约束（起床 / 入睡）────────────────
        if cross_midnight:
            awake_rule = (
                f"- 清醒时段从 {wake_text} 睡醒开始，跨过午夜延续到次日 {sleep_text} 入睡为止，"
                f"所有清醒活动必须安排在这个区间内"
            )
            sleep_rule = (
                f"- {sleep_text} 到 {wake_text} 是睡眠时段"
            )
        else:
            awake_rule = (
                f"- 清醒时段为 {wake_text} - {sleep_text}，所有清醒活动必须安排在这个区间内"
            )
            sleep_rule = (
                f"- {sleep_text} 到次日 {wake_text} 是睡眠时段"
            )
        if no_sleep_mode:
            routine_rule = (
                f"{sleep_rule}：按【无睡眠模式】**不安排任何日程**（该时段整体留空，不生成条目）"
            )
        else:
            routine_rule = (
                f"{sleep_rule}：安排一个从 {sleep_text} 开始、到次日 {wake_text} 结束的"
                f"'睡觉'活动（goal_type: daily_routine；跨午夜用大于 24h 的累计时长表达，不要硬切成两条）"
            )
        if no_sleep_mode:
            seam_rule = (
                f"- 最后一个活动的结束时间必须恰好是 {sleep_text}，"
                f"第一个活动从次日 {wake_text} 开始；两者之间的睡眠时段不生成条目"
            )
        else:
            seam_rule = (
                f"- 睡眠时段首尾必须与相邻活动无缝衔接"
                f"（入睡时刻 = 前一个活动的结束时间，起床时刻 = 后一个活动的开始时间）"
            )
        time_range_requirement = f"""
🔴 【作息时间】本角色 {sleep_text} 上床入睡，{wake_text} 睡醒起床（作息锚点，不可移动）！
   {awake_rule}
   {routine_rule}
   {seam_rule}
"""

        # ── v4.7.0：无睡眠模式块（睡眠时段不生成日程）────────────
        no_sleep_requirement = ""
        if no_sleep_mode:
            no_sleep_requirement = f"""
🔴 【无睡眠模式】本角色**不需要睡觉**！
   - 睡眠时段（{sleep_text} 到次日 {wake_text}）**不生成任何日程条目**——该时段整体留空
   - 日程只覆盖清醒时段（{wake_text} 到 {sleep_text}），时段内部保持无缝衔接、无空档
   - 全天任何活动的 name 都不得包含"睡 / 眠 / 憩"字样（睡觉、睡眠、午休、午睡、小憩、打盹、赖床等一律禁止）
"""

        # ── v4.6.0：动态 JSON 示例 + 无缝演算 ────────────────────
        example_text = self._build_example_text(
            wake_min, sleep_min, no_sleep_mode, enable_detailed_description,
            min_activities, max_activities,
        )

        # ── 时间合理性框架（随作息/模式自适应）──────────────────
        if no_sleep_mode:
            night_row = f"   • {sleep_text} - 次日{wake_text}  （不生成日程条目，睡眠时段整体留空）"
        else:
            night_row = f"   • {sleep_text} - 次日{wake_text}  睡觉（睡眠时段固定不动）"
        routine_rows = f"""   • {wake_text} 起       起床/洗漱
   • 早餐 06:00-09:00 ← 必须在这个窗口
   • 上午时段    学习/工作/娱乐
   • 午餐 11:00-14:00 ← 必须在这个窗口
   • 下午时段    可细分为2-3个不同活动，避免单个活动超过3小时
   • 晚餐 17:00-20:00 ← 必须在这个窗口
   • 晚间时段    娱乐/社交/兴趣
{night_row}"""

        # ── 覆盖口径（随模式自适应）：正常模式要求全天闭环，无睡眠模式只要求清醒时段 ──
        if no_sleep_mode:
            coverage_core = "🔴 核心要求：清醒时段内日程必须无缝衔接，不允许任何时间空档！（睡眠时段整体留空，不计入）"
            coverage_rule = f"1. {min_activities}-{max_activities}个活动，完整覆盖清醒时段（无缝衔接；睡眠时段不生成条目）"
            coverage_seam = "1. 无缝覆盖清醒时段：每个活动结束时间 = 下个活动开始时间（睡眠时段留空，不算空档）"
            coverage_final = "🔴 核心要求：必须无缝覆盖清醒时段（睡眠时段不生成条目，不算空档）！"
        else:
            coverage_core = "🔴 核心要求：日程必须全天无缝衔接，不允许任何时间空档！"
            coverage_rule = f"1. {min_activities}-{max_activities}个活动，完整覆盖全天（绕时钟闭环，无缝衔接）"
            coverage_seam = "1. 无缝覆盖全天：每个活动结束时间 = 下个活动开始时间（不允许任何空档）"
            coverage_final = "🔴 核心要求：必须无缝覆盖全天，不能有任何时间空档！"

        prompt += f"""
【任务】生成今天的详细日程JSON：
{coverage_core}
   - 每个活动的结束时间 = 下一个活动的开始时间
   - 计算公式：结束时间 = time_slot + duration_hours
{time_range_requirement}{no_sleep_requirement}【原则】（重要！）
- 作息框架（睡眠时段 / 三餐 / 起床 / 入睡）每天稳定，是基础保留项 —— 不动这个框架
- 真实的人 ≠ 日程机器：同一作息框架下，每天的"做什么"与"心情"应有微变化
- 例：早餐时段不变，但今天可能粥配油条，明天面包黄油；上午时段不变，但今天审稿子，明天写专栏
- ⚠️ 不要为了"特色"突破常识作息（凌晨跑清醒活动、跳过晚餐、午餐推到 16 点都不可以）
- ⚠️ 不要为了"和昨天不同"而把作息打乱（睡眠时段、三餐时段必须正常）

{coverage_rule}
{desc_requirement}
3. 严格遵守开头的角色人设：身份、习惯、所处世界观要贯穿全天（不要泛化成"普通女大学生"）
4. 兴趣偏好：{interest if interest else "日常生活"}
5. description 字段的语气贴合开头的表达风格（reply_style）；活动 name 保持简短中性
6. **今日特色（在作息框架内做微变化）**：至少 2 个活动名/描述体现今天独有的细节
   - ✅ 把"上午活动"具化成今天具体在做什么（例：审稿、回邮件、写专栏、整理藏书）
   - ✅ 与【最近聊天背景】呼应（例：朋友提到的事 → 反映到 description 或 name 中）
   - ✅ description 写出今天的小心情 / 小插曲（不夸张，像日记一笔带过）
   - ❌ 不要每天叫"上午学习""下午学习""夜聊"这种通用名
   - ❌ 不要为求新意突破作息（清醒时段越过入睡时刻、跳过用餐都不允许）
   - ❌ "今日特色"≠ 改变作息时段，是同一时段填不同的具体内容{(
    f'''
7. **避免与最近几天重复**：今天的活动 name 至少 30% 不要与【最近几天日程参考】里的相同
   - 防止"周一审稿 / 周二写专栏 / 周三又审稿"这种交替式循环
   - 看到最近几天反复出现的活动，今天换个具体内容或换措辞
   - ⚠️ 不是换作息时段：早餐还是早餐时段，但内容可以不同'''
    if has_real_yesterday else ""
)}
"""

        # 如果有自定义prompt，强调一下
        if custom_prompt:
            prompt += f"6. ⚠️ 优先延续上述【当前生活阶段与今日重点】的内容\n"

        prompt += f"""
【活动类型】
daily_routine(作息)|meal(吃饭)|study(学习)|entertainment(娱乐)|social_maintenance(社交)|exercise(运动)|learn_topic(兴趣)|rest(休息/放空)|free_time(自由)|custom(其他)

⚠️ **重要：meal类型活动命名规范**
- 活动名称必须直接使用：早餐、午餐、晚餐
- 禁止使用：准备xx、零食时间、下午茶等变体
- 时间要求：早餐06:00-09:00，午餐11:00-14:00，晚餐17:00-20:00
"""

        prompt += example_text + "\n"

        prompt += f"""
【时间合理性要求 - 重要！】
⚠️ 必须同时满足以下两点：
{coverage_seam}
2. 遵守常识性时间安排，参考以下框架（已按本角色作息锚点适配）：
{routine_rows}

⚠️ 注意：下午和晚间的大时段应该细分成多个活动，不要一个活动占据5小时以上！

【要求】
- 严格JSON格式，无注释
- time_slot按时间递增（HH:MM格式）
- {coverage_final}
  * 每个活动结束时间 = 下个活动开始时间
  * 计算方式：结束时间 = time_slot + duration_hours
- ⚠️ 关键活动时间必须合理：早餐6-9点、午餐11-14点、晚餐17-20点
""" + ("- description填空字符串\"\"即可\n" if not enable_detailed_description else f"- description简洁，{min_desc_len}-{max_desc_len}字\n") + f"""- 体现{weekday}特色（{"周末睡懒觉" if is_weekend else "工作日早起"}）
"""

        # 添加Schema约束（精简版）
        if schema:
            import json
            schema_desc = f"""
【Schema要求】
- {min_activities}-{max_activities}个活动（必须）
- 必填：name(2-20字), time_slot, goal_type, priority
""" + ("- description填空字符串\"\"即可\n" if not enable_detailed_description else f"- description: {min_desc_len}-{max_desc_len}字\n") + f"""- priority: high/medium/low
- duration_hours: 0.25-12（活动持续时长，小时）

Schema: {json.dumps(schema.get('properties', {}).get('schedule_items', {}), ensure_ascii=False)}
"""
            prompt += schema_desc

        return prompt

    def build_retry_prompt(
        self,
        schedule_type: str,
        preferences: Dict[str, Any],
        schema: Dict,
        previous_issues: List[str],
        yesterday_context: Optional[str] = None,
        pending_commitments: Optional[List[Dict[str, Any]]] = None,
        history_context: str = "",
        knowledge_context: str = "",
        emotion_context: Optional[str] = None,
        low_mood_days: int = 0,
    ) -> str:
        """构建第二轮prompt（附带反馈）

        Args:
            schedule_type: 日程类型
            preferences: 用户偏好
            schema: JSON Schema
            previous_issues: 上一轮的问题列表
            yesterday_context: 最近几天日程上下文（可选）
            pending_commitments: 今日约定（可选）
            history_context: 最近聊天背景（可选）
            knowledge_context: 相关记忆参考（可选）
            emotion_context: 睡醒情绪描述 + 引导约束（可选；M5 §6.2 F，空串=不注入）
            low_mood_days: 连续低迷天数（M5 §6.2 F；≥3 时注明「最近有点低落」）

        Returns:
            改进后的提示词
        """
        base_prompt = self.build_schedule_prompt(
            schedule_type, preferences, schema, yesterday_context,
            pending_commitments=pending_commitments,
            history_context=history_context,
            knowledge_context=knowledge_context,
            emotion_context=emotion_context,
            low_mood_days=low_mood_days,
        )

        feedback = "\n\n⚠️ **上一次生成存在以下问题，请改进：**\n\n"
        for idx, issue in enumerate(previous_issues[:5], 1):  # 只列出前5个
            feedback += f"{idx}. {issue}\n"

        feedback += "\n**请重新生成一个更合理的日程，特别注意以上问题！**\n"

        return base_prompt + feedback

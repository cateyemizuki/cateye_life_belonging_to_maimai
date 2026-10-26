"""Prompt Builder Module.

This module provides prompt building functionality for schedule generation.
Separated from BaseScheduleGenerator to follow Single Responsibility Principle.

PR8 3.0.0 清退（依据 = 用户 2026-10-27 授权直接清理 + 逐符号审定清单 WP4）：
旧 daily 提示词面（``build_schedule_prompt``/``build_retry_prompt``/
``_build_example_items``/``_build_example_text``/``build_emotion_context``）已整链
删除；本模块仅保留 spine 日意图提示词链（``build_spine_prompt`` 及其依赖）。
"""

from typing import Any, Dict, List, Optional, Tuple
import logging

from suite_prompts import (
    SCHEDULE_GENERATION_EXTRA_DEFAULT,
    SCHEDULE_LOW_MOOD_NOTE_DEFAULT,
    SCHEDULE_SPINE_COMMITMENT_NOTE_DEFAULT,
    SCHEDULE_SPINE_DESCRIBED_NOTE_DEFAULT,
    SCHEDULE_SPINE_NO_DESCRIBED_NOTE_DEFAULT,
    SCHEDULE_SPINE_NO_SLEEP_LINE_DEFAULT,
    SCHEDULE_SPINE_SLEEP_LINE_DEFAULT,
    SCHEDULE_SPINE_TASK_DEFAULT,
    prompt as prompt_of,
    render,
)

from ..time_utils import TimezoneManager

logger = logging.getLogger(__name__)

# bot_profile 缺失时显式报错暴露配置问题，不再用任何 fallback 默认值
# （主程序正常情况下 bot.nickname / personality.personality 一定能拉到；
#  拿不到说明 IPC 异常或主程序配置缺失，需要在日志里红色报警让用户立刻修）

# 作息解析默认值（配置留空/非法时兜底）：07:00 起床，23:00 入睡
_DEFAULT_WAKE_MINUTES = 7 * 60
_DEFAULT_SLEEP_MINUTES = 23 * 60


#: M5 连续低迷软约束的触发门槛（与 emotion/state_machine.LOW_MOOD_MIN_DAYS 同值；prompt 注明用）
LOW_MOOD_MIN_DAYS = 3


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


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

    该类负责（PR8 3.0.0 起 spine 唯一路线，旧 daily/retry 提示词已清退）：
        1. 构建 spine 日意图提示词
        2. 整合配置、上下文、Schema 约束

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

    def _plugin_ref(self) -> Any:
        """取 plugin 引用（生成配置字典里的 ``plugin`` 键；None = 提示词全走默认）。

        2026-10-10：日程生成提示词可经 ``[prompts]`` 段定制（追加指令 / 情绪块 /
        低迷软约束 / 重试反馈），本方法提供读配置所需的 plugin 引用。
        """
        return self.config.get("plugin") if isinstance(self.config, dict) else None

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
                "错误：bot_profile.personality 未配置或为空 —— 生成的日程将无法贴合角色人设。"
                "请在主程序 bot 配置中填写 personality.personality 字段。"
            )
        if not bot_name:
            logger.error(
                "错误：bot_profile.bot_name 未配置或为空 —— prompt 中将不出现角色名。"
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
    # spine 日意图提示词（2026-10-25 滚动化改造新增；R7 依据 = 用户 2026-10-25 直派，
    # 设计目录 D:/deepseek-harness_workspace/maibot-plugins/2026-10-25-日程系统滚动化改造设计）
    # ------------------------------------------------------------

    def build_spine_prompt(
        self,
        schema: Optional[Dict] = None,
        *,
        min_intentions: int = 3,
        max_intentions: int = 6,
        max_described: int = 1,
        no_sleep_mode: bool = False,
        yesterday_context: Optional[str] = None,
        pending_commitments: Optional[List[Dict[str, Any]]] = None,
        history_context: str = "",
        knowledge_context: str = "",
        low_mood_days: int = 0,
        holiday_context: Optional[str] = None,
    ) -> str:
        """构建 spine 日意图提示词（设计 §4.2 精简口径）。

        与旧 daily 提示词（PR8 3.0.0 已清退）的差异：

        - 无 JSON 示例与无缝衔接演算（spine 不要求覆盖全天，空档合法）；
        - **不注入情绪块**（``schedule_emotion_context`` 措辞含「无缝衔接要求」，
          与稀疏口径冲突——保守裁量，登记 notes）；低迷软约束 / 节假日 / 约定 /
          生活阶段 / 追加指令照常注入；
        - Schema 为精简 8 字段意图卡（见 ``SchemaBuilder.build_spine_schema``）；
          只做结构校验、不强制重试（多轮与否由调用方决定）。
        """
        personality, reply_style, interest, bot_name = self._get_cached_config()
        custom_prompt = self.config.get('custom_prompt', '').strip()
        wake_min, sleep_min = self._resolve_schedule_bounds()
        wake_text = _fmt_minutes(wake_min)
        sleep_text = _fmt_minutes(sleep_min)

        today = self.tz_manager.get_now()
        date_str = today.strftime("%Y-%m-%d")
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        weekday = weekday_names[today.weekday()]
        is_weekend = today.weekday() >= 5

        if bot_name and personality:
            persona_line = f"你是{bot_name}，{personality}"
        elif bot_name:
            persona_line = f"你是{bot_name}"
        elif personality:
            persona_line = personality
        else:
            persona_line = ""
        style_block = f"\n\n【表达风格】\n{reply_style}" if reply_style else ""
        prompt_header = persona_line + style_block if persona_line else style_block.lstrip()
        prompt = f"""{prompt_header}

今天是{date_str} {weekday}{"（周末）" if is_weekend else ""}
""" if prompt_header else f"""今天是{date_str} {weekday}{"（周末）" if is_weekend else ""}
"""

        yesterday_text = yesterday_context or "最近几天没有具体记录"
        prompt += f"""
【最近几天日程参考】
{yesterday_text}
"""

        if history_context:
            prompt += f"""
【最近聊天背景】
以下是 bot 近期在聊天中观察到的事，可作为今日意图灵感（不要直接复述）：
{history_context}
"""

        if knowledge_context:
            prompt += f"""
【相关记忆参考】
{knowledge_context}
"""

        if pending_commitments:
            commit_lines: List[str] = ["", "【今天需要纳入的约定】"]
            for item in pending_commitments:
                t = (item or {}).get("time", "")
                title = (item or {}).get("title", "")
                notes = (item or {}).get("notes", "")
                hardness = str((item or {}).get("hardness") or "hard").strip().lower()
                line = f"- {t} {title}（hardness={hardness}".strip()
                if notes:
                    line += f"｜{notes}"
                source = str((item or {}).get("source") or "").strip()
                if source:
                    line += f"｜来源={source}"
                item_id = str((item or {}).get("commitment_id") or "").strip()
                if item_id:
                    line += f"｜ID={item_id}"
                line += "）"
                commit_lines.append(line)
            prompt += "\n".join(commit_lines) + "\n"
            prompt += render(
                prompt_of(self._plugin_ref(), "schedule_spine_commitment_note"),
                fallback=SCHEDULE_SPINE_COMMITMENT_NOTE_DEFAULT,
            ) + "\n"

        if custom_prompt:
            prompt += f"""
【当前生活阶段与今日重点】
{custom_prompt}
"""

        # M5 低迷软约束照常注入（措辞「安排一次恢复类活动」与稀疏口径兼容）
        days = _to_int(low_mood_days, 0)
        if days >= LOW_MOOD_MIN_DAYS:
            note = render(
                prompt_of(self._plugin_ref(), "schedule_low_mood_note"),
                fallback=SCHEDULE_LOW_MOOD_NOTE_DEFAULT,
                days=days,
            )
            prompt += "\n" + note + "\n"

        if holiday_context:
            prompt += "\n" + str(holiday_context).strip() + "\n"

        if _to_int(max_described, 0) >= 1:
            described_note = render(
                prompt_of(self._plugin_ref(), "schedule_spine_described_note"),
                fallback=SCHEDULE_SPINE_DESCRIBED_NOTE_DEFAULT,
                max_described=_to_int(max_described, 1),
            )
        else:
            described_note = render(
                prompt_of(self._plugin_ref(), "schedule_spine_no_described_note"),
                fallback=SCHEDULE_SPINE_NO_DESCRIBED_NOTE_DEFAULT,
            )
        if no_sleep_mode:
            no_sleep_line = render(
                prompt_of(self._plugin_ref(), "schedule_spine_no_sleep_line"),
                fallback=SCHEDULE_SPINE_NO_SLEEP_LINE_DEFAULT,
                sleep_text=sleep_text, wake_text=wake_text,
            )
        else:
            no_sleep_line = render(
                prompt_of(self._plugin_ref(), "schedule_spine_sleep_line"),
                fallback=SCHEDULE_SPINE_SLEEP_LINE_DEFAULT,
                sleep_text=sleep_text, wake_text=wake_text,
            )

        prompt += "\n" + render(
            prompt_of(self._plugin_ref(), "schedule_spine_task"),
            fallback=SCHEDULE_SPINE_TASK_DEFAULT,
            min_intentions=_to_int(min_intentions, 3),
            max_intentions=_to_int(max_intentions, 6),
            wake_text=wake_text,
            sleep_text=sleep_text,
            no_sleep_line=no_sleep_line,
            described_note=described_note,
        ) + "\n"
        prompt += f"\n兴趣偏好：{interest if interest else '日常生活'}\n"

        # [prompts].schedule_generation_extra：用户的自由追加指令（留空不追加；与 daily 同键）
        extra = prompt_of(self._plugin_ref(), "schedule_generation_extra")
        if extra.strip() and extra.strip() != SCHEDULE_GENERATION_EXTRA_DEFAULT.strip():
            prompt += "\n" + extra.strip() + "\n"

        if schema:
            import json
            min_i = _to_int(min_intentions, 3)
            max_i = _to_int(max_intentions, 6)
            note_rule = (
                f"- note：可选，最多给 {_to_int(max_described, 1)} 条重要意图写一句 15-25 字"
                if _to_int(max_described, 0) >= 1
                else "- 不要输出 note 字段"
            )
            prompt += f"""
【Schema要求】
- {min_i}-{max_i} 条意图（必须）
- 必填：name(2-20字), goal_type, window, min_minutes, max_minutes, priority, hardness, interruptibility
{note_rule}
- window：["HH:MM", "HH:MM"] 两元素数组
- min_minutes / max_minutes：5-720 整数，max >= min
- interruptibility：0-1 小数

Schema: {json.dumps(schema, ensure_ascii=False)}
"""
        return prompt

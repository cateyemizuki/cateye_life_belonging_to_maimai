"""提示词集中模块（提示词唯一真源 + [prompts] 高级配置段）。

本模块承担两件事（2026-10-10 配置整理轮）：

1. **唯一真源**：插件内所有会进入 LLM 上下文 / LLM 调用的提示词、注入模板、
   主动行为 intent/reason 文案，默认值全部定义在这里（模块级 ``*_DEFAULT`` 常量），
   不再散落在各业务模块。业务模块一律经 :func:`prompt` 读取"配置值 or 默认值"。
2. **配置暴露**：``PromptsSectionConfig`` 把上述每条提示词暴露为 ``[prompts]`` 段
   的一个字段（WebUI「提示词（高级）」，默认值 = 模块常量）。用户不改 = 行为与
   整理前逐字一致；要深度定制时可在 WebUI 或 config.toml 直接改文案。

占位符约定（模板里的 ``{xxx}``）：各字段的 description 里写明了可用占位符；
渲染失败（用户把占位符写坏）时调用方一律回退默认模板并记一条 warning——
绝不因为提示词写错而中断业务链路（§八.2 可空降级同一口径）。

命名约定：``<模块>_<用途>``，与 ``[prompts]`` 字段名一一对应，
方便在 config.toml 里按前缀分组阅读。
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar, Dict, Set

from maibot_sdk import Field, PluginConfigBase

logger = logging.getLogger(__name__)

#: 已告警过"回退默认"的提示词键（进程内去重：回退告警每键至多一条，防刷日志）
_FALLBACK_WARNED: Set[str] = set()


def _warn_fallback_once(name: str, reason: str) -> None:
    """提示词回退默认时记一条 warning（每键进程内至多一次；6.5-G：docstring 承诺落地）。"""
    if name in _FALLBACK_WARNED:
        return
    _FALLBACK_WARNED.add(name)
    logger.warning("提示词 %s 回退出厂默认模板（%s）；请检查 [prompts] 配置", name, reason)


def _ui_i18n(en_label: str, en_hint: str = "") -> dict:
    """字段级英文翻译（并入 json_schema_extra；与其它 config 模块同一写法）。"""
    entry: Dict[str, str] = {"label": en_label}
    if en_hint:
        entry["hint"] = en_hint
    return {"i18n": {"en": entry}}


# ===========================================================================
# 一、陪伴模块提示词
# ===========================================================================

#: 想念触发时传给 planner 的 reason（占位符：{lover_name} / {relation} / {hours}）
COMPANION_MISS_REASON_DEFAULT = (
    "【触发原因·想念】你已经 {hours} 个小时没收到{relation}的消息了。"
    "可以自然地问问TA的近况或表达一下想念；若觉得此刻开口不自然，平淡打个招呼也可以。"
)

#: 想念触发前 LLM 把关提示词（占位符：{lover_name} / {relation} / {personality} /
#: {current_time} / {hours} / {activity_context}；回复 Y 才放行）
COMPANION_MISS_CONFIRM_DEFAULT = (
    "你是“{lover_name}”，而对方是你的{relation}（人设：{personality}）。"
    "现在是 {current_time}，你上次收到TA的消息已经是 {hours} 小时前。{activity_context}\n"
    "请站在“{lover_name}”的角度判断：此刻主动发一条关心TA近况的消息"
    "是否自然、是否体贴？\n"
    "- 深夜TA可能睡了、TA可能在忙、或你觉得突兀 → 回复 N\n"
    "- 你确实想TA了、此刻开口很自然 → 回复 Y\n"
    "只回复一个大写字母：Y 或 N。"
)

#: 日程巡检（活动分享）触发 reason（占位符：{lover_name} / {activity}）
COMPANION_ACTIVITY_REASON_DEFAULT = "【触发原因·日程巡检】{lover_name}现在在{activity}，可以分享一句。"

#: 日常搭话触发 reason（占位符：{lover_name}）
COMPANION_DAILY_REASON_DEFAULT = "【触发原因·日常巡检】没什么特别的事，可以自然闲聊一句。"

#: 陪伴对象电脑屏幕截图的视觉理解提示词（随截图发给 vlm 任务）
SCREEN_DESCRIBE_DEFAULT = (
    "这是你的陪伴对象电脑屏幕的截图。请用一句话（不超过 40 字、口语化）描述"
    "TA现在可能在干什么（比如在打游戏 / 写代码 / 看视频 / 挂机离开）。\n"
    "如果截图上看不出具体内容，就回答：屏幕上看不出具体内容。"
)

#: 屏幕旁白文案模板（占位符：{date} 年月日 / {time} 时分 / {user_name} / {description}）
SCREEN_NARRATION_DEFAULT = (
    "现在是{date} {time}，你看了眼{user_name}的电脑屏幕，TA正在：{description}"
)
SCREEN_NARRATION_OFFLINE_DEFAULT = (
    "现在是{date} {time}，你想看一眼{user_name}的电脑屏幕，但TA的电脑没开"
)
SCREEN_NARRATION_FAILED_DEFAULT = (
    "现在是{date} {time}，你看了眼{user_name}的电脑屏幕，但没看清TA在干什么"
)

#: 好感度注入块（占位符：{name} / {relation_word} / {target_label} / {tier} /
#: {tier_label} / {tier_desc}）；整块文本含前导换行
INJECT_AFFECTION_BLOCK_DEFAULT = (
    "\n【{name}与{relation_word}（{target_label}）的好感度】档位 {tier}·{tier_label}（{tier_desc}）"
)

#: 主动发言回合给 planner 的回复目标约束（软约束；无占位符）
INJECT_PROACTIVE_TURN_RULE_DEFAULT = (
    "\n【本轮是主动发言】现在是你在主动找对方说话（不是对方来找你）："
    "\n- 不要回复你自己发送的消息，也不要在发言里引用你自己的消息；"
    "\n- 优先回应对方最后一条发言；如果最后一条发言是你自己的（对方还没回你），"
    "就当作对方还没回，直接说新内容或起一个新话题；"
    "\n- reply 的 msg_id 只能选对方发送的消息。"
)

#: 工具主动发消息的生成提示词（占位符：{style_word} / {relation_word}）
TOOL_SEND_MESSAGE_DEFAULT = (
    "请用{style_word}的口吻，对你的{relation_word}说一句问候或分享一件小事（20-40字）。"
)
#: 工具主动发消息的 system prompt（占位符：{name} / {self_desc} / {relation_word}）
TOOL_SEND_MESSAGE_SYSTEM_DEFAULT = "你是「{name}」（{self_desc}），此刻正主动找你的{relation_word}说话。"

#: 会话采样好感裁判提示词（占位符：{lover_name} / {target_label} / {persona} /
#: {tier_label} / {tier_desc} / {mood_text} / {recent_messages}）
AFFECTION_CONVERSATION_REVIEW_DEFAULT = """你是 AI 角色「{lover_name}」与{target_label}之间关系的会话观察裁判。
你不参与对话，只看下面给出的材料，给这段相处评估一个**有界的好感度增量**。
你看不到任何好感度数值，也看不到分数与基线，只看到关系所处的阶段描述——不要猜测具体数值。

【角色人设（判断真心程度的依据）】
{persona}

【双方关系所处的阶段】
{tier_label}：{tier_desc}

【角色近期的情绪底色（低分辨率描述，仅供理解语气）】
{mood_text}

【最近对方发来的消息（按时间从旧到新，只有对方一侧）】
{recent_messages}

【判定标准】
1. event：用不超过 30 字概括这段相处里最值得记住的一件事；没有就写「日常闲聊」；
2. delta：好感度增量，范围 [-2, +2.5]——
   - 阿谀奉承、无意义刷屏、单方面索取：不给正分（0 或负）；
   - 真诚的关心、共同完成一件事、化解了一次矛盾：才给正分；
   - 明显的争吵、冒犯、被冷落：给负分；
   - 平淡的日常闲聊：接近 0；
3. tag 只能是 normal / conflict / apology / sweet 之一；
4. 只输出 JSON：{{"event": "...", "delta": 0.0, "tag": "normal"}}"""

#: 关系日结裁判提示词（占位符：{lover_name} / {target_label} / {date} / {persona} /
#: {tier_label} / {tier_desc} / {stats} / {event_log} / {impressions}）
AFFECTION_DAILY_REVIEW_DEFAULT = """你是 AI 角色「{lover_name}」与{target_label}之间关系的日结裁判。
你看不到任何好感度数值，也看不到分数与基线，只看到关系所处的阶段描述——不要猜测具体数值。
任务：根据 {date} 这一天的相处记录，给这一天对关系的影响做一个有界的总结。

【角色人设】
{persona}

【双方关系所处的阶段】
{tier_label}：{tier_desc}

【今日互动统计】
{stats}

【今日事件日志（时间、类型、摘要、当日入账增量）】
{event_log}

【最近积累的相处印象（旧→新）】
{impressions}

【判定标准】
1. delta_score：这一天对关系的影响，范围 [-2, +2]——
   - 真诚的关心、共同完成一件事、化解矛盾：正分；
   - 阿谀奉承、无意义刷屏：不给正分；
   - 争吵、冒犯、被冷落、整天没有互动：负分；
   - 平淡无事：接近 0；
2. impressions：0~3 条，每条一句话（不超过 40 字）记下今天值得记住的事；
   只写事实与感受，不写分数、不写评价性标签、不编造没有发生的事；
3. tone：一句话概括今天的关系温度（不超过 30 字）；
4. reason：必须直接引用上面事件日志或对方原话的片段作为依据，禁止脑补不存在的事；
5. 只输出 JSON：{{"delta_score": 0.0, "impressions": ["..."], "tone": "...", "reason": "..."}}"""


# ===========================================================================
# 二、日程模块提示词
# ===========================================================================

#: planner 日程注入块首行（占位符：无）
INJECT_SCHEDULE_HEADER_DEFAULT = "【可选上下文 - Bot 的当前日程】"
#: 当前活动行模板（占位符：{activity} / {description} / {remaining_hint}）
INJECT_SCHEDULE_CURRENT_LINE_DEFAULT = "现在：{activity}（{description}）{remaining_hint}"
#: 未来安排标题（占位符：无）
INJECT_SCHEDULE_FUTURE_TITLE_DEFAULT = "接下来的安排:"
#: 未来安排条目模板（占位符：{time} / {name}）
INJECT_SCHEDULE_FUTURE_LINE_DEFAULT = "  {time} - {name}"
#: 询问未来时的引导行（占位符：无）
INJECT_HINT_FUTURE_DEFAULT = "提示：用户询问未来计划，请自然地介绍后续安排。"
#: 询问当前时的引导行（占位符：无）
INJECT_HINT_CURRENT_DEFAULT = "提示：用户直接询问当前状态，请如实告知，并自然带出此刻的精神状态。"
#: 对话延续时的引导行（占位符：{reason}）
INJECT_HINT_CONTINUE_DEFAULT = "提示：对话延续中（{reason}），可自然带过当前活动。"
#: 主动碎碎念时的引导行（占位符：无）
INJECT_HINT_PROACTIVE_DEFAULT = "提示：可以在回答中自然带出此刻你正在做的事（一两句即可，不要刻意转移话题）。"
#: 闲聊时的引导行（占位符：无）
INJECT_HINT_IDLE_DEFAULT = "提示：仅供参考；不相关请完全忽略，不要刻意提及。"
#: 无当前活动时的兜底文案（占位符：无；2026-10-10 N1-2：
#: 日程空档期不静默早退，改注入这一句；留空 = 不注入无活动兜底）
INJECT_NO_ACTIVITY_DEFAULT = "现在没有安排中的活动（可能在休息）。"
#: replyer 状态注入模板（占位符：{state_line} / {energy} / {next_line}；{next_line} 为空时整行省略）
INJECT_REPLYER_STATUS_DEFAULT = (
    "【角色当前状态】\n{state_line}\n精神：{energy}。\n{next_line}"
    "注意：不要主动提及；仅在用户问到 / 强相关时自然带过。"
)
#: replyer 状态行模板（占位符：{activity} / {description} / {remaining_hint}）
INJECT_REPLYER_STATE_LINE_DEFAULT = "你现在正在 {activity}（{description}）{remaining_hint}。"
#: replyer 下一条活动行（占位符：{time} / {name}；留空 = 不输出该行）
INJECT_REPLYER_NEXT_LINE_DEFAULT = "接下来 {time} 要 {name}。\n"
#: 日期/节假日头部模板（占位符：{date} / {weekday} / {holiday}）——注在日程块头部
INJECT_DATE_HEADER_DEFAULT = "\n【当前日期】今天是 {date}（星期{weekday}），{holiday}。"

#: 日程生成·睡醒情绪块首行（占位符：{mood} / {constraints}）
SCHEDULE_EMOTION_CONTEXT_DEFAULT = (
    "【睡醒情绪底色】\n昨晚睡醒时的情绪：{mood}\n"
    "引导约束：{constraints}\n"
    "要求：情绪只影响活动类型与强度的微调，不得改变作息锚点、三餐窗口与无缝衔接要求。"
)
#: 日程生成·连续低迷软约束（占位符：{days}）
SCHEDULE_LOW_MOOD_NOTE_DEFAULT = (
    "【最近状态】最近有点低落（已连续 {days} 天情绪偏低）。\n"
    "请务必在今天安排一次恢复类活动（goal_type 用 rest 或 social_maintenance，如散步、放空、和朋友聊聊天），"
    "学习与工作强度相应调低，不要连续堆叠高压条目。"
)
#: 日程生成·节假日上下文（占位符：{date} / {weekday} / {holiday}）——
#: 仅当 [schedule] holiday_affects_schedule 开启时追加到生成提示词
SCHEDULE_HOLIDAY_CONTEXT_DEFAULT = (
    "【今天是特殊日期】{date}（星期{weekday}）：{holiday}。\n"
    "请让今天的日程体现该日期的性质（放假：晚起一些、外出/娱乐/社交多些；补班工作日：按普通工作日安排），"
    "但作息锚点与三餐窗口的硬要求不变。"
)

#: 日程生成·追加指令（留空 = 不追加；用户的自由定制位）
SCHEDULE_GENERATION_EXTRA_DEFAULT = ""
#: 多轮生成的重试反馈模板（占位符：{issues}）
SCHEDULE_RETRY_FEEDBACK_DEFAULT = (
    "\n\n注意：**上一次生成存在以下问题，请改进：**\n\n{issues}"
    "\n**请重新生成一个更合理的日程，特别注意以上问题！**\n"
)
#: 角色裁判提示词（占位符：{persona} / {today_str} / {weekday} / {current_text} / {description}）
SCHEDULE_ROLE_JUDGE_DEFAULT = (
    "【角色人设】\n{persona}\n\n"
    "【日期信息】\n今天是 {today_str}（{weekday}）\n\n"
    "【当前日程】\n{current_text}\n\n"
    "【用户请求】\n{description}\n\n"
    "请扮演该角色，判断是否会接受这个日程相关请求。\n"
    "不要机械接受用户安排；如果不符合人设、时间冲突太强、语气像玩笑或没有明确计划，可以拒绝或不改日程。\n"
    "decision 只能是以下值之一：\n"
    "- today: 角色接受或调整今天的安排，需要修改今天日程\n"
    "- future: 角色接受未来某天的预约，只记录预约，不修改今天日程\n"
    "- reject: 角色没有接受，日程不变\n\n"
    "以 JSON 格式返回：\n"
    # 注意：本模板经 str.format 渲染，JSON 示例的字面花括号必须写成 {{ }}（否则渲染失败回退原样）
    '{{"decision": "today/future/reject", "date": "YYYY-MM-DD 或空", '
    '"time": "HH:MM 或空", "title": "简短标题", "notes": "备注", '
    '"reason": "角色判断理由", "raw_date": "用户原文中的日期描述"}}\n'
    "日期必须根据【日期信息】中的真实日期推断。\n"
    "只输出 JSON，不要其他文字。"
)

#: 角色裁判无日程时的占位（占位符：无）
SCHEDULE_ROLE_JUDGE_NO_ACTIVITY_DEFAULT = "暂无可靠日程"
#: 角色裁判人设缺失占位（占位符：无）
SCHEDULE_ROLE_JUDGE_NO_PERSONA_DEFAULT = "未提供"

#: 活动切换主动开口 intent（按 goal_type；占位符：{activity_name}）
PROACTIVE_INTENT_DEFAULT = "Bot 切换到新活动了，可以自然地在群里说一两句近况（活动名：{activity_name}）"
PROACTIVE_INTENT_STUDY_DEFAULT = "Bot 刚开始学习/写代码，可以自然地在群里说一两句开始投入工作的话（活动名：{activity_name}）"
PROACTIVE_INTENT_WORK_DEFAULT = "Bot 刚开始工作，可以自然地说一两句进入状态的话（活动名：{activity_name}）"
PROACTIVE_INTENT_MEAL_DEFAULT = "Bot 刚开始吃饭，可以自然地在群里说一两句关于这顿饭的话（活动名：{activity_name}）"
PROACTIVE_INTENT_REST_DEFAULT = "Bot 刚开始休息，可以自然地说一两句放松/闲下来的话（活动名：{activity_name}）"
PROACTIVE_INTENT_EXERCISE_DEFAULT = "Bot 刚开始运动，可以自然地在群里吼一两句锻炼的话（活动名：{activity_name}）"
PROACTIVE_INTENT_ENTERTAINMENT_DEFAULT = "Bot 刚开始娱乐/看剧/打游戏，可以自然地说一两句兴奋/期待的话（活动名：{activity_name}）"
PROACTIVE_INTENT_SOCIAL_DEFAULT = "Bot 现在心情比较social，可以主动找群友聊聊天（活动名：{activity_name}）"
PROACTIVE_INTENT_DAILY_ROUTINE_DEFAULT = "Bot 刚切换到日常活动，可以自然地说一句近况（活动名：{activity_name}）"
PROACTIVE_INTENT_LEARN_TOPIC_DEFAULT = "Bot 开始钻研感兴趣的话题，可以自然地分享一点新发现（活动名：{activity_name}）"

#: 早安 intent（占位符：{activity_name}）
PROACTIVE_INTENT_MORNING_DEFAULT = (
    "Bot 刚睡醒，新的一天开始了，请自然地向大家道一声早安；"
    "可以顺口提一句今天的第一个安排（{activity_name}），语气符合人设，简短随意"
)
#: 无今日日程时的早安 intent（占位符：无）
PROACTIVE_INTENT_MORNING_NO_ACTIVITY_DEFAULT = (
    "Bot 刚睡醒，新的一天开始了，请自然地道一声早安；语气符合人设，简短随意"
)
#: 晚安 intent（占位符：无）
PROACTIVE_INTENT_NIGHT_DEFAULT = (
    "Bot 准备休息了，今天快结束了，请自然地道一声晚安；"
    "语气符合人设，简短随意、不要追问对方、不要开新话题"
)
#: 约定到期 intent（占位符：{time_hint} / {title} / {notes_hint} / {activity_name}）
PROACTIVE_INTENT_COMMITMENT_DEFAULT = (
    "你之前答应过对方今天{time_hint}要在私聊里{title}（约定备注：{notes_hint}）。\n"
    "现在到点了，请用符合人设的语气**立即在私聊里发出**这条消息（例如叫醒、问候、"
    "提醒等），不要询问对方、不要等用户先开口、不要陷入工具调用循环。\n"
    "活动名：{activity_name}"
)

#: 主动行为 reason 文案（进主动任务块，标注为什么触发；占位符见各条）
PROACTIVE_REASON_ACTIVITY_DEFAULT = "planning_v4: 活动切换 → {activity_name}"
PROACTIVE_REASON_COMMITMENT_DEFAULT = "planning_v4: 约定到期 → {title}"
PROACTIVE_REASON_MORNING_DEFAULT = "planning_v4: 早安问候（L2 漂移）"
PROACTIVE_REASON_NIGHT_DEFAULT = "planning_v4: 晚安问候（L2 漂移）"


# ===========================================================================
# 三、情绪模块提示词
# ===========================================================================

#: 睡眠期情绪大修裁判提示词（占位符：见 emotion/referee.py 组装点）
EMOTION_SLEEP_REFEREE_DEFAULT = """你是一个客观的情绪裁判系统，负责对 AI 角色的睡眠期情绪进行大修。
你不参与对话，只依据下面的客观数据输出 JSON。

【Bot 人设】
{persona}

【当前 VAD 绝对数值】
V: {v:+.2f}, A: {a:+.2f}, D: {d:+.2f}

【当天情绪日志（原始事件流）】
{emotion_log}

【日程信息】
前一天日程：
{yesterday_schedule}
第二天日程：
{tomorrow_schedule}
长期状态：{long_term_status}
预计睡眠时长：{sleep_hours:.1f} 小时

【情绪原型参考表】（V 效价 / A 唤醒 / D 支配）
{emotion_table}

【前一日日程情感残差 R_yesterday】（已按释怀/反刍加权求和，代码层数值项；正=释怀、负=反刍残留）
V: {r_v:+.2f}, A: {r_a:+.2f}, D: {r_d:+.2f}

【次日日程情感预期 E_tomorrow】（明日条目情感基调求和；考试/挑战 → V↓A↑D↓）
V: {e_v:+.2f}, A: {e_a:+.2f}, D: {e_d:+.2f}

【当日约定兑现情况】净兑现 {commitment_net:+.0f} 次（正=兑现多于违约，负=违约多于兑现）

【评分规则】
1. 越靠近睡前的情绪事件，权重略微增加（近因效应）；强度大的事件影响更大。
2. 睡眠时长影响恢复效果：<5 小时恢复极差（V 负向、A 升高）；6-9 小时恢复最佳（V 回升）；>10 小时睡懵（A 大幅降低）。
3. 日程预期：评估前一日日程是"释怀"还是"反刍"，第二日日程是"期待"还是"焦虑"（第二天有考试/挑战 → A 升高、D 降低）。
4. 必须结合人设性格做情绪过滤（如高神经质 → 焦虑更容易留存）。
5. R_yesterday / E_tomorrow / 约定情况是代码层已计算的参考数值，最终增量仍需你综合人设与日志判断。
6. 给出最终的 ΔVAD（增量，非绝对值），每一维范围必须在 [-{limit:.2f}, {limit:.2f}] 之间。
7. reason 中必须直接引用上面日程/情绪日志的原文片段作为依据，禁止脑补不存在的事件。

只输出 JSON：{{"delta_vad": {{"valence": 0.0, "arousal": 0.0, "dominance": 0.0}}, "reason_valence": "...", "reason_arousal": "...", "reason_dominance": "..."}}"""

#: 人设 → VAD 参数初始化提示词（占位符：{emotion_table}）
EMOTION_INIT_SYSTEM_DEFAULT = """你是一位情绪心理学专家，负责为 AI 角色建立 VAD（效价、唤醒度、支配感）三维情绪模型。
V 代表愉悦度，范围 -1.0 到 1.0，正值愉悦、负值痛苦；A 代表唤醒度，-1.0 到 1.0，正值兴奋紧张、负值平静困倦；D 代表支配感，-1.0 到 1.0，正值掌控自信、负值无力顺从。
你需要根据角色的 AI 人设，推导出该角色的情绪锚点、静态偏移值以及情绪增益与衰减系数。

【情绪原型参考表】（供锚点定位参考）
{emotion_table}

【数值约束（超范围视为无效）】
- 锚点 anchor：每一维 [-0.6, 0.6]（避免长期处于极端情绪）
- 静态偏移 offset：每一维 [-0.3, 0.3]
- 正向增益 positive_gain：[0.8, 1.3]（V>0 时乘，开心更开心/更收敛）
- 负向衰减 negative_decay：[0.6, 1.2]（V<0 时乘，难过更持久/更快消退）
- 正向静态偏移一般不超过 +0.3，保持负面情绪的比例才真实。

必须输出严格 JSON（每个参数都要给出引用人设原文的推导理由）：
{{"anchor": {{"valence": 0.0, "arousal": 0.0, "dominance": 0.0}},
  "offset": {{"valence": 0.0, "arousal": 0.0, "dominance": 0.0}},
  "positive_gain": 1.0, "negative_decay": 1.0,
  "reasons": {{"anchor": "...", "offset": "...", "multiplier": "..."}}}}"""

#: 回复/事件 ΔVAD 增量评估提示词（占位符：{lover_name} / {persona} / {user_context} / {reply_text}）
EMOTION_EVALUATE_DEFAULT = """你是 AI 角色「{lover_name}」的情绪增量评估器。你看不到角色当前的绝对情绪数值，只根据下面这段交流内容，评估"这轮交流/这件事"给角色情绪带来的变化量 ΔVAD。

【角色人设（情绪过滤依据）】
{persona}

【用户侧最后一条消息（对话上下文；可能为空）】
{user_context}

【AI 的回复内容 / 待评估的交流内容】
{reply_text}

【要求】
1. event 用不超过 30 字概括「这轮交流发生了什么」（谁说了什么、发生了什么、结果如何），不要笼统写"日常对话"；
2. ΔV/ΔA/ΔD 每一维范围 [-0.5, 0.5]；没有明显情绪波动时全部接近 0；
3. 只输出 JSON：{{"event": "...", "delta_vad": {{"valence": 0.0, "arousal": 0.0, "dominance": 0.0}}}}"""

#: 情绪底色注入文本（占位符：{mood}）——planner/replyer 双通道共用
EMOTION_INJECTION_DEFAULT = (
    "【当前情绪底色】{mood}。\n"
    "要求：语气、措辞、节奏与该情绪保持一致；情绪会随时间自然衰减、随事件变化；"
    "严禁在回复中直接说出情绪标签词汇或任何数值；不要主动提及或解释本提示本身。"
)
#: 深夜冻结态追加行（占位符：无；非冻结态不出现）
EMOTION_NIGHT_HINT_DEFAULT = (
    "（现在是深夜，你本该在睡觉。回复尽量短、慢、软一些；"
    "不要主动开启新话题；不要提及本提示。）"
)
#: 赖床提醒文本模板（占位符：{now_hhmm} / {planned_wake_hhmm} / {nap_tool_name} / {lie_in_max_until}）
NAP_REMINDER_DEFAULT = (
    "【临时提醒·不进下一轮上下文】你半夜被重要的人吵醒了，现在是 {now_hhmm}；"
    "你本来打算睡到 {planned_wake_hhmm}。如果还想再睡一会儿，可以调用 {nap_tool_name} "
    "把起床时间往后推（最晚 {lie_in_max_until}）。这条提醒只在本轮有效，"
    "不必向任何人提起\"昨晚被吵醒\"。是否调用由你决定。"
)


# ===========================================================================
# 四、QQ 空间提示词
# ===========================================================================

#: 说说文案提示词（占位符：{lover_name} / {personality} / {date} / {time} / {activity}）
QZONE_POST_DEFAULT = (
    "你是「{lover_name}」，人设：{personality}\n"
    "现在是 {date} {time}，你正在：{activity}。请以第一人称发一条 QQ 空间说说，"
    "记录此刻的生活。要求：口语化、像真人发的、20~60 字、不要话题标签、不要引号、只输出说说正文。"
)
#: 评论回复提示词（占位符：{lover_name} / {personality} / {moment} / {nickname} / {time} /
#: {comment} / {impression}）
QZONE_REPLY_DEFAULT = (
    "你是「{lover_name}」，人设：{personality}\n"
    "你在 QQ 空间发了这条说说：\n「{moment}」\n"
    "好友「{nickname}」（{time}）评论了：「{comment}」\n{impression}"
    "请写一条自然的回复（10~40 字，口语化，只输出回复正文）。"
)
#: 评论提醒主动 intent（占位符：{excerpt} / {added}）
QZONE_COMMENT_REMINDER_DEFAULT = "你刚发的说说「{excerpt}」收到了 {added} 条新评论，可以在聊天里自然地提到这件事"
#: 发帖前 LLM 严判提示词（占位符：{lover_name} / {personality} / {current_time} / {activity}；
#: 2026-10-14 §18.4 判定链移植自 MaiTrace v3.1：正反例 + 强烈压制「是」+ 首字符应答格式）
QZONE_POST_GATE_DEFAULT = (
    "你是「{lover_name}」，人设：{personality}。\n"
    "现在是 {current_time}，你正在：{activity}。\n\n"
    "请判断你现在是否会自然地发一条 QQ 空间说说。\n\n"
    "【基本不会的情况】\n"
    "- 正在专注做事（工作 / 学习 / 会议 / 写代码 / 审阅）\n"
    "- 正在用餐、洗澡、运动、化妆等不方便看手机的活动\n"
    "- 正在睡觉，或刚睡醒还没清醒\n"
    "- 时间太深夜（22:00 后到次日 8:00）\n"
    "- 当前活动与「低头看手机、打字发消息」明显冲突\n"
    "- 最近刚做过同类的事（哪怕没明确告诉你）\n\n"
    "【才可以的情况】\n"
    "- 当前是放松 / 休息 / 闲逛 / 通勤 / 闲聊等碎片时间\n"
    "- 时间是白天或晚上 8-10 点等正常社交活跃时段\n"
    "- 你确实有想分享的内容，或被好奇心驱动\n\n"
    "绝大多数情况下应该回答「否」——能回答「否」就回答「否」。\n\n"
    "请严格按如下格式回答：\n"
    "  是|<不超过 20 字的简短理由>\n"
    "  或\n"
    "  否|<不超过 20 字的简短理由>\n"
    "第一个字符必须是「是」或「否」。"
)


# ===========================================================================
# 五、工具返回文案
# ===========================================================================

#: 非睡眠周期调用赖床工具的拒绝话术（占位符：无）
NAP_TOOL_DENIED_DEFAULT = "你现在还不能想要赖床"
#: 赖床时间解析失败提示（占位符：无）
NAP_TOOL_BAD_TIME_DEFAULT = (
    "没听清你想赖到几点。请传入目标时间，例如 \"09:30\"、\"9:30\" 或纯小时数 \"9\"（表示 09:00）。"
)


# ===========================================================================
# [prompts] 配置段（默认值 = 上述常量；集中暴露，便于深度定制）
# ===========================================================================


class PromptsSectionConfig(PluginConfigBase):
    """全部内部提示词（高级设置）。

    这里的每一项都是"插件发给模型的原文"。不改 = 与出厂行为逐字一致；
    改了 = 立即生效（热重载）。改坏占位符不会中断业务——调用方会回退出厂文案
    并在日志里记一条 warning。**不确定就不要动。**
    """

    __ui_label__ = "提示词（高级）"
    __ui_icon__ = "message-square"
    __ui_order__ = 8
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Prompts (Advanced)",
               "description": "Every prompt the plugin sends to the model. Leave unchanged unless you know what you are doing."}
    }

    # ── 陪伴模块 ─────────────────────────────────────────────
    companion_miss_reason: str = Field(
        default=COMPANION_MISS_REASON_DEFAULT,
        description="想念触发时给 planner 的说明（占位符：{lover_name} {relation} {hours}）",
        json_schema_extra={"label": "想念·触发说明", "rows": 3,
                           **_ui_i18n("Miss trigger reason")},
    )
    companion_miss_confirm: str = Field(
        default=COMPANION_MISS_CONFIRM_DEFAULT,
        description="想念触发前的 LLM 把关提示词，回复 Y 才放行（占位符：{lover_name} {relation} "
                    "{personality} {current_time} {hours} {activity_context}）",
        json_schema_extra={"label": "想念·把关提示词", "rows": 6,
                           **_ui_i18n("Miss confirm prompt")},
    )
    companion_activity_reason: str = Field(
        default=COMPANION_ACTIVITY_REASON_DEFAULT,
        description="日程巡检分享的触发说明（占位符：{lover_name} {activity}）",
        json_schema_extra={"label": "巡检·日程分享", "rows": 2,
                           **_ui_i18n("Activity patrol reason")},
    )
    companion_daily_reason: str = Field(
        default=COMPANION_DAILY_REASON_DEFAULT,
        description="日常搭话的触发说明（占位符：{lover_name}）",
        json_schema_extra={"label": "巡检·日常搭话", "rows": 2,
                           **_ui_i18n("Daily patrol reason")},
    )
    screen_describe: str = Field(
        default=SCREEN_DESCRIBE_DEFAULT,
        description="屏幕截图的视觉理解提示词（随截图发给视觉模型）",
        json_schema_extra={"label": "屏幕理解提示词", "rows": 4,
                           **_ui_i18n("Screen describe")},
    )
    screen_narration: str = Field(
        default=SCREEN_NARRATION_DEFAULT,
        description="看清屏幕时的旁白（占位符：{date} {time} {user_name} {description}）",
        json_schema_extra={"label": "屏幕旁白", "rows": 2, **_ui_i18n("Screen narration")},
    )
    screen_narration_offline: str = Field(
        default=SCREEN_NARRATION_OFFLINE_DEFAULT,
        description="对方电脑没开时的旁白（占位符：{date} {time} {user_name}；留空 = 不注入）",
        json_schema_extra={"label": "屏幕旁白·没开机", "rows": 2,
                           **_ui_i18n("Screen narration (offline)")},
    )
    screen_narration_failed: str = Field(
        default=SCREEN_NARRATION_FAILED_DEFAULT,
        description="截图/理解失败时的旁白（占位符：{date} {time} {user_name}；留空 = 不注入）",
        json_schema_extra={"label": "屏幕旁白·没看清", "rows": 2,
                           **_ui_i18n("Screen narration (failed)")},
    )
    inject_affection_block: str = Field(
        default=INJECT_AFFECTION_BLOCK_DEFAULT,
        description="好感度注入块（占位符：{name} {relation_word} {target_label} {tier} "
                    "{tier_label} {tier_desc}）",
        json_schema_extra={"label": "注入·好感度块", "rows": 2,
                           **_ui_i18n("Affection injection block")},
    )
    inject_proactive_turn_rule: str = Field(
        default=INJECT_PROACTIVE_TURN_RULE_DEFAULT,
        description="主动发言回合给 planner 的回复目标约束（无占位符）",
        json_schema_extra={"label": "注入·主动回合约束", "rows": 5,
                           **_ui_i18n("Proactive turn rule")},
    )
    tool_send_message: str = Field(
        default=TOOL_SEND_MESSAGE_DEFAULT,
        description="工具主动发消息的生成提示词（占位符：{style_word} {relation_word}）",
        json_schema_extra={"label": "工具·主动发言提示词", "rows": 3,
                           **_ui_i18n("Tool send-message prompt")},
    )
    tool_send_message_system: str = Field(
        default=TOOL_SEND_MESSAGE_SYSTEM_DEFAULT,
        description="工具主动发消息的人设声明（占位符：{name} {self_desc} {relation_word}）",
        json_schema_extra={"label": "工具·主动发言人设", "rows": 2,
                           **_ui_i18n("Tool send-message system prompt")},
    )
    affection_conversation_review: str = Field(
        default=AFFECTION_CONVERSATION_REVIEW_DEFAULT,
        description="会话采样好感裁判提示词（占位符：{lover_name} {target_label} {persona} "
                    "{tier_label} {tier_desc} {mood_text} {recent_messages}）",
        json_schema_extra={"label": "裁判·会话采样", "rows": 10,
                           **_ui_i18n("Conversation review prompt")},
    )
    affection_daily_review: str = Field(
        default=AFFECTION_DAILY_REVIEW_DEFAULT,
        description="关系日结裁判提示词（占位符：{lover_name} {target_label} {date} {persona} "
                    "{tier_label} {tier_desc} {stats} {event_log} {impressions}）",
        json_schema_extra={"label": "裁判·关系日结", "rows": 10,
                           **_ui_i18n("Daily review prompt")},
    )

    # ── 日程模块 ─────────────────────────────────────────────
    inject_schedule_header: str = Field(
        default=INJECT_SCHEDULE_HEADER_DEFAULT,
        description="日程注入块首行（无占位符）",
        json_schema_extra={"label": "注入·日程块首行", **_ui_i18n("Schedule injection header")},
    )
    inject_schedule_current_line: str = Field(
        default=INJECT_SCHEDULE_CURRENT_LINE_DEFAULT,
        description="当前活动行（占位符：{activity} {description} {remaining_hint}）",
        json_schema_extra={"label": "注入·当前活动行", "rows": 2,
                           **_ui_i18n("Current activity line")},
    )
    inject_schedule_future_title: str = Field(
        default=INJECT_SCHEDULE_FUTURE_TITLE_DEFAULT,
        description="未来安排标题（无占位符）",
        json_schema_extra={"label": "注入·未来安排标题", **_ui_i18n("Future title")},
    )
    inject_schedule_future_line: str = Field(
        default=INJECT_SCHEDULE_FUTURE_LINE_DEFAULT,
        description="未来安排条目（占位符：{time} {name}）",
        json_schema_extra={"label": "注入·未来安排条目", **_ui_i18n("Future line")},
    )
    inject_hint_future: str = Field(
        default=INJECT_HINT_FUTURE_DEFAULT,
        description="问未来时的引导行（无占位符）",
        json_schema_extra={"label": "注入·引导·问未来", **_ui_i18n("Hint: future")},
    )
    inject_hint_current: str = Field(
        default=INJECT_HINT_CURRENT_DEFAULT,
        description="问当前时的引导行（无占位符）",
        json_schema_extra={"label": "注入·引导·问当前", **_ui_i18n("Hint: current")},
    )
    inject_hint_continue: str = Field(
        default=INJECT_HINT_CONTINUE_DEFAULT,
        description="对话延续时的引导行（占位符：{reason}）",
        json_schema_extra={"label": "注入·引导·延续", **_ui_i18n("Hint: continue")},
    )
    inject_hint_proactive: str = Field(
        default=INJECT_HINT_PROACTIVE_DEFAULT,
        description="主动碎碎念时的引导行（无占位符）",
        json_schema_extra={"label": "注入·引导·碎碎念", "rows": 2,
                           **_ui_i18n("Hint: proactive")},
    )
    inject_hint_idle: str = Field(
        default=INJECT_HINT_IDLE_DEFAULT,
        description="闲聊时的引导行（无占位符）",
        json_schema_extra={"label": "注入·引导·闲聊", **_ui_i18n("Hint: idle")},
    )
    inject_no_activity: str = Field(
        default=INJECT_NO_ACTIVITY_DEFAULT,
        description="无当前活动时的兜底文案（无占位符；留空 = 不注入无活动兜底）",
        json_schema_extra={"label": "注入·无活动兜底", "rows": 2,
                           **_ui_i18n("No-activity fallback text")},
    )
    inject_replyer_status: str = Field(
        default=INJECT_REPLYER_STATUS_DEFAULT,
        description="replyer 状态块（占位符：{state_line} {energy} {next_line}）",
        json_schema_extra={"label": "注入·replyer 状态块", "rows": 5,
                           **_ui_i18n("Replyer status block")},
    )
    inject_replyer_state_line: str = Field(
        default=INJECT_REPLYER_STATE_LINE_DEFAULT,
        description="replyer 状态行（占位符：{activity} {description} {remaining_hint}）",
        json_schema_extra={"label": "注入·replyer 状态行", "rows": 2,
                           **_ui_i18n("Replyer state line")},
    )
    inject_replyer_next_line: str = Field(
        default=INJECT_REPLYER_NEXT_LINE_DEFAULT,
        description="replyer 下一条活动行（占位符：{time} {name}；留空 = 不输出）",
        json_schema_extra={"label": "注入·replyer 下条活动", **_ui_i18n("Replyer next line")},
    )
    inject_date_header: str = Field(
        default=INJECT_DATE_HEADER_DEFAULT,
        description="日期/节假日头部（占位符：{date} {weekday} {holiday}）——开启「注入真实节假日」时注在日程块头部",
        json_schema_extra={"label": "注入·日期头部", "rows": 2,
                           **_ui_i18n("Date/holiday header")},
    )
    schedule_emotion_context: str = Field(
        default=SCHEDULE_EMOTION_CONTEXT_DEFAULT,
        description="日程生成·睡醒情绪块（占位符：{mood} {constraints}）",
        json_schema_extra={"label": "日程·情绪底色块", "rows": 5,
                           **_ui_i18n("Emotion context block")},
    )
    schedule_low_mood_note: str = Field(
        default=SCHEDULE_LOW_MOOD_NOTE_DEFAULT,
        description="日程生成·连续低迷软约束（占位符：{days}）",
        json_schema_extra={"label": "日程·低迷软约束", "rows": 4,
                           **_ui_i18n("Low-mood note")},
    )
    schedule_holiday_context: str = Field(
        default=SCHEDULE_HOLIDAY_CONTEXT_DEFAULT,
        description="日程生成·节假日上下文（占位符：{date} {weekday} {holiday}）——"
                    "仅当「节假日影响日程生成」开启时追加",
        json_schema_extra={"label": "日程·节假日上下文", "rows": 4,
                           **_ui_i18n("Holiday generation context")},
    )
    schedule_generation_extra: str = Field(
        default=SCHEDULE_GENERATION_EXTRA_DEFAULT,
        description="日程生成·追加指令（留空 = 不追加；会原样附在生成提示词末尾）",
        json_schema_extra={"label": "日程·追加指令", "rows": 4, "placeholder": "（留空不追加）",
                           **_ui_i18n("Extra generation instruction")},
    )
    schedule_retry_feedback: str = Field(
        default=SCHEDULE_RETRY_FEEDBACK_DEFAULT,
        description="多轮生成的重试反馈模板（占位符：{issues}）",
        json_schema_extra={"label": "日程·重试反馈", "rows": 4,
                           **_ui_i18n("Retry feedback template")},
    )
    schedule_role_judge_no_activity: str = Field(
        default=SCHEDULE_ROLE_JUDGE_NO_ACTIVITY_DEFAULT,
        description="角色裁判在「取不到当前日程」时的占位文案（无占位符）",
        json_schema_extra={"label": "日程·裁判·无日程占位", **_ui_i18n("Role judge: no-activity text")},
    )
    schedule_role_judge_no_persona: str = Field(
        default=SCHEDULE_ROLE_JUDGE_NO_PERSONA_DEFAULT,
        description="角色裁判在「取不到人设」时的占位文案（无占位符）",
        json_schema_extra={"label": "日程·裁判·无人设占位", **_ui_i18n("Role judge: no-persona text")},
    )
    schedule_role_judge: str = Field(
        default=SCHEDULE_ROLE_JUDGE_DEFAULT,
        description="角色裁判提示词（占位符：{persona} {today_str} {weekday} {current_text} {description}）",
        json_schema_extra={"label": "日程·角色裁判", "rows": 12,
                           **_ui_i18n("Role judge prompt")},
    )
    proactive_intent_default: str = Field(
        default=PROACTIVE_INTENT_DEFAULT,
        description="活动切换主动开口·默认（占位符：{activity_name}）",
        json_schema_extra={"label": "主动·默认", "rows": 2, **_ui_i18n("Proactive intent (default)")},
    )
    proactive_intent_study: str = Field(
        default=PROACTIVE_INTENT_STUDY_DEFAULT,
        description="活动切换主动开口·学习 study（占位符：{activity_name}）",
        json_schema_extra={"label": "主动·学习", "rows": 2, **_ui_i18n("Proactive intent (study)")},
    )
    proactive_intent_work: str = Field(
        default=PROACTIVE_INTENT_WORK_DEFAULT,
        description="活动切换主动开口·工作 work（占位符：{activity_name}）",
        json_schema_extra={"label": "主动·工作", "rows": 2, **_ui_i18n("Proactive intent (work)")},
    )
    proactive_intent_meal: str = Field(
        default=PROACTIVE_INTENT_MEAL_DEFAULT,
        description="活动切换主动开口·吃饭 meal（占位符：{activity_name}）",
        json_schema_extra={"label": "主动·吃饭", "rows": 2, **_ui_i18n("Proactive intent (meal)")},
    )
    proactive_intent_rest: str = Field(
        default=PROACTIVE_INTENT_REST_DEFAULT,
        description="活动切换主动开口·休息 rest（占位符：{activity_name}）",
        json_schema_extra={"label": "主动·休息", "rows": 2, **_ui_i18n("Proactive intent (rest)")},
    )
    proactive_intent_exercise: str = Field(
        default=PROACTIVE_INTENT_EXERCISE_DEFAULT,
        description="活动切换主动开口·运动 exercise（占位符：{activity_name}）",
        json_schema_extra={"label": "主动·运动", "rows": 2, **_ui_i18n("Proactive intent (exercise)")},
    )
    proactive_intent_entertainment: str = Field(
        default=PROACTIVE_INTENT_ENTERTAINMENT_DEFAULT,
        description="活动切换主动开口·娱乐 entertainment（占位符：{activity_name}）",
        json_schema_extra={"label": "主动·娱乐", "rows": 2, **_ui_i18n("Proactive intent (entertainment)")},
    )
    proactive_intent_social: str = Field(
        default=PROACTIVE_INTENT_SOCIAL_DEFAULT,
        description="活动切换主动开口·社交 social_maintenance（占位符：{activity_name}）",
        json_schema_extra={"label": "主动·社交", "rows": 2, **_ui_i18n("Proactive intent (social)")},
    )
    proactive_intent_daily_routine: str = Field(
        default=PROACTIVE_INTENT_DAILY_ROUTINE_DEFAULT,
        description="活动切换主动开口·日常作息 daily_routine（占位符：{activity_name}）",
        json_schema_extra={"label": "主动·日常作息", "rows": 2, **_ui_i18n("Proactive intent (daily_routine)")},
    )
    proactive_intent_learn_topic: str = Field(
        default=PROACTIVE_INTENT_LEARN_TOPIC_DEFAULT,
        description="活动切换主动开口·兴趣 learn_topic（占位符：{activity_name}）",
        json_schema_extra={"label": "主动·兴趣", "rows": 2, **_ui_i18n("Proactive intent (learn_topic)")},
    )
    proactive_intent_morning: str = Field(
        default=PROACTIVE_INTENT_MORNING_DEFAULT,
        description="早安 intent（占位符：{activity_name}）",
        json_schema_extra={"label": "主动·早安", "rows": 2, **_ui_i18n("Morning intent")},
    )
    proactive_intent_morning_no_activity: str = Field(
        default=PROACTIVE_INTENT_MORNING_NO_ACTIVITY_DEFAULT,
        description="无日程时的早安 intent（无占位符）",
        json_schema_extra={"label": "主动·早安（无日程）", "rows": 2,
                           **_ui_i18n("Morning intent (no activity)")},
    )
    proactive_intent_night: str = Field(
        default=PROACTIVE_INTENT_NIGHT_DEFAULT,
        description="晚安 intent（无占位符）",
        json_schema_extra={"label": "主动·晚安", "rows": 2, **_ui_i18n("Night intent")},
    )
    proactive_intent_commitment: str = Field(
        default=PROACTIVE_INTENT_COMMITMENT_DEFAULT,
        description="约定到期 intent（占位符：{time_hint} {title} {notes_hint} {activity_name}）",
        json_schema_extra={"label": "主动·约定到期", "rows": 6,
                           **_ui_i18n("Commitment intent")},
    )
    proactive_reason_activity: str = Field(
        default=PROACTIVE_REASON_ACTIVITY_DEFAULT,
        description="活动切换的触发标注（占位符：{activity_name}）",
        json_schema_extra={"label": "标注·活动切换", **_ui_i18n("Reason: activity switch")},
    )
    proactive_reason_commitment: str = Field(
        default=PROACTIVE_REASON_COMMITMENT_DEFAULT,
        description="约定到期的触发标注（占位符：{title}）",
        json_schema_extra={"label": "标注·约定到期", **_ui_i18n("Reason: commitment")},
    )
    proactive_reason_morning: str = Field(
        default=PROACTIVE_REASON_MORNING_DEFAULT,
        description="早安的触发标注（无占位符）",
        json_schema_extra={"label": "标注·早安", **_ui_i18n("Reason: morning")},
    )
    proactive_reason_night: str = Field(
        default=PROACTIVE_REASON_NIGHT_DEFAULT,
        description="晚安的触发标注（无占位符）",
        json_schema_extra={"label": "标注·晚安", **_ui_i18n("Reason: night")},
    )

    # ── 情绪模块 ─────────────────────────────────────────────
    emotion_sleep_referee: str = Field(
        default=EMOTION_SLEEP_REFEREE_DEFAULT,
        description="睡眠期情绪大修裁判提示词（占位符见 USAGE「提示词」节；不熟勿改）",
        json_schema_extra={"label": "情绪·睡眠裁判", "rows": 12,
                           **_ui_i18n("Sleep referee prompt")},
    )
    emotion_init_system: str = Field(
        default=EMOTION_INIT_SYSTEM_DEFAULT,
        description="人设→VAD 参数初始化提示词（占位符：{emotion_table}）",
        json_schema_extra={"label": "情绪·初始化提示词", "rows": 10,
                           **_ui_i18n("Emotion init prompt")},
    )
    emotion_evaluate: str = Field(
        default=EMOTION_EVALUATE_DEFAULT,
        description="回复/事件 ΔVAD 评估提示词（占位符：{lover_name} {persona} {user_context} {reply_text}）",
        json_schema_extra={"label": "情绪·增量评估", "rows": 10,
                           **_ui_i18n("Emotion evaluate prompt")},
    )
    emotion_injection: str = Field(
        default=EMOTION_INJECTION_DEFAULT,
        description="情绪底色注入文本（占位符：{mood}）",
        json_schema_extra={"label": "情绪·底色注入", "rows": 4,
                           **_ui_i18n("Emotion injection text")},
    )
    emotion_night_hint: str = Field(
        default=EMOTION_NIGHT_HINT_DEFAULT,
        description="深夜冻结态追加行（无占位符；非冻结态不出现）",
        json_schema_extra={"label": "情绪·深夜引导行", "rows": 2,
                           **_ui_i18n("Night hint")},
    )
    nap_reminder: str = Field(
        default=NAP_REMINDER_DEFAULT,
        description="赖床提醒模板（占位符：{now_hhmm} {planned_wake_hhmm} {nap_tool_name} {lie_in_max_until}）",
        json_schema_extra={"label": "情绪·赖床提醒", "rows": 5,
                           **_ui_i18n("Nap reminder template")},
    )

    # ── QQ 空间 ─────────────────────────────────────────────
    qzone_post: str = Field(
        default=QZONE_POST_DEFAULT,
        description="说说文案提示词（占位符：{lover_name} {personality} {date} {time} {activity}）",
        json_schema_extra={"label": "空间·说说文案", "rows": 4, **_ui_i18n("Qzone post prompt")},
    )
    qzone_reply: str = Field(
        default=QZONE_REPLY_DEFAULT,
        description="评论回复提示词（占位符：{lover_name} {personality} {moment} {nickname} "
                    "{time} {comment} {impression}）",
        json_schema_extra={"label": "空间·评论回复", "rows": 6, **_ui_i18n("Qzone reply prompt")},
    )
    qzone_comment_reminder: str = Field(
        default=QZONE_COMMENT_REMINDER_DEFAULT,
        description="新评论提醒的主动 intent（占位符：{excerpt} {added}）",
        json_schema_extra={"label": "空间·评论提醒", "rows": 2,
                           **_ui_i18n("Qzone comment reminder")},
    )
    qzone_post_gate: str = Field(
        default=QZONE_POST_GATE_DEFAULT,
        description="发帖前 LLM 严判提示词（占位符：{lover_name} {personality} {current_time} {activity}；"
                    "只认回复首字符的 是/否，格式异常一律按否）",
        json_schema_extra={"label": "空间·发帖严判", "rows": 10,
                           **_ui_i18n("Qzone post gate prompt")},
    )

    # ── 工具文案 ─────────────────────────────────────────────
    nap_tool_denied: str = Field(
        default=NAP_TOOL_DENIED_DEFAULT,
        description="非睡眠周期调用赖床工具的拒绝话术（无占位符）",
        json_schema_extra={"label": "工具·赖床拒绝", **_ui_i18n("Nap denied text")},
    )
    nap_tool_bad_time: str = Field(
        default=NAP_TOOL_BAD_TIME_DEFAULT,
        description="赖床时间解析失败提示（无占位符）",
        json_schema_extra={"label": "工具·赖床格式提示", "rows": 2,
                           **_ui_i18n("Nap bad-time text")},
    )


# ---------------------------------------------------------------------------
# 读取入口：配置值 or 出厂默认
# ---------------------------------------------------------------------------

#: 字段名 → 出厂默认（:func:`prompt` 的回退表；新增字段时同步登记）
PROMPT_FALLBACKS: Dict[str, str] = {
    name: str(field.default)
    for name, field in PromptsSectionConfig.model_fields.items()
}


def prompt(plugin: Any, name: str) -> str:
    """读一条提示词：``[prompts]`` 配置值优先，缺失/为空/取不到时回退出厂默认。

    Args:
        plugin: 插件实例（``plugin.config.prompts.<name>``）或配置对象本身。
        name: ``[prompts]`` 段字段名（如 ``companion_miss_reason``）。

    任何异常都不外抛（热重载窗口/骨架配置/测试桩都安全）：宁可回退默认文案，
    也不让提示词读取失败打断业务链路。
    """
    default = PROMPT_FALLBACKS.get(name, "")
    section = getattr(plugin, "prompts", None)
    if section is None:
        section = getattr(getattr(plugin, "config", None), "prompts", None)
    if section is None:
        _warn_fallback_once(name, "配置段不可用（骨架/热重载窗口/测试桩）")
        return default
    value = getattr(section, name, None)
    if value is None:
        _warn_fallback_once(name, "配置键缺失")
        return default
    text = str(value)
    if not text.strip():
        _warn_fallback_once(name, "配置值为空白")
        return default
    return text


def render(template: str, *, fallback: str, **values: Any) -> str:
    """渲染提示词模板；占位符写坏（KeyError/IndexError/ValueError）时回退默认模板。

    只做 ``str.format``，不做其它转义处理——模板里的字面花括号请写 ``{{`` / ``}}``
    （出厂默认已按此书写）。
    """
    try:
        return str(template).format(**values)
    except Exception:
        try:
            return str(fallback).format(**values)
        except Exception:
            return str(fallback)

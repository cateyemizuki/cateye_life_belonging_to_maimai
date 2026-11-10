"""提示词默认值与用户可定制文案。

内部数据协议、数值裁判、注入结构与目标约束由代码维护；配置仅保留
陪伴口吻、屏幕描述、日程追加要求、早晚安与空间文案等常用定制入口。
模板渲染失败回退内置值，不把配置错误带入业务链路。
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar, Dict, Set

from maibot_sdk import Field, PluginConfigBase
from pydantic import model_validator

from core.i18n import ui_i18n as _ui_i18n

logger = logging.getLogger(__name__)

#: 已告警过"回退默认"的提示词键（进程内去重：回退告警每键至多一条，防刷日志）
_FALLBACK_WARNED: Set[str] = set()


def _warn_fallback_once(name: str, reason: str) -> None:
    """提示词回退默认时记一条 warning（每键进程内至多一次；6.5-G：docstring 承诺落地）。"""
    if name in _FALLBACK_WARNED:
        return
    _FALLBACK_WARNED.add(name)
    logger.warning("提示词 %s 回退出厂默认模板（%s）；请检查 [prompts] 配置", name, reason)


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
    "现在是{date} {time}，你想看一眼{user_name}的电脑屏幕，但暂时无法连接TA的电脑"
)
SCREEN_NARRATION_FAILED_DEFAULT = (
    "现在是{date} {time}，你看了眼{user_name}的电脑屏幕，但没看清TA在干什么"
)

#: 好感度注入块（占位符：{name} / {relation_word} / {target_label} / {tier} /
#: {tier_label} / {tier_desc}）；整块文本含前导换行
INJECT_AFFECTION_BLOCK_DEFAULT = (
    "\n【{name}与{relation_word}（{target_label}）的好感度】档位 {tier}·{tier_label}（{tier_desc}）"
)

#: 主动发言回合给 planner 的主动激活引导（软约束；无占位符）。
#: PR-PROACTIVE-REPLY（3.17.2）：不再要求「优先回应对方最后一条发言」（那条消息
#: 可能已过时）；改为按状况概括+当前状态发言。读取仍经 prompt()（内部模板名，
#: 回退表机制不变，兼容自定义模板口径）。
INJECT_PROACTIVE_TURN_RULE_DEFAULT = (
    "\n【本轮是主动发言】现在是你在主动找对方说话（不是对方来找你）："
    "\n- 本轮为插件主动激活：根据你的状况概括，结合你此刻的状态自然发言，"
    "不需要特地回复某个信息，也不要围绕某条过时的消息组织回复；"
    "\n- 不要回复你自己发送的消息，也不要在发言里引用你自己的消息；"
    "\n- 如果对方还没回你，直接说新内容或起一个新话题；"
    "\n- reply 的 msg_id 只能选对方发送的消息。"
)

#: 2026-11-04 评估 F4 清理：主动私信工具（lbm_mai_lover_send_message）已整条移除
#: （companion/components.py 仅存移除说明注释，全仓无注册点），原
#: TOOL_SEND_MESSAGE_DESCRIPTION / TOOL_SEND_MESSAGE_TEXT_DESCRIPTION /
#: TOOL_SEND_MESSAGE_DEFAULT / TOOL_SEND_MESSAGE_SYSTEM_DEFAULT 四常量与
#: [prompts].tool_send_message 可编辑字段、PROMPT_FALLBACKS 两条目一并删除。
#: 旧配置里的 tool_send_message 值由 _ignore_internal_overrides 静默滤掉（死键无行为）。

#: 会话采样好感裁判提示词（占位符：{lover_name} / {target_label} /
#: {tier_label} / {tier_desc} / {mood_text} / {recent_messages}；人设经 LLMService
#: with_identity 注入 system_prompt，{persona} 占位已移除，渲染方传空兼容旧定制模板）
AFFECTION_CONVERSATION_REVIEW_DEFAULT = """你不参与对话，只看下面给出的材料，给这段相处评估一个**有界的好感度增量**。
你看不到任何好感度数值，也看不到分数与基线，只看到关系所处的阶段描述——不要猜测具体数值。

【判定标准】
1. event：用不超过 30 字概括这段相处里最值得记住的一件事；没有就写「日常闲聊」；
2. delta：好感度增量，范围 [-4, +5]——
   - 阿谀奉承、无意义刷屏、单方面索取：不给正分（0 或负）；
   - 真诚的关心、共同完成一件事、化解了一次矛盾：才给正分；
   - 明显的争吵、冒犯、被冷落：给负分；
   - 平淡的日常闲聊：接近 0；
3. tag 只能是 normal / conflict / apology / sweet 之一；
4. 只输出 JSON：{{"event": "...", "delta": 0.0, "tag": "normal"}}

你是 AI 角色「{lover_name}」与{target_label}之间关系的会话观察裁判。

【双方关系所处的阶段】
{tier_label}：{tier_desc}

【角色近期的情绪底色（低分辨率描述，仅供理解语气）】
{mood_text}

【最近对方发来的消息（按时间从旧到新，只有对方一侧）】
{recent_messages}"""

#: 关系日结裁判提示词（占位符：{lover_name} / {target_label} / {date} /
#: {tier_label} / {tier_desc} / {stats} / {event_log} / {impressions}；人设经 LLMService
#: with_identity 注入 system_prompt，{persona} 占位已移除，渲染方传空兼容旧定制模板）
AFFECTION_DAILY_REVIEW_DEFAULT = """你看不到任何好感度数值，也看不到分数与基线，只看到关系所处的阶段描述——不要猜测具体数值。

【判定标准】
1. delta_score：这一天对关系的影响，范围 [-4, +4]——
   - 真诚的关心、共同完成一件事、化解矛盾：正分；
   - 阿谀奉承、无意义刷屏：不给正分；
   - 争吵、冒犯、被冷落、整天没有互动：负分；
   - 平淡无事：接近 0；
2. impressions：0~3 条，每条一句话（不超过 40 字）记下今天值得记住的事；
   只写事实与感受，不写分数、不写评价性标签、不编造没有发生的事；
3. tone：一句话概括今天的关系温度（不超过 30 字）；
4. reason：必须直接引用下面事件日志或对方原话的片段作为依据，禁止脑补不存在的事；
5. 只输出 JSON：{{"delta_score": 0.0, "impressions": ["..."], "tone": "...", "reason": "..."}}

你是 AI 角色「{lover_name}」与{target_label}之间关系的日结裁判。
任务：根据 {date} 这一天的相处记录，给这一天对关系的影响做一个有界的总结。

【双方关系所处的阶段】
{tier_label}：{tier_desc}

【今日互动统计】
{stats}

【今日事件日志（时间、类型、摘要、当日入账增量）】
{event_log}

【最近积累的相处印象（旧→新）】
{impressions}"""


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
#: 对话延续时的引导行（占位符：{reason}；2026-10-24 用户直派弱化：旧版「可自然带过当前活动」
#: 带弱鼓励色彩——现明确「只是背景信息、不问不说」，仅话题接不上时才允许用一句当前活动开新话题）
INJECT_HINT_CONTINUE_DEFAULT = (
    "提示：对话延续中（{reason}）。当前活动只是背景信息：用户不问就不要说；"
    "只有当话题确实接不上、需要找新话题时，才可以用一句当前活动自然开启新话题。"
    "不要主动汇报日程、接下来的安排或刚完成的事。"
)
#: 主动碎碎念时的引导行（占位符：无；2026-10-20 用户指示：不再引导 bot 无问自答提日程）
INJECT_HINT_PROACTIVE_DEFAULT = (
    "提示：不要主动提及你正在日程里做什么。只有当对方问起你在做什么、"
    "或你确实需要引出新话题时，才简要带一句当前活动。"
    "群冷场时优先等待或结束本轮，不要为填补空白而主动起话题或汇报日程/接下来安排。"
)
#: 闲聊时的引导行（占位符：无；2026-11-04 强化：补充「闲聊不主动提自己日程、
#: 除非对方问起」语义——软约束已被生产证伪不能单靠它，此处仅作双通道门控的辅助）
INJECT_HINT_IDLE_DEFAULT = (
    "提示：仅供参考；不相关请完全忽略，不要刻意提及。"
    "闲聊时不主动提及自己的日程安排，除非对方问起；也不要主动汇报接下来安排或刚完成的事。"
)
#: 无当前活动时的兜底文案（占位符：无；2026-10-10 N1-2：
#: 日程空档期不静默早退，改注入这一句；留空 = 不注入无活动兜底）
INJECT_NO_ACTIVITY_DEFAULT = "你现在没有进行中的活动，处于空闲状态；不要声称正在做某事，也不要汇报不存在的活动或日程。"
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
# ── spine 日意图生成提示词（2026-10-25 滚动化改造新增；R7 依据 = 用户 2026-10-25 直派，
#    设计目录 D:/deepseek-harness_workspace/maibot-plugins/2026-10-25-日程系统滚动化改造设计）──
# PR8 3.0.0：spine 为唯一生成路线（旧 [schedule].spine_mode 已退役），本提示词无条件生效。
# 零 emoji、不引导主动提日程、不要求长描述。
#: spine 日意图主任务（占位符：{min_intentions} / {max_intentions} / {wake_text} /
#: {sleep_text} / {no_sleep_line} / {described_note}）
SCHEDULE_SPINE_TASK_DEFAULT = (
    "【任务】生成今天的日意图（spine）JSON：\n"
    "今天只决定 {min_intentions}-{max_intentions} 件「今天想做的事」（意图），不是排满全天的时间表。\n"
    "- 不要求覆盖全天：意图之间、前后的空档都是合法的（自由时间不需要写成意图）\n"
    "- 每条意图给出允许发生的窗口 window：[\"HH:MM\", \"HH:MM\"]（24 小时制，起=窗口开始，止=窗口结束）\n"
    "- min_minutes / max_minutes：这次活动最短/最长做多少分钟（max_minutes 不得小于 min_minutes）\n"
    "- priority：high / medium / low；hardness：hard = 硬约定（时间固定不可移动），soft = 软意图\n"
    "- interruptibility：0~1 的小数，越高越容易被别的事情打断\n"
    "- 作息锚点：{wake_text} 起床，{sleep_text} 入睡；软意图的窗口必须落在清醒时段内\n"
    "{no_sleep_line}"
    "- 三餐参考常识窗口（软参考）：早餐 06:00-09:00、午餐 11:00-14:00、晚餐 17:00-20:00\n"
    "{described_note}"
    "- 意图只描述角色自己的生活安排；不要安排「给对方发消息 / 等对方回复 / 主动找人聊天」一类动作\n"
    "- 严格遵守开头的角色人设与兴趣偏好；与【最近几天日程参考】里的活动不要同名重复\n"
    "- 只输出 JSON：{{\"intentions\": [...]}}，严格 JSON、无注释、无多余文字"
)
#: spine 模式的约定硬约束行（占位符：无；存在今日约定时追加）
SCHEDULE_SPINE_COMMITMENT_NOTE_DEFAULT = (
    "要求：上述约定按每条标注的 hardness 纳入——hard=时间固定的硬约定（不得改期/降级），"
    "soft=可安排的软约定（不得被升级为 hard）；goal_type 用 social_maintenance 或对应类型。"
)
#: spine 模式的睡觉锚点行（占位符：{sleep_text} / {wake_text}）
SCHEDULE_SPINE_SLEEP_LINE_DEFAULT = (
    "- 睡觉是一条固定意图：窗口 {sleep_text} 到次日 {wake_text}，hardness=hard，"
    "必须包含在意图里\n"
)
#: spine 模式无睡眠时的锚点行（占位符：{sleep_text} / {wake_text}）
SCHEDULE_SPINE_NO_SLEEP_LINE_DEFAULT = (
    "- 无睡眠模式：不生成睡觉意图，睡眠时段（{sleep_text} 到次日 {wake_text}）整体留空，"
    "任何意图的名称都不得包含「睡 / 眠 / 憩」字样\n"
)
#: spine 模式描述行（占位符：{max_described}）
SCHEDULE_SPINE_DESCRIBED_NOTE_DEFAULT = (
    "- note 字段：最多给 {max_described} 条重要意图写一句 15-25 字的补充（想做什么、为什么）；"
    "其余意图不要写 note\n"
)
#: spine 模式描述行（全部不写）
SCHEDULE_SPINE_NO_DESCRIBED_NOTE_DEFAULT = "- 所有意图都不要写 note 字段\n"
#: 角色裁判提示词（占位符：{today_str} / {weekday} / {current_text} / {description}；
#: 人设经 LLMService with_identity 注入 system_prompt，{persona} 占位已移除，
#: 渲染方传空兼容旧定制模板）
SCHEDULE_ROLE_JUDGE_DEFAULT = (
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
    "只输出 JSON，不要其他文字。\n\n"
    "【日期信息】\n今天是 {today_str}（{weekday}）\n\n"
    "【当前日程】\n{current_text}\n\n"
    "【用户请求】\n{description}"
)

#: 角色裁判无日程时的占位（占位符：无）
SCHEDULE_ROLE_JUDGE_NO_ACTIVITY_DEFAULT = "暂无可靠日程"
#: 角色裁判人设缺失占位（占位符：无）
SCHEDULE_ROLE_JUDGE_NO_PERSONA_DEFAULT = "未提供"

#: 2026-10-20 用户指示：活动切换主动开口整条移除，原 PROACTIVE_INTENT_DEFAULT 与
#: PROACTIVE_INTENT_STUDY/WORK/MEAL/REST/EXERCISE/ENTERTAINMENT/SOCIAL/DAILY_ROUTINE/
#: LEARN_TOPIC 及 PROACTIVE_INTENT_COMMITMENT_DEFAULT（约定到期）常量已删除，
#: [prompts] 段的同名 Field 一并移除；主动开口只剩早/晚安。

#: 早安 intent（纯问候导向；2026-10-20 用户指示：不再引导提及日程/安排）
PROACTIVE_INTENT_MORNING_DEFAULT = (
    "Bot 刚睡醒，新的一天开始了，请自然地向大家道一声早安；"
    "语气符合人设，简短随意，不要提日程或今天的安排"
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
#: 约定到期 intent 已随活动切换主动开口一并删除（2026-10-20 用户指示）

#: 主动行为 reason 文案（进主动任务块，标注为什么触发；占位符见各条）
PROACTIVE_REASON_MORNING_DEFAULT = "日程：自然醒后的早安问候"
PROACTIVE_REASON_NIGHT_DEFAULT = "日程：入睡前的晚安问候"


# ===========================================================================
# 三、情绪模块提示词
# ===========================================================================

#: 睡眠期情绪大修裁判提示词（占位符：见 emotion/referee.py 组装点；
#: 人设经 LLMService with_identity 注入 system_prompt，{persona} 占位已移除，
#: 渲染方传空兼容旧定制模板）
EMOTION_SLEEP_REFEREE_DEFAULT = """你是一个客观的情绪裁判系统，负责对 AI 角色的睡眠期情绪进行大修。
你不参与对话，只依据下面的客观数据输出 JSON。

【评分规则】
1. 越靠近睡前的情绪事件，权重略微增加（近因效应）；强度大的事件影响更大。
2. 睡眠时长影响恢复效果：<5 小时恢复极差（V 负向、A 升高）；6-9 小时恢复最佳（V 回升）；>10 小时睡懵（A 大幅降低）。
3. 日程预期：评估前一日日程是"释怀"还是"反刍"，第二日日程是"期待"还是"焦虑"（第二天有考试/挑战 → A 升高、D 降低）。
4. 必须结合人设性格做情绪过滤（如高神经质 → 焦虑更容易留存）。
5. R_yesterday / E_tomorrow / 约定情况是代码层已计算的参考数值，最终增量仍需你综合人设与日志判断。
6. 给出最终的 ΔVAD（增量，非绝对值），每一维范围必须在 [-{limit:.2f}, {limit:.2f}] 之间。
7. reason 每条不超过 30 字，概括依据即可，不必逐字引用原文，禁止脑补不存在的事件。

只输出 JSON：{{"delta_vad": {{"valence": 0.0, "arousal": 0.0, "dominance": 0.0}}, "reason_valence": "...", "reason_arousal": "...", "reason_dominance": "..."}}

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

【当日约定兑现情况】净兑现 {commitment_net:+.0f} 次（正=兑现多于违约，负=违约多于兑现）"""

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

#: 回复/事件 ΔVAD 增量评估提示词（占位符：{lover_name} / {user_context} / {reply_text}；
#: 人设经 LLMService with_identity 注入 system_prompt，{persona} 占位已移除，
#: 渲染方传空兼容旧定制模板）
EMOTION_EVALUATE_DEFAULT = """你看不到角色当前的绝对情绪数值，只根据下面这段交流内容，评估"这轮交流/这件事"给角色情绪带来的变化量 ΔVAD。

【要求】
1. event 用不超过 30 字概括「这轮交流发生了什么」（谁说了什么、发生了什么、结果如何），不要笼统写"日常对话"；
2. ΔV/ΔA/ΔD 每一维范围 [-0.5, 0.5]；没有明显情绪波动时全部接近 0；
3. 只输出 JSON：{{"event": "...", "delta_vad": {{"valence": 0.0, "arousal": 0.0, "dominance": 0.0}}}}

你是 AI 角色「{lover_name}」的情绪增量评估器。

【用户侧最后一条消息（对话上下文；可能为空）】
{user_context}

【AI 的回复内容 / 待评估的交流内容】
{reply_text}"""

#: 情绪底色注入文本（占位符：{mood}）——planner/replyer 双通道共用
#: 3.17.29（复核 VAD-4.1）：原「要求：语气、措辞、节奏与该情绪保持一致」是强制表演
#: 指令，会把情绪变成每轮的表达任务；改为轻量背景参考（用户定案：VAD 只轻微影响
#: 情绪走向，不决定本轮说什么）。句末由 {mood} 自带句号，模板不再追加（消双句号）。
EMOTION_INJECTION_DEFAULT = (
    "【轻微心境参考】{mood}\n"
    "仅可轻微影响语气；以人设和当前对话为准，不必刻意表现，"
    "也不据此添加话题、经历或改变回复任务。"
    "严禁在回复中直接说出情绪标签词汇或任何数值；不要主动提及或解释本提示本身。"
)
#: 深夜冻结态追加行（占位符：无；非冻结态不出现）
EMOTION_NIGHT_HINT_DEFAULT = (
    "（现在是深夜，你本该在睡觉。回复尽量短、慢、软一些；"
    "不要主动开启新话题；不要提及本提示。）"
)
# PR8 3.0.0（依据：用户 2026-10-27 授权直接清理 + 逐符号审定清单）：NAP_REMINDER_DEFAULT
# 与 [prompts].nap_reminder 随 M9 赖床提醒链整链删除；NAP_TOOL_DENIED_DEFAULT /
# NAP_TOOL_BAD_TIME_DEFAULT（[prompts].nap_tool_denied / nap_tool_bad_time）同批删除
# （赖床工具面已清退）。


# ===========================================================================
# 四、QQ 空间提示词
# ===========================================================================

#: 说说文案提示词（占位符：{date} / {time} / {activity}；人设经 LLMService
#: with_identity 注入 system_prompt，{lover_name}/{personality} 占位已移除，
#: 渲染方传空兼容旧定制模板）
QZONE_POST_DEFAULT = (
    "请以第一人称发一条 QQ 空间说说，记录此刻的生活。"
    "要求：口语化、像真人发的、20~60 字、不要话题标签、不要引号、只输出说说正文。"
    "避免自我复读：不重复近期自己空间发过的内容与措辞；允许主题自然延续——"
    "连续几天同一兴趣可以递进出新角度，不原样重发。"
    "不泄露私聊/群聊内容；记得自己发过≠别人看过或点赞过。\n"
    "现在是 {date} {time}，你正在：{activity}。"
)
#: 评论回复提示词（占位符：{moment} / {nickname} / {time} / {comment} / {impression}；
#: 人设经 LLMService with_identity 注入 system_prompt，{lover_name}/{personality}
#: 占位已移除，渲染方传空兼容旧定制模板）
QZONE_REPLY_DEFAULT = (
    "请写一条自然的回复（10~40 字，口语化，只输出回复正文）。\n"
    "你在 QQ 空间发了这条说说：\n「{moment}」\n"
    "好友「{nickname}」（{time}）评论了：「{comment}」\n{impression}"
)
#: 评论提醒主动 intent（占位符：{excerpt} / {added}）
QZONE_COMMENT_REMINDER_DEFAULT = "你刚发的说说「{excerpt}」收到了 {added} 条新评论，可以在聊天里自然地提到这件事"

#: 复核 P0-7：历史出厂默认模板特征集（6f3aa19/adce1d9 引入、a74c457/3efd0cc
#: 移除前的原文）。_merge_disk_values 把「磁盘值 != 新默认」一律当用户定制保留，
#: 致代码升级默认后存量用户永卡旧模板（渲染空身份）。磁盘值精确命中本集合 =
#: 旧出厂默认而非用户定制 → 不保留，让新默认生效；用户改过一个字就不命中。
LEGACY_FACTORY_PROMPT_VALUES = frozenset({
    ("请以第一人称发一条 QQ 空间说说，记录此刻的生活。"
     "要求：口语化、像真人发的、20~60 字、不要话题标签、不要引号、只输出说说正文。\n"
     "你是「{lover_name}」，人设：{personality}\n"
     "现在是 {date} {time}，你正在：{activity}。"),
    ("请写一条自然的回复（10~40 字，口语化，只输出回复正文）。\n"
     "你是「{lover_name}」，人设：{personality}\n"
     "你在 QQ 空间发了这条说说：\n「{moment}」\n"
     "好友「{nickname}」（{time}）评论了：「{comment}」\n{impression}"),
})
#: 发帖前 LLM 严判提示词（占位符：{lover_name} / {current_time} / {activity}；
#: 2026-10-14 §18.4 判定链移植自 MaiTrace v3.1：正反例 + 强烈压制「是」+ 首字符应答格式。
#: PR-AUDIT 3.16.1 PR-2：人设经 LLMService with_identity 注入 system_prompt，
#: {personality} 占位已移除（渲染方仍传值兼容旧定制模板，3.9.2 先例）；严判规则原文不动）
QZONE_POST_GATE_DEFAULT = (
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
    "第一个字符必须是「是」或「否」。\n\n"
    "你是「{lover_name}」，依据系统人设判断。\n"
    "现在是 {current_time}，你正在：{activity}。"
)


# ===========================================================================
# [prompts] 配置段（默认值 = 上述常量；集中暴露，便于深度定制）
# ===========================================================================


class PromptsSectionConfig(PluginConfigBase):
    """用户可定制的表达风格与日程追加要求。"""

    @model_validator(mode="before")
    @classmethod
    def _ignore_internal_overrides(cls, value):
        if isinstance(value, dict):
            return {k: v for k, v in value.items() if k in cls.model_fields}
        return value

    __ui_label__ = "提示词（高级）"
    __ui_icon__ = "message-square"
    __ui_order__ = 12
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Prompts (Advanced)",
               "description": "Customize wording and additional schedule preferences; internal protocols are maintained by the plugin."}
    }

    # ── 陪伴模块 ─────────────────────────────────────────────
    companion_miss_reason: str = Field(
        default=COMPANION_MISS_REASON_DEFAULT,
        description="想念触发时给 planner 的说明（占位符：{lover_name} {relation} {hours}）",
        json_schema_extra={"label": "想念·触发说明", "rows": 3,
                           **_ui_i18n("Miss trigger reason")},
    )
    companion_activity_reason: str = Field(
        default=COMPANION_ACTIVITY_REASON_DEFAULT,
        description="【已停用】旧日程巡检分享触发说明，现版本无消费方；保留字段仅为兼容存量配置（复核 P0-7 标注）",
        json_schema_extra={"label": "巡检·日程分享（已停用）", "rows": 2,
                           **_ui_i18n("Activity patrol reason")},
    )
    companion_daily_reason: str = Field(
        default=COMPANION_DAILY_REASON_DEFAULT,
        description="【已停用】旧日常搭话触发说明，现版本无消费方；保留字段仅为兼容存量配置（复核 P0-7 标注）",
        json_schema_extra={"label": "巡检·日常搭话（已停用）", "rows": 2,
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
    # tool_send_message 字段已随主动私信工具移除删除（2026-11-04 评估 F4；见上方常量区注释）

    # ── 日程模块 ─────────────────────────────────────────────
    schedule_generation_extra: str = Field(
        default=SCHEDULE_GENERATION_EXTRA_DEFAULT,
        description="日程生成·追加指令（留空 = 不追加；会原样附在生成提示词末尾）",
        json_schema_extra={"label": "日程·追加指令", "rows": 4, "placeholder": "（留空不追加）",
                           **_ui_i18n("Extra generation instruction")},
    )
    # ── spine 日意图（2026-10-25 滚动化改造新增；PR8 3.0.0 起为唯一生成路线）──
    # 2026-10-20 用户指示：活动切换/约定主动开口整条移除，原 proactive_intent_default 与
    # proactive_intent_study/work/meal/rest/exercise/entertainment/social/daily_routine/
    # learn_topic 及 proactive_intent_commitment、proactive_reason_activity/commitment
    # 这些 Field 已删除（旧配置由迁移器/宿主忽略）。
    proactive_intent_morning: str = Field(
        default=PROACTIVE_INTENT_MORNING_DEFAULT,
        description="早安 intent（纯问候导向；无占位符，{activity_name} 传参仍兼容但不使用）",
        json_schema_extra={"label": "主动·早安", "rows": 2, **_ui_i18n("Morning intent")},
    )
    proactive_intent_night: str = Field(
        default=PROACTIVE_INTENT_NIGHT_DEFAULT,
        description="晚安 intent（无占位符）",
        json_schema_extra={"label": "主动·晚安", "rows": 2, **_ui_i18n("Night intent")},
    )

    # ── 情绪模块 ─────────────────────────────────────────────
    emotion_night_hint: str = Field(
        default=EMOTION_NIGHT_HINT_DEFAULT,
        description="深夜冻结态追加行（无占位符；非冻结态不出现）",
        json_schema_extra={"label": "情绪·深夜引导行", "rows": 2,
                           **_ui_i18n("Night hint")},
    )

    # ── QQ 空间 ─────────────────────────────────────────────
    qzone_post: str = Field(
        default=QZONE_POST_DEFAULT,
        description="说说文案提示词（占位符：{lover_name} {personality} {date} {time} {activity}；身份另经 system 注入，占位渲染真实昵称/人设）",
        json_schema_extra={"label": "空间·说说文案", "rows": 4, **_ui_i18n("Qzone post prompt")},
    )
    qzone_reply: str = Field(
        default=QZONE_REPLY_DEFAULT,
        description="评论回复提示词（占位符：{lover_name} {personality} {moment} {nickname} "
                    "{time} {comment} {impression}；身份另经 system 注入，占位渲染真实昵称/人设）",
        json_schema_extra={"label": "空间·评论回复", "rows": 6, **_ui_i18n("Qzone reply prompt")},
    )
    qzone_comment_reminder: str = Field(
        default=QZONE_COMMENT_REMINDER_DEFAULT,
        description="新评论提醒的主动 intent（占位符：{excerpt} {added}）",
        json_schema_extra={"label": "空间·评论提醒", "rows": 2,
                           **_ui_i18n("Qzone comment reminder")},
    )


# ---------------------------------------------------------------------------
# 读取入口：配置值 or 出厂默认
# ---------------------------------------------------------------------------

#: 字段名 → 出厂默认（:func:`prompt` 的回退表；新增字段时同步登记）
PROMPT_FALLBACKS: Dict[str, str] = {
    'companion_miss_reason': COMPANION_MISS_REASON_DEFAULT,
    'companion_miss_confirm': COMPANION_MISS_CONFIRM_DEFAULT,
    'companion_activity_reason': COMPANION_ACTIVITY_REASON_DEFAULT,
    'companion_daily_reason': COMPANION_DAILY_REASON_DEFAULT,
    'screen_describe': SCREEN_DESCRIBE_DEFAULT,
    'screen_narration': SCREEN_NARRATION_DEFAULT,
    'screen_narration_offline': SCREEN_NARRATION_OFFLINE_DEFAULT,
    'screen_narration_failed': SCREEN_NARRATION_FAILED_DEFAULT,
    'inject_affection_block': INJECT_AFFECTION_BLOCK_DEFAULT,
    'inject_proactive_turn_rule': INJECT_PROACTIVE_TURN_RULE_DEFAULT,
    'affection_conversation_review': AFFECTION_CONVERSATION_REVIEW_DEFAULT,
    'affection_daily_review': AFFECTION_DAILY_REVIEW_DEFAULT,
    'inject_schedule_header': INJECT_SCHEDULE_HEADER_DEFAULT,
    'inject_schedule_current_line': INJECT_SCHEDULE_CURRENT_LINE_DEFAULT,
    'inject_schedule_future_title': INJECT_SCHEDULE_FUTURE_TITLE_DEFAULT,
    'inject_schedule_future_line': INJECT_SCHEDULE_FUTURE_LINE_DEFAULT,
    'inject_hint_future': INJECT_HINT_FUTURE_DEFAULT,
    'inject_hint_current': INJECT_HINT_CURRENT_DEFAULT,
    'inject_hint_continue': INJECT_HINT_CONTINUE_DEFAULT,
    'inject_hint_proactive': INJECT_HINT_PROACTIVE_DEFAULT,
    'inject_hint_idle': INJECT_HINT_IDLE_DEFAULT,
    'inject_no_activity': INJECT_NO_ACTIVITY_DEFAULT,
    'inject_replyer_status': INJECT_REPLYER_STATUS_DEFAULT,
    'inject_replyer_state_line': INJECT_REPLYER_STATE_LINE_DEFAULT,
    'inject_replyer_next_line': INJECT_REPLYER_NEXT_LINE_DEFAULT,
    'inject_date_header': INJECT_DATE_HEADER_DEFAULT,
    'schedule_emotion_context': SCHEDULE_EMOTION_CONTEXT_DEFAULT,
    'schedule_low_mood_note': SCHEDULE_LOW_MOOD_NOTE_DEFAULT,
    'schedule_holiday_context': SCHEDULE_HOLIDAY_CONTEXT_DEFAULT,
    'schedule_generation_extra': SCHEDULE_GENERATION_EXTRA_DEFAULT,
    'schedule_retry_feedback': SCHEDULE_RETRY_FEEDBACK_DEFAULT,
    'schedule_spine_task': SCHEDULE_SPINE_TASK_DEFAULT,
    'schedule_spine_commitment_note': SCHEDULE_SPINE_COMMITMENT_NOTE_DEFAULT,
    'schedule_spine_sleep_line': SCHEDULE_SPINE_SLEEP_LINE_DEFAULT,
    'schedule_spine_no_sleep_line': SCHEDULE_SPINE_NO_SLEEP_LINE_DEFAULT,
    'schedule_spine_described_note': SCHEDULE_SPINE_DESCRIBED_NOTE_DEFAULT,
    'schedule_spine_no_described_note': SCHEDULE_SPINE_NO_DESCRIBED_NOTE_DEFAULT,
    'schedule_role_judge_no_activity': SCHEDULE_ROLE_JUDGE_NO_ACTIVITY_DEFAULT,
    'schedule_role_judge_no_persona': SCHEDULE_ROLE_JUDGE_NO_PERSONA_DEFAULT,
    'schedule_role_judge': SCHEDULE_ROLE_JUDGE_DEFAULT,
    'proactive_intent_morning': PROACTIVE_INTENT_MORNING_DEFAULT,
    'proactive_intent_morning_no_activity': PROACTIVE_INTENT_MORNING_NO_ACTIVITY_DEFAULT,
    'proactive_intent_night': PROACTIVE_INTENT_NIGHT_DEFAULT,
    'proactive_reason_morning': PROACTIVE_REASON_MORNING_DEFAULT,
    'proactive_reason_night': PROACTIVE_REASON_NIGHT_DEFAULT,
    'emotion_sleep_referee': EMOTION_SLEEP_REFEREE_DEFAULT,
    'emotion_init_system': EMOTION_INIT_SYSTEM_DEFAULT,
    'emotion_evaluate': EMOTION_EVALUATE_DEFAULT,
    'emotion_injection': EMOTION_INJECTION_DEFAULT,
    'emotion_night_hint': EMOTION_NIGHT_HINT_DEFAULT,
    'qzone_post': QZONE_POST_DEFAULT,
    'qzone_reply': QZONE_REPLY_DEFAULT,
    'qzone_comment_reminder': QZONE_COMMENT_REMINDER_DEFAULT,
    'qzone_post_gate': QZONE_POST_GATE_DEFAULT,
}

INTERNAL_PROMPT_NAMES = frozenset(PROMPT_FALLBACKS) - frozenset(PromptsSectionConfig.model_fields)


def prompt(plugin: Any, name: str) -> str:
    """读一条提示词：``[prompts]`` 配置值优先，缺失/为空/取不到时回退出厂默认。

    Args:
        plugin: 插件实例（``plugin.config.prompts.<name>``）或配置对象本身。
        name: ``[prompts]`` 段字段名（如 ``companion_miss_reason``）。

    任何异常都不外抛（热重载窗口/骨架配置/测试桩都安全）：宁可回退默认文案，
    也不让提示词读取失败打断业务链路。
    """
    default = PROMPT_FALLBACKS.get(name, "")
    if name in INTERNAL_PROMPT_NAMES:
        return default
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


#: 已告警过「渲染失败回退」的模板文本（进程内去重：同一坏模板至多一条，防刷日志）
_RENDER_FALLBACK_WARNED: Set[str] = set()


def render(template: str, *, fallback: str, **values: Any) -> str:
    """渲染提示词模板；占位符写坏（KeyError/IndexError/ValueError）时回退默认模板。

    只做 ``str.format``，不做其它转义处理——模板里的字面花括号请写 ``{{`` / ``}}``
    （出厂默认已按此书写）。

    PR-AUDIT（3.16.1 PR-2）：回退时留一条 warning 诊断（按模板文本进程内去重），
    自定义模板占位符失效可观测；返回文本契约不变（回退默认模板）。
    """
    try:
        return str(template).format(**values)
    except Exception as exc:
        text = str(template)
        if text not in _RENDER_FALLBACK_WARNED:
            _RENDER_FALLBACK_WARNED.add(text)
            logger.warning("提示词模板占位符渲染失败（%s: %s），回退出厂默认模板；请检查 [prompts] 自定义模板",
                           type(exc).__name__, exc)
        try:
            return str(fallback).format(**values)
        except Exception:
            return str(fallback)

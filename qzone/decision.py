"""QQ 空间发帖判定链的纯逻辑部分。

用途：把自由文本日程归类成 :class:`ActivityType` 枚举（:func:`classify_activity`）、
最严解析 LLM 的「是/否」回答（:func:`parse_llm_decision`）、归一化活动黑名单
（:func:`normalize_blocked_activities`），并用 :class:`DecisionHistory` 以带 maxlen 的
deque 留下每次判定的可观测痕迹（供调试命令打印）。本模块**不发起任何网络/LLM 调用**，
全部依赖仅 stdlib。

判定链移植自 MaiTrace v3.1（MIT），规则只认枚举、不认自由文本。

口径要点（与移植源一致）：
- :data:`TYPE_MAP` 是「关键词 → 枚举」的有序字典，**dict 顺序即优先级**，子串命中即返回；
- 归类失败一律落 ``ActivityType.OTHER``（``OTHER`` 不在任何黑名单里，由上层 LLM 闸门兜底）；
- LLM 输出只认首字符，格式异常一律按「否」处理（保守拒绝）；
- 每次决策（无论在哪道闸被拒）都写入历史 deque，自动行为必须留痕。
"""

from __future__ import annotations

import collections
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

__all__ = [
    "ActivityType",
    "TYPE_MAP",
    "classify_activity",
    "parse_llm_decision",
    "normalize_blocked_activities",
    "DecisionHistory",
]


class ActivityType(str, Enum):
    """日程活动大类（判定链只认这些枚举值，不认自由文本）。"""

    SLEEPING = "sleeping"
    WAKING_UP = "waking_up"
    EATING = "eating"
    WORKING = "working"
    STUDYING = "studying"
    EXERCISING = "exercising"
    RELAXING = "relaxing"
    SOCIALIZING = "socializing"
    COMMUTING = "commuting"
    HOBBY = "hobby"
    SELF_CARE = "self_care"
    OTHER = "other"


#: 关键词 → 活动枚举。**dict 顺序即优先级**：:func:`classify_activity` 顺序做子串匹配，命中即返回。
#: 前半段为 MaiTrace v3.1 原表（中英关键词打底），后半段为本仓补充的常见中文词。
TYPE_MAP: Dict[str, ActivityType] = {
    # —— MaiTrace v3.1 原表（中英关键词） ——
    "work": ActivityType.WORKING,
    "study": ActivityType.STUDYING,
    "sleep": ActivityType.SLEEPING,
    "meal": ActivityType.EATING,
    "工作": ActivityType.WORKING,
    "会议": ActivityType.WORKING,
    "办公": ActivityType.WORKING,
    "学习": ActivityType.STUDYING,
    "阅读": ActivityType.STUDYING,
    "看书": ActivityType.STUDYING,
    "运动": ActivityType.EXERCISING,
    "健身": ActivityType.EXERCISING,
    "散步": ActivityType.EXERCISING,
    "吃": ActivityType.EATING,
    "餐": ActivityType.EATING,
    "料理": ActivityType.EATING,
    "休息": ActivityType.RELAXING,
    "放松": ActivityType.RELAXING,
    "泡澡": ActivityType.RELAXING,
    "睡": ActivityType.SLEEPING,
    "梦": ActivityType.SLEEPING,
    "午休": ActivityType.SLEEPING,
    "小憩": ActivityType.SLEEPING,
    "通勤": ActivityType.COMMUTING,
    "赶路": ActivityType.COMMUTING,
    "出行": ActivityType.COMMUTING,
    "起床": ActivityType.WAKING_UP,
    "醒": ActivityType.WAKING_UP,
    "聊天": ActivityType.SOCIALIZING,
    "社交": ActivityType.SOCIALIZING,
    "化妆": ActivityType.SELF_CARE,
    "打扮": ActivityType.SELF_CARE,
    "hobby": ActivityType.HOBBY,
    # —— 本仓补充：常见中文词（同组多字精确词与单字泛词同型，互不抢占） ——
    "写代码": ActivityType.WORKING,
    "编程": ActivityType.WORKING,
    "开会": ActivityType.WORKING,
    "读论文": ActivityType.STUDYING,
    "上课": ActivityType.STUDYING,
    "自习": ActivityType.STUDYING,
    "跑步": ActivityType.EXERCISING,
    "锻炼": ActivityType.EXERCISING,
    "瑜伽": ActivityType.EXERCISING,
    "早饭": ActivityType.EATING,
    "午饭": ActivityType.EATING,
    "晚饭": ActivityType.EATING,
    "吃饭": ActivityType.EATING,
    "用餐": ActivityType.EATING,
    "午睡": ActivityType.SLEEPING,
    "打盹": ActivityType.SLEEPING,
    "睡觉": ActivityType.SLEEPING,
    "醒来": ActivityType.WAKING_UP,
    "洗漱": ActivityType.WAKING_UP,
    "闲逛": ActivityType.RELAXING,
    "发呆": ActivityType.RELAXING,
    "聚会": ActivityType.SOCIALIZING,
    "视频通话": ActivityType.SOCIALIZING,
    "地铁": ActivityType.COMMUTING,
    "公交": ActivityType.COMMUTING,
    "游戏": ActivityType.HOBBY,
    "追剧": ActivityType.HOBBY,
    "看电影": ActivityType.HOBBY,
    "听歌": ActivityType.HOBBY,
    "爱好": ActivityType.HOBBY,
    "护肤": ActivityType.SELF_CARE,
    "洗澡": ActivityType.SELF_CARE,
}


def classify_activity(goal_type: Any, description: Any) -> ActivityType:
    """把日程的自由文本归类成 :class:`ActivityType`。

    拼接 ``goal_type + " " + description`` 并小写化后，按 :data:`TYPE_MAP` 的顺序做
    子串匹配，命中即返回；未命中落 ``ActivityType.OTHER``。任何入参都不抛异常
    （``None`` 与非字符串经 ``str()`` 兜底）。
    """
    combined = (str(goal_type or "") + " " + str(description or "")).lower()
    for keyword, activity_type in TYPE_MAP.items():
        if keyword in combined:
            return activity_type
    return ActivityType.OTHER


def parse_llm_decision(answer: Any) -> Tuple[bool, str]:
    """解析 LLM 的「是/否|理由」回答，返回 ``(是否同意, 理由)``。

    最严解析（移植自 MaiTrace）：先去空白与首尾的 `` ` ' " `` 引号；空串 → ``(False, "")``；
    只认首个字符——``是`` → ``True``，``否`` → ``False``，其余一律 ``False``（保守拒绝）。
    理由 = ``|`` 或全角 ``｜`` 之后的部分（截 80 字），无分隔符时为空串；
    格式异常时理由以 ``格式异常: `` 开头（截 30 字）。任何入参都不抛异常。
    """
    clean = str(answer or "").strip().strip("`'\"").strip()
    if not clean:
        return False, ""
    reason = ""
    if "|" in clean:
        _, _, reason = clean.partition("|")
    elif "｜" in clean:
        _, _, reason = clean.partition("｜")
    reason = reason[:80]
    first = clean[0]
    if first == "是":
        return True, reason
    if first == "否":
        return False, reason
    return False, f"格式异常: {clean[:30]}"


def normalize_blocked_activities(raw: Any) -> Set[str]:
    """把活动黑名单配置归一化成小写字符串集合。

    接受 ``list`` / ``tuple`` / ``set``（逐项 ``str().strip().lower()``，忽略空串与非字符串）
    或逗号分隔字符串（按 ``,`` 拆分，同样忽略空段）。其余形态返回空集。
    """
    result: Set[str] = set()
    if isinstance(raw, (list, tuple, set)):
        for item in raw:
            if not isinstance(item, str):
                continue
            value = item.strip().lower()
            if value:
                result.add(value)
        return result
    if isinstance(raw, str):
        for part in raw.split(","):
            value = part.strip().lower()
            if value:
                result.add(value)
    return result


class DecisionHistory:
    """发帖判定历史（带 maxlen 的 deque，最新在前）。

    每次决策——无论在哪道闸被拒——都应 ``add`` 一条，供调试命令打印，
    否则「为什么没发」无从排查。
    """

    def __init__(self, maxlen: int = 20) -> None:
        """``maxlen`` 为历史容量，至少 1（非法值回退 20）。"""
        try:
            length = int(maxlen)
        except (TypeError, ValueError):
            length = 20
        self._maxlen = max(1, length)
        self._entries: "collections.deque[Dict[str, Any]]" = collections.deque(maxlen=self._maxlen)

    def add(
        self,
        *,
        action: str,
        activity_type: str = "",
        gate: str = "",
        reason: str = "",
        llm_answer: str = "",
        result: str = "",
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """追加一条判定记录（最新在前），返回该条 dict。

        字段：``time``（``now`` 或当前时刻的 ``%H:%M:%S``）、``action``、``activity_type``、
        ``gate``、``reason``（截 200）、``llm_answer``（截 80）、``result``（截 80）。
        """
        timestamp = (datetime.now() if now is None else now).strftime("%H:%M:%S")
        entry: Dict[str, Any] = {
            "time": timestamp,
            "action": str(action),
            "activity_type": str(activity_type),
            "gate": str(gate),
            "reason": str(reason)[:200],
            "llm_answer": str(llm_answer)[:80],
            "result": str(result)[:80],
        }
        self._entries.appendleft(entry)
        return entry

    def entries(self) -> List[Dict[str, Any]]:
        """全部历史（最新在前）的浅拷贝列表。"""
        return list(self._entries)

    def last(self) -> Optional[Dict[str, Any]]:
        """最近一条记录；无记录返回 ``None``。"""
        return self._entries[0] if self._entries else None

    def format_text(self, limit: int = 10) -> str:
        """调试文本：每行 ``[HH:MM:SS] action｜闸=..｜活动=..｜原因｜LLM=..｜结果=..``。

        空字段段整体省略；最多 ``limit`` 条、最新在前；空历史返回 ``（暂无发帖判定记录）``。
        """
        if not self._entries:
            return "（暂无发帖判定记录）"
        try:
            count = max(0, int(limit))
        except (TypeError, ValueError):
            count = 10
        lines: List[str] = []
        for entry in list(self._entries)[:count]:
            segments: List[str] = [f"[{entry['time']}] {entry['action']}".rstrip()]
            gate = str(entry.get("gate") or "")
            activity_type = str(entry.get("activity_type") or "")
            reason = str(entry.get("reason") or "")
            llm_answer = str(entry.get("llm_answer") or "")
            result = str(entry.get("result") or "")
            if gate:
                segments.append(f"闸={gate}")
            if activity_type:
                segments.append(f"活动={activity_type}")
            if reason:
                segments.append(reason)
            if llm_answer:
                segments.append(f"LLM={llm_answer}")
            if result:
                segments.append(f"结果={result}")
            lines.append("｜".join(segments))
        return "\n".join(lines)

"""QQ 空间发帖判定链的纯逻辑部分（2026-10-20 精简）。

用途：最严解析 LLM 的「是/否」回答（:func:`parse_llm_decision`），并用
:class:`DecisionHistory` 以带 maxlen 的 deque 留下每次判定的可观测痕迹（供调试命令
打印）。本模块**不发起任何网络/LLM 调用**，全部依赖仅 stdlib。

判定链骨架移植自 MaiTrace v3.1（MIT）；**2026-10-20 用户指示后不再按活动类型拦**：
原 ``ActivityType`` 枚举、``TYPE_MAP`` 关键词归类（``classify_activity``）与活动黑名单
（``normalize_blocked_activities``）随之移除——「是否允许发空间」的时段维度只看静默
（恒复用 `[schedule]` 作息；开启 `[patrol].nap_quiet_enabled` 时叠加午休静默），
内容质量由 LLM 严判把关（见 qzone/service.py 判定链）。

口径要点：
- LLM 输出只认首字符，格式异常一律按「否」处理（保守拒绝）；
- 每次决策（无论在哪道闸被拒）都写入历史 deque，自动行为必须留痕。
"""

from __future__ import annotations

import collections
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "parse_llm_decision",
    "DecisionHistory",
]


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

        字段：``time``（``now`` 或当前时刻的 ``%H:%M:%S``）、``action``、``activity_type``
        （2026-10-20 起新记录不再填充，字段保留以兼容历史记录展示）、``gate``、
        ``reason``（截 200）、``llm_answer``（截 80）、``result``（截 80）。
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

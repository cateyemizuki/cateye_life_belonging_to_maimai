"""LLM 消耗统计（F3，2026-10-11）：按模型 / 按模块记录最近 24 小时的 token 消耗。

- **数据源** = ``LLMService`` 唯一出口：宿主 LLM 能力返回载荷自带的
  ``model_name / prompt_tokens / completion_tokens / total_tokens``
  （旧宿主或测试桩没有这些字段 → 记 0 tokens、只计调用次数，面板据此提示）。
- **落盘** = ``<data_dir>/llm_usage_events.jsonl``：每次调用一行
  ``{"ts", "event", "model", "tokens"}``；写入时惰性裁剪超过保留期的旧行；
  JSONL 单行追加、损坏行跳过（统计类数据，不要求事务性）。
- **模块归属** = ``event`` 名静态映射（各调用点的 ``event=`` 全仓唯一）。
- **无配置项**（用户口径）；跨热重载连续（落盘），满足 §9.2「计数必须落盘」教训。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: 调用点 event → 模块（新增 LLM 调用点时同步登记；未知 event 归「其他」）
EVENT_MODULE_MAP: Dict[str, str] = {
    "schedule_generation": "planning",
    "schedule_spine": "planning",  # 2026-10-25 滚动化改造新增：spine 日意图生成（模块 planning）
    "role_judge": "planning",
    "miss_confirm": "companion",
    "affection_review": "companion",
    "affection_daily_review": "companion",
    "cateye_screen_describe": "companion",
    "emotion_sleep_referee": "emotion",
    "emotion_init": "emotion",
    "emotion_evaluate": "emotion",
    "qzone_post": "qzone",
    "qzone_reply": "qzone",
    "qzone_post_gate": "qzone",
}
#: 模块展示顺序（面板按此排序；映射外的事件归「其他」）
MODULE_ORDER = ("planning", "companion", "emotion", "qzone", "其他")

#: 账本保留期（小时）：面板只看 24h，多留一天缓冲
KEEP_HOURS = 48


def module_of_event(event: Any) -> str:
    """调用点 event → 模块名（未知 event 归「其他」，不丢数）。"""
    return EVENT_MODULE_MAP.get(str(event or ""), "其他")


class LLMUsageTracker:
    """LLM 消耗账本（JSONL 追加 + 惰性裁剪 + 按窗口聚合）。"""

    def __init__(self, data_dir: Any, *, keep_hours: int = KEEP_HOURS) -> None:
        self._path: Optional[Path] = Path(data_dir) / "llm_usage_events.jsonl" if data_dir else None
        self._keep_hours = max(1, int(keep_hours))

    # ---- 记账 ----
    def record(self, *, event: Any, model: Any = "",
               prompt_tokens: Any = 0, completion_tokens: Any = 0,
               total_tokens: Any = 0) -> None:
        """记一次调用（tokens 为 0 也记调用次数；异常一律吞掉，绝不打断业务链路）。"""
        if self._path is None:
            return
        try:
            total = int(total_tokens or 0)
        except (TypeError, ValueError):
            try:
                total = int(prompt_tokens or 0) + int(completion_tokens or 0)
            except (TypeError, ValueError):
                total = 0
        entry = {"ts": datetime.now().isoformat(timespec="seconds"),
                 "event": str(event or ""), "model": str(model or ""), "tokens": total}
        try:
            self._prune()
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            logger.debug("LLM 消耗记账写盘失败（忽略）", exc_info=True)

    def _prune(self) -> None:
        """惰性裁剪：全行扫描保留期内的行，无陈旧行则不重写（账本体量受保留期约束，开销可忽略）。"""
        path = self._path
        if path is None or not path.exists():
            return
        cutoff = datetime.now() - timedelta(hours=self._keep_hours)
        kept: List[str] = []
        changed = False
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        changed = True          # 损坏行顺手清掉
                        continue
                    ts = str((entry or {}).get("ts") or "")
                    try:
                        fresh = bool(ts) and datetime.fromisoformat(ts) >= cutoff
                    except ValueError:
                        fresh = False
                    if fresh:
                        kept.append(line)
                    else:
                        changed = True
            if not changed:
                return
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            logger.debug("LLM 消耗账本裁剪失败（忽略）", exc_info=True)

    # ---- 聚合 ----
    def summary(self, hours: int = 24) -> Dict[str, Any]:
        """聚合最近 ``hours`` 小时：调用次数 / token 总量 / 按模型 / 按模块。

        返回 ``{"calls", "tokens", "models", "modules", "has_usage"}``；
        ``has_usage`` = 宿主是否回传过非零 token（面板据此区分"无调用"与
        "有调用但宿主未返回用量"）。文件缺失/损坏 → 空账本（不抛异常）。
        """
        window_start = datetime.now() - timedelta(hours=max(1, int(hours)))
        calls = 0
        tokens = 0
        models: Dict[str, int] = {}
        modules: Dict[str, int] = {}
        if self._path is not None and self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts = str((entry or {}).get("ts") or "")
                        try:
                            if ts and datetime.fromisoformat(ts) < window_start:
                                continue
                        except ValueError:
                            continue
                        calls += 1
                        count = int((entry or {}).get("tokens") or 0)
                        tokens += count
                        model = str((entry or {}).get("model") or "")
                        module = module_of_event((entry or {}).get("event"))
                        if model:
                            models[model] = models.get(model, 0) + count
                        modules[module] = modules.get(module, 0) + count
            except OSError:
                logger.debug("LLM 消耗账本读取失败（忽略）", exc_info=True)
        return {"calls": calls, "tokens": tokens, "models": models,
                "modules": modules, "has_usage": tokens > 0}

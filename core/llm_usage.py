"""LLM 消耗统计（F3，2026-10-11）：按模型 / 按模块记录最近 24 小时的 token 消耗。

- **数据源** = ``LLMService`` 唯一出口：宿主 LLM 能力返回载荷自带的
  ``model_name / prompt_tokens / completion_tokens / total_tokens``
  （旧宿主或测试桩没有这些字段 → 记 0 tokens、只计调用次数，面板据此提示）。
- **落盘** = ``<data_dir>/llm_usage_events.jsonl``：每次调用一行
  ``{"ts", "event", "model", "tokens"}``（2026-10-29 口径轮起新增 ``success`` /
  ``usage_present`` 两个布尔；旧行无新字段，聚合时归 legacy 桶，不回填不改旧行）；
  写入时惰性裁剪超过保留期的旧行；JSONL 单行追加、损坏行跳过（统计类数据，不要求事务性）。
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
    "schedule_events": "planning",  # 2026-10-27 PR4 共享接线：事件库生成（唯一外部新任务名）
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

#: 四桶键名（§2.2 模型消耗口径）：聚合结果按此并列输出。
#: 正常回传 = 宿主标明来源且用量在场；成功但无用量；失败；legacy = 无新字段的旧行。
BUCKET_HOST_REPORTED = "host_reported"
BUCKET_SUCCESS_NO_USAGE = "success_no_usage"
BUCKET_FAILED = "failed"
BUCKET_LEGACY = "legacy"
USAGE_BUCKETS = (BUCKET_HOST_REPORTED, BUCKET_SUCCESS_NO_USAGE, BUCKET_FAILED, BUCKET_LEGACY)


def empty_buckets() -> Dict[str, Dict[str, int]]:
    """四桶空骨架（聚合面统一形状；值全 0）。"""
    return {name: {"calls": 0, "tokens": 0} for name in USAGE_BUCKETS}


def usage_bucket_of(entry: Any) -> str:
    """单条账本行 → 四桶键名（判定优先级：legacy > 失败 > 正常回传 > 成功但无用量）。

    - **legacy**：无 ``success``/``usage_present`` 新字段的旧行（不回填不猜测）；
    - **失败**：``success`` 非真即失败桶——失败响应即使带 0 值 usage 也归失败，
      成败不由 usage 推断（2026-10-29 口径修正的根因即在此）；
    - **正常回传**：``usage_source == "host_reported"`` 且 ``usage_present`` 为真；
    - 其余（成功但宿主未回传可识别用量字段）→ **成功但无用量**。
    """
    if not isinstance(entry, dict):
        return BUCKET_LEGACY
    if "success" not in entry and "usage_present" not in entry:
        return BUCKET_LEGACY
    if entry.get("success") is not True:
        return BUCKET_FAILED
    if entry.get("usage_source") == "host_reported" and entry.get("usage_present") is True:
        return BUCKET_HOST_REPORTED
    return BUCKET_SUCCESS_NO_USAGE


def missing_usage_source(entry: Any) -> bool:
    """「缺用量来源调用」口径（§2.2 改名，替代旧「缺少来源」）：
    ``usage_source != host_reported`` **或** ``usage_present`` 不为真。
    旧行无 ``usage_present`` 字段 → 一律计入（与报告「该指标实际主要覆盖旧记录」一致）。"""
    if not isinstance(entry, dict):
        return True
    return entry.get("usage_source") != "host_reported" or entry.get("usage_present") is not True


def reported_token_count(entry: Any) -> int:
    """Only retain counts explicitly attributed to a host usage response."""
    if not isinstance(entry, dict) or entry.get("usage_source") != "host_reported":
        return 0
    try:
        return max(0, int(entry.get("tokens") or 0))
    except (TypeError, ValueError, OverflowError):
        return 0

USAGE_TERMS = {
    "calls": {"label": "调用次数", "description": "收到宿主响应的请求数，包含失败响应；未收到响应的本地异常不计入。"},
    "tokens": {"label": "Token 消耗", "description": "宿主回传的文本处理量；缺失时只计调用，不推算。"},
    "missing_usage_source_calls": {"label": "缺用量来源调用", "description": "缺少完整宿主用量或新记录标记的调用，可与失败调用重叠。"},
    "host_reported": {"label": "正常回传", "description": "宿主返回成功响应及可识别的完整用量。"},
    "success_no_usage": {"label": "成功但无用量", "description": "success=true，但宿主未回传完整usage。"},
    "failed": {"label": "失败调用", "description": "宿主响应的success不为true。"},
    "legacy": {"label": "旧记录", "description": "升级前缺少success和usage_present标记的记录，保留原始记录不补写。"},
}

def module_of_event(event: Any) -> str:
    """调用点 event → 模块名（未知 event 归「其他」，不丢数）。"""
    return EVENT_MODULE_MAP.get(str(event or ""), "其他")


class LLMUsageTracker:
    """LLM 用量记录（JSONL 追加 + 惰性裁剪 + 按窗口聚合）。"""

    def __init__(self, data_dir: Any, *, keep_hours: int = KEEP_HOURS) -> None:
        self._path: Optional[Path] = Path(data_dir) / "llm_usage_events.jsonl" if data_dir else None
        self._keep_hours = max(1, int(keep_hours))

    # ---- 记账 ----
    def record(self, *, event: Any, model: Any = "",
               prompt_tokens: Any = 0, completion_tokens: Any = 0,
               total_tokens: Any = 0, request_id: Any = "",
               usage_available: bool = False,
               success: bool = False, usage_present: bool = False) -> None:
        """记一次调用（tokens 为 0 也记调用次数；异常一律吞掉，绝不打断业务链路）。

        2026-10-29 新增两个布尔（只加字段不改既有字段语义）：
        - ``success``：请求成败，来自调用出口（``_result_from_response``/``_interpret``）
          的实际结果——拿到非空正文为真；宿主失败/空正文/异常均假。成败不由 usage 推断。
        - ``usage_present``：宿主响应里是否存在可识别的用量字段（含 0 值）；
          与 ``usage_available``（total 可得，预算层口径）分离。
        """
        if self._path is None:
            return
        # Missing usage is a missing observation, never a synthetic token count.
        try:
            total = max(0, int(total_tokens)) if usage_available and total_tokens is not None else 0
            if usage_available and total <= 0 and prompt_tokens is not None and completion_tokens is not None:
                total = max(0, int(prompt_tokens)) + max(0, int(completion_tokens))
        except (TypeError, ValueError, OverflowError):
            total = 0
        entry = {"ts": datetime.now().isoformat(timespec="seconds"),
                 "event": str(event or ""), "model": str(model or ""), "tokens": max(0,total),
                 "request_id": str(request_id or "")[:128],
                 "usage_source": "host_reported" if usage_available else "unavailable",
                 "success": bool(success),
                 "usage_present": bool(usage_present)}
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
            logger.debug("LLM 用量记录裁剪失败（忽略）", exc_info=True)

    # ---- 聚合 ----
    def summary(self, hours: int = 24) -> Dict[str, Any]:
        """聚合最近 ``hours`` 小时：调用次数 / token 总量 / 按模型 / 按模块 / 四桶。

        返回 ``{"calls", "tokens", "models", "modules", "has_usage", "buckets",
        "missing_usage_source_calls"}``；
        ``has_usage`` = 宿主是否回传过非零 token（面板据此区分"无调用"与
        "有调用但宿主未返回用量"）；
        ``buckets`` = 四桶拆分（正常回传 / 成功但无用量 / 失败 / legacy，各含
        ``calls``/``tokens``，与既有键并列输出）；
        ``missing_usage_source_calls`` = 缺用量来源调用次数（口径见
        :func:`missing_usage_source`，旧「缺少来源」改名后的新键名）。
        文件缺失/损坏 → 空账本（不抛异常）。
        """
        window_start = datetime.now() - timedelta(hours=max(1, int(hours)))
        calls = 0
        tokens = 0
        missing_calls = 0
        buckets = empty_buckets()
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
                        count = reported_token_count(entry)
                        tokens += count
                        if missing_usage_source(entry):
                            missing_calls += 1
                        bucket = usage_bucket_of(entry)
                        buckets[bucket]["calls"] += 1
                        buckets[bucket]["tokens"] += count
                        model = str((entry or {}).get("model") or "")
                        module = module_of_event((entry or {}).get("event"))
                        if model:
                            models[model] = models.get(model, 0) + count
                        modules[module] = modules.get(module, 0) + count
            except OSError:
                logger.debug("LLM 用量记录读取失败（忽略）", exc_info=True)
        return {"calls": calls, "tokens": tokens, "models": models,
                "modules": modules, "has_usage": tokens > 0, "terms": USAGE_TERMS,
                "buckets": buckets, "missing_usage_source_calls": missing_calls}

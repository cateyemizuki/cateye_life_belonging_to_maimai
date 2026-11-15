"""LLM 消耗统计（F3，2026-10-11；2026-11-02 账本 v2 与 30 天保留）。

- **数据源** = ``LLMService`` 唯一出口：宿主 LLM 能力返回载荷自带的
  ``model_name / prompt_tokens / completion_tokens / total_tokens``
  （旧宿主或测试桩没有这些字段 → 记 0 tokens、只计调用次数，面板据此提示）。
- **落盘** = ``<data_dir>/llm_usage_events.jsonl``：每次调用一行。
  v2 行（``schema=2``）额外记录子模块、任务名、输入/输出拆分、缓存命中/未命中、
  耗时、尝试次数与 ``abandoned`` 标记；旧行无新字段，读取端按 legacy 桶处理，
  不回填不改写。
- **保留期** = ``KEEP_HOURS``（默认 720 = 30 天，硬上限）。裁剪是惰性的：仅在
  「跨日后的首次写入」或「累计写入超过 1500 行」时全量扫描一次，避免每次记账
  都重写账本。
- **模块归属** = v2 行直接用 ``module`` 字段；旧行回退 ``event`` 映射
  （:func:`module_of_event`），未知 event 归「其他」。
- **无配置项**（用户口径）；跨热重载连续（落盘），满足 §9.2「计数必须落盘」教训。
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
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
    "qzone_video_decision": "qzone",  # 2026-11-07 PR-A0：空间视频候选影子判定（发布面归 qzone）
    "qzone_video_caption": "qzone",  # 2026-11-07 PR-A2：空间视频说说独立文案（发布面归 qzone）
    "video_watch_visual": "video",
    "video_watch_description": "video",
    "video_watch_impression": "video",
}
#: 模块展示顺序（面板按此排序；映射外的事件归「其他」，未标注的 v2 行归「unknown」）
MODULE_ORDER = ("planning", "companion", "emotion", "qzone", "video", "core", "其他", "unknown")
#: 子模块枚举（调用方必须使用这些值；缺失由 Gateway 记为 unknown）
MODULE_VALUES = ("planning", "emotion", "qzone", "companion", "video", "core")
MODULE_UNKNOWN = "unknown"

#: 账本保留期（小时）：30 天硬上限，面板三档窗口都落在保留期内
KEEP_HOURS = 720
#: 允许显示的窗口（小时）：24 小时 / 7 天 / 30 天
USAGE_WINDOWS = (24, 168, 720)
#: 默认显示窗口（30 天）
DEFAULT_USAGE_WINDOW_HOURS = 720

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


def _optional_count(value: Any) -> Optional[int]:
    """非负整数或 None；bool/非法值一律 None（不把 False 当 0 参与统计）。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_module(module: Any) -> str:
    """把调用方给的模块名收敛到枚举；缺失/非法返回 ``unknown``。"""
    value = str(module or "").strip().lower()
    return value if value in MODULE_VALUES else MODULE_UNKNOWN


def entry_module(entry: Any) -> str:
    """单条账本行的模块：v2 行读 ``module``，旧行回退 event 映射。"""
    if not isinstance(entry, dict):
        return "其他"
    value = str(entry.get("module") or "").strip()
    if value:
        return value
    return module_of_event(entry.get("event"))


USAGE_TERMS = {
    # F1 口径对齐（2026-11-04）：Gateway 异常路径（超时/断连/取消/回退首尝异常）
    # 补记 abandoned/degraded 行后，calls 同时包含这些「已发出未收到响应」的调用。
    "calls": {"label": "调用次数", "description": "收到宿主响应的请求数（含失败响应），加上已发出但因本地异常（超时/断连/取消）未收到响应的已放弃调用；后者只计次数，不推算 token。"},
    "tokens": {"label": "Token 消耗", "description": "宿主回传的文本处理量；缺失时只计调用，不推算。"},
    "prompt_tokens": {"label": "输入 Token", "description": "宿主回传的输入（prompt）Token 总量，仅统计明确回传的记录。"},
    "completion_tokens": {"label": "输出 Token", "description": "宿主回传的输出（completion）Token 总量，仅统计明确回传的记录。"},
    "cache_hit_tokens": {"label": "缓存命中", "description": "宿主回传的 prompt 缓存命中 Token；供应商未回传时无法区分真 0 与未报。"},
    "cache_miss_tokens": {"label": "缓存未命中", "description": "宿主回传的 prompt 缓存未命中 Token。"},
    "missing_usage_source_calls": {"label": "缺用量来源调用", "description": "缺少完整宿主用量或新记录标记的调用，可与失败调用重叠。"},
    "host_reported": {"label": "正常回传", "description": "宿主返回成功响应及可识别的完整用量。"},
    "success_no_usage": {"label": "成功但无用量", "description": "success=true，但宿主未回传完整usage。"},
    "failed": {"label": "失败调用", "description": "宿主响应的success不为true。"},
    # F4 口径对齐（2026-11-04）：legacy 桶按**字段**判定（缺 success/usage_present），
    # 与「schema=1 旧格式行数」是两套口径——带标记的 v1 行归正常桶，legacy 桶可为 0；
    # schema 口径另见 summary 的 schema1_calls 并列展示，避免面板被读成「没有旧记录」。
    "legacy": {"label": "旧记录", "description": "缺少 success 和 usage_present 标记的记录（字段口径，不回填不补写）；带标记的 v1 旧行按成败/用量归入正常桶，故本桶可为 0。schema 口径见 schema1_calls。"},
    "schema1_calls": {"label": "旧schema记录", "description": "schema=1（升级前格式）的行数，与四桶 legacy（字段口径）并列的另一套口径；带 success/usage_present 标记的旧行同时计入本项与正常桶。"},
    "abandoned": {"label": "已放弃", "description": "插件主动放弃但宿主可能已完成并计费的调用（含超时/断连/取消等未收到响应的异常路径）；token 缺失时只计次数。"},
}


def module_of_event(event: Any) -> str:
    """调用点 event → 模块名（未知 event 归「其他」，不丢数）。"""
    return EVENT_MODULE_MAP.get(str(event or ""), "其他")


@dataclass(frozen=True)
class UsageRow:
    """只读账本行（诊断与逐笔明细共用；字段缺省保持 None）。"""

    ts: str
    module: str
    event: str
    task_name: str
    model: str
    prompt: Optional[int]
    completion: Optional[int]
    total: int
    cache_hit: Optional[int]
    cache_miss: Optional[int]
    duration_ms: Optional[int]
    attempts: Optional[int]
    request_id: str
    success: bool
    usage_present: bool
    usage_source: str
    degraded: bool
    abandoned: bool
    schema: int


def _row_of(entry: Any) -> Optional[UsageRow]:
    if not isinstance(entry, dict):
        return None
    return UsageRow(
        ts=str(entry.get("ts") or ""),
        module=entry_module(entry),
        event=str(entry.get("event") or ""),
        task_name=str(entry.get("task_name") or ""),
        model=str(entry.get("model") or ""),
        prompt=_optional_count(entry.get("prompt")),
        completion=_optional_count(entry.get("completion")),
        total=reported_token_count(entry),
        cache_hit=_optional_count(entry.get("cache_hit")),
        cache_miss=_optional_count(entry.get("cache_miss")),
        duration_ms=_optional_count(entry.get("duration_ms")),
        attempts=_optional_count(entry.get("attempts")),
        request_id=str(entry.get("request_id") or ""),
        success=entry.get("success") is True,
        usage_present=entry.get("usage_present") is True,
        usage_source=str(entry.get("usage_source") or ""),
        degraded=entry.get("degraded") is True,
        abandoned=entry.get("abandoned") is True,
        schema=int(entry.get("schema") or 1),
    )


class LLMUsageTracker:
    """LLM 用量记录（JSONL 追加 + 惰性裁剪 + 按窗口聚合）。"""

    #: 未跨日时的最小裁剪间隔（写入行数）；避免每次记账全量重写账本
    PRUNE_EVERY_WRITES = 1500

    def __init__(self, data_dir: Any, *, keep_hours: int = KEEP_HOURS) -> None:
        self._path: Optional[Path] = Path(data_dir) / "llm_usage_events.jsonl" if data_dir else None
        self._keep_hours = max(1, min(KEEP_HOURS, int(keep_hours)))
        self._writes_since_prune = 0
        self._last_prune_day: Optional[str] = None

    @property
    def path(self) -> Optional[Path]:
        return self._path

    # ---- 记账 ----
    def record(self, *, event: Any, model: Any = "",
               prompt_tokens: Any = 0, completion_tokens: Any = 0,
               total_tokens: Any = 0, request_id: Any = "",
               usage_available: bool = False,
               success: bool = False, usage_present: bool = False) -> None:
        """旧调用路径：保留原签名，内部转 v2（不写 module，读取端按 event 回退）。"""
        self.record_v2(
            event=event, module="", model=model,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            total_tokens=total_tokens, request_id=request_id,
            usage_available=usage_available, success=success,
            usage_present=usage_present,
        )

    def record_v2(self, *, event: Any, module: Any = "", model: Any = "",
                  task_name: Any = "", prompt_tokens: Any = None,
                  completion_tokens: Any = None, total_tokens: Any = None,
                  cache_hit_tokens: Any = None, cache_miss_tokens: Any = None,
                  duration_ms: Any = None, attempts: Any = 1,
                  request_id: Any = "", usage_available: bool = False,
                  success: bool = False, usage_present: bool = False,
                  degraded: bool = False, abandoned: bool = False) -> None:
        """记一次调用（tokens 为 0 也记调用次数；异常一律吞掉，绝不打断业务链路）。

        v2 字段只增不改：``module`` / ``task_name`` / 输入输出拆分 / 缓存命中
        与未命中 / ``duration_ms`` / ``attempts`` / ``degraded`` / ``abandoned``。
        """
        if self._path is None:
            return
        total = self._total_of(prompt_tokens, completion_tokens, total_tokens,
                               usage_available)
        from .generation_evidence import request_scope
        scope=request_scope.get() or {}
        entry = {
            "generation_group_id": str(scope.get("group_id") or ""),
            "generation_attempt": scope.get("attempt"),
            "schema": 2,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "module": str(module or ""),
            "event": str(event or ""),
            "task_name": str(task_name or ""),
            "model": str(model or ""),
            "tokens": total,
            "prompt": _optional_count(prompt_tokens),
            "completion": _optional_count(completion_tokens),
            "total": _optional_count(total_tokens),
            "cache_hit": _optional_count(cache_hit_tokens),
            "cache_miss": _optional_count(cache_miss_tokens),
            "duration_ms": _optional_count(duration_ms),
            "attempts": _optional_count(attempts),
            "request_id": str(request_id or "")[:128],
            "usage_source": "host_reported" if usage_available else "unavailable",
            "success": bool(success),
            "usage_present": bool(usage_present),
            "degraded": bool(degraded),
            "abandoned": bool(abandoned),
        }
        try:
            self._writes_since_prune += 1
            if self._should_prune():
                self._prune()
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            logger.debug("LLM 消耗记账写盘失败（忽略）", exc_info=True)

    @staticmethod
    def _total_of(prompt_tokens: Any, completion_tokens: Any,
                  total_tokens: Any, usage_available: bool) -> int:
        """明确回传才取 total；缺失时用输入+输出补齐，绝不估算。"""
        if not usage_available:
            return 0
        total = _optional_count(total_tokens)
        if total is not None:
            return total
        prompt = _optional_count(prompt_tokens)
        completion = _optional_count(completion_tokens)
        if prompt is not None and completion is not None:
            return prompt + completion
        return 0

    def _should_prune(self) -> bool:
        """跨日首次写入或累计写入达阈值才裁剪（默认 30 天保留期）。"""
        today = datetime.now().date().isoformat()
        if self._last_prune_day != today or self._writes_since_prune >= self.PRUNE_EVERY_WRITES:
            self._last_prune_day = today
            self._writes_since_prune = 0
            return True
        return False

    def _prune(self) -> None:
        """惰性裁剪：全行扫描保留期内的行，无陈旧行则不重写。"""
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
                        at = datetime.fromisoformat(ts)
                        # Imported/legacy ledgers can contain aware timestamps; compare actual instants.
                        boundary = cutoff.astimezone(at.tzinfo) if at.tzinfo is not None else cutoff
                        fresh = bool(ts) and at >= boundary
                    except (ValueError, TypeError):
                        fresh = False
                    if fresh:
                        kept.append(line)
                    else:
                        changed = True
            if not changed:
                return
            # F3（2026-11-04）：tmp 名加进程号+随机后缀，避免重载重叠期两个进程
            # 写同一个固定 .tmp 互相覆盖（理论丢行窗口）；失败时清理残 tmp。
            tmp = path.with_name(
                f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
            try:
                tmp.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
                tmp.replace(path)
            except Exception:
                try:
                    tmp.unlink()
                except OSError:
                    # 门禁修复（2026-11-04，P2-08 静默失败守卫）：残 tmp 清理失败
                    # 不再静默 pass——落 debug 痕（best-effort 语义不变，随后照旧
                    # raise 交外层记录裁剪失败）；至多遗留一个带 pid/随机后缀的 tmp。
                    logger.debug("LLM 用量裁剪残留 tmp 清理失败（忽略）：%s", tmp, exc_info=True)
                raise
        except Exception:
            logger.debug("LLM 用量记录裁剪失败（忽略）", exc_info=True)

    # ---- 聚合 ----
    def summary(self, hours: int = DEFAULT_USAGE_WINDOW_HOURS, *,
                now: Optional[datetime] = None,
                include_rows: bool = False) -> Dict[str, Any]:
        """聚合最近 ``hours`` 小时：调用次数 / token / 模型 / 模块 / 输入输出 / 缓存。

        返回既有键（``calls`` / ``tokens`` / ``models`` / ``modules`` /
        ``has_usage`` / ``buckets`` / ``missing_usage_source_calls``）保持原语义，
        新增：``window_hours`` / ``prompt_tokens`` / ``completion_tokens`` /
        ``cache_hit_tokens`` / ``cache_miss_tokens`` / ``module_calls`` /
        ``matrix``（模块 × 模型，值为 ``{calls,tokens}``）/ ``abandoned_calls`` /
        ``unknown_module_calls`` / ``schema1_calls``（F4：schema=1 旧格式行数，
        与四桶 legacy 的字段口径并列展示）/ ``rows``（逐笔明细；仅 ``include_rows=True``
        时收集，避免首屏 payload 膨胀）；W-09 只增：``prompt_reported_calls`` /
        ``completion_reported_calls`` / ``cache_reported_calls`` /
        ``split_tokens`` / ``split_coverage``（拆分覆盖率，tokens=0 时为 None）。
        文件缺失/损坏 → 空账本（不抛异常）。
        """
        from .monitoring import usage_summary
        return usage_summary(self._path.parent if self._path else None,
                             now or datetime.now(),hours or DEFAULT_USAGE_WINDOW_HOURS,
                             include_rows=include_rows)

    def rows(self, hours: int = DEFAULT_USAGE_WINDOW_HOURS, *,
             now: Optional[datetime] = None,
             limit: int = 50, offset: int = 0, level: str = '', module: str = '') -> Dict[str, Any]:
        """逐笔明细（分页；不塞进首屏 payload）。"""
        data = self.summary(hours, now=now, include_rows=True)
        return usage_rows_page(data,limit=limit,offset=offset,level=level,module=module)


def usage_rows_page(data,*,limit=50,offset=0,level='',module=''):
    """Filter the exact summary projection before paging; no second read or reclassification."""
    level=level if level in ('info','warn','error','unknown') else ''
    module=str(module or '')[:80]
    all_rows=[row for row in data['rows'] if (not level or row['level']==level)
              and (not module or row['module']==module)]
    start=max(0,int(offset or 0));count=max(1,min(200,int(limit or 50)))
    page=all_rows[start:start+count]
    return {'window_hours':data['window_hours'],'total':len(all_rows),
            'unfiltered_total':len(data['rows']),'offset':start,'limit':count,'rows':page,
            'truncated':len(all_rows)>start+len(page),'read_truncated':data['truncated'],
            'semantic_evidence_truncated':data['semantic_evidence_truncated'],
            'filters':{'level':level,'module':module},'level_counts':data['level_counts']}

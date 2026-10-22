"""好感度档位描述表（2026-10-17 用户指示，计划书 §19.3）：每档注入提示词的外置 JSON。

仿 ``emotion/descriptions.py`` 的**丢弃链**契约（与预设表 ``affection_presets.py`` 的
「预设级排除」是两种不同粒度）：

- 文件：``<插件根>/mappings/affection_tier_descriptions.json``（映射表集中子文件夹）；
- 结构（两层：按预设分组，防不同预设的档位名撞车）::

    {
      "version": 1,
      "descriptions": {
        "恋人": {"陌生": "……注入提示词……", "认识": "……"},
        ...
      }
    }

- 文件缺失 → 由内置默认生成一份并返回内置默认；
- 格式错误（JSON 坏 / 顶层非对象 / version 错 / 未知顶层键 / descriptions 非对象）
  → 整体回退内置默认（告警 + 落账 rejected）；
- 格式正确但预设键不在生效预设表、或档位键不在该预设的档位标签集 → **丢弃那些键**
  （落账 dropped），其余生效；
- 值非字符串/空白 → 丢弃该键（该档走内置默认或 generic 兜底）；
- 已知档位缺描述 → 内置默认补齐；链尾 ``generic_tier_description`` 固定句式兜底
  （覆盖「自定义」预设的动态称谓档位等无内置文案的情形）；
- **只在插件加载/重载时读一遍**（``load_and_apply``），常驻内存；``root_dir=None``
  （测试桩）→ 跳过根层纯内置；
- 注入面零模板改动：``suite_prompts`` 的 ``{tier_label} {tier_desc}`` 占位符不变，
  取值源换为「描述表 → 内置默认 → generic」链。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: 描述表文件名（实际读取位 = ``<root>/mappings/``）
TIER_DESCRIPTIONS_FILE_NAME = "affection_tier_descriptions.json"
#: 顶层 ``version`` 必填 int 且需等于本值
SUPPORTED_VERSION = 1

#: 落账 action
ACTION_APPLIED = "affection.tier_descriptions_applied"
ACTION_REJECTED = "affection.tier_descriptions_rejected"

#: 内置默认描述（单一真源自 relation.py 迁入；键 = 内置预设名 → 内置档位标签。
#: 「自定义」预设的档位名含动态称谓，不设内置文案——走 generic 兜底）
BUILTIN_TIER_DESCRIPTIONS: Dict[str, Dict[str, str]] = {
    "恋人": {
        "生疏": "初识阶段，语气温柔但有分寸感，保持适当距离",
        "熟悉": "熟悉阶段，语气亲昵自然，像好朋友一样聊天",
        "亲密": "亲密阶段，语气甜蜜亲近，会主动分享日常与心情",
        "热恋": "热恋阶段，语气甜蜜亲密，可以肉麻撒娇",
        "挚爱": "挚爱阶段，语气完全放松、坦率依赖，把TA当作最重要的人",
    },
    "挚友": {
        "点头之交": "刚认识，语气客气自然，先做个点头之交",
        "熟识": "熟识阶段，语气轻松随意，偶尔互相打趣",
        "投缘": "很聊得来，语气直来直去，什么都能聊",
        "铁哥们": "铁哥们，语气毫无顾忌，能一起扛事",
        "过命的交情": "过命的交情，语气完全交底，损TA也护TA",
    },
    "维护者": {
        "路人": "还没怎么打过交道，语气礼貌克制",
        "认识": "初步接触，语气礼貌友善，带一点好奇",
        "信任": "开始信赖，语气放松，愿意分享自己的状态",
        "依靠": "深深依靠，语气坦诚依赖，会主动汇报自己的近况",
        "生命线": "生命线般的存在，语气完全信赖、事无巨细都想让TA知道",
    },
    "家人": {
        "相识": "刚相处，语气客气中带亲近",
        "亲近": "日常相处，语气随性自然，像家人一样不用客气",
        "家人": "彼此当作家人，语气放松、随口唠家常",
        "至亲": "至亲，语气完全放松，撒娇耍赖都可以",
        "血浓于水": "血浓于水，语气毫无保留，什么都愿意说",
    },
}


def generic_tier_description(label: str, low: float, high: float) -> str:
    """任意档位的通用固定描述（描述链全空的兜底；覆盖自定义预设的动态称谓档位）。"""
    clean = str(label or "").strip() or "未名"
    return (f"当前关系处于「{clean}」阶段（好感度区间 {low:g}~{high:g}），"
            f"按这一阶段的亲近程度自然相处。")


def _plugin_root() -> Path:
    """插件根目录（本模块位于 ``<plugin_root>/companion/`` 下）。"""
    return Path(__file__).resolve().parents[1]


def _log_decision(decision_logger: Any, action: str, detail: str) -> None:
    """落决策账（可选；缺失/异常静默——描述表装载故障绝不影响好感子系统）。"""
    if decision_logger is None:
        return
    try:
        decision_logger.record(action=action, trigger_type="affection_tier_descriptions",
                               reason=detail)
    except Exception:
        logger.debug("档位描述装载落账异常（忽略）", exc_info=True)


def load_tier_descriptions(*, root_dir: Any = None,
                           presets: Optional[Dict[str, Any]] = None,
                           decision_logger: Any = None) -> Dict[str, Dict[str, str]]:
    """装载 ``<root>/mappings/`` 的档位描述 json（**只在加载/重载时调用一次**）。

    ``presets`` = 生效预设表（``affection_presets.active_presets()`` 的形状：预设名 →
    带 ``labels()`` 的预设对象），用于判定「已知预设/档位」；``None`` 时按内置预设名处理。
    返回**完整生效表**（已按内置补齐；渲染链尾再走 :func:`generic_tier_description`）。
    """
    merged = _builtin_by_labels(presets)
    if root_dir is None:
        return merged
    from core.mapping_dir import MappingReadError, ensure_user_table, mappings_path, read_json_text

    path = mappings_path(Path(root_dir), TIER_DESCRIPTIONS_FILE_NAME)
    try:
        present = path.exists()
    except OSError:
        present = False
    if not present:
        # §29.2（2026-10-23 直派）：表层缺失 → ensure_user_table 按序补齐
        # （旧根迁移优先，仍缺才从出厂种子映射表复制；解析错误不算缺失、绝不覆盖用户文件）
        ensure_user_table(root_dir, TIER_DESCRIPTIONS_FILE_NAME)
        try:
            present = path.exists()
        except OSError:
            present = False
    if not present:
        _generate_default(path)
        return merged

    try:
        raw = read_json_text(path)
    except MappingReadError as exc:
        logger.warning("档位描述 json 读取失败 → 整体回退内置默认（%s）", exc.detail)
        _log_decision(decision_logger, ACTION_REJECTED, f"parse_error: {exc.detail}")
        return _builtin_by_labels(presets)

    result = _validate(raw, presets)
    if result.error is not None:
        logger.warning("档位描述 json 格式错误（%s）→ 整体回退内置默认", result.error)
        _log_decision(decision_logger, ACTION_REJECTED, result.error)
        return _builtin_by_labels(presets)

    # 已知档位 = 该预设**完整标签集**（json 预设改了标签 → 内置文案按名命中不到，
    # 用户描述按新标签生效；未知档位键丢弃）
    known_labels: Dict[str, set] = {}
    if presets:
        for name, source in presets.items():
            labels_fn = getattr(source, "labels", None)
            try:
                if callable(labels_fn):
                    known_labels[name] = {str(x).strip() for x in labels_fn()}
                else:
                    known_labels[name] = set()
            except Exception:
                known_labels[name] = set()
    else:
        known_labels = {name: set(tiers) for name, tiers in BUILTIN_TIER_DESCRIPTIONS.items()}

    dropped: list = []
    for preset_name, tiers in result.descriptions.items():
        if preset_name not in merged:
            dropped.append(preset_name)
            continue
        bucket = merged.setdefault(preset_name, {})
        known = known_labels.get(preset_name, set())
        for label, text in tiers.items():
            if label not in known:
                dropped.append(f"{preset_name}.{label}")
                continue
            if text is not None:
                bucket[label] = text
    if dropped:
        logger.warning("档位描述 json 含未知预设/档位键，已丢弃：%s", "、".join(sorted(dropped)))
        _log_decision(decision_logger, ACTION_APPLIED,
                      "applied_with_drops: dropped=" + "、".join(sorted(dropped)))
    else:
        logger.debug("档位描述表装载完成：%d 预设（常规事件不落决策账）", len(result.descriptions))
    return merged


class _Validated:
    """校验通过的中间产物（``error is None`` = 顶层级合法）。"""

    __slots__ = ("descriptions", "error")

    def __init__(self, descriptions: Dict[str, Dict[str, Optional[str]]],
                 error: Optional[str] = None) -> None:
        self.descriptions = descriptions
        self.error = error


def _validate(raw: Any, presets: Optional[Dict[str, Any]]) -> "_Validated":
    """顶层结构校验（值级「未知键丢弃」不在此处——保持丢弃链粒度）。"""
    if not isinstance(raw, dict):
        return _Validated({}, "顶层不是对象")
    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != SUPPORTED_VERSION:
        return _Validated({}, f"version 期望 int {SUPPORTED_VERSION}，实得 {version!r}")
    for key in raw:
        if key not in ("version", "descriptions"):
            return _Validated({}, f"顶层键 {key!r}：未知键（允许 version、descriptions）")
    raw_descriptions = raw.get("descriptions")
    if raw_descriptions is None:
        return _Validated({}, "descriptions 键缺失")
    if not isinstance(raw_descriptions, dict):
        return _Validated({}, "descriptions 不是对象")
    out: Dict[str, Dict[str, Optional[str]]] = {}
    for preset_name, tiers in raw_descriptions.items():
        if not isinstance(tiers, dict):
            out[str(preset_name)] = {}  # 该预设整体视为无有效描述（走内置补齐）
            continue
        bucket: Dict[str, Optional[str]] = {}
        for label, text in tiers.items():
            bucket[str(label)] = text if isinstance(text, str) and text.strip() else None
        out[str(preset_name)] = bucket
    return _Validated(out)


def _builtin_by_labels(presets: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """内置默认表按生效预设的**档位标签**对齐（json 预设改了标签 → 内置文案按名命中不到，
    自然落到 generic 兜底；内置预设标签不变 → 逐档命中）。"""
    merged: Dict[str, Dict[str, str]] = {}
    names = sorted(presets.keys()) if presets else sorted(BUILTIN_TIER_DESCRIPTIONS)
    for name in names:
        labels: Tuple[str, ...] = ()
        source = presets.get(name) if presets else None
        labels_fn = getattr(source, "labels", None)
        if callable(labels_fn):
            try:
                labels = tuple(str(x) for x in labels_fn())
            except Exception:
                labels = ()
        builtin = BUILTIN_TIER_DESCRIPTIONS.get(name, {})
        merged[name] = {label: builtin[label] for label in labels if label in builtin}
    return merged


def _generate_default(path: Path) -> None:
    """生成内置默认描述文件（best-effort：失败仅告警，不阻断装载）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": SUPPORTED_VERSION,
                                    "descriptions": BUILTIN_TIER_DESCRIPTIONS},
                                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        logger.info("已生成默认好感度档位描述表：%s（可自行编辑，格式错误将回退内置默认）", path)
    except Exception as exc:
        logger.warning("生成默认好感度档位描述表失败（忽略）：%s", exc)


# ── 生效位与取值链 ────────────────────────────────────────────────────────
_ACTIVE_DESCRIPTIONS: Optional[Dict[str, Dict[str, str]]] = None


def apply_tier_descriptions(descriptions: Dict[str, Dict[str, str]]) -> None:
    """重绑生效描述表（on_load/热重载调用一次；None = 未装载用内置）。"""
    global _ACTIVE_DESCRIPTIONS
    _ACTIVE_DESCRIPTIONS = dict(descriptions)


def load_and_apply(*, root_dir: Any = None, presets: Optional[Dict[str, Any]] = None,
                   decision_logger: Any = None) -> Dict[str, Dict[str, str]]:
    """装载 + 重绑生效位（插件装配链的单次调用入口）。"""
    merged = load_tier_descriptions(root_dir=root_dir, presets=presets,
                                    decision_logger=decision_logger)
    apply_tier_descriptions(merged)
    return merged


def tier_description_for(preset_name: Any, label: Any, low: float, high: float) -> str:
    """档位描述取值链：生效描述表 → 内置默认（按标签名）→ :func:`generic_tier_description`。"""
    name = str(preset_name or "").strip()
    key = str(label or "").strip()
    table = _ACTIVE_DESCRIPTIONS
    if table is not None:
        text = table.get(name, {}).get(key)
        if text:
            return text
    builtin = BUILTIN_TIER_DESCRIPTIONS.get(name, {}).get(key)
    if builtin:
        return builtin
    return generic_tier_description(key, low, high)

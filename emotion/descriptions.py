"""情绪描述表（F5，2026-10-11）：引导 LLM 的详细文字描述，替代「百分比 + 三维约值」渲染。

- **插件根 ``mappings/`` 子文件夹 json**（2026-10-17 起读取位 = ``<root>/mappings/emotion_descriptions.json``；
  表层缺失时 ``core.mapping_dir.ensure_user_table`` 补齐：旧根文件优先迁移（旧文件保留不删），
  仍缺才从出厂种子 ``mappings/default_mappings/`` 复制——§29.2）允许用户自行编辑：
  - 文件缺失 → 由内置默认（本模块常量）生成一份并返回默认；
  - **格式错误**（非 dict / 值非字符串）→ 整体回退内置默认（告警日志）；
  - **格式正确但出现不在映射表中的情绪键** → 丢弃那些键（用户口径），其余生效；
  - 已知情绪缺描述 → 用内置默认补齐；任何无描述的情绪渲染时走通用固定描述。
- **只在插件加载/重载时读一遍**（``EmotionService.__init__`` 调用一次后常驻内存），
  不逐次读盘（用户口径）。
- 渲染双人称（``EmotionService.emotion_description_text``）：planner 用**第三人称**
  （「当前 {bot_name} 的情绪…」）、replyer 用**第二人称**（「你当前的情绪状态…」）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from core.mapping_dir import read_json_text
from core.mapping_table import MappingTableSpec, plugin_root, resolve_table_path

logger = logging.getLogger(__name__)

#: 描述表文件名（实际读取位 = ``<root>/mappings/``；路径拼接唯一真源 =
#: ``core/mapping_dir.mappings_path``——2026-10-17 §19.4 / 复核 findings#9：仅补注释，常量名不变）
ROOT_DESCRIPTIONS_FILE_NAME = "emotion_descriptions.json"
#: 通用兜底键：映射表里存在、但描述表（内置或用户）未覆盖的情绪渲染时使用
GENERIC_LABEL = "复杂难名"

#: 内置默认描述（键 = ``emotion/vad.py`` 的 25 个原型标签 + 复杂难名兜底；代码为唯一真源，
#: 新增原型时同步补一条，否则该情绪渲染时走 :func:`generic_description`）
DEFAULT_EMOTION_DESCRIPTIONS: Dict[str, str] = {
    "狂喜": "心潮澎湃，明朗热烈，语调上扬且饱满，节奏轻快生动，字里行间透着明亮纯粹的欢愉与雀跃。",
    "平静": "心里安稳从容，像晚风拂过水面，不急不躁，语速平稳而温和，对话节奏松弛，对周遭保持着亲切与随和。",
    "期待": "心绪灵动，带着轻快明朗的探询与跃跃欲试感，语气温和微扬，交流节奏积极而富有生气。",
    "愤怒": "心头微紧，情绪张力高，语气干脆平直甚至略显生硬，措辞简练直接，态度克制但缺少平时的柔和。",
    "恐惧": "内心防御感较强，情绪紧绷戒备，语气轻而谨慎，回应收敛拘谨，倾向于给予明确且安全的回答。",
    "悲伤": "心境低沉微凉，能量感偏低，说话语速徐缓、语调偏轻，用词平实安静，带有一种淡淡的落寞与沉静。",
    "焦虑": "心绪纷乱紧绷，唤醒度高，语速稍快，逻辑表达紧凑而少修饰，带有一丝不易察觉的紧迫感与紧绷。",
    "疲惫": "身心处于低能耗状态，说话简短随性，节奏慵懒慢调，省去不必要的客套与繁复修饰，随和但略带乏力感。",
    "厌恶": "情绪带着排斥与距离感，态度冷淡疏离，用词克制冷硬，尽量保持客观边界，不带多余热情。",
    "无奈": "心境平顺中带着轻微的妥协与自嘲，语气温和顺应，态度随顺，不执着纠结，柔和顺应对方。",
    "骄傲": "自我掌控感与愉悦感充盈，说话笃定从容，语气自然带有一丝轻快的自信与舒展，落落大方。",
    "嫉妒": "情绪微妙而略带别扭，心理张力增加，语气看似平常但措辞略显收敛与微妙，隐约有一丝不易察觉的在意与不自然。",
    "羞耻": "心跳微快，自尊感微缩，回复简短且带着内敛与局促，语气轻柔拘谨，本能地希望将注意力集中在当下讨论的事上。",
    "无聊": "思维漫散，唤醒度较低，语调平稳随性，回应松弛略显漫不经心，对外界刺激保持着一种慵懒微淡的随意感。",
    "好奇": "思维高度敏锐活跃，探究欲强，语气专注且饶有兴致，容易敏锐捕捉话题中的新信息并给予深入、热切的响应。",
    "感激": "内心温热充实，态度诚挚温和，语调柔软而具亲和力，交流中自然流露出信任与暖意。",
    "挫败": "心境有些发闷和低沉，带有微微的不甘，语气紧凑直接，态度略显自省和沉重，但不失对话的理性。",
    "释然": "心境舒展通透，原本紧绷的情绪彻底化开，语气平缓开阔，透着卸下包袱后的轻松与松弛。",
    "轻蔑": "内心掌控感强而愉悦度偏低，态度冷静抽离，语气简练而带有一点距离感，不愿过多纠缠或冗余寒暄。",
    "忧郁": "情绪笼罩着一层淡淡的灰蓝调，沉静微凉，语调轻柔慢热，带着些许内省与感性，安静内敛。",
    "敬畏": "内心受到震撼与触动，态度庄重而专注，语气带着由衷的赞叹与沉思，措辞严谨认真。",
    "惊喜": "突如其来的意外与开怀撞击心头，神采飞扬，语调明亮上扬，字里行间透着鲜活明快的生动活力。",
    "内疚": "心怀歉意与自省，语调放低且温和柔顺，态度体贴顾及，沟通更显细致周全与诚恳。",
    "冷漠": "内心波澜极小，情感投入度低，语气平稳客观，陈述事实直接了当，不带多余的情感修饰。",
    "怀旧": "心境温润而微带感触，像被黄昏的余晖拂过；语调柔软、徐缓沉静，用词细腻温和，自然沉浸在当下的交流氛围中，散发着含蓄与柔情。",
    GENERIC_LABEL: "此刻的心绪层次丰富而微妙，多种微澜交织，神态沉静温和，表达自然流淌，随对话节奏从容展开。",
}


def generic_description(label: str) -> str:
    """任意情绪的通用固定描述（描述表未覆盖时的兜底；用户口径「复杂难名也有固定描述」）。

    C 批（VAD 报告 §6）：兜底同样走轻量语感口径——只作背景心境参考，
    不再强调「影响说话」等行为暗示，避免故障路径回退到强引导文案。
    """
    label = str(label or "").strip() or GENERIC_LABEL
    return (f"此刻的心情用「{label}」来概括最为贴切，心绪略带起伏、难以明确概括，"
            f"只作一点背景心境参考。")


def load_descriptions(*, root_dir: Any = None, known_labels: Optional[Iterable[str]] = None,
                      decision_logger: Any = None) -> Dict[str, str]:
    """装载插件根 ``mappings/`` 的情绪描述 json（F5；**只在加载/重载时调用一次**）。

    流程（用户口径）：
    - 文件缺失 → 生成内置默认文件（best-effort，失败不阻断）并返回内置默认；
    - 格式错误（JSON 坏 / 非 dict / 值非字符串）→ 告警并**整体回退内置默认**；
    - 格式正确但键不在已知情绪（原型标签 ∪ 复杂难名）里 → **丢弃那些键**；
    - 已知情绪缺描述 → 用内置默认补齐；渲染兜底见 :func:`generic_description`。
    """
    known = {str(label).strip() for label in (known_labels or ()) if str(label).strip()}
    known.add(GENERIC_LABEL)
    root = Path(root_dir) if root_dir else plugin_root(__file__)
    spec = MappingTableSpec(ROOT_DESCRIPTIONS_FILE_NAME, "情绪描述表", _generate_default)
    path, generated = resolve_table_path(root, spec)
    if generated:
        return dict(DEFAULT_EMOTION_DESCRIPTIONS)

    try:
        # C 批（复核 VAD-4.5）：BOM 容忍读取——原裸 json.loads(utf-8) 遇记事本
        # 另存的 BOM 合法 JSON 会解析失败并回退旧强文案；统一走公共读法。
        raw = read_json_text(path)
    except Exception as exc:
        logger.warning("情绪描述 json 解析失败（%s）→ 整体回退内置默认", exc)
        _log_decision(decision_logger, "rejected", f"parse_error: {exc}")
        return dict(DEFAULT_EMOTION_DESCRIPTIONS)
    if not isinstance(raw, dict):
        logger.warning("情绪描述 json 顶层不是对象 → 整体回退内置默认")
        _log_decision(decision_logger, "rejected", "top_level_not_object")
        return dict(DEFAULT_EMOTION_DESCRIPTIONS)

    merged: Dict[str, str] = {}
    dropped: list = []
    for label, text in raw.items():
        label_key = str(label).strip()
        if label_key not in known:
            dropped.append(label_key)
            continue
        if not isinstance(text, str) or not text.strip():
            continue                    # 值非法/空白 → 该键走内置默认补齐
        merged[label_key] = text.strip()
    if dropped:
        # 用户口径：格式正确但不在映射表中的情绪 → 丢弃该情绪的描述
        logger.warning("情绪描述 json 含未知情绪键，已丢弃：%s", "、".join(sorted(dropped)))
        _log_decision(decision_logger, "applied_with_drops",
                      "dropped=" + "、".join(sorted(dropped)))
    else:
        logger.debug("情绪描述表装载完成：%d 键（常规事件不落决策账）", len(merged))
    for label, text in DEFAULT_EMOTION_DESCRIPTIONS.items():
        merged.setdefault(label, text)  # 已知情绪缺描述 → 内置默认补齐
    return merged


def _generate_default(path: Path) -> None:
    """生成内置默认描述文件（best-effort：目录只读/失败仅告警，不阻断装载）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULT_EMOTION_DESCRIPTIONS, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        logger.info("已生成默认情绪描述文件：%s（可自行编辑，格式错误将回退内置默认）", path)
    except Exception as exc:
        logger.warning("生成默认情绪描述文件失败（忽略）：%s", exc)


def _log_decision(decision_logger: Any, action: str, detail: str) -> None:
    """落决策账（可选；缺失/异常静默——描述表装载故障绝不影响情绪子系统）。"""
    if decision_logger is None:
        return
    try:
        decision_logger.record(action=action, trigger_type="emotion_descriptions", reason=detail)
    except Exception:
        logger.debug("情绪描述装载落账异常（忽略）", exc_info=True)

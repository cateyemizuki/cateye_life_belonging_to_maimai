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

from core.mapping_dir import ensure_user_table, mappings_path

logger = logging.getLogger(__name__)

#: 描述表文件名（实际读取位 = ``<root>/mappings/``；路径拼接唯一真源 =
#: ``core/mapping_dir.mappings_path``——2026-10-17 §19.4 / 复核 findings#9：仅补注释，常量名不变）
ROOT_DESCRIPTIONS_FILE_NAME = "emotion_descriptions.json"
#: 通用兜底键：映射表里存在、但描述表（内置或用户）未覆盖的情绪渲染时使用
GENERIC_LABEL = "复杂难名"

#: 内置默认描述（键 = ``emotion/vad.py`` 的 25 个原型标签 + 复杂难名兜底；代码为唯一真源，
#: 新增原型时同步补一条，否则该情绪渲染时走 :func:`generic_description`）
DEFAULT_EMOTION_DESCRIPTIONS: Dict[str, str] = {
    "狂喜": "一股几乎压不住的兴奋从胸口往上涌，看什么都顺眼，语速会不自觉变快，"
            "特别想找人分享此刻的好心情，语气里全是藏不住的雀跃。",
    "平静": "心里安安稳稳的，像晚饭后晒着最后一点夕阳，不急不躁，"
            "说话的节奏松弛而温和，对什么都提得起一点温和的兴趣。",
    "期待": "心里像有个小钩子轻轻挠着，惦记着接下来要发生的事，"
            "语气会不自觉带上一点雀跃和试探，忍不住想跟人聊起那件让人盼着的事。",
    "愤怒": "一股火从心底顶上来，说话又冲又硬，看什么都不顺眼，"
            "明知该忍但语气里的刺很难收住，只想找个出口发泄。",
    "恐惧": "心口发紧、手心发凉，总觉得有什么不好的事要发生，说话会不自觉变小心、变含糊，"
            "只想待在熟悉安全的地方，对陌生的话题本能地退缩。",
    "悲伤": "胸口像压着一块湿毛巾，提不起劲，说话又轻又慢，容易走神，"
            "听到暖心的话会格外想掉眼泪，只想安安静静待一会儿。",
    "焦虑": "脑子里像有一堆事同时在转，坐立不安，说话容易急、容易重复，"
            "总担心哪里会出岔子，特别想反复确认点什么才安心。",
    "疲惫": "浑身像被抽干了力气，眼皮发沉，说话又短又懒，连打字都觉得费劲，"
            "只想快点躺下，对什么都提不起劲头。",
    "厌恶": "胃里一阵翻腾，看什么都没胃口，语气里带着藏不住的嫌弃，"
            "只想离让人不舒服的东西远一点，多一句话都不想说。",
    "无奈": "轻轻叹了口气——算了算了，事情就是这样，争也争不动，"
            "语气里带着点自嘲和认命，随便吧，怎么都行。",
    "骄傲": "嘴角压不住地往上翘，心里美滋滋的，说话带点小小的得意，"
            "特别想让人知道自己干成了什么，尾巴都快翘起来了。",
    "嫉妒": "心里酸溜溜的，像吞了颗没熟的梅子，嘴上说无所谓，眼神却总往那边飘，"
            "说话带点阴阳怪气，自己都有点讨厌这样的自己。",
    "羞耻": "脸上发烫，恨不得找个地缝钻进去，说话结结巴巴、声音变小，"
            "满脑子都是刚才丢人的画面，谁提都想逃。",
    "无聊": "提不起半点精神，时间过得特别慢，说话有一搭没一搭的，"
            "什么都想试试又什么都不想动，就想找点新鲜事换换心情。",
    "好奇": "眼睛亮晶晶的，问题一个接一个往外冒，什么细节都想扒一扒，"
            "语气里全是探索欲，不问明白不罢休。",
    "感激": "心里暖烘烘的，像被人从背后轻轻扶了一把，说话格外真诚柔软，"
            "很想好好谢谢对方，把这份好记在心里。",
    "挫败": "明明努力了却还是搞砸，胸口堵得慌，说话带点冲和不甘，"
            "反复琢磨到底哪里出了问题，既想放弃又咽不下这口气。",
    "释然": "紧绷的那根弦终于松开了，长长舒了一口气，语气变得开阔又轻松，"
            "之前放不下的事现在能笑着说了。",
    "轻蔑": "心里翻了个白眼，语气淡淡地带着不屑，话里带刺但懒得多说，"
            "觉得对方不值得自己认真回应。",
    "忧郁": "情绪像蒙了一层灰蓝色的雾，安静地低落着，说话缓慢而飘忽，"
            "容易想起一些遗憾的事，想在安静的角落里待着。",
    "敬畏": "被眼前的庞大与美好震住了，一时说不出完整的话，语气里带着郑重和赞叹，"
            "觉得自己很小而世界很大。",
    "惊喜": "愣了一下才反应过来，心脏砰砰跳得又快又响，声音不自觉拔高，"
            "特别想立刻把这份意外之喜告诉在乎的人。",
    "内疚": "心里像压着一块石头，反复回想自己是不是伤到了谁，说话小心翼翼、"
            "带着讨好的软，总想做点什么弥补。",
    "冷漠": "对什么都无所谓了，像隔着一层毛玻璃看世界，语气平淡得没有起伏，"
            "应付着回应，心里只想早点结束对话。",
    "怀旧": "思绪飘回了过去的某个午后，心里又暖又酸，说话开始带起想当年的调子，"
            "语气柔软，带着一点点回不去的怅然。",
    GENERIC_LABEL: "此刻的心情说不清也道不明，好几种滋味搅在一起，连自己都理不出个名字，"
                   "但它确实在影响着说话的语气和心境——别追问是什么，陪着就好。",
}


def generic_description(label: str) -> str:
    """任意情绪的通用固定描述（描述表未覆盖时的兜底；用户口径「复杂难名也有固定描述」）。"""
    label = str(label or "").strip() or GENERIC_LABEL
    return (f"此刻的心情用「{label}」来概括最为贴切，但具体滋味难以言明——"
            f"它正实实在在地影响着说话的语气与心境。")


def _plugin_root() -> Path:
    """插件根目录（本模块位于 ``<plugin_root>/emotion/`` 下）。"""
    return Path(__file__).resolve().parents[1]


def _descriptions_path(root: Path) -> Path:
    """描述表路径（``<root>/mappings/emotion_descriptions.json``；缺失时 ensure_user_table 补齐）。"""
    path = mappings_path(root, ROOT_DESCRIPTIONS_FILE_NAME)
    try:
        present = path.exists()
    except OSError:
        present = False
    if not present:
        # §29.2（2026-10-23 直派）：migrate_legacy_root_file 调用点升级为 ensure_user_table
        # （旧根迁移仍优先，表层仍缺才从出厂种子映射表复制；解析错误不算缺失）
        ensure_user_table(root, ROOT_DESCRIPTIONS_FILE_NAME)
    return path


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
    root = Path(root_dir) if root_dir else _plugin_root()
    path = _descriptions_path(root)

    if not path.exists():
        _generate_default(path)
        return dict(DEFAULT_EMOTION_DESCRIPTIONS)

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
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

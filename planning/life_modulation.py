"""情绪 → 生活状态调制层 LifeModulationService（R7 依据 = 用户 2026-10-26 直派
【日程系统二期 PR2 双向闭环·调制侧，计划书 §三十四 / 定稿 §4.1.1】）。

职责单一：把 emotion 侧的**生效 VAD 快照**（v/a/d，含静态偏移/乘数结算后的最终值，
真源 emotion/state_machine.py ``EmotionStateMachine.effective_vad()``）折算成
LifeState 五项自然速率的 rise/fall 增益（gain），供 ``LifeStateService.tick`` 对每个
状态的**基础 delta**（被动漂移 + 活动画像速率）按符号相乘——不乘当前状态值，
不触碰 episode 事件入账（LifeState → VAD 反向写回由 B 员的 life_feedback 落）。

流水线（定稿 §4.1.1 逐条）：

1. 取生效 VAD 的 v/a/d，先 clamp [-1, 1]；
2. EMA 平滑（时间常数默认 15 分钟，服务内状态，进程内不持久化——定稿原文
   「不持久化 gain，重启从当前 VAD 重算」）；
3. 中性死区 0.12：某一维 |x| < 死区时该维按 0 处理（该维回到中性，对应调制量取
   中性值）；
4. 三调制量（定稿公式逐字）::

       low_mood   = max(0, -v)
       activation = clamp((a + 1) / 2, 0, 1)
       tension    = max(0, a) * max(0, -d)

5. 五项 gain 按定稿 §4.1.1 表（rise = 状态上升方向 / fall = 下降方向；focus 是容量
   不区分方向，fall 同 rise），单项先按公式（含 floor），全部再夹 [0.70, 1.30]：

   ================  ==============================================  =============
   状态              rise gain                                       fall gain
   ================  ==============================================  =============
   energy            1-.25*low_mood-.10*tension（floor .75）          1+.25*act+.15*low_mood
   hunger            1+.20*low_mood-.10*act                          1-.10*low_mood+.05*act
   focus             (1-1.2*(act-.35)^2)*(1-.25*low_mood)（floor .60） 同 rise
   stress            1+.35*tension+.15*low_mood                      1-.10*low_mood（floor .80）
   social_battery    1-.15*low_mood（floor .80）                      1+.20*low_mood+.15*tension
   ================  ==============================================  =============

系数外置表：``mappings/emotion_life_modulation.json``（经 core/mapping_dir 种子链：
表层缺失先 ensure_user_table 按序补齐——旧根迁移优先、仍缺从出厂种子复制；解析错误
不算缺失、绝不覆盖用户文件）。表 schema 与定稿 §4.1.1 的显式 rise/fall 公式对齐
（附录 B.3 的 inverted_u 等结构**不是**本装载方的读取口径）。装载方兼容两种落表
形态（数值语义同源，仅键位不同）：**canonical 形态**（顶层直挂状态、terms 包裹、
floor/quad_weight/quad_optimal 内嵌）::

    {"version": 1,
     "smoothing_minutes": 15, "dead_zone": 0.12, "gain_min": 0.70, "gain_max": 1.30,
     "energy": {"rise": {"terms": {"low_mood": -0.25, "tension": -0.10}, "floor": 0.75},
                "fall": {"terms": {"activation": 0.25, "low_mood": 0.15}}},
     "focus":  {"rise": {"quad_weight": 1.2, "quad_optimal": 0.35,
                          "terms": {"low_mood": -0.25}, "floor": 0.60}}, ...}

与**种子落码形态**（随包种子 ``mappings/default_mappings/emotion_life_modulation.json``
实际采用的键位：``"gains"`` 包裹、terms 内联、条目级 ``rise_floor``/``fall_floor``、
focus 用 ``activation_penalty``/``activation_center`` 与 ``fall_same_as_rise``）——
装载侧逐字段兼容，用户按任一形态编辑都生效。::

    {"version": 1, "smoothing_minutes": 15, "dead_zone": 0.12,
     "gain_min": 0.70, "gain_max": 1.30,
     "gains": {"energy": {"rise": {"low_mood": -0.25, "tension": -0.10}, "rise_floor": 0.75,
                          "fall": {"activation": 0.25, "low_mood": 0.15}}, ...}}

字段级容错：缺键/坏键单独回退内置值；顶层非法 / version 不认识 → 整表回退内置；
**文件缺失/解析失败绝不阻塞**（gain 全按内置默认=定稿表生效）。

纯离线零 LLM（红线 1）；不发布事件、不触碰主动消息链（纪律 5）；全部方法失败静默
回退、不阻塞主循环（与 life_state 同款 B.5.1 纪律）。
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .time_utils import TimezoneManager

logger = logging.getLogger(__name__)

# ── 常量（定稿 §4.1.1 冻结数值）───────────────────────────────────────────
#: 表名登记（本装载方持有常量；写法照抄 planning/activity_profiles.PROFILES_FILE_NAME）
MODULATION_FILE_NAME = "emotion_life_modulation.json"
#: 表结构版本（读到不认识的版本 → 整表回退内置 + 告警）
TABLE_VERSION = 1
#: EMA 时间常数默认（分钟；定稿 §4.1.1「EMA 平滑（默认 15 min）」）
DEFAULT_SMOOTHING_MINUTES = 15.0
#: 中性死区默认（定稿 §4.1.1「中性死区 0.12」）
DEFAULT_DEAD_ZONE = 0.12
#: gain 全局夹取（定稿 §4.1.1「全部 gain 夹 [0.70,1.30]」）
DEFAULT_GAIN_MIN = 0.70
DEFAULT_GAIN_MAX = 1.30
#: 中性死区下限防御（配置成 0 及以下会让死区失效，钳一个极小正值保底）
_DEAD_ZONE_FLOOR = 1e-6

#: 内置系数表（= 定稿 §4.1.1 起点；``floor`` 为 None 表示该项无下限）。
#: ``quad_weight``/``quad_optimal`` 只在 focus 出现：(1 - w*(act-opt)^2) 乘线性项。
BUILTIN_COEFFS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "energy": {
        "rise": {"terms": {"low_mood": -0.25, "tension": -0.10}, "floor": 0.75},
        "fall": {"terms": {"activation": 0.25, "low_mood": 0.15}, "floor": None},
    },
    "hunger": {
        "rise": {"terms": {"low_mood": 0.20, "activation": -0.10}, "floor": None},
        "fall": {"terms": {"low_mood": -0.10, "activation": 0.05}, "floor": None},
    },
    "focus": {
        # 定稿 §4.1.1：focus 是容量，不区分方向——fall 缺省即沿用 rise（构造时补齐）
        "rise": {"quad_weight": 1.2, "quad_optimal": 0.35,
                 "terms": {"low_mood": -0.25}, "floor": 0.60},
    },
    "stress": {
        "rise": {"terms": {"tension": 0.35, "low_mood": 0.15}, "floor": None},
        "fall": {"terms": {"low_mood": -0.10}, "floor": 0.80},
    },
    "social_battery": {
        "rise": {"terms": {"low_mood": -0.15}, "floor": 0.80},
        "fall": {"terms": {"low_mood": 0.20, "tension": 0.15}, "floor": None},
    },
}
#: 五项状态名（与 life_state.STATE_FIELDS 同口径，本地声明避免装载期依赖）
MODULATED_STATES: Tuple[str, ...] = ("energy", "hunger", "focus", "stress", "social_battery")
#: 表顶层标量键（缺省/坏值回退内置；字段级容错）
_TABLE_SCALARS: Tuple[Tuple[str, float], ...] = (
    ("smoothing_minutes", DEFAULT_SMOOTHING_MINUTES),
    ("dead_zone", DEFAULT_DEAD_ZONE),
    ("gain_min", DEFAULT_GAIN_MIN),
    ("gain_max", DEFAULT_GAIN_MAX),
)


def _as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """宽松数值解析（bool/NaN 不算数值）；失败 → default。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    if number != number:  # NaN 防御
        return default
    return number


def _load_table(root_dir: Any) -> Dict[str, Any]:
    """读表层调制表（经 core.mapping_dir 种子链；只读表层，缺失/坏表 → {} = 全内置）。

    写法照抄 planning/activity_profiles.load_profiles 的补链次序（564-574）：
    表层缺失先 ``ensure_user_table``（旧根迁移优先、仍缺从出厂种子复制；解析错误
    不算缺失、绝不覆盖用户文件），仍缺按内置默认生效、不告警不落账（首装常态）。
    """
    if root_dir is None:
        return {}
    from core.mapping_dir import (  # 延迟导入：保持本模块可独立使用（与画像表同款）
        MappingReadError, ensure_user_table, mappings_path, read_json_text)

    path = mappings_path(Path(root_dir), MODULATION_FILE_NAME)
    try:
        present = path.exists()
    except OSError:
        present = False
    if not present:
        ensure_user_table(root_dir, MODULATION_FILE_NAME)
        try:
            present = path.exists()
        except OSError:
            present = False
    if not present:
        return {}
    try:
        raw = read_json_text(path)
    except MappingReadError as exc:
        # 红线：解析错误不算缺失，绝不重写表层文件——按内置默认处理，不阻塞
        logger.warning("情绪调制表解析失败（按内置默认系数处理）：%s", exc.detail)
        return {}
    if not isinstance(raw, dict):
        logger.warning("情绪调制表顶层结构非法（%s），按内置默认系数处理", path.name)
        return {}
    version = raw.get("version")
    if version is not None and version != TABLE_VERSION:
        logger.warning("情绪调制表 version 不认识（期望 %d，实得 %r），按内置默认系数处理",
                       TABLE_VERSION, version)
        return {}
    return raw


class LifeModulationService:
    """VAD → LifeState 五项速率增益的折算服务（0 LLM；进程内状态，不持久化）。

    Args:
        root_dir: 插件根目录（调制表经 ``<root>/mappings/`` 种子链装载；``None`` =
            测试桩/纯内置，跳过表装载）。
        timezone_manager: 时区真源（EMA 的 ``dt`` 用；缺省 Asia/Shanghai，与
            life_state / rolling_planner 同一条 :class:`~planning.time_utils.TimezoneManager`
            链，不手写本地时间换算）。
    """

    def __init__(self, *, root_dir: Any = None,
                 timezone_manager: Optional[TimezoneManager] = None) -> None:
        self._tz = timezone_manager or TimezoneManager("Asia/Shanghai")
        #: tzinfo 直取（TimezoneManager 内部字段；naive ``now`` 补 tzinfo 用）
        self._tzinfo = getattr(self._tz, "_tz", None)
        table = _load_table(root_dir)
        # 顶层标量：字段级容错（坏值回退内置；钳制防呆）
        smoothing = _as_float(table.get("smoothing_minutes"), DEFAULT_SMOOTHING_MINUTES)
        self._smoothing_minutes = max(0.1, float(smoothing))
        dead = _as_float(table.get("dead_zone"), DEFAULT_DEAD_ZONE)
        self._dead_zone = max(_DEAD_ZONE_FLOOR, abs(float(dead)))
        gain_min = _as_float(table.get("gain_min"), DEFAULT_GAIN_MIN)
        gain_max = _as_float(table.get("gain_max"), DEFAULT_GAIN_MAX)
        self._gain_min = min(float(gain_min), float(gain_max))
        self._gain_max = max(float(gain_min), float(gain_max))
        # 系数：字段级合并（表值合法才覆盖内置）；状态表兼容顶层直挂（A 形态）与
        # "gains" 包裹（B 形态=随包种子落码形态）；focus 的 fall 显式补成同 rise
        gains_table = table.get("gains") if isinstance(table.get("gains"), dict) else table
        self._coeffs: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for state in MODULATED_STATES:
            builtin_entry = BUILTIN_COEFFS[state]
            entry = gains_table.get(state) if isinstance(gains_table, dict) else None
            rise_spec = self._spec_from(entry.get("rise") if isinstance(entry, dict) else None,
                                        entry, builtin_entry.get("rise"), "rise_floor")
            if isinstance(entry, dict) and entry.get("fall_same_as_rise"):
                fall_spec = dict(rise_spec)  # 定稿 §4.1.1：focus 是容量，不区分方向
            else:
                fall_spec = self._spec_from(
                    entry.get("fall") if isinstance(entry, dict) else None, entry,
                    builtin_entry.get("fall") or builtin_entry.get("rise"), "fall_floor")
            self._coeffs[state] = {"rise": rise_spec, "fall": fall_spec}
        # EMA 状态（进程内；不持久化——定稿「重启从当前 VAD 重算」）
        self._ema: Optional[Dict[str, float]] = None
        self._last_at: Optional[datetime] = None

    # ── 表合并（字段级容错；A/B 两落表形态兼容）───────────────────────────
    #: spec 层保留键（内联 terms 提取时剔除；A=canonical / B=种子落码形态共有）
    _RESERVED_SPEC_KEYS = frozenset({
        "terms", "floor", "quad_weight", "quad_optimal",
        "activation_center", "activation_penalty", "fall_same_as_rise"})

    def _spec_from(self, raw_spec: Any, entry: Any,
                   builtin_spec: Optional[Dict[str, Any]], floor_key: str) -> Dict[str, Any]:
        """单方向 gain spec 解析（字段级回退内置）。

        兼容两种落表形态（数值语义完全同源=定稿 §4.1.1，仅键位不同）：

        - **A（canonical）**：``{"terms": {...}, "floor": 0.75, "quad_weight": ..,
          "quad_optimal": ..}``；
        - **B（随包种子 emotion_life_modulation.json 落码形态）**：terms 内联
          （``{"low_mood": -0.25, ...}``），floor 用条目级 ``rise_floor``/``fall_floor``，
          focus 二次项用 ``activation_penalty``/``activation_center``，
          ``fall_same_as_rise: true`` 表 fall 沿用 rise。
        """
        spec: Dict[str, Any] = dict(builtin_spec) if builtin_spec else {"terms": {}}
        spec["terms"] = dict(spec.get("terms") or {})
        if isinstance(raw_spec, dict):
            src_terms = raw_spec.get("terms")
            if isinstance(src_terms, dict):
                pairs = src_terms.items()
            else:  # B 形态：terms 内联（剔除保留键后其余数值键即系数）
                pairs = ((key, value) for key, value in raw_spec.items()
                         if key not in self._RESERVED_SPEC_KEYS)
            for key, value in pairs:
                parsed = _as_float(value)
                if parsed is not None:
                    spec["terms"][str(key)] = parsed
            for src_key, dst_key in (("floor", "floor"), ("quad_weight", "quad_weight"),
                                     ("quad_optimal", "quad_optimal")):
                parsed = _as_float(raw_spec.get(src_key))
                if parsed is not None:
                    spec[dst_key] = parsed
            # B 形态的 focus 二次项（写在 rise/fall 内）
            penalty = _as_float(raw_spec.get("activation_penalty"))
            if penalty is not None:
                spec["quad_weight"] = penalty
            center = _as_float(raw_spec.get("activation_center"))
            if center is not None:
                spec["quad_optimal"] = center
        if isinstance(entry, dict):  # B 形态的条目级 floor（rise_floor / fall_floor）
            floor = _as_float(entry.get(floor_key))
            if floor is not None:
                spec["floor"] = floor
        return spec

    # ── 时间助手（与 life_state._aware 同款降级链）─────────────────────────
    def _now(self, now: Optional[datetime]) -> datetime:
        dt = now or self._tz.get_now()
        if dt.tzinfo is None:
            tz = self._tzinfo
            if tz is None:
                tz = datetime.now().astimezone().tzinfo
                self._tzinfo = tz
            return dt.replace(tzinfo=tz)
        return dt

    # ── VAD 读取（dict / 对象两形态；逐维 clamp [-1,1] + NaN 防御）─────────
    @staticmethod
    def _read_vad(vad_snapshot: Any) -> Tuple[float, float, float]:
        def dim(name: str) -> float:
            value: Any = None
            if isinstance(vad_snapshot, dict):
                value = vad_snapshot.get(name)
            else:
                value = getattr(vad_snapshot, name, None)
            parsed = _as_float(value, 0.0)
            return max(-1.0, min(1.0, float(parsed)))

        return dim("valence"), dim("arousal"), dim("dominance")

    # ── EMA 平滑（时间常数 smoothing_minutes；首拍直接取当前值，不跳变）────
    def _smooth(self, v: float, a: float, d: float, now: datetime) -> Dict[str, float]:
        if self._ema is None:
            self._ema = {"v": v, "a": a, "d": d}
            self._last_at = now
            return dict(self._ema)
        try:
            seconds = (now - self._last_at).total_seconds() if self._last_at else 0.0
        except TypeError:  # naive/aware 混撞（防御性；_now 已归一，理论不可达）
            seconds = 0.0
        minutes = seconds / 60.0
        alpha = 1.0 - math.exp(-minutes / self._smoothing_minutes) if minutes > 0 else 0.0
        for key, value in (("v", v), ("a", a), ("d", d)):
            self._ema[key] = self._ema[key] + alpha * (value - self._ema[key])
        self._last_at = now
        return dict(self._ema)

    # ── 单项 gain（线性项 + 可选二次项，floor 后再全局夹取）────────────────
    def _gain_of(self, spec: Dict[str, Any], quantities: Dict[str, float]) -> float:
        gain = 1.0
        for name, coeff in (spec.get("terms") or {}).items():
            gain += float(coeff) * quantities.get(name, 0.0)
        quad_w = spec.get("quad_weight")
        if quad_w is not None:
            optimal = float(spec.get("quad_optimal") or 0.0)
            gain *= 1.0 - float(quad_w) * (quantities["activation"] - optimal) ** 2
        floor = spec.get("floor")
        if floor is not None:
            gain = max(float(floor), gain)
        return max(self._gain_min, min(self._gain_max, gain))

    # ── 对外接口（定稿 §4.1.1）─────────────────────────────────────────────
    def compute(self, vad_snapshot: Any, now: Optional[datetime] = None
                ) -> Dict[str, Dict[str, float]]:
        """生效 VAD 快照 → 五项 rise/fall gain（消费方 = ``LifeStateService.tick``）。

        ``vad_snapshot``：dict（``valence/arousal/dominance`` 键，即
        ``VADState.to_dict`` 形态）或同名字段对象（``VADState`` 实例原生兼容）；
        维度缺失/非法按 0.0（中性）。返回 ``{state: {"rise": float, "fall": float}}``
        （五项恒齐；gain 已含单项 floor 与 [0.70,1.30] 全局夹取）。任何异常由调用方
        兜底（plugin 编排层捕获 → 本拍 ``modulation=None`` 走旧行为）。
        """
        v, a, d = self._read_vad(vad_snapshot)
        now = self._now(now)
        ema = self._smooth(v, a, d, now)
        # 中性死区：|x| < dead_zone 的维按 0 处理（该维回中性，调制量随之取中性值）
        for key in ("v", "a", "d"):
            if abs(ema[key]) < self._dead_zone:
                ema[key] = 0.0
        v, a, d = ema["v"], ema["a"], ema["d"]
        quantities = {
            "low_mood": max(0.0, -v),
            "activation": max(0.0, min(1.0, (a + 1.0) / 2.0)),
            "tension": max(0.0, a) * max(0.0, -d),
        }
        return {state: {"rise": self._gain_of(self._coeffs[state]["rise"], quantities),
                        "fall": self._gain_of(self._coeffs[state]["fall"], quantities)}
                for state in MODULATED_STATES}

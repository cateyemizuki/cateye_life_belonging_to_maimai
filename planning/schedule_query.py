"""日程查询：当前活动、全天摘要（runtime-only；PR8 3.0.0 消费者语义收口）。

R7 依据 = 用户 2026-10-25 直派（日程系统滚动化改造，设计目录
D:/deepseek-harness_workspace/maibot-plugins/2026-10-25-日程系统滚动化改造设计）：
runtime provider 注入——构造参数或 :meth:`ScheduleQuery.set_runtime_provider` 注入
:class:`~planning.life_schedule.LifeScheduleReadModel`。

PR8 3.0.0（依据 = 用户 2026-10-27 授权直接清理 + 逐符号审定清单 W4，裁定 5/6）：
``find_current_activity`` 改 **runtime-only**——只读 runtime provider 的 episode
当前视图；provider 未就绪（未装配或 ``[schedule].enabled=false``）/ 当前空闲 /
读取异常一律如实返回 ``None``。旧时间窗读表路径（``_current_from_time_window``/
睡眠环回退 ``_in_sleep_ring``/``_warn_night_sleep_fallback``）与生产零调用的
``find_next_activities``（C-17）已整体删除：不再用计划时间伪造「正在做/已睡着/
接下来」；对外「日程未启用/暂不可用」语义由各消费者按 ``[schedule].enabled``
门控与空值如实呈现（规则 5/6），本查询不伪装空闲或清醒。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .schedule_db import ScheduleDB
from .time_utils import (describe_energy, format_day_minute, get_energy_level,
                         ring_minutes_ahead)
from core.time_utils import minutes_of_now

logger = logging.getLogger(__name__)

# sleep 类型条目不算「正在做的事」（本文件旧时间窗匹配删除后仅作对外口径常量保留：
# companion/components 以 _SCHEDULE_SKIP_TYPES 引用；runtime episode 视图的睡眠跳过
# 语义见 life_schedule.include_sleep）。
# A-21(a)（用户 2026-10-27 批）：夜间睡眠判定唯一真源 = schedule_items.is_night_sleep_item。
# D1（2026-11-01 模块解耦计划）：转公开常量——原私有名 _SKIP_TYPES 被外部当契约
# 引用（计划 §3.1 禁止私有符号跨模块），外部引用方 companion/components 已同步。
SKIP_TYPES = {"sleep"}


class ScheduleQuery:
    def __init__(self, db: ScheduleDB, cache_minutes: int = 15,
                 runtime_provider: Optional[Any] = None,
                 config_provider: Optional[Any] = None) -> None:
        self._db = db
        #: runtime provider（LifeScheduleReadModel；None = 未装配——R7 滚动化改造；
        #: PR8 3.0.0：``find_current_activity`` runtime-only，见模块 docstring）
        self._runtime_provider = runtime_provider
        #: 配置读取回调（PR8 3.0.0 后无内部消费者——睡眠环回退已随时间窗路径删除；
        #: 形参与赋值保留兼容既有装配 plugin.py:357-359 与测试桩）
        self._config_provider = config_provider
        # ``cache_minutes`` 形参保留兼容既有装配（plugin.py 传
        # schedule_advanced.inject_cache_minutes）；旧 15 分钟时间窗缓存随
        # ``_current_from_time_window`` 删除，查询已无缓存态。

    def set_runtime_provider(self, provider: Optional[Any]) -> None:
        """注入/更换 runtime provider（装配期调用；传 None 恢复未装配态）。"""
        self._runtime_provider = provider

    @staticmethod
    def _runtime_ready(provider: Any) -> bool:
        """provider 存在且 ``enabled``（经 WP1 映射 = ``[schedule].enabled`` 唯一总开关；
        enabled 属性缺失/异常 → False = 如实未就绪）。"""
        try:
            return provider is not None and bool(getattr(provider, "enabled", False))
        except Exception:
            return False

    @property
    def db(self) -> ScheduleDB:
        """底层日程库（只读访问器）。"""
        return self._db

    def get_day_schedule(self, date: str) -> List[Dict[str, Any]]:
        return self._db.get_day_schedule(date)

    def find_current_activity(self, date: str, now: Optional[datetime] = None,
                              include_sleep: bool = False) -> Optional[Dict[str, Any]]:
        """返回当前正在进行的日程条目（runtime-only；默认跳过 sleep 类型）。

        PR8 3.0.0（依据 = 用户 2026-10-27 授权直接清理 + 逐符号审定清单 W4，裁定 5/6）：
        只读 runtime provider 的 episode 当前视图——provider 未就绪 / 当前空闲 / 读取
        异常一律如实返回 ``None``，不回退计划时间窗（旧「编造正在做/睡眠环伪造已睡着」
        路径已删）。「日程未启用/暂不可用」的对外呈现由消费者按 ``[schedule].enabled``
        门控与空值如实给出（规则 6）。

        include_sleep=True 时 sleep episode 也参与返回（情绪睡眠链依赖，见
        life_schedule include_sleep 语义）。runtime 结果不缓存（episode 由 5 分钟
        tick 推进，缓存会引入滞后矛盾）。``date`` 形参保留兼容既有调用方签名
        （runtime 视图按 provider 自身的时区/日期帧取数，本方法不再使用）。
        """
        return self.runtime_current(now, include_sleep=include_sleep)

    def runtime_current(self, now: Optional[datetime] = None,
                        include_sleep: bool = False) -> Optional[Dict[str, Any]]:
        """runtime provider 当前活动（公开访问器；未就绪/空闲/异常 → ``None``）。

        PR8 3.0.0 新增（W4）：消费者（proactive/emotion）经此读 runtime episode，
        与注入同口径，不再触碰 ``_runtime_provider`` 私有属性。
        """
        provider = self._runtime_provider
        if not self._runtime_ready(provider):
            return None  # 日程未启用/未装配：如实未就绪（不伪装空闲或清醒）
        try:
            return provider.current(now, include_sleep=include_sleep)
        except Exception as exc:
            logger.warning("runtime provider 读取当前活动失败（如实返回 None）：%s", exc)
            return None

    @staticmethod
    def _previous_date(date: str) -> Optional[str]:
        """``YYYY-MM-DD`` → 前一天（解析失败返回 None，调用方跳过跨日合并）。"""
        try:
            return (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            return None

    def format_activity_line(self, item: Dict[str, Any], now: Optional[datetime] = None) -> str:
        """当前活动的一行描述（含剩余时间与情绪/能量提示），注入与 Tool 共用。

        时间窗一律经 ``time_utils.format_day_minute`` 渲染：跨午夜条目 ``end>1440``
        显示为「23:00-次日 07:00」（不再是裸 ``//60`` 的 31:00）。
        """
        now = now or datetime.now()
        start = int(item.get("start_minute") or 0)
        end = int(item.get("end_minute") or 0)
        # 剩余时间按同一个 24h 环归一：跨午夜条目（end>1440）在次日凌晨（如 06:50）也能
        # 得到真实的「约还剩 N 分钟」，不再是 1830 这类帧外大数（裸 end - cur 的旧缺陷）
        remain = ring_minutes_ahead(end, minutes_of_now(now))
        line = f"{item.get('name', '???')}"
        if item.get("description"):
            line += f"（{item['description']}）"
        line += f" [{format_day_minute(start)}-{format_day_minute(end)}]"
        if 0 < remain <= 15:
            line += f"，约还剩 {remain} 分钟"
        energy = describe_energy(get_energy_level(now.hour))
        return f"{line}｜精神状态：{energy}"

    def format_day_summary(self, date: str) -> str:
        """全天日程逐条摘要（时间窗同样经 ``format_day_minute`` 渲染，跨午夜标「次日」）。"""
        items = self._db.get_day_schedule(date)
        if not items:
            return f"{date}：今天还没有安排日程"
        lines = [f"{date} 的日程（共 {len(items)} 项）："]
        for idx, item in enumerate(items, 1):
            start, end = int(item.get("start_minute") or 0), int(item.get("end_minute") or 0)
            lines.append(f"{idx}. [{format_day_minute(start)}-{format_day_minute(end)}] "
                         f"{item.get('name', '???')}｜{item.get('goal_type', '')}")
            if item.get("description"):
                lines.append(f"   └ {item['description']}")
        return "\n".join(lines)

    def next_day_preview(self, date: str, days: int = 1) -> List[Tuple[str, str]]:
        """明天/未来几天的首个活动预览（注入用，骨架版只看明天第一条）。"""
        result = []
        base = datetime.strptime(date, "%Y-%m-%d")
        for offset in range(1, days + 1):
            d = (base + timedelta(days=offset)).strftime("%Y-%m-%d")
            items = self._db.get_day_schedule(d)
            if items:
                first = items[0]
                start = int(first.get("start_minute") or 0)
                result.append((d, f"{format_day_minute(start)} {first.get('name', '')}"))
        return result

"""组合根只读具名视图（2026-11-02 质量清单 P1-01 起步）。

业务模块过去通过 ``getattr(plugin, "_planning", None)`` 这类字符串契约取组合根
状态：重命名/删除不会触发编译错误，只在运行期静默降级。本类把高频键收成具名
只读属性，作为新代码与渐进迁移的唯一入口。

设计约束：
- **不复制状态**：每次访问都从插件实例实时读取，热重载/晚装配语义不变；
- **缺失按 None**：未装配能力返回 None，与既有 ``getattr(..., None)`` 降级口径一致；
- **不导入业务模块**：只做属性转发，避免形成新的依赖环。

迁移节奏：先由结构守卫把现有 ``getattr(plugin, "_xxx")`` 键钉进
``core/contracts.PLUGIN_ATTR_CONTRACT``，再逐模块改用本视图；本批迁移
``planning/services.py`` 作为样板，其余模块保持行为不变。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from core.contracts import PLUGIN_ATTR_CONTRACT

logger = logging.getLogger(__name__)


class PluginContext:
    """组合根 ``plugin`` 的只读具名视图（晚绑定，不持有状态副本）。"""

    __slots__ = ("_plugin",)

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin

    def get(self, name: str, default: Any = None) -> Any:
        """按契约名（可带/不带前导下划线）读取，缺失回退 ``default``。"""
        key = "_" + str(name).lstrip("_")
        if key not in PLUGIN_ATTR_CONTRACT:
            # F7（2026-11-04）：白名单外的键留 debug 线索（漂移可观测）；
            # 不改变既有 getattr 回退 default 的降级语义。
            logger.debug("PluginContext.get(%r) 不在 PLUGIN_ATTR_CONTRACT 白名单内", key)
        return getattr(self._plugin, key, default)

    @property
    def planning(self) -> Any:
        return self.get("planning")

    @property
    def companion(self) -> Any:
        return self.get("companion")

    @property
    def qzone(self) -> Any:
        return self.get("qzone")

    @property
    def qzone_video(self) -> Any:
        return self.get("qzone_video")

    @property
    def video_watch(self) -> Any:
        # PR-ENTERTAINMENT（3.17.13）：娱乐面板最近观看只读投影消费；
        # 缺失按 None（未装配降级语义不变）。
        return self.get("video_watch")

    @property
    def emotion(self) -> Any:
        return self.get("emotion")

    @property
    def decision_logger(self) -> Any:
        return self.get("decision_logger")

    @property
    def data_dir(self) -> Any:
        return self.get("data_dir")

    @property
    def db(self) -> Any:
        return self.get("db")

    @property
    def llm(self) -> Any:
        return self.get("llm")

    @property
    def llm_logger(self) -> Any:
        return self.get("llm_logger")

    @property
    def bus(self) -> Any:
        return self.get("bus")

    @property
    def sleep_engine(self) -> Any:
        return self.get("sleep_engine")

    @property
    def schedule_query(self) -> Any:
        return self.get("schedule_query")

    @property
    def schedule_gen(self) -> Any:
        return self.get("schedule_gen")

    @property
    def life_state(self) -> Any:
        return self.get("life_state")

    @property
    def life_model(self) -> Any:
        return self.get("life_model")

    @property
    def affection(self) -> Any:
        return self.get("affection")

    @property
    def lover_name(self) -> Any:
        return self.get("lover_name")

    @property
    def personality(self) -> Any:
        return self.get("personality")

    @property
    def stream_id(self) -> Any:
        # F4（B3）：目标陪伴私聊 stream_id，供社交边界判目标私聊。
        return self.get("stream_id")

    @property
    def plugin_root(self) -> Any:
        return self.get("plugin_root")

    @property
    def attention(self) -> Any:
        return self.get("attention")

    @property
    def group_attention(self) -> Any:
        # F1（B1）：群注意力衰减状态仓库/乘数入口（进程内）；缺失按 None 旧行为。
        return self.get("group_attention")

    @property
    def screen_perception(self) -> Any:
        # PR-LINKAGE-SCREEN（3.17.16）：屏幕感知联动服务（适配器 cateye_screen）；
        # 缺失按 None（未装配降级语义不变，patrol/问候链不阻塞触发）。
        return self.get("screen_perception")

    @property
    def bpp_bridge(self) -> Any:
        # R1（结构审计批）：life→bpp 跨插件联动适配器（cateye_bpp）具名视图；
        # 运行期消费方经 linkage.get_adapter("cateye_bpp") 查询注册表，本视图
        # 供诊断/面板只读；缺失按 None（未装配降级语义不变，fail-open）。
        return self.get("bpp_bridge")

    @property
    def activity_preferences(self) -> Any:
        # P3（3.17.27）：日程偏好服务；缺失按 None（未装配 → 面板偏好标签不显示，
        # 纯读降级，不阻塞池投影）。
        return self.get("activity_preferences")

    # 公开属性（无前导下划线）与宿主门面
    @property
    def config(self) -> Any:
        return getattr(self._plugin, "config", None)

    @property
    def logger(self) -> Any:
        return getattr(self._plugin, "logger", None)

    @property
    def ctx(self) -> Optional[Any]:
        return getattr(self._plugin, "ctx", None)

    @property
    def sleep_asleep(self) -> Any:
        return getattr(self._plugin, "sleep_asleep", None)

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"PluginContext({type(self._plugin).__name__})"

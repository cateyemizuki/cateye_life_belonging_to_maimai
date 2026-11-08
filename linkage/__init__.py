"""跨插件联动层（PR-LINKAGE-SCREEN，3.17.16）。

本包是「属于麦麦的生活」与其他插件/外部能力联动的统一登记处（战未来：
后续任何跨插件联动适配器都在此登记，消费方经注册表查询，不再各自硬接线）。

层间纪律（与 ``core`` 单向纪律同源，boundaries 门禁守卫）：

- ``core`` 不 import ``linkage``（core 是基础设施，方向单向）；
- ``linkage`` 可 import ``core``（基础设施）与 ``suite_prompts``/``suite_config``
  等顶层共享面；
- 业务模块（``companion``/``planning`` 等）import ``linkage`` 合规——联动层位于
  业务之下、core 之上，业务消费联动适配器，联动层不反向持有业务服务束。

首个适配器 ``cateye_screen``（:class:`linkage.screen.service.ScreenPerceptionService`）：
陪伴对象电脑屏幕感知（peek + 旁白模板渲染 + 注入缓存发布/丢弃），由
``companion/cateye_client.py`` 与 patrol 的旁白逻辑抽取迁移而来。

注册表为模块级单例：适配器在组合根装配时 :func:`register_adapter` 登记，
消费方经 :func:`get_adapter` / :func:`list_adapters` 查询。注册表只持有适配器
引用，不复制状态；缺失按 None 降级（与既有可空降级语义一致）。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class LinkageAdapter(Protocol):
    """跨插件联动适配器的最小契约。

    实现方声明唯一 ``name``（注册表键），并提供可用性判定与人类可读描述。
    适配器自身的业务能力（如屏幕感知的 publish/discard）由具体类扩展，
    本协议只约束登记/查询所需的最小面。
    """

    #: 适配器唯一名（注册表键；如 ``cateye_screen``）
    name: str

    def available(self) -> bool:
        """当前是否可用（依赖装配齐全且能力就绪）；不可用时消费方降级。"""
        ...

    def describe(self) -> str:
        """人类可读描述（诊断/面板展示用；不含敏感内容）。"""
        ...


#: 模块级注册表单例（name → adapter）。组合根装配时登记，消费方只读查询。
_REGISTRY: Dict[str, LinkageAdapter] = {}


def register_adapter(adapter: LinkageAdapter) -> LinkageAdapter:
    """登记一个联动适配器（按 ``adapter.name`` 覆盖同名旧实例）；返回入参便于链式装配。"""
    name = str(getattr(adapter, "name", "") or "").strip()
    if not name:
        raise ValueError("联动适配器必须声明非空 name")
    _REGISTRY[name] = adapter
    return adapter


def unregister_adapter(name: str) -> None:
    """移除一个适配器（幂等；装配拆卸/测试隔离用）。"""
    _REGISTRY.pop(str(name or "").strip(), None)


def get_adapter(name: str) -> Optional[LinkageAdapter]:
    """按名取适配器；未登记返回 None（消费方按可空降级语义处理）。"""
    return _REGISTRY.get(str(name or "").strip())


def list_adapters() -> List[LinkageAdapter]:
    """返回当前已登记适配器的快照列表（登记序；只读副本，改动不影响注册表）。"""
    return list(_REGISTRY.values())


__all__ = [
    "LinkageAdapter",
    "register_adapter",
    "unregister_adapter",
    "get_adapter",
    "list_adapters",
]
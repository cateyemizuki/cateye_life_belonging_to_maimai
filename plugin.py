"""属于麦麦的生活（lbm）— 整合插件组合根。

三个业务子模块（各自拥有 config / services / components）：
- planning/   自主规划复刻：日程生成（多轮+评分）/ SQLite 目标库 / planner+replyer 注入 /
  活动切换主动开口 / /plan 命令 / get_current_activity API
- companion/  虚拟陪伴复刻：好感度 / 巡检主动聊天（早安晚安想念）/ 屏幕感知 /
  陪伴对象电脑联动 / 节假日 / /mai_* 命令
- qzone/      QQ 空间：按日程发说说（双通道）/ 自动回评（Web CGI 直连，含楼中楼）/ /life 命令
- emotion/    情绪维护系统：VAD 三维状态机（日常增量+指数衰减）/ 睡眠裁判大修 / 人设初始化 / 情绪底色注入

本文件只做生命周期与装配：创建服务束、挂到 self._planning / self._companion /
self._qzone，组件注册由三个 components.py 的 Mixin 提供（多继承收集）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, ClassVar, Iterable

# ── 宿主运行时导入引导（勿删）──────────────────────────────────────────
# 宿主 Runner 用 spec_from_file_location 加载本文件，只把「plugins 根目录」和宿主
# 「src」临时放进 sys.path，从不加入本插件目录（src/plugin_runtime/runner/
# plugin_loader.py:568-584，2026-10-14 对照 MaiBot-main 宿主源码复核订正）；
# 本插件又用绝对导入引用自身模块，故必须自行把插件
# 目录放到 sys.path 首位，否则 companion/core/planning 等会被宿主 src/ 下同名模块
# 抢先命中（core/config 在宿主 src 下均存在）。配置模块命名为 suite_config 而非
# config：同进程可加载多个插件，顶层 'config' 会被宿主 src/config 或先加载的兄弟
# 插件占住 sys.modules。
import os as _os
import sys as _sys

_PLUGIN_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _PLUGIN_DIR in _sys.path:
    _sys.path.remove(_PLUGIN_DIR)
_sys.path.insert(0, _PLUGIN_DIR)

import copy

from maibot_sdk import MaiBotPlugin
from planning.dashboard_components import LifeDashboardComponentsMixin

from companion.affection import AffectionManager
from companion.components import CompanionComponentsMixin
from companion.config import target_qq_of
from companion.harass_report import HARASS_TOOL_NAME
from companion.services import CompanionServices
from suite_config import (
    CURRENT_LAYOUT_VERSION,
    LEGACY_PROACTIVE_KEYS,
    LEGACY_ROUTE_KEYS,
    LEGACY_SECTIONS,
    SUPPORTED_CONFIG_VERSION,
    CateyeSuiteConfig,
    factory_defaults,
)
from core.diag import DecisionLogger, LLMCallLogger
from core.event_bus import EventBus, install_proactive_budget
from core.llm_utils import LLMService, llm_task_name_of
from emotion.components import EmotionComponentsMixin
from emotion.services import EmotionServices
from planning.components import PlanningComponentsMixin
from planning.life_modulation import LifeModulationService
from planning.life_schedule import LifeScheduleReadModel
from planning.life_state import LifeStateService
from planning.sleep_engine import NO_HARD_COMMITMENT, SleepEngine
from planning.sleep_schedule import SleepEndpointHook
from planning.rolling_planner import RollingPlanner
from planning.schedule_db import ScheduleDB
from planning.schedule_query import ScheduleQuery
from planning.services import PlanningServices
from qzone.cgi import QzoneCookieManager
from qzone.components import QzoneComponentsMixin
from qzone.service import QzoneService

# ── 磁盘旧文件抢救（2026-10-10 升级的迁移保障，详见 normalize_plugin_config）──────
#: 「键不存在」哨兵（区分"默认值是 None"与"没有默认值可参照"）
_MISSING = object()
#: 磁盘配置文件名（宿主 Runner 固定用这个名字，见 runner_main._save_plugin_config）
_CONFIG_FILE_NAME = "config.toml"
#: 调制采用观测事件（附录 B.6.1；有界内部事件，不新增消息/模型调用/路径键）
EVENT_EMOTION_MODULATION_APPLIED = "life.emotion_modulation_applied"


def _read_config_file(path: str) -> dict:
    """读磁盘 ``config.toml``：``tomllib``（3.11+）优先，失败回退宿主自带的 ``tomlkit``。

    任何异常（文件不存在/无权限/解析失败）→ 返回 ``{}``：抢救失败绝不阻塞宿主。
    """
    try:
        import tomllib  # type: ignore[import-not-found]

        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    try:
        import tomlkit  # 宿主依赖（Runner 用它写回配置）

        doc = tomlkit.parse(open(path, "r", encoding="utf-8").read())
        unwrapped = doc.unwrap() if hasattr(doc, "unwrap") else doc
        return dict(unwrapped) if hasattr(unwrapped, "items") else {}
    except Exception:
        return {}


def _type_compatible(value: object, reference: object) -> bool:
    """旧值是否与「新键出厂默认值」类型兼容（对齐宿主 rebuild 的保留规则）。

    ``reference`` 为哨兵/None 时不设限；bool 与 int 严格区分（避免 True 混进数字字段）。
    """
    if reference is _MISSING or reference is None:
        return True
    if isinstance(reference, bool):
        return isinstance(value, bool)
    if isinstance(reference, int):
        # 与宿主 _is_plugin_config_value_type_compatible 逐字对齐：int 字段只收 int
        # （放行 float 会在落盘校验期抛 ValidationError，而宿主激活路径 suppress_errors=False
        #  → 整个插件激活失败，比"丢一个用户值"严重得多；T3 审计 #12）
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(reference, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(reference, str):
        return isinstance(value, str)
    if isinstance(reference, list):
        return isinstance(value, list)
    if isinstance(reference, dict):
        return isinstance(value, dict)
    return True


def _merge_disk_values(incoming: dict, disk: dict, defaults: dict) -> tuple[dict, bool]:
    """把磁盘旧配置里的用户值并回「已被宿主按新骨架重建」的输入（纯函数，离线可测）。

    - **被删的 5 个旧段**（[target]/[cateye]/[judge]/[inject]/[planning_cleanup]）整段带回，
      交给聚合迁移器搬运；
    - **存活段**逐键：输入缺失 → 用磁盘值；磁盘值偏离出厂默认 且 输入仍是出厂默认
      → 判为用户配过（宿主重建可能已把它降级为默认），用磁盘值；
    - 值类型与出厂默认不兼容时**不搬**（宿主 rebuild 正是按类型兼容性保留字段的，
      照搬会在校验期炸掉整份配置）；``[plugin]`` 的 config_version/layout_version 不动。

    返回 ``(合并结果, 是否发生变化)``；不修改入参。
    """
    out: dict = copy.deepcopy(incoming)
    changed = False

    for name in LEGACY_SECTIONS:
        if isinstance(disk.get(name), dict) and name not in out:
            out[name] = copy.deepcopy(disk[name])
            changed = True

    for section, disk_section in disk.items():
        if section in LEGACY_SECTIONS or not isinstance(disk_section, dict):
            continue
        default_section = defaults.get(section) if isinstance(defaults, dict) else None
        if not isinstance(default_section, dict):
            default_section = {}
        target_section = out.get(section)
        if not isinstance(target_section, dict):
            out[section] = copy.deepcopy(disk_section)
            changed = True
            continue
        for key, value in disk_section.items():
            if section == "plugin" and key in ("config_version", "layout_version"):
                continue
            reference = default_section.get(key, _MISSING)
            if not _type_compatible(value, reference):
                continue
            if key not in target_section:
                target_section[key] = copy.deepcopy(value)
                changed = True
            elif (reference is not _MISSING and value != reference
                  and target_section.get(key) == reference):
                target_section[key] = copy.deepcopy(value)
                changed = True
    return out, changed


def _rescue_config_from_disk(config_data: object) -> tuple[object, bool]:
    """宿主归一化的前置抢救：把尚未被覆写的磁盘 config.toml 里的用户值并回输入。

    只在「输入与磁盘都还没带本轮布局标记」时执行（标记 = ``[plugin] layout_version``，
    由迁移器写进落盘结果）。任何异常 → 原样返回输入。
    """
    try:
        if not isinstance(config_data, dict) or not config_data:
            return config_data, False
        incoming_plugin = config_data.get("plugin")
        if isinstance(incoming_plugin, dict) and \
                str(incoming_plugin.get("layout_version") or "") == CURRENT_LAYOUT_VERSION:
            return config_data, False
        disk = _read_config_file(_os.path.join(_PLUGIN_DIR, _CONFIG_FILE_NAME))
        if not disk:
            return config_data, False
        disk_plugin = disk.get("plugin")
        if isinstance(disk_plugin, dict) and \
                str(disk_plugin.get("layout_version") or "") == CURRENT_LAYOUT_VERSION:
            return config_data, False
        return _merge_disk_values(config_data, disk, factory_defaults())
    except Exception:
        return config_data, False


class CateyeSuitePlugin(LifeDashboardComponentsMixin, PlanningComponentsMixin, CompanionComponentsMixin, QzoneComponentsMixin,
                        EmotionComponentsMixin, MaiBotPlugin):
    config_model: ClassVar[type] = CateyeSuiteConfig
    # 订阅宿主 bot 配置热重载（人设/昵称变化自动同步）
    config_reload_subscriptions: ClassVar[Iterable[str]] = ("bot",)

    # ------------------------------------------------------------------
    # 组件注册面（2026-10-24 用户直派，需求3：告状工具「关闭不注册」）
    # ------------------------------------------------------------------
    def get_components(self):
        """``[admin].harass_report_enabled=false`` 时不注册 ``lbm_report_harassment``。

        super().get_components() 收集全部装饰组件后按名剔除（dict / 对象列表两种
        收集形态都兼容，与宿主收集语义一致；离线 sdk_stub.get_components 返回
        ``{attr: info}`` 同走 dict 分支）。配置不可读（如冒烟在 on_load 前收集）
        → 按默认值 True 处理、不误删组件。返回形态与 super() 一致（dict 进 dict 出）。
        """
        components = super().get_components()
        try:
            enabled = bool(self.config.admin.harass_report_enabled)
        except Exception:
            enabled = True
        if enabled:
            return components
        if isinstance(components, dict):
            return {attr: info for attr, info in components.items()
                    if str((info or {}).get("name") or "") != HARASS_TOOL_NAME}
        if isinstance(components, (list, tuple)):
            kept = []
            for comp in components:
                name = comp.get("name") if isinstance(comp, dict) else getattr(comp, "name", None)
                if str(name or "") != HARASS_TOOL_NAME:
                    kept.append(comp)
            return type(components)(kept) if isinstance(components, tuple) else kept
        return components

    # ------------------------------------------------------------------
    # 配置归一化（含「磁盘旧文件抢救」——版本升级迁移保障，T1 自 1.1.0 起建立）
    # ------------------------------------------------------------------
    def normalize_plugin_config(self, config_data: Any) -> tuple[dict[str, Any], bool]:
        """宿主 Runner 的配置归一化入口（SDK 基类同名方法的包装）。

        **为什么需要它**：``config_version`` 递增（红线 R6：与插件版本同步）后，宿主在
        「配置版本号变化」时会**先**按新默认骨架重建配置、只保留新旧同名的键——跨段旧键
        （``[target]``/``[cateye]``/``[judge]``/``[inject]``/``[planning_cleanup]``）与改名键
        （``miss_hours_min`` → ``miss_silence_hours`` 等）在插件**看到它们之前**就没了。
        本方法在这一刻读**尚未被宿主覆写**的磁盘 ``config.toml``，把用户值并回输入，再交给
        SDK 基类（merge 默认 → 校验 → 聚合迁移器搬运 → 宿主按 ``should_persist`` 落盘）。
        迁移因此仍然只发生一次：落盘结果带 ``[plugin] layout_version`` 标记，下次加载即跳过。

        磁盘路径取模块级 :data:`_PLUGIN_DIR`（便于离线用例替换成临时目录）；
        tomllib 缺失时回退宿主自带的 tomlkit；任何异常 → 原样返回输入（抢救失败不阻塞宿主）。
        """
        # 2026-10-27 用户裁定：规范 max_generated_events 显式值（含 8）优先于旧
        # max_events；必须在本方法即将调用的 SDK「merge 出厂默认 → 校验」之前，对
        # **原始输入**先搬迁旧键，否则默认 8 一旦 merge 进来就无法再靠值判断显式性。
        incoming = config_data
        try:
            from planning.event_catalog_config import migrate_legacy_event_catalog

            def _migrate_ec(payload: Any) -> Any:
                if not isinstance(payload, dict) or not isinstance(payload.get("event_catalog"), dict):
                    return payload
                before = payload["event_catalog"]
                after = migrate_legacy_event_catalog(before)
                if after is before:
                    return payload
                out = dict(payload)
                out["event_catalog"] = after
                return out

            incoming = _migrate_ec(incoming)
        except Exception:
            pass  # 迁移失败不阻塞宿主；段级校验器仍会再尝试一次
        # PR8 3.0.0（R7 依据 = 用户 2026-10-27 授权直接清理 + 逐符号审定清单 WP2 落点 B）：
        # 退役路线/运行期开关键（:data:`LEGACY_ROUTE_KEYS` + nap 两键）的 raw 前置 pop，
        # 仿 ``_migrate_ec`` 先例对 **原始输入与磁盘抢救结果各做一次**——宿主重建+抢救会把
        # 退役键带回，必须在 SDK「merge 出厂默认 → 校验」之前清掉。纯 pop、语义与聚合校验器
        # 入口 pop（落点 A）一致；**绝不写 [schedule].enabled**（裁定 2：显式 false 保持
        # false，缺省由出厂默认 True 承担）。
        def _pop_retired_keys(payload: Any) -> Any:
            if not isinstance(payload, dict):
                return payload
            out = dict(payload)
            changed = False
            for section_map in (LEGACY_ROUTE_KEYS, LEGACY_PROACTIVE_KEYS):
                for section_name, stale_keys in section_map.items():
                    section_data = out.get(section_name)
                    if not isinstance(section_data, dict):
                        continue
                    cleaned = dict(section_data)
                    for stale_key in stale_keys:
                        if cleaned.pop(stale_key, None) is not None:
                            changed = True
                    out[section_name] = cleaned
            return out if changed else payload

        incoming = _pop_retired_keys(incoming)
        rescued, rescued_changed = _rescue_config_from_disk(incoming)
        try:
            migrated_rescued = _migrate_ec(rescued)
            if migrated_rescued is not rescued:
                rescued = migrated_rescued
                rescued_changed = True
        except Exception:
            pass
        rescued_popped = _pop_retired_keys(rescued)
        if rescued_popped is not rescued:
            rescued = rescued_popped
            rescued_changed = True
        parent = getattr(super(), "normalize_plugin_config", None)
        if callable(parent):  # 宿主/SDK 环境：走官方 merge → 校验 → 迁移
            normalized, changed = parent(rescued)
            original = config_data if isinstance(config_data, dict) else {}
            return normalized, bool(changed or rescued_changed or normalized != original)
        # 无 SDK 基类实现（离线 stub）：按 SDK 文档语义就地补齐 + 校验，保持与宿主同形
        try:
            dumped = CateyeSuiteConfig.model_validate(rescued).model_dump(mode="python")
            original = config_data if isinstance(config_data, dict) else {}
            return dumped, bool(rescued_changed or dumped != original)
        except Exception:
            return rescued, rescued_changed

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def on_load(self) -> None:
        data_dir = self.ctx.paths.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self._data_dir = data_dir
        # F4（2026-10-11）：插件根目录（映射表/情绪描述根文件所在；测试桩无此属性 → 走 M12 原路径）
        self._plugin_root = _PLUGIN_DIR

        cfg = self.config
        # 公共基础设施（三个服务束共享的实例，缺省时它们会自建兜底）
        self._decision_logger = DecisionLogger(data_dir, enabled=cfg.admin.decision_log_enabled,
                                               retention_days=cfg.admin.log_retention_days)
        self._llm_logger = LLMCallLogger(data_dir, enabled=cfg.admin.llm_log_enabled,
                                         retention_days=cfg.admin.log_retention_days)
        self._db = ScheduleDB(data_dir / "schedule.db")
        # 好感度：运行期动态读 [affection] 段（热重载即时生效）+ 决策日志留痕；
        # 预设表/描述表由 CompanionServices 装配时装载（mappings/，只读一遍）
        self._affection = AffectionManager(
            data_dir, config_provider=lambda: self.config, decision_logger=self._decision_logger,
        )
        # config_provider：judge() 等在调用时动态读顶层配置（[judge] 段热更新即时生效）
        # 任务名统一读 [llm].task_name（2026-10-10 起 LLM 调用参数集中在 [llm] 段）
        self._llm = LLMService(self.ctx, task_name=llm_task_name_of(cfg),
                               call_logger=self._llm_logger, config_provider=lambda: self.config)
        # 缓存窗口在 [schedule_advanced]（2026-10-10 段拆分）
        # config_provider：PR1 P0 修复（2026-10-26），计划书 §三十四 / 定稿 §2 第 0.5 项——
        # runtime 空闲回退睡眠条目的特判需读 [schedule] 作息锚点；动态读配置，热重载即时生效
        self._schedule_query = ScheduleQuery(
            self._db, cache_minutes=cfg.schedule_advanced.inject_cache_minutes,
            config_provider=lambda: self.config)

        # 进程内事件总线（M0，MAINTENANCE_PLAN §三）：先于服务束构造（各服务束自读 plugin._bus），
        # 预算计数器在装配时装入（先于服务束启动；M0 实际订阅者仅它）
        self._bus = EventBus(self._decision_logger)
        # A2：预算计数落盘 data_dir（proactive_budget.json），跨 file_watcher 热重载连续
        # M11（附录 II §3.3）：预算只统计发往目标陪伴私聊的主动打扰——注入 self._stream_id
        # 取值回调（G11 权威来源，随 _apply_target/热重载更新；不新增持久化状态）
        self._proactive_budget = install_proactive_budget(
            self._bus, config_provider=lambda: self.config, decision_logger=self._decision_logger,
            data_dir=data_dir,
            target_stream_provider=lambda: getattr(self, "_stream_id", ""))

        # 服务束（内部动态读 self.config，热重载自动生效）
        self._planning = PlanningServices(self)
        self._companion = CompanionServices(self)
        self._qzone_cookies = QzoneCookieManager(
            self.ctx, data_dir,
            napcat_http_host=cfg.qzone.napcat_http_host,
            napcat_http_port=cfg.qzone.napcat_http_port,
            napcat_http_token=cfg.qzone.napcat_http_token,
        )
        # 第二实参为配置实例（服务读 .qzone 段）；另显式传 plugin：bus/emotion 动态属性与
        # 热重载配置（plugin.config 每次重取，SDK 热重载会整体替换配置实例）都挂在插件对象上
        self._qzone = QzoneService(self.ctx, self.config, self._schedule_query, self._llm,
                                   self._decision_logger, plugin=self,
                                   cookie_manager=self._qzone_cookies)
        self._qzone.bind_state_dir(data_dir)
        self._emotion = EmotionServices(self)

        # 宿主人设（bot 配置）
        await self._refresh_persona()

        self._stream_id = ""
        self._bg_started = False
        self._bg_tasks: list[asyncio.Task] = []
        # 生活状态服务束（R7 滚动化改造）：默认全 None（[schedule].enabled=false 时零实例化零开销）
        self._life_state: LifeStateService | None = None
        self._rolling_planner: RollingPlanner | None = None
        self._life_model: LifeScheduleReadModel | None = None
        # 情绪调制服务（PR2，2026-10-26，定稿 §4.1.1）：只在 [life_state].
        # emotion_modulation_enabled=true 时才构造（惰性，见 _ensure_life_modulation）
        self._life_modulation: LifeModulationService | None = None
        # 睡眠引擎（PR3，2026-10-26；随 [schedule].enabled 唯一总开关装配/停摆）
        self._sleep_engine: SleepEngine | None = None
        # 内部重检通知（睡眠 loop 安全唤醒；日程/约定写入共同口触达，见 request_life_recheck）
        self._life_recheck_event: asyncio.Event | None = None
        self._life_tick_loop_ref: Any = None
        self._life_tick_task: asyncio.Task | None = None
        self._life_cleanup_date = ""
        # PR6（2026-10-27）：目标频率校准的单飞 task 与「无 loop 时挂起」标记
        self._frequency_calibration_task: Any = None
        self._frequency_calibration_pending = False
        if not cfg.plugin.enabled:
            self.ctx.logger.info("插件未启用（plugin.enabled=false），仅加载组件")
            return

        # 目标私聊 stream 解析（失败则后台重试）
        self._stream_id = await self._resolve_target_stream()
        self._apply_target(target_qq_of(cfg), self._stream_id)
        # 生活状态装配（R7 滚动化改造，2026-10-25 用户直派；PR8 3.0.0 起门控 =
        # [schedule].enabled 唯一总开关）：false 时不实例化任何新服务；失败告警回退、
        # 绝不拖挂插件加载（_setup_life_services 内保底）
        self._setup_life_services()
        await self._start_bg()
        self.ctx.logger.info("属于麦麦的生活已加载 v%s（目标=%s，stream=%s）",
                             SUPPORTED_CONFIG_VERSION, target_qq_of(cfg) or "未配置",
                             self._stream_id or "待解析")

    async def on_unload(self) -> None:
        await self._qzone.stop()
        # PR6：取消未完成的频率校准 task（不留悬挂协程）
        try:
            await self._stop_target_frequency_calibration()
        except Exception:
            pass
        try:
            await self._emotion.stop()
        except Exception:
            pass
        try:
            await self._companion.stop()
        except Exception:
            pass
        try:
            await self._planning.stop()
        except Exception:
            pass
        if getattr(self, "_stop_event", None) is not None:
            self._stop_event.set()
        for task in getattr(self, "_bg_tasks", []):
            task.cancel()
        for task in getattr(self, "_bg_tasks", []):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._bg_started = False
        # 生活状态停机（R7 滚动化改造）：停 tick 循环并落盘 life_state.json（先于 db.close）
        try:
            await self._stop_life_tick_task()
        except Exception:
            pass
        self._stop_life_services()
        self._affection.flush()
        self._db.close()
        self.ctx.logger.info("属于麦麦的生活已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope == "bot":
            await self._refresh_persona()
            return
        if scope != "self":
            return
        cfg = self.config
        self._llm.update_task(llm_task_name_of(cfg))
        self._companion.sync_from_config()
        if not cfg.plugin.enabled:
            await self._qzone.stop()
            await self._companion.stop()
            await self._planning.stop()
            # PR6：插件停用时取消未完成的频率校准
            try:
                await self._stop_target_frequency_calibration()
            except Exception:
                pass
            # 生活状态停机（R7 滚动化改造）：停 tick 循环 + 落盘（服务实例保留待重启用）
            try:
                await self._stop_life_tick_task()
            except Exception:
                pass
            self._stop_life_services()
            self._bg_started = False
            return
        # 目标 QQ 变化 → 重新解析 stream
        self._stream_id = await self._resolve_target_stream()
        self._apply_target(target_qq_of(cfg), self._stream_id)
        # 生活状态热启用（R7 滚动化改造；PR8 3.0.0 门控 = [schedule].enabled）：首次
        # 置真时此处补装配，再挂 tick 循环（_start_bg 的 _bg_started 守卫会早退，故单独 _ensure）
        self._setup_life_services()
        # PR3（2026-10-26，定稿 §3）：睡眠引擎随配置热更新重装配（开关由关到开/由开到关）；
        # life 反馈订阅也在此按当前开关同步（不能因 bridge 已订阅而早退）
        try:
            self._sync_sleep_engine()
        except Exception:
            pass
        try:
            self._emotion.sync_from_config()
        except Exception:
            pass
        await self._start_bg()
        self._ensure_life_tick_task()
        # PR4（2026-10-27）：事件库模式随配置热更新切换（off/manual/auto 三态；复用同一
        # 实例，只 start/stop 后台；关闭中的在飞生成按保守结果保留旧库）
        try:
            await self._planning.sync_event_catalog()
        except Exception as exc:
            try:
                self.ctx.logger.warning("事件库模式切换失败（保持只读）：%s", exc)
            except Exception:
                pass
        # PR6：配置热更新（含目标/开关变化）后立即校准目标频率，不等活动切换
        try:
            await self.flush_target_frequency_calibration()
            await self._calibrate_target_frequency()
        except Exception:
            pass
        if self._stream_id:
            self._companion.patrol.start()
        if not cfg.qzone.enabled:
            await self._qzone.stop()

    # ------------------------------------------------------------------
    # 装配助手
    # ------------------------------------------------------------------
    def _apply_target(self, target_qq: str, stream_id: str) -> None:
        self._companion.set_target(target_qq, stream_id)
        self._qzone.set_target(stream_id)
        # PR6（2026-10-27 共享接线）：目标解析成功/切换后**立即**校准目标频率，
        # 不等第一次活动切换。本方法是 sync 装配口：绝不在此 await，也不创建
        # 无法回收的裸协程（无 loop / 未装配 → 记 pending，由 flush 口补发）。
        self._schedule_target_frequency_calibration()

    def _schedule_target_frequency_calibration(self) -> None:
        """在 sync 装配口安全排队一次频率校准（单飞：旧 task 先取消再重建）。"""
        loop: Any = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            # 无事件循环（同步装配/测试直接调用）：只记待办，不创建协程对象，
            # 下一次 async 口（_start_bg / flush_target_frequency_calibration）补齐。
            self._frequency_calibration_pending = True
            return
        previous = getattr(self, "_frequency_calibration_task", None)
        if previous is not None and not previous.done():
            previous.cancel()
        try:
            self._frequency_calibration_task = loop.create_task(
                self._calibrate_target_frequency())
            self._frequency_calibration_pending = False
        except Exception:
            self._frequency_calibration_task = None
            self._frequency_calibration_pending = True

    async def _calibrate_target_frequency(self) -> None:
        """调用 PR6 公共校准口；失败只记日志，绝不拖挂。"""
        planning = getattr(self, "_planning", None)
        calibrate = getattr(planning, "calibrate_target_frequency", None)
        if not callable(calibrate):
            return
        try:
            result = await calibrate(force=True)
            if isinstance(result, dict) and (result.get("errors") or result.get("reason")):
                self.ctx.logger.info("目标频率校准未生效（%s）：%s",
                                     result.get("reason") or "",
                                     "；".join(result.get("errors") or []) or "-")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                self.ctx.logger.warning("目标频率校准异常（忽略）：%s", exc)
            except Exception:
                pass

    async def flush_target_frequency_calibration(self) -> None:
        """补齐 sync 阶段挂起的校准（无 loop 时只记 pending 的那次）。"""
        if not getattr(self, "_frequency_calibration_pending", False):
            return
        self._frequency_calibration_pending = False
        await self._calibrate_target_frequency()

    async def _stop_target_frequency_calibration(self) -> None:
        """取消未完成的校准 task（on_unload / 插件停用）。"""
        task = getattr(self, "_frequency_calibration_task", None)
        self._frequency_calibration_task = None
        self._frequency_calibration_pending = False
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    # ------------------------------------------------------------------
    # 生活状态服务束（R7 滚动化改造）
    # R7 依据 = 用户 2026-10-25 直派（日程系统滚动化改造，设计目录
    # D:/deepseek-harness_workspace/maibot-plugins/2026-10-25-日程系统滚动化改造设计，
    # 主设计 §9.1 plugin.py 行 + 附录 B.4.1/B.5.1/B.5.2/B.5.3）
    # ------------------------------------------------------------------
    def _setup_life_services(self) -> bool:
        """装配 LifeStateService + RollingPlanner + LifeScheduleReadModel 并注入
        ScheduleQuery runtime provider（幂等；已装配直接 True）。

        - ``[schedule].enabled=false``（段缺失/读不到一律按关；PR8 3.0.0 起唯一日程
          总开关，旧 ``[life_state].enabled`` / ``sleep_engine_enabled`` 退役）→
          **不实例化任何新服务**（零开销零行为变化：ScheduleQuery 无 provider，
          读取侧如实「暂不可用」）；
        - 任何装配异常 → 告警日志 + 清残留引用并返回 False，**绝不拖挂插件加载**；
        - spine 生成侧经 planning/auto_scheduler 的 spine 钩子生效（spine 为唯一生成
          路线），本层无遗漏：auto_scheduler 由 PlanningServices.cleanup.run_scheduler_loop
          守护启动，不依赖本装配段。
        """
        if getattr(self, "_life_model", None) is not None:
            return True
        try:
            schedule_on = bool(getattr(getattr(self.config, "schedule", None),
                                       "enabled", False))
            if not schedule_on:
                return False
            section = getattr(self.config, "life_state", None)
            # 活动画像表装载（PR1 P0 修复（2026-10-26），计划书 §三十四 / 定稿 §2 第 0.6 项）：
            # load_and_apply 此前无任何装配点调用（_ACTIVE 恒 None，RollingPlanner/LifeState
            # 实际全用内置默认表）。挂在 [schedule].enabled 判定之后——日程关闭时零装配
            # 动作、零决策日志副作用；热重载 = 宿主重导入重跑 on_load → 自动重读（与
            # activity_properties 同款语义）；失败只告警不拖挂装配。
            try:
                from planning.activity_profiles import load_and_apply as _load_activity_profiles
                _profiles_result = _load_activity_profiles(
                    root_dir=getattr(self, "_plugin_root", None),
                    decision_logger=getattr(self, "_decision_logger", None))
                if getattr(_profiles_result, "rejected", None):
                    self.ctx.logger.warning("活动画像表有 %d 个条目被排除（其余生效）",
                                            len(_profiles_result.rejected))
            except Exception as exc:
                try:
                    self.ctx.logger.warning("活动画像表装载异常（按内置默认处理）：%s", exc)
                except Exception:
                    pass
            state_file = str(getattr(section, "state_file", "") or "").strip() or "life_state.json"
            timezone_str = str(getattr(self.config.schedule_advanced, "timezone", "")
                               or "Asia/Shanghai")
            # 状态文件 = <plugin._data_dir>/<life_state.state_file>（B.4.1 相对数据目录）
            # 与 InitService 共用安全解析口：越界/外部绝对路径回退默认，防运行态读写外部文件。
            from planning.life_state import resolve_state_file_path
            life = LifeStateService(resolve_state_file_path(self._data_dir, state_file),
                                    timezone_str=timezone_str,
                                    decision_logger=getattr(self, "_decision_logger", None))

            def _write_schedule_items(items: Any, date: str, reason: str = "") -> Any:
                """D9 晨间 spine 写回：生成器共享漏斗（不消费未来约定）。"""
                generator = getattr(self, "_schedule_gen", None)
                apply_items = getattr(generator, "apply_schedule_items", None)
                if not callable(apply_items):
                    return 0
                return apply_items(items, date, consume_commitments=False,
                                   reason=reason or "morning_wake_adjust")

            planner = RollingPlanner(
                self._db, life,
                config_provider=lambda: self.config, event_bus=getattr(self, "_bus", None),
                decision_logger=getattr(self, "_decision_logger", None),
                timezone_str=timezone_str, schedule_writer=_write_schedule_items,
                # 硬约定正式起床在 planner 内 force_wake：回调同步真实醒来到周期四变量
                wake_recorder=self._record_actual_wake_cycle,
                # PR4 共享接线：随机事件优先从事件库取（单实例由 PlanningServices 持有；
                # None = 事件库装配失败 → planner 逐字回退旧内置池路径）
                event_catalog=getattr(getattr(self, "_planning", None), "event_catalog", None))
            engine: SleepEngine | None = None
            # PR8 3.0.0：睡眠引擎随 [schedule].enabled 装配（旧 sleep_engine_enabled
            # 键退役；本分支已达 schedule.enabled=true，引擎 enabled 属性同源派生）。
            try:
                engine = SleepEngine(
                    config_provider=lambda: self.config, life_state=life,
                    session_store=self._db, event_bus=getattr(self, "_bus", None),
                    decision_logger=getattr(self, "_decision_logger", None),
                    timezone_str=timezone_str,
                    episode_closer=planner.close_sleep_episode,
                    sleep_endpoint_updater=SleepEndpointHook(self),
                    emotion_bridge=getattr(self, "_emotion", None))
                planner.set_sleep_engine(engine)
                if engine.enabled:
                    engine.restore()
                    self.ctx.logger.info(
                        "睡眠引擎已装配（阈值=%.2f，硬约定提前=%s 分钟，上限复检=%ss）",
                        engine._wake_threshold(),  # noqa: SLF001 - 装配日志只读
                        engine._cfg_int("sleep_hard_commitment_lead_minutes", 30),  # noqa: SLF001
                        engine._wake_check_seconds())  # noqa: SLF001
            except Exception as exc:
                engine = None
                try:
                    self.ctx.logger.warning("睡眠引擎装配失败（本次不启用引擎，睡眠态未就绪）：%s", exc)
                except Exception:
                    pass
            model = LifeScheduleReadModel(
                self._db, life_state=life, planner=planner,
                config_provider=lambda: self.config, timezone_str=timezone_str)
            self._life_state = life
            self._rolling_planner = planner
            self._life_model = model
            self._sleep_engine = engine
            self._install_life_recheck_hooks(engine)
            self._schedule_query.set_runtime_provider(model)
            self.ctx.logger.info("生活状态服务已装配（tick=%ss，状态文件=%s）",
                                 getattr(section, "tick_seconds", 300), state_file)
            return True
        except Exception as exc:
            self._life_state = None
            self._rolling_planner = None
            self._life_model = None
            self._life_modulation = None
            self._sleep_engine = None
            try:
                self._schedule_query.set_runtime_provider(None)
            except Exception:
                pass
            try:
                self.ctx.logger.warning("生活状态服务装配失败（本次不装配，运行态未就绪，不阻塞）：%s", exc)
            except Exception:
                pass
            return False

    def _sync_sleep_engine(self) -> None:
        """按当前配置热更新睡眠引擎（PR3；开→装配/关→解除注入并落盘）。

        不能只依赖 `_setup_life_services`（幂等早退）：日程开关由关到开时须补装配，
        由开到关时须解除 planner/provider（关闭不保留任何新行为）。PR8 3.0.0：
        desired = ``[schedule].enabled`` 唯一总开关（旧 ``[life_state]`` 两开关退役）。
        """
        desired = bool(getattr(getattr(self.config, "schedule", None), "enabled", False))
        engine = getattr(self, "_sleep_engine", None)
        planner = getattr(self, "_rolling_planner", None)
        life = getattr(self, "_life_state", None)
        if not desired:
            if engine is not None:
                # 用户 2026-10-27 批准（热关闭过渡）：以关闭时刻结束当前睡眠会话，再交回
                # 停止日程（PR8 3.0.0 无旧 fallback）—— actual_end=真实关闭时刻、走一次
                # 结算、不留 paused 恢复池条目、
                # 不改既有已结束历史。注意与「硬约定=正式起床」（actual_end=约定−lead 的
                # 真实唤醒时刻）及「夜间 <5 分钟短扰只重置连续段」是三条独立规则。
                self._close_sleep_for_disable(engine)
                if planner is not None:
                    try:
                        planner.set_sleep_engine(None)
                    except Exception:
                        pass
                self._sleep_engine = None
            self._install_life_recheck_hooks(None)
            return
        if engine is not None or planner is None or life is None:
            return
        try:
            timezone_str = str(getattr(self.config.schedule_advanced, "timezone", "")
                               or "Asia/Shanghai")
            engine = SleepEngine(
                config_provider=lambda: self.config, life_state=life,
                session_store=self._db, event_bus=getattr(self, "_bus", None),
                decision_logger=getattr(self, "_decision_logger", None),
                timezone_str=timezone_str,
                episode_closer=planner.close_sleep_episode,
                sleep_endpoint_updater=SleepEndpointHook(self),
                emotion_bridge=getattr(self, "_emotion", None))
            planner.set_sleep_engine(engine)
            if engine.enabled:
                engine.restore()
                self.ctx.logger.info("睡眠引擎已热装配（[schedule].enabled=true）")
            else:
                self.ctx.logger.warning("睡眠引擎未生效（日程未启用或装配降级）")
            self._sleep_engine = engine
        except Exception as exc:
            self._sleep_engine = None
            try:
                self.ctx.logger.warning("睡眠引擎热装配失败（本次不启用引擎，睡眠态未就绪）：%s", exc)
            except Exception:
                pass
            self._install_life_recheck_hooks(None)

    def _close_sleep_for_disable(self, engine: Any) -> None:
        """引擎热关闭/停用前正式结束当前睡眠（用户 2026-10-27 批准的热关闭过渡）。

        - 正在睡（或存在未关闭会话）→ ``force_wake(reason="manual")`` 走一次性结算链：
          会话/DB 行 ``completed``、episode 关闭（actual_end = 真实 now）、端点写回；
        - 无未关闭会话 → 只 ``persist()``（不动历史，不虚构结束）。
        """
        session = getattr(engine, "_session", None)  # noqa: SLF001 - 同源会话判定
        open_session = bool(session is not None and not getattr(session, "closed", True))
        asleep: Any = None
        try:
            asleep = engine.is_asleep()
        except Exception:
            asleep = None
        if open_session or asleep is True:
            shutdown = getattr(engine, "shutdown_session", None)
            try:
                if callable(shutdown):
                    outcome = shutdown(reason="manual")
                else:
                    outcome = engine.force_wake(reason="manual")
                if isinstance(outcome, dict) and outcome.get("committed"):
                    # 热关闭以关闭时刻结束会话 → 同时把真实醒点同步进周期四变量
                    # （2026-10-27 用户批准；不改历史已醒记录）
                    self._record_actual_wake_cycle(
                        when=getattr(session, "closed_at", None))
                    return
            except Exception:
                pass
        try:
            engine.persist()
        except Exception:
            pass

    def _install_life_recheck_hooks(self, engine: Any) -> None:
        """挂/卸「日程/约定写入 → 即时重检」通知（引擎关 → 断开，旧路径行为恢复）。

        只接**已有**写入共同口（ScheduleDB 日程写入 / GoalManager 约定写入），不改
        它们的业务语义；回调只置内部 Event，不推进状态、不发消息、不调模型。
        """
        callback = self._on_life_schedule_change if engine is not None else None
        db = getattr(self, "_db", None)
        setter = getattr(db, "set_schedule_change_listener", None)
        if callable(setter):
            try:
                setter(callback)
            except Exception:
                pass
        goal_manager = getattr(getattr(self, "_planning", None), "goal_manager", None)
        gm_setter = getattr(goal_manager, "set_change_listener", None)
        if callable(gm_setter):
            try:
                gm_setter(callback)
            except Exception:
                pass

    def _on_life_schedule_change(self, *args: Any, **kwargs: Any) -> None:
        """日程/约定写入共同口回调（可能在 DB 线程触发 → request_life_recheck 已线程安全）。"""
        try:
            self.request_life_recheck("schedule_changed")
        except Exception:
            pass

    def _ensure_life_tick_task(self) -> None:
        """挂生活状态 tick 循环（幂等；仅已装配时）。独立间隔 [life_state].tick_seconds，
        写法参照 companion/patrol.py 的可打断睡眠循环（wait_for + stop_event）。"""
        if getattr(self, "_life_model", None) is None:
            return
        task = getattr(self, "_life_tick_task", None)
        if task is not None and not task.done():
            return
        self._life_tick_task = asyncio.create_task(self._life_tick_loop())

    async def _stop_life_tick_task(self) -> None:
        """停生活状态 tick 循环（on_unload / 插件停用分支调用）。"""
        task = getattr(self, "_life_tick_task", None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        self._life_tick_task = None

    def _stop_life_services(self) -> None:
        """生活状态停机：落盘 life_state.json（best-effort；tick 每拍已存，此处兜底收尾）。"""
        engine = getattr(self, "_sleep_engine", None)
        if engine is not None:
            try:
                engine.persist()
            except Exception:
                pass
        life = getattr(self, "_life_state", None)
        if life is None:
            return
        try:
            life.save()
        except Exception as exc:
            try:
                self.ctx.logger.warning("生活状态停机保存失败（忽略）：%s", exc)
            except Exception:
                pass

    async def _life_tick_loop(self) -> None:
        """生活状态 tick 定时循环（可打断睡眠，参照 companion/patrol.py `_loop`）。

        每轮活读 ``[life_state]`` 段：热重载改 tick_seconds 即时生效；``[schedule].enabled``
        关闭 → 循环自退出（重新开启由 on_config_update 的 _ensure_life_tick_task 重新挂起；
        PR8 3.0.0 起唯一日程总开关，旧 ``[life_state].enabled`` 退役）。

        等待上限不再恒为 ``tick_seconds``：睡眠引擎给出下一个**绝对边界**（12:00 上限 /
        硬约定阈值 / 最早起床 / 阈值复检网格）时按边界精确等待（用户 2026-10-27：固定
        300s 会让 11:58 的下一拍睡过 12:00、让 08:02 的硬约定阈值漂到 08:03+）。
        等待同时挂「内部重检通知」（:meth:`request_life_recheck`，日程/约定写入共同口触达），
        睡眠中新增临近硬约定即时重算，不固定等满 5 分钟。
        """
        self._life_tick_loop_ref = asyncio.get_running_loop()
        try:
            while not self._stop_event.is_set():
                # PR8 3.0.0：退出门控 = [schedule].enabled 唯一总开关（旧
                # [life_state].enabled 退役；依据 = 用户 2026-10-27 授权直接清理）。
                if not bool(getattr(getattr(self.config, "schedule", None),
                                    "enabled", False)):
                    return
                try:
                    self._life_tick_once()
                except Exception:
                    pass  # 单拍失败不影响循环
                try:
                    interval = max(30, int(getattr(
                        getattr(self.config, "life_state", None), "tick_seconds", 300)))
                except (TypeError, ValueError):
                    interval = 300
                timeout = self._life_next_wait_seconds(interval)
                if await self._life_wait(timeout):
                    return
        finally:
            self._life_tick_loop_ref = None

    def _life_recheck_event_obj(self) -> asyncio.Event:
        """内部重检事件（惰性创建；同一实例复用 → 通知幂等）。"""
        event = getattr(self, "_life_recheck_event", None)
        if event is None:
            event = asyncio.Event()
            self._life_recheck_event = event
        return event

    def request_life_recheck(self, reason: str = "") -> bool:
        """安全唤醒生活状态 loop 即刻重检（幂等；**不**推进状态/不发消息/不调模型）。

        只做 ``Event.set``：已有未消化通知 → 返回 False（重复通知不会让循环多跑一拍）。
        从非 loop 线程调用时经所属 loop ``call_soon_threadsafe`` 排入；loop 未起动/已结束
        → 置位（下次循环起点立即重检）。日程写入、约定写入等共同口都接这里。
        """
        event = self._life_recheck_event_obj()
        if event.is_set():
            return False
        loop = getattr(self, "_life_tick_loop_ref", None)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is not None and loop is not running:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                return False
        else:
            event.set()
        return True

    def _life_next_wait_seconds(self, interval: Any) -> float:
        """本拍等待上限 = min(tick_seconds, 到下一个绝对睡眠边界的秒数)。

        PR8 3.0.0：删除旧「``sleep_engine_enabled`` 关 → 恒 base」早退——引擎在位
        （随 ``[schedule].enabled`` 装配）即按边界等待（依据 = 用户 2026-10-27 授权
        直接清理 + 逐符号审定清单 WP1/C-7）。
        """
        try:
            base = max(1.0, float(interval))
        except (TypeError, ValueError):
            base = 300.0
        engine = getattr(self, "_sleep_engine", None)
        if engine is None:
            return base
        hard_start = None
        planner = getattr(self, "_rolling_planner", None)
        resolver = getattr(planner, "sleep_hard_commitment_start", None)
        if callable(resolver):
            try:
                hard_start = resolver()
            except Exception:
                hard_start = None
        try:
            boundary = engine.next_wake_boundary(hard_commitment_start=hard_start)
            if boundary is None:
                return base
            remain = (boundary - engine._now()).total_seconds()  # noqa: SLF001 - 同源时钟
        except Exception:
            return base
        if remain <= 0:
            return 1.0  # 已过边界（回拨/长间隔/刚过点）→ 尽快重检，不睡过点
        return max(1.0, min(base, remain))

    async def _life_wait(self, timeout: Any) -> bool:
        """等待下一拍：``stop_event`` 或内部重检通知，先到先返回（True = 停机）。"""
        try:
            limit = max(0.0, float(timeout))
        except (TypeError, ValueError):
            limit = 300.0
        recheck = self._life_recheck_event_obj()
        if recheck.is_set():
            recheck.clear()
            return self._stop_event.is_set()
        tasks = [asyncio.ensure_future(self._stop_event.wait()),
                 asyncio.ensure_future(recheck.wait())]
        try:
            await asyncio.wait(set(tasks), timeout=limit,
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            for task in tasks:
                try:
                    await task
                except BaseException:  # noqa: BLE001 - 取消/异常都不影响循环
                    pass
            recheck.clear()
        return self._stop_event.is_set()

    def _life_tick_once(self) -> None:
        """单拍编排（PR2 + PR3，定稿 §3.1 步序）：读 VAD → engine.prepare → life.tick →
        engine.commit → planner.tick（睡眠 episode 由引擎负责，planner 跳过自动关）。

        睡眠引擎启用时 ``sleep_rates`` 只经一次 ``life.tick`` 应用（不叠加清醒漂移/
        调制/单拍 delta 上限）；引擎关闭/未装配 → ``sleep_rates=None``，逐字旧路径。
        本拍醒来 → 引擎一次性结算/关 episode/发 ``life.sleep_woke``，随后按实际睡眠
        时长做 D9 晨间 spine 调整（写回今日日程）。任何新链失败都不阻塞主循环。
        """
        planner = getattr(self, "_rolling_planner", None)
        if planner is None:
            return
        prepared = self._sleep_prepare(planner)
        sleep_rates = (prepared or {}).get("rates")
        snapshot = self._life_tick_state(planner, sleep_rates=sleep_rates)
        commit = self._sleep_commit(prepared, snapshot)
        if commit.get("woke"):
            # 2026-10-27 用户批准：周期起点同步真实醒来时刻（早醒/晚醒都按真实时刻；
            # 意图计划时间保持不动；后续编辑/再生成复用同一标记）
            self._record_actual_wake_cycle(when=commit.get("woke_at"))
            try:
                planner.apply_morning_spine_adjustment(
                    sleep_hours=commit.get("actual_sleep_hours"),
                    wake_delay_minutes=commit.get("wake_delay_minutes", 0.0))
            except Exception as exc:
                try:
                    self.ctx.logger.warning("晨间 spine 调整失败（忽略）：%s", exc)
                except Exception:
                    pass
        if snapshot is not None:
            report = planner.tick(life_snapshot=snapshot) or {}
        else:
            report = planner.tick() or {}
        date = str(report.get("date") or "")
        if not date or date == getattr(self, "_life_cleanup_date", ""):
            return
        section = getattr(self.config, "life_state", None)
        keep_days = getattr(section, "keep_episode_days", 14)
        try:
            keep = int(keep_days) if isinstance(keep_days, int) and not isinstance(keep_days, bool) else 14
        except (TypeError, ValueError):
            keep = 14
        try:
            removed = self._db.cleanup_old_episodes(keep_days=keep)
            self._life_cleanup_date = date
            if removed:
                self.ctx.logger.info("生活状态 episode 过期清理完成（删除 %d 条）", removed)
        except Exception as exc:
            self.ctx.logger.warning("生活状态 episode 清理失败（忽略）：%s", exc)

    def _life_tick_state(self, planner: Any,
                         sleep_rates: Any = None) -> dict[str, Any] | None:
        """编排侧状态推进：life.tick 并返回快照（PR2；PR3 追加 sleep_rates）。

        - 未装配 LifeStateService → None（编排方回退 planner 自行推进）；
        - ``[life_state].emotion_modulation_enabled``（默认 False）→ 不读 VAD、
          ``modulation=None``（逐字旧行为）；
        - ``sleep_rates`` 非 None（睡眠引擎本拍给）→ 睡眠分支：只按引擎速率积分，
          不叠加被动漂移/调制（由 ``LifeStateService.tick`` 保证）；
        - emotion 服务不可得/停用/读数异常 → modulation=None；任何异常 → None，本拍
          不调制、不阻塞 tick 循环。
        """
        life = getattr(self, "_life_state", None)
        if life is None:
            return None
        if sleep_rates is not None:
            try:
                return life.tick(active_profile=None, modulation=None,
                                 sleep_rates=sleep_rates)
            except Exception as exc:
                try:
                    self.ctx.logger.warning("睡眠期状态推进失败（本拍回退）：%s", exc)
                except Exception:
                    pass
                return None
        modulation = None
        section = getattr(self.config, "life_state", None)
        if section is not None and bool(getattr(section, "emotion_modulation_enabled", False)):
            try:
                service = self._ensure_life_modulation()
                vad = self._emotion_vad_snapshot() if service is not None else None
                if service is not None and vad is not None:
                    modulation = service.compute(vad)
            except Exception as exc:
                modulation = None
                try:
                    self.ctx.logger.warning("情绪调制本拍失败（按不调制处理）：%s", exc)
                except Exception:
                    pass
        try:
            snapshot = life.tick(active_profile=planner.resolve_profile_key(),
                                 modulation=modulation)
        except Exception as exc:
            try:
                self.ctx.logger.warning("生活状态推进失败（本拍由 planner 回退处理）：%s", exc)
            except Exception:
                pass
            return None
        # 附录 B.6.1：只有新调制打开且 tick 成功采用调制时发布一次内部观测事件；
        # modulation=None（开关关/服务不可得/VAD 不可得/计算失败）不发，tick 失败不发。
        # ``gains`` 是 life_state 应用 homeostatic override / 单拍 cap **之前**的计算值，
        # ``state_snapshot`` 才是 tick + clamp 后快照——两者作用域显式标注，不把保护量
        # 当成数学原始 gain 伪报。
        if modulation is not None:
            self._publish_emotion_modulation_applied(modulation, snapshot)
        return snapshot

    def _publish_emotion_modulation_applied(self, gains: dict[str, Any],
                                            snapshot: Any) -> None:
        """发布 ``life.emotion_modulation_applied``（best-effort，静默失败）。"""
        bus = getattr(self, "_bus", None)
        if bus is None:
            return
        try:
            payload = {
                "gains": dict(gains or {}),
                "gains_scope": "computed_pre_homeostatic_override_pre_tick_cap",
                "state_snapshot": dict(snapshot or {}) if isinstance(snapshot, dict) else {},
                "snapshot_scope": "post_tick_post_clamp",
            }
            bus.publish_soon(EVENT_EMOTION_MODULATION_APPLIED, **payload)
        except Exception:
            try:
                self.ctx.logger.debug("调制观测事件发布失败（静默）")
            except Exception:
                pass

    def _record_actual_wake_cycle(self, when: Any = None) -> None:
        """真实醒来 → 周期四变量 ``cycle_start``（2026-10-27 用户批准；best-effort）。

        - 只写「本帧真实醒来」标记（``current_start_source=actual_wake`` +
          ``current_start_date``），后续日程编辑/再生成/重启由
          ``cycle_state.enter_schedule_cycle`` 复用，不会被最早清醒意图的计划时间盖回；
        - 各条意图的计划时间原样保留（不动 schedule 行）；
        - 不新增 LLM 调用、不发任何消息、不改历史已醒记录。
        """
        data_dir = getattr(self, "_data_dir", None)
        if data_dir is None:
            return
        try:
            from planning.cycle_state import (entry_context_from_plugin,
                                              record_actual_wake)
            from planning.time_utils import TimezoneManager
            ctx = entry_context_from_plugin(self)
            tz_name = str(ctx.get("tz_name") or "") or "Asia/Shanghai"
            wake_at = when
            if not isinstance(wake_at, datetime):
                wake_at = TimezoneManager(tz_name).get_now()
            record_actual_wake(
                str(data_dir), schedule_date=wake_at.strftime("%Y-%m-%d"),
                wake_epoch=wake_at.timestamp(), reason="engine_wake", tz_name=tz_name,
                cfg_wake=ctx.get("cfg_wake", ""), cfg_sleep=ctx.get("cfg_sleep", ""),
                decision_logger=getattr(self, "_decision_logger", None), now=wake_at)
        except Exception as exc:
            try:
                self.ctx.logger.warning("真实醒来同步周期起点失败（忽略）：%s", exc)
            except Exception:
                pass

    def _sleep_prepare(self, planner: Any) -> dict[str, Any] | None:
        """睡眠引擎拍前准备（PR3，定稿 §3.1 步 2；未装配/关 → None 旧路径）。"""
        engine = getattr(self, "_sleep_engine", None)
        if engine is None:
            return None
        try:
            current = planner.current() if callable(getattr(planner, "current", None)) else None
            schedule = getattr(self.config, "schedule", None)
            # 硬约定三态（2026-10-27 用户批准）：解析器不可用 → None（沿用会话缓存）；
            # 解析器明确无有效约定 → NO_HARD_COMMITMENT（撤销缓存，不幽灵唤醒）；
            # 有约定 → datetime（采用并缓存）。
            hard_start: Any = None
            resolver = getattr(planner, "sleep_hard_commitment_start", None)
            if callable(resolver):
                try:
                    resolved = resolver()
                except Exception:
                    resolved = None
                hard_start = (NO_HARD_COMMITMENT if resolved is None else resolved)
            prepared = engine.prepare_tick(
                sleep_episode=current,
                hard_commitment_start=hard_start,
                no_sleep_mode=bool(getattr(schedule, "no_sleep_mode", False)))
            return prepared if isinstance(prepared, dict) else None
        except Exception as exc:
            try:
                self.ctx.logger.warning("睡眠引擎 prepare 失败（本拍按无睡眠处理）：%s", exc)
            except Exception:
                pass
            return None

    def _sleep_commit(self, prepared: Any, snapshot: Any) -> dict[str, Any]:
        """睡眠引擎拍后提交（定稿 §3.1 步 4）：应用觉醒/结算/事件，恰好一次。"""
        engine = getattr(self, "_sleep_engine", None)
        if engine is None or not isinstance(prepared, dict):
            return {}
        try:
            energy = snapshot.get("energy") if isinstance(snapshot, dict) else None
            result = engine.commit_tick(energy=energy)
            if isinstance(result, dict) and result.get("woke"):
                session = getattr(engine, "_session", None)  # noqa: SLF001 - 同源会话读
                closed_at = getattr(session, "closed_at", None)
                if isinstance(closed_at, datetime):
                    result["woke_at"] = closed_at
            return result if isinstance(result, dict) else {}
        except Exception as exc:
            try:
                self.ctx.logger.warning("睡眠引擎 commit 失败（忽略本拍）：%s", exc)
            except Exception:
                pass
            return {}

    # ── 对外：睡眠引擎读数/打扰记账（emotion 侧按鸭子类型调用）──────────
    def sleep_asleep(self, now: Any = None) -> Any:
        """单一 wake provider：``True/False`` 权威，``None`` = 引擎不表态走旧窗口。"""
        engine = getattr(self, "_sleep_engine", None)
        if engine is None:
            return None
        try:
            return engine.is_asleep(now)
        except Exception:
            return None

    def sleep_status(self, now: Any = None) -> Any:
        """只读睡眠状态（面板/diag；引擎不表态 → None）。"""
        engine = getattr(self, "_sleep_engine", None)
        if engine is None:
            return None
        try:
            return engine.sleep_status(now)
        except Exception:
            return None

    def note_sleep_interruption(self, now: Any = None) -> bool:
        """夜间被打扰记账（emotion ``note_night_reply`` / ``emotion.sleep_interrupt`` 接）。"""
        engine = getattr(self, "_sleep_engine", None)
        if engine is None:
            return False
        try:
            return bool(engine.note_interruption(now))
        except Exception:
            return False

    def _ensure_life_modulation(self) -> LifeModulationService | None:
        """情绪调制服务惰性构造（PR2，2026-10-26，定稿 §4.1.1；幂等）。

        只在 ``[life_state].emotion_modulation_enabled=true`` 分支内被调用（该开关
        独立于日程总开关 ``[schedule].enabled`` 保留，裁定 3；日程关闭时生活状态服务束
        不装配，本口不会被触达 → 零实例化）；构造失败置 ``False`` 哨兵不再重试
        （防每拍告警刷屏），热重载 = 宿主重导入重跑 on_load 自动复位。
        """
        service = getattr(self, "_life_modulation", None)
        if service is not None:
            return service if isinstance(service, LifeModulationService) else None
        try:
            service = LifeModulationService(root_dir=getattr(self, "_plugin_root", None))
            self._life_modulation = service
            self.ctx.logger.info("情绪调制服务已启用（系数表=mappings/emotion_life_modulation.json，缺失/坏表按内置默认）")
            return service
        except Exception as exc:
            self._life_modulation = False  # type: ignore[assignment]
            try:
                self.ctx.logger.warning("情绪调制服务构造失败（本周期内按不调制处理）：%s", exc)
            except Exception:
                pass
            return None

    def _emotion_vad_snapshot(self) -> Any:
        """emotion 生效 VAD 完整快照（v/a/d；可空降级，PR2 定稿 §4.1.1）。

        读数序（全鸭子类型，planning 不 import emotion 的单向依赖纪律不变）：
        束级公共口（``effective_vad_snapshot`` / ``effective_vad``，B 员接线后自动命中）
        → 内层状态机 ``effective_vad()`` 真源（emotion/state_machine.py，现状束层无
        full-VAD 转发时的唯一完整读数位）。emotion 未装配/停用/全链失败 → None。
        """
        emotion = getattr(self, "_emotion", None)
        if emotion is None:
            return None
        enabled = getattr(emotion, "is_enabled", None)
        if callable(enabled):
            try:
                if not enabled():
                    return None
            except Exception:
                return None
        for name in ("effective_vad_snapshot", "effective_vad"):
            getter = getattr(emotion, name, None)
            if callable(getter):
                try:
                    return getter()
                except Exception:
                    return None
        machine = getattr(getattr(emotion, "service", None), "_machine", None)
        getter = getattr(machine, "effective_vad", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                return None
        return None

    async def _start_bg(self) -> None:
        """启动后台任务（on_load 与「停用→启用」配置切换共用；幂等）。"""
        if self._bg_started:
            return
        self._stop_event = asyncio.Event()
        await self._planning.start()
        # PR6：sync 装配口挂起的频率校准在此补发（无 loop 时不会创建裸协程）
        try:
            await self.flush_target_frequency_calibration()
        except Exception:
            pass
        await self._companion.start()
        if self.config.qzone.enabled:
            self._qzone.start()
        await self._emotion.start()
        self._bg_tasks.append(asyncio.create_task(self._stream_retry_loop()))
        self._ensure_life_tick_task()
        self._bg_started = True

    async def _refresh_persona(self) -> None:
        try:
            self._personality = str(await self.ctx.config.get("personality.personality", "") or "")
            nickname = str(await self.ctx.config.get("bot.nickname", "") or "")
        except Exception:
            self._personality, nickname = "", ""
        self._lover_name = nickname or "麦麦"
        self._companion.set_persona(self._lover_name, self._personality)
        self._qzone.set_persona(self._lover_name, self._personality)
        self._emotion.set_persona(self._lover_name, self._personality)

    # ------------------------------------------------------------------
    # stream 解析（companion 巡检与 qzone 提醒共用的目标会话）
    # ------------------------------------------------------------------
    async def _resolve_target_stream(self) -> str:
        """解析目标 QQ 的私聊 stream_id；失败返回空串（后台重试循环接管）。"""
        target_qq = target_qq_of(self.config)
        if not target_qq or target_qq == "123456789":
            return ""  # 占位号不解析
        try:
            info = await self.ctx.chat.get_stream_by_user_id(user_id=target_qq)
            payload = info.get("stream") if isinstance(info, dict) else None
            payload = payload or info
            if isinstance(payload, dict) and payload.get("stream_id"):
                return str(payload["stream_id"])
        except Exception:
            pass
        try:  # 回退：遍历私聊流
            streams = await self.ctx.chat.get_private_streams()
            if isinstance(streams, dict):
                streams = streams.get("streams") or streams.get("data") or []
            for s in streams or []:
                if isinstance(s, dict) and str(s.get("user_id") or "") == target_qq and s.get("stream_id"):
                    return str(s["stream_id"])
        except Exception:
            pass
        return ""

    async def _stream_retry_loop(self) -> None:
        """stream 未解析时每 60 秒重试；解析到则启动巡检循环。"""
        while not self._stop_event.is_set():
            if not self.config.plugin.enabled or self._companion.patrol.has_target():
                return
            stream_id = await self._resolve_target_stream()
            if stream_id:
                self._stream_id = stream_id
                self._apply_target(target_qq_of(self.config), stream_id)
                self._companion.patrol.start()
                self.ctx.logger.info("目标 stream 解析成功：%s", stream_id)
                return
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                continue


def create_plugin() -> MaiBotPlugin:
    """Runner 加载入口。"""
    return CateyeSuitePlugin()

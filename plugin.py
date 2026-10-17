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

from companion.affection import AffectionManager
from companion.components import CompanionComponentsMixin
from companion.config import target_qq_of
from companion.services import CompanionServices
from suite_config import (
    CURRENT_LAYOUT_VERSION,
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


class CateyeSuitePlugin(PlanningComponentsMixin, CompanionComponentsMixin, QzoneComponentsMixin,
                        EmotionComponentsMixin, MaiBotPlugin):
    config_model: ClassVar[type] = CateyeSuiteConfig
    # 订阅宿主 bot 配置热重载（人设/昵称变化自动同步）
    config_reload_subscriptions: ClassVar[Iterable[str]] = ("bot",)

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
        rescued, rescued_changed = _rescue_config_from_disk(config_data)
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
        self._schedule_query = ScheduleQuery(
            self._db, cache_minutes=cfg.schedule_advanced.inject_cache_minutes)

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
        if not cfg.plugin.enabled:
            self.ctx.logger.info("插件未启用（plugin.enabled=false），仅加载组件")
            return

        # 目标私聊 stream 解析（失败则后台重试）
        self._stream_id = await self._resolve_target_stream()
        self._apply_target(target_qq_of(cfg), self._stream_id)
        await self._start_bg()
        self.ctx.logger.info("属于麦麦的生活已加载 v%s（目标=%s，stream=%s）",
                             SUPPORTED_CONFIG_VERSION, target_qq_of(cfg) or "未配置",
                             self._stream_id or "待解析")

    async def on_unload(self) -> None:
        await self._qzone.stop()
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
            self._bg_started = False
            return
        # 目标 QQ 变化 → 重新解析 stream
        self._stream_id = await self._resolve_target_stream()
        self._apply_target(target_qq_of(cfg), self._stream_id)
        await self._start_bg()
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

    async def _start_bg(self) -> None:
        """启动后台任务（on_load 与「停用→启用」配置切换共用；幂等）。"""
        if self._bg_started:
            return
        self._stop_event = asyncio.Event()
        await self._planning.start()
        await self._companion.start()
        if self.config.qzone.enabled:
            self._qzone.start()
        await self._emotion.start()
        self._bg_tasks.append(asyncio.create_task(self._stream_retry_loop()))
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

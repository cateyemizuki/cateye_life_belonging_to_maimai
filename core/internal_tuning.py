# -*- coding: utf-8 -*-
"""内部调参表（2026-11-04 配置精简轮，方案 J 类；3.17.6 起 46 键，B1 增 9、B2 增 16、
B3 增 12 → 92 键）。

把「内部调参但可能需现场微调」的阈值/权重/容量/缓存/超时/采样温度从聚合
TOML schema 移入 ``mappings/internal_tuning.json``（表层用户数据区）+
``mappings/default_mappings/internal_tuning.json``（随包种子），沿用既有种子链
（:mod:`core.mapping_dir` / :mod:`core.mapping_table`：表层缺表 → 旧根迁移 →
种子复制 → 内置默认；**种子不覆盖用户表**；解析错误不算缺失、绝不覆盖用户文件）。

- 读取侧：各 section 类把已删 Field 换成同名只读 property（:func:`tuning_value`），
  全部既有 ``getattr(section, key, default)`` / 属性直读消费点零改动；
  表层 JSON 改动按 mtime 失效缓存，下一拍读取即生效（无需宿主重载）。
- 迁移侧：旧 TOML 键由 :func:`core.config_migration.migrate_internal_tuning`
  纯内存摘出，``plugin.normalize_plugin_config`` 在 SDK merge 出厂默认**之前**
  经 :func:`merge_captured` 落盘捕获值——**只补用户表缺失键**（幂等，不覆盖
  已有现场微调），生产非默认值不丢失。
- 坏值口径：单键类型不符 → 该键回退出厂默认（告警一次），不整表拒绝；
  整表解析失败 → 全量内置默认（表层文件原地不动，红线同 mapping_dir）。

本模块只依赖标准库与 :mod:`core.mapping_dir`（不 import maibot_sdk / 业务模块），
各 config section 可安全引用，无导入环。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from .mapping_dir import (MappingReadError, mappings_path, read_json_text,
                          ensure_user_table, migrate_legacy_root_file)

logger = logging.getLogger(__name__)

#: 表层用户表文件名（mappings/ 下；不随包分发，更新插件不覆盖）
INTERNAL_TUNING_FILE_NAME = "internal_tuning.json"

#: 表结构版本（顶层 version 必填 int 且等于本值）
SUPPORTED_VERSION = 1

#: 出厂默认单一真源：段 → 键 → 默认值（3.17.6 起 46 键；历史注释「40 项」已陈旧，
#: 后续批次有增键；3.17.6 退役 event_catalog.max_pool_events 一项，47→46；
#: B1 增 9、B2 增 16、B3 增 12 → 92 键；数值逐字取自原 Field default）。种子 JSON
#: 由本表生成，运行期内置兜底也用它。
INTERNAL_TUNING_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "patrol": {
        "screen_timeout_seconds": 20.0,
        "screen_context_ttl_minutes": 90,
    },
    "affection": {
        "tier_hysteresis": 6.0,
    },
    "schedule": {
        "spine_recent_days": 2,
        "spine_history_messages": 12,
        "spine_knowledge_limit": 3,
    },
    "schedule_advanced": {
        "max_rounds": 2,
        "quality_threshold": 0.85,
        "generation_timeout": 180.0,
        "recent_schedule_days": 3,
        "history_message_limit": 30,
        "knowledge_search_limit": 5,
        "min_description_length": 20,
        "max_description_length": 50,
        "role_judge_temperature": 0.3,
        "image_timeout_seconds": 15.0,
        "context_max_turns": 3,
        "context_ttl": 600,
        "inject_cache_minutes": 15,
        "cache_ttl": 300,
        "cache_max_size": 100,
        "cleanup_interval": 3600,
    },
    "life_state": {
        "horizon_minutes": 90,
        "resume_pool_size": 3,
        "resume_expire_minutes": 240,
        "low_energy_threshold": 0.30,
        "high_hunger_threshold": 0.70,
        "high_stress_threshold": 0.80,
        "brief_attention_progress": 0.30,
        "late_attention_progress": 0.70,
        "sleep_interrupt_min_minutes": 5,
        # PR-REALISM-TUNING（3.17.20，规格 §1A/§1B）：真实回复社交电量消耗
        # （夹取 ≥0）与睡眠债每小时抬升的压力基线下限。内部调参，不新增
        # 用户可见 schema 键；debt_stress_per_h 经 life_state._settlement_params_v2
        # 注入结算 params（纯函数层不读盘）。
        "chat_social_drain_per_reply": 0.015,
        "debt_stress_per_h": 0.01,
        # PR-STATE-CONTRACT（3.17.22 P1-C）：计划社交活动消耗幅度上限（/min，夹取 ≥0）。
        # 软化旧默认画像 -0.02/min 的一次触底；经 _settlement_params_v2 注入结算，
        # 不改用户画像表层映射。内部调参，不新增用户可见 schema 键。
        "planned_social_drain_cap_per_min": 0.006,
    },
    "dynamic_sleep": {
        "pressure_weight": 0.5,
        "circadian_weight": 0.3,
        "debt_weight": 0.2,
        "awakening_sensitivity": 0.30,
        "chronotype_offset_minutes": 0,
        "weekday_phase_minutes": 0,
        "weekend_phase_minutes": 15,
        # PR-SLEEP-RECOVERY（3.17.19，规格 §1B）：高债提前睡触发阈值（小时）
        # 与夜间恢复目标最大加成（小时）。内部调参，不新增用户可见 schema 键。
        "debt_trigger": 6.0,
        "target_max_bonus": 1.0,
    },
    # 3.17.6（PR-REMOVE-POOLCAP）：event_catalog.max_pool_events 退役（纯 pop
    # 不搬运）——池上限改由 planning.event_catalog_config.derive_pool_cap 派生。
    # 旧表层 JSON 里的该键由 _validate_entries 按未知键静默忽略（不报错）。
    "llm": {
        "judge_temperature": 0.2,
    },
    "dashboard_display": {
        "history_state_epsilon": 0.01,
        "history_debt_epsilon": 0.1,
    },
    "emotion": {
        "episode_idle_minutes": 10,
        "target_bonus_step": 0.1,
        "dedup_window_seconds": 180,
        # 4.3b（2026-11-11 修复轮）：评估配额分区总开关。内部调参，不占用户
        # schema 叶子；默认 True（私聊 6/群聊 4 分区），False = 回旧单一共享池。
        "emotion_quota_partition_enabled": True,
        # 3.17.33 每日情绪兜底结算（用户直派）：白天配额可能被用光，日终再结算一次。
        # 内部调参（J 类），不占用户 schema 叶子；缺键 = 默认开启 / 23:50 / 摘要 800 字。
        # ``daily_settlement_minute`` = 当日第几分钟（1430 = 23:50）。
        "daily_settlement_enabled": True,
        "daily_settlement_minute": 1430,
        "daily_settlement_max_chars": 800,
    },
    "video_watch": {
        "queue_capacity": 3,
        "task_deadline_seconds": 10800,
    },
    # F1 群注意力衰减（B1，2026-11-11 v2 §3.4）：实验期参数在用户可见
    # ``[experimental].group_attention_*``，本段是同源默认值与「转正后迁回 J 类」的
    # 目标段（迁移映射见 union/20261111_实验配置迁移映射.md）。运行期不读表层
    # internal_tuning.json 的该段；新增 9 键为 additive，不改既有键值。
    "group_attention": {
        "sample_size": 20,
        "decay_window_minutes": 45,
        "silence_weight": 0.45,
        "mismatch_weight": 0.55,
        "step_per_tick": 0.02,
        "recover_step_per_tick": 0.05,
        "planner_hint_min_multiplier": 0.85,
        "planner_hint_interval_minutes": 30,
        "quiet_group_grace_minutes": 30,
    },
    # F3 转场拖延（B2，2026-11-11 v2 §4.4）：实验期参数在用户可见
    # ``[experimental].transition_*``（16 键），本段是同源默认值与「转正后迁回
    # J 类」的目标段（迁移映射见 union/20261111_实验配置迁移映射.md §2）。运行期
    # 不读表层 internal_tuning.json 的该段；新增 16 键为 additive，不改既有键值。
    "transition": {
        "enabled": False,
        "window_min": 3,
        "window_max": 8,
        "procrastination_prob": 0.20,
        "delay_minutes": 15,
        "delay_jitter_minutes": 5,
        "daily_buffer_limit": 4,
        "daily_procrastination_limit": 2,
        "low_focus_types": ["meal", "entertainment", "rest", "idle"],
        "high_focus_types": ["work", "study"],
        "boundary_margin_minutes": 2,
        "focus_target": 0.68,
        "focus_tau_min": 3.0,
        "confess_probability": 0.10,
        "confess_min_interval_minutes": 20,
        "procrastination_event_branch": False,
    },
    # F4 社交边界（B3，2026-11-11 v2 §5.4）：实验期参数在用户可见
    # ``[experimental].social_boundary_*``（12 参数 + 1 总开关），本段是同源默认值
    # 与「转正后迁回 J 类」的目标段（迁移映射见 union/20261111_实验配置迁移映射.md §3）。
    # 运行期不读表层 internal_tuning.json 的该段；新增 12 键为 additive，不改既有键值。
    "boundary": {
        "refusal_energy_threshold": 0.25,
        "refusal_stress_threshold": 0.80,
        "refusal_mental_floor": 0.15,
        "deep_work_types": ["work", "study"],
        "deep_work_modes": ["brief", "later"],
        "group_line_interval_minutes": 30,
        "wait_hint_enabled": True,
        "wait_hint_min_seconds": 45,
        "wait_hint_max_seconds": 180,
        "wait_hint_cooldown_minutes": 30,
        "fragment_hint_probability": 0.20,
        "rhythm_short_reply_probability": 0.25,
    },
}

#: 插件根目录（本文件位于 core/ 下，parents[1] = 插件根；与 mapping_table.plugin_root 同口径）
_PLUGIN_ROOT = Path(__file__).resolve().parents[1]

#: 读取缓存：(path_str, mtime_ns, size) → 合并后的生效表；表层文件改动即失效
_CACHE: Dict[str, Any] = {"key": None, "table": None}

#: 坏键告警去重（进程级；同一键只告警一次，防 tick 循环刷屏）
_WARNED_BAD_KEYS: set = set()

#: 校验器二次捕获的待落盘队列（进程级）。聚合校验器保持**无 IO**：古老段搬迁
#: （cateye/judge/inject）产生的 J 值先 :func:`stage_captured` 入队，由
#: plugin.normalize_plugin_config 在校验成功后 :func:`drain_pending` 统一落盘
#: （root 与磁盘抢救同一 _PLUGIN_DIR，离线用例可替换）。纯离线 model_validate
#: 不经 normalize 时值留在内存、不写任何文件（无文件系统副作用）。
_PENDING_CAPTURES: list = []


def stage_captured(captured: Dict[str, Dict[str, Any]]) -> None:
    """校验器侧：二次捕获值入队（不落盘、不做 IO）。"""
    if captured:
        _PENDING_CAPTURES.append(captured)


def drain_pending() -> Dict[str, Dict[str, Any]]:
    """取出并清空待落盘捕获值（同键后入优先；调用方负责 merge_captured 落盘）。"""
    merged: Dict[str, Dict[str, Any]] = {}
    while _PENDING_CAPTURES:
        for sec, keys in _PENDING_CAPTURES.pop(0).items():
            merged.setdefault(sec, {}).update(keys)
    return merged


def default_root() -> Path:
    """默认插件根（离线用例可给 tuning_value/merge_captured 传 root_dir 覆盖）。"""
    return _PLUGIN_ROOT


def _type_ok(value: Any, default: Any) -> bool:
    """单键类型校验：默认值决定期望类型；bool 只接受 bool（防 true/false 混进数值键）。

    3.17.33：先判 ``default`` 是否为 bool 再排除「value 是 bool」——原实现把
    ``isinstance(value, bool)`` 放在最前，导致所有 bool 键的现场值被静默丢弃、
    永远回退出厂默认（``daily_settlement_enabled`` / ``emotion_quota_partition_enabled``
    这类开关无法从表层 JSON 关闭）。数值默认仍严格排除 bool（``True`` 不算 1）。
    """
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    if isinstance(default, int):
        return isinstance(value, int)
    if isinstance(default, float):
        return isinstance(value, (int, float))
    return type(value) is type(default)


def _validate_entries(raw: Any) -> Dict[str, Dict[str, Any]]:
    """从原始文档摘出合法已知键（段/键未知或类型不符 → 丢弃；坏键告警一次）。"""
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for section, keys in INTERNAL_TUNING_DEFAULTS.items():
        sec_raw = raw.get(section)
        if not isinstance(sec_raw, dict):
            continue
        good: Dict[str, Any] = {}
        for key, default in keys.items():
            if key not in sec_raw:
                continue
            value = sec_raw[key]
            if _type_ok(value, default):
                good[key] = value
            else:
                token = f"{section}.{key}"
                if token not in _WARNED_BAD_KEYS:
                    _WARNED_BAD_KEYS.add(token)
                    logger.warning("internal_tuning.json 键 %s 类型不符（%r），该键按出厂默认 %r 处理",
                                   token, value, default)
        if good:
            out[section] = good
    return out


def _load_table(root_dir: Any) -> Dict[str, Dict[str, Any]]:
    """装载生效表 = 内置默认 ← 表层合法键覆盖（mtime 缓存；任何异常 → 内置默认）。"""
    root = Path(root_dir) if root_dir is not None else _PLUGIN_ROOT
    path = mappings_path(root, INTERNAL_TUNING_FILE_NAME)
    try:
        st = path.stat()
        cache_key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        cache_key = (str(path), None, None)
    if _CACHE["key"] == cache_key and _CACHE["table"] is not None:
        return _CACHE["table"]
    table: Dict[str, Dict[str, Any]] = {
        section: dict(keys) for section, keys in INTERNAL_TUNING_DEFAULTS.items()
    }
    if cache_key[1] is not None:
        try:
            raw = read_json_text(path)
            if isinstance(raw, dict) and raw.get("version") == SUPPORTED_VERSION:
                for section, keys in _validate_entries(raw).items():
                    table[section].update(keys)
            else:
                logger.warning("internal_tuning.json 顶层 version 缺失/不支持（期望 %s），按内置默认处理",
                               SUPPORTED_VERSION)
        except MappingReadError as exc:
            logger.warning("internal_tuning.json 读取失败（按内置默认处理，表层文件不覆盖）：%s", exc.detail)
        except Exception as exc:  # 装载绝不拖挂运行期
            logger.warning("internal_tuning.json 装载异常（按内置默认处理）：%s", exc)
    _CACHE["key"] = cache_key
    _CACHE["table"] = table
    return table


def tuning_value(section: str, key: str, *, root_dir: Any = None) -> Any:
    """读单个内部调参值：表层用户表 → 出厂默认（键恒在 DEFAULTS 内，缺省安全）。"""
    table = _load_table(root_dir)
    sec = table.get(section) or {}
    if key in sec:
        return sec[key]
    return INTERNAL_TUNING_DEFAULTS[section][key]


def tuning_section(section: str, *, root_dir: Any = None) -> Dict[str, Any]:
    """读整段生效值（副本；供热重载同步/诊断面使用）。"""
    return dict(_load_table(root_dir).get(section) or INTERNAL_TUNING_DEFAULTS.get(section) or {})


def _atomic_write_json(path: Path, doc: Dict[str, Any]) -> None:
    """同目录临时文件 + os.replace 原子落盘（失败抛给调用方兜底）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError as cleanup_exc:
            # 清理失败留痕（P2-08 静默失败守卫）；原异常照常上抛给调用方兜底，
            # 临时文件残留不改变失败语义。
            logger.debug("internal_tuning 临时文件清理失败（原异常照常上抛）：%s：%s",
                         tmp_name, cleanup_exc)
        raise


def merge_captured(captured: Dict[str, Dict[str, Any]], *, root_dir: Any = None) -> int:
    """把旧 TOML 捕获值幂等并入表层用户表；返回新写入的键数。

    - 只补**缺失键**：用户表已有的键（含现场微调）绝不覆盖；
    - 表层缺表且有捕获值：先旧根迁移（用户旧资产优先），仍缺则直接生成
      「内置默认 + 捕获值覆盖」完整表——**不复制纯默认种子**（种子全键默认
      一旦占位，「只补缺失键」就再也补不进捕获值）；
    - 表层缺表且无捕获值：走既有种子链（旧根迁移 → 种子复制）补齐纯默认表；
    - 表层存在但解析失败：**不写不覆盖**（红线：解析错误不算缺失），返回 0；
    - 捕获值里类型不符的键丢弃（告警口径同读取侧）。
    """
    root = Path(root_dir) if root_dir is not None else _PLUGIN_ROOT
    clean = _validate_entries({"version": SUPPORTED_VERSION, **(captured or {})})
    path = mappings_path(root, INTERNAL_TUNING_FILE_NAME)
    try:
        present = path.exists()
    except OSError:
        present = False
    if not present and clean:
        # 有捕获值时**不复制纯默认种子**：种子全键默认一旦占位，「只补缺失键」
        # 就再也补不进捕获值（首轮离线复现抓到的丢值缺陷）。直接生成
        # 「内置默认 + 捕获值覆盖」完整表——与种子内容同源，仅捕获键不同。
        # 旧根资产优先口径不受影响：本表为 3.1.0 新增，旧根不可能有同名旧资产；
        # 防御性仍先走一次旧根迁移，若真出现旧资产则按「已有用户表」只补缺失键。
        migrate_legacy_root_file(root, INTERNAL_TUNING_FILE_NAME)
        try:
            present = path.exists()
        except OSError:
            present = False
    if not present and clean:
        doc: Dict[str, Any] = {"version": SUPPORTED_VERSION}
        for section, keys in INTERNAL_TUNING_DEFAULTS.items():
            merged = dict(keys)
            merged.update(clean.get(section) or {})
            doc[section] = merged
        _atomic_write_json(path, doc)
        _CACHE["key"] = None
        return sum(len(keys) for keys in clean.values())
    if not present:
        # 无捕获值：走既有种子链补齐纯默认表（旧根迁移 → 种子复制）
        ensure_user_table(root, INTERNAL_TUNING_FILE_NAME)
        return 0
    try:
        raw = read_json_text(path)
    except MappingReadError as exc:
        logger.warning("internal_tuning.json 解析失败，旧配置捕获值本次不落盘（表层文件不覆盖）：%s",
                       exc.detail)
        return 0
    if not isinstance(raw, dict) or raw.get("version") != SUPPORTED_VERSION:
        logger.warning("internal_tuning.json 顶层结构/version 异常，旧配置捕获值本次不落盘（表层文件不覆盖）")
        return 0
    existing = _validate_entries(raw)
    added = 0
    for section, keys in clean.items():
        target = raw.get(section)
        if not isinstance(target, dict):
            target = {}
            raw[section] = target
        have = existing.get(section) or {}
        for key, value in keys.items():
            if key in have:
                continue  # 已有合法键（含用户微调）→ 不覆盖
            if key in target:
                continue  # 已有键但类型非法 → 也不覆盖（留给用户修）
            target[key] = value
            added += 1
    if added:
        _atomic_write_json(path, raw)
        _CACHE["key"] = None  # 落盘后失效读取缓存
    return added

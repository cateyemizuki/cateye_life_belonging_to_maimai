"""离线睡眠结算编排服务（2026-11-04 评估 F1/P1-18：自 plugin.py 下沉）。

DEV_GUIDE:9 要求组合根「不沉积业务算法」——原 ``CateyeSuitePlugin._settle_offline_gap``
/ ``_apply_offline_settlement``（冷启动离线 gap 分级分流 + 一次性结算的完整业务编排：
帧分钟/session 行/episode 足迹/醒沿事件/晨间联动）整体迁入本文件，plugin.py 只留
同名薄委托（装配与容错）。逻辑逐字搬迁、不改行为；依赖经构造注入（db/配置提供者/
结算档位提供者/醒沿记录回调/事件总线与日志提供者），本服务不 import companion，
也不持有权威状态（LifeStateService/ScheduleDB 仍是各自数据的唯一所有者）。

文件名沿用 ``suite_`` 前缀纪律（同 suite_config/suite_prompts）：宿主 Runner 的
sys.path 含宿主 src 与兄弟插件目录，顶层通用名会被抢占（见 plugin.py 引导头注释）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional

from planning.life_state import DT_MAX_MINUTES
from planning.offline_sleep import plan_offline_settlement

from planning.sleep_engine import EVENT_SLEEP_WOKE, SleepEngine


def offline_episode_id(plan: dict, *, now: datetime | None = None) -> str:
    """复核 P2-4c / CF-6：离线结算幂等键。

    优先用计划携带的 ``sleep_date``（入睡日历日）+ ``start_minute``（入睡时刻在
    该日程日帧内的分钟，跨午夜 >1440）→ ``offline:{sleep_date}:{start_minute}``。
    这两个字段由 :meth:`OfflineSettlementService.apply_offline_settlement` 按配置
    时区与 ``gap_seconds`` 现算后注入到幂等键；键只由该日帧决定，不读本地墙钟
    日期，也不受跨午夜/本地时区漂移影响。对已携带这两个字段的计划，``now`` 完全
    不参与——同一计划在不同本地 ``now`` 下得到同一键。旧实现用
    ``int(effective_minutes)`` 取整，同日不同起始分钟（或不同有效时长）的合法新段
    会碰撞；新键按帧分钟区分，不再误拦。

    仅在计划缺 ``sleep_date``/``start_minute``（含非法值）时回退旧实现（本地日期
    + 有效分钟）；旧格式保持不变，供既有调用方/回归兼容。回退路径不抛异常。
    """
    sleep_date = str(plan.get("sleep_date") or "").strip()
    start_minute = plan.get("start_minute")
    if sleep_date and start_minute is not None and not isinstance(start_minute, bool):
        try:
            # 规范成 YYYY-MM-DD；非法日历日/格式 → 走旧键回退
            normalized = datetime.strptime(sleep_date, "%Y-%m-%d").strftime("%Y-%m-%d")
            return f"offline:{normalized}:{int(start_minute)}"
        except (TypeError, ValueError):
            pass
    day = (now or datetime.now()).strftime("%Y-%m-%d")
    try:
        minutes = int(float(plan.get("effective_minutes") or 0.0))
    except (TypeError, ValueError):
        minutes = 0
    return f"offline:{day}:{minutes}"


class OfflineSettlementService:
    """冷启动离线 gap 收口 + 一次性离线睡眠结算（编排层；无自有持久化状态）。

    - ``settle_offline_gap``：pending gap 恰好一次消费；引擎认领 → 弃置（防双记账）；
      grace/计划失败 → 旧冷启动清醒漂移；nap/medium/long/overlong → 一次结算。
    - ``apply_offline_settlement``：调用方保证恰好调用一次；各步独立容错，
      单步失败告警继续，不回滚已应用步。
    """

    def __init__(self, *, db: Any,
                 config_provider: Callable[[], Any],
                 wake_recorder: Callable[..., Any],
                 event_bus_provider: Optional[Callable[[], Any]] = None,
                 logger_provider: Optional[Callable[[], Any]] = None,
                 settlement_v2_module: Any = None) -> None:
        self._db = db
        self._config_provider = config_provider
        self._wake_recorder = wake_recorder
        self._event_bus_provider = event_bus_provider
        self._logger_provider = logger_provider
        self._settlement_v2 = settlement_v2_module

    # ── 容错告警/日志（logger 自身异常也吞掉；绝不拖挂启动链）────────────
    def _logger(self) -> Any:
        if self._logger_provider is None:
            return None
        try:
            return self._logger_provider()
        except Exception:
            return None

    def _warn(self, message: str, exc: Any) -> None:
        try:
            logger = self._logger()
            if logger is not None:
                logger.warning(message, exc)
        except Exception:
            pass

    def _info(self, message: str, *args: Any) -> None:
        try:
            logger = self._logger()
            if logger is not None:
                logger.info(message, *args)
        except Exception:
            pass

    def settle_offline_gap(self, life: Any, planner: Any, *,
                           engine_claimed: bool,
                           plan_settlement_fn: Optional[Callable[..., Any]] = None,
                           baseline_hours: float = 8.0) -> None:
        """冷启动离线 gap 收口（离线睡眠补全 A2 编排，2026-10-29；本方法不抛异常）。

        步序（指令 ①）：LifeState 冷启动已把原始离线 gap 暂存为 pending（A1）→
        ``SleepEngine.restore()`` 先认领（``engine_claimed``=恢复出未关闭会话/恢复池
        条目）→ 未被认领时按 ``planning.offline_sleep`` 分级分流：

        - 引擎认领 → 离线段归引擎连续段记账，pending gap 取走即弃（防双记账）；
        - 分级 grace（<5 分钟抖动）或结算计划生成失败 → 回退**旧冷启动清醒漂移**
          （≤24h，与重构前 ``LifeState._load`` 补算行为等价）；
        - nap/medium/long/overlong → :meth:`apply_offline_settlement` 一次结算
          （状态 gains 恰好一次 + 睡眠债 + episodes/sessions 落库 + wake 事件 +
          long/overlong 晨间联动）。

        pending gap **无条件先消费**（一次性，防后续重装配/常规 tick 重复结算）；
        任何一步异常只告警，绝不阻塞装配/插件加载（宿主契约：on_load 失败 = 插件
        加载失败）。
        """
        try:
            gap = life.consume_pending_offline_gap()
            consume_awake = getattr(life, "consume_pending_awake_before_offline_seconds", None)
            awake_before = consume_awake() if callable(consume_awake) else 0.0
        except Exception as exc:
            self._warn("离线 gap 读取失败（跳过收口，按无 pending 处理）：%s", exc)
            return
        if gap is None or (gap <= 0 and awake_before <= 0):
            return
        if engine_claimed:
            # 引擎持有未关闭睡眠会话/恢复池：离线段归引擎（连续钟/整晚规则）记账；
            # pending 已取走即弃，本编排不再补漂移或补睡（防双记账）
            self._info("离线段已由睡眠引擎 restore 认领（不重复结算）")
            return
        try:
            debt_before = float(life.snapshot().get("sleep_debt") or 0.0)
        except Exception:
            debt_before = 0.0
        plan: Any = None
        # 策略函数由组合根调用期注入（缺省用本模块导入的同名函数）——保持
        # scratch 回归经 plugin 模块名补丁策略函数的接缝可用。
        plan_fn = plan_settlement_fn if callable(plan_settlement_fn) else plan_offline_settlement
        try:
            # 复核 P0-4：基准非默认时透传（默认 8.0 保持旧二元调用，兼容回归
            # 用例注入的旧签名策略函数）
            if float(baseline_hours) != 8.0:
                plan = plan_fn(gap, debt_before, baseline_hours=float(baseline_hours))
            else:
                plan = plan_fn(gap, debt_before)
        except Exception as exc:
            self._warn("离线结算计划生成失败（回退旧冷启动漂移）：%s", exc)
        if not isinstance(plan, dict) or not plan.get("settle"):
            # grace（或计划失败）→ 旧冷启动清醒漂移：≤24h（DT_MAX_MINUTES 与
            # LifeState._dt_minutes 同钳制口径），等价重构前 _load 补算；
            # 唯一模型走 apply_offline_awake 的基础时钟积分
            try:
                dt_min = min((gap + awake_before) / 60.0, DT_MAX_MINUTES)
                if dt_min > 0:
                    life.apply_offline_awake(dt_min)
                    life.save()
                    self._info("离线 %.1f 分钟按抖动级 grace 处理（清醒回退）",
                               gap / 60.0)
            except Exception as exc:
                self._warn("离线 grace 漂移回退失败（忽略）：%s", exc)
            return
        try:
            if awake_before > 0:
                # This was observed running time, never inferred sleep.
                life.apply_offline_awake(min(awake_before / 60.0, DT_MAX_MINUTES))
            self.apply_offline_settlement(life, planner, plan)
        except Exception as exc:
            self._warn("离线睡眠结算失败（已尽力应用，忽略继续启动）：%s", exc)

    def apply_offline_settlement(self, life: Any, planner: Any, plan: Dict[str, Any]) -> None:
        """应用一次离线睡眠结算（指令 ②③；调用方已保证恰好调用一次）。

        步序：pre-offline active episode 以 interrupted 关闭（``interrupted_by=
        "offline_gap"``、进度按已用/计划封顶 0.95，走 ``ScheduleDB.interrupt``
        默认口径）→ 状态 gains 恰好一次（``apply_episode`` 显式增量门，``gains``
        里的 ``hours`` 键非 STATE_FIELDS 自动忽略）+ 睡眠债/醒沿字段（
        ``apply_sleep_settlement`` 既有一次性结算口，不走 ``handle_sleep`` 三档）
        → 一条 completed 睡眠会话（``wake_reason="offline_reconnect"``）+ 一条
        source=offline episode 足迹（计划意图**不动**）→ 醒沿只发既有
        ``life.sleep_woke``（载荷带 ``retroactive=True``；**不**补发
        ``schedule.sleep_started``——那会错误触发情绪冻结/关系日结）→ long/
        overlong 追加晨间联动（真实醒来周期同步 + 晨间 spine 调整，nap/medium
        不触发）。各步独立容错：单步失败告警继续，不回滚已应用步。
        """
        tz_name = str(getattr(getattr(self._config_provider(), "schedule_advanced", None),
                              "timezone", "") or "Asia/Shanghai")
        from planning.time_utils import TimezoneManager
        wake_at = TimezoneManager(tz_name).get_now()
        gap = max(0.0, float(plan.get("gap_seconds") or 0.0))
        sleep_start = wake_at - timedelta(seconds=gap)
        # 日帧口径与引擎 sleep_sessions 写库完全一致（入睡日历日 + 跨午夜帧分钟 >1440）
        sleep_date = sleep_start.strftime("%Y-%m-%d")
        start_minute = SleepEngine._frame_minutes(sleep_start, sleep_date)  # noqa: SLF001 - 与引擎写库同口径
        end_minute = SleepEngine._frame_minutes(wake_at, sleep_date)  # noqa: SLF001
        plan_meta = dict(plan.get("metadata") or {})
        # 复核 CF-6：幂等键改用 (入睡日历日, 入睡帧分钟)，与上面的会话/episode 日帧
        # 同源；不依赖本地墙钟日期与 int(effective_minutes) 取整（后者会让同日同
        # 分钟的不同段互相误拦，并随跨午夜/本地时区漂移）。仅当 plan 缺这两个字段
        # 时 offline_episode_id 才回退旧键——此处显式注入，保证真实结算路径不回落。
        id_plan = {**plan, "sleep_date": sleep_date, "start_minute": start_minute}
        # 1) pre-offline active episode → interrupted（进度走 interrupt 默认口径：
        #    已用/计划、上限 0.95；actual_end = 入睡时刻）
        try:
            active = self._db.active_episode()
            if isinstance(active, dict) and active.get("status") == "active":
                self._db.interrupt(str(active.get("episode_id") or ""),
                                   interrupted_by="offline_gap",
                                   actual_end_minute=start_minute,
                                   metadata={"close_reason": "offline_gap_settlement",
                                             "offline_reconnect": True})
        except Exception as exc:
            self._warn("离线结算：pre-offline active episode 关闭失败（忽略）：%s", exc)
        # 2) 状态 gains 恰好一次 + 睡眠债/醒沿（先拍前后快照供会话行 energy 端点）
        before = life.snapshot()
        # v2（2026-11-03 优化方案 §5.2）：整段睡眠按目标趋近重算增量——与在线
        # tick(sleep_rates) 的 v2 睡眠路径共用同一积分函数（settlement_v2.integrate_sleep），
        # 消除离线/在线收支分叉；睡眠债终值仍用 plan 的整晚规则（引擎同式），
        # 状态恢复只使用目标趋近增量，不应用旧曲线收益。
        try:
            gains = dict(self._settlement_v2.sleep_state_gains(
                before, float(plan.get("effective_hours") or 0.0),
                life.settlement_params if isinstance(life.settlement_params, dict)
                else self._settlement_v2.V2_PARAMS,
                float(plan.get("sleep_debt_before") or 0.0)))
            payload = {"state_deltas": gains, "settlement_kind": "offline_sleep",
                       "diag_dt_min": float(plan.get("effective_minutes") or 0.0),
                       # 复核 P2-4c/CF-6：补幂等键——life.apply_episode 的空 eid 不登记
                       # 回执，绕开 _episode_receipts 保护；稳定 id 提供纵深防御（恰好
                       # 一次仍由 pending gap 一次性消费保证）。键取入睡日帧，非墙钟日。
                       "episode_id": offline_episode_id(id_plan)}
        except Exception as exc:
            # Never mix legacy rate integration into current state accounting.
            self._warn("离线睡眠增量计算失败（仅结算睡眠债）：%s", exc)
            payload = {"state_deltas": {}, "settlement_kind": "offline_sleep",
                       "episode_id": offline_episode_id(id_plan)}
        after = life.apply_episode(payload)
        life.apply_sleep_settlement({
            "sleep_debt": plan.get("sleep_debt_after"),
            "wake_reason": "offline_reconnect",
            "now": wake_at,
        })
        session_id = f"sleep:{sleep_date}:{start_minute}"
        # 3) completed 睡眠会话（sleep_sessions 无 status 列，actual_end 非 NULL 即 completed；
        #    走 upsert_sleep_session 既有写库口径）
        self._db.upsert_sleep_session({
            "session_id": session_id,
            "schedule_date": sleep_date,
            "planned_start_minute": start_minute,
            "planned_end_minute": end_minute,
            "actual_start_minute": start_minute,
            "actual_end_minute": end_minute,
            "wake_reason": "offline_reconnect",
            "continuous_minutes": int(plan.get("effective_minutes") or 0),
            "interruption_count": 0,
            "energy_start": before.get("energy"),
            "energy_end": after.get("energy"),
            "sleep_debt_before": plan.get("sleep_debt_before"),
            "sleep_debt_after": plan.get("sleep_debt_after"),
            "quality": plan.get("quality"),
            "metadata": {"settled": True, "emitted": ["woke"],
                         "offline_settlement": True, **plan_meta},
            "created_at": sleep_start.isoformat(),
            "updated_at": wake_at.isoformat(),
        })
        # 4) source=offline episode 足迹（start→finish 两写复用既有公开口；
        #    finish 默认进度 = 已用/计划封顶 0.95；close_reason 带引擎前缀，
        #    与 _reconcile_recorded_wake 的恢复口径兼容）
        try:
            eid = self._db.start_episode(
                sleep_date, str(plan.get("label") or "睡眠"), "sleep", "offline",
                start_minute, end_minute, actual_start_minute=start_minute,
                metadata={**plan_meta,
                          "close_reason": "sleep_engine_offline_reconnect",
                          "offline_settlement": True})
            if eid:
                self._db.finish(eid, actual_end_minute=end_minute)
        except Exception as exc:
            self._warn("离线结算：episode 足迹写入失败（忽略）：%s", exc)
        # 5) 醒沿事件：既有 life.sleep_woke（payload 形态与引擎 _close_session 一致
        #    + retroactive=True + session_id）；全树无订阅方（grep 核对），纯观测。
        #    绝不补发 schedule.sleep_started（emotion 冻结/关系日结挂在它上面）。
        try:
            bus = self._event_bus_provider() if self._event_bus_provider is not None else None
            if bus is not None:
                bus.publish_soon(
                    EVENT_SLEEP_WOKE,
                    session_id=session_id,
                    wake_reason="offline_reconnect",
                    actual_minutes=round(gap / 60.0, 1),
                    sleep_hours=round(float(plan.get("effective_hours") or 0.0), 4),
                    energy=after.get("energy"),
                    quality=plan.get("quality"),
                    sleep_debt=plan.get("sleep_debt_after"),
                    retroactive=True)
        except Exception as exc:
            self._warn("离线醒沿事件发布失败（忽略）：%s", exc)
        # 6) 晨间联动：仅 long/overlong（分级定义即 ≥4h；nap/medium 不触发）。
        #    wake_delay 离线路径无引擎 earliest_wake 基准，按 0（不改意图计划时间）。
        if plan.get("category") in ("long", "overlong"):
            try:
                self._wake_recorder(when=wake_at)
                planner.apply_morning_spine_adjustment(
                    sleep_hours=plan.get("effective_hours"), wake_delay_minutes=0.0)
            except Exception as exc:
                self._warn("离线晨间 spine 调整失败（忽略）：%s", exc)
        self._info(
            "离线睡眠结算完成（分级=%s，有效 %.2fh，未记录 %.2fh，债 %.2f→%.2f）",
            plan.get("category"), float(plan.get("effective_hours") or 0.0),
            float(plan.get("unrecorded_seconds") or 0.0) / 3600.0,
            float(plan.get("sleep_debt_before") or 0.0),
            float(plan.get("sleep_debt_after") or 0.0))
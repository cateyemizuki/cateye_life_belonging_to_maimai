"""`/lbm_life rolling` 只读诊断渲染（P2-06 自 companion/components.py 下沉）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.time_utils import minutes_of_now
from planning.rolling_planner import extract_spine_intents
from planning.time_utils import format_day_minute, ring_minutes_ahead


def render_rolling(host: Any, args: str) -> str:
    """滚动化调试命令主体（/lbm_life rolling <status|state|episodes|spine|sleep|preview>）。

    R7 依据 = 用户 2026-10-25 直派（日程系统滚动化改造，设计目录
    D:/deepseek-harness_workspace/maibot-plugins/2026-10-25-日程系统滚动化改造设计）。
    纯只读：零 LLM、零消息发送（返回文本由 /lbm_life 命令层统一发送）、
    零副作用（不写库/不写状态文件/不推进 tick/不掷骰）；任何异常降级为错误
    提示绝不抛；输出零 emoji。实现集中于本单一函数（含全部子渲染与句柄解析），
    命令分发表只加一行（qzone/components.py 的 ``rolling`` 分支）——移除时
    删本函数 + 一行即净（「便利移除」用户要求）。

    服务句柄解析序（接线阶段 PlanningServices 挂 ``life_state`` /
    ``rolling_planner`` / ``life_schedule`` 三属性后自动点亮）：
    PlanningServices 属性优先，读模型回退 ``ScheduleQuery._runtime_provider``，
    planner / life_state 回退读模型私有 ``_planner`` / ``_life``，db 回退
    ``plugin._db``。开关判定 = ``[schedule].enabled`` 唯一总开关（PR8 3.0.0；
    旧 ``[life_state].enabled`` / ``[schedule].spine_mode`` 已退役，不得再读）；
    总开关关闭 → 「rolling 未启用」提示，开启但服务未接线 → 就绪=否。
    """
    usage = (
        "用法: /lbm_life rolling <status|state|episodes|spine|sleep|preview>"
        "（管理员；纯只读，零 LLM 零发送零副作用）\n"
        "  status   — 总览（总开关/就绪/当前 episode/next 2 条/状态行/今日 episode 计数）\n"
        "  state    — LifeState 全字段数值 + 最近阈值跨越\n"
        "  episodes — 今日 episode 逐条（状态/进度/计划 vs 实际/来源/原因）\n"
        "  spine    — 今日 spine 意图与硬约定及参数\n"
        "  sleep    — 睡眠引擎/本夜睡眠会话只读状态（不 tick、不掷点、不落盘）\n"
        "  preview  — 下一 tick 候选 top3 与分数（planner 只读预演，不落库）"
    )
    arg_parts = (args or "").split(maxsplit=1)
    sub = (arg_parts[0] if arg_parts else "").strip().lower()
    rest = arg_parts[1:]
    if sub in ("", "help", "usage"):
        return usage
    try:
        # ── 句柄解析（全部可空降级；接线前各服务缺失不报错）────────────
        planning = getattr(host, "_planning", None)
        query = getattr(host, "_schedule_query", None)
        model = getattr(planning, "life_schedule", None)
        if model is None:
            model = getattr(query, "_runtime_provider", None)
        planner = getattr(planning, "rolling_planner", None)
        if planner is None and model is not None:
            planner = getattr(model, "_planner", None)
        life = getattr(planning, "life_state", None)
        if life is None and model is not None:
            life = getattr(model, "_life", None)
        db = getattr(host, "_db", None)
        if db is None and model is not None:
            db = getattr(model, "_db", None)
        # 睡眠引擎（PR6 只读展示用；PR3-review 装配在 plugin._sleep_engine，
        # 也兼容 PlanningServices.sleep_engine 与 planner._sleep_engine 三处句柄）
        engine = getattr(planning, "sleep_engine", None)
        if engine is None:
            engine = getattr(host, "_sleep_engine", None)
        if engine is None and planner is not None:
            engine = getattr(planner, "_sleep_engine", None)

        def _cfg_on(section: str, key: str) -> bool:
            try:
                return bool(getattr(getattr(host.config, section, None), key, False))
            except Exception:
                return False

        # PR8 3.0.0：日程唯一总开关 = [schedule].enabled；就绪只看真实服务句柄，
        # 不再用已退役的 spine_mode / life_state.enabled 覆盖统一开关。
        schedule_on = _cfg_on("schedule", "enabled")
        runtime_ready = model is not None and planner is not None and life is not None
        if not schedule_on:
            return "rolling 未启用（[schedule].enabled=false）"

        now = datetime.now()
        date = now.strftime("%Y-%m-%d")
        cur = minutes_of_now(now)

        def _pct(value: Any) -> str:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return f"{int(round(max(0.0, min(1.0, float(value))) * 100))}%"
            return "-"

        # ── status：总览 ──────────────────────────────────────────────
        def _lines_status() -> str:
            out = [f"[rolling status] {date} {now.strftime('%H:%M')}"]
            out.append(f"开关：[schedule].enabled={schedule_on}")
            out.append(f"就绪：{'是' if runtime_ready else '否'}"
                       f"（runtime/planner/life_state 三者齐备）")
            out.append(f"接线：read_model={'是' if model is not None else '否'}"
                       f"｜planner={'是' if planner is not None else '否'}"
                       f"｜life_state={'是' if life is not None else '否'}")
            current = None
            if model is not None:
                try:
                    current = model.current(now) or None
                except Exception as exc:
                    out.append(f"当前 episode：读取失败（{exc}）")
            if current is None:
                out.append("当前 episode：无（空闲）")
            elif isinstance(current, dict):
                segs = [str(current.get("name") or "?")]
                for label, key in (("类型", "goal_type"), ("状态", "status"),
                                   ("注意力", "attention_mode"), ("来源", "source")):
                    value = str(current.get(key) or "").strip()
                    if value:
                        segs.append(f"{label} {value}")
                segs.append(f"进度 {_pct(current.get('progress'))}")
                try:
                    s = int(current.get("start_minute") or 0)
                    e = int(current.get("end_minute") or 0)
                    segs.append(f"时间 {format_day_minute(s)}-{format_day_minute(e)}")
                    if s > 0:
                        # 已进行 = 环距「经过」方向（2026-10-25 门禁员修新代码 bug，R7 依据 =
                        # 用户 2026-10-25 直派【滚动化改造】；与 inject_service._life_block_lines
                        # 同款换参：ring_minutes_ahead(cur, s)，防 start<cur 环回大数）
                        segs.append(f"已进行 {ring_minutes_ahead(cur, s)} 分钟")
                    if e > 0:
                        segs.append(f"约还剩 {ring_minutes_ahead(e, cur)} 分钟")
                except (TypeError, ValueError):
                    pass
                out.append("当前 episode：" + "｜".join(segs))
            nxt: list = []
            if model is not None:
                try:
                    nxt = list(model.next(now, limit=2) or [])
                except Exception as exc:
                    out.append(f"next 读取失败：{exc}")
            parts: list = []
            for item in nxt:
                if not isinstance(item, dict):
                    continue
                try:
                    s = int(item.get("start_minute") or 0)
                except (TypeError, ValueError):
                    continue
                mark = ("（可恢复）" if item.get("paused")
                        else "（预演）" if item.get("is_preview")
                        else "（意图）" if item.get("is_spine_intent") else "")
                parts.append(f"{format_day_minute(s)} {item.get('name')}{mark}")
            out.append("next：" + ("；".join(parts) if parts else "无"))
            state_line = ""
            if model is not None:
                try:
                    state_line = str(model.state_line(now, max_items=2) or "").strip()
                except Exception:
                    state_line = ""
            if not state_line and life is not None:
                try:
                    state_line = str(life.describe(now, max_items=2) or "").strip()
                except Exception:
                    state_line = ""
            out.append(state_line or "状态行：不可用（LifeState 未接线或无读数）")
            summary: Any = None
            if model is not None:
                try:
                    summary = (model.today(now) or {}).get("summary")
                except Exception:
                    summary = None
            if isinstance(summary, dict):
                out.append(
                    "今日 episode：意图 "
                    f"{summary.get('intent_count', 0)}｜episode {summary.get('episode_count', 0)}"
                    f"｜完成 {summary.get('completed_count', 0)}"
                    f"｜打断 {summary.get('interrupted_count', 0)}"
                    f"｜放弃 {summary.get('abandoned_count', 0)}")
            else:
                out.append("今日 episode：计数不可用（读模型未接线）")
            info = _sleep_fields()
            if info:
                asleep = info.get("asleep")
                out.append("睡眠：" + ("引擎未表态" if asleep is None
                                      else ("睡眠中" if asleep else "已醒"))
                           + f"｜连续 {info.get('continuous_minutes', 0)} 分钟"
                           + f"｜打断 {info.get('interruption_count', 0)} 次")
            return "\n".join(out)

        # ── state：LifeState 全字段 + 最近阈值跨越 ─────────────────────
        def _lines_state() -> str:
            out = ["[rolling state] LifeState 全字段（五项 0~1；sleep_debt 0~12h）"]
            if life is None:
                out.append("LifeState 未接线（等待接线阶段实例化 LifeStateService）。")
                return "\n".join(out)
            try:
                snap = dict(life.snapshot() or {})
            except Exception as exc:
                out.append(f"snapshot 读取失败（已降级）：{exc}")
                return "\n".join(out)
            for key in ("energy", "hunger", "focus", "stress", "social_battery",
                        "sleep_debt", "mental_state"):
                if key in snap:
                    out.append(f"{key} = {snap[key]}")
            for key in ("last_meal_at", "last_break_at", "last_sleep_at",
                        "last_tick_at", "updated_at"):
                if key in snap:
                    out.append(f"{key} = {snap.get(key) or '（无）'}")
            # 最近阈值跨越：决策账本 rolling.state_changed 条目（新→旧，取最近一条）
            crossed = ""
            logger_ = (getattr(host, "_decision_logger", None)
                       or getattr(host._mai_companion(), "decision_logger", None))
            if logger_ is not None:
                try:
                    for entry in logger_.read_recent(days=1, limit=300):
                        if str(entry.get("action") or "") != "rolling.state_changed":
                            continue
                        crosses = entry.get("crosses")
                        texts = []
                        for c in crosses if isinstance(crosses, list) else []:
                            if isinstance(c, dict):
                                texts.append(f"{c.get('field')} {c.get('direction')}"
                                             f"（{c.get('value')}）")
                        if texts:
                            crossed = f"{entry.get('time', '')} " + "；".join(texts)
                            break
                except Exception:
                    crossed = ""
            out.append(f"最近阈值跨越：{crossed or '（最近 1 天无 life.state_changed 记录）'}")
            flags = getattr(planner, "_threshold_flags", None)
            if isinstance(flags, dict) and flags:
                out.append("当前滞回旗标（rolling_planner.HYSTERESIS_ENTER/EXIT）：" + "｜".join(
                    f"{k}={'在阈值带' if v else '带外'}" for k, v in flags.items()))
            return "\n".join(out)

        # ── episodes：今日 episode 逐条 ───────────────────────────────
        def _reason_of(row: dict) -> str:
            meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            for key in ("close_reason", "pause_reason", "abandon_reason", "reason"):
                if meta.get(key):
                    return str(meta[key])
            if row.get("interrupted_by"):
                return str(row.get("interrupted_by"))
            return ""

        def _span(start: Any, end: Any) -> str:
            try:
                return f"{format_day_minute(int(start))}-{format_day_minute(int(end))}"
            except (TypeError, ValueError):
                return "?"

        def _lines_episodes() -> str:
            out = [f"[rolling episodes] {date}"]
            rows: list = []
            if model is not None:
                try:
                    rows = list((model.today(now) or {}).get("episodes") or [])
                except Exception as exc:
                    out.append(f"episodes 读取失败：{exc}")
            if not rows and db is not None:
                try:
                    rows = list(db.episodes_today(date) or [])
                except Exception:
                    rows = []
            if not rows:
                out.append("今日还没有 episode 记录（planner 未开块或未接线）。")
                return "\n".join(out)
            out.append(f"共 {len(rows)} 条：")
            for idx, row in enumerate(rows, 1):
                if not isinstance(row, dict):
                    continue
                plan_txt = _span(row.get("planned_start_minute"),
                                 row.get("planned_end_minute"))
                a_s = row.get("actual_start_minute")
                a_e = row.get("actual_end_minute")
                act_txt = (_span(a_s, a_e) if a_s is not None and a_e is not None
                           else "未记录")
                reason = _reason_of(row)
                out.append(
                    f"{idx}. {row.get('name')}｜状态 {row.get('status')}"
                    f"｜进度 {_pct(row.get('progress'))}｜计划 {plan_txt}｜实际 {act_txt}"
                    f"｜来源 {row.get('source') or '-'}"
                    + (f"｜原因 {reason}" if reason else ""))
            return "\n".join(out)

        # ── spine：今日意图与硬约定及参数 ─────────────────────────────
        def _lines_spine() -> str:
            out = [f"[rolling spine] {date}"]
            intents: list = []
            if db is not None:
                try:
                    intents = extract_spine_intents(db.get_day_schedule(date))
                except Exception as exc:
                    out.append(f"spine 意图读取失败：{exc}")
            if not intents:
                out.append("今日没有 spine 意图行（今日尚未生成 spine 日程，"
                           "或旧日表未重新生成）。")
                return "\n".join(out)
            hard = [i for i in intents if i.get("hardness") == "hard"]
            out.append(f"意图 {len(intents)} 条（硬约定 {len(hard)} 条）：")
            for idx, it in enumerate(intents, 1):
                tag = "硬约定" if it.get("hardness") == "hard" else "软意图"
                note = str(it.get("note") or it.get("description") or "").strip()
                out.append(
                    f"{idx}. [{format_day_minute(it['start_minute'])}-"
                    f"{format_day_minute(it['end_minute'])}] {it['name']}"
                    f"｜{it['goal_type']}｜{tag}｜优先级 {it['priority']}"
                    f"｜时长 {it['min_minutes']}~{it['max_minutes']} 分钟"
                    f"｜可中断 {float(it['interruptibility']):.2f}"
                    + (f"｜note {note}" if note else ""))
            return "\n".join(out)

        # ── preview：下一 tick 候选 top3（只读预演）───────────────────
        def _lines_preview() -> str:
            out = ["[rolling preview] 下一 tick 候选 top3"
                   "（planner 只读预演：不落库、不消耗当日 RNG）"]
            if planner is None:
                out.append("RollingPlanner 未接线（等待接线阶段实例化）。")
                return "\n".join(out)
            try:
                entries = list(planner.preview(now, limit=3) or [])
            except Exception as exc:
                out.append(f"preview 读取失败（已降级）：{exc}")
                return "\n".join(out)
            if not entries:
                out.append("无候选（spine 为空且无锚点命中）。")
                return "\n".join(out)
            for idx, e in enumerate(entries, 1):
                if not isinstance(e, dict):
                    continue
                win_txt = ""
                window = e.get("window")
                if isinstance(window, (tuple, list)) and len(window) == 2:
                    win_txt = f"｜窗口 {_span(window[0], window[1])}"
                mm = f"｜时长 {e.get('min_minutes')}~{e.get('max_minutes')} 分钟"
                out.append(
                    f"{idx}. {e.get('name')}（{e.get('goal_type')}）"
                    f"｜kind {e.get('kind')}｜score {e.get('score')}"
                    f"｜priority {e.get('priority_score')}{win_txt}{mm}"
                    + (f"｜resume_of {e.get('resume_of')}" if e.get("resume_of") else ""))
            return "\n".join(out)

        # ── sleep：睡眠引擎只读状态（PR6；不 tick/不掷点/不写状态）─────────
        def _sleep_fields() -> dict:
            """引擎诊断（只读；异常 → 空 dict）。"""
            if engine is None:
                return {}
            try:
                return dict(engine.diagnostics() or {})
            except Exception:
                return {}

        def _lines_sleep() -> str:
            out = [f"[rolling sleep] {date} {now.strftime('%H:%M')}"]
            if engine is None:
                out.append("睡眠引擎未装配（[schedule].enabled=false "
                           "或未接线）。")
            else:
                info = _sleep_fields()
                out.append(f"引擎：enabled={info.get('enabled')}"
                           + (f"｜{info.get('warning')}" if info.get("warning") else ""))
                asleep = info.get("asleep")
                if asleep is None:
                    out.append("本夜状态：引擎不表态（未入睡/已过上限/未启用）")
                else:
                    out.append("本夜状态：" + ("睡眠中" if asleep else "已醒"))
                out.append(f"会话：{info.get('session_id') or '无'}"
                           f"｜连续 {info.get('continuous_minutes', 0)} 分钟"
                           f"｜打断 {info.get('interruption_count', 0)} 次")
                wake_reason = str(info.get("wake_reason") or "").strip()
                out.append(f"醒因：{wake_reason or '（无）'}"
                           f"｜最早醒沿 {info.get('wake_at') or '（无）'}"
                           f"｜赖床上限 {info.get('lie_in_cap') or '（无）'}")
            if life is not None:
                try:
                    snap = dict(life.sleep_status() or {})
                except Exception:
                    snap = {}
                if snap:
                    out.append("LifeState 睡眠字段："
                               + "｜".join(f"{k}={v}" for k, v in snap.items()))
            out.append("（纯只读：不调用 tick/force_wake，不推进睡眠时钟，不写状态文件）")
            return "\n".join(out)

        renderers = {
            "status": _lines_status,
            "state": _lines_state,
            "episodes": _lines_episodes,
            "spine": _lines_spine,
            "sleep": _lines_sleep,
            "preview": _lines_preview,
        }
        renderer = renderers.get(sub)
        if renderer is None:
            return f"未知子命令: {sub}\n{usage}"
        return renderer()
    except Exception as exc:
        # 异常降级为错误提示，绝不抛（纯只读诊断不影响任何链路）
        return f"rolling 诊断读取失败（已降级）：{exc}"

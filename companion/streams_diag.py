"""诊断流记录渲染（P2-06 自 companion/components.py 下沉；只读、无副作用）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional


async def build_streams_records(host: Any) -> list[dict[str, Any]]:
    """``/lbm_diag streams`` 的合并转发节点主体：主动候选流与名单状态（2026-10-20 用户指示）。

    节点布局（无 emoji，便于日志粘贴；§25.2 records 化，§26.1 展示重构，§29.1 序号根治）：

    1. ``[名单状态]``：``[planning_proactive]`` 的群/私聊名单——标题行（mode + 模式
       后标注留空语义：黑名单「留空=全生效」、白名单「留空=无人」）+ 每个配置 id
       一行（R7 依据 = 用户 2026-10-22 第三批直派，计划书 §二十八：ids 原文逐行
       回显、去引号；空名单保留单行、标题行后内联「（空）」）；另含
       ``[patrol].target_qq`` 与 ``enabled``、早/晚安两个总开关；
    2. ``[候选解析]``：``host._planning.proactive`` 可用时调
       ``_resolve_greeting_streams_universe_readonly()``（§29.1：R7 依据 = 用户
       2026-10-23 直派，计划书 §二十九）——候选/序号基底换**只读全集枚举**
       （群聊生效集 ∪ 其他私聊生效集，只做名单解析、不做今日硬门槛过滤；桩服务无
       该支路时退回只读候选支路 ``_resolve_greeting_candidates_readonly``，不再回退
       正式枚举写缓存分支）与 ``_resolve_target_streams_readonly()``；不可用 →
       「规划束未装配」；``[口径]`` 行并入本节点结尾。§31.3 三流分族（R7 =
       用户 2026-10-23 第二轮直派，计划书 §31.1/§31.3）起**目标流回归诊断可见**：
       「问候·群聊」/「问候·其他私聊」/「问候·目标私聊（无条件）」/「频率调控
       （群聊∪私聊）」四个标题行（§二十八逐行化形态保留：标题行后每流一行、
       两空格缩进 + ``#NN 显示名``——频控行纯显示名、无序号；序号 = 群聊 → 其他
       私聊 → 目标私聊，既有序号零漂移）；[候选解析] 的「问候·群聊」行对判定窗查
       不到消息的群聊流行尾加注「（硬闸窗内无消息·不发晨晚问候）」（§29.1 静默群不
       丢弃语义随新窗重锚），``[patrol].target_qq`` 配置行照旧保留在块 1；
    3. ``[候选流最后一条非命令、非 bot 消息·群聊 时段：… (第 i/N 片)]`` 与
       ``[候选流最后一条非命令、非 bot 消息·私聊 时段：…（目标/其他）(第 i/N 片)]``：
       群聊单列、其他私聊与目标流并入「私聊」节点（§31.3 三流分族：目标流回归诊断
       可见、进私聊切片）；两类各按枚举序**每 10 个流切一片**，**无上限、不折叠**。
       检查窗（§31.3，R7 = 用户 2026-10-23 第二轮直派）：三族同窗 =
       **[上份日程开始 → 本份日程开始)**（早安判定窗，经 ``svc.greeting_cycle_span``
       周期四变量读口，**不再看日历 00:00**；窗端不可得 → 行面「周期不可得」）。
       每行走 ``get_by_time_in_chat``，过滤空白/斜杠命令后取最新一条的 HH:MM；
       无 → 历史无消息（静默群聊照常占序号、进切片节点）；查询异常 → 查询失败(宁不发)。
       无任何候选流 → 单节点 ``[候选流最后一条非命令、非 bot 消息]`` + ``无候选流``。

    序号（§26.1 同源直派 → §27.2 豁免 → §29.1 全集 → §31.3 三流分族，R7 = 用户
    2026-10-23 第二轮直派）：每个问候候选流一个 ``#NN``（两位补零，≥100 自然进位），
    编号顺序 = **群聊（枚举序）→ 其他私聊（枚举序）→ 目标私聊（无条件族殿后）**，
    自 ``#01`` 起；行 = ``#NN {label}: {结果}``，``[候选解析]`` 块的每流行同步带
    ``#NN``。频率调控-only 流（不在问候候选）不编号、不进 [候选流最后…] 块。
    **全集枚举基底 → 跨帧稳定**（只受名单/宿主枚举变化影响），主查询与子查询
    （``_mai_diag_stream_index``）同序号系。

    诊断纯读（§29.1，R7 依据 = 用户 2026-10-23 直派，计划书 §二十九）：整链改走
    只读全集枚举，不再调 ``_resolve_greeting_candidates``（正式写缓存分支）/ 不经
    ``_group_floor_ok(use_cache=True)``——diag 不再写 ``_greeting_group_floor``
    与名单告警缓存（``_greeting_list_warned``），§26.2「快照纯读」从子查询扩到全链。

    节点记录形态与 ``_mai_build_llm_log_records`` 逐字一致：
    ``{"user_id": "0", "nickname": "streams 诊断",
    "segments": [{"type": "text", "content": <节点文本>}]}``。

    显示口径（R7 依据 = 用户 2026-10-21 追加三直派①，计划书 §二十五）：块 2 的候选
    列表与块 3 的每行流展示位一律显示**名称而非纯 session_id**——经
    ``ProactiveService.stream_display_labels()`` 取「session_id → 显示名」映射
    （群 = group_name；私聊 = user_cardname 优先、user_nickname 次之）；名称不可得 →
    回显原 session_id（``{label}``，兜底 ``{sid}``）。块 1 名单 ids 与
    ``[patrol].target_qq`` 行仍是**配置原文**（配置回显不换名）。

    整体 try 兜底：任何异常都只降级为单节点错误文本，不向调用方抛。
    """
    def _record(text: str) -> dict[str, Any]:
        return {"user_id": "0", "nickname": "streams 诊断",
                "segments": [{"type": "text", "content": text}]}

    try:
        b1: List[str] = ["[名单状态]"]
        cfg_all = getattr(host, "config", None)
        cfg_p = getattr(cfg_all, "planning_proactive", None)
        patrol = getattr(cfg_all, "patrol", None)

        if cfg_p is None:
            b1.append("  [planning_proactive] 配置不可用")
        else:
            g_ids = list(getattr(cfg_p, "proactive_group_ids", None) or [])
            g_mode = str(getattr(cfg_p, "proactive_group_list_mode", "blacklist")
                         or "blacklist")
            p_ids = list(getattr(cfg_p, "proactive_private_ids", None) or [])
            p_mode = str(getattr(cfg_p, "proactive_private_list_mode", "whitelist")
                         or "whitelist")
            # §28.1（R7 依据 = 用户 2026-10-22 第三批直派，计划书 §二十八）：名单拆
            # 「标题行（mode + 留空语义）+ 每个配置 id 一行」，ids 原文回显去引号；
            # 空名单保留单行（标题行后内联「（空）」）。
            b1.append(f"  群聊名单: mode={g_mode}"
                      f"{'（留空=全生效）' if g_mode == 'blacklist' else '（留空=无人）'}"
                      f"{'' if g_ids else '（空）'}")
            for gid in g_ids:
                b1.append(f"  {gid}")
            b1.append(f"  私聊名单: mode={p_mode}"
                      f"{'（留空=全生效）' if p_mode == 'blacklist' else '（留空=无人）'}"
                      f"{'' if p_ids else '（空）'}")
            for pid in p_ids:
                b1.append(f"  {pid}")
            b1.append(
                f"  问候开关: morning={bool(getattr(cfg_p, 'greeting_morning_enabled', True))}"
                f" night={bool(getattr(cfg_p, 'greeting_night_enabled', True))}"
            )
            # §31.3（R7 = 用户 2026-10-23 第二轮直派）：闸门判据一行留痕
            b1.append(
                f"  判据: necessity={bool(getattr(cfg_p, 'greeting_require_activation', True))}"
                f" 早安阈值={int(getattr(cfg_p, 'greeting_other_private_min_messages', 5) or 5)}"
                f" 晚安阈值={int(getattr(cfg_p, 'greeting_private_night_min_messages', 5) or 5)}"
            )
            try:
                _sadv = getattr(cfg_all, "schedule_advanced", None)
                b1.append(
                    f"  补发冷静期={int(getattr(_sadv, 'greeting_min_silence_minutes', 3) or 0)} 分钟"
                )
            except Exception:
                pass
        if patrol is None:
            b1.append("  [patrol] 配置不可用")
        else:
            b1.append(
                f"  [patrol] target_qq={getattr(patrol, 'target_qq', '') or '（未设置）'}"
                f" enabled={bool(getattr(patrol, 'enabled', False))}"
            )

        # F1（B1）：实验开关开时才多一行「群注意力」；默认关输出与既有逐字一致。
        try:
            _attention = getattr(host, "_group_attention", None)
            if _attention is not None and _attention.enabled():
                _snap = dict(_attention.snapshot() or {})
                _decaying = sum(1 for item in _snap.values()
                                if float(item.get("multiplier", 1.0)) < 0.999)
                b1.append(f"  群注意力: 跟踪={len(_snap)} 衰减中={_decaying}")
        except Exception:
            pass

        b2: List[str] = ["[候选解析]"]
        planning = getattr(host, "_planning", None)
        svc = getattr(planning, "proactive", None)
        greeting_cands: List[Any] = []
        target_streams: List[str] = []
        stream_nums: Dict[str, int] = {}
        # 显示名映射：svc 不可用/取映射失败 → 保持空 dict，全部展示位回显原 sid
        stream_labels: Dict[str, str] = {}
        if svc is None:
            b2.append("  规划束未装配（_planning.proactive 不可用）")
        else:
            effective_cfg = cfg_p if cfg_p is not None else svc._proactive_cfg()
            # §29.1（R7 依据 = 用户 2026-10-23 直派，计划书 §二十九）：候选/序号基底
            # 换只读全集枚举（群聊生效集 ∪ 其他私聊生效集，不做今日硬门槛过滤；目标流
            # 不进枚举）→ 序号建立在名单全集上、同序号恒指向同一流。整链纯读：不回退
            # 正式枚举写缓存分支（_resolve_greeting_candidates 不再在此出现）。
            universe_resolver = getattr(
                svc, "_resolve_greeting_streams_universe_readonly", None)
            if callable(universe_resolver):
                try:
                    greeting_cands = list(await universe_resolver() or [])
                except Exception as exc:
                    b2.append(f"  问候候选解析失败: {exc}")
            else:
                # 桩服务无全集支路 → 退回只读候选支路（同样纯读；该支路仍做群聊
                # floor 只读实查，仅影响容量不影响纯读纪律——universe 存在时不走它）
                readonly_resolver = getattr(
                    svc, "_resolve_greeting_candidates_readonly", None)
                if callable(readonly_resolver):
                    try:
                        greeting_cands = list(
                            await readonly_resolver(effective_cfg) or [])
                    except Exception as exc:
                        b2.append(f"  问候候选解析失败: {exc}")
            # §29.1 诊断全链纯读：频控行同步换只读支路（写告警/解析缓存的正式支路
            # 仅供旧桩服务兜底；真实服务的 readonly 支路恒先命中）
            target_resolver = getattr(svc, "_resolve_target_streams_readonly", None)
            try:
                if callable(target_resolver):
                    target_streams = list(await target_resolver() or [])
                else:
                    target_streams = list(await svc._resolve_target_streams() or [])
            except Exception as exc:
                b2.append(f"  频率调控候选解析失败: {exc}")
            try:
                stream_labels = dict(await svc.stream_display_labels() or {})
            except Exception as exc:
                host.ctx.logger.debug(f"获取流显示名失败（按纯 sid 回显）: {exc}")

            by_cat = {"target": [], "group": [], "other": []}
            for item in greeting_cands:
                try:
                    cand_sid, cand_cat = item
                except (TypeError, ValueError):
                    continue
                by_cat.setdefault(str(cand_cat), []).append(str(cand_sid))
            # §27.2 序号 → §31.3 三流分族（R7 = 用户 2026-10-23 第二轮直派）：
            # 编号顺序 = 群聊（枚举序）→ 其他私聊（枚举序）→ 目标私聊（无条件族殿后，
            # 既有序号零漂移），自 #01 起；同一次调用内稳定。
            ordered_pairs: List[tuple] = []
            numbered_seen: set = set()
            for cand_cat in ("group", "other", "target"):
                for cand_sid in by_cat.get(cand_cat, []):
                    if cand_sid and cand_sid not in numbered_seen:
                        ordered_pairs.append((cand_sid, cand_cat))
                        numbered_seen.add(cand_sid)
            for cand_cat, cand_sids in by_cat.items():
                if cand_cat in ("target", "group", "other"):
                    continue
                for cand_sid in cand_sids:
                    if cand_sid and cand_sid not in numbered_seen:
                        ordered_pairs.append((cand_sid, cand_cat))
                        numbered_seen.add(cand_sid)
            stream_nums = {
                s: idx for idx, (s, _cat) in enumerate(ordered_pairs, start=1)}

            def _num_label(cand_sid: str) -> str:
                """``#NN 显示名``（名称不可得回显原 sid；未编号则只显示名）。"""
                label = stream_labels.get(cand_sid, cand_sid)
                num = stream_nums.get(cand_sid)
                return f"#{num:02d} {label}" if num else label

            # §29.1（R7 依据 = 用户 2026-10-23 直派，计划书 §二十九）：消息实查**前移**
            # ——先对全集流做一次 ``get_by_time_in_chat`` 窗查询，产出「流 → 行面状态」
            # 映射（``last_lines``）+ 「当日无消息群聊」集合（``silent_group_sids``），
            # 供 [候选解析] 群聊行尾加注与切片节点共用；同一帧、同一次查询，两处行面
            # 状态恒一致。纯读：直接走宿主消息接口，不经 ``_group_floor_ok``（不写
            # ``_greeting_group_floor`` 门槛缓存）。
            # §31.3（R7 = 用户 2026-10-23 第二轮直派）：检查窗 = **周期判定窗**
            # [上份日程开始 → 本份日程开始)——经 svc 周期四变量读口
            # ``greeting_cycle_span``，不再看日历 0 点；窗端不可得（0/非法）→
            # 行面统一「周期不可得」。
            try:
                _span = dict(svc.greeting_cycle_span() or {})
            except Exception:
                _span = {}
            _window_lo = float(_span.get("prev_start") or 0)
            _window_hi = float(_span.get("cur_start") or 0)
            span_ok = _window_lo > 0 and _window_hi > 0
            _window_fmt = "%Y-%m-%d %H:%M"
            if span_ok:
                span_text = (f"{datetime.fromtimestamp(_window_lo).strftime(_window_fmt)}"
                             f" → "
                             f"{datetime.fromtimestamp(_window_hi).strftime(_window_fmt)}")
            else:
                span_text = "周期四变量不可得"
            group_window = private_window = span_text
            query_start_ts = _window_lo
            query_end_ts = _window_hi

            ctx_obj = getattr(host, "ctx", None)
            getter = getattr(getattr(ctx_obj, "message", None),
                             "get_by_time_in_chat", None)
            last_lines: Dict[str, str] = {}
            silent_group_sids: set = set()
            if not span_ok:
                for sid, _cat in ordered_pairs:
                    head = f"  #{stream_nums.get(sid, 0):02d} {stream_labels.get(sid, sid)}"
                    last_lines[sid] = f"{head}: 周期不可得(宁不发)"
            elif callable(getter):
                for sid, cat in ordered_pairs:
                    label = stream_labels.get(sid, sid)
                    head = f"  #{stream_nums.get(sid, 0):02d} {label}"
                    start_ts = query_start_ts
                    try:
                        msgs = await getter(
                            sid, str(start_ts), str(query_end_ts), limit=10, filter_mai=True)
                    except Exception:
                        try:
                            msgs = await getter(
                                sid, str(start_ts), str(query_end_ts), limit=10)
                        except Exception:
                            last_lines[sid] = f"{head}: 查询失败(宁不发)"
                            continue
                    if isinstance(msgs, dict):
                        msgs = msgs.get("messages") or msgs.get("data") or []
                    if not isinstance(msgs, list):
                        last_lines[sid] = f"{head}: 查询失败(宁不发)"
                        continue
                    best_ts: Optional[float] = None
                    best_hm = ""
                    found = False
                    for msg in msgs:
                        if not isinstance(msg, dict):
                            continue
                        if any(msg.get(k) is True for k in ("is_mai", "is_bot", "is_self")):
                            continue
                        text = str(msg.get("processed_plain_text") or "").strip()
                        if not text or text.startswith("/"):
                            continue
                        found = True
                        try:
                            ts_f = float(msg.get("timestamp"))
                            hm = datetime.fromtimestamp(ts_f).strftime("%H:%M")
                        except (TypeError, ValueError, OSError):
                            ts_f = 0.0
                            hm = ""
                        if best_ts is None or ts_f >= best_ts:
                            best_ts = ts_f
                            best_hm = hm
                    if not found:
                        # §27.2（R7）：全体面文案统一为「历史无消息」；§29.1：全集中的
                        # 静默群聊不丢弃——行面状态进行尾加注（问候·群聊行）与切片节点
                        if cat == "group":
                            silent_group_sids.add(sid)
                        last_lines[sid] = f"{head}: 历史无消息"
                    else:
                        last_lines[sid] = f"{head}: {best_hm or '有消息（时间未知）'}"

            # R7 依据 = 用户 2026-10-21 追加三直派①（计划书 §二十五）：
            # 流展示位显示名替代纯 sid；名称不可得 → .get(sid, sid) 回显原 sid。
            # §27.2（R7 依据 = 用户 2026-10-22 第二批四直派，计划书 §二十七）：
            # 陪伴目标聊天流整体不出现——目标问候列表行删除。
            # §28.1（R7 依据 = 用户 2026-10-22 第三批直派，计划书 §二十八）：三个
            # 列表行改「标题行 + 每流一行」，候选行 = #NN 显示名（枚举序与切片/序号
            # 一致、stream_nums 既有）、频控行 = 纯显示名无序号；回显去引号，空列表
            # 时标题行内联「无」（唯一单行情形）。
            gre_group_sids = list(by_cat.get("group", []))
            gre_other_sids = list(by_cat.get("other", []))
            if gre_group_sids:
                b2.append("  问候·群聊:")
                for cand_sid in gre_group_sids:
                    # §29.1 静默群聊不丢弃 + §31.3 新窗：判定窗查不到消息者行尾加注
                    # 「（硬闸窗内无消息·不发晨晚问候）」（查不到消息 = 行面
                    # 「历史无消息」，判据 = 非 bot、非斜杠命令、非空白文本）。
                    note_text = ("（硬闸窗内无消息·不发晨晚问候）"
                                 if cand_sid in silent_group_sids else "")
                    b2.append(f"  {_num_label(cand_sid)}{note_text}")
            else:
                b2.append("  问候·群聊: 无")
            if gre_other_sids:
                b2.append("  问候·其他私聊:")
                for cand_sid in gre_other_sids:
                    b2.append(f"  {_num_label(cand_sid)}")
            else:
                b2.append("  问候·其他私聊: 无")
            # §31.3 三流分族（R7 = 用户 2026-10-23 第二轮直派）：目标流族块回归
            gre_target_sids = list(by_cat.get("target", []))
            if gre_target_sids:
                b2.append("  问候·目标私聊（无条件）:")
                for cand_sid in gre_target_sids:
                    b2.append(f"  {_num_label(cand_sid)}")
            else:
                b2.append("  问候·目标私聊（无条件）: 无")
            # 频率调控-only 流不编号（§26.1）：频控行仍是唯一的频控名单键（群聊 ∪ 私聊）
            if target_streams:
                b2.append("  频率调控（群聊∪私聊）:")
                for cand_sid in target_streams:
                    b2.append(f"  {stream_labels.get(cand_sid, cand_sid)}")
            else:
                b2.append("  频率调控（群聊∪私聊）: 无")

        # §25.2 → §31.3（R7 = 用户 2026-10-23 第二轮直派）：[口径] 行随新机制重写
        b2.append("[口径] 闸门窗 = [上份日程开始 → 本份日程开始)（周期四变量，非日历 0 点）；"
                  "群聊早安=硬闸（+必要性）逐 tick 实查、群聊晚安=连锁（marks v2）、"
                  "其他私聊=阈值 OR 连锁、目标私聊=无条件")

        # §26.1 展示重构 + §27.2 目标流豁免 + §31.3 三流分族（R7 = 用户 2026-10-23
        # 第二轮直派）：群聊/私聊（其他+目标）两族节点各按枚举序每 10 个流切一片
        # （无上限、不折叠）；频率调控-only 流不进本块。检查窗 = §31.1 判定窗
        # [上份日程开始 → 本份日程开始)（§31.3 起实查前移，窗口与行面状态在
        # [候选解析] 之前一次产出，经 `greeting_cycle_span` 读口，非日历 0 点）；
        # 标题时段文本每 tick 现算。
        stream_slices: List[List[str]] = []
        if svc is None or not stream_nums:
            stream_slices.append(["[候选流最后一条非命令、非 bot 消息]",
                                  "  无候选流"])
        else:
            group_sids = [sid for sid, cat in ordered_pairs if cat == "group"]
            # 私聊节点只装其他私聊（目标流豁免），顺序 = 枚举序，与编号一致
            private_sids = [sid for sid, cat in ordered_pairs if cat == "other"]

            def _slice_block(sids: List[str], title_fmt: str) -> List[List[str]]:
                """按枚举序每 10 个流切一片；标题带时段与「第 i/N 片」。"""
                blocks: List[List[str]] = []
                total = (len(sids) + 9) // 10
                for idx in range(total):
                    chunk = sids[idx * 10:(idx + 1) * 10]
                    lines = [title_fmt.format(i=idx + 1, n=total)]
                    if not callable(getter):
                        lines.append("  消息能力不可用（宁不发）")
                    else:
                        lines.extend(last_lines.get(
                            sid, f"  #{stream_nums.get(sid, 0):02d} "
                                 f"{stream_labels.get(sid, sid)}: 查询失败(宁不发)")
                            for sid in chunk)
                    blocks.append(lines)
                return blocks

            stream_slices.extend(_slice_block(
                group_sids,
                "[候选流最后一条非命令、非 bot 消息·群聊 时段："
                f"{group_window} (第 {{i}}/{{n}} 片)]"))
            stream_slices.extend(_slice_block(
                private_sids,
                "[候选流最后一条非命令、非 bot 消息·私聊 时段："
                f"{private_window}（目标/其他）(第 {{i}}/{{n}} 片)]"))

        return [_record("\n".join(block))
                for block in ([b1, b2] + stream_slices)]
    except Exception as exc:  # 兜底：诊断命令绝不抛
        return [_record(f"[streams] 诊断失败: {exc}")]

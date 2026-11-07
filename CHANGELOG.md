# 更新日志（CHANGELOG）

## 3.17.3（2026-11-08）

- PR-FIX-TZ 紧急修复跨日 sleep_engine naive/aware datetime 比较异常（真机生产 bug：监视 dwfrun-a22c4ef0 轮17–20，跨日 00:15 起每拍 `rolling_planner tick 异常（本拍跳过）：can't compare offset-naive and offset-aware datetimes`，栈指 `planning/rolling_planner.py:795 _try_dynamic_sleep`；bot 调度停摆、qzone 视频帖闸2「门控复检异常（保守不发）」持续 defer）。
- **根因**：跨日后存在已闭合睡眠会话（`earliest_wake_at` None/naive + `actual_end_minute` + `schedule_date`）时，`_suppress_until` 返回 naive（strptime 回退无 tzinfo / `datetime.min` 兜底 / naive earliest+1h），与 aware `current` 混比抛 TypeError → `evaluate_onset` → `_try_dynamic_sleep` → 整拍跳过。
- **睡眠引擎（`planning/sleep_engine.py`）**：新增内部助手 `_aware(dt, ref)`（naive 按引擎本地墙钟附参照 tz，aware/None 原样，与 `_restore_clock.stamp` 同口径）；`_suppress_until` 改实例方法、三分支返回一律 aware（含 `datetime.min` 兜底）；`should_start_sleep_episode`/`_recently_woke`/`actual_wake_today`/`evaluate_onset`（now 与 hard_start）/`_adapt_phase`/`is_asleep`/`next_wake_boundary`/`prepare_tick`/`_close_session`/`_start_session`/`_restore_recent_closed`/`_reconcile_recorded_wake` 比较点统一规范化。**抑制窗/wake 锚点/硬约定语义逐字不变**（naive 与 aware 同墙钟 → 同判定）。
- **qzone 闸2 独立崩溃点（`qzone/service.py:_cooldown_remain_minutes`）**：冷却锚点 `last_post_at` 由 `datetime.now().isoformat()` 写入为 naive，闸2 复检 now 为业务时区 aware（`plugin._surf_now`）→ `now - last` 抛 TypeError → bridge「门控复检异常（保守不发）」。比较前统一 tz（naive 锚点附 now.tzinfo；反向 naive now 按系统本地折算）；回拨保守/损坏 fail-open/冷却关语义不变。
- 红线：不重构睡眠引擎，只修时区一致性；不牺牲事实边界。配置：无新增字段（schema 不变，18 段 202 叶子）。版本双号 3.17.3（_manifest.json == SUPPORTED_CONFIG_VERSION）；门禁版本重钉（round：3.17.3；test_prompt_identity_audit 双号断言顺延）。新增专项 `scratch/test_sleep_tz_crossday.py` 16/16（修前最小复现 `_tz_crossday_repro.py` 10/11 红、栈与真机一致，修后 11/11 绿）；回归 test_sleep_consistency_c3 / test_dynamic_sleep / test_sleep_anchor_380 / test_midnight_meal / test_qzone_video_a0–a3 不回归；七门禁 7/7。边界：本批不部署、不 git commit、不碰主机、不切 mode；部署后需观察跨日 tick 恢复正常 + 视频帖过闸2。

## 3.17.2（2026-11-07）

- PR-PROACTIVE-REPLY 陪伴目标私聊主动激活轮的 reply 目标指令改写。背景：即使深处理改写了 planner 回复的 msg_id，宿主 replyer 仍从该 msg_id 渲染「你想要回复的消息是 … msg_id为 …」（自己消息则「你想要补充说明你自己…」），bot 被过时目标指令干扰。
- **replyer 侧（核心）**：`companion/components.py:mai_hook_replyer_model_inject` 在陪伴目标私聊（`_mai_reply_target_stream` 精确匹配）+ 主动激活轮（`active_task(items)` 命中或巡检触发 90 秒窗口内；窗口路径若上下文最后一条真实来信比触发更新则保守让位）时，把宿主目标指令整段（前缀行 + 后续「- quote=/- 发言内容/- 你之前的发言内容」明细行）替换为「本轮回复为插件主动激活，你需要根据上方planner传回的状况概括，结合你此刻的状态进行回复，不需要特地回复某个信息」。只触碰「当前时间：」起始的宿主最终用户消息文本，历史消息/旁白/参考文本不碰；未匹配到 → 保守追加尾随锚点 + warning 诊断；已含文案幂等不二次追加。纯函数在 `integrations/proactive_reply_target.py`（rewrite_host_target_text / rewrite_replyer_payload / append_proactive_notice / latest_incoming_timestamp）。
- **planner 侧对齐**：`_target_prompt` 主动轮不再指认具体 msg_id，改为主动激活语义（保留「msg_id 只能选对方发送的消息」有效性约束与不回复选择）；`INJECT_PROACTIVE_TURN_RULE_DEFAULT` 去掉「优先回应对方最后一条发言」，改主动激活引导（仍经 prompt() 回退表读取）；深处理 `ProactiveReplyTargets.after` 主动轮跳过强制 msg_id（rewrite_reply_calls 保留为 utility），目标指令改写统一由 replyer 钩子完成。
- 红线：仅陪伴目标私聊 + 主动激活轮生效；非主动轮（用户真实来信回复）逐字不变；预算/静默/好感/隐私门控不触碰；不新增命令/工具。
- 配置：无新增字段（schema 不变，18 段 202 叶子）；`proactive_reply_target_mode` 字段保留、描述更新为 3.17.2 语义（浅/深均提示-only）。版本双号 3.17.2（_manifest.json == SUPPORTED_CONFIG_VERSION）；门禁版本重钉（round：3.17.2；test_prompt_identity_audit 双号断言顺延）。新增专项 `scratch/test_proactive_reply_rewrite.py`；既有 `test_proactive_reply_target.py` 中断言旧「指认 msg_id/深处理强制改写」行为的用例按规格更新为主动激活语义预期。边界：本批不部署、不 git commit、不碰主机、不切 mode；部署后需真机主动触发验证 replyer 快照实际生效。

## 3.17.0（2026-11-07）

- PR-WATCH `/lbm_plan watch` 改为强制 debug 命令（与当前日程并行触发，绕过 attention_revoked 注意力门控）。背景：监视发现 19:53 的 `/lbm_plan watch` 周期 `gap-cmd-8f86ec3641d4` 以 `failed_submit/attention_revoked`（「当前没有有效的自动观看注意力，不启动任务」）关闭，整条链路零步未跑。
- **强制并行启动**：`planning/command_service.py:_handle_watch` 调 `coord.start_manual_cycle(..., force_debug=True)`（仅手动命令路径置位）。`integrations/video_schedule.py` 的 `start_manual_cycle/_manual_cycle/_manual_cycle_inner/_start_watching` 增 kw-only `force_debug`（缺省 False，随机 gap/日程 dedicated 路径恒 False、语义不变）：force_debug 时**跳过 SURF 租约申请**（`try_acquire_surf`）与选片期 `lease_valid` 复核——不占聊天注意力、与日程并行；entry 标 `forced_debug=True` 供诊断辨识；无任务收尾 `closed_reason=forced_debug_no_task`。防叠加守卫保留（`_manual_task` 未 done → `manual_cycle_running`；`_active_watch_entry()` 非 None → `watch_active`）。
- **submit/运行期注意力闸绕过（source 方案）**：`plugin.py:_call_watch_api` 改 `source = kwargs.pop("source", "schedule")` 透传（缺省仍 schedule，其余调用方逐字不变）；debug 路径 submit 传 `source="debug"`。`video_watch/service.py:handle_action` 补 debug owner 分支 `owner=f"debug:{episode_id}"`（与 schedule 引用身份隔离，收尾只释放自己引用不误杀）；submit 闸（`source=="schedule"` 才查 `_automatic_task_allowed`）与运行期守卫（`source!="schedule"` 放行）因 source 自然跳过。manager 台账 source 归一为 tool，消息钩子 `revoke_automatic` 只摘 `schedule:` 引用，debug 任务与聊天并行存续。
- **日程恢复不收回 debug 任务**：`_sync_inner` gap 轮询 `if active_id and not entry.get("forced_debug")` 才按 `schedule_resumed` 收尾；forced_debug 条目照常轮询至终态存档（普通 gap 语义不变）。
- **红线保留**：只绕过注意力门控，**不绕**额度（quota）/候选（无真实候选不编造 BV）/证据/隐私/防重门；debug watch 完成仍喂分享记录器 + 空间候选（`_record_video_result` 第三路），产出 A 线视频帖样本；命令总数不变（不新增命令，仍 8 命令）。
- 配置：无新增字段（18 段 202 叶子不变；debug 强制是命令固有语义，不新增配置键）。版本双号 3.17.0（_manifest.json == SUPPORTED_CONFIG_VERSION）；门禁版本重钉（round：3.17.0；test_prompt_identity_audit 双号断言随版本顺延）。新增专项 `scratch/test_watch_debug_force.py`。边界：本批不部署、不 git commit、不碰主机、不切 mode。

## 3.16.1（2026-11-07）

- PR-AUDIT 提示词结构与缓存复用审计修补（依据 `union/MaiBot_v3.16.0_提示词结构与缓存复用审计报告.md` PR-1/2/3）：新视频社交链路人设统一经 `with_identity=True` 进 system 前缀（宿主同款开头，共享前缀缓存），清理动态 JSON/用户提示词中的重复人设。
- PR-1：`plugin.py` ShareEvaluator（module=video）与 qzone_video（module=qzone）生成回调追加 `with_identity=True`；`video_share_evaluator` 与 `qzone_video_bridge` `_ask`/`_caption` body 移除重复 `persona` 键（persona_provider 保留兼容），POLICY 规则原文不动。
- PR-2：`qzone/service.py:_llm_post_gate` generate 追加 `with_identity=True`；`QZONE_POST_GATE_DEFAULT` 将「人设：{personality}」改为「依据系统人设判断」（「绝大多数情况下回答否」严判保留）；render 仍传 personality kwarg 兼容旧自定义模板 + 回退诊断 warning。
- PR-3：`planning/event_prompt.py` compose 去掉重复「人设：{p}」片段（人设已经 event_catalog with_identity 进 system），稳定规则/NO_PUZZLE/名称块/时段约束不变，签名保留兼容。
- `companion/initiative_profile.py` 保守保持现状（llm 为 Any 类型、测试传非 LLMService 桩，审计 P3 不假定任意对象是 LLMService）。
- 红线：event/module/task_name 与决策 JSON 契约逐字不变；动态材料后置、人设进 system 前缀；分享路由/隐私/防重/发帖严判与 shadow/auto 回退无漂移。版本双号 3.16.1，schema 不变（18 段 202）；新增 `scratch/test_prompt_identity_audit.py` 19/19，命名回归 224/224，七门禁 7/7。

## 3.16.0（2026-11-07）

- PR-D1 空间多日主题延续与全链诊断（计划书 H9/H10 / §7 PR-D1 / §8.2.6，视频×空间拟人化升级计划最后一个 PR）。
- **多日主题延续（H9）**：`qzone/service.py` 日程发帖文案 `generate_moment_text` 变量区末尾注入「近期自己的空间主题」摘要块——来源仅两处且跨渠道分域：B1 经历索引 `published/qzone_posted` recent ≤5（不新建表；防御复核只认 published 层）+ 既有 `list_my_moments` 有界读取 ≤5（`is_private` 仅自己可见合并不进公开 prompt）；合并去重、单行 ≤120 字、总行数 ≤8（THEME_BLOCK_MAX_LINES，保守裁量 needsReview）；含「私聊/群聊」标记的摘要第二道闸过滤。规则双保险：防复读/自然递进延续/不泄露私聊/「记得自己发过≠别人看过或点赞过」写进 `QZONE_POST_DEFAULT` 稳定模板，主题块头（QZONE_THEME_BLOCK_HEADER）重申规则后才列事实行（稳定前缀逐字不变、变量后置）。空索引/未装配/开关关闭/读取失败 → 不注入，prompt 与旧行为逐字一致；聊天分享事实（video_social）与观看事实不进空间主题。
- **全链诊断（H10）**：既有 `/lbm_diag` 挂 `qzone_video` 只读子输出（命令总数不变）——候选状态分布、最近决策及原因（为什么发/不发）、资产清单（id/尺寸/字节/TTL，无路径无 sha）、发布回执（含 uncertain）、事实同步状态、门控拒绝原因聚合（新增 `DecisionHistory.rejection_summary()`，复用既有判定历史；进程内 deque 热重载清零如实为空）。实现：`integrations/qzone_video_diag.py` 新增 `qzone_video_diag_payload`（零 LLM/零发送/零落盘，未装配/失败如实降级）；`QzoneVideoLedger` 新增只读端口 `asset_manifest`/`recent_attempts`（有界 1..100）。
- **WebUI 系统板块**：候选/资产/回执/事实同步计数芯片（沿用既有 summarychips 组件风格，不重构页面）——`planning/dashboard.py` 新增 `qzone_video` 只读投影（只暴露计数与状态分布，不含路径/正文；payload_limit 兜底同步降级），`webui/source/panel.js` + `index.template.html` 加 `qzone-video-chips` 并经 `tools/build_webui.py` 重建 `webui/index.html`；不可用整块隐藏不造假 0。组合根契约登记 `_qzone_video`（core/contracts.py + PluginContext 具名属性，守卫 #8 口径）。
- 配置：无新增字段（18 段 202 叶子不变）。版本双号 3.16.0（_manifest.json == SUPPORTED_CONFIG_VERSION）；门禁重钉（round：3.16.0；behavior：/lbm_diag 用法帮助 6→7 行 + qzone_video 路由/降级断言）。新增专项 `scratch/test_qzone_theme_d1.py`（18 例：稳定前缀/规则在 prompt/私密过滤/跨渠道独立/空降级/有界去重/开关关闭/published 复核/诊断只读零副作用/uncertain 展示/资产无路径/门控聚合/决策可解释/事实同步/降级/WebUI 计数与 ledger 一致/前端与命令接线）。七门禁 7/7 + 专项 18 例 + 回归 a0/a1/a2/a3/b1/b2/c1/c2/c3 全绿。边界：本批不部署、不 git commit。

## 3.15.0（2026-11-07）

- PR-C3 动态睡眠与硬意图一致性回归加固（计划书 H6 / §5.5 / §5.6 / §7 PR-C3）。**现状核查先行**：3.8.0 已落地 P1–P3（免睡不补睡眠意图、睡眠行 hardness 统一 hard、WebUI「作息参考」独立标注、commitment_hardness=hard 核验门槛、睡眠锚点不参与硬约定唤醒），§5.6 明确不重复开发——本批**零睡眠引擎行为改动**，以回归测试加固为主。
- 新增专项 `scratch/test_sleep_consistency_c3.py`（14 例矩阵，规格 §1 逐条覆盖）：① 配置参考时间变化不关闭已入睡会话（会话 id/边界不变）；② 新日程睡眠锚点不直接关闭动态会话、不成为唤醒约定；③ LLM 新生成 hard 意图（无 commitment_hardness=hard 用户确认）不触发提前醒；④ 用户确认硬约定（pending_commitment + hardness=hard）经真实 apply_schedule_items 匹配链可提前醒（正例）；⑤ 跨午夜生成 sleep_time=01:00 时 00:30 日程帧分钟=1470 不折回（关联 test_midnight_meal 口径）；⑥ 生效时区非 Asia/Shanghai 时锚点/时段按插件时区墙钟；⑦ 关机恢复 reload 后状态连续（不重开/不丢/不重复结算）；⑧ 实际午休静默与意图行提前静默同口径（夜间睡眠两边排除）；⑨ 时钟回拨不产生负时长/不重复结算；⑩ 免睡模式正常生成/缺项补齐/LLM 失败模板三路径均不补睡眠意图；⑪ 睡眠锚点不参与硬约定唤醒（is_night_sleep_item 排除）；⑫ WebUI 睡眠条目 goal_type=sleep 标「作息参考」不随 hardness 误标（渲染逻辑断言，panel.js 与 index.html 双文件）；⑬ 07:58 剩余 2 分钟不启动需跨段的长活动（内置池/事件库两路径 + 08:00 正例对照）；⑭ 异常回退路径不绕过时段过滤（取池异常/非法事件/未装配降级三路）。
- 矩阵全绿未发现需修复的行为性不一致（规格 §2 预期 0–2 处小修，实际 0 处）。裁量登记（needsReview）：restore() 后首拍前 sleep_status().continuous_minutes 为落盘缓存值（信息性字段，觉醒判定/结算均以恢复后的时钟为准，首拍即刷新）——测试按生产编排 restore→tick 断言，与 test_dynamic_sleep 既有口径一致，未改引擎。
- 配置：无新增字段（18 段 202 叶子不变）。版本双号 3.15.0（_manifest.json == SUPPORTED_CONFIG_VERSION）；门禁版本重钉（round：3.15.0）。边界：本批不部署、不 git commit。

## 3.14.0（2026-11-07）

- PR-C2 真实观看驱动的兴趣演变（计划书 §5 H7 / §4 / §7 PR-C2）。**证据权重层**：`integrations/video_interest.py` 兴趣文档新增 `evidence_weights` 层（话题 → `{persona_base, watch_score, updated_at, positive_count}` + 已应用观看幂等键表）；最终权重 = persona_base + bounded(watch_score)，watch_score 饱和封顶 ±50%×base（内部常量）。persona_base = LLM 问询关键词的隐式基线 1.0，**观看反馈绝不改写 persona_base / LLM 关键词集合**——单次偶然观看不重写人格；周期人设刷新保留证据层，显式偏好初始化（reset）才整体重置（保守裁量，needsReview）。
- **真实观看反馈源**：B1 索引 `observed/watched_video` 行（经注入 `watch_evidence_provider` 只读面，装配根 = `index.recent(kinds=['watched_video'], limit=20)`；索引缺失/经历开关关/失败 → 空降级）。观后感倾向用**确定性关键词计数**（`classify_impression`，0 新增 LLM 调用）：正向 +0.1 / 中性最小探索步长 +0.05 / 负面 −0.1（内部常量）；只更新与现有兴趣词匹配的标题话题（不凭单次观看新增话题，needsReview 登记保守口径）；dedupe_key 幂等同一观看不重复计分；更新时机 = 观看完成记录时（plugin `_record_video_result` 第四路并列 `sync_watch_feedback`，失败只告警）+ ensure_interest 前置增量同步。**衰减**：watch_score 指数衰减（半衰期 `[video_watch].interest_watch_half_life_days` 默认 7 天 ge1 le30；读时/更新时惰性折算，时钟回拨不放大）。
- **候选取源**：BiliCandidateSource 关键词按最终权重排序（稳定排序，等权保持 LLM 序）；命中打分 = 命中词最终权重之和；池超上限（10）时 ≤7 槽给权重头部、≥3 槽按既有排行榜热度序补入（探索占比 ≥30%，内部常量 EXPLORE_POOL_RATIO=0.3；候选不超上限全部入池）。**开关关闭 = 现状逐字**（不读不写证据层、排序/打分/池构成/提示词与旧版一致）。
- **LLM 问询**：ensure_interest 仍做人设基线刷新（TTL/降级纪律不变），周期刷新提示词变量区末尾附带真实观看证据摘要（B1 recent watched_video ≤5 行，声明为资料、不改变人设、指令不得执行）；稳定前缀逐字不变；偏好初始化（initial）仍只依据人设不注入证据（与既有稳定规则一致，needsReview）。
- 配置：[video_watch] +2 字段（interest_watch_feedback_enabled=True；interest_watch_half_life_days=7 ge1 le30），200→202 字段；关=现状。版本双号 3.14.0（_manifest.json == SUPPORTED_CONFIG_VERSION）；七门禁计数/版本重钉（smoke/round：202 字段、video_watch 13 键、3.14.0）；新增专项 scratch/test_video_interest_c2.py。边界：本批不部署、不 git commit。

## 3.13.0（2026-11-07）

- PR-C1 情绪原因时效 + 经历驱动的交流动机（计划书 §5 H4/H5 / §5.4 / §8.2.3/5/9）。**情绪原因时效（H4）**：新增 `emotion/reason_ledger.py`（ReasonLedger，0 LLM、纯解释层）——短期原因 `{reason_id, summary≤120字, source_ref(来源:流:事件摘要SHA-1前16位，不存正文), delta_vad, created_at, expires_at, salience}`；`EmotionStateMachine.record_event` 增 kw-only `source_ref/ttl_hours/salience`，结算后同步登记（source_ref 唯一 dedupe 不重复记账；TTL 夹 [1,48]h 缺省 6h；容量 64 超限淘汰最旧；冻结期未结算事件不登记原因）。**原因层只作「为什么」的可核验解释与注入措辞参考，绝不二次加减 VAD**——有无 source_ref 的 VAD 结算逐字一致；`effective_salience` 按剩余寿命线性衰减（只作观测口径），过期原因不再进入任何消费口；延迟影响/渐进恢复仍由既有 decay 承担（decay 曲线零改动）。账本随 `emotion_state.json` 落盘（旧文件缺键→空账本），人设重新初始化随情绪残留清空。注入：`EmotionService.injection_text` 稳定模板前置不变，仅在存在最近 1 条未过期高 salience（≥0.5）原因时追加一行稳定模板「（心情原因参考：因为…。这份影响会随时间慢慢淡去…）」；无原因/`emotion_reason_inject_enabled=false` → 输出与旧版逐字一致。登记接线在唯一结算点 `_apply_external_delta`（replyer 评估/episode 冲刷/工具上报/模板事件共用）。
- **经历驱动的交流动机（H5）**：`planning/motives.py` 新增来源 kind=`experience_share`——B1 索引 published/observed 且 salience≥0.5、created_at 在 24h 近期窗内（保守固定值，规格未给定，needsReview）→「想找人聊聊：…」动机，强度封顶 0.6（低于通用饱和 0.8）；同一经历只占一条动机（提升为 share 后不重复派生 experience）；created_at 缺失/不可解析 → 不视为「刚完成」，回落 B2 experience 来源。开关 `[planning_proactive].experience_share_motives_enabled=false` = 现状（active/bonus/refresh 全链路即时过滤，经历回落 B2 来源）。**消费**：`companion/patrol.py` 想念候选阶段（evaluate_miss 判 eligible 且 coordinator.permission 通过之后）读 `plugin._motive_store.active()` 最强 experience_share 动机——topic 作可选话题参考行追加进想念 reason（「可以自然地带一句，也可以选择不提」）+ 概率轻微乘法加成 ×(1+0.15×strength) 封顶 0.35（evaluate_miss 既有上限）；**最终门控不变**（ProactiveBudget/静默/注意力/话题相关性，dispatch 内复检照旧），默认可不联系；**initiative_profile 孤独锚点机制零改动**（动机不能让未过锚点的候选变 eligible）；dice 仍可放弃、预算/静默拒绝照旧留痕（决策行增 experience_share_ref/strength/miss_probability_boosted 观测字段）。动机存在≠发消息：无 store/无分享动机/异常 → 候选阶段逐字旧行为。
- 配置：[emotion] +2 字段（emotion_reason_ttl_hours=6 ge1 le48；emotion_reason_inject_enabled=True）、[planning_proactive] +1 字段（experience_share_motives_enabled=True），197→200 字段；关=现状。版本双号 3.13.0（_manifest.json == SUPPORTED_CONFIG_VERSION）；七门禁计数/版本重钉（smoke/round：200 字段、emotion 16 键、planning_proactive 17 键、3.13.0）；新增专项 scratch/test_emotion_reason_c1.py。边界：本批不部署、不 git commit。

## 3.12.0（2026-11-07）

- PR-B2 跨日事务 + 持续动机（计划书 §5 H1/H3 / §5.1 / §5.3）。**跨日事务（H1）**：`planning/rolling_planner.py` 新增确定性活动三分类 `classify_activity_continuity`（无延续价值=发呆/吃零食/随手刷QQ 等类型与关键词直判；可能延续=work/study 类型或整理/收拾/收藏/研究等关键词，可经注入 `continuity_keywords_provider` 补充；拿不准→无延续价值保守缺省；LLM 不参与分类；明确承诺走既有硬约定机制不动）。「可能延续」活动启动时经注入的 GoalManager（**唯一目标表，不造第二个目标表**）关联/创建长期目标，episode metadata 增 `parent_goal_id`，来源标 `simulated_activity`（不伪装现实经历）；活动中断/被挤/恢复池放弃 → 目标保留 paused（**中断/放弃 ≠ 目标消失**，进度按 episode 真实 progress 折算上限 95），活动完成 → 目标完成（不长留待办）；恢复行透传父目标并重新 active。跨日继续候选：spine 生成后的候选阶段对 paused 且有延续价值的目标按精力/完成度/近期重复度温和加权提供「继续昨天没做完的事」候选，**每日此类候选 ≤1、energy 低于阈值不出现、睡眠环内不出现**，今日已继续过的目标不重复入围；只是普通候选（softmax 采样保留放弃概率，不形成压力队列）。
- **持续动机（H3）**：新增 `planning/motives.py`（MotiveStore，0 LLM、纯旁路、不 import 业务模块）——2–4 条短中期动机 `{motive_id, topic, source, strength, created_at, expires_at}`，来源限明确目标（goals 表 active/paused，pending_commitment 除外）/真实观看与发布结果（B1 索引 observed/published；inferred 强度封顶 0.3 只低权重）/状态驱动（精力低→想休息、压力大→想放松、社交电量充足→想找人聊聊）；simulated_activity 经历不直接成为动机（经目标间接进入）。strength 指数衰减（半衰期默认 3 天），到期移除（创建后 3 个半衰期，保守裁量 needsReview），同题材重复加强饱和上限 0.8；`motives.json` 原子写、损坏重置空表不阻塞。**消费点只有两处**：rolling_planner 候选打分 `motive_bonus`（≤15% 基础分，不匹配候选加成为 0——探索概率保留，新候选不受动机垄断）与 spine 提示词变量区 ≤4 条动机摘要（稳定规则前置不变，措辞注明「不是必须执行的任务，状态不佳可以不做」）。**不改睡眠/硬约定/注意力/社交预算判定**；动机不强制外化（无合适候选/状态低→不作为，不生成消息）。
- 接线：plugin.py 装配 MotiveStore（三键动态读 [life_state]，热重载即时生效；装配失败保守停用 None→动机分支整体旁路）并注入 planner（goal_manager/motive_store）；只读 provider `_motive_goal_rows`（goals 表 ≤50 行）/`_motive_experience_rows`（B1 索引 kinds=watched_video/qzone_posted ≤10 行，索引缺失空降级）；公开端口 `motive_context_lines`（≤4 条）→ `ScheduleGenerator._load_motive_context` → PromptBuilder 变量区【近期持续动机参考】。planner 未注入 goal_manager/motive_store 时全部新分支旁路，旧行为逐字不变。
- 配置：[life_state] +3 字段（motives_enabled=True；motive_max_count=4 ge2 le4；motive_half_life_days=3 ge1 le14），194→197 字段；关=零影响（已有动机文件保留只读不删）。版本双号 3.12.0（_manifest.json == SUPPORTED_CONFIG_VERSION）；七门禁计数/版本重钉（smoke/round：197 字段、life_state 16 键、3.12.0）；新增专项 scratch/test_motives_b2.py。边界：本批不部署、不 git commit。

## 3.11.0（2026-11-07）

- PR-B1 生活经历索引（B 线起始，计划书 §4/§5.2/§6）：新增 `core/life_experience_index.py`——轻量跨域事实层，**只存事实与引用，不承担执行/副作用/人格大模型循环**，主模块依旧拥有业务数据和执行权。SQLite `data/life_experience.sqlite3`（WAL，连接/事务风格同 ShareLedger）；表 `experiences`（id=exp_+uuid hex、type 四层 observed/simulated_activity/inferred/published **必填缺省拒绝**、kind 开放字符串、source_ref 可追溯、dedupe_key UNIQUE 同一事件不重复记账、summary ≤300 字**不复制原始聊天正文/图片**、outcome/salience/confidence/related_goal_id/created_at/expires_at）；`record/recent/by_goal/expire/count` 全部有界，容量 2000 行超出淘汰最旧低 salience，TTL 到期清理。
- 事件订阅（统一入口不侵入各模块，handler 异常由总线隔离，索引失败绝不影响主业务）：`qzone.posted`→published（dedupe `qzone:{tid}`）；`schedule.activity_ended`→simulated_activity（无延续价值也记、salience 低；现 payload 无 episode id，幂等键用 ts+name，needsReview）；`schedule.sleep_started/woke_up`→observed 睡眠会话事实；`relation.message_received`→observed **只存「收到某用户消息」的事实引用与时间，不存正文**（现 payload 无 user_id，needsReview）。视频观看完成选**侵入更小**的记录器并列路：plugin.py `_record_video_result` 第三路调 `index.record_watched_video`（证据等级沿用 watched_facts 闸，无证据不记；不新增总线事件，needsReview 登记二选一裁量）。
- 接 A3 遗留 qzone fact_sync（规格 §2.1）：桥新增注入端口 `record_published`（同步 (candidate,tid)->bool，经 asyncio.to_thread），`_write_fact` 优先走 `index.record_watched_video_published`（published/qzone_posted，dedupe `qzone:{tid}`，幂等成功含同 tid 已在账）——成功→fact_sync=ok/synced，失败留 pending 由 A3 有界重试（≤3 次）；**不再依赖未装配的 `_qzone_video_self_stream`**（append_fact 旧路径保留兼容裸桥/桩），绝不复用陪伴私聊流注入用户聊天。观看事实（observed）与空间发布事实（published）都进索引，供 D1 主题延续/防复读消费。
- 只读消费端口（本批最小接线，证明链路）：spine 生成经 `plugin.experience_context_lines`（有界 ≤5 条，kinds=watched_video/activity_done）→ `ScheduleGenerator._load_experience_context` → 提示词变量区【近期真实经历参考】（稳定前置规则不变；失败/为空/开关关闭→不注入空降级）。本批不改兴趣权重/动机逻辑（B2/C2）。诊断：`/lbm_diag video_share` 输出追加 `experience_index` count/recent 只读块（挂既有命令，命令总数不变）。
- 配置：[life_state] +2 字段（experience_index_enabled=True；experience_ttl_days=14 ge1 le90），192→194 字段；关=不记录不消费（已有数据保留只读）。版本双号 3.11.0（_manifest.json == SUPPORTED_CONFIG_VERSION）；七门禁计数/版本重钉（smoke/round：194 字段、life_state 13 键、3.11.0）；新增专项 scratch/test_life_experience_b1.py。边界：索引纯旁路零回归；本批不部署、不 git commit。

## 3.10.3（2026-11-07）

- PR-A3 回执幂等与失败恢复：`integrations/qzone_video_bridge.py` 新增 `publish_attempts` 表（attempt_id PK、candidate_id/bvid/watched_generation(=task_id)/channel='qzone'/request_key/status(claimed/sending/sent/uncertain/rejected)/receipt_tid/reason/created_at/finished_at，**UNIQUE(bvid,watched_generation,channel)**；加法式建表，meta version 保持 '1'）。这是「视频→空间」专用幂等关联层，与 `QzoneService._publish_ledger`（post_moment 内部防重）**两层都在、不互相替代**。发布流程改为：门控通过后先事务 `claim_publish`（同键已 sent/uncertain/sending，及保守视同在途的 claimed → 拒绝重发；rejected 确定未发/人工清除 → 原地重置新 attempt）→ 资产核验/claim → 文案 → 分发前写 `request_key`（=`preview_post(text,images,1).key`，与 post_moment 同口径）并置 sending → post_moment → `finish_attempt`（sent/uncertain/rejected，终态不被覆盖）。每个确定未发早退路径都收尾 attempt 为 rejected，崩溃窗口不留悬空 claimed。
- 启动恢复 `bridge.on_start()`（plugin.py 装配后调用，异常保守忽略）：① 终态尝试对账（sent→候选 posted+资产 released；rejected+候选 evaluated→解除 claim）；② 非终态尝试 claimed/sending → uncertain + 候选 uncertain（**不盲重发**，与视频分享「未知结果跨重启不补发」同口径；claimed保守视同未知，留 needsReview）；③ claimed 素材三态——候选 posted→released（按 TTL 正常清理）、uncertain→保持 claimed（人工核对前保证据）、rejected/expired→删文件（无候选归属保守保持留痕）；④ pending/deferred/evaluated 过期复检 + reserved 无引用清理（A1 构造已做，幂等复核）。全部动作日志 + ledger 留痕。
- uncertain 运维（挂既有 `/lbm_life` 调度器，**命令总数不变**，仅分发表加一行）：`/lbm_life qzone_video status` 纯只读（运行模式/候选与尝试状态/uncertain 明细 attempt_id·bvid·标题·时间·request_key/事实同步汇总，零 LLM 零发送零副作用）；`/lbm_life qzone_video clear <attempt_id> confirm` 管理员人工核验确未送达后标记 rejected（reason=manual_verified_unsent，**保留审计行不删证据**），该代 claimed 资产 released，候选可重新评估一次（deferred + manual_cleared_once 防无限再评；已清除过→rejected、已过期→expired），**不自动恢复发送**（再发仍走完整门控）。鉴权沿用既有管理员口径（`_qz_is_admin`，分发表 admin 闸）；实现集中在新增 `integrations/qzone_video_diag.py`，参照 3.7.1 `/lbm_life share`（video_share_diag.py）模式；companion/components.py 加 `_lbm_life_qzone_video` 单一函数 + qzone/components.py 分发表/帮助各一行。
- 事实同步收尾：posted 候选事实回写失败留 `fact_sync=pending`，tick 有界重试 ≤3 次（`FACT_SYNC_MAX_ATTEMPTS`，单拍 ≤3 候选），仍失败置 `fact_sync=failed` 留痕不再试；`fact_sync_attempts` 计数进 decision_json，`snapshot()` 增 `publish_attempts`/`fact_sync` 汇总进诊断。**provider 缺省 no-op 留 pending**（不烧重试次数）；bot 自身上下文流真实接点（`_qzone_video_self_stream`）本批不强行复用陪伴私聊流（防误注入用户聊天），留 B1 经历索引接线（needsReview）；record_posted 已存 posted_tid，事实可追溯不丢。
- 共享额度不抢（计划书 §8.1.7）：`QzoneService.post_moment` 增 kw-only `enforce_daily_limit: bool=False`（默认 False 逐字保持手动/命令直发基线），True 时在既有 `_publish_lock` 内、回执账本 begin 之前复检共享每日额度——两套自动触发器（日程普通说说 `generate_and_post_by_schedule` + 视频候选）共用同一把锁串行分发，锁内复检消除「先查额度后分发」TOCTOU 竞争，并发不超额；额度用尽确定未发（None，不置 `_send_ambiguous`）。视频路径经 plugin.py 包装注入 `enforce_daily_limit=True`（不改桥端口签名，A2 裸桥/桩注入契约不变）。
- 版本双号 3.10.3（_manifest.json == SUPPORTED_CONFIG_VERSION）；schema 不变（18 段 192 字段，无新增配置键，重试上限/尝试容量为模块常量）；七门禁版本重钉（round：manifest/SUPPORTED_CONFIG_VERSION 3.10.3）；新增专项 scratch/test_qzone_video_a3.py。边界：生产部署后默认仍 shadow（不发布），切 auto 由主代理真机验证后单独操作；本批不部署、不 git commit。

## 3.10.2（2026-11-07）

- PR-A2 内容驱动的空间视频帖子接通：`integrations/qzone_video_bridge.py` 新增发布相位——`mode=auto` 且发布端口已接线时，`tick()` 消费 `status='evaluated'` 且 `decision.action='post'`、`publish_after` 到点的候选（单飞有界 ≤1/拍），经完整门控发布一条**来源可信**的真实视频图文说说；shadow 仍只留痕、绝不发布；端口缺省（A0/A1 裸桥）保持影子行为。candidates 表加法式新增 `publish_after REAL`（meta version 保持 '1'，旧库 ALTER 补列），`decide()` 写 post 时按 `decision.delay_minutes` 设 `publish_after`。
- 发布前置（全部满足才发）：① 共享每日额度 `posts_today()<daily_max_posts`（与普通说说同一额度，不另设视频额度）；② `QzoneService.check_post_gates(now)` 复检（睡眠/静默/午休/冷却/LLM 严判，异常保守不发）；③ 资产核验+claim：decision 选定 `asset_ids`（评估提示词变量区追加该候选 available 资产清单 id/时间点/尺寸，**不含路径**；模型选 ≤max_assets_per_video），实测文件存在+sha256 相符+status=available → claimed（防 tick 清理），**任一缺失 → 拒绝本次（do_not_post，reason=高清资产不可用），绝不纯文本/随机配图**；④ 独立文案调用 `event='qzone_video_caption'`（仅已核验事实 标题/UP/摘要/观后感/证据等级 + focus/tone + 人设 + 有界 list_my_moments 近期主题防复读；稳定规则前置、禁止编造情节/对白/数值与机械报告腔；输出纯文本 ≤caption_max_chars；attach_source 默认开附标题+UP 署名、不附链接）；⑤ 可见性沿用默认公开 ugc_right=1，不静默改。
- 图片硬性要求：`QzoneService.post_moment`/`_post_moment_once` 增 kw-only `require_images: bool=False`（默认 False 路径逐字保持，普通说说基线不回归）。True 且空图 → 回执账本 begin 前早退确定未发（不留 image_count=0 的 retry_wait 行，防 `_process_delivery_queue` 纯文本重放）；True 时**保守直走受控 CGI**（实测 SnowLuma 适配器 `send_qzone_msg` 为纯透传、不识别 require_images、无法保证图全挂拒发 → 规格 §2/§6「适配器不支持则走 CGI 或不发」），`QzoneWebClient.publish` 增 `require_images` 参数：图全挂（含无图）分发前抛 `QzoneImagesRequiredError`（确定未发，绝不退纯文本），已分发但回执丢失才置 uncertain。结果三态：{tid}→posted（记 receipt_id=tid、decision.posted_tid、资产 claimed→released、bot 自身事实回写）；None+确定未发→released（资产解除 claim、候选可延后一次，再失败硬拒防无限重试）；uncertain→候选 uncertain、**不自动重发**留人工核对（A3）。
- 事实回写：发布成功后经注入 `append_fact` 端口向 **bot 自身上下文**追加「已向QQ空间发布视频说说：bvid《标题》，tid=…，这是已发生行为记录，勿重复发布」（参照 `_append_social_fact` 成对 Hook 口径 source_kind+message_id、replyable=False，**不进任何用户聊天注入**、不经 send.text）；失败/端口缺失留 `fact_sync=pending`（A3 完善重试）。聊天卡片分享与空间发帖独立账本互不影响（同 bvid 聊天已分享不阻止空间帖）。
- 配置：[qzone_video] +2 字段（caption_max_chars=500 ge50 le2000；attach_source=True），mode 描述更新（auto 经完整门控发布，默认仍 shadow）。schema 18 段 190 → 192 字段，迁移保持用户值（新键取默认）。core/llm_usage.py EVENT_MODULE_MAP += qzone_video_caption→qzone（18→19 键）。
- 版本双号 3.10.2（_manifest.json == SUPPORTED_CONFIG_VERSION）；七门禁计数/版本重钉（smoke/round：192 字段、qzone_video:10、EVENT_MODULE_MAP 19 键；behavior：19 键）；新增专项 scratch/test_qzone_video_a2.py。边界：生产部署后默认仍 shadow（不发布），切 auto 由主代理真机验证后单独操作；本批不部署、不 git commit。

## 3.10.1（2026-11-07）

- PR-A1 高清非拼图截图资产：`video_watch/worker.py` 新增可选分支 `_fetch_playurl_hq`（公开 playurl qn=64 + platform=html5，**不携带任何登录态**；已实测未登录可得 720P，qn=32 反回落 360P 故不试）与 `_extract_qzone_assets`（VLM 拼图帧取得后、finally 清理前调用；按首/中/末分散选点最多 1–4 个，`-ss <t> -i <直链> -frames:v 1 -q:v 2` 抽**原生分辨率**单帧，**无 scale 滤镜**；读 JPEG SOF 实测宽度，低于 min_width 如实弃用标 `hd_unavailable`，绝不低清放大；Pillow 灰度方差查黑屏/纯色、sha256 去重；单帧 ≤8MiB、复用任务总截止）。资产写入受控绝对目录 `data/video_watch/qzone_assets/vframe_<uuid hex>.jpg`（resolve 限定根目录内、拒绝 symlink），result 顶层只挂 manifest 元数据（asset_id/bvid/cid/task_id/时间点/实测宽高/mime/sha256/bytes/status/created_at/expires_at，**不含播放直链**）与 `qzone_assets_diag`（≤350 字）；提取失败 try/except 隔离，不拖挂 VLM 主结果。VLM 低清拼图链路（scale=480:-2、q:v=7）逐字保持。
- 资产生命周期：`QzoneVideoLedger` 新表 `assets`（reserved/available/claimed/released/expired）。`record_from_result`：候选被证据闸接纳 → reserved→available；候选被拒/去重 → 立即删 reserved 文件与行；证据闸拒绝/模式 off/空间关 → manifest 文件即删（不囤积）。`tick()`：过期删文件+行、总量超 asset_max_total_mb 从最旧 available 释放、off/空间关释放全部非 claimed；**claimed（A2/A3 发布中）永不删**。启动恢复：无候选引用的 reserved 一律删、有引用的提升 available、目录内无索引行的无主文件清除。资产不进聊天注入历史、不进 result_text 对外文案。
- 接线：plugin.py `_qzone_video_assets_spec`（仅 `[qzone_video].mode=auto` 且 `qzone.enabled` 时给 spec 带 `qzone_assets={min_width,max_assets,asset_dir(绝对),ttl_hours}`；shadow/off 零开销不提取——计划书 §3.4 影子只记候选时间点）；VideoWatchService/WatchTaskManager 增 `qzone_assets_provider` 注入并在 submit 快照、_run_task 装配进 worker spec（worker 不读插件配置）；manager._finish 的 visual.pop("frames") 边界保持，manifest 元数据允许穿过；ledger 构造传 asset_dir。
- 配置：[qzone_video] +4 字段（min_width=1280 ge320 le3840；max_assets_per_video=2 ge1 le4；asset_ttl_hours=24 ge1 le168；asset_max_total_mb=200 ge16 le2048），mode 描述更新（auto 提取资产但仍不发布，PR-A2 未接通）。schema 18 段 186 → 190 字段，迁移保持用户值（新键取默认）。
- 版本双号 3.10.1（_manifest.json == SUPPORTED_CONFIG_VERSION）；七门禁计数/版本重钉（smoke/round：190 字段）；新增专项 scratch/test_qzone_video_a1.py。边界：只做资产提取/核验/生命周期，不发帖（A2）、不建发布回执（A3）。部署由主代理执行，本批不部署、不 git commit。

## 3.10.0（2026-11-07）

- PR-A0 视频空间候选与权限边界（影子模式）：新增 `integrations/qzone_video_bridge.py`（QzoneVideoLedger + QzoneVideoBridge）。自主观看（自动日程 + /lbm_plan watch 手动周期）完成且证据闸通过（视觉 completed+summary 或 ≥40 字公开字幕，复用 video_share_policy.watched_facts）时记录空间视频候选；`data/video_watch/qzone_video.sqlite3` 落盘，UNIQUE(bvid,task_id) 幂等去重（同一次观看不重复建候选，同 bvid 不同次观看允许新候选）。用户点播工具链不经此记录器，权限边界保持现状。
- 影子判定：稳定规则/JSON Schema 前置 + 人设/状态/视频事实/近期空间主题后置（3.9.1 前缀缓存口径），经 LLM 唯一入口 generate（module='qzone'，event='qzone_video_decision'，temperature=0.3，strict_params）；严解析 post/defer/do_not_post，非法/异常兜底 defer（判定暂不可用，30 分钟）；单候选判定含首评超限强制 do_not_post（重评超限）；每日判定额度按业务时区日切（0=当天不判，候选留待次日）；TTL 过期放弃；deferred 到点重评；单次 tick 有界 ≤3。**shadow/auto 本批都只写 ledger + 日志留痕，绝不发帖**（auto 日志注明发布链路未接通 PR-A2）；不提取截图资产（PR-A1）、不建发布回执账本（PR-A3），不改 video_watch/worker.py 与 qzone/service.py 既有行为。
- 接线：plugin.py `_build_qzone_video`（仿 _build_video_social，异常 warning+None 保守停用）；`_share_recorder` 改双路并列（聊天分享 + 空间候选）各自 try/except 隔离，空间候选失败不影响聊天分享；bridge.tick 挂现有生活循环（与 video_social tick 同节拍，单飞），停机链取消 `_qzone_video_task`。core/llm_usage.py EVENT_MODULE_MAP += qzone_video_decision→qzone（17→18 键）。
- 配置：新增 [qzone_video] 段 4 字段（mode=shadow，off/shadow/auto 非法回退 shadow；candidate_ttl_hours=24 ge1 le168；max_evaluations=3 ge1 le10；decision_daily_limit=6 ge0 le50），qzone/config.py 拥有（发布面），suite_config 注册；WebUI 中文 label。schema 17 段 182 字段 → 18 段 186 字段，迁移保持用户值（新段取默认）。
- 版本双号 3.10.0（_manifest.json == SUPPORTED_CONFIG_VERSION）；七门禁计数/版本重钉（smoke/round/behavior：186 字段、18 段、EVENT_MODULE_MAP 18 键），新增专项 scratch/test_qzone_video_a0.py。部署由主代理执行，本批不部署、不 git commit。

## 3.9.3（2026-11-07）

- 热修：日程注入统一模板 `_render_unified_prompt` 在动态睡眠开启时引用未定义局部变量 `now`（`engine.dynamic_status(now)` → NameError），导致该路径下日程注入整体失败（真机日志「注入日程信息失败: name 'now' is not defined」，inject_service.py:859）。改为 `engine.dynamic_status()` 缺省取引擎自身时区当前时间，与 sleep_engine 内部调用同口径。缺陷由 4a67ac7（3.5.0–3.6.1 工作树汇总）引入，仅动态睡眠开启时触发。
- round 门禁新增回归检查 `check_inject_dynamic_band`：动态睡眠开启时统一模板必须渲染成功并追加困意提示（有点困/困了，准备休息/刚醒，还在缓神），关闭时不追加。
- 版本双号 3.9.3，schema 不变（17 段 182 字段），纯修复递增，迁移保持用户值。

## 3.9.2（2026-11-07）

- 小事件三时段配额改为「可复用 / 一次性」分开配置（event_catalog_reusable_00_08/08_16/16_24 默认 1/5/4，event_catalog_one_shot_00_08/08_16/16_24 默认 3/6/5，单项 0–12），合计生成 24 条；旧 event_catalog_period_limit_* 三键退役（纯 pop，不搬运）。
- 更新周期默认改为 7–10 天（min_interval_days=7 / max_interval_days=10）；用户自定义范围仍按 1–365 夹取，不锁死默认值。
- 总池扩容：内置 5 + 生成 24 = 29，max_pool_events 上限 32（internal_tuning.json 与出厂默认同钉）。
- 新增 NO Puzzle 开关（event_catalog_no_puzzle，默认开，WebUI 标签即 "NO Puzzle"）：开启时生成提示词追加反套路引导，避免拼拼图、十字绣等被滥用的套路活动。
- 周期结束不要求用完所有事件（纯权重抽取，现状保持）。版本双号 3.9.2，配置 17 段 182 字段（[schedule] +4）。

## 3.9.1（2026-11-07）

- 超限自动裁剪兜底（新增 [llm].over_limit_auto_trim，默认开）：小事件生成超量时取前 N 条而非整批作废（修复模型多返导致刷新失败）；日程意图超量按时间排序后保留末尾 N 条，晚间计划不被裁掉（关闭则沿用旧截断）。
- 修复 _normalize_builtin 丢弃 allowed_slots 致自定义内置事件被时段过滤全部排除的问题（缺省默认全时段）。
- 小事件生成提示词条数硬约束（恰好 N 条 + 规则条款），稳定规则/JSON 示例前置、变量任务后置以命中前缀缓存。
- 全插件提示词前缀缓存重构：日程 spine（人设/任务规则/Schema 前置，日期/上下文后置）、角色裁判/睡眠裁判/情绪评估/好感会话与日结裁判/QQ空间说说与回评门禁/视频兴趣等模板稳定部分前置、变量后置；占位符与语义不变。
- 版本双号 3.9.1，配置 17 段 178 字段（[llm] +1），迁移保持用户值。本地七门禁 7/7、三时段专项 14/14、视频兴趣离线 41/41 通过。

## 3.9.0（2026-11-07）

- 小事件三时段独立生成闭环：00-08/08-16/16-24 三个互不重叠时段（默认上限 1/3/3），同一生成周期每时段独立一次 LLM（共 3 次），批次内混合可复用/一次性池（服务端按配额分配），跨批次去重，全部成功才原子替换整库（任一时段失败保留旧库）。
- 新增配置 event_catalog_period_limit_00_08/08_16/16_24（默认 1/3/3，0=该时段不调用 LLM）；三时段上限之和超过可用容量（总池 12-内置 5）时显式报容量不足。
- 强制时段标签：生成项入库带唯一 time_slot；内置项带 allowed_slots（五项默认覆盖三段）；运行时 pick_event 按生效时区当前时段过滤 + 最小时长容纳剩余时段 + 时长封顶不越界；rolling_planner 启动前二次复核，一次性 claim 在校验通过后执行；降级内置池/后备路径同样受时段约束。
- 旧 v1/v2 无时段标签事件停止随机抽取（不默认全天可用），保留旧文件与消耗回执；新增 /lbm_plan events [force] 子命令立即按三时段重生成替换旧库。
- POOL_VERSION/CATALOG_VERSION 升至 3（兼容 v1/v2 加载）；episode metadata 快照 event_time_slot。版本双号 3.9.0，配置 17 段 177 字段（schedule +3），迁移保持用户值。
- 本地七门禁 7/7、三时段专项 14/14 通过；已部署小主机并核验（配置迁移零差异、loaded=24 failed=0）。

## 3.8.0（2026-11-07）

- 视频自主分享放开与受众预筛选：新增 `allow_all_groups`（默认 true，所有已注册群可候选）与 `min_group_daily_messages`（默认 100，群当日非 bot 消息不足则不建立/更新画像、不纳入候选，节省 token 并防误发不活跃小群；陪伴私聊豁免）。出厂默认改为 `mode=自动分享`、`daily_limit=3`（含默认开启视频分享）。
- 动态睡眠锚点显示与一致性（Doc1 P1/P2/P3）：免睡模式下后处理不再补入睡眠意图（正常生成路径与模板兜底同口径）；正常/缺项补齐/LLM 失败模板的睡眠意图分类统一为 hard；WebUI 夜间睡眠条目独立标"作息参考"（按 goal_type 识别，不随 hardness 误标硬约定），睡眠详情并列显示配置参考作息/动态入睡窗口/实际入睡/最早醒沿/赖床上限。
- 硬约定核验门槛（P3）：仅用户明确确认（匹配 pending_commitment、带 commitment_hardness=hard）的硬约定可触发提前醒；LLM 新生成、来源不明的 hard 意图不作正式起床依据。睡眠锚点本就不参与硬约定唤醒（引擎已排除），运行态继续以 SleepEngine 会话为权威。
- WebUI 修复：夜间睡眠条目标"作息参考"并在睡眠详情并列配置参考与真实会话；系统板块"最近日志片段"框下移 12px，消除与模块 Token 芯片边框重叠。（历史按日实线/日期栏高亮见 3.7.1 后续提交。）
- 版本双号同步 3.8.0；配置 17 段 174 叶子字段/2 隐藏/172 可见（video_share +2 键），迁移保持用户值。本地七门禁 7/7、社交专项 71/71、睡眠锚点专项 7/7 通过；分享放开与睡眠/WebUI 两批已部署小主机并核验。

## 3.7.1（2026-11-07）

- 质量加固批，无新功能行为变更：独立视频分享调度器与账本核心（claim/finish/_auto_limit_reason）展开为多行，逻辑逐字等价。
- 异步路径中的同步账本调用改用 asyncio.to_thread 卸载，竞争写锁不再阻塞插件事件循环；record_from_result/snapshot 保持同步回调契约。
- 发送前路由复核只比身份键（stream_id/account_id/kind），群改名/昵称变化不再误判撤授权；账号或流变化仍拦截。
- 新增管理员运维子命令 /lbm_life share：status 只读诊断 uncertain 回执，clear <id> confirm 在人工核验确未送达后标记确知未发（保留审计、视频置终态、自动不自行恢复），缓解 uncertain 永不淘汰与回执上限的长期权衡。命令总数不变。
- 订正 suite_config 滞后的版本叙述。版本双号同步 3.7.1；配置 schema 不变（17段172字段），纯版本号递增，迁移保持用户值。
- 本地七门禁 7/7、社交专项 66/66（含新增 uncertain 运维 7 项）通过；业务 WebUI 与宿主配置保持。

## 3.7.0（2026-11-07）

- 新增独立、无工具的分享判断：分享/延后/不分享；自动分享移除旧ShareDelivery→optional_action→planner入口，保留自然手动工具。
- 显式授权受众、有界增量话题画像与覆盖/新鲜度校验；生产范围仅陪伴目标私聊，群聊为空。
- SQLite保存多候选、决策、BV×目标回执和事实；事务竞争防重，发送前预留现有预算，未知结果跨重启不补发；兼容迁移旧JSON但不重写原件。
- 群/私聊共用原生JSON卡片通道，账号身份校验避免跨账号错投；受理、送达、上下文同步分开记录。
- 手动分享共用防重，返回实际BV与标题；本流事实经成对Hook恢复到后续上下文，不创建新分享任务。
- 新增10项“视频自主分享”配置，默认影子；17段172字段、2隐藏，版本双号3.7.0。业务WebUI和宿主配置保持。

## 3.6.1（2026-11-06）

- 陪伴私聊主动任务统一指定用户最后来信为reply目标，覆盖早午晚安、想念、刷QQ、视频分享与点播完成；不再只依赖想念触发后90秒的旧软约束。
- 新增默认“浅处理”的回复目标档位；“深度处理”在planner输出到工具执行之间仅改写reply.msg_id，保留回复内容参考和其他工具参数，保留planner不回复的选择。
- 目标从本轮真实上下文提取，排除自身消息、系统提示和工具数据；任务结束、其他插件任务和新来信回合不沿用旧目标。未知或歧义情况跳过深改。
- 版本双号同步3.6.1；配置新增1项，16段162字段、2隐藏。业务WebUI不在本批修改范围。


## 3.6.0（2026-11-06，已部署小主机）

- 退役20个旧字段，6个日意图/结算字段转正、5个事件库字段并入日程；17段181字段收为16段161字段。旧自定义值、false/0、屏幕权限与作息保留，形象预设fish/human，默认human。
- 想念改为人设联系倾向与真实来信周期；删除独立Y/N把关。想念、刷QQ、视频分享只提供可选动机，planner可保持沉默；预算预留、未知防重和跨动机碰撞保护共用。
- 三类事件池与三列来源详情：内置5/可复用生成/一次性生成；双请求原子发布，一次性最终抽中先持久消费，启动失败或中断不返池。默认12=5+4+3，不提高触发频率。
- 生活状态只用目标趋近，删除旧三档覆盖与情绪速率调制；正常睡眠永久动态。睡眠边界仅按已确认跨度恢复，状态写盘失败回滚时钟和数值后再试；完成增量与回执同文件原子防重。
- QQ空间仅在真实午休静默，普通休息和跳过的午休意图不触发；发呆/躺一会/零食的完成反馈有界且避免重复恢复。
- 本地完整七门禁7/7；主体30/30、三池17/17、动态14/14、离线16/16、热重载5/5、数据投影2/2；桌面与手机三列、来源详情、折叠消费、翻页动画、fish/human及old/new兼容和旧快照通过。小主机插件目录外的独立副本已通过真实SDK迁移/默认合并抢救，16段161字段2隐藏，0模型/0消息。

## 屏幕感知与目标私聊工具 3.5.0（2026-11-06）

- 屏幕感知拆为陪伴模块后的独立 `[screen]` 配置，自动感知、截图模糊与范围保留原值；新段显式设置优先，旧布局及古老磁盘配置均兼容迁移。
- 新增默认关闭的屏幕窥探工具 `lbm_peek_companion_screen`。仅为已解析的陪伴目标私聊注册原生会话白名单，模型请求前再过滤，执行前后复核当前身份和开关；未知流、其他私聊、群聊均不可用。工具返回本轮文字描述和时间范围，不发送图片、不控制电脑、不打断 planner。
- 会话绑定仅保存于插件数据目录；首次绑定尚未就绪时不注册，下次宿主热重载生效。
- 配套本地连接客户端按完整 UTF-8 响应计算传输大小，计入重复主屏兼容副本。超限先省略副屏，再有界缩小主屏；发送前再校验，仍过大则返回小错误，保持连接。服务端 10 MiB 上限保持。

## 视频偏好与分享闭环 3.4.0（2026-11-05）

- 视频偏好首次及显式全量初始化只依人设生成，周期刷新参考刷新前近三日实际活动；复用生活节拍检查三天有效期，后台单飞，失败退避 30 分钟。显式初始化失败不会复活旧偏好，重置后旧候选池不可继续选片。
- 生产实测发现排行榜对真实偏好无匹配，补充公开关键词检索：每次至多两个偏好词，结果短缓存、真实 BV/时长校验和观看去重。去除标题高亮标签，检索依据与实际标题命中分开记录；不伪造视频、不用不相关热门内容填满候选。
- 关键词检索有 6 条真实候选及 3 个有效 BV 的成功样本，也观察到间歇 HTTP 412。已有可用结果就停止检索其他词；412/429 退避 10 分钟并区分诊断，不连续请求、不声称外部接口已稳定。
- 新增 `lbm_share_recent_video`：从真实完成且文字记忆有效的近期视频中选一条，向当前聊天提交视频卡片，返回 BV、标题与受理状态；必要时由 planner 使用观看工具按 BV 取缓存。发送未知、损坏/满回执均保守防重，不补发，不生成新观看记录。
- 新增默认开启的 `[llm].prompt_optimization_enabled`：仅对已标记短任务观察结构化 reasoning 输出，后续固定追加一次直接输出要求。显式模型分别标记；宿主隐式路由只能按任务与事件约束，包含该任务内部的模型兜底。不延长超时、不额外重试、不改变宿主路由或 Token 限制。
- 接续另一会话已有问候间隔修复：早/午/晚问候刷新陪伴发言时间，不增加主动计数，使想念巡检沿用原间隔与冷却。08:54 两句相同早安取证为「早安后又触发想念，第二回合复述」，不是两次早安结算。
- 动态睡眠出厂仍默认关闭；本次按用户要求通过宿主官方配置接口启用小主机现值，仅改 `life_state.dynamic_sleep_enabled`，漂移 45 分钟、v2、预算 5 和作息锚点保持。滚动待机优化仅规划，空间视频联动仍为独立计划。

## 动态睡眠与日程详情 3.3.0（2026-11-05，已部署 mini-direct；条目补记于 11-05）

> 补记时点：2026-11-05 18:30 许（Asia/Shanghai，文档收口批）。manifest 已 3.3.0 而本条目缺失，现据开发文档当前状态 v3.3.0 段、work_report/20261105_0158、执行工作区证据（release manifest / gates 输出 / 生产部署与复验 JSON）及 checkpoint 分支差异（`git log checkpoint/20261105-dynamic-release..checkpoint/20261105-schedule-layout --stat`）重建，非当时写就；各批验收数字引自当时落盘输出，本补记批未重跑。

- **01:56/01:58 首批（动态睡眠与日程详情）**：P1–P3 接入同一 SleepEngine（评分、入睡/起床窗口、困倦表现、软日程调整、逐日相位适应均受动态开关控制），午休改滚动休息活动（起晚/精神好/无空档可跳过）；日程主页面显示实际活动、同类短待机界面归组（间隔与原段可展开），详情双列；新增配置 `dynamic_sleep_enabled`/`sleep_drift_max_minutes`，`/lbm_plan sleep` 受入睡窗口与约定约束，诊断入口 `/lbm_life rolling sleep` 与 WebUI 睡眠详情。01:56 上传 21 路径、01:58 补发 3 个界面文件，沿用同一 3.3.0 版本；门禁 7/7、动态专项 13/13、关闭模式 288 拍日程/睡眠回放与基线逐字一致；生产生效动态关闭、漂移上限 45 分钟、结算 v2、179 字段 16 段、预算 5、作息 01:00–08:30。源码 `7bab58f`、测试 `8cc4815`，分支 `checkpoint/20261105-dynamic-release`；备份 `lbm_backup_dynamic_20261105`/`lbm_backup_dynamic_20261105_ui`。开启后的动态自然夜与自动午休待现场核验。详见[部署验收报告](../cateye_life_belonging_to_maimai开发文档/work_report/20261105_0158_动态睡眠与日程详情部署.md)。
- **02:25 usage-detail 补批**：重做模型调用详情（用量概览、模块模型、逐笔筛选），固定弹窗支持矮屏、触屏层级复核。源码 `4484b6c`（详情面板重设计，5 个 WebUI 文件）+`75b7a27`（弹窗层级高于桌宠，2 文件）；首批 02:20 部署 5 路径，02:21 首次复验脚本遇 HTTP 502（exit=1）未落成功结果，层级补批 02:25 部署 2 文件后终验 02:25:37 通过：组合 5 路径 SHA 与运行页面字节匹配、7 天汇总 calls=215、真实 50 条分页、配置保持、SQLite ok。检查点源码 `75b7a27`、测试 `2e1122c`，分支 `checkpoint/20261105-usage-detail`；备份 `lbm_backup_usage_detail_20261105`/`lbm_backup_usage_layer_20261105`。
- **03:07 schedule-pools 补批**：修复 Token 外页，日程时间线恢复固定意图，新增小日程/待恢复/内置/生成事件池分页与来源详情；日程投影与插件 SDK 解耦。源码 `bd67d65`+`7f72c3d`，7 路径（planning/dashboard.py、planning/life_schedule.py + 5 WebUI）部署复验匹配，实际回传固定意图 3、内置事件 6、生成事件 6、待恢复 0，配置保持、SQLite ok。检查点源码 `7f72c3d`、测试 `2dcaa7c`，分支 `checkpoint/20261105-schedule-pools`；备份 `lbm_backup_schedule_pools_20261105`。同批压力只读取证：最近 288 采样 100 次上升，工作段从 0 升至约 6%（0.060061），睡眠取样时约 2.2%。本批五页布局由 05:55 批次取代。
- **05:55 schedule-layout 补批**：修复行程记录分页框左上圆角，页面收为「行程记录 / 小事件」两页，取消重复小日程页；完整计划意图仍在第一页详情右列，待恢复事件池移到左列下方；内置与生成小事件在主页面和来源详情均双列显示；翻页沿用板块淡出/淡入并支持减少动画设置。桌面、手机专项及七门禁 7/7 通过；源码 `2d3f818`（5 个 WebUI 文件）部署 5 路径 SHA 与运行页面字节匹配，配置保持、SQLite 正常。检查点源码 `2d3f818`、测试 `e0d507b`，分支 `checkpoint/20261105-schedule-layout`；备份 `lbm_backup_schedule_layout_20261105`。此批取代 03:07 批次的五页布局。
- **版本与红线**：`_manifest.json` version=3.3.0；四批均为备份+staging 全验+原子移入+mtime-touch 触发 file_watcher/页面缓存刷新路径，无宿主启停记录；每日主动预算 5、睡眠锚点 01:00–08:30、名单、凭据未动；`dynamic_sleep_enabled` 生产生效 false（11-05 18:30 许只读复验仍为 false、漂移 45 分钟）。三份补批工作报告见开发文档 work_report/20261105_0225、20261105_0307、20261105_0555（均补记于 11-05）。
- **订正说明（compaction 口径，3.2.0 历史条目不倒改）**：下方 3.2.0 条目末项"context_compaction 已完成视频话题闭合/过期"位于"宿主侧建议（未实施，需另行授权）"标题下，是建议项的压缩表述（建议原文见 union/20261104_主动行为与视频排查/收口报告.md §五.2：对已完成视频话题设闭合/过期），易误读为已完成。11-05 傍晚 SSH 只读核实：宿主实际配置 `config/bot_config.toml`（根目录无同名文件）compaction 相关仅 `context_compaction_summary_max_chars=6000` 一键（[chat] 段第 78 行），无话题闭合/过期配置；`data/custom_prompts` 无 11-05 凌晨改动（manifest mtime 10-30 20:32、active_version_id=null，唯一版本文件 mtime 09-03 05:30），内容为 maisaka 决策提示词、不含视频话题闭合/过期语义。**结论：该宿主建议未实施**，与开发文档当前状态遗留口径一致；实施仍需另行授权。

## 主动行为重写与刷视频功能 3.2.0（2026-11-04，已部署 mini-direct）

- 本批四线部署：24 文件（22 差异+2 新增 miss_trigger.py/video_interest.py）staging 全验+生产 py_compile 21/21+原子移入，活体 SHA 24/24 一致；file_watcher 重载 22:19:56 loaded=24 failed=0 inactive=11，v3.2.0 加载确认，本插件 ERROR=0；备份 lbm_backup_proactive_20261104_2208（213 文件）。门禁终轮 --all 7/7 exit=0 全绿（首轮 4/7 失败均为陈旧断言，5 测试文件按等价不变量重钉带批注、未削弱检查，未发现真实代码缺陷）。部署事件：备份初置 plugins/ 内致重复插件 ID failed=1，移至 MaiBot 根目录+mtime-touch 重载转绿，未回滚代码。
- **主动行为重写**：想念改状态驱动概率 p=clamp(0.65×状态×情绪×好感,0,0.9)（中性≈0.668，较原日程巡检主路径 0.6 略升 +11.4%；低落/低社交电量更易想念，因子有界、不可得降级 1.0），不再要求沉默超阈值、不再每日一次；**日程主动巡检删除**（排查证实它只服务目标私聊、对群内日程提及贡献 0），4 个旧配置键（patrol_activity_enabled/activity_trigger_rate/default_speak_rate/miss_speak_rate）升级时一次性 pop 退役；**中午问好**新增（仅目标陪伴私聊 12:00-13:00，双重去重落盘热重载不重发、免预算、睡眠闸）；**早安有界**（期限≤min(配置上限默认 10:00, 醒后 2 小时)，超界静默放弃留诊断）。每日主动预算 5、静默/冷却/LLM 把关保留；群内主动仍只有早/晚安。
- **刷视频功能**：日程「刷视频」事件现在真正能自动选片——先按人设+当日日程询问麦麦想刷什么（兴趣关键词 3 天缓存，热重载沿用），再从 B 站 popular 排行榜免鉴权候选中按关键词匹配，对已看历史∪文字缓存∪活跃任务三重去重；无合适候选如实跳过、不编造 BV。观看中回复率倍率 dedicated 0.45/gap 0.70（修复 scenario 误读+结束恢复）；空隙刷视频额度消费接线（空闲 10% 掷点+30 分钟冷却，日程恢复即停）。事件受 [video_watch] enabled 运行时门控。popular 接口未真机验证、整链真实验收待首个真实 episode。
- **日程注入门控**：群与非目标私聊的实况注入改双通道意图门控——问当前/问未来给全量，闲聊只给当前活动一行；replyer 非询问不再携带"接下来 X 要 Y"；上下文缓存判据改"近 2 轮确有询问"，切断"自提日程→判成追问→再注入"反馈环。目标陪伴私聊保留全量；空活动防编造兜底保留。取证基线：群 planner 64-69% 回合带日程块、多起逐字泄漏；门控后效果待自然窗口观察。
- **视频措辞**：修复前批遗留的 9 处视觉缺失 outward 措辞（前批只修音频）——描述/观后感生成聚焦内容本身、不主动强调"没有画面/没采样到/资料有限"，被直接问及时诚实简短；impression_error 改内部诊断 impression_diag 不再外泄。排查证实"念念不忘"主因是宿主滚动摘要固化视频话题（插件通知防重/缓存按需均正确），宿主侧摘要闭合建议待另行授权；15 天旧缓存旧措辞自然过期、不重算。
- **配置**：schema 179→**177 字段**（删 4 增 2），config_version 与 manifest 同步 **3.2.0**（R6）；新增 [planning_proactive] greeting_morning_latest（默认 "10:00"）/greeting_noon_enabled（默认 true）；config SHA 7625acbe…→ed985977…（预期迁移：纯 pop+默认键）；daily_max_speak=5、睡眠锚点、名单、settlement_model=v2 不变。_manifest.json 描述订正（主动聊天新口径+/lbm_init 初始化文案）。
- 宿主侧建议（未实施，需另行授权）：群 177127696 专属 talk_value_rule 0.2-0.3、prompt 硬约束"连发 2 条无回应必须 stop"、自发 reply 不清零 wait 计数；context_compaction 已完成视频话题闭合/过期。详见 union/20261104_主动行为与视频排查/收口报告.md 与开发文档当前状态。

## 全量推进部署与结算 v2 启用（2026-11-04，已部署 mini-direct）

- 配置精简 3.1.0 部署：23 代码文件（20 差异+3 新增）staging 全验+生产 .venv 解释器 py_compile+原子移入，活体 SHA 全一致（LIVE_SHA_ALL_OK）；file_watcher 重载 07:35:38→07:36:09，loaded=24 failed=0 inactive=11，本插件 ERROR=0；备份 lbm_backup_pushforward_20261104_0729（215 文件）。下条「已实施待部署」自本条起为已部署（原条目保留时点不倒改）。门禁 exit=0 全绿。
- 部署重载触发插件自带配置迁移（预期行为）：J 类 9 键（horizon_minutes=90 等）自 config.toml 移入 mappings/internal_tuning.json[life_state] 并保值、新增 settlement_model/settlement_v2_params_json 键；config SHA 链 C3859237(部署前)→AEF87B75(迁移后 v1)→7625ACBE(v2 活体)。
- **结算 v2 正式启用**（用户「全量推进」授权）：备份后 config.toml 仅 settlement_model v1→v2（精确替换 1 字节，活体逐键对比其余 178 键全同），file_watcher 07:41:29 热重载（运行器 pid 不变，未启停宿主）；07:46:02 首结算 settlement_diag version=20261103.1（kind=sleep、dt_min=5.0046），contrib 与五轴变化精确吻合；**数值未重置**——sleep_debt=9.4715 精确保留，energy/social_battery/continuous_sleep 为 5 分钟自然连续演化；ERROR=0、无异常未回滚。回滚口径：仅退 v2 用 config_v2_before.toml（AEF87B75）；整体代码回滚用 backup/plugin（含迁移前 config）。清醒段待醒后自然触发观察。
- m.q.qq.com 短链闭环核查：6 种 UA 实测均 200 无重定向、HTTP 层无可提取视频目标，bv.py 维持安全 None 未改（SHA 0602852b…）；b23.tv 终验路径 13/13 用例过。bot 自产卡片 qqdocurl 注入（方案 A/B）待用户另行安排。
- 待补全：life_init.json 统一记录（管理员 /lbm_init，幂等零模型消耗）、活动画像中文别名（放松/休息）、v2 清醒段自然观察。详见 union/20261104_全量推进/推进报告.md 与开发文档当前状态。

## 配置精简 3.1.0（2026-11-04，已实施待部署；用户「全量推进」裁决）

- 按 union/20261104_全量推进/配置精简方案.md 逐字段落实：聚合 schema 224 → 179 叶子字段（keep 179 / json 40 / hardcode 5）。config_version 与 manifest 同步递增 3.0.0 → 3.1.0（R6）；layout_version 标记不换新（C-12）。
- **J 类 40 项**内部调参（阈值/权重/容量/缓存/超时/采样温度）移入 `mappings/internal_tuning.json`（表层用户表）+ `default_mappings/internal_tuning.json`（随包种子，第八张表）；各 section 以同名只读 property 桥接，全部既有消费点零改动。旧 TOML 键经 `migrate_internal_tuning` 在 SDK merge 前值搬运落盘（只补缺失键、不覆盖现场微调；宿主版本重建丢键由磁盘捕获通道兜底，不受布局哨兵门控）；古老段（cateye/judge/inject/planning_cleanup）搬入的 J 值经校验器二次捕获入队、normalize 校验成功后统一落盘。
- **H 类 5 项**固化为代码常量（生产现值=默认已获 evidence 证实）：life_state.tick_seconds / sleep_wake_check_seconds（=300 内部节拍）、state_file（逐字 'life_state.json'）、event_catalog.temperature（锁定 0.9）、max_events（兼容死键，迁移函数保留）；旧键经 LEGACY_ROUTE_KEYS 入口 pop。
- 红线保全：睡眠锚点/时钟字段、每日主动预算上限 5、settlement_model/v2 参数入口、名单、凭据等 179 项 keep 逐字不动；4 个 evidence 未列段（affection/schedule_advanced/prompts/dashboard_display）一律不 hardcode。
- 离线复现全链路通过（scratch/_cfg_simplify_checks.py：版本重建→磁盘捕获→落盘→幂等→merge 默认路径→古老配置二次捕获→坏表兜底）；既有配置回归（scratch/_config_round_checks.py，快照断言已同步 179/3/176 与 3.1.0 双号）全套通过。WebUI 配置页随 schema 自动收缩；面板 history_*_epsilon 投影经 property 继续供给，前端零改动。

## 视频链路补充修复与终批部署（2026-11-04，已部署 mini-direct）

- 针对 11-04 凌晨卡片点播事件取证的四处断点补充修复：① 画面采样单点超时/失败重试一次后跳过续采，部分帧也出拼图（如实标注采样帧数），仅全部采样点失败才降级文本来源；不突破下载限额、不自动全片下载。② 识别 QQ 小程序分享短链 m.q.qq.com/a/s/（与 b23.tv 同安全模式：展开重定向并校验最终目标为 bilibili；无重定向时如实拒绝、不猜测 BV）。③ 面向麦麦与用户的文本不再反复强调"没有声音/音频"，聚焦画面内容本身；内部诊断仍如实记录音频未转写，被直接问及时诚实简短回应。④ 点播完成通知加睡眠闸：睡眠/静默/午休时段暂缓推送，通知保留待醒后重检单次投递，不再深夜直推引发主动发言。
- 终批部署 43 文件（5 个语义变更 + 38 个纯行尾归一化），备份 lbm_backup_fullfix_20261104_0551（210 文件）；file_watcher 重载 05:55:10→05:55:37，loaded=24 failed=0，43/43 SHA 复验一致，本插件 ERROR/WARNING=0；config.toml 未改、生产 effective=v1 不变。门禁 exit=0（终版事实源 05:43:58）。
- 全批（含下方第一波条目）语义变更合计 53 文件、53 项修复。待裁决不变：v2 结算启用、自动分享候选源接入。详见开发文档当前状态与 union/20261104_全量修复/修复报告.md（终版）。

## 全量修复与卡片分享（2026-11-04，已部署 mini-direct）

- 基于 11-04 模块深度评估（62 项发现、无高危）的全量修复：8 模块 49 项编号修复 + 组合根集成，51 文件部署（49 改 + 2 新增），门禁 exit=0 全绿；备份 lbm_backup_fullfix_20261104_0507，51/51 SHA 复验一致，config.toml 未改、生产 effective=v1 不变。
- **视频分享卡片化**：分享一律发 B 站小程序卡片（两步透传生成+发送），不再发纯文本链接；卡片生成失败则不分享，发送结果不明保守防重。自动分享候选来源仍未接入，整链真实验收待授权。
- **意图分类调优开关**：「日程系统·高级设置」新增 `intent_tuning_enabled`（默认开）——修复"问未来/陈述句被判问当前、技术求助漏判"的宽误判；关闭即完整回退旧分类规则。
- 睡眠/日程：重启后"本睡眠环不再重开"闸门修复（会话查询改最近优先）；醒来事件标记随会话持久化；边界拍不再微量过施加；日程库损坏重建补版本行、目标更新列白名单、写库时间戳统一 +08:00；清理服务改读现行配置段（用户自定义间隔/保留天数生效）；日意图生成失败不再当日放弃（退避重试）；随机事件增量先应用后标记。
- 陪伴：告状发送纳入主动预算闸门、冷却跨热重载持久化；好感度调档工具加开关与目标私聊校验（拒绝留痕）；巡检异常留痕、回复补计校验会话流；预算查询/记账统一持锁、提交流按预留入账。
- 情绪：睡眠裁判异常不再静默（计入失败上限/退避）；片段评估配额加在途预占防并发击穿；水位表 7 天清理；观测旁路故障留痕。
- QQ 空间：工具/命令发帖补齐睡眠/静默/午休/冷却/严判闸门；午休读日程笔误与循环吞错修复；回评生成失败保留重试（单轮上限 5）；CGI 回退透传可见性（私密帖不再变公开）；请求级脱敏诊断；全部 CGI 客户端禁环境代理、cookie 落盘权限收紧（Windows 效果有限已如实标注）。
- 视频链：预算门面缺位改拒绝；任务/作业目录按 15 天+容量清理（非终态不删）；加工超时降级记忆纳入已完成画面摘要；退款还原间隔锚点；活跃去重仅按 BV。
- 基础层：LLM 超时/断连/取消/回退首尝异常补记账本（只计次数不估 Token）；过期主动块仅剥本插件；账本裁剪临时名加进程/随机后缀；面板并列展示旧 schema 口径；损坏 JSON 回退留痕。
- WebUI：方角宿主下 20px 圆角守卫真正生效（复现宿主双 style 时序验证）；构建产物换行固定跨平台 SHA 一致；用量窗口仅参数类错误降级；逐笔明细防并发乱序；桌宠早退接口补全；仪表盘时区显式化；页高红线钳制+旧页清理。
- 组合根：离线结算编排下沉独立模块（行为不变）；停机链 4s 总超时保底落盘；睡眠拍失败回滚重试 3 拍；插件总开关关闭时视频服务同步停摆；删除已移除私信工具的死提示词/配置位（配置字段 224 不变）。
- 待裁决：是否启用 v2 结算（睡眠债结构性增长决策包已备好）；自动分享候选源接入（新功能）。详见开发文档当前状态与 union/20261104_全量修复/修复报告.md。

## 日程：回滚实况门控 + 空活动兜底显式化（2026-11-03，已部署）

- 回滚"实况块仅问当前/问未来注入"，恢复无条件注入（用户直派）。
- 空活动兜底改为显式告知"你现在没有进行中的活动…不要汇报不存在的活动或日程"，并移除优化器对空活动闲聊的跳过，让该告知能送达 bot。

## 日程：弱化注入强调（2026-11-03，已部署）

- 实况追加块（当前活动实况/接下来/刚完成）只在用户问当前/问未来时注入；闲聊/延续/主动不再每次塞入 salient 日程行。
- 引导行明确禁止主动汇报日程/接下来安排/刚完成的事；冷场优先等待或结束，不为填补空白起话题。
- 空活动兜底去掉"（可能在休息）"猜测措辞。
- 目的：降低 bot "太爱唠日程"的 priming（真机取证见工作报告）。

## 陪伴：移除主动私信工具（2026-11-03，已部署）

- 删除 lbm_mai_lover_send_message 工具：此前它可绕过主动发言预算/冷却直发私聊（P0 缺口），现关闭该路径。
- 主动发言统一经巡检→人格化主动回合，受每日预算与冷却守卫；不提高每日上限。
- 配置 schema 与历史决策日志标签保留，不影响既有数据。

## 生活面板：桌宠气泡文案排版修复（2026-11-03，已部署，含回滚点）

- 修复气泡文案偏移：中心改取椭圆内腔的实测水平+垂直中心（沿赤道列取连续不透明段，排除思考点尾巴干扰）。
- 修复长文案撞边框：文本框改为椭圆内接安全框（宽≈直径72%、高≈内腔62%）+内边距避让；超长时自动逐步缩字号直到放下，不再靠溢出/撞边容纳。
- 短文案保持原字号居中；桌宠形象与捏捏交互不变。
- 视觉微调：文案整体下移约 1/4 字高（transform 垂直 +0.25em，随字号缩放），抵消字形偏上观感。

## 生活面板：整页视觉重构（2026-11-03，已部署，含回滚点）

- 全新设计语言：分段胶囊页签（选中实心主色）、玻璃顶栏（嵌入宿主时收为紧凑工具条）、统一 20px 圆角卡片+细描边+柔和投影、暖纸底主色晕染。
- 区块标题改小写眉题+主色竖条；大数字用显示字体；徽标/观察条药丸化；表格斑马纹+大写表头。
- 日程改等高双列：左右底边对齐，右列两卡内部滚动、滚动条贴卡片右边界；时间线限高内滚。
- 桌宠与板块左下角小鱼详情入口保持不变；睡眠曲线维持「主卡极简趋势+详情全图」。
- 圆角守卫同步为 20px，宿主方角主题下仍保持设计圆角。

## 生活面板：视觉精修（2026-11-03，已部署，含回滚点）

- 五项状态卡每卡一色：图标浅色底、数值更挺括，并新增「较上一真实采样」趋势小标签（只算真实观察点，估算不参与）。
- 区块标题加主色竖条、页签选中加指示条；徽标、观察条统一为更圆润的药丸样式。
- 表格加斑马纹与悬停高亮、滚动时表头固定；日程时间线「进行中」轻底色、计划/实际徽标区分着色。
- 睡眠债按轻重着色（轻绿/中橙/重红），不再一律红色；陪伴主动次数超预算时如实标红。
- Token 等大数字加千分位便于阅读（悬停可见精确值）。
- 卡片外壳仍跟随宿主主题；左下角小鱼详情入口保持不变。
- 滚动条统一为「无框 + 细条」：轨道透明无边框、滑块细圆角、悬停略加深，覆盖页面/列表/表格/时间线/详情层（麦麦气泡保留专属棕色细条）。

## 生活面板：小鱼详情回归与日程排版修正（2026-11-03，已部署）

- 各模块详情入口恢复为板块左下角的探头小鱼：点击打开/固定详情气泡，悬停预览，再点收起；上一版的标题行「详情」文字按钮撤销。
- 模块圆角恢复为固定的 18px 设计值，不再被宿主主题的卡片圆角采样抹平；颜色与字体仍跟随宿主主题。
- 日程页右侧「实际活动记录 / 计划意图」改为紧贴「当日活动」摘要置顶显示，刚进入不再出现右侧空白；左侧行程时间线限高内滚，长日程不再把页面撑到几屏。
- 「数据与运行说明」摘要文案改为指引点左下角麦麦查看来源与明细。
- 其余首屏摘要、按页签渲染、跨午夜时间标注、用量「未回传」口径等更新保持不变。

## 结算v2与桌宠/WebUI批次（2026-11-03，代码已部署）

- 新增互斥的目标趋近结算v2及参数版本；默认仍v1，当前生产effective=v1，尚未启用新模型。
- 麦麦互动收敛为开关/预设/共用尺寸；新旧形象、拼体气泡和32组状态文案已部署。
- 首屏摘要、对比度、详情与诊断等P0更新已上线；D1遮挡、W-11/W-15/W-16及最新全量回归/黄金基线仍有余项。
- 账本已写入真实schema2记录，旧“首条待验”结论过时；不倒改旧用量。
- 已确认私聊直发工具可绕过主动预算与冷却，尚未修复。部署成功不代表该缺口已关闭。
- 当前部署含未提交工作树；按文件SHA核对，本轮文档整理没有提交业务代码或改变生产开关。

本文件记录「属于麦麦的生活」（lbm）**面向使用者**的版本变更：新增能力、口径变化、缺陷修复与已知问题。

## LLM 统一入口与用量明细（2026-11-02，已部署 mini-direct）

- 新增 `tools/llm_gateway.py`：所有模型调用经唯一入口，`module` 必填（planning/emotion/qzone/companion/video/core），缺失或非法记 `unknown` 并留 warning；`LLMService` 保留原签名、内部委托 Gateway。
- 账本 v2：每次宿主响应写入模块、任务名、输入/输出、缓存命中/未命中、耗时、尝试次数与 `abandoned` 标记；旧行不改写，读取端按 legacy 桶回退。
- 保留期默认 30 天（`KEEP_HOURS=720`），只在跨日后首次写入或累计 1500 行时惰性裁剪；显示窗口 24 小时 / 7 天 / 30 天，默认 30 天。
- WebUI「模型调用与 Token」新增窗口选择器、输入/输出与缓存命中概览、模块 × 模型矩阵与逐笔明细；旧 `webui.json` 未声明新参数时自动退回旧参数，页面仍可用。
- `[llm]` 新增 `usage_retention_hours`、`usage_default_window_hours`（默认均 720）；配置面 16 段 / 231 字段 / 5 隐藏 / 226 可见。
- 2026-11-02 23:03 已部署 mini-direct（生产代码 `b469bff`，宿主文件监听重载成功）；生产 `webui.json` 已合并 `usage_hours`/`usage_rows_limit`/`usage_rows_offset`，30 天窗口与逐笔明细已可用。部署备份 `C:/Users/Administrator/Desktop/MaiBot/lbm_backup_llmwebui_20261102_2301`。

## 视频内容描述接入画面采样（2026-11-02，已部署 mini-direct）

- 独立内容描述请求现在使用已完成的画面采样理解，并附带真实采样时间点/范围/帧数/拼图数；有公开字幕时附真实字幕节选。
- 提示词要求区分「画面可确认 / 简介或字幕补充 / 未覆盖或不确定」，小字数值不确定时不得当作精确事实；无视觉结果时如实退回元信息。
- planner 五字段、完整输出、实际 Token 用量与 15 天文字记忆不变；旧文字缓存不会自动重算，真实复验需明确重看。

## 代码质量整改（2026-11-02，已部署 mini-direct）

- 门禁：补齐七门禁单一入口（`python run_gates_lbm.py --all`），smoke 通过后写 `_gate_facts.json`，组件/配置/双仓版本改为单点事实源。
- 公共层：新增 `core/coerce.py`、`core/i18n.py`、`core/mapping_table.py`，收敛 26 处数值/i18n 副本与 6 个映射表装载器的路径、读取、版本校验样板。
- 结构守卫：新增组合根字符串属性契约与高风险 `except-pass` 两项守卫；`planning/services.py` 迁移到 `PluginContext` 具名视图。
- 文档：README 组件/配置数字按离线门禁实算重钉；`planning/` 明确为时间/日程领域内核。
- 修正 `[video_watch]` 字段英文翻译返回键（此前使用 `_ui_i18n`，宿主读取的 `i18n` 为空）。
- 发布门槛：`sdk.min_version` 提升为已实测的 2.8.0，避免旧 SDK 潜在不兼容。
- 结构：`companion` 两个长诊断方法下沉独立模块；`emotion` 状态持久化与展示/摘要层抽离（评估器、睡眠裁判仍待后续）。
- 2026-11-02 08:02 已部署 mini-direct（本地 `f8b986b`）；生产配置/表层数据保留。

## 日程跨午夜误餐修正（2026-11-02）

- 修复当天未来意图的跨午夜尾段被误当成当天凌晨候选，防止低饥饿时执行未来晚餐。
- 兜底晚餐窗口收紧为入睡前3–5小时，沿作息锚点派生，不硬编码钟点。
- 保持高饥饿应急进食、已启动跨午夜活动与睡眠处理；保留已发生的真实历史。

## 视频完整描述与观看记忆（2026-11-02）

- 拼图识别后独立请求标题/UP主/简介的详细内容描述，planner仅接标题、UP主、时长、内容描述和观后感；完整保留文本，取消300/1600/2200字等插件截断。
- 默认采样间隔30→25秒，拼图张数仍按实际采样准入；宿主输出长度仍是唯一Token上限。
- 缓存仅BV与文字、默认15天固定保留期；命中明确是旧观看记忆，无任务/模型/通知。显式重看仍遵守活跃任务去重。
- 日程命中记忆退回未启动的观看预留，不当作新观看或新分享。旧缓存迁移保留保存时间，不改任务历史。
- 工具不强制中断planner；本批不修改、构建或部署WebUI。

## 配置与提示词整理（2026-11-01）

- 日程命令与注入组件注册名移除历史版本后缀，用户命令入口保持不变。
- 历史变化显示阈值与条数移至「数据展示」，旧位置值兼容迁移；「麦麦互动」仅保留交互参数。
- 告状目标按显式对象、陪伴对象、宿主全局管理员解析；不存在有效对象时不发送。
- 移除视频配置待看列表；直接点播与后台完成通知保持可用，自动日程无候选时跳过。
- 常用提示词保留13项，43项内部协议改为代码维护；统一重复回退模板，好感度裁判文案对齐现行数值边界。
- 配置说明删除历史变更叙述，保留功能、条件与单位。WebUI文件不在本批修改或部署范围。

## 2026-11-01 链接、stream与后台通知纠偏

- 支持QQ分享文字/小程序短链，新增resolve，受控跳转且不允许猜BV。
- 默认stream直连定点采样，可切换download与seek/paced；拼图张数准入替代时长/散帧旧限值，4×4时间标注图降低提交图片数。
- 后台终态通知来源聊天，持久意图防重；工具受理后stop_after_execution结束轮次，不再指导轮询。
- “视频观看”配置分组汉化，13字段；原型旧时长/散帧键可加载但不继续限制新模式。未修改WebUI文件。

## 2026-11-01 小主机部署前验收修复

- 视频：任务身份/模式隔离、排队绝对截止、父侧取消与帧清理、完整worker进程树回收、插件自有解码器与定点采样。
- 日程与陪伴：自动门控接通，真实额度预留与租约释放，运行态驱动回复倍率；私聊倍率保持1.0。
- 分享：持久化投递意图、并发单飞、目标绑定、既有主动预算预留、未知送达防重及真实SDK回执判定。
- 验收：新增跨模块与异步竞态回归；间隙冲浪及生产自然链仍按真实证据登记，不冒充已通过。

## 未发布增量 - 2026-11-01（后端解耦 D2–D5 收口 + 视频观看内置化 B1–B3；本地检查点，未部署）

- **视频观看并入本插件（B1）**：新增内置模块 `video_watch/`（任务/媒体/结果/缓存单一服务实例），注册 AI 工具 `lbm_watch_bili_video`（submit/status/result/cancel）；不再要求安装独立插件 `cateye.bili-watch`，旧原型仓只读保留、不作为运行依赖。新增 `[video_watch]` 配置段（11 键：开关/队列/时限/时长上限/观后感/待看列表/画面采样）；旧独立插件节名 `[bili_watch]` 的配置值升级时自动并入新段。
- **画面采样（B2，诚实降级）**：本机装有 ffmpeg 且 `[video_watch] visual_enabled=true` 时，观看任务会做**有界**画面采样（低清直链限额下载 → 等间隔抽帧 ≤12 帧 → vlm 多模态任务摘要）；无 ffmpeg、下载超限、受限/登录/会员视频、vlm 任务不可用都如实标注原因，**文本来源结果不受影响**。结果模式分型入缓存键（`text_sources` 与 `frames-*` 互不命中，旧文本缓存不会冒充已看画面）。观看记忆只保留摘要与采样依据，不落原始帧；无音轨转写，不编造对白。仅记录宿主回传的真实 usage，不设插件输出上限。
- **日程刷视频换内部端口（B3）**：日程「刷视频」事件改为直接调用本插件内部观看服务（与 AI 工具共用同一实例/队列/缓存），删除对 `cateye.bili-watch` 的跨插件 API 依赖；唯一提交、去重、抢占/取消、晚到结果存档语义不变；观看完成**不会**自动向任何群或私聊发消息。
- **后端解耦 D2–D5（内部结构，行为不变）**：D2 事件总线与打扰预算分文件（预算归 companion 单写者，模块级全局兜底删除）；D3 打断情绪↔陪伴依赖环（情绪经注入的锚点查询面/组合根公共口取真实作息锚点，情绪包零 companion 导入；qzone/面板经组合根公共门面取陪伴判定与摘要）；D4 删除 `core.next_run_at` 死代码（两套 time_utils 保持分层不合并）；D5 结构守卫（业务间 import 允许清单逐条带所有者与退役计划、无动态 import 藏匿、emotion↔companion 双向零容忍）。组件计数 30→31（8 命令 + 10 Hook + 8 Tool + 5 API）、配置键 259→270（隐藏 5 不变）。
- **已知边界与待验**：真实宿主上的画面采样链路（playurl 直链可得性、ffmpeg 版本、vlm 任务已配置视觉模型）未实测——不可用时自动降级为文本来源并如实标注；自然夜与生产环境行为未验证；Windows 进程树硬隔离未实现（worker 为单进程无孙进程，超时用 kill+等待，与独立原型同口径如实记录）。

## 未发布增量 - 2026-11-01（探头姿态/粘连气泡 U 线 + 刷视频日程事件 B2 + 解耦 D0/D1；本地检查点，未部署）

- **桌宠姿态与动作（U 线）**：常驻麦麦与各板块入口的小麦麦统一为 logo 式「贴角冒头」——只露头顶+叶+眼睛（眼睛完整可见，尾巴上的叶子移出视野），身体藏进角落；**入口小麦麦放进各板块框内、贴左下圆角**，越界部分由卡片自身圆角自然裁剪（不再有矩形蒙版凸起），桌宠则更深地收进视口左下角（常驻麦麦默认尺寸放大 96→128）。鼠标指向露出的麦麦后**先向上弹起、再落到左下角底部**（带落地缓冲，全程约 0.56s），落地后才接受捏捏；**按住渐变压扁并保持压扁形态**（约 0.26s 渐变，此前按压会自动弹回，属实现缺陷已修），松开后从实际压扁程度连续回弹，回弹中再次按住无缝接续，取消/失焦/窗口切换不会卡死或永久压扁。指针远离左下角交互区域（默认 200px，进出双阈值防抖）约 0.2s 后自动收起回探头姿态；触屏首次点按探头弹出、点空白收起；键盘 Enter/Space 在探头态=弹出、落地态=回弹。原「对角直线平移加转正」的假弹出移除。
- **详情粘连气泡（U 线）**：恢复悬停临时预览（不抢键盘焦点、不锁滚动）与点击固定（锁定滚动+焦点管理）、再点同一入口取消固定；点击另一入口直接切换。详情面板**以本次入口为原点**展开/回收，并**按入口周围的可用空间自适应**——面板放在空间更多的一侧（上/下），高度适配该侧空间，曲线气泡尾从面板最近的一边连住入口（不限定哪一边），入口与内容始终完整可见；入口**原位保留并提升到遮罩之上**（同一元素 portal，不再搬进标题栏、不再消失），左下角常驻麦麦在详情期间保持可见（仅暂停捏捏输入）。背景为**轻微模糊（默认 3px）+ 低强度遮罩**，背后内容可辨认（修复：旧规则把遮罩层盖成不透明导致模糊从未生效）；不支持 backdrop-filter 时降级为仍可辨认背景的轻遮罩。
- **配置变化（[maimai_interaction] 16→15 键）**：`detail_fish_size_px` 随「入口搬标题栏」退役（旧配置自动清除）；`mascot_size_px` 默认 96→128、`peek_angle_deg` 默认 45→12（小倾角冒头）、`detail_blur_px` 默认 16→3、`detail_scale_from` 默认 0.96→0.6、`hover_out_px` 语义改为「远离交互区域的收起距离」且默认 120→200。旧值已保存的配置保留用户值。
- **日程新增「刷视频」小事件（B2）**：滚动日程的低优先级可中断娱乐事件（与既有随机小事件同池、同去重与日上限，无新增定时器）。选中后经跨插件公共接口调用独立观看插件 `cateye.bili-watch`（需另行安装）：从其待看列表选一个未看过的视频真实观看，结果与观后感记入活动关联；无候选、观看能力不可用、队列满、被硬约定抢占时都如实标记，不影响其他日程。观看完成**不会**自动向任何群或私聊发消息。
- **解耦 D1（内部结构，行为不变）**：情绪读睡眠、陪伴读情绪改为装配期显式注入的最小契约端口（core/contracts.py）；日程跳过集合转公开常量。配套退役键与配置计数已在门禁重钉（259/5/254）。
- **版本与部署边界**：插件版本与 `[plugin] config_version` 维持 **3.0.0** 不变；本批为本地 Git 检查点（源码仓 d53eb01/8702e3f/72bdf51/0052ca2、测试仓 4e2d367/3ffca92/a3e335d、观看插件仓 cad3d2d），**未部署、未 push/tag**；真实宿主加载、真机触摸/键盘与自然夜行为待生产验证。

## 验收补修 - 2026-10-30（3.0.0，18:03 已部署生产开发版，未发布）

- 睡眠锚点不再作为唤醒自己的硬约定或普通活动候选；名称兼容的“睡觉”条目同样排除，真实工作约定仍按提前量唤醒。情绪层尊重引擎明确已醒的状态，空闲时也不会因仍处夜窗而重新冻结、调用裁判或恢复情绪。
- 全屏详情在刷新或宿主快捷键导致焦点离开弹窗后，Tab / Shift+Tab 会将焦点带回详情；负 tabindex 元素不进入循环。
- 原有错误睡眠记录和已入账睡眠债保留，不修改历史或模拟新自然夜。

## 未发布增量 - 2026-10-30（多屏截图适配插入轮；18:03 已随验收修复部署，未发布）

- **屏幕感知支持多屏（新配置 `[patrol] screen_scope`）**：`main`（默认）＝只看主屏，与旧版行为逐字一致；`all`＝对方有多块屏幕时逐屏抓取，全部截图按屏序一次交给视觉模型分屏概述，再合成一句旁白。需要对端 cateye 统一连接插件 v1.1.0+；单屏客户端、对端过旧（无 screens 数据）或负载超限自动降级时，两种取值等价（自动退化为只看主屏）。
- **旁白仍是一句话**：巡检触发前看一眼屏幕的注入机制、`[prompts]` 模板与消耗账本均不变；多屏时视觉模型按屏说明后整体概括，仍走一次 VLM 调用，不增加 `screen_timeout_seconds` 预算压力与调用次数。
- **降级安全**：多屏截图某屏图像为空（对端降级）时跳过该屏；视觉模型不支持多图时描述失败，旁白按「没看清 TA 在干什么」处理，可切回 `main`。
- **版本与部署边界**：插件版本与 `[plugin] config_version` 维持 **3.0.0** 不变；本批为本地 Git 检查点（源码仓 8c1ac8e、测试仓 b27fb88），已于 2026-10-30 18:03 随源码 5a1a5e5 部署开发版，尚未发布。

## 未发布增量 - 2026-10-30（互动全屏改造；18:03 已随验收修复部署，未发布）

- **常驻麦麦**：页面左下角新增 45° 冒头的常驻麦麦互动角色（原 hero 固定鱼移除）。鼠标移入沿对角跳出、按住压扁、松开回弹一次，可用键盘（Tab 聚焦 + Enter/Space）触发；打开详情时隐藏、关闭后恢复。`prefers-reduced-motion` 下全部动画关闭。
- **全屏详情**：各板块详情入口移至卡片左下角，点击/Enter/Space 显式打开全屏详情面板（整页背景高斯模糊，不支持时回退高不透明遮罩；焦点陷阱、关闭后焦点回到触发入口；Esc/点击背景/关闭按钮关闭；打开期间锁定页面滚动）。旧的桌面悬停临时查看/点击固定小浮层与手机底部详情卡整体移除；hover 或聚焦不再自动打开详情，再点同一入口等于关闭。打开期间 30 秒只读刷新照常进行，不重置滚动位置。
- **最近变化**：详情内新增「最近变化」折叠组——由前端从历史采样（排除估算点）、活动边界（`state_before/after`，标注因果）与睡眠会话（醒来结算）派生最近真实变化；条数与阈值由配置下发；无历史的板块明确显示「暂无对应历史记录」。
- **日程纵向时间线**：「日程」栏由横向双轨改为纵向列表，计划（虚线轴点）与实际（实线轴点）同列展示、徽标区分；中断红虚线、跨午夜条目续到次日、进行中的活动以当前时刻收尾；状态变化行带百分比。
- **文案清理与整数计数**：「小鱼」统一改称「麦麦」；移除只读声明、免责/隐私类冗余文案；「今日主动」等计数统一整数口径（非法/缺失显示 `—`，不四舍五入）。
- **新配置段 `[maimai_interaction]`（16 键）**：常驻麦麦与全屏详情的展示/交互参数（开关、尺寸、冒头角度、跳出距离/时长、按压缩放、回弹周期/缓动、背景模糊、详情动画、最近变化条数与阈值等），聚合模型最后一段（WebUI 段序末尾），显示名暂为「？？？」占位。
- **`life_dashboard` 新增 `maimai` 只读投影**：`[maimai_interaction]` 经白名单 + 范围钳制下发给前端（≤1KiB），不下发整份配置；配置段缺失时按出厂默认出全量视图。
- **版本与部署边界**：插件版本与 `[plugin] config_version` 维持 **3.0.0** 不变；本批为本地 Git 检查点（源码仓 a4f008c、测试仓 db4f8d4），已于 2026-10-30 18:03 随源码 5a1a5e5 部署开发版，尚未发布。

## 未发布增量 - 2026-10-29（生产验收中）

- 生产移动集成修复：custom页面采用宿主正式支持的viewport布局；底栏与详情卡固定在iframe可视高度内，避开宿主底部导航和状态浮条，新增真实SDK与宿主遮挡层组合回归。
- 生产验收补修：宿主日志混用UTC ISO与无年份本地时间，运行说明统一导出带时区ISO时间，跨年取实际最近年份，坏时间不猜测。
- 生产验收补修按日视图字段契约：前端改读后端真实的 footprint.episodes，兼容旧导入的items；新增真实Python投影→前端完整链测试，避免示例数据掩盖字段不一致。
- 修复五分钟采样与短热重载合并成补眠的边界：正常卸载另存在线检查点，离线只从该点计算；未积分的运行段按清醒漂移保留，睡眠引擎认领时两段均不重复结算。旧无检查点文件保留兼容估算；离线会话在页面明确标为估算。

- 新增统一探头鱼详情：10个板块复用单个浮层，桌面悬停/局部模糊/点击固定、Esc与键盘操作；手机底部详情卡，官方鱼随标题保留关闭入口；历史默认折叠，刷新保留固定、展开与说明焦点，精确时间遵守数据时区。
- 2026-10-29 新决策：所有插件侧输出长度与每日Token限制退役，旧配置安全忽略，宿主任务配置为唯一长度限制；事件库只累计实际usage，不预占或猜测。
- 统一 Token 统计：每个宿主响应均计调用（含失败与模型回退），缺完整usage不推算；PNG/WebUI四类与解释同步，旧辅助比较工具从插件包退役。
- 指定原图等比例导出256/512 PNG/WebP并接入local图标；不裁切、不重绘。

- 工具主动私聊提示优化：明确固定配置收件人、群友身份与话题不可混用、冷场不单独构成私聊理由、正文需独立可理解；避免无依据推断忙闲/作息和罐头连问。直接传message时真正生效的是工具schema说明；留空自动生成的默认prompt同步优化，自定义模板不覆盖。仅提示约束，无新增发送门禁、预算变更或模型调用。专项5/5及门禁231通过，已随本轮部署。

- 离线重连分级补眠：宿主/插件停止后重连，按原始离线时长分 grace/nap/medium/long/overlong；短睡/中睡不改睡眠债，长睡按整晚规则结算；离线段记为“从未被打断”的 completed 睡眠会话，写 `wake_reason=offline_reconnect`，并生成 `source=offline` 的已完成睡眠足迹；只发一次 retroactive `life.sleep_woke`，不补发 `sleep_started`。
- 按日历史 API：`life_dashboard` 新增 `day` 参数（`webui.json` 已声明），可读取指定日的实际足迹/计划意图，带上限与 512 KiB 裁剪；旧 `hours` 路径保持兼容。
- 缺口估算：≤2h 的中间真实缺口可线性补 `estimated:true` 点；>2h 大缺口改由 completed 睡眠会话生成 `estimated_segments`；估算只在投影层，不写回真实历史，不计入真实观察点。
- WebUI 已完成五栏目、睡眠合并、9 张背景与原版官方鱼、按日历史、用量四桶、估算虚线/离线斜纹、活动边界/快照时刻标记、计划与实际双时间轴、手机底栏/触摸缩放与降低动态效果。
- 修复页面挂住仍显示实时：8 秒超时、旧请求隔离、独立陈旧计时与可重试刷新；修复慢快照导入覆盖后选快照的竞态。
- 修复重启后连续睡眠计数归零：持久化段起点与进行中的打断；旧会话兼容、未知版本保留。
- 成功睡眠裁判回执持久化恢复；VAD、回执消费与已结算标记同文件原子写，保存失败回滚后重试，避免同周期重启重算与重复应用。
- 可重建源码及已验收素材随包保存；`python tools/build_webui.py` 输出约 0.94 MiB 自包含页面，小于宿主 2 MiB 上限。
- 验证追加：时钟 6/6、裁判回执 10/10、素材/构建 2/2；四套离线前端脚本及真实宿主 schema/SDK/CSP 验证通过；最终 smoke/round/behavior 231 全绿。
- 验证：`_phase2_offline_sleep_test` 16/16、`_phase2_dashboard_day_usage_test` 19/19；smoke/round/behavior 均 exit 0（behavior 231 段）。

## 2026-10-29 诊断与睡眠展示修正（待自然醒验收后部署）

- 宿主模型/数据库模块错误即使提及调用方插件名，也归入宿主/其他来源，避免把宿主用量写入失败误计为本插件故障。仅修正只读诊断计数，不修改数据库、模型请求或睡眠行为。

- 睡眠活动等待引擎建立会话时显示准备入睡，已入睡时提示稍后再聊；裁判计数按实际语义显示连续失败次数，成功后归零不再被误读为本进程零次调用。

## 2026-10-28 已确认计划执行（开发版，未发布）

- WebUI 加入情绪 VAD、睡眠裁判状态、空间登录健康/回执/草稿预览、模型用量和分来源错误计数。只读投影不含聊天、提示词、凭据或原始日志；不通过网页发布。
- VAD 真值从保存状态时开始采样：5 分钟节流、7 天/2304 点、有界且原子落盘，缺口不回填；默认睡眠裁判改为 utils，显式配置保持优先，新增最近尝试/成功/失败状态。
- 空间发布新增持久回执、24 小时同内容幂等、串行发送、最多两次明确未发出的有限重试；超时/无 tid 的结果待核对，不盲目换通道。cookie 获取失败负缓存 60 秒，区分未获取/未登录/过期/适配器/网络/缓存状态。
- 生产配置、主动消息/空间预算与宿主保持原样；新增纯本地草稿预览不调用模型。

## 未发布增量 - 2026-10-28（生活面板与观察历史）

- 面板名称改为「属于麦麦的生活」，指标显示当前状态而非固定方向说明；趋势图对齐首页请求趋势的曲线、坐标、方块图例与拖选缩放。
- 修复滚动评分把精力/专注 `0` 当成缺失并替换为 `1`；低专注、疲劳或高压力时提供有冷却的真实休息候选，不直接改状态数值。修复活动画像 `social_rate` 到社交电量的字段映射。
- 快照导出使用可全选复制的 JSON，兼容宿主 `allow-scripts` 沙箱，不依赖被宿主禁止的文件下载或剪贴板权限。
- 真机增量修复：已醒睡眠会话在 `persist()` 时不再把活动睡眠字段写回 LifeState；热重载保留本周期已醒状态。对旧版本已造成的“已结算标记 + 重开的会话行”按持久醒沿及已完成 sleep episode 修复关闭事实，不再次结算睡眠债、不重新恢复精力、不重复发布醒沿。
- 插件自带 WebUI v2 状态面板接入：修正页面 id/mode 与能力声明，增加私有只读 `life_dashboard` API，宿主静态目录不变。
- 重做浅暗主题、响应式布局、五项状态趋势、睡眠债与恢复、实际足迹和日意图区分、陪伴关系与预算/问候记录。可查看采样明细、导出/导入快照；无内嵌生产历史，无外部资源依赖。
- 增加独立有界生活历史：每 5 分钟观察与活动/醒沿边界，7 天 / 2304 点，原子落盘；损坏先备份，未知版本拒绝覆盖，失败隔离，重启不伪造缺失点。新增 `life.state_changed` 决策留痕，不改变状态积分、睡眠结算、预算或消息触发。
- API 字段白名单、字符串截断和编码后字节预算；时间回拨、陈旧快照、断线、关闭与未就绪分别呈现。新 API 使注册面变为 30 组件（8 Command / 10 Hook / 7 Tool / 5 API）。
- 验证、部署与未完成边界见外置开发文档本轮工作报告；版本仍为未发布的 3.0.0。

## [3.0.0] - 2026-10-27（开发版，未发布；破坏性清理）

> PR8 破坏性清理：滚动化/生活状态/睡眠引擎收成**唯一主线**，旧全天日表生成路线与灰度开关整体退役。用户 2026-10-27 明确授权直接清理，不等灰度与观察期。**本版未发布，未做真机/真实模型/真实 QQ 验证**；离线验收证据见本节末尾。

### 破坏性变更（升级前必读）

- **唯一日程总开关 `[schedule] enabled`（默认 `true`）**：开启时同时装配 spine 日意图、滚动运行时、生活状态与睡眠引擎；关闭时日程整体停止（不生成、不注入、不主动开口，工具/命令回「日程未启用」），**不再回落到旧全天生成 fallback**。
- **旧开关退役（一次性 pop，只丢弃不搬运）**：`[schedule] spine_mode`、`[life_state] enabled`、`[life_state] sleep_engine_enabled`、`[life_state] profile_path`（死键）、`[schedule] planning_daily_token_budget`、`[planning_proactive] nap_reminder_enabled` / `nap_reminder_max_per_day`。
- **显式 `[schedule] enabled=false` 不被旧 true 覆盖**：3.0.0 只保证「显式 `false` 保持关闭、旧开关 `true` 不会自动反推开启」；一次性告警日志只在部分校验路径出现，并非所有 raw/normalize 路径的保证。
- **删除的旧生成链**：旧 daily 生成、校验（validator）、质量评分（quality）、响应解析（response-parser）；spine 生成 + 模板兜底是唯一路线。
- **删除的旧入口**：工具别名 `lbm_update_schedule_v4`、赖床工具 `lbm_nap_lie_in`、独立命令 `/lbm_emotion_init`（统一并入 `/lbm_init emotion`）、`lbm_get_schedule_info` 的 `lover_day` / `goals` 旧视图、按活动切换强制主动开口的旧链与相关提示词/工具扩展。保留 `schedule_inject` / `schedule_inject_replyer` / `lbm_planning` 等**稳定 SDK 注册名**（名字带 v4 不代表旧路线）。
- **仍独立、按需开启**：`[life_state] emotion_modulation_enabled`、`[event_catalog] mode` 与 `[schedule] no_sleep_mode`、`[patrol] nap_quiet_enabled`、夜间打扰统计、睡眠大修、各类上限全部保留。午休静默只读**新 spine 意图**用于静默，不伪造当前活动。
- **数据兼容**：`CURRENT_LAYOUT_VERSION` 保持 `2026-10-10`（3.0.0 迁移是纯 pop，不换布局标记，避免存量已迁移用户重跑旧白名单搬运）；`schedule.db` v6 与历史日表行、`life_state.json`、情绪/好感/空间状态**不删除、不改写**。旧日表行不再作为当前活动来源；升级当天若目标日只有旧日表，调度会重新生成 spine 日意图（旧行归档，非破坏性）。

### 口径统一（3.0.0 起）

- **当前活动唯一真源 = 当前滚动 runtime**：public `companion_get_current_activity` 在无活动/日程关闭时返回空串；结构化 API 返回 `has_activity=False` 与空列表；`/lbm_plan list`、注入、QQ 空间、巡检、`/lbm_life rolling` 同源。错误/未就绪不得冒充确定清醒。
- **睡眠会话**：正式起床结束会话、记 completed、**不进恢复池**；固定时间硬约定**不因晚起顺延，迟到如实记录**；睡眠中热关日程以关闭真实时刻结束会话并交回停止状态；**重新打开不复活旧会话**。诊断只读 `[schedule] enabled`，文案统一「日程未启用」。
- **VAD → 生活反馈预算**：最终入账增量上限 0.15、原子区写、逐轴净累计保留；升级当天旧 raw 记录当日 hold（暂停新增、旧账保留、不重置额度），次日进入新口径；`per_reply` 预占失败即释放。
- **事件库**：生成上限 8、总池 12（`max_generated_events` / `max_pool_events`；`max_events` 仅旧键映射）；情绪-生活调制表路径固定 `mappings/emotion_life_modulation.json`。

### 真机验证与热修（2026-10-28，开发工作树未提交）

- **真机首轮实弹（用户服务器 + 真实 QQ 群 392653174）**：`/lbm_life help|status|rolling sleep`、`/lbm_plan list`（日程图当场重渲染）、`/lbm_diag life`、`/lbm_emotion_config`（锚点/VAD/当日情绪事件 25 条）、`/lbm_dashboard`（总览图当场重渲染）、@ 自然消息（71.7s 后 3 条人设回复）**全部回复正常**；服务器配置迁移契约逐项核对通过（`[schedule].enabled=true`、退役键全 0 命中、`config_version=3.0.0`、`layout_version=2026-10-10`、admin/target 配置正确）；宿主热重载部署（file_watcher → Supervisor 重启完成，loaded=25 failed=0）实弹走通。
- **修复：`/lbm_life` 帮助行 rolling 子命令清单缺 `sleep`**（真机实测帮助文本与 USAGE 六子命令口径不一致；`qzone/components.py` 帮助串与 `companion/components.py` 文档串补齐，behavior 211 断言同批更新）。
- **修复：跨午夜 `planned_end` 的过期 episode 永不关闭**——`planned_end_minute>1440`（次日口径）的 active 行，在次日 tick 的裸 `cur` 比较下永假，步 2 不收尾 → 步 3 睡眠锚点（要求无当前 episode）与步 5 选活动被无限阻塞（真机实测：前日 23:42 的待机 episode 卡 391 分钟、当日 episode=0、每日 06:00 定时重启不解——DB active 行即它）。修法：步 2 关闭比较改用 `_episode_frame_minute`（episode 自身日帧，与 schedule_items 同口径），`actual_end`=计划末端不倒填现在；同帧（当日）到期行为逐字不变。新增 behavior 用例 231（红→绿）。
- **部署适配（真机实测，非代码缺陷）**：`[llm].emotion_referee_task_name` 默认跟随主任务；**宿主主任务为思考型模型时，睡眠裁判会思考耗尽 `max_tokens` 导致正文为空、连续 3 次失败后本周期大修放弃**（2026-10-28 服务器实测 06:30~06:50 三败）。此类宿主应在插件配置把该键改绑便宜非思考任务（如 `utils`）；字段说明已补陷阱提示。
- **回滚点**：服务器 `MaiBot\pr8b_hotfix_backup_20261028\`（3 个被替换原文件）+ 本地 `work\lbm-phase2\pr8b-rollback-snapshot\`；回滚 = 用备份覆盖 3 文件后 touch 触发热重载。

### 验收状态

- **离线验收（已完成，34/34 rc=0）**：29 个 `_phase2_*` 专项 263 cases + 新增 public chain 5 cases + 独立复核 8 cases = **276 专项 cases**，全部 rc=0；`smoke_test_cateye_test.py` 与 `_config_round_checks.py` 在 f1，`behavior_test_cateye_test.py` 最终 rc=0（f7d，230 段；f7/f7b/f7c 三次历史红保留）。29 专项与三件套证据在 `pr8-final-f1..f6.json`+`pr8-final-f8.json`（33 条），行为在 `pr8-final-f7d.json`（1 条）。原 F1/F3 三红已修复转绿，F2 改 `_runtime_asleep` 只读真实态、未知 fail-closed；保护红例完整性见 `pr8-coordinator-review.json`（私有调用名/解释字符串变化，断言不变）。**剩余仅为真实环境验证**（真机/真实模型/QQ/跨进程/真实计费）；2.2.0 的 263 用例绿仍属历史证据。**2026-10-28 真机修复轮复跑（维护树 `CATEYE_TEST_DIR`）：smoke / round / behavior 全 rc=0，behavior 231 段（新增跨午夜用例 231）**。

## [2.2.0] - 2026-10-27（开发版，未发布）

> 日程系统二期（PR1~PR7）离线验收完成条目。**2.2.0 为开发版，未发布**；真机灰度、观察期与 PR8 破坏性清理当时仍延期。
> **历史条目备注**：其中「PR8 破坏性清理延期」已由上方 [3.0.0] 于 2026-10-27 落地；本段保留当时口径，现行配置与开关以 [3.0.0] 与 README 为准。
> 最终离线证据：三件套 exit 0 + 29 个二期专项共 **263 用例** exit 0（含真实双向 2016 拍、旧 7 日、VAD 48、事件独立 9）；配置 13 段/258 字段/5 隐藏/253 可见；组件 32 = 9 命令 + 10 Hook + 9 Tool + 4 API；manifest 能力 17。

### 新增：日程系统二期能力（全部默认关，唯一例外见下）

- **双向闭环（默认关）**：`[life_state].emotion_modulation_enabled` —— 情绪按平滑读数轻微调制精力/饥饿/专注/压力/社交电量的变化速率（gain 夹 0.70~1.30）；活动完成/打断反向产生有界 VAD 增量（睡眠目标双向跳过）。每日 VAD 回写预算 `life_feedback_budget.json` **按最终实际入账增量**检查记账、保留逐轴净累计。
- **睡眠引擎（默认关，须与生活状态同开）**：`[life_state].sleep_engine_enabled` —— 精力阈值（0.75）/最晚起床（复用 `lie_in_max_until`，默认 12:00）/硬约定（至少提前 30 分钟，**固定时间 hard 不因晚起顺延、迟到如实**）/过睡递减；**正式起床结束会话、completed、不进恢复池**；夜扰 <5 分钟只统计、**≥5 分钟才重置连续段**；热关闭以关闭真实时刻结束会话。**只开睡眠开关只提示依赖，不自动开生活状态。**
- **事件库（默认 off）**：`[event_catalog]` —— `mode=off/manual/auto`；生成上限 `max_generated_events=8`、总池 `max_pool_events=12`，旧键 `max_events` 仅作兼容读法；独立任务 `schedule_events`（缺失回退 `utils`）；`daily_token_budget=9000` 保守预占 + 实际用量结算；原子写 + `.bak` 3 份；损坏回退内置池且不覆盖用户文件。
- **统一初始化**：`/lbm_init`（全量）/ `emotion` / `events` / `status` 四用法；shared 已注册四态（入口先 `plugin.enabled` 再管理员）、阶段独立、汇总 `life_init.json`；旧 `/lbm_emotion_init` 已真正委托 `run_emotion(force=True)` 并给迁移提示（PR8 删）。
- **目标频率恒 1（默认开，唯一例外）**：`[planning_proactive].target_frequency_always_one=true` —— 只对解析成功的目标陪伴私聊固定 1.0，其他流仍按日程属性；启动/配置热重载/目标切换自动校准，失败不写缓存。
- **工具 v2**：`lbm_update_schedule` 为规范名，旧名 `lbm_update_schedule_v4` 独立注册别名（同一业务，PR8 删）；`lbm_get_schedule_info` 新增 `next/episodes/spine/intents` 只读视图并保留旧视图；全部零写零模型。
- **睡眠/滚动只读诊断**：`/lbm_life rolling` 增加 `sleep` 子视图（六子命令）；纯读不改变调度/掷点/预算。

### 配置 / 数据 / 映射

- 新增 `[life_state]` 睡眠 7 键（`sleep_engine_enabled`、`wake_energy_threshold`、`sleep_oversleep_start_hours`、`sleep_interrupt_min_minutes`、`sleep_wake_check_seconds`、`sleep_hard_commitment_lead_minutes`、`sleep_oversleep_penalize_focus`）与 `[event_catalog]` 段；`target_frequency_always_one` 默认 true。
- `[event_catalog].max_events` 与 `temperature` **隐藏**：前者仅兼容读法（规范字段优先），后者协议固定 0.9、改配置不生效，均为“保留读取、不提供可编辑旋钮”。
- 调制表路径**固定** `mappings/emotion_life_modulation.json`，**不新增** `emotion_modulation_profile` 路径键。
- `schedule.db` 升 v6（新增 `sleep_sessions`，只增不改）；用户文件 `mappings/generated_events.json`；数据文件 `life_init.json`、`life_feedback_budget.json`；`life_state.json` 损坏/未知版本先备份再内存重建，未知版本拒绝覆盖，**备份失败不得覆盖原坏文件**。
- 升级当天旧 raw 口径 VAD 记录无法可靠换算 actual → **当日 hold（暂停新增生活反馈，旧账保留）**，次日自动进入 actual 新口径；不会重置额度或伪造 actual。

### 兼容 / 回滚 / 边界

- 新功能全部可单开关回滚；关闭后与 2.1.0 行为逐字一致；旧表、旧路径、旧别名保留到 PR8。
- **shared 接线（2026-10-27）**：事件库聚合/生命周期、旧 `max_events` 迁移、频率单飞校准、`/lbm_plan list` runtime 行、`cycle_start` 实际醒来、硬约定删除/改期撤销、`life_state` 备份/未知版本保护均已落码；A-21 属性统一、D13 有界事件、饥饿护栏、真实 wake-cycle 与晨间 hard 不平移均由 PR7 完成。**离线验收：三件套 + 29 专项共 263 用例 exit 0**；映射表 7 张出厂种子在 `mappings/default_mappings/`。
- **不承诺**跨进程/崩溃时两文件事务一致，也不承诺超出宿主 SDK 契约的硬 token 计费上限；事件预算只是请求前保守预占 + 实际结算。
- 真机文件监视热重载、WAL 多进程、宿主事件循环边界唤醒、真实 QQ/模型与真实 usage 计费仍待真机验证（离线闭环已完成）；真机灰度、观察期与 PR8 破坏性清理不在本版。

## [2.0.0] - 2026-10-12

第四轮（遗留清单收口）、19 项历史待裁决落地、5 项新需求与 OOM 热修——逐项变更见下方 [1.1.0] 段内
「第三轮修复 / 第四轮收尾 / 第四轮续批」各条目与计划书 §十六/§十七（1.1.0 未单独发布，随本版一并发布；
配置版本号与插件版本号同步提升至 2.0.0，升级链路「磁盘旧文件抢救 + 一次性迁移器」照常工作）。

### 新增：空间模块扩展（2026-10-14，计划书 §十八四项全落地）

- **带图说说（§18.1）**：Web CGI 通道接通图片上传（`cgi_upload_image`，路线移植自麻省空间 Maizone，MIT）——
  说说可配图发布（`pic_bo` 逗号连接 / `richtype=1` / `richval` tab 连接三个细节齐备才挂得上图）；
  SnowLuma 动作通道沿用既有 `images` 参数（本插件以 `base64://` 形态传入）。
- **配图来源（§18.2/§18.3）**：`[qzone] image_mode` 三选一 + `off`（默认 **off = 与旧行为逐字一致**）：
  `emoji` = 宿主表情包库按说说正文语义匹配（`ctx.emoji.get_by_description`，manifest 能力声明由 16 恢复为 **17 项**——
  A-7 当时以「无消费点」移除，本版配图落地后消费点存在；`[emotion] emoji_enabled` 配置键**维持移除不变**）；
  `library` = 本地图库目录随机取图（`image_library_dir` 绝对路径 + `image_number` 张数上限 1~4）；
  `random` = 两者各半掷骰。**铁律：配图失败永不阻断发布**——单张取不到跳过，全部取不到发纯文本。
- **日程巡检发空间判定链（§18.4）**：自动发帖在既有「每日上限 / 静默 / 活动 / 概率」之后新增判定链
  （移植自 MaiTrace v3.1，MIT）：活动归类（自由文本 → 枚举，失败落 other）→ 无活动不发 → **睡眠硬跳
  （连 LLM 都不问）** → 冷却（`post_cooldown_minutes`，**锚点只在发送成功后前移**、失败不重置）→ 静默复检 +
  活动黑名单（`post_blocked_activities`）→ LLM 严判（`post_llm_gate_enabled`，正反例 + 强烈压制「是」，
  只认回复首字符「是/否」，格式异常一律按否）→ 可选掷骰（`post_max_chance`）→ 发完进入冷却。
- **配图三路同口径（2026-10-14 用户指示）**：自动巡检、手动 `/lbm_life qzone post` 与 `lbm_life_qzone` 工具发帖都按 `[qzone] image_mode` 取随机图库/表情包（跟随 Maizone 行为）；`off` 或取图失败 → 纯文本，配图永不阻断发布。
- **可观测性**：每轮判定（无论卡在哪道闸）写入带上限 20 的判定历史；`/lbm_life qzone decisions [条数]`
  （管理员）查看「为什么没发」；`/lbm_life qzone status` 增加配图模式。
- **配置面**：`[qzone]` 新增 7 键（`image_mode` / `image_library_dir` / `image_number` /
  `post_cooldown_minutes` / `post_blocked_activities` / `post_llm_gate_enabled` / `post_max_chance`）、
  `[prompts]` 新增 1 键（`qzone_post_gate` 发帖严判提示词）；合计 11 段 **220 字段 / 217 可见**（原 212/209）。（**历史数字**：2026-10-17 §19 移除 `[affection] current_level` 后，现行口径为 **219 字段 / 216 可见**，见上方 [2.0.0] 的「好感度 0~200 重构」子节。）
  版本号维持 **2.0.0**（未经用户指示不递增；R6 双侧仍同步）。
- **新增 LLM 调用点**：`qzone_post_gate`（已登记进消耗统计的模块归属，计入「QQ空间」模块的 token 消耗；
  不配 `post_llm_gate_enabled=false` 时每轮自动发帖至多一次严判调用）。

### 变更：好感度 0~200 重构与映射表集中（2026-10-17，计划书 §十九；版本仍 2.0.0）

本轮把好感度整套标度从 0~100 **等比放大到 0~200**、档位结构外置成 json 预设表，并把全部用户可编辑映射表
集中到插件包 `mappings/` 子目录；同时**移除 v1 三档口径**。**旧档案自动迁移，无需手改**；**旧配置里的分值参数不会自动迁移，见下文加粗提示**。

- **分数标度 0~100 → 0~200（等比 ×2，体验等价）**：初始分 `30 → 60`；内置「恋人」档区间
  `(0,20)(20,45)(45,70)(70,88)(88,100) → (0,40)(40,90)(90,140)(140,176)(176,200)`；日累计限幅默认
  `+4.0/−3.0 → +8.0/−6.0`；档位迟滞带默认 `3.0 → 6.0`（上界 `le 30 → le 60`）；规则事件增量表、裁判
  增量区间、周结大事记阈值同批等比 ×2。
- **⚠ 分值参数不自动迁移**：你在配置里**显式改过**的分值参数（`decay_pull` / `baseline_ema` /
  `daily_positive_cap` / `daily_negative_cap` / `tier_hysteresis` / `tier_min_dwell_hours`）**保持你的字面值不变**
  （插件无从判断你的意图）。新标度下这些旧字面值**偏严**（例如旧的 `tier_hysteresis=3.0` 相当于新标度的 1.5）——
  **想要等价手感请自行按 ×2 调整**；出厂默认值已随本版等比更新，没改过的用户无需任何操作。
- **档位结构外置 + 1~5 档自由构造**：新增 `mappings/affection_presets.json`（预设名 → 档位表：每档
  `label` / `min` / `max`，须**连续无缝覆盖 0~200**，1~5 档）与 `mappings/affection_tier_descriptions.json`
  （按预设分组的档位注入文案）。配置面**不再含档位/区间键**；同名预设按「json 覆盖内置」生效，坏预设**只排除该条**不连坐，
  顶层级错误才整表回退内置。
- **`[patrol] relation_type` 改为动态下拉**：WebUI「关系类型」选项 = 内置五名（恋人／挚友／维护者／家人／自定义）
  ∪ `mappings/affection_presets.json` 里的合法预设名；在 json 里新增预设后，**重启或热重载**下拉即多出该项
  （内置五名恒在，默认值「恋人」永不非法）。删掉某预设后若配置仍选着它 → 回退内置「恋人」并在下拉中消失。
- **v1 三档口径移除（行为变化）**：
  - 配置键 **`[affection] current_level` 删除**（旧值由一次性迁移器显式丢弃并登记）；`[affection]` 段 13 → **12 键**，
    全插件可见配置项 217 → **216**（字段 220 → 219，隐藏仍 3）。
  - Tool `lbm_mai_lover_affection` 参数 `level`（0/1/2）→ **`tier`（1~N，1 起计数）**；命令
    `/lbm_mai_affection [0|1|2]` → **`/lbm_mai_affection <1~N>`**；越界返回当前预设的档位清单错误提示。
  - 跨插件 API `companion_get_affection_level` **保留组件（组件数 29 不变）**，返回值由兼容档 `0/1/2` 改为
    **当前档位索引（0 起计数，0..N−1）**。
  - `[affection] enabled=false` 语义由「回退 v1 三档静态行为」改为「**好感度完全停用**」（不记账、不衰减、不重估；
    档案保留，可随时再开）。
- **映射表集中到 `mappings/`**：`emotion_mapping.json` / `emotion_descriptions.json` 从插件根目录移入
  `mappings/`，与新增的两张好感度表同处；**旧根文件在新位置缺失时自动复制**进来（只复制、不删旧文件）。
  四张表**只在插件加载 / 热重载时读一遍**，改完须重启或热重载。随包新增 **`mappings/README.md`**（四张表的结构、
  校验规则、错误回退粒度与示例；属功能资产说明文档，随插件包分发）。
- **档案 `version=3` 自动迁移**：`version:2` 档案的 `score` / `baseline` **×2**、删除 `affection_level` 回滚镜像键；
  v1（无 `version` 无 `score`）档案按新表 `{0:50, 1:110, 2:160}` 迁移（旧表 `{0:25,1:55,2:80}` 的等比位）；
  未知字段保留。迁移幂等，升级不丢数据。
- **门禁**：behavior 编号至 **179**（175 预设校验 / 176 装载路径 / 177 非 5 档全链路 / 178 描述注入面 / 179 病态输入）；
  组件 29、manifest 能力 **17 项**均不变；插件版本与配置版本维持 **2.0.0**（R6 双侧同步不动）。

## [2.1.0] - 2026-10-21

2026-10-20 四批变更的归档发布版：**随图变动日志扩容与情绪三维变动标注** / **静默窗复用作息、配置段重排与配图默认 emoji** / **发帖不再按活动类型拦截与午休静默跨模块** / **主动消息安全治理**。本版同时按 R6 把**配置版本号与插件版本号同步提升至 2.1.0**（2026-10-21 用户裁定；上述四批变更原记于 [2.0.0] 段内、当时标注「版本仍 2.0.0」，现整体归入本版，正文按当时口径保留）。另有 **2026-10-21 第五批「真实作息同步与日程属性映射」（版本不变）并入本版**，以及 **同日用户追加三直派「diag streams 名称显示与合并转发 + 其他私聊门槛窗口扩到凌晨」（版本不变）并入本版**，见本段各后继子节（2026-10-21 第五批直派、三直派、2026-10-22 各批直派与分支闸门重构、2026-10-23 三直派等，逐节按时间序登记）。

- **README 面向用户重写 + 新增致谢章节（不含行为变化）**：README 改为面向使用者的结构（安装、快速上手、四模块、配置速查、映射表、升级注意、常见问题、许可与致谢、更新历程）；新增「参考与致谢」章节，逐项列出来源、许可与用途。代码、配置、命令与行为均未变化。

### 变更：随图变动日志扩容与情绪三维变动标注（2026-10-20，用户口径；版本仍 2.0.0）

`/lbm_dashboard` 面板图后跟随的「日程与情绪变动」日志文字本轮改造：

- **窗口与容量扩容**：最近 **24 小时 → 48 小时**、日志上限 **98 → 500 条**；输出由单条文字档改为
  **分片发送**——每条消息最多 **50 条日志**、单次最多发送 **10 条消息**（=500 条）。标题行并入首片
  （不计入 50 条）；超出单次发送上限的更早日志在末片尾注注明条数。合并转发失败回退普通发送时逐片照发。
- **情绪更新行标注三维变动**：每条「情绪更新」日志精确标注本次相对上一次的三维变动，形如
  「情绪更新：情绪基调 期待(35%)…（愉悦度 +0.10｜唤醒度 -0.05）」（变动不足 0.01 的维度不列；
  三维 = 愉悦度/唤醒度/支配感，与情绪低分辨率描述用词一致）。
- **重复行消除（缺陷修复）**：此前情绪状态在两次更新间未跨档时，低分辨率文案逐字相同，
  随图日志会连续出现多条一模一样的「情绪更新」行（如 01:01 与 01:21 两条）。现在**三维全部无变动的
  更新不再重复展示**（标题行计数「无变动合并 N 条」）；升级前旧记录的记录没有三维数值，
  退化为「文案与上一条情绪更新完全相同 → 合并」。
- **注入链同轮重复日志修复（2026-10-20 服务器日志取证发现）**：`planning/inject_service.py` 自基线起
  同一轮调用 `should_continue_inject` 两次，第二次在同一时刻必然返回与第一次相同的结果、只多打一条
  debug 日志（服务器日志中 2320 对逐字重复的「检测到连续日程话题…」，本插件日志的最大宗重复）。
  已删除该冗余调用，注入行为零变化。
- **无配置项变化**：本项纯日志展示改造，不加键不改默认值；48h 窗口要求决策日志保留 ≥2 天
  （`[admin] 日志保留天数` 默认 3，够用，无需调整）。门禁 behavior 编号至 **182**。

### 变更：静默窗复用作息、配置段重排与配图默认 emoji（2026-10-20，用户口径；版本仍 2.0.0）

这一轮把「静默时段」彻底交给日程作息，顺带整理了 WebUI 的配置段顺序，并把说说配图的出厂默认从关改为 emoji。

- **静默窗恒复用日程作息（行为变化）**：`[patrol] silence_start` / `silence_end` 与 `[qzone] silence_start` / `silence_end`
  四个键已删除——陪伴侧巡检与空间侧发帖／评论监听的静默时段，现在**一律等于 `[schedule]` 的入睡~起床**
  （支持跨午夜作息），要调整静默只需改 `[schedule]` 的起床／入睡锚点。由此带来两点行为变化：
  ① 原先「留空跟随作息、非空自定义覆盖」的覆盖能力整体移除，静默窗不再能单独配置；
  ② 晚安的期限**恒为入睡锚点**（原「静默起点提前则期限提前」的行为移除），问候本身仍不受静默拦截。
  旧配置里显式写过的静默键由一次性迁移器**显式丢弃并登记**（不是配置出错，是这两项已不存在）；
  作息值非法时仍按「宁可不发」fail-safe。配置面随之缩小：全插件 **219 → 215 字段、216 → 212 可见**，
  `[patrol]` 24 → **22 键**、`[qzone]` 23 → **21 键**（`[emotion]` 17 键不变）。
- **配置段 WebUI 重排**：新段序（从前到后）为
  `plugin → patrol → affection → schedule → schedule_advanced → planning_proactive → qzone → emotion → llm → prompts → admin`
  ——`[qzone]`、`[emotion]`、`[llm]`、`[prompts]`、`[admin]` 依次前移到第 6~10 位。**这只是界面分组顺序变了**，
  段名、键名、默认值与迁移逻辑都不受影响，你的旧配置照常自动搬运，不用做任何事。
- **`[qzone] image_mode` 出厂默认 `off` → `emoji`**：新装或没写过该键的配置，说说会自动配一张宿主表情包
  （按说说正文语义匹配）。**⚠ 注意**：旧版本的出厂值 `off` 可能已被宿主落盘成你的显式值——这类升级用户
  **不会**自动吃到新默认，需要你手动把 `image_mode` 改成 `emoji`；本来就显式写过该键的用户（`off` / `emoji` /
  `library` / `random` 任一）保持自己的值不变。
- **门禁与用例**：用例 156 / 119 / 120 / 171 已同步，smoke 与 round_checks 的静默检查按「恒复用日程作息」口径
  重写（smoke 另新增「`image_mode` 默认 emoji」与配置段序断言），behavior 编号至 **182**。
  插件版本与 `[plugin] config_version` 维持 **2.0.0 不递增**；组件 29、manifest 能力 17 项均不变。

### 变更：发帖不再按活动类型拦截与午休静默跨模块（2026-10-20，用户口径；版本仍 2.0.0）

这一轮把「是否允许发空间」从**活动类型**彻底交给**静默时段 + LLM 严判**，并让午休静默开关跨到空间侧。

- **判定链精简（行为变化）**：`[qzone]` 发帖判定链删除**睡眠硬跳**与**活动黑名单**两道闸——「是否允许发空间」不再按活动类型判定。
  现闸序（成本从低到高）= 静默时段复检（恒复用 `[schedule]` 作息）→ 无活动 → 午休静默（见下）→ 冷却 → LLM 严判 → 掷骰。
  原 `[qzone] post_blocked_activities`（活动黑名单）键**删除**（`[qzone]` 21 → 20 键），旧配置里的显式值由一次性迁移器
  **显式 `pop` 丢弃并登记**（`suite_config.py` 的 `LEGACY_SILENCE_KEYS` qzone 元组），不是配置出错、也不需要手改。
  **行为变化**：夜间睡觉条目天然落在静默窗内；**白天时段的睡眠/学习/运动等条目不再一律不发**，默认由 LLM 严判把关。
  想更省 LLM 调用 / 更保守，可开启 `[patrol] nap_quiet_enabled` 午休静默，并保持作息与睡眠窗重合。
- **午休静默跨模块**：`[patrol] nap_quiet_enabled` 开启（默认关）后，**陪伴巡检与空间发帖／评论监听**在午休/小睡条目
  （名称含 午休/午睡/小憩/打盹，或 `goal_type=rest`）进行中都不动作，空间侧判定历史记 `gate=nap_quiet`。
  判定真源为 `companion/patrol.py` 的 `NAP_NAME_KEYWORDS` + `nap_item_matches`（巡检与空间**同源判定**）；
  该键的配置描述同步扩面为「开启 = 陪伴巡检与空间发帖/评论监听都跳过」。
- **配置面**：全插件 **215 → 214 字段、212 → 211 可见**（隐藏仍 3），`[qzone]` 21 → **20 键**。
- **决策历史与门禁**：`/lbm_life qzone decisions` 数据面结构不变——`activity_type` 记录段保留结构，**新记录不再填充**
  （`qzone/decision.py` 的 `ActivityType` / `TYPE_MAP` / `classify_activity` / `normalize_blocked_activities` 已删除，
  `parse_llm_decision` 与 `DecisionHistory` 保留）。门禁：behavior 编号 **182**；用例 **171** 按新闸序 + 午休静默分支重写、
  **172** 删除分类器病态段；smoke / round_checks 本轮无静默口径改动并复跑通过。无新增 LLM 调用点（`EVENT_MODULE_MAP` 不动）；
  组件 29、manifest 能力 17 项均不变；插件版本与 `[plugin] config_version` 维持 **2.0.0 不递增**。

### 变更：主动消息安全治理（2026-10-20，用户口径；版本仍 2.0.0）

这一轮把主动消息收紧到「宁不发，不错发」：日程侧的主动开口整条移除，问候的激活闸改为强制且按自然日观测，提示词不再引导提及日程，并给名单与激活观测补了一条可查命令。

- **日程主动行为整条移除（决策链移除）**：不再因「日程活动切换／约定到期」向群聊或非目标私聊主动开口；
  `[planning_proactive] enable_proactive_trigger` 与 `proactive_fresh_window_minutes`（触发窗口）两个配置键**删除**，
  旧值由一次性迁移器**丢弃并登记**。移除后主动消息来源只剩：**早/晚安**、**陪伴巡检的日程巡检分享**（仅陪伴目标私聊）、
  **频率调控**（`enable_frequency_modulation` 保留，只调节聊天频率、不发送消息）。
- **早/晚安激活强制化**：`greeting_require_activation` 配置键**删除**——激活闸**强制开启且不可关闭**：
  **当日 00:00 起**，该聊天流必须有过**非 bot 本人、非命令**的消息，才允许发早安/晚安
  （查询带 `filter_mai=True` 并剔除命令前缀；查询异常按不发处理）。宁不发不错发。
- **提示词去日程引导**：「碎碎念」（无问自答提及当前活动）概率 **0.15 → 0.0 恒关闭**；注入提示改为
  「不要主动提及日程；只有被问起或需要引出新话题才简要提一句」；早安文案去掉「顺口提一句今天的第一个安排」。
  提及兴致／提日程只剩两种情境：**被问起**、**作为话题引子**。
- **新增 `/lbm_diag streams`（管理员）**：输出名单状态（群/私聊名单模式与成员及含义注解、目标 QQ、早晚安开关）
  ＋候选解析结果＋各候选流「最后一条非命令、非 bot 消息时间」（当日 00:00 起查询，宁不发口径）；`/lbm_diag` 帮助文案现为 **5 行**。
- **配置面**：全插件 **198 字段 / 3 隐藏 / 195 可见**（`planning_proactive` 15 → **12 键**、`prompts` 53 键；**11 段不变**）。
- **门禁**：behavior 用例 **182**——135 号用例随日程主动链移除删除、120 源码计数 3→2、117/118/121/124/164 激活口径改为
  当日 00:00 + `filter_mai`、79 增 `streams` 断言；插件仓提交 `765c700`、scratch `26c23e9`；
  组件 29、manifest 能力 17 项不变；插件版本与 `[plugin] config_version` 维持 **2.0.0 不递增**。

### 变更：真实作息同步与日程属性映射（2026-10-21，用户直派；版本不变）

这一轮给「作息」换上真实源：日程生成失败时的模板兜底不再把日程整体压回固定默认区间，静默/夜窗等作息消费点改从当日实际日程取真实入睡/起床锚点，活动频率倍率外置成 `mappings/` 第五张用户可编辑映射表（计划书 §二十四，任务顺序 A→B→C）。**无任何配置项变化，版本维持 2.1.0**。

- **默认区间退化修复（修复；模板兜底锚化作息）**：此前一轮日程 LLM 生成失败／致命错误时，模板兜底日程会**整体硬编码回「22:00 睡觉」等默认区间**——更新插件 + 一轮生成失败 = 日程被整体换成默认作息（与本轮用户报障吻合）。现在模板条目按你 `[schedule]` 的生效作息锚定：起床条从起床锚点起、睡觉条从入睡锚点起（时长 = 睡眠环跨度，钳 3~12 小时），钟点落在睡眠环内的中间填充条目**丢弃**（宁缺勿碎，不再产出 15 分钟碎片），**任何路径不再出现硬编码 22:00 的兜底睡觉段**；作息锚点非法时回退 07:00/23:00（与生成链同源）。**自诊**：宿主日志 grep「使用模板兜底」（或「日程 LLM 生成失败 / 日程 LLM 致命错误」）可断代历史坏日程；已被写成默认区间的当日日程用 `/lbm_plan regenerate` 重建。
- **真实作息锚点接线面（行为变化）**：静默窗（陪伴巡检与空间发帖/评论监听）、晚安期限、早安基准、夜间情绪窗口（冻结/夜间打扰）等原本只读 `[schedule]` 锚点的消费点，改为**优先从当日实际日程解析真实入睡/起床环**——今晨所属睡眠环可能写在昨天的日程里（跨午夜归属），赖床顺延后锚点随移；日程缺失／数据库不可用／解析异常一律回退 `[schedule] wake_time/sleep_time` 配置锚点（现行行为），**绝不静默回退成「可打扰」**。读取即解析、零内存镜像；配置为纯配置对象（测试桩）时走原配置路径不变。
- **第五张映射表 + 频率倍率可调 + plan list 属性标注**：新增 `mappings/schedule_properties.json`（日程属性表）——每条日程按「`goal_type` 精确命中 → 条目名关键词 → 兜底条目」（先到先得）归入一条属性（睡觉/学习/工作/休息等内置条目 + 「空闲」兜底），聊天频率倍率 = 属性的 `reply_factor`（**0~2 可在 json 里自行调整**，改完重启或热重载生效；旧按活动类型的固定倍率数值逐条迁为该表内置默认，`free_time/custom` 缺口随兜底条目补齐）；`/lbm_plan list` 文字与日程图条目行新增 `[属性]` 短标签、日程图条目按属性取色（非法/缺失回退原类型色）。
- **随本轮删除两段旧判定代码**：`proactive_service._in_sleep_window`（零调用死代码，真实化后保留会成为双口径假源）与 `_FREQUENCY_FACTOR_BY_GOAL_TYPE`（频率数值已迁入日程属性表，保留同样双口径）——行为语义不变，倍率以映射表为唯一真源。
- **`mappings/README.md` 新章**：随包说明文档新增第六章「日程属性表」——文件作用、顶层结构与字段表、匹配口径、内置默认数值表、三级粒度校验（顶层级坏整表回退内置／单条目坏仅排除该条并留账／非法颜色只丢色位）、可抄示例。
- **门禁**：behavior 编号至 **188**——新增 **183)** 模板兜底锚化、**184)** 真实锚点 resolver、**185)** 真实锚点消费面、**186)** 属性表装载/校验/病态、**187)** 频率经属性、**188)** 渲染属性标注（R7 依据 = 用户 2026-10-21 直派，计划书 §二十四）；配置面 **198 字段 / 3 隐藏 / 195 可见**、组件 **29**、manifest **17 项**均不变；**零新增配置键、零新增 LLM 调用点**；插件版本与 `[plugin] config_version` 维持 **2.1.0 不递增**（用户未裁定）。

### 变更：diag streams 名称显示 + 合并转发送出 + 其他私聊门槛窗口扩到凌晨（2026-10-21，用户追加直派；版本不变）

用户读完计划书 §二十四 闸门清单后的追加三项指示（计划书 §二十五，登记即执行）：①`/lbm_diag streams` 里的流改**显示名称**而非纯 id；②该诊断改**合并转发**送出；③其他私聊问候门槛统计窗改为「**昨天 0 点 → 该类问候开始处理的时刻**」。**无任何配置项变化，版本维持 2.1.0**。

- **streams 诊断流名称化（行为变化）**：`/lbm_diag streams`（管理员）的候选解析与「最后一条非命令、非 bot 消息」各流展示位改为**显示名称**——群聊 = 群名、私聊 = 对方群名片优先 / 昵称次之（名称单次取自宿主全部聊天流，名称查不到的流回显原 id 兜底）；[名单状态] 的名单 ids 与 `[patrol].target_qq` 行仍是**配置原文**，配置回显不换名。诊断仍纯只读、不发业务消息、不调模型。
- **streams 诊断合并转发送出（行为变化）**：三块内容改**每块一条消息的合并转发**发出（[名单状态] / [候选解析] / [候选流最后一条非命令、非 bot 消息]，原「[口径]」说明行并入候选解析块结尾），不再整段刷屏；适配器不支持合并转发时自动回退纯文本（三块空行相连，与 LLM 日志 / 决策日志同一回退链）。
- **其他私聊问候门槛窗口扩到凌晨（行为变化）**：其他私聊问候的「当日消息数达到门槛」闸（默认至少 5 条）统计窗从 [昨日 00:00, 今日 00:00) 改为 **[昨天 0 点 → 该类问候开始处理的时刻)**——早安评估时，用户**当天凌晨发的消息也计入**；端点在首次评估时定死（当日缓存吸收后续轮次的时钟漂移）。**晚安按同一公式**（当天白天/傍晚消息亦计入）——此条为裁量登记，**用户可否决为保持旧窗**。查询失败 / 能力不可用一律按 0 处理（宁不发），门槛下限与配置约束不变；群聊与目标私聊不涉及此闸。
- **门禁**：behavior 编号至 **190**——新增 **189)** streams 记录化（三块 = 三节点、名称展示与缺失回显、回退文本 = 三块相连）与 **190)** 门槛窗口（凌晨消息计入 + 端点定死缓存吸收漂移 + 查询失败按 0 + 同输入同结果）（R7 依据 = 用户 2026-10-21 追加三直派，计划书 §二十五）；配置面 **198 字段 / 3 隐藏 / 195 可见**、组件 **29**、manifest **17 项**不变；**零新增配置键、零新增 LLM 调用点**；插件版本与 `[plugin] config_version` 维持 **2.1.0 不递增**（用户未裁定）。

### 变更：diag streams 不折叠 + 类别切片 + 私聊独立节点 + 流序号 + 序号查询触发状态（2026-10-22，用户三直派；版本不变）

用户对 `/lbm_diag streams` 追加四项定位（计划书 §二十六，登记即执行）：候选流**不再折叠**（删除「最多 30 条 / 省略 N」截断）、**群聊与私聊分节点**、每个聊天流**带序号**、新增 `streams <序号>` **触发状态子查询**。**无任何配置项变化，版本维持 2.1.0**。

- **streams 展示重构（行为变化）**：候选流节点由单节点改为「群聊」与「私聊（目标/其他）」两族——群聊单列，目标私聊与其他私聊并入私聊节点；每族按枚举序**每 10 个流切一片**，标题带「第 i/N 片」，**无上限、不折叠、不省略**（§25 的候选上限 30 口径由本轮用户改判废止）；无候选流仍单节点「无候选流」；切片仍走合并转发、失败回退纯文本链，诊断保持纯只读。
- **流序号（行为变化）**：每个问候候选流一个 `#NN` 序号（两位补零，≥100 自然进位），候选解析的三类列表行与「候选流最后一条非命令、非 bot 消息」行首同步显示；编号顺序 = 目标私聊 → 群聊（枚举序）→ 其他私聊（枚举序），与候选解析同一帧枚举（**2026-10-22 第二批起改为群聊 → 其他私聊、目标流豁免，见下方 §二十七子节**）；**同一次调用内稳定，跨调用可能因流增减漂移**。频率调控-only 流（不在问候候选）**不编号、不进候选流块**——若需把频率候选也编入请再明示。
- **`streams <序号>` 触发状态子查询（新增）**：`/lbm_diag streams <正整数>` 查该流类别（目标私聊/群聊/其他私聊/仅频率调控/不在问候候选）与下一次早安、晚安是否已满足触发——结论行含「已满足（下一 tick 将发送）/将发送（约 HH:MM）/未满足（差激活或差门槛 M/N，窗内剩 X 分钟）/已放弃（原因）/今日已发/不适用/取不到（宁不发）」，下一行给当前判定关键量；非数字/0/负数/越界 → 提示可用序号范围 `#01~#NN` 与用法。**快照纯读**：不写掷点表、门槛缓存、marks 与解析缓存，不调用真实门槛/掷点/标记方法；单节点经合并转发送出，失败回退纯文本。**门禁**：behavior 编号至 **192**——新增 **191)** 不折叠/切片与序号确定性、**192)** 触发状态结论分档与快照零写副作用（R7 依据 = 用户 2026-10-22 三直派，计划书 §二十六）；配置面 **198 字段 / 3 隐藏 / 195 可见**、组件 **29**、manifest **17 项**不变；**零新增配置键、零新增 LLM 调用点**；插件版本与 `[plugin] config_version` 维持 **2.1.0 不递增**。

### 变更：群聊必要性回归 + 底层硬门槛 + 其他私聊「对方发送」阈值 + diag 目标流豁免与时段标注（2026-10-22，用户第二批四直派；版本不变）

用户看实机 `/lbm_diag streams 34` 读数后按分支重定问候闸门（计划书 §二十七，登记即执行）。本轮**配置面 +1 键**（`[planning_proactive]` 12→13 键；全插件 198→199 字段 / 3 隐藏 / 195→196 可见），**零新增 LLM 调用点**，插件版本与 `[plugin] config_version` 维持 **2.1.0 不递增**（用户未裁定）。

- **群聊必要性配置回归（行为变化）**：`[planning_proactive].greeting_require_activation` 恢复为真配置键（bool，默认 `true`，描述 = 群聊早晚安必要性检查）——开启时群聊在问候观察窗内无任何非 bot、非命令消息则跳过该类问候；**只作用于群聊**，目标私聊维持激活强制，其他私聊改由阈值判定。迁移面：该键已从 `LEGACY_PROACTIVE_KEYS` 元组移除，迁移器不再 pop，旧配置中的显式值自本版起**保留生效**。**重要提示**：该键在 2026-10-20 主动消息治理轮曾被迁移器 pop 过一次——当时已被 pop 的旧配置不会自动恢复显式值，**旧用户请自行在 WebUI 核对并按需设置**（默认 `true` 即必要性开启）。
- **群聊底层硬门槛（无条件）**：该群**今日（本地 00:00 起）没有任何非 bot、非命令消息 → 不进入问候候选**，与必要性开关无关；首评实查一次并按 `(session_id, date)` 缓存一日一查，查询异常按 fail-closed 不入候选；必要性开启且门槛已证明“今日有人说话”时复用该判定，免去重复激活查询。
- **其他私聊阈值改「对方发送」并放宽下限（行为变化）**：统计窗仍为「昨日 00:00 → 该类问候开始处理的时刻」，查询加 `filter_mai=True`——**bot 自身消息不再计入**（命令消息按对方普通输入计入，裁量可再议）；门槛配置 `greeting_other_private_min_messages` 由 `ge=5` 改为 **`ge=1`**（默认 5 不变；0/负数在该字段处直接拒绝，运行期兜底由 `max(5, …)` 改为 `max(1, …)`）。其他私聊不再经过激活闸，早晚两向同阈值，缓存键仍按 `(stream_id, date, kind)` 分族。
- **diag streams 四改（行为变化）**：①**目标陪伴聊天流整体豁免**——候选解析块删除目标行、候选流切片与 `#NN` 序号均不含目标流（编号顺序 = 群聊 → 其他私聊）；②每行「今日无消息」改为 **「历史无消息」**；③**检查窗分族**——群聊片 = 今日 00:00 → now，私聊片 = 昨日 00:00 → now，切片标题**注明时段（精确到年月日时分）**；④`streams <序号>` 关键量行同步——其他私聊显示「阈值 M/N（对方发送）」且不再显示激活行，群聊补「必要性 = 开/关」一行，结论矩阵不变。
- **门禁**：behavior 编号至 **194**——新增 **193)** 群聊必要性 ON/OFF + 底层硬门槛（zero-nonbot/bot-only/查询异常 fail-closed、缓存一日一查、目标私聊不受开关影响）与 **194)** 其他私聊「对方发送」阈值（对方 5 条 + bot 10 条 → 计 5、0/负数拒绝、显式 1 生效、晚安同口径）（R7 依据 = 用户 2026-10-22 第二批四直派，计划书 §二十七）；既有 117/118/121/124/164/79/189/190/191/192 同批同步。配置面 **199 字段 / 3 隐藏 / 196 可见**、组件 **29**、manifest **17 项**不变。

### 变更：diag streams 全量逐行显示（2026-10-22，用户第三批直派；版本不变）

用户报「调试命令序号错乱」并要求无论群聊还是私聊，每个聊天流都以回车分行显示（计划书 §二十八，登记即执行）。日志判读成因：`/lbm_diag streams` 输出中候选解析的候选列表与名单 ids 以 `['#01 …', '#02 …']` 形态挤成单行，QQ 端自动硬折行后序号视觉错乱。本轮**零配置项变化、零新增 LLM 调用点**，插件版本与 `[plugin] config_version` 维持 **2.1.0 不递增**。

- **候选解析逐行（行为变化）**：「问候·群聊」/「问候·其他私聊」/「频率调控（群聊∪私聊）」三个列表行改为「标题行 + 每流一行」——每个候选流独占一行（两空格缩进 + 行首 `#NN` 序号，枚举序与切片/序号一致），频控行为纯显示名、无序号；空列表时标题行内联「无」（唯一单行情形）；全部回显**去引号**，不再出现 `['…']` 列表形态。
- **名单状态逐行（行为变化）**：「群聊名单」「私聊名单」各拆「标题行（名单模式 + 留空语义）+ 每个配置 id 一行」（ids 配置原文回显）；空名单保留单行、标题行内联「（空）」。
- **不动的部分**：「候选流最后一条非命令、非 bot 消息」两族切片节点本就每流一行（每 10 流切一片、检查窗分族与时段标注均沿用 §二十七）；`streams <序号>` 子查询只覆盖单节点与序号索引面（本就每流一行），断言零改动。
- **门禁**：behavior 编号至 **194**（用例编号不变——既有 **189)** streams 记录化、**191)** 切片与序号、**192)** 触发状态的受影响断言同步，R7 依据 = 用户 2026-10-22 第三批直派，计划书 §二十八）；配置面 **199 字段 / 3 隐藏 / 196 可见**、组件 **29**、manifest **17 项**不变。

### 变更：diag streams 序号全集化根治 + 映射表出厂种子目录 + README 标题与升级注意修订（2026-10-23，用户四直派；版本不变）

用户四项直派（计划书 §二十九，登记即执行）：「候选列表序号错位（显示 #03 为私聊、按 3 查却是群聊）并推测静默群被丢弃」的根治、README 标题修订、升级注意事项移除、映射表目录加深一层「出厂种子」。**无任何配置项变化，零新增 LLM 调用点，插件版本与 `[plugin] config_version` 维持 2.1.0 不递增。**

- **streams 序号全集化根治（行为变化）**：诊断序号（`#NN`）的枚举基底从「问候候选（今日硬门槛过滤后）」改为**只读名单全集**（群聊生效集 ∪ 其他私聊生效集，只做名单解析、不做今日硬门槛过滤；目标流维持豁免）——**今日无消息的静默群不从候选中丢弃**，仍占序号、行显「历史无消息」，候选解析的群聊行尾加注「（今日无消息·不发晨晚问候）」。主查询与 `streams <序号>` 子查询共用同一全集支路：序号同帧一致、跨调用稳定（只受名单/宿主枚举变化影响，不再随日内外消息量翻转漂移）。
- **诊断全链纯读（行为变化·副作用消除）**：主查询不再走正式候选解析（不再写门槛缓存 `_greeting_group_floor` 与名单告警缓存）——「只读诊断」从小查询扩到全链，不干扰当天问候链的一日一查判定。
- **`streams <序号>` 新结论档**：序号命中的全集群聊若今日无消息，早/晚结论显示「**门槛不过（今日无消息，不发晨晚问候）**」（只读实查一次，判据与群聊硬门槛同源；既有全部结论档保留不变）。
- **映射表出厂种子目录（目录结构变化·表层文件退出随包）**：`mappings/` 加深一层——`mappings/`（表层）= **用户数据区**（文件不随包分发、更新插件不覆盖）、`mappings/出厂映射表/`（深层）= **随包种子源**（出厂 README + 五张默认表随包分发）。表层缺某张表时自动补齐：先看旧的插件根目录版（用户旧资产优先），仍缺才从深层复制出厂默认；**解析错误不算缺失**（表层文件存在但写坏 = 走既有「告警 + 回退内置默认」链，绝不复制/覆盖用户文件）。出厂默认随版本更新进深层，是否合并由用户自行决定。表层五张 `*.json` 与表层 `README.md` 退出仓库跟踪（说明文档迁入深层；fresh checkout 后表层缺表由插件补齐，行为与旧版一致）。
- **README 标题与升级注意修订（文档）**：标题删「（lbm）」括号缩写（命令前缀 `lbm_` 不变）；删「升级注意事项」小节——此前版本未正式发布、无升级路径（第 1/2 条为旧版升级核对项随节移除；第 3 条「`greeting_other_private_min_messages` 最低为 1，填 0 或负数无效」是现行行为约束而非升级提示，保留于配置速查行）；新增「问候诊断（streams）」小节与映射表两层结构说明。
- **门禁**：behavior 编号至 **196**——新增 **195)** 序号根治（全集序号 + 主/子查询同序号系 + 主查询纯读零写 + 新结论档）与 **196)** 映射表出厂种子链（五装载方统一框架）；**189/191 断言同批同步、192 复核零改动**（均就地注明 R7 依据 = 用户 2026-10-23 四直派，计划书 §二十九）；smoke 新增 §29.2 映射面断言（深层种子六件随包可解析 + `.gitignore` 两条清退规则不误伤深层，真 `git check-ignore` 实证）；配置面 **199 字段 / 3 隐藏 / 196 可见**、组件 **29**、manifest **17 项**不变。

### 变更：渲染图重绘重置（2026-10-23，用户直派【按样张重绘指导】；版本不变）

用户按样张「重绘指导.md」直派重绘日程图与状态总览图（计划书 §三十，登记即执行）。本轮只改渲染与展示数据面，**零新增配置键、零新增 LLM 调用点；配置面 199 字段 / 3 隐藏 / 196 可见、组件 29、manifest 17 项、插件版本与 `config_version` 2.1.0 全部不变**。

- **共享渲染工具 `core/render_kit.py`（新增）**：指导 §5.1 色板 tokens（纸/墨/红/弱化/线/底）、`fit_text`（像素宽度二分截断 + `…`，输入预截 2000）、`wrap_text`（像素换行 + 行封顶）、anchor 绘制（`ls/rs/ms`，不再用 `y±2/4/8` 目测偏移）、2× 超采样助手（**仅 1× 高度 ≤2000 开 2×**；4096 按 1× 判断）、逻辑坐标 `Canvas`（绘制后 LANCZOS 回 1×）。两个渲染器共用；禁止 `[:N]` 字符截断。
- **日程图（`/lbm_plan list`，`planning/schedule_image.py`）**：固定列网格 `44 / 172 / 132 / 1fr / 92`（色条固定 x=300、名称 412、状态右缘 1016、出血 48..1032）、行高 `15+58+15+1`；三态（已完成退后 / 进行中 tint+4px 红左线+红点 / 未开始空心环）；跨午夜主行 `22:00–01:00` + 第二行「次日」，00:30 当前判定正确；24h 密度条；命令只在脚注出现一次；0 条空态、1~24 条单页、25~40 条每页 24 条（附页 `schedule_today_p2.jpg`）、>40 条只画前 24 条并在脚注注明「其余见文字版」。
- **状态总览图（`/lbm_dashboard`，`cateye_dashboard.py`）**：左栏 280 KPI 轨（好感 60/700 + track + 基线/今日Δ、情绪词 + 强度 + 粗粒度、24h 消耗堆叠条 + 图例、关系温度、命令面板 01~05 唯一命令区）+ 竖线 x=379 + 右栏 5 节 601（01 当前 + 接下来 3 条 + 汇总 / 02 定义行不重复 score·tier / 03 VAD 行 + 引用 ≤3 行 / 04 空间 3 行压缩 / 05 双组消耗条各自定标）；未启用模块显示「（未启用）」不画假 0；两遍绘制（先量后画，footer 由内容底反推），超 4096 按右栏节分页（`dashboard_today_p2.jpg`，返回恒为第一页）。
- **展示数据面增量（文字版逐字不变）**：`emotion/service.py` 的 `status_snapshot()` 新增 `label` / `share` / `vad_words` / `effective`（`effective` 与 `effective_vad()` 同源）；`cateye_dashboard.py` 日程块新增 `done_count` / `upcoming_count` / `next_items`（当前之后下 3 条）/ `current` 结构体（保留 `current_item` 只读别名）。纯文本档 `items_full` / `commitments` / `render_dashboard_text` 字段与输出保持不变。
- **死代码清理**：日程图 `_draw_header` / `_draw_row_card` / `_build_rows` / `_build_stats_text` / `COLOR_CARD` / `_spaced` / `_ellipsis` / `_text_width` / `_draw_text`，总览图 `_MODULE_ACCENT` / `COLOR_CARD*` / `COLOR_GRADIENT_*` / `COLOR_TEXT_*` / `_ellip` / `_right` / `_BLOCK_BODY[...][:40]` 全部移除；`generate_schedule_image` / `render_dashboard_image` 各只保留一个入口。
- **输出与红线**：文件名仍为 `.jpg`（R6 文件名契约不变），JPEG 参数提升为 `quality=95, subsampling=0`（4:4:4）；**画布高度 ≤4096 红线保留**——正常内容先分页满足，病态输入以钳制作最后防线；渲染失败仍抛 `RuntimeError`，调用方静默降级为纯文本（三级降级契约不变）。
- **门禁**：behavior 编号至 **198**——新增 **197)** 日程图（列网格/行高/三态/跨午夜/0·1·8·24·25·40·41 条分页/超长省略/2×/JPEG/字体缺失/病态 ≤4096）与 **198)** 状态总览（两栏限宽/命令唯一/引用 ≤3 行/双组定标/未启用/两遍绘制/病态 ≤4096）；既有断言中仅 78 号 `planning_block.current` 随结构体同步（R7 就地注明），文字版输出断言零改动；smoke / config / behavior 三件套 exit 0 全绿。

### 变更：问候链日程周期化重构（2026-10-23 第二轮，用户逐格裁定；版本不变）

本轮把早/晚安链的全部时间锚从「日历日（0 点）」整体迁移到 **bot 的作息周期四变量**（上一份/本轮日程的开始与结束时刻，每份日程进账时由代码重新计算），旧按「当日窗 + 首评时端点定死」的门槛闸与激活闸随之被机制替换（A-22 bug①②③ 收口）；晚安改**硬性连锁**（本周期早安开口才可晚安）；补发加**沉默冷静期**。计划书 §三十一（闸门矩阵经用户逐格确认，为唯一规格真源）。

- **作息周期四变量（新增数据层）**：每份日程进账时由代码计算——`schedule_cycle_state.json` 持久化 `previous/current_cycle_start/end`（epoch 秒）+ 进账原因（`tool_edit` / `regenerate` / `scheduled_generate` / `fallback_template`，LLM 不参与记数）+ 无日程兜底标记（anchored）。`cycle_start` ≈ 起床、`cycle_end` ≈ 入睡（晚安期限项与静默起点项并入同一真源）；夜猫子作息的跨 0 点端点照算真实时刻、不折回。
- **跨天进账校验（新安全网）**：例行生成的新日程窗与旧窗**重叠 >25% 或开始日跳动 >3 天** = 异常 → 告警并**拒绝替换**（保旧窗）；同日重排（命令 regenerate、bot 工具改单、模板兜底）跳过该校验，只做跨夜一致性检查与锚点偏差告警（只告警不罚）。
- **旧日程不再悄悄丢**：改日程或重排当日时，旧行先归档进 `schedule.db` 新伴随表 `schedule_archive`（快照 + 周期戳 + 进账原因），数据库自动迁移到 v4（旧行按其起止时刻补戳、标记 `legacy_v3`，升级无感）。
- **问候闸门按三流重定（判定窗由日历日换成周期窗，闸机制随之替换）**：
  - **群聊早安** = **硬闸**（判定窗 [上份日程开始 → 本份日程开始) 内 ≥1 条非 bot 消息，**逐 tick 实查**、不再首评定死整日锁死）+（可配）必要性检查：今天日程**第一个条目时段**内有其他人主动开口（AND，`greeting_require_activation`，关 = 不判）。硬闸不过时直接跳过该流，不建问候条目。
  - **群聊晚安** = **硬性连锁**：本群早安本轮已开口才可开口；未开口 → diag 显「因早安未开口→晚安连锁硬性忽略」。
  - **其他私聊早安** = 判定窗内对方发送条数 ≥ 阈值（`greeting_other_private_min_messages` 保留、ge=1、默认 5、描述已改）；**晚安** = 二选一（OR）：本轮早安已对该流开口（连锁）∨ 窗内对方发送 ≥ 新增阈值 `greeting_private_night_min_messages`（ge=1、默认 5）。
  - **目标私聊** = **无条件**：不再要求当日有非 bot 消息（旧「激活强制」随本轮解除），只看陪伴模块开关与既有掷点/到点流程。
- **补发冷静期（新）**：过掷点后才达标的补发，须 bot 本会话最近一条消息距今 ≥ `[schedule_advanced] greeting_min_silence_minutes`（默认 3，ge=0；0 = 不要求）才开口；到点前已正常达标的照常触发不受影响，窗内一直未达标到期仍弃。所有「不得在期限后道问候」的分支补齐越界弃置（含旧描写凌乱的几处）。
- **去重升版（marks v2）**：`greeting_stream_marks.json` 升 `version=2`——按流按 kind 记**最新已发周期**；旧 v1 记录视为不存在。早/晚安按流按周期去重，三类流与目标私聊同一口径，**热重载后不重发**。
- **无日程 fail-safe**：周期四变量取不到时，回退 `[schedule]` 两键构造本周期边界（内存态，标记 anchored，不写盘）；此时晚安按「本轮无早安 → 连锁关闭」处理（宁不发不变）。
- **diag 对齐（`/lbm_diag streams`）**：候选按**三流分族**（群聊 / 其他私聊 / 目标私聊（无条件）+ 频率调控），目标流回归诊断可见且序号殿后（既有群聊/其他私聊序号不漂移）；检查窗改周期判定窗（非日历 0 点）；判定窗内查不到消息的群聊行尾改注「硬闸窗内无消息·不发晨晚问候」——群聊硬闸改为「判定窗内 ≥1 条非 bot 消息」**逐 tick 实查**。`streams <序号>` 结论新增四档文案：「早安未开始（连锁待定）」「因早安未开口→晚安连锁硬性忽略」「连锁已通过」「数量闸达标 M/N」，关键量行增双阈值 M/N 与判定窗端、冷静期余量、目标流「无条件」前缀。
- **配置面 +2 键**：`[schedule_advanced]` 增 `greeting_min_silence_minutes`（补发冷静期，int ge=0 默认 3）；`[planning_proactive]` 增 `greeting_private_night_min_messages`（晚安 OR 阈值，int ge=1 默认 5），段键数 30→**31** / 13→**14**；全插件 11 段 **199 → 201 字段 / 3 隐藏 / 196 → 198 可见**。`greeting_require_activation`（群聊必要性）与 `greeting_other_private_min_messages`（私聊早安门槛）**键名不变、描述改为新判定窗语义**；无键删除，旧配置照常自动迁移。
- **B3 修复**：跨插件 API `companion_get_schedule` 时间渲染改走 `format_day_minute` 日帧助手（手写 `//60` 清零）+ 透传 `day_offset` + 昨日跨日行并入，夜猫子日表不再时序倒挂；smoke 增加源码守卫（读取渲染面禁手写 //60/%60）。
- **提示词放开**：日程生成移除「固定相等段长」锚定手法，只约束起点=起床锚点、终点=入睡锚点、条目数在 6~12 由配置决定；示例与 duration 提示改非均匀示意；长时段（如 5 小时长活动）不再被评分罚分（数量/锚点/空档校验保留）。`/lbm_plan regenerate` 重建即可吃到新写法。
- **门禁**：behavior 编号至 **201**（**199** = 周期数据层与锚点合并、**200** = 三流闸门与连锁/冷静期/标记 v2、**201** = 提示词放开 + B3；既有 15 个问候/streams/周期用例同步重写，R7 注明）；配置面 **201 字段 / 3 隐藏 / 198 可见**、组件 29、manifest 17 项、版本 2.1.0 不变；smoke / config / behavior 三件套 exit 0 全绿。

### 变更：映射表种子目录更名 default_mappings + 表层清空（2026-10-24，用户直派；版本不变）

用户直派（计划书 §三十二，登记即执行）：深层出厂种子目录改名 `default_mappings`，表层 `mappings/` 不再保留任何映射表文件。**无任何配置项变化，零新增 LLM 调用点，插件版本与 `[plugin] config_version` 维持 2.1.0 不递增。**

- **深层种子目录改名（目录结构变化）**：`mappings/出厂映射表/` → **`mappings/default_mappings/`**（`git mv` 保历史）；深层 `README.md` + 五张出厂默认表照旧随包分发、随插件版本更新。
- **表层彻底清空（目录结构变化）**：此前仅退出仓库跟踪、工作区仍保留的五张 `*.json` 与表层 `README.md` 已**物理删除**；`mappings/` 目录里只剩种子子文件夹（fresh checkout 与开发树同形态）。插件运行期仍**只读表层**（`core/mapping_dir.mappings_path` 不变）：某张表缺失时按既有链条补齐——旧插件根目录版优先迁移，仍缺才从 `default_mappings/` 复制出厂默认；**解析错误不算缺失**（存在但写坏 → 告警 + 回退内置默认，绝不复制/覆盖用户文件）。
- **`.gitignore` 两条规则不变**（`/mappings/*.json` + `mappings/README.md`，仅注释内深层目录名同步为 `default_mappings`）。
- **门禁**：behavior 编号仍至 **201**（本批不新增用例；196 映射种子链用例内的目录名断言与注释同步，结构断言不变）；smoke / config / behavior 三件套 exit 0 全绿；配置面 **201 字段 / 3 隐藏 / 198 可见**、组件 29、manifest 17 项、版本 2.1.0 不变。

### 变更：睡眠裁判提示词压缩与 max_tokens 1024（2026-10-24，用户裁定 A-23 推荐组合 (e')+(e'+)+(f') 插件侧落码；版本不变）

用户裁定 A-23 推荐组合的插件侧部分，登记即执行：睡眠裁判提示词采用实验 W3 模板（reason 压缩 + 内部思考收短），睡眠裁判调用 `max_tokens` 2000 → 1024。实验依据 scratch/_r34：W3 变体 reasoning 中位 253 ≈ 基线 1/3、通过率 5/6；r33b/r34 矩阵 2/2 稳定（reasoning 227/230）。**无任何配置项变化、零新增配置键，插件版本与 `[plugin] config_version` 维持 2.1.0 不递增**。

- **提示词模板（`emotion_sleep_referee`）**：规则 7 改为「reason 每条不超过 30 字，概括依据即可，不必逐字引用原文，禁止脑补不存在的事件」（保留原句的禁止脑补约束）；其后新增规则 8「内部思考控制在两三句以内，随后立即输出最终 JSON」（即实验 W3 的末尾追加句）。占位符集不变（`{limit}` 等 18 个全部保留）。
- **调用参数（`emotion/referee.py`）**：睡眠裁判 `max_tokens` 2000 → **1024**；注释同步说明现行口径与历史脉络（早期 600 tokens 会截断 JSON）。`temperature 0.4` 等其余调用参数不变。
- **宿主侧 (f') 纯配置路径**：宿主模型可再配 `reasoning_effort=low` 进一步压思考链——此为宿主侧操作、插件零代码，指引见 USAGE.md「睡眠裁判思考强度配置指引」小节；不配 effort 时 W3 模板亦可在 1024 下工作（实验口径 n=2）。

### 变更：状态总览情绪节「近期情绪变化」（2026-10-24，用户直派；版本不变）

用户直派：`/lbm_dashboard` 状态总览（文字版 + 图片版）情绪节新增「近期情绪变化」——最近 3 条已入账情绪事件的成因与 ΔVAD 变动。**只读展示投影，零配置项变化、零新增 LLM 调用点，插件版本与 `[plugin] config_version` 维持 2.1.0 不递增。**

- **数据面（`emotion/service.py` `status_snapshot`）**：新增只读字段 `recent_events` = 最近 3 条**已入 `emotion_log`** 的事件，每条 `{"time": "HH:MM", "desc": 事件描述截 40 字, "v"/"a"/"d": ΔVAD 四位小数}`；冻结期 `pending_events` 不计（醒后并入日志才可见）；既有字段零变化。
- **透传（`cateye_dashboard.py` `emotion_block`）**：`recent_events` 透传进状态块，旧快照无键回退 `[]`，条目形状异常逐条防御。
- **文字版**：情绪节末尾追加「近期变化」段（既有行零删，§9.3 铁律），每条一行「`近期变化 HH:MM desc（V+0.08|A-0.05|D+0.12）`」，|变动| < 0.005 的维度不列（与随图日志三维变动标注口径一致），三维全低 →「（微小波动）」；空列表显示「（无记录）」。
- **图片版**：右栏 03 情绪节追加「近期变化」行组（每条一行、上限 3；desc 段在测量遍经 `render_kit.fit_text` 按像素截断、ΔVAD 段恒可见；空列表「（无记录）」）；测量/绘制两遍同一 `_Painter` 指令流接入；4096 钳制与按节分页契约不动。

### 变更：告状工具 `lbm_report_harassment` + 日程提示词 continue 键弱化（2026-10-24，用户直派；版本不变）

用户直派两项（需求3/需求4）：**① 告状工具**——bot 受骚扰时向维护者转达（小工具模块、可关闭、关闭则不注册）；**② 日程注入的「对话延续」引导词弱化**——去掉「可自然带过当前活动」的弱鼓励色彩。除告状 3 个新配置键外无其他配置项变化；**零新增 manifest 能力、零新增 LLM 调用点**（告状为纯转发，不调模型）；插件版本与 `[plugin] config_version` 维持 2.1.0 不递增。

- **告状工具（新增 Tool `lbm_report_harassment`）**：bot 被恶意刷屏/辱骂/攻击/被故意戏弄到不适时，把一句话情况说明转达给维护者私聊；description 引导明确「普通玩笑与日常聊天不要调用」。发送文案为**运维消息**形态 `【告状】{（发生地：…）}{description 原文}`——**不追加情绪后缀**（不是角色消息）；kwargs 里带 `stream_id` 时作为发生地附在文案与日志。纯逻辑（维护者解析优先级/冷却判定/文案构造/决策日志字段）独立在 `companion/harass_report.py`（不触碰宿主 ctx，可独立 import 测试）；ctx 操作（stream 解析/发送/日志）在 `companion/components.py` 组件层，维护者 stream 解析形态照 `plugin._resolve_target_stream`（`get_stream_by_user_id` 首选 → `get_private_streams` 遍历回退）。
- **维护者解析优先级**：`[admin].harass_report_target_qq` 非空优先（占位号 123456789 视为未配置）；留空回退 `admin_users` 第一个纯 QQ 元素（`qq:123456` 形式也算）；两者皆空 → 工具返回「未配置维护者」提示、不发送。
- **冷却与缓存**：`[admin].harass_report_cooldown_minutes`（默认 30，0 = 不冷却）——冷却内再次调用返回剩余分钟提示并跳过发送；冷却时间戳与维护者解析结果**进程内缓存**（热重载/重启清零，可接受）。
- **关闭不注册**：`plugin.py` 新增 `get_components()` override（最小化，此前无该 override）——`[admin].harass_report_enabled=false` 时从 `super().get_components()` 结果中按名剔除 `lbm_report_harassment`（dict / 对象列表两种收集形态兼容；配置不可读时按默认值处理、不误删）。Tool 入口另有双保险：组件驻留窗口期调用返回「告状工具未启用」。
- **可观测性**：每次调用写决策日志 `trigger_type=admin.harass_report`，成功（`reported`）/失败（`send_failed`/`stream_resolve_failed`/`no_maintainer`）/冷却跳过（`cooldown_skip`）三形态齐全。
- **日程提示词 continue 键弱化**：`[prompts].inject_hint_continue` 出厂文案（`suite_prompts.INJECT_HINT_CONTINUE_DEFAULT`）由「提示：对话延续中（{reason}），可自然带过当前活动。」改为「提示：对话延续中（{reason}）。当前活动只是背景信息：用户不问就不要说；只有当话题确实接不上、需要找新话题时，才可以用一句当前活动自然开启新话题。」（`{reason}` 占位符保留；`inject_service` 消费面零改动，键 8 `INJECT_HINT_PROACTIVE_DEFAULT` 与其余键不动）。
- **配置面（`[admin]` 5→8 键）**：新增 `harass_report_enabled`（bool，默认 `true`）/ `harass_report_target_qq`（str，默认 `""`）/ `harass_report_cooldown_minutes`（int，默认 30，ge=0）三键；本批全插件 **+3 字段**（均可见、均落 `[admin]` 段）。
- **组件面（29→30）**：Tool +1（`lbm_report_harassment`）；`harass_report_enabled=false` 时运行期注册面回落 29。
- **门禁提示**：本批与同日并行批次（问候链静默窗等）先后落码，scratch 冒烟脚本（本轮不改）的组件计数断言（29 组件 / tool 7）、配置面断言与 `[planning_proactive]` 键集清单需在下一轮门禁**按当日全部批次的合计实算**统一更新（仅本批的增量 = 组件 30 / tool 8、[admin] 8 键）。

### 变更：早安沉默闸 + 无睡眠模式大修改新日程进账触发（2026-10-24，用户直派；版本不变）

用户直派两项（R7 = 用户 2026-10-24 直派）：**① 早安沉默闸**——bot 至少在今日第一日程（= 周期起点）前沉默 N 小时，否则该会话流跳过早安；**② 无睡眠模式大修改触发时序改造**——`[schedule].no_sleep_mode=true` 时夜内不再打睡眠裁判（不重试、不做 resleep/夜扰扫描），睡眠情绪大修改由**新日程进账**（`schedule.generated` 事件）触发醒沿结算链一次性完成。配置面 `[planning_proactive]` +1 键；**零新增 LLM 调用点**（大修仍走既有睡眠裁判 `ask_sleep_referee`，仅新增触发时机）；插件版本与 `[plugin] config_version` 维持 2.1.0 不递增。

- **早安沉默闸（新配置键）**：`[planning_proactive] greeting_morning_silence_hours`（float，默认 `2.0`，`ge=0`；`0` = 禁用闸门）。闸门在 `_pre_gate` 早安分支入口、**三流分派之前**作为第一个检查：`N>0` 且 `now >= cur_start` 时，查窗 `[cur_start − N×3600, cur_start]` 内该流 bot 自身消息（`_messages_in_window(…, limit=20, keep_bot=True)` 后逐条按 `is_mai` 过滤，写法对齐补发冷静期 `_calm_gate`）——存在 ≥1 条 → `_log_gate_skip`（reason 含 `morning_silence` 与窗口）+ durable skip（**不掷点、不建条目**，对齐群聊硬闸语义）；查询异常 fail-closed（同硬闸形态）。**三流统一受此闸（含目标私聊）**；`now < cur_start`（尚未进入本周期）或周期起点不可得时恒通过。
- **早安沉默闸（diag 同步）**：`/lbm_diag streams <序号>` 早安结论新增一档「**沉默闸未过（第一日程前 N 小时内 bot 发过消息）**」（`_greeting_kind_status` 只读实查一次、未过即短路其余档位；明细行加「沉默闸=通过/未过/取不到（窗=HH:MM → HH:MM，N=Xh）」）；消息查询能力缺失且 N>0 时早安族归入既有「取不到（缺消息查询能力，宁不发）」档（与闸门 fail-closed 行为一致）。
- **无睡眠模式·夜内跳过裁判**：`_check_and_overhaul` 睡眠入口段（夜窗兜底 = 作息锚点整夜窗，即「日程睡眠时间」同源）在 `_no_sleep_mode()` 为真时**只 freeze + flush**——跳过 `compute_sleep_overhaul`（不打裁判、不建缓存），决策日志留痕 `emotion.sleep_overhaul`（`trigger_type=schedule_generated_deferred`，reason 含 `no_sleep_defer`）；frozen 分支在 no_sleep_mode 为真时同样**跳过裁判重试与 resleep/夜扰扫描**（`_roll_night_segment` / `_after_night_segment_closed` 加 no_sleep 门；吵醒记账仍被既有 `note_night_reply` / nap 提醒的 no_sleep 门控挡住，未改动）。
- **无睡眠模式·新日程进账触发大修**：emotion 侧新增订阅事件总线 `schedule.generated`（生成侧发布点 `schedule_generator` 不变、无总线静默；订阅挂 `EmotionService.start()`、`stop()` 注销，owner=`emotion_no_sleep_overhaul`，幂等）。回调：`no_sleep_mode && is_frozen()` 时立即执行**醒沿结算链**——`close_night_interrupt_segment('wake')`（no_sleep 下无段、天然空操作）→ `flush_all_episodes('wake')` → `compute_sleep_overhaul(now)`（失败仅告警**不重试**，下一份日程进账再试）→ 置缓存（带 cycle_key）→ `_apply_cached_overhaul`（复用既有应用语义：unfreeze + apply_sleep_overhaul + `applied_sleep_date` 落盘护栏，缓存由 apply 内部取走）；决策日志留痕 `emotion.sleep_overhaul`（`trigger_type=schedule_generated`）。回调 try 全包、异常绝不外抛（总线单订阅者隔离之外再加兜底）。
- **兼容**：`no_sleep_mode=false` 时全链行为零变化（订阅回调第一判直接 return；`_check_and_overhaul` 两处 no_sleep 门均短路放行旧路径）。
- **配置面（`[planning_proactive]` 14→15 键）**：本批全插件 **+1 字段**（可见、落 `[planning_proactive]` 段）；组件面不变。
- **门禁提示**：同日并行批次已登记的 scratch 冒烟组件/配置面断言漂移（29 组件 / tool 7 基线 vs 实际）在本批继续累积 +1 配置字段（`greeting_morning_silence_hours`），统一更新仍按当日全部批次合计在下一轮门禁执行。

### 新增：日程系统滚动化改造（2026-10-25，用户直派；版本不变）

> 依据：用户 2026-10-25 直派（日程系统滚动化改造，设计目录 `D:\deepseek-harness_workspace\maibot-plugins\2026-10-25-日程系统滚动化改造设计`）。本轮把日程从「全天一张排满的表」升级为「日意图（spine）+ 滚动执行 + 生活状态 + 活动日志」四层；**全部新功能默认关**——`[life_state].enabled=false` 与 `[schedule].spine_mode=false` 两开关全关时插件行为与 2.1.0 逐字一致（既有 behavior 用例仅两处 schema_version 断言随 v5 迁移同步加严，其余零改动全绿实证）。**插件版本与 `[plugin] config_version` 维持 2.1.0 不递增（政策随用户裁定递增，本轮未裁定）**。

- **新模块清单（5 个新文件 + 8 个既有文件增量，旧路径/旧函数一律保留不删）**：
  - `planning/life_state.py`（LifeStateService）：精力/饥饿/专注/压力/社交电量五项 0~1 状态 + 睡眠债 0~12h，纯本地 tick 推进（被动漂移 + 活动画像速率，零模型调用）；睡眠三档结算、episode 完成盖吃饭/休息时间戳、状态文件 `life_state.json` 原子落盘（损坏重置默认 + 告警不阻塞，重启从 last_tick_at 补算最多 24h）；
  - `planning/rolling_planner.py`（RollingPlanner）：滚动执行核心——候选打分（硬约定/锚点/软意图/恢复池/待机/随机事件六源）、top2~3 softmax 采样、硬约定锁定三态（提前 15 分钟预警、到点抢占、错过如实登记）、打断/恢复/放弃状态机（恢复池上限与过期）、随机小事件（只在转场掷骰、每日上限、同事件 4 小时去重、模板 1 句零模型）、重规划节流（冷却 + 每小时上限 + 阈值滞回）、早晨确定性调整（按实际睡眠裁剪日意图，零模型）；
  - `planning/life_schedule.py`（LifeScheduleReadModel）：对上层提供 current / next / today / state_line 读取门面（睡眠条目过滤语义与既有口径一致）；
  - `planning/activity_profiles.py` + 深层种子 `mappings/default_mappings/activity_profiles.json`（**第六张映射表「活动画像表」**）：10 类内置画像（时长分布/最小块/可中断性/切走成本/五项状态速率/恢复倾向/自发权重），json 同类型覆盖内置、缺类型回退 `daily_routine`、三级校验粒度与既有映射表同款，缺失回退内置默认；
  - 接线：`schedule_generator.py` 加 `mode="spine"` 分支（旧 `daily` 模式零删除）、`auto_scheduler.py` 加 spine 固定时刻触发、`schedule_query.py` 加 runtime provider 前置分支（缺失/关闭/异常三态回退旧时间窗逐字）、`inject_service.py` 注入块追加 runtime 实况 + 能量行 LifeState 优先逐字回退、`proactive_service.py` 仅改当前活动读取源（**主动消息链零触碰**，不新增任何主动开口）、`plugin.py` 装配（关闭即零实例化、装配失败告警回退绝不拖挂加载）、`planning/services.py` 加三个占位属性、`companion/components.py` + `qzone/components.py` 加 rolling 调试命令。
- **schedule.db 升级 v5（只增不改）**：新增 `activity_episodes` 活动日志表（episode 全字段 + 三索引 + active 单例唯一索引），v4→v5 故障隔离迁移（失败告警不抛、旧表可读、版本下次重试）；episode 状态机 = planned/active/completed/interrupted/paused/abandoned/skipped；旧表零改动，schema_version 由 `'4'` 改按迁移结果写入 `'5'`。
- **新配置键（全部默认关，共 +34 键）**：`[schedule]` +11 个 spine 键（`spine_mode` / `spine_min_intentions` / `spine_max_intentions` / `spine_max_described_intents` / `spine_recent_days` / `spine_history_messages` / `spine_knowledge_limit` / `spine_max_tokens` / `spine_use_multi_round` / `spine_generate_at` / `planning_daily_token_budget`）；新段 `[life_state]` 17 键（`enabled` / `tick_seconds` / `horizon_minutes` / `replan_cooldown_minutes` / `max_replans_per_hour` / `random_event_rate` / `max_random_events_per_day` / `resume_pool_size` / `resume_expire_minutes` / `low_energy_threshold` / `high_hunger_threshold` / `high_stress_threshold` / `brief_attention_progress` / `late_attention_progress` / `profile_path` / `state_file` / `keep_episode_days`）；`[prompts]` +6 个 spine 提示词键。配置面 **11 → 12 段、205 → 239 字段 / 3 隐藏 / 236 可见**（以门禁实算为准）。
- **调试命令（管理员，纯只读零模型零副作用）**：`/lbm_life rolling [status|state|episodes|spine|preview]`——status=今日滚动概览、state=五项生活状态全字段、episodes=今日活动日志、spine=今日日意图、preview=下一 tick 候选预演；两开关全关时输出「rolling 未启用（[life_state].enabled / [schedule].spine_mode 均为 false）」。
- **新事件与消耗标签**：`life.episode_started/completed/interrupted/resumed/abandoned`、`life.state_changed`、`life.random_event` 七事件经既有事件总线发布（旧 `schedule.*` 事件全保留）；LLM 消耗标签新增 `schedule_spine`（归属 planning 模块，`/lbm_diag llm` 与面板「模型消耗」可见）——本轮唯一新增模型调用点，滚动 tick / 状态更新 / 随机事件 / 早晨调整全部零模型调用。
- **默认关口径与回滚点**：两开关全关下所有新分支旁路、注入文本与旧行为逐字一致（行为门禁既有 204 用例零改动全绿 + 新增 205~212 用例实证）；本轮基于 git 回滚点 tag `rollback/pre-rolling-20261025`（另有备份分支 `backup/pre-rolling-20261025`），关闭两开关即回到旧行为，无需回滚代码。
- **门禁**：behavior 编号至 **212**（新增 205 LifeState / 206 活动画像表种子链 / 207 schedule.db v5 episodes / 208 RollingPlanner / 209 ScheduleQuery runtime provider / 210 spine 生成分支 / 211 rolling 调试命令 / 212 注入面 runtime 实况；既有 163/199 两处 schema_version 断言 4→5 同步加严，R7 注明非削弱）；smoke 更新为 12 段 / 239 字段 / 3 隐藏 / 236 可见 + 深层种子六件→七件；三件套 exit 0 全绿。**遗留（登记未实现，交后续轮/用户裁定）**：patch（补丁重规划）与 digest（日摘要）两项模型调用未实现、emotion 订阅 `life.*` 事件的耦合系数未做、`life_state_snapshots` 趋势表未建、晨间调整钩子已定义未接线、画像表落位为深层种子（`mappings/default_mappings/`，与设计字面路径 `mappings/activity_profiles.json` 不同，符合 §29.2「表层 = 用户数据区」纪律）。

### 变更：日程系统二期 PR1——滚动化开启态 P0 前置修复（2026-10-26，用户直派；版本不变）

> 依据：用户 2026-10-26 直派，按日程系统二期计划书推进（计划书 §三十四 / 定稿 PR1）。本轮**全部是滚动化开启态**（`[life_state].enabled` / `[schedule].spine_mode` 任一开启）的行为修复（P0 表 0.1~0.7）——**两开关全关时插件行为与 2.1.0 逐字一致**，升级后不配置任何新键的用户零感知。**零新增配置键、零新增 LLM 调用点；插件版本与 `[plugin] config_version` 维持 2.1.0 不递增。**

- **运行中活动进度随实际增长（0.1，修复）**：开启 `[life_state].enabled` 后，进行中活动（episode）的进度此前停在起始值不随时间推进；现在滚动 tick 每拍按「已用 / 计划时长」重算并写回日程库（上限 0.95、不超 1.0），`/lbm_life rolling status` 与注入面读到的进度随之真实增长；已完成/暂停/计划等非运行中行不被改写（日程库新增运行中行刷新方法，行不存在 / 非运行中 / 写失败一律静默跳过不抛）。
- **注意力分档运行期生效（0.2，修复）**：低可中断活动（如写报告）的注意力档（正常 → 简短回复 → 稍后细说）此前不随进度切换；现在每拍按 `[life_state] brief_attention_progress`（默认 0.3）/ `late_attention_progress`（默认 0.7）阈值随当前进度重算，**档位变化才写回**（大多数拍只更新进度一行）；中/高可中断与硬约定（恒「稍后细说」）的档位行为不变。
- **被硬约定抢占的活动如实落值（0.3，修复）**：硬约定到点抢占时，被暂停的活动行此前不写实际结束时刻与进度，导致恢复池排序失效；现在写入**抢占时刻**与按默认口径算出的**真实进度**（上限 0.95），恢复池能按「进度 × 优先级」重新命中该活动；进度不足 0.2 时按原概率放弃的既有语义不变。
- **随机事件状态增量随活动关闭落账（0.4，修复）**：随机小事件（如「找东西」专注 −0.05、压力 +0.02）此前只停留叙事、五项状态不变；现在活动关闭时按事件键回查随机事件池，把状态增量交给生活状态系统**恰一次**应用（episode 元数据持久写 `state_deltas_applied` 标记，重复关闭不二次应用）；被硬约定抢占后经恢复池恢复再关闭的事件同样能取到增量；被直接挤掉（close-active）的路径本轮不应用也不标记（留给二期后续接线，不会双应用）。
- **空闲读取统一（0.5，行为变化，仅滚动化开启时）**：滚动层就绪后「现在在做什么」一律以活动日志为准——空闲时段**如实返回无活动**，不再回退旧时间窗编造条目；唯一例外：深夜睡眠环内（`[schedule]` 入睡~起床）且旧时间窗命中睡觉条目时仍回退返回并告警一次（同一睡眠窗只告一次，跨窗重新允许）。滚动层未开启 / 已关闭 / 读取异常三态仍走旧时间窗路径逐字不变（情绪与空间侧的睡眠期读取链不受影响）。
- **活动画像表装载接线（0.6，修复）**：第六张映射表 `mappings/activity_profiles.json`（活动画像表）此前已随包定义但未装载；现在随插件装配装载（`[life_state].enabled=true` 时），坏文件整退内置仅告警、不拖挂插件加载；改表层文件后**重启或热重载生效**（与另外五张映射表同口径）。注意 `[life_state].profile_path` 键本轮仍未接线——读取位固定为表层 `mappings/activity_profiles.json`，自定义路径不生效（待用户裁决）。
- **regenerate 跟随 spine 模式（0.7，行为变化，仅 spine 开启时）**：`[schedule].spine_mode=true` 时，`/lbm_plan regenerate` 与日程工具重排「今天」的路径同样生成 3~6 条日意图（spine 口径）；关闭时生成入参与旧全天生成逐字一致。
- **用例与门禁**：behavior 211 日期腐烂修复（夹具不再硬编码日期，隔日不再必红；preview 断言按睡眠环二分，两分支语义都钉住）；208 豁免断言按新进度口径同步（恢复块按当前块窗口进度判定，0% → 50% 属口径同步）；新增 **213~219** 七条用例覆盖 0.1~0.7（各含反向断言：开关关闭时不装配、零可观测）。门禁：behavior 编号至 **219**，smoke / config round / behavior 三件套 exit 0 全绿；配置面 **239 字段 / 3 隐藏 / 236 可见**、组件 30、manifest 能力 17 项均不变（R7 依据 = 用户 2026-10-26 直派，计划书 §三十四 / 定稿 PR1）。

### 变更：日程系统二期 PR2——情绪与生活状态双向闭环（2026-10-26，用户直派；版本不变）

> 依据：用户 2026-10-26 直派，按日程系统二期计划书推进（计划书 §三十五 / 定稿 PR2）。本轮让**情绪与生活状态互相影响**：调制侧（情绪 → 生活状态速率）与反馈侧（活动完成/打断 → 情绪）同批实现。总开关为 `[life_state]` 新增键 **`emotion_modulation_enabled`（默认 `false`）**——**关闭（含旧配置无此键）时插件行为与本轮之前逐字一致**：零调制实例化、零情绪读取、零订阅、零预算写入，升级后不配置任何新键的用户零感知。**tick 全链零模型调用**（本轮零新增 LLM 调用点）；**插件版本与 `[plugin] config_version` 维持 2.1.0 不递增**；组件 30、manifest 能力 17 项不变。

- **调制侧（情绪 → 生活状态，行为变化，仅开关开时）**：`emotion_modulation_enabled=true` 且情绪模块可用时，生效情绪（愉悦度/唤醒度/支配感三维）经夹取 → 15 分钟 EMA 平滑 → 中性死区（|x|<0.12 按中性处理）折算成低落/激活/紧张三个调制量，再按系数表对精力/饥饿/专注/压力/社交电量五项状态的**变化速率**乘 rise/fall 增益（全部夹在 [0.70, 1.30] 内）——例如低落时精力掉得更快、紧张时压力更难回落；只调制自然漂移速率，不直接改状态值，睡眠三档结算路径不受调制。情绪模块停用或读数异常时自动降级为无调制（行为与开关关完全相同，tick 循环不中断）。
- **反馈侧（活动 → 情绪，行为变化，仅开关开时）**：生活状态开启且开关开时，订阅活动日志事件——完成 25 分钟以上的工作/学习 → 情绪小幅正向（愉悦度 +0.02 / 支配感 +0.01）；被打断的工作/学习 → 小幅负向（愉悦度 −0.03 / 唤醒度 +0.02 / 支配感 −0.02）；完成休息与吃饭等也有对应小增量，均走既有外部情绪事件的同一入账口径（含 ±0.5 硬限幅）。每类事件有**每日上限**（如深度工作的愉悦度累计到 +0.06 后当日不再入账）、同一活动按 episode 去重；每日记账落数据目录独立文件 `life_feedback_budget.json`（可安全删除）。睡眠类活动双向跳过（不调制、不反馈），无双重记账。
- **第七张映射表 `mappings/emotion_life_modulation.json`（情绪-生活调制表）**：调制系数与反馈规则全部外置进既有两层种子链——表层 `mappings/` 是用户数据区（不随包分发、更新插件不覆盖），缺失时自动从深层 `mappings/default_mappings/` 补齐；**坏表回退内置默认且绝不覆盖/重写你的表层文件**；只写部分条目时未写的沿用内置默认（同名覆盖、其余保留）；改完**重启或热重载**生效。随包说明文档 `mappings/default_mappings/README.md` 新增「八、情绪-生活调制表」一节（字段表、校验与出错行为、可抄示例）。
- **配置面**：`[life_state]` 17 → **18 键**（新增 `emotion_modulation_enabled`）；全插件 **239 → 240 字段 / 3 隐藏 / 236 → 237 可见**，12 段不变。
- **用例与门禁**：behavior 新增 **220~224** 五条用例（调制计算与系数表装载/回退、状态速率按符号乘增益、planner 快照契约与插件编排、反馈事件入账与日上限/去重、三重开关门控与降级），behavior 编号至 **224**；smoke 深层种子断言七件 → **八件**；smoke / config round / behavior 三件套 exit 0 全绿（R7 依据 = 用户 2026-10-26 直派，计划书 §三十五 / 定稿 PR2）。
- **遗留（登记未实现，交后续轮）**：睡眠恢复速率的调制接线（本轮睡眠走三档结算、无速率积分面）与定稿 §4.1.3 的 homeostatic override / 每 tick delta 上限未做；「7 天模拟稳定」属 PR3 shadow/soak 阶段范围。

## [1.1.0] - 2026-10-10

配置大整理版：把配置从 13 段重排为 **11 段**，集中了提示词与模型调用，修好既有缺陷，并重做了早/晚安链。**旧配置在首次启动时自动迁移，无需手改**（幂等，可反复重启）；升级时插件会先读回磁盘上的旧配置再搬运，**跨段/改名键的用户值不会丢**。插件版本与配置版本均为 **1.1.0**（同步）。本版同时收口了第三轮真机问题（日程跨午夜写库/空档重罚、无活动兜底、静默留空跟随作息、注入链 session_id 写回、用户可见文案去 emoji）。

### 新增

- **`[llm]` LLM 调用段（9 键）**：集中插件全部模型调用——主任务名 `task_name`、判定类模型与参数（`judge_model` / `judge_max_tokens` / `judge_temperature`）、以及各用途任务名（好感裁判 / 情绪睡眠裁判 / 情绪评估 / 空间 / 视觉）。原先散落在 `[judge]`、`[schedule] llm_task_name`、`[affection] review_task_name`、`[emotion] evaluator_task_name / referee_task_name`、`[qzone] llm_task_name`、`[cateye] vlm_task` 六处的模型路由统一收拢于此。
- **`[prompts]` 提示词段（65 键，高级）**：插件发给模型的全部原文集中暴露（陪伴 / 日程 / 情绪 / 空间 / 工具五组），带占位符说明。不改 = 与出厂文案逐字一致，改了立即生效；占位符写坏只会回退出厂文案并记一条告警，**不会中断业务**。（第三轮新增 1 键：`inject_no_activity` 无当前活动时的兜底文案；留空 = 不注入。）
- **`[schedule_advanced]` 日程系统·高级设置段（30 键）**：原 `[inject]`（注入调优）、`[planning_cleanup]`（后台清理）两段，以及原 `[schedule]` 的一批深水项（时区、生成 token/超时/回看、角色裁判、绘图、缓存、赖床上限、情绪影响强度等）全部并入。
- **`[planning_proactive]` 统一名单系统**：群聊名单 `proactive_group_ids` + `proactive_group_list_mode`（默认黑名单，留空 = 全群生效）与私聊名单 `proactive_private_ids` + `proactive_private_list_mode`（默认白名单，留空 = 全不发）；一份名单同时服务「活动切换主动开口」与「早/晚安问候」。（**后续口径**：其中「活动切换主动开口」已于 2026-10-20 整体移除，见上文 [2.0.0] 的「主动消息安全治理」子节。）
- **`[patrol] inject_affection_in_group`**（默认开）：群聊上下文出现陪伴对象时注入好感度档位块。
- **`[schedule] holiday_inject_enabled` / `holiday_affects_schedule`**（默认开 / 默认关）：真实节假日注入日程块头部 / 影响日程生成。

### 变更

- **配置段 13 → 11**：`[target]`、`[cateye]` 并入 **`[patrol]`（陪伴模块）**；`[judge]` 并入 **`[llm]`**；`[inject]`、`[planning_cleanup]` 并入 **`[schedule_advanced]`**；提示词集中到 **`[prompts]`**。新段序（WebUI）：`plugin → patrol → affection → schedule → schedule_advanced → planning_proactive → llm → prompts → admin → qzone → emotion`。
- **早/晚安简化**：三类会话开关（`greeting_target_enabled` / `greeting_group_enabled` / `greeting_other_private_enabled`）与问候专用黑白名单收拢为**两个总开关** `greeting_morning_enabled` / `greeting_night_enabled`（默认都开）＋统一名单；问候**恒免主动打扰预算**。
- **早/晚安链重做（2026-10-10 第二轮，用户口径）**——固定顺序「**掷点 → 激活观测 → 到点（或已过点则立即）发**」：
  - **激活观测对所有会话一视同仁（含目标陪伴私聊）**：`greeting_require_activation=true`（默认）时，只有观察窗内**该会话有人发过消息**才问候；窗内无人 → 当天不发。观察窗：早安 = `[schedule] wake_time` → 当天第一段非睡眠日程结束（无日程 = 起床 + 120 分钟）；晚安 = 最后一段非睡眠日程结束 → `min(入睡时间, 静默起点)`。
  - **漂移点**：早安 = 起床锚点 + `randint(a,b)`；晚安 = **睡前界（`min(入睡时间, 静默起点)`）− `randint(a,b)`**（睡前随机点，不再在最后一段日程还没结束时开口）。
  - **静默时段不再拦截问候**：默认静默窗 00:00-07:30 与起床锚点 07:00 重叠，此前会把早安漂移整段吞掉甚至直接放弃；问候的必要性由"激活观测"把关。`[patrol] silence_*` 继续约束巡检。
- **统一名单严格生效（B2）**：私聊名单为**黑名单**模式时，把某个 QQ（**包括目标陪伴对象自己的 QQ**）写进名单即不再问候它；白名单模式下目标私聊视为恒在名单内（留空不影响默认问候体验）。名单元素现在也接受 `group:` / `qq:group:` / `private:` / `qq:private:` / `qq:` 前缀写法（自动取号），此前这些写法会被当作非法元素**静默忽略**（黑名单因此"看起来没生效"）。
- **每日预算即上限**：删除独立的目标私聊打扰上限字段（`proactive_target_daily_cap` / 旧名 `global_proactive_cap`）；`[patrol] daily_max_speak` 即陪伴侧主动打扰上限，只统计**日程巡检与想念**两类（早/晚安不占预算）。
- **想念阈值合并**：`miss_hours_min` + `miss_hours_max` 合并为一个 `[patrol] miss_silence_hours`（如 `"4-8"`，或单数字 `8` = `0~8`）；想念「避开未来日程」功能整体移除。
- **注入矩阵定稿**：目标陪伴私聊**始终**注入好感度；其他私聊**始终只注入日程**；群聊仅在上下文出现陪伴对象且 `inject_affection_in_group=true`（默认开）时注入好感度档位块（不带关系记忆行）。日期/节假日行改由日程模块注入（`[schedule] holiday_inject_enabled`）；日程注入范围 `inject_scope` 默认 `all`（所有会话）。
- **静默窗默认改为「留空 = 跟随日程作息」（第三轮）**：`[patrol] silence_start` / `silence_end` 出厂默认由 `"00:00"` / `"07:30"` 改为 **`""`**——留空即跟随 `[schedule]` 的 **入睡时间（`sleep_time`）~ 起床时间（`wake_time`）**，非空才是自定义静默窗（支持跨午夜；两键可各自独立跟随或覆盖）。巡检静默闸与晚安的睡前界（`min(入睡时间, 生效静默起点)`）共用同一生效值解析；生效值非法仍按「宁可不发」fail-safe（巡检整天静默、当晚不排晚安）。**显式写过非空值的旧配置逐值不变**。
- **日程写库改为「跨午夜 = 同一个 24h 环」（第三轮）**：睡觉条目保留原时长并允许写出跨午夜形态（如 23:00 入睡 + 8h ⇒ `23:00–07:00`，`end` 大于 24:00）；跨午夜作息的"睡眠环内"条目同样保留时长，非睡眠条目的结束才夹到下一个入睡时刻。此前入睡钟点早于条目起点时，全天条目会被压成 15 分钟（真机表现为"晚餐 17:30–17:45、下一项 18:30"的大段空档）。**注意**：旧库里已被压坏的历史行不会被代码修好，请用 `/lbm_plan regenerate` 重建当日日程。
- **空档升级为可重试的质量问题（第三轮）**：空档检查改在**归一化之后**复查（跨午夜作息下睡眠时段不算空档），空档类警告按 **0.25/条**单独重罚（原 0.05/条）——单项即可把满分压到 0.75、低于 0.85 质量线，必然触发下一轮重试；时间覆盖率由"不同起始小时数 / 16"改为**分钟级**（清醒环内占用分钟 / 清醒时长）。
- **用户可见文案去掉 emoji（第三轮）**：命令返回、工具返回、注入提示词与状态图标题全部改为中文词（`提示：` / `注意：` / `进行中` / `高（high）` 等）；状态图的"早安/晚安/想念"标记由 ✅/❌ 改为 **是/否**（打包字体无 emoji 字形，顺带避免豆腐块）。`[prompts]` 出厂文案同步清洗（⚠️→`注意：`、💡→`提示：`，句尾结构与占位符原样保留）；**日志与开发者注释不在清洗范围**。
- **移除的配置项**（新界面不再提供；旧值会被自动迁移或忽略）：`[patrol] morning_start / morning_end / night_start / night_end`（问候时间窗）、`min_interval_exempt_greetings`（问候恒豁免最小间隔）、`min_interval_exempt_miss`（想念不豁免）、`proactive_target_daily_cap` / `global_proactive_cap`、`miss_avoid_future_schedule` / `miss_future_schedule_hours`；`[planning_proactive]` 的 `enable_morning_greeting` / `enable_night_greeting`、`greeting_exempt_budget`、`[schedule] auto_generate_time`（降级为 `[schedule_advanced]` 隐藏兼容位）；**以及 9 个隐藏兼容字段**——`greeting_target_enabled` / `greeting_group_enabled` / `greeting_other_private_enabled` / `greeting_group_blacklist` / `greeting_group_whitelist` / `greeting_other_private_whitelist` / `greeting_other_private_blacklist` / `morning_other_private_enabled` / `morning_other_private_min_messages`（**旧命名一律清除**：这些字段运行期已不参与判定，留着只会让用户以为"配了黑名单却没生效"；其中四个旧名单与 `greeting_other_private_enabled` / `morning_other_private_*` 的旧值会在首轮迁移里并入统一名单与门槛；**而 `greeting_target_enabled` / `greeting_group_enabled` 的旧值不保留**（显式 `false` 升级后恢复默认开，要停掉某类问候请改统一名单与两个总开关））。

### 修复

- **`[affection] current_level` 传字符串不再导致整份配置校验失败**：WebUI 表单或 `config.toml` 以字符串 `"0"` / `"1"` / `"2"` 提交时，先归一为整数再校验；`"3"`、`"x"` 等非法值照旧拒绝。（**历史条目**：该键已于 2026-10-17 §19 重构**整体移除**，见上方 [2.0.0] 的「好感度 0~200 重构」子节。）
- **早安漂移延迟失效（B1，真机观察）**：默认静默窗（00:00-07:30）与起床锚点（07:00）重叠，早安漂移点（07:10~07:30）整段落在静默里 → 要么被推迟到 07:30（看起来"没有漂移、每天固定时刻"），要么因第一段日程结束在静默内而直接放弃（"干脆不发"）；晚安则以"最后一段日程结束"为参考做减法，最后一段恰好结束在入睡时间时会在日程中段提前开口。现已按上面的新链重做（静默不拦截、晚安锚定睡前界）。
- **配置为黑名单的聊天流仍触发早安（B2，真机观察）**：两处成因——① 旧的隐藏问候黑白名单字段看起来"配了黑名单"但运行期不读；② 统一名单只认裸号，带前缀/非数字写法被静默忽略（黑名单变空 = 全部放行）。现已彻底移除旧字段、名单支持前缀写法，并让黑名单对**目标私聊**同样生效。
- **升级时旧配置的跨段/改名值被静默丢弃（迁移链两处缺陷）**：
  ① 迁移幂等标记（`[plugin] layout_version`）曾被写进出厂默认配置，而宿主与 SDK 归一化都会先 merge 出厂默认 → 迁移器在真实宿主路径下**从不执行**；
  ② 迁移搬运用"目标键缺失才搬"的判断，但 merge 过默认的输入里所有键都存在 → 跨段旧值（`target_qq`、`timezone`、`miss_silence_hours`、旧名单等）全部回默认。
  现改为「出厂默认不带标记 + 搬运前剔除等于默认的值」，并新增**磁盘旧文件抢救**（`normalize_plugin_config` 在宿主覆写磁盘前读回旧配置），升级不再丢配置。
- **日程空档期注入整段静默（第三轮·真机）**：`current_activity=None` 时 planner 与 replyer 都直接早退（真机 263 轮里 186 轮 `intent=None`、空档期无任何注入）。现在空档期照常走"分类 → 决策 → 模板路由"，当前行/状态行换成 `[prompts] inject_no_activity`（默认「现在没有安排中的活动（可能在休息）。」）；**该键留空 = 回退旧行为**（空档期不注入）。决策仍受注入优化器把关（空档期只放行"问当前 / 问未来 / 上下文延续"，闲聊维持概率与冷却节流）。
- **日程条目被压成 15 分钟 → 大段空档（第三轮·真机）**：`_normalize_items` 对条目结束时间的入睡锚点硬夹，在"入睡钟点早于条目起点"的作息下把每条压成 `起点+15`，并由"校验/评分跑在归一化之前"而**写库前完全看不见**（真机"晚餐 17:30–17:45、下一项 18:30"、质量分仍 0.95 放行）。现在跨午夜环口径写库 + 归一化后复查空档 + 空档重罚。
- **日程截止显示成 11:15（第三轮·真机）**：睡觉/跨午夜条目被起床锚点上钳再加 15 分钟地板（`start = max(起床, 起点)`、`end = max(start+15, …)`），在 `wake_time=11:00` 一类作息下把"01:00 截止"渲染成 `11:00–11:15`。睡觉与睡眠环内条目不再上钳、不再加地板。
- **好感度块全窗口 0 命中 / replyer 注入 2/8（第三轮·真机）**：陪伴与情绪两个 planner 钩子把 `session_id` 收进**具名参数**，回传 `modified_kwargs` 时被整份替换吞掉 → 好感度作用域恒 `skip`、日程注入缓存键退化为全局 `unknown`（不同会话互相盖 300 秒冷却）。现改为纯 `**kwargs` 读取并原样回写；replyer 通道改用**真实意图**（复用同轮 planner 判定，不再写死"闲聊"）、冷却桶按 `{session_id}:replyer` 与 planner 分开，意图分类器补上宿主追加的尾部指令（`当前时间：…` / `时间：…` / `请简短的回复…` / `你需要输出对…发言的分析…` / `【启发式记忆-内部参考】`），不再拿宿主模板当用户问题。
- **跨午夜条目的读取侧渲染/判定不一致（第四轮收尾）**：日程查询（当前活动判定、剩余时间）、日程信息工具（`view=status` 的"正在进行"分类与时间渲染）、`/lbm_plan` 文字版与状态图、注入反查窗口、状态总览图全部统一走 `planning/time_utils` 的日帧助手——`23:00–07:00` 不再显示成 `23:00–31:00`（改标「次日 07:00」），凌晨 00:30 能正确命中"23:00 → 次日 07:00"的睡觉条目、06:50 能看到「约还剩 10 分钟」；空档告警的跨午夜端点同样标「次日」。24h 常量的三份私有副本收拢为 `time_utils.DAY_MINUTES` 单一真源。
- **19 项历史待裁决全部收口（第四轮·用户逐条批复）**：日期帧标记（A-16）、巡检日志独立观察字段（A-17）、常识时间窗折中、午休静默可配置、非目标类问候按流持久化去重、睡眠宣告守卫（R1）、emoji 情绪联动移除（A-7/F1）等，逐项落地口径见计划书 §十七；其中 `evaluate_mode` 改「值≠出厂」判定、提示词回退补 warning 日志。
- **LLM 消耗统计（新功能）**：`/lbm_dashboard` 面板新增「模型消耗」块——最近 24 小时按模型/按模块的 token 消耗（仅 token，无配置项，跨热重载连续）。数据源 = 宿主 LLM 能力返回自带的用量字段；旧宿主未返回时只计调用次数并明示。
- **情绪映射表与情绪描述外置到插件根目录（新功能）**：`emotion_mapping.json`（映射表，优先读，缺失时自动生成内置默认，格式错误回退内置默认）与 `emotion_descriptions.json`（每个情绪的详细文字描述，用户可编辑；格式错误回退默认、映射表外的情绪键丢弃）。均只在插件加载/重载时读取一遍。
- **情绪引导改为详细文字描述（新功能）**：planner 注入用第三人称（"麦麦此刻的情绪…"）、replyer 注入用第二人称（"你此刻的情绪…"），替代原「标签(百分比)+三维档词」；百分比与三维数值退出 LLM 注入面（仅保留在裁判提示词与状态面板展示面）。
- **状态图/面板重绘（新功能）**：打包 NotoSansSC-Bold 真粗体字重（原为描边模拟）；面板新增模块色强调条与头部分隔线，五块布局（新增模型消耗块）。
- **瑞士极简风终版（用户选型 V2+S1）**：`/lbm_dashboard` 面板改为双栏网格（左栏好感/情绪/消耗三枚巨型读数 + 情绪详细描述，右栏 01~05 编号节明细 + 每节命令指路）；`/lbm_plan list` 日程图改为时刻表（巨型日期 + 编号行 + 状态标注 + 命令指路）；行距与字号整体增大；情绪描述注入面板展示。`/lbm_dashboard` 在面板图后**跟随一条「日程与情绪变动」日志文字**（数据源 = 决策记录的总线事件，最近 24 小时、时间升序、最多 98 条，超出保最新并注明省略数；无记录时不发）。
- **修复 `/lbm_dashboard` 在低配服务器上引发系统卡死（热修，2026-10-11）**：根因 = 瑞士风重写时丢失画布高度上限，情绪描述换行输出行数无上限，描述较长时 `Image.new` 按未钳制高度申请 GB 级位图导致 OOM。现已全链路钳制（画布 ≤4096px、换行 ≤12 行且输入预截、各节行数上限、超大串预截断 O(n) 化），并附病态输入回归用例（16 万字描述秒级渲染）；随图「日程与情绪变动」日志同步改为记录有界直读（子串预过滤 + 封顶），不再整文件解析。
- **移除配置键 `[emotion] emoji_enabled`**：emoji 情绪联动自注册面收口起无消费组件，随本版整体移除（服务实现 + manifest 能力声明 17→16）；旧配置里的该键由宿主忽略。
- **新增配置键 `[patrol] nap_quiet_enabled`**（默认关）：开启后陪伴对象处于午休/小睡日程条目（名称含午休/午睡/小憩/打盹，或目标类型为休息）时，巡检整轮跳过主动开口。
- 说明：本版未重复登记 1.0.0 已含的出图与输出目录修复。

### 已知问题

- **帧首条目的「双关」语义需生成侧标记（第四轮登记）**：`start_minute=0` 的条目（如 00:00–01:00）在同一日期帧里既可能指"当天凌晨（已过）"也可能指"当晚午夜（未到）"——夜猫子作息（入睡晚于 00:00）下是后者、常规作息下是前者，读取侧无法从单行数据分辨。现行口径：「接下来要做什么」（`find_next_activities`）按环上帧尾列出且排序最后；日程信息工具的"即将到来/已完成"按帧内单调比较。彻底修法需在生成/写库侧给条目标注所属日期帧（数据格式变更，待裁决）。
- **emoji 清洗范围**：日程生成提示词（`planning/generator/prompt_builder.py`）、`core/inject.py` 的 replyer 提示词、`companion/` 的命令返回与"~❤️"人设后缀仍保留 emoji（第三轮清洗只覆盖命令/工具返回、注入提示词与状态图；人设后缀属语气面，待裁决）。

### 升级须知

- **旧配置首启动自动迁移（幂等）**：插件启动时把旧布局的值搬进新家并落盘——`[target] target_qq → [patrol] target_qq`、`[cateye] * → [patrol] screen_* / [llm] vlm_task / [prompts] screen_*`、`[judge] * → [llm] judge_*`、`[inject] * / [planning_cleanup] * → [schedule_advanced] *`、各模块任务名与一批 `[schedule]` 深水项按映射搬家、`miss_hours_min + miss_hours_max → miss_silence_hours`、旧问候名单/开关 → 统一名单与门槛。迁移只在首轮发生（以 `[plugin] layout_version` 标记），反复重启不会二次改写；段名不变的键原样保留。
- **升级不丢配置（「磁盘旧文件抢救」）**：宿主在配置版本变化时会**先**按新骨架重建设置文件（跨段旧键在插件看到之前就被丢掉）。本版起，插件在归一化入口先读**尚未被覆写**的磁盘 `config.toml`（旧段整段带回、存活段的用户值并回输入），再走一次性迁移器；抢救失败（文件缺失/损坏/无权限）绝不阻塞启动。
- **`[plugin] config_version` = 1.1.0，与插件版本同步**（红线 R6：两者必须一致；改版本号时请同步递增，别只动一个）。
- **数据目录与 1.0.0 相同**（`data/plugins/cateye.life_belonging_to_maimai/`），本版无需搬运数据；升级 1.0.0 → 1.1.0 只需重启/重载插件。
- **静默窗语义变化只影响"留空"的用户**：`[patrol] silence_start` / `silence_end` 默认值由 `"00:00"`/`"07:30"` 变为空串——如果你从未显式填过这两个键（配置文件里是旧默认值），升级后静默窗会自动跟随 `[schedule]` 的入睡~起床；要固定成某个钟点，把它们填成 `HH:MM` 即可。
- **建议重建当天日程**：跨午夜写库修复只对**新生成/新写入**的日程生效；升级后请执行一次 `/lbm_plan regenerate`（或清理当日日程行），把此前被压成 15 分钟的历史条目替换掉。

## [1.0.0] - 2026-10-09

首个正式发布版：四个模块（日程规划 / 虚拟陪伴 / QQ 空间 / 情绪维护）的完整能力面首次落地。

### 新增

**自主规划日程**

- 由大模型按人设生成每日作息日程：wake/sleep 锚点的动态 JSON 示例与衔接演算（示例永远与正文约束自洽）、三餐窗口硬约束、无睡眠模式（睡眠时段不生成条目且禁用相关字眼）、解析控制字符清理、语义校验（只告警）、质量评分（0~1，达阈值提前结束重试）、失败重试带上轮反馈、生成上下文（近 N 天日程摘要 / 跨群聊天历史 / 知识库 / 自定义生活阶段）。
- 日程持久化：本地数据库（日程条目 + 长期目标 + 未来约定 + 元数据，具备写入日志与损坏自愈）、当天已有日程跳过、模板兜底（骨架继承）、每日定时自动生成、过期清理。
- 日程注入对话生成链路：意图分类（问当前 / 问未来 / 闲聊 / 技术 / 命令）、注入优化器（冷却 + 概率 + TTL）、会话续注、能量模型；精神状态行与情绪系统同源（情绪不可得时省略该行）。
- 指令族 `/lbm_plan`（`/lbm_规划`）：list / regenerate / delete / clear / help；日程渲染为图片（内置字体）并以合并转发发送，渲染失败自动降级纯文本。
- LLM 工具：`lbm_get_schedule_info`（只读四视图 status/current/lover_day/goals）与 `lbm_update_schedule_v4`（唯一日程写入口，经角色裁判）。
- 主动行为：活动切换主动开口（去重）、约定到期强指令、频率调控、睡眠窗口内静默。（**后续口径**：活动切换与约定到期主动开口已于 2026-10-20 整体移除，见上文 [2.0.0] 的「主动消息安全治理」子节。）
- 自然语言约定：角色裁判判定 today / future / reject，分别落到今日重排或未来约定。
- **生成锚点相对化**：新一天日程不再按绝对时刻生成，改为"当天最后一个**非睡眠**条目结束后 `auto_generate_after_last_end_minutes`（默认 90）分钟"，无日程时回退 `sleep_time` 锚点；硬约束"不晚于新一天起点前 30 分钟"，超出强制拉回（旧字段 `auto_generate_time` 兼容读取并按绝对时刻理解，两字段同配时新字段优先）。
- **早晚安漂移**：早/晚安只配漂移区间（`morning_drift`/`night_drift`，默认 `10-30`；单数字 `N` 表示 `0~N`，负数或非法值回退默认）。每个（聊天流, 日期, 早安/晚安）独立掷点、同日复用同一结果；早安基准为睡眠窗口结束、晚安基准为当天最后一个非睡眠条目结束；到点按必要性激活（自基准点起该流有人说话才开口，到期限仍无人即放弃）。

**虚拟陪伴**

- **好感度 v2**：连续分 `score`（0~100，初始 30）+ 慢速 EMA 关系底色 `baseline` + 五档关系（生疏 / 熟悉 / 亲密 / 热恋 / 挚爱）；档位升降带迟滞（越界 3 分且停留 ≥24 小时才变档）、日切向底色回归、规则事件记账（一问一答、沉默后对方先开口、早晚安、约定兑现/违约，带每日累计限幅）；单用户档案，旧档案自动迁移（旧 0/1/2 → 25/55/80）。
- **好感 LLM 裁判**：会话采样裁判（每 20 轮采样一次、每日最多 2 次）与关系日结（每日最多 1 次，在睡眠开始、情绪大修之后执行；产出增量与 0~3 条印象）；印象滚动 10 条 / 7 天过期；模型不可用或输出异常时静默跳过，系统按纯规则继续运行。
- **巡检主动聊天完整判定链**：日切 → 静默（fail-safe）→ 用户冷却 → 预算（晚安预留）→ 早晚安状态检查 → 想念（沉默阈值随机采样 + 概率 + 模型把关 + 驳回冷却 + 未来日程避让）→ 日程节点分享 / 日常搭话。
- **主动打扰预算**：每日上限 `patrol.proactive_target_daily_cap`（默认 0 = 自动取 `patrol.daily_max_speak + 2`），达上限的触发被拦截并留痕到决策日志；计数按日落盘，跨热重载与重启连续（见「修复」节）。
- **情绪消费**：巡检读取情绪系统的纯规则系数——低落时降低主动开口概率、更快想念、主动理由附带低分辨率情绪语义；情绪模块停用或异常时各项系数恒为 1.0（行为与旧版一致）。
- **周结大事记（默认关）**：开启后周日的日结按纯规则汇总最近 7 天关系温度与净变化，写入档案并在好感记忆行追加一行【本周大事记】。
- 屏幕感知「尾随锚点」注入（对话生成双通道）+ 陪伴对象电脑联动（截屏 + 视觉模型描述）。
- 节假日服务（多级接口降级）。
- 指令与总览：`/lbm_mai_affection`、`/lbm_mai_help`、`/lbm_diag`（排障三合一）、`/lbm_dashboard`（聚合总览，见下）。

**QQ 空间**

- 按日程自动发说说（静默时段 → 活动判定 → 概率 → 每日上限 → 模型文案），发布双通道：适配器能力优先、网页接口直连回退，cookie 多级降级获取。
- 自动回评：读评论（含楼中楼）→ 过滤自己与已处理（去重落盘）→ 模型回复（注入关系印象）→ 楼中楼回评并立即标记；cookie 不可用时降级为"评论数基线 + 提醒"。
- 手动命令 `/lbm_life`：status / qzone（moments、comments、post、reply、like）/ diag / help——列评论、按"说说序号[.评论序号]"回复（含楼中楼）、点赞。
- 情绪联动：发帖概率随情绪调整；效价过低且开启 `qzone.emotion_filter`（默认开）时本轮不发说说（避免低谷硬营业），说说与回评文案附一行情绪底色。

**情绪维护系统**

- VAD 三维外部状态机：模型只输出"事件→增量"，状态机维护绝对值（日常指数衰减 → 加增量 → 静态偏移 → 乘数 → 裁剪）。
- 25 种情绪原型加权距离映射与复合标签，低分辨率三档描述（用于注入，模糊化反馈防漂移）。
- 回复后增量评估（默认**会话片段制**）：回复时只登记会话片段（零模型调用），会话闲置后把整段往来交给小模型转成增量；可切换为"每回复评估"或关闭，两种模式共用每日次数硬上限。
- **外部事件桥**：沉默、约定兑现/违约、空间收到评论等事件统一入口记账；`target`（陪伴对象）识别与三层加算倍率；群聊经工具 `lbm_emotion_report(event, is_target)` 上报，参数永不拒绝、非法值静默按非 target 处理。
- **睡眠结算改造 + 睡眠大修 v2**：进入作息夜间窗口后冻结整夜（事件只入队、衰减与评估暂停）；睡眠裁判结合当前状态、当天日志、前后日日程、长期状态、睡眠时长、前日残差/明日预期与当日约定净兑现计算并缓存，醒沿一次性应用并解冻；应用幅度动态限幅、向锚点收敛且恢复系数恒 ≥0.3。
- **睡醒情绪 → 次日日程**：睡醒情绪描述与引导约束按 `schedule.emotion_influence`（0~1，默认 0.6，0 = 关闭）进入日程生成的 `emotion_context` 槽；日程条目可带 `emotional_tone` 供睡眠大修使用。
- **连续低迷软约束**：连续多日日均情绪低于阈值时发布事件，日程生成强制插入一次休整/社交活动，并在提示词中注明"最近有点低落"。
- **睡眠期打扰记账**：目标私聊内连续回复达阈值即认定"被吵醒"（默认 3 次回复 / 10 分钟窗口 / 回睡静默 5 分钟），按扰段区间并集计算实际睡着时长并计入恢复系数；整夜合计净正增量在醒沿一次性入账（零模型成本）；深夜暂停期注入语气引导行。
- **赖床提醒与专用工具**：睡眠期内到点发一轮赖床提醒（**每个睡眠周期**上限 `nap_reminder_max_per_day`，非自然日，默认 2；文本点名工具、不强制调用）；工具 `lbm_nap_lie_in` 把当天起床时间往后推（受 `[schedule] lie_in_max_until` 上限钳制，默认 12:00），非睡眠期调用被拒绝。
- **情绪映射 JSON 覆盖层**：可选数据文件 `<data_dir>/emotion_mapping.json` 可覆盖情绪原型表、事件增量表、三层倍率与五档阈值；**整文件原子生效**——JSON 解析失败或任一键校验失败即全量回退代码默认并告警（合法与回退都落决策日志）。生效优先级为 **JSON 覆盖层 > 配置项 > 代码默认**；改动该文件后需触发一次热重载（或重启）才生效。映射算法、注入文案与限幅/衰减/睡眠参数不放开（仍由配置与代码决定）。
- 人设初始化 `/lbm_emotion_init`（由宿主人设推导锚点/偏移/乘数，失败可重试）、`/lbm_emotion_config`（查看 / reset / clear）。
- 排障诊断 `/lbm_diag`：llm / decision / life 三个子命令合一，管理员鉴权。

**聚合总览**

- `/lbm_dashboard`：一条命令汇总四个模块（日程要点与当前活动、好感与关系状态、情绪与锚点偏移、空间双通道与发帖额度）并渲染为一张状态图片；渲染失败降级为纯文本（文本档展开完整日程并附配置摘要）。

### 变更

**1. 插件标识**

- 插件注册 id：`cateye.life_belonging_to_maimai`；插件显示名：**属于麦麦的生活**。
- 工具名前缀 `lbm_`，指令名前缀 `/lbm_`（含 `/lbm_规划` 等中文别名）；保留的别名均为**新前缀下的旧短名**（如 `/lbm_mai_llm_log`、`/lbm_life diag`）。
- 数据目录：`data/plugins/cateye.life_belonging_to_maimai/`。
- 仓库与文档链接：`https://github.com/cateyemizuki/cateye_life_belonging_to_maimai`。

**2. 早晚安问候改为按会话类型分派**

- 新增三个独立开关：`greeting_target_enabled`（目标陪伴私聊，默认开，**必定触发、豁免名单**）、`greeting_group_enabled`（群聊，默认开，受问候专用黑白名单与必要性激活限制，两名单留空 = 全群生效）、`greeting_other_private_enabled`（非目标私聊，默认关，白名单留空 = 全不发，且前一日该流消息数需 ≥ `greeting_other_private_min_messages`，默认 5，配置校验不允许低于 5）。
- 问候名单只接受裸群号 / 裸 QQ 号（带前缀、含非数字、空串、重复项会被忽略并记一条汇总告警；白名单 `["*"]` 表示不过滤；`*` 与具体号混用按全放行处理）。
- **例行问候不再占用主动打扰预算**：`greeting_exempt_budget`（默认开）下，早/晚安既不查预算闸门也不计用量；想念、日常搭话、活动切换、赖床提醒等仍受预算约束。

**3. 主动打扰预算收窄为"仅目标陪伴私聊"**

- 计数只统计发往目标私聊的主动打扰：巡检计入、活动切换仅目标私聊计入、空间评论提醒不计入、赖床提醒计入。
- 拦截日志与提示文案同步改为"主动打扰（仅目标陪伴私聊）"（不再有"全渠道"的说法）；上限字段更名见下表。

**4. 排障与状态命令收敛**

- LLM 日志、决策日志、生命状态三条查看命令合并为 `/lbm_diag`（llm / decision / life 子命令），旧名保留为别名。
- 旧的状态 / 日程 / 配置三条查看命令被 `/lbm_dashboard` 吸收（状态、日程、配置实时值成为总览的区块），旧名不再单独应答（按普通消息处理）。
- 组件注册面：29 个（8 指令 + 10 Hook + 7 工具 + 4 跨插件 API）。

**5. 情绪评估默认改为会话片段制**

- 默认不再"每回复评估一次"，改为会话闲置后整段评估（每日次数硬上限不变），显著降低日常模型调用量；可在 `[emotion] evaluate_mode` 切回旧口径。

除以上各项外，本版不改功能语义与数值口径。

### 修复

- **主动打扰预算计数不再被热重载清零**：计数按日落盘、启动时装载、日切重置；插件文件变更触发的热重载不再把当日用量清空（此前会因此同日超发、并被误判为预算失效）。
- **睡眠结算与醒沿口径修正**：冻结窗口改按作息锚点整夜计算（原先按日程条目会被归一化截断，导致入睡约十五分钟后误醒、恢复系数恒为下限）；"明日预期"与当日约定改在**醒来侧**重算（次日日程在后半夜才生成，按入睡侧取会取不到值）；恢复系数保证恒 ≥0.3。
- **睡眠结算链路顺序修复**：醒沿改为"先冲刷会话片段 → 先解冻再结算"，使睡眠期间发生的事件进入裁判视野（此前夜间事件既不入队也不进评估，等于丢失）。
- **醒沿护栏被后续落盘抹除**：睡眠状态的"本周期已结算"标记在醒沿写入后会被后续保存覆盖而消失，导致同一夜热重载后可能重复结算；现已保留合并该标记。
- **补记：结算过程曾冲掉同一护栏**：结算入账时的保存会覆盖醒沿刚写入的已结算标记，已改为"结算内先留后补"并固定该行为。
- **同日重复早安**：热重载后同一天可能二次送出早安——原因是发送判定只认内存状态、不查已落盘的"今日已问候"标记；现在发送前补查该标记并留痕去重。当前覆盖**目标私聊**的早/晚安（见「已知问题」）。
- **赖床延长后起床时间不随之顺延**：把起床时间往后推后，与延长窗口重叠的当日清醒条目会抢先命中"已醒"判定，使醒沿仍按原锚点触发；现在写回时同步顺延 / 移出这些条目（跨午夜时同时改写醒来日日程）。
- **晚安激活窗口倒置**：群聊晚安的激活观察窗口改为"从计划问候时刻到当前"，与早安的"从基准点到当前"口径区分，修复了晚安窗口取错导致的漏发/误发。
- **关系日评的印象字段解析容错**：日结返回的 `impressions` 结构异常（标量、混类型、超长等）不再导致整次日评失败，按容错规则归一后写入；装载、产出、写入与读取四处均有防护。
- **模型 JSON 输出的前导 `+` 兜底**：部分模型会把数值写成 `+0.04`（严格 JSON 非法），原先解析直接失败，睡眠期反复重试白耗调用；现统一在三处宽松解析入口剥离前导 `+`（字符串内部与指数形式不受影响）。
- **旧状态文件兼容读取**：好感档案 v1 → v2 自动迁移；情绪状态、"连续低迷"统计、周结等新增字段在旧文件中缺失时按默认值读取，不报错。
- **修复日程图片无法生成**：日程渲染的表头绘制引用了未定义的图像符号名，导致出图步骤每次都在内部报错并被静默降级为纯文字——`/lbm_plan` 的日程图片在**此前所有版本**实际都不可用。现已修复，可正常出图。

### 已知问题

以下问题在 1.0.0 中已知，但**不会崩溃、不会损坏状态文件**，部分问题将在后续版本处理：

1. **睡眠期热重载 / 重启的"锚点整夜口径"边界**：若热重载或重启发生在睡眠期内，同一夜可能出现双份起床结算（内存观测时长与"窗口 − 扰段"两种口径）、当夜已确认的打扰归零、已结算周期的重复宣告不冻结、以及挪动锚点叠加重载产生两个周期标识等情形；危害有界。其中三处口径（重入口径、是否禁止对"已结算周期"再次宣告、周期标识的取值时序）将在后续版本统一处理。
2. **日程生成侧尚未设置上界**：日程生成的 `start_minute` 只设了下界，夜窗内的清醒条目仍可能经由正常生成流程入库（赖床写回路径已修复，但日程生成侧的入参校验仍未补上界）。
3. **非目标类问候的跨热重载去重未实现**：目前只有**目标私聊**的早安/晚安带持久化标记、可在热重载/重启后去重；群聊与非目标私聊的跨重载去重尚未实现（如需支持，须按聊天流持久化该状态）。
4. **宿主级复验尚未完成**：同日重复问候的去重、赖床醒沿顺延与午休跨午夜倒挂三项仅在离线环境验证过，真实宿主上的复验将在后续版本进行。
# 2026-11-01 小主机真实媒体复测

- 修复多分P把合集总时长作为单页采样范围导致越界；分P信息、CID和时长同源，缓存画面能力版本升级。
- 显式非默认分P与多媒体分段如实拒绝/降级，缺失字幕不声称已读；单页旧输入仍兼容。

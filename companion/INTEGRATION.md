# companion/ — 虚拟恋人子模块（复刻自 maibot-community_mai-love v2.6.3）

> 来源：`maibot-community.mai-love`（原作者 octmicy，cateye 维护 fork，MIT）。
> **日程生成不复刻**：mai-love 的日程系统由 planning/ 子模块提供，本模块只消费
> `planning.schedule_query.ScheduleQuery`（当前活动 / 未来活动 / 全天摘要）。

## 模块地图

| 文件 | 职责 | 对应源模块 |
|---|---|---|
| `config.py` | patrol / affection / cateye / judge 四个配置段（whitelist→顶层 target 段；llm_log/proactive_log→顶层 admin 段；**新增 relation_type/relation_custom 关系配置**——miss 提示词占位 {relation}、消息口吻/后缀/档位名随关系变化，热更新经 sync_from_config；**affection 段 M1 扩为 v2**：enabled/decay_pull/baseline_ema/tier_hysteresis/tier_min_dwell_hours/daily_positive_cap/daily_negative_cap，**M2 起 review_enabled/review_sample_every_rounds/review_max_per_day/review_task_name 生效**；emotion_modulation 属 M3、**已接线**：好感 delta × clamp(1+0.25×V×sign(delta), 0.6, 1.4)） | config.py |
| `affection.py` | 好感度 v2 状态层：score/baseline/5 档迟滞/日累计/事件日志/滚动印象/关系温度 + v1→v2 迁移 + v1 字段原地保留（`level()` 保持 0/1/2 兼容口径）+ 档位变化发布 `relation.affection_tier_changed`；1 秒节流写盘 + flush；**M3 增 `emotion_modulation_enabled()` 与 `emotion_valence()`（`set_emotion_vad_provider` 注入，可空降级）** | affection_manager.py（M1 重写） |
| `affection_events.py` | 好感规则事件层（M1 规则表 + M2 裁判 kind，零 LLM 成本）：`RULE_EVENT_TABLE` + 频控 + 日累计限幅 + `note_user_message`；`llm_review`/`daily_review` 由 M2 裁判传入 delta（仅受日累计限幅）；**M3 情绪调制生效**：入账前 delta × clamp(1+0.25×V×sign(delta), 0.6, 1.4)（开关关/读不到 V → 恒 1.0） | 新增（MAINTENANCE_PLAN §5.4） |
| `affection_referee.py` | **好感 LLM 裁判（M2）**：会话采样裁判 `evaluate_conversation_delta`（delta ∈ [-2,+2.5] + tag）+ 关系日结 `run_daily_review`（delta ∈ [-2,+2] + 0~3 条印象 + tone + reason）+ `AffectionReviewer` 编排（双频控、后台任务、sleep_started 订阅、印象 10 条/7 天、失败静默）；prompt 不含 score/baseline | 新增（MAINTENANCE_PLAN §5.4） |
| `relation.py` | **关系系统（简单版）**：relation_type/relation_custom 配置 → RelationProfile（称谓、三档档位名 + **v2 5 档标签/描述**、口吻、后缀池、括号小剧场、档位 miss 系数表），patrol/message_service/components 消费；3 档兼容映射经 `affection.legacy_level_for_tier` 转发 | 关系扩展 |
| `patrol.py` | 巡检调度器完整判定链：日切→静默(fail-safe)→用户冷却→预算(晚安预留)→早安(S)→晚安(S)→想念(A：随机阈值+概率+LLM 把关+驳回冷却+未来日程避让+豁免开关)→日程节点分享(B)→日常搭话(B)；决策日志每轮恰好一条（SKIP_PRIORITY）；触发经 maisaka.proactive.trigger + 计 0.5 发言数；**M3 在 A 级段发布 `relation.silence_detected`（每日 ≤2/间隔 ≥4h/仅 target，独立于想念判定）**；**M4 三处消费 emotion `mood_effect()`：概率乘数 / miss 双系数 / reason 追加 intent_hint（见下）** | scheduler.py |
| `message_service.py` | 好感度后缀 AFFECTION_SUFFIXES + 档位2 括号小剧场（10%）+ send_to_target | message_service.py |
| `memory_manager.py` | 用户习惯读写（委托 AffectionManager.habits）+ 个人上下文构建 | memory_manager.py |
| `sender_identity.py` | SenderCache（msg_id→QQ，TTL 24h）+ SessionKindCache（会话→是否群聊）+ 提取助手 | sender_identity.py |
| `screen_context.py` | 屏幕旁白「尾随锚点」注入：ScreenContextStore（planner/replyer 双通道、锚点滑出作废、容量淘汰） | screen_context.py |
| `cateye_client.py` | 恋人电脑联动：connect-hub 跨插件 API（status/screenshot）+ VLM 描述 + 超时与缓存 | cateye_client.py + llm_service.describe_image |
| `holiday_service.py` | 节假日多级降级：timor 整年表 → jiejiariapi → timor 按天 → 本地星期几；3 天 TTL 原子落盘 | holiday_service.py |
| `components.py` | `CompanionComponentsMixin`：5 Hook + 6 Tool + 8 Command + 3 API（方法名 `mai_` 前缀；API 组件名加 `companion_` 前缀避免与 planning 撞名） | plugin.py 组件面 |
| `services.py` | `CompanionServices` 服务束（复用顶层 _affection/_llm/_loggers；`set_persona`/`set_target`/`sync_from_config` 入口；**M2 构造并持有 `reviews`（`AffectionReviewer`）**；**M3 装配期注入情绪效价提供者 `set_emotion_vad_provider`（惰性读 `plugin._emotion`，可空降级）**） | plugin.py 装配段 |

## 集成方式（已在 plugin.py 完成）

```python
class CateyeSuitePlugin(CompanionComponentsMixin, ...):
self._companion = CompanionServices(self)   # 复用 plugin._data_dir/_affection/_llm/_loggers/_schedule_query
await self._companion.start()               # 日志清理 + 节假日预热 + 巡检（stream 已解析时）
await self._companion.stop()
# 目标会话解析后：self._companion.set_target(qq, stream_id)
# bot 配置热重载：self._companion.set_persona(lover_name, personality) / sync_from_config()
```

## 好感度 v2（M1 落地，2026-10-03）

**状态层 `companion/affection.py`（`AffectionManager`）**
- 状态：`score`（0~100，初始 30）/ `baseline`（慢速 EMA 关系底色）/ `tier`（恋人 5 档，含迟滞结果）/ `tier_entered_at` / `daily_delta` / `event_log`（上限 300 条，M2 日结裁判输入）；档案仍为 `affection_memory.json`（写回即 version=2），单用户（只服务 target_qq，多用户扩展已裁撤）。
- 读 API：`score()` / `baseline()` / `tier()` / `tier_label()` / `tier_description()` / `tier_entered_at()` / `daily_delta()` / `event_log()`；v1 兼容口径 `level()` / `level_label()`（`TIER_TO_LEGACY_LEVEL`：0/1→旧0、2/3→旧1、4→旧2），后缀池/状态报告/`companion_get_affection_level` 零改动沿用旧口径。
- 写 API：`update_level(0/1/2)`（v1 兼容入口：score 移到 30.0/57.5/94.0、强制重估并跳过迟滞）、`set_tier()`（各档区间中值）、`set_score()`、`reevaluate_tier()`（迟滞：升档需 ≥ 上界+`tier_hysteresis`(3.0)、降档需 ≤ 下界−3.0，且距进入当前档 ≥ `tier_min_dwell_hours`(24h)）、`commit_rule_delta()`、`append_event_entry()`、`apply_daily_decay()`（score += (baseline−score)×`decay_pull`(0.06)，baseline 随后 += (score−baseline)×`baseline_ema`(0.03)）、`sync_daily()`（事件早于巡检到达时的跨天兜底，含一次日衰减）、`reset_daily()`；v1 预算方法（`increment_speak()` 等）语义不变、与好感分无关。
- 迁移：无 `version` 的非空档案按 v1 迁移（`score = {0:25,1:55,2:80}`、`baseline=score`、`tier=level`）并写回；JSON 损坏按全新档案重建并告警。档位变化发布 `relation.affection_tier_changed`（old/new/score/reason，总线缺失静默降级），关键动作留痕 `decision_logs`。

**规则事件层 `companion/affection_events.py`**（零 LLM 成本）
- 统一入口 `record_rule_event(affection, kind, delta=None, desc="", *, caps=None, key="")`（`AffectionManager.record_rule_event` 同名转发）：频控 → 日累计限幅 → 入账 → 迟滞重估 → event_log/决策日志。
- M1 表 `RULE_EVENT_TABLE`：`chat_round` +0.05（日 ≤ +1.0）、`user_initiated` +0.15（沉默 ≥1h 后对方先开口，日 ≤ +0.5）、`morning_exchange`/`night_exchange` 各 +0.2（1 次/日）、`commitment_kept` +1.0 / `commitment_broken` −0.5（每约定 1 次，`key` 去重）；M2 追加 `llm_review` / `daily_review`（delta 由裁判传入并已裁剪，仅受日累计限幅）。日累计 positive ≤ +4.0 / negative ≥ −3.0。未登记 kind 按 `unknown_kind` 丢弃并告警（不静默扩表）。
- `note_user_message()` 由 `update_last_user_msg_time()` 顺带调用；情绪调制（M3 接线）：入账前 delta × clamp(1 + 0.25 × V × sign(delta), 0.6, 1.4)，乘数恒 1.0 的降级路径见下节。

**接线挂点**

| 挂点 | 位置 | 行为 |
|---|---|---|
| 日衰减 | `patrol.py` 巡检日切分支（`_tick`） | `reset_daily()` 后调 `apply_daily_decay()`，每自然日恰好一次 |
| 对话事件 | `components.py` `chat.receive.after_process` | target 私聊记 `chat_round` + 刷新 `last_user_msg_time`（触发 `user_initiated` 检测）；同时发 `relation.message_received` |
| planner 注入 | `components.py` `_mai_build_activity_suffix` | 好感行改读 `tier()` + `relation.tier_label()/tier_description()`（5 档口径） |
| 约定订阅 | `services.py` `_register_event_subscriptions`（`start()` 注册 / `stop()` 反订阅） | `schedule.commitment_kept` / `schedule.commitment_broken` → `record_rule_event`，去重键取 `commitment`（回退 `summary`） |
| Tool/命令 | `components.py` | `cateye_test_mai_lover_affection` 与 `/cateye_test_mai_affection` 走 `update_level()`（3 档兼容、跳过迟滞）；状态行输出 score/baseline/5 档档位/今日 Δ（M2 追加最近 1~2 条印象 + 关系温度） |

## 好感 LLM 裁判（M2 落地，2026-10-03）

**裁判层 `companion/affection_referee.py`**（镜像 `emotion/referee.py` 结构；LLM 全部经 `core/llm_utils.LLMService.generate`，`event=affection_review` / `affection_daily_review`，任务名取 `[affection] review_task_name`）

- **会话采样裁判** `evaluate_conversation_delta()`：输入 = 最近 ~10 条对方消息 + 双方称呼与人设 + 关系档位描述 + 当前情绪低分辨率描述（`emotion.vad.mood_summary`，无 VAD 数值）；输出 `{"event"≤30字, "delta"[-2,+2.5], "tag"}`（tag ∈ normal/conflict/apology/sweet，未知回退 normal）；失败/垃圾输出返回 None。
- **关系日结裁判** `run_daily_review()`：输入 = 当日 event_log + 互动统计（轮数/沉默后对方先开口/bot 主动发言/沉默时长/约定；取不到的字段省略）+ 最近印象 + 档位描述 + 人设；输出 `{"delta_score"[-2,+2], "impressions"0~3条, "tone", "reason"}`（reason 要求引用原文；代码层打日期、裁 3 条）。
- **编排器** `AffectionReviewer`：会话采样双频控（每 `review_sample_every_rounds` 轮 + 每日 ≤ `review_max_per_day`，计数持久化在档案 `review` 段），LLM 请求放后台任务（不阻塞 1s 超时的 chat Hook）；日结订阅 `schedule.sleep_started`，arm 后台任务并等 `overhaul_grace_seconds`(90s) 后执行——因为 `emotion/service.py` 是「先 publish、返回后才跑睡眠大修」，注册顺序无法保证晚于大修，必须延迟；`review_enabled=false` 两个裁判整体关闭。
- **编排器 API**：`note_target_message(text, now=)` → `{sampled, reason, rounds, samples}`（采样入口；reason ∈ rounds/scheduled/daily_cap/disabled/affection_disabled/no_loop）；`on_sleep_started(payload)`（订阅入口，只 arm 延迟任务）；`perform_daily_review(date=)` → `{ok, delta_score, applied, impressions, tone, reason, event_log_cleared}`（可独立单测的日结执行体）；`close()`（服务束 `stop()` 取消所有未完成后台任务，含采样请求与延迟日结）。
- **结果应用**：delta 走 `record_rule_event` 既有入账入口（`llm_review`/`daily_review` kind，独立于 kind 上限、受 §5.5 日累计 +4/−3 限幅）→ 追加 impressions（单次 ≤3 条、每条 ≤80 字、日期由代码打上；追加时按 7 天窗口剔除旧条目并滚动保留最新 10 条，落盘在 `affection_memory.json` 的 `impressions` 字段）→ 写 `tone`（读取时按同一 7 天窗口过滤）→ 清空当日 `event_log`；失败时不清 log、不落印象、不记 `daily_reviewed_date`（下个 sleep 边沿可重试）。
- **注入叠加（§5.6.1）**：`components.py` 好感后缀行后追加最近 1~2 条印象 + 最近一次日结关系温度（tone 超 7 天不注入）；无印象/无 tone 时输出与 M1 完全一致。

**M2 接线挂点**

| 挂点 | 位置 | 行为 |
|---|---|---|
| 会话采样触发 | `components.py` `chat.receive.after_process`（chat_round 记账点旁） | `_mai_note_review_round()` → `reviewer.note_target_message()`（滚动缓冲 + 轮次/每日双频控，命中采样只调度后台任务） |
| 日结订阅 | `services.py` `_register_event_subscriptions`（`start()` 注册 / `stop()` 反订阅 + `reviews.close()`） | `schedule.sleep_started` → `reviewer.on_sleep_started()`（延迟 ≥90s 后日结，晚于情绪睡眠大修） |
| 印象/tone 注入 | `components.py` `_mai_build_activity_suffix` / `_mai_affection_state_line` | 好感行后叠加「【最近印象】…；【关系温度·日期】…」，无数据降级现状 |

## M3 落地（2026-10-03）：沉默事件发布 + 情绪调制接线

**沉默事件发布（B1 输入端，`patrol.py`）**
- `_tick` 的 A 级判定段（想念判定之前）调用 `_maybe_publish_silence(now)`：`last_user_msg_time` 超过 `miss_hours_min` → `bus.publish("relation.silence_detected", hours, target_bonus)`；只发布事件，不参与想念触发判定，异常静默吞掉（绝不影响判定链）；
- 频控（内存态、跨天清零）：每日 ≤2 次、间隔 ≥4h（`SILENCE_EVENT_MAX_PER_DAY` / `SILENCE_EVENT_MIN_INTERVAL_HOURS`）；**仅 target**——`has_target()` 为假不发布；
- payload `target_bonus` 仅留痕（= `[emotion].target_bonus_step × (档位+1)`；档位读 affection，任一不可得返回 0 不猜）；emotion 侧桥按同一公式重算 K（单一事实来源仍是档位 + 配置）。

**情绪调制好感（B2，`affection_events.py` + `affection.py` + `services.py`）**
- 规则事件入账前对 delta 乘 `clamp(1 + 0.25 × V × sign(delta), 0.6, 1.4)`（`_EMOTION_MULTIPLIER` 基准 1.0、k=0.25）；调制在频控 / kind 日上限 / 日累计限幅之前应用，后续限幅按调制后的值计算；`requested_raw`/`multiplier`/`emotion_valence` 落决策日志；
- V 来源：`AffectionManager.emotion_valence()`（clamp ±1）← 装配期 `set_emotion_vad_provider(lambda: _emotion_valence(plugin))`（`services.py`，惰性解析，因 `plugin._emotion` 在 companion 服务束之后装配）；情绪模块缺失/停用/异常 → None → 乘数 1.0；
- 开关 `[affection] emotion_modulation`（默认 true，`emotion_modulation_enabled()`）：false、情绪模块停用或读不到 V 时乘数恒 1.0（M1 占位语义的降级路径，§八.2 可空降级）。

**M3 接线挂点**

| 挂点 | 位置 | 行为 |
|---|---|---|
| 沉默发布 | `patrol.py` `_maybe_publish_silence`（A 级判定段调用） | 频控通过 → 发 `relation.silence_detected`（hours / target_bonus） |
| 情绪 V 注入 | `services.py` 装配段 | `affection.set_emotion_vad_provider(...)` 惰性读 `plugin._emotion.effective_valence()`；停用返回 None |
| 调制应用 | `affection_events.py` `record_rule_event` | delta → 乘调制系数 → 频控/限幅 → 入账 + 决策日志 |

## M4 落地（2026-10-03）：情绪 → 巡检行为输出（patrol 三处消费点）

`patrol.py` `_tick` 掷骰前读 `plugin._emotion.mood_effect()`（跨模块 getattr 判空 + 恒等降级，§八.2：emotion 停用/缺失/异常时全部乘数恒 1.0、reason 原样透传）：

| # | 消费点 | 位置 | 行为 |
|---|---|---|---|
| 1 | 概率乘数 | `_tick` 概率段（掷骰前） | `activity_trigger_rate`/`default_speak_rate` × `proactive_rate`（clamp [0.7,1.3]）；乘后概率与 `mood_proactive_rate` 落决策日志 |
| 2 | miss 双系数 | `_sample_miss_threshold(mood_mult, tier_mult)` | 区间随机阈值 × `mood_effect.miss_threshold`（情绪）× `RelationProfile.miss_factor_for_tier(tier)`（档位；恋人档 0~4 = 1.4~0.6）；留痕 `miss_threshold_hours`/`mood_miss_threshold`/`tier_miss_factor`；任一不可得恒 1.0 |
| 3 | intent_hint | `_trigger_planner` → `_append_intent_hint(reason)` | proactive intent 的 reason 追加低分辨成语义行（如「你今天有点低落，语气可以软一点」）；中性空串时 reason 字节不变，零新 Hook |

三处均不新增 Hook；`NEUTRAL_MOOD_EFFECT` + `_mood_multiplier` 对上游值做防御夹取。

## 有意的裁剪 / TODO

- 源插件的 `schedule_generator.py` / `mai_template.json` / `external_schedule.py`（跨插件读日程 API）不复刻——日程由 planning/ 提供；
- 源插件 5 个 Hook 中 `maisaka.replyer.after_response` 的补计数窗口（90s）与 planner 注入的载荷收缩（>8MB 换图片占位）均已保留；
- 决策日志 / LLM 日志合并进 `core/diag.py`（record 既有签名兼容，新增 read_entries/cleanup/update_settings）。

## 上游同步记录（v2.6.2 / v2.6.3，2026-10-03 合并）

| 上游变更 | 本仓库落点 |
|---|---|
| v2.6.2 `LLMService.generate` 失败三态分诊（空正文附 reasoning 证据 / 裸失败 / 非 dict） | `core/llm_utils.py` `_interpret()`（原"未知错误"兜底移除） |
| v2.6.2 `miss_confirm` max_tokens 16 → 512 | 随 v2.6.3 `[judge]` 板块落地（默认 512），`patrol.py` 改走 `judge()` |
| v2.6.2 过期宿主主动任务块剥离（planner + replyer 请求载荷，30 分钟阈值，宁多勿丢） | `core/inject.py`（`without_stale_proactive_blocks` 纯函数）+ `companion/components.py` 两个 Hook 最前调用 |
| v2.6.3 `[judge]` 配置板块（model / max_tokens / temperature，热更新即时生效） | `companion/config.py` `JudgeSectionConfig` → 顶层 `config.py` 挂为 `[judge]` 段（字段校验按本仓库 ge/le 惯例，越界由 pydantic 拒绝而非静默夹取） |
| v2.6.3 `LLMService.judge()` + `generate()` 可选 model 覆盖 | `core/llm_utils.py`：`model` 覆盖只传 model 不传 task_name（宿主按「先任务名、未命中按具体模型名」双解析，见开发文档 §8.3 路由规则）；配置经 `config_provider` 回调动态读 |

部署提示：同实例装有 `cateye_mai_set_avatar` 时须 ≥1.0.2，否则其 `meta=None` 坏条目会让宿主丢弃 `planner.before_request` 链上所有插件的载荷修改（含本版的任务块剥离与旁白注入）。

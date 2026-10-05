# emotion/ — 情绪维护系统子模块（按《MaiBot情绪维护系统设计文档.md》实现）

> 来源：`D:\deepseek-harness_workspace\maibot-plugins\MaiBot情绪维护系统设计文档.md`
>（VAD 三维情绪模型 + 日常增量微调 + 睡眠期大修 Overhaul + 人设初始化）。
> 与其他两个复刻模块同构：独立总开关（`emotion.enabled`）、服务束、组件 Mixin、cateye_test 前缀组件名。

## 模块地图

| 文件 | 职责 | 设计文档章节 |
|---|---|---|
| `config.py` | emotion 配置段（总开关、衰减率、评估开关与节流、裁判/评估任务名、注入开关、睡眠检测间隔；**M3 增三项事件倍率**：global_event_multiplier/non_target_multiplier/target_bonus_step，一律标量；**A3 增跨来源去重两项**：`dedup_enabled`/`dedup_window_seconds`，默认 true/180s） | §十二 |
| `vad.py` | VADState/EmotionEvent 数据结构；25 种情绪原型（10 正/3 中/12 负）；加权欧氏距离映射（V1.0/A0.8/D1.2）+ 阈值复合标签 + 低分辨率三档描述 | §一/§四/§七 |
| `state_machine.py` | 外部状态机：日常指数衰减、事件结算（衰减→增量→裁剪→日志）、静态偏移+乘数（effective_vad，避免偏移每轮累加）、睡眠大修（锚点收敛 × 倒 U 恢复系数[恒 ≥0.3] + 裁判增量 + R_yesterday/E_tomorrow/约定三项数值）、参数生效/恢复默认/序列化；**M5 增冻结/解冻**（`freeze`/`unfreeze`：冻结期事件只入队 `pending_events`、衰减与结算暂停；解冻推进 `last_update`，不补算整夜） | §二/§五/§九/§十 |
| `referee.py` | 三个独立 LLM 请求：睡眠裁判（**M5 起动态限幅 ±(0.4+0.4×`schedule.emotion_influence`)，缺省兼容 ±0.8** + R/E/约定三组数值输入 + reason 须引用日程原文）、人设初始化（锚点 ±0.6/偏移 ±0.3/增益 [0.8,1.3]/衰减 [0.6,1.2] 校验裁剪）、回复增量评估（±0.5）；宽松 JSON 解析 | §六/§十 |
| `service.py` | 状态持久化（emotion_state.json / emotion_config.json）、睡眠检测循环（消费 planning 日程的 sleep 条目，跨午夜时长计算）、前后日日程与长期状态（planning 的 custom_prompt）汇总、初始化指令主体、隐私接口（清空日志）；**M3 增 `record_external_event` 统一外部事件入口 + target 识别/三层加算倍率 + 评估上下文反查（get_by_time_in_chat）**；**M4 增 `mood_effect()` 情绪→行为系数（附录 C 映射，纯规则）**；**M5 改睡眠循环为冻结→醒后结算**（入睡冻结 + 冻结期计算并缓存大修 → 醒沿应用一次并解冻；`is_frozen()`/`sleep_delta_limit()`/`schedule_tone_sum()`/`compute_sleep_overhaul()`/`_apply_cached_overhaul()`；`applied_sleep_date` 记睡眠周期键，旧裸日期值兼容读取）；**冻结窗口按 [schedule] 作息锚点整夜**（条目会被 `_normalize_items` 截断为 23:00–23:15，不能作窗口；午休按条目窗口），计划时长取整夜 8h；R/E/约定在醒来时按入睡日基准重算（次日日程 00:30 才生成）；**M5 连续低迷**：每 tick 采样**清醒时段** effective V（冻结期跳过）→ 日均线（滚动 7 天）→ **日历逐日**判定连续「日均 V<−0.5」（无采样日归零）→ 满 3 天发 `emotion.low_mood_day`（`low_mood_days()`/`low_mood_state()`，随状态落盘、旧文件缺键兼容）；**A3 增跨来源去重**（`dedup_policy()`/`_dedup_skip_result()`：replyer ⇄ tool:emotion_report 按流+窗口先入账者生效，内存在线登记、不改状态文件） | §五/§六/§十/§十三 |
| `bridge.py` | **M3 新增**：跨模块事件桥——订阅 `relation.*` / `schedule.commitment_*` / `qzone.*` 并翻译为有界情绪增量（模板或 LLM 路径），全部经 `record_external_event` 入账；注册/注销挂 `services.py` | MAINTENANCE_PLAN §4.2 / §6.2 A·E·G |
| `components.py` | `EmotionComponentsMixin`：2 Command + 2 Tool（**M3 新增 `cateye_test_emotion_report`**）+ 3 HookHandler（方法名 `emo_` 前缀，组件名 `cateye_test_emotion_*`） | —— |
| `services.py` | `EmotionServices` 服务束（start/stop/set_persona 转发；**M3 起 start/stop 注册与注销 bridge 事件订阅、对外暴露 `effective_valence()` 供 B2 调制**；**M4 增 `mood_effect()` 转发，异常兜底恒等中性值**） | —— |

## 与其他模块的协作

- **planning/**：日程即"日程 LLM"——睡眠检测消费 `ScheduleQuery.find_current_activity` 的 `goal_type=="sleep"` 条目（时长 = start/end 分钟差，跨午夜自动 +1440）；睡眠裁判的前一日/第二日日程取 `format_day_summary`；长期状态取 `schedule.custom_prompt`；约定兑现/违约经 `schedule.commitment_kept/broken` 事件进桥（M3）；**M5：日程条目 `parameters.emotional_tone`（裁剪 ±0.5）求和为前日残差/明日预期进入睡眠大修，`schedule.emotion_influence` 同时决定大修动态限幅与日程 `emotion_context` 槽强度**；反向通道已接线——`emotion.overhauled`（`mood` + 三档词 `levels`）由 planning 订阅缓存，在日程生成时拼「睡醒情绪描述 + 引导约束」进 prompt（`build_emotion_context()`）；
- **宿主配置**：人设（personality.personality / bot.nickname）由顶层 `_refresh_persona` → `set_persona` 透传；
- **companion/**：Hook 层各自独立（同名 Hook 点多 handler 顺序执行），M3 起加一层事件耦合——沉默事件 `relation.silence_detected` 由 companion 巡检发布、本模块 bridge 消费；target 加算加成读取 companion 的 `AffectionManager.tier()`（可空降级，读不到不加成）；
- **qzone/**：`qzone.comment_received` 进桥记小幅正事件（ΔV +0.05~+0.1，不建评论者档案）。

## 职责划分（开环控制，设计文档 §二）

- **对话 LLM**：不可视 VAD 绝对值，只提供事件 → 增量（由 replyer.after_response 后的小模型评估请求实现，±0.5 限幅；M3 起同一评估器也服务外部事件桥的 LLM 路径）；
- **睡眠裁判 LLM**：只接收客观数据（当前 VAD 用已应用偏移的有效值、当天原始事件日志、前后日日程、睡眠时长、R_yesterday/E_tomorrow/约定净兑现），输出 ΔVAD_sleep（±(0.4+0.4×`schedule.emotion_influence`)，缺省 ±0.8）；M5 起该请求在冻结期发出并缓存，醒后由状态机一次性应用；
- **Python 状态机**：维护绝对数值、裁剪/衰减/偏移/乘数、睡眠冻结/解冻与锚点收敛。

## 事件桥、target 识别与倍率（M3 落地，2026-10-03）

**统一入口 `service.py` `record_external_event(source, description, delta=None, *, use_llm=False, multiplier=1.0)`**
- `delta` 给出模板增量 → 直接 `record_event`（再乘倍率）；`delta is None` 且 `use_llm=True` → 走 `evaluate_reply_delta` 有界评估（±0.5，与回复评估共用节流 `evaluate_min_interval_seconds`）；
- 入口统一再乘 `global_event_multiplier`（clamp [0.1,3.0]），即 `final = delta × global_event_multiplier × K`；最终增量经 ±0.5 硬限幅（`EVENT_DELTA_CLAMP`）入账；
- 全量留痕 `emotion.external_event`：`k` / `global_multiplier` / `multiplier`（最终）+ `delta_before` / `delta_scaled` / `delta_applied`（裁剪前/后），满足 MAINTENANCE_PLAN §6.3-7 可观测；
- 模块停用返回 `{"ok": false, "reason": "disabled"}`；空事件 / 节流 / LLM 失败分别返回 `empty_event` / `throttled` / `llm_failed`。

**target 识别 + 三层加算倍率（MAINTENANCE_PLAN §6.2 A）**
- 私聊：`is_target_session(session_id)` 比对目标私聊 stream_id（`plugin._stream_id`，随 target 配置解析/热重载）；replyer.after_response 链路把 session_id 传入 `evaluate_reply`；
- 群聊：Tool `cateye_test_emotion_report(event, is_target)` 传参声明——参数永不拒绝，缺失/非 bool/胡填一律静默按非 target（最多 debug 日志），代码不报错；
- K：target = `1 + target_bonus_step × (档位+1)`（档 0~4 → +0.1~+0.5；档位经 `plugin._affection.tier()` 读取，可空降级、读不到不加成）；非 target = `non_target_multiplier`；调用方 K 有 `EXTERNAL_K_MAX=10` 防御上限。

**事件桥 `emotion/bridge.py`**（注册/注销挂 `EmotionServices.start()/stop()`；总线缺失静默跳过，处理器绝不抛异常）

| 事件 | 模板/口径 | K |
|---|---|---|
| `relation.silence_detected` | ΔV=−0.12、ΔA=+0.04（轻度不安；频控与「仅 target」由发布方 patrol 负责） | target（1+档位加成） |
| `schedule.commitment_kept` | ΔV=+0.2 | target（1+档位加成） |
| `schedule.commitment_broken` | ΔV=−0.15 | target（1+档位加成） |
| `qzone.comment_received` | ΔV 在 [+0.05, +0.1] 区间取值 | K=1（仅乘全局倍率，不建评论者档案 §九.1） |
| `relation.message_received` | 仅计数/debug 日志，不产生情绪增量（对话情绪走回复评估主通道） | —— |
| 其余 `relation.*` / `qzone.*`（interaction_event / affection_tier_changed / daily_reviewed / posted / comment_replied） | 仅 debug 日志（附录 A 全量登记） | —— |

**评估上下文增强**
- `evaluate_reply` 先经 `_last_user_message_text(session_id)` 反查：`ctx.message.get_by_time_in_chat(chat_id, start_time, end_time, limit=10, filter_mai=True)` 取最近 20 分钟该流最后一条用户文本（dict 形态兼容；异常/空一律跳过，绝不阻塞评估）；
- `referee.py EVALUATE_PROMPT` 新增【用户侧最后一条消息】槽位，并要求 event 概括"这轮交流发生了什么"（含谁说了什么/结果），不写"日常对话"；prompt 与输出仍不含绝对 VAD（MAINTENANCE_PLAN §6.3-6）。

**跨来源去重（A3 落地，2026-10-05；MAINTENANCE_PLAN §2.4.6 A3）**
- 口径 `policy=cross_source_stream_window`：仅 `replyer`（replyer.after_response 自动评估主通道）与 `tool:emotion_report`（群聊工具上报）两条来源互斥；按聊天流（`session_id`/`stream_id`，空流归 `-`）+ 时间窗 `emotion.dedup_window_seconds`（默认 180s）判定——窗口内**先入账者生效**，后到者返回 `{"ok": false, "reason": "duplicate_cross_source", "dedup": {...}}`，不入 `emotion_log`、不发 `emotion.updated`，并落一条 `emotion.external_event_dedup` 决策日志；窗口相对"最近一次真正入账"滚动，模板事件（bridge:* 等）与其它来源不参与；
- 判定发生在评估 LLM 之前：命中即跳过（不消耗 `evaluate_min_interval_seconds` 节流、不产生 LLM 成本）；`record_external_event` 返回值恒带 `dedup` 口径描述（`dedup_policy()`）；
- Tool 侧：`cateye_test_emotion_report` 从宿主 kwargs 取 `stream_id` 随上报传入；命中重复时回文本「这件事刚才已经计入情绪了（与自动评估重复），未重复计入。」；
- 兼容：新增两字段均有默认值（`dedup_enabled=True`/`dedup_window_seconds=180`），关闭/置 0 即回退旧行为；去重为内存态，不改 `emotion_state.json` 任何字段（旧状态文件缺键/未知键照常读取）。

**接线挂点**

| 挂点 | 位置 | 行为 |
|---|---|---|
| Tool 上报 | `components.py` `emo_tool_report` | 群聊 target 声明 → 算 K → `record_external_event(use_llm=True)`；deferred 池，触发条件写进 `detailed_description` |
| 回复评估 target 判定 | `components.py` `emo_hook_reply_evaluate` → `service.evaluate_reply` | session_id 进 target 识别；ΔVAD 按 K × 全局倍率入账 |
| 事件订阅 | `services.py` `_register_event_subscriptions` | `register_bridge(bus, service, plugin)`；`stop()` 执行反订阅闭包 |
| 档位读取 | `service.affection_tier()` | 跨模块 getattr 判空，读不到返回 None → 不加成 |
| B2 对外读数 | `services.effective_valence()` | 供 companion 好感调制；模块停用/异常返回 None（消费方乘数恒 1） |

## 情绪 → 行为输出（M4 落地，2026-10-03）

**`service.py` `mood_effect() -> dict`**（纯规则、零 LLM 成本；附录 C 默认映射；四种消费字段）

| 字段 | 映射（V=当前生效效价，A=唤醒度） | 消费方 |
|---|---|---|
| `proactive_rate` | V>0.3 且 A>0.2 → 1.2；V<−0.3 → 0.8；否则 1.0 | patrol：`activity_trigger_rate`/`default_speak_rate` 乘数 |
| `miss_threshold` | V<−0.3 → 0.8；V>0.3 → 1.1；否则 1.0 | patrol：想念随机阈值（再 × affection 档位系数，双系数） |
| `post_rate` | V<−0.6 → 0；V<−0.3 → 0.8；V>0.3 → 1.2；否则 1.0 | qzone：发帖概率与 `emotion_filter` 门控 |
| `intent_hint` | V<−0.3 →「你今天有点低落，语气可以软一点」；V>0.3 →「你今天心情不错，语气可以轻快一点」；否则空串 | patrol：proactive intent 的 reason |

- 数值一律 clamp [0.7,1.3]（`MOOD_EFFECT_MIN/MAX`），`post_rate` 允许 0~1.2（`MOOD_POST_RATE_MAX`）；
- 模块停用（`emotion.enabled=false` / `plugin.enabled=false`）→ 返回恒等中性值 `{1.0, 1.0, 1.0, ""}`，消费方无需另判开关（§八.2 可空降级）；
- 跨模块读取入口为服务束 `EmotionServices.mood_effect()`（内部 `try/except` → 异常同样返回恒等值；patrol/qzone 经 `plugin._emotion.mood_effect()` 读取）；
- 下游三处消费点见 `companion/INTEGRATION.md` §M4 与 `qzone`（发帖门控 + 说说/回评文案情绪行）。

## 有意的裁剪 / TODO

- 情绪增量评估为"回复后补一次小模型请求"（设计文档要求对话 LLM 直接输出 JSON ΔVAD，宿主 Hook 面拿不到结构化输出，此为等价实现），可关（`evaluate_on_reply`）；
- M3 群聊 target 上报 Tool 默认进 **deferred 工具池**（未设 `core_tool=True`），bot 自发调用不保证——replyer.after_response 自动评估是主通道，工具仅作群聊补充（触发条件写进 `detailed_description`）；
- 中性情绪细分、人设动态偏移映射（§七.2.3）、planner 通道情绪注入、长期日程高压联动偏移调制（§九.4.4）未实现；
- 多模态情绪来源（语音/面部/生理）不在 MaiBot 插件能力范围，仅文本路径。

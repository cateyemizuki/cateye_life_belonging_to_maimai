# 注册面简化处置清单 v2.1（2026-10-06 · Tool 终态 6 个 + 情绪评估会话片段制）

> v2 增量（吸收四命令/工具合并×2）之上，并入 **2026-10-06 二次自检（用户确认）**：
> manage_goal / apply_schedule / mai_lover_status / emotion_mood 四工具移除，
> 情绪评估改会话片段制（成本封顶）。v1 的 Hook/配置/频率判定不变。
> 执行者：会话 2（T6 注册面收口 + T7 评估片段制）。

## 一、Tool 终态（6 个）与全量调用链路

1. **`cateye_test_get_schedule_info(view)`**（四视图；吸收 get_planning_status_v4 /
   current_activity / mai_lover_schedule / **manage_goal 只读**）
   触发：planner 处理日程/活动/目标询问 → 宿主 tooling → PlanningComponentsMixin 处理器 →
   `ToolsService.get_planning_status` / `ScheduleQuery`（schedule.db schedule_items，15min 缓存）/
   `GoalManager.get_active_goals`（view=goals 只读）→ 文本。只读，无写路径。
2. **`cateye_test_update_schedule_v4(request)`**（**唯一日程写入口**，吸收 apply 写语义）
   触发：自然语言改日程/约定 → `role_judge`（LLM：today/future/reject）→
   today：`regenerate_today_schedule`（LLM 多轮+评分）→ `apply_schedule_items(consume_commitments=False)`；
   future：`add_pending_commitment` → 到期强指令兑现 → `bus:schedule.commitment_kept/broken`
   → 好感规则事件（+1.0/−0.5）+ 情绪模板事件。底层 `apply_schedule_items` 保留
   （生成流水线 :228 仍用）——删的是 Tool 注册，不是能力。
3. **`cateye_test_mai_lover_send_message(message?)`**
   触发：bot 主动开口 → `MessageService`（空则 LLM relation 口吻生成）→
   `append_affection_suffix` → `ctx.send.text`（目标私聊）→ `increment_speak`（A2 持久化）→
   `proactive.used` → 决策日志。
4. **`cateye_test_mai_lover_affection(level)`**（纠错入口，低频但必要）
   触发：规则盲区大事件（见家长→调 3）/ 裁判失误恢复 / 管理员纠偏 / 里程碑跳变 →
   `AffectionState.score` 移至区间中值 → 迟滞重估（±3/24h）→
   `relation.affection_tier_changed` → 注入与 miss 系数全链切换 → affection_memory.json。
5. **`cateye_test_life_qzone(action, message?)`**（post + moments 读写合一）
   触发：bot 运营空间 → post：`post_moment`（SnowLuma 优先 → CGI 回退）→ `daily_max_posts`
   → `bus:qzone.posted`；moments：`list_my_moments` → 文本。
6. **`cateye_test_emotion_report(event, is_target)`**
   触发：群聊归因声明 → 跨来源去重（replyer⇄tool、同流 180s 先入账生效）→
   `evaluate_reply_delta`（±0.5 clip）→ `record_external_event` ×(global × (1+加成) | non_target)
   → emotion_state.json + 决策落账。

**移除（四项）**：`manage_goal_v4`（写路径撤，只读入 view=goals）、`apply_schedule_v4`
（写语义入 update，过裁判）、`mai_lover_status`（注入全覆盖）、`emotion_mood`（注入全覆盖；
USAGE 标注 `injection_enabled=false` 组合不支持心情问答）。

## 二、情绪评估改会话片段制（T7，成本封顶）

**旧实现的问题**：每回复 1 次评估 ≈ 700 tok/次，**随回复数线性增长**（50 回复日 ≈ 35k
tok/日，插件最大常驻消费）；产出的 ±0.1 级增量最终只被低分辨率五档注入消费——精度浪费。

**新实现（默认 episode 模式）**：
1. replyer.after_response Hook 只做**记录**（stream_id + 时间戳，零 LLM）；
2. 会话闲置 `episode_idle_minutes`（默认 10）→ 冲刷该流自上次评估以来的完整往来
   （`get_by_time_in_chat`，bot+user）→ **一次评估整段片段**（上下文更全、去短视）；
3. 日上限 `evaluate_max_per_day`（默认 10）硬封顶；超限片段并入次日或丢弃（记日志）；
4. `schedule.sleep_started` 时**先冲刷全部未结片段，再跑睡眠裁判**（保证大修读到完整日志）；
5. 兼容：`evaluate_mode = episode(默认) | per_reply（旧模式，60s 节流） | off`；
   旧 `evaluate_on_reply` 布尔迁移为 mode（true→episode / false→off）；
6. A3 去重不变（replyer 源的粒度从"每回复"变"每片段"，去重窗口照旧）；
7. behavior 用例：闲置触发、日上限、睡眠前冲刷顺序、per_reply 兼容、片段内容组装。

**成本对账**：典型日 20 回复/6 片段——旧 20×700=14k tok → 新 6×1000=6k tok（−57%）；
封顶后最坏 10k tok/日。

## 三、最终目标态

| 维度 | T1~T5 后 | T6+T7 后 | §9.3 清退后 |
|---|---|---|---|
| Command | 9~10 | **8** | 8 |
| Tool | 13 | **6** | 6 |
| Hook | 10 | 10 | 10 |
| API | 4 | 4 | **1** |
| 组件总数 | 39 | **28** | **25** |
| 配置 | 13 段 | +`[emotion]` 3 字段（评估片段制） | 定版 |

执行：T6/T7 均在 ws2/m6m7 分支；每步独立提交 + 四套脚本门（smoke 断言改 28）+
实施/验证分立；完成后 merge 协议回主仓，部署归会话 1。

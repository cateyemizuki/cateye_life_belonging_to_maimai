# 注册面简化处置清单 v2（2026-10-06 · 并入 T1~T5 as-built + 用户裁决增量）

> v1（c4001ee）审计结论仍有效：Hook 10 全保留、配置零死字段、frequency.set_adjust 保留。
> 本 v2 增量 = ①会话 2 T1~T5 as-built 对齐（组件 40→39）；②用户裁决（10-06）：**能合并到
> dashboard 的命令全部移除合并** + 工具自检合并×2。执行者：会话 2（T6）。

## 一、现状（T1~T5 后，smoke 断言 39）

已落地：A5 diag 三件套合一（−1 Command）、mai_lover_config Tool 删除（−1 Tool）、
M7 dashboard（/cateye_test_dashboard）、M6 周结+emoji（默认关）、mai_config 实时化、
scratch 脚手架 CATEYE_TEST_DIR 参数化。
仍在册（本轮处置对象）：命令 `mai_test` / `mai_status` / `mai_schedule` / `mai_config`；
工具 `mai_lover_current_activity` / `mai_lover_schedule` / `life_post_qzone` / `life_qzone_moments`；
API `companion_get_*` ×3（零消费者，随 §9.3 清退）。

## 二、T6 增量（用户裁决 2026-10-06）

1. **命令移除×4 → dashboard 吸收**：
   - `mai_status` → dashboard companion 区块（score/baseline/档位/tone/巡检状态）
   - `mai_schedule` → dashboard planning 区块（今日日程/当前活动/约定）
   - `mai_config` → dashboard config 区块（**实时 score/tier**，T5 成果保留）+ 关键配置摘要
   - `mai_test` → 直接删除（无信息量）
   - 文本回退 = dashboard 自身降级路径（渲染失败 → 同命令纯文本），不再需要独立命令
2. **工具合并×2**：
   - `get_planning_status_v4`（基座）+ `mai_lover_current_activity` + `mai_lover_schedule`
     → **`cateye_test_get_schedule_info(view)`**，view = status（分组）/ current（当前活动
     恋人口吻）/ lover_day（恋人视角全天）——三工具信息面同源（ScheduleQuery）且与
     planner 注入重叠（注入概率跳过时由工具补位）
   - `life_post_qzone` + `life_qzone_moments` → **`cateye_test_life_qzone(action)`**，
     action = post | moments（/life 命令仍覆盖全部人工操作）
3. 三同步：`_manifest.json`（.config.get 能力若仍被 dashboard 引用则保留）、README 组件表、
   USAGE 命令表；smoke 断言更新为 **33**。

## 三、最终目标态

| 维度 | T1~T5 后 | T6 后 | §9.3 清退后 |
|---|---|---|---|
| Command | 9~10 | **8**（plan / dashboard / diag / life / mai_affection / mai_help / emotion_init / emotion_config） | 8 |
| Tool | 13 | **11**（见 §四调用链路） | 11 |
| Hook | 10 | 10 | 10 |
| API | 4 | 4 | **1**（仅 get_current_activity） |
| 组件总数 | 39 | **33** | **30** |

## 四、Tool 全量调用链路（T6 后 11 个）

1. **`cateye_test_get_schedule_info(view)`**（三合一）
   触发：planner 处理日程/活动询问 → 宿主 tooling → PlanningComponentsMixin 处理器 →
   `ToolsService.get_planning_status` / `ScheduleQuery`（schedule.db schedule_items，15min 窗口缓存）
   → view=status 分组文本 / current 当前活动恋人口吻行 / lover_day 全天日程 → 返回模型。只读。
2. **`cateye_test_manage_goal_v4(action, …)`**
   触发：planner 目标管理意图 → `ToolsService.manage_goal`（create/list/get/update/pause/
   resume/complete/cancel/delete 分发）→ `GoalManager` → schedule.db goals 表 → 决策日志。
3. **`cateye_test_apply_schedule_v4(items)`**
   触发：planner 落地生成日程 → 参数校验 → `ScheduleDB.schedule_items` 写入 →
   `bus:schedule.generated`（emotion 缓存明日预期、qzone 订阅）→ 注入缓存失效。
4. **`cateye_test_update_schedule_v4(request)`**
   触发：自然语言改日程/约定 → `ToolsService.update_schedule` → `role_judge`（LLM：
   today/future/reject）→ today：`regenerate_today_schedule`（LLM 多轮+评分）；
   future：`GoalManager.add_pending_commitment` → 到期 proactive 强指令兑现 →
   `bus:schedule.commitment_kept/broken` → 好感规则事件（+1.0/−0.5）+ 情绪模板事件。
5. **`cateye_test_mai_lover_status()`**
   触发：关系状态询问 → `AffectionState`（score/baseline/tier/daily_delta）+
   `PatrolService.patrol_status` → 档位描述走 relation profile → 文本。只读。
6. **`cateye_test_mai_lover_send_message(message?)`**
   触发：bot 想主动找重要的人 → `MessageService`（message 空则 LLM 以 relation 口吻生成）→
   `append_affection_suffix`（档位后缀）→ `ctx.send.text`（目标私聊）→
   `increment_speak`（预算，A2 持久化）→ `proactive.used` 事件 → 决策日志。
7. **`cateye_test_mai_lover_affection(level)`**
   触发：档位兜底调整 → `AffectionState.score` 移至区间中值 → 迟滞重估（±3/24h）→
   `relation.affection_tier_changed` → 注入描述与 patrol miss 系数切换 → affection_memory.json。
8. **`cateye_test_life_qzone(action, message?)`**（二合一）
   触发：bot 运营空间 → post：`QzoneService.post_moment`（SnowLuma 动作层优先 → CGI 回退）
   → `daily_max_posts` 额度 → `bus:qzone.posted`；moments：`list_my_moments` → 最近说说文本。
9. **`cateye_test_emotion_mood()`**
   触发："你现在什么心情" → `EmotionService.mood_text()` → `effective_vad`（raw+offset+乘数）
   → 25 原型加权距离映射 + 低分辨率五档描述（不泄漏绝对值）。
10. **`cateye_test_emotion_report(event, is_target)`**
    触发：群聊中 bot 判断事件与重要的人相关且明显动了情绪 → 跨来源去重（replyer⇄tool、
    同流 180s 窗口先入账者生效，A3）→ `evaluate_reply_delta`（utils 小模型，±0.5 clip）→
    `record_external_event` ×(global_event_multiplier × (1+加成(tier)) | non_target_multiplier)
    → emotion_state.json + `emotion.external_event` 决策落账。
11. **（非 Tool）dashboard 文本降级路径**：吸收 mai_status/mai_schedule/mai_config 三命令的
    全部读取面（companion/planning/config 三区块），config 区块含实时 score/tier（T5 成果）。

被合并工具去向：`get_planning_status_v4`（基座改造）、`mai_lover_current_activity`（→view=current）、
`mai_lover_schedule`（→view=lover_day）、`life_post_qzone`（→action=post）、
`life_qzone_moments`（→action=moments）、`mai_lover_config`（→dashboard config 区块）。

## 五、不变判定（v1 结论维持）

Hook 10 全保留（跨模块边界不合并）；配置 13 段零死字段不删（M6 新增默认关字段）；
`frequency.set_adjust` 保留（与预算/mood_effect 为不同杠杆）；API `get_current_activity`
保留（生态兼容承诺）；`companion_get_*` ×3 随 §9.3 清退。

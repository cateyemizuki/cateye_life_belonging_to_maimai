# 注册面简化处置清单（REGISTRY_DISPOSITION · 2026-10-06）

> 依据：ws2 副本源码（v0.4.0，40 组件）+ 宿主日志使用实证（10-04 05:01 → 10-05 全窗口）+
> 跨插件消费者 grep（工作区 + 宿主已装插件）+ 配置字段零读取扫描（脚本实证）。
> 判据：**存在的意义**（谁在用、是否唯一通道）× **必要性**（删除/合并的代价）。
> 已纳入既定计划不重复提议：A5 三 diag 合一、mai_config Tool 收敛、M7 dashboard、
> emotion_report 判保留、A2/A6 已落地。
> 执行时机：标注"随 M7"的由会话 2 在 M7 分支内执行；标注"随 §9.3 清退"的等 L5 复验通过。

## 一、Command（12 → 11，可再可选 −1）

| 指令 | 使用证据 | 判定 | 处置与时机 |
|---|---|---|---|
| /cateye_test_plan（+规划别名） | 日程运营唯一人工入口 | 保留 | — |
| /cateye_test_mai_status | 状态问答；M7 后为 dashboard 的文本降级 | 保留 | — |
| /cateye_test_mai_schedule | 与 planning status 语义重叠（恋人视角 vs 通用视角） | **可选合并** | M7 落地后看 dashboard 覆盖度：并入则命令名保留别名；不强制 |
| /cateye_test_mai_config | 管理员配置查看（T5 实时化后价值回升） | 保留 | T5 实时化 |
| /cateye_test_mai_affection | 好感兜底调整（唯一写通道） | 保留 | — |
| /cateye_test_mai_help | 帮助入口 | 保留 | 同步文本 |
| /cateye_test_mai_llm_log | 排障 | **合并** | 随 M7/A5 → /cateye_test_diag llm |
| /cateye_test_mai_diag | 排障 | **合并** | 随 M7/A5 → /cateye_test_diag decision |
| /cateye_test_life | qzone 唯一人工通道（status/qzone/diag/help） | 保留 | diag 子命令随 A5 迁出 |
| /cateye_test_emotion_init | 初始化必需 | 保留 | — |
| /cateye_test_emotion_config | 参数查看/重置 | 保留 | — |
| **/cateye_test_mai_test** | 纯发送自检，无信息量（§2.4.3 判定） | **删除** | 随 M7（三同步 + help 文本 + smoke 断言） |
| （新）/cateye_test_dashboard | M7 | **新增** | T2 |
| （新）/cateye_test_diag | A5 | **新增** | T3 |

## 二、Tool（14 → 13）

| 工具 | 使用证据 | 判定 | 处置与时机 |
|---|---|---|---|
| manage_goal_v4 / get_planning_status_v4 / apply_schedule_v4 / update_schedule_v4 | planner 的日程/目标/约定管理面（B5 入口） | 保留 | — |
| mai_lover_current_activity | "在干嘛"问答 | 保留 | — |
| mai_lover_status | 关系状态问答（与 planning status 视角不同） | 保留 | — |
| mai_lover_schedule | 恋人视角日程（与 planning status 部分重叠） | 保留·观察 | M7 后若 dashboard/注入已覆盖则再议；Tool 是 bot 自发调用面，与人的命令面不冲突 |
| mai_lover_send_message | 主动开口 Tool 通道（与 patrol 后台链互补） | 保留 | — |
| mai_lover_affection | 档位兜底 | 保留 | — |
| **mai_lover_config** | 与 /cateye_test_mai_config 完全重复（§2.4.2 已判收敛） | **删除/收敛** | 随 M7 并入 dashboard 文本降级路径 |
| life_post_qzone / life_qzone_moments | qzone 运营面 | 保留 | — |
| emotion_mood | 心情问答（模糊化输出） | 保留 | — |
| emotion_report | 判保留 + 去重已实现（03f0234） | 保留 | 继续观测自发调用 |

## 三、Hook（10 → 10，不合并）

全部为内部机制件且职责唯一：planning 2（planner/replyer 注入）、companion 5（身份缓存/
目标时间/旁白+好感后缀/旁白/补计）、emotion 3（双通道注入/增量评估）。
同事件多 handler（planner.before_request 上有 planning+companion+emotion 三个）分属不同
模块——合并会破坏模块边界与独立停用能力（§八.2），**全部保留**。
`frequency.set_adjust` 能力：与预算/mood_effect 是不同杠杆（宿主回复倾向 vs 主动触发），
运行时活跃（enable_frequency_modulation 门控）——**保留**；M7 时可评估让 mood 参与
其因子计算，但不删能力声明。

## 四、API（4 → 1）

| API | 消费者证据 | 判定 | 处置与时机 |
|---|---|---|---|
| get_current_activity | 生态兼容承诺（原 autonomous_planning 插件同名 API 结构；harness /h_api 可调） | 保留 | — |
| companion_get_current_activity | 零消费者（仓内无调用、harness 仅示例文本、宿主已装插件无引用） | **删除** | 随 §9.3 清退批次 |
| companion_get_schedule | 同上 | **删除** | 同上 |
| companion_get_affection_level | 同上 | **删除** | 同上 |

## 五、配置（13 段 ≈140 字段 → 不删不合并，仅新增）

零死字段（脚本逐字段扫描实证，唯一命中 `__ui_i18n__` 为类属性误报）。语义重叠组
（patrol 两个 exempt 开关等）合并的 churn 大于收益，且违反"配置字段名不改"契约——**保留**。
新增（随里程碑，全部默认关）：M6 周结开关与 emoji 段字段；M7 无新配置；
`[inject]` 已由 A6 清理完毕。

## 六、最终注册面目标态

| 维度 | 现状 | 目标 | 变化来源 |
|---|---|---|---|
| Command | 12 | **11**（可选再 −1：schedule 并入） | −mai_test +dashboard +diag −2 diag 合并 |
| Tool | 14 | **13** | −mai_lover_config |
| Hook | 10 | **10** | 不变 |
| API | 4 | **1** | −3 个零消费 companion_get_* |
| 组件总数 | 40 | **35** | 上四项合计 |
| 能力声明 | 16 | 16（+emoji 随 M6 → 17） | frequency 保留 |
| 配置段/字段 | 13 段 | 13 段（M6 +2~4 字段默认关） | 零删除 |

执行批次：Command 删/合 + Tool 收敛 + dashboard/diag 新增 → **随 M7（会话 2 分支内完成）**；
3 个 API 删除 → **随 §9.3 发布清退**（L5 复验通过后）；每一步三同步 + smoke 断言更新为当时准确数。

# cateye_test 精简清单（CLEANUP_PROPOSALS.md）

> 版本：v1.1（2026-10-03 按用户裁决收窄）｜配套：执行规程见 [MAINTENANCE_PLAN_10.25.md](MAINTENANCE_PLAN_10.25.md) §八.6，对应其 **R0 结构重置**里程碑（旧 `MAINTENANCE_PLAN.md` 为 v1.2 快照，已废弃）。
> 性质：**只评估，不动代码**；本清单**只保留无争议项**（v1.0 中的决策项已按用户裁决撤销或归档，见 §二——未来 agent **不得重复提议**）。
> 标记：🟡 合并/清理（小重构）｜🔵 顺路清理（挂在功能里程碑上）。

---

## 一、待执行项（无争议，R0 执行）

| # | 对象 | 内容 | 规模 | 验收 | 勾选 |
|---|---|---|---|---|---|
| C1 | 能量曲线**双实现**合并 | `planning/energy_model.py` 与 `core/time_utils.py:52-80` 内置副本是同源但已漂移的两份实现（planning 侧忠于上游原件，与上游功能一致、仅 docstring/注释差异、非字节一致；core 骨架副本与之 19/24 小时数值、4/5 档文案不同），经裁决以 planning 侧（忠于上游）为唯一实现（各自注明"来源：自主规划插件 energy_model"）；`inject_service.py:34` 从前者导入、`schedule_query.py:9` 从后者导入。合并到一处，两个消费点改 import | 净 -60 行 | behavior test 能量映射用例不变全绿 | ✅ |
| C2 | 时间工具**双模块**合并 | `planning/time_utils.py`（102 行：窗口迁移/strip_tz）与 `core/time_utils.py`（parse_hhmm/is_in_time_window/hours_between/能量）职责交叠；`timezone_manager.py`（48 行，仅 inject_service/command_service 用）一并并入。方案：core 只留纯时间函数，窗口类与能量（C1）归 planning 侧一个模块。纯重构行为不变 | 净 -80 行左右 | smoke + behavior 全绿；全部 import 点改齐（planning 内为相对导入） | ✅ |
| C3 | `affection.py:18-25` 顶层 `LEVEL_LABELS`/`LEVEL_DESCRIPTIONS` 死常量 | relation.py 已接管档位文案（运行时一律走 profile）；🔵 **随 M1/M2 顺路清理**，不提前动（Tool 返回文案兜底仍在引用） | ~30 行 | 全局无引用后删除，smoke 组件面不变 | ☐ |

> 运行产物（`__pycache__`、日志等）由 `.gitignore` 与打包排除覆盖，不列为工作项。

## 二、用户裁决记录（2026-10-03，**不得再次提议移除**）

| 对象 | 裁决 | 原因 |
|---|---|---|
| `UPLOAD_AGENT.md`（插件根，31KB） | **保留** | 未来发布插件时的必要约束文件，不允许删除；执行代码修改时也**不需要阅读**它 |
| 恋人电脑屏幕感知（`companion/cateye_client.py` + `screen_context.py` + `[cateye]` 配置段 + patrol 旁白钩子） | **保留** | 属于插件联动功能 |
| planning 早间问好（`planning_proactive.enable_morning_greeting`） | **保留** | 与 patrol 早安**职责不同、并不重叠**：patrol 早安只对目标用户私聊生效，planning 早安面向群聊 |
| 字体资源（`planning/assets/fonts` NotoSansSC 2.2MB）与 PIL 渲染链（`planning/schedule_image.py`） | **保留** | 将作为聚合状态指令的渲染基础（MAINTENANCE_PLAN_10.25.md 里程碑 M7：一条命令总览四模块状态并渲染为图片）；`ctx.render.html2png` 替代方案撤销，仅作未来可选技术备选 |

## 三、永久保留清单（防止未来 agent 误删）

- `companion/sender_identity.py`：私聊 scope 判定与身份缓存核心，好感/情绪联动的识别链路复用；
- `companion/patrol.py` 判定链（早安/晚安/想念/预算/冷却）：插件核心价值，M1/M3/M4 的调制挂点；
- `qzone` 双通道（SnowLuma 动作层 + Web CGI）与 `poll_comments` 基线提醒：各有独占能力，后者是 cookie 不可用时的 fail-safe；
- `planning/inject/` 的 intent_classifier + inject_optimizer + context_cache：注入节流核心（state_analyzer 除外——M4 改读情绪后按计划书 §4.2 删除）；
- `planning/generator/` 全套、`emotion/vad.py` 原型表：日程生成质量与情绪映射基础；
- `core/diag.py` 读取接口：`/life diag`、`/mai_llm_log`、`/mai_diag` 在用；
- `planning/stream_filter.py`、`lru_cache.py`、`parameter_validator.py`、`exceptions.py`：各有真实消费点；
- 组件名前缀 `cateye_test_`：改名会丢失用户已配置的组件启停状态；
- `planning/assets/fonts` + `schedule_image.py`：渲染基础设施（见 §二）。

## 四、执行纪律（重申 MAINTENANCE_PLAN_10.25 §八.6）

1. 每项独立提交、独立可回滚；先摘引用再删/合，完成后必须跑 `scratch/smoke_test_cateye_test.py` + `scratch/behavior_test_cateye_test.py`；
2. **不改状态文件格式、不改组件名、不改 config 字段名**；
3. 每完成一项在本文件勾选 + 写一行执行记录（日期/版本），README 组件数与 USAGE 同步。

## 五、执行记录

| 日期 | 项 | 提交 | 验证 | 备注 |
|---|---|---|---|---|
| 2026-10-03 | C1（能量曲线双实现合并：以 planning 侧为唯一实现、core 骨架副本删除） | `11f8498` | smoke + behavior 全绿（含新增能量输出钉住断言：schedule_query 通道逐值等于 `planning/energy_model.py`，h7/h19/h23 文案锚点） | 本项为 R0 唯一已知行为变化，仅限 schedule_query 通道（对齐上游），其余通道零变化 |
| 2026-10-03 | C2（时间工具双模块合并：core 只留纯时间函数，窗口+能量归 planning 侧一个模块，timezone_manager 并入） | `17e526b` | smoke + behavior 全绿（含 C1 钉住断言回归通过） | 纯重构行为不变；分层保持：core 不依赖 planning |
| 2026-10-03 | 随 M4 顺路：删除 `planning/inject/state_analyzer.py`（§4.2 planner 注入融合后无消费点，§三 预告项） | `4a95a72` | smoke + behavior 全绿 | 在 M4 功能提交内顺路删除；`inject/__init__.py` 导出与全部调用点清零 |

> 版本号随收尾阶段统一升至 0.3.0，此处不单独递增。

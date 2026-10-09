# 命令与工具手册（USAGE.md）

> 猫眼生活套件（github.cateye.cateye-suite）现有 **7 个 LLM 工具**（T6 收口 6 + M9 赖床 1）、**8 个聊天指令**、**4 个跨插件 API**
> （**29 组件 = 8 Command + 7 Tool + 10 Hook + 4 API**；T6 收口 28 → M9 增 1 个 hidden Tool）。
> 四个业务子模块各自提供一组：planning（日程规划）/ companion（虚拟陪伴）/ emotion（情绪）/ qzone（QQ 空间）。
> 本手册给出每个条目的描述原文、参数、行为细节与权限要求；架构与安装说明见 README.md。
>
> **T6 变更（2026-10-06）**：工具与指令均按**直接删除条目**处理（不标注废弃）——被删入口
> 的信息面/能力已由下表的新入口完整承接（四视图查询、dashboard 文本档、update_schedule
> 唯一写入口），保留废弃别名只会让模型与用户读到已不存在的入口。
>
> **M9 变更（2026-10-07，附录 §四 L3 赖床）**：新增 Tool `cateye_test_nap_lie_in`（**hidden**——
> 平时不进工具名册）+ 夜间被吵醒后的**单轮赖床提醒**；说明见 §一 planning 第 3 条与 §七。

## 0. 通用行为说明（先读）

- **LLM 工具（Tool）**：由模型在对话推理中自主决定调用（deferred 池，经 tool_search 发现），参数由模型生成；给模型阅读的结果放在返回的 `content` 里。
- **聊天指令（Command）**：用户文本命中正则即执行，**命中后不再进入 LLM 回复流程**（intercept）。所有正则用负向前瞻 `(?<!\S)` 匹配，兼容「引用回复」场景下命令不在文本开头的情况。
- **权限模型**（详见 §4）：
  - planning 的 `/cateye_test_plan` 与 qzone 的写操作：`admin.admin_users` 白名单（兼容 `123456` 与 `qq:123456` 两种写法）；**留空 = 默认拒绝**，仅本机控制台（`is_local_operator`）可用；
  - companion 的 `/cateye_test_mai_affection`、`/cateye_test_mai_help`：额外要求触达者 QQ 等于 `target.target_qq`（占位值 123456789 时一律拒绝，避免误发给陌生人）；**A5 收敛后 `/cateye_test_diag` 与旧名 `/cateye_test_mai_llm_log`、`/cateye_test_mai_diag` 收归 admin 判定**（见下一条）；
  - 排障诊断 `/cateye_test_diag`（A5 三合一：llm | decision | life，旧名 `mai_llm_log` / `mai_diag` 与 `/cateye_test_life diag` 为别名）：按 admin 白名单（同 `/cateye_test_plan`），不要求 `target.target_qq`；
  - 聚合总览 `/cateye_test_dashboard`（M7）：按 admin 白名单（同 `/cateye_test_plan`），不要求 `target.target_qq`。
- 工具/指令的执行体都在各子模块的 service 里，组件层只做鉴权与转发；服务实例动态读配置，改 `config.toml` 热重载立即生效。
- **模块独立总开关**：四个业务模块各有自己的 `enabled`（`schedule.enabled` / `patrol.enabled` / `qzone.enabled` / `emotion.enabled`），关闭后该模块停止后台循环、主动触发与注入，工具与命令返回「模块未启用」提示；互不影响其他模块。新增模块请照此模式实现。

---

## 一、LLM 工具（7 个）

### planning — 日程规划（3 个）

#### `cateye_test_get_schedule_info`
- **描述**：查询日程/活动/目标的只读信息（四视图）。
- **参数**：
  | 参数 | 类型 | 必填 | 说明 |
  |---|---|---|---|
  | view | string | | `status`=今日日程状态总览（默认）\| `current`=当前活动 \| `lover_day`=今日完整日程 \| `goals`=活动目标列表；缺省或非法值一律按 `status` 处理 |
- **行为**：四个视图**全部只读**，不触碰任何写路径——
  - `status`：今日日程按「正在进行 / 即将到来 / 已完成」三段展示（含描述与"共 N 个活动"，无日程时回「今天还没有日程」）；
  - `current`：当前时间窗内的活动（含剩余时间与精神状态行）；无活动回休息话术；
  - `lover_day`：今日完整日程逐条（`[HH:MM-HH:MM] 名称`，不截断）；
  - `goals`：`GoalManager.get_active_goals` 的进行中目标（逐条 `get_summary()`，只读）；无目标回「当前没有进行中的目标」。
- **来源**：吸收原 `cateye_test_get_planning_status_v4`（含 `detailed` 参数，已取消——`status` 视图固定展示三段含已完成）、`cateye_test_mai_lover_current_activity`、`cateye_test_mai_lover_schedule` 三工具，以及 `cateye_test_manage_goal_v4` 的**只读**部分。

#### `cateye_test_update_schedule_v4`
- **描述**：根据自然语言请求维护日程：角色裁判决定接受/未来约定/拒绝；接受时调整今日日程，未来约定写入候选清单。**唯一日程写入口。**
- **参数**：`description`（string，必填）自然语言描述，如「下午两点一起学习」「周末打游戏」。
- **行为**：角色裁判（LLM）三选一——
  - `today` → 按请求重新生成今日日程（原日程作废，可叠加额外要求）；
  - `future` → 校验日期后写入 pending_commitment，到期由主动行为以强指令提醒；
  - `reject` → 裁判认为不合理（如凌晨约会），日程不变并说明理由。
- **备注**：原 `cateye_test_apply_schedule_v4`（直接落库，无裁判）与 `cateye_test_manage_goal_v4`（目标写路径）两工具已移除：日程写入全部经本工具过裁判，目标写 API 仍保留在底层（生成流水线与约定消费在用）。

#### `cateye_test_nap_lie_in`（M9 · **hidden**）

- **描述原文**：把今天的起床时间往后推（赖床）。传入目标时间，支持 "HH:MM"、"H:MM" 或纯小时数（如 "09:30" / "9:30" / "9" 表示 09:00）。仅在夜间被吵醒、收到赖床提醒时可用；超过 `[schedule_advanced] lie_in_max_until`（默认 12:00）会被钳到该时间。
- **参数**：
  | 参数 | 类型 | 必填 | 说明 |
  |---|---|---|---|
  | time | string | | 目标起床时间，三种写法等价：`"HH:MM"` / `"H:MM"` / **纯小时数**（`"9"` = 09:00）；缺参或非法（`"abc"`、`"25:00"`、`"12:60"`、`"9:"`）→ 返回友好错误 + 格式示例，**不抛异常** |
- **可见性（hidden，用户裁决 C4）**：注册时声明 `metadata["visibility"] = "hidden"`——宿主 `component_query` 据此置 `enabled=False` 且**不进 ToolSpec 名册**（附录 F13），即"平时不暴露"。
- **行为**（附录 §4.4）：
  1. **非睡眠周期调用 → 拒绝**：经 `plugin._emotion` 服务束查询"是否处于夜间睡眠周期"（本周期窗口在册时按该窗口，含赖床延长后的端点；`[schedule] no_sleep_mode=true` 一律拒绝）→ 返回「你现在还不能想要赖床」；
  2. **上限钳制**：目标 > `[schedule_advanced] lie_in_max_until` → 钳到该值（默认 12:00；提醒文本给的也是这个值，提示与判定同源）；
  3. **改日程（睡眠条目 + 重叠清醒条目顺延，P6 修复后的 as-built）**：找**入睡日**日程的 sleep 条目（"今日"按 `[schedule] wake_time` 锚点定义的新一天：凌晨仍读入睡日；主判 `goal_type=sleep`，兜底按名称含"睡觉/睡眠/安睡"——M5 实测生产条目是 `daily_routine` + 名称"睡觉"，午休/午睡/小憩不算），把其 `end_minute` 推到目标时刻；**与延长窗口重叠的当日非睡眠条目一并顺延到延长端点之后**（P6/W2 修复：否则下一 tick 的醒沿判定会先命中这些清醒条目、醒沿不随端点推迟；保留时长、24:00 封顶不产生倒挂，只动赖床写回路径，生成/归一化契约不动；跨午夜时醒来日日程另写一次），经**共享底层** `ScheduleGenerator.apply_schedule_items(items, today, consume_commitments=False)` 写回（不消费未来约定、不触发重生成）；**无 sleep 条目或写回失败 → 失败文本且不计数**；
  4. **延长睡眠窗口**：成功后通知 emotion 把本睡眠周期窗口结束端推到目标时刻（只增不减、受同一 `lie_in_max_until` 钳制），**醒沿因此推迟**到新端点（P6/W2 修复后成立：写回已同步顺延重叠清醒条目，离线用例「延长后醒沿于延长端点触发」红转绿；宿主级复验见 §七 清单第 5 项）；
  5. **计数（as-built 口径 = 钳制后成功延长即计次）**：**先钳制**（目标 > `lie_in_max_until` → 钳到上限；钳制本身不影响计次），钳制后的目标**晚于当前睡眠窗口端点**且日程写回、emotion 窗口延长均成功 → 本 `cycle_key` 的「有效提醒次数」+1（纯内存）；未调用、非睡眠期拒绝、无 sleep 条目、写回失败、或目标不比当前醒沿晚（含钳到上限后仍不晚）→ **不计次**并返回友好提示（写回前预检 + 写回后 `extend_night_sleep_until` 双保险，日程不被误写）；**额度用尽只停提醒，不阻止本工具**。
- **暴露方案取舍（实现者择优，A2/C4）**：
  - **方案 ①（运行时翻转）**：提醒前 `ctx.component.enable_component(...)` 把本工具提为可见、提醒轮后 `disable_component(...)` 收回。优点：模型在提醒轮内**真正看得到**工具定义（含参数 schema），调用成功率最高。代价：宿主组件启停是**异步、全局**操作，与"单流单轮"语义不完全对齐（翻转期间其它流/轮次也可能看到），且需要额外宿主能力与失败回滚；**当前未采用**。
  - **方案 ②（hidden + 提示词点名 + tool_search 兜底）**：本工具**保持 hidden**，赖床提醒文本直接**点名工具名**（A2），由模型自行经 `tool_search` 找到它。优点：零运行时状态、零宿主组件面写入，注册期静态生效（F13 的可见性本来就是注册期读取的静态值）。代价：**依赖宿主 `tool_search` 对 hidden 工具的可达性**——若宿主把 hidden 工具排除在检索面之外，点名也只是"提示"，模型可能找不到该工具。
  - **本实现采用方案 ②**（离线无法实测宿主检索面；方案 ① 留作宿主实测不通过时的升级路径），因此**需要宿主实测确认**（见 §七 末尾清单）。
- **计数与提醒的关系**：两者共用同一「有效提醒次数」（`[planning_proactive] nap_reminder_max_per_day`，默认 2，按 `cycle_key` 计，纯内存、不落盘）；额度用尽后只停提醒，bot 仍可自行调本工具改日程。

### companion — 虚拟陪伴（2 个）

#### `cateye_test_mai_lover_send_message`
- **描述**：以麦麦的口吻向陪伴对象主动发送一条消息（口吻与后缀随关系自动变化）。
- **参数**：`message`（string，可选）要发送的消息文本，留空则自动生成。
- **行为**：发送到目标私聊，追加好感度情绪后缀（热恋档 10% 概率附括号小剧场）；成功计 1 次发言数并写决策日志；发送失败有明确错误返回。

#### `cateye_test_mai_lover_affection`
- **描述**：调整与关系对象的好感度：把 score 移动到 0/1/2 兼容档对应的 v2 5 档区间并重估档位（档位名称随关系类型变化，如恋人=熟悉/亲密/热恋）。
- **参数**：`level`（integer，必填）只能填 0、1 或 2。
- **行为**：3 档兼容入口——0/1/2 分别落到 v2 档位 1/2/4（score 取 30.0/57.5/94.0），并**强制重估档位（跳过迟滞）**，保证旧 `level()` 输出与输入一致；持久化后 planner 注入按 v2 5 档档位描述表达（后缀/小剧场仍按 3 档折算）。

> T6 说明：原 `cateye_test_mai_lover_current_activity`、`cateye_test_mai_lover_status`、`cateye_test_mai_lover_schedule` 三工具已移除——当前活动/今日完整日程统一走 `cateye_test_get_schedule_info`（view=current / lover_day），陪伴状态面由 `/cateye_test_dashboard` 的伴侣区块与纯文本档承载。
> A5 指令收敛（2026-10-06，随 M7）：原 `cateye_test_mai_lover_config` Tool 已删除，配置摘要并入 `/cateye_test_dashboard` 的**纯文本档末行**（单行紧凑版，图片档不含）。

### qzone — QQ 空间（1 个）

#### `cateye_test_life_qzone`
- **描述**：QQ空间运营：action=post 发说说（message 留空自动按当前活动生成文案，受每日上限限制）| action=moments 查最近发表的说说（内容摘要、评论数、发表时间）。
- **参数**：
  | 参数 | 类型 | 必填 | 说明 |
  |---|---|---|---|
  | action | string | ✔ | `post`=发说说 \| `moments`=查最近说说；缺省/非法值回用法提示（不抛异常） |
  | message | string | | 说说正文（`action=post` 时可选；留空则按日程当前活动由 LLM 生成） |
- **行为**：
  - `post`：受 `qzone.enabled` 与 `qzone.daily_max_posts` 约束（未启用/额度用完明确提示）；发布走双通道（SnowLuma 动作层 `send_qzone_msg` 优先，失败回退 Web CGI 直连）；成功返回带 `stop_after_execution=True`——发完即结束本轮思考，避免 planner 继续自说自话；
  - `moments`：动作通道拉最近 5 条（内容摘要、评论数、时间、tid），空间模块未启用时明确提示。
- **来源**：原 `cateye_test_life_post_qzone` 与 `cateye_test_life_qzone_moments` 两工具**合一**为本工具（读写两分支，语义与限制原样保留）。

---

### emotion — 情绪系统（1 个）

#### `cateye_test_emotion_report`
- **描述**：上报一件影响 bot 心情的事件（由 bot 判断是否与最重要的人有关），入账有界情绪增量。
- **参数**：
  | 参数 | 类型 | 必填 | 说明 |
  |---|---|---|---|
  | event | string | ✔ | 一句话概括这轮交流发生了什么（谁说了什么、结果如何） |
  | is_target | boolean | | 是否由最重要的人（`target.target_qq`）引发；是=true；缺失/类型不对/胡填一律按非 target 处理，不报错 |
- **行为**：target 时 `K = 1 + target_bonus_step × (档位+1)`、非 target 时 `K = non_target_multiplier`，再乘 `global_event_multiplier` → 事件文本走小模型有界评估 ΔVAD（±0.5）→ 入账；与其他事件共用评估节流（`evaluate_min_interval_seconds`）与 ±0.5 硬限幅，结果落决策日志（`emotion.external_event`）。
- **备注**：Tool 默认进 **deferred 工具池**（未设 `core_tool=True`），bot 自发调用不保证——**触发条件已写进 `detailed_description`**（仅群聊里出现重要的人相关/由 TA 引发且影响心情的事件才调用；普通群友闲聊与私聊无需调用，`replyer.after_response` 自动评估始终是主通道），本工具只是群聊场景的补充。

> **T6 移除注记（`cateye_test_emotion_mood`）**：原「查询当前情绪底色」工具已随注册面收口**移除**。
> 「你现在什么心情」这类问答现在由**情绪底色注入**覆盖——planner/replyer 的系统提示词里本就
> 带着模糊化的情绪底色（严禁直说标签词），bot 据此自然作答即可。
> **已知组合限制**：`[emotion] injection_enabled=false` 时注入关闭，此时**不支持心情问答**
> （既无注入、也无查询工具；如需该能力请保持注入开启）。
> 服务层的 emoji 情绪联动（`emoji_for_mood`，默认关）随工具移除而**当前无组件消费点**，
> 配置项与 manifest 能力声明保留，详见 §五。

## 二、聊天指令（8 个）

### planning — /cateye_test_plan（1 个）

#### `/cateye_test_plan` / `/cateye_test_规划`
- **描述**：日程规划系统管理命令（支持 /cateye_test_plan 与 /cateye_test_规划）。
- **权限**：**管理员**（`admin.admin_users`；未配置时 QQ 侧默认拒绝并提示，本机控制台不受限）。
- **子命令**：
  | 子命令 | 说明 |
  |---|---|
  | `/cateye_test_plan list` | 查看今日日程（PIL 日程图片 + 详细文字合并转发，图片失败降级纯文本；**今日无日程时会先自动生成再展示**） |
  | `/cateye_test_plan regenerate [额外要求]` | 重新生成今日日程（先删今天再重生，可叠加临时要求如「加入下午跑步」；操作不可逆） |
  | `/cateye_test_plan delete <goal_id或序号>` | 删除指定目标（序号来自 /cateye_test_plan list） |
  | `/cateye_test_plan clear` | 清理昨天及更早的旧日程（自动保留今天） |
  | `/cateye_test_plan help` | 显示完整帮助（合并转发） |
- **备注**：日程每天自动生成（默认 00:30），无需手动创建；输出用合并转发防刷屏。

### companion — /cateye_test_mai_affection、/cateye_test_mai_help 与聚合总览（4 个）

> 鉴权规则：触发者 QQ == `target.target_qq`，或本机控制台；目标 QQ 为占位值 123456789 时一律拒绝。无权限时回复「没有权限」并拦截。
> 例外（**admin 判定**，`admin.admin_users` ∪ 本机控制台；未配置管理员时 QQ 侧默认拒绝，不要求 `target.target_qq`）：`/cateye_test_diag`（A5 排障三合一，含旧名 `mai_llm_log` / `mai_diag`）与 `/cateye_test_dashboard`（M7 聚合总览）。
> **权限变化（A5）**：旧 `/cateye_test_mai_llm_log`、`/cateye_test_mai_diag` 原按 `target.target_qq` 白名单，收敛后随 `/cateye_test_diag` 收归管理员；旧 `/cateye_test_life diag` 本就是管理员，口径不变。
> **T6 移除**：`/cateye_test_mai_status`、`/cateye_test_mai_schedule`、`/cateye_test_mai_config`、`/cateye_test_mai_test` 四条命令已删除——状态与完整日程进 `/cateye_test_dashboard`（纯文本档展开完整日程，并含好感/发言/早晚安想念标记），配置摘要同源末行，「测试消息」用 `/cateye_test_mai_lover_send_message` 工具或 `qzone post` 替代。

| 指令 | 描述 | 行为 |
|---|---|---|
| `/cateye_test_mai_affection [0\|1\|2]` | 查看/调整好感度：不带参数输出 score/baseline/5 档档位/今日 Δ + 最近印象（最多 2 条，「最近印象」行）与关系温度（tone，「关系温度·日期」行）——印象与 tone 为 M2 关系日结产出，为空则不显示（tone 读取时按 7 天过期判断）；带参数按 3 档兼容口径调整 | 0/1/2 → score 移到 v2 档位 1/2/4（取 30.0/57.5/94.0）并强制重估（跳过迟滞）；参数取正则命名捕获组（兼容引用+命令混排）；缺参/非法值提示用法与当前状态 |
| `/cateye_test_mai_help` | 查看麦麦陪伴所有可用命令 | 命令清单直发（含「日程与生活管理见 /cateye_test_plan 与 /cateye_test_life」指路行） |
| `/cateye_test_diag [llm\|decision\|life] [天数]` | **A5 排障三合一**：`llm` = LLM 调用日志（旧名 `/cateye_test_mai_llm_log`）、`decision` = 主动行为决策日志（旧名 `/cateye_test_mai_diag`）、`life` = 决策日志紧凑文本（旧名 `/cateye_test_life diag`）；无参数/未知子命令回四行用法帮助 | **管理员 ∪ 本机控制台**；`llm`/`decision` JSONL 按天读取（1~30 天，缺省=整个保留期）后合并转发，未启用时提示配置项、空日志时给提示（decision 附巡检状态行）；`life` 紧凑文本（1~7 天，缺省 1）直发 |
| `/cateye_test_dashboard` | **M7 聚合总览**：一条命令看四模块（日程规划/伴侣关系/情绪/QQ空间）并渲染状态图（内置 NotoSansSC + PIL，浅色卡片排版） | **管理员 ∪ 本机控制台**（`admin.admin_users`，留空时 QQ 侧默认拒绝并提示「权限不足」，不要求 target_qq）；有图 = 图片 + 文本合并转发；渲染失败/超时（PIL 缺失、字体缺失、10s 超时）静默降级为纯文本直发（不发失败提示）；某模块未启用/未装配 → 该区块显示「（未启用）」；**纯文本档**：日程区块展开今日**完整**日程（不截断，原 `/cateye_test_mai_schedule` 的信息面）+ 伴侣区块含好感/今日发言/早安·晚安·想念标记，末行附单行配置摘要（原 `cateye_test_mai_lover_config` Tool 与 `/cateye_test_mai_config` 命令的信息面）；**图片档仍按 ≤5 条截断且不含配置摘要** |

### qzone — /cateye_test_life（1 个）

#### `/cateye_test_life`
- **描述**：猫眼生活套件管理命令。
- **权限**：`status` / `help` 及 qzone 的读操作（`moments`/`comments`）所有人可用；`diag` 与 qzone 写操作（`post`/`reply`/`like`）需要管理员（同 /cateye_test_plan 规则）。
- **子命令**：
  | 子命令 | 说明 |
  |---|---|
  | `/cateye_test_life status` | 三模块状态总览：好感度+巡检（运行中/上次触发）、日程规划摘要、QQ 空间（通道+cookie+今日额度） |
  | `/cateye_test_life qzone status` | 空间模块状态（动作通道命中情况、cookie uin 与刷新时间、评论监听开关） |
  | `/cateye_test_life qzone moments` | 最近 10 条说说（内容摘要+评论数） |
  | `/cateye_test_life qzone comments` | 自己说说下的评论列表（含楼中楼，标注自己的评论；序号供 reply 使用） |
  | `/cateye_test_life qzone post <文本>` | 手动发一条说说（双通道） |
  | `/cateye_test_life qzone reply <说说序号> [评论序号] <内容>` | 只给说说序号 = 普通评论；给「说说.评论」序号（如 `2.1`）= 楼中楼回复该条评论（需 cookie 就绪） |
  | `/cateye_test_life qzone like <tid> [目标QQ]` | 点赞（省略目标 QQ = 自己的说说；好友说说传对方 QQ 号） |
  | `/cateye_test_life diag [天数]` | 决策日志最近 N 天（默认 1，最多 7）；**旧名别名**：新入口为 `/cateye_test_diag life`，行为不变（仍管理员） |
  | `/cateye_test_life help` | 帮助（提示 /cateye_test_plan 与 /cateye_test_mai_* 两组命令的存在） |

---

### emotion — 情绪系统（2 个，均需管理员）

| 指令 | 描述 | 行为 |
|---|---|---|
| `/cateye_test_emotion_init` | 由人设自动推导情绪锚点/静态偏移/乘数并生效（VAD 情绪系统初始化） | 拦截指令→提取宿主人设→独立 LLM 请求（不带对话历史）→校验裁剪（锚点 ±0.6、偏移 ±0.3、增益 [0.8,1.3]、衰减 [0.6,1.2]）→落盘 emotion_config.json→当前 VAD 回到锚点、清空情绪残留；回复参数与推导理由。失败时提示重试 |
| `/cateye_test_emotion_config [reset\|clear]` | 查看情绪系统参数（锚点/偏移/乘数/当前 VAD） | 无参=查看（含当前情绪底色与当天事件数、推导理由）；`reset`=恢复默认（零锚点零偏移中性乘数）；`clear`=清空当天情绪日志（隐私接口） |

> **可选数据文件（M12）**：`<data_dir>/emotion_mapping.json` 是情绪四张映射表（25 原型区间 / 模板事件 4 条 / 三层倍率 / 五档四切点）的**可选 JSON 覆盖层**，缺省不存在 = 纯代码默认；上表两条命令**都不读写它**（`reset` 只重置 `emotion_config.json` 的锚点/偏移/乘数）。注意：若该文件写了 `multipliers`，`reset` 后覆盖仍生效（优先级 **JSON > config > 默认**，此时须删除/改名该 JSON 才能让 config 旋钮回到生效态）；字段/回退语义/差异披露与示例见 [EMOTION_MAPPING.md](EMOTION_MAPPING.md) §九，改完需 touch 插件文件触发热重载或等宿主重启。

## 三、跨插件 API（4 个，供其他插件 `ctx.api.call` 调用）

| API | 提供方 | 返回结构 | 说明 |
|---|---|---|---|
| `get_current_activity` | planning | `{"has_activity", "activity": {name/description/goal_type/time_window}, "next_activities": [{"time","name"}], "as_of", "timezone", "error"}` | 当前活动快照，与自主规划插件返回结构兼容（经注入缓存通道） |
| `companion_get_current_activity` | companion | 活动描述字符串（无活动返回空串） | 陪伴对象视角的当前活动 |
| `companion_get_schedule` | companion | `[{time, activity}]` | 今日完整日程（兼容虚拟陪伴插件返回结构） |
| `companion_get_affection_level` | companion | `0 / 1 / 2` | 好感度档位（兼容虚拟陪伴插件返回结构） |

> 命名说明：companion 的三个 API 在源插件中叫 `get_*`，因与 planning 的 `get_current_activity` 在同一插件内撞名，统一加了 `companion_` 前缀。

## 四、权限模型速查

| 入口 | 谁能用 | 配置项 |
|---|---|---|
| `/cateye_test_plan` 全部子命令 | 管理员 ∪ 本机控制台 | `admin.admin_users`（留空 = 仅本机控制台，QQ 侧默认拒绝） |
| `/cateye_test_mai_affection`、`/cateye_test_mai_help`（`/cateye_test_diag` 除外） | 目标 QQ ∪ 本机控制台 | `target.target_qq`（占位值 123456789 时全拒） |
| `/cateye_test_diag`（A5 三合一；旧名 `/cateye_test_mai_llm_log` / `/cateye_test_mai_diag` 走同一入口） | 管理员 ∪ 本机控制台 | `admin.admin_users`（留空 = 仅本机控制台，QQ 侧默认拒绝；不要求 `target.target_qq`） |
| `/cateye_test_dashboard` | 管理员 ∪ 本机控制台 | `admin.admin_users`（留空 = 仅本机控制台，QQ 侧默认拒绝；不要求 `target.target_qq`） |
| `/cateye_test_life status` / `help` / `qzone moments` / `qzone comments` | 所有人 | —— |
| `/cateye_test_life diag`（=`/cateye_test_diag life` 旧名别名） / `qzone post` / `qzone reply` / `qzone like` | 管理员 ∪ 本机控制台 | `admin.admin_users` |
| 全部 LLM 工具 | 模型自主调用（无用户级鉴权；敏感操作如发说说受每日上限/开关约束） | `qzone.daily_max_posts`、`qzone.enabled` 等 |

> **A5 权限变化提醒**：旧 `/cateye_test_mai_llm_log` 与 `/cateye_test_mai_diag` 原按 `target.target_qq` 白名单放行，收敛进 `/cateye_test_diag` 后改为管理员判定——升级后若只配置了 `target.target_qq` 而未配置 `admin.admin_users`，QQ 侧调用将提示「权限不足」（本机控制台不受限）。

## 五、M6 可选增强开关（默认关）

两个子项都**默认关闭**：不配置（或保持 `false`）时插件行为与本版本之前逐字一致，既有配置无需任何改动。

| 配置项 | 默认 | 说明 |
|---|---|---|
| `[affection] weekly_milestones_enabled` | `false` | **周结大事记**：开启后周日的**关系日结**（`schedule.sleep_started` 触发、晚于情绪睡眠大修）按纯规则汇总最近 7 天，生成 1 条「本周关系温度：…（净变化 +0.0）」；`\|净变化\| ≥ 1.0` 时再追加 1 条「升温/降温明显」。结果写入好感档案 `week_log`（滚动 8 天）与 `milestones`（cap 20 条；同一周日**幂等**，重复日结不重复生成），并让 planner 注入与 `/cateye_test_mai_affection` 的记忆行多出一行「【本周大事记】日期 文案」（只取最近 1 条）。**零 LLM 成本**；周内日结记录不足 3 天时不生成；日结本身失败不落里程碑；周结步骤异常不影响日结主流程 |
| `[emotion] emoji_enabled` | `false` | **emoji 情绪联动**：按当前情绪首个标签名（取不到时回退情绪底色文本）经宿主 `emoji.get_by_description` 取一个表情。**注意（T6）**：其唯一消费入口 `cateye_test_emotion_mood` Tool 已随注册面收口移除，本开关当前**无组件消费点**——服务层实现（`EmotionService.emoji_for_mood`）与测试保留，宿主侧接线与开关默认关行为不变；如需恢复该能力，重新注册一个消费组件即可 |

> 说明：`emoji.get_by_description` 已写入 `_manifest.json` 的 `capabilities`（仅声明，默认不使用）；宿主侧对该能力的实际接线由宿主级验证会话负责——离线测试只覆盖插件侧门控与降级路径。`statistics` 成本看板经裁定**不做**（扩大能力面、收益存疑）。
> 组件面（T6 终态 28 → M9 后 29）：29 = 8 命令 + 10 Hook + 7 Tool + 4 API，两个开关均为**配置项**。

## 六、情绪评估模式（T7 会话片段制，默认 `episode`）

`replyer.after_response` 的情绪增量评估默认**不再每回复一次**，改为「会话闲置后整段评估」：

| 配置项 | 默认 | 区间 | 说明 |
|---|---|---|---|
| `[emotion] evaluate_mode` | `episode` | episode / per_reply / off | `episode`=会话闲置后整段评估（零 LLM 只登记，成本封顶，默认）｜`per_reply`=旧模式，每回复评估（60s 节流）｜`off`=不评估 |
| `[emotion] episode_idle_minutes` | 10 | [1, 720] | episode 模式：会话闲置多少分钟后冲刷该流片段；扫描精度 = `check_interval_minutes`（睡眠检测循环，默认 10min） |
| `[emotion] evaluate_max_per_day` | 10 | [0, 200] | 每日评估上限（次，两种模式共用，硬封顶；0 = 当日不评估）；超限片段**丢弃不并入次日**并落决策日志（`reason` 含 `daily_cap`） |
| `[emotion] evaluate_on_reply` | true | — | **已废弃**（迁移为 `evaluate_mode`）：显式设置 `evaluate_mode` 时本字段被忽略 |

- **迁移口径**：未显式设置 `evaluate_mode` 时按旧布尔迁移——`evaluate_on_reply=true → episode`、`false → off`。
- **一次性迁移风险（升级须知）**：宿主 UI 在保存配置时会把 `evaluate_mode` 以当前值写进配置文件（此后视为"显式设置"）。若旧配置里 `evaluate_on_reply=false`，**先**把 `evaluate_mode` 显式设为 `off` 再保存一次 UI；否则保存后 `evaluate_mode` 会被写死为默认 `episode`，旧的 `false` 不再生效。
- **片段制细节**：Hook 只登记（纯内存、零 LLM）→ 闲置冲刷该流自上次评估以来的完整往来（`message.get_by_time_in_chat`，bot+user 全量）→ 一次评估整段 → 入账（±0.5 限幅与跨来源去重 A3 口径不变，粒度从"每回复"变"每片段"）。片段上限 40 条 / 3000 字，**超出时保留最近的往来**并在片段头部标注省略；进评估器时正文预算放宽到片段上限（评估器默认的 600 字/保留头部是 `per_reply` 旧语义）。
- **睡眠入口**：进入睡眠时段时**先冲刷全部未结片段再跑睡眠裁判**，保证大修读到完整日志。
- **成本**：典型日 20 回复/6 片段 ≈ 旧 20×700 tok → 新 6×1000 tok；封顶后最坏 ≈ 10k tok/日。
- 说明：`evaluate_mode` 的"是否显式设置"取自宿主配置模型的 `model_fields_set`；离线环境无真实 `maibot_sdk`，该语义由 `scratch/sdk_stub.py` 的 pydantic 模型等价复现，宿主侧最终行为由宿主级验证会话确认。

## 七、赖床提醒（M9 · 附录 §四 L3）

夜间被重要的人吵醒、又回到睡眠之后，bot 会收到**一条只在本轮有效的临时提醒**（不强制、不进下一轮上下文）。

**触发（全部满足才发）**：①该打扰段**确认成立过**（窗口内 ≥ `interrupt_reply_threshold` 次 target 私聊 reply）且以 `resleep` 闭合（`最后一次 reply + resleep_minutes` 内无新 reply）；②本睡眠周期有效提醒次数 < `nap_reminder_max_per_day`；③当前仍在睡眠窗口内；④`[schedule] no_sleep_mode == false`；⑤`[planning_proactive] nap_reminder_enabled`。每段至多提醒一次，被拦时落 `emotion.nap_reminder_blocked`（`block_reason` 区分 quota / budget / gate_* / trigger_*）。

**提醒文本（逐字模板，`{nap_tool_name}` 引用唯一常量 `NAP_TOOL_NAME`）**：

> 【临时提醒·不进下一轮上下文】你半夜被重要的人吵醒了，现在是 {now_hhmm}；你本来打算睡到
> {planned_wake_hhmm}。如果还想再睡一会儿，可以调用 {nap_tool_name} 把起床时间往后推（最晚
> {lie_in_max_until}）。这条提醒只在本轮有效，不必向任何人提起"昨晚被吵醒"。是否调用由你决定。

- `{planned_wake_hhmm}` = **当前睡眠窗口结束**（周期在册端点优先，回退 `[schedule] wake_time` 锚点）；
- 发送走**统一主动链**：`ctx.maisaka.proactive.trigger(stream_id=该段末条 reply 流, intent="nap_reminder", reason=文本, metadata={"kind": "nap_reminder"})`，调用前查主动打扰预算（仅目标陪伴私聊；`allow_proactive` → `proactive.used`），**纳入打扰预算**；预算拒绝 → 不发送、不计数；
- **不进下一轮**：提醒块由宿主写进该流的内存历史（per-stream，不落库），本插件在注入 Hook 的既有剥离点新增 `is_consumed_ephemeral_proactive_block`——块龄 > `NAP_BLOCK_LINGER_SECONDS`（120s）即从请求载荷剔除（下一轮 planner 起不再出现）；入队超 30 分钟的块仍由原有规则兜底剥离，只影响本插件的 `kind=nap_reminder` 块，不误伤其他主动块与真实消息；
- **提醒轮豁免（§4.5）**：提醒发出后 120s 内该流若意外触发 reply，**不刷新 resleep 计时、不重开吵醒态、不额外限制工具调用**。

| 配置项 | 默认 | 约束 | 说明 |
|---|---|---|---|
| `[planning_proactive] nap_reminder_enabled` | `true` | — | 赖床提醒开关；关闭后不提醒，但不阻止 bot 自行改日程 |
| `[planning_proactive] nap_reminder_max_per_day` | `2` | `ge=1` | 每**睡眠周期**（不是自然日）的有效提醒上限：提醒发送成功 + 工具**钳制后成功延长**（目标晚于当前醒沿）各计 1 次；用尽后只停提醒 |
| `[schedule_advanced] lie_in_max_until` | `12:00` | `HH:MM` | 赖床硬上限时刻：工具传入超过它会被钳到它，提醒文本给出的也是它 |

**宿主实测项清单（离线不可覆盖，归宿主级验证会话）**：

1. `metadata["visibility"]="hidden"` 的实际效果：`component_query` 是否置 `enabled=False` 且**不进 ToolSpec 名册**（F13 已实测过宿主代码，本插件只需确认注册期声明被读取）；
2. **方案 ② 的关键前提**：宿主 `tool_search`（或等价的工具检索面）**能否检索到 hidden 工具**——能，则提醒轮点名即可调用；不能，则需升级到方案 ①（提醒前 `ctx.component.enable_component` 翻转可见性、提醒轮后收回），并确认翻转的作用域（全局/单流）与失败回滚；
3. `ctx.maisaka.proactive.trigger` 的 `metadata` 透传与宿主把它**序列化进块文本一行**（F12；块 id 形态 `proactive:{plugin_id}:{epoch_ms}`）——决定 `is_consumed_ephemeral_proactive_block` 的 `plugin_id`/`kind` 匹配是否按预期命中（当前实现：`plugin_id` 段不一致则不动，取不到该段则退回只按 `kind=nap_reminder` 标记判定）；
4. 提醒块所在流是否确为 per-stream（F10）——确认剥离只需处理"同一流后续轮次"；
5. 钳制后的睡眠窗口端点能推迟醒沿（**P6/W2 修复后成立**）：离线侧已验收——lie-in 写回时同步顺延/移出与延长窗口重叠的当日非睡眠条目（+ `ScheduleQuery.db` 只读访问器承接跨午夜醒来日顺延写回，W2，`23b6265`），离线用例「延长后 `woke_up` 于延长端点触发」红转绿（对照缺陷形态：修复前 11:06:53 照常醒、预期 12:00）；**宿主侧复验**仍归宿主级验证会话——部署后观察 `schedule.woke_up` / `emotion.sleep_overhaul` 的实际触发时刻是否随 `lie_in_max_until` 后的端点顺延。

## 八、早/晚安（2026-10-10 配置整理轮重写）

早/晚安**由日程侧驱动、陪伴侧不重复发**：`planning/proactive_service.py` 在 60s 循环里
「每聊天流每日掷点一次 → 到点判定 → 走统一主动链 `ctx.maisaka.proactive.trigger`」；
`companion/patrol.py` **从不触发问候**，只读 `affection` 的每日已发标记做去重观察。

**开关与时机**

| 配置项 | 默认 | 说明 |
|---|---|---|
| `[planning_proactive] greeting_morning_enabled` | `true` | 早安总开关（含目标私聊与群聊）；关闭 = 完全不发早安 |
| `[planning_proactive] greeting_night_enabled` | `true` | 晚安总开关；关闭 = 完全不发晚安 |
| `[planning_proactive] morning_drift` | `"10-30"` | 早安漂移区间（分钟）：触发时刻 = `[schedule] wake_time` 锚点 + `randint(a,b)`；单数字 `N` = `0~N`；非法回退 10-30 |
| `[planning_proactive] night_drift` | `"10-30"` | 晚安漂移区间：触发时刻 = 当天最后一个非睡眠日程条目结束 − `randint(a,b)`（无日程回退 `[schedule] sleep_time`） |
| `[planning_proactive] greeting_require_activation` | `true` | **群聊**必要性激活：到点需先观察到该流自基准点起有人发过消息才问候；无人则推迟、到期限放弃（当天不补发）。目标私聊不受本项影响 |
| `[planning_proactive] greeting_other_private_min_messages` | `5` | **其他私聊**门槛：前一日该流消息数 ≥ 该值才可能收到问候（`ge=5` 硬约束）；取不到计数按 0（不发） |

- `[schedule] wake_time` / `sleep_time` 是早安/晚安的时间基准来源；**旧的 `[patrol] morning_start/…/night_end` 四个窗口字段已删除**（配置整理轮）。
- 早/晚安**始终免主动打扰预算**（既不查闸门也不计用量）；预算只服务陪伴侧的巡检与想念。
- 目标私聊问候的额外条件：**陪伴模块开关** `[patrol] enabled=true` 且 `target_qq` 已解析出 stream。

**覆盖范围（统一名单系统）**

| 会话类型 | 附加条件 |
|---|---|
| 目标陪伴私聊（`[patrol] target_qq`） | 无名单限制（必定触发；受总开关、静默时段、当日已发、陪伴模块开关约束） |
| 群聊 | 统一名单的**群聊部分**：`proactive_group_ids` + `proactive_group_list_mode`（默认 `blacklist`，留空 = 全群生效）+ 必要性激活 |
| 其他私聊 | 统一名单的**私聊部分**：`proactive_private_ids` + `proactive_private_list_mode`（默认 `whitelist`，留空 = 全不发）+ 前一日消息数门槛 |

名单只收**裸号**（群聊填群号、私聊填 QQ 号，不需要任何前缀；带前缀/非数字/空串/重复项忽略并记一条汇总告警；
不支持通配 `*`）。**目标陪伴 QQ 始终按私聊黑名单处理**：活动切换主动开口与其他私聊问候都不会落到 TA 的私聊上。

**基准与期限**

| 问候 | 基准点 | 触发时刻 | 放弃期限 |
|---|---|---|---|
| 早安 | 睡眠窗口结束（`[schedule] wake_time`） | 基准 + `randint(a,b)` | 睡眠结束后**第一个非睡眠**条目的结束；无日程 → 基准 + 120 分钟 |
| 晚安 | 当天最后一个非睡眠日程条目结束（无日程 → `sleep_time`） | 基准 − `randint(a,b)` | `min(sleep_time 锚点, [patrol] silence_start)`，取先到者 |

- **每流每日一次**：`(stream_id, 日期, kind)` 只掷一次点，同日重复决策复用同一结果（纯内存，热重载后允许重掷）。
- **重掷不等于重发**：触发前补查 `affection` 的按天标记——**目标陪伴私聊**该 kind 当日已发 → 视为已触发并落 `skip`「持久化标记命中，热重载去重」行；群聊/非目标私聊的跨热重载去重仍未实现（登记待议）。
- **跨午夜条目**（结束 > 24:00）按绝对时间戳处理；循环因重启错过时刻时，未到期限会在下一 tick 补发。

**宿主实测项（离线不可覆盖，归宿主级验证会话）**：① 漂移时刻与 `wake_time` / 末条日程结束的实际对齐（含跨午夜）；
② 群聊必要性激活在真实 `message.get_by_time_in_chat` 下的命中口径；③ 同一目标私聊流同一天早/晚安各至多一条（含并发窗口）。

## 九、日程生成锚点（M10 · 附录 §五 B2）

新一天的日程**不再按配置的绝对时刻生成**，而是"**最后日程结束后多久生成**"——按相对锚点判定，
并叠加一条"至少在新一天开始前 30 分钟生成"的硬约束（裁决 B2）。

**一句话驱动链**：`planning/auto_scheduler.py` 的后台循环每轮算"下一个待生成目标日 + 它的到期时刻"→
到点且该目标日无日程 → 用 LLM 生成并落库（当天已有日程则跳过）；完全静默，不发任何消息。

- **目标日（"新一天"）**：当日帧（帧起点 = `[schedule] wake_time` 锚点，帧跨越午夜）的**次日**——
  例：wake 07:00 时，00:30 生成的是"今天"的日程（凌晨仍属前一天帧），22:30 生成的是"明天"的日程。
- **基准（B4）**：当天**最后一个非睡眠日程条目**的结束时间；当天没有日程 → 回退 `[schedule] sleep_time`
  锚点（"有日程用日程、无日程用锚点"）。"非睡眠条目"的主判 `goal_type=sleep`、名称兜底**仅夜间词**
  （睡觉/睡眠/安睡；午休/午睡/小憩/打盹不算）——与 M9 赖床工具**同一份代码**
  （`planning/schedule_items.py`，两处不再各留一份口径）。
- **硬约束（B2）**：`到期时刻 = min(基准 + 延迟, 新一天起点 − 30 分钟)`；相对延迟算出的时刻更晚 →
  **强制拉回**该阈值（保证新一天开始前至少 30 分钟已生成）。

| 配置项 | 默认 | 约束 | 说明 |
|---|---|---|---|
| `[schedule] auto_generate_after_last_end_minutes` | `90` | `ge=0` | 生成延迟（分钟）：最后日程结束后多久生成新一天日程；无日程回退 `sleep_time` 锚点。默认 90 在缺省入睡锚点 23:00 下等价旧的 `00:30`（老配置零感知迁移） |
| `[schedule_advanced] auto_generate_time` | `00:30` | `HH:MM` | **已废弃**（B2，隐藏字段）→ 改用上面的延迟字段。保留仅作**兼容读取**：当延迟字段仍是出厂值 90、而本字段偏离出厂值 00:30 时，按绝对时刻理解（同样受「新一天起点 − 30 分钟」硬约束钳制）；否则一律走相对延迟 |
| `[schedule] auto_generate_enabled` | `true` | — | 自动生成总开关（关掉后不跑循环；目标日已有日程照旧跳过） |

**防重**：到期触发后记"已生成的目标日"，同一天不再触发；下一次触发自然顺延到次日同点（跨日重新判定）。

**宿主实测项（离线不可覆盖，归宿主级验证会话）**：① 相对锚点与真实日程末条结束（含跨午夜条目）的对齐；
② 老配置文件（曾把 `auto_generate_time` 改成非 00:30）升级后是否仍按绝对时刻生效（值判断口径见上表）；
③ WebUI 保存配置时隐藏字段的写回形态。


## 十、配置总览（2026-10-10 配置整理轮）

配置一共 **11 段**，按「用户改动频率」排序（WebUI 顺序即此序）。**日常只需要动前五段**，
`[llm]` / `[prompts]` 属于深度定制，不懂就别动（`[prompts]` 是高级设置：改坏占位符会回退出厂文案并记 warning，不会中断业务）。

| 段 | 中文标题 | 管什么 | 什么时候动它 |
|---|---|---|---|
| `[plugin]` | 插件 | 总开关 / 配置版本 | 停用整个插件时 |
| `[patrol]` | **陪伴模块** | 目标 QQ、关系类型、巡检开关与概率、想念（开关/阈值/概率/LLM 把关）、每日预算、最小间隔、用户冷却、静默时段、**群聊好感度注入开关**、屏幕感知（含模糊半径/超时/旁白存活） | 想调"陪伴对象是谁、多久主动找 TA 说话"时 |
| `[affection]` | 好感度 | 档位、v2 开关、日衰减/基线漂移、每日增减上限、档位迟滞、好感裁判（会话采样 + 关系日结）、情绪调制、周结大事记 | 想调"感情升温/降温的快慢"时 |
| `[schedule]` | 日程系统 | 总开关、作息锚点（起床/入睡）、无睡眠模式、自动生成与延迟、活动数量、生活阶段、活动描述、**注入开关与注入范围**、**真实节假日（注入 / 影响生成）** | 日常调整作息与"日程注不注入、注给谁" |
| `[planning_proactive]` | 日程主动行为 | 活动切换主动开口、触发窗口、频率调控、**群聊/私聊两份名单 + 名单模式**、**早/晚安两个开关**、漂移区间、问候激活、赖床提醒 | 想调"在哪些群/私聊主动说话"与早/晚安时 |
| `[schedule_advanced]` | 日程系统·高级设置 | 多轮生成、角色裁判（开关 + 温度）、`list` 绘图片与超时、时区、生成 token/超时/回看天数/历史与知识库条数、注入调优（意图分类/优化器/概率/上下文）、缓存、赖床硬上限、情绪影响强度、后台清理 | 深度调优生成质量、成本与渲染时 |
| `[llm]` | **LLM 调用** | 主任务名 + 判定（judge）模型与参数 + 各用途任务名（好感裁判/情绪裁判/情绪评估/心情空间/视觉） | 换模型、判定老被驳回时 |
| `[prompts]` | **提示词（高级）** | 插件发给模型的全部原文（64 项，含陪伴/日程/情绪/空间/工具五组） | 深度定制人格化文案时；**不改 = 与出厂逐字一致** |
| `[admin]` | 管理与日志 | 管理员、允许会话、LLM/决策日志开关与保留天数 | 排障与权限 |
| `[qzone]` | QQ 空间 | 空间总开关与发帖/回评节奏 | 空间功能 |
| `[emotion]` | 情绪系统 | VAD 状态机、评估模式、睡眠结算、注入开关 | 情绪系统 |

**旧配置自动迁移（不需要手改）**

宿主在插件启动时会把旧结构交给插件，插件内置迁移器把值搬进新家并落盘（**段名不变的键原样保留**）：

| 旧位置 | 新位置 |
|---|---|
| `[target] target_qq` | `[patrol] target_qq` |
| `[cateye] enabled/screenshot_blur/timeout_seconds/context_ttl_minutes` | `[patrol] screen_*` |
| `[cateye] vlm_task` | `[llm] vlm_task` |
| `[cateye] describe_prompt / narration_template / offline_narration_template / failed_narration_template` | `[prompts] screen_describe / screen_narration / screen_narration_offline / screen_narration_failed`（只搬非默认值） |
| `[judge] model/max_tokens/temperature` | `[llm] judge_model / judge_max_tokens / judge_temperature` |
| `[schedule] llm_task_name` / `[affection] review_task_name` / `[emotion] evaluator_task_name` / `[emotion] referee_task_name` / `[qzone] llm_task_name` | `[llm] task_name / review_task_name / emotion_evaluator_task_name / emotion_referee_task_name / qzone_task_name` |
| `[inject] *`、`[planning_cleanup] *` | `[schedule_advanced] *`（键名不变） |
| `[patrol] miss_hours_min` + `miss_hours_max` | `[patrol] miss_silence_hours = "min-max"` |
| 旧问候四名单 / 活动切换两名单 | `[planning_proactive] proactive_group_ids` + `proactive_private_ids`（+ 模式；见 §八） |

**已删除的配置项**（值不再有意义，迁移时丢弃）：`[patrol] morning_start/morning_end/night_start/night_end`、
`min_interval_exempt_greetings`（早/晚安**始终**豁免）、`min_interval_exempt_miss`（想念**不**豁免）、
`proactive_target_daily_cap` / `global_proactive_cap`（预算上限 = 每日预算本身）、
`miss_avoid_future_schedule` / `miss_future_schedule_hours`（功能移除）、
`[planning_proactive] enable_morning_greeting/enable_night_greeting/greeting_*_enabled/greeting_*_blacklist/greeting_*_whitelist/morning_other_private_*/greeting_exempt_budget`、
`[schedule] auto_generate_time`（降级为高级段隐藏兼容位）。

**注入矩阵（谁在什么会话里看到什么）**

| 会话 | 日程块（含日期/节假日头部） | 好感度块 | 屏幕旁白 / 主动回合约束 |
|---|---|---|---|
| 目标陪伴私聊 | ✅（`inject_scope=all` 时） | ✅ **始终**（陪伴模块启用时） | ✅（仅此会话） |
| 其他私聊 | ✅ | ❌ **始终不注入** | ❌ |
| 群聊 | ✅ | 仅当**群内上下文出现陪伴用户**且 `[patrol] inject_affection_in_group=true`（默认开）；此档只给档位块，不带关系记忆行 | ❌ |

- 日期/节假日头部：`[schedule] holiday_inject_enabled=true`（默认）时注在日程块头部，模板 `[prompts] inject_date_header`；
  `holiday_affects_schedule=true` 额外让**日程生成**考虑节假日（默认关；开启后生成提示词会追加「今天是特殊日期」块，模板 `[prompts] schedule_holiday_context`）。
- 日程块是否注入由 `[schedule] inject_into_planner / inject_into_replyer` 与 `inject_scope` 共同决定（默认 `all`）。

**预算口径（一张表说清）**

| 主动行为 | 是否占预算 | 说明 |
|---|---|---|
| 陪伴·想念 / 日程巡检（活动分享 / 日常搭话） | ✅ 占（仅目标陪伴私聊计数） | 上限 = `[patrol] daily_max_speak`（0 = 陪伴侧静音） |
| 早/晚安（目标私聊与群聊） | ❌ **始终免**预算 | 不查闸门也不计用量 |
| 活动切换主动开口（群聊 / 名单内私聊） | ❌ 不计 | 不落到目标陪伴私聊（目标始终视为私聊黑名单） |

**宿主实测项（离线不可覆盖）**：① WebUI 对新段布局（含 `hidden` 兼容字段）的渲染与保存；
② 旧配置文件（含已删段）在真机启动时的迁移落盘形态；③ `[prompts]` 字段在 WebUI 中的编辑体验（大文本域）。

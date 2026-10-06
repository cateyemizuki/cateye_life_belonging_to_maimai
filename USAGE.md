# 命令与工具手册（USAGE.md）

> 猫眼生活套件（github.cateye.cateye-suite）现有 **6 个 LLM 工具**、**8 个聊天指令**、**4 个跨插件 API**
> （T6 注册面收口终态：**28 组件 = 8 Command + 6 Tool + 10 Hook + 4 API**）。
> 四个业务子模块各自提供一组：planning（日程规划）/ companion（虚拟恋人）/ emotion（情绪）/ qzone（QQ 空间）。
> 本手册给出每个条目的描述原文、参数、行为细节与权限要求；架构与安装说明见 README.md。
>
> **T6 变更（2026-10-06）**：工具与指令均按**直接删除条目**处理（不标注废弃）——被删入口
> 的信息面/能力已由下表的新入口完整承接（四视图查询、dashboard 文本档、update_schedule
> 唯一写入口），保留废弃别名只会让模型与用户读到已不存在的入口。

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

## 一、LLM 工具（6 个）

### planning — 日程规划（2 个）

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

### companion — 虚拟恋人（2 个）

#### `cateye_test_mai_lover_send_message`
- **描述**：以麦麦恋人的口吻向用户主动发送一条恋人消息。
- **参数**：`message`（string，可选）要发送的消息文本，留空则自动生成。
- **行为**：发送到目标私聊，追加好感度情绪后缀（热恋档 10% 概率附括号小剧场）；成功计 1 次发言数并写决策日志；发送失败有明确错误返回。

#### `cateye_test_mai_lover_affection`
- **描述**：调整与关系对象的好感度：把 score 移动到 0/1/2 兼容档对应的 v2 5 档区间并重估档位（档位名称随关系类型变化，如恋人=熟悉/亲密/热恋）。
- **参数**：`level`（integer，必填）只能填 0、1 或 2。
- **行为**：3 档兼容入口——0/1/2 分别落到 v2 档位 1/2/4（score 取 30.0/57.5/94.0），并**强制重估档位（跳过迟滞）**，保证旧 `level()` 输出与输入一致；持久化后 planner 注入按 v2 5 档档位描述表达（后缀/小剧场仍按 3 档折算）。

> T6 说明：原 `cateye_test_mai_lover_current_activity`、`cateye_test_mai_lover_status`、`cateye_test_mai_lover_schedule` 三工具已移除——当前活动/今日完整日程统一走 `cateye_test_get_schedule_info`（view=current / lover_day），恋人状态面由 `/cateye_test_dashboard` 的伴侣区块与纯文本档承载。
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
| `/cateye_test_mai_help` | 查看麦麦恋人所有可用命令 | 命令清单直发（含「日程与生活管理见 /cateye_test_plan 与 /cateye_test_life」指路行） |
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

## 三、跨插件 API（4 个，供其他插件 `ctx.api.call` 调用）

| API | 提供方 | 返回结构 | 说明 |
|---|---|---|---|
| `get_current_activity` | planning | `{"has_activity", "activity": {name/description/goal_type/time_window}, "next_activities": [{"time","name"}], "as_of", "timezone", "error"}` | 当前活动快照，与自主规划插件返回结构兼容（经注入缓存通道） |
| `companion_get_current_activity` | companion | 活动描述字符串（无活动返回空串） | 恋人视角的当前活动 |
| `companion_get_schedule` | companion | `[{time, activity}]` | 今日完整日程（兼容虚拟恋人插件返回结构） |
| `companion_get_affection_level` | companion | `0 / 1 / 2` | 好感度档位（兼容虚拟恋人插件返回结构） |

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
> 组件面（T6 终态）：28 = 8 命令 + 10 Hook + 6 Tool + 4 API，两个开关均为**配置项**。

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

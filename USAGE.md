# 命令与工具手册（USAGE.md）

> 属于麦麦的生活（插件清单 `id`：`cateye.life_belonging_to_maimai`）现有 **7 个 LLM 工具**、**8 个聊天指令**、**4 个跨插件 API**
> （共 **29 个组件 = 8 Command + 7 Tool + 10 Hook + 4 API**）。
> 四个业务子模块各自提供一组：planning（日程规划）/ companion（虚拟陪伴）/ emotion（情绪）/ qzone（QQ 空间）。
> 本手册给出每个条目的描述、参数、行为细节与权限要求；安装步骤见 [README.md](README.md)，全部配置键见本文「十、配置键总表」，历史变更见 [CHANGELOG.md](CHANGELOG.md)。

## 安装与首次配置

1. 把插件目录放进 MaiBot 的 `plugins/` 下（目录名建议 `lbm`），重启 MaiBot 或在插件管理里重载。
2. 在 WebUI 的插件配置里填 **目标 QQ**（`[target] target_qq`）；保持默认占位值 `123456789` 时**不解析目标会话**——不会主动私聊任何人，相关命令鉴权也一律拒绝。
3. 想从 QQ 侧使用管理员指令，请在 `[admin] admin_users` 填上你的 QQ 号（支持 `qq:123456` 写法）；**留空时仅本机控制台可用**。若你另外配置了 `[admin] allowed_streams`，名单外的会话会被静默拒绝。
4. 依赖 `httpx>=0.24` 与 `Pillow>=9.0`：前者用于网络请求，后者只用于日程图／状态图渲染，**缺失或渲染失败时自动降级为纯文本**，不影响其他功能。
5. 依次验证：`/lbm_dashboard`（总览）→ `/lbm_plan`（日程，首次会自动生成）→ `/lbm_emotion_init`（情绪初始化）；可选模块（QQ 空间、屏幕感知）默认关闭，需要时在对应配置段打开。

## 0. 通用行为说明（先读）

- **LLM 工具（Tool）**：由模型在对话推理中自主决定调用（deferred 池，经 tool_search 发现），参数由模型生成；给模型阅读的结果放在返回的 `content` 里。
- **聊天指令（Command）**：用户文本命中正则即执行，**命中后不再进入 LLM 回复流程**（intercept）。所有正则用负向前瞻 `(?<!\S)` 匹配，兼容「引用回复」场景下命令不在文本开头的情况。
- **权限模型**（详见 §四）：
  - planning 的 `/lbm_plan` 与 qzone 的写操作：`admin.admin_users` 白名单（兼容 `123456` 与 `qq:123456` 两种写法）；**留空 = 默认拒绝**，仅本机控制台（`is_local_operator`）可用；
  - companion 的 `/lbm_mai_affection`、`/lbm_mai_help`：额外要求触达者 QQ 等于 `target.target_qq`（占位值 123456789 时一律拒绝，避免误发给陌生人）；
  - 排障诊断 `/lbm_diag` 与聚合总览 `/lbm_dashboard`：按 admin 白名单（同 `/lbm_plan`），不要求 `target.target_qq`。
- 工具/指令的执行体都在各子模块的 service 里，组件层只做鉴权与转发；服务实例动态读配置，改 `config.toml` 热重载立即生效。
- **模块独立总开关**：四个业务模块各有自己的 `enabled`（`schedule.enabled` / `patrol.enabled` / `qzone.enabled` / `emotion.enabled`），关闭后该模块停止后台循环、主动触发与注入，工具与命令返回「模块未启用」提示；互不影响其他模块。

---

## 一、LLM 工具（7 个）

### planning — 日程规划（3 个）

#### `lbm_get_schedule_info`
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
- **备注**：`status` 视图固定展示三段（含已完成），不再提供 `detailed` 参数——需要完整逐条日程时用 `lover_day`。

#### `lbm_update_schedule_v4`
- **描述**：根据自然语言请求维护日程：角色裁判决定接受/未来约定/拒绝；接受时调整今日日程，未来约定写入候选清单。**唯一日程写入口。**
- **参数**：`description`（string，必填）自然语言描述，如「下午两点一起学习」「周末打游戏」。
- **行为**：角色裁判（LLM）三选一——
  - `today` → 按请求重新生成今日日程（原日程作废，可叠加额外要求）；
  - `future` → 校验日期后写入 pending_commitment，到期由主动行为以强指令提醒；
  - `reject` → 裁判认为不合理（如凌晨约会），日程不变并说明理由。
- **备注**：日程写入一律经本工具过裁判；目标写入路径不对模型开放（生成流水线与约定消费在底层使用）。

#### `lbm_nap_lie_in`

- **描述原文**：把今天的起床时间往后推（赖床）。传入目标时间，支持 "HH:MM"、"H:MM" 或纯小时数（如 "09:30" / "9:30" / "9" 表示 09:00）。仅在夜间被吵醒、收到赖床提醒时可用；超过 `[schedule] lie_in_max_until`（默认 12:00）会被钳到该时间。
- **参数**：
  | 参数 | 类型 | 必填 | 说明 |
  |---|---|---|---|
  | time | string | | 目标起床时间，三种写法等价：`"HH:MM"` / `"H:MM"` / **纯小时数**（`"9"` = 09:00）；缺参或非法（`"abc"`、`"25:00"`、`"12:60"`、`"9:"`）→ 返回友好错误 + 格式示例，**不抛异常** |
- **可见性**：注册时声明 `metadata["visibility"] = "hidden"`——宿主据此置 `enabled=False` 且**不进工具名册**，即"平时不暴露"。
- **行为**：
  1. **非睡眠周期调用 → 拒绝**：查询"是否处于夜间睡眠周期"（本周期窗口在册时按该窗口，含赖床延长后的端点；`[schedule] no_sleep_mode=true` 一律拒绝）→ 返回「你现在还不能想要赖床」；
  2. **上限钳制**：目标 > `[schedule] lie_in_max_until` → 钳到该值（默认 12:00；提醒文本给的也是这个值，提示与判定同源）；
  3. **改日程（睡眠条目 + 重叠清醒条目顺延）**：找**入睡日**日程的 sleep 条目（"今日"按 `[schedule] wake_time` 锚点定义的新一天：凌晨仍读入睡日；主判 `goal_type=sleep`，兜底按名称含"睡觉/睡眠/安睡"——实际生产条目多为 `daily_routine` + 名称"睡觉"，午休/午睡/小憩不算），把其 `end_minute` 推到目标时刻；**与延长窗口重叠的当日非睡眠条目一并顺延到延长端点之后**（否则下一轮的醒沿判定会先命中这些清醒条目、醒沿不随端点推迟；保留时长、24:00 封顶不产生倒挂；跨午夜时醒来日日程另写一次），经**共享底层** `apply_schedule_items(items, today, consume_commitments=False)` 写回（不消费未来约定、不触发重生成）；**无 sleep 条目或写回失败 → 失败文本且不计数**；
  4. **延长睡眠窗口**：成功后通知 emotion 把本睡眠周期窗口结束端推到目标时刻（只增不减、受同一 `lie_in_max_until` 钳制），**醒沿因此推迟**到新端点；
  5. **计数（口径 = 钳制后成功延长即计次）**：**先钳制**（目标 > `lie_in_max_until` → 钳到上限；钳制本身不影响计次），钳制后的目标**晚于当前睡眠窗口端点**且日程写回、emotion 窗口延长均成功 → 本睡眠周期的「有效提醒次数」+1（纯内存）；未调用、非睡眠期拒绝、无 sleep 条目、写回失败、或目标不比当前醒沿晚（含钳到上限后仍不晚）→ **不计次**并返回友好提示（写回前预检 + 写回后双保险，日程不被误写）；**额度用尽只停提醒，不阻止本工具**。
- **关于「不暴露」的取舍**：本工具保持 hidden，赖床提醒文本会**直接点名工具名**，由模型自行经宿主的工具检索（`tool_search` 或等价面）找到它。好处是零运行时状态、注册期静态生效；代价是**依赖宿主工具检索面能检索到隐藏工具**——若你的宿主把隐藏工具排除在检索面之外，模型可能找不到该工具（此时可改为在提醒轮临时把它提为可见）。
- **计数与提醒的关系**：两者共用同一「有效提醒次数」（`[planning_proactive] nap_reminder_max_per_day`，默认 2，按睡眠周期计，纯内存、不落盘）；额度用尽后只停提醒，bot 仍可自行调本工具改日程。

### companion — 虚拟陪伴（2 个）

#### `lbm_mai_lover_send_message`
- **描述**：以麦麦的口吻向陪伴对象主动发送一条消息（口吻与后缀随关系自动变化）。
- **参数**：`message`（string，可选）要发送的消息文本，留空则自动生成。
- **行为**：发送到目标私聊，追加好感度情绪后缀（热恋档 10% 概率附括号小剧场）；成功计 1 次发言数并写决策日志；发送失败有明确错误返回。

#### `lbm_mai_lover_affection`
- **描述**：调整与关系对象的好感度：把 score 移动到 0/1/2 兼容档对应的 v2 5 档区间并重估档位（档位名称随关系类型变化，如恋人=熟悉/亲密/热恋）。
- **参数**：`level`（integer，必填）只能填 0、1 或 2。
- **行为**：3 档兼容入口——0/1/2 分别落到 v2 档位 1/2/4（score 取 30.0/57.5/94.0），并**强制重估档位（跳过迟滞）**，保证旧 `level()` 输出与输入一致；持久化后 planner 注入按 v2 5 档档位描述表达（后缀/小剧场仍按 3 档折算）。

### qzone — QQ 空间（1 个）

#### `lbm_life_qzone`
- **描述**：QQ空间运营：action=post 发说说（message 留空自动按当前活动生成文案，受每日上限限制）| action=moments 查最近发表的说说（内容摘要、评论数、发表时间）。
- **参数**：
  | 参数 | 类型 | 必填 | 说明 |
  |---|---|---|---|
  | action | string | ✔ | `post`=发说说 \| `moments`=查最近说说；缺省/非法值回用法提示（不抛异常） |
  | message | string | | 说说正文（`action=post` 时可选；留空则按日程当前活动由 LLM 生成） |
- **行为**：
  - `post`：受 `qzone.enabled` 与 `qzone.daily_max_posts` 约束（未启用/额度用完明确提示）；发布走双通道（SnowLuma 动作层 `send_qzone_msg` 优先，失败回退 Web CGI 直连）；成功返回带 `stop_after_execution=True`——发完即结束本轮思考，避免 planner 继续自说自话；
  - `moments`：动作通道拉最近 5 条（内容摘要、评论数、时间、tid），空间模块未启用时明确提示。

---

### emotion — 情绪系统（1 个）

#### `lbm_emotion_report`
- **描述**：上报一件影响 bot 心情的事件（由 bot 判断是否与最重要的人有关），入账有界情绪增量。
- **参数**：
  | 参数 | 类型 | 必填 | 说明 |
  |---|---|---|---|
  | event | string | ✔ | 一句话概括这轮交流发生了什么（谁说了什么、结果如何） |
  | is_target | boolean | | 是否由最重要的人（`target.target_qq`）引发；是=true；缺失/类型不对/胡填一律按非 target 处理，不报错 |
- **行为**：target 时 `K = 1 + target_bonus_step × (档位+1)`、非 target 时 `K = non_target_multiplier`，再乘 `global_event_multiplier` → 事件文本走小模型有界评估 ΔVAD（±0.5）→ 入账；与其他事件共用评估节流（`evaluate_min_interval_seconds`）与 ±0.5 硬限幅，结果落决策日志（`emotion.external_event`）。
- **备注**：Tool 默认进 **deferred 工具池**（未设 `core_tool=True`），bot 自发调用不保证——**触发条件已写进 `detailed_description`**（仅群聊里出现重要的人相关/由 TA 引发且影响心情的事件才调用；普通群友闲聊与私聊无需调用，`replyer.after_response` 自动评估始终是主通道），本工具只是群聊场景的补充。

> **心情问答怎么工作**：「你现在什么心情」这类问答由**情绪底色注入**覆盖——planner/replyer 的系统提示词里本就带着模糊化的情绪底色（严禁直说标签词），bot 据此自然作答即可。
> **已知组合限制**：`[emotion] injection_enabled=false` 时注入关闭，此时**不支持心情问答**（既无注入、也无专门的查询工具；如需该能力请保持注入开启）。
> 服务层的 emoji 情绪联动（默认关）当前**无组件消费点**，配置项与 manifest 能力声明保留，详见 §五。

## 二、聊天指令（8 个）

### planning — /lbm_plan（1 个）

#### `/lbm_plan` / `/lbm_规划`
- **描述**：日程规划系统管理命令（支持 /lbm_plan 与 /lbm_规划）。
- **权限**：**管理员**（`admin.admin_users`；未配置时 QQ 侧默认拒绝并提示，本机控制台不受限）。
- **子命令**：
  | 子命令 | 说明 |
  |---|---|
  | `/lbm_plan list` | 查看今日日程（PIL 日程图片 + 详细文字合并转发，图片失败降级纯文本；**今日无日程时会先自动生成再展示**） |
  | `/lbm_plan regenerate [额外要求]` | 重新生成今日日程（先删今天再重生，可叠加临时要求如「加入下午跑步」；操作不可逆） |
  | `/lbm_plan delete <goal_id或序号>` | 删除指定目标（序号来自 /lbm_plan list） |
  | `/lbm_plan clear` | 清理昨天及更早的旧日程（自动保留今天） |
  | `/lbm_plan help` | 显示完整帮助（合并转发） |
- **备注**：日程每天自动生成（默认：当天最后一个非睡眠活动结束后 90 分钟生成次日日程），无需手动创建；输出用合并转发防刷屏。

### companion — /lbm_mai_affection、/lbm_mai_help 与聚合总览（4 个）

> 鉴权规则：触发者 QQ == `target.target_qq`，或本机控制台；目标 QQ 为占位值 123456789 时一律拒绝。无权限时回复「没有权限」并拦截。
> 例外（**admin 判定**，`admin.admin_users` ∪ 本机控制台；未配置管理员时 QQ 侧默认拒绝，不要求 `target.target_qq`）：`/lbm_diag`（排障三合一，含旧名 `mai_llm_log` / `mai_diag`）与 `/lbm_dashboard`（聚合总览）。

| 指令 | 描述 | 行为 |
|---|---|---|
| `/lbm_mai_affection [0\|1\|2]` | 查看/调整好感度：不带参数输出 score/baseline/5 档档位/今日 Δ + 最近印象（最多 2 条，「最近印象」行）与关系温度（tone，「关系温度·日期」行）——印象与 tone 为关系日结产出，为空则不显示（tone 读取时按 7 天过期判断）；带参数按 3 档兼容口径调整 | 0/1/2 → score 移到 v2 档位 1/2/4（取 30.0/57.5/94.0）并强制重估（跳过迟滞）；参数取正则命名捕获组（兼容引用+命令混排）；缺参/非法值提示用法与当前状态 |
| `/lbm_mai_help` | 查看麦麦陪伴所有可用命令 | 命令清单直发（含「日程与生活管理见 /lbm_plan 与 /lbm_life」指路行） |
| `/lbm_diag [llm\|decision\|life] [天数]` | **排障三合一**：`llm` = LLM 调用日志（旧名 `/lbm_mai_llm_log`）、`decision` = 主动行为决策日志（旧名 `/lbm_mai_diag`）、`life` = 决策日志紧凑文本（旧名 `/lbm_life diag`）；无参数/未知子命令回四行用法帮助 | **管理员 ∪ 本机控制台**；`llm`/`decision` JSONL 按天读取（1~30 天，缺省=整个保留期）后合并转发，未启用时提示配置项、空日志时给提示（decision 附巡检状态行）；`life` 紧凑文本（1~7 天，缺省 1）直发 |
| `/lbm_dashboard` | **聚合总览**：一条命令看四模块（日程规划/伴侣关系/情绪/QQ空间）并渲染状态图（内置 NotoSansSC + PIL，浅色卡片排版） | **管理员 ∪ 本机控制台**（`admin.admin_users`，留空时 QQ 侧默认拒绝并提示「权限不足」，不要求 target_qq）；有图 = 图片 + 文本合并转发；渲染失败/超时（PIL 缺失、字体缺失、10s 超时）静默降级为纯文本直发（不发失败提示）；某模块未启用/未装配 → 该区块显示「（未启用）」；**纯文本档**：日程区块展开今日**完整**日程（不截断）+ 伴侣区块含好感/今日发言/早安·晚安·想念标记，末行附单行配置摘要；**图片档仍按 ≤5 条截断且不含配置摘要** |

### qzone — /lbm_life（1 个）

#### `/lbm_life`
- **描述**：属于麦麦的生活管理命令。
- **权限**：`status` / `help` 及 qzone 的读操作（`moments`/`comments`）所有人可用；`diag` 与 qzone 写操作（`post`/`reply`/`like`）需要管理员（同 /lbm_plan 规则）。
- **子命令**：
  | 子命令 | 说明 |
  |---|---|
  | `/lbm_life status` | 模块状态总览：好感度+巡检（运行中/上次触发）、日程规划摘要、QQ 空间（通道+cookie+今日额度） |
  | `/lbm_life qzone status` | 空间模块状态（动作通道命中情况、cookie uin 与刷新时间、评论监听开关） |
  | `/lbm_life qzone moments` | 最近 10 条说说（内容摘要+评论数） |
  | `/lbm_life qzone comments` | 自己说说下的评论列表（含楼中楼，标注自己的评论；序号供 reply 使用） |
  | `/lbm_life qzone post <文本>` | 手动发一条说说（双通道） |
  | `/lbm_life qzone reply <说说序号> [评论序号] <内容>` | 只给说说序号 = 普通评论；给「说说.评论」序号（如 `2.1`）= 楼中楼回复该条评论（需 cookie 就绪） |
  | `/lbm_life qzone like <tid> [目标QQ]` | 点赞（省略目标 QQ = 自己的说说；好友说说传对方 QQ 号） |
  | `/lbm_life diag [天数]` | 决策日志最近 N 天（默认 1，最多 7）；**旧名别名**：新入口为 `/lbm_diag life`，行为不变（仍管理员） |
  | `/lbm_life help` | 帮助（提示 /lbm_plan 与 /lbm_mai_* 两组命令的存在） |

---

### emotion — 情绪系统（2 个，均需管理员）

| 指令 | 描述 | 行为 |
|---|---|---|
| `/lbm_emotion_init` | 由人设自动推导情绪锚点/静态偏移/乘数并生效（VAD 情绪系统初始化） | 拦截指令→提取宿主人设→独立 LLM 请求（不带对话历史）→校验裁剪（锚点 ±0.6、偏移 ±0.3、增益 [0.8,1.3]、衰减 [0.6,1.2]）→落盘 emotion_config.json→当前 VAD 回到锚点、清空情绪残留；回复参数与推导理由。失败时提示重试 |
| `/lbm_emotion_config [reset\|clear]` | 查看情绪系统参数（锚点/偏移/乘数/当前 VAD） | 无参=查看（含当前情绪底色与当天事件数、推导理由）；`reset`=恢复默认（零锚点零偏移中性乘数）；`clear`=清空当天情绪日志（隐私接口） |

> **可选数据文件**：`<数据目录>/emotion_mapping.json` 是情绪四张映射表（25 原型区间 / 模板事件 4 条 / 三层倍率 / 五档四切点）的**可选 JSON 覆盖层**，缺省不存在 = 纯代码默认；上表两条命令**都不读写它**（`reset` 只重置 `emotion_config.json` 的锚点/偏移/乘数）。注意：若该文件写了 `multipliers`，`reset` 后覆盖仍生效（优先级 **JSON > config > 默认**，此时须删除/改名该 JSON 才能让 config 旋钮回到生效态）；字段/回退语义与示例见 [EMOTION_MAPPING.md](EMOTION_MAPPING.md) §九，改完需 touch 插件文件触发热重载或等宿主重启。

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
| `/lbm_plan` 全部子命令 | 管理员 ∪ 本机控制台 | `admin.admin_users`（留空 = 仅本机控制台，QQ 侧默认拒绝） |
| `/lbm_mai_affection`、`/lbm_mai_help`（`/lbm_diag` 除外） | 目标 QQ ∪ 本机控制台 | `target.target_qq`（占位值 123456789 时全拒） |
| `/lbm_diag`（三合一；旧名 `/lbm_mai_llm_log` / `/lbm_mai_diag` 走同一入口） | 管理员 ∪ 本机控制台 | `admin.admin_users`（留空 = 仅本机控制台，QQ 侧默认拒绝；不要求 `target.target_qq`） |
| `/lbm_dashboard` | 管理员 ∪ 本机控制台 | `admin.admin_users`（留空 = 仅本机控制台，QQ 侧默认拒绝；不要求 `target.target_qq`） |
| `/lbm_life status` / `help` / `qzone moments` / `qzone comments` | 所有人 | —— |
| `/lbm_life diag`（=`/lbm_diag life` 旧名别名） / `qzone post` / `qzone reply` / `qzone like` | 管理员 ∪ 本机控制台 | `admin.admin_users` |
| 全部 LLM 工具 | 模型自主调用（无用户级鉴权；敏感操作如发说说受每日上限/开关约束） | `qzone.daily_max_posts`、`qzone.enabled` 等 |

> **会话级限制**：`admin.allowed_streams` 留空 = 不限制；填了名单后，不在名单内的会话发指令会被拒绝（提示「当前会话未启用日程功能」），注入也会一并受限。

## 五、可选增强开关（默认关）

两个子项都**默认关闭**：不配置（或保持 `false`）时插件行为与本版本之前逐字一致，既有配置无需任何改动。

| 配置项 | 默认 | 说明 |
|---|---|---|
| `[affection] weekly_milestones_enabled` | `false` | **周结大事记**：开启后周日的**关系日结**（睡眠时段触发、晚于情绪睡眠大修）按纯规则汇总最近 7 天，生成 1 条「本周关系温度：…（净变化 +0.0）」；`\|净变化\| ≥ 1.0` 时再追加 1 条「升温/降温明显」。结果写入好感档案 `week_log`（滚动 8 天）与 `milestones`（cap 20 条；同一周日**幂等**，重复日结不重复生成），并让 planner 注入与 `/lbm_mai_affection` 的记忆行多出一行「【本周大事记】日期 文案」（只取最近 1 条）。**零 LLM 成本**；周内日结记录不足 3 天时不生成；日结本身失败不落里程碑；周结步骤异常不影响日结主流程 |
| `[emotion] emoji_enabled` | `false` | **emoji 情绪联动**：按当前情绪首个标签名（取不到时回退情绪底色文本）经宿主 `emoji.get_by_description` 取一个表情。**注意**：当前**没有任何组件消费这个能力**（相关的情绪查询工具已移除），服务层实现保留；如需恢复该能力，注册一个消费组件即可。关闭时输出与旧版逐字一致 |

> 说明：`emoji.get_by_description` 已写入 `_manifest.json` 的 `capabilities`（仅声明，默认不使用）。`statistics` 成本看板不做。
> 组件面：29 = 8 命令 + 10 Hook + 7 Tool + 4 API，两个开关均为**配置项**。

## 六、情绪评估模式（会话片段制，默认 `episode`）

`replyer.after_response` 的情绪增量评估默认**不再每回复一次**，改为「会话闲置后整段评估」：

| 配置项 | 默认 | 区间 | 说明 |
|---|---|---|---|
| `[emotion] evaluate_mode` | `episode` | episode / per_reply / off | `episode`=会话闲置后整段评估（零 LLM 只登记，成本封顶，默认）｜`per_reply`=旧模式，每回复评估（60s 节流）｜`off`=不评估 |
| `[emotion] episode_idle_minutes` | 10 | [1, 720] | episode 模式：会话闲置多少分钟后冲刷该流片段；扫描精度 = `check_interval_minutes`（睡眠检测循环，默认 10min） |
| `[emotion] evaluate_max_per_day` | 10 | [0, 200] | 每日评估上限（次，两种模式共用，硬封顶；0 = 当日不评估）；超限片段**丢弃不并入次日**并落决策日志（`reason` 含 `daily_cap`） |
| `[emotion] evaluate_on_reply` | true | — | **已废弃**（迁移为 `evaluate_mode`）：显式设置 `evaluate_mode` 时本字段被忽略 |

- **迁移口径**：未显式设置 `evaluate_mode` 时按旧布尔迁移——`evaluate_on_reply=true → episode`、`false → off`。
- **一次性迁移风险（升级须知）**：宿主 UI 在保存配置时会把 `evaluate_mode` 以当前值写进配置文件（此后视为"显式设置"）。若旧配置里 `evaluate_on_reply=false`，**先**把 `evaluate_mode` 显式设为 `off` 再保存一次 UI；否则保存后 `evaluate_mode` 会被写死为默认 `episode`，旧的 `false` 不再生效。
- **片段制细节**：Hook 只登记（纯内存、零 LLM）→ 闲置冲刷该流自上次评估以来的完整往来（`message.get_by_time_in_chat`，bot+user 全量）→ 一次评估整段 → 入账（±0.5 限幅与跨来源去重口径不变，粒度从"每回复"变"每片段"）。片段上限 40 条 / 3000 字，**超出时保留最近的往来**并在片段头部标注省略；进评估器时正文预算放宽到片段上限（评估器默认的 600 字/保留头部是 `per_reply` 旧语义）。
- **睡眠入口**：进入睡眠时段时**先冲刷全部未结片段再跑睡眠裁判**，保证大修读到完整日志。
- **成本**：典型日 20 回复/6 片段 ≈ 旧 20×700 tok → 新 6×1000 tok；封顶后最坏 ≈ 10k tok/日。
- 说明：`evaluate_mode` 的"是否显式设置"取自宿主配置模型的 `model_fields_set`（即「这次配置里是否显式写了这个键」）。

## 七、赖床提醒

夜间被重要的人吵醒、又回到睡眠之后，bot 会收到**一条只在本轮有效的临时提醒**（不强制、不进下一轮上下文）。

**触发（全部满足才发）**：①该打扰段**确认成立过**（窗口内 ≥ `interrupt_reply_threshold` 次 target 私聊 reply）且以再次睡着闭合（`最后一次 reply + resleep_minutes` 内无新 reply）；②本睡眠周期有效提醒次数 < `nap_reminder_max_per_day`；③当前仍在睡眠窗口内；④`[schedule] no_sleep_mode == false`；⑤`[planning_proactive] nap_reminder_enabled`。每段至多提醒一次，被拦时落 `emotion.nap_reminder_blocked`（`block_reason` 区分 quota / budget / gate_* / trigger_*）。

**提醒文本（逐字模板，`{nap_tool_name}` 引用赖床工具名常量）**：

> 【临时提醒·不进下一轮上下文】你半夜被重要的人吵醒了，现在是 {now_hhmm}；你本来打算睡到
> {planned_wake_hhmm}。如果还想再睡一会儿，可以调用 {nap_tool_name} 把起床时间往后推（最晚
> {lie_in_max_until}）。这条提醒只在本轮有效，不必向任何人提起"昨晚被吵醒"。是否调用由你决定。

- `{planned_wake_hhmm}` = **当前睡眠窗口结束**（周期在册端点优先，回退 `[schedule] wake_time` 锚点）；
- 发送走**统一主动链**：`ctx.maisaka.proactive.trigger(stream_id=该段末条 reply 流, intent="nap_reminder", reason=文本, metadata={"kind": "nap_reminder"})`，调用前查主动打扰预算（仅目标陪伴私聊），**纳入打扰预算**；预算拒绝 → 不发送、不计数；
- **不进下一轮**：提醒块由宿主写进该流的内存历史（per-stream，不落库），本插件在注入 Hook 的既有剥离点新增 `is_consumed_ephemeral_proactive_block`——块龄 > 120 秒即从请求载荷剔除（下一轮 planner 起不再出现）；入队超 30 分钟的块仍由原有规则兜底剥离，只影响本插件的 `kind=nap_reminder` 块，不误伤其他主动块与真实消息；
- **提醒轮豁免**：提醒发出后 120 秒内该流若意外触发 reply，**不刷新再次睡着计时、不重开吵醒态、不额外限制工具调用**。

| 配置项 | 默认 | 约束 | 说明 |
|---|---|---|---|
| `[planning_proactive] nap_reminder_enabled` | `true` | — | 赖床提醒开关；关闭后不提醒，但不阻止 bot 自行改日程 |
| `[planning_proactive] nap_reminder_max_per_day` | `2` | `ge=1` | 每**睡眠周期**（不是自然日）的有效提醒上限：提醒发送成功 + 工具**钳制后成功延长**（目标晚于当前醒沿）各计 1 次；用尽后只停提醒 |
| `[schedule] lie_in_max_until` | `12:00` | `HH:MM` | 赖床硬上限时刻：工具传入超过它会被钳到它，提醒文本给出的也是它 |

## 八、早晚安问候（漂移触发）

早/晚安**只配漂移区间**：每聊天流每日独立掷点一次，到点按「必要性激活」决定是否真的开口。

**驱动链（一句话）**：`[planning_proactive]` 的漂移机制在 planning 的 60 秒循环里掷点 → 到点查该流是否有人说话 → 统一主动链 `ctx.maisaka.proactive.trigger`。早安/晚安**各只有这一条驱动链**：`[patrol]` 不再自行判时，只按 `affection` 的每日已发标记去重；**例行问候默认免主动打扰预算**（闸门与计数一起跳，不查预算也不计用量），活动切换/想念/日常等非问候主动行为仍照旧计入主动打扰预算（仅目标陪伴私聊）。

| 配置项 | 默认 | 约束 | 说明 |
|---|---|---|---|
| `[planning_proactive] greeting_target_enabled` | `true` | — | **目标陪伴私聊**早/晚安开关：目标 QQ **不看任何名单，必定触发**（例外：静默时段、当天该流已发过同类问候、本开关关闭） |
| `[planning_proactive] greeting_group_enabled` | `true` | — | **群聊**早/晚安开关：受下面两张群聊名单与必要性激活限制（`greeting_require_activation`） |
| `[planning_proactive] greeting_group_blacklist` | 空 | 只收裸群号 | 群聊问候黑名单：命中即不发；**留空 = 全发**（受激活限制）。带前缀/非数字/空串/重复项忽略并记一条汇总告警；**不支持通配** |
| `[planning_proactive] greeting_group_whitelist` | 空 | 只收裸群号 | 群聊问候白名单：非空时**仅这些群**生效；留空 = 不过滤（全发）；`["*"]` = 显式全发；`*` 与具体号混用 → 全放行 + 告警 |
| `[planning_proactive] greeting_other_private_enabled` | `false` | — | **非目标私聊**早/晚安开关（早/晚共用）：开启后还需命中名单且前一日该流消息数 ≥ 门槛 |
| `[planning_proactive] greeting_other_private_whitelist` | 空 | 只收裸 QQ 号 | 非目标私聊问候白名单：**留空 = 全不发**（默认安全的收紧）；`["*"]` = 不过滤（对已知私聊流生效） |
| `[planning_proactive] greeting_other_private_blacklist` | 空 | 只收裸 QQ 号 | 非目标私聊问候黑名单：命中即不发；不支持通配 |
| `[planning_proactive] greeting_other_private_min_messages` | `5` | `ge=5`（**不允许低于 5**，配置校验直接拒绝） | 非目标私聊问候门槛（早/晚共用）：**前一日**该流消息数达到该值才成为候选；取不到计数按 0 处理（不发送） |
| `[planning_proactive] greeting_exempt_budget` | `true` | — | 例行问候免主动打扰预算：开启后早/晚安触发**既不查预算闸门也不计主动打扰用量**（闸门与计数一起跳）；关闭则照旧受预算约束 |
| `[planning_proactive] morning_drift` | `"10-30"` | `^\d+(-\d+)?$` | 早安漂移区间（分钟）：触发时刻 = 睡眠窗口结束（`[schedule] wake_time` 锚点）+ `randint(a,b)`；单数字 `N` = `0~N` 随机；`a>b` 自动交换；**负数/非法回退默认区间**（不收负数） |
| `[planning_proactive] night_drift` | `"10-30"` | 同上 | 晚安漂移区间（分钟）：触发时刻 = 当天最后一个非睡眠日程条目结束 − `randint(a,b)`（向前漂移）；无日程回退 `[schedule] sleep_time` 锚点 |
| `[planning_proactive] greeting_require_activation` | `true` | — | 群聊必要性激活：到点需先观察到「自基准点起该流有人发过消息」才问候；无人则推迟到下一轮，到期限仍无人即放弃（当天不再补发） |

**被取代的旧键**（仍可继续读到，详见「十、配置键总表」的兼容映射表）：`enable_night_greeting` **显式 false = 晚安全灭（三类都不发）**、显式 true / 缺省 = 三类新开关接管；`morning_other_private_enabled` **显式 true** 归并生效为 `greeting_other_private_enabled`（早/晚共用；新键显式时新键优先）；`morning_other_private_min_messages` 显式值沿用为新门槛 `greeting_other_private_min_messages` 的缺省（新键显式优先）。`enable_morning_greeting` 与 `morning_greeting_require_activation` 亦为纯兼容字段。旧键存在不报错。

**基准与期限**

| 问候 | 基准点 | 触发时刻 | 放弃期限 |
|---|---|---|---|
| 早安 | 睡眠窗口结束（`[schedule] wake_time` 锚点） | 基准 + `randint(a,b)` | 睡眠结束后**第一个非睡眠**日程条目的结束；无日程 → 基准 + 120 分钟 |
| 晚安 | 当天最后一个非睡眠日程条目的结束（无日程 → `sleep_time` 锚点） | 基准 − `randint(a,b)` | `min(睡眠窗口开始 sleep_time, [patrol] silence_start)`，取先到者 |

- **每流每日一次**：`(stream_id, 日期, 早安/晚安)` 只掷一次点，同日重复决策**复用同一结果**；热重载后允许重掷（纯内存、不落盘）。**重掷不等于重发**：planning 发送闸在触发前补查 `affection` 的按天标记——**目标陪伴私聊**该类型当日已发 → 视为已触发（落 `skip`「持久化标记命中，热重载去重」行）不再重发；标记按天全局且**仅目标类写入**，故群聊/非目标私聊**跨热重载去重未实现**——重载后同流同日仍可能再发一条。
- **跨午夜条目**（结束 > 24:00）按绝对时间戳处理。
- **锚点参照点**：晚安的「入睡锚点 / `silence_start`」按**今天的起床锚点**取"今晚的那一次"——常规作息得到当天时刻，夜猫子作息（`sleep < wake`，日程延续到次日凌晨）自动落到次日凌晨，不会误判成"窗口已过"。
- **延迟补发**：循环因重启等原因错过触发时刻时，只要未到期限仍会在下一个 tick 补发；已过期限即记放弃（`abandoned`），当天不再评估。

**覆盖范围（三类触发矩阵）**

| 会话类型 | 开关 | 名单 | 附加条件 |
|---|---|---|---|
| 目标陪伴私聊（`[target] target_qq`） | `greeting_target_enabled`（默认 **true**） | **豁免名单**（不看任何黑白名单） | 无（必定触发） |
| 群聊 | `greeting_group_enabled`（默认 **true**） | `greeting_group_blacklist`（空 = 全发）+ `greeting_group_whitelist`（非空 = 仅白名单；`["*"]` = 不过滤） | **必要性激活**（`greeting_require_activation`） |
| 非目标私聊 | `greeting_other_private_enabled`（默认 **false**） | 白名单 `greeting_other_private_whitelist`（**空 = 全不发**）+ 黑名单命中即不发 | 前一日该流消息数 ≥ `greeting_other_private_min_messages` |

名单**只收裸群号 / 裸 QQ 号**（带前缀、含非数字、空串、重复项一律忽略并记一条汇总告警，落决策日志；同签名每日一次）；黑名单不支持通配；`*` 与具体号混用 → 全放行 + 告警。与活动切换主动开口的 `proactive_group_ids` / `proactive_other_streams`（支持 `session:` / `qq:private:` 前缀）**两套名单不混用**。

**预算与去重**：问候默认**免主动打扰预算**（`greeting_exempt_budget=true`：不查闸门、不记用量）；关闭免预算后照旧走预算、预算拒绝时当轮不发送、掷点状态不变（下一轮继续尝试，到期限自然放弃）。目标私聊触发成功后写 `affection` 的 `morning_sent`/`night_sent` 每日标记：`[patrol]` 见已发即不再触发，**planning 发送闸亦在触发前查同一标记**（命中 → 视为已触发 + `skip` 去重行）——同一**目标**私聊流同一天早安/晚安**各至多一条（含热重载/重启后）**。非目标类（群聊 / 非目标私聊）不写该标记，跨热重载去重**未实现**（见上「每流每日一次」条）。

**[patrol] 的四个时间窗字段已废弃**：`morning_start` / `morning_end` / `night_start` / `night_end` 保留在配置里**仅作兼容读取**（仍参与 `HH:MM` 格式校验、非法值照旧落 `invalid_time_config` 留痕），**不再参与任何触发判定**；实际时刻由上面的漂移机制决定。`[patrol]` 的静默时段、每日发言上限（含「未发晚安预留 1 个预算位」）与主动打扰预算（仅目标陪伴私聊）照旧生效；`[patrol] min_interval_exempt_greetings`（早晚安豁免最小间隔）随 patrol 判时下线而**不再有消费点**（字段保留，间隔约束由漂移机制自身的基准/期限承担）。

## 九、日程生成锚点

新一天的日程**不再按配置的绝对时刻生成**，而是"**最后日程结束后多久生成**"——按相对锚点判定，
并叠加一条"至少在新一天开始前 30 分钟生成"的硬约束。

**一句话驱动链**：后台循环每轮算"下一个待生成目标日 + 它的到期时刻"→
到点且该目标日无日程 → 用 LLM 生成并落库（当天已有日程则跳过）；完全静默，不发任何消息。

- **目标日（"新一天"）**：当日帧（帧起点 = `[schedule] wake_time` 锚点，帧跨越午夜）的**次日**——
  例：wake 07:00 时，00:30 生成的是"今天"的日程（凌晨仍属前一天帧），22:30 生成的是"明天"的日程。
- **基准**：当天**最后一个非睡眠日程条目**的结束时间；当天没有日程 → 回退 `[schedule] sleep_time`
  锚点（"有日程用日程、无日程用锚点"）。"非睡眠条目"的主判 `goal_type=sleep`、名称兜底**仅夜间词**
  （睡觉/睡眠/安睡；午休/午睡/小憩/打盹不算）——与赖床工具**同一份判定**，不再各留一份口径。
- **硬约束**：`到期时刻 = min(基准 + 延迟, 新一天起点 − 30 分钟)`；相对延迟算出的时刻更晚 →
  **强制拉回**该阈值（保证新一天开始前至少 30 分钟已生成）。

| 配置项 | 默认 | 约束 | 说明 |
|---|---|---|---|
| `[schedule] auto_generate_after_last_end_minutes` | `90` | `ge=0` | 生成延迟（分钟）：最后日程结束后多久生成新一天日程；无日程回退 `sleep_time` 锚点。默认 90 在缺省入睡锚点 23:00 下等价旧的 `00:30`（老配置零感知迁移） |
| `[schedule] auto_generate_time` | `00:30` | `HH:MM` | **已废弃** → 改用上面的延迟字段。保留仅作**兼容读取**：仍配置它时按绝对时刻理解，且同样受「新一天起点 − 30 分钟」硬约束钳制；**两个字段都被显式配置时以 `auto_generate_after_last_end_minutes` 为准**（判定用 `model_fields_set`，与 `evaluate_mode`/`evaluate_on_reply` 的迁移惯例一致） |
| `[schedule] auto_generate_enabled` | `true` | — | 自动生成总开关（关掉后不跑循环；目标日已有日程照旧跳过） |

**防重**：到期触发后记"已生成的目标日"，同一天不再触发；下一次触发自然顺延到次日同点（跨日重新判定）。

## 十、配置键总表

共 **13 个配置段、167 个配置键**。下表与插件的配置模型逐字段对齐（键名 / 类型 / 默认值 / 约束）；若与运行行为不一致，以代码为准。括号内为数量。

### [plugin] 插件（2 键）

| 键名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 是否启用插件 |
| `config_version` | str | `"1.0.0"` | 配置版本（与插件版本同步；WebUI 中隐藏且只读） |

### [target] 目标用户（1 键）

| 键名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `target_qq` | int | `123456789` | 陪伴对象私聊的 QQ 号（主动聊天、想念、好感度都只对这个会话生效）；保持占位值时不会主动私聊任何人 |

### [admin] 管理与日志（5 键）

| 键名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `admin_users` | list[str] | `[]` | 命令管理员（QQ 号或 `qq:123456` 形式）；留空 = 仅本机控制台可用管理子命令 |
| `allowed_streams` | list[str] | `[]` | 命令/注入允许的会话（session_id）；留空 = 不限制 |
| `llm_log_enabled` | bool | `true` | 记录 LLM 调用日志（JSONL，按天分文件） |
| `decision_log_enabled` | bool | `true` | 记录巡检/空间/规划决策日志（JSONL，按天分文件） |
| `log_retention_days` | int | `3`（`ge=1 le=30`） | 日志保留天数 |

### [schedule] 日程系统（37 键）

| 键名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 日程规划模块总开关：关闭后停止日程生成/注入/主动行为（工具与命令返回未启用提示） |
| `wake_time` | str | `"07:00"` | 起床时间锚点（HH:MM），清醒时段的活动从该时间开始安排；入睡早于起床表示跨午夜夜猫子作息 |
| `sleep_time` | str | `"23:00"` | 入睡时间锚点（HH:MM），这一刻之后到次日起床是睡眠时段；无睡眠模式下该时段不生成日程 |
| `no_sleep_mode` | bool | `false` | 无睡眠模式：入睡到起床的时段不生成任何日程（提示词要求 + 生成后销毁残留条目，与边界重叠的截断到边界），且该时段内主动行为不触发 |
| `auto_generate_enabled` | bool | `true` | 每天到点自动用 LLM 生成次日日程（当天已有日程则跳过） |
| `auto_generate_after_last_end_minutes` | int | `90`（`ge=0`） | 自动生成时刻（相对锚点）：当天最后一个**非睡眠**日程条目结束后 N 分钟生成新一天日程；当天没有日程时以入睡锚点为基准；硬约束「不晚于新一天起点前 30 分钟」 |
| `auto_generate_time` | str | `"00:30"` | 【已废弃】旧的「每日自动生成的绝对时刻」（HH:MM）；保留仅作兼容读取，两字段都被显式配置时以上一项为准 |
| `min_activities` | int | `6`（`ge=1 le=20`） | 生成的日程最少活动数 |
| `max_activities` | int | `12`（`ge=1 le=25`） | 生成的日程最多活动数 |
| `custom_prompt` | str | `""` | 自定义日程生成提示词：角色的长期生活状态/持续阶段（如「正在环游世界」「备考研究生」），留空使用默认风格 |
| `use_multi_round` | bool | `true` | 启用多轮生成（首轮质量不达标时按反馈重试） |
| `max_rounds` | int | `2`（`ge=1 le=5`） | 多轮生成最大轮数（越多越费 token） |
| `quality_threshold` | float | `0.85`（`ge=0.0 le=1.0`） | 多轮生成质量阈值（达到此分即提前停止重试） |
| `enable_detailed_description` | bool | `true` | 启用详细活动描述（关闭后生成、注入、命令展示都不显示长描述） |
| `min_description_length` | int | `20`（`ge=5`） | 活动描述最小字符数（20-30 较合理，过短易模糊） |
| `max_description_length` | int | `50`（`ge=5`） | 活动描述最大字符数（50-100 较合理，过长占 token） |
| `max_tokens` | int | `8192`（`ge=1000`） | LLM 生成日程的最大 token 数（过小可能截断日程） |
| `recent_schedule_days` | int | `3`（`ge=1 le=14`） | 生成新日程时回看最近 N 天的日程作为去重参考（N=1 只看昨天） |
| `history_message_limit` | int | `30`（`ge=0`） | 生成日程时回读各会话当天最近 N 条聊天消息作为上下文；0 = 禁用 |
| `knowledge_search_limit` | int | `5`（`ge=0`） | 生成日程时检索知识库的最大条数；0 = 禁用 |
| `cross_day_activity` | bool | `true` | 启用跨天活动（time_window 跨夜的写法 + 注入时回读昨日跨夜活动） |
| `llm_task_name` | str | `"replyer"` | 日程生成使用的模型任务名（utils/replyer/planner 等，走宿主任务路由） |
| `generation_timeout` | float | `180.0`（`ge=10.0`） | 日程生成 LLM 调用超时（秒），推荐 120-300（避免被宿主默认 30s 截断） |
| `role_judge_enabled` | bool | `true` | 角色裁判模式：自然语言日程请求由 LLM 扮演 bot 判断接不接（today=调整今天；future=写入未来约定；reject=拒绝）；判定失败安全降级为日程不变。关闭后所有请求一律记为未来约定 |
| `role_judge_temperature` | float | `0.3`（`ge=0.0 le=2.0`） | 角色裁判 LLM 温度（越低越保守，更倾向拒绝离谱请求） |
| `inject_into_planner` | bool | `true` | 在 planner 决策时把当前活动注入 items 上下文（SystemMessageItem 快照协议） |
| `inject_into_replyer` | bool | `true` | 在 replyer 调 LLM 前把当前活动作为 extra_prompt 注入；与 planner 注入共享冷却，不会两阶段连刷 |
| `inject_scope` | Literal[`target_private`, `all`] | `"target_private"` | 注入范围：target_private=只注入目标私聊；all=所有会话 |
| `max_future_activities` | int | `3`（`ge=0`） | 智能注入时最多显示的未来活动数量；0 = 不显示 |
| `inject_cache_minutes` | int | `15`（`ge=1 le=60`） | 当前活动查询的时间窗口缓存（分钟） |
| `cache_ttl` | int | `300`（`ge=10`） | 日程注入缓存有效期（秒，默认 5 分钟）；过期后重查数据库 |
| `cache_max_size` | int | `100`（`ge=10`） | 注入缓存最大条目数（LRU 策略，超出按最近最少使用淘汰） |
| `list_draw_image` | bool | `true` | `/lbm_plan list` 是否绘制日程图片；关闭后只返回文字长文本 |
| `image_timeout_seconds` | float | `15.0`（`ge=1.0`） | 日程图片绘制超时（秒）；超时静默放弃图片并降级纯文字（不提示绘制失败） |
| `timezone` | str | `"Asia/Shanghai"` | 时区设置（IANA 命名，如 Asia/Shanghai、UTC、America/New_York） |
| `emotion_influence` | float | `0.6`（`ge=0.0 le=1.0`） | 情绪对日程的影响强度（0~1，0=关闭）：睡醒情绪进日程生成提示词，同时决定睡眠大修裁判增量的动态限幅 |
| `lie_in_max_until` | str | `"12:00"` | 赖床（推迟起床）的硬上限时刻（HH:MM）；传入时间超过本值会被钳到本值，提醒文本给出的也是本值；格式非法一律按 12:00 处理 |

### [inject] 智能注入策略（5 键）

| 键名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enable_intent_classification` | bool | `true` | 启用意图分类（识别问当前 / 问未来 / 闲聊 / 技术问答等，决定注入的详细程度与是否注入） |
| `enable_inject_optimization` | bool | `true` | 启用注入优化器（防止重复注入和无效打扰，planner / replyer 共用冷却） |
| `casual_chat_inject_probability` | float | `0.5`（`ge=0.0 le=1.0`） | 闲聊场景（非直接询问 / 非技术问答）的注入概率 |
| `context_max_turns` | int | `3`（`ge=1`） | 对话上下文保留轮数（用于判断是否在连续讨论日程话题） |
| `context_ttl` | int | `600`（`ge=60`） | 对话上下文过期时间（秒），超时后认为是新会话 |

### [planning_proactive] 日程主动行为（24 键）

| 键名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `proactive_group_ids` | list[str] | `[]` | 群聊主动行为白名单：直接填群号（如 `["123456"]`），无需前缀；留空 = 所有群聊生效 |
| `proactive_other_streams` | list[str] | `[]` | 其他会话主动行为白名单：按 `qq:private:789` / `session:xxx` 格式填写；留空 = 不含其他会话 |
| `proactive_fresh_window_minutes` | int | `10`（`ge=1 le=60`） | 主动行为触发窗口（分钟）：活动开始后每个会话在窗口内获得独立随机延迟，延迟结束即触发；错过窗口不补发 |
| `enable_proactive_trigger` | bool | `true` | 活动切换主动发起：活动开始后在触发窗口内让 bot 主动开口（每会话每活动每天 1 次）。不覆盖当天睡醒后的第一个活动，睡眠时段内不触发 |
| `enable_morning_greeting` | bool | `false` | （旧字段）仅参与「本轮是否需要评估主动行为」的入口判定，不决定早安是否发出——早安由三类问候开关与漂移机制决定 |
| `morning_greeting_require_activation` | bool | `false` | （旧字段）群聊早间问候是否需激活；与 `greeting_require_activation` **取或**——旧字段开启时同样生效 |
| `enable_frequency_modulation` | bool | `true` | 按当前活动类型动态调节聊天频率（学习/工作时 30%，休息/娱乐时 130%~150%） |
| `nap_reminder_enabled` | bool | `true` | 赖床提醒开关：夜间被吵醒且重新睡着后，向 bot 发一条临时提醒（点名赖床工具；只在当轮有效）。关闭后不提醒，但不阻止 bot 自行改日程 |
| `nap_reminder_max_per_day` | int | `2`（`ge=1`） | 每**睡眠周期**（不是自然日）的有效提醒次数上限：提醒发送成功 + 工具成功改日程各计 1 次；用尽后不再提醒，但仍允许 bot 自行推迟起床 |
| `morning_drift` | str | `"10-30"`（`^\d+(-\d+)?$`） | 早安漂移区间（分钟，相对 `[schedule] wake_time` 锚点）：填 `"a-b"` 或单数字 N（= 0~N 随机）；负数/非法一律回退默认区间 |
| `night_drift` | str | `"10-30"`（同上） | 晚安漂移区间（分钟，相对当天最后一个非睡眠条目结束）：触发时刻 = 基准 − `randint(a,b)`；负数/非法一律回退默认区间 |
| `enable_night_greeting` | bool | `false` | （旧字段）显式 `false` = 晚安全灭（三类都不发）；显式 `true` / 未配置 = 由三类新开关接管 |
| `morning_other_private_enabled` | bool | `false` | （旧字段）其他私聊的早安开关；显式 `true` 归并生效为 `greeting_other_private_enabled` |
| `morning_other_private_min_messages` | int | `5`（`ge=5`） | （旧字段）其他私聊早安的前一日消息数门槛；显式值作为 `greeting_other_private_min_messages` 的缺省 |
| `greeting_require_activation` | bool | `true` | 问候的必要性激活：**群聊**到点需先观察到「自基准点起该流有人发过消息」才问候，无人则推迟到下一轮、到期限仍无人即放弃（当天不再补发）；关闭则到点直接问候。目标私聊不受本项影响，非目标私聊由消息数门槛承担 |
| `greeting_target_enabled` | bool | `true` | 目标陪伴私聊的早/晚安开关：目标 QQ **不看任何名单，必定触发**（例外：静默时段、当天该流已发过同类问候、本开关关闭） |
| `greeting_group_enabled` | bool | `true` | 群聊早/晚安开关：受群名单与必要性激活限制；两名单留空 = 所有群聊生效（受激活限制） |
| `greeting_group_blacklist` | list[str] | `[]` | 群聊问候黑名单（只收**裸群号**）：命中即不发；留空 = 全发。带前缀、含非数字、空串、重复项被忽略并记一条汇总告警；不支持通配 |
| `greeting_group_whitelist` | list[str] | `[]` | 群聊问候白名单（只收**裸群号**）：非空时仅这些群生效；留空 = 不过滤（全发）；`["*"]` = 显式全发；`*` 与具体号混用按全放行处理并告警 |
| `greeting_other_private_enabled` | bool | `false` | 非目标私聊的早/晚安开关（早/晚共用）：开启后还需命中名单且前一日该流消息数达到门槛 |
| `greeting_other_private_whitelist` | list[str] | `[]` | 非目标私聊问候白名单（只收**裸 QQ 号**）：**留空 = 全不发**；`["*"]` = 不过滤（对已知私聊流生效） |
| `greeting_other_private_blacklist` | list[str] | `[]` | 非目标私聊问候黑名单（只收**裸 QQ 号**）：命中即不发；不支持通配 |
| `greeting_other_private_min_messages` | int | `5`（`ge=5`） | 非目标私聊问候的前一日消息数门槛（早/晚共用）：达到该值才成为候选；取不到计数按 0 处理（不发送） |
| `greeting_exempt_budget` | bool | `true` | 例行问候免主动打扰预算：开启后问候触发**既不查预算闸门也不计用量**；关闭则照旧受预算约束（想念/日常/活动切换等非问候行为始终受约束） |

### [planning_cleanup] 日程后台清理（2 键）

| 键名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `cleanup_interval` | int | `3600`（`ge=60`） | 后台清理循环间隔（秒，默认 1 小时） |
| `cleanup_old_goals_days` | int | `30`（`ge=1`） | 保留多少天前的已完成 / 取消目标（超过自动删除） |

### [patrol] 主动巡检（28 键）

| 键名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 陪伴模块总开关：关闭后停止巡检主动聊天与情绪上下文注入（命令与工具返回未启用提示） |
| `relation_type` | Literal[`恋人`, `挚友`, `维护者`, `家人`, `自定义`] | `"恋人"` | 目标用户是 bot 的谁：影响想念提示词、消息口吻、好感度档位名称与消息后缀 |
| `relation_custom` | str | `""` | 自定义关系的称谓（关系类型选「自定义」时生效，如：姐姐、师兄、搭子） |
| `check_interval_minutes` | int | `10`（`ge=1 le=120`） | 巡检循环间隔（分钟）：每隔多久判断一次该不该主动说话 |
| `proactive_trigger_enabled` | bool | `true` | 触发方式开关：开启 = 经 `maisaka.proactive.trigger` 唤醒 planner（人格化表达）；关闭 = 完全不主动 |
| `daily_max_speak` | int | `5`（`ge=0 le=50`） | 每日主动发言预算（次）；未发晚安时为晚安预留 1 次；0 = 完全静音 |
| `proactive_target_daily_cap` | int | `0`（`ge=0 le=200`） | 目标陪伴私聊的每日主动打扰上限（次）：仅统计发往目标陪伴私聊的主动打扰；0 = 自动取「每日预算 + 2」（旧键名 `global_proactive_cap` 兼容读取） |
| `min_trigger_interval_minutes` | int | `240`（`ge=0 le=1440`） | 任意两次主动发言之间的硬性最小间隔（分钟），0 = 不限制；对全部触发生效（早晚安/想念可用下面开关豁免） |
| `min_interval_exempt_greetings` | bool | `false` | 早安/晚安是否豁免「主动发言最小间隔」；**当前无消费点**（早晚安判时已下线，间隔约束由漂移机制自身承担） |
| `min_interval_exempt_miss` | bool | `false` | 想念是否豁免「主动发言最小间隔」；想念自身有沉默时长门槛，豁免可避免「当天已主动发言就永远说不了想你」 |
| `user_cooldown_minutes` | int | `30`（`ge=0 le=180`） | 用户冷却（分钟）：对方最后一次发言后多久内不主动打扰（想念/节点/日常受控，早晚安不受控） |
| `morning_start` | str | `"07:00"` | 【已废弃】早安触发窗口开始（HH:MM）；早晚安时刻改由 `[planning_proactive]` 漂移机制决定，字段保留仅作兼容读取 |
| `morning_end` | str | `"10:00"` | 【已废弃】早安触发窗口结束（HH:MM）；同上 |
| `night_start` | str | `"22:00"` | 【已废弃】晚安触发窗口开始（HH:MM）；同上 |
| `night_end` | str | `"23:59"` | 【已废弃】晚安触发窗口结束（HH:MM）；同上 |
| `silence_start` | str | `"00:00"` | 静默时段开始（HH:MM），静默期内巡检不触发（支持跨午夜；配置非法按静默处理） |
| `silence_end` | str | `"07:30"` | 静默时段结束（HH:MM） |
| `default_speak_rate` | float | `0.5`（`ge=0.0 le=1.0`） | 日常搭话触发概率（巡检命中且无当前活动时） |
| `activity_trigger_rate` | float | `0.6`（`ge=0.0 le=1.0`） | 日程节点分享概率（巡检命中当前日程活动时） |
| `miss_enabled` | bool | `true` | 想念机制：目标用户沉默超过阈值后按概率触发想念发言（每天最多一次） |
| `miss_hours_min` | float | `4.0`（`ge=0.5 le=72.0`） | 想念触发区间下限（小时）：沉默不足该时长绝不触发；写反时自动交换 |
| `miss_hours_max` | float | `8.0`（`ge=0.5 le=72.0`） | 想念触发区间上限（小时）：区间内每轮巡检随机取阈值，沉默越久越容易触发 |
| `miss_speak_rate` | float | `0.5`（`ge=0.0 le=1.0`） | 想念触发概率 |
| `miss_avoid_future_schedule` | bool | `false` | 想念是否避开未来日程：开启后未来窗口内有日程节点则本轮不打扰（默认关闭 = 不看日程） |
| `miss_future_schedule_hours` | float | `2.0`（`ge=0.0 le=72.0`） | 「想念避开未来日程」的窗口长度（小时） |
| `miss_llm_check_enabled` | bool | `true` | 想念触发前用 LLM 以角色身份把关（回答 Y 才触发），驳回后 30 分钟内不再重复判断 |
| `miss_reason_prompt` | str | `"你已经有 {hours} 个小时没收到{relation}的消息了，你有点想TA了。可以主动开口问问近况、表达一下想念；但不必强行找话题，如果觉得此刻开口不自然，平淡地打个招呼也可以。"` | 想念触发时传给 planner 的提示文本；可用占位符 `{lover_name}`、`{hours}` |
| `miss_confirm_prompt` | str | `"你是“{lover_name}”，而对方是你的{relation}（人设：{personality}）。现在是 {current_time}，你上次收到TA的消息已经是 {hours} 小时前。{activity_context}<br>请站在“{lover_name}”的角度判断：此刻主动发一条关心TA近况的消息是否自然、是否体贴？<br>- 深夜TA可能睡了、TA可能在忙、或你觉得突兀 → 回复 N<br>- 你确实想TA了、此刻开口很自然 → 回复 Y<br>只回复一个大写字母：Y 或 N。"` | 想念触发前 LLM 把关的提示词模板（回复 Y 才触发）；占位符 `{lover_name}`、`{personality}`、`{current_time}`、`{hours}`、`{activity_context}` |

### [affection] 好感度（14 键）

| 键名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `current_level` | Literal[`0`, `1`, `2`] | `0` | 好感度档位（旧三档口径）：0=熟悉（温柔拘谨），1=亲密（活泼热情），2=热恋（撒娇抱抱） |
| `enabled` | bool | `true` | 启用好感度 v2：按 score/baseline 连续分运行（规则事件记账 + 日衰减 + 档位迟滞）；关闭则回退旧的静态三档行为（档位仅由命令/工具设置，不自动变化） |
| `decay_pull` | float | `0.06`（`ge=0.0 le=1.0`） | 日衰减拉回基线的比例：每天日切执行 `score += (baseline − score) × 该值`（0 = 不衰减） |
| `baseline_ema` | float | `0.03`（`ge=0.0 le=1.0`） | 关系基线漂移速度：日切执行 `baseline += (score − baseline) × 该值`（0 = 基线永不移动） |
| `daily_positive_cap` | float | `4.0`（`ge=0.0 le=100.0`） | 单日正向好感增量上限（含规则事件与裁判）：超出部分记日志丢弃 |
| `daily_negative_cap` | float | `3.0`（`ge=0.0 le=100.0`） | 单日负向好感扣减下限（含规则事件与裁判）：超出部分记日志丢弃 |
| `tier_hysteresis` | float | `3.0`（`ge=0.0 le=30.0`） | 档位迟滞带（分）：升档需 score ≥ 当前档上界 + 该值，降档需 ≤ 下界 − 该值（0 = 无迟滞） |
| `tier_min_dwell_hours` | float | `24.0`（`ge=0.0 le=720.0`） | 档位最短停留时长（小时）：距进入当前档位不足该时长则不升/降档（0 = 不限） |
| `review_enabled` | bool | `true` | 好感度 LLM 裁判总开关（会话采样评估 + 睡眠时段关系日结）；关闭则完全不做 LLM 评估（规则事件仍照常记账） |
| `review_sample_every_rounds` | int | `20`（`ge=1 le=1000`） | 会话裁判采样频率（轮）：每多少轮对方私聊消息抽样一次（与每日上限取先到者） |
| `review_max_per_day` | int | `2`（`ge=0 le=50`） | 会话裁判每日上限（次）：0 = 当天不做采样评估（不影响睡眠时段的关系日结） |
| `review_task_name` | str | `"utils"` | 好感裁判使用的 LLM 任务名（宿主任务键） |
| `emotion_modulation` | bool | `true` | 情绪调制好感增量：`delta × clamp(1 + 0.25 × V × sign(delta), 0.6, 1.4)`；情绪模块停用或读不到当前 V 时乘数恒 1 |
| `weekly_milestones_enabled` | bool | `false` | 周结大事记开关（默认关）：周日的日结后按最近 7 天关系温度与净变化汇总出 1~2 条里程碑，写入档案并随好感注入展示；纯规则零 LLM 成本 |

### [cateye] 陪伴对象电脑感知（9 键）

| 键名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `false` | 启用屏幕感知：每次主动触发前经 cateye.connect-hub 截图并转述；需要 cateye 插件与客户端在线 |
| `screenshot_blur` | int | `0`（`ge=0 le=64`） | 截图模糊半径（0-64）：0=清晰；介意隐私可设 30 左右（视觉模型仍能看出大概在干什么） |
| `vlm_task` | str | `"vlm"` | 截图理解使用的视觉模型任务名（宿主任务键） |
| `timeout_seconds` | float | `20.0`（`ge=5.0 le=120.0`） | 查看电脑（截图 + 视觉理解）整体超时（秒）；超时不阻塞巡检，本轮按不可用处理 |
| `describe_prompt` | str | `"这是你的陪伴对象电脑屏幕的截图。请用一句话（不超过 40 字、口语化）描述TA现在可能在干什么（比如在打游戏 / 写代码 / 看视频 / 挂机离开）。<br>如果截图上看不出具体内容，就回答：屏幕上看不出具体内容。"` | 屏幕截图的视觉理解提示词（随截图发给视觉模型，要求一句话描述） |
| `narration_template` | str | `"现在是{date} {time}，你看了眼{user_name}的电脑屏幕，TA正在：{description}"` | 看清楚时注入的旁白模板；占位符 `{date}`、`{time}`、`{user_name}`、`{description}` |
| `context_ttl_minutes` | int | `90`（`ge=0 le=1440`） | 屏幕旁白存活上限（分钟）：超过即丢弃不再注入；0 = 不限时（只随锚点滑出上下文失效） |
| `offline_narration_template` | str | `"现在是{date} {time}，你想看一眼{user_name}的电脑屏幕，但TA的电脑没开"` | 电脑没开（客户端未连接 / hub 不可用）时注入的旁白；留空 = 该情况不注入 |
| `failed_narration_template` | str | `"现在是{date} {time}，你看了眼{user_name}的电脑屏幕，但没看清TA在干什么"` | 截图/视觉理解失败时注入的旁白；留空 = 该情况不注入，整体超时按不注入处理 |

### [judge] LLM 判断（3 键）

| 键名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `model` | str | `""` | 判断用模型：填宿主模型任务名（utils/replyer/planner）或 WebUI 模型列表里的具体模型名；留空 = 跟随主任务（`[schedule] llm_task_name`） |
| `max_tokens` | int | `512`（`ge=16 le=8192`） | 判断调用的最大生成 token 数。**思考型模型会先消耗 token 输出推理内容**，预算太小会导致正文为空、判定被当作失败（按驳回处理） |
| `temperature` | float | `0.2`（`ge=0.0 le=2.0`） | 判断调用的采样温度（越低越稳定）；调高更容易出现模糊表述，导致结论解析失败（同样按驳回处理） |

### [qzone] QQ 空间（17 键）

| 键名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `false` | 启用 QQ 空间模块；需要 SnowLuma 适配器（NapCat 画像不支持 QZone API） |
| `check_interval_minutes` | int | `30`（`ge=5 le=360`） | 空间巡检间隔（分钟）：检查发说说的时机与新评论 |
| `daily_max_posts` | int | `2`（`ge=0 le=20`） | 每日最多发几条说说；0 = 不发 |
| `post_rate` | float | `0.5`（`ge=0.0 le=1.0`） | 每次巡检命中发帖时机时的实际发出概率 |
| `emotion_filter` | bool | `true` | 情绪低落时跳过发帖：当前情绪效价 V < −0.6 时本轮不发说说（低落不硬营业） |
| `post_by_schedule` | bool | `true` | 根据日程发说说：优先挑当前活动节点，把活动内容转化成生活动态 |
| `require_activity` | bool | `true` | 只在没有静默/睡眠且日程里有当前活动时才发说说 |
| `reply_comments_enabled` | bool | `true` | 监听自己说说下的评论并自动回复：走 Web CGI 直连读取评论正文与楼中楼（cookie 经适配器获取） |
| `reply_number` | int | `5`（`ge=1 le=20`） | 自动回评检查自己最近多少条说说下的评论 |
| `reply_probability` | float | `1.0`（`ge=0.0 le=1.0`） | 每轮自动回评的触发概率（1.0 = 每次巡检都检查） |
| `notify_new_comments` | bool | `true` | 回退路径：cookie 不可用、无法读评论正文时，检测到评论数变化经主动链提醒 |
| `llm_task_name` | str | `"utils"` | 说说文案/回评生成的模型任务名（utils/replyer 等，走宿主任务路由） |
| `silence_start` | str | `"01:00"` | 空间静默时段开始（HH:MM）：静默期内不发说说、不监听评论（支持跨午夜；配置非法按静默处理） |
| `silence_end` | str | `"08:00"` | 空间静默时段结束（HH:MM） |
| `napcat_http_host` | str | `""` | 备选：NapCat HTTP 服务器地址（cookie 获取降级通道 2，如 `127.0.0.1`） |
| `napcat_http_port` | str | `""` | 备选：NapCat HTTP 服务器端口（如 `9999`）；留空则不用该通道 |
| `napcat_http_token` | str | `""` | 备选：NapCat HTTP 服务器的 access token |

### [emotion] 情绪系统（20 键）

| 键名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 情绪模块总开关：关闭后停止情绪评估/注入/睡眠大修（命令与工具返回未启用提示） |
| `decay_rate` | float | `0.08`（`ge=0.01 le=0.3`） | 日常指数衰减率（每小时）：情绪向基线回落的速度；对话频繁可调高防无限累积 |
| `evaluate_on_reply` | bool | `true` | 【已废弃，迁移为 `evaluate_mode`】每次回复后评估该轮情绪增量；显式设置 `evaluate_mode` 时本字段被忽略 |
| `evaluate_mode` | Literal[`episode`, `per_reply`, `off`] | `"episode"` | 回复后情绪评估模式：episode=会话闲置后整段评估（默认，成本封顶）｜per_reply=每回复评估（旧模式，60s 节流）｜off=不评估 |
| `episode_idle_minutes` | int | `10`（`ge=1 le=720`） | episode 模式：会话闲置多少分钟后冲刷评估该流片段 |
| `evaluate_max_per_day` | int | `10`（`ge=0 le=200`） | 情绪评估每日上限（次，两种模式共用，硬封顶；0 = 当日不评估） |
| `evaluate_min_interval_seconds` | int | `60`（`ge=0 le=3600`） | 两次增量评估之间的最小间隔（秒），控制评估成本 |
| `evaluator_task_name` | str | `"utils"` | 增量评估使用的模型任务名（睡眠裁判默认见 `referee_task_name`） |
| `referee_task_name` | str | `"replyer"` | 睡眠裁判 LLM 的模型任务名（独立请求，不携带对话历史） |
| `global_event_multiplier` | float | `1.0`（`ge=0.1 le=3.0`） | 全局情绪事件幅度旋钮：作用于一切外部事件增量（不含日常衰减），运行期调控情绪整体波动大小 |
| `non_target_multiplier` | float | `1.0`（`ge=0.0 le=1.0`） | 非 target 事件系数（群聊等非重要他人引发的事件）：调低可淡化群聊影响 |
| `target_bonus_step` | float | `0.1`（`ge=0.0 le=1.0`） | target 每档情绪加成步长（加算）：加成 = step × (档位+1)，档 0~4 对应 +0.1~+0.5 |
| `injection_enabled` | bool | `true` | 把当前情绪底色（模糊化描述）注入 replyer 提示词，引导语气与情绪一致 |
| `dedup_enabled` | bool | `true` | 跨来源去重：自动评估与 `lbm_emotion_report` 工具对同一事件的重复入账只计一次 |
| `dedup_window_seconds` | int | `180`（`ge=0 le=3600`） | 跨来源去重时间窗（秒）：同一聊天流内两条来源先后入账超过该间隔则各自计入；0 = 关闭窗口判定 |
| `check_interval_minutes` | int | `10`（`ge=1 le=120`） | 睡眠检测循环间隔（分钟）：进入日程睡眠时段后触发睡眠裁判大修 |
| `interrupt_reply_threshold` | int | `3`（`ge=1 le=100`） | 夜间打扰判定：同一时间窗内累计 reply 达到该次数才算「被吵醒」（只触发 planner、没有 reply 不算；生效还要求处于睡眠周期且 reply 来自目标私聊） |
| `interrupt_window_minutes` | int | `10`（`ge=1 le=1440`） | 夜间打扰判定的计数窗口（分钟）：窗口 = [最后一次 reply − 本值, 最后一次 reply]，随 reply 滚动 |
| `resleep_minutes` | int | `5`（`ge=1 le=1440`） | 夜间打扰判定：自最后一次 reply 起该时长内无新 reply 即视为再次睡着（打扰段闭合，回到睡眠态） |
| `emoji_enabled` | bool | `false` | emoji 情绪联动开关（默认关）：服务层保留「按当前情绪标签向宿主按描述匹配一个表情」的实现，**当前无组件消费它**；关闭时输出与旧版逐字一致 |

### 旧键 → 新键兼容映射（deprecated）

| 旧键 | 现行写法 | 兼容行为 |
|---|---|---|
| `[patrol] global_proactive_cap` | `[patrol] proactive_target_daily_cap` | 配置加载时把旧键值填入新键（**新旧同给时新键优先**）；旧键不再出现在配置模型里。另有属性级回退：运行期对象取不到新键时回落旧属性名。旧值为 0 时仍按「每日预算 + 2」自动取值 |
| `[planning_proactive] enable_night_greeting` | `greeting_target_enabled` / `greeting_group_enabled` / `greeting_other_private_enabled` 三类开关 | **显式 `false` = 晚安全灭（三类都不发）**；显式 `true` 或未配置 = 三类新开关接管（晚间问候总开关被细分取代）。运行期解析，旧键存在不报错 |
| `[planning_proactive] morning_other_private_enabled` | `greeting_other_private_enabled` | **显式 `true`** 时归并生效（早/晚共用）；新键显式时**新键优先**；旧键显式 `false` 且新键未显式时按 `false` 处理 |
| `[planning_proactive] morning_other_private_min_messages` | `greeting_other_private_min_messages` | 旧键显式值时作为新门槛的缺省；新键显式时新键优先；运行期再兜底 `max(5, 值)`（**不允许低于 5**） |
| `[planning_proactive] enable_morning_greeting` | 无（早安不再需要总开关） | 纯兼容字段：仅参与「本轮是否需要评估主动行为」的入口判定，不决定早安是否发出 |
| `[planning_proactive] morning_greeting_require_activation` | `greeting_require_activation` | 与 `greeting_require_activation` **取或**：旧字段显式开启时，群聊早间问候同样要求必要性激活 |
| `[patrol] morning_start` / `morning_end` / `night_start` / `night_end` | 无（时刻改由 `[planning_proactive] morning_drift` / `night_drift` 决定） | 字段保留、仍参与 `HH:MM` 格式校验（非法值照旧留 `invalid_time_config` 痕），但**不再参与任何触发判定** |
| `[patrol] min_interval_exempt_greetings` | 无（不再需要） | 字段保留但**无消费点**：早晚安判时下线后，最小间隔约束由漂移机制自身的基准/期限承担 |
| `[schedule] auto_generate_time` | `[schedule] auto_generate_after_last_end_minutes` | 旧键显式配置 → 按绝对时刻理解，并同样受「新一天起点 − 30 分钟」硬约束钳制；两键都显式时**新键优先**；都不显式时用新键默认值 |
| `[emotion] evaluate_on_reply` | `[emotion] evaluate_mode` | 未显式设置 `evaluate_mode` 时按旧布尔迁移：`true → episode`、`false → off`；显式设置 `evaluate_mode` 时旧字段被忽略 |

> 「显式配置」一律按宿主配置模型的 `model_fields_set` 判定（即本次配置里是否真的写了这个键）。升级后若发现某个旧键不再生效，通常是宿主 UI 保存配置时把新键按当前值写回、使新键成为「显式配置」——此时以新键为准即可。

## 十一、FAQ / 常见问题

**① 配了目标 QQ，但没人私聊我？**
- 先确认 `[target] target_qq` 已改成真实 QQ 号：默认值 `123456789` 是占位值，此时**不解析目标会话**，插件不会主动私聊任何人。
- 插件要能查到该 QQ 的**私聊流**（先按 `chat.get_stream_by_user_id` 查，失败再遍历私聊流）。查不到时后台每 60 秒重试一次，直到解析成功才开始巡检——**和目标 QQ 先互发一条消息**通常就能解析到。
- 解析成功后还要看主动链的前置条件：`[patrol] enabled`、`proactive_trigger_enabled` 为真，且不在静默时段、未超出每日预算、不在用户冷却与最小间隔内。`/lbm_life status` 能看到巡检是否运行、上次触发情况。

**② 命令没反应？**
- 管理员指令（`/lbm_plan`、`/lbm_dashboard`、`/lbm_diag`、`/lbm_emotion_*`、qzone 写操作）要求你的 QQ 在 `[admin] admin_users` 里；**留空时只有本机控制台能用**，QQ 侧会回「权限不足」。
- `/lbm_mai_affection`、`/lbm_mai_help` 走的是目标 QQ 白名单：`target_qq` 是占位值时一律拒绝。
- 若配置了 `[admin] allowed_streams`（会话白名单），名单外的会话发指令会被拒绝（提示「当前会话未启用日程功能」）——该项**留空 = 不限制**。
- 命令命中后会拦截本轮 LLM 回复，所以「没反应」也可能是提示消息被发到了别处：注意命令的执行结果由插件直接回发到当前会话。

**③ 日程图／状态图只有文字、没有图？**
这是**预期的静默降级**：Pillow 缺失、字体缺失或渲染超时（状态图 10 秒、日程图 15 秒）时，插件改发纯文本，且**不提示渲染失败**。想确认原因可看决策日志与 `/lbm_diag`。日程图还可用 `[schedule] list_draw_image=false` 直接关掉只留文字。

**④ 早安／晚安没来，或者来了重复的？**
- 三类开关：`greeting_target_enabled`（目标私聊，默认开）、`greeting_group_enabled`（群聊，默认开）、`greeting_other_private_enabled`（非目标私聊，**默认关**）。
- 群聊还受两张名单与**必要性激活**限制（`greeting_require_activation`）：到点前该流需有人说过话，否则推迟到下一轮，到期限仍无人即放弃当天。
- 非目标私聊要求**前一日**该流消息数 ≥ `greeting_other_private_min_messages`（最低 5），且白名单留空 = 全不发。
- 例行问候**默认免主动打扰预算**（`greeting_exempt_budget=true`），所以「预算用完」不是问候不来的原因；只有你把它关掉后才会受预算约束。
- 去重：**目标私聊**同一流同日同类型（早安/晚安）至多一条，**热重载/重启后也生效**；群聊与非目标私聊的跨热重载去重**尚未实现**，重载后同日可能再发一条。

**⑤ QQ 空间发不出去 / 评论不回复？**
- 用 `/lbm_life qzone status` 看通道命中情况与 cookie 状态。cookie 按多级降级获取：适配器 `get_cookies` → NapCat HTTP（`napcat_http_host/port/token`）→ 本地文件；全部拿不到时**无法读评论正文，只做「评论数变化」提醒而不回评**。
- 发帖被挡住常见原因：`[qzone] enabled=false`、处于空间静默时段（`silence_start`~`silence_end`）、当日已达 `daily_max_posts`、`post_rate` 概率未命中、`require_activity=true` 但当前没有日程活动、`emotion_filter=true` 且当前情绪效价过低。
- 回评还受 `reply_comments_enabled`、`reply_number`、`reply_probability` 控制；楼中楼回复需要 cookie 就绪。
- **图片说说**：动作通道（SnowLuma）支持带图说说；一旦回退到 Web CGI 通道，只支持纯文字说说（该通道的图片上传尚未接通）。

**⑥ 裁判类任务的输出是空的？**
把 `[judge] model`（想念把关等判定）以及情绪睡眠裁判（`[emotion] referee_task_name`）、好感裁判（`[affection] review_task_name`）绑到**真正的非思考型模型**。思考型模型会先把 `max_tokens` 预算花在推理内容上，导致正文为空、判定整体失效（这类失败按「驳回／跳过」安全降级）。必要时调大 `[judge] max_tokens`。`/lbm_diag llm` 能看到实际使用的模型与失败形态。

**⑦ 怎么临时关掉某个模块？**
- `[qzone] enabled=false` → 关 QQ 空间；`[affection] enabled=false` → 关好感度 v2（回退旧的静态三档行为）；`[emotion] enabled=false` → 关情绪系统（评估/注入/睡眠大修全停，情绪相关工具与命令返回未启用提示）。
- `[schedule] enabled=false` / `[patrol] enabled=false` 分别关日程与巡检。
- `[plugin] enabled=false` 更彻底：只保留组件注册，不跑任何后台循环。
- 只想去掉某类主动打扰：`[planning_proactive] greeting_*_enabled=false`（对应会话类型的问候）、`[patrol] miss_enabled=false`（想念）、`[planning_proactive] enable_proactive_trigger=false`（活动切换主动开口）。

**⑧ 升级后，配置和数据会怎样？**
- **配置**：全部旧键继续兼容读取（见「十、配置键总表」的旧键 → 新键映射表），旧键存在不报错；被新键取代的旧键在「新键被显式配置」时以新键为准。`[emotion] evaluate_on_reply=false` 的用户请**先**把 `evaluate_mode` 显式设为 `off` 再保存一次 WebUI（否则保存后会被写死为默认 `episode`）。
- **数据**：数据目录随插件 `id` 变化，变为 `data/plugins/cateye.life_belonging_to_maimai/`；**旧目录不会自动迁移**，需要手动把旧目录下的 `schedule.db`、`emotion_state.json`、`emotion_config.json`、好感档案与日志复制过去再重启。
- 自定义的 `<数据目录>/emotion_mapping.json`（可选覆盖层）与 `proactive_budget.json` 同样在数据目录里，迁移时一并带上。

## 十二、已知问题

见 [CHANGELOG.md](CHANGELOG.md) 的「已知问题」节（统一登记，本手册不重复罗列）。

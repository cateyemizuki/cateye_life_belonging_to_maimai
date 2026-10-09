# 命令与工具手册（USAGE.md）

> 属于麦麦的生活（插件清单 `id`：`cateye.life_belonging_to_maimai`）现有 **7 个 LLM 工具**、**8 个聊天指令**、**4 个跨插件 API**
> （共 **29 个组件 = 8 Command + 7 Tool + 10 Hook + 4 API**）。
> 四个业务子模块各自提供一组：planning（日程规划）/ companion（虚拟陪伴）/ emotion（情绪）/ qzone（QQ 空间）。
> 本手册给出每个条目的描述、参数、行为细节与权限要求；安装步骤见 [README.md](README.md)，全部配置段与配置键见本文「十、配置总览」，历史变更见 [CHANGELOG.md](CHANGELOG.md)。

## 安装与首次配置

1. 把插件目录放进 MaiBot 的 `plugins/` 下（目录名建议 `cateye_life_belonging_to_maimai`），重启 MaiBot 或在插件管理里重载。
2. 在 WebUI 的插件配置里填 **目标 QQ**（`[patrol] target_qq`）；保持默认占位值 `123456789` 时**不解析目标会话**——不会主动私聊任何人，相关命令鉴权也一律拒绝。
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

- **描述原文**：把今天的起床时间往后推（赖床）。传入目标时间，支持 "HH:MM"、"H:MM" 或纯小时数（如 "09:30" / "9:30" / "9" 表示 09:00）。仅在夜间被吵醒、收到赖床提醒时可用；超过 `[schedule_advanced] lie_in_max_until`（默认 12:00）会被钳到该时间。
- **参数**：
  | 参数 | 类型 | 必填 | 说明 |
  |---|---|---|---|
  | time | string | | 目标起床时间，三种写法等价：`"HH:MM"` / `"H:MM"` / **纯小时数**（`"9"` = 09:00）；缺参或非法（`"abc"`、`"25:00"`、`"12:60"`、`"9:"`）→ 返回友好错误 + 格式示例，**不抛异常** |
- **可见性**：注册时声明 `metadata["visibility"] = "hidden"`——宿主据此置 `enabled=False` 且**不进工具名册**，即"平时不暴露"。
- **行为**：
  1. **非睡眠周期调用 → 拒绝**：查询"是否处于夜间睡眠周期"（本周期窗口在册时按该窗口，含赖床延长后的端点；`[schedule] no_sleep_mode=true` 一律拒绝）→ 返回「你现在还不能想要赖床」；
  2. **上限钳制**：目标 > `[schedule_advanced] lie_in_max_until` → 钳到该值（默认 12:00；提醒文本给的也是这个值，提示与判定同源）；
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
| `[schedule_advanced] lie_in_max_until` | `12:00` | `HH:MM` | 赖床硬上限时刻：工具传入超过它会被钳到它，提醒文本给出的也是它 |

## 八、早/晚安问候（漂移触发）

早/晚安**由日程侧驱动、陪伴侧不重复发**：planning 的 60 秒循环「每聊天流每日掷点一次 → 到点判定 → 走统一主动链」，`[patrol]` 侧**从不触发问候**，只读好感度的每日已发标记做去重观察——两模块不会各发一条。

**开关与时机**

| 配置项 | 默认 | 说明 |
|---|---|---|
| `[planning_proactive] greeting_morning_enabled` | `true` | 早安总开关（含目标私聊与群聊）；关闭 = 完全不发早安 |
| `[planning_proactive] greeting_night_enabled` | `true` | 晚安总开关；关闭 = 完全不发晚安 |
| `[planning_proactive] morning_drift` | `"10-30"` | 早安漂移区间（分钟）：触发时刻 = `[schedule] wake_time` 锚点 + `randint(a,b)`；单数字 `N` = `0~N`；非法值回退 10-30 |
| `[planning_proactive] night_drift` | `"10-30"` | 晚安漂移区间：触发时刻 = 当天最后一个非睡眠日程条目结束 − `randint(a,b)`（向前漂移）；无日程回退 `[schedule] sleep_time` 锚点 |
| `[planning_proactive] greeting_require_activation` | `true` | **群聊**必要性激活：到点需先观察到该流自基准点起有人发过消息才问候；无人则推迟、到期限放弃（当天不补发）。目标私聊不受本项影响 |
| `[planning_proactive] greeting_other_private_min_messages` | `5` | **其他私聊**门槛（`ge=5`，**不允许低于 5**）：前一日该流消息数 ≥ 该值才可能收到问候；取不到计数按 0（不发） |

- `[schedule] wake_time` / `sleep_time` 是早安/晚安的时间基准来源；**旧的 `[patrol] morning_start / morning_end / night_start / night_end` 四个时间窗字段已删除**。
- 早/晚安**始终免主动打扰预算**（既不查闸门也不计用量）；预算只服务陪伴侧的巡检与想念。
- 目标私聊问候的额外条件：**陪伴模块开关** `[patrol] enabled=true` 且 `target_qq` 已解析出私聊会话。

**覆盖范围（统一名单系统）**

| 会话类型 | 附加条件 |
|---|---|
| 目标陪伴私聊（`[patrol] target_qq`） | 无名单限制（必定触发；受总开关、静默时段、当日已发、陪伴模块开关约束） |
| 群聊 | 统一名单的**群聊部分**：`proactive_group_ids` + `proactive_group_list_mode`（默认 `blacklist`，留空 = 全群生效）+ 必要性激活 |
| 其他私聊 | 统一名单的**私聊部分**：`proactive_private_ids` + `proactive_private_list_mode`（默认 `whitelist`，留空 = 全不发）+ 前一日消息数门槛 |

名单只收**裸号**（群聊填群号、私聊填 QQ 号，不需要任何前缀；带前缀/非数字/空串/重复项忽略并记一条汇总告警；不支持通配 `*`）。**目标陪伴 QQ 始终按私聊黑名单处理**：活动切换主动开口与其他私聊问候都不会落到 TA 的私聊上。

**基准与期限**

| 问候 | 基准点 | 触发时刻 | 放弃期限 |
|---|---|---|---|
| 早安 | 睡眠窗口结束（`[schedule] wake_time` 锚点） | 基准 + `randint(a,b)` | 睡眠结束后**第一个非睡眠**日程条目的结束；无日程 → 基准 + 120 分钟 |
| 晚安 | 当天最后一个非睡眠日程条目的结束（无日程 → `sleep_time` 锚点） | 基准 − `randint(a,b)` | `min(睡眠窗口开始 sleep_time, [patrol] silence_start)`，取先到者 |

- **每流每日一次**：`(stream_id, 日期, 早安/晚安)` 只掷一次点，同日重复决策**复用同一结果**；热重载后允许重掷（纯内存、不落盘）。
- **重掷不等于重发**：发送闸在触发前补查好感度档案的按天标记——**目标陪伴私聊**该类型当日已发 → 视为已触发并落 `skip` 去重行、不再重发；**群聊/非目标私聊的跨热重载去重仍未实现**，重载后同流同日仍可能再发一条。
- **跨午夜条目**（结束 > 24:00）按绝对时间戳处理；循环因重启等原因错过触发时刻时，只要未到期限仍会在下一轮补发，已过期限即记放弃（当天不再评估）。

**当天已发标记**：目标私聊触发成功后写好感度档案的 `morning_sent` / `night_sent` 每日标记；`[patrol]` 侧见已发即不再触发，发送闸亦在触发前查同一标记——同一**目标**私聊流同一天早安/晚安**各至多一条（含热重载/重启后）**。


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
| `[schedule_advanced] auto_generate_time` | `00:30` | `HH:MM` | **已废弃**（隐藏字段）→ 改用上面的延迟字段。保留仅作**兼容读取**：当延迟字段仍是出厂值 `90`、而本字段偏离出厂值 `00:30` 时，按绝对时刻理解（同样受「新一天起点 − 30 分钟」硬约束钳制）；否则一律走相对延迟。**判定按值（是否偏离出厂值）**，不再依赖宿主是否显式配置过该键 |
| `[schedule] auto_generate_enabled` | `true` | — | 自动生成总开关（关掉后不跑循环；目标日已有日程照旧跳过） |

**防重**：到期触发后记"已生成的目标日"，同一天不再触发；下一次触发自然顺延到次日同点（跨日重新判定）。

## 十、配置总览

配置一共 **11 段、208 个可见配置项**（另有若干「隐藏·兼容」位，见各表标注）。段序即 WebUI 顺序；**日常只需要动前五段**，`[llm]` / `[prompts]` 属于深度定制，不懂就别动（`[prompts]` 改坏占位符会回退出厂文案并记 warning，**不会中断业务**）。

### 段总览

| 段 | 中文标题 | 管什么 | 什么时候动它 |
|---|---|---|---|
| `[plugin]` | 插件 | 总开关 / 配置版本 | 停用整个插件时 |
| `[patrol]` | **陪伴模块** | 目标 QQ、关系类型、巡检开关与概率、想念（开关/阈值/概率/LLM 把关）、每日预算、最小间隔、用户冷却、静默时段、群聊好感度注入开关、屏幕感知（模糊半径/超时/旁白存活） | 想调「陪伴对象是谁、多久主动找 TA 说话」时 |
| `[affection]` | 好感度 | 档位、v2 开关、日衰减/基线漂移、每日增减上限、档位迟滞、好感裁判（会话采样 + 关系日结）、情绪调制、周结大事记 | 想调「感情升温/降温的快慢」时 |
| `[schedule]` | 日程系统 | 总开关、作息锚点、无睡眠模式、自动生成与延迟、活动数量、生活阶段、活动描述、注入开关与范围、真实节假日 | 日常调整作息与「日程注不注入、注给谁」 |
| `[schedule_advanced]` | 日程系统·高级设置 | 多轮生成、角色裁判、`list` 绘图与超时、时区、生成 token/超时/回看天数/历史与知识库条数、注入调优、缓存、赖床硬上限、情绪影响强度、后台清理 | 深度调优生成质量、成本与渲染时 |
| `[planning_proactive]` | 日程主动行为 | 活动切换主动开口、触发窗口、频率调控、群聊/私聊两份名单 + 名单模式、早/晚安两个开关、漂移区间、问候激活、赖床提醒 | 想调「在哪些群/私聊主动说话」与早/晚安时 |
| `[llm]` | **LLM 调用** | 主任务名 + 判定（judge）模型与参数 + 各用途任务名（好感裁判/情绪裁判/情绪评估/空间/视觉） | 换模型、判定老被驳回时 |
| `[prompts]` | **提示词（高级）** | 插件发给模型的全部原文（64 项，含陪伴/日程/情绪/空间/工具五组） | 深度定制人格化文案时；**不改 = 与出厂逐字一致** |
| `[admin]` | 管理与日志 | 管理员、允许会话、LLM/决策日志开关与保留天数 | 排障与权限 |
| `[qzone]` | QQ 空间 | 空间总开关与发帖/回评节奏 | 空间功能 |
| `[emotion]` | 情绪系统 | VAD 状态机、评估模式、睡眠结算、注入开关 | 情绪系统 |

### 旧配置自动迁移（不需要手改）

插件启动时会把旧结构交给插件，插件内置迁移器把值搬进新家并落盘（**段名不变的键原样保留**）。迁移只发生一次（以 `[plugin] layout_version` 标记），反复重启不会二次改写：

| 旧位置 | 新位置 |
|---|---|
| `[target] target_qq` | `[patrol] target_qq` |
| `[cateye] enabled / screenshot_blur / timeout_seconds / context_ttl_minutes` | `[patrol] screen_enabled / screen_blur / screen_timeout_seconds / screen_context_ttl_minutes` |
| `[cateye] vlm_task` | `[llm] vlm_task` |
| `[cateye] describe_prompt / narration_template / offline_narration_template / failed_narration_template` | `[prompts] screen_describe / screen_narration / screen_narration_offline / screen_narration_failed`（只搬非默认值） |
| `[judge] model / max_tokens / temperature` | `[llm] judge_model / judge_max_tokens / judge_temperature` |
| `[schedule] llm_task_name` / `[affection] review_task_name` / `[emotion] evaluator_task_name` / `[emotion] referee_task_name` / `[qzone] llm_task_name` | `[llm] task_name / review_task_name / emotion_evaluator_task_name / emotion_referee_task_name / qzone_task_name` |
| `[schedule]` 的 23 个深水字段（时区、生成 token/超时/回看、角色裁判、绘图、缓存、赖床上限、情绪影响强度、`auto_generate_time` 等） | `[schedule_advanced]`（键名不变） |
| `[inject] *`、`[planning_cleanup] *` | `[schedule_advanced] *`（键名不变） |
| `[patrol] miss_hours_min` + `miss_hours_max` | `[patrol] miss_silence_hours = "min-max"` |
| 旧问候四名单 / 活动切换两名单 | `[planning_proactive] proactive_group_ids` + `proactive_private_ids`（+ 名单模式） |

### 已移除的配置项（新界面不再提供）

以下字段已不再有意义——迁移时丢弃，或降级为隐藏兼容位（旧值仍被读取，但不占新界面）：

- `[patrol] morning_start` / `morning_end` / `night_start` / `night_end`（问候时间窗；时刻改由漂移区间决定）；
- `[patrol] min_interval_exempt_greetings`（早/晚安**始终**豁免最小间隔）、`min_interval_exempt_miss`（想念**不**豁免）；
- `[patrol] proactive_target_daily_cap` / 旧名 `global_proactive_cap`（**每日预算即上限**，见下）；
- `[patrol] miss_avoid_future_schedule` / `miss_future_schedule_hours`（功能整体移除）；
- `[planning_proactive] enable_morning_greeting` / `enable_night_greeting`、旧问候四名单、`morning_other_private_*`、`greeting_exempt_budget`（收拢为两个总开关 + 统一名单 + 恒免预算）；
- `[schedule] auto_generate_time`（降级为 `[schedule_advanced]` 隐藏兼容位）。

### 注入矩阵（谁在什么会话里看到什么）

| 会话 | 日程块（含日期/节假日头部） | 好感度块 | 屏幕旁白 / 主动回合约束 |
|---|---|---|---|
| 目标陪伴私聊 | ✅（`inject_scope=all` 时） | ✅ **始终**（陪伴模块启用时） | ✅（仅此会话） |
| 其他私聊 | ✅ | ❌ **始终不注入** | ❌ |
| 群聊 | ✅ | 仅当**群内上下文出现陪伴用户**且 `[patrol] inject_affection_in_group=true`（默认开）；此档只给档位块，不带关系记忆行 | ❌ |

- 日期/节假日头部：`[schedule] holiday_inject_enabled=true`（默认）时注在日程块头部，模板 `[prompts] inject_date_header`；`holiday_affects_schedule=true` 额外让**日程生成**考虑节假日（默认关）。
- 日程块是否注入由 `[schedule] inject_into_planner / inject_into_replyer` 与 `inject_scope` 共同决定（默认 `all`）。

### 预算口径（一张表说清）

| 主动行为 | 是否占预算 | 说明 |
|---|---|---|
| 陪伴·想念 / 日程巡检（活动分享 / 日常搭话） | ✅ 占（仅目标陪伴私聊计数） | 上限 = `[patrol] daily_max_speak`（0 = 陪伴侧静音） |
| 早/晚安（目标私聊与群聊） | ❌ **始终免**预算 | 不查闸门也不计用量 |
| 活动切换主动开口（群聊 / 名单内私聊） | ❌ 不计陪伴预算 | 不落到目标陪伴私聊（目标始终视为私聊黑名单） |

### 完整配置键总表

「类型」为配置模型字段类型；「默认值」为出厂默认。标注【隐藏·兼容】的字段不出现在新界面，仅为读取旧配置保留。若运行行为与下表不一致，以代码为准。


#### [plugin] 插件（3 键：1 可见 + 2 隐藏·兼容）

| 键名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 是否启用插件 |
| `config_version` | str | `"1.0.0"` | 【隐藏·兼容】配置版本（与插件版本同步） |
| `layout_version` | str | `""` | 【隐藏·兼容】段布局标记（2026-10-10 配置整理轮的迁移幂等用；用户不要改） |

#### [patrol] 陪伴模块（23 键）

| 键名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 陪伴模块总开关：关闭后停止巡检主动聊天与好感度注入（命令与工具返回未启用提示） |
| `target_qq` | int | `123456789` | 陪伴对象的 QQ 号：主动聊天、想念、好感度、屏幕感知都只对这个私聊会话生效 |
| `relation_type` | Literal["恋人", "挚友", "维护者", "家人", "自定义"] | `"恋人"` | 目标用户是 bot 的谁：影响想念提示词、消息口吻、好感度档位名称与消息后缀 |
| `relation_custom` | str | `""` | 自定义关系的称谓（关系类型选「自定义」时生效，如：姐姐、师兄、搭子） |
| `proactive_trigger_enabled` | bool | `true` | 主动巡检总开关：开启后按下方各触发开关与概率判断该不该主动说话 |
| `check_interval_minutes` | int | `10` | 巡检循环间隔（分钟）：每隔多久判断一次该不该主动说话（ge=1、le=120） |
| `patrol_activity_enabled` | bool | `true` | 日程巡检开关：巡检发现 bot 正在某个日程活动时，按下方概率主动分享/搭话 |
| `activity_trigger_rate` | float | `0.6` | 日程巡检概率：巡检命中当前有日程活动时，主动分享一次的概率（ge=0.0、le=1.0） |
| `default_speak_rate` | float | `0.5` | 日程巡检概率：巡检时没有正在进行的日程活动，纯日常搭话一次的概率（ge=0.0、le=1.0） |
| `miss_enabled` | bool | `true` | 想念开关：目标用户沉默超过阈值后按概率主动说想TA（每天最多一次） |
| `miss_silence_hours` | str | `"4-8"` | 想念阈值（小时）：用户沉默超过该时长才考虑想念。写 \"4-8\" = 区间内每轮随机取阈值（沉默越久越容易触发）；只写一个数字如 8 = 0~8 区间的宽松写法 |
| `miss_speak_rate` | float | `0.5` | 想念概率：每轮巡检在沉默超过阈值后真的开口说想念的概率（ge=0.0、le=1.0） |
| `miss_llm_check_enabled` | bool | `true` | 想念 LLM 把关：开口前让模型以角色身份判断此刻合不合适（回答 Y 才发），驳回后 30 分钟不再判断 |
| `daily_max_speak` | int | `5` | 每日预算（次）：陪伴侧主动发言的每日上限，只统计日程巡检与想念；早/晚安不占预算（由日程侧驱动）（ge=0、le=50） |
| `min_trigger_interval_minutes` | int | `240` | 最小间隔（分钟）：任意两次巡检/想念主动发言之间的硬性间隔，0 = 不限制；早/晚安始终豁免本限制（ge=0、le=1440） |
| `user_cooldown_minutes` | int | `30` | 用户冷却（分钟）：对方最后一次发言后多久内不主动打扰（想念/巡检受控，早/晚安不受控）（ge=0、le=180） |
| `silence_start` | str | `"00:00"` | 静默时段开始（HH:MM）：静默期内巡检不触发、问候不发（支持跨午夜；配置非法按静默处理） |
| `silence_end` | str | `"07:30"` | 静默时段结束（HH:MM） |
| `inject_affection_in_group` | bool | `true` | 群聊好感度注入：开启后，群里最近有人提到/包含陪伴对象时，给 bot 注入一点好感度上下文；目标私聊始终注入好感度，其他私聊始终不注入 |
| `screen_enabled` | bool | `false` | 屏幕感知：每次主动触发前经 cateye.connect-hub 截一张陪伴对象电脑的屏幕并转述成一句旁白 |
| `screen_blur` | int | `0` | 截图模糊半径（0-64）：0 = 清晰；介意隐私可设 30 左右（视觉模型仍能看出大概在干什么）（ge=0、le=64） |
| `screen_timeout_seconds` | float | `20.0` | 查看电脑（截图 + 视觉理解）整体超时（秒）；超时不阻塞巡检，本轮按不可用处理（ge=5.0、le=120.0） |
| `screen_context_ttl_minutes` | int | `90` | 屏幕旁白存活上限（分钟）：超过即丢弃不再注入；0 = 不限时（ge=0、le=1440） |

#### [affection] 好感度（13 键）

| 键名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `current_level` | Literal[0, 1, 2] | `0` | 好感度档位：0=熟悉（温柔拘谨），1=亲密（活泼热情），2=热恋（撒娇抱抱） |
| `enabled` | bool | `true` | 启用好感度 v2：开启后按 score/baseline 连续分运行（规则事件记账 + 日衰减 + 档位迟滞）；关闭则回退 v1 三档行为（档位仅由命令/工具设置，不自动变化） |
| `decay_pull` | float | `0.06` | 日衰减拉回基线的比例：每天日切执行 score += (baseline − score) × 该值（0=不衰减）（ge=0.0、le=1.0） |
| `baseline_ema` | float | `0.03` | 关系基线漂移速度：日切执行 baseline += (score − baseline) × 该值（0=基线永不移动）（ge=0.0、le=1.0） |
| `daily_positive_cap` | float | `4.0` | 单日正向好感增量上限（含规则事件与后续裁判）：超出部分记日志丢弃（ge=0.0、le=100.0） |
| `daily_negative_cap` | float | `3.0` | 单日负向好感扣减下限（含规则事件与后续裁判）：超出部分记日志丢弃（ge=0.0、le=100.0） |
| `tier_hysteresis` | float | `3.0` | 档位迟滞带（分）：升档需 score ≥ 当前档上界 + 该值，降档需 ≤ 下界 − 该值（0=无迟滞）（ge=0.0、le=30.0） |
| `tier_min_dwell_hours` | float | `24.0` | 档位最短停留时长（小时）：距进入当前档位不足该时长则不升/降档（0=不限）（ge=0.0、le=720.0） |
| `review_enabled` | bool | `true` | 好感度 LLM 裁判总开关：会话采样评估 + 睡眠时段关系日结；关闭则完全不做 LLM 评估 |
| `review_sample_every_rounds` | int | `20` | 会话裁判采样频率（轮）：每多少轮对方私聊消息抽样一次（与每日上限取先到者）（ge=1、le=1000） |
| `review_max_per_day` | int | `2` | 会话裁判每日上限（次）：0 = 当天不做采样评估（不影响睡眠时段的关系日结）（ge=0、le=50） |
| `emotion_modulation` | bool | `true` | 情绪调制好感增量：开心时好事加分更多、低落时坏事扣分更狠（乘数 0.6~1.4） |
| `weekly_milestones_enabled` | bool | `false` | 周结大事记：周日的日结后按最近 7 天关系温度与净变化汇总出 1~2 条里程碑，随好感注入展示 |

#### [schedule] 日程系统（15 键）

| 键名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 日程模块总开关：关闭后停止日程生成/注入/主动行为（工具与命令返回未启用提示） |
| `wake_time` | str | `"07:00"` | 起床时间锚点（HH:MM）：清醒时段从这一刻开始；早安问候也以这一刻为基准做漂移 |
| `sleep_time` | str | `"23:00"` | 入睡时间锚点（HH:MM）：这一刻之后进入睡眠时段；晚安问候以当天最后一个日程为准、无日程时回退本时刻 |
| `no_sleep_mode` | bool | `false` | 无睡眠模式：入睡到起床的时段不生成任何日程（生成后自动销毁该时段残留条目），且睡眠时段内不触发主动行为。适合不需要睡眠的角色设定 |
| `auto_generate_enabled` | bool | `true` | 每天自动用 LLM 生成当日日程（当天已有日程则跳过） |
| `auto_generate_after_last_end_minutes` | int | `90` | 生成时刻（相对锚点）：当天最后一个非睡眠日程条目结束后 N 分钟生成新一天日程；当天没有日程时以「入睡时间」为基准。硬约束：不晚于「起床时间」前 30 分钟（ge=0） |
| `min_activities` | int | `6` | 每天生成的最少活动数（ge=1、le=20） |
| `max_activities` | int | `12` | 每天生成的最多活动数（ge=1、le=25） |
| `custom_prompt` | str | `""` | 当前生活阶段 / 长期状态：角色的持续设定（如「正在环游世界」「备考研究生」），日程生成会延续这一方向 |
| `enable_detailed_description` | bool | `true` | 活动描述开关：关闭后生成、注入、命令展示都不再显示长描述（省 token） |
| `inject_into_planner` | bool | `true` | 在 planner 决策时把当前日程注入上下文（决定 bot 说话时知不知道自己在干什么） |
| `inject_into_replyer` | bool | `true` | 在 replyer 生成回复前把当前日程作为额外提示注入；与 planner 注入共享冷却，不会两阶段连刷 |
| `inject_scope` | Literal["all", "target_private"] | `"all"` | 注入范围：all = 所有会话（群聊、私聊都注入日程）；target_private = 只给目标陪伴私聊注入 |
| `holiday_inject_enabled` | bool | `true` | 注入真实节假日：开启后日程块头部会注明今天日期、星期几与节假日状态（如「国庆节假期」/「调休补班」） |
| `holiday_affects_schedule` | bool | `false` | 节假日影响日程生成：开启后生成日程时会考虑今天是不是节假日（假期更放松、补班更像工作日） |

#### [schedule_advanced] 日程系统·高级设置（30 键：29 可见 + 1 隐藏·兼容）

| 键名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `use_multi_round` | bool | `true` | 多轮生成：首轮质量不达标时按反馈重试（提升日程质量，增加 token 成本） |
| `max_rounds` | int | `2` | 多轮生成最大轮数（1-5，推荐 2；越多越费 token）（ge=1、le=5） |
| `quality_threshold` | float | `0.85` | 质量阈值：达到该分立即停止重试（0.80-0.90 推荐 0.85）（ge=0.0、le=1.0） |
| `max_tokens` | int | `8192` | 日程生成 LLM 的最大 token 数（过小可能截断日程）（ge=1000） |
| `generation_timeout` | float | `180.0` | 日程生成 LLM 调用超时（秒），推荐 120-300；会换算为宿主超时，避免被默认 30 秒截断（ge=10.0） |
| `recent_schedule_days` | int | `3` | 生成新日程时回看最近 N 天的日程作为去重参考（3-5 防交替式重复）（ge=1、le=14） |
| `history_message_limit` | int | `30` | 生成日程时回读各会话当天最近 N 条聊天消息作为灵感。0 = 禁用（ge=0） |
| `knowledge_search_limit` | int | `5` | 生成日程时检索知识库的最大条数。0 = 禁用（ge=0） |
| `cross_day_activity` | bool | `true` | 跨天活动：允许 time_window 跨夜写法，并在注入时回读昨日跨夜活动 |
| `min_description_length` | int | `20` | 活动描述最小字符数（20-30 较合理，过短易模糊）（ge=5） |
| `max_description_length` | int | `50` | 活动描述最大字符数（50-100 较合理，过长占 token）（ge=5） |
| `timezone` | str | `"Asia/Shanghai"` | 时区设置（IANA 命名，如 Asia/Shanghai、UTC、America/New_York）：日程日期、活动时段、问候漂移都按此时区计算 |
| `role_judge_enabled` | bool | `true` | 角色裁判模式：你（用户）通过 lbm_update_schedule_v4 用自然语言要求改日程时，让 bot 以角色身份先做判断——接受并改今天（today）/ 接受为未来预约（future）/ 拒绝不改（reject）。判定失败时安全回退为「日程不变」 |
| `role_judge_temperature` | float | `0.3` | 角色裁判温度：越低越保守（更容易拒绝离谱请求），越高越随和（ge=0.0、le=2.0） |
| `list_draw_image` | bool | `true` | /lbm_plan list 是否绘制日程图片（内置字体渲染后合并转发）；关闭后只返回文字长文本 |
| `image_timeout_seconds` | float | `15.0` | 日程图片绘制超时（秒）；超时 /lbm_plan list 静默降级为纯文字（不提示失败）（ge=1.0） |
| `enable_intent_classification` | bool | `true` | 意图分类：识别用户消息属于问当前 / 问未来 / 闲聊 / 技术问答，决定注入的详细程度与是否注入 |
| `enable_inject_optimization` | bool | `true` | 注入优化器：防止重复注入和无效打扰（planner / replyer 共用冷却） |
| `casual_chat_inject_probability` | float | `0.5` | 闲聊场景（非直接询问 / 非技术问答）的注入概率（ge=0.0、le=1.0） |
| `context_max_turns` | int | `3` | 对话上下文保留轮数（用于判断是否在连续讨论日程话题）（ge=1） |
| `context_ttl` | int | `600` | 对话上下文过期时间（秒），超时后认为是新会话（ge=60） |
| `inject_cache_minutes` | int | `15` | 当前活动查询的时间窗口缓存（分钟）（ge=1、le=60） |
| `cache_ttl` | int | `300` | 日程注入缓存有效期（秒，默认 5 分钟）；过期后重查数据库（ge=10） |
| `cache_max_size` | int | `100` | 注入缓存最大条目数（LRU 策略，超出按最近最少使用淘汰）（ge=10） |
| `max_future_activities` | int | `3` | 注入时最多显示的未来活动条数。0 = 不显示（ge=0） |
| `emotion_influence` | float | `0.6` | 情绪对日程的影响强度（0 = 关闭）：睡醒情绪会写进日程生成提示词，同时决定睡眠结算的增量限幅（ge=0.0、le=1.0） |
| `lie_in_max_until` | str | `"12:00"` | 赖床（推迟起床）的硬上限时刻（HH:MM）：夜间被吵醒后调用赖床工具时，传入的起床时间超过本值会被钳到本值；格式非法按 12:00 处理 |
| `cleanup_interval` | int | `3600` | 后台清理循环间隔（秒，默认 1 小时）（ge=60） |
| `cleanup_old_goals_days` | int | `30` | 保留多少天前的已完成 / 取消目标（超过自动删除）（ge=1） |
| `auto_generate_time` | str | `"00:30"` | 【隐藏·兼容】【已废弃】旧的「每日自动生成日程的绝对时刻」（HH:MM）。仅当日程生成延迟保持默认且本值被改过时才生效；新配置请用「生成延迟（最后日程结束后）」 |

#### [planning_proactive] 日程主动行为（24 键：15 可见 + 9 隐藏·兼容）

| 键名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enable_proactive_trigger` | bool | `true` | 活动切换主动开口：活动开始后在触发窗口内让 bot 主动说一两句（每个会话每个活动每天 1 次）。跳过当天睡醒后的第一个活动（由早安负责），睡眠时段不触发 |
| `proactive_fresh_window_minutes` | int | `10` | 触发窗口（分钟）：活动开始后每个会话在窗口内获得独立随机延迟，延迟结束即触发；错过窗口不补发（ge=1、le=60） |
| `enable_frequency_modulation` | bool | `true` | 按当前活动类型调节聊天频率：学习/工作时降低发言频率，休息/娱乐时提高 |
| `proactive_group_ids` | list[str] | `[]` | 群聊名单（只填**裸群号**，如 ["123456"]，不需要任何前缀）：名单模式=黑名单时这些群不生效、留空则所有群生效；名单模式=白名单时只有这些群生效、留空则所有群都不生效。不支持通配 * |
| `proactive_group_list_mode` | Literal["blacklist", "whitelist"] | `"blacklist"` | 群聊名单模式：黑名单 = 名单内的群不生效（留空 = 全部生效）；白名单 = 只有名单内的群生效（留空 = 全部不生效） |
| `proactive_private_ids` | list[str] | `[]` | 私聊名单（只填**裸 QQ 号**，如 ["789"]，不需要任何前缀）：名单模式=白名单时只有这些私聊生效、留空则所有私聊都不生效；名单模式=黑名单时这些私聊不生效。目标陪伴对象始终按黑名单处理 |
| `proactive_private_list_mode` | Literal["blacklist", "whitelist"] | `"whitelist"` | 私聊名单模式：白名单 = 只有名单内的私聊生效（留空 = 都不生效）；黑名单 = 名单内的私聊不生效（留空 = 全部生效） |
| `greeting_morning_enabled` | bool | `true` | 启用早安：睡醒时刻（`[schedule]` 起床时间）+ 早安漂移后，向候选会话道早安 |
| `greeting_night_enabled` | bool | `true` | 启用晚安：当天最后一个非睡眠日程结束时刻 − 漂移后，向候选会话道晚安 |
| `morning_drift` | str | `"10-30"` | 早安漂移区间（分钟，相对起床时刻）：填 \"a-b\" 或单数字 N（= 0~N 随机）；触发时刻 = 起床时刻 + 随机漂移。非法值回退 10-30 |
| `night_drift` | str | `"10-30"` | 晚安漂移区间（分钟，相对最后一条日程结束）：填 \"a-b\" 或单数字 N（= 0~N 随机）；触发时刻 = 最后日程结束 − 随机漂移。非法值回退 10-30 |
| `greeting_require_activation` | bool | `true` | 问候需激活（群聊与其他私聊）：到点后需先观察到该会话自基准点起有人发过消息才问候，无人则推迟、到期限放弃（当天不补发）。目标私聊不受此项影响 |
| `greeting_other_private_min_messages` | int | `5` | 其他私聊问候门槛（条）：前一日该私聊消息数达到该值才可能收到早/晚安（**不允许低于 5**）；取不到消息计数按 0 处理（不发送）（ge=5） |
| `nap_reminder_enabled` | bool | `true` | 赖床提醒：夜间被吵醒且重新睡着后，向 bot 发一条临时提醒（点名赖床工具，只在当轮有效） |
| `nap_reminder_max_per_day` | int | `2` | 每个睡眠周期（不是自然日）的有效提醒次数上限：提醒发送 + 工具成功改日程各计 1 次；用尽后不再提醒（ge=1） |
| `greeting_target_enabled` | bool | `true` | 【隐藏·兼容】（兼容旧配置）目标陪伴私聊的问候开关；新界面不再提供，默认开 |
| `greeting_group_enabled` | bool | `true` | 【隐藏·兼容】（兼容旧配置）群聊问候开关；新界面不再提供，默认开 |
| `greeting_other_private_enabled` | bool | `true` | 【隐藏·兼容】非目标私聊问候总开关（2026-10-10 起默认开）：真正的「发不发」由私聊名单表达——私聊名单留空（默认白名单）时本就无人候选，故默认开不会打扰任何人；只有旧配置显式 false 时才整体关闭其他私聊问候 |
| `greeting_group_blacklist` | list[str] | `[]` | 【隐藏·兼容】（兼容旧配置）群聊问候黑名单；已并入「群聊名单」 |
| `greeting_group_whitelist` | list[str] | `[]` | 【隐藏·兼容】（兼容旧配置）群聊问候白名单；已并入「群聊名单」 |
| `greeting_other_private_whitelist` | list[str] | `[]` | 【隐藏·兼容】（兼容旧配置）非目标私聊问候白名单；已并入「私聊名单」 |
| `greeting_other_private_blacklist` | list[str] | `[]` | 【隐藏·兼容】（兼容旧配置）非目标私聊问候黑名单；已并入「私聊名单」 |
| `morning_other_private_enabled` | bool | `false` | 【隐藏·兼容】（兼容旧配置）旧「其他私聊早安」开关；并入 greeting_other_private_enabled |
| `morning_other_private_min_messages` | int | `5` | 【隐藏·兼容】（兼容旧配置）旧「其他私聊早安门槛」；并入 greeting_other_private_min_messages（ge=5） |

#### [llm] LLM 调用（9 键）

| 键名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `task_name` | str | `"replyer"` | 主任务名：日程生成、工具发言等「生成类」调用默认用它（宿主模型任务键，如 replyer/utils/planner） |
| `judge_model` | str | `""` | 判断用模型：填宿主任务名或具体模型名；留空 = 跟随「主任务名」。判定类调用只需要明确结论（如 Y/N） |
| `judge_max_tokens` | int | `512` | 判断调用的最大生成 token 数。思考型模型会先消耗 token 输出推理内容，预算太小时正文为空、判定按失败（驳回）处理（ge=16、le=8192） |
| `judge_temperature` | float | `0.2` | 判断调用的采样温度：越低结论越稳定（判定不需要创意）（ge=0.0、le=2.0） |
| `review_task_name` | str | `"utils"` | 好感裁判任务名：会话采样评估与关系日结使用（建议便宜、非思考型） |
| `emotion_referee_task_name` | str | `"replyer"` | 情绪睡眠裁判任务名：睡眠期情绪大修使用（需要理解人设与日程，建议与主任务同档） |
| `emotion_evaluator_task_name` | str | `"utils"` | 情绪评估任务名：回复/事件的 ΔVAD 增量评估使用（调用频繁，建议便宜、非思考型） |
| `qzone_task_name` | str | `"utils"` | QQ 空间任务名：说说文案与评论回复使用 |
| `vlm_task` | str | `"vlm"` | 视觉任务名：屏幕截图理解使用（必须是支持图片输入的视觉模型任务） |

#### [prompts] 提示词（高级）（64 键）

| 键名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `companion_miss_reason` | str | （出厂文案） | 想念触发时给 planner 的说明（占位符：{lover_name} {relation} {hours}） |
| `companion_miss_confirm` | str | （出厂文案） | 想念触发前的 LLM 把关提示词，回复 Y 才放行（占位符：{lover_name} {relation} {personality} {current_time} {hours} {activity_context}） |
| `companion_activity_reason` | str | （出厂文案） | 日程巡检分享的触发说明（占位符：{lover_name} {activity}） |
| `companion_daily_reason` | str | （出厂文案） | 日常搭话的触发说明（占位符：{lover_name}） |
| `screen_describe` | str | （出厂文案） | 屏幕截图的视觉理解提示词（随截图发给视觉模型） |
| `screen_narration` | str | （出厂文案） | 看清屏幕时的旁白（占位符：{date} {time} {user_name} {description}） |
| `screen_narration_offline` | str | （出厂文案） | 对方电脑没开时的旁白（占位符：{date} {time} {user_name}；留空 = 不注入） |
| `screen_narration_failed` | str | （出厂文案） | 截图/理解失败时的旁白（占位符：{date} {time} {user_name}；留空 = 不注入） |
| `inject_affection_block` | str | （出厂文案） | 好感度注入块（占位符：{name} {relation_word} {target_label} {tier} {tier_label} {tier_desc}） |
| `inject_proactive_turn_rule` | str | （出厂文案） | 主动发言回合给 planner 的回复目标约束（无占位符） |
| `tool_send_message` | str | （出厂文案） | 工具主动发消息的生成提示词（占位符：{style_word} {relation_word}） |
| `tool_send_message_system` | str | （出厂文案） | 工具主动发消息的人设声明（占位符：{name} {self_desc} {relation_word}） |
| `affection_conversation_review` | str | （出厂文案） | 会话采样好感裁判提示词（占位符：{lover_name} {target_label} {persona} {tier_label} {tier_desc} {mood_text} {recent_messages}） |
| `affection_daily_review` | str | （出厂文案） | 关系日结裁判提示词（占位符：{lover_name} {target_label} {date} {persona} {tier_label} {tier_desc} {stats} {event_log} {impressions}） |
| `inject_schedule_header` | str | （出厂文案） | 日程注入块首行（无占位符） |
| `inject_schedule_current_line` | str | （出厂文案） | 当前活动行（占位符：{activity} {description} {remaining_hint}） |
| `inject_schedule_future_title` | str | （出厂文案） | 未来安排标题（无占位符） |
| `inject_schedule_future_line` | str | （出厂文案） | 未来安排条目（占位符：{time} {name}） |
| `inject_hint_future` | str | （出厂文案） | 问未来时的引导行（无占位符） |
| `inject_hint_current` | str | （出厂文案） | 问当前时的引导行（无占位符） |
| `inject_hint_continue` | str | （出厂文案） | 对话延续时的引导行（占位符：{reason}） |
| `inject_hint_proactive` | str | （出厂文案） | 主动碎碎念时的引导行（无占位符） |
| `inject_hint_idle` | str | （出厂文案） | 闲聊时的引导行（无占位符） |
| `inject_replyer_status` | str | （出厂文案） | replyer 状态块（占位符：{state_line} {energy} {next_line}） |
| `inject_replyer_state_line` | str | （出厂文案） | replyer 状态行（占位符：{activity} {description} {remaining_hint}） |
| `inject_replyer_next_line` | str | （出厂文案） | replyer 下一条活动行（占位符：{time} {name}；留空 = 不输出） |
| `inject_date_header` | str | （出厂文案） | 日期/节假日头部（占位符：{date} {weekday} {holiday}）——开启「注入真实节假日」时注在日程块头部 |
| `schedule_emotion_context` | str | （出厂文案） | 日程生成·睡醒情绪块（占位符：{mood} {constraints}） |
| `schedule_low_mood_note` | str | （出厂文案） | 日程生成·连续低迷软约束（占位符：{days}） |
| `schedule_holiday_context` | str | （出厂文案） | 日程生成·节假日上下文（占位符：{date} {weekday} {holiday}）——仅当「节假日影响日程生成」开启时追加 |
| `schedule_generation_extra` | str | （出厂文案） | 日程生成·追加指令（留空 = 不追加；会原样附在生成提示词末尾） |
| `schedule_retry_feedback` | str | （出厂文案） | 多轮生成的重试反馈模板（占位符：{issues}） |
| `schedule_role_judge_no_activity` | str | （出厂文案） | 角色裁判在「取不到当前日程」时的占位文案（无占位符） |
| `schedule_role_judge_no_persona` | str | （出厂文案） | 角色裁判在「取不到人设」时的占位文案（无占位符） |
| `schedule_role_judge` | str | （出厂文案） | 角色裁判提示词（占位符：{persona} {today_str} {weekday} {current_text} {description}） |
| `proactive_intent_default` | str | （出厂文案） | 活动切换主动开口·默认（占位符：{activity_name}） |
| `proactive_intent_study` | str | （出厂文案） | 活动切换主动开口·学习 study（占位符：{activity_name}） |
| `proactive_intent_work` | str | （出厂文案） | 活动切换主动开口·工作 work（占位符：{activity_name}） |
| `proactive_intent_meal` | str | （出厂文案） | 活动切换主动开口·吃饭 meal（占位符：{activity_name}） |
| `proactive_intent_rest` | str | （出厂文案） | 活动切换主动开口·休息 rest（占位符：{activity_name}） |
| `proactive_intent_exercise` | str | （出厂文案） | 活动切换主动开口·运动 exercise（占位符：{activity_name}） |
| `proactive_intent_entertainment` | str | （出厂文案） | 活动切换主动开口·娱乐 entertainment（占位符：{activity_name}） |
| `proactive_intent_social` | str | （出厂文案） | 活动切换主动开口·社交 social_maintenance（占位符：{activity_name}） |
| `proactive_intent_daily_routine` | str | （出厂文案） | 活动切换主动开口·日常作息 daily_routine（占位符：{activity_name}） |
| `proactive_intent_learn_topic` | str | （出厂文案） | 活动切换主动开口·兴趣 learn_topic（占位符：{activity_name}） |
| `proactive_intent_morning` | str | （出厂文案） | 早安 intent（占位符：{activity_name}） |
| `proactive_intent_morning_no_activity` | str | （出厂文案） | 无日程时的早安 intent（无占位符） |
| `proactive_intent_night` | str | （出厂文案） | 晚安 intent（无占位符） |
| `proactive_intent_commitment` | str | （出厂文案） | 约定到期 intent（占位符：{time_hint} {title} {notes_hint} {activity_name}） |
| `proactive_reason_activity` | str | （出厂文案） | 活动切换的触发标注（占位符：{activity_name}） |
| `proactive_reason_commitment` | str | （出厂文案） | 约定到期的触发标注（占位符：{title}） |
| `proactive_reason_morning` | str | （出厂文案） | 早安的触发标注（无占位符） |
| `proactive_reason_night` | str | （出厂文案） | 晚安的触发标注（无占位符） |
| `emotion_sleep_referee` | str | （出厂文案） | 睡眠期情绪大修裁判提示词（占位符较多；不熟勿改） |
| `emotion_init_system` | str | （出厂文案） | 人设→VAD 参数初始化提示词（占位符：{emotion_table}） |
| `emotion_evaluate` | str | （出厂文案） | 回复/事件 ΔVAD 评估提示词（占位符：{lover_name} {persona} {user_context} {reply_text}） |
| `emotion_injection` | str | （出厂文案） | 情绪底色注入文本（占位符：{mood}） |
| `emotion_night_hint` | str | （出厂文案） | 深夜冻结态追加行（无占位符；非冻结态不出现） |
| `nap_reminder` | str | （出厂文案） | 赖床提醒模板（占位符：{now_hhmm} {planned_wake_hhmm} {nap_tool_name} {lie_in_max_until}） |
| `qzone_post` | str | （出厂文案） | 说说文案提示词（占位符：{lover_name} {personality} {date} {time} {activity}） |
| `qzone_reply` | str | （出厂文案） | 评论回复提示词（占位符：{lover_name} {personality} {moment} {nickname} {time} {comment} {impression}） |
| `qzone_comment_reminder` | str | （出厂文案） | 新评论提醒的主动 intent（占位符：{excerpt} {added}） |
| `nap_tool_denied` | str | （出厂文案） | 非睡眠周期调用赖床工具的拒绝话术（无占位符） |
| `nap_tool_bad_time` | str | （出厂文案） | 赖床时间解析失败提示（无占位符） |

#### [admin] 管理与日志（5 键）

| 键名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `admin_users` | list[str] | `[]` | 命令管理员（QQ 号或 qq:123456 形式）；留空 = 仅本机控制台可用管理子命令 |
| `allowed_streams` | list[str] | `[]` | 命令/注入允许的会话（session_id）；留空 = 不限制 |
| `llm_log_enabled` | bool | `true` | 记录 LLM 调用日志（JSONL，按天分文件） |
| `decision_log_enabled` | bool | `true` | 记录巡检/空间/规划决策日志（JSONL，按天分文件） |
| `log_retention_days` | int | `3` | 日志保留天数（ge=1、le=30） |

#### [qzone] QQ 空间（16 键）

| 键名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `false` | 启用 QQ 空间模块；需要 SnowLuma 适配器（NapCat 画像不支持 QZone API） |
| `check_interval_minutes` | int | `30` | 空间巡检间隔（分钟）：检查发说说的时机与新评论（ge=5、le=360） |
| `daily_max_posts` | int | `2` | 每日最多发几条说说；0=不发（ge=0、le=20） |
| `post_rate` | float | `0.5` | 每次空间巡检命中发帖时机时的实际发出概率（ge=0.0、le=1.0） |
| `emotion_filter` | bool | `true` | 情绪低落时跳过发帖：当前情绪效价 V<−0.6 时本轮不发说说（低落不硬营业） |
| `post_by_schedule` | bool | `true` | 根据日程发说说：优先挑当前活动节点，把活动内容转化成生活动态 |
| `require_activity` | bool | `true` | 只在没有静默/睡眠且日程里有当前活动时才发说说 |
| `reply_comments_enabled` | bool | `true` | 监听自己说说下的评论并自动回复：走 Web CGI 直连读取评论正文与楼中楼（cookie 经适配器 get_cookies 获取） |
| `reply_number` | int | `5` | 自动回评检查自己最近多少条说说下的评论（ge=1、le=20） |
| `reply_probability` | float | `1.0` | 每轮自动回评的触发概率（1.0=每次巡检都检查）（ge=0.0、le=1.0） |
| `notify_new_comments` | bool | `true` | 回退路径：cookie 不可用无法读评论正文时，检测到评论数变化经主动链提醒 |
| `silence_start` | str | `"01:00"` | 空间静默时段开始（HH:MM）：静默期内巡检不发说说、不监听评论（支持跨午夜；配置非法按静默处理） |
| `silence_end` | str | `"08:00"` | 空间静默时段结束（HH:MM） |
| `napcat_http_host` | str | `""` | 备选：NapCat HTTP 服务器地址（cookie 获取降级通道 2，如 127.0.0.1） |
| `napcat_http_port` | str | `""` | 备选：NapCat HTTP 服务器端口（如 9999）；留空则不用该通道 |
| `napcat_http_token` | str | `""` | 备选：NapCat HTTP 服务器的 access token |

#### [emotion] 情绪系统（18 键）

| 键名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 情绪模块总开关：关闭后停止情绪评估/注入/睡眠大修（命令与工具返回未启用提示） |
| `decay_rate` | float | `0.08` | 日常指数衰减率（每小时）：情绪向基线回落的速度；对话频繁可调高防无限累积（ge=0.01、le=0.3） |
| `evaluate_on_reply` | bool | `true` | 每次 bot 回复后用小模型评估该轮对话的情绪增量 ΔVAD（事件→增量映射）（已废弃，迁移为 evaluate_mode；显式设置 evaluate_mode 时本字段被忽略） |
| `evaluate_mode` | Literal["episode", "per_reply", "off"] | `"episode"` | 回复后情绪评估模式：episode=会话闲置后整段评估（默认，成本封顶）／per_reply=每回复评估（旧模式，60s 节流）／off=不评估 |
| `episode_idle_minutes` | int | `10` | episode 模式：会话闲置多少分钟后冲刷评估该流片段（ge=1、le=720） |
| `evaluate_max_per_day` | int | `10` | 情绪评估每日上限（次，两种模式共用，硬封顶；0=当日不评估）（ge=0、le=200） |
| `evaluate_min_interval_seconds` | int | `60` | 两次增量评估之间的最小间隔（秒），控制评估成本（ge=0、le=3600） |
| `global_event_multiplier` | float | `1.0` | 全局情绪事件幅度旋钮：作用于一切外部事件增量（不含日常衰减），运行期调控情绪整体波动大小（ge=0.1、le=3.0） |
| `non_target_multiplier` | float | `1.0` | 非 target 事件系数（群聊等非重要他人引发的事件）：默认 1.0 保持自然幅度，调低可淡化群聊影响（ge=0.0、le=1.0） |
| `target_bonus_step` | float | `0.1` | target 每档情绪加成步长（加算）：加成 = step × (档位+1)，档 0~4 对应 +0.1~+0.5（ge=0.0、le=1.0） |
| `injection_enabled` | bool | `true` | 把当前情绪底色（模糊化描述）注入 replyer 提示词，引导语气与情绪一致 |
| `dedup_enabled` | bool | `true` | 跨来源去重：自动评估（replyer）与 emotion_report 工具对同一事件的重复入账只计一次 |
| `dedup_window_seconds` | int | `180` | 跨来源去重时间窗（秒）：同一聊天流内两条来源先后入账超过该间隔则各自计入；0=关闭窗口判定（ge=0、le=3600） |
| `check_interval_minutes` | int | `10` | 睡眠检测循环间隔（分钟）：进入日程睡眠时段后触发睡眠裁判大修（ge=1、le=120） |
| `interrupt_reply_threshold` | int | `3` | 夜间打扰判定：同一时间窗内累计 reply 达到该次数才算「被吵醒」（只触发 planner、没有 reply 不算；生效还要求处于睡眠周期且 reply 来自 target 私聊）（ge=1、le=100） |
| `interrupt_window_minutes` | int | `10` | 夜间打扰判定的计数窗口（分钟）：窗口 = [最后一次 reply − 本值, 最后一次 reply]，随 reply 滚动（ge=1、le=1440） |
| `resleep_minutes` | int | `5` | 夜间打扰判定：自最后一次 reply 起该时长内无新 reply 即视为再次睡着（打扰段闭合，回到睡眠态）（ge=1、le=1440） |
| `emoji_enabled` | bool | `false` | emoji 情绪联动开关（默认关）：启用后服务层会按当前情绪标签向宿主 emoji.get_by_description 匹配一个表情。注意：当前没有任何组件消费这个能力（原消费入口已移除），实现保留备用；关闭时输出与旧版逐字一致 |


## 十一、FAQ / 常见问题

**① 配了目标 QQ，但没人私聊我？**
- 先确认 `[patrol] target_qq` 已改成真实 QQ 号：默认值 `123456789` 是占位值，此时**不解析目标会话**，插件不会主动私聊任何人。
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
- 两个总开关：`greeting_morning_enabled`（早安）、`greeting_night_enabled`（晚安），默认都开；关闭 = 这类问候完全不发（含目标私聊与群聊）。
- 覆盖范围（统一名单）：目标陪伴私聊**恒生效**（不看名单）；群聊看群聊名单 `proactive_group_ids` + 名单模式（默认黑名单，留空 = 全群）并受**必要性激活**限制；其他私聊看私聊名单 `proactive_private_ids` + 名单模式（**默认白名单，留空 = 全不发**）并受前一日消息数门槛限制。
- 群聊必要性激活（`greeting_require_activation`）：到点前该流需有人说过话，否则推迟到下一轮，到期限仍无人即放弃当天。
- 其他私聊要求**前一日**该流消息数 ≥ `greeting_other_private_min_messages`（最低 5）。
- 例行问候**始终免主动打扰预算**（不查闸门也不计用量），所以「预算用完」不是问候不来的原因。
- 去重：**目标私聊**同一流同日同类型（早安/晚安）至多一条，**热重载/重启后也生效**；群聊与其他私聊的跨热重载去重**尚未实现**，重载后同日可能再发一条。

**⑤ QQ 空间发不出去 / 评论不回复？**
- 用 `/lbm_life qzone status` 看通道命中情况与 cookie 状态。cookie 按多级降级获取：适配器 `get_cookies` → NapCat HTTP（`napcat_http_host/port/token`）→ 本地文件；全部拿不到时**无法读评论正文，只做「评论数变化」提醒而不回评**。
- 发帖被挡住常见原因：`[qzone] enabled=false`、处于空间静默时段（`silence_start`~`silence_end`）、当日已达 `daily_max_posts`、`post_rate` 概率未命中、`require_activity=true` 但当前没有日程活动、`emotion_filter=true` 且当前情绪效价过低。
- 回评还受 `reply_comments_enabled`、`reply_number`、`reply_probability` 控制；楼中楼回复需要 cookie 就绪。
- **图片说说**：动作通道（SnowLuma）支持带图说说；一旦回退到 Web CGI 通道，只支持纯文字说说（该通道的图片上传尚未接通）。

**⑥ 裁判类任务的输出是空的？**
把 `[llm] judge_model`（想念把关、角色裁判等判定）以及情绪睡眠裁判（`[llm] emotion_referee_task_name`）、好感裁判（`[llm] review_task_name`）、情绪评估（`[llm] emotion_evaluator_task_name`）绑到**真正的非思考型模型**。思考型模型会先把 `max_tokens` 预算花在推理内容上，导致正文为空、判定整体失效（这类失败按「驳回／跳过」安全降级）。必要时调大 `[llm] judge_max_tokens`。`/lbm_diag llm` 能看到实际使用的模型与失败形态。

**⑦ 怎么临时关掉某个模块？**
- `[qzone] enabled=false` → 关 QQ 空间；`[affection] enabled=false` → 关好感度 v2（回退旧的静态三档行为）；`[emotion] enabled=false` → 关情绪系统（评估/注入/睡眠大修全停，情绪相关工具与命令返回未启用提示）。
- `[schedule] enabled=false` / `[patrol] enabled=false` 分别关日程与巡检。
- `[plugin] enabled=false` 更彻底：只保留组件注册，不跑任何后台循环。
- 只想去掉某类主动打扰：`[planning_proactive] greeting_morning_enabled=false` / `greeting_night_enabled=false`（分别停早安/晚安）、`[patrol] miss_enabled=false`（想念）、`[planning_proactive] enable_proactive_trigger=false`（活动切换主动开口）。

**⑧ 升级后，配置和数据会怎样？**
- **配置**：旧配置在**首次启动时自动迁移**到新段（段名不变的键原样保留；迁移幂等、反复重启不二次改写），旧键不再出现在新界面；完整迁移映射见「十、配置总览」。注意：宿主显示的**插件版本为 1.1.0，而 `[plugin] config_version` 保持 1.0.0**——这是有意保留的（迁移依赖「版本号不变」），**请勿修改**。`[emotion] evaluate_on_reply=false` 的用户请**先**把 `evaluate_mode` 显式设为 `off` 再保存一次 WebUI（否则保存后会被写死为默认 `episode`）。
- **数据**：数据目录为 `data/plugins/cateye.life_belonging_to_maimai/`（自 1.0.0 起）；**1.0.0 之前的旧目录不会自动迁移**，需要手动把旧目录下的 `schedule.db`、`emotion_state.json`、`emotion_config.json`、好感档案与日志复制过去再重启。1.0.0 及以后版本之间升级无需搬运数据。
- 自定义的 `<数据目录>/emotion_mapping.json`（可选覆盖层）与 `proactive_budget.json` 同样在数据目录里，迁移时一并带上。

## 十二、已知问题

见 [CHANGELOG.md](CHANGELOG.md) 的「已知问题」节（统一登记，本手册不重复罗列）。

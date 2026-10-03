# 命令与工具手册（USAGE.md）

> 猫眼生活套件（github.cateye.cateye-suite）现有 **14 个 LLM 工具**、**12 个聊天指令**、**4 个跨插件 API**。
> 四个业务子模块各自提供一组：planning（日程规划）/ companion（虚拟恋人）/ emotion（情绪）/ qzone（QQ 空间）。
> 本手册给出每个条目的描述原文、参数、行为细节与权限要求；架构与安装说明见 README.md。

## 0. 通用行为说明（先读）

- **LLM 工具（Tool）**：由模型在对话推理中自主决定调用（deferred 池，经 tool_search 发现），参数由模型生成；给模型阅读的结果放在返回的 `content` 里。
- **聊天指令（Command）**：用户文本命中正则即执行，**命中后不再进入 LLM 回复流程**（intercept）。所有正则用负向前瞻 `(?<!\S)` 匹配，兼容「引用回复」场景下命令不在文本开头的情况。
- **权限模型**（详见 §4）：
  - planning 的 `/cateye_test_plan` 与 qzone 的写操作：`admin.admin_users` 白名单（兼容 `123456` 与 `qq:123456` 两种写法）；**留空 = 默认拒绝**，仅本机控制台（`is_local_operator`）可用；
  - companion 的全部 `/cateye_test_mai_*`：额外要求触达者 QQ 等于 `target.target_qq`（占位值 123456789 时一律拒绝，避免误发给陌生人）。
- 工具/指令的执行体都在各子模块的 service 里，组件层只做鉴权与转发；服务实例动态读配置，改 `config.toml` 热重载立即生效。
- **模块独立总开关**：四个业务模块各有自己的 `enabled`（`schedule.enabled` / `patrol.enabled` / `qzone.enabled` / `emotion.enabled`），关闭后该模块停止后台循环、主动触发与注入，工具与命令返回「模块未启用」提示；互不影响其他模块。新增模块请照此模式实现。

---

## 一、LLM 工具（14 个）

### planning — 日程规划（4 个）

#### `cateye_test_manage_goal_v4`
- **描述**：管理长期目标，支持创建、查看、更新、暂停、恢复、完成、取消、删除目标。
- **参数**：
  | 参数 | 类型 | 必填 | 说明 |
  |---|---|---|---|
  | action | string | ✔ | create / list / get / update / pause / resume / complete / cancel / delete |
  | goal_id | string | | 目标 ID（除 create 和 list 外都需要） |
  | name / description | string | | 目标名称/描述（create 时必需） |
  | goal_type | string | | health_check / social_maintenance / learn_topic / custom |
  | priority | string | | high / medium / low |
  | time_window | string | | `'HH:MM-HH:MM'`（如 `'09:00-10:30'`） |
  | deadline_hours | number | | 截止时间（从现在开始的小时数） |
  | parameters | string | | 目标参数（JSON 字符串） |
- **行为**：写入 SQLite `goals` 表（含未来约定 pending_commitment）；create 有长度限制与危险字符过滤；返回 `{"type": "goal_created"|"goal_list"|..., "content": 给模型看的文本}`。

#### `cateye_test_get_planning_status_v4`
- **描述**：查看今日日程安排，按时间顺序显示正在进行和即将到来的活动。
- **参数**：`detailed`（boolean，可选）是否显示详细信息。
- **行为**：返回今日日程三段文本（正在进行 / 即将到来 / 已完成），来源为 SQLite 日程条目。

#### `cateye_test_apply_schedule_v4`
- **描述**：应用之前生成的日程，将日程项转换为全局可执行的目标。
- **参数**：`schedule_data`（string，必填）日程数据 JSON（含 items 列表）。
- **行为**：把 JSON 日程重建为 ScheduleItem 后整体落库（time_slot+duration → start/end 分钟），供注入与主动行为消费。

#### `cateye_test_update_schedule_v4`
- **描述**：根据自然语言请求维护日程：角色裁判决定接受/未来约定/拒绝；接受时调整今日日程，未来约定写入候选清单。
- **参数**：`description`（string，必填）自然语言描述，如「下午两点一起学习」「周末打游戏」。
- **行为**：角色裁判（LLM）三选一——
  - `today` → 按请求重新生成今日日程（原日程作废，可叠加额外要求）；
  - `future` → 校验日期后写入 pending_commitment，到期由主动行为以强指令提醒；
  - `reject` → 裁判认为不合理（如凌晨约会），日程不变并说明理由。

### companion — 虚拟恋人（6 个）

#### `cateye_test_mai_lover_current_activity`
- **描述**：查询麦麦现在正在做什么。当用户问'在干嘛''在做什么'或想了解麦麦当前状态时调用。
- **行为**：经 planning 的 ScheduleQuery 读当前活动（含剩余时间与精神状态）；日程未就绪时返回降级文案。

#### `cateye_test_mai_lover_status`
- **描述**：查看麦麦恋人的当前状态：好感度档位、今日发言次数、日程摘要。
- **行为**：聚合好感度管理器（档位/今日发言/早晚安标记）与当前活动。

#### `cateye_test_mai_lover_schedule`
- **描述**：查看麦麦的今日完整日程安排。
- **行为**：全天日程文本（时间轴+描述）。

#### `cateye_test_mai_lover_send_message`
- **描述**：以麦麦恋人的口吻向用户主动发送一条恋人消息。
- **参数**：`message`（string，可选）要发送的消息文本，留空则自动生成。
- **行为**：发送到目标私聊，追加好感度情绪后缀（热恋档 10% 概率附括号小剧场）；成功计 1 次发言数并写决策日志；发送失败有明确错误返回。

#### `cateye_test_mai_lover_affection`
- **描述**：调整与关系对象的好感度：把 score 移动到 0/1/2 兼容档对应的 v2 5 档区间并重估档位（档位名称随关系类型变化，如恋人=熟悉/亲密/热恋）。
- **参数**：`level`（integer，必填）只能填 0、1 或 2。
- **行为**：3 档兼容入口——0/1/2 分别落到 v2 档位 1/2/4（score 取 30.0/57.5/94.0），并**强制重估档位（跳过迟滞）**，保证旧 `level()` 输出与输入一致；持久化后 planner 注入按 v2 5 档档位描述表达（后缀/小剧场仍按 3 档折算）。

#### `cateye_test_mai_lover_config`
- **描述**：查看麦麦恋人当前插件配置：巡检间隔、时间窗口、概率、今日发言上限、好感度。
- **行为**：只读配置摘要（单行）。

### qzone — QQ 空间（2 个）

#### `cateye_test_life_post_qzone`
- **描述**：以 bot 的口吻发一条 QQ 空间说说。message 留空则自动按当前活动生成文案。受每日上限限制。
- **参数**：`message`（string，可选）说说正文，留空按日程当前活动由 LLM 生成。
- **行为**：发布走双通道（SnowLuma 动作层 `send_qzone_msg` 优先，失败回退 Web CGI 直连）；成功返回带 `stop_after_execution=True`——发完即结束本轮思考，避免 planner 继续自说自话。

#### `cateye_test_life_qzone_moments`
- **描述**：查看 bot 最近发表的 QQ 空间说说（内容摘要、评论数、发表时间）。用户问空间/说说/动态相关话题时调用。
- **行为**：动作通道拉最近 5 条，含 tid（后续指令操作定位用）；空间模块未启用时明确提示。

---

### emotion — 情绪系统（2 个）

#### `cateye_test_emotion_mood`
- **描述**：查询 bot 当前的情绪底色（情绪标签与低分辨率描述）。用户问'你现在什么心情''感觉怎么样'时调用。
- **行为**：从 VAD 状态机取当前有效值（含偏移/乘数），映射为情绪标签（含混合比例）与"愉悦度/唤醒度/支配感"模糊描述；情绪模块未启用时明确提示。

#### `cateye_test_emotion_report`
- **描述**：上报一件影响 bot 心情的事件（由 bot 判断是否与最重要的人有关），入账有界情绪增量。
- **参数**：
  | 参数 | 类型 | 必填 | 说明 |
  |---|---|---|---|
  | event | string | ✔ | 一句话概括这轮交流发生了什么（谁说了什么、结果如何） |
  | is_target | boolean | | 是否由最重要的人（`target.target_qq`）引发；是=true；缺失/类型不对/胡填一律按非 target 处理，不报错 |
- **行为**：target 时 `K = 1 + target_bonus_step × (档位+1)`、非 target 时 `K = non_target_multiplier`，再乘 `global_event_multiplier` → 事件文本走小模型有界评估 ΔVAD（±0.5）→ 入账；与其他事件共用评估节流（`evaluate_min_interval_seconds`）与 ±0.5 硬限幅，结果落决策日志（`emotion.external_event`）。
- **备注**：Tool 默认进 **deferred 工具池**（未设 `core_tool=True`），bot 自发调用不保证——**触发条件已写进 `detailed_description`**（仅群聊里出现重要的人相关/由 TA 引发且影响心情的事件才调用；普通群友闲聊与私聊无需调用，`replyer.after_response` 自动评估始终是主通道），本工具只是群聊场景的补充。

## 二、聊天指令（12 个）

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

### companion — /cateye_test_mai_*（8 个，全部需要鉴权）

> 鉴权规则：触发者 QQ == `target.target_qq`，或本机控制台；目标 QQ 为占位值 123456789 时一律拒绝。无权限时回复「没有权限」并拦截。

| 指令 | 描述 | 行为 |
|---|---|---|
| `/cateye_test_mai_status` | 查看麦麦恋人状态（好感度、发言计数、日程摘要） | 状态报告直发 |
| `/cateye_test_mai_schedule` | 查看麦麦今日完整日程 | 全天日程直发 |
| `/cateye_test_mai_affection [0\|1\|2]` | 查看/调整好感度：不带参数输出 score/baseline/5 档档位/今日 Δ + 最近印象（最多 2 条，「最近印象」行）与关系温度（tone，「关系温度·日期」行）——印象与 tone 为 M2 关系日结产出，为空则不显示（tone 读取时按 7 天过期判断）；带参数按 3 档兼容口径调整 | 0/1/2 → score 移到 v2 档位 1/2/4（取 30.0/57.5/94.0）并强制重估（跳过迟滞）；参数取正则命名捕获组（兼容引用+命令混排）；缺参/非法值提示用法与当前状态 |
| `/cateye_test_mai_help` | 查看麦麦恋人所有可用命令 | 命令清单直发 |
| `/cateye_test_mai_config` | 查看当前插件配置摘要 | 巡检间隔/时间窗口/概率/发言上限/好感度 |
| `/cateye_test_mai_llm_log [天数]` | 查看插件发起的 LLM 调用日志 | JSONL 按天读取，合并转发；未启用时提示配置项 |
| `/cateye_test_mai_diag [天数]` | 查看主动行为决策日志 | 每轮巡检一条（为什么发言/为什么没发言），合并转发 |
| `/cateye_test_mai_test` | 发送一条测试消息以验证发送通道 | 目标私聊直发一条测试文本 |

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
  | `/cateye_test_life diag [天数]` | 决策日志最近 N 天（默认 1，最多 7） |
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
| `/cateye_test_mai_*` 全部 | 目标 QQ ∪ 本机控制台 | `target.target_qq`（占位值 123456789 时全拒） |
| `/cateye_test_life status` / `help` / `qzone moments` / `qzone comments` | 所有人 | —— |
| `/cateye_test_life diag` / `qzone post` / `qzone reply` / `qzone like` | 管理员 ∪ 本机控制台 | `admin.admin_users` |
| 全部 LLM 工具 | 模型自主调用（无用户级鉴权；敏感操作如发说说受每日上限/开关约束） | `qzone.daily_max_posts`、`qzone.enabled` 等 |

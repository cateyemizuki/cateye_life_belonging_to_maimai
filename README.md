# 属于麦麦的生活（lbm）— 整合插件 v1.1.0

> 一个插件，给麦麦一套「自己的生活」：**自主规划日程** ＋ **虚拟陪伴关系** ＋ **QQ 空间运营** ＋ **情绪维护系统**。

**功能亮点**

- **自主规划日程**：按人设由大模型生成每天的生活（睡眠、三餐、工作、娱乐），把「麦麦现在在做什么」注入对话；活动切换、约定到期时主动开口；`/lbm_plan` 随时查看，还能渲染日程图片。
- **虚拟陪伴**：好感度养成（连续分＋恋人五档＋日衰减）、早晚安问候、想念、日程节点分享、陪伴对象电脑的屏幕感知旁白——都跑在概率、每日预算与静默时段的约束下。
- **QQ 空间**：按日程自动发说说、自动回复评论（含楼中楼）与点赞；cookie 取不到时自动降级成「有新评论」的提醒。
- **情绪维护系统**：VAD 三维情绪状态机 + 25 种情绪原型映射；日常衰减、事件增量、睡眠期「大修」结算；情绪反过来影响主动频率、想念阈值、发帖意愿与回复语气。

📖 版本变更与本版已知问题见 **[CHANGELOG.md](CHANGELOG.md)**；每个工具/指令的参数、权限、11 段配置总览与完整配置键总表见维护手册 `USAGE.md`，情绪映射现状导出见 `EMOTION_MAPPING.md`——两份维护文档在插件目录旁的开发文档目录 `..\cateye_life_belonging_to_maimai开发文档\`（**不随插件包发布**）。

## 2026-10-10 配置大整理（1.1.0）

本次把配置从 13 段重排为 **11 段**，并集中了提示词与模型调用；**功能行为不变**，旧配置会在插件首次启动时**自动迁移，无需手改**（迁移幂等）。你需要知道：

- **段合并**：`[target]`、`[cateye]` 并入 **`[patrol]`（陪伴模块）**；`[judge]` 并入新增 **`[llm]`（LLM 调用）**；`[inject]`、`[planning_cleanup]` 与原 `[schedule]` 的一批深水项并入新增 **`[schedule_advanced]`（日程系统·高级设置）**；插件发给模型的全部原文集中到新增 **`[prompts]`（提示词·高级）**。
- **早/晚安简化**：三类会话开关与问候专用名单收拢为**两个总开关**（`[planning_proactive] greeting_morning_enabled` / `greeting_night_enabled`，默认都开）＋**统一名单**（群聊 `proactive_group_ids` / 私聊 `proactive_private_ids`，各带名单模式）；早/晚安**始终免主动打扰预算**。
- **预算收窄**：每日预算 `[patrol] daily_max_speak` **即上限**（不再有独立的目标私聊打扰上限字段），只统计陪伴侧的日程巡检与想念。
- **想念阈值合并**：`miss_hours_min` + `miss_hours_max` 合并为 `miss_silence_hours`（如 `"4-8"`）。
- **注入矩阵定稿**：目标陪伴私聊**始终**注入好感度；其他私聊**始终只注入日程**；群聊仅在上下文出现陪伴对象且 `[patrol] inject_affection_in_group`（默认开）时注入好感度档位块。日程注入范围 `[schedule] inject_scope` 默认 `all`。
- **已移除的配置项**：旧问候时间窗（`morning_start` 等）、问候免预算开关、两处最小间隔豁免、想念避让未来日程、目标私聊打扰上限等——新界面不再提供，旧值会被自动迁移或忽略。
- **两处修复**：`[affection] current_level` 以字符串 `"1"` 提交时不再导致整份配置校验失败；日程图片出图与图片输出目录两处修正。
- **早晚安链修复（B1/B2）**：问候改为「先掷点 → 只在**该会话有人说过话**时开口（所有会话一视同仁，含目标私聊）→ 未到点等待 / 已过点立即发」；晚安漂移点锚定在**睡前界**（`min(入睡时间, 生效静默起点)`；静默起点留空 = 跟随入睡时间）向前随机，不再在最后一段日程还没结束时开口；静默时段不再拦截问候（默认静默窗 00:00-07:30 与起床 07:00 重叠，此前会把漂移整段吞掉）；**黑名单严格生效**——把某个群/私聊（含目标私聊自己的 QQ）写进统一名单的黑名单，就不再问候它。
- **旧问候配置位已彻底移除**：9 个隐藏兼容字段（旧问候开关/黑白名单）从配置面删除——此前填了它们"看起来配了却没生效"（B2 的成因之一）。旧值仍在**首次启动的自动迁移**里并入统一名单与门槛，无需手改；**例外**：`greeting_target_enabled` / `greeting_group_enabled` 两个旧开关的显式 `false` 不保留（升级后按新默认"开"生效，要停掉某类问候请改统一名单与两个总开关）。
- **版本说明**：宿主显示的**插件版本与 `[plugin] config_version` 均为 1.1.0**（同步）；升级到本版时旧配置的跨段/改名键由「磁盘旧文件抢救 + 一次性迁移器」搬运并落盘，**升级不丢配置**。

详细迁移映射与新段总览见维护手册 `USAGE.md` 的「十、配置总览」（位于 `..\cateye_life_belonging_to_maimai开发文档\`），完整变更见 **[CHANGELOG.md](CHANGELOG.md)**。

## 2026-10-10 第三轮修复（真机问题）

本版在同一版本号内收口了第三轮真机问题（详见 CHANGELOG 的「变更 / 修复 / 已知问题」）：

- **日程不再被压成 15 分钟（N2/N4）**：日程写库改为「跨午夜 = 同一个 24h 环」——睡觉条目保留原时长与跨午夜写法（23:00 入睡 + 8h ⇒ `23:00–07:00`），不再被起床/入睡锚点截断成 `23:00–23:15`，也不会在"入睡时间早于条目起点"的作息下把全天条目压扁、凭空制造 45–225 分钟空档。**已被压坏的历史日程请用 `/lbm_plan regenerate` 重建**。
- **空档成为可重试的质量问题（N1/N2）**：空档改在写库前对归一化条目复查（睡眠时段不算空档），并单项重罚 0.25（0.85 质量线以下 → 触发下一轮重试）；覆盖率改分钟级。
- **空档期不再"什么都不注入"（N1）**：无当前活动时，planner 与 replyer 照常注入，当前行/状态行换成 `[prompts] inject_no_activity`（默认「现在没有安排中的活动（可能在休息）。」）；**留空 = 回退旧行为**。
- **静默窗留空 = 跟随日程作息（N5）**：`[patrol] silence_start / silence_end` 默认改为**留空**——留空即跟随 `[schedule]` 的入睡~起床，非空才是自定义静默窗（支持跨午夜；两键可各自独立跟随/覆盖）。巡检静默闸与晚安睡前界共用同一生效值；值非法仍按「宁可不发」fail-safe。显式写过值的旧配置逐值不变。
- **注入链不再吞 session_id（N6）**：陪伴/情绪两个 planner 钩子改为整份回写 `modified_kwargs`（含 `session_id`），好感度块与会话维度缓存键恢复；replyer 改用真实意图 + 独立冷却桶，且不再把宿主追加的尾部指令当用户消息。
- **用户可见文案去掉 emoji（N7）**：命令/工具返回、注入提示词与状态图标题改中文词（`提示：` / `注意：` / `是·否` 等）；日志与注释不清洗。


## 安装

1. 把插件目录放进 MaiBot 的 `plugins/` 下（目录名建议 `cateye_life_belonging_to_maimai`）。
2. 重启 MaiBot（或在插件管理里重载插件）。
3. 在 WebUI 的插件配置里填 **目标 QQ**（`[patrol] target_qq`）——陪伴功能只服务这一个私聊会话。**保持默认占位值 `123456789` 时不解析目标会话**：不会主动私聊任何人，命令鉴权也一律关闭。
4. （可选）在 `[admin] admin_users` 填上你的 QQ 号，才能从 QQ 侧使用管理员指令；**留空时仅本机控制台可用**。

**依赖**：`httpx>=0.24`、`Pillow>=9.0`。Pillow 只用于日程图／状态图渲染，**缺失或渲染失败时自动降级为纯文本**，不影响其他功能。

**可选开关**（默认全关，开了才生效）：屏幕感知（`[patrol] screen_enabled`）、QQ 空间（`[qzone] enabled`）、周结大事记（`[affection] weekly_milestones_enabled`）、emoji 情绪联动（`[emotion] emoji_enabled`，当前无组件消费，开启暂不生效）。

**声明能力**：`_manifest.json` 共声明 17 项宿主能力与 2 项 Python 依赖（发文本／图片／合并转发、配置读取、LLM 生成、会话查询、消息回查、主动触发、上下文追加、频率调节、知识库检索、跨插件 API、数据库只读、emoji 按描述匹配）。

## 快速上手

1. **看总览**：`/lbm_dashboard` —— 一条命令看四模块状态（日程／关系／情绪／空间），渲染状态图，渲染失败自动降级为纯文本（需管理员）。
2. **看日程**：`/lbm_plan` —— 首次会自动生成今日日程；`/lbm_plan regenerate 加入下午跑步` 可重新生成。
3. **初始化情绪**：`/lbm_emotion_init` —— 由宿主人设推导情绪锚点与基线；之后正常聊天即可观察语气变化，`/lbm_emotion_config` 查看或重置。
4. **排障**：`/lbm_life status` 看各模块与通道状态；出问题用 `/lbm_diag llm|decision|life` 看调用与决策日志（需管理员）。
5. **QQ 空间**（先开 `[qzone] enabled`）：`/lbm_life qzone status` 看通道与 cookie，`/lbm_life qzone post 测试` 手动发一条，`comments` + `reply` 验证楼中楼回复。

早晚安、想念、活动切换主动开口无需触发命令，会按配置的时段、概率与预算自行发生。

## 功能说明

### 日程规划（planning）

- 每日自动生成日程（默认「当天最后一个非睡眠活动结束后 90 分钟」生成次日），当天已有日程则跳过；模板兜底、过期清理、长期目标与未来约定都在 SQLite 里持久化（WAL + 损坏自愈）。
- 生成质量可控：多轮生成 + 质量评分提前达标、三餐窗口硬约束、语义校验、自定义生活阶段、可回读最近日程／群聊历史／知识库作为上下文；生成文案与各注入块可在 `[prompts]` 逐项定制，深度调优项集中在 `[schedule_advanced]`。
- planner 与 replyer 双通道注入当前活动（含意图分类与注入优化：冷却、概率、TTL），注入范围由 `[schedule] inject_scope` 决定（默认 `all` 覆盖所有会话）；并按活动类型动态调节聊天频率。
- 主动行为：活动切换主动开口、早/晚安问候、约定到期强指令、睡眠窗口抑制。
- 真实节假日：`[schedule] holiday_inject_enabled`（默认开）把日期／节假日写进日程块头部，`[schedule] holiday_affects_schedule`（默认关）让生成考虑节假日。
- 自然语言约定：`lbm_update_schedule_v4` 由「角色裁判」判断接受／未来约定／拒绝，接受则重排今日日程。
- `/lbm_plan` 命令：查看（图片 + 文字合并转发，失败降级纯文本）、重新生成、删除目标、清理旧日程、帮助。

### 虚拟陪伴（companion）

- **好感度**：`score`（0~100 连续分，初始 30）+ `baseline`（慢速 EMA 关系底色）+ 恋人五档（生疏／熟悉／亲密／热恋／挚爱）；档位升降有迟滞带与最短停留时长；日切时 score 向 baseline 回归。
- **记账**：一问一答、沉默后对方先开口、早晚安、约定兑现／违约等规则事件零 LLM 成本入账，单日正负增量各自限幅；另有可选的 LLM 裁判（会话采样 + 睡眠时段关系日结）产出增量、印象与关系温度。
- **巡检主动链**：日切 → 静默（fail-safe）→ 用户冷却 → 预算 → 早/晚安状态检查（陪伴侧从不触发问候，只按已发标记去重）→ 想念（沉默阈值 `miss_silence_hours` 随机采样 + 概率 + LLM 把关 + 驳回冷却）→ 日程巡检（分享／日常搭话两个概率，可用 `patrol_activity_enabled` 关闭）。
- **早/晚安**：由日程侧驱动，两个总开关 `greeting_morning_enabled` / `greeting_night_enabled`（默认都开）＋统一名单；**所有会话都先看「该会话有人说过话」才开口**（含目标私聊——避免对着空会话自说自话），早安在起床漂移点、晚安在睡前漂移点发出；问候**始终免主动打扰预算**、**不受静默时段拦截**。
- **主动打扰预算**：每日上限 `[patrol] daily_max_speak` **即上限**，只统计发往目标陪伴私聊的日程巡检与想念；达上限即拦截并留痕决策日志。
- **关系设定**：`[patrol] relation_type` 决定对方是恋人／挚友／维护者／家人／自定义称谓，影响想念提示词、消息口吻与好感档位名称。
- **好感度注入矩阵**：目标陪伴私聊**始终**注入；其他私聊**始终不注入**；群聊仅在上下文出现陪伴对象且 `[patrol] inject_affection_in_group`（默认开）时注入档位块。
- **模型调用集中于 `[llm]`**：主任务名与判定类模型／参数（想念把关、角色裁判等共用 `judge_*`）、各用途任务名都集中在此段；`[prompts]` 可定制全部提示词。
- **屏幕感知**（可选）：经 connect-hub 截图并由视觉模型转述一行旁白，锚点滑出即失效。
- **节假日服务**：多级降级获取节假日信息。

### QQ 空间（qzone）

- 按日程发说说：静默时段 → 当前活动 → 概率 → 每日上限 → LLM 文案；发布走双通道（SnowLuma 动作层优先，失败回退 Web CGI 直连）。
- 自动回评：cookie 多级降级获取（适配器 → NapCat HTTP → 本地文件）→ CGI 读评论（含楼中楼）→ 过滤自己与已处理 → LLM 回复 → 楼中楼回评；**cookie 不可用时降级为「评论数基线 + 提醒」**。
- 情绪联动：当前情绪效价过低时本轮不发说说；说说文案与回评各拼一行情绪底色。
- 手动命令：`/lbm_life qzone comments|reply|like` 分别用于列评论、回复（含楼中楼）、点赞。

### 情绪维护（emotion）

- **状态机**：LLM 只输出「事件 → 增量」，绝对值由状态机维护——日常指数衰减 → 叠加增量 → 静态偏移 → 增益／衰减乘数 → 裁剪。
- **映射**：25 种情绪原型（10 正 / 3 中 / 12 负）按加权欧氏距离映射，阈值内出复合标签，对外只用低分辨率模糊描述（防漂移）。
- **评估**：默认「会话片段制」——回复后只登记会话片段（零 LLM），会话闲置 10 分钟后整段交给小模型转成 ΔVAD（±0.5 限幅，每日有硬上限）；也可切回逐条评估或关闭。
- **睡眠结算**：进入作息夜间窗口后冻结整夜（事件只入队），由睡眠裁判 LLM 独立请求算出大修量并缓存，醒来时一次性应用并解冻——睡眠时长取**实际睡着时长**，恢复系数有下界。
- **情绪 → 行为**：纯规则零成本地给出主动概率乘数、想念阈值乘数、发帖率与语义提示行；模块停用或读不到情绪时全部退回中性值。
- **人设初始化**：`/lbm_emotion_init` 推导锚点／偏移／乘数（数值校验裁剪后落盘）；`/lbm_emotion_config` 查看、`reset` 恢复默认、`clear` 清空当天日志。
- **可选覆盖层**：`<数据目录>/emotion_mapping.json` 可整表替换四张映射数据表，解析或校验失败即全量回退代码默认。

## 组件总览（29 个组件）

| 类型 | 组件 |
|---|---|
| Hook ×10 | planning：`maisaka.planner.before_request`（注入当前日程，items 快照协议）、`maisaka.replyer.before_request`（把当前活动作为额外提示注入）｜companion：`chat.receive.before_process`（缓存发送者身份）、`chat.receive.after_process`（记录目标私聊发言时间）、`maisaka.planner.before_request`（屏幕旁白 + 好感度后缀）、`maisaka.replyer.before_model_request`（屏幕旁白）、`maisaka.replyer.after_response`（补计发言数，只观察）｜emotion：`maisaka.planner.before_request`、`maisaka.replyer.before_model_request`（情绪底色注入）、`maisaka.replyer.after_response`（情绪增量评估，只观察；夜间兼作打扰记账） |
| Tool ×7 | planning：`lbm_get_schedule_info`（只读四视图 status/current/lover_day/goals）、`lbm_update_schedule_v4`（**唯一日程写入口**，角色裁判）、`lbm_nap_lie_in`（赖床：把今日睡眠条目的结束时间推到指定时刻；平时不进工具名册，只在赖床提醒轮被点名）｜companion：`lbm_mai_lover_send_message`、`lbm_mai_lover_affection`｜qzone：`lbm_life_qzone`（action=post 发说说 / moments 查说说）｜emotion：`lbm_emotion_report`（群聊事件上报） |
| Command ×8 | planning：`/lbm_plan`、`/lbm_规划`（list/regenerate/delete/clear/help，图片 + 合并转发）｜companion：`/lbm_mai_affection`、`/lbm_mai_help`、`/lbm_diag`（llm/decision/life 三合一排障；别名 `/lbm_mai_llm_log`、`/lbm_mai_diag`、`/lbm_life diag`，管理员）、`/lbm_dashboard`（聚合总览，管理员）｜qzone：`/lbm_life`（status / qzone[moments/comments/post/reply/like] / diag / help）｜emotion：`/lbm_emotion_init`、`/lbm_emotion_config`（查看/reset/clear） |
| API ×4 | `get_current_activity`（planning，兼容原插件返回结构）｜`companion_get_current_activity` / `companion_get_schedule` / `companion_get_affection_level`（companion） |

## 常用配置速查

| 配置键 | 默认 | 说明 |
|---|---|---|
| `[plugin] enabled` | `true` | 插件总开关；关掉后只保留组件注册，不跑任何后台循环 |
| `[patrol] target_qq` | `123456789` | 陪伴对象 QQ；**保持占位值 = 不主动私聊、命令鉴权关闭** |
| `[admin] admin_users` | `[]` | 管理员 QQ 号（支持 `qq:123456` 写法）；留空 = 仅本机控制台 |
| `[schedule] wake_time` / `sleep_time` | `07:00` / `23:00` | 起床／入睡锚点，决定睡眠窗口、早/晚安基准与日程帧 |
| `[schedule] auto_generate_after_last_end_minutes` | `90` | 次日照程在「当天最后一个非睡眠活动结束后」多久生成 |
| `[schedule] inject_scope` | `all` | 日程注入范围：`all` 所有会话 / `target_private` 仅目标私聊 |
| `[planning_proactive] greeting_morning_enabled` / `greeting_night_enabled` | `true` / `true` | 早安／晚安总开关 |
| `[planning_proactive] greeting_require_activation` | `true` | 有人说过话才问候（观察窗：早安=起床→第一段日程结束；晚安=最后一段日程结束→睡前界）；关 = 到点就发 |
| `[planning_proactive] morning_drift` / `night_drift` | `"10-30"` | 漂移区间（分钟）：早安 = 起床锚点 + 随机；晚安 = 睡前界（`min(入睡时间,生效静默起点)`）− 随机 |
| `[patrol] silence_start` / `silence_end` | `""` / `""` | 巡检静默窗：**留空 = 跟随 `[schedule]` 入睡~起床**；非空 = 自定义静默窗（HH:MM，支持跨午夜；两键可各自独立跟随/覆盖）。晚安睡前界与巡检闸共用该生效值 |
| `[planning_proactive] proactive_group_ids` / `proactive_private_ids` | `[]` / `[]` | 统一名单：群聊名单（默认黑名单，留空 = 全群）／私聊名单（默认白名单，留空 = 全不发；目标私聊在白名单下恒生效、写进黑名单则被排除） |
| `[patrol] daily_max_speak` | `5` | 每日主动预算**即上限**（只统计日程巡检与想念；早/晚安免预算） |
| `[patrol] miss_silence_hours` | `"4-8"` | 想念阈值区间（小时） |
| `[patrol] inject_affection_in_group` | `true` | 群聊出现陪伴对象时注入好感度档位块 |
| `[qzone] enabled` | `false` | QQ 空间模块总开关 |
| `[affection] enabled` | `true` | 好感度 v2；关掉回退旧的静态三档行为 |
| `[emotion] enabled` / `evaluate_mode` / `injection_enabled` | `true` / `episode` / `true` | 情绪模块总开关／评估模式／情绪底色注入 |
| `[schedule_advanced] emotion_influence` | `0.6` | 情绪对日程的影响强度（`0` = 关闭） |
| `[llm] task_name` / `judge_model` | `replyer` / `""` | 主任务名／判定用模型（留空跟随主任务）；插件全部模型调用集中于此段 |

> 全部 **11 个配置段、209 个可见配置项**的**键名｜类型｜默认值｜说明**总表、旧配置迁移映射、已移除项清单、注入矩阵与预算口径，见维护手册 `USAGE.md` 的「十、配置总览」（位于 `..\cateye_life_belonging_to_maimai开发文档\`）。

## FAQ

- **配了目标 QQ 还是没人私聊我？** 先确认 `[patrol] target_qq` 已改成真实 QQ（占位值不解析）；插件需要能在宿主里查到该 QQ 的**私聊流**，查不到时每 60 秒重试一次——和目标 QQ 先互发一条消息通常就能解析到。详见维护手册 `USAGE.md` 的 FAQ（位于 `..\cateye_life_belonging_to_maimai开发文档\`）。
- **命令没反应？** 管理员指令要求 `admin.admin_users` 里有你（留空时只有本机控制台能用）；另外若配置了 `admin.allowed_streams`，不在名单内的会话会被静默拒绝。
- **日程图／状态图只有文字没有图？** 这是预期的静默降级：Pillow 或字体不可用、渲染超时（状态图 10 秒／日程图 15 秒）时改发纯文本，功能不受影响。
- **早安／晚安没来、或来了两条？** 看两个总开关 `greeting_morning_enabled` / `greeting_night_enabled` 与统一名单。**问候要"有人说话"才发**（`greeting_require_activation`，默认开，所有会话一视同仁）：早安的观察窗 = 起床锚点 → 当天第一段日程结束，晚安的观察窗 = 最后一段日程结束 → 睡前界（`min(入睡时间, 生效静默起点)`）；窗内没人说话就不发。漂移点：早安 = 起床 + 10~30 分钟；晚安 = 睡前界 − 10~30 分钟（都在各自区间里随机，**不再被静默时段吞掉**）。例行问候**始终免预算**，所以「预算用完」不是原因。目标私聊同日同类型至多一条（热重载后也生效），群聊／非目标私聊跨热重载去重尚未实现。
- **QQ 空间发不出去／评论不回复？** 用 `/lbm_life qzone status` 看通道与 cookie：cookie 不可用时会降级成「有新评论」的提醒而不回评；`qzone.enabled=false`、静默时段、每日上限、`emotion_filter` 都会拦住发帖。（另：图片说说只有动作通道支持，CGI 回退通道的图片上传尚未接通。）
- **裁判类任务输出为空？** 建议把 `[llm] judge_model` 与情绪裁判（`[llm] emotion_referee_task_name`）、好感裁判（`[llm] review_task_name`）的任务绑到**真正的非思考型模型**——思考型模型会把小 `max_tokens` 预算花在推理上，导致正文为空。
- **怎么临时关掉某个模块？** `[qzone] enabled=false`、`[affection] enabled=false`、`[emotion] enabled=false` 分别关闭对应模块；`[plugin] enabled=false` 则只注册组件、不跑任何循环。
- **升级后配置和数据会怎样？** **配置**：旧键在首次启动时自动迁移到新段（幂等，反复重启不二次改写），无需手改；本版（1.1.0）升级时会额外做一次「磁盘旧文件抢救」——宿主在配置版本变化时会先按新骨架重建设置文件，插件在此之前读回磁盘上的旧值并搬运，因此**跨段/改名键的用户值不会丢**。`[plugin] config_version` = **1.1.0**（与插件版本同步，不要手改）。**数据**：数据目录 `data/plugins/cateye.life_belonging_to_maimai/` 与 1.0.0 相同，本版无需搬运数据。

## 数据目录

```
data/plugins/cateye.life_belonging_to_maimai/
```

日程库、情绪状态、好感档案、空间状态与各类日志都在这里；插件只在宿主给定的数据目录内读写。

## 已知问题

见 [CHANGELOG.md](CHANGELOG.md) 的「已知问题」节（统一登记，本文件不重复罗列）。

## 许可

MIT。

> 日程规划模块（`planning/`）是对一个 **AGPL-3.0** 自主规划插件的机制复刻（重写实现）；若后续大量照搬其源码，发布时整体应按 AGPL-3.0 处理。

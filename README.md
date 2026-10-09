# 属于麦麦的生活（lbm）— 整合插件 v1.0.0

> 一个插件，给麦麦一套「自己的生活」：**自主规划日程** ＋ **虚拟陪伴关系** ＋ **QQ 空间运营** ＋ **情绪维护系统**。

**功能亮点**

- **自主规划日程**：按人设由大模型生成每天的生活（睡眠、三餐、工作、娱乐），把「麦麦现在在做什么」注入对话；活动切换、约定到期时主动开口；`/lbm_plan` 随时查看，还能渲染日程图片。
- **虚拟陪伴**：好感度养成（连续分＋恋人五档＋日衰减）、早晚安问候、想念、日程节点分享、陪伴对象电脑的屏幕感知旁白——都跑在概率、每日预算与静默时段的约束下。
- **QQ 空间**：按日程自动发说说、自动回复评论（含楼中楼）与点赞；cookie 取不到时自动降级成「有新评论」的提醒。
- **情绪维护系统**：VAD 三维情绪状态机 + 25 种情绪原型映射；日常衰减、事件增量、睡眠期「大修」结算；情绪反过来影响主动频率、想念阈值、发帖意愿与回复语气。

**命名空间**：1.0.0 起统一为 `lbm`——工具名 `lbm_*`、指令名 `/lbm_*`（旧前缀入口不再提供）；插件清单 `id` 为 `cateye.life_belonging_to_maimai`。

📖 每个工具/指令的参数、权限与**完整配置键总表**见 **[USAGE.md](USAGE.md)**；情绪系统的映射关系现状导出见 **[EMOTION_MAPPING.md](EMOTION_MAPPING.md)**（代码为唯一真源）；版本变更与本版已知问题见 **[CHANGELOG.md](CHANGELOG.md)**。

## 安装

1. 把插件目录放进 MaiBot 的 `plugins/` 下（目录名建议 `lbm`）。
2. 重启 MaiBot（或在插件管理里重载插件）。
3. 在 WebUI 的插件配置里填 **目标 QQ**（`[target] target_qq`）——陪伴功能只服务这一个私聊会话。**保持默认占位值 `123456789` 时不解析目标会话**：不会主动私聊任何人，命令鉴权也一律关闭。
4. （可选）在 `[admin] admin_users` 填上你的 QQ 号，才能从 QQ 侧使用管理员指令；**留空时仅本机控制台可用**。

**依赖**：`httpx>=0.24`、`Pillow>=9.0`。Pillow 只用于日程图／状态图渲染，**缺失或渲染失败时自动降级为纯文本**，不影响其他功能。

**可选开关**（默认全关，开了才生效）：屏幕感知（`[cateye] enabled`）、QQ 空间（`[qzone] enabled`）、周结大事记（`[affection] weekly_milestones_enabled`）、emoji 情绪联动（`[emotion] emoji_enabled`）。

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
- 生成质量可控：多轮生成 + 质量评分提前达标、三餐窗口硬约束、语义校验、自定义提示词、可回读最近日程／群聊历史／知识库作为上下文。
- planner 与 replyer 双通道注入当前活动（含意图分类与注入优化：冷却、概率、TTL），并按活动类型动态调节聊天频率。
- 主动行为：活动切换主动开口、早晚安问候、约定到期强指令、睡眠窗口抑制。
- 自然语言约定：`lbm_update_schedule_v4` 由「角色裁判」判断接受／未来约定／拒绝，接受则重排今日日程。
- `/lbm_plan` 命令：查看（图片 + 文字合并转发，失败降级纯文本）、重新生成、删除目标、清理旧日程、帮助。

### 虚拟陪伴（companion）

- **好感度**：`score`（0~100 连续分，初始 30）+ `baseline`（慢速 EMA 关系底色）+ 恋人五档（生疏／熟悉／亲密／热恋／挚爱）；档位升降有迟滞带与最短停留时长；日切时 score 向 baseline 回归。
- **记账**：一问一答、沉默后对方先开口、早晚安、约定兑现／违约等规则事件零 LLM 成本入账，单日正负增量各自限幅；另有可选的 LLM 裁判（会话采样 + 睡眠时段关系日结）产出增量、印象与关系温度。
- **巡检主动链**：日切 → 静默（fail-safe）→ 用户冷却 → 预算（晚安预留）→ 早晚安去重 → 想念（沉默阈值随机采样 + 概率 + LLM 把关 + 驳回冷却 + 可选避开未来日程）→ 日程节点分享／日常搭话。
- **主动打扰预算**：每日上限只统计发往目标陪伴私聊的主动打扰；达上限即拦截并留痕决策日志。
- **关系设定**：`[patrol] relation_type` 决定对方是恋人／挚友／维护者／家人／自定义称谓，影响想念提示词、消息口吻与好感档位名称。
- **判定类调用独立成段**：`[judge]` 可把想念把关等判定请求单独指到便宜、非思考型的任务／模型。
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
| `[target] target_qq` | `123456789` | 陪伴对象 QQ；**保持占位值 = 不主动私聊、命令鉴权关闭** |
| `[admin] admin_users` | `[]` | 管理员 QQ 号（支持 `qq:123456` 写法）；留空 = 仅本机控制台 |
| `[schedule] wake_time` / `sleep_time` | `07:00` / `23:00` | 起床／入睡锚点，决定睡眠窗口、早晚安基准与日程帧 |
| `[schedule] auto_generate_after_last_end_minutes` | `90` | 次日照程在「当天最后一个非睡眠活动结束后」多久生成 |
| `[planning_proactive] greeting_target_enabled` | `true` | 目标私聊早／晚安开关（目标 QQ 不看名单，必定触发） |
| `[planning_proactive] greeting_group_enabled` | `true` | 群聊早／晚安开关（受群名单与「必要性激活」限制） |
| `[planning_proactive] greeting_other_private_enabled` | `false` | 非目标私聊早／晚安开关；开启后还须命中白名单与消息数门槛 |
| `[planning_proactive] greeting_exempt_budget` | `true` | 例行问候免主动打扰预算（不查闸门也不计用量） |
| `[patrol] daily_max_speak` / `proactive_target_daily_cap` | `5` / `0` | 每日主动发言预算／目标私聊主动打扰上限（`0` = 自动取「每日预算 + 2」） |
| `[qzone] enabled` | `false` | QQ 空间模块总开关 |
| `[affection] enabled` | `true` | 好感度 v2；关掉回退旧的静态三档行为 |
| `[emotion] enabled` / `evaluate_mode` / `injection_enabled` | `true` / `episode` / `true` | 情绪模块总开关／评估模式／情绪底色注入 |
| `[schedule] emotion_influence` | `0.6` | 情绪对日程的影响强度（`0` = 关闭） |

> 全部 13 个配置段、167 个配置键的**键名｜类型｜默认值｜说明**总表，以及旧键 → 新键的兼容映射，见 [USAGE.md](USAGE.md) 的「配置键总表」。

## FAQ

- **配了目标 QQ 还是没人私聊我？** 先确认 `target_qq` 已改成真实 QQ（占位值不解析）；插件需要能在宿主里查到该 QQ 的**私聊流**，查不到时每 60 秒重试一次——和目标 QQ 先互发一条消息通常就能解析到。详见 [USAGE.md](USAGE.md) 的 FAQ。
- **命令没反应？** 管理员指令要求 `admin.admin_users` 里有你（留空时只有本机控制台能用）；另外若配置了 `admin.allowed_streams`，不在名单内的会话会被静默拒绝。
- **日程图／状态图只有文字没有图？** 这是预期的静默降级：Pillow 或字体不可用、渲染超时（状态图 10 秒／日程图 15 秒）时改发纯文本，功能不受影响。
- **早安／晚安没来、或来了两条？** 看三类问候开关与「必要性激活」；例行问候默认免预算，所以「预算用完」不是原因。目标私聊同日同类型至多一条（热重载后也生效），群聊／非目标私聊跨热重载去重尚未实现。
- **QQ 空间发不出去／评论不回复？** 用 `/lbm_life qzone status` 看通道与 cookie：cookie 不可用时会降级成「有新评论」的提醒而不回评；`qzone.enabled=false`、静默时段、每日上限、`emotion_filter` 都会拦住发帖。（另：图片说说只有动作通道支持，CGI 回退通道的图片上传尚未接通。）
- **裁判类任务输出为空？** 建议把 `[judge] model` 与情绪裁判、好感裁判的任务绑到**真正的非思考型模型**——思考型模型会把小 `max_tokens` 预算花在推理上，导致正文为空。
- **怎么临时关掉某个模块？** `[qzone] enabled=false`、`[affection] enabled=false`、`[emotion] enabled=false` 分别关闭对应模块；`[plugin] enabled=false` 则只注册组件、不跑任何循环。
- **升级后配置和数据会怎样？** 旧键继续兼容读取（见 USAGE.md 的兼容映射表）；**数据目录随 `id` 变化，不会自动迁移**，需手动搬运，见下节。

## 已知问题

见 [CHANGELOG.md](CHANGELOG.md) 的「已知问题」节（统一登记，本文件不重复罗列）。

## 升级注意：数据目录变更

`_manifest.json` 的 `id` 在本版改为 `cateye.life_belonging_to_maimai`，因此数据目录变为：

```
data/plugins/cateye.life_belonging_to_maimai/
```

**旧目录（`data/plugins/github.cateye.cateye-suite/`）不会自动迁移。** 升级时请把旧目录下的 `schedule.db`、`emotion_state.json`、`emotion_config.json`、`affection` 档案、决策／LLM 日志等，手动复制到新目录再重启，历史日程与好感／情绪才能延续。

## 许可

MIT。

> 日程规划模块（`planning/`）是对一个 **AGPL-3.0** 自主规划插件的机制复刻（重写实现）；若后续大量照搬其源码，发布时整体应按 AGPL-3.0 处理。

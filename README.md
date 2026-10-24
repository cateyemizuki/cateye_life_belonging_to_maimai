# 属于麦麦的生活

> 一个 MaiBot 插件，让麦麦拥有自己的日程、关系、QQ 空间与情绪。当前版本 2.1.0。

## 这个插件是什么

「属于麦麦的生活」把四件互相关联的事交给麦麦自己完成：

- **日程规划**：每天生成整日作息与活动，并把「现在在做什么」自然地注入对话。
- **虚拟陪伴**：维护与陪伴对象的好感度，在合适的时间问候、想念与搭话。
- **QQ 空间**：按日程节奏自动发说说、配图，并回复评论。
- **情绪维护**：让情绪随聊天起伏，为回复提供底色与描述。

四个模块共享同一份作息；发往目标私聊的主动消息共用每日预算（早/晚安免预算），QQ 空间另有自己的每日上限。

## 安装

1. 把插件目录放进 MaiBot 的 `plugins/`（目录名建议 `cateye_life_belonging_to_maimai`）。
2. 重启 MaiBot，或在插件管理里重载插件。
3. 在 WebUI 的「陪伴模块」里填写 `target_qq`（`[patrol]`）。保持默认占位值 `123456789` 时不会主动私聊任何人，`/lbm_mai_*` 命令不可用；管理员命令仍由 `admin_users` 决定。
4. 如需从 QQ 使用管理员命令，在「管理与日志」（`[admin]`）里填上你的 QQ 号；留空时仅本机控制台可用。
5. 依赖 `httpx` 与 `Pillow`。Pillow 只用于渲染日程图与状态图，缺失或渲染失败时自动降级为纯文本。

可选开关默认关闭：屏幕感知 `[patrol] screen_enabled`、QQ 空间 `[qzone] enabled`、周结大事记 `[affection] weekly_milestones_enabled`、午休静默 `[patrol] nap_quiet_enabled`。

**前置条件**：QQ 空间需要 SnowLuma 适配器（NapCat 画像不支持 QZone API）；屏幕感知需要「cateye 统一连接插件」且对方电脑上的客户端在线。目标私聊流解析不到时，插件每 60 秒重试一次，期间不会有任何主动消息；先与目标 QQ 互发一条消息通常即可解析到。

WebUI 段名对照：插件（`[plugin]`）、陪伴模块（`[patrol]`）、好感度（`[affection]`）、日程系统（`[schedule]`）、日程系统·高级设置（`[schedule_advanced]`）、日程主动行为（`[planning_proactive]`）、QQ 空间（`[qzone]`）、情绪系统（`[emotion]`）、LLM 调用（`[llm]`）、提示词（高级）（`[prompts]`）、管理与日志（`[admin]`）。

## 快速上手

| 命令 | 用途 | 权限 |
|---|---|---|
| `/lbm_dashboard` | 四模块状态总览图与最近变动日志，情绪节附近期情绪变化 | 管理员 |
| `/lbm_plan list` | 查看今日日程图；当天没有日程时自动生成 | 管理员 |
| `/lbm_plan regenerate [额外要求]` | 重新生成今日日程，可附加临时要求 | 管理员 |
| `/lbm_mai_affection <1~N>` | 把好感度直接调到第 N 档 | 目标 QQ 本人或本机控制台 |
| `/lbm_diag llm` / `decision` / `life` | 模型调用、主动决策、空间决策日志 | 管理员 |
| `/lbm_diag streams [序号]` | 查看问候候选流、序号与最近消息；给出序号可查该流下一次触发状态 | 管理员 |
| `/lbm_life qzone status` | 查看 QQ 空间通道与 cookie 状态 | 不限 |
| `/lbm_life qzone post 文本` | 手动发一条说说 | 管理员 |
| `/lbm_emotion_init` | 由人设推导情绪基线 | 管理员 |
| `/lbm_emotion_config` | 查看情绪参数（`reset` 恢复默认，`clear` 清空当天记录） | 管理员 |
| `/lbm_mai_help` | 查看陪伴相关命令帮助 | 目标 QQ 本人或本机控制台 |

`/lbm_mai_*` 只对 `[patrol] target_qq` 本人（或本机控制台）开放；`/lbm_life qzone status|moments|comments` 不限权限。首次使用前请确认已配置管理员，否则 `/lbm_emotion_init` 会直接提示权限不足。

`/lbm_dashboard` 与 `/lbm_plan list` 的两张图已于 2026-10-23 按重绘指导重置：1080 固定列网格、先测量后绘制、先分页后钳制；效果与边界见 CHANGELOG [2.1.0]「渲染图重绘重置」与 USAGE「图片渲染口径」。

首次使用建议顺序：`/lbm_emotion_init` 初始化情绪；`/lbm_plan list` 确认日程；`/lbm_dashboard` 看整体状态。

## 四个模块

### 日程规划

- 每天自动生成整日日程；`[schedule] wake_time` / `sleep_time` 是生成基准（起床与入睡锚点）。
- 每份日程**进账即成为 bot 的作息周期**：上一份日程退为「上轮边界」，新的「本轮开始（≈起床）/本轮结束（≈入睡）」持久化生效；bot 实际改了日程后（例如赖床、整日重排），静默窗与晚安时点会跟随真实作息周期，而不是固定配置。
- `/lbm_plan list` 查看时刻表图，条目带日程属性标签；当天无日程时会先自动生成。
- `/lbm_plan regenerate [额外要求]` 重建今日日程；`delete` 删除指定目标；`clear` 清理旧日程。
- 模型生成失败时使用模板兜底，模板同样按你的作息锚定，不会把整日压成默认时段。
- 聊天频率随日程属性变化，倍率可在 `mappings/schedule_properties.json` 中调整。
- 节假日可写入日程块头部，也可让生成时考虑节假日（两个独立开关）。
- 在对话里提出日程变动时，bot 会判断接受、记为未来约定或拒绝。

### 虚拟陪伴

- 好感度 0~200，默认五档；档数、档名与区间由 `mappings/affection_presets.json` 定义。
- 早/晚安由作息周期驱动：早安在起床后的漂移点，晚安在入睡期限前的漂移点发出（期限 = 本轮日程的入睡时刻，跨 0 点照算真实时刻）。
- 问候始终免主动消息预算，也不受静默时段拦截。
- 问候闸门按三流分离（判定窗都是「上份日程开始 → 本份日程开始」这类周期窗，不看日历 0 点）：
  目标私聊**无条件**（只看陪伴模块开关，不再要求当日有消息）；群聊早安过**硬闸**（判定窗内 ≥1 条非 bot 消息，必要性开关开启的话再叠加「今天日程第一个条目时段内有人主动开口」）；其他私聊早安 = 判定窗内对方发送条数达门槛（默认 5），晚安 = 早安已发**连锁**或对方发送达晚安阈值（默认 5）二选一。
- 群聊早安本轮没开口，当晚群晚安**连锁硬性失效**；过了掷点才达标的补发还有**冷静期**——bot 本会话最近一条消息距今 ≥ `greeting_min_silence_minutes` 分钟才开口，一直不满足则到期仍弃。
- **早安沉默闸**（`greeting_morning_silence_hours`，默认 2 小时，0 = 关闭）：今日第一日程（周期起点）前 N 小时内 bot 自己在该会话发过消息 → 该会话本周期跳过早安（三流统一生效，含目标私聊；不掷点、不建条目）。
- 陪伴巡检与想念只发生在目标私聊，受每日预算、最小间隔、用户冷却与静默时段约束。
- 静默时段恒等于日程作息（入睡到起床），不需要单独配置。
- 可选：屏幕感知旁白、周结大事记、午休静默。

### 问候诊断（streams）

- `/lbm_diag streams` 的候选与序号按**三流分族**显示：「问候·群聊」「问候·其他私聊」「问候·目标私聊（无条件）」「频率调控」四块逐行输出，目标私聊流**回归诊断可见**（序号 = 群聊 → 其他私聊 → 目标私聊）。
- 检查窗 = 周期判定窗（上份日程开始 → 本份日程开始，非日历 0 点）；判定窗内查不到消息的群聊**不从候选中丢弃**，仍占一个 `#NN` 序号、行显「历史无消息」，行尾注明「硬闸窗内无消息·不发晨晚问候」。
- 带序号查询（`/lbm_diag streams <序号>`）与候选列表共用**同一序号系**；每流按早/晚各一行给结论（含「早安未开始（连锁待定）」「因早安未开口→晚安连锁硬性忽略」「连锁已通过」「数量闸达标 M/N」等新档），下一行给判定窗端、阈值、冷静期余量等关键量。
- 诊断全链**纯读**：不写任何问候状态（掷点、连锁标记、事件缓存），不会干扰问候链自身的判定。

### QQ 空间

- 开启 `[qzone] enabled` 后，插件按日程节奏自动发说说。
- 发帖判定链：每日上限、发帖意愿与情绪闸、是否需要当前有活动、概率、静默时段、无活动、午休静默、冷却、模型严判、掷骰；拿不准就不发。
- 配图有四种模式：`off`（不配图）、`emoji`（宿主表情包，出厂默认）、`library`（本地图库）、`random`（两者各半）。
- 任何一张图取不到都只跳过该图，全部取不到就发纯文本，配图失败不会阻断发帖。
- 自动回评；cookie 不可用时降级为「有新评论」提醒。点赞需手动 `/lbm_life qzone like`。
- 手动命令：`/lbm_life qzone post|comments|reply|like`；`decisions` 查看最近为什么没发。

### 情绪维护

- 情绪以愉悦度、唤醒度、支配感三个维度持续起伏，聊天中的大事件会带来冲击。
- 情绪为回复提供底色与文字描述，也会轻微影响主动频率与发帖意愿。
- 夜间进入睡眠窗后冻结，醒来时统一结算。
- `/lbm_emotion_init` 由人设推导基线；`/lbm_emotion_config` 查看、`reset` 或 `clear`。
- 情绪映射与描述可在 `mappings/` 的对应表中调整。
- 睡眠裁判提示词已压缩 reason 与思考要求；调用 `max_tokens` 为 1024（如需进一步压思考链，见 USAGE.md「睡眠裁判思考强度配置指引」）。

### bot 自用工具

以下工具由麦麦在对话中自行调用，无需手动输入：`lbm_get_schedule_info`、`lbm_update_schedule_v4`、`lbm_nap_lie_in`、`lbm_mai_lover_send_message`、`lbm_mai_lover_affection`、`lbm_report_harassment`、`lbm_life_qzone`、`lbm_emotion_report`。

## 配置速查

常改的键如下，未列出的键保持默认即可：

| 配置键 | 默认 | 说明 |
|---|---|---|
| `[patrol] target_qq` | `123456789` | 陪伴对象 QQ；占位值 = 不主动私聊、`/lbm_mai_*` 不可用 |
| `[admin] admin_users` | `[]` | 管理员 QQ；留空 = 仅本机控制台 |
| `[schedule] wake_time` / `sleep_time` | `07:00` / `23:00` | 起床、入睡锚点：日程生成的基准；无真实日程时也作作息周期的兜底边界 |
| `[patrol] relation_type` | `恋人` | bot 与对方的关系，影响口吻与档位名称 |
| `[patrol] nap_quiet_enabled` | `false` | 午休静默：巡检与空间发帖、评论监听跳过午休时段 |
| `[planning_proactive] greeting_morning_enabled` / `greeting_night_enabled` | `true` / `true` | 早安、晚安总开关 |
| `[planning_proactive] morning_drift` / `night_drift` | `10-30` | 问候漂移区间（分钟）：早安 = 本轮起床钟点 + 漂移；晚安 = 本轮入睡期限 − 漂移 |
| `[planning_proactive] greeting_morning_silence_hours` | `2.0` | 早安沉默闸（小时）：今日第一日程（周期起点）前 N 小时内 bot 自己在该会话发过消息 → 该会话本周期跳过早安；0 = 关闭 |
| `[planning_proactive] greeting_require_activation` | `true` | 群聊早安必要性检查：今天日程第一个条目时段内有人主动开口（硬闸之上仍然 AND）；关闭 = 只看硬闸 |
| `[planning_proactive] proactive_group_ids` / `proactive_private_ids` | `[]` / `[]` | 群聊、私聊名单 |
| `[planning_proactive] greeting_other_private_min_messages` | `5` | 其他私聊早安门槛：判定窗（上份日程开始 → 本份日程开始）内对方发送的条数达到该值；最低 1 |
| `[planning_proactive] greeting_private_night_min_messages` | `5` | 其他私聊晚安阈值：本轮日程开始 → 晚安期限内对方发送条数达到该值即通过（与早安已发连锁二选一）；最低 1 |
| `[schedule_advanced] greeting_min_silence_minutes` | `3` | 补发冷静期（分钟）：过掷点才达标的早/晚安补发，须 bot 本会话最近一条消息距今 ≥N 分钟才开口；0 = 不要求 |
| `[qzone] enabled` | `false` | QQ 空间模块总开关 |
| `[qzone] image_mode` | `emoji` | 配图来源：`off` / `emoji` / `library` / `random` |
| `[patrol] daily_max_speak` | `5` | 每日主动预算上限，只统计巡检与想念 |

## 自定义映射表

插件包的 `mappings/` 目录分两层（2026-10-23 §29.2 起）：

| 位置 | 角色 |
|---|---|
| `mappings/`（表层） | **用户数据区**：五张映射表放这里、按需编辑；表层文件**不随插件包分发，更新插件也不会覆盖**。首次使用若表层缺表，插件自动从深层复制出厂默认补齐 |
| `mappings/default_mappings/`（深层） | **随包种子源**：出厂说明文档与五张默认表随包分发、随插件版本更新；是否把新版出厂内容合并进你的表层，由你自行决定 |

五张映射表（表层位置、文件名不变）：

| 文件 | 用途 |
|---|---|
| `emotion_mapping.json` | 情绪原型与映射数据 |
| `emotion_descriptions.json` | 情绪的文字描述 |
| `affection_presets.json` | 好感度档数、档名与区间（1~5 档） |
| `affection_tier_descriptions.json` | 各档位的注入文案 |
| `schedule_properties.json` | 日程属性、聊天频率倍率与标签色 |

这些表只在插件加载或热重载时读取，修改后需要重启或重载才生效。结构、校验与错误回退规则见 `mappings/default_mappings/README.md`。

## 数据目录

```
data/plugins/cateye.life_belonging_to_maimai/
```

这里保存日程库、情绪状态、好感档案、空间状态与日志，插件只在宿主给定的数据目录内读写。

- `schedule.db`、`emotion_state.json`、`affection_memory.json`、`qzone_state.json` 等可安全删除，删除后相当于重新开始。
- `qzone_cookies.json` 删除后插件会先尝试重新向适配器获取 cookie；只有适配器也取不到时，才需要检查 QQ 登录或适配器状态。

## 常见问题

- **日程变成了默认时段或明显被压扁？** 用 `/lbm_plan regenerate` 重新生成今日日程即可。
- **早安或晚安没来？** 先用 `/lbm_diag streams` 查看候选流与序号，再用 `/lbm_diag streams <序号>` 查看该流下一次早安、晚安是否满足触发条件。群聊早安需要判定窗（上份日程开始 → 本份日程开始）内有人说话（必要性开关开启时今天日程第一个条目时段内也要有人开口），且早安没开口的当晚群晚安会被连锁拦下；其他私聊早安需要对方发送条数达门槛、晚安达标即可（连锁或阈值二选一）；过点才达标的补发还受冷静期约束（默认最近 3 分钟 bot 没说过话）。目标私聊无条件，但在 streams 里「无条件」族可见；没收到时用 `/lbm_dashboard` 或决策日志排查。streams 的候选解析与名单状态均为逐行显示：每个候选流独占一行（行首带序号）、名单与频率调控逐项列出，不再以列表形态挤在单行，无折叠、无省略。
- **QQ 空间没发出去或没回评？** 用 `/lbm_life qzone status` 检查通道与 cookie，用 `/lbm_life qzone decisions` 查看最近卡在哪一道闸。
- **命令没反应？** 管理员命令要求 `[admin] admin_users` 中有你；`/lbm_mai_*` 只对 `[patrol] target_qq` 本人开放。`[admin] allowed_streams` 只约束 `/lbm_plan` 与日程注入，不影响其他命令。
- **只收到文字没有图？** 这是预期的降级：Pillow 或字体不可用、渲染超时时自动改发文字，功能不受影响。
- **想临时关闭某个模块？** 关闭 `[schedule] enabled`、`[patrol] enabled`、`[qzone] enabled`、`[affection] enabled` 或 `[emotion] enabled` 可分别停用对应模块；关闭 `[plugin] enabled` 会让全部模块停摆（含命令与工具）。

## 技术信息

- 组件 29 个：8 个命令、10 个 Hook、7 个工具、4 个跨插件 API。
- 声明 17 项宿主能力。
- 配置 11 个段、201 个字段（3 个隐藏、198 个可见）。
- 用户可编辑映射表 5 张，位于 `mappings/`。

## 许可

插件根目录以 MIT 许可发布（见 `LICENSE`）。

例外：`planning/`（日程规划）是对自主规划插件 v4.9.1 的复刻与移植（保留原架构，部分文件直接移植），按 **AGPL-3.0** 单独授权。插件以源码形式分发，因此分发或修改 `planning/` 目录时需遵守 AGPL-3.0 的义务（提供对应源码、保留许可声明等）。AGPL-3.0 全文见 [`planning/LICENSE`](planning/LICENSE)，出处与范围说明见 [`planning/README.md`](planning/README.md)。

## 参考与致谢

本插件站在以下项目与生态的肩膀上：

1. **麦麦自主规划插件 v4（v4.9.1）**：作者靓仔（xuqian13），[github.com/xuqian13/autonomous_planning_plugin](https://github.com/xuqian13/autonomous_planning_plugin)；本插件使用 cateye 维护的 fork：[github.com/cateyemizuki/autonomous_planning_plugin](https://github.com/cateyemizuki/autonomous_planning_plugin)；许可 **AGPL-3.0**。用途：`planning/` 模块（日程生成管线、目标管理、注入、工具、命令、自动调度、日程图、事件服务等）源自其架构的复刻与移植；问候链的闸序也参照其设计。
2. **Maizone（麦麦空间）**：作者 internetsb，[github.com/internetsb/Maizone](https://github.com/internetsb/Maizone)，许可 **MIT**（插件市场名「麦麦空间 Maizone」v3.0.3 / qzone-toolkit）。用途：QQ 空间 Web CGI 直连上传、发图与表情包模式的实现路线参考（`qzone/cgi.py`、`image_sources.py`）。
3. **MaiTrace**：作者 Rabbit-Jia-Er，v3.1，[github.com/Rabbit-Jia-Er/MaiTrace](https://github.com/Rabbit-Jia-Er/MaiTrace)，许可 **MIT**。用途：QQ 空间发帖判定链（模型严判正反例、首字符裁定、决策历史）与图库、表情包收集思路的移植参考。
4. **SnowLuma 适配器协议层 与 qzone-toolkit**：[github.com/Mai-with-u/MaiBot-SnowLuma-Adapter](https://github.com/Mai-with-u/MaiBot-SnowLuma-Adapter)；qzone-toolkit 随 Maizone 分发（见第 2 项）。用途：空间动作与 Web 接口字段参考（发帖双通道的协议一致性问题即来源于此）。
5. **MaiBot 宿主与插件开发文档 v1.x**：[github.com/Mai-with-u/MaiBot](https://github.com/Mai-with-u/MaiBot)，生态基础。

此外，**`companion/` 模块由虚拟陪伴插件 mai-love 改造而来**——作者 octmicy，[github.com/octmicy/Mai_love](https://github.com/octmicy/Mai_love)，许可 **MIT**；`core/` 的部分实现（如 llm_utils）同样参考/移植自它。对照用 fork：[github.com/cateyemizuki/Mai_love](https://github.com/cateyemizuki/Mai_love)。日程图与状态图使用 Noto Sans SC 字体（[github.com/googlefonts/noto-cjk](https://github.com/googlefonts/noto-cjk)），按 SIL Open Font License 1.1 分发（随包 `planning/assets/fonts/OFL.txt`）。

上述第 2、3 项为 MIT 许可的直接参考实现；第 1 项为 AGPL-3.0 复刻，另见上文许可说明。本插件对这些项目的理解与实现若有不准之处，一切以原项目为准。

## 更新历程

更新历程见 [CHANGELOG.md](CHANGELOG.md)。

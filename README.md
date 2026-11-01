# 属于麦麦的生活

> 一个 MaiBot 插件，让麦麦拥有自己的日程、关系、QQ 空间与情绪。当前开发版 **3.0.0**（未发布；最近发布版 2.1.0）。

2026-11-01 本地增量（未部署）：桌宠与详情入口统一为右上探头姿态，弹出落地+捏捏接续+远离自动收起；详情恢复悬停预览/点击固定，以入口为原点的粘连气泡+轻模糊背景。日程新增「刷视频」小事件（依赖独立观看插件 `cateye.bili-watch`，无候选/能力缺失如实跳过；观看完成不自动发消息）。部署状态及生产验证以并列开发文档中的最新交接为准。2026-10-30 验收补修：睡眠锚点由睡眠引擎管理，不能作为唤醒自身的硬约定；已醒状态不会因夜窗再次冻结。全屏详情在焦点意外移到背景后仍能通过 Tab 回到弹窗。

## 这个插件是什么

「属于麦麦的生活」把四件互相关联的事交给麦麦自己完成：

- **日程规划**：每天生成一组「日意图（spine）」并由滚动层执行成真实活动，把「现在在做什么」自然地注入对话。
- **虚拟陪伴**：维护与陪伴对象的好感度，在合适的时间问候、想念与搭话。
- **QQ 空间**：按日程节奏自动发说说、配图，并回复评论。
- **情绪维护**：让情绪随聊天起伏，为回复提供底色与描述。

四个模块共享同一份真实的作息与当前活动；发往目标私聊的主动消息共用每日预算（早/晚安免预算），QQ 空间另有自己的每日上限。

## 安装

1. 把插件目录放进 MaiBot 的 `plugins/`（目录名建议 `cateye_life_belonging_to_maimai`）。
2. 重启 MaiBot，或在插件管理里重载插件。
3. 在 WebUI 的「陪伴模块」里填写 `target_qq`（`[patrol]`）。保持默认占位值 `123456789` 时不会主动私聊任何人，`/lbm_mai_*` 命令不可用；管理员命令仍由 `admin_users` 决定。
4. 如需从 QQ 使用管理员命令，在「管理与日志」（`[admin]`）里填上你的 QQ 号；留空时仅本机控制台可用。
5. 依赖 `httpx` 与 `Pillow`。Pillow 只用于渲染日程图与状态图，缺失或渲染失败时自动降级为纯文本。

可选开关默认关闭：屏幕感知 `[patrol] screen_enabled`、QQ 空间 `[qzone] enabled`、周结大事记 `[affection] weekly_milestones_enabled`、午休静默 `[patrol] nap_quiet_enabled`、情绪-生活双向调制 `[life_state] emotion_modulation_enabled`、事件库生成 `[event_catalog] mode=off`。默认打开的新功能只有两项：日程主线 `[schedule] enabled=true`（见下节）与目标频率恒 1 `[planning_proactive] target_frequency_always_one=true`。

屏幕感知默认只看主屏（`[patrol] screen_scope=main`，与旧版行为一致）；TA 常在副屏上干活时可改 `all`——对端（cateye 统一连接插件 v1.1.0+）多屏时逐屏抓取，全部截图一次交给视觉模型分屏概述；对端过旧、单屏客户端或负载超限自动降级时，`all` 退化为只看主屏，与 `main` 等价。

**前置条件**：QQ 空间需要 SnowLuma 适配器（NapCat 画像不支持 QZone API）；屏幕感知需要「cateye 统一连接插件」且对方电脑上的客户端在线。目标私聊流解析不到时，插件每 60 秒重试一次，期间不会有任何主动消息；先与目标 QQ 互发一条消息通常即可解析到。

WebUI 段名对照：插件（`[plugin]`）、陪伴模块（`[patrol]`）、好感度（`[affection]`）、日程系统（`[schedule]`）、日程系统·高级设置（`[schedule_advanced]`）、日程主动行为（`[planning_proactive]`）、QQ 空间（`[qzone]`）、情绪系统（`[emotion]`）、LLM 调用（`[llm]`）、提示词（高级）（`[prompts]`）、管理与日志（`[admin]`）、生活状态（`[life_state]`）、事件库（`[event_catalog]`）、麦麦互动（`[maimai_interaction]`，聚合段最后一段；WebUI 显示名暂为「？？？」占位，正式名未定稿）。

## 工具主动私聊

`lbm_mai_lover_send_message` 始终发给配置的固定关系对象，不会按当前群友昵称改变收件人。群聊冷场本身不足以成为转去私聊的理由；正文须让收件人独立看懂，不把其他群友的兴趣、关系设定或作息当作该对象的事实。

Planner 直接提供 `message` 时不调用文案生成，因此约束放在工具说明/参数说明中；`[prompts].tool_send_message` 与 `tool_send_message_system` 只影响留空自动生成。自定义模板保持优先。相关提示已加强并随本轮部署；它们属于提示约束，模型实际遵从仍需结合使用记录观察。

## 生活面板详情

页面左下角有一只**贴角冒头**的常驻麦麦（只露头顶和眼睛，默认 128px）：鼠标指向露出的它，它会先向上弹起、再落到左下角底部站稳（落地后身体水平）；落稳后按住会渐渐压扁并**保持压扁**，松开才从当前形态连续回弹；指针远离左下角约半秒后自动收起、退回贴角探头姿态。触屏设备点按探头即弹出、点空白收起；键盘 Tab 聚焦后 Enter/Space 同样可弹出或触发回弹。详情打开期间它保持可见（只是暂停捏捏），关闭后一切照旧。

每个数据板块左下角有一个详情入口小麦麦（同样贴角冒头，默认收起）。**鼠标悬停即可临时预览**详情（不抢焦点、不锁滚动），移入面板保持阅读，移开片刻自动收起；**点击固定**（锁定滚动、焦点管理生效），再点同一入口取消固定；点击另一入口直接切换。详情面板以入口为原点展开/收回，一条气泡尾把面板和入口连在一起；入口始终在原位清晰可见。按 Esc（说明 tooltip 打开时先关说明）、点击面板外背景或右上角关闭按钮关闭。背景为轻微模糊+低强度遮罩，背后内容仍可辨认。打开期间 30 秒只读刷新照常进行，不会重置滚动位置；历史曲线、日期选择、快照导入仍可使用。宿主内采用viewport布局，页面内部滚动，避免手机底栏被宿主导航遮住。

「日程」栏的时间线改为纵向列表：计划（虚线轴点）与实际（实线轴点）同列展示、徽标区分，中断标红虚线、跨午夜条目续到次日、进行中的活动以当前时刻收尾；「最近变化」折叠组列在板块详情末尾，展示最近真实发生的状态与活动变化（估算采样点不入列）。日程在空闲时段可能选中「刷视频」小事件（可中断的娱乐活动）：若安装了独立观看插件 `cateye.bili-watch` 并配置了待看列表，会真实观看一个视频并把结果记入活动；无候选或能力不可用时该次事件如实跳过。互动与详情的展示参数（常驻麦麦尺寸/探头角度/收起距离、背景模糊半径、最近变化条数等）由配置段 `[maimai_interaction]` 控制，默认值即可使用。

## 模型输出与 Token 统计

插件不再设置模型输出长度或每日 Token 限制；输出长度完全使用宿主任务配置。旧限制键自动忽略，其余任务路由、温度、超时、主动消息每日次数和事件池规则保留。

调用次数统计收到宿主响应的请求，包含失败；Token只累计宿主明确回传的用量，缺失不推算。四类调用为正常回传、成功但无用量、失败调用、旧记录；“缺用量来源调用”是独立提示，可与失败重叠。WebUI提供说明按钮，PNG展示相同口径。

## WebUI 生活面板

在宿主侧边栏打开「属于麦麦的生活 → 属于麦麦的生活」。页面通过插件自己的 `webui.json`（v2）与沙箱查询读取实时数据，自动跟随宿主主题，不需要替换宿主静态文件。

- 当前活动只显示真正 active 的运行记录；日意图、实际足迹和中断分别展示，不把候选活动当成正在发生。
- 五项生活状态、睡眠债与精神状态可视化；饥饿和压力保留原值并明确方向。可选 6/24/72/168 小时历史、切换曲线、查看采样值。
- 睡眠会话展示精力恢复、睡眠债结算与真实醒来原因；掉线重连分级补眠会显示“离线补觉醒来”，不补发入睡事件。陪伴摘要展示关系分、基线、巡检预算和早晚安记录。
- 每 30 秒只读刷新，隐藏页面时暂停，失败保留上次快照并退避。页面不触发模型调用、主动消息或业务写入。
- 生活历史单独写入状态文件旁的 `<状态文件名>_history.json`（默认 `life_state_history.json`）：正常 tick 至多每 5 分钟采样，另记录活动和醒沿；最多 7 天 / 2304 点。重启续接，长缺口断线；持久化历史只记真实采样，图表可按 API 的 `estimated` 标记显示小缺口线性估算与离线睡眠估算段，估算不写回历史。决策日志同步记录 `life.state_changed`，受原日志开关与保留天数约束。
- 开发版页面现分「此刻 / 历史 / 日程 / 心情与分享 / 系统」五栏。历史、日程与心情栏支持今天/昨天/日期查询；此刻始终显示当前运行态。估算虚线、离线睡眠斜纹与真实观察分开；计划与实际活动双轨对照，缺失与清理状态如实展示。
- 9 张已验收 WebP 与原版官方鱼 SVG 自包含嵌入，按宿主主题显示；支持降低动态效果。手机底部栏目、放大/全览按钮与双指缩放。
- 查询 8 秒超时后保留旧数据，旧请求与旧导入晚返回不能覆盖新结果；独立时钟在 45 秒提示可能过期、120 秒标记陈旧，不再只凭桥连接宣称实时。
- 2026-10-30 18:03 已部署生产开发版，完成桌面/390/320、明暗、详情、按日数据与 Token 显示复测；自然夜及真实多屏 provider 另行验证。页面源码位于 `webui/source/`；执行 `python tools/build_webui.py` 可重建 `webui/index.html`，无需临时素材目录或网络依赖。
- 「导出快照」提供可全选复制的 JSON，保存为 `.json` 文件后，可在独立打开的 `webui/index.html` 中导入查看；兼容宿主不允许文件下载的沙箱。快照不含聊天、提示词、账号标识和凭据，但包含生活与关系信息，请按个人数据保管。没有真实数据时显示空态。

需要宿主支持 WebUI v2 `custom_pages`。只修改 HTML 可使用宿主「重新加载文档」；修改声明或 Python API 必须重载插件。

2026-10-28 增量修复了“醒后热重载又显示睡眠中”的持久化问题。已结束会话落盘保持清醒字段；检测到旧版遗留的重复恢复时，只按既有醒沿修正关闭事实，不重算精力或睡眠债。

## 3.0.0 破坏性变更（升级必读）

3.0.0 把「日程滚动化 + 生活状态 + 睡眠引擎」收成**唯一主线**，旧的两开关式灰度与旧全天日表路线已整体退役：

- **唯一总开关 `[schedule] enabled`（默认 `true`）**：开启时同时装配 spine 日意图、滚动运行时、生活状态与睡眠引擎；关闭时**仅日程链**停止（不生成、不注入、日程主动链不开口，工具/命令回「日程未启用」），**不会回落到旧全天生成路线**；陪伴、QQ 空间、情绪等独立模块仍按各自开关运行。
- **不再存在的旧入口**：旧 daily 生成/校验/质量评分/响应解析链、`lbm_update_schedule_v4` 工具别名、`lbm_nap_lie_in` 赖床工具、`/lbm_emotion_init` 独立命令（统一并入 `/lbm_init emotion`）、`lbm_get_schedule_info` 的 `lover_day` / `goals` 旧视图、按活动切换强制主动开口的旧链。
- **仍然独立、按需开启**：`[life_state] emotion_modulation_enabled`（情绪-生活双向调制）、`[event_catalog] mode`（事件库生成）、`[schedule] no_sleep_mode`（无睡眠模式）、`[patrol] nap_quiet_enabled`（午休静默）、夜间打扰统计、睡眠大修、各类上限都保留。
- **数据兼容**：`life_state.json`、日程库 `schedule.db` 与用户数据**不清空、不批量改写已执行历史**；当前/未来仅有旧非 spine 日表的日期，可把旧行归档后重建 spine 日意图（非破坏性，旧记录仍可追溯）。旧日表行不再被当作当前活动来源。
- **回滚**：3.0.0 是破坏性清理，没有「改回旧开关」的回退路线；需要旧行为请回退插件版本与配置备份。

## 快速上手

| 命令 | 用途 | 权限 |
|---|---|---|
| `/lbm_dashboard` | 四模块状态总览图与最近变动日志，情绪节附近期情绪变化 | 管理员 |
| `/lbm_plan list` | 查看今日日程图；当天没有日程时自动生成 | 管理员 |
| `/lbm_plan regenerate [额外要求]` | 重新生成今日日意图，可附加临时要求 | 管理员 |
| `/lbm_init [emotion\|events\|status]` | 统一初始化：全量 / 情绪 / 事件库 / 只读状态 | 管理员 |
| `/lbm_mai_affection <1~N>` | 把好感度直接调到第 N 档 | 目标 QQ 本人或本机控制台 |
| `/lbm_diag llm` / `decision` / `life` | 模型调用、主动决策、空间决策日志 | 管理员 |
| `/lbm_diag streams [序号]` | 查看问候候选流、序号与最近消息；给出序号可查该流下一次触发状态 | 管理员 |
| `/lbm_life qzone status` | 查看 QQ 空间通道与 cookie 状态 | 不限 |
| `/lbm_life rolling [status\|state\|episodes\|spine\|sleep\|preview]` | 滚动/睡眠调试（纯只读，六个子命令；日程未启用时提示未启用） | 管理员 |
| `/lbm_life qzone post 文本` | 手动发一条说说 | 管理员 |
| `/lbm_emotion_config` | 查看情绪参数（`reset` 恢复默认，`clear` 清空当天记录） | 管理员 |
| `/lbm_mai_help` | 查看陪伴相关命令帮助 | 目标 QQ 本人或本机控制台 |

`/lbm_mai_*` 只对 `[patrol] target_qq` 本人（或本机控制台）开放；`/lbm_life qzone status|moments|comments` 不限权限。首次使用前请确认已配置管理员，否则 `/lbm_init` 会直接提示权限不足。

`/lbm_dashboard` 与 `/lbm_plan list` 的两张图已于 2026-10-23 按重绘指导重置：1080 固定列网格、先测量后绘制、先分页后钳制；效果与边界见 CHANGELOG [2.1.0]「渲染图重绘重置」与 USAGE「图片渲染口径」。

首次使用建议顺序：`/lbm_init emotion` 初始化情绪；`/lbm_plan list` 确认日程；`/lbm_dashboard` 看整体状态。

## 四个模块

### 日程规划

- 每天由模型只生成 3~6 条「今天想做什么」的稀疏意图（spine）+ 硬约定 + 作息锚点，其余时段交给滚动层按本地规则推进，不再一次排满全天。
- `[schedule] wake_time` / `sleep_time` 仍是起床与入睡锚点；`no_sleep_mode=true` 时睡眠窗内不生成任何活动。
- 每份日程**进账即成为 bot 的作息周期**：上一份退为「上轮边界」，新的「本轮开始（≈起床）/本轮结束（≈入睡）」持久化生效；真实睡眠会话结束时会用真实醒来时刻校正周期。
- 当前活动只有一个真源：滚动运行时的真实 episode。空闲时如实「没有安排中的活动」，不按时间窗或旧日表编造。
- `/lbm_plan list` 查看时刻表图，条目带日程属性标签；当天无日程时会先自动生成。
- `/lbm_plan regenerate [额外要求]` 重建今日日意图；`delete` 删除指定目标；`clear` 清理旧日程。
- 模型生成失败时使用 spine 模板兜底，模板同样按你的作息锚定。
- 聊天频率随日程属性变化，倍率可在 `mappings/schedule_properties.json` 中调整。
- 节假日可写入日程块头部，也可让生成时考虑节假日（两个独立开关）。
- 在对话里提出日程变动时，bot 会判断接受、记为未来约定或拒绝；固定时间的硬约定**不因晚起顺延，迟到如实记录**。

### 生活状态 / 睡眠（随日程总开关）

- **滚动执行与生活状态**：精力/饥饿/专注/压力/社交电量五项连续状态（纯本地规则推进，零模型调用），按 5 分钟节奏在日意图框架内「走一步看一步」地选活动；活动进度随实际进行时长增长（上限 0.95），空闲时段如实显示无活动；被打断的活动进恢复池，可能恢复也可能不了了之。
- **情绪-生活双向联动（可选，`[life_state] emotion_modulation_enabled`，默认关）**：情绪（愉悦度/唤醒度/支配感）按平滑读数轻微加快或减慢五项生活状态的变化速率；活动完成或被打断也会小幅影响情绪。映射表固定 `mappings/emotion_life_modulation.json`，不提供路径配置键。
- **睡眠引擎（随日程总开关，无独立开关键）**：到最早起床时间后按精力阈值（默认 0.75）决定是否赖床，最晚 `[schedule_advanced] lie_in_max_until`（默认 12:00）必起；硬约定**至少提前 30 分钟**醒，**固定时间硬约定不因晚起顺延、迟到如实记录**；**正式起床结束当前睡眠会话、记为完成、不进恢复池**；半夜短扰只统计，**确认清醒 ≥5 分钟才重置连续睡眠段、会话仍保留**。睡眠中热关闭日程，会以关闭真实时刻结束会话并交回停止状态；**重新打开不会复活旧会话**。
- **午休静默（可选，`[patrol] nap_quiet_enabled`，默认关）**：只读取**新 spine 的今日意图**判断是否午休，仅用于静默，不伪造当前活动；旧日表不参与。
- **活动日志**：实际发生的事记入日程库（计划、进行中、完成、被打断、暂停、放弃、跳过），「我在做什么」只从真实发生读。
- **调试命令**：管理员可用 `/lbm_life rolling` 命令族（status / state / episodes / spine / sleep / preview 六个子命令）纯只读地查看滚动层与睡眠引擎状态；日程未启用时提示「日程未启用」。

### 事件库（可选，默认 off）

- `[event_catalog] mode` = `off`（只用内置池）/`manual`（仅手动刷新）/`auto`（到期后台生成）。
- 单次生成上限 `max_generated_events=8`、合并总池上限 `max_pool_events=12`；内置池至少 3 条兜底、生成项 `llm_` 前缀、全量校验通过才替换。
- 事件文件写坏/缺失/版本不认识时回退内置池并保留 `.bak`，不会覆盖你的用户文件。

### 统一初始化

`/lbm_init`（全量：情绪 + 事件 + 状态/睡眠 + 频率）、`/lbm_init emotion`（强制情绪）、`/lbm_init events`（强制刷新事件库）、`/lbm_init status`（只读）。阶段互相独立，失败不拖垮其他阶段；汇总写数据目录 `life_init.json`。旧 `/lbm_emotion_init` 命令已在 3.0.0 删除，请改用 `/lbm_init emotion`。

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
- 带序号查询（`/lbm_diag streams <序号>`）与候选列表共用**同一序号系**；每流按早/晚各一行给结论（含「早安未开始（连锁待定）」「因早安未开口→晚安连锁硬性忽略」「连锁已通过」「数量闸达标 M/N」等档），下一行给判定窗端、阈值、冷静期余量等关键量。
- 诊断全链**纯读**：不写任何问候状态（掷点、连锁标记、事件缓存），不会干扰问候链自身的判定。

### QQ 空间

- 开启 `[qzone] enabled` 后，插件按日程节奏自动发说说。
- 发帖判定链：每日上限、发帖意愿与情绪闸、是否需要当前有活动、概率、静默时段、无活动、午休静默、冷却、模型严判、掷骰；拿不准就不发。当前活动与「是否有活动」读运行时真源，日程关断时如实按无活动处理。
- 配图有四种模式：`off`（不配图）、`emoji`（宿主表情包，出厂默认）、`library`（本地图库）、`random`（两者各半）。
- 任何一张图取不到都只跳过该图，全部取不到就发纯文本，配图失败不会阻断发帖。
- 自动回评；cookie 不可用时降级为「有新评论」提醒。点赞需手动 `/lbm_life qzone like`。
- 手动命令：`/lbm_life qzone post|comments|reply|like`；`decisions` 查看最近为什么没发。

### 情绪维护

- 情绪以愉悦度、唤醒度、支配感三个维度持续起伏，聊天中的大事件会带来冲击。
- 情绪为回复提供底色与文字描述，也会轻微影响主动频率与发帖意愿。
- 夜间进入睡眠窗后冻结，醒来时统一结算；有真实睡眠会话时以会话为准，无会话时按 tick 级空窗冻结入口兜底。
- `/lbm_init emotion` 由人设推导基线；`/lbm_emotion_config` 查看、`reset` 或 `clear`。
- 情绪映射与描述可在 `mappings/` 的对应表中调整。
- 情绪→生活反馈的每日回写预算在数据目录 `life_feedback_budget.json`：按**最终实际入账增量**检查并记账、保留逐轴净累计、单次上限 0.15；升级当天的旧 raw 记录无法可靠换算 actual 时**当日暂停新增生活反馈**（旧账原样保留、不重置额度），次日自动进入新口径。

### bot 自用工具

以下工具由麦麦在对话中自行调用，无需手动输入：`lbm_get_schedule_info`（只读，支持 status/current/next/episodes/spine/intents 视图）、`lbm_update_schedule`（唯一日程写入口，写 spine 意图与未来约定）、`lbm_mai_lover_send_message`、`lbm_mai_lover_affection`、`lbm_report_harassment`、`lbm_life_qzone`、`lbm_emotion_report`、`lbm_watch_bili_video`（看B站视频：submit 提交 BV/链接，status/result 查询进度与真实结果，cancel 取消；后台有界执行，画面采样视本机 ffmpeg 与 vlm 任务可用性如实降级，观看完成绝不自动发消息）。3.0.0 已删除旧别名 `lbm_update_schedule_v4`、赖床工具 `lbm_nap_lie_in` 与 `lover_day` / `goals` 视图。

## 配置速查

常改的键如下，未列出的键保持默认即可：

| 配置键 | 默认 | 说明 |
|---|---|---|
| `[schedule] enabled` | `true` | **日程唯一总开关**：开 = spine + 滚动运行时 + 生活状态 + 睡眠引擎；关 = 全部停止、无旧路线回落 |
| `[schedule] wake_time` / `sleep_time` | `07:00` / `23:00` | 起床、入睡锚点：spine 生成与作息周期的基准 |
| `[schedule] no_sleep_mode` | `false` | 无睡眠模式：睡眠窗内不生成任何活动、不主动开口 |
| `[patrol] target_qq` | `123456789` | 陪伴对象 QQ；占位值 = 不主动私聊、`/lbm_mai_*` 不可用 |
| `[admin] admin_users` | `[]` | 管理员 QQ；留空 = 仅本机控制台 |
| `[patrol] relation_type` | `恋人` | bot 与对方的关系，影响口吻与档位名称 |
| `[patrol] nap_quiet_enabled` | `false` | 午休静默：巡检与空间发帖跳过午休时段（只读 spine 意图） |
| `[planning_proactive] greeting_morning_enabled` / `greeting_night_enabled` | `true` / `true` | 早安、晚安总开关 |
| `[planning_proactive] morning_drift` / `night_drift` | `10-30` | 问候漂移区间（分钟） |
| `[planning_proactive] greeting_morning_silence_hours` | `2.0` | 早安沉默闸（小时）；0 = 关闭 |
| `[planning_proactive] greeting_require_activation` | `true` | 群聊早安必要性检查 |
| `[planning_proactive] proactive_group_ids` / `proactive_private_ids` | `[]` / `[]` | 群聊、私聊名单 |
| `[planning_proactive] greeting_other_private_min_messages` | `5` | 其他私聊早安门槛 |
| `[planning_proactive] greeting_private_night_min_messages` | `5` | 其他私聊晚安阈值 |
| `[planning_proactive] target_frequency_always_one` | `true` | 目标陪伴私聊回复频率恒 1.0（关 = 回属性倍率） |
| `[schedule_advanced] greeting_min_silence_minutes` | `3` | 补发冷静期（分钟）；0 = 不要求 |
| `[schedule_advanced] lie_in_max_until` | `12:00` | 赖床硬上限时刻 |
| `[qzone] enabled` | `false` | QQ 空间模块总开关 |
| `[qzone] image_mode` | `emoji` | 配图来源：`off` / `emoji` / `library` / `random` |
| `[patrol] daily_max_speak` | `5` | 每日主动预算上限，只统计巡检与想念 |
| `[life_state] emotion_modulation_enabled` | `false` | 情绪-生活双向调制（独立于日程总开关） |
| `[event_catalog] mode` | `off` | 事件库：`off` / `manual` / `auto` |
| `[video_watch] enabled` | `true` | 内置视频观看模块总开关（关 = 工具如实返回未启用、日程刷视频跳过） |
| `[video_watch] watchlist` | `[]` | 待看列表：BV 号或视频链接每行一个；日程刷视频事件从这里选片 |
| `[video_watch] visual_enabled` | `true` | 画面采样（本机无 ffmpeg 或 vlm 任务不可用时如实降级为文本来源） |

`spine_mode`、`[life_state] enabled`、`[life_state] sleep_engine_enabled` 等旧键已退役；旧配置中的显式值在升级时被一次性 pop，不再出现在 WebUI。

## 自定义映射表

插件包的 `mappings/` 目录分两层（2026-10-23 §29.2 起）：

| 位置 | 角色 |
|---|---|
| `mappings/`（表层） | **用户数据区**：七张映射表放这里、按需编辑；表层文件**不随插件包分发，更新插件也不会覆盖**。首次使用若表层缺表，插件自动从深层复制出厂默认补齐 |
| `mappings/default_mappings/`（深层） | **随包种子源**：出厂说明文档与七张默认表随包分发、随插件版本更新；是否把新版出厂内容合并进你的表层，由你自行决定 |

七张映射表（文件名不变；出厂种子在 `mappings/default_mappings/`，你的编辑/运行读取在 `mappings/` 表层）：

| 文件 | 用途 |
|---|---|
| `emotion_mapping.json` | 情绪原型与映射数据 |
| `emotion_descriptions.json` | 情绪的文字描述 |
| `affection_presets.json` | 好感度档数、档名与区间（1~5 档） |
| `affection_tier_descriptions.json` | 各档位的注入文案 |
| `schedule_properties.json` | 日程属性、聊天频率倍率与标签色 |
| `activity_profiles.json` | 活动画像表：滚动层每类活动的时长分布、可中断性与状态速率 |
| `emotion_life_modulation.json` | 情绪-生活调制表：双向闭环的 gain 系数与活动反馈增量/日上限；`emotion_modulation_enabled=false` 时不装载。**路径固定，无自定义路径键** |

这些表只在插件加载或热重载时读取，修改后需要重启或重载才生效。结构、校验与错误回退规则见 `mappings/default_mappings/README.md`。

## 数据目录

```
data/plugins/cateye.life_belonging_to_maimai/
```

这里保存日程库、情绪状态、好感档案、空间状态与日志，插件只在宿主给定的数据目录内读写。

- `schedule.db`、`emotion_state.json`、`affection_memory.json`、`qzone_state.json`、`life_state.json` 等可安全删除，删除后相当于重新开始。
- `schedule.db` 为 v6（含 `sleep_sessions` 等表）；`mappings/generated_events.json`（事件库）、`life_init.json`（初始化汇总）、`life_feedback_budget.json`（情绪→生活反馈预算）为插件自管用户数据文件。
- `qzone_cookies.json` 删除后插件会先尝试重新向适配器获取 cookie；只有适配器也取不到时，才需要检查 QQ 登录或适配器状态。

## 常见问题

- **升级到 3.0.0 后生活状态/睡眠不动了？** 检查 `[schedule] enabled` 是否为 `true`。旧 `[life_state] enabled` / `sleep_engine_enabled` 已退役，不会再被读取；若你曾显式关掉日程，升级后保持关闭。
- **日程变成了默认时段或明显被压扁？** 用 `/lbm_plan regenerate` 重新生成今日日意图即可。
- **早安或晚安没来？** 先用 `/lbm_diag streams` 查看候选流与序号，再用 `/lbm_diag streams <序号>` 查看该流下一次早安、晚安是否满足触发条件。群聊早安需要判定窗（上份日程开始 → 本份日程开始）内有人说话（必要性开关开启时今天日程第一个条目时段内也要有人开口），且早安没开口的当晚群晚安会被连锁拦下；其他私聊早安需要对方发送条数达门槛、晚安达标即可（连锁或阈值二选一）；过点才达标的补发还受冷静期约束（默认最近 3 分钟 bot 没说过话）。目标私聊无条件，但在 streams 里「无条件」族可见；没收到时用 `/lbm_dashboard` 或决策日志排查。
- **QQ 空间没发出去或没回评？** 用 `/lbm_life qzone status` 检查通道与 cookie，用 `/lbm_life qzone decisions` 查看最近卡在哪一道闸。
- **命令没反应？** 管理员命令要求 `[admin] admin_users` 中有你；`/lbm_mai_*` 只对 `[patrol] target_qq` 本人开放。`[admin] allowed_streams` 只约束 `/lbm_plan` 与日程注入，不影响其他命令。
- **只收到文字没有图？** 这是预期的降级：Pillow 或字体不可用、渲染超时时自动改发文字，功能不受影响。
- **想临时关闭某个模块？** 关闭 `[schedule] enabled`、`[patrol] enabled`、`[qzone] enabled`、`[affection] enabled` 或 `[emotion] enabled` 可分别停用对应模块；关闭 `[plugin] enabled` 会让全部模块停摆（含命令与工具）。

## 技术信息

- 组件 **29 个** = 8 个命令 + 10 个 Hook + 7 个工具 + 4 个跨插件 API（`[admin].harass_report_enabled=false` 时运行期注册面回落 28）。
- 声明 **17 项**宿主能力。
- 配置 **14 个段 / 260 字段 / 5 隐藏 / 255 可见**（`[plugin]` 3、`[patrol]` 23、`[affection]` 12、`[schedule]` 24、`[schedule_advanced]` 31、`[planning_proactive]` 14、`[life_state]` 22、`[event_catalog]` 10、`[llm]` 9、`[prompts]` 56、`[admin]` 8、`[qzone]` 20、`[emotion]` 17、`[maimai_interaction]` 16；3.0.0 离线 stub+pydantic 实算，2026-10-30 随互动改造批新增末段、随多屏插入轮 `[patrol]` 新增 `screen_scope`）。`max_events`、`temperature` 为隐藏兼容键，不提供可编辑旋钮。
- 用户可编辑映射表 **7 张**：出厂种子在 `mappings/default_mappings/`（随包分发、随版本更新），你的实际数据区是 `mappings/`（表层，不随包覆盖）；首次使用缺表会从种子自动补齐。另有插件自动读写的用户数据文件 `mappings/generated_events.json`（事件库）。

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


### 2026-10-28 生活面板扩展

WebUI 的「属于麦麦的生活」展示生活趋势、实际活动/日意图、睡眠、陪伴、情绪 VAD、睡眠裁判、空间凭据健康/发布回执及模型用量。趋势支持图例、拖动缩放、采样表与安全快照导入；继承宿主颜色、明暗和图表风格。持久化历史只记录真实采样，不补造部署前数据；图表可显示带 `estimated` 标记的小缺口/离线睡眠估算段，估算不写回历史。

专注 0 表示注意力涣散，压力 0 表示很放松。低专注/低精力/高压力会增加休息候选；修复零值在评分中误当成满值、社交电量速率字段未接线的问题。恢复遵循真实时间和活动，不直接修改状态。

空间草稿在浏览器本地预览。发布以回执为准，同内容 24 小时去重；仅明确未发出的纯文字请求在原有静默、冷却和预算闸内最多尝试两次。发送结果不明会保留待核对，重启后也不自动重发；图片失败需重新提供图片，不降成队列纯文字重发。回执与凭据文件留在服务器，WebUI 不显示原文与秘密。

## 2026-11-01 小主机验收补修

观看任务现在按任务与模式返回结果，取消和卸载会结束加工任务及其子进程树；聊天/睡眠门控覆盖启动和模型阶段，排队、缓存与终态不降频，陪伴私聊维持1.0。自动分享发送前持久化意图并预留原有主动预算，失败回执不会冒充成功，未知送达保守防重；候选绑定创建时的陪伴对象。

自动观看额度已接到真实日程提交路径，按窗口保守预留；无候选、明确提交失败或缓存命中不计实际启动。其他活动里的低概率间隙触发仍待实现，不能把底层账本/倍率测试通过当作整个场景完成。

解码器优先用PATH中的ffmpeg，也可放在插件data_dir/video_watch/decoder/ffmpeg.exe；不修改宿主PATH。部署包使用[imageio-ffmpeg官方0.6.0 Windows wheel](https://pypi.org/project/imageio-ffmpeg/0.6.0/)内的解码器，另存来源、SHA256与许可文件；不具备解码/视觉能力时明确返回部分结果，不宣称完整观看。

多分P只处理默认选中页，时长、CID、页码与采样绑定，不用合集总时长跳帧；显式p>1链接当前拒绝。多媒体分段尚不拼接，画面会如实降级，字幕缺失不会写成已读。

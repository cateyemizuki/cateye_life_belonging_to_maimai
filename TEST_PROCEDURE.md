# cateye_test 宿主自测试流程（TEST_PROCEDURE.md）
> **【会话 2 范围声明 · 2026-10-06】** 本副本（cateye_test_ws2）的会话 2 仅执行 §三 的 L0 离线测试。
> §四（harness）与 §五（SOP：宿主启停/部署/注入）为会话 1 专属——**只读理解，禁止执行**；
> 一切 `E:\maibot` 路径的操作不得由会话 2 发起。

> 版本：v1.1（2026-10-05 补路径约定与 §一.4 平台坑引用）｜**读者：执行测试的 agent**。配套：[MAINTENANCE_PLAN_10.05.md](MAINTENANCE_PLAN_10.05.md)（里程碑验收标准 + 剩余工作 §九）、[CLEANUP_PROPOSALS.md](CLEANUP_PROPOSALS.md)。
> 性质：开发/测试文档，**发布时排除**（同 scratch/ 测试脚本，开发文档陷阱 #23）。
> ⚠️ **离线脚本路径（2026-10-05 实测订正）**：`smoke_test_cateye_test.py` / `behavior_test_cateye_test.py` / `sdk_stub.py`
> 都在 **`D:\deepseek-harness_workspace\maibot-plugins\scratch\`（`cateye_test` 的同级目录）**；
> `cateye_test\scratch\` **不存在**（L5 分析员曾按该路径查找脚本白耗一轮）。执行时以该目录为 cwd。
> 目标：在不破坏宿主、不烧 token、不依赖真人守着电脑的前提下，自动化验证插件功能与 bug 修复。

---

## 〇、环境事实（已实地核实，勿凭记忆改写）

| 项 | 值 |
|---|---|
| 宿主目录 | `E:\maibot\MaiBot-main`（**不是 git 仓库**——非破坏编辑规则见 §三） |
| 宿主启动 | `E:\maibot\MaiBot-main\run.bat`（内容 = `uv run python bot.py`），**必须前台运行** |
| 启动分工 | 宿主由 agent 启动（方式见 §五.1）；**QQ 与 SnowLuma 适配器侧由用户启动**，agent 只确认 ready |
| 插件安装路径 | `E:\maibot\MaiBot-main\plugins\<插件目录>\`；**拷入后稍等自动热重载**（宿主行为，用户确认） |
| 被测插件 | id = `github.cateye.cateye-suite`，数据目录 `data/plugins/github.cateye.cateye-suite/` |
| 平台链路 | QQ ⇄ SnowLuma 适配器（插件 `maibot-team_snowluma-adapter`，**网关名 `snowluma_gateway`**，账号 **123456789**）⇄ 宿主 |
| 适配器白名单 | `config/adapter_policy.toml`：群聊 default **block**，仅放行 `530610253`、`392653174`；私聊无条目（默认放行）——**注入消息必须走 `snowluma_gateway` 才继承这套名单与真实身份** |
| 宿主日志 | 控制台 stdout（agent 启动时捕获到文件）+ `logs/app_*.log.jsonl`；模型请求 Prompt 预览经 WebUI 推理过程页（`bot_config.toml` 的 `maisaka_prompt_preview_limit`/`llm_request_snapshot_limit`） |
| 宿主数据库 | `data/MaiBot.db`（Messages/ChatSession 等；agent 只做**只读**查询，注意 WAL） |
| 被测插件自有日志 | `data/plugins/github.cateye.cateye-suite/`：`decision_logs/`（决策+联动事件）、LLM 日志、`emotion_state.json`、`affection_memory.json`、`qzone_state.json` |

## 一、测试目标流与安全红线（优先级最高，违反=立即停止测试）

| 流 | 角色 | 规则 |
|---|---|---|
| 群 `392653174` | **主测试群** | 可注入消息、执行命令、触发主动行为；bot 回复会真实发出（预期） |
| 私聊 `123456789` | **主测试私聊** | 同上；测试期间被测插件 `target_qq` 应配置为 `123456789`（想念/早晚安/好感链路的对象） |
| 群 `530610253` | **只许旁观** | **禁止主动激活 bot 聊天**：不得注入、不得发命令、不得把它设进任何主动白名单；仅当框架自身触发时辅助观察并记录 |
| 私聊 `2634571198` | **另一台服务器上的 bot（"她俩"之一）** | 高危：本 bot 与她可能互聊死循环烧 token。禁令：①注入消息的 sender **永远不得是** 2634571198；②不得在该私聊注入诱导内容；③如确需观察 bot↔bot，先开启 harness 环路守卫，至多单条触发，**≥2 个来回立即 /h_freeze**；④默认完全不碰 |
| 宿主进程（maibot 本体） | **用户专属操作权**（2026-10-04 新增红线） | 宿主的启动/停止/重启属用户专属操作权：①宿主必须以前台可见控制台窗口运行（§五.1 形式），禁止无窗口后台承载；②**用户已启动的宿主实例禁止杀**（无论以何种理由）；③agent 需要重启时必须先征得用户确认；④违例一次即停工报告 |

**Token 保护三件套（每次测试会话开始前核对）**：
1. 被测插件配置收紧：`patrol.daily_max_speak ≤ 3`、`patrol.check_interval_minutes` 临时调小仅用于观察、`[judge]`/评估/裁判任务指向便宜非思考任务（`model_config.toml` 任务清单用 `/h_models` 核对）；
2. 监控：LLM 日志按小时计数，异常增长先冻结再排查；
3. 熔断：`/h_freeze` 随手可用（§四.3），最后手段是关闭宿主进程。

## 二、测试分层（L0→L5，逐层收窄）

| 层 | 名称 | 是否需要宿主 | 内容 |
|---|---|---|---|
| L0 | 离线单测 | 否 | `maibot-plugins/scratch/smoke_test_cateye_test.py`（40 组件收集、配置模型、能力面核对）+ `maibot-plugins/scratch/behavior_test_cateye_test.py`（纯算法回归）。**每个里程碑编码前先加用例、编码后必须全绿**（路径见本文件头部的实测订正） |
| L1 | 装载与热重载 | 是 | 拷贝插件 → 轮询确认重载完成 → 版本/组件数核对 → 宿主日志无加载报错 |
| L2 | **注入式集成测试** | 是 | 核心手段：经 `snowluma_gateway` 注入合成 MessageDict，走**完整真实入站链**（写库 → chat.receive hooks → maisaka 回复决策）。无需真人发消息，最大程度"模仿实际应用环境"且零宿主改动 |
| L3 | 真人联动 | 是 | 注入覆盖不到的：真实消息 ID 的引用回复链、表情、图片、用户自己的措辞习惯。由用户在测试群/私聊手打 |
| L4 | 长周期（日切/日程生成/睡眠大修/好感日结/日衰减） | 是 | **不 mock 时钟、不改宿主**：把被测插件配置里的时间参数临时搬到"几分钟后"（`[schedule] auto_generate_time/wake_time/sleep_time`、`[emotion] check_interval_minutes`、`[affection]` 周期参数），插件热重载生效，观察后恢复。禁止改系统时间 |
| L5 | 稳定性长跑 | 是 | 挂机 ≥1 天：decision_logs 事件流完整性、全渠道预算互斥、无异常堆积、无重复主动行为。**复验口径（2026-10-05 增补，对应 MAINTENANCE_PLAN_10.05.md §9.1）**：窗口起点=整改后首次干净启动（如 A2 部署触发的新代启动）；挂机满 24h 后仅对未过项复验，产物追加/新建 recheck 文件、不改写首轮结论 |

**注入的重要性质**（务必理解再用）：注入的消息**不会出现在真实 QQ**（只进宿主 DB 与上下文）；但 bot 的**回复**会经 SnowLuma 真实发出到对应群/私聊——所以只在 392653174 / 123456789 注入会触发回复的内容。`is_notify=True` 的注入只入库不触发回复（可用来低成本更新 last_user_msg_time、模拟撤回/戳一戳通知）。

## 三、非破坏性宿主源码编辑规则（仅在 harness 插件做不到时启用）

- 原则：**能不改就不改**。注入（改入站链路）、组件 enable/disable（改行为）、Prompt 预览/请求快照（看模型请求）、decision_logs（看插件决策）、`ctx.database.get`（读宿主数据）已覆盖绝大多数需求；预期 L2 测试**零宿主改动**。
- 确需编辑时（如观察某内部变量、临时放宽某断言）：
  1. 改前复制原文：`xxx.py` → 同目录 `xxx.py.orig_YYYYMMDD`（宿主非 git 仓库；若用户同意可 `git init` 建本地基线，之后用 git 管理补丁）；
  2. 编辑必须**增量且可回滚**：新增行优先；修改行用配置开关/环境变量守卫（默认走原逻辑）；
  3. 每处改动登记到 §七 HOST_PATCHES 表（文件、行、目的、回滚方法）；
  4. 验证完成后**尽快还原**（对照 .orig 恢复并删除备份）；宿主升级/重新部署前补丁必须清零；
  5. 禁止：改动插件加载器/hook 分发器的核心语义、删除校验逻辑、"顺手重构"宿主代码。

## 四、测试辅助插件 `github.cateye.cateye-harness`（开发目录 `maibot-plugins/cateye_harness/`）

宿主只接受"真实入站消息"驱动，故写一个辅助插件充当测试夹具（test harness）。它同样走热重载安装；自身**不调 LLM、不主动 send.\***（除显式 /h_say），守卫钩子只计数不存内容。

> **实现状态（0.1.1），宿主复验进行中**：下表 11 个组件（9 Command + 1 Hook 环路守卫 + 1 注入网关 `harness_inject_gateway`）均已实现并随宿主验证日投用（`tasks/*.json` → `results/<id>.json` 文件任务通道，L0 四套全绿；见 §八 2026-10-03/10-04 记录）。

| 组件 | 功能 | 备注 |
|---|---|---|
| `/h_inject` | **核心**：构造 MessageDict（开发文档 §10.2 格式）经 `snowluma_gateway` 注入。参数：`group_id` 或 `private_qq`（二选一）、`sender_id`/`sender_nickname`、`text`、可选 `is_at`/`is_notify`/`reply_to_msg_id` | `additional_config.self_id=123456789`；群消息 `group_info` 必填；消息 ID 用 `harness-<uuid>` 防撞。sender 默认取 `123456789`；**sender 白名单校验：拒绝 2634571198**（防误注成另一 bot） |
| `/h_seq <场景名>` | 预置场景脚本：注入序列 + 步进间隔（asyncio.sleep），逐条打印结果 | 场景库与里程碑映射见 §六；新里程碑先在此登记场景 |
| `/h_freeze` / `/h_resume` | 熔断/恢复：`ctx.component.disable` 逐个停用 cateye-suite 的 tool/hook 组件，并提示 agent 改 `plugin.enabled=false` 停后台循环（patrol/emotion 是插件内 asyncio 任务，不是组件，必须靠配置停） | 熔断动作写宿主日志显著标记 |
| 环路守卫 | `chat.receive.after_process`（OBSERVE）计数 麦麦(123456789) ⇄ 2634571198 的往来：10 分钟窗口内 ≥2 个来回 → 自动执行 /h_freeze 等价动作 + 宿主日志 ERROR | 防左脚踩右脚 |
| `/h_state` | 宿主进程内读取被测插件状态：emotion VAD/档位、好感 score/tier、qzone 额度 + decision_logs 尾部 N 条 | agent 平时直接读文件即可，此命令用于进程内权威快照（排除写盘延迟） |
| `/h_wait_reload <version>` | 轮询 `ctx.component.get_all_plugins()` 直到 cateye-suite version/组件数符合预期，超时报错 | 热重载确认（L1） |
| `/h_api <api_name>` | `ctx.api.call` 调被测插件公开 API（`get_current_activity`、`companion_get_affection_level` 等）做集成断言 | 失败形态按 §8.0 兼容 dict/异常两种 |
| `/h_models` | `ctx.llm.get_available_models()` 列任务名 | 前置检查 [judge]/评估任务指向 |
| `/h_say <text>` | 显式让**用户侧**真人测试通道发消息？——不，此命令为 `ctx.send.text` 直发到指定测试流（agent 少用；触发回复链请用 /h_inject，以免污染上下文归属） | 仅 392653174 / 123456789 |

实现注意：`route_message` 前置条件是目标网关 ready（snowluma 由用户启动，注入前用 `/h_api` 或 get_all_plugins 确认）；**命令消息可直接注入**（text 以 `/` 开头即走 Command 组件），可借此测命令鉴权（如以非管理员 sender 注入 `/cateye_test_emotion_init` 应被拒）。

## 五、标准测试流程 SOP（每轮照此执行）

1. **启动宿主（前台可见窗口，2026-10-04 二次修订为本机实测配方）**：
   `powershell Start-Process -FilePath 'cmd.exe' -ArgumentList '/k','run.bat' -WorkingDirectory 'E:\maibot\MaiBot-main' -WindowStyle Normal`
   ——弹出可见 Windows Terminal 窗口（/k 保证崩溃后窗口留存可看报错）。**关键坑**：①禁止重定向 stdout/stderr（重定向=完全无窗口）；②必须设 WorkingDirectory=E:\maibot\MaiBot-main（uv 从 cwd 找 pyproject.toml，错误 cwd 会静默改用系统 Python）；③本机默认终端是 Windows Terminal，被启动 cmd 的 MainWindowHandle 恒为 0，窗口核验用 EnumWindows 找 `CASCADIA_HOSTING_WINDOW_CLASS` 可见窗口（实测窗口名 "cmd.exe - run.bat"），不要依赖 Process.MainWindowHandle。agent **不捕获 stdout**；自动日志读取一律走 `logs/app_*.log.jsonl`。配方出处：`D:\deepseek-harness_workspace\notes\agent-open-foreground-terminal.md`。等待日志出现就绪标志；QQ/SnowLuma 侧由用户启动，agent 经 `/h_models`、get_all_plugins 确认 `snowluma_gateway` ready。
2. **前置检查单**：宿主起 → 适配器 ready → 被测插件已装载（版本/组件数）→ `/h_models` 任务名齐全 → 被测插件测试配置已收紧（§一三件套）→ `target_qq=123456789` → decision_logs 无上轮残留 → 环路守卫已启用。
3. **L0 先行**：跑两个 scratch 脚本全绿再碰宿主。
4. **部署**：robocopy 镜像拷贝到 `plugins\`（**排除 `__pycache__`、`scratch/`、`*.md` 可带**）→ `/h_wait_reload <新版本>`。
5. **执行场景**：按 §六 映射选 `/h_seq` 场景或手工 `/h_inject` 序列；一次只验一个行为。
6. **四通道核对**（缺一不可）：
   - bot 实际回复内容（测试群/私聊里真实发出的）；
   - `decision_logs/`：事件、增量（裁剪前/后 + multiplier）、skip 原因；
   - 宿主日志：`app_*.log.jsonl` 搜 `无法反序列化`（items 注入失败特征）、`cap.send` 失败、hook 超时；
   - 状态 JSON：VAD/score/日切标记是否按预期变化。
7. **记录**：每轮在 §八 追加（日期、里程碑、场景、结果、日志摘录路径）；bug 记"复现步骤/期望/实际/疑似代码位置"。
8. **收尾**：/h_freeze 非必要主动行为 → 恢复 §L4 临时改过的配置 → 无后续任务则询问用户是否关宿主。

## 六、里程碑 ↔ 测试场景映射（随路线图更新）

| 里程碑 | 场景（/h_seq 名） | 关键断言（对应 MAINTENANCE_PLAN_10.05 验收列；A2 为独立先行项，其验收见该文件 §七 A2 行） |
|---|---|---|
| R0 清理 | — | smoke+behavior 全绿、组件数与 CLEANUP 勾选一致 |
| M0 事件总线 | `bus_check` | 注入一问一答 → decision_logs 出现 `bus:relation.*`/`bus:schedule.*`；主动触发写入 `proactive.used`；同日全渠道触发不超全局上限 |
| M1 好感 v2 | `affection_basic` | 连续注入 10 轮对话 → score 上涨但 ≤ 日累计上限（刷 100 条也 +1.0 封顶）；v1 迁移（先放旧格式 JSON 再装载）；档位迟滞（升档需越界+3 且满 24h——用 L4 搬运） |
| M2 裁判/日结 | `affection_review` | L4：sleep 锚点搬到几分钟后 → 会话采样裁判触发（≤2 次/日）、日结 JSON 合法、impressions 滚动；LLM 任务故意配错 → 纯规则退化不崩 |
| M3 识别/倍率/沉默 | `attribution`、`silence` | 私聊注入 → target 加成生效（decision_logs 记 K=1+加成）；群聊注入 → ×1.0；群聊 + 工具 is_target=true → 加成生效；is_target 传垃圾值 → 静默按 1.0；`silence`：is_notify 注入最后一条消息后静置（miss_hours 临时调小）→ silence_detected 每日 ≤2 次、间隔 ≥4h |
| M4 情绪驱动 | `mood_effect` | 构造 V<−0.6 状态 → qzone 跳过发帖、patrol 乘数生效（decision_logs 记录乘后概率）；planner 注入出现情绪行 |
| M5 睡眠结算 | `sleep_settle` | L4：入睡时段开始 → 冻结（期间注入事件只入队）；wake 后一次性应用大修；emotional_tone 越界裁剪 |
| M6/M7 | `dashboard` | M7：/cateye_test_dashboard 图片四区块数值与 /h_state、各 status 文本一致；渲染失败降级文本 |
| Bug 回归 | `regress_<编号>` | 每个 bug 修复配一个最小复现场景，永久留在场景库防复发 |

## 七、HOST_PATCHES 登记（宿主源码改动，**常态应为空**）

| 日期 | 文件 | 位置 | 目的 | 回滚方法 | 状态 |
|---|---|---|---|---|---|
| 2026-10-03 | config/bot_config.toml | [debug].enable_console_input | true→false：Git Bash 后台承载下 BotConsole(prompt_toolkit) 因 TERM=xterm-256color 崩溃致宿主退出；自动化验证无需终端输入 | 改回 true | 生效中 |
| 2026-10-03 | config/adapter_policy.toml | 文件末尾新增 [[adapters]] 块 | harness 注入网关（github.cateye.cateye-harness:harness_inject_gateway）策略放行：私聊 allow、群聊仅 392653174——[defaults] 对未登记网关群/私全 block，导致 /h_inject accepted=False | 删除该 [[adapters]] 块 | 生效中（热读） |

## 八、测试记录（追加式，最新在上）

| 日期 | 里程碑/场景 | 结果 | 摘要/日志位置 |
|---|---|---|---|
| 2026-10-07 | **合并 ws2/m6m7（M8/M9/M10）+ 部署 L1** | 合并 225a87c（零冲突，计划书单份 753 行双方增量齐）；smoke 29=8C+10H+7T+4A exit 0；**部署 L1 PASS**：staging /MIR 重刷 103 文件 md5 全等 → robocopy /E → 08:55:05 file_watcher 自触发热重载 → 装载失败=0、组件 29（含新 nap Tool）；deprecated 旧键（sleep_time/wake_time/auto_generate_time）零报错兼容、新键（morning/night_drift、interrupt_reply_threshold、nap_reminder_max_per_day）走默认值；config.toml 未写回；M8/M9/M10 行为留痕待自然触发（如实记） | 宿主 PID 24724 全程存活未启停；_manifest 仍 0.4.0（变化体现在组件数 28→29）；验证安排：①M8 今晚自然睡眠夜取证（明早分析）②M9 USAGE §七 5 项清单暴露实测+nap 跨午夜倒挂复核 ③M10 真实早安/晚安被发送链路故障阻塞（SnowLuma numeric segment 错误 10-06 20:15 起持续）、patrol 回归 L2 可先行。证据：部署工作流运行记录（staging 刷新/部署/md5 四点抽查） |
|---|---|---|---|
| 2026-10-07 | **M5 自然睡眠夜三合一分析（08:00 定时，分析员只读宿主）→ M5 宿主级闭环裁决** | 冻结 evidence-confirmed / 醒沿一次性应用 evidence-confirmed / 恢复系数 0.85 合代码表；「只入队」与 A3 去重实弹均 **not-observable**（夜内零情绪事件、零 `emotion_report` 调用、零去重拒绝——属"未发生"非"未验证"）；night_health 三中二（发送链路集中爆发证否第三项，非 M5 范畴）→ **协调会话裁决：接受离线覆盖（behavior 11 组 M5 用例 + A3 离线 2 用例），两缺口记「宿主级实弹留观」，M5 宿主级闭环** | 窗口 10-06 23:01:50（sleep_started+缓存，冻结期唯一 LLM=emotion_sleep_referee）→ 10-07 07:01:50（醒沿恰 1 次应用：delta 与缓存逐值相同、无重问裁判、e_tomorrow 醒后重算、恢复 0.85）；冻结窗严格内部 59 行决策日志 emotion-\*/episode/message_received 全 0；状态侧 emotion_log 仅醒后 2 条、vad_raw 反推入睡前值与 10-06 20:31 mood 文本逐维一致；宿主整夜未重启（心跳最大间隔 30.1s）。附带发现：①`applied_sleep_date` 落盘被后续 episode 冲刷 `_save_state` 抹除（守卫弱化，本夜无热重载无实害）→ 会话 2 修补轮 P4；②平台发送链路故障（07:38 群回合 10 轮回复全部 FAILED "numeric message segment…"，同类自 10-06 20:15 起）→ 告知用户/会话 1，阻塞 A3 实弹与 M10 宿主级真实问候观察。证据：`zcode/l5_work/m5_night_analysis.md`（+m5_audit_scan*.py/.out）；裁决报告：`work_report/20261007_0816_M5宿主级闭环裁决_自然睡眠夜三合一分析.md` |
| 2026-10-06 | **合并 ws2/m6m7 + 合并版部署 L1 + 会话 2 清单核对** | 合并 052f41f（唯一冲突 README 取 ws2 侧；计划书单份 744 行双方增量齐；TEST_PROCEDURE 主仓版+会话 2 横幅均在）；smoke 28=8C+10H+6T+4A exit 0；**部署 L1 PASS**（staging 按 HEAD 重刷 /MIR → robocopy /E 退 3 → 19:51:37 热重载 → 已加载=2 失败=0、/h_wait_reload 实测 28）；**迁移=运行时生效**（config.toml 未写回，emotion.episode_flushed idle 冲刷实跑 20:31:39，episode 键入状态文件）；**清单缺口**：三被删命令无别名/无降级/无未知命令应答（按普通消息路由 Maisaka，双源核实），dashboard 本体命中——需求单候选转会话 2（补别名或修订 v1.4 表述） | 合并+部署+清单证据：results/vc2-delcmd-{status,schedule,config,dashboard-coverage}.json、宿主 :3438/:3493/:3501、回写详见 work_report/20261006_1920_会话2执行T6T7注册面收口与评估片段制.md §九；操作教训补一条：robocopy /MIR 刷 staging 顺带清掉了其中残留的 MAINTENANCE_PLAN_10.25.md |
| 2026-10-06 | **M5 宿主级 L4（sleep_settle 快速验证）+ A3 去重注入** | 冻结 PASS / 一次性应用 PASS / 恢复原值 PASS；emotional_tone 裁剪未可观测（重生成模型未输出该字段，代码层 clip ±0.5 在位）；「冻结期只入队」直接证据与去重实弹未取得（如实记录） | ①前置五项全过（宿主 PID 24724 存活/部署副本 M5 标记/patrol.daily_max_speak=3 与 target_qq 未动/harness 通道在位/L4 原值快照 wake=07:00·sleep=23:00·auto_generate=00:30·check_interval=10）；②L4：三键临时搬移（wake 18:41/sleep 18:34/auto_generate 18:30）+touch 重载（再次实证 config.toml 单改不触发 Supervisor，须 touch）；重生成经 tasks/ 注入 /cateye_test_plan regenerate 触发（18:29:18 生成 13 项；auto 路径因当日已有日程按设计跳过，偏差如实记录）；18:38:06 bus:schedule.sleep_started(sleep_hours=0.5) → 18:38:15 emotion.sleep_overhaul_cached「冻结期计算并缓存大修（未落状态）」→ 冻结窗内 emotion 状态与 decision_logs 零结算；18:48:15 解冻，emotion.sleep_overhaul 与 bus:emotion.overhauled **各恰好 1 次**（delta 限幅 0.64、恢复系数 0.3、唤醒侧重算字段齐全）；config.toml 三键逐字节恢复（md5 核对）+touch；③去重注入：19:12:46 经 tasks/ 向群 392653174 注入成功（回执 results/m5-a3-dedup-1.json + MaiBot.db 落库行实证），但 15 分钟内 bot 回合未完成（轮次调度器必要性 5<80「等待更多消息」、无 planner 运行、0 出站）→ 工具通道与 replyer 均未入账，宿主级去重路径未 exercised（如实记录；口径与离线用例见计划书 §2.4.6 A3 行）；全程未改系统时间/未启停宿主/注入仅私聊 123456789 与群 392653174。证据：验证工作流运行记录 + decisions_2026-10-06.jsonl |
| 2026-10-06 | **M5 部署上宿主 + A2 冷启动档补录** | 部署成功（robocopy /E 94 文件 md5 一致、热重载最新代 40 组件失败=0）；两项清退落地；**A2 完全闭环（热重载+断电冷启动双档）** | ①robocopy 首试因 MSYS 路径转换吃掉 /E 退 16（未复制任何文件），`MSYS_NO_PATHCONV=1` 重试成功（操作教训入账）；②热重载后部署副本 planning/config.py 含 emotion_influence、装载无失败；③T3：部署残留旧名 config.py（6225B）删除（先 grep 实证 staging 无 config 顶层引用）；④A6 收尾：部署副本 config.toml 删 enable_state_analysis 一行——**实测修正认知：宿主 config.toml 改动不触发 Supervisor 重启**（仅 .py/plugin.py/_manifest.json 触发）；⑤A2 冷启动档（修正结论）：10-06 凌晨运行中日切重置落盘（date→2026-10-06）→10:28 断电硬停盘上 used=3→17:44 用户冷启动从盘装载同日 3→实时累计 4（扛过真实断电），启动日志零告警——跨日重置分支静默=已知 _load 行为；⑥宿主进程全程未启停（用户 17:44 实例）。证据：部署工作流运行记录 + proactive_budget.json + app_20261006_174436/174441 |
| 2026-10-06 | **§9.1 正式复验（M0 闭环前）** | **PASS**（error_pileup=pass、proactive_budget=pass；分析员/审计员双独立一致，无需仲裁）→ **M0 宿主级闭环**，tag v0.4.1-m0-closed→632685b | 窗口 2026-10-05 05:50:58.600→2026-10-06 06:07（约 24h16m，31 日志 55,631 行）：error_pileup 首轮破阈两类（snowluma_pipe/cateye_plugin）全窗口 **0 条**，逐类峰值 0/14/0/12/4 无一 >20/h；两处「≥3h 递增」字面命中（timeout_msg 0→10→12、other 0→1→3）实证均为恒定步长周期假象（600.7s 整数倍 RMSE 4.8s、10:56 自止、来源=宿主 src/common/remote.py:170；15 分钟统计行桶相位）→ 按用户预裁决原则判非堆积；proactive_budget：10-05 日计数 5≤5（patrol 2/planning 3）、19 条 budget.blocked（used={5}/cap={5}）上限严格执行、A2 JSON 与事件流逐字段互证、两次重载（05:50 部署/08:07 取证）计数连续、全库零「损坏/全新起步」告警；新边界核定：10-06 05:42 文件切分为 5MiB 大小轮转非重启。关机机制调查：宿主唯一优雅关机=SIGINT（bot.py:477），WebUI restart=os._exit(42) 被 runner 判为重启非关机，外部信号=硬杀禁止 → agent 无把握，提请用户 09:50 前控制台 Ctrl+C（「收到 prepare_shutdown」实为 runner 子进程 file_watcher 重载日志，非宿主关机判据）。证据：zcode/l5_work/l5_report_final.md、recheck_final_audit_numbers.md、final_errors.out、final_budget.out、final_stability.out |
| 2026-10-05 | **过夜推进：A2 部署+宿主取证 PASS；A6/A2补丁/M5 离线四步全提交** | A2 宿主级验收 PASS；M5 离线全部落地（提交链 99c762d→632685b→8d11f54→1ccae20→530a89b→8354708→03f0234，四套门全绿）；§9.1 复验窗口进行中（起点 05:50:58.600） | ①A2 部署：robocopy exit 3（85 文件）→ file_watcher 3s 自动重载（05:50:58），新代干净启动、组件 40 不变、proactive_budget.json 首次装载取证；②受控重载取证 PASS（08:07）：touch plugin.py → Supervisor 5s 重启 → used=2 不回落、JSON mtime 未改写、零「全新起步」warning、跨代真实增量 2→3（patrol 自盘保留）；③离线提交：8d11f54（A6 删死字段，宿主 extra="ignore" 实证+venv 探针）、1ccae20（A2 _load 降级补全，用例 77 先红后绿）、530a89b（M5 冻结/醒后应用+大修 v2+emotion_context，用例先行 4 红→绿，注入时钟跨午夜）、8354708（连续低迷）、03f0234（A3 判保留+跨来源去重，可发现性取证=harness 群聊注入 1 次通过）；④staging 镜像 HEAD（94 文件，8 个涉及文件 md5 一致）；⑤error_pileup 半项先行复验=pass（首轮破阈两类全窗口归零），timeout_msg 0→6→12 口径分歧已记录待最终仲裁；⑥宿主写入全程仅：部署 1 次+touch 1 次+harness 注入 1 份（用户授权），零源码/配置改动、零进程启停。遗留：M5 宿主级 L2/L4 待窗口结束+用户在场；宿主 config.toml 死键随 M5 部署清；scratch 版本管理待裁决。证据：work_report/20261005_0845_过夜自动推进中期报告_A2部署取证与M5离线四步.md、zcode/l5_work/l5_report_recheck.md |
| 2026-10-05 | **文档一致性修订（无代码改动）** | 完成 | ①L5 挂账落档：A2（预算计数持久化）自 M5 拆出为**独立先行项**，`proactive_budget` 复验的前置改为 A2；②计划书换版：现行版 = `MAINTENANCE_PLAN_10.05.md`（旧 `MAINTENANCE_PLAN.md` 加废弃横幅、只读留档），本文件与 CLEANUP/SYNERGY/README 的链接与 §指针同步改指新版；③平台坑补两条实测教训（`file_watcher` 热重载清零内存态计数、自建注入网关需过 `adapter_policy` 放行）见计划书 §八.8；④L0 脚本路径实测订正见本文件头部；⑤新增工作报告规范（`work_report/`，计划书 §八.12）。证据：`cateye_test/work_report/20261005_*.md` |
| 2026-10-05 | **L5 判定（九项检查；分析员→独立审计→仲裁三权分立，子代理模型=deepseek-v4.1-flash）** | 7 pass、1 warn（proactive_budget）、1 fail（error_pileup，按检查项字面口径）→ **总体 FAIL 如实保留，M0 闭环待整改后窗口复验后裁决** | 判定窗口 10-03 18:15:07→10-05 01:24（累计 31h09m ≥1 天；宿主 5 代启动与上文违例通报史逐一对账，无未记录崩溃；日志并集最大空洞 63s、07:35:17 后零重启）。**PASS 项**：①silence≥4h 断言（3 条事件：相邻间隔 15.43h/4.000h、≤2/日、自然窗口 2 条真实触发 hours=4.15/8.15 可回溯 last_user_msg_time=10-04 07:36:29）→ **M3 顺延项就此闭环**；②decision_logs 事件流完整性（722 行、非法 JSON=0、bus 六类全踪迹、>6h 空洞=0）；③无重复主动行为（早安/晚安/活动切换 5 个 intent 各 1 次、qzone 2 次=上限）；④LLM 监控（31 条/30.7h、峰值 14/h、规则与沉默事件零 LLM）；⑤状态文件健全（25/25 断言）；⑥smoke 40 组件复跑通过（脚本实际位于 maibot-plugins/scratch/；dev↔部署 72 个共同 .py MD5 一致，部署侧多 1 个旧名 config.py 残留→M5 部署时清理）。**warn**：10-04 全渠道 used=6>上限 5，根因=07:35:16 热重载清零内存态预算计数器（core/event_bus.py:191-197；+1 有界、单进程生命周期内 18 次拦截严格执行；patrol 渠道自身预算 2≤2 达标）→ **真实缺陷「预算计数不跨热重载持久化」挂账随 M5 修复**。**fail**：error_pileup 破阈全部落于 10-03 18:15~10-04 06:54 验证日混乱期——cateye 插件错误 74 条=已修复 P1（29bf06f）与 #7（bdf29e6）的定位期记录、修复后归零；SnowLuma 管道错误（WinError 232）去重 1573 条=重启搅动期适配器侧产物（适配器归用户）；整改后窗口（≥10-04 07:35:17）全类别零破阈。**裁定路径**：字面 FAIL 不掩盖 → 复验窗口起点取 A2（预算计数持久化，MAINTENANCE_PLAN_10.05.md §七 A2 行/§9.1 前置修订）部署后的干净启动，挂机满 24h 后对 error_pileup+proactive_budget 两项独立复验（仍实施/验证分立，产物追加 l5_report_recheck.md，不改写首轮结论）→ 通过则 M0 闭环+tag。证据：D:\deepseek-harness_workspace\zcode\l5_work\l5_report.md（52KB，逐项含仲裁说明）及全部中间产物/复现脚本 |
| 2026-10-04 | **宿主验证日终态汇总（六场景 + 顺延复验 + qzone 复验 + 7 缺陷）** | 六场景全部 PASS：bus_check（M0）、affection_basic + v1 迁移（M1）、attribution（M3，复验后）、mood_effect（M4，复验后）、silence（M3；≥4h 间隔断言并入 L5）、affection_review（M2，复验后）；qzone 子项=PASS | 累计发现并修复 7 项缺陷：①插件目录不在宿主 sys.path（91251a9/8176d19 引导头）；②schedule_query 缓存窗口 15h→15min（5008cf1）；③P1 EmotionServices 委托缺失（e993e9f 扩展）；④P2 is_notify 被忽略；⑤P3 睡眠裁判 max_tokens 截断；⑥P4 日结失败落 date（③~⑥ 即 P1~P4，29bf06f 修复复验）；⑦QzoneService config 接线错位（bdf29e6）。顺延项与去向：silence ≥4h→L5 判定、qzone 回评→留观或小号配合、工具 is_target→M5 挂账裁决（详见 MAINTENANCE_PLAN.md §2.3/§七）；L5 挂机进行中 |
| 2026-10-04 | qzone 子项复验（status/post/文案情绪行） | PASS | 接线修复 bdf29e6 生效：status 命令端到端可用（修复前同命令 100% AttributeError）；真实发帖 `bus:qzone.posted {tid, summary, source:qzone}`（载荷符附录 A）+ `qzone_state.json posted_today=1`（额度 1/2）；M4 mood_line 融合代码级取证（qzone/service.py:376-380/:442-445）；日程驱动路径 prompt 断言随 L5；cookie/动作通道未连通=环境状态（如实回报，非插件缺陷） |
| 2026-10-04 | 宿主验证日缺陷 #7（QzoneService config 接线错位） | 已修复 bdf29e6 | qzone 首次启用点亮死路径：8 处 `self._config.qzone` 落空（plugin.py:103 第二实参误传插件对象）；修复=实参改 self.config + service 增 _qzone_cfg() 活取值器（保热重载）+ keyword-only plugin 形参（_bus/emotion 路径）+ qzone/config.py 补 3 字段；宿主实测 status 收到 bot 回复 |
| 2026-10-04 | 顺延复验（mood_effect / attribution / affection_review） | 三项转 PASS | emotion init 实证（bot 05:16 回执「✅ 情绪系统初始化完成」+参数摘要）；planner 快照情绪行维度随真实 VAD 更新；decision_logs emotion.external_event K=1.4（tier3 加成 0.4，source=replyer，δV+0.14 限幅内）；sleep_started→overhauled→daily_review 全链（tone+impressions×2 落盘，P4 成功才落 date）；四套回归全绿；整合验收 9/10 |
| 2026-10-04 | 残留清理 | 已清 | 整合验收指出 harness-sleep-rv（日结复验用 sleep 条目）未随首轮清理删除 → 主会话删除并复核归零；L5 观察去污染 |
| 2026-10-04 | 宿主进程边界违例（通报一） | 违例确认 + 规程修订（§五.1 可见窗口 / §一 宿主进程红线） | 启停史：10-03 16:56 用户启动宿主（崩溃循环 4 次，16:56:34 进程对 37584/45796 残留、后自行退出）；10-03 18:07 agent attempt1（无窗口后台，BotConsole 崩溃退出）；18:15 attempt3（无窗口后台，运行至 10-04 01:55 被 agent 树杀）；10-04 01:47 attempt4（无窗口后台，04:41 无日志硬退出）；04:42 attempt5（无窗口后台，被用户手动清除）。违例说明：agent 以"测试环境清理残留/使配置生效"为由多次启动与终止宿主实例，均未征得用户确认且采用无窗口后台形式——终止对象经复核为 agent 自启实例（30328/32068 树），用户 16:56 实例在 agent 处置前已自行退出；但宿主操作权边界自此事件起明确归用户，agent 此前的一切启停均按违例记录 |
| 2026-10-04 | L5 事件：attempt4 硬退出 | 记录 | 04:41 退出码 1、无优雅关闭痕迹、崩溃前仅 A_Memorix 400 常规噪音（agent_session_stdout.attempt4-crashed-0441.log 留存）；已按新规程以可见窗口重启续 L5 |
| 2026-10-04 | 宿主验证日·六场景 L2 批量核对 | bus_check=PASS；affection_basic+v1迁移=PASS；attribution/mood_effect/silence/affection_review=PARTIAL（顺延项见下） | 证据：decision_logs（decisions_10-03/04.jsonl）、宿主 DB mai_messages（bot 出站 15+ 条，"博士"称呼自然对话）、harness results/*.json、maisaka_prompt 快照；独立核对员报告见会话存档。修复 P1-P4（29bf06f）后情绪行注入已实证（02:38 快照含【当前情绪底色】） |
| 2026-10-04 | L2 实证缺陷 P1-P4（全部修复 29bf06f） | P1 EmotionServices 缺委托→情绪 Hook AttributeError ×44；P2 is_notify 被整体忽略；P3 睡眠裁判 max_tokens=600 截断；P4 日结失败也落 reviewed_date | 各项修复经四套测试门+独立验收；P1 情绪行宿主复验通过；P3 token 提升仍被思考型输出淹没（非思考模型绑定为宿主 model_config 决策，§八.8） |
| 2026-10-04 | L5 挂机（进行中） | 宿主自 10-03 18:15 起持续运行（中途两次计划内重启），恢复配置生效 | 定期抽查 decision_logs；完成判定待 ≥1 天窗口 |
| 2026-10-03 | 宿主验证日 L1 | 首次部署加载失败→修复（宿主加载器证据见附录「L1 实证发现」） | 四套测试全绿（L0：smoke/behavior × cateye_test/cateye_harness） |
| 2026-10-03 | harness v0.1.1 文件任务通道 | L0 四套全绿 | `tasks/*.json` 四动作白名单（inject/seq/state/freeze\|resume）→ `results/<id>.json` 回执，宿主验证日主控链路就绪 |
| 2026-10-03 | cateye_harness 0.1.0 实现 | L0 四套全绿（smoke/behavior 各两套） | 代码 D:/deepseek-harness_workspace/maibot-plugins/cateye_harness，staging D:/deepseek-harness_workspace/maibot-plugins/cateye_harness\build_staging\； |
| 2026-10-03 | 文档建立 | — | 环境事实与红线制定；harness 插件已实现待宿主验证（L0 全绿，staging 就绪） |

## 附录：全自动模式（宿主验证日 SOP）

> 性质：计划版本；**2026-10-03 宿主验证日已进入执行——L1（部署/热重载）已产出实证结论（见下「L1 实证发现」），全流程其余步骤待执行**；验证日的实证结论与偏差追加记录到 §八。

### 〇、前提

- 用户已启动 QQ + SnowLuma 适配器侧（agent 只确认 ready）。
- 宿主环境纯测试：**配置无需保全**，任何改动记录在案即可。
- emotion 模块本次**不初始化**：零锚点即可跑完全部场景；人设锚点推导留待用户在场时执行管理员命令或后续 admin API——**不在自动范围内**。

### 步骤

0. 两仓库打 tag：`cateye_test` v0.4.0-offline-verified / `cateye_harness` v0.1.1。
1. agent 前台启动宿主（Bash 后台任务承载 `run.bat`，stdout 捕获到 `logs/agent_session_stdout.log`）；随后轮询宿主日志确认 `snowluma_gateway` ready（**最多等 10 分钟，超时即停并报告**；若用户侧未就绪导致超时，等待后重试一次）。
2. robocopy 部署两 staging（`cateye_harness\build_staging\cateye_test` → `plugins\cateye_test`；`cateye_harness\build_staging\cateye_harness` → `plugins\cateye_harness`）；热重载确认：轮询宿主日志“插件加载”行 + 组件文件时间戳，**实证拷贝是否自动重载**；若不自动则 touch 插件文件强制重载——把实证结论记入 §八。
   - **实证结论（2026-10-03）**：宿主 file_watcher 自动热重载**实证成立**——拷贝后 watchfiles 检出变更批次（18:16:51.9）约 1s 即完成重启，宿主日志依次出现 `开始重启插件运行时 Supervisor: file_watcher`（18:16:52）与 `插件运行时 Supervisor 已重启完成: file_watcher`（18:16:53），**全程无需 touch**。（证据：宿主 `logs/app_20261003_181507.log.jsonl`；机制：`src/plugin_runtime/integration.py:1511-1531`（watchfiles 监视插件根目录，debounce 600ms）+ `:602-628`（重启日志））。上文“若不自动则 touch”仅作后备（本次未用）。
3. 首次加载生成 `config.toml` 后直接写入收紧配置（`patrol.daily_max_speak ≤ 3`、`[judge]`/评估任务指便宜非思考任务、`target_qq=123456789`）+ 强制重载 + 读回验证。
4. 网关连通性探测（**第一发任务文件**）：先私聊注入（123456789→123456789 私聊流）确认链路与回复（回复确认以宿主日志/decision_logs 为准，真实 QQ 侧由用户目视）；再群聊注入 392653174——若 harness 网关被宿主适配器名单拦截，在 `config/adapter_policy.toml` 增补 harness 条目（测试环境允许，记入 §七 HOST_PATCHES），重试通过。
5. L2 场景链按序全自动（经 `tasks/*.json` 下发 inject/seq 任务，收 `results/<id>.json` 回执）：`bus_check` → `affection_basic` → `affection_review` → `attribution` → `silence` → `mood_effect`；每场景四通道核对**全部文件化**（decision_logs jsonl / 宿主 app log / 状态 JSON / results 回执），逐场景写核对记录（主会话驱动操作，核对派子代理）。
6. L4 子项（迟滞 24h、sleep 锚点搬运）：agent 直接改 `config.toml` + 强制重载 + 等待观察；用完搬回并记录（环境一次性，但同日后续场景依赖合理配置）。
7. 全部通过 → L5 挂机 ≥1 天（宿主保持运行，agent 定期抽查 decision_logs）。
8. 收尾报告：每场景 PASS/FAIL + 四通道证据文件路径 + HOST_PATCHES + git log。

### L1 实证发现（2026-10-03 宿主验证日）

**宿主扩展插件运行时不把插件目录加入 sys.path**（两插件首载失败的根因）：宿主 Runner 用 `spec_from_file_location` 加载插件 `plugin.py`，只在 `exec_module` 期间临时把「plugins 根目录」与宿主 `src` 加入 sys.path（`src/plugin_runtime/runner/plugin_loader.py:583-584` 两处 `_temporary_sys_path_entry`；`:571` 的 `submodule_search_locations=[plugin_dir]` 只作用于插件模块自身，不等于把插件目录加进 sys.path）。因此插件内的顶层绝对导入（`companion`/`core`/`config` 等）会被宿主 `src/` 下同名模块抢先命中，或直接找不到。

**两插件首次部署加载失败证据**（宿主日志 `logs/app_20261003_181507.log.jsonl`，18:16:53；汇总行 `插件注册失败: ... 已加载=0，失败=2`）：
- `github.cateye.cateye-harness`（旧版 `plugin.py:49`）：`from config import CateyeHarnessConfig` → `cannot import name 'CateyeHarnessConfig' from 'config' (E:\maibot\MaiBot-main\src\config\__init__.py)`，即 `config` 落到了宿主 `src/config`；
- `github.cateye.cateye-suite`（旧版 `plugin.py:22`）：`from companion.affection import AffectionManager` → `ModuleNotFoundError: No module named 'companion'`。

**修复（实际方案，已落地于开发目录与 staging）**：
1. 两插件 `plugin.py` 顶部加 sys.path 引导：自身插件目录先 remove 再 `insert(0)` 到 sys.path 首位（`cateye_test/plugin.py:31-34`、`cateye_harness/plugin.py:56-59`），保证自身顶层模块优先命中；
2. **模块冲突处理**：配置模块改名，避开通用顶层名 `config`——`config.py` → `suite_config.py`（cateye_test）/ `harness_config.py`（cateye_harness）。原因：同进程多插件共用 `sys.modules`，顶层 `config` 会被宿主 `src/config` 或先加载的兄弟插件占住；
3. **同运行时多插件顶层模块命名需唯一**：sys.path 顺序只对“首次 import”生效，一旦某名字已进 `sys.modules`，后加载插件就会拿到占用者的模块——新增顶层模块前先全仓搜索同名文件/目录。

### 分工说明

- 操作驱动（启停/拷贝/写配置/下发任务文件/轮询日志）由**主会话**执行；
- 每场景四通道证据核对由**独立子代理**执行。

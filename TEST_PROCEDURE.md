# cateye_test 宿主自测试流程（TEST_PROCEDURE.md）

> 版本：v1.0（2026-10-03）｜**读者：执行测试的 agent**。配套：[MAINTENANCE_PLAN.md](MAINTENANCE_PLAN.md)（里程碑验收标准）、[CLEANUP_PROPOSALS.md](CLEANUP_PROPOSALS.md)。
> 性质：开发/测试文档，**发布时排除**（同 scratch/ 测试脚本，开发文档陷阱 #23）。
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

**Token 保护三件套（每次测试会话开始前核对）**：
1. 被测插件配置收紧：`patrol.daily_max_speak ≤ 3`、`patrol.check_interval_minutes` 临时调小仅用于观察、`[judge]`/评估/裁判任务指向便宜非思考任务（`model_config.toml` 任务清单用 `/h_models` 核对）；
2. 监控：LLM 日志按小时计数，异常增长先冻结再排查；
3. 熔断：`/h_freeze` 随手可用（§四.3），最后手段是关闭宿主进程。

## 二、测试分层（L0→L5，逐层收窄）

| 层 | 名称 | 是否需要宿主 | 内容 |
|---|---|---|---|
| L0 | 离线单测 | 否 | `scratch/smoke_test_cateye_test.py`（39+ 组件收集、配置模型、能力面核对）+ `scratch/behavior_test_cateye_test.py`（纯算法回归）。**每个里程碑编码前先加用例、编码后必须全绿** |
| L1 | 装载与热重载 | 是 | 拷贝插件 → 轮询确认重载完成 → 版本/组件数核对 → 宿主日志无加载报错 |
| L2 | **注入式集成测试** | 是 | 核心手段：经 `snowluma_gateway` 注入合成 MessageDict，走**完整真实入站链**（写库 → chat.receive hooks → maisaka 回复决策）。无需真人发消息，最大程度"模仿实际应用环境"且零宿主改动 |
| L3 | 真人联动 | 是 | 注入覆盖不到的：真实消息 ID 的引用回复链、表情、图片、用户自己的措辞习惯。由用户在测试群/私聊手打 |
| L4 | 长周期（日切/日程生成/睡眠大修/好感日结/日衰减） | 是 | **不 mock 时钟、不改宿主**：把被测插件配置里的时间参数临时搬到"几分钟后"（`[schedule] auto_generate_time/wake_time/sleep_time`、`[emotion] check_interval_minutes`、`[affection]` 周期参数），插件热重载生效，观察后恢复。禁止改系统时间 |
| L5 | 稳定性长跑 | 是 | 挂机 ≥1 天：decision_logs 事件流完整性、全渠道预算互斥、无异常堆积、无重复主动行为 |

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

1. **启动宿主（前台）**：agent 用 Bash 后台任务承载前台控制台进程并捕获输出：
   `cd /e/maibot/MaiBot-main && cmd //c run.bat`（run_in_background，输出重定向到 `E:\maibot\MaiBot-main\logs\agent_session_stdout.log`）。等待控制台出现就绪标志；QQ/SnowLuma 侧由用户启动，agent 经 `/h_models`、get_all_plugins 确认 `snowluma_gateway` ready。
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

| 里程碑 | 场景（/h_seq 名） | 关键断言（对应 MAINTENANCE_PLAN 验收列） |
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
| （无） | | | | | |

## 八、测试记录（追加式，最新在上）

| 日期 | 里程碑/场景 | 结果 | 摘要/日志位置 |
|---|---|---|---|
| 2026-10-03 | 文档建立 | — | 环境事实与红线制定；harness 插件待实现 |

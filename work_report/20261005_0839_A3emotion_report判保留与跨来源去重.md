# 20261005_0839_A3emotion_report判保留与跨来源去重

- **时间戳**：2026-10-05 08:39（本机 UTC+8）
- **会话性质**：宿主只读取证 + 一次 harness 文件通道注入（A3 可发现性取证）→ 改代码（跨来源去重）→ 改文档（计划书/INTEGRATION/本报告）
- **依据（读过的上游材料）**：
  - `D:\deepseek-harness_workspace\maibot-plugins\cateye_test\MAINTENANCE_PLAN_10.05.md`：§2.4.2（`cateye_test_emotion_report` 行）、§2.4.6 A3 行、§6.2 A、§八.1/8.2/8.8/8.10/8.12、§9.2/§9.4；
  - `D:\deepseek-harness_workspace\maibot-plugins\cateye_test\TEST_PROCEDURE.md`（注入非破坏性规则与红线；仅读取，未改动）；
  - `D:\deepseek-harness_workspace\maibot-plugins\cateye_test\emotion\service.py`、`config.py`、`services.py`、`components.py`、`bridge.py`、`state_machine.py`（现状实现）；
  - `D:\deepseek-harness_workspace\maibot-plugins\scratch\behavior_test_cateye_test.py`（既有 M3/M5 用例与 `_m3_emotion_service`/`_M3RecLogger`/`_M3LLMStub` 约定）、`smoke_test_cateye_test.py`（40 组件门）；
  - 宿主源码（只读）：`E:\maibot\MaiBot-main\src\maisaka\reasoning_engine.py`（:451-492 工具面构造、:1522-1543 工具调用日志）、`runtime.py`（:1546-1557、:1650-1734 deferred 池/提示/搜索）、`builtin_tool\tool_search.py`、`turn_scheduler.py`；宿主部署配置 `E:\maibot\MaiBot-main\plugins\cateye_harness\config.toml`；
  - `D:\deepseek-harness_workspace\maibot-plugins\cateye_harness\plugin.py`（文件任务通道与白名单实现，注入前核对字段）。

## 一、任务与产出（绝对路径）

### 1) A3 可发现性取证（宿主侧；唯一写入 = 一次注入任务文件）

- 任务文件（写入后由通道归档）：`E:\maibot\MaiBot-main\data\plugins\github.cateye.cateye-harness\tasks\a3-probe-1.json` → `tasks\done\a3-probe-1.json`（210 B），内容：
  `{"id":"a3-probe-1","action":"inject","params":{"group_id":392653174,"sender_id":123456789,"sender_nickname":"阿米娅","text":"今天项目总算上线了，忙了两周终于能睡个好觉了"}}`
- 回执：`E:\maibot\MaiBot-main\data\plugins\github.cateye.cateye-harness\results\a3-probe-1.json` —— `status=ok`、`ts_iso=2026-10-05T07:45:29+08:00`、`target=group:392653174`、`sender=123456789`、`is_notify=false`、`message_id=harness-48fa5bbbd9124215876422f81f25177e`。
- 结论：**可用**（保留）。注入后真实回合（08:13:51 由插件活动切换任务触发，轮次 17/18）的 planner 请求 deferred tools 提示已列出该工具：
  - `E:\maibot\MaiBot-main\logs\maisaka_prompt\planner\qq_group_392653174\1791159241197.json` request_items[54]（system-reminder 第 4 条 `cateye_test_emotion_report: 上报一件影响 bot 心情的事件…`）、注入消息在 request_items[53]；
  - `...\1791159271034.json` request_items[52] 同样列出；该回合模型 reasoning 明确感知 deferred 面后主动不调 tool_search；
  - 零调用佐证：宿主日志 `E:\maibot\MaiBot-main\logs\app_20261004_050153.log.jsonl` 窗口内工具调用仅 `reply` 一条（08:14:01），`tool_search`/`emotion_report` grep 均为 0；`E:\maibot\MaiBot-main\data\plugins\github.cateye.cateye-suite\decision_logs\decisions_2026-10-05.jsonl` 中 `source=tool:emotion_report` 为 0 条（10-03/04 两天同为 0）。
- 附带观察（未修复，非本项范围）：该回合 reply 因引用 harness 非数字消息 id 被 snowluma 适配器拒绝发送（`numeric message segment field must contain only an integer, received "harness-48fa…"`）；不影响取证结论，留作宿主验证日线索。

### 2) 跨来源去重实现（dev 仓，均为 `D:\deepseek-harness_workspace\maibot-plugins\cateye_test\`）

- `emotion\service.py`：新增常量 `CROSS_SOURCE_DEDUP_SOURCES=("replyer","tool:emotion_report")`、`DEDUP_WINDOW_DEFAULT_SECONDS=180`、`_dedup_registry_max`；模块级 `_event_fingerprint()`；实例内存在线登记 `_cross_source_last`；新方法 `dedup_policy()`、`_dedup_enabled()`、`_dedup_window()`、`_dedup_stream_key()`、`_dedup_duplicate()`、`_dedup_note()`、`_dedup_skip_result()`；`evaluate_reply` 与 `_apply_external_delta` 入账前判重、`record_external_event` 新增 `session_id: str = ""`（带默认值）并在评估 LLM 之前先判重、成功/重复返回均带 `dedup` 口径字段。
- `emotion\config.py`：新增 `dedup_enabled`（默认 True）、`dedup_window_seconds`（默认 180，0~3600，0=关窗口）。
- `emotion\services.py`：`EmotionServices.record_external_event` 转发签名同步新增 `session_id`（与内层逐字对齐，facade 审计门保持有效）。
- `emotion\components.py`：`cateye_test_emotion_report` 从宿主 kwargs 取 `stream_id` 随上报传入；命中重复时回文本「这件事刚才已经计入情绪了（与自动评估重复），未重复计入。」；docstring 写明去重口径。

### 3) 用例（`D:\deepseek-harness_workspace\maibot-plugins\scratch\behavior_test_cateye_test.py`）

- 新增 A3 段（文件尾、最终 PASS 行之前）：`test_a3_cross_source_dedup_counts_once()`（①工具先→自动评估后 / ②自动评估先→工具后，同一流+窗口只计一次，且去重命中不产生多余 LLM 调用）、`test_a3_cross_source_dedup_boundaries()`（③异流/窗口过期/同来源连续/模板事件/开关关闭/返回值口径/配置默认值/状态 JSON 不含去重字段/旧状态文件兼容），附 `_a3_run_cases()` RED/GREEN 运行器；
- 既有用例唯一改动（必要同步，非放宽断言）：`test_component_facade_audit` 的 `_FakeInner` 桩与期望元组增加 `session_id`（因对外签名按 §八.2 兼容地新增该参数）。

### 4) 文档

- `MAINTENANCE_PLAN_10.05.md`：§2.4.6 A3 行重写为「已裁决：保留 + 去重」并附口径与证据路径；§2.4.2 `emotion_report` 行尾追加"裁决落定"；§七 M5 行状态与 A3 勾选；§9.2 进度勾销 A3。
- `emotion\INTEGRATION.md`：模块表 `config.py`/`service.py` 两行补记 + 新增「跨来源去重（A3 落地）」小节。
- 本报告；`work_report\README.md` 索引补登（含上一份遗漏的 0738 报告）。

## 二、实测证据（命令 + 数字 + 路径）

| # | 命令（任意 cwd） | 结果 |
|---|---|---|
| 1 | `python D:\deepseek-harness_workspace\maibot-plugins\scratch\behavior_test_cateye_test.py`（**实现前**，RED 取证） | **exit 1**；`A3-RED ①…: AssertionError: 同一流窗口内自动评估不得重复入账`、`A3-RED ③…: AttributeError: 'EmotionService' object has no attribute '_dedup_stream_key'`；`A3 用例红灯 2/2`；输出留存 `D:\deepseek-harness_workspace\maibot-plugins\scratch\_a3_red.out` |
| 2 | 同命令（**实现后**，GREEN） | **exit 0**；`A3-GREEN ①`、`A3-GREEN ③`、`A3 用例全绿 2/2`、`=== ALL BEHAVIOR TESTS PASSED ===`；输出留存 `...\scratch\_a3_green.out` |
| 3 | `python D:\deepseek-harness_workspace\maibot-plugins\scratch\smoke_test_cateye_test.py` | **exit 0**；`组件统计： 40 {'command': 12, 'hook_handler': 10, 'tool': 14, 'api': 4}`、`=== SMOKE TEST PASSED ===`；输出留存 `...\scratch\_a3_smoke.out` |
| 4 | 宿主取证命令 | `grep "工具调用" app_20261004_050153.log.jsonl`（窗口内 1 条=reply）；`grep -c "tool_search" / "emotion_report"` = 0/0；`grep -c "tool:emotion_report" decisions_2026-10-0{3,4,5}.jsonl` = 0/0/0；preview item 索引来自 `1791159241197.json`/`1791159271034.json` |

## 三、计划书修订明细（逐条）

1. §2.4.6 A3 行（原"不生效则清除；若保留则必须实现跨来源去重 | M5"）→ 重写为「已裁决（2026-10-05）：保留 + 跨来源去重已落地」，含取证路径（preview 两份 item 索引 + 回执）、口径（`cross_source_stream_window`、默认 180s、先入账者生效、模板事件不参与）、实现与用例位置，里程碑列标 `M5（✅ 2026-10-05）`。
2. §2.4.2 `cateye_test_emotion_report` 行尾追加"裁决落定（2026-10-05）：保留，去重已落地；可发现性取证=可用"。
3. §七 M5 行：状态 `🔶 部分落地` → `✅ 已落地（2026-10-05）`；正文"余：A3 裁决"改为"A3 已裁决：保留 + 跨来源去重已落地"。
4. §9.2 第 2 条进度："余：A3 工具裁决"改为"A3 工具裁决 ✅（2026-10-05）"。
5. `emotion/INTEGRATION.md` 两处模块表补记 + 新增去重小节（口径/返回/工具文本/兼容）。

## 四、本次会话的裁量（可被用户否决）

1. **去重优先级取"先入账者生效"**（时间对称、实现简单）：生产真实时序通常是"Planner 内工具上报（回合中）→ 回复生成后自动评估"，因此群聊 target 事件由工具侧（含档位加成 K）计入、自动评估被去重；若用户希望改为"工具恒定优先"或"自动评估恒定优先"，属策略切换而非结构改动。
2. **窗口默认 180s**、空流归 `-`；窗口内两条来源互斥即判重，极端情况下同流两件真实不同事件若恰被两条来源在 180s 内先后上报，后到者会被判重（事件指纹仅作审计留痕、不参与匹配，因两侧文本形态不同、指纹匹配在实践中不可靠）——已在文档写明。
3. **去重为内存态、不落盘**：与 A2 的"按日累计必须落盘"不同，此处丢状态只损失一次窗口内判重机会（无长期漂移）；宿主 file_watcher 热重载会清空登记表，行为已文档化。
4. **对外签名新增 `session_id` 且带默认值**（§八.2 兼容）：同步修改了 service/服务束两处与 facade 审计桩（既有用例 2 行同步），未放宽任何断言强度。
5. **§七 M5 行 🔶→✅**：因 A3 是 §9.2 所列 M5 最后一项，按"完成后勾选并回写 §七 状态列"执行；若用户认为 M5 还有其他未完项，可回退此勾选而不影响代码。
6. `work_report/README.md` 索引顺带补登此前遗漏的 20261005_0738 报告与本次报告。

## 五、遗留与未完成（含去向）

- **宿主级实机验证未做**：真实链路下"工具先/评估后"顺序的实测（含持久化上线后的行为与热重载清零）留待用户在场的宿主验证日（TEST_PROCEDURE 规程）；本次仅离线单测 + 只读宿主取证。
- **重复上报的实机端到端**：本次探针回合模型未调用该工具（自然结果），去重逻辑未在宿主机上被真实触发过；离线用例已覆盖两条方向与边界。
- 清退（§2.4.4 T1~T5）、§9.1 复验窗口、§9.3 定版：按原计划，去向定版流程。
- 宿主侧 reply 引用 harness 非数字消息 id 的发送失败：留作宿主验证日线索（非本插件产物）。

## 六、校验结果（跑了什么、退出码、数字是否与断言一致）

- behavior：**exit 0**，A3 2/2 GREEN，全套 `ALL BEHAVIOR TESTS PASSED`；实现前同命令 **exit 1**（A3-RED 2/2）——用例先行 RED→GREEN 链路完整。
- smoke：**exit 0**，组件总数 **40**（12 Command + 10 Hook + 14 Tool + 4 API），与断言一致；配置 13 段不缺默认值。
- 数字一致性：preview item 索引（54/52/53）、回执时间（07:45:29）、宿主日志工具调用数（窗口内 1 条 reply）、decision_logs `tool:emotion_report`=0 —— 均与文中断言一致。
# emotion/ — 情绪维护系统子模块（按《MaiBot情绪维护系统设计文档.md》实现）

> 来源：`D:\deepseek-harness_workspace\maibot-plugins\MaiBot情绪维护系统设计文档.md`
>（VAD 三维情绪模型 + 日常增量微调 + 睡眠期大修 Overhaul + 人设初始化）。
> 与其他两个复刻模块同构：独立总开关（`emotion.enabled`）、服务束、组件 Mixin、cateye_test 前缀组件名。

## 模块地图

| 文件 | 职责 | 设计文档章节 |
|---|---|---|
| `config.py` | emotion 配置段（总开关、衰减率、评估开关与节流、裁判/评估任务名、注入开关、睡眠检测间隔） | §十二 |
| `vad.py` | VADState/EmotionEvent 数据结构；25 种情绪原型（10 正/3 中/12 负）；加权欧氏距离映射（V1.0/A0.8/D1.2）+ 阈值复合标签 + 低分辨率三档描述 | §一/§四/§七 |
| `state_machine.py` | 外部状态机：日常指数衰减、事件结算（衰减→增量→裁剪→日志）、静态偏移+乘数（effective_vad，避免偏移每轮累加）、睡眠大修（锚点收敛 × 倒 U 恢复系数 + 裁判增量）、参数生效/恢复默认/序列化 | §二/§五/§九/§十 |
| `referee.py` | 三个独立 LLM 请求：睡眠裁判（±0.8 限幅 + reason 须引用日程原文）、人设初始化（锚点 ±0.6/偏移 ±0.3/增益 [0.8,1.3]/衰减 [0.6,1.2] 校验裁剪）、回复增量评估（±0.5）；宽松 JSON 解析 | §六/§十 |
| `service.py` | 状态持久化（emotion_state.json / emotion_config.json）、睡眠检测循环（消费 planning 日程的 sleep 条目，跨午夜时长计算）、前后日日程与长期状态（planning 的 custom_prompt）汇总、初始化指令主体、隐私接口（清空日志） | §五/§六/§十/§十三 |
| `components.py` | `EmotionComponentsMixin`：2 Command + 1 Tool + 3 HookHandler（方法名 `emo_` 前缀，组件名 `cateye_test_emotion_*`） | —— |
| `services.py` | `EmotionServices` 服务束（start/stop/set_persona 转发） | —— |

## 与其他模块的协作

- **planning/**：日程即"日程 LLM"——睡眠检测消费 `ScheduleQuery.find_current_activity` 的 `goal_type=="sleep"` 条目（时长 = start/end 分钟差，跨午夜自动 +1440）；睡眠裁判的前一日/第二日日程取 `format_day_summary`；长期状态取 `schedule.custom_prompt`；
- **宿主配置**：人设（personality.personality / bot.nickname）由顶层 `_refresh_persona` → `set_persona` 透传；
- **companion/**：互不依赖（各自独立 Hook，同名 Hook 点多 handler 顺序执行）。

## 职责划分（开环控制，设计文档 §二）

- **对话 LLM**：不可视 VAD 绝对值，只提供事件 → 增量（由 replyer.after_response 后的小模型评估请求实现，±0.5 限幅）；
- **睡眠裁判 LLM**：只接收客观数据（当前 VAD 用已应用偏移的有效值、当天原始事件日志、前后日日程、睡眠时长），输出 ΔVAD_sleep（±0.8）；
- **Python 状态机**：维护绝对数值、裁剪/衰减/偏移/乘数、睡眠锚点收敛。

## 有意的裁剪 / TODO

- 情绪增量评估为"回复后补一次小模型请求"（设计文档要求对话 LLM 直接输出 JSON ΔVAD，宿主 Hook 面拿不到结构化输出，此为等价实现），可关（`evaluate_on_reply`）；
- 中性情绪细分、人设动态偏移映射（§七.2.3）、planner 通道情绪注入、长期日程高压联动偏移调制（§九.4.4）未实现；
- 多模态情绪来源（语音/面部/生理）不在 MaiBot 插件能力范围，仅文本路径。

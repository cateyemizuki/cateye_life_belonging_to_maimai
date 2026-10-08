# 情绪映射表（EMOTION_MAPPING.md）

> **性质**：现状导出文档（**只读镜像，不是设计稿**）——把 `emotion/` 里散落在各常量的映射关系集中成一张可核对的表。
> 代码是唯一真源：本文件与代码不一致时，**以代码为准**，并回头修本文件。
> **快照时间**：2026-10-05（本机 UTC+8）｜**适用版本**：`_manifest.json version=0.4.0`、`[plugin].config_version=0.2.0`
> **上游**：设计与联动口径见 [MAINTENANCE_PLAN_10.05.md](MAINTENANCE_PLAN_10.05.md) §四（情绪系统）、§6.2（联动通道）、§6.3（非决定性七原则）、附录 C（默认参数）；
> 命令与工具见 [USAGE.md](USAGE.md)；测试规程见 [TEST_PROCEDURE.md](TEST_PROCEDURE.md)。
> **来源文件**：`emotion/vad.py`｜`emotion/state_machine.py`｜`emotion/referee.py`｜`emotion/service.py`｜`emotion/bridge.py`｜`emotion/config.py`｜`companion/patrol.py`｜`companion/affection_events.py`｜`companion/relation.py`｜`qzone/service.py`
>
> **M5（emotion 侧）已落地（2026-10-05）**：睡眠结算已由"入睡瞬时大修"改为"冻结 → 醒后一次性应用"；
> 大修限幅由固定 ±0.8 改为动态 ±(0.4+0.4×`schedule.emotion_influence`)；大修公式新增
> R_yesterday / E_tomorrow / 约定净兑现三项数值输入；日程条目 `emotional_tone`（裁剪 ±0.5）已
> 落 `parameters`；恢复系数下界 0.3 由常量固定。本文件 §5.3、§6.3、§7.3、§7.4、§8、附录 A 已同步。
> **M5（planning 侧）已落地（2026-10-05，同批）**：`emotion.overhauled` payload 补低分辨率三档词 `levels`；
> planning 订阅并缓存睡醒情绪（`PlanningServices.wake_emotion()`），日程生成把「睡醒情绪描述 + 引导约束」
> 按 `schedule.emotion_influence` 拼进 `emotion_context` 槽（`build_emotion_context()`）；
> 日程条目 `emotional_tone` 经 schema（逐维 ±0.5）+ `clip_emotional_tone` 落 `parameters`。
> **M5 连续低迷软约束已落地（2026-10-05，三批）**：每日 effective V 采样 → 日均线（滚动 7 天，落盘）→
> 连续「日均 V<−0.5」满 3 天 → 发 `emotion.low_mood_day`（payload `days/date/mean_valence`）；
> planning 消费后强制插入一次 rest/social 并在 prompt 注明「最近有点低落」。
> **M8 段 1 已落地（2026-10-07，附录定稿 §2.5 结算链路修复）**：醒沿顺序改为
> 「`flush_all_episodes("wake")` → 先解冻（`unfreeze(drain_to_log=True)`）→ 采集夜间事件摘要 →
> `apply_sleep_overhaul`」，摘要（`drained_events_count` / `drained_events`）随 `emotion.sleep_overhaul`
> 落 decision_logs；冻结态 `injection_text()` 追加一行深夜引导。本文件 §4.2、§7.3、§八 已同步。
> **M8 段 2 已落地（2026-10-07，附录定稿 §2.1/§2.2：夜间打扰判定与归属门控）**：`replyer.after_response`
> （三态，含 off）记账入口 `note_night_reply(session_id)`——只计 **target 私聊** reply；窗口 =
> `[最后一次 reply − emotion.interrupt_window_minutes, 最后一次 reply]` 滚动累计 ≥
> `emotion.interrupt_reply_threshold`（默认 3 次 / 10min）确认「吵醒态」，末次 reply ＋
> `emotion.resleep_minutes`（默认 5min）无新 reply 即回到睡眠；`[schedule].no_sleep_mode=true` 时
> 整条 L1 显式静默，非夜间睡眠周期（含小睡）不计入；每段闭合落一条 `emotion.sleep_interrupt`
> （§2.6 字段 + `confirmed_at`/`closed_at`/`close_reason`），段与计数只进内存 + decision_logs、
> 不写 `emotion_state.json`（F5）。本文件 §7.3 已同步。
> **M8 段 3 已落地（2026-10-07，附录定稿 §2.3/§2.4/§2.6：整夜合计结算与实际睡着时长）**：醒沿最终顺序
> = `flush("wake")` → `unfreeze` → 夜间事件摘要 → `apply_sleep_overhaul`（**恢复系数输入改为实际睡着
> 时长** = 窗口总长 − 打扰区间并集）→ **`sleep_interrupt` 整夜合计结算**；净正曲线
> `ΔV_i = max(0, 0.05−0.01×(i−1))`、整夜 `min(Σ, 0.15)`，经既有 `record_external_event`
> （K = target 档 × global）**每 `cycle_key` 结算 1 次**（结算即清空本周期归档），零 LLM；
> 留痕 `emotion.sleep_interrupt_settled`（含 `k`/`global_multiplier`/裁剪前·乘后·入账三值）与
> `emotion.sleep_overhaul` 的 `sleep_window_minutes`/`interrupted_minutes`/`interrupt_count`。
> 本文件 §5.1、§7.3、§八 已同步。
> **M9 段 4 已落地（2026-10-07，附录定稿 §四：L3 赖床提醒 + §4.5 提醒轮豁免；`27cf1d4`，
> 组件 28→29）**：打扰段以 `resleep` 闭合后按 §4.1 五条件发一条提醒（段已确认成立过 / 本周期有效
> 提醒次数 < `nap_reminder_max_per_day`（默认 2）/ 仍在睡眠窗口内（含赖床延长后的端点）/
> `no_sleep_mode=false` / `nap_reminder_enabled`），触发
> `proactive.trigger(intent="nap_reminder", metadata={"kind":"nap_reminder"})`、文本按 §4.3 模板
> 点名隐藏工具 `cateye_test_nap_lie_in`；提醒块过了提醒轮（块龄 >120s）由 `core/inject.py`
> `is_consumed_ephemeral_proactive_block` 从请求载荷剥离；发送成功后登记 §4.5 提醒轮豁免窗口
> （`NAP_REMINDER_ROUND_SECONDS`=120.0：窗口内该流意外 reply 不刷新 `resleep_minutes`、不重进吵醒态）。
> **M11 预算归属（2026-10-08，附录 II §3.3）**：赖床提醒**计入**「主动打扰预算」，且该预算**仅**按
> 目标陪伴私聊的 `stream_id` 维度计数——非目标流的段不计入、不受限；qzone 评论提醒**不再计入**该预算。
> **仍未落地（M5 其余部分）**：A3 `emotion_report` 工具裁决；A6 死字段清理已完成（`8d11f54`）。

---

## 目录

- [一、三维模型与数值约定](#一三维模型与数值约定)
- [二、25 原型情绪表（含中心点）](#二25-原型情绪表含中心点)
- [三、VAD → 情绪标签的映射算法](#三vad--情绪标签的映射算法)
- [四、标签与描述的输出格式（注入面）](#四标签与描述的输出格式注入面)
- [五、事件 → 情绪增量（ΔVAD）](#五事件--情绪增量δvad)
- [六、累积层：结算顺序 / 倍率 / 限幅 / 衰减](#六累积层结算顺序--倍率--限幅--衰减)
- [七、参数层：人设推导 / 睡眠大修 / 情绪→行为](#七参数层人设推导--睡眠大修--情绪行为)
- [八、未接线与待定（M5 相关）](#八未接线与待定m5-相关)
- [附录 A：默认参数速查](#附录-a默认参数速查)
- [附录 B：映射相关的文件位置索引](#附录-b映射相关的文件位置索引)

---

## 一、三维模型与数值约定

| 维度 | 含义 | 范围 | 极性 |
|---|---|---|---|
| **V** valence | 效价（愉悦度） | [-1.0, +1.0] | 正=愉悦，负=痛苦 |
| **A** arousal | 唤醒度 | [-1.0, +1.0] | 正=兴奋/紧张，负=平静/困倦 |
| **D** dominance | 支配感 | [-1.0, +1.0] | 正=掌控/自信，负=无力/顺从 |

三条结构性约定（都写在 `emotion/vad.py` / `state_machine.py` 顶部注释里）：

1. **开环控制**：LLM 只做"事件 → 增量"，**永不持有**情绪绝对值；绝对值（`vad_raw`）只由状态机维护。
2. **裁剪处处存在**：单维越界一律 `_clip` 回 [-1, 1]（`emotion/vad.py:23`）。
3. **LLM 隔离**：裁判/评估类 prompt 里**不出现**任何绝对 VAD 数值（只有低分辨率描述），防"模型看着数值凑答案"（MAINTENANCE_PLAN §6.3-6）。

### 1.1 单维五档描述（低分辨率化的唯一标准）

_level(value)`（`emotion/vad.py:157-167`）——判据是**闭/开区间**，实测边界如下：

| 档位文案 | 判据（代码原文） | 实测边界 |
|---|---|---|
| 很低 | `value <= -0.5` | -1.000 → 很低；-0.5 → 很低；-0.5001 → 很低 |
| 偏低 | `-0.5 < value <= -0.15` | -0.15 → 偏低 |
| 中等 | `-0.15 < value < +0.15` | -0.1499 → 中等；0.0 → 中等；+0.1499 → 中等 |
| 偏高 | `+0.15 <= value < +0.5` | +0.15 → 偏高；+0.4999 → 偏高 |
| 很高 | `value >= +0.5` | +0.5 → 很高；+1.0 → 很高 |

> ⚠️ 注意两处**不对称**（不是笔误，是代码现状）：下边界 `-0.5` 归入"很低"（含），上边界 `+0.5` 归入"很高"（含）——即五档左闭右开、两端各自含端点。

### 1.2 强度与内部量

| 量 | 公式 | 用途 |
|---|---|---|
| `VADState.intensity()` | `√(V² + A² + D²)` | 事件原始强度（`raw_intensity`，写进当天日志，供睡眠裁判判断"影响多大"） |
| `vad_raw` | 状态机内部累计值（未加偏移/乘数） | 衰减与增量都作用于它 |
| `effective_vad()` | `V_raw + offset` → 按符号乘 `positive_gain`/`negative_decay`（**只作用于 V**）→ 裁剪 | 一切对外呈现（注入 / Tool / mood_effect / 好感调制）都用它 |

---

## 二、25 原型情绪表（含中心点）

来源 `emotion/vad.py:81-109`：**10 基础 + 15 拓展 = 25 种**（按注释分组：基础 10 种 / 拓展 15 种；其中"平静 / 怀旧"等中性色，无独立"中性"分组标签）。
中心点 = 各维区间中点，映射时以**中心点**参与距离计算（不是区间）。

| # | 标签 | V 区间 | A 区间 | D 区间 | 中心点 (V, A, D) |
|---|---|---|---|---|---|
| 1 | 狂喜 | [+0.7, +1.0] | [+0.6, +1.0] | [+0.5, +1.0] | (+0.85, +0.80, +0.75) |
| 2 | 平静 | [+0.3, +0.8] | [-0.8, -0.2] | [+0.2, +0.7] | (+0.55, -0.50, +0.45) |
| 3 | 期待 | [+0.2, +0.8] | [+0.3, +0.7] | [-0.2, +0.4] | (+0.50, +0.50, +0.10) |
| 4 | 愤怒 | [-0.9, -0.3] | [+0.6, +1.0] | [+0.4, +0.9] | (-0.60, +0.80, +0.65) |
| 5 | 恐惧 | [-0.9, -0.4] | [+0.7, +1.0] | [-0.9, -0.4] | (-0.65, +0.85, -0.65) |
| 6 | 悲伤 | [-1.0, -0.5] | [-0.5, -0.1] | [-0.8, -0.3] | (-0.75, -0.30, -0.55) |
| 7 | 焦虑 | [-0.7, -0.2] | [+0.5, +0.9] | [-0.7, -0.2] | (-0.45, +0.70, -0.45) |
| 8 | 疲惫 | [-0.4, +0.1] | [-0.9, -0.4] | [-0.5, +0.0] | (-0.15, -0.65, -0.25) |
| 9 | 厌恶 | [-0.9, -0.4] | [+0.2, +0.6] | [+0.3, +0.8] | (-0.65, +0.40, +0.55) |
| 10 | 无奈 | [-0.5, -0.1] | [-0.4, +0.2] | [-0.9, -0.5] | (-0.30, -0.10, -0.70) |
| 11 | 骄傲 | [+0.6, +0.9] | [+0.4, +0.8] | [+0.6, +1.0] | (+0.75, +0.60, +0.80) |
| 12 | 嫉妒 | [-0.7, -0.3] | [+0.4, +0.7] | [-0.6, -0.2] | (-0.50, +0.55, -0.40) |
| 13 | 羞耻 | [-0.8, -0.4] | [+0.3, +0.6] | [-0.9, -0.6] | (-0.60, +0.45, -0.75) |
| 14 | 无聊 | [-0.3, +0.1] | [-0.8, -0.5] | [+0.0, +0.4] | (-0.10, -0.65, +0.20) |
| 15 | 好奇 | [+0.3, +0.7] | [+0.4, +0.7] | [-0.1, +0.4] | (+0.50, +0.55, +0.15) |
| 16 | 感激 | [+0.7, +1.0] | [+0.2, +0.6] | [-0.2, +0.3] | (+0.85, +0.40, +0.05) |
| 17 | 挫败 | [-0.6, -0.2] | [+0.5, +0.8] | [-0.5, -0.1] | (-0.40, +0.65, -0.30) |
| 18 | 释然 | [+0.4, +0.8] | [-0.6, -0.2] | [+0.3, +0.7] | (+0.60, -0.40, +0.50) |
| 19 | 轻蔑 | [-0.6, -0.2] | [+0.2, +0.5] | [+0.6, +0.9] | (-0.40, +0.35, +0.75) |
| 20 | 忧郁 | [-0.6, -0.2] | [-0.7, -0.3] | [-0.6, -0.2] | (-0.40, -0.50, -0.40) |
| 21 | 敬畏 | [+0.5, +0.9] | [+0.6, +0.9] | [-0.7, -0.3] | (+0.70, +0.75, -0.50) |
| 22 | 惊喜 | [+0.4, +0.8] | [+0.7, +1.0] | [-0.4, +0.2] | (+0.60, +0.85, -0.10) |
| 23 | 内疚 | [-0.8, -0.4] | [+0.3, +0.7] | [-0.7, -0.3] | (-0.60, +0.50, -0.50) |
| 24 | 冷漠 | [-0.4, +0.0] | [-0.9, -0.6] | [+0.3, +0.7] | (-0.20, -0.75, +0.50) |
| 25 | 怀旧 | [+0.2, +0.6] | [-0.3, +0.2] | [-0.2, +0.3] | (+0.40, -0.05, +0.05) |

**给 LLM 的摘要版**（`EMOTION_TABLE_SUMMARY`，砍掉区间只留中心点，控制 token；睡眠裁判与人设初始化 prompt 用）：

```
- 狂喜：V=+0.9，A=+0.8，D=+0.8   - 平静：V=+0.6，A=-0.5，D=+0.5   - 期待：V=+0.5，A=+0.5，D=+0.1
- 愤怒：V=-0.6，A=+0.8，D=+0.7   - 恐惧：V=-0.7，A=+0.9，D=-0.7   - 悲伤：V=-0.8，A=-0.3，D=-0.6
… （共 25 行，格式 `- 标签：V=±0.0，A=±0.0，D=±0.0`，一位小数）
```

**观察到的两处对称对**（读表时有用）：`期待(+0.50,+0.50,+0.10)` / `焦虑(-0.45,+0.70,-0.45)`、`惊喜(+0.60,+0.85,-0.10)` / `敬畏(+0.70,+0.75,-0.50)`——A 都高、D 都低，靠 V 的正负区分"好事将至"与"坏事将至"。

---

## 三、VAD → 情绪标签的映射算法

### 3.1 加权欧氏距离

```
d(vad, center) = √( 1.0·(V - cv)² + 0.8·(A - ca)² + 1.2·(D - cd)² )
```

| 项 | 值 | 来源 | 设计含义 |
|---|---|---|---|
| 权重 `VAD_WEIGHTS` | `(1.0, 0.8, 1.2)` | `emotion/vad.py:16` | D 权重最高——区分"恐惧 / 愤怒"这类同为高唤醒负效价、但支配感相反的情绪 |
| 距离阈值 `LABEL_DISTANCE_THRESHOLD` | **0.55** | `emotion/vad.py:18` | 只有 `d <= 0.55` 才认为命中该原型 |
| 最多标签数 `MAX_COMBINED_LABELS` | **3** | `emotion/vad.py:20` | 复合描述同时输出的上限 |

### 3.2 激活占比（复合标签）

```
hits = 距离最小的前 3 个里 d <= 0.55 的那些
share_i = (1/d_i) / Σ(1/d_j)        # 反距离归一化，Σ share = 100%
```

- 返回 `([(标签, 占比)], 最近标签)`；**空 hits** 时返回 `([], 最近标签)`。
- 占比只用于展示（`mood_summary` 取前 2 条），**不参与任何数值结算**。

### 3.3 实测校准（本文件编写时逐例跑过，可复现）

| 输入 VAD | 最近距离 | 命中（占比） | `mood_summary` 输出 |
|---|---|---|---|
| (+0.65, +0.70, +0.60) | 0.360 | 骄傲 41%、狂喜 39%、好奇 20% | `骄傲(41%)、狂喜(39%)。愉悦度：很高；唤醒度：很高；支配感：很高` |
| (-0.20, -0.35, -0.10) | 0.360 | 疲惫 40%、忧郁 31%、无聊 29% | `疲惫(40%)、忧郁(31%)。…` |
| (0.00, 0.00, 0.00) | 0.360 | **怀旧 100%** | `怀旧(100%)。愉悦度：中等；唤醒度：中等；支配感：中等` |
| (-0.75, +0.80, -0.70) | 0.360 | 恐惧 60%、羞耻 21%、内疚 19% | `恐惧(60%)、羞耻(21%)。…` |
| (+1.0, -1.0, -1.0) | 1.448 | **无命中** | `复杂难名。愉悦度：很高；唤醒度：很低；支配感：很低` |

> 两个值得知道的现状（不是缺陷，但读日志时会遇到）：
> ① **原点 (0,0,0) 映射为"怀旧"**（距离 0.437 ≤ 0.55）——未初始化时 `mood_text()` 会给出"怀旧"，而不是"中性/平静"；
> ② **极端角落会"复杂难名"**：如 `(-1,-1,-1)`（最近距离 0.835）、`(+1,-1,-1)`（1.448）、`(-1,+1,+1)`（0.582）全部超阈值——这是低分辨率兜底的设计意图（不给 LLM 硬贴标签）。

---

## 四、标签与描述的输出格式（注入面）

### 4.1 三种文本形态

| 函数 | 输出形态 | 消费方 |
|---|---|---|
| `low_resolution_description(vad)` | `愉悦度：{档}；唤醒度：{档}；支配感：{档}` | 只作为 mood_summary 的后半段 |
| `mood_summary(vad)` | `{标签1}({占比})、{标签2}({占比})。{低分辨率描述}`；无命中时前缀为 `复杂难名` | `mood_text()` |
| `EmotionService.injection_text()` | 见下（**唯一进 LLM 提示词的形态**） | planner / replyer 注入 Hook |

### 4.2 注入文本模板（逐字，`emotion/service.py` `injection_text()` / `NIGHT_INJECTION_HINT`）

```
【当前情绪底色】{mood_summary}。
要求：语气、措辞、节奏与该情绪保持一致；情绪会随时间自然衰减、随事件变化；严禁在回复中直接说出情绪标签词汇或任何数值；不要主动提及或解释本提示本身。
```

**冻结态追加行（M8 段 1，附录 §2.5.5；逐字）**——`machine.frozen` 为真时在上面两行**之后**追加一行，
非冻结态输出与追加前**逐字一致**（既有两个模板字节不得改动）：

```
（现在是深夜，你本该在睡觉。回复尽量短、慢、软一些；不要主动开启新话题；不要提及本提示。）
```

**三条硬约束（都在模板里明写）**：① 语气随情绪；② **严禁说出情绪标签词汇或任何数值**；③ 不得提及/解释该提示。
冻结态追加行的意图：夜里被吵醒后的回复应短/慢/软、且不主动开新话题（避免把深夜打扰变成新话题起点）。

注入位点：`maisaka.planner.before_request` 与 `maisaka.replyer.before_model_request` 两条 Hook，**插在第一条系统提示词之后**（`emotion/components.py:186/197`；replyer 侧 `attempt>1` 的重试请求不重复注入）。
注入开关：`[emotion].injection_enabled`（默认 true）；模块开关 `[emotion].enabled` 关掉后 Hook 直接 `continue`。

### 4.3 两个只读出口

| 出口 | 位置 | 说明 |
|---|---|---|
| `/cateye_test_emotion_config` | `emotion/components.py:74` | 打印 `params_summary()`：初始化态 / 锚点 / 偏移 / 乘数 / **当前 VAD（含偏移，带数值）** / 当天事件数 + 推导理由。**管理员限定**（默认拒绝） |
| ~~Tool `cateye_test_emotion_mood`~~ | ~~`emotion/components.py:99`~~ | **已移除（T6，2026-10-06，REGISTRY_DISPOSITION v2.1 §一）**：心情问答由情绪底色注入覆盖，`injection_enabled=false` 组合不支持心情问答（USAGE §五 已标注）；emoji 情绪联动保留在服务层（`EmotionService.emoji_for_mood`），当前无组件消费点 |

> 注意 `params_summary()` 是**唯一**向对话面暴露绝对 VAD 数值的地方，但它只发给管理员，且不进 LLM 上下文（是命令回复文本）——与 §1 的"LLM 隔离"不冲突。

---

## 五、事件 → 情绪增量（ΔVAD）

情绪增量的**全部来源只有两条**：① 规则/模板事件（零 LLM 成本）；② LLM 评估（有界）。下表是完整清单。

### 5.1 模板事件表（`emotion/bridge.py:37-43` + `service.py`）

| 事件来源（`source`） | 触发事件 | ΔVAD（模板） | K 归属 | 频控（发布方） | 零 LLM |
|---|---|---|---|---|---|
| `bridge:silence` | `relation.silence_detected` | **ΔV=−0.12，ΔA=+0.04，ΔD=0** | target（K=1+加成） | patrol：每日 ≤2 次、间隔 ≥4h、仅 target | ✅ |
| `bridge:commitment_kept` | `schedule.commitment_kept` | **ΔV=+0.20** | target | 每约定 1 次 | ✅ |
| `bridge:commitment_broken` | `schedule.commitment_broken` | **ΔV=−0.15** | target | 每约定 1 次 | ✅ |
| `bridge:qzone_comment` | `qzone.comment_received` | **ΔV ∈ [+0.05, +0.10] 随机取值** | **K≡1**（只乘全局倍率，不加成也不淡化） | qzone 回评轮次 | ✅ |
| `replyer` | `maisaka.replyer.after_response`（每轮回复后） | LLM 评估輸出（±0.5） | target / 非 target（按 session_id 判定） | `evaluate_min_interval_seconds`（默认 60s） | ❌ |
| `tool:emotion_report` | Tool `cateye_test_emotion_report(event, is_target)` | LLM 评估輸出（±0.5） | target / 非 target（按 `is_target` 参数） | 与回复评估共用同一 60s 节流 | ❌ |
| `sleep_interrupt` | **睡眠周期结束（醒沿，大修之后）** | **整夜合计净正**：单段 `ΔV_i = max(0, 0.05−0.01×(i−1))`，整夜 `min(Σ ΔV_i, 0.15)`（只作用 **V**，A/D 恒 0；`i` = 已确认打扰段序号，下限 0） | target（K=1+加成；被打扰**唯一归属** target 私聊） | **每 `cycle_key` 1 次**（结算即清空本周期归档；无打扰不入账、不落日志） | ✅ |

**只订阅不结算的事件**（仅 debug 日志，`IGNORED_EVENTS`，`emotion/bridge.py:46-52`）：`relation.interaction_event`、`relation.affection_tier_changed`、`relation.daily_reviewed`、`qzone.posted`、`qzone.comment_replied`。
**只记账不产生增量**：`relation.message_received`（`EmotionBridge.message_count` 计数，事件本身 0 增量——对话情绪走 replyer 自动评估主通道）。

### 5.2 target 归属判定（决定 K）

| 场景 | 判定方式 | 位置 |
|---|---|---|
| 私聊（自动评估路径） | `session_id == plugin._stream_id`（目标私聊 stream_id，由 `target.target_qq` 解析） | `service.py:329-337` |
| 群聊（工具上报路径） | Tool 参数 `is_target`（`is True` 才算；缺失/非 bool/垃圾值一律按非 target，**不报错**、仅 debug） | `emotion/components.py:151-156` |
| 取不到目标流 | 按非 target 计 | 同上 |

### 5.3 LLM 评估的输入输出契约（三处共用 `_delta_from` 二次限幅）

| 用途 | 函数 / event | 提示词要点 | 输出 JSON | 代码层限幅 |
|---|---|---|---|---|
| 回复后增量 | `evaluate_reply_delta` / `emotion_evaluate` | 看不到绝对 VAD；`event` ≤30 字（**代码截 60**）；无波动则接近 0 | `{"event": "...", "delta_vad": {V,A,D}}` | **±0.5** |
| 群聊工具上报 | 同上（`record_external_event` 的 LLM 分支） | 同上 | 同上 | ±0.5 |
| 睡眠大修 | `ask_sleep_referee` / `emotion_sleep_referee` | 见 §7.3（输入含前后日日程、睡眠时长、原型表、R/E/约定数值项；reason 必须引用原文） | `{"delta_vad": {…}, "reason_valence/arousal/dominance": "..."}` | **±(0.4+0.4×`schedule.emotion_influence`)**（服务侧传；缺省兼容 ±0.8） |
| 人设初始化 | `derive_params_from_persona` / `emotion_init` | 见 §7.1 | `{"anchor": {...}, "offset": {...}, "positive_gain", "negative_decay", "reasons"}` | 各参数区间钳制（§7.1） |

**解析容错**（`_parse_json_loose`）：整段 → ```json 围栏 → 首个 `{...}` 块，三级降级；全部失败按"请求失败"处理（静默跳过、下轮重试）。

**一处规格—实现偏差（如实记录）**：`EVALUATE_PROMPT` 要求 `event` **≤30 字**，但 `evaluate_reply_delta` 取用时截到 **60 字**（`referee.py:202`），`record_external_event` 再截到 **60 字**（`service.py:428`）。即 prompt 的 30 字约束只靠模型自觉，代码不强制。改不改待定，此处只做记录。

---

## 六、累积层：结算顺序 / 倍率 / 限幅 / 衰减

### 6.1 结算顺序（`state_machine.py:117-146`，与设计文档 §九.3 一致）

```
record_event(desc, delta):
    1) apply_daily_decay(now)                    # 先衰减：raw *= exp(-decay_rate·Δt)
    2) raw = clip(raw + delta)                   # 再加增量（逐维裁剪到 ±1）
    3) 追加当天日志（desc 截 200 字；日志上限 200 条，超出丢最旧）
effective_vad():
    4) v = clip(raw.V + offset.V) → 按符号乘 positive_gain / negative_decay → 再裁剪
    5) a = clip(raw.A + offset.A)；d = clip(raw.D + offset.D)     # 偏移与乘数只在对外结算时加
```

> 关键点：**偏移/乘数不写回 `vad_raw`**（否则每轮重复累加）；乘数**只作用于 V**，A/D 只吃偏移。

### 6.2 三层倍率（§6.2 A 加算倍率）

```
final_delta = delta × global_event_multiplier × K
K = 1 + target_bonus_step × (档位 + 1)     # target 事件（加算，非乘算）
K = non_target_multiplier                  # 非 target 事件
```

| 参数 | 默认 | 允许区间 | 代码夹取 | 备注 |
|---|---|---|---|---|
| `emotion.global_event_multiplier` | 1.0 | [0.1, 3.0] | `max(0.1, min(3.0, v))` | 作用于**一切**外部事件增量（不含衰减）；运行期总幅度旋钮 |
| `emotion.non_target_multiplier` | 1.0 | [0.0, 1.0] | `max(0.0, min(1.0, v))` | 想淡化群聊影响可设 0.8 |
| `emotion.target_bonus_step` | 0.1 | [0.0, 1.0] | `max(0.0, min(1.0, v))` | 加成 = step×(档+1) → 档 0~4 得 **+0.1 / +0.2 / +0.3 / +0.4 / +0.5** |
| 调用方 K | — | — | `EXTERNAL_K_MAX = 10.0` | 防御性上限（公式最大值 1+1×(4+1)=6，留余量） |

**target 加成的档位来源**：`companion` 好感 v2 的 `tier`（0~4，读 `plugin._affection.tier()`）；读不到 → 加成 0（即 K=1，不给加成）。

### 6.3 限幅（四道，全部在代码层）

| 层 | 限幅 | 位置 |
|---|---|---|
| 单事件（外部事件入口） | **±0.5** 每维（`EVENT_DELTA_CLAMP`） | `service.py:19 / :362-368` |
| LLM 输出（解析后） | 回复/工具 **±0.5**；睡眠裁判 **±(0.4+0.4×`schedule.emotion_influence`)**（0.4~0.8，默认 0.6→0.64；缺省调用仍 ±0.8） | `referee.py` `DEFAULT_SLEEP_DELTA_LIMIT` / `_delta_from(data, limit)` |
| 睡眠大修应用（状态机再夹一道） | `apply_sleep_overhaul(..., delta_limit=…)` 逐维夹 ±delta_limit；R/E 夹 ±0.5、约定项夹 ±0.3 | `state_machine.py`（M5 v2） |
| 单维状态 | 裁剪到 [-1, 1] | `vad.py:23` `_clip` |
| 好感日累计（相邻系统，§6.2 B 输出侧） | positive ≤ +4.0 / negative ≥ −3.0 | `companion/affection.py` |

### 6.4 衰减

```
factor = exp(-decay_rate × Δt_小时)      raw.V/A/D 各乘 factor
```

- `decay_rate`（`[emotion].decay_rate`）默认 **0.08**，区间 [0.01, 0.3]，状态机初始化时再夹一次（`state_machine.py:98`）。
- 含义：**半衰期 ≈ 8.7 小时**（`ln2 / 0.08`）；衰减只朝 0（原点）回落，**不是**朝人设锚点——向锚点收敛只发生在睡眠大修（§7.3）。
- 触发点：每次 `record_event` 前（惰性衰减，无独立定时器），以及每次 `effective_vad()` 读取时**不**衰减（读不改状态）。

### 6.5 决策日志的映射留痕（可回放）

emotion.external_event` 一条记录里同时落：`k`（调用方 K）、`global_multiplier`、`multiplier`（= K×global，最终总倍率）、`delta_before` / `delta_scaled` / `delta_applied`（裁剪前后）、`clipped`、`tier`、`target_bonus`、`non_target_multiplier`、`is_target`、`source`、`llm`。
—— 这就是 MAINTENANCE_PLAN §6.3-7「全链可观测」的情绪侧落地形态；调参时按这三个 delta 字段对账。

M8 段 3 的 `emotion.sleep_interrupt_settled` 复用同一三值口径（`delta_before` / `delta_scaled` /
`delta_applied` 直接取自本次 `record_external_event` 的回执，故与 `emotion.external_event` 行逐值一致），
并补整夜合计的 `delta_v`（曲线合计，乘倍率前）/ `delta_v_applied`（实际入账净 V）与
`k` / `global_multiplier` / `multiplier`（§6.2 A 三层倍率对账）。

---

## 七、参数层：人设推导 / 睡眠大修 / 情绪→行为

### 7.1 人设初始化参数（锚点 / 偏移 / 乘数）

由 `/cateye_test_emotion_init` 触发（管理员命令）→ `derive_params_from_persona` → 落 `emotion_config.json`。

| 参数 | 含义 | LLM 要求区间 | **代码钳制区间** | 默认/中性值 |
|---|---|---|---|---|
| `anchor` (V, A, D) | 情绪锚点（长期底色；睡眠大修向它收敛） | [-0.6, +0.6] 每维 | `_bounded(v, -0.6, 0.6)` | 0, 0, 0 |
| `offset` (V, A, D) | 静态偏移（对外结算时加） | [-0.3, +0.3] 每维 | `_bounded(v, -0.3, 0.3)` | 0, 0, 0 |
| `positive_gain` | V>0 时乘数（开心更开心 / 更收敛） | [0.8, 1.3] | `_bounded(v, 0.8, 1.3, 1.0)` | 1.0 |
| `negative_decay` | V<0 时乘数（难过更持久 / 更快消退） | [0.6, 1.2] | `_bounded(v, 0.6, 1.2, 1.0)` | 1.0 |
| `reasons` | 三类推导理由（引用人设原文） | — | 每条截 400 字 | {} |
| `persona_digest` | 人设摘要 | — | 截 500 字 | "" |
| `initialized` | 是否已初始化 | — | bool | false |

- 初始化后 `apply_params()`：**当前 VAD 直接设为锚点**、清空情绪日志（`state_machine.py:178-183`）。
- `/cateye_test_emotion_config reset` → `reset_params()`：零锚点 / 零偏移 / 中性乘数、VAD 归零。
- 落盘：`emotion_config.json`（参数）+ `emotion_state.json`（运行时）；启动时**以 config 文件为准**（防 state 里的旧参数覆盖初始化结果，`service.py:46-50`）。
- 未初始化时的实测后果：参数全中性 → `effective_vad = (0,0,0)` → `mood_text()` = **"怀旧(100%)。愉悦度：中等；…"**（见 §3.3 观察 ①）。

### 7.2 情绪 → 行为输出 `mood_effect()`（附录 C）

EmotionService.mood_effect()`（`service.py:116-177`）返回 4 个字段，**纯规则、零 LLM 成本**；模块停用/异常时返回恒等中性值（`{"proactive_rate":1.0,"miss_threshold":1.0,"post_rate":1.0,"intent_hint":""}`）。

| 字段 | 判据（用 `effective_vad` 的 V，A 仅用于第一行） | 输出 | 夹取范围 |
|---|---|---|---|
| `proactive_rate`（patrol 主动概率乘数） | `V > +0.3 且 A > +0.2` → 1.2；`V < −0.3` → 0.8；否则 1.0 | 1.2 / 1.0 / 0.8 | [0.7, 1.3] |
| `miss_threshold`（想念阈值乘数） | `V < −0.3` → 0.8；`V > +0.3` → 1.1；否则 1.0 | 0.8 / 1.0 / 1.1 | [0.7, 1.3] |
| `post_rate`（qzone 发帖率乘数） | `V < −0.6` → **0**；`−0.6 ≤ V < −0.3` → 0.8；`V > +0.3` → 1.2；否则 1.0 | 0 / 0.8 / 1.0 / 1.2 | [0.0, 1.2] |
| `intent_hint`（语义行，拼进 intent 的 reason） | `V < −0.3` → "你今天有点低落，语气可以软一点"；`V > +0.3` → "你今天心情不错，语气可以轻快一点"；否则 **空串** | 字符串 | — |

**消费点（逐条，含乘后夹取）**：

| 消费方 | 怎么用 | 位置 |
|---|---|---|
| patrol 主动概率 | `activity_trigger_rate × proactive_rate`、`default_speak_rate × proactive_rate`（掷骰前） | `companion/patrol.py:251-262` |
| patrol 想念阈值 | 在 `[miss_hours_min, miss_hours_max]` 内采样后 × `mood_effect.miss_threshold` × **affection 档位系数**（双系数） | `patrol.py:788-793 / :808-819` |
| patrol intent 文案 | `reason` 后追加 `。{intent_hint}`（空串则原样，不改既有字节） | `patrol.py:755-764` |
| qzone 发帖门控 | `post_rate × mood_effect.post_rate`；且 `post_rate<=0` 且 `[qzone].emotion_filter=true` → 本轮跳过发帖，记 `skip` 原因"情绪低落（V<−0.6）…" | `qzone/service.py:653-661` |
| 恒等降级 | patrol / qzone 各自持一份 `NEUTRAL_MOOD_EFFECT`，emotion 缺失/停用/异常时用（`patrol.py:53-56`、`qzone/service.py:81-83`） | 同上 |

**想念阈值的 affection 档位系数**（`companion/relation.py:107-113`；× `miss_hours_min/max`）：

| 关系 | 档 0 | 档 1 | 档 2 | 档 3 | 档 4 |
|---|---|---|---|---|---|
| 恋人 | 1.4 | 1.2 | 1.0 | 0.8 | 0.6 |
| 挚友 | 1.5 | 1.3 | 1.1 | 0.9 | 0.7 |
| 维护者 | 1.5 | 1.3 | 1.1 | 0.9 | 0.8 |
| 家人 | 1.4 | 1.2 | 1.0 | 0.9 | 0.8 |
| 自定义 | 1.5 | 1.3 | 1.1 | 0.9 | 0.7（同挚友） |

> 读法：系数 <1 = 阈值缩短 = **更快想念**；越亲密档位系数越小（恋人档 4 = 0.6，最快）。

### 7.3 睡眠大修（M5 起：**冻结 → 醒后一次性应用**；M8 段 1 起：**醒沿先冲刷片段 → 解冻 → 结算**；
M8 段 3 起：**恢复系数用实际睡着时长 → 整夜合计结算**）

**触发与冻结/结算时序**（`emotion/service.py` `_check_and_overhaul`，睡眠检测循环 `[emotion].check_interval_minutes` 默认 10 分钟）：

1. **进入睡眠时段** → `schedule.sleep_started` 上升沿 + `flush_all_episodes(reason="sleep_entry")`
   → `machine.freeze(now)`：冻结期**事件只入队**（`pending_events`，上限 200 条）、
   **衰减与评估结算暂停**（`apply_daily_decay` 恒返回 1.0 且不推进 `last_update`；
   `record_event` 不入 `vad_raw`）；
2. **冻结期**同一周期内计算大修（裁判 LLM）并**缓存**（`_pending_overhaul`，不落状态）；
   裁判失败保持冻结、下一轮重试（重试只需仍在睡眠周期内，不要求条目仍"当前"）；
3. **醒沿** = 离开睡眠窗口，或日程出现非睡眠的清醒活动（文档口径「醒来的第一个活动」），
   **且仅在睡眠周期真的结束的那一次 tick**（冻结中或本周期已发过 `sleep_started`；否则白天每个
   tick 都命中该分支，会让片段闲置机制失效）。固定顺序（M8 段 1 + 段 3，附录 §2.5/§2.3/§2.4）：
   0. **闭合本周期打扰段**（`close_night_interrupt_segment("wake")`，M8 段 2；放在最前，
      保证后续任一步抛错也不丢这条留痕）；
   1. **`flush_all_episodes(reason="wake")`**（醒沿先冲刷片段）—— 让「夜里最后一轮对话」当夜被评估
      而不是攒到下一夜。
      ⚠ 此刻机器**仍冻结**：冲刷产生的评估事件按 `record_event` 语义**只入 `pending_events`**
      （不直接改 VAD）。夜里被打扰的情绪后果由附录 §2.3 的 `sleep_interrupt` **整夜合计**统一入账，
      此处若绕过冻结入账会**双重记账**；
   2. `schedule.woke_up` 下降沿（边沿语义与 M5 相同）；
   3. **`unfreeze(now, drain_to_log=True)`**（先解冻，早于结算）→ 夜间事件并入当天日志；
   4. **采集「夜间事件摘要」**（条数 + 每条 `描述（ΔV/ΔA/ΔD）`；`_drained_events_summary`）；
   5. **应用缓存的大修一次**（`apply_sleep_overhaul`，**尾部照旧清空当天日志** ← M5 语义不变，
      故上一步的摘要是这批夜间事件在 decision_logs 里的唯一留痕）。**恢复系数输入 = 实际睡着时长**
      （M8 段 3：窗口总长 − 打扰区间并集，见下面「实际睡着时长口径」），计划整夜时长降为对照值
      `sleep_hours_planned`；
   6. 写 `applied_sleep_date` = **睡眠周期键** `"周期起始日@起始分钟"`（如 `2030-05-05@1380`；
      旧裸日期值仍兼容读取；同一周期不重复结算、且不误伤次夜）→ 发 `emotion.overhauled`；
   7. **`sleep_interrupt` 整夜合计结算**（M8 段 3，附录 §2.3/C2/C3；**在大修之后**）：
      `delta_v = min(Σ max(0, 0.05−0.01×(i−1)), 0.15)`（只作用 V）经**既有** `record_external_event`
      （`source="sleep_interrupt"`，`multiplier = target_multiplier(True)`，global 由入口内乘一次）
      入账，落 `emotion.sleep_interrupt_settled`，并**清空本周期打扰归档**（每 `cycle_key` 至多 1 次）；
      无打扰 → 不入账、不落 settled 日志。
      **顺序理由**：大修是"夜里决定次日情绪的主语"（§2.3 上限注释）——被打扰的暖意叠加大修**结果
      之上**；若在大修前入账，会被 `(raw − anchor) × recovery_factor` 的向锚点收敛二次稀释。

> **M8 段 1 修的是什么**（附录 §一 F3）：修复前顺序是「先应用大修（尾部清日志）→ 再解冻并入日志」，
> 于是①夜间事件永远不进本次结算视野（裁判增量在入睡时刻就缓存好了，读的是入睡前的日志）；
> ②被并入的夜间事件要等到**下一次**大修才连 ΔVAD 一起被清掉（夜里发生的事在情绪上等于没发生）。
> 修复后：冲刷发生在醒沿、解冻早于结算、摘要随 `emotion.sleep_overhaul` 落盘可追溯；
> **夜间事件的 ΔVAD 仍不进 VAD**（夜间打扰的入账归 §2.3 `sleep_interrupt` 整夜合计，本段不重复入账）。
> 段 1 披露的未闭环项 **「恢复系数仍取计划整夜时长」已由 M8 段 3 闭环**（附录 §2.5.4：改为实际
> 睡着时长，代码侧 `compute_sleep_overhaul` 的 `TODO(M8-3)` 已勾销）。

> ⚠️ **睡眠窗口按作息锚点，不按条目**（M5 验证者修复）：`_normalize_items` 会把入睡条目
> 截断到入睡锚点（`end=min(sleep_min,…)` 后 `max(start+15,end)`，**最长 23:00–23:15**），
> 夜间无任何条目覆盖——若按"条目窗口"冻结，入睡约 15 分钟即误判醒来、恢复系数恒落 <5h 档（0.30）。
> 故：**夜间睡眠**按 `[schedule].sleep_time → 次日 wake_time` 整夜冻结
> （`_sleep_entry_period` / `_night_window`），计划时长取整夜
> （默认 23:00→07:00 = 8h → 恢复系数 0.85）；**午休/小睡**（非夜间窗口的睡眠条目）才按条目窗口。
> 睡眠条目识别同时接受 `goal_type=="sleep"` 与名称含睡觉类关键词（日程生成 prompt 让"睡觉"
> 条目用 `goal_type: daily_routine`，仅按 goal_type 会漏判）；夜间启动/热重载后即使无条目，
> 处于锚点夜间窗口也会按周期进入冻结。

**三输入的日期基准与重算**（M5 修复）：入睡时刻（默认 23 点）读"次日"日程时，次日日程还没到生成时刻
（M10 起 = 最后日程结束 + `auto_generate_after_last_end_minutes`，缺省锚点等价旧 00:30；`auto_generate_time` 已废弃）→ **状态侧的 R_yesterday / E_tomorrow / 约定净兑现
在醒来时按入睡日为基准重算一次**（R = 入睡日−1；E = 入睡日的次日 = 醒来日，此时已生成；
_wake_side_inputs`），保证"明日预期"在生产时序下真实生效；入睡缓存值只作裁判 prompt 文本参考，
决策日志同时留痕 `*_cached` 与应用值（`emotion.sleep_overhaul_cached` / `emotion.sleep_overhaul`）。

**公式**（`state_machine.py` `apply_sleep_overhaul`，只作用 `vad_raw`）：

```
raw = anchor + (raw − anchor) × recovery_factor
      + ΔVAD_judge(±delta_limit)          # 动态限幅 ±(0.4+0.4×schedule.emotion_influence)
      + R_yesterday × 0.5                 # 前日情感残差（条目 emotional_tone 求和，逐维夹 ±0.5）
      + E_tomorrow  × 0.5                 # 明日情感预期（同上）
      + clamp(约定净兑现 × 0.1, ±0.3)      # 当日 kept − broken 次数（按桥描述前缀从当天日志统计）
```

**恢复系数（倒 U 型，`sleep_recovery_factor`；下界由 `MIN_SLEEP_RECOVERY_FACTOR` 恒 ≥0.3）**：

| 睡眠时长 | 系数 |
|---|---|
| < 5 h | **0.30**（= 下界） |
| 5 ~ <6 h | 0.60 |
| 6 ~ 9 h（含 9） | **0.85**（最佳恢复） |
| > 9 ~ 10 h | 0.70 |
| > 10 h | 0.50 |

**实际睡着时长口径（M8 段 3，附录 §2.4 / 用户裁决 A4；替代计划时长进上面的系数表）**：
纯函数 `emotion.service.summarize_sleep_window(window_start, window_end, segments, resleep_minutes)`
（无副作用、可直测；窗口相对分钟只作演示）——

```
打扰区间_i = [首次 reply_i, 最后一次 reply_i + resleep_minutes]   # resleep 缓冲计入被吵醒时长
            ↑ 逐段封顶到睡眠窗口 [window_start, window_end]
实际睡着时长 = (窗口总长 − |∪ 打扰区间_i|)                        # 并集：重叠/相邻只扣一次
```

| 项 | 口径 |
|---|---|
| 输入 ``segments`` | 本周期**已确认**（窗口计数达阈值）的打扰段（第 2 段内存归档；`awake=False` 的候选段不算被打扰、不扣时长） |
| ``∪`` 为空（无打扰/未达阈值） | 实际 = 窗口总长（与改造前一致） |
| 返回字段 | `sleep_window_minutes` / `interrupted_minutes` / `actual_sleep_hours` / `interrupt_count`（同批落 `emotion.sleep_overhaul` 与 settled 日志） |
| 退化行为 | 窗口倒挂 → 三个时长全 0；`sleep_hours = 0` 时系数仍取**下界 0.30**（`MIN_SLEEP_RECOVERY_FACTOR` 不变） |

> `compute_sleep_overhaul`（入睡/重试）仍用**计划**整夜时长做裁判 prompt 与缓存对照；醒沿
> `_apply_cached_overhaul` 现算实际值（`sleep_hours_planned` 留痕对照），`TODO(M8-3)` 已勾销。

**副作用**：大修后**清空当天情绪日志**（`emotion_log = []`；M8 段 1 起解冻（夜间事件并入）发生在结算**之前**，
故清空后日志为空——夜间事件只以「摘要」形式留在 decision_logs，不再被带进下一夜的裁判输入），
并把 `sleep_hours`（M8 段 3 起 = **实际**睡着时长）/ `recovery_factor` / `before` / `after_raw` /
`delta_requested` / `delta_applied` / `delta_limit` / `r_yesterday` / `e_tomorrow` / `commitment_net` /
`commitment_delta` / `log_cleared` / **`drained_events_count` / `drained_events`** /
**`sleep_hours_planned` / `sleep_window_minutes` / `interrupted_minutes` / `interrupt_count` / `cycle_key`**
写进返回，
并以 `emotion.sleep_overhaul` 落决策日志（§6.3-7 可观测）；`drained_events` 是解冻队列的紧凑摘要
（`描述（ΔV/ΔA/ΔD）`，条数上限 20，`drained_events_count` 报真实条数）。

> ⚠️ **修复状态（2026-10-07，M8 段 1 + 段 3）**：本节描述的「先结算后并入、下次再清空」缺口（附录 §一 F3）**已修**——
> 醒沿改为 `flush_all_episodes("wake")` → `unfreeze(drain_to_log=True)` → 采集夜间事件摘要 →
> `apply_sleep_overhaul`（见上面第 3 步的小步），夜间事件不再"等于没发生"：它们在被清空前被压成摘要
> 随 `emotion.sleep_overhaul` 落 decision_logs（`drained_events_count` / `drained_events`），且醒沿冲刷
> 使「夜里最后一轮对话」当夜被评估。段 1 披露的两项未闭环**均已闭环（M8 段 3）**：①恢复系数改用
> **实际睡着时长**（附录 §2.5.4，取 §2.4 的扰段并集口径，代码侧 `TODO(M8-3)` 已勾销）；②
> `sleep_interrupt` 整夜合计入账与 §5.1 模板事件登记（见下）。
> 依据文档：`D:\deepseek-harness_workspace\maibot-plugins\MAINTENANCE_PLAN_APPENDIX_20261007_0155_睡眠打扰与早晚安漂移.md`
> §2.5（该附件 §一 的 F3/F4；**定稿版**，草稿 `…_0114_ARCHIVED_草稿v0.1_…md` 已归档、非执行依据）。
> 指针若失效：在 `D:\deepseek-harness_workspace\maibot-plugins\` 下检索 `睡眠打扰与早晚安漂移` 定位。

**夜间打扰判定（M8 段 2 落地，附录 §2.1/§2.2）**：`replyer.after_response`
（`episode`/`per_reply`/`off` 三态）在模式分发前调 `EmotionService.note_night_reply(session_id)`
（`EmotionServices.note_night_reply` 转发）——只登记**真的 reply**，只触发 planner 的轮次不算。
**归属门控**：仅 `is_target_session()` 命中的 target 私聊计入（群聊/其他私聊不计数、不重置计时）；
计数**跨聊天流**合并。**生效门控**：`[schedule].no_sleep_mode=true` 时整条 L1 显式静默（附录 F1），
且仅当处于**夜间睡眠周期**内（`_in_sleep` / `_sleep_period` + 作息锚点整夜窗口；小睡不计入）才计数。
**口径**（`interrupt_reply_threshold` = 3 / `interrupt_window_minutes` = 10 / `resleep_minutes` = 5）：
窗口 = `[最后一次 reply − 窗口, 最后一次 reply]` 滚动，窗内累计 ≥ 阈值 → 确认「吵醒态」（`confirmed_at`）；
末次 reply + `resleep_minutes` 无新 reply → 回到睡眠（闭段）。**留痕**：每次**段闭合**（resleep 到期 /
睡眠窗口结束 / 醒沿）落一条 `emotion.sleep_interrupt`（`cycle_key` / `started_at` / `last_reply_at` /
`reply_count` / `is_target` / `stream_id` + `stream_ids` / `window_reply_count` / `confirmed_at` /
`closed_at` / `close_reason`；每段至多一条）；段与计数**只进内存 + decision_logs**，不写
`emotion_state.json`（附录 F5/§八.4）。

**整夜合计结算（M8 段 3 落地，附录 §2.3/§2.6；用户裁决 C2/C3）**：醒沿**大修之后**一次性入账
（`EmotionService.settle_night_interrupts`，由 `_check_and_overhaul` 醒沿分支调用）——
`n` = 本周期已确认打扰段数；单段 `ΔV_i = max(0, 0.05−0.01×(i−1))`（首档 +0.05、每次递减 0.01、下限 0），
整夜 `delta_v = min(Σ ΔV_i, 0.15)`（上限远小于单事件 ±0.5 硬限幅——次日情绪的主语仍是睡眠大修）；
经**既有** `record_external_event(source="sleep_interrupt", multiplier=target_multiplier(True))` 入账
（global 由入口内乘一次，§6.2 A 三层倍率同源，**不新开路径**、**零 LLM**）；
`emotion.sleep_interrupt_settled` 留痕 `cycle_key` / `interrupt_count` / `sleep_window_minutes` /
`interrupted_minutes` / `actual_sleep_hours` / `delta_v`（曲线合计） / `delta_v_applied`（实际入账净 V） /
`k` / `global_multiplier` / `multiplier` / `delta_before`·`delta_scaled`·`delta_applied`（与
`emotion.external_event` 同语义的裁剪前/乘后/入账三值）/ `record_ok` / `stream_id` / `reply_count`；
**每 `cycle_key` 至多 1 次**（结算后清空本周期归档；无打扰 → 不入账、不落 settled 日志）。

**裁判输入**：人设 + **当前 VAD 绝对值**（唯一给 LLM 看绝对值的场合，因为它是裁判不是对话模型）+ 当天日志 + **前一天日程** + **第二天日程** + 长期状态 + 预计睡眠时长 + 25 原型表 + **R_yesterday / E_tomorrow / 约定净兑现三组数值项**（§4.2 输入扩展）；prompt 内嵌评分规则 7 条（近因效应、时长倒 U、日程"释怀/反刍/期待/焦虑"、人设情绪过滤、三输入为参考、ΔVAD ∈ [-limit, limit]、reason 必须引用原文）。
**已实测的失败模式**：`max_tokens` 给 600 会被三组长 reason 截断（宿主验证日 8 次尝试零落地）→ 现为 **2000**；思考型模型仍可能挤爆预算，裁判任务必须绑真非思考模型（MAINTENANCE_PLAN §八.8）。

### 7.4 睡眠进出事件（M0 发边沿；M5 起与冻结/结算绑定）

| 事件 | 触发 | payload |
|---|---|---|
| `schedule.sleep_started` | 进入睡眠时段的**上升沿**（每段睡眠只发一次；同时进入冻结） | `sleep_hours`（计划时长：**夜间取作息锚点整夜**，如 8.0；小睡/直接调用仍按条目时长）、`date` |
| `schedule.woke_up` | 离开睡眠条目的**下降沿**（随后应用缓存大修并解冻） | `sleep_hours`（实际观测时长）、`date` |
| `emotion.overhauled` | 醒后**应用缓存大修**（一次）后 | `mood`（低分辨率描述文本）、**`levels`**（V/A/D 三档词：很低/偏低/中等/偏高/很高）、`recovery_factor`、`sleep_hours`（M8 段 3 起 = **实际睡着时长**，即进恢复系数的那个值）；消费方：planning（`emotion_context` 槽，M5）、qzone、日志 |
| `emotion.updated` | 每次 VAD 结算后（回复评估 / 外部事件 / 睡眠大修） | `mood`、`trigger` |
| `emotion.low_mood_day` | 日切评估时连续「日均 V<−0.5」满 3 天（每日最多一次）；**连续按日历逐日补评**（无采样日 → 当日按非低迷归零，不拼假连续）；`date` 恒为**昨天**（新鲜） | `days`（连续天数）、`date`（最后达标日）、`mean_valence`（该日日均线 V）；消费方：planning（强制插入一次 rest/social + prompt 注明「最近有点低落」；事件缓存超 72h 视为陈旧不消费） |

### 7.5 情绪 → 好感度（§6.2 B，情绪调制）

```
delta_affection_final = delta × clamp(1 + 0.25 × V_current × sign(delta), 0.6, 1.4)
```

| 项 | 值 | 来源 |
|---|---|---|
| `EMOTION_MODULATION_K` | 0.25 | `companion/affection_events.py:97` |
| 乘数夹取 | [0.6, 1.4] | `affection_events.py:98-99` |
| `V_current` 来源 | `AffectionManager.emotion_valence()` ← `EmotionService.effective_valence()` | `affection.py:401`、`service.py:100-105` |
| 开关 | `[affection].emotion_modulation`（默认 true）；关或 V 不可得 → 乘数恒 1.0 | `affection_events.py:125-159` |
| 留痕 | 决策日志带 `multiplier` 与 `valence` | `affection_events.py:287-297` |

**行为含义**：开心时正事件加分更多、负事件更宽容；低落时反之（`sign(delta)` 让正负事件各自同向缩放，不出现"心情差所以骂人也不扣分"）。

---

## 八、未接线与待定（M5 项已闭环；9~11 为追加计划书新增项）

| # | 项 | 现状 | 去向 |
|---|---|---|---|
| 1 | 睡眠结算（冻结 / 醒后应用） | ✅ **已实现（2026-10-05）**：进 sleep 条目冻结（事件只入队、暂停衰减/结算）→ 冻结期计算并缓存大修 → 离开 sleep 应用一次并解冻 | — |
| 2 | 睡眠大修限幅动态化 | ✅ **已实现**：±(0.4 + 0.4×`schedule.emotion_influence`)，默认 0.6→±0.64（旧固定 ±0.8 仅作缺省调用兼容） | — |
| 3 | 日程条目 `emotional_tone` → 数值输入 | ✅ **已实现**：schema 增可选字段（逐维 ±0.5）+ `clip_emotional_tone` 裁剪入库 `parameters`；睡眠侧求和为 R_yesterday/E_tomorrow（再夹 ±0.5）进入 v2 公式 | — |
| 4 | 恢复系数下界 | ✅ **已固定**：`MIN_SLEEP_RECOVERY_FACTOR = 0.3`，各时长档全部 ≥0.3（<5h = 0.30） | — |
| 5 | 连续低迷软约束 `emotion.low_mood_day` | ✅ **已实现（2026-10-05）**：日采样/日均线/连续计数（`sample_daily_v`/`daily_v_mean`/`update_low_mood_streak`，同日幂等）→ 满 3 天发事件；planning 缓存并强制插入一次 rest/social + prompt 注明 | — |
| 6 | `planner` 注入与情绪底色融合 | ✅ 已接线（M4 删 `state_analyzer` 静态模板，改读 `mood_text()`）；**M5** 另把睡醒情绪按 `schedule.emotion_influence` 注入日程生成 prompt 的 `emotion_context` 槽（`build_emotion_context()` + `PlanningServices.wake_emotion()`） | — |
| 7 | `EVALUATE_PROMPT` 30 字 vs 代码截 60 字 | 规格—实现不一致（§5.3） | 待裁决（可不改） |
| 8 | 白天事件对**锚点**的回归 | 日常衰减只朝 0，不朝锚点 | 设计如此（锚点只在睡眠大修收敛）；若将来要改，属设计变更 |
| 9 | **夜间事件不进结算**（睡眠期事件被"先结算后并入、下次再清空"，见 §7.3 缺口警示） | ✅ **已闭环（2026-10-07，M8 段 1 + 段 3）**：醒沿改「先冲刷片段 → 先解冻再结算 → 采集夜间事件摘要落 `emotion.sleep_overhaul`」（§7.3 第 3 步 / §4.2 追加行）；恢复系数改用**实际睡着时长**（§2.5.4 扰段并集口径；代码 `TODO(M8-3)` 已勾销）；`sleep_interrupt` 整夜合计入账（§2.3/C2/C3）与 §5.1 模板事件登记同批落地 | —（附录 §2.5.1~2.5.5 + §2.3/§2.4 全落地） |
| 10 | **睡眠期注入无深夜措辞**（夜间回复与白天同构） | ✅ **已实现（2026-10-07，M8 段 1）**：`injection_text()` 在 `machine.frozen` 为真时于既有文本后追加深夜引导一行（逐字见 §4.2）；非冻结态输出与追加前逐字一致 | —（附录 §2.5.5 已落地） |
| 11 | **睡眠质量按计划时长 + 打扰情绪无归宿**（原口径缺陷） | ✅ **已实现（2026-10-07，M8 段 3）**：`summarize_sleep_window` 纯函数给出「窗口总长 − 打扰区间并集」（逐段封顶、重叠只扣一次）；恢复系数输入改实际值（计划值留 `sleep_hours_planned`）；整夜合计一次性入账（`emotion.sleep_interrupt_settled`：曲线 `max(0, 0.05−0.01×(i−1))`、整夜钳 0.15、每 `cycle_key` 1 次、大修之后、零 LLM、结算即清档） | —（附录 §2.3/§2.4/§2.6 已落地） |

---

## 附录 A：默认参数速查

| 参数（`[emotion]` 段） | 默认 | 区间 | 作用于 |
|---|---|---|---|
| `enabled` | true | — | 模块总开关（关 → Hook 直接 continue、mood_effect 恒等） |
| `decay_rate` | 0.08 | [0.01, 0.3] | 每小时指数衰减（半衰期 ≈8.7h） |
| `evaluate_mode` | "episode" | episode / per_reply / off | 回复后评估模式：episode=会话闲置后整段评估（默认，成本封顶）｜per_reply=每回复评估（旧模式，60s 节流）｜off=不评估（T7） |
| `episode_idle_minutes` | 10 | [1, 720] | episode 模式：会话闲置多少分钟后冲刷该流片段（T7；扫描精度=睡眠检测循环间隔） |
| `evaluate_max_per_day` | 10 | [0, 200] | 每日情绪评估上限（次，episode/per_reply 共用；0=当日不评估；超限片段丢弃不并入次日，落 `daily_cap` 日志）（T7） |
| `evaluate_on_reply` | true（**已废弃**） | — | 迁移用：未显式设置 `evaluate_mode` 时 true→episode / false→off；显式设置 `evaluate_mode` 后本字段被忽略（T7） |
| `evaluate_min_interval_seconds` | 60 | [0, 3600] | LLM 评估节流（回复评估与工具上报共用） |
| `evaluator_task_name` | "utils" | — | 回复评估 / 工具上报 / 人设初始化用的任务 |
| `referee_task_name` | "replyer"（**类默认**；部署 config 实测为 "utils"） | — | 睡眠裁判任务 |
| `global_event_multiplier` | 1.0 | [0.1, 3.0] | 一切外部事件增量的总幅度 |
| `non_target_multiplier` | 1.0 | [0.0, 1.0] | 非 target 事件的 K |
| `target_bonus_step` | 0.1 | [0.0, 1.0] | target 加成步长（档 0~4 → +0.1~+0.5） |
| `injection_enabled` | true | — | 情绪底色注入 |
| `check_interval_minutes` | 10 | [1, 120] | 睡眠检测循环间隔 |

**代码常量（不可配，改需改代码）**：`VAD_WEIGHTS=(1.0,0.8,1.2)`｜`LABEL_DISTANCE_THRESHOLD=0.55`｜`MAX_COMBINED_LABELS=3`｜`EVENT_DELTA_CLAMP=0.5`｜`EXTERNAL_K_MAX=10.0`｜`MOOD_EFFECT_MIN/MAX=0.7/1.3`｜`MOOD_POST_RATE_MAX=1.2`｜`MAX_LOG=200`｜沉默事件 ΔV/ΔA = −0.12/+0.04｜约定兑现/违约 = +0.20/−0.15｜空间评论 ΔV ∈ [0.05, 0.10]｜好感调制 k=0.25、clamp 0.6~1.4。
**M5 连续低迷常量**：`LOW_MOOD_THRESHOLD=−0.5`（日均线 V 阈值）｜`LOW_MOOD_MIN_DAYS=3`（连续天数门槛；planning 侧 `prompt_builder.LOW_MOOD_MIN_DAYS` 同值，行为用例断言两侧一致防漂移）｜`DAILY_V_KEEP_DAYS=7`（日采样桶滚动保留，也是逐日补评的窗口上限）｜状态 JSON 新增 `daily_v`/`low_mood_streak`/`low_mood_last_eval_date`/`low_mood_checked_date`（旧文件缺键 → 默认空，向后兼容）。
**「日」口径（连续低迷）**：日均线 = **清醒时段**的 effective V 采样均值（每次检测 tick 采样一次；**冻结的睡眠期不采样**，避免 23:00→07:00 的整夜冻结值稀释）；判定按**日历逐日**推进——缺失/无采样日按非低迷并归零，因此"连续 N 天"必须是紧邻的 N 个自然日。
**M5 新增常量**：`MIN_SLEEP_RECOVERY_FACTOR=0.3`（恢复系数下界）｜`SLEEP_RESIDUAL_WEIGHT=0.5` / `SLEEP_EXPECTATION_WEIGHT=0.5`（R/E 数值项权重）｜`SLEEP_COMMITMENT_WEIGHT=0.1` / `SLEEP_COMMITMENT_CLAMP=0.3`（约定净兑现项）｜`SCHEDULE_TONE_CLAMP=0.5`（日程 tone 求和后逐维上限）｜`MAX_PENDING=200`（冻结期队列上限）｜`DEFAULT_SLEEP_DELTA_LIMIT=0.8`（裁判缺省限幅兼容值）。
**M5 新增配置**：`schedule.emotion_influence`（0~1，默认 0.6，0=关闭）——同时驱动两处：①睡眠大修裁判增量动态限幅 ±(0.4+0.4×值)；②日程生成 `emotion_context` 槽的强度（0=不注入；<0.5 精简为 1 条引导约束，≥0.5 完整）。

---

## 附录 B：映射相关的文件位置索引

| 内容 | 文件:行（v0.4.0 快照，重构后以符号名为准） |
|---|---|
| 三维约定 / 裁剪 / 强度 | `emotion/vad.py:16-53` |
| 25 原型表 | `emotion/vad.py:81-109` |
| 中心点 + LLM 摘要 | `emotion/vad.py:111-120` |
| 加权距离 / 映射 / 占比 | `emotion/vad.py:123-151` |
| 五档描述 / mood_summary | `emotion/vad.py:154-182` |
| 结算顺序 / 衰减 / 偏移乘数 | `emotion/state_machine.py:96-146` |
| 睡眠大修 + 恢复系数 | `emotion/state_machine.py:74-88, 149-175` |
| 参数约束与钳制 | `emotion/referee.py:59-70, 159-181` |
| 三个 prompt（睡眠/初始化/评估） | `emotion/referee.py:19-86` |
| 限幅与倍率实现 | `emotion/service.py:18-28, 290-384` |
| mood_effect 映射 | `emotion/service.py:116-177` |
| 睡眠循环与进出事件 | `emotion/service.py:490-582` |
| 模板事件表 | `emotion/bridge.py:36-52` |
| 配置段字段与 i18n | `emotion/config.py:17-102` |
| mood_effect 消费（patrol） | `companion/patrol.py:49-56, 251-262, 721-793` |
| mood_effect 消费（qzone） | `qzone/service.py:25-28, 78-83, 653-661` |
| 档位 → miss 系数 | `companion/relation.py:105-113` |
| 情绪调制好感（§6.2 B） | `companion/affection_events.py:96-159, 287-297` |

---

## 维护约定

1. **本文件是镜像**：改 `emotion/` 的任何映射常量、阈值、提示词契约，**同批**更新本文件对应小节 + 快照时间与版本。
2. 本文件属开发文档，**发布时排除**（同 `TEST_PROCEDURE.md`、`scratch/`，见开发文档陷阱 #23）。
3. 新增情绪原型 / 改权重 / 改阈值后，跑 `maibot-plugins\scratch\behavior_test_cateye_test.py`
   （映射用例在其中），并用配套导出脚本 `maibot-plugins\scratch\gen_emotion_mapping.py` 重新生成
   §二 的中心点表与 §3.3 的实测校准（该脚本用 `assert` 自校验中心点与代码一致，可直接重跑对账）。

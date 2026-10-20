# 映射表自定义指南（`mappings/`）

这个文件夹是插件**留给使用者改的配置区**。里面每个 `.json` 都是一张映射表，改完不用动任何代码。

---

## 一、这个文件夹是什么

### 5 张表各自管什么

| 文件 | 管什么 | 读它的代码 |
|---|---|---|
| `emotion_mapping.json` | bot 的情绪怎么算：情绪原型（VAD 三维区间）、事件对情绪的加减、三个倍率、情绪档位切分点 | `emotion/mapping_override.py` |
| `emotion_descriptions.json` | 每种情绪注入给 LLM 的**文字描述**（替代「百分比 + 三维约值」的干巴巴渲染） | `emotion/descriptions.py` |
| `affection_presets.json` | 关系预设 → 好感度档位区间（总分 0~200，1~5 档自由构造） | `companion/affection_presets.py` |
| `affection_tier_descriptions.json` | 每个档位（如「热恋」「铁哥们」）注入提示词的那句话 | `companion/affection_tier_descriptions.py` |
| `schedule_properties.json` | **日程属性表**（2026-10-21 新增第五张）：每个日程「属性」的回复率调控倍率、属性关键词词表与展示标签/颜色 | `planning/activity_properties.py` |

五张表互相独立：改一张不会影响另外四张（按表的键联动的地方见各节说明）。

### 改了多久生效

**只在插件加载 / 热重载时读一遍**，之后常驻内存，不会每次对话都去读盘。

所以改法固定是：**编辑并保存 json → 触发热重载（或在宿主里重启插件）**。只保存不重载，改动不会生效。

读取入口：情绪两张在情绪服务构造时读（`emotion/service.py:211`、`emotion/service.py:226`），好感度两张由 `companion/services.py:42` 的 `_load_affection_tables` 读，插件加载时调一次（`companion/services.py:119`）、配置热重载时再调一次（`companion/services.py:222`）；**日程属性表**由日程服务束构造时装载（`planning/services.py` 规划束初始化里 `activity_properties.load_and_apply`，同款「加载/热重载各读一遍」），倍率消费点在 `planning/proactive_service.py` 的 `_apply_frequency`。

### 旧版文件会自动搬进来

老版本把两张情绪表放在**插件根目录**（就是 `mappings/` 的上一级）。现在读取位统一到了 `mappings/`：

- 新位置**缺失**、而旧位置 `emotion_mapping.json` / `emotion_descriptions.json` 存在 → 自动**复制**一份到 `mappings/`；
- 只复制，**不删除也不改写**旧文件（旧文件留原处，要不要删你自己决定）；
- 新位置已存在 → 什么都不做，以新位置为准。

（实现见 `core/mapping_dir.py:69`。好感度那两张表是 2026-10-17 新增的，没有旧位置。）

### 文件缺失会怎样

- 五张表都**不存在也不会报错**：`emotion_mapping.json` / `emotion_descriptions.json` / `affection_presets.json` / `affection_tier_descriptions.json` / `schedule_properties.json` 在缺失时会各自生成一份内置默认文件（内容就是出厂默认），然后按内置默认运行。所以第一版可以直接打开生成好的文件照葫芦画瓢。

---

## 二、情绪映射表 `emotion_mapping.json`

### 文件作用

决定 bot 的「情绪引擎」怎么跑：有哪些情绪、每个情绪落在 VAD（愉悦度 / 唤醒度 / 支配度）的哪一片区域、收到某类事件时情绪怎么变、情绪档位怎么切。

### 顶层结构与字段表

| 键 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `version` | **是** | 整数 | 必须恰为 `1` |
| `prototypes` | 否 | 数组 | 情绪原型，**10~40 条** |
| `template_events` | 否 | 对象 | 模板事件增量，**4 个事件全都要写** |
| `multipliers` | 否 | 对象 | 三层倍率，**3 个键全都要写** |
| `level_thresholds` | 否 | 数组 | 情绪档位切分点，**恰好 4 个数** |

**「存在即整表替换」**：某个表键只要写进 json，就必须写完整（表内漏键 = 校验失败），不会跟内置默认逐条合并。不写的表键则整张沿用内置默认。

`prototypes` 每条：

| 字段 | 类型 | 约束 |
|---|---|---|
| `label` | 字符串 | 去掉首尾空白后非空，且表内互不重复 |
| `valence` | 长度 2 的数值数组 `[lo, hi]` | `lo <= hi`（可以相等），两个数都在 `-1.0 ~ 1.0` |
| `arousal` | 同上 | 同上 |
| `dominance` | 同上 | 同上 |

（情绪实际落点取区间中点 `(lo+hi)/2`，见 `emotion/mapping_override.py:575`。）

`template_events` 的四个事件名固定，不能增删改名：

| 事件名 | 内容 |
|---|---|
| `relation.silence_detected` | 恰为 `valence` / `arousal` / `dominance` 三个数值 |
| `schedule.commitment_kept` | 同上 |
| `schedule.commitment_broken` | 同上 |
| `qzone.comment_received` | 恰为 `valence_min` / `valence_max` 两个数值，且 `min <= max` |

每个数值的 **绝对值必须 ≤ 0.5**（这是单次事件增量的硬限幅，代码里叫 `EVENT_DELTA_LIMIT`，与 `emotion/service.py:38` 的 `EVENT_DELTA_CLAMP` 同值）。

`multipliers` 三个键名逐字固定：

| 键 | 取值区间 | 说明 |
|---|---|---|
| `global_event_multiplier` | `[0.1, 3.0]` | 所有事件增量的总倍率 |
| `non_target_multiplier` | `(0.0, 1.0]` | 非目标用户的增量倍率，**下限开区间，写 0 不合法** |
| `target_bonus_step` | `(0.0, 1.0]` | 目标用户每档加成步长，**同样不收 0** |

`level_thresholds`：恰好 4 个数，每个在 `-1.0 ~ 1.0`，且**单调不减**（`c1 <= c2 <= c3 <= c4`）。

### 校验规则与出错时的行为

**错误粒度：整文件回退。** 任何一条校验不过，整张表全部作废、四张表一起回退内置默认，不做「部分生效」。日志里只报**第一个**出错的位置（点分路径，如 `prototypes[3].valence`），值的类型记为下列之一：

| 错误类型 | 什么时候出现 |
|---|---|
| `parse` | 读不了 / 不是 UTF-8 / JSON 语法错 |
| `version_missing` | 没有 `version` 键 |
| `version_unsupported` | `version` 不是整数（`true` 也不算整数）或不等于 1 |
| `unknown_key` | 出现了不在结构里的键（顶层、原型条目、事件条目、倍率里都算） |
| `incomplete_table` | 该有的键缺失（表漏键、4 个事件没写全） |
| `type` | 类型不对（表不是对象/数组、数值位写了布尔或字符串等） |
| `range` | 数量或取值越界（原型条数、阈值个数、超范围、标签空或重复、`valence_min > valence_max`、阈值下降等） |

两条容易踩的细节：

- `NaN`、`Infinity` 这类字面量一律按越界拒绝；
- `level_thresholds` 出现**下降**才拒绝；相邻**相等**只告警不拒绝（那一档会是空档）。

补充说明：`mappings/emotion_mapping.json` 存在时**以它为准**，不会再叠加插件数据目录里的同名覆盖层（`emotion/mapping_override.py:660-666`）。只有它缺失时，才会继续去看数据目录那份（老式覆盖层，普通使用者不用管）。

### 完整可抄的最小示例

只改「倍率 + 档位切分点」两张表，其余沿用内置默认：

```json
{
  "version": 1,
  "multipliers": {
    "global_event_multiplier": 1.0,
    "non_target_multiplier": 1.0,
    "target_bonus_step": 0.1
  },
  "level_thresholds": [-0.5, -0.15, 0.15, 0.5]
}
```

再给一个含事件表的版本（4 个事件一个都不能少）：

```json
{
  "version": 1,
  "template_events": {
    "relation.silence_detected": {"valence": -0.12, "arousal": 0.04, "dominance": 0.0},
    "schedule.commitment_kept": {"valence": 0.2, "arousal": 0.0, "dominance": 0.0},
    "schedule.commitment_broken": {"valence": -0.15, "arousal": 0.0, "dominance": 0.0},
    "qzone.comment_received": {"valence_min": 0.05, "valence_max": 0.1}
  },
  "multipliers": {
    "global_event_multiplier": 1.0,
    "non_target_multiplier": 1.0,
    "target_bonus_step": 0.1
  },
  "level_thresholds": [-0.5, -0.15, 0.15, 0.5]
}
```

想改 `prototypes` 的话，直接照抄本文件夹里那份自带的 `emotion_mapping.json` 的 25 条原型改，注意**最少 10 条、最多 40 条**。

---

## 三、情绪描述表 `emotion_descriptions.json`

### 文件作用

给每种情绪配一段**人话描述**，渲染进提示词里告诉 LLM「bot 现在是什么心情、说话会是什么样」。

### 顶层结构与字段表

这张表**没有 `version` 键**，顶层就是一个平铺的「情绪名 → 描述文字」对象：

| 键 | 值 |
|---|---|
| 情绪名（如 `狂喜`、`平静`） | 字符串，非空白 |
| `复杂难名` | 字符串，通用兜底描述（键名固定，建议保留） |

出厂默认有 26 个键（25 个情绪 + `复杂难名`）。

**哪些情绪名算合法**：以当前生效的 `emotion_mapping.json` 里的 `prototypes` 标签集合为准（再加上固定的 `复杂难名`）。也就是说，如果你在情绪映射表里新增/改名了原型，那张原型的标签自动成为这里合法的键。

### 校验规则与出错时的行为

**错误粒度：丢弃非法键，其余生效。** 具体分四层：

| 情况 | 行为 |
|---|---|
| JSON 读不了 / 语法错 / 顶层不是对象 | **整体回退内置默认**（告警日志） |
| 键不在已知情绪名里 | **丢弃这个键**，其余照常生效（告警日志会列出被丢的键） |
| 值不是字符串、或是空白字符串 | 丢弃这个键的值（该情绪走内置默认补齐） |
| 已知情绪在这张表里没写 | 用**内置默认描述**补齐 |
| 渲染时连内置默认都没有 | 走固定句式兜底（`复杂难名` 那套通用描述） |

（实现见 `emotion/descriptions.py:131-160`。）

### 完整可抄的最小示例

只写两条也行，其余情绪自动用内置默认：

```json
{
  "平静": "此刻心里很稳，像晚饭后晒着最后一点夕阳，说话松弛温和，不急不躁。",
  "复杂难名": "此刻的心情说不清也道不明，好几种滋味搅在一起，但它确实在影响着说话的语气。"
}
```

---

## 四、好感度预设表 `affection_presets.json`

### 文件作用

定义「一段关系分几档、每档叫什么、覆盖好感度的哪一段分数」。好感度总分固定 `0 ~ 200`。

### 顶层结构与字段表

| 键 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `version` | **是** | 整数 | 必须恰为 `1` |
| `presets` | 否 | 对象 | 预设名 → `{"tiers": [...]}` |

每个预设：

| 字段 | 类型 | 约束 |
|---|---|---|
| `tiers` | 数组 | 档位数 **1~5**，且**只允许有 `tiers` 这一个键** |

每档（`tiers` 的每一项）恰好三个键：

| 字段 | 类型 | 约束 |
|---|---|---|
| `label` | 字符串 | 去掉首尾空白后非空，且**同一预设内**互不重复 |
| `min` | 数值 | `0 <= min <= 200`，不能是布尔值、`NaN`、`Infinity` |
| `max` | 数值 | 同上，且 `min < max` |

### 区间必须严格连续覆盖 0~200

三条硬约束，缺一不可：

1. **首档 `min` 必须等于 0**；
2. **相邻档必须相接**：后一档的 `min` 必须等于前一档的 `max`（不允许有缝，也不允许重叠）；
3. **末档 `max` 必须等于 200**。

例：`0-50`、`50-90`、`90-200` 合法（首 0、相接、末 200）；`0-50`、`60-200` 不合法（中间断了）；`0-50`、`50-190` 不合法（末尾没到 200）。

档位的分数归属规则：某档覆盖 `[min, max)` —— 分数落在该档时按「从末档往前找第一个 `score >= min` 的档」判定（`companion/affection_presets.py:112`），所以正好等于边界值时算**后一档**。末档到 200 为止，200 分算末档。

### 预设名与 WebUI「关系类型」下拉的关系

`[patrol].relation_type`（WebUI 里叫「关系类型」）的下拉选项 = **内置五个名字 ∪ 这张表里所有合法预设名**。

- 内置五个名字**恒在**选项里，改 json 也删不掉：`恋人`、`挚友`、`维护者`、`家人`、`自定义`；
- 你在 json 里加一个预设（例如 `搭子`），只要它通过校验，**每次启动 / 热重载后下拉里就会多出这一项**（选项在模块导入期动态构造，`companion/config.py:45`，所以是「改 json → 重启或热重载 → 下拉更新」，不用改代码）；
- 下拉的默认值 `恋人` 因此永远不会因为 json 内容而变得非法。

### 校验规则与出错时的行为

**错误粒度：坏预设仅排除该预设，顶层级错误才整退。** 这是四张表里最特别的一种：

| 情况 | 行为 |
|---|---|
| JSON 读不了 / 语法错 / 顶层不是对象 | **整文件回退内置默认** |
| `version` 缺失 / 不是整数 / 不等于 1 | **整文件回退内置默认** |
| 顶层出现 `version`、`presets` 之外的键 | **整文件回退内置默认** |
| `presets` 的值不是对象 | **整文件回退内置默认** |
| 没有 `presets` 键 | 视为「无自定义」，全部用内置（只告警，不算错） |
| **某一个**预设校验不过（档数不对、区间不连续、标签重复……） | **只把这一条预设排除**，日志里列出排除原因；其它合法预设照常生效 |
| 预设名去掉空白后是空的 | 该预设被排除 |
| 所有预设都被排除 | 结果等于只有内置预设 |

优先级：**json 里的预设 > 同名内置预设（覆盖）> 其余内置预设**。也就是说你可以在 json 里只写 `恋人` 一条来覆盖内置的「恋人」，另外四个内置预设不受影响。

### 完整可抄的最小示例

一个预设、一档、覆盖 0~200 的最小写法：

```json
{
  "version": 1,
  "presets": {
    "搭子": {
      "tiers": [
        {"label": "刚认识", "min": 0, "max": 200}
      ]
    }
  }
}
```

### 用户示例「0-50 陌生、50-70 认识」的完整写法

只有两档 `0-50`、`50-70` 是**不合法**的——末档没到 200：

```json
{
  "version": 1,
  "presets": {
    "搭子": {
      "tiers": [
        {"label": "陌生", "min": 0,  "max": 50},
        {"label": "认识", "min": 50, "max": 70}
      ]
    }
  }
}
```

上面这份会被**排除**，日志里会写：`末档 max：期望 200（覆盖区间终点），实得 70.0`。要把这两档留住并补齐到 200，就得把剩下的分段也写出来，例如**五档**版本：

```json
{
  "version": 1,
  "presets": {
    "搭子": {
      "tiers": [
        {"label": "陌生", "min": 0,   "max": 50},
        {"label": "认识", "min": 50,  "max": 70},
        {"label": "熟悉", "min": 70,  "max": 120},
        {"label": "亲密", "min": 120, "max": 170},
        {"label": "挚爱", "min": 170, "max": 200}
      ]
    }
  }
}
```

这样 `陌生` 是 0~50、`认识` 是 50~70，后面三档把 70~200 接上，整段 0~200 连续无缝。写好后重启或热重载，「关系类型」下拉里就会多出 `搭子` 这一项。

---

## 五、好感度档位描述表 `affection_tier_descriptions.json`

### 文件作用

给每个档位配一句**注入提示词的描述**（「热恋阶段，语气甜蜜亲密，可以肉麻撒娇」这类），决定 bot 在这个档位上的说话口吻。

### 顶层结构与字段表

两层结构：**预设名 → 档位名 → 提示词**（按预设分组，避免不同预设的档位名互相撞车）。

| 键 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `version` | **是** | 整数 | 必须恰为 `1` |
| `descriptions` | **是** | 对象 | 预设名 → `{档位名: 提示词}` |

- 第一层的键必须是**生效预设表里存在的预设名**（内置五名 ∪ `affection_presets.json` 的合法预设名）；
- 第二层的键必须是**该预设档位表里的 `label`**——也就是要和 `affection_presets.json` 里那一档的 `label` **逐字一致**。改了预设表里的档位名，这里也要跟着改；
- 值是字符串（非空白）。值不是字符串 / 是空白 → 该键作废，走下面的兜底；
- 某预设的第二层不是对象 → 该预设整体视为「没有自定义描述」，全部走兜底。

### 校验规则与出错时的行为

**错误粒度：丢弃非法键，其余生效。**

| 情况 | 行为 |
|---|---|
| JSON 读不了 / 语法错 / 顶层不是对象 | **整体回退内置默认**（告警 + 落账） |
| `version` 缺失 / 不是整数 / 不等于 1 | **整体回退内置默认** |
| 顶层出现 `version`、`descriptions` 之外的键 | **整体回退内置默认** |
| 没有 `descriptions` 键，或它不是对象 | **整体回退内置默认** |
| 预设名不在生效预设表里 | **丢弃这个键**（连同它下面的档位一起丢弃），其余生效 |
| 档位名不在该预设的档位标签里 | **丢弃这个键**，其余生效 |
| 值不是字符串 / 是空白 | 丢弃该键的值 |
| 已知档位在这张表里没写 | 用内置默认文案补齐 |

被丢掉的键会写进告警日志（形如 `恋人.热恋`）。

### 缺省档位的兜底顺序

某个档位最终显示哪句话，按这个顺序找，找到就用：

1. **这张 json 里写的**（你要改的就是这一层）；
2. **内置默认文案**——按「预设名 + 档位名」精确匹配。内置文案只覆盖 `恋人` / `挚友` / `维护者` / `家人` 四套内置档位名（`companion/affection_tier_descriptions.py:51`），`自定义` 预设不设内置文案；
3. **通用固定句式**——前两层都没有时用的兜底，会带上档位名和区间，例如：
   `当前关系处于「搭档」阶段（好感度区间 50~70），按这一阶段的亲近程度自然相处。`

注意第 2 条是**按档位名匹配**的：如果你在预设表里把「热恋」改名成「甜甜」，而这里没写「甜甜」，那么这一档虽然属于内置的「恋人」预设，也匹配不到内置文案（内置里只有「热恋」），会直接落到第 3 条通用句式。

### 完整可抄的最小示例

只给「搭子」这一档写描述（前提：`affection_presets.json` 里已经有 `搭子` 且档位名是 `刚认识`）：

```json
{
  "version": 1,
  "descriptions": {
    "搭子": {
      "刚认识": "刚认识，语气客气自然，先摸清对方的聊天节奏。"
    }
  }
}
```

给内置「恋人」预设改一句：

```json
{
  "version": 1,
  "descriptions": {
    "恋人": {
      "生疏": "初识阶段，语气温柔但有分寸感，保持适当距离。"
    }
  }
}
```

---

## 六、日程属性表 `schedule_properties.json`（第五张表，2026-10-21）

### 文件作用

把「bot 现在做某件事时聊天频率调多高」的**回复率倍率**和「什么样的日程算哪种属性」的
**关键词词表**外置给你改。原 `proactive_service._FREQUENCY_FACTOR_BY_GOAL_TYPE`
频率表已删除，倍率只从这张表取（`planning/activity_properties.py` 单一真源）：

- **倍率**进 bot 的主动行为频率调控（`planning/proactive_service.py` 的 `_apply_frequency`，
  经 `ctx.frequency.set_adjust` 推给主程序）：<1 减少回复、>1 增加回复、1.0 = 不调整；
- **关键词**用于给日程条目归类属性（纯离线字符串匹配，零 LLM 调用）；
- **标签/颜色**显示在 `/lbm_plan list` 文字与日程图条目行上（`[属性]` 短标签 + 属性色条）。

### 顶层结构与字段表

```json
{
  "version": 1,
  "properties": [
    {"id": "sleep", "label": "睡觉", "goal_types": ["sleep"],
     "keywords": ["睡觉", "睡眠", "安睡"], "reply_factor": 0.05, "color": "#5A6982"},
    ...
  ],
  "default_property": {"id": "free_time", "label": "空闲", "goal_types": ["free_time", "custom"],
                        "keywords": [], "reply_factor": 1.0, "color": "#6D8FB0"}
}
```

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `version` | **是** | 整数 | 必须恰为 `1` |
| `properties` | 否 | 数组 | 属性条目列表（缺 = 全部用内置条目） |
| `default_property` | 否 | 对象 | 兜底条目（缺 = 内置「空闲」，free_time/1.0） |

每个属性条目：

| 字段 | 必填 | 类型 | 约束 |
|---|---|---|---|
| `id` | **是** | 字符串 | 去空白后非空，且表内互不重复 |
| `label` | **是** | 字符串 | 去空白后非空；渲染标签是 `[label]`，推荐 **2~3 字中文**（渲染按 4 字截断） |
| `reply_factor` | **是** | 数值 | `0 ≤ factor ≤ 2`，不能是布尔值 / 字符串 / `NaN` / `Infinity`，**缺了也不算合法条目** |
| `goal_types` | 否 | 字符串数组 | 该属性直接认领的 `goal_type` 列表（空数组 = 只靠 keywords 万能匹配） |
| `keywords` | 否 | 字符串数组 | 条目名含该子串就归入本属性（子串匹配） |
| `color` | 否 | 字符串 | `"#RRGGBB"` 十六进制色或省略；**颜色写错只丢颜色位**，条目照常生效（图上回退类型色） |

### 匹配口径（确定性，纯离线）

一条日程（`goal_type` + 名称）按这个顺序找一个属性，先到先得：

1. **`goal_type` 精确命中**：条目的 `goal_type` 出现在某条属性的 `goal_types` 里（按 json 数组顺序取第一条命中的属性）；
2. **名称关键词子串**：第 1 步没有命中（goal_type 未被任何属性收录）时，按数组顺序找第一条「条目名含其关键词」的属性；
3. **兜底条目**：都没命中 → `default_property`（内置 = 空闲，倍率 1.0）。

注意第 1 步**优先于**第 2 步：`goal_type` 已收录的条目（如 `daily_routine`）即使名字里有
「睡觉」，也先按 `daily_routine` 的属性取倍率——这与旧频率表按 `.get(goal_type, 1.0)` 的
行为一致；关键词只服务「goal_type 没被收录」的条目（如自定义类型写了名字叫「午休」的活动）。

### 内置默认（出厂即这套，改前先抄）

| 属性 id | 标签 | goal_types | reply_factor | 色值 |
|---|---|---|---|---|
| `sleep` | 睡觉 | sleep | **0.05** | #5A6982 |
| `study` | 学习 | study | **0.3** | #4A90E2 |
| `work` | 工作 | work | **0.3** | #6E7F95 |
| `learn_topic` | 兴趣 | learn_topic | **0.5** | #00A8A8 |
| `exercise` | 运动 | exercise | **0.7** | #27AE60 |
| `meal` | 用餐 | meal | **1.2** | #F2994A |
| `rest` | 休息 | rest | **1.3** | #A3B7CE |
| `daily_routine` | 日常 | daily_routine | 1.0 | #7F8FA6 |
| `entertainment` | 娱乐 | entertainment | **1.5** | #9B51E0 |
| `social_maintenance` | 社交 | social_maintenance | **1.5** | #EB568E |
| `free_time`（兜底条目） | 空闲 | free_time、custom | 1.0 | #6D8FB0 |

（粗体 = 与旧频率表数值语义一致；`free_time` / `custom` / 未收录的 goal_type 一律走兜底条目 1.0——旧表「缺 free_time/custom」的缺口随本表补齐。）

内置关键词词表（与 ``mappings/README`` 之外的两套睡眠词相依但**独立保留**，改这里不影响
静默窗/午休静默判定）：

- `sleep` 属性：`睡觉` / `睡眠` / `安睡`（夜间词，与 `planning/schedule_items.NIGHT_SLEEP_NAME_KEYWORDS` 同词不同源）；
- `rest` 属性：`午休` / `午睡` / `小憩` / `打盹`（午休词，与 `companion/patrol.NAP_NAME_KEYWORDS` 同词不同源）。

### 校验规则与出错时的行为

**错误分三级粒度（§24.3 冻结口径）**：

| 情况 | 行为 |
|---|---|
| JSON 读不了 / 语法错 / 顶层不是对象 | **整文件回退内置默认**（告警） |
| `version` 缺失 / 不是整数 / 不等于 1 | **整文件回退内置默认** |
| 顶层出现 `version`、`properties`、`default_property` 之外的键 | **整文件回退内置默认** |
| `properties` 不是数组 / `default_property` 不是对象 | **整文件回退**（前者）/ **回退内置兜底条目**（后者，其余照常生效） |
| `properties` 键缺失 / 数组为空 / `default_property` 键缺失 | 视为「无自定义」，全部用内置（提示不算错） |
| **某一个**条目坏（缺 id/label、`reply_factor` 非数值/bool/NaN/Inf/越界/缺失、`goal_types`/`keywords` 不是字符串数组、id 或 label 重复） | **只排除这一条**，其余条目照常生效 |
| 条目里出现结构外的未知键 | **丢弃这个键**，条目其余字段照常生效（提示留账） |
| `color` 写错（不是 `#RRGGBB` 形态） | **只丢颜色位**，条目与倍率照常生效（渲染回退类型色 → 兜底色） |
| 该表没写的内置条目 | 沿用内置条目（**同 id 覆盖、其余内置保留**——json 只写你想改的条目即可） |

每次装载的生效情况都会写进决策日志（decision log）留账：`planning.properties_applied`（生效清单）/
`planning.properties_rejected`（整文件回退原因 + 被排除条目清单）两类 action。

### 完整可抄的最小示例

只想把「学习」调得更低频（0.3 → 0.1），整张表只写这一条即可（其余内置保留）：

```json
{
  "version": 1,
  "properties": [
    {
      "id": "study",
      "label": "学习",
      "goal_types": ["study"],
      "keywords": ["学习", "功课"],
      "reply_factor": 0.1,
      "color": "#4A90E2"
    }
  ]
}
```

想新增一个自定义属性（比如把自由活动改成三倍社交欲），同时改兜底条目：

```json
{
  "version": 1,
  "properties": [
    {"id": "hobby", "label": "爱好", "goal_types": ["photography", "birdwatch"],
     "keywords": ["摄影", "观鸟"], "reply_factor": 2.0, "color": "#20846B"}
  ],
  "default_property": {"id": "free_time", "label": "空闲", "reply_factor": 1.0}
}
```

注意 `default_property` 只在「都匹配不上」时兜底，改它不会影响已命中的条目。

---

## 七、FAQ

**Q：改完 json 为什么没生效？**

A：五张表都**只在插件加载 / 热重载时读一遍**。保存文件后要**触发热重载或在宿主里重启插件**才会重读。

如果重载后还是没变化，按顺序查：

1. JSON 语法对不对——命令行跑一句 `python -c "import json;json.load(open('mappings/affection_presets.json',encoding='utf-8'))"`，不报错才算语法合法；
2. 编码是不是 UTF-8（文件开头允许多一个 BOM，Windows 记事本另存也不会坏）；
3. 看日志有没有告警：
   - 情绪映射表整文件回退；
   - 情绪描述表「含未知情绪键，已丢弃」；
   - 好感度预设表「有 N 个预设被排除」；
   - 好感度档位描述表「含未知预设/档位键，已丢弃」；
   - 日程属性表整文件回退 /「有 N 个条目被排除」（`planning.properties_rejected` 留账）。

**Q：日程属性表怎么调 bot 在某类活动里的回话频率？**

A：改 `schedule_properties.json` 那条属性的 `reply_factor` 后热重载。倍率 <1 少说话、
>1 多说话、1.0 = 不调整（范围 0~2，睡觉 0.05、娱乐/社交 1.5 是出厂值）。只能改已在该表的
条目——本身就是按属性条目工作的，不在表里的 `goal_type` 一律走 `default_property`（默认
空闲 1.0，把它改大 = 所有认不出属性的日程都更爱说话）。

**Q：我把某条属性从 `schedule_properties.json` 里删了，会怎样？**

A：该条目回到**内置默认**（见第六节内置默认表）——同 id 覆盖、其余内置保留的合并语义，
条目只会回值，不会消失。想让某个 `goal_type` 归别条属性管，把它写进那条属性的 `goal_types`
即可（数组序先到先得，写在前面的属性优先）。

**Q：我把某个预设从 `affection_presets.json` 里删了，但配置里还选着它，会怎样？**

A：不会崩，会**回退到内置的「恋人」**（取不到该名字时回退第一个内置预设，见 `companion/affection_presets.py:429`；关系解析侧同样回退「恋人」，`companion/relation.py:234`）。

同时「关系类型」下拉里**不会再出现这个名字**。建议顺手把 WebUI 里的「关系类型」改成一个存在的名字（内置五个之一，或 json 里其它合法预设名）。

**Q：我以前的分数是 0~100 的，现在怎么算？**

A：**乘 2**。整套标度做了等比放大，体验等价：

- 内置档位区间就是旧区间 ×2：旧的 `(0,20)(20,45)(45,70)(70,88)(88,100)` → 新的 `(0,40)(40,90)(90,140)(140,176)(176,200)`；
- 旧档案（`version: 2`）升级时 `score` / `baseline` 自动 ×2（`companion/affection.py:195`）；
- 初始分从 30 变成 60；
- 之前手动写在配置里的分值参数**不会自动迁移**（无从判断意图），需要的话按 ×2 自己调。

**Q：能不能只改一个预设？**

A：能。`presets` 里**只写你想改的那一个**就行，同名覆盖内置，其余内置预设保持原样。比如你只想调整「恋人」的档位，就在 json 里只写 `恋人` 一条，`挚友` / `维护者` / `家人` 不受影响。

同理，档位描述表也只需要写你要改的预设 / 档位，其余自动用内置文案或通用句式兜底。

**Q：档位数能不能不是 5 档？**

A：可以，**1~5 档**都行，每档的名字自己起。但要注意两点：区间必须首 0、相接、末 200；档位数变了以后，档位描述表里对应预设的档位名要跟着改，否则会被丢弃。

---

> 维护提示：本文档随插件包分发，描述的是当前代码的行为；代码与文档都可能继续演进，若两者冲突，**以代码内注释为准**（各表对应的源码：`emotion/mapping_override.py`、`emotion/descriptions.py`、`companion/affection_presets.py`、`companion/affection_tier_descriptions.py`、`planning/activity_properties.py`、`core/mapping_dir.py`）。

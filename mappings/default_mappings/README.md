<!-- lbm-document-status: 当前维护 -->
> 文档状态：**当前维护** · 整理日期：2026-11-16
> 适用源码：3.18.3
> 当前说明：[README](../../README.md) · [完整文档清单](../../DOCUMENTATION.md)

# 映射表与出厂种子

表层 `mappings/` 是用户编辑区，`mappings/default_mappings/` 是随包只读种子源。缺表时可从旧位置/种子补齐；用户已有但损坏的文件不会被静默覆盖。升级不以出厂默认替换用户修改。

| 文件/目录 | 当前职责 |
| --- | --- |
| `emotion_mapping.json` | 情绪原型、事件增量、倍率和阈值 |
| `emotion_descriptions.json` | 情绪注入的文字呈现 |
| `affection_presets.json` / `affection_tier_descriptions.json` | 关系分数、档位及描述 |
| `schedule_properties.json` | 回复率、显示标签与颜色；不是 `watch_video`/`browse_qq` 模块属性 |
| `activity_profiles.json` | 活动画像、时长/可中断性与生活状态参数 |
| `emotion_life_modulation.json` | 当前生活事件情绪反馈；历史 gains 名称不代表还叠加另一份生活速率调制 |
| `internal_tuning.json` | 不对普通配置页公开的内部调参；不把只读兼容 property 当公开 TOML 项 |
| `initial_events/` | [初始小事件定义](initial_events/README.md)，实际编辑用户表层副本 |
| `templates/` | [统一 LLM 保守模板](templates/README.md) |

一般映射按加载/重载装入内存，内部调参等有专门动态读取口；不要把“保存 JSON”一概当作所有消费者已刷新。修改后按相应装载口重载并检查诊断，已经启动的活动仍按自己的快照结束。

活动模块属性由最终日程批量标注，失败为 null；状态类型、回复率属性和模块属性是三种职责，不能改一个名称就获得工具或发送权限。现有映射校验以代码为准，未知字段/坏条目按各表策略拒绝或降级，不回写成用户没选过的值。

本目录是生活插件的出厂数据与维护说明，来源随所属模块：规划沿现有AGPL声明，关系部分参考mai-love，情绪与联动数据由本项目维护。完整原仓与范围见[第三方说明](../../THIRD_PARTY.md)，公开配置见[配置参考](../../CONFIGURATION.md)。

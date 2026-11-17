<!-- lbm-document-status: 当前维护 -->
> 文档状态：**当前维护** · 整理日期：2026-11-16
> 适用源码：3.18.3
> 当前说明：[README](../../../README.md) · [完整文档清单](../../../DOCUMENTATION.md)

# 初始小事件库

本目录为随包种子，用户在表层 `mappings/initial_events/` 编辑自己的定义。首次初始化复制种子；之后已有文件不覆盖，删除项不按一般升级自动补回。明确新增/退役的出厂项按 loader 的内存合并/兼容策略处理，不改写用户文件。

普通小事件是模拟活动及有界状态变化，不是工具调用、QQ发送、网络或文件操作；`watch_bili` / `browse_qq` 两个内置联动ID保留，不允许用户定义冒充。日程批量标注的模块属性也不是可在本JSON中任意加的执行指令。

## 真实种子示例

```json
{
  "version": 1,
  "event": {
    "event_id": "daydream",
    "name": "发会呆",
    "enabled": true,
    "goal_type": "rest",
    "min_minutes": 5,
    "max_minutes": 12,
    "interruptibility": 0.95,
    "weight": 0.7,
    "cooldown_minutes": 45,
    "allowed_slots": [
      "00-08",
      "08-16",
      "16-24"
    ],
    "state_deltas": {
      "stress": -0.02
    },
    "templates": [
      "望着窗外发了一小会儿呆，脑子里放空了一阵。",
      "靠在椅背上走了一小会儿神，什么也没想。"
    ],
    "tags": [
      "放空",
      "发呆"
    ]
  }
}
```

定义校验由 `planning/initial_events.py` 负责：文件名与event_id一致，版本、时段、时长、可中断性、权重、微小状态增量和文案均受边界约束；重复ID、坏JSON或未知字段拒绝该文件并留原因。JSON不能写睡眠债或另外实现积分器。

修改/重载只影响后续候选，已开始活动使用启动快照。来源沿规划目录现有AGPL与本项目后续实现；原仓与参考版本见[规划说明](../../../planning/README.md)。运行目录与种子关系见[映射指南](../README.md)。

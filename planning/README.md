<!-- lbm-document-status: 当前维护 -->
> 文档状态：**当前维护** · 整理日期：2026-11-16
> 适用源码：3.18.7
> 当前说明：[README](../README.md) · [完整文档清单](../DOCUMENTATION.md)

# 日程规划：能力、来源与许可

本目录继承并改造自麦麦自主规划插件，原作者靓仔（xuqian13）。

- 原仓库：[xuqian13/autonomous_planning_plugin](https://github.com/xuqian13/autonomous_planning_plugin)。
- 实际维护参考版本：cateye 维护分支 **4.9.1**，[固定提交 01ce890f](https://github.com/cateyemizuki/autonomous_planning_plugin/tree/01ce890f9b577d48fd5e3eb6eca1572a4d27df68)。
- [该参考版本 LICENSE](https://github.com/cateyemizuki/autonomous_planning_plugin/blob/01ce890f9b577d48fd5e3eb6eca1572a4d27df68/LICENSE) 为 AGPL-3.0。目录现有 SPDX 声明为 `AGPL-3.0-or-later`，完整许可保留于 [LICENSE](LICENSE)；本次不修改许可。

本地 `planning/LICENSE` 已与参考上游 AGPLv3 原文核对，Git blob 为 `0ad25db4bd1d86c452db3f9602ccdbe172438f52`；保留原位置和原文，不在 `licenses/` 另建重复副本。它与根 MIT 文件共存是现有声明事实，不表示将规划内容重新授予 MIT，也不作整个组合许可或兼容性承诺。

这不是把旧插件原样运行：生成已改为稀疏日意图，实际活动由滚动规划、睡眠引擎和统一状态积分推进；SQLite、周期发布、硬约定、主动行为、事件库、偏好与诊断均持续改造。目标管理、时区、缓存、校验和若干服务接口保留其来源声明。

## 当前数据与流程

`ScheduleGenerator` 构造最终候选，确定性合并硬约定，再调用一次批量活动属性标注；失败为 `null`。发布前再合并等待期间新增约定，事务替换日表，然后持久化正式或待接替周期。周期失败不会报完整成功，已有日表可不重调模型地补齐。

`schedule_items` 是计划，`activity_episodes` 是运行活动，`sleep_sessions` 是真实睡眠会话。它们都不是外部消息或视频已发送/已观看的证明；这些事实归对应观看/发送回执所有。

明确时间的硬约定按稳定 ID 保留，不因模型遗漏或时间邻近误认而丢失；排入不结算，真实完成或到期后才通过持久 outbox 结算。交付要求的陪伴和情绪消费者齐全、确认成功后才确认 outbox，部分启停期间继续保留重试。

`activity_attributes` 是模块联动列表，`goal_type` 继续用于状态计算，回复率/颜色属性另见 `activity_properties.py`。共享规范化和守护位于 `core/activity_attributes.py`，不依赖规划服务或模型。

午休仍按生理阈值、醒后时长、窗口和当前活动守护决定，不强制每日午睡；`rolling.nap_evaluated` 与窗口末日志分别记录生理原因和活动阻挡，避免只看粗 `busy` 推断执行。

当天和未来的明确时间硬约定共用稳定目标。已有日表会立即合入新约定，取消同时撤销尚未结算的目标和计划，按序号删除使用当日列表的排序。执行开始记录真实时间，恢复活动采用新段所属日。

免睡模式在作息窗口内保持静默。睡眠重启恢复使用已持久会话和最后成功积分游标补算，超过会话最晚醒点的区间按清醒处理。活动终态的生活增量随 SQLite 保存待办，失败后凭同一活动收据重试。旧终态记录只保留已有事实。

更多使用说明见[根 README](../README.md)，出处范围见[第三方说明](../THIRD_PARTY.md)。

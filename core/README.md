<!-- lbm-document-status: 当前维护 -->
> 文档状态：**当前维护** · 整理日期：2026-11-16
> 适用源码：3.18.3
> 当前说明：[README](../README.md) · [完整文档清单](../DOCUMENTATION.md)

# 公共核心与叶协议

本目录的诊断日志、早期 LLM 工具等部分实现参考或移植自 mai-love，原作者 octmicy，[原仓库](https://github.com/octmicy/Mai_love)，实际维护参考为 [2.6.3 / cf6ff0da](https://github.com/cateyemizuki/Mai_love/tree/cf6ff0da7ae8227d1c2872e2a2c8f894864b694a)，许可 MIT，文本副本见 [licenses](../licenses/Mai_love-MIT.txt)。其余新增基础设施按本项目实现维护，不把全部 `core/` 都归为同一上游原码。

当前职责包括配置/身份规范化、持久化、统一 LLM 请求结果与策略、用量与生成证据、事件分发、命令登记及跨模块共享类型。业务分类与真实执行事实仍由业务服务所有，不在通用层用关键词替业务判定。

`activity_attributes.py`、观看任务协议及 LLM 结果类型是无服务装配的共享叶；读取这些协议不应导入规划/视频运行器或隐式创建服务。`publish` 保留普通事件隔离契约；持久 outbox 使用 `publish_confirmed` 并显式指定必要消费者，部分启动或停止时不提前确认。

用量只记宿主实际回传，不用模板、估算或界面分页改变历史状态。代码中保留的旧诊断接口用于兼容，不意味着相应旧配置项仍公开。见[配置参考](../CONFIGURATION.md)和[来源说明](../THIRD_PARTY.md)。

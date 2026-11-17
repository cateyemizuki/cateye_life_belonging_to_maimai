<!-- lbm-document-status: 当前维护 -->
> 文档状态：**当前维护** · 整理日期：2026-11-16
> 适用源码：3.18.3
> 当前说明：[README](../README.md) · [完整文档清单](../DOCUMENTATION.md)

# 虚拟陪伴

本模块由 **mai-love / Mai_love** 改造而来，原作者 **octmicy**。

- 原仓库：[octmicy/Mai_love](https://github.com/octmicy/Mai_love)。
- 实际维护参考：**2.6.3**，[cateye 维护分支固定提交 cf6ff0da](https://github.com/cateyemizuki/Mai_love/tree/cf6ff0da7ae8227d1c2872e2a2c8f894864b694a)。
- 许可：MIT，[参考 LICENSE](https://github.com/cateyemizuki/Mai_love/blob/cf6ff0da7ae8227d1c2872e2a2c8f894864b694a/LICENSE)，[保留的许可副本](../licenses/Mai_love-MIT.txt)。

配置、记忆、发送、身份缓存、屏幕上下文和组件面保留其原始移植说明；主动巡检、好感分数/档位、情绪联动、SDK 服务装配、消息优先级、持久预算和稳定约定回执已持续重写，不能以旧插件说明替代当前行为。

陪伴目标与管理员继承只接受明确可识别的唯一 QQ 身份，不从多个管理员猜目标。主动发送经统一可选行为、预算与回执；问候有独立条件，群聊与私聊范围分别处理。稳定约定事件按约定 ID 持久去重，好感变动与回执同文件提交。

屏幕客户端适配现位于 `linkage/screen/`，消费方私有的尾随上下文仍在本目录；自动感知与工具开关分开，工具只提供给陪伴目标私聊。使用与隐私边界见[根 README](../README.md)，完整来源见[第三方说明](../THIRD_PARTY.md)。

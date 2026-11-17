<!-- lbm-document-status: 当前维护 -->
> 文档状态：**当前维护** · 整理日期：2026-11-16
> 适用源码：3.18.3
> 当前说明：[README](../README.md) · [完整文档清单](../DOCUMENTATION.md)

# 外部客户端联动

本目录是生活插件的联动适配层。屏幕客户端 `screen/cateye_client.py` 来源于 mai-love 的客户端实现，参考 **2.6.3** 的 [cateye 维护分支](https://github.com/cateyemizuki/Mai_love/tree/cf6ff0da7ae8227d1c2872e2a2c8f894864b694a)，原作者 octmicy，[原仓库](https://github.com/octmicy/Mai_love)，MIT 许可副本见 [licenses](../licenses/Mai_love-MIT.txt)。

后续改造包括适配器注册、能力就绪、调用超时与陪伴上下文注入。屏幕自动感知与窥屏工具默认都关闭，开关独立；工具仅在陪伴目标私聊可用，主屏/全屏与模糊半径由 `screen` 段控制。截图会交给已配置视觉任务处理；本地客户端在线和授权仍是前提。

本层不自行决定聊天预算或主动频率，消息投递走既有统一链。当前能力和隐私事实见[根 README](../README.md)，其余来源边界见[第三方说明](../THIRD_PARTY.md)。

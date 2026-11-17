<!-- lbm-document-status: 当前维护 -->
> 文档状态：**当前维护** · 整理日期：2026-11-16
> 适用源码：3.18.7
> 当前说明：[README](../README.md) · [完整文档清单](../DOCUMENTATION.md)

# QQ 空间

本模块由生活插件维护，不是一个未经改造的上游插件副本。它整合适配器动作、直接 CGI、模型判定、媒体收集与持久发送回执。

## 原始参考与实际范围

- **Maizone / qzone-toolkit**：作者 internetsb，[原仓库](https://github.com/internetsb/Maizone)，参考 **3.0.3**，[固定提交 0884ce32](https://github.com/internetsb/Maizone/tree/0884ce328a92a8aeea171c78a23f8475dbe55c16)，[历史 MIT LICENSE](https://github.com/internetsb/Maizone/blob/0884ce328a92a8aeea171c78a23f8475dbe55c16/LICENSE)。`cgi.py` 的上传路线、cookie/gtk 思路及 `image_sources.py` 配图收集参照该实现。
- **MaiTrace**：原记录作者 Rabbit-Jia-Er，[旧仓地址](https://github.com/Rabbit-Jia-Er/MaiTrace) 现重定向为 [Yae-Miko-Rabbit/MaiTrace](https://github.com/Yae-Miko-Rabbit/MaiTrace)。本仓参考 **3.1.1 / f376c49c** 的[历史 MIT LICENSE](https://github.com/Yae-Miko-Rabbit/MaiTrace/blob/f376c49c9b475ec5cb6d706711efdd2c32d2ede4/LICENSE)，范围为 `decision.py` 判定骨架、解析和配图思路。当前上游已采用 AGPL，不能把其当前许可倒推到此历史参考。
- **SnowLuma**：`service.py` 注明的是客户端 v1.12.10 动作面实测/协议字段参考；[客户端资料地址](https://github.com/SnowLuma/SnowLuma) 本次在线不可访问，未核实该版本许可。[MaiBot 适配器](https://github.com/Mai-with-u/MaiBot-SnowLuma-Adapter) 是另一项目，其当前 manifest 声明 GPL-v3.0-or-later；不把二者或协议兼容实现混称 MIT 移植。

上游 MIT 文本副本见 [Maizone](../licenses/Maizone-3.0.3-MIT.txt)、[MaiTrace](../licenses/MaiTrace-3.1.1-MIT.txt)。根许可范围未变。

## 当前改造

发帖规则、LLM 严判、静默/额度、评论和楼中楼处理、可选配图及发送结果均按本插件装配。发布时间只由确认结果更新；结果未知时保守防重，不因补图或打开面板重新发送。

`post_media.py` 仅后台读取已确认自己发送的动态图片并独立缓存；展示接口返回且通过协议校验的地址，沿用原动态访问权限和链接时效。HTTP(S) 地址校验不是帖子公开授权，图片仍可能受可见范围、鉴权或时效限制。WebUI 展示不执行发帖动作，补图不改变发布回执。

宿主受限图片策略只为可信本插件身份追加有限 HTTPS CDN，已知 CDN 的 HTTP 地址升级为 HTTPS，非白名单不自动放开；其它插件和连接/脚本/框架/字体政策不变。实际安装状态以配套交付记录为准。视频空间候选与聊天视频分享分开：前者由 `qzone_video.mode` 控制，后者由 `video_share.mode` 控制。完整边界见[根 README](../README.md)与[来源说明](../THIRD_PARTY.md)。

普通评论遇到结果不明时，可在空间查看评论记录。工具、自动发帖及重试共用每日额度，发布前复核静默和冷却。管理员显式直发仍沿用既有日限豁免。`video_share.video_share_quality_gate` 及其阈值作用于空间视频候选的评估和发布。

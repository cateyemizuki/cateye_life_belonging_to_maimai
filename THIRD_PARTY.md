<!-- lbm-document-status: 当前维护 -->
> 文档状态：**当前维护** · 整理日期：2026-11-16
> 适用源码：3.18.3
> 当前说明：[README](README.md) · [完整文档清单](DOCUMENTATION.md)

# 来源、引用范围与第三方许可

本文件记录实际查到的来源与参考版本，不变更[根 LICENSE](LICENSE)、[规划目录 LICENSE](planning/LICENSE)或字体许可，也不把当前上游许可证倒推到旧源码。

| 来源 | 实际参考与许可 | 本仓范围与改造 |
| --- | --- | --- |
| [mai-love / octmicy](https://github.com/octmicy/Mai_love) | [cateye 维护分支 2.6.3 / cf6ff0da](https://github.com/cateyemizuki/Mai_love/tree/cf6ff0da7ae8227d1c2872e2a2c8f894864b694a)，MIT；[许可副本](licenses/Mai_love-MIT.txt) | `companion/` 的配置/身份/记忆/发送等、`linkage/screen/cateye_client.py`、`core/` 部分早期日志/LLM工具；主动行为、好感、SDK装配和回执已持续改造 |
| [自主规划 / xuqian13](https://github.com/xuqian13/autonomous_planning_plugin) | [维护分支 4.9.1 / 01ce890f](https://github.com/cateyemizuki/autonomous_planning_plugin/tree/01ce890f9b577d48fd5e3eb6eca1572a4d27df68)，参考 manifest 为 AGPL-3.0，本仓目录 SPDX 为 AGPL-3.0-or-later；[现存许可](planning/LICENSE) | `planning/` 的架构与若干直接移植文件；现已演进为稀疏意图、实际活动、动态睡眠、事务发布与约定结算，不再是旧插件完整原样复刻 |
| [Maizone / internetsb](https://github.com/internetsb/Maizone) | [3.0.3 / 0884ce32](https://github.com/internetsb/Maizone/tree/0884ce328a92a8aeea171c78a23f8475dbe55c16)，[历史 MIT LICENSE](https://github.com/internetsb/Maizone/blob/0884ce328a92a8aeea171c78a23f8475dbe55c16/LICENSE)，[副本](licenses/Maizone-3.0.3-MIT.txt) | `qzone/cgi.py` 上传、cookie/gtk 实现路线及配图收集思路；结合本插件适配器与回执改造 |
| [MaiTrace（旧 Rabbit-Jia-Er 地址）](https://github.com/Rabbit-Jia-Er/MaiTrace) | 现地址 [Yae-Miko-Rabbit/MaiTrace](https://github.com/Yae-Miko-Rabbit/MaiTrace)；本仓参考 [3.1.1 / f376c49c 的 MIT LICENSE](https://github.com/Yae-Miko-Rabbit/MaiTrace/blob/f376c49c9b475ec5cb6d706711efdd2c32d2ede4/LICENSE)，[副本](licenses/MaiTrace-3.1.1-MIT.txt) | 空间判定骨架、首字符解析与图库思路，见 `qzone/decision.py`、`image_sources.py`；不属于本日程系统来源 |
| SnowLuma 客户端 | 本仓头注释注明 v1.12.10 动作面实测/字段反推；[资料地址](https://github.com/SnowLuma/SnowLuma) 本次在线不可访问，未核实该参考版本许可 | `qzone/service.py` 的接口字段与返回结构参考；不随本仓分发其客户端 TS 源码 |
| [MaiBot SnowLuma 适配器](https://github.com/Mai-with-u/MaiBot-SnowLuma-Adapter) | 当前 manifest 声明 GPL-v3.0-or-later，默认分支未发现根 LICENSE 文件 | 适配器接口一致性参考；与上行客户端、Maizone 工具是不同项目，不统一称作 MIT 移植 |
| [MaiBot](https://github.com/Mai-with-u/MaiBot) | 宿主当前 LICENSE 为 GPL-3.0 | 插件运行与开发生态依赖；不据此声称把宿主整体代码移入本仓 |
| [Noto Sans SC](https://github.com/notofonts/noto-cjk) | 原 googlefonts 地址已重定向；SIL Open Font License 1.1；[随包 OFL](planning/assets/fonts/OFL.txt) | 字体资产用于日程/状态图片，文件内字体名及 Adobe 版权已核对 |
| cateye 自有本机视频原型 | `cateye_bili_watch` / `cad3d2d2b9e49d7a6c7cc35f1b8764b82e97d60f`，manifest 声明 MIT；未设置 Git remote，未发现独立 LICENSE | `video_watch` 最初迁入 `bv/store/manager/worker` 四件，后续内置并持续重构；不编造不存在的公开原仓地址 |
| [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) | Python 包装器为 BSD-2-Clause；既有部署渠道为[官方 0.6.0 wheel](https://pypi.org/project/imageio-ffmpeg/0.6.0/) | 解码器获取渠道；具体 FFmpeg 二进制须保留其实际构建的随附许可，不能用包装器许可代替 |

MaiTrace 当前上游在 2026-10-23 的 [a6ccc954](https://github.com/Yae-Miko-Rabbit/MaiTrace/commit/a6ccc954b9f9f063bfed9113c416015a7852f199) 升级 6.2.4 时改为 AGPL；本文件对旧参考的 MIT 说明明确钉在历史提交，不承诺其当前版本仍为 MIT。

SnowLuma 本机较新参考副本 2.4.2 附有源码可见非商业许可；这是该副本事实，不能代替对旧 v1.12.10 的许可核实。本仓的协议兼容说明不会因此宣称拿到了对任意版本源码的复制授权。

以上将“直接移植/持续改造”“实现思路/协议参考”“运行依赖”“自有原型”分开列出。原 MIT 文本副本保留上游原有版权行，不改成 cateye，也不以本文件替换原许可证。来源清单与模块说明是当前维护文档；旧致谢和历史验收见[文档目录](DOCUMENTATION.md)。

日程 AGPLv3 完整原文已经随包保留在 [planning/LICENSE](planning/LICENSE)，与参考 4.9.1 的[原文件](https://github.com/cateyemizuki/autonomous_planning_plugin/blob/01ce890f9b577d48fd5e3eb6eca1572a4d27df68/LICENSE)具有相同 Git blob `0ad25db4bd1d86c452db3f9602ccdbe172438f52`，因此不再复制一份。该上游原文、根 MIT、三份 MIT 来源文本及字体 OFL 都不加状态前言、不重写授权。本文件只说明引用与声明共存，不承诺整个组合可单独按 MIT 分发，也不作兼容性结论。

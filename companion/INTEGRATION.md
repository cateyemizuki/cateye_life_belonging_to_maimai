# companion/ — 虚拟恋人子模块（复刻自 maibot-community_mai-love v2.6.3）

> 来源：`maibot-community.mai-love`（原作者 octmicy，cateye 维护 fork，MIT）。
> **日程生成不复刻**：mai-love 的日程系统由 planning/ 子模块提供，本模块只消费
> `planning.schedule_query.ScheduleQuery`（当前活动 / 未来活动 / 全天摘要）。

## 模块地图

| 文件 | 职责 | 对应源模块 |
|---|---|---|
| `config.py` | patrol / affection / cateye / judge 四个配置段（whitelist→顶层 target 段；llm_log/proactive_log→顶层 admin 段；**新增 relation_type/relation_custom 关系配置**——miss 提示词占位 {relation}、消息口吻/后缀/档位名随关系变化，热更新经 sync_from_config） | config.py |
| `affection.py` | 好感度管理：3 档 + habits 字典 + 每日计数/标记 + 1 秒节流写盘 + flush | affection_manager.py + memory_manager 的存储部分 |
| `relation.py` | **关系系统（简单版）**：relation_type/relation_custom 配置 → RelationProfile（称谓、三档档位名、口吻、后缀池、括号小剧场），patrol/message_service/components 消费 | 关系扩展 |
| `patrol.py` | 巡检调度器完整判定链：日切→静默(fail-safe)→用户冷却→预算(晚安预留)→早安(S)→晚安(S)→想念(A：随机阈值+概率+LLM 把关+驳回冷却+未来日程避让+豁免开关)→日程节点分享(B)→日常搭话(B)；决策日志每轮恰好一条（SKIP_PRIORITY）；触发经 maisaka.proactive.trigger + 计 0.5 发言数 | scheduler.py |
| `message_service.py` | 好感度后缀 AFFECTION_SUFFIXES + 档位2 括号小剧场（10%）+ send_to_target | message_service.py |
| `memory_manager.py` | 用户习惯读写（委托 AffectionManager.habits）+ 个人上下文构建 | memory_manager.py |
| `sender_identity.py` | SenderCache（msg_id→QQ，TTL 24h）+ SessionKindCache（会话→是否群聊）+ 提取助手 | sender_identity.py |
| `screen_context.py` | 屏幕旁白「尾随锚点」注入：ScreenContextStore（planner/replyer 双通道、锚点滑出作废、容量淘汰） | screen_context.py |
| `cateye_client.py` | 恋人电脑联动：connect-hub 跨插件 API（status/screenshot）+ VLM 描述 + 超时与缓存 | cateye_client.py + llm_service.describe_image |
| `holiday_service.py` | 节假日多级降级：timor 整年表 → jiejiariapi → timor 按天 → 本地星期几；3 天 TTL 原子落盘 | holiday_service.py |
| `components.py` | `CompanionComponentsMixin`：5 Hook + 6 Tool + 8 Command + 3 API（方法名 `mai_` 前缀；API 组件名加 `companion_` 前缀避免与 planning 撞名） | plugin.py 组件面 |
| `services.py` | `CompanionServices` 服务束（复用顶层 _affection/_llm/_loggers；`set_persona`/`set_target`/`sync_from_config` 入口） | plugin.py 装配段 |

## 集成方式（已在 plugin.py 完成）

```python
class CateyeSuitePlugin(CompanionComponentsMixin, ...):
self._companion = CompanionServices(self)   # 复用 plugin._data_dir/_affection/_llm/_loggers/_schedule_query
await self._companion.start()               # 日志清理 + 节假日预热 + 巡检（stream 已解析时）
await self._companion.stop()
# 目标会话解析后：self._companion.set_target(qq, stream_id)
# bot 配置热重载：self._companion.set_persona(lover_name, personality) / sync_from_config()
```

## 有意的裁剪 / TODO

- 源插件的 `schedule_generator.py` / `mai_template.json` / `external_schedule.py`（跨插件读日程 API）不复刻——日程由 planning/ 提供；
- 源插件 5 个 Hook 中 `maisaka.replyer.after_response` 的补计数窗口（90s）与 planner 注入的载荷收缩（>8MB 换图片占位）均已保留；
- 决策日志 / LLM 日志合并进 `core/diag.py`（record 既有签名兼容，新增 read_entries/cleanup/update_settings）。

## 上游同步记录（v2.6.2 / v2.6.3，2026-10-03 合并）

| 上游变更 | 本仓库落点 |
|---|---|
| v2.6.2 `LLMService.generate` 失败三态分诊（空正文附 reasoning 证据 / 裸失败 / 非 dict） | `core/llm_utils.py` `_interpret()`（原"未知错误"兜底移除） |
| v2.6.2 `miss_confirm` max_tokens 16 → 512 | 随 v2.6.3 `[judge]` 板块落地（默认 512），`patrol.py` 改走 `judge()` |
| v2.6.2 过期宿主主动任务块剥离（planner + replyer 请求载荷，30 分钟阈值，宁多勿丢） | `core/inject.py`（`without_stale_proactive_blocks` 纯函数）+ `companion/components.py` 两个 Hook 最前调用 |
| v2.6.3 `[judge]` 配置板块（model / max_tokens / temperature，热更新即时生效） | `companion/config.py` `JudgeSectionConfig` → 顶层 `config.py` 挂为 `[judge]` 段（字段校验按本仓库 ge/le 惯例，越界由 pydantic 拒绝而非静默夹取） |
| v2.6.3 `LLMService.judge()` + `generate()` 可选 model 覆盖 | `core/llm_utils.py`：`model` 覆盖只传 model 不传 task_name（宿主按「先任务名、未命中按具体模型名」双解析，见开发文档 §8.3 路由规则）；配置经 `config_provider` 回调动态读 |

部署提示：同实例装有 `cateye_mai_set_avatar` 时须 ≥1.0.2，否则其 `meta=None` 坏条目会让宿主丢弃 `planner.before_request` 链上所有插件的载荷修改（含本版的任务块剥离与旁白注入）。

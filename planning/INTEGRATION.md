# planning/ — 自主规划子模块（复刻自 autonomous_planning_plugin v4.9.1）

> 来源：`xuqian13.autonomous-planning-plugin-v4`（上游 xuqian13，cateye 维护 fork，AGPL-3.0）。
> 许可提示：本子模块为机制复刻（重写实现），若后续大量照搬源码，发布时整体应按 AGPL-3.0。

## 模块地图

| 文件 | 职责 | 对应源模块 |
|---|---|---|
| `config.py` | schedule / inject / planning_proactive / planning_cleanup 四个配置段 | config_models.py |
| `exceptions.py` | LLMError 体系（配额/限流/超时/无效响应） | core/exceptions.py |
| `goal_manager.py` | Goal/GoalManager 单例：长期目标 CRUD + pending_commitment 未来约定 | planner/goal_manager.py |
| `schedule_db.py` | SQLite：schedule_items（日程条目）+ goals（目标/约定）+ metadata，WAL + 损坏自愈 | database/goal_db.py |
| `schedule_generator.py` | 日程生成协调器：多轮生成 + 评分 + 提前达标 + retry 带反馈 + 无睡眠后处理 + 约定匹配落库 + regenerate | planner/schedule_generator.py |
| `schedule_query.py` | 当前/未来活动查询（N 分钟窗口缓存） | ——（骨架保留） |
| `generator/` 子包 | prompt_builder（wake/sleep 锚点动态 JSON 示例 + 衔接演算 + 三餐窗口 + 无睡眠模式）/ schema_builder / response_parser（**控制字符清理**）/ validator（语义校验，只警告）/ quality_scorer（0~1 评分）/ context_loader（跨群历史 + 知识库 + 近 N 天摘要）/ config | planner/generator/* |
| `inject/` 子包 + `inject_service.py` | 意图分类 UserIntent / 注入优化器（冷却+概率+TTL）/ 会话上下文缓存（续注判定）/ 状态分析；planner（items 强 schema 协议）与 replyer（extra_prompt 协作式）两个注入入口 | handlers/inject/* + services/inject_service.py |
| `proactive_service.py` | 60s 轮询：活动切换主动开口（每 stream×活动×日期去重）、早间问好（含需激活）、约定到期强指令（priority=high）、频率调控、睡眠窗口判定 | services/proactive_service.py |
| `auto_scheduler.py` | 每日定时自动生成（当天已有则跳过；指数退避） | planner/auto_scheduler.py |
| `cleanup_service.py` | 过期日程/旧目标/旧日志清理循环 + 守护 auto_scheduler | services/cleanup_service.py |
| `role_judge.py` | 自然语言约定裁判（today/future/reject + 宽松 JSON 解析） | planner/role_judge.py |
| `tools_service.py` / `command_service.py` | 4 个 Tool 实现体 + /plan 命令（图片+文字合并转发，失败降级） | services/tools_service.py、command_service.py |
| `schedule_image.py` + `assets/fonts/` | PIL 日程图片渲染（asyncio.to_thread + 超时，失败静默；pillow 可选依赖） | utils/schedule_image_generator.py |
| `time_utils.py`（窗口迁移/时区管理/能量模型，R0-C2 合并） / `stream_filter.py` / `lru_cache.py` / `parameter_validator.py` | 基础工具照搬 | utils/*、cache/ |
| `components.py` | `PlanningComponentsMixin`：4 Tool + /plan Command + get_current_activity API + 2 HookHandler（方法名 `plan_` 前缀） | plugin.py 组件面 |
| `services.py` | `PlanningServices` 服务束（初始化/启动/停止全部后台循环；重绑 plugin._schedule_gen 为完整管线） | plugin.py 装配段 |

## 集成方式（已在 plugin.py 完成）

```python
class CateyeSuitePlugin(PlanningComponentsMixin, ...):
self._planning = PlanningServices(self)   # 需要 plugin._data_dir/_db/_llm
await self._planning.start()              # cleanup + auto_scheduler + inject 预热 + proactive
await self._planning.stop()
```

## 有意的裁剪 / TODO

- `normalize_plugin_config` 旧版本配置迁移（v4.5→v4.6）未移植（全新安装无历史配置）；
- HomeCard / EventHandler 未移植（宿主侧已确认不触发）；
- `chat.get_group_streams` 未使用（proactive 用 get_all_streams 枚举）。

# planning/ — 自主规划子模块（复刻自 autonomous_planning_plugin v4.9.1）

> 来源：`xuqian13.autonomous-planning-plugin-v4`（上游 xuqian13，cateye 维护 fork，AGPL-3.0）。
> 许可提示：本子模块为机制复刻（重写实现），若后续大量照搬源码，发布时整体应按 AGPL-3.0。

## 模块地图

| 文件 | 职责 | 对应源模块 |
|---|---|---|
| `config.py` | schedule / inject / planning_proactive / planning_cleanup 四个配置段；**M5 在 `[schedule]` 增 `emotion_influence`（0~1，默认 0.6，睡眠大修动态限幅权重）** | config_models.py |
| `exceptions.py` | LLMError 体系（配额/限流/超时/无效响应） | core/exceptions.py |
| `goal_manager.py` | Goal/GoalManager 单例：长期目标 CRUD + pending_commitment 未来约定 | planner/goal_manager.py |
| `schedule_db.py` | SQLite：schedule_items（日程条目）+ goals（目标/约定）+ metadata，WAL + 损坏自愈 | database/goal_db.py |
| `schedule_generator.py` | 日程生成协调器：多轮生成 + 评分 + 提前达标 + retry 带反馈 + 无睡眠后处理 + 约定匹配落库 + regenerate；**M5：`_normalize_items` 把条目 `emotional_tone` 经 `clip_emotional_tone` 裁剪 ±0.5 后落 `parameters`；`_load_emotion_context` 按 `schedule.emotion_influence` 装载睡醒情绪上下文（优先 `plugin._planning.wake_emotion()`，无事件时退化为实时 `mood_text()`）；`_load_low_mood` + `_ensure_recovery_item` 在连续低迷时强制插入一次 rest/social（**空档含首尾** [wake,首条目)/条目间/(末条目,sleep]；无缝日程切最长高强度条目尾段）** | planner/schedule_generator.py |
| `schedule_query.py` | 当前/未来活动查询（N 分钟窗口缓存） | ——（骨架保留） |
| `generator/` 子包 | prompt_builder（wake/sleep 锚点动态 JSON 示例 + 衔接演算 + 三餐窗口 + 无睡眠模式；**M5 增 `emotion_context` 槽 + `build_emotion_context()`**）/ schema_builder（**M5 增可选 `emotional_tone` 字段，逐维 ±0.5**）/ prompt_builder 的 `emotion_context` 与 `low_mood_days` 槽（**M5 增，连续低迷注明「最近有点低落」**）/ response_parser（**控制字符清理**）/ validator（语义校验，只警告；**M5 增 `clip_emotional_tone`**）/ quality_scorer（0~1 评分）/ context_loader（跨群历史 + 知识库 + 近 N 天摘要）/ config；base_generator 持 `emotion_context` 动态上下文字段并透传两个 prompt 构建器 | planner/generator/* |
| `inject/` 子包 + `inject_service.py` | 意图分类 UserIntent / 注入优化器（冷却+概率+TTL）/ 会话上下文缓存（续注判定）；planner（items 强 schema 协议）与 replyer（extra_prompt 协作式）两个注入入口；**M4：状态分析器（state_analyzer）删除，精神状态行改读情绪系统 `mood_text()`（见下）** | handlers/inject/* + services/inject_service.py |
| `proactive_service.py` | 60s 轮询：活动切换主动开口（每 stream×活动×日期去重）、早间问好（含需激活）、约定到期强指令（priority=high）、频率调控、睡眠窗口判定 | services/proactive_service.py |
| `auto_scheduler.py` | 每日定时自动生成（当天已有则跳过；指数退避）；**M5 语义：当天已有日程 → 睡醒情绪上下文不生效，只在重生成（`/cateye_test_plan regenerate` / 次日生成）时注入** | planner/auto_scheduler.py |
| `cleanup_service.py` | 过期日程/旧目标/旧日志清理循环 + 守护 auto_scheduler | services/cleanup_service.py |
| `role_judge.py` | 自然语言约定裁判（today/future/reject + 宽松 JSON 解析） | planner/role_judge.py |
| `tools_service.py` / `command_service.py` | 4 个 Tool 实现体 + /plan 命令（图片+文字合并转发，失败降级） | services/tools_service.py、command_service.py |
| `schedule_image.py` + `assets/fonts/` | PIL 日程图片渲染（asyncio.to_thread + 超时，失败静默；pillow 可选依赖） | utils/schedule_image_generator.py |
| `time_utils.py`（窗口迁移/时区管理/能量模型，R0-C2 合并） / `stream_filter.py` / `lru_cache.py` / `parameter_validator.py` | 基础工具照搬 | utils/*、cache/ |
| `components.py` | `PlanningComponentsMixin`：4 Tool + /plan Command + get_current_activity API + 2 HookHandler（方法名 `plan_` 前缀） | plugin.py 组件面 |
| `services.py` | `PlanningServices` 服务束（初始化/启动/停止全部后台循环；重绑 plugin._schedule_gen 为完整管线）；**M5：订阅 `emotion.overhauled` 缓存睡醒情绪（`wake_emotion()`，含低分辨率 `mood`+`levels`）供日程生成 `emotion_context` 槽；订阅 `emotion.low_mood_day` 缓存连续低迷状态（`low_mood_state()`）供强制插入恢复类活动** | plugin.py 装配段 |

## 集成方式（已在 plugin.py 完成）

```python
class CateyeSuitePlugin(PlanningComponentsMixin, ...):
self._planning = PlanningServices(self)   # 需要 plugin._data_dir/_db/_llm
await self._planning.start()              # cleanup + auto_scheduler + inject 预热 + proactive
await self._planning.stop()
```

## M4 落地（2026-10-03）：planner 注入精神状态行融合（state_analyzer 删除）

- `inject_service._render_unified_prompt` 的「精神状态」行改读 `plugin._emotion.mood_text()`（`_emotion_mood_line()`，与 emotion 注入条目同源，避免两套矛盾的「精神状态」）；情绪未装配/停用/异常 → 省略该行，不再回退静态模板（§八.2 可空降级）；
- replyer 极简 extra_prompt 不再拼活动状态短语；`_compute_activity_aux` 只余剩余分钟一项（`goal_type` 参数保留以兼容调用签名）；
- `inject/state_analyzer.py`（`ActivityStateAnalyzer`，295 行）随 M4 删除——§4.2 planner 注入融合后无消费点（CLEANUP_PROPOSALS §三 预告项），`inject/__init__.py` 导出同步清理；
- `[inject] enable_state_analysis` 死字段已随 A6 删除（全仓无消费点）。宿主配置归一化对未定义键是**宽松**的——
  `PluginConfigBase` 设 `extra="ignore"`（宿主 venv `maibot_sdk/config.py:51`，Runner 归一化链见
  `plugin_runtime/runner/runner_main.py:906` / `maibot_sdk/plugin.py:179-181`），旧 `config.toml`
  残留该键加载不报错、仅被静默丢弃（离线探针 `scratch/_probe_a6_lenient.py` 实证）；宿主部署副本
  `plugins/cateye_test/config.toml` 的同名键留待 M5 部署时清理（窗口期禁写宿主）。

## 有意的裁剪 / TODO

- `normalize_plugin_config` 旧版本配置迁移（v4.5→v4.6）未移植（全新安装无历史配置）；
- HomeCard / EventHandler 未移植（宿主侧已确认不触发）；
- `chat.get_group_streams` 未使用（proactive 用 get_all_streams 枚举）。

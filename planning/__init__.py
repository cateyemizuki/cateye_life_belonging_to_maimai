"""日程规划子系统（来源：自主规划插件 autonomous_planning_plugin v4.9.1，完整复刻）。

模块布局：
- config.py             配置 section（schedule / inject / planning_proactive / planning_cleanup）
- services.py           PlanningServices 服务束（组合根，plugin.py 集成入口）
- components.py         PlanningComponentsMixin（4 Tool + 1 Command + 1 API + 2 HookHandler）
- schedule_db.py        SQLite 持久化（schedule_items + goals 双表）
- goal_manager.py       GoalManager（目标 / 未来约定 / 日程合成视图，单例）
- schedule_generator.py 多轮生成 + 质量评分 + 约定匹配的日程生成器
- generator/            生成管线子组件（prompt / schema / context / validator / scorer / parser）
- role_judge.py         自然语言日程请求的角色裁判
- auto_scheduler.py     每日定时生成循环
- inject/ + inject_service.py  智能注入（意图分类 / 优化器 / 上下文缓存；精神状态行读情绪 mood_text，M4）
- proactive_service.py  活动切换主动开口 / 早间问好 / 频率调控（60s 轮询）
- tools_service.py      4 个 LLM Tool 的业务实现
- command_service.py    /plan 命令业务实现（含日程图片合并转发）
- cleanup_service.py    过期日程 / 旧目标清理循环
- schedule_image.py     PIL 日程图片渲染（pillow 可选，失败静默降级）
- exceptions.py / parameter_validator.py / time_utils.py（含时区管理/能量模型，R0-C2 合并） /
  stream_filter.py / lru_cache.py   公共设施（照搬源插件）
"""

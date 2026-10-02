# 猫眼生活套件（cateye_test）— 整合插件 v0.2.0

> 状态：**三模块整合版（可加载、核心链路可运行）**。把三个来源的功能整合进一个插件，
> 按来源拆为三个子目录（模块化维护），功能尽可能复刻原插件：
>
> 1. **自主规划插件**（autonomous_planning_plugin，上游 xuqian13，AGPL-3.0）→ `planning/`
> 2. **虚拟恋人插件**（maibot-community mai-love，MIT）→ `companion/`
> 3. **QQ 空间模块**（新增，接口依据 SnowLuma v1.12.10 动作面 + 麦麦空间 Maizone v3.0.3 实现路线）→ `qzone/`
> 4. **情绪维护系统**（新增，按《MaiBot情绪维护系统设计文档》实现：VAD 三维模型 + 睡眠期大修）→ `emotion/`
>
> 许可提示：planning/ 为 AGPL 插件的机制复刻（重写实现）；若后续大量照搬其源码，发布时整体应按 AGPL-3.0。
>
> 📖 **命令与工具的详细功能介绍、参数与权限说明见 [USAGE.md](USAGE.md)**；**模块间联动规划与流程图见 [SYNERGY.md](SYNERGY.md)**（昼夜节律闭环：日程⇄情绪⇄关系⇄空间）；**长期演进/维护总纲见 [MAINTENANCE_PLAN.md](MAINTENANCE_PLAN.md)**（联动事件总线、好感度 v2 重设计、模块相互影响矩阵、v1.3.0 可行性核对与 R0+M0~M7 分期路线图，其路线图取代 SYNERGY §5）；**结构与功能精简清单（"重置"）见 [CLEANUP_PROPOSALS.md](CLEANUP_PROPOSALS.md)**；**宿主自测试规程见 [TEST_PROCEDURE.md](TEST_PROCEDURE.md)**（agent 向：注入式集成测试、测试辅助插件、安全红线）。

## 目录结构

```
cateye_test/
├── _manifest.json / plugin.py / config.py
│   ├── plugin.py  = 纯组合根：生命周期 + 服务束装配；组件注册由三个 Mixin 多继承收集
│   └── config.py  = 顶层聚合：plugin / target / admin 基础段 + 各子模块 section 扁平挂载
├── planning/          # 自主规划复刻（详见 planning/INTEGRATION.md）
│   ├── config.py / services.py / components.py
│   ├── goal_manager.py / schedule_db.py / schedule_generator.py / schedule_query.py
│   ├── generator/     # prompt_builder(动态示例+衔接演算) / schema / parser(控制字符清理) / validator / scorer / context_loader
│   ├── inject/ + inject_service.py   # 意图分类 / 注入优化器 / 上下文缓存 / 状态分析
│   ├── proactive_service.py / auto_scheduler.py / cleanup_service.py / role_judge.py
│   ├── tools_service.py / command_service.py / schedule_image.py(+assets/fonts)
│   └── exceptions.py / time_utils.py（窗口/时区/能量，R0-C2 合并） / stream_filter.py / lru_cache.py ...
├── companion/         # 虚拟恋人复刻（日程除外，详见 companion/INTEGRATION.md）
│   ├── config.py / services.py / components.py
│   ├── affection.py / patrol.py / message_service.py / memory_manager.py
│   ├── sender_identity.py / screen_context.py / cateye_client.py / holiday_service.py
├── emotion/           # 情绪维护系统（新增）
│   ├── config.py / services.py / components.py
│   ├── vad.py             # 25 种情绪原型 + 加权距离映射 + 低分辨率描述
│   ├── state_machine.py   # VAD 状态机（指数衰减/增量结算/偏移乘数/睡眠大修）
│   ├── referee.py         # 睡眠裁判 / 人设初始化 / 回复增量评估（独立 LLM 请求）
│   └── service.py         # 状态持久化 / 睡眠检测循环 / 初始化指令主体
├── qzone/             # QQ 空间（新增）
│   ├── config.py / components.py
│   ├── service.py     # 动作层(SnowLuma) + Web CGI 双通道；自动回评 / 评论基线提醒
│   └── cgi.py         # cookie 管理(适配器 get_cookies 多级降级) + QZone Web CGI 直连(含楼中楼回评)
└── core/              # 公共基础设施：llm_utils / diag(决策+LLM 日志) / time_utils / storage / inject(注入投影助手)
```

## 组件注册面（共 39 个组件）

| 类型 | 组件 |
|---|---|
| HookHandler ×10 | planning：`maisaka.planner.before_request`（日程注入，items 强 schema 协议）、`maisaka.replyer.before_request`（extra_prompt 协作式）｜companion：`chat.receive.before_process`（身份缓存）、`chat.receive.after_process`（目标私聊时间）、`maisaka.planner.before_request`（屏幕旁白+好感度后缀）、`maisaka.replyer.before_model_request`（旁白）、`maisaka.replyer.after_response`（补计发言数，OBSERVE）｜emotion：`maisaka.planner.before_request`（情绪底色注入，系统提示词后）、`maisaka.replyer.before_model_request`（情绪底色注入，系统提示词后）、`maisaka.replyer.after_response`（ΔVAD 增量评估，OBSERVE） |
| Tool ×13 | planning：cateye_test_manage_goal_v4 / cateye_test_get_planning_status_v4 / cateye_test_apply_schedule_v4 / cateye_test_update_schedule_v4（角色裁判）｜companion：cateye_test_mai_lover_current_activity / status / schedule / send_message / affection / config｜qzone：cateye_test_life_post_qzone / cateye_test_life_qzone_moments｜emotion：cateye_test_emotion_mood |
| Command ×12 | planning：/cateye_test_plan、/cateye_test_规划（list/regenerate/delete/clear/help，图片+合并转发）｜companion：/cateye_test_mai_status /cateye_test_mai_schedule /cateye_test_mai_affection /cateye_test_mai_help /cateye_test_mai_config /cateye_test_mai_llm_log /cateye_test_mai_diag /cateye_test_mai_test｜qzone：/cateye_test_life（status/qzone[moments/comments/post/reply/like]/diag/help）｜emotion：/cateye_test_emotion_init（人设初始化）、/cateye_test_emotion_config（查看/reset/clear） |
| API ×4 | get_current_activity（planning，兼容原插件返回结构）｜companion_get_current_activity / companion_get_schedule / companion_get_affection_level |

## 各模块功能清单

### planning/ — 日程系统（复刻自主规划插件）
- ✅ SQLite 持久化（schedule_items + 长期目标 goals + 未来约定 pending_commitment + metadata；WAL + 损坏自愈）
- ✅ LLM 多轮日程生成：wake/sleep 锚点**动态 JSON 示例 + 衔接演算**（示例永远与正文约束自洽）、三餐窗口硬约束、无睡眠模式（睡眠时段不生成条目 + 禁字）、**控制字符清理**解析、语义校验（只警告）、质量评分（0~1，≥阈值提前达标）、retry 带上轮反馈、生成上下文（近 N 天日程摘要 / 跨群聊天历史 / 知识库 / 自定义生活阶段）
- ✅ 当天已有日程跳过、模板兜底（骨架继承）、每日定时自动生成、过期清理
- ✅ planner 注入（意图分类：问当前/问未来/闲聊/技术/命令，注入优化器冷却+概率+TTL，会话续注，能量模型）+ replyer 注入
- ✅ 主动行为：活动切换主动开口（去重）、早间问好（含"需激活"）、约定到期强指令、频率调控、睡眠窗口
- ✅ 自然语言约定（角色裁判 today/future/reject → 今日重排或未来约定）
- ✅ /plan 命令（日程图片 PIL 渲染 + 合并转发，失败降级纯文本）与 4 个 LLM 工具

### companion/ — 恋人生活（复刻 mai-love，日程除外；已扩展为关系系统）
- ✅ 好感度 3 档 + habits 记忆 + 每日计数/早晚安/想念标记（JSON 节流落盘）
- ✅ 巡检完整判定链（与源插件对齐）：日切→静默(fail-safe)→用户冷却→预算（晚安预留）→早安/晚安（S）→想念（A：沉默阈值随机采样+概率+LLM 把关+驳回 30 分钟冷却+未来日程避让+豁免开关）→日程节点分享/日常搭话（B）
- ✅ **关系系统（简单版）**：`patrol.relation_type` 配置对方是 bot 的谁（恋人/挚友/维护者/家人/自定义称谓），想念提示词、消息生成口吻、好感度档位名、消息后缀与括号小剧场全部随关系变化（`companion/relation.py` 预设，热更新生效）
- ✅ 触发统一 `maisaka.proactive.trigger`；好感度后缀 + 括号小剧场；message_service 直发
- ✅ **LLM 判定板块 `[judge]`**（同步 mai-love 2.6.3）：想念把关等判定类调用的模型 / 最大 token 数 / 温度独立成段可配（默认跟随主任务、512、0.2），可为把关单独指到便宜、非思考型的任务/模型；把关调用走 `LLMService.judge()`，`/cateye_test_mai_llm_log` 记录实际使用的模型
- ✅ **LLM 失败三态分诊**（同步 mai-love 2.6.2）：「成功但正文为空」（思考型模型把小 max_tokens 预算花在 reasoning 上）不再落进"未知错误"兜底，日志附 reasoning 尾部证据；宿主裸失败与非 dict 结果单独识别
- ✅ **过期宿主主动任务块剥离**（同步 mai-love 2.6.2）：宿主 `enqueue_proactive_task` 的 `<plugin_proactive_task>` 块持久化进聊天历史且从不清理，planner/replyer 请求 Hook 把入队超过 30 分钟的块从本次请求载荷剥离（不回写宿主历史；新块与解析失败条目一律保留）
- ✅ 屏幕感知「尾随锚点」注入（planner/replyer 双通道，锚点滑出作废）+ 恋人电脑联动（connect-hub 截屏 + VLM 描述）
- ✅ 节假日服务（timor/jiejiariapi 多级降级）；决策日志每轮一条；/mai_* 命令组（鉴权 = target_qq 白名单 + 本机控制台，占位值拒绝）

### qzone/ — QQ 空间（新增）
- ✅ 按日程发说说（静默→活动→概率→每日上限→LLM 文案），发布双通道：SnowLuma 动作层优先、Web CGI 直连回退
- ✅ 自动回评：cookie 多级降级获取（适配器 get_cookies→NapCat HTTP→本地文件）→ CGI 读评论（含楼中楼）→ 过滤自己/已处理（LRU 去重落盘）→ LLM 回复（注入 PersonInfo 印象）→ 楼中楼回评（commentId+commentUin+@前缀）→ 立即标记；cookie 不可用降级为评论数基线 + maisaka 提醒
- ✅ 手动命令：/life qzone comments（列评论带序号）、reply 说说序号[.评论序号] 内容（普通评论/楼中楼）、like tid [目标QQ]（CGI 可取好友说说 tid）
- ✅ g_tk 与 SnowLuma 同为 djb2(p_skey)&0x7FFFFFFF；接口来源见 planning 对应说明与 SnowLuma `onebot/actions/qzone.ts`

### emotion/ — 情绪维护系统（新增，按设计文档实现）
- ✅ VAD 外部状态机：LLM 只输出"事件→增量"，状态机维护绝对值；日常指数衰减（decay_rate 可配）→ 加增量 → 静态偏移 → 乘数（V>0 增益/V<0 衰减）→ 裁剪
- ✅ 25 种情绪原型（10 正/3 中/12 负）加权欧氏距离映射（V1.0/A0.8/D1.2），阈值内复合标签（激活度归一化），低分辨率三档描述（模糊化反馈防漂移）
- ✅ 回复后增量评估：replyer.after_response 后用小模型把该轮对话转成 ΔVAD（±0.5 限幅+节流），记入当天情绪日志（原始事件流）
- ✅ 睡眠期情绪大修：进入日程睡眠时段（消费 planning 日程的 sleep 条目）→ 睡眠裁判 LLM 独立请求（当前 VAD+当天日志+前后日日程+长期状态+睡眠时长，reason 必须引用日程原文）→ ΔVAD_sleep ±0.8 限幅 → 向锚点收敛（睡眠时长倒 U 型恢复系数）→ 清空日志
- ✅ 人设初始化：/cateye_test_emotion_init 由宿主人设推导锚点/偏移/乘数（数值约束校验裁剪，落盘重启生效）；/cateye_test_emotion_config 查看/reset/clear
- ✅ 情绪底色注入 replyer（模糊化描述 + 严禁直说情绪标签词汇）；Tool 查询心情

## manifest 声明（16 能力 + 2 依赖）

`send.text/image/forward`｜`config.get`｜（能力清单同前，情绪模块无新增能力）`llm.generate`｜`chat.get_all_streams/get_stream_by_user_id/get_private_streams/get_stream_by_group_id`｜`message.get_by_time_in_chat`｜`maisaka.proactive.trigger/context.append`｜`frequency.set_adjust`｜`knowledge.search`｜`api.call`｜`database.get`；依赖 `httpx>=0.24`、`Pillow>=9.0`（日程图片，缺失自动降级）。

## 快速测试

> ⚠️ **部署依赖**：若同一 MaiBot 实例装有 `cateye_mai_set_avatar`，必须升级到 **1.0.2+**（或先部署它）——
> 旧版的 `meta=None` 块条目会让宿主把 `planner.before_request` 链上**所有**插件的修改整体丢弃
> （含本插件的过期任务块剥离与屏幕旁白注入）。

1. 放进 MaiBot 的 `plugins/` 目录重启；WebUI 里配置 `目标 QQ`（占位值时主动私聊/命令鉴权保持关闭）；
2. `/cateye_test_life status` 总览四模块；`/cateye_test_plan` 看日程（首次会触发生成）；自然聊天问「在干嘛」验证注入与 `mai_lover_current_activity`；
3. 观察决策日志 `data/plugins/github.cateye.cateye-suite/decision_logs/`；`/cateye_test_mai_diag`、`/cateye_test_life diag` 查判定链；
4. 空间模块：`/cateye_test_life qzone status` 看通道与 cookie；`/cateye_test_life qzone post 测试` 手动发一条；`comments`+`reply` 验证楼中楼；情绪模块：`/cateye_test_emotion_init` 初始化后聊天观察语气变化；
5. 离线测试：`scratch/smoke_test_cateye_test.py`（stub SDK 收集 39 组件 + 配置模型 + 能力核对）与 `scratch/behavior_test_cateye_test.py`（16 组核心算法回归，含 mai-love 2.6.2/2.6.3 合并回归）。

## 骨架占位 / TODO

| 事项 | 说明 |
|---|---|
| 空间发图说说 | 动作层已支持 images 自动上传；CGI 通道的 cgi_upload_image → pic_bo/richval 待接 |
| 空间好友动态自动阅读（秒赞秒评） | **经确认不做** |
| 定时发说说时间表 | **经确认不做**（日程驱动 + 概率覆盖） |
| planning 兼容契约 | ScheduleGenerator/ScheduleQuery/AffectionManager/PatrolService 等保留了骨架版方法签名（新增参数均带默认值），旧调用点无需改动 |
| 情绪扩展 | 中性情绪细分（发呆/疏离）、人设动态偏移映射（§七.2.3）、planner 通道情绪注入、与长期日程高压状态联动的偏移调制（§九.4.4） |
| 许可 | planning/ 参考 AGPL-3.0 插件机制；发布前请复核开源义务 |

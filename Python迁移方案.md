# GoGo Agent 多智能体差旅助手 · Python 迁移方案（LangChain + LangGraph）

> 本文是给下一个执行者的**完整交接文档**。目标：把 `src/main/java` 的 Java/AgentScope 版本，
> **只换语言和框架、业务逻辑与功能不变**，用 Python 的 LangChain + LangGraph + FastAPI 重写。
> 本文已把我读完全部关键 Java 源码后梳理出的架构、业务逻辑、LangChain/LangGraph 职责边界、
> 逐模块移植方案、以及「已完成 / 待完成」清单全部写清，照着做即可，不必再重新读一遍 Java。

---

## 0. 一句话结论

- **LangChain 管「单点能力」**：模型抽象、工具封装、结构化输出、RAG 检索、消息/记忆。
- **LangGraph 管「编排」**：整条差旅流水线（三层意图短路 → 条件改写 → 意图路由 → 子智能体即工具的可递归调度 → Human-in-the-Loop 暂停/恢复）就是一张 `StateGraph`，用条件边和 checkpointer 表达。
- 业务逻辑（12 条差旅政策规则、规划评分引擎、六维审核、行程冲突检测、意图 L1 正则表等**确定性逻辑**）照抄 Java，一行语义都不改。

---

## 1. 现状盘点（历史基线）

以下是迁移开始时的历史盘点。当前执行结果见文档末尾的“执行状态”，不要把本节的“还没写”当成当前代码状态。

`gogo-agent/` 下当时有两套 Python 残留，**都不是忠实移植**：

| 位置 | 现状 | 判断 |
|---|---|---|
| `gogo-agent/app/` | 一个 `planner→researcher→analyst→writer→critic` 的**通用研究报告流水线**（找目的地/估算费用/写 Markdown） | ❌ 与真实差旅业务（申请→审批→规划→审核→预订→报销）**完全无关**，是占位 demo |
| `gogo-agent/backend/` | 第二版，`travel_graph.py` 还是 `planner→researcher→analyst→writer→critic` 线性图；`routers.py` 有部分真接口但 `chat` 只调那个 demo 图 | ❌ 骨架结构对，但核心业务没移植 |

**结论：`app/` 目录直接删除；`backend/` 目录保留目录结构但内容整体重写。** Java 版 `src/` 一字不删，作为逐项比对基准。

### 我已经写好的部分（下一步直接复用，不要重写）

我已完成 `backend/` 的**地基层**，这些是「忠实移植」且可直接用：

- `backend/config/settings.py` — 全部配置收敛点（三档模型名、DB URL、城市分级、熔断参数、MCP 白名单）
- `backend/domain/models.py` — SQLAlchemy 2.0 的 11 张表（与 `schema.sql` 字段一一对应）
- `backend/domain/schemas.py` — API DTO
- `backend/infrastructure/db.py` — 建表 + 种子数据（等价 schema.sql 的 INSERT，含 12 条政策规则）
- `backend/infrastructure/llm.py` — `strong_model()/stable_model()/strong_model_with_thinking()` 三档模型工厂
- `backend/infrastructure/stores.py` — 进程内 KV（等价 Redis 的规划结果/搜索候选/会话/熔断态）
- `backend/infrastructure/repositories.py` — 全部仓储（用户/订单/审批/预订/政策/聊天/偏好/ApiKey）
- `backend/infrastructure/security.py` — 脱敏 + ApiKey 混淆
- `backend/infrastructure/bootstrap.py`、`database.py`(shim)、`__init__.py`
- `backend/intent/` — **三层意图识别已完整移植**：`category.py`（16 类意图枚举）、`result.py`（统一结果）、`rule_matcher.py`（L1 全部正则规则表 1:1）、`vector_matcher.py`（L2 向量 + 本地零依赖 embedding）、`router.py`（L0/L1/L2 编排）、`seed.py`（L2 种子语料）
- `backend/services/` — `policy_service.py`（政策+舱位等级）、`order_service.py`（订单生命周期事务）、`approval_service.py`、`booking_service.py`、`user_service.py`、`auth_service.py`、`preference_service.py`、`chat_service.py`、`sse.py`（完整 SSE 事件协议）、`circuit_breaker.py`（三态熔断）、`llm_services.py`（标题/推荐两个轻量 LLM 服务）、`__init__.py`
- `backend/memory/long_term.py` — 长期记忆（record/retrieve + 请求级缓存）
- `backend/prompts.py` — 提示词加载器（复用 `src/main/resources/prompts/*.md`，支持 `{{fragment}}` 引用）

### 当时还没写的（历史记录）

1. `backend/tools/` — 14 类工具用 LangChain `@tool` 封装
2. `backend/agents/` — 9 个智能体 + 公共底座
3. `backend/workflow/` — LangGraph 流水线（核心）
4. `backend/api/` — FastAPI 路由（SSE + REST）+ `cli.py`
5. `backend/rag/knowledge.py` 重写、`backend/core/state.py` 重写为 LangGraph 状态
6. 删除 `app/`、清理旧 `backend` 残留文件、写测试

---

## 2. 目标架构总览（与原 Java 一一对应）

Java 请求主流程（一次对话）：`ChatController(SSE) → AgentPipelineService → (QueryRewriting → IntentRecognition → Master → 子智能体) → 落库`。

Python 版保持同一张图，只是把「AgentScope 的 ReActAgent + Reactor 流水线」换成「LangGraph 的 StateGraph」。

```
用户消息
  │
  ├─ L1/L2 快速意图识别（原始问题，不触发 LLM）
  │     ├─ 命中 ─► 跳过「问题改写」，直接调度
  │     └─ 未命中 ─► QueryRewriting 改写 ─► 重走完整 L1/L2/L3
  │
  └─ 调度：统一交给 MasterAgent 路由（单意图直跳已回滚，见 Java 注释）
        MasterAgent 把 4 个子智能体当「工具」调用：
          itinerary_manage_agent / itinerary_plan_agent / info_agent / booking_agent
          其中 itinerary_plan_agent 直接调用 ItineraryReviewTools；已废弃的
          ItineraryReviewAgent 不在 Master 或 Plan 的运行拓扑中
```

---

## 3. LangChain vs LangGraph 职责划分（你最关心的部分）

### 3.1 LangChain 负责——「会做什么」

| Java 原物 | LangChain 对应物 | 说明 |
|---|---|---|
| `Model`（strong/stable/strongWithThinking，DashScope） | `ChatOpenAI`（`langchain_openai`，DashScope 兼容 OpenAI 协议） | 三档模型只是 model 名 + temperature 不同，已在 `infrastructure/llm.py` 建好工厂 |
| `@Tool` 注解的 14 类工具 | `@tool` 装饰器 → `BaseTool` | 每个 Java 工具方法 → 一个 Python 函数，加 docstring 作为 description（工具名/参数名/描述照抄 Java 的 `@Tool(name=...)/@ToolParam`） |
| `AgentBase`（QueryRewriting/IntentRecognition 单次 LLM 调用） | `model.with_structured_output(PydanticModel)` 或 `ChatPromptTemplate \| model` | 纯文本/结构化输出的单次调用，**不需要 ReAct 循环** |
| `Knowledge`（3 个 RAG 库 + 意图向量库） | `VectorStore` + `Retriever`（`langchain_community`/`langchain-chroma`） | 景点/差旅政策/差旅指南三个知识库 + L2 意图种子库 |
| 消息类型 `Msg` | `SystemMessage/HumanMessage/AIMessage/ToolMessage` | 上下文拼接 |
| `LongTermMemory`（百炼 AGENT_CONTROL） | 自定义 `record_to_memory/retrieve_from_memory` 工具（已建 `memory/long_term.py`） | 语义接口一致，可换向量库 |
| `AutoContextMemory`（历史压缩） | LangChain 的 `trim_messages` / summarization | token 优化 |

### 3.2 LangGraph 负责——「怎么串」

| Java 原物 | LangGraph 对应物 | 说明 |
|---|---|---|
| `AgentPipelineService`（条件流水线） | `StateGraph` + `add_conditional_edges` | **这就是 LangGraph 的核心用途**：把「命中/未命中 → 走哪条分支」写成条件路由 |
| `AgentSessionContext` + `ThreadLocal` | `TypedDict` 状态（`TravelState`）+ 节点间传参 | 不再需要 ThreadLocal，状态在图里显式流转 |
| `SessionPersistenceHook`（MySQL 会话） | `checkpointer`（`SqliteSaver`/`PostgresSaver`） | 多轮记忆持久化 + 会话续跑 |
| `SubAgentTool`（子智能体即工具，可递归） | ① 子 agent 用 `create_react_agent` 包成 `BaseTool` 挂给 Master（**推荐，最贴 Java 语义**）；或 ② 子 agent 作为子图节点 | Plan 内嵌 Review 同理 |
| `Tool Suspend/Resume`（Human-in-the-Loop `ask_user`） | `interrupt()` + `Command(resume=...)` | 工具暂停 → 前端表单 → 用户提交 → 续跑 |
| SSE 事件推送（`ProgressNotifierHook`） | `graph.astream(..., stream_mode="messages/updates")` 逐 token/逐事件 yield | 事件名沿用 Java：message/thinking/progress/travel_data/plan_update/user_interaction/suggestions/agent-switch/interrupted/done |
| `AgentExecutionRegistryHook`（优雅中断） | `graph.astream` 的取消（`asyncio` 取消 + `interrupt`） | 用户点「停止生成」 |
| `ToolCircuitBreakerHook` | 工具调用前的装饰器/包装（已建 `services/circuit_breaker.py`） | 熔断是「工具级」能力，放 LangChain 工具侧 |

### 3.3 一句话记忆口诀

> **模型、工具、检索、记忆、结构化输出 → LangChain；路由、循环、状态流转、暂停恢复、持久化、流式 → LangGraph。**

判断标准：如果某段逻辑是「**多个节点/智能体按条件互相调用**」，放 LangGraph；如果只是「**单个模型/工具/检索器的能力**」，放 LangChain。

---

## 4. Java → Python 目录映射表

| Java 包（`com.gogo.travel.*`） | Python 目录 | 迁移内容 |
|---|---|---|
| `agent/`（9 个 Agent + `BaseSubAgent`） | `backend/agents/` | 每个 Agent 一个文件，`base.py` 沉淀公共工具/模型/Hook |
| `agent/intent/` | `backend/intent/` | ✅ 已移植 |
| `agent/tools/` | `backend/tools/` | 14 类工具用 `@tool` 封装 |
| `agent/service/AgentPipelineService` | `backend/workflow/pipeline.py` | LangGraph 流水线 |
| `agent/hook/` | `backend/hooks/` + LangGraph 原生能力 | SSE 推送、熔断、会话持久化、时间注入 |
| `agent/memory/` | `backend/memory/` | ✅ 已移植长期记忆 |
| `agent/session/` | `backend/infrastructure/stores.py` + checkpointer | 会话态 KV |
| `business/*/entity` | `backend/domain/models.py` | ✅ 已移植 |
| `business/*/service` | `backend/services/` | ✅ 已移植（订单/审批/预订/政策/用户/鉴权/聊天/偏好） |
| `business/*/repo/mapper` | `backend/infrastructure/repositories.py` | ✅ 已移植 |
| `controller/` | `backend/api/` | FastAPI 路由 |
| `config/`（application.yml 等） | `backend/config/settings.py` | ✅ 已移植 |
| `resources/prompts/*.md` | **原样复用**（`backend/prompts.py` 加载） | ✅ 已移植加载器 |
| `resources/db/schema.sql` | `backend/infrastructure/db.py` 建表 | ✅ 已移植 |
| `resources/intent-seed.yml` | `backend/intent/seed.py` | ✅ 已移植 |
| `resources/city-tier.yml` | `settings.py` 里的城市分级 | ✅ 已移植 |

---

## 5. 逐模块迁移方案（关键：每个模块的移植要点）

### 5.1 三层意图识别（✅ 已移植，这里说明设计，便于验证）

**L1 规则**（`IntentRuleMatcher`，`backend/intent/rule_matcher.py`）：
- 15 条规则按优先级排序，关键词用正则 + `negativeKeywords` 排除跨类干扰。
- 规则顺序编码了消歧语义：**报销/政策/审批/取消/修改 > 泛化查询/预订**，泛化预订放最后。
- 多意图守卫：按 `[，。；！？!?;,、] | 然后|接着|顺便|… | 还要|还想|… | 和|跟` 拆子句逐句匹配；跨 ≥2 个目标子智能体命中 → `AMBIGUOUS`（放行 L3）；寒暄子句不参与歧义判定。
- 已把全部正则规则原样搬进 `_Rule` 列表，`evaluate()` 返回 `HIT/AMBIGUOUS/MISS`。

**L2 向量**（`IntentVectorMatcher`，`backend/intent/vector_matcher.py`）：
- Top-2 检索，top-1 相似度 ≥ 0.75 命中；top-1/top-2 分属不同意图且分差 < 0.05 → 放行 L3。
- 相似度 ≥0.85→high，≥0.75→medium，否则 low。
- **关键改动**：Java 用 DashScope embedding（需网络），Python 版默认用「字符 n-gram 哈希 + 余弦」本地零依赖计算；配置 `GOGO_USE_LLM` + embedding 后可换成真实 embedding。种子语料已从 `intent-seed.yml` 搬到 `seed.py`。

**L3 LLM 兜底**（`IntentRecognitionAgent`，见 5.3）：
- 单次 LLM 调用，无 ReAct 循环，用 `stableModel`，系统提示词 `intent-recognition-agent-system.md`。
- 输出 schema 必须与 L1/L2 的 `to_dict()` 完全一致：`{intents[], primary_intent, multi_intent, overall_reason}`。

**编排**（`IntentRecognitionRouter`，`backend/intent/router.py`）：
- L0 结构启发（句长 ≥10 且含强连词且连词位置 ≥4 → 疑似多意图，跳过 L1/L2 交 L3）→ L1 → L2。

### 5.2 条件流水线（LangGraph 核心，下一个执行者重点做）

对应 `AgentPipelineService.java`，在 `backend/workflow/` 里用 `StateGraph` 表达：

```
START
  │
  fast_intent(node): 用原始问题跑 L1/L2（intent_router.route，只 L1/L2，不触发 L3）
  │
  conditional:
    ├─ 命中 → master_dispatch（跳过 rewrite）
    └─ 未命中 → rewrite(node: QueryRewritingAgent 单次 LLM) → full_intent(node: L1/L2/L3) → master_dispatch
  │
  master_dispatch(node): 调 MasterAgent（tool-calling），Master 自行决定调哪个子 agent
  │
  END
```

要点：
- `TravelState`（TypedDict）字段建议：`session_id, user_id, messages, original_question, rewritten_question, intent_json, final, trace, active_agent`。
- `dispatchByIntent` 的输入拼接照抄 Java：Master 输入 = `[SYSTEM("问题改写结果：\n..."), SYSTEM("意图识别结果：\n..."), *原始消息]`。
- 直跳子智能体逻辑（`tryPlanDirectDispatch`）**已回滚**（Java 里 @Deprecated 注释掉），Python 不要实现，统一走 MasterAgent。
- 三条快捷入口照抄：`executeFullPipeline` / `executeFromIntentRecognition`（跳过改写）/ `executeMasterAgentDirectly`（跳过改写+识别，用于「确认/继续」continuation）。
- `isInterruptRecovery` 判断中断恢复文本，中文提示 `"已停止生成。请告诉我接下来有什么可以帮您的？"`。
- 会话续跑：`activeAgentName` 为 `MasterAgent`→直接续跑 Master；为 `IntentRecognitionAgent`→从意图识别开始；否则走完整流水线。

### 5.3 Agent 拓扑 + 2 个轻量服务

统一底座 `backend/agents/base.py`（对应 `BaseSubAgent`）：公共工具（policy/orderRead/bookingRead/userInfoRead/Write/destinationLive）、三档模型、工具超时配置（1 分钟、最多 3 次、退避 3 秒、仅网络错误重试）、公共 Hook（执行日志/进度推送/会话持久化/熔断/CLI 结果压缩/动态时间注入）。

每个智能体用 `create_react_agent(model, tools, prompt)` 包成 LangGraph 子图或 `BaseTool`，模型/工具/maxIters 照抄下表：

| 智能体 | 模型 | maxIters | 工具 | 提示词 md |
|---|---|---|---|---|
| MasterAgent | strong | 15 | `ask_user` + 4 个 subAgent 工具（manage/plan/info/booking） | master-agent-system.md |
| ItineraryManageAgent | strong | 10 | orderWrite, orderRead, bookingRead, conflict, userInfoRead, userInfoWrite | itinerary-manage-agent-system.md |
| ItineraryPlanAgent | strong+thinking | 30 | policy, orderRead, bookingRead, userInfoRead/Write, apiKey, destinationLive(cb), planner, weatherMCP, visaMCP, ItineraryReviewTools, planHtml + SkillBox(tuniu-cli/html-plan) | itinerary-plan-agent-system.md |
| ItineraryReviewAgent | stable | 8 | policy, destinationLive, review | itinerary-review-agent-system.md |
| InfoAgent | stable | 5 | policy, destinationLive(cb), weatherMCP, visaMCP + 3 个 RAG 知识库(AGENTIC) | info-agent-system.md |
| BookingAgent | strong+thinking | 10 | orderRead, bookingRead, bookingWrite, userInfoRead/Write, apiKey + SkillBox(tuniu-cli) | booking-agent-system.md |
| ReimbursementAgent | — | — | 占位（Java `build()` 直接 return null，源码标 todo A2A） | reimbursement-agent-system.md |
| QueryRewritingAgent | stable | 单次 | 无工具（AgentBase） | query-rewriting-agent-system.md |
| IntentRecognitionAgent | stable | 单次 | 无工具（AgentBase，内部 L1/L2/L3） | intent-recognition-agent-system.md |
| ConversationTitleService | fast/stable | 单次 | 无工具（@Service，非 ReAct） | conversation-title-agent-system.md |
| QuestionRecommendationService | fast/stable | 单次 | 无工具（@Service，非 ReAct） | question-recommendation-agent-system.md |

> 注意：**MasterAgent 的「子智能体即工具」**是最核心的架构点。Master 仅注册 Manage/Plan/Info/Booking 四个子 Agent。Plan 不再内嵌 Review Agent，而是调用 `ItineraryReviewTools`；该工具内部会临时并行构造 TripExperience/Resilience/Preference 三个主观评估器。`ItineraryReviewAgent` 已标记 `@Deprecated`。

### 5.4 工具层（14 类，用 LangChain `@tool`）

工具名/参数/描述**全部照抄 Java**（我已用 grep 提取到全部工具签名，见下表；参数默认值和必填项也照抄）。

| 工具类 | 工具方法 | 移植要点 |
|---|---|---|
| PolicyTools | `query_travel_policy(city)`, `check_travel_policy(city, order_summary)` | ✅ 服务层已建 `travel_policy_service`，工具只做缓存+序列化（会话级 policy 缓存，Java 用 `sessionCtx.getTravelPolicy`） |
| UserInteractionTools | `ask_user(question, ui_type, options?, fields?, ...)` | Human-in-the-Loop：调用后 LangGraph `interrupt()` 暂停，等前端回复 |
| TravelOrderReadTools | `query_travel_order(order_id?, status?, start_date?, end_date?)`, `query_approval_status`, `check_travel_time_validity`, `check_travel_order_approval` | 用 `travel_order_repository`/`approval_repository` |
| TravelOrderWriteTools | `submit_travel_approval(destination, departure_city, departure_date, return_date, purpose)`, `cancel_travel_order(order_id, reason?, force?)`, `modify_travel_order(order_id, ..., force?)` | ✅ 服务层已建 `travel_order_service`（创建并提交/取消/修改重提），工具只做日期格式校验 + 调服务 |
| TravelOrderConflictTools | `check_travel_order_conflicts(departure_city, destination, departure_date, return_date, exclude_order_id?)` | 见 6.3 冲突检测算法 |
| ItineraryPlannerTools | `plan_itinerary(origin, destination, departure_date, return_date, preferences?, scores?, policy?, weather_summary?)`, `get_candidates`, `get_proposals` | 见 6.2 规划评分引擎，**重点** |
| ItineraryReviewTools | `review_itinerary(origin, destination, departure_date, policy, weather_summary, news_summary, user_preferences)` | 见 6.4 六维审核 |
| BookingReadTools | `query_booking_record(booking_id?, travel_order_id?, biz_type?, status?)` | 用 `booking_repository` |
| BookingWriteTools | `cancel_booking(booking_id, reason?)` | 用 `booking_service.cancel` |
| UserInfoReadTools | `query_user_contact_info`, `query_user_base_location` | 用 `user_repository.get_profile` |
| UserInfoWriteTools | `update_user_contact_info(...)`, `update_user_base_location(base_city)` | 用 `user_repository.upsert_profile` |
| ApiKeyTools | `check_flight_api_key`, `save_flight_api_key`, `check_tuniu_api_key`, `save_tuniu_api_key` | 用 `api_key_repository` + `security.encrypt_api_key` |
| DestinationLiveTools | `query_weather(city, date?)`, `query_destination_news(city, topic?)` | 外部 HTTP；无 Key 时优雅降级返回占位；注册进熔断组 |
| PlanHtmlTools | `save_plan_html(html, title)` | 存 `travel_order.plan_html_url`（MinIO 换成本地文件/对象存储抽象） |
| ReimbursementTools | `ocr_invoice`, `generate_expense_report`, `submit_reimbursement` | 占位（Java 未实现） |

**工具打包**：LangChain 的 `@tool` 装饰器 + 类型注解（`str/int/float/Optional[...]`）会自动生成 JSON schema，等价 Java 的 `@ToolParam`。每个工具文件末尾提供一个 `tools() -> list[BaseTool]` 聚合函数。

### 5.5 RAG / MCP / Skill / 长期记忆 / 熔断 / SSE（外围能力）

- **RAG（3 个知识库）**：`backend/rag/knowledge.py` 建 `attraction_knowledge / policy_knowledge / guideline_knowledge` 三个 Retriever。数据源在 `src/main/resources/dataset/`（`tourist_attraction.xlsx`、`business_travel_policy.docx`、`business_travel_guidelines.docx`）。InfoAgent 用 `create_react_agent(..., tools=[retriever.as_tool()])` 实现 AGENTIC（智能体自主决定何时检索）。无 embedding API 时降级为本地 BM25/关键词检索。
- **MCP（天气/签证）**：`langchain-mcp-adapters` 加载 `weather-mcp`（Streamable HTTP）和 `orizn-visa-mcp`（stdio `npx -y orizn-visa-mcp`）。工具白名单照抄 `settings.weather_mcp_enabled_tools` / `orizn_mcp_enabled_tools`。无 Key 时优雅降级。
- **Skill（tuniu-cli 等）**：这些是外部 CLI（途牛/航班管家/rolling-go-hotel），Python 版**保留 `src/main/resources/skills/*.md` 原样**，在 `base.py` 里实现一个「读 SKILL.md 注入上下文 + 白名单 shell 执行」的适配器（对应 Java SkillBox + ShellCommandTool）。无第三方 Key 时下单动作返回「未配置第三方凭证」占位。
- **长期记忆**：✅ `backend/memory/long_term.py` 已建 `record_to_memory/retrieve_from_memory`，MasterAgent 和 PlanAgent 挂这两个工具。
- **熔断**：✅ `backend/services/circuit_breaker.py` 已建三态熔断，包装在 `query_weather/query_destination_news` 工具调用外层。
- **SSE**：✅ `backend/services/sse.py` 已建全部事件构造器，API 层用 `StreamingResponse` + `graph.astream` 逐事件 yield。

---

## 6. 关键业务逻辑细节（移植时最容易错的点，照此实现）

### 6.1 差旅政策（12 条规则 + 合规校验）

- 规则：4 职级区间（P8+/P7/P6/P5-）× 3 城市等级（一线/新一线/其他）= 12 条，字段见 `schema.sql` 的 `travel_policy_rule`（✅ 已种子）。
- 城市分级：一线（北上广深）、新一线（成都杭州等 20 城）、二线（济南等 16 城）、其余「其他」。**政策表城市等级只区分一线/新一线/其他**（二线归「其他」）。✅ 已在 `policy_service.py`。
- 职级解析：`"P7" → 7`（`re.sub(r"[^0-9]","",level)`）。
- 合规校验 `check_travel_policy`：`FLIGHT` 比舱位、`HOTEL` 比 `hotelLimit`（amount≤0 显式报错防假阴性）、`TRAIN` 比席别。
- 舱位等级（`CabinRankUtil`）：经济舱=1 < 商务舱=2 < 头等舱=3；二等座=1 < 一等座=2 < 商务座=3；`经济舱/商务舱` 用 `/`、`,`、`，` 切分取允许列表；未知舱位回退字符串匹配。✅ 已在 `policy_service.is_cabin_compliant`。

### 6.2 规划评分引擎（`plan_itinerary`，重点照抄 `ItineraryPlannerTools.java`）

这是纯确定性数学，移植时**一行语义都不能变**：

1. **读候选**：`search_candidate_store`（对应 Redis）里有 `transport_options[]` + `hotel_options[]`（含 id/type/时间/价格/舱位/品牌）。
2. **切去/返程**：优先级 `direction 字段 > 出发城市匹配 > 出发日期匹配`（`classifyDirection`，注意 `cityMatches` 是「任一以另一开头，至少比较 2 字符」）。
3. **笛卡尔积**：去程 × 酒店 × 返程；过滤条件「返程出发必须晚于去程到达」；算 `total_price = 去 + 返 + 房价×晚数`、`stay_hours`、`total_transit_min`。
4. **政策软约束**（`PolicyChecker`）：比对 policy 里的酒店限额/舱位，产出 `policy_score + policy_violations + warnings`。
5. **体验分**（`ExperienceScorer`）：红眼航班/晚到达/长通勤/恶劣天气（暴雨/大雾/大风/雷暴关键词，恶劣天气时飞机扣分优先高铁）→ `experience_score_raw + experience_flags`。
6. **归一化**：`CandidateRanker.normalizeScores`，维度权重固定 `{time:0.20, price:0.10, preference:0.40, experience:0.30}`。
7. **偏好分合成**：`(去+住+返)/3`，LLM 在 `scores` 参数里给 `{transport_scores:{T1:{score,basis}}, hotel_scores:{H1:{score,basis}}}`；空/`"auto"` 时自动中性分 50。
8. **排序 + 4 类代表方案**：`CandidateRanker.rankAndBuild` 输出带 `tags` 的 4 类方案（如「最省时」「最省钱」「最舒适」「综合最优」）。
9. **落库**：`itinerary_plan_store.save(userId, ...)`（✅ 已有 store）。

配套工具 `get_candidates` / `get_proposals` 供审核修复阶段读取候选池和当前方案。

### 6.3 行程冲突检测（`check_travel_order_conflicts`）

- 同员工时间重叠：`[departure_date, return_date]` 与已有生效单（DRAFT/SUBMITTED/APPROVED）区间重叠。
- 跨城通行合理性：用 `city_pair_minutes`（✅ 已在 settings）高频城市对最短衔接时长；两单「目的地≠下一单出发地」且衔接时间不足 → 物理不可能。
- 输出 `{has_conflict, total_conflicts, conflicts[{type, severity(HIGH/MEDIUM/LOW), order_id, order_summary, description, suggestion}], summary}`。
- `exclude_order_id` 用于修改场景排除自身。

### 6.4 六维审核（`review_itinerary`）

对应 `ItineraryReviewTools` + `tools/review/` 包：完整性 / 时间合理性 / 出发目的地 / 预算 / 舱位合规 / 路径 / 环境合理性（天气资讯）——输出审核报告 + 具体修改建议 + `remediation_priority`（剔除/补搜/调整）。主观维度（差旅体验/行程韧性/偏好）由 LLM 评估器给分，客观维度（时间/预算/舱位）用确定性规则。

### 6.5 订单生命周期（✅ 已建 `order_service.py`，供工具调用）

状态机：`DRAFT → SUBMITTED → APPROVED/REJECTED → COMPLETED/CANCELLED`。
- **提交**：①差旅单落库 DRAFT → ②审批单 PENDING → ③差旅单 SUBMITTED 并回写审批 ID（三步原子）。
- **取消**：差旅单 CANCELLED + 同步撤销关联审批单；APPROVED 状态需 `force=true` 二次确认。
- **修改**：撤销旧审批 → 差旅单重置 DRAFT → 提交新审批 → SUBMITTED 关联新审批。
- **审批决策**：审批单 PENDING → APPROVED/REJECTED，差旅单同步状态（管理员接口 + 钉钉回调共用）。

### 6.6 预订落库（`BookingPersistenceHook`）

预订成功后解析平台返回（途牛下单/取消 CLI 结果）自动写 `booking_record`（对 LLM 透明），含内外部单号、`travel_order_id`、统一状态（CREATED/PENDING_PAYMENT/PAID/CONFIRMED/COMPLETED/CANCELLED/REFUNDED/FAILED）。✅ 表结构已建，服务层 `booking_service` 已建。

---

## 7. 已完成 vs 待完成（执行顺序建议）

**已完成**（可直接用，见第 1 节）：config / domain / infrastructure / intent / services / memory / prompts 加载器。

**待完成**（按依赖顺序做）：

1. `backend/core/state.py` — 把旧 `TravelAgentState` 重写为 LangGraph 状态（加 `messages`、`intent_json`、`rewritten_question`、`active_agent` 等字段）。
2. `backend/tools/` — 14 类工具（先写纯确定性工具：policy/order/booking/userInfo/planner/review/conflict，再写外部工具：weather/news/apiKey/ask_user/planHtml）。
3. `backend/agents/base.py` + 9 个 agent 文件 + 2 个轻量服务（服务层已建，agent 层主要是 `create_react_agent` 组装 + 工具挂载）。
4. `backend/workflow/pipeline.py` + `graph.py` — LangGraph 流水线（条件短路 + 路由 + 子 agent 工具）。
5. `backend/rag/knowledge.py` — 3 个 RAG 检索器。
6. `backend/api/`（`main.py`/`routers.py`/`dependencies.py`/`contracts.py`）+ `backend/cli.py` — FastAPI + SSE。
7. 删除 `app/` 目录；清理旧 `backend` 残留（`observability.py` 保留、`services/intent_service.py` 删除、`core/state.py` 重写、`agents/travel_agents.py` 重写）。
8. `tests/` — 至少覆盖：意图 L1 规则命中/歧义、政策合规、规划评分、冲突检测、图编译冒烟。
9. 更新 `pyproject.toml` 依赖（补 `sqlalchemy`、`langchain-community`、`langchain-mcp-adapters`、`python-dotenv`），重写 `.env.example`。

---

## 8. 验证方案

1. **零 Key 冒烟**（`GOGO_USE_LLM=false`）：`python -m backend.cli "帮我查一下差旅政策"` 应走 L1 命中 → 政策查询，不调 LLM。
2. **意图识别单测**：喂「你好，帮我订机票」→ 不应判寒暄；「查下差旅政策，顺便把发票报销了」→ L1 AMBIGUOUS 或 L2 歧义 → 走 L3 多意图。
3. **图编译**：`build_pipeline_graph().invoke({"messages":[("user","我要出差去杭州")]})` 不报错、trace 有 MasterAgent 节点。
4. **接真模型**：`.env` 填 `GOGO_USE_LLM=true` + DashScope `OPENAI_BASE_URL`/`OPENAI_API_KEY`，跑「下周去上海出差三天」应触发申请 → 规划 → 审核闭环。
5. **API**：`uvicorn backend.api.main:app`，`POST /api/auth/login`（admin/123456）→ 拿 token → `POST /api/chat/{session_id}` SSE 流式。

---

## 9. 面试展示点（写进简历/README 的卖点）

1. **LangGraph 显式状态编排**：三层意图短路 + 条件改写 + 多智能体路由，一张图看懂，比 Java Reactor 链式调用更直观。
2. **子智能体即工具、可递归**：Master→Plan→Review 两层嵌套，LangChain `create_react_agent` + `as_tool` 表达。
3. **确定性逻辑与 LLM 分离**：意图 L1 正则、政策 12 规则、规划评分、冲突检测都是纯函数，可单测、不烧 token——这是 Agent 工程「能不用 LLM 就不用」的体现。
4. **Human-in-the-Loop**：`interrupt()` 暂停 + `ask_user` 结构化表单（text/select/confirm/form/date/number）。
5. **零依赖 fallback**：无 API Key 也能跑通确定性主流程，有 Key 自动切 LLM。
6. **工具级熔断 + SSE 流式 + checkpointer 会话持久化**，覆盖企业级稳定性诉求。
7. **提示词原样复用**：Java 与 Python 共用同一份 `prompts/*.md`，迁移前后行为一致。

---

## 附：我已建立的文件清单（下一步直接 `from backend.xxx import ...`）

```
backend/config/settings.py          # 配置
backend/domain/models.py schemas.py # 11 表 + DTO
backend/infrastructure/db.py llm.py stores.py repositories.py security.py bootstrap.py
backend/intent/{category,result,rule_matcher,vector_matcher,router,seed}.py
backend/services/{policy,order,approval,booking,user,auth,preference,chat,sse,circuit_breaker,llm_services}.py
backend/memory/long_term.py
backend/prompts.py
```

当前实现、外部凭据清单和真实完成度以 `PYTHON_ONE_TO_ONE_PLAN.md` 的“当前执行状态”为准；本文件前面的分步章节保留为设计依据和 Java 对照。

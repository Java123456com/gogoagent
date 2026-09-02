# GoGo Agent Python 对等迁移设计

本文以 `gogo-agent_java_version` 的实际源码为基准，不以 README 中的功能宣称作为实现证明。

## 1. 原版真实拓扑

### Agent

- 流水线辅助：`QueryRewritingAgent`、`IntentRecognitionAgent`（单次调用，不持有 ReAct memory）。
- 主 Agent：`MasterAgent`。
- Master 直接注册的子 Agent：`ItineraryManageAgent`、`ItineraryPlanAgent`、`InfoAgent`、`BookingAgent`。
- 独立但已废弃：`ItineraryReviewAgent`；规划 Agent 实际调用 `ItineraryReviewTools`，不是 Review Agent 子图。
- 占位：`ReimbursementAgent` 的 `build()` 返回 `null`，Python 保留占位，不虚构 OCR 能力。
- 审核工具内部临时并行实例：`TripExperienceAssessor`、`ResilienceAssessor`、`PreferenceAssessor`，均由同一个 `SubjectiveAssessorAgent` 类构造；它们不是 Master 子 Agent。

按逻辑运行角色计数是 12 个：1 个 Master、4 个可调度子 Agent、2 个流水线单次 Agent、1 个废弃 Agent、1 个未实现占位 Agent、3 个审核内部临时评估器。按 Java 类计数则不能把三个临时实例算成三个类。标题生成和问题推荐是 Service，不计入 Agent。

### 检索和记忆

1. `attractionKnowledge`：百炼托管知识库，景点查询。
2. `corporateTravelPolicyKnowledge`：本地 DOCX + DashScope embedding + `InMemoryStore`，政策问答。
3. `corporateTravelGuidelinesKnowledge`：本地 DOCX + DashScope embedding + `InMemoryStore`，差旅指南问答。
4. `intentRouterKnowledge`：本地意图样本 + DashScope embedding + `InMemoryStore`，只用于 L2 意图路由。
5. `BailianLongTermMemory`：按用户隔离的百炼长期偏好记忆；Redis 只缓存召回结果。

因此知识库是 **4 个**（3 个业务 RAG + 1 个意图路由库），长期记忆库另算 **1 个**。百炼托管的是景点知识库和用户长期记忆；政策、指南和意图路由库由项目本地构建。

### Java 原版真实的四层上下文

1. 工作/短期记忆：每个 ReAct Agent 的 `AutoContextMemory`，负责大消息卸载和历史压缩。
2. 会话记忆：`SessionPersistenceHook` 按 `sessionId:agentName` 保存每个 Agent 的 Memory。
3. 长期记忆：`BailianLongTermMemory`，按 `userId` 隔离，采用 `AGENT_CONTROL` 主动读写。
4. 召回缓存：`TravelPreferenceMemoryCache` 使用 Redis 缓存百炼长期记忆的 retrieve 结果，默认 1800 秒。

Java 中不存在一个通用的“请求缓存知识库”。`AgentSessionManager` 的 Caffeine 是运行中/暂停 Agent 对象的本地 TTL+LRU 缓存，不是第五种对话记忆；Python 的 `RequestMemoryCache` 只是单次调用去重工具，也不应被包装成 Java 原版独立层。

### 上下文覆盖矩阵

| Agent | AutoContext | Session | Bailian LTM | 业务 RAG | 结果/Skill Hook |
|---|---|---|---|---|---|
| Master | 有 | 有 | 有 | 无 | AutoContext、缓存控制 |
| ItineraryManage | 有 | 有 | 无 | 无 | 时间注入、AutoContext |
| ItineraryPlan | 有 | 有 | 有 | 不直接挂 Knowledge | 时间、工具压缩、Skill 折叠、计划/搜索 Hook |
| Booking | 有 | 有 | 有 | 无 | 时间、工具压缩、Skill 折叠 |
| Info | 有 | 有 | 无 | 3 个业务 RAG | 时间、工具压缩、MCP |
| ItineraryReview | 有 | 基类会话态 | 有 | 无 | 已废弃，独立审核工具 |
| QueryRewrite/Intent | 无状态 | 无 | 无 | Intent 样本仅供 Intent 使用 | 单次调用 |
| Reimbursement | 未实现 | 未实现 | 未实现 | 无 | 占位 |

## 2. Python 对等边界

```text
backend/rag/knowledge.py                # 三个业务 Knowledge facade
backend/rag/providers.py                # 官方 Bailian SDK Retrieve adapter
backend/memory/providers.py             # LongTermMemory provider protocol
backend/memory/context.py               # original/working/offload/events
backend/memory/session.py               # session key + durable checkpoint
backend/hooks/                           # tool、skill、time、token hooks
backend/agents/                          # 按矩阵装配 Agent
backend/workflow/                        # 条件流水线和子 Agent 调度
```

Provider 选择由配置控制：

- RAG：`local` 或 `bailian`；景点库启用时使用官方 `alibabacloud-bailian20231229` SDK，未配置时回退 local。
- 长期记忆：`local` 或 `bailian`；用户可通过 `BAILIAN_MEMORY_ENABLED` 选择，百炼协议使用 AgentScope 同款 REST API。
- Session：开发环境 SQLite/内存；生产环境 SQLAlchemy MySQL/PostgreSQL，Redis 仅存跨节点临时状态。

## 3. 验收标准

- 每个原版 Agent 的模型、工具、最大迭代次数、长期记忆和 Hook 覆盖可在 Python 配置中查到。
- 业务 RAG 返回 `source/provider/score/content`，能证明命中 local 或 Bailian。
- 长期记忆按 `user_id` 隔离，record 后主动失效 Redis 缓存。
- 会话同时保存原始消息、工作消息、卸载上下文和压缩事件，并能恢复。
- 工具结果压缩不改变业务语义；Skill 正文只折叠发送给模型的副本。
- Token 统计来自模型 usage 或明确的 tokenizer 估算；不宣称未经基准测试的百分比。

## 4. 当前执行状态

本计划已经在 `backend/` 执行，而不是停留在目录设计：

- **Agent 调度**：Master 的可调用子 Agent 已与 Java `MasterAgent.java` 对齐为 4 个；Plan 内部使用 `review_tools`，Review/报销的占位状态保留并在目录中可见。
- **Agent 工具边界**：Manage 使用订单读写/预订读/冲突/用户信息；Plan 使用订单读/预订读/规划/审核/外部查询；Booking 使用订单读/预订读写/用户信息；Info 使用政策工具、实时工具和三个 Knowledge 检索工具。
- **业务 RAG**：`attractionKnowledge`、`corporateTravelPolicyKnowledge`、`corporateTravelGuidelinesKnowledge` 均可检索；景点库启用后调用 Java 同款 Bailian `Retrieve`（AK/SK 签名），政策和指南继续读取 Java 原始 DOCX 并可用 `text-embedding-v4`。
- **意图库**：`intentRouterKnowledge` 使用 `backend/intent/seed.py` 的本地样本和向量匹配，只服务 L2 意图路由，不混入业务 RAG。
- **四层上下文**：工作上下文 `ContextCompressionHook`、按 `sessionId:agentName` 隔离的 `PersistentSessionStore`、用户级 `LongTermMemory`、Redis `RedisPreferenceCache` 已实现；每个 ReAct Agent 调用前执行同一组 AutoContext 参数，原始/工作消息、卸载内容、压缩事件和 token 前后估算均持久化。真实 LLM 路径遵循 Java `AGENT_CONTROL`，长期偏好只由记忆工具主动召回。
- **Hook**：工具结果去重压缩、Skill 旧结果折叠、动态时间注入、工具熔断、执行登记和中断恢复已接入。中断采用 Python 可实现的协作式取消，正在运行的网络/模型调用返回后即丢弃结果并保存 interrupted 状态。
- **外部服务**：NewsData、wttr.in、天气 Streamable HTTP MCP、Orizn stdio MCP 已有真实调用适配；未配置密钥或 endpoint 时返回可读降级结果。

未宣称固定的“Token 下降 65%～75%”：Java 的 AutoContext 参数已一一复刻，但实际比例取决于对话内容和模型，需用线上 usage 做 A/B 基准。

## 5. 账号申请清单

| 配置 | 用途 | 是否必须 |
|---|---|---|
| `DASHSCOPE_API_KEY` | qwen3.7-max、glm-5.1、qwen3.6-flash | 使用真实 LLM 时必须 |
| `DASHSCOPE_API_KEY` + `DASHSCOPE_EMBEDDING_MODEL=text-embedding-v4` | 本地政策/指南/意图样本向量化 | 启用远程 embedding 时必须；无 Key 可关键词/本地哈希降级 |
| `BAILIAN_ACCESS_KEY_ID/SECRET`、`BAILIAN_WORKSPACE_ID`、`BAILIAN_INDEX_ID` | 景点百炼知识库 | 切换景点 Bailian Provider 时必须 |
| `BAILIAN_MEMORY_LIBRARY_ID` | 用户跨会话偏好记忆库 | 启用百炼长期记忆时必须 |
| `BAILIAN_PROJECT_ID`、`BAILIAN_PROFILE_SCHEMA` | 百炼长期记忆画像范围/字段定义 | 按百炼记忆库配置选择 |
| `NEWS_API_KEY` | NewsData.io 目的地新闻 | 查询新闻时必须 |
| `WEATHER_MCP_ENDPOINT` | 超过三天的天气 MCP | 查询远期天气时必须 |
| `ORIZN_VISA_API_KEY`、本机 Node/npx | Orizn Visa stdio MCP | 详细签证查询时必须 |
| `GOGO_DATABASE_URL`（MySQL `mysql+pymysql://...`） | 生产业务/会话/长期记忆持久化 | 本地 SQLite 可不填 |
| `REDIS_URL` | 跨进程会话/长期偏好缓存和熔断状态 | 单进程本地缓存可不填 |
| `MINIO_ENDPOINT/ACCESS_KEY/SECRET_KEY/BUCKET` | Java 原版行程 HTML 对象存储 | 生产启用时必须；未配置时仅使用本地订单字段降级 |
| 用户级 Flight API Key | 航班搜索 CLI，由用户在系统内保存 | 使用航班搜索时必须，不是服务端统一启动 Key |
| 用户级 Tuniu API Key | 途牛搜索/预订 CLI，由用户在系统内保存 | 使用途牛下单时必须，不是服务端统一启动 Key |

> 景点 RAG 的 AK/SK 只用于官方 SDK 签名；长期记忆只需要 DashScope API Key，Java/AgentScope 的客户端不会发送 AccessKeySecret。

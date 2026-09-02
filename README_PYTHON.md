# GoGo Agent · LangChain + LangGraph

这是 `gogo-agent` 原 Java/AgentScope 企业差旅助手的 Python 重构入口。Python 实现位于 `backend/`，按 Java 的职责边界拆分；启动资源与知识库已迁到 `backend/resources/`。完整迁移基线见 [Python迁移方案.md](Python迁移方案.md)。

外部 Key、云端 MySQL/Redis、Skill 凭证、前后端启动及最终验收目前统一暂缓，项目完成后按 [项目完成后统一配置与验收清单](PROJECT_COMPLETION_CONFIGURATION_CHECKLIST.md) 一次性处理。

工作流：`fast_intent → (rewrite → full_intent)? → master_dispatch`。运行架构固定为 **4 个 ReAct Agent**（Master、Manage、Info、Booking）、**1 个 Plan-and-Execute Agent**（Plan）、**2 个单次 Agent**（Rewrite、Intent）和 **2 个轻量 LLM 服务**（标题、推荐问题）。Master 严格按 Java 源码只注册 Manage/Plan/Info/Booking 四个子 Agent；Review 仅保留兼容类，规划直接使用 ReviewTools；Reimbursement 保留未实现占位契约。

行程规划使用显式、可恢复的 LangGraph 子图：候选搜索 → 组合评分 → 多维审核 → 结构化整改 → 最多两轮重新规划。审核仲裁的 `remediation_priority` 会被校验并编译为候选剔除、评分调整、补搜或人工处理动作；每轮重规划前使用候选/约束指纹确认发生了实质变化，无变化时停止无效循环。图状态在每个阶段写入数据库会话检查点，进程恢复后从待执行节点继续。

模型按角色静态分档：标题/推荐走 `qwen3.6-flash`，信息/审核/识别走 `glm-5.1`，Master/Manage 走 `qwen3.7-max`，Plan/Booking 走开启 thinking 的 `qwen3.7-max`（`STRONG_MODEL_THINKING_BUDGET=2048`）。Plan 是显式 LangGraph 子图：校验 → 建计划 → 加载约束 → 生成候选 → 审核 → 最多两轮修复 → 输出；它没有预订写工具，实际预订仍由 Booking 承担。默认 deterministic fallback，无 API Key 也能跑通；配置 `GOGO_USE_LLM=true` 与 `DASHSCOPE_API_KEY`（或 OpenAI-compatible Key）后启用模型。

## Python 目录与 Java 对照

| Python 目录 | 对应 Java 职责 | 当前内容 |
|---|---|---|
| `backend/api` | `controller` | FastAPI 路由与请求/响应 |
| `backend/agents` | `agent` | 规划、检索、分析、撰写、审查 Agent 节点 |
| `backend/workflow` | `AgentPipelineService` / `MasterAgent` | LangGraph 编排与状态流转 |
| `backend/tools` | `agent/tools` | 检索、政策、成本工具适配层 |
| `backend/services` | `business/*/service` | 用例服务与会话编排 |
| `backend/domain` | `business/*/entity` / request DTO | Pydantic 领域模型 |
| `backend/memory` | `agent/memory` / `session` | 会话与长期记忆接口 |
| `backend/rag` | `RagKnowledgeConfig` | 知识检索边界 |
| `backend/infrastructure` | `config` / 外部 SDK | LLM、数据库、MCP 等适配器 |
| `backend/config` | `config` | 环境和运行时配置 |
| `backend/core` | `context` / common | LangGraph 全局状态与公共协议 |

其它对应目录：`backend/intent` 是 L1/L2/L3 意图识别，`backend/rag` 是三个业务 Knowledge 检索器（景点可切换百炼，政策/指南读取 Java DOCX），`backend/memory` 是四层上下文和长期记忆，`backend/services` 是订单/审批/预订/政策/聊天等领域服务，`backend/infrastructure` 是 SQLAlchemy、LLM、数据库、MCP 等适配边界。

迁移顺序应按业务垂直链路完成：先把核心 Agent 逐个转到 `backend/agents`，再把订单、审批、用户偏好、报销的 repository/service 转到 `domain + services + infrastructure`。保留对照文档和资源文件，便于逐项比对和回归。

## 启动

推荐使用 `uv` 管理项目专属 Python 环境和锁文件；完整的 Windows 安装、云端 MySQL/Redis、外部 Key 与最终验收步骤见 [项目完成后统一配置与验收清单](PROJECT_COMPLETION_CONFIGURATION_CHECKLIST.md)。

```powershell
cd gogo-agent
uv sync --python 3.11 --extra dev
npm install
Copy-Item .env.example .env
uv run uvicorn backend.api.main:app --reload --port 8000
```

访问 `http://127.0.0.1:8000/docs`。先调用 `POST /api/auth/login`（演示账号 `admin/123456`），再调用 `POST /api/chat/{session_id}` 获取 SSE 流；也支持 `/api/preferences`、`/api/my-travel/orders`、`/api/admin/approvals` 等 Java 原 API 对应接口。

CLI：`uv run python -m backend.cli "周五去杭州参加客户会议"`；测试：`uv run pytest -q`。

## 分层记忆

Python 版本提供与 Java 版本对应的四层上下文管理：

- 工作/短期记忆：`ContextCompressionHook` 保留原始消息，同时对历史消息摘要化、对大工具结果卸载并记录压缩事件。
- 会话记忆：`PersistentSessionStore` 按 `sessionId:agentName` 写入 `agentscope_session`，支持多轮对话、人工确认和中断恢复。
- 用户级长期记忆：`LongTermMemory` 按 `user_id` 持久化旅行偏好，真实模型路径由 Agent 通过工具主动读写。
- 长期记忆召回缓存：`RedisPreferenceCache` 以用户维度缓存百炼召回结果并在写入后失效，默认 TTL 30 分钟。

Java 原版没有通用“请求缓存层”；Caffeine 只缓存运行中或暂停的 Agent 会话对象。代码里的 `RequestMemoryCache` 是 Python 单轮去重辅助，不作为原版四层之一。AutoContext 的阈值已对齐，但“单轮 Token 固定下降 65%～75%”没有 Java 基准报告或当前线上 A/B 数据，不能作为已验证结论。

长期记忆后端通过 `LongTermMemory` 接口隔离：默认是本地 SQLAlchemy + Redis 可选缓存，配置 `BAILIAN_MEMORY_ENABLED=true` 和 `DASHSCOPE_API_KEY` 后切换到 AgentScope 同款 Bailian REST Provider（默认 `https://dashscope.aliyuncs.com`），失败可回退本地。景点库配置 `BAILIAN_KNOWLEDGE_ENABLED=true`、AK/SK、workspace/index 后通过官方 `alibabacloud-bailian20231229` SDK 调用 Java 同款 `Retrieve`（默认 `bailian.cn-beijing.aliyuncs.com`）；未配置时使用本地景点文件。默认 SQLite 适合开发演示，生产环境建议配置 MySQL 和 Redis。

配置操作详见 [项目完成后统一配置与验收清单](PROJECT_COMPLETION_CONFIGURATION_CHECKLIST.md) 的“Python 本地模式与阿里云模式切换”章节。Python 默认关闭百炼远程 Provider：复制 `.env.example` 为 `.env` 后，设置 `GOGO_USE_LLM=true` 可启用真实模型；设置 `BAILIAN_MEMORY_ENABLED=true` 可启用百炼长期记忆；设置 `BAILIAN_KNOWLEDGE_ENABLED=true` 并填写 AK/SK、Workspace、Index 可启用百炼景点知识库。修改 `.env` 后需要重启服务。政策、指南和意图样本仍是项目本地知识库，长期记忆与业务知识库不是同一个东西。

实时集成：天气近三天调用 Java 同款 `wttr.in`，远期调用 `WEATHER_MCP_ENDPOINT` 的 Streamable HTTP；新闻调用 `NEWS_API_KEY` 对应的 NewsData.io；签证通过项目级 npm 依赖和 `npx --no-install orizn-visa-mcp` stdio MCP。所有外部调用都有结构化降级和工具级熔断。

四个旅行 Skill、两个 MCP、多租户凭证与 RollingGo OAuth 隔离的 Java/Python 对照、安装步骤和生产配置见 [外部旅行能力企业化实现](EXTERNAL_INTEGRATIONS_PYTHON.md)。

## 工具韧性与 Agent 隔离

所有 LangChain 工具在 `BaseSubAgent` 组装时统一经过 `ResilientToolHook`。策略按工具副作用分为只读、幂等写、非幂等写和交互类：只读/幂等操作可按超时、最大尝试次数、指数退避和随机抖动重试；预订、出票、审批、取消等非幂等操作默认不自动重试，超时后返回未知结果并要求核验。熔断器按一次逻辑调用累计失败，并支持 Redis 共享状态。

子 Agent 通过 `SubAgentExecutor` 调用。子 Agent Tool 对模型仅暴露任务文本；用户、父会话、请求 ID、订单和截止时间由 `AgentRequestContext` 注入，不能被工具参数覆盖。`SUBAGENT_EXECUTION_MODE=local` 保持单进程开发路径；设置为 `process` 后，Manage、Plan、Info、Booking 分别由 `spawn` Worker 执行，Supervisor 负责硬超时终止、崩溃检测、退避重启和事件回传。Worker 只接收可序列化的请求上下文快照，SSE 接收器仍经事件队列回传。会话、规划结果、订单和长期记忆均存储在数据库/Redis，因此 Worker 重启后可恢复；未配置 Redis 时，本地内存 fallback 仅适用于单进程开发，不跨 Worker 共享。完整设计与验收标准见 [统一工具韧性与故障隔离改造计划](TOOL_RESILIENCE_AND_AGENT_ISOLATION_PLAN.md)。

多 API 实例部署时，设置 `CLUSTER_MODE=true`，让所有节点共享同一个 `REDIS_URL` 与生产数据库，并给每个节点配置唯一 `APP_INSTANCE_ID`。Redis Pub/Sub 频道 `agent:interrupt` 会把“停止生成”广播给实际持有 Agent 的节点；数据库仍是会话 checkpoint 与任意节点续跑的真相来源。`process` 是单节点内 Worker 隔离，不等同于多节点集群。配置示例和验收步骤见 [项目完成后统一配置与验收清单](PROJECT_COMPLETION_CONFIGURATION_CHECKLIST.md)。

架构契约测试、模型 Profile 测试、Plan 子图测试、轻量服务测试和工具权限测试均在 `tests/` 下。完整设计和剩余运营化工作（指标、真实模型 smoke test、生产发布）见 [MULTI_AGENT_EXECUTION_PLAN.md](MULTI_AGENT_EXECUTION_PLAN.md)。

面试展示点：显式 LangGraph 状态编排、可替换工具适配层、LLM 无依赖 fallback、FastAPI API、节点 trace 和 critic 质量门禁。后续可接 LangSmith、PostgreSQL checkpointer、RAG、MCP 和人机审批。

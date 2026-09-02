# GoGo Agent — 企业智能差旅助手

GoGo Agent 是一个面向企业差旅场景的多智能体应用。项目使用 FastAPI、LangChain 与 LangGraph，将需求理解、政策查询、行程规划、多源搜索、方案审核、预订和审批编排成可恢复的工作流，并通过 React 前端以 SSE 实时展示执行过程。

## 核心能力

- 多智能体协作：Master、Manage、Plan、Info、Booking 等智能体按职责分工。
- 显式规划工作流：候选搜索、组合评分、多维审核、结构化整改与最多两轮重规划。
- 企业数据隔离：登录用户身份贯穿 API、Agent、Tool、数据库和缓存边界。
- 多源旅行能力：支持途牛、FlyAI、航班管家、RollingGo、天气与签证 MCP。
- 凭据安全：用户 API Key 使用 AES-256-GCM 加密，按用户隔离存储和运行时注入。
- 可恢复执行：会话检查点、人工确认、请求代际围栏、跨节点中断广播。
- 工具韧性：超时、重试、指数退避、熔断、幂等控制和进程级 Agent 隔离。
- 可观测性：结构化运行事件、SSE 时间线和敏感信息统一脱敏。

## 技术栈

| 层次 | 技术 |
|---|---|
| 后端 API | Python 3.11+、FastAPI、Pydantic |
| Agent 编排 | LangChain、LangGraph |
| 数据层 | SQLAlchemy、SQLite / MySQL |
| 分布式状态 | Redis |
| 前端 | React、TypeScript、Vite、Zustand、Ant Design |
| 外部能力 | MCP、HTTP JSON-RPC、受限 CLI Skills |
| 安全 | AES-256-GCM、租户上下文、递归脱敏 |

## 架构概览

```text
React 前端
   │ REST / SSE + Authorization
   ▼
FastAPI API ── 登录鉴权 / 用户与管理员权限
   │
   ▼
LangGraph 主工作流
   ├── Intent / Rewrite
   ├── Master Agent
   ├── Manage / Info / Booking Agent
   └── Plan-and-Execute 子图
          ├── 多源候选搜索
          ├── 政策与偏好评分
          ├── 多维审核
          └── 整改与重规划
   │
   ├── SQLAlchemy：账号、订单、审批、会话
   ├── Redis：缓存、凭据密文、熔断、集群协调
   └── MCP / Skills / 外部旅行服务
```

## 目录结构

```text
backend/
  agents/          # 智能体实现
  api/             # FastAPI 路由和依赖
  config/          # 环境与运行配置
  core/            # 请求上下文和公共协议
  domain/          # 领域模型
  infrastructure/  # 数据库、Redis、LLM、MCP、安全适配器
  intent/          # 多层意图识别
  memory/          # 会话与长期记忆
  rag/             # 知识检索
  runtime/         # 工具韧性与进程隔离
  services/        # 业务用例服务
  tools/           # Agent 工具
  workflow/        # LangGraph 工作流
frontend/          # React 前端
tests/             # 自动化测试
```

## 本地运行

```powershell
uv sync --python 3.11 --extra dev
Copy-Item .env.example .env
uv run uvicorn backend.api.main:app --reload --port 8080
```

另开终端启动前端：

```powershell
cd frontend
npm install
npm run dev
```

浏览器访问 `http://localhost:5173`。默认采用本地确定性 fallback，无模型密钥也可运行；需要真实模型时，在 `.env` 中设置 `GOGO_USE_LLM=true` 和对应 API Key。

## 验证

```powershell
uv run pytest -q
uv run ruff check backend tests
cd frontend
npm run build
```

当前基线：116 项 Python 测试通过，前端生产构建通过。

## 分支说明

- `master`：面试展示与稳定版本。
- `develop`：日常集成分支。
- `feature/*`：按基础设施、鉴权、记忆与 RAG、Agent 编排、差旅业务、外部集成、前端和测试文档拆分的模块分支。

更完整的设计、迁移映射和生产配置说明见 [README_PYTHON.md](README_PYTHON.md)、[MULTI_AGENT_EXECUTION_PLAN.md](MULTI_AGENT_EXECUTION_PLAN.md) 与 [PROJECT_COMPLETION_CONFIGURATION_CHECKLIST.md](PROJECT_COMPLETION_CONFIGURATION_CHECKLIST.md)。

> 仓库不包含真实凭据。请复制 `.env.example` 创建本地 `.env`，不要提交 `.env`、运行目录、数据库文件或第三方 Token。

# 项目完成后统一配置与验收清单（Python 版）

> 状态：**暂缓处理，等功能开发完成后统一执行。**  
> 当前阶段不申请外部 Key、不修改线上中间件、不提交真实订单。本清单用于最终联调、答辩演示和发布前验收。

## 1. 先明确：附件是 Java 说明，Python 不照搬

Java 原版使用 `application.yml`、Spring Boot、Maven、JDK 21 和 `schema.sql`；当前项目使用：

- 根目录 `.env`：所有 Python 运行配置。
- FastAPI/Uvicorn：后端启动。
- `pyproject.toml`：Python 依赖。
- `uv`：首选 Python 版本、虚拟环境、依赖同步和命令执行工具。
- `uv.lock`：收尾安装时生成并提交，锁定可复现的 Python 依赖版本。
- 根目录 `package.json`：项目专属旅行 Skill CLI 与 Orizn MCP。
- SQLAlchemy：首次启动自动建表和写入开发种子数据，不手工执行 Java `schema.sql`。

当前运行拓扑固定为：**本地 Windows 运行 Python、前端和 Skill；云服务器 Docker 运行 MySQL、Redis。**

## 2. 最终配置状态说明

| 标记 | 含义 |
|---|---|
| 🔴 收尾必做 | 完整 Agent 演示或当前部署拓扑必须配置 |
| 🟡 按需配置 | 不配置可启动，但对应外部能力会降级 |
| 🟢 已内置 | 代码或本地 fallback 已存在，无需申请外部资源 |
| ⏳ 暂缓 | 现在不处理，项目完成后按本清单统一处理 |

以下所有外部申请目前统一标记为 **⏳ 暂缓**。

## 3. Java 配置到 Python `.env` 的映射

| 用途 | Python 配置 | 最终要求 | 未配置时行为 | 收尾动作 |
|---|---|---:|---|---|
| DashScope 大模型 | `DASHSCOPE_API_KEY`、`GOGO_USE_LLM=true` | 🔴 | 使用确定性 fallback，可启动但不是真实多 Agent 推理 | ⏳ 去阿里云百炼申请 Key，并做成本限制 |
| 模型网关 | `OPENAI_BASE_URL`、`OPENAI_API_KEY` | 🟡 | 有 DashScope Key 时自动使用 DashScope 兼容端点 | ⏳ 只有改用其他 OpenAI-compatible 网关时配置 |
| MySQL | `GOGO_DATABASE_URL` | 🔴 | 默认使用本地 SQLite | ⏳ 填云服务器 MySQL Docker 地址并验证权限 |
| Redis | `REDIS_URL` | 🔴（企业能力） | 部分缓存、跨节点 Token/熔断降级为本地或关闭 | ⏳ 填云服务器 Redis Docker 地址并验证网络 |
| 多节点中断协调 | `CLUSTER_MODE`、`APP_INSTANCE_ID`、`INTERRUPT_BROADCAST_CHANNEL` | 🟡（多实例时为🔴） | 单节点仅本地协作式中断；不广播到其他 API 节点 | ⏳ 部署多个 API 实例前开启并验证 Redis Pub/Sub |
| API Key 加密主密钥 | `API_KEY_ENCRYPT_SECRET` | 🔴 | 示例值只适合开发，不适合保存真实凭证 | ⏳ 生成至少 32 字节随机值并备份 |
| 百炼长期记忆 | `BAILIAN_MEMORY_ENABLED`、`DASHSCOPE_API_KEY`、`BAILIAN_MEMORY_LIBRARY_ID`、`BAILIAN_PROJECT_ID`、`BAILIAN_PROFILE_SCHEMA` | 🟡 | 使用 MySQL 本地长期记忆 + Redis 可选缓存 | ⏳ 需要复刻百炼记忆时创建记忆库后开启 |
| 百炼景点 RAG | `BAILIAN_KNOWLEDGE_ENABLED`、`BAILIAN_ACCESS_KEY_ID`、`BAILIAN_ACCESS_KEY_SECRET`、`BAILIAN_WORKSPACE_ID`、`BAILIAN_INDEX_ID` | 🟡 | 使用项目内 `tourist_attraction.md` | ⏳ 需要云端景点库时上传数据并开启 |
| 差旅政策 RAG | 项目内 `business_travel_policy.md`、`business_travel_guidelines.md` | 🟢 | 本地直接可用 | 无需创建百炼索引；这是政策双通道中的 RAG 通道 |
| NewsData 新闻 | `NEWS_API_KEY` | 🟡 | 返回“新闻服务未配置”的结构化降级 | ⏳ 最终需要目的地新闻时申请 |
| 天气 MCP | `WEATHER_MCP_ENDPOINT`、`WEATHER_MCP_ENABLED_TOOLS` | 🟡 | 近三天仍可用 wttr.in；远期天气不可用 | ⏳ 购买/申请阿里云天气 MCP 网关后填写 |
| Orizn 签证 | `ORIZN_VISA_API_KEY`、`ORIZN_MCP_ENABLED_TOOLS` | 🟡 | 不填 Key 时尝试免费模式 | ⏳ 最终需要完整签证数据时申请 |
| MinIO | `MINIO_ENDPOINT`、`MINIO_ACCESS_KEY`、`MINIO_SECRET_KEY`、`MINIO_BUCKET` | 🟡 | 不影响核心对话；附件/HTML 对象存储不可用 | ⏳ 需要对象存储时配置 |

### 模型费用与兼容性检查

当前默认模型包括 `qwen3.6-flash`、`qwen3.7-max`、`glm-5.1`。所有模型暂时通过同一个 OpenAI-compatible endpoint 调用。收尾时必须：

1. 确认所选网关实际提供这三个模型名。
2. 若 DashScope 不提供某个模型，将 `FAST_MODEL`、`STRONG_MODEL`、`STABLE_MODEL`、`STRONG_MODEL_WITH_THINKING` 统一换成该网关可用模型。
3. 为高价模型设置账户预算和调用告警；正式联调前保持 `GOGO_USE_LLM=false` 可避免误扣费。

### 3.1 Python 本地模式与阿里云模式切换

Python 版不是所有能力默认都访问阿里云，而是通过项目根目录的 `.env` 选择运行模式。修改 `.env` 后必须重启 Uvicorn；配置在进程启动时读取，运行中不会自动切换。

需要先创建配置文件：

```powershell
cd D:\x-project\codingPractice\python\gogoagent\gogo-agent
Copy-Item .env.example .env
```

默认配置是本地开发模式：

```env
GOGO_USE_LLM=false
BAILIAN_MEMORY_ENABLED=false
BAILIAN_KNOWLEDGE_ENABLED=false
GOGO_DATABASE_URL=sqlite:///./gogo_travel.db
REDIS_URL=
```

此模式下：

- 不调用真实大模型，走确定性 fallback；
- 用户长期记忆保存到本地 SQLAlchemy 数据库；
- 景点、政策、指南使用项目内本地语料；
- Redis 不启用，缓存使用进程内降级实现。

切换为真实 DashScope/百炼模型：

```env
GOGO_USE_LLM=true
DASHSCOPE_API_KEY=你的DashScope或百炼API_Key
```

切换用户长期记忆到阿里云百炼：

```env
BAILIAN_MEMORY_ENABLED=true
DASHSCOPE_API_KEY=你的DashScope或百炼API_Key
BAILIAN_MEMORY_LIBRARY_ID=你的长期记忆库ID
BAILIAN_PROJECT_ID=你的项目ID
BAILIAN_PROFILE_SCHEMA=你的画像Schema（按需）
```

切换景点 RAG 到阿里云百炼：

```env
BAILIAN_KNOWLEDGE_ENABLED=true
BAILIAN_ACCESS_KEY_ID=你的阿里云AccessKey_ID
BAILIAN_ACCESS_KEY_SECRET=你的阿里云AccessKey_Secret
BAILIAN_WORKSPACE_ID=你的百炼Workspace_ID
BAILIAN_INDEX_ID=你的百炼知识库Index_ID
```

切换后重启服务：

```powershell
uv run uvicorn backend.api.main:app --reload --port 8000
```

边界必须区分清楚：景点知识库才是当前 Python 版可切换到百炼的业务知识库；差旅政策、差旅指南和意图样本仍由项目本地维护。长期记忆保存的是用户偏好或旅行事实，例如“只坐高铁一等座”，不是政策文档。`REDIS_URL` 只控制缓存和跨进程临时状态，不会把本地知识库自动上传到阿里云。

Java 原版同样不是所有知识都托管在阿里云：景点知识库和用户长期记忆使用百炼，政策、指南、意图路由样本仍是项目本地知识/向量库；区别是 Java 完整运行配置更偏向直接要求阿里云 Key，Python 为了本地开发增加了默认关闭远程 Provider 的 fallback 模式。

### 3.2 多节点部署与 Redis 中断广播

`SUBAGENT_EXECUTION_MODE=process` 只是在**单个 Python API 进程**内隔离业务 Worker，并不等于应用集群。应用集群是部署两个或更多 API 实例，并让它们共同访问同一个 MySQL/PostgreSQL 与 Redis：

```text
负载均衡 / Nginx
  ├─ gogo-api-1 ─┐
  └─ gogo-api-2 ─┼─ 共享 MySQL/PostgreSQL（会话与 checkpoint）
                  └─ 共享 Redis（熔断、缓存、agent:interrupt 广播）
```

多节点时，每个 API 节点都必须在 `.env` 中配置同一个 Redis 地址，并使用不同的节点标识：

```env
CLUSTER_MODE=true
REDIS_URL=redis://:<redis-password>@<redis-host>:6379/0
APP_INSTANCE_ID=gogo-api-1
INTERRUPT_BROADCAST_CHANNEL=agent:interrupt
```

第二个节点只需将 `APP_INSTANCE_ID` 改为 `gogo-api-2`。当停止请求落到节点 A、实际 Agent 在节点 B 运行时，A 会先清理本地执行，再向 `agent:interrupt` 发布消息；B 收到后停止本地 Agent、保存中断 checkpoint、清理 pending tool，并仅向 B 自己持有的 SSE 连接发送 `interrupted` 事件。

Redis Pub/Sub 是实时控制通道，不是会话真相来源：消息、活跃 Agent、待恢复工具和上下文 checkpoint 仍保存到数据库，因此节点重启或后续请求落到其他节点时仍可以恢复。集群模式下 Redis 不可用会导致服务启动失败；本地模式下 Redis 不配置仍可运行，但只有本节点中断能力。

## 4. 本地项目连接云端 MySQL/Redis

最终 `.env` 示例（密码只填在本地 `.env`，不得提交 Git）：

```env
GOGO_DATABASE_URL=mysql+pymysql://gogo_user:<mysql-password>@<cloud-host>:3306/gogo_travel
REDIS_URL=redis://:<redis-password>@<cloud-host>:6379/0
API_KEY_ENCRYPT_SECRET=<至少32字节随机值>
```

安全要求：

- 不要把无密码 Redis 暴露公网。
- 云安全组至少限制为当前本地公网 IP；更推荐 VPN/Tailscale/SSH 隧道。
- 使用 SSH 隧道时，本地映射到 `13306/16379`，`.env` 再连接 `127.0.0.1` 的映射端口。
- MySQL Docker 中需要先创建数据库和最小权限账号；表结构由 Python 首次启动自动创建。

建库示例（收尾阶段在 MySQL 执行）：

```sql
CREATE DATABASE gogo_travel DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

Python 启动时会自动创建当前 13 张 SQLAlchemy 表，并在空库写入开发种子数据，包括 5 个用户、5 个登录账号、12 条差旅政策及示例订单/预订。不要再执行 Java 的 `src/main/resources/db/schema.sql`。

开发账号仍为 `admin/alice/bob/charlie/david`，密码均为 `123456`。**最终发布前必须删除、改密或改造成安全密码哈希，不能保留明文开发账号。**

## 5. Skill 与运行时凭证（项目完成后处理）

### 5.1 安装位置

Skill 文档已经随仓库放在：

```text
backend/resources/skills/
```

项目 CLI/MCP 依赖由根目录 `package.json` 管理，收尾联调时在项目根目录执行：

```powershell
npm install
```

它们只写入项目的 `node_modules/` 和 `node_modules/.bin/`，不要执行 `npm install -g`。

注意：这是根目录旅行 Skill 的 npm 安装；前端还需要在 `frontend/` 目录单独执行一次 `npm install`。

### 5.2 每个平台需要什么

| Provider | 本地程序 | 用户凭证 | 什么时候处理 | 缺失时 |
|---|---|---|---|---|
| 途牛 | `tuniu-cli` | 每用户 Tuniu Key/OAuth | ⏳ 真实搜索/下单联调前 | 途牛搜索与下单不可用 |
| 航班管家 | 无额外 CLI，走 HTTPS JSON-RPC | 每用户 Flight API Key | ⏳ 航班联调前 | 该来源跳过 |
| FlyAI/飞猪 | `@fly-ai/flyai-cli` | 可选增强 Key | ⏳ 多源搜索联调前 | 无 CLI 时跳过；服务支持范围以当时控制台为准 |
| RollingGo | `@rollinggo/hotel` 或项目内 `rgh.exe` | 每用户 OAuth PKCE 登录态 | ⏳ 酒店联调前 | 酒店来源跳过 |
| Orizn | `orizn-visa-mcp` | API Key 可选 | ⏳ 签证联调前 | 免费模式或结构化降级 |

用户级 Tuniu/Flight/FlyAI Key 不写进 `.env`。最终通过 Agent 的 `save_*_api_key` 工具保存，系统使用 AES-256-GCM 加密并按用户隔离。

## 6. 其他 Java 配置的 Python 对应项

| Java 项 | Python 对应方式 | 收尾处理 |
|---|---|---|
| `server.port` | `uvicorn ... --port 8000` | 端口冲突时改命令参数 |
| `sa-token.timeout` | `TOKEN_TIMEOUT_SECONDS`，默认 30 天 | 按环境调整 |
| `logging.level.*` | Uvicorn `--log-level debug` | 当前尚无统一 `LOG_LEVEL` 设置；如需统一，收尾新增 |
| 熔断工具白名单 | `CIRCUIT_BREAKER_MONITORED_TOOLS` / `CIRCUIT_BREAKER_EXCLUDED_TOOLS` | 新增高风险远程工具时补充 |
| Skill 目录 | 固定为 `backend/resources/skills` | 通常无需配置 |
| 天气 MCP 白名单 | `WEATHER_MCP_ENABLED_TOOLS` | 只开放需要的工具 |
| Orizn MCP 白名单 | `ORIZN_MCP_ENABLED_TOOLS` | 免费模式仅保留可用工具 |
| 后端 API 地址 | 前端 `VITE_API_BASE` | Python 默认端口为 8000，必须覆盖前端当前 8080 默认值 |

## 7. 最终启动顺序（现在只记录）

### 7.1 在 Windows 安装 uv

项目完成后，在 PowerShell 执行一次：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
uv --version
```

安装命令以 [uv 官方 Windows 安装文档](https://docs.astral.sh/uv/getting-started/installation/) 为准。若安装后当前终端仍提示找不到 `uv`，关闭并重新打开 PowerShell，再执行 `uv --version`。

`uv` 安装在当前 Windows 用户环境中，但项目依赖仍只放进当前项目的 `.venv/`，不会装成全局 Python 包。项目要求 Python 3.11 或更高版本；本机没有合适版本时执行：

```powershell
uv python install 3.11
```

### 7.2 后端（首选 uv）

```powershell
cd D:\x-project\codingPractice\python\gogoagent\gogo-agent
uv sync --python 3.11 --extra dev
npm install
Copy-Item .env.example .env
uv run uvicorn backend.api.main:app --reload --port 8000
```

第一次 `uv sync` 会创建项目专属 `.venv/` 和 `uv.lock`。`uv.lock` 应提交 Git，`.venv/` 不提交；修改 `pyproject.toml` 后重新执行 `uv sync --extra dev`。CI 或固定版本验收使用 `uv sync --locked --extra dev`，避免锁文件过期时静默更新。具体行为见 [uv 项目同步文档](https://docs.astral.sh/uv/concepts/projects/sync/)。

这里三种安装不要混淆：

| 执行位置与命令 | 管理内容 | 安装位置 |
|---|---|---|
| 项目根目录 `uv sync --extra dev` | Python 后端及开发依赖 | 项目 `.venv/` |
| 项目根目录 `npm install` | 旅行 Skill CLI 与 Orizn MCP | 项目 `node_modules/` |
| `frontend/` 下 `npm install` | Vue 前端依赖 | `frontend/node_modules/` |

无需手工激活 `.venv`，后续统一通过 `uv run ...` 执行。若暂时不能安装 uv，原来的 `python -m venv .venv` + `pip install -e ".[dev]"` 仍可作为备用方式，但最终文档和验收以 uv 为准。

验证：浏览器打开 `http://127.0.0.1:8000/health` 和 `http://127.0.0.1:8000/docs`。

后端命令示例：

```powershell
uv run python -m backend.cli "周五去杭州参加客户会议"
uv run pytest -q
uv run ruff check backend tests
```

### 7.3 前端

在 `frontend/.env.local` 写入：

```env
VITE_API_BASE=http://127.0.0.1:8000
```

然后执行：

```powershell
cd frontend
npm install
npm run dev
```

浏览器打开 `http://127.0.0.1:5173`。

## 8. 第一次完整验收

按以下顺序，失败时停止并记录对应层：

1. `GET /health` 返回 `status=ok`。
2. 使用 `alice / 123456` 登录，仅用于本地联调。
3. 输入“你好”，验证 LLM 或 fallback 基础链路。
4. 输入“我明天从北京去上海出差 3 天，帮我看下政策”，验证本地政策 RAG + 个性化 Policy Function Calling。
5. 创建差旅单并完成人机确认，验证 HITL。
6. 在浏览器 Network 检查 `POST /api/chat/{session_id}` SSE，确认 `progress/message/suggestions/done` 等事件。
7. 刷新页面检查会话与消息恢复。
8. 验证 Plan 流程：政策 → 多源搜索 → 组合评分 → 审核 → 最多两轮修复 → 输出。
9. 分 Provider 进行真实搜索 smoke test；最后才测试创建订单，并使用测试账号/最低风险数据。
10. 检查日志、SSE 和数据库中没有明文 API Key、Token、手机号、证件号。

数据库核对：

```sql
SELECT conversation_id, title, updated_at
FROM chat_conversation ORDER BY updated_at DESC LIMIT 5;

SELECT message_id, role, content
FROM chat_message ORDER BY created_at DESC LIMIT 10;

SELECT * FROM agentscope_session LIMIT 3;
```

## 9. 最终故障排查

| 现象 | Python 版排查方向 |
|---|---|
| 找不到 `uv` 命令 | 重新打开 PowerShell，执行 `uv --version`；仍失败则按官方安装文档重装并检查用户 PATH |
| `uv sync` 失败 | 检查网络、Python 版本和 `pyproject.toml`；锁文件过期时先用普通 `uv sync --extra dev` 更新，不要手改 `uv.lock` |
| MySQL 连接失败 | 云安全组、Docker 端口映射、账号权限、URL 特殊字符编码、SSH 隧道 |
| Redis 超时 | `REDIS_URL`、密码、6379 映射；禁止无密码公网访问 |
| DashScope 401/403 | `.env` 是否被当前进程加载、Key 是否有效、模型是否有权限 |
| 模型总是 fallback | `GOGO_USE_LLM` 是否为 `true`，Key/endpoint/model 名是否匹配 |
| Orizn 启动失败 | 根目录是否执行 `npm install`，`node_modules/.bin/orizn-visa-mcp.cmd` 是否存在 |
| Skill 搜索跳过 | `node_modules/.bin` 中对应 `.cmd` 是否存在，用户 Key/OAuth 是否配置 |
| 前端登录网络错误 | `frontend/.env.local` 是否指向 `http://127.0.0.1:8000` |
| SSE 中断 | 查看后端异常、浏览器 Network；使用代理时关闭 SSE buffering |
| 会话/中断无法跨进程恢复 | Redis 是否连通、数据库是否写入 session/checkpoint |
| 熔断误伤 | 检查 `CIRCUIT_BREAKER_*` 阈值、冷却时间和监控白名单 |

## 10. 项目完成后的统一处理顺序

- [ ] 冻结代码和数据库模型，跑全量测试。
- [ ] 在本地 Windows 安装并验证 `uv`，执行 `uv sync --python 3.11 --extra dev`。
- [ ] 检查并提交根目录 `uv.lock`，确认 `.venv/` 未提交。
- [ ] 本地根目录与 `frontend/` 分别执行 `npm install`。
- [ ] 创建最终 `.env`，生成强加密主密钥。
- [ ] 打通云服务器 MySQL/Redis，确认安全组或隧道。
- [ ] 申请 DashScope Key，核对模型名和预算。
- [ ] 按需创建百炼长期记忆、景点知识库。
- [ ] 按需申请 NewsData、Weather MCP、Orizn。
- [ ] 按 Provider 申请 Tuniu/Flight/FlyAI，完成 RollingGo OAuth。
- [ ] 修正前端 `VITE_API_BASE`，完成前后端联调。
- [ ] 执行完整验收，最后才做真实预订测试。
- [ ] 清除开发账号/默认密码，审计日志、权限、凭证与公网端口。

在上述清单正式开始前，保持远程 Provider 开关关闭、不要提交 `.env`、不要使用真实个人证件或支付信息。

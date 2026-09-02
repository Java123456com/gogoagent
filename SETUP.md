# GoGo Agent 环境安装与配置

本文说明运行项目需要安装的软件，以及 MySQL、Redis、MinIO、模型和旅行 Skill 的配置方式。所有真实密钥只写入本地 `.env` 或通过系统的用户凭据功能保存，不要提交到 Git。

## 1. 最小演示环境

面试演示只需要：

- Python 3.11 或更高版本
- uv（Python 包与虚拟环境管理）
- Node.js 20.19+ 或 22.12+
- npm

可从以下官方入口安装：

- [Python](https://www.python.org/downloads/)
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- [Node.js](https://nodejs.org/)

本地演示默认使用 SQLite，不需要安装 MySQL、Redis 或 MinIO，也可以在不配置模型密钥的情况下运行确定性流程。

## 2. 安装项目依赖

在项目根目录执行：

```powershell
uv sync --python 3.11 --extra dev
npm install
cd frontend
npm install
cd ..
Copy-Item .env.example .env
```

三组命令的作用分别是：

- `uv sync`：安装 FastAPI、LangChain、LangGraph、SQLAlchemy、Redis 客户端、测试和代码检查依赖。
- 根目录 `npm install`：安装途牛、FlyAI、RollingGo 和签证 MCP 的项目级 CLI。
- `frontend/npm install`：安装 React、Vite、Ant Design 和前端开发依赖。

## 3. 最小配置

保持 `.env` 中以下配置即可使用本地演示模式：

```dotenv
GOGO_USE_LLM=false
GOGO_DATABASE_URL=sqlite:///./gogo_travel.db
REDIS_URL=
CLUSTER_MODE=false
```

首次启动时会自动创建 SQLite 数据库和演示数据。

## 4. 启动项目

启动后端：

```powershell
uv run uvicorn backend.api.main:app --reload --port 8080
```

另开终端启动前端：

```powershell
cd frontend
npm run dev
```

访问地址：

- 前端：`http://localhost:5173`
- API 文档：`http://localhost:8080/docs`

## 5. MySQL（可选）

SQLite 足够本地演示。需要更接近生产环境时，可安装 MySQL 8，创建 `gogo_travel` 数据库，然后修改 `.env`：

```dotenv
GOGO_DATABASE_URL=mysql+pymysql://gogo:your_password@127.0.0.1:3306/gogo_travel?charset=utf8mb4
```

使用 Docker 的示例：

```powershell
docker run --name gogo-mysql -e MYSQL_ROOT_PASSWORD=change_me -e MYSQL_DATABASE=gogo_travel -p 3306:3306 -d mysql:8
```

应用启动时会自动创建所需表。生产环境应使用独立账号和强密码，不要直接使用 root 账号。

## 6. Redis（可选）

Redis 用于共享会话、缓存、工具熔断状态、凭据密文和多实例中断广播。单机演示可以不启用；多实例部署必须配置同一个 Redis。

使用 Docker 启动：

```powershell
docker run --name gogo-redis -p 6379:6379 -d redis:7-alpine
```

对应 `.env`：

```dotenv
REDIS_URL=redis://127.0.0.1:6379/0
CLUSTER_MODE=false
```

只有部署多个 API 实例时才设置 `CLUSTER_MODE=true`。

## 7. MinIO（可选）

MinIO 用于保存生成的行程 HTML。未配置时不影响核心演示流程。

使用 Docker 启动：

```powershell
docker run --name gogo-minio -p 9000:9000 -p 9001:9001 -e MINIO_ROOT_USER=gogo_admin -e MINIO_ROOT_PASSWORD=change_this_password -v gogo-minio-data:/data -d quay.io/minio/minio server /data --console-address ":9001"
```

对应 `.env`：

```dotenv
MINIO_ENDPOINT=127.0.0.1:9000
MINIO_ACCESS_KEY=gogo_admin
MINIO_SECRET_KEY=change_this_password
MINIO_BUCKET=gogo-travel
```

这些示例值只能用于本地开发，生产环境必须更换。

## 8. 模型与知识库（可选）

启用真实模型调用：

```dotenv
GOGO_USE_LLM=true
OPENAI_API_KEY=your_api_key
OPENAI_BASE_URL=your_openai_compatible_endpoint
```

如果使用 DashScope，可配置 `DASHSCOPE_API_KEY`。百炼长期记忆和知识库默认关闭，只有准备好相应的 AccessKey、Workspace、Index 和 Memory Library 后再开启：

```dotenv
BAILIAN_MEMORY_ENABLED=true
BAILIAN_KNOWLEDGE_ENABLED=true
BAILIAN_ACCESS_KEY_ID=your_access_key_id
BAILIAN_ACCESS_KEY_SECRET=your_access_key_secret
BAILIAN_WORKSPACE_ID=your_workspace_id
BAILIAN_INDEX_ID=your_index_id
BAILIAN_MEMORY_LIBRARY_ID=your_memory_library_id
```

## 9. 旅行 Skill 与外部服务

Skill 定义已经包含在 `backend/resources/skills/`，不需要再执行额外的 `skill install`。在项目根目录执行一次 `npm install`，即可安装项目声明的 CLI：

| 能力 | 本地依赖 | 凭据或授权 |
|---|---|---|
| 途牛搜索与预订 | `tuniu-cli`，由根目录 `npm install` 安装 | 在系统中为当前用户保存途牛 API Key |
| FlyAI 搜索 | `@fly-ai/flyai-cli`，由根目录 `npm install` 安装 | 可免 Key 试用；增强能力需要 FlyAI API Key |
| 航班管家 | 直接调用远程 HTTP MCP，无额外 CLI | 在系统中为当前用户保存航班 API Key |
| RollingGo 酒店 | `@rollinggo/hotel`，由根目录 `npm install` 安装 | 首次使用时完成浏览器 OAuth 授权 |
| 签证查询 | `orizn-visa-mcp`，由根目录 `npm install` 安装 | 按需配置 `ORIZN_VISA_API_KEY` |
| 天气 | 默认可使用公开天气数据；也支持天气 MCP | 按需配置 `WEATHER_MCP_ENDPOINT` |
| 目的地新闻 | HTTP API | 配置 `NEWS_API_KEY` |

第三方旅行 API Key 不应直接写进命令或提交到 `.env`。项目会通过 Agent 凭据工具按用户加密保存，并在调用时注入。

## 10. 发布前验证

```powershell
uv run pytest -q
uv run ruff check backend tests
cd frontend
npm run build
```

当前基线为 116 项 Python 测试通过，后端静态检查和前端生产构建通过。

## 11. 常见问题

- `uv` 找不到：重新打开终端并确认 uv 已加入 PATH。
- `npm install` 失败：确认 Node.js 版本符合要求，并检查网络代理。
- CLI 找不到：必须先在项目根目录执行 `npm install`，不要依赖全局安装。
- MySQL 连接失败：检查端口、账号、密码、数据库名和 `pymysql` 连接串。
- Redis 连接失败：单机演示可暂时清空 `REDIS_URL`；集群模式不能省略 Redis。
- 前端请求失败：确认后端运行在 `8080` 端口，并检查 Vite 的代理配置。

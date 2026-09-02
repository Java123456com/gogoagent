# 外部旅行能力企业化实现

本文记录 Java 原版外部旅行能力在 Python 版中的落点、运行依赖和生产配置。这里的“复刻”是职责与行为一一对应，不复制 Spring/AgentScope 的语法；Python 使用 FastAPI、LangGraph、LangChain Tool、SQLAlchemy、Redis 和 MCP Python SDK 实现同一套边界。

当前不立即申请或配置外部服务；项目完成后统一执行 [项目完成后统一配置与验收清单](PROJECT_COMPLETION_CONFIGURATION_CHECKLIST.md)。

## 当前运行拓扑

本项目采用“本地应用 + 云端中间件”模式：

```text
本地 Windows 电脑
  ├── FastAPI / LangGraph Python 后端
  ├── 项目目录 backend/resources/skills
  ├── 项目目录 node_modules/.bin（tuniu / flyai / rgh / orizn）
  └── 浏览器前端

云服务器 Docker
  ├── MySQL
  └── Redis
```

因此，Python、Node.js、npm、四个旅行 CLI 和 Orizn MCP 都安装在本地项目环境；云服务器只承载 MySQL/Redis。`.env` 中的数据库和 Redis 地址填写云服务器地址，不填写 `127.0.0.1`，除非中间件也运行在本机。

## 实现结论

| Java 设计 | Python 实现 | 状态 |
|---|---|---|
| `SkillBox` + `ShellCommandTool` | Skill 注册表、按需文档加载、白名单 Shell Tool | 已完成 |
| `tuniu-cli` / `flight-manager` / `flyai` / `rolling-go-hotel` | 四 Provider 并发搜索、标准化、去重、单 Provider 失败隔离 | 已完成 |
| `AbstractShellApiKeyHook` / Flight / Tuniu Hook | 用户级 API Key 服务、DB + Redis 密文缓存、执行前环境注入 | 已完成 |
| `SecretCipher` | SHA-256 派生密钥的 AES-256-GCM，随机 nonce，旧 XOR 密文迁移读取 | 已完成 |
| `RghUserIsolationHook` | 每用户 HOME/USERPROFILE、命令前投影、命令后回写、登录 watcher、logout 清理 | 已完成 |
| `RghTokenStore` / `RghWorkspaceCleaner` | Redis 密文权威源、TTL、本地 700/600 权限、过期工作区清理 | 已完成 |
| Weather MCP 单例 Bean | Streamable HTTP 持久 MCP session、工具白名单、失败重建 | 已完成 |
| Orizn Visa MCP 单例 Bean | stdio 持久 MCP session、免费/完整模式、工具白名单 | 已完成 |
| `SensitiveMasker` / Logback Converter | 日志、运行事件、SSE 三层边界递归脱敏 | 已完成 |
| `TravelDataNormalizer` / Search capture | 四来源结果归一化为交通和酒店候选，接入 Plan LangGraph | 已完成 |

Java 当前源码虽然保存了四份 Skill 资源，但 `ItineraryPlanAgent` 和 `BookingAgent` 的 `registerSkill` 实际只显式注册了 `tuniu-cli`。Python 版为满足项目目标，将四个旅行 Skill 全部加入注册表，并在候选搜索节点真实并发调用；这属于补齐 Java 资源与装配之间的缺口，不是删除 Java 的企业级隔离设计。

规划闭环为：加载政策与偏好 → 四源并发搜索交通/酒店 → 统一数据模型 → 组合与多目标评分 → 天气/签证/政策等多维审核 → 结构化整改 → 最多两轮重新规划 → 输出方案。预订写操作仍只由 Booking Agent 执行。

## 需要安装什么

基础环境：

```powershell
cd D:\x-project\codingPractice\python\gogoagent\gogo-agent
uv sync --python 3.11 --extra dev
```

尚未安装 uv 时，项目收尾阶段先按 [uv 官方 Windows 安装文档](https://docs.astral.sh/uv/getting-started/installation/) 安装；具体命令和锁文件规范见 [项目完成后统一配置与验收清单](PROJECT_COMPLETION_CONFIGURATION_CHECKLIST.md)。Python 依赖已经写入 `pyproject.toml`，其中新增 `cryptography`（AES-GCM）和 `mcp`（持久 MCP 客户端），无需逐个安装。`uv` 会把依赖放入项目 `.venv/`，不会安装成全局 Python 包。

运行四个旅行 Skill，推荐安装 Node.js 18 或更高版本，然后执行：

```powershell
npm install
```

上面是项目级安装：依赖落在 Windows 项目根目录的 `node_modules/`，命令落在 `node_modules/.bin/`，不会污染系统全局 npm。Python 执行器会自动把该目录加入 Skill 子进程的 `PATH`。不要使用 `npm install -g`。

确认 `node_modules/.bin` 下存在 `tuniu.cmd`、`flyai.cmd`、`rgh.cmd` 和 `orizn-visa-mcp.cmd`。Python Agent 会自动调用项目级命令；手工验证可使用 `npx --no-install tuniu --version`。RollingGo 不想安装 Node.js 时，也可在 `backend/resources/skills/rolling-go-hotel` 下运行 `python scripts/install.py`，将独立二进制下载到该 Skill 的 `bin/rgh.exe`。

`flight-manager` 不需要本地 CLI，Python 通过 HTTPS JSON-RPC 调用。每位用户分别获取并保存自己的 Key：

- 途牛：https://open.tuniu.com/mcp/login
- 航班管家：https://h5.133.cn/webapp/pages/mcpApiKey
- FlyAI（可选增强 Key）：https://open.fly.ai/
- RollingGo：首次使用时由 `rgh` OAuth PKCE 登录链接授权，不手工保存 Token。

签证 MCP 使用项目级依赖和 `npx --no-install orizn-visa-mcp`，因此需要 Node.js/npm；不配置 `ORIZN_VISA_API_KEY` 时使用免费模式。天气 MCP 是 Streamable HTTP 服务，不需要本地程序，但远期天气需要配置 `WEATHER_MCP_ENDPOINT`。

## 本地项目连接云端中间件

- MySQL：将 `GOGO_DATABASE_URL` 改为 `mysql+pymysql://user:password@云服务器地址:3306/gogo`。SQLite 只用于本地演示。
- Redis：将 `REDIS_URL` 改为 `redis://:密码@云服务器地址:6379/0`。它保存 API Key 缓存、RollingGo Token、熔断状态和中断广播。
- 云服务器安全组只允许你的本地公网 IP 访问 3306/6379；更推荐通过 VPN、Tailscale 或 SSH 隧道连接，不要把 Redis 无密码暴露到公网。
- MinIO：需要持久化行程 HTML/附件时配置；非该功能的强制依赖。
- `API_KEY_ENCRYPT_SECRET`：使用密码管理系统生成至少 32 字节随机值，所有节点一致，不能使用示例默认值。
- Python 后端和各 CLI 必须保留在同一个 Windows 项目目录；不要只在云服务器安装 CLI，因为当前执行器是本地 `subprocess`。
- 不要在系统环境变量中配置用户级 `TUNIU_API_KEY`、`FLIGHT_API_KEY`、`FLYAI_API_KEY`。Python 会清除继承值，只注入当前认证用户的密钥。

完整变量已写入 `.env.example`。外部服务未配置或单个 Provider 故障时，Plan 会保留其他来源结果并结构化降级，不会拖垮整个规划流程。

## 验证

```powershell
uv run ruff check backend/infrastructure/security.py backend/infrastructure/mcp.py backend/services/api_key_service.py backend/services/rgh_isolation.py backend/services/candidate_search.py backend/tools/apikey.py backend/tools/live.py backend/tools/skills.py tests/test_external_integrations_enterprise.py
uv run pytest -q
```

自动测试覆盖 AES-GCM 完整性、旧密文迁移、租户上下文不可覆盖、全边界脱敏、四 Skill 注册、多用户 API Key 注入、RollingGo Token 投影/回写和 MCP 白名单。真实搜索、OAuth 和预订仍需使用你自己的测试账号在联调环境验收，自动测试不会提交真实订单。

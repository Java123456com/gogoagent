# GoGo Agent 统一工具韧性与故障隔离改造计划

## 1. 背景与结论

Python 版已经具备 MasterAgent、四个业务子 Agent、统一 `BaseSubAgent`、上下文 Hook、会话记忆、长期记忆及工具熔断等基础能力，但工具执行策略仍分散在 MCP、HTTP 和 Shell 的具体实现中，子 Agent 也仍在 API 进程内直接调用。

本次改造补齐以下四项能力：

1. 所有工具统一进入超时控制。
2. 根据工具副作用等级执行安全重试。
3. 重试采用带随机抖动的指数退避，并与熔断器协同。
4. 四个业务子 Agent 支持独立 Worker 进程执行、超时终止、崩溃拉起及事件回传，实现进程级故障隔离。

本计划不把项目直接拆成四个独立微服务。先在单仓库、单部署单元内实现多进程隔离，同时保留可切换的本地执行模式；后续如需独立扩缩容，可沿同一个执行协议迁移到远程服务。

## 2. 改造目标

### 2.1 功能目标

- `BaseSubAgent` 注册的所有 LangChain 工具统一经过韧性包装层。
- 默认策略对未知工具采取保守行为：有超时、不自动重试。
- 查询类工具支持网络异常、连接超时及明确的临时服务错误重试。
- 写工具按照幂等能力分级，禁止对非幂等写操作盲目重试。
- 熔断状态支持 Redis 共享；Redis 不可用时降级为进程内状态。
- MasterAgent 通过统一执行协议调用子 Agent，不再直接依赖具体 Agent 单例。
- 生产隔离模式下，Manage、Plan、Info、Booking 分别运行在独立 Worker 进程。
- Worker 超时、退出或无响应时，主进程返回结构化错误并按策略重启 Worker，不拖垮其他 Agent。
- 工具重试、超时、熔断、Worker 重启等事件进入现有日志和 SSE 事件体系。

### 2.2 质量目标

- 不改变现有 Agent 提示词、工具名称和前端 SSE 契约。
- 不破坏 `sessionId:agentName` 会话隔离和用户级长期记忆。
- 默认开发模式继续支持无外部模型、无 Redis 的确定性测试。
- 所有现有测试继续通过，并新增策略、幂等、熔断和进程隔离测试。

## 3. 目标架构

```text
FastAPI / SSE
      |
AgentPipelineService
      |
MasterAgent
      |
SubAgentExecutor 协议
      +-- LocalSubAgentExecutor（开发/单元测试）
      |
      `-- ProcessSubAgentExecutor（生产隔离模式）
             +-- Manage Worker
             +-- Plan Worker
             +-- Info Worker
             `-- Booking Worker
                    |
                    `-- ResilientToolHook
                          |
                          +-- Policy Registry
                          +-- Deadline / Timeout
                          +-- Retry + Exponential Backoff + Jitter
                          +-- Circuit Breaker
                          +-- Idempotency / Reconciliation
                          `-- Progress & Metrics Events
```

执行顺序统一为：

```text
策略解析
  -> 熔断准入
  -> 幂等检查
  -> 带截止时间的工具调用
  -> 失败分类
  -> 安全重试与指数退避
  -> 成功提交/失败登记
  -> 结果压缩、业务副作用和进度通知
```

熔断器按一次“逻辑工具调用”计一次最终失败，不按内部每次重试分别累计，避免一次请求快速击穿熔断阈值。半开状态只允许一个探测请求进入。

## 4. 工具执行策略设计

### 4.1 工具副作用分级

| 等级 | 含义 | 默认重试 | 典型工具 |
|---|---|---:|---|
| `READ_ONLY` | 不修改业务状态 | 允许 | 天气、新闻、签证、知识库、订单查询、政策查询、记忆召回 |
| `LOCAL_IDEMPOTENT` | 本地计算或可重复覆盖 | 允许有限重试 | 行程评分、审核、计划读取、HTML 生成 |
| `IDEMPOTENT_WRITE` | 有稳定幂等键或唯一约束的写操作 | 条件允许 | 更新联系人、更新计划状态、记录长期记忆 |
| `NON_IDEMPOTENT_WRITE` | 重复执行可能产生重复订单或不可逆副作用 | 禁止自动重试 | 提交审批、实际预订、出票、部分取消操作 |
| `INTERACTIVE` | 等待用户输入或控制流信号 | 禁止 | `ask_user` |

具体登记方式采用中心注册表，按工具名显式配置；未登记工具使用 `UNKNOWN` 保守策略。注册表启动时校验所有 Agent 工具是否已分类，测试环境对漏配直接失败，生产启动记录高优先级告警。

### 4.2 默认策略

与 Java 版默认值保持语义对齐，同时增加安全边界：

| 参数 | 默认值 |
|---|---:|
| 单次尝试超时 | 60 秒 |
| 最大尝试次数 | 3 次（含首次） |
| 初始退避 | 3 秒 |
| 退避倍率 | 2.0 |
| 最大退避 | 15 秒 |
| 随机抖动 | 0～20% |
| Agent 总截止时间 | Manage 120 秒、Info 120 秒、Booking 300 秒、Plan 600 秒 |

工具可覆盖默认值。例如 Shell 保留自身最长 600 秒限制，规划类本地计算可使用较短超时，外部预订执行采用业务专用总截止时间但默认不自动重试。

### 4.3 可重试失败

允许重试：

- 网络连接建立失败、连接重置、临时 DNS 失败。
- HTTP/MCP 明确返回 408、429、502、503、504。
- 服务端明确标记为 `temporary` 或 `retryable` 的错误。
- 读取型工具发生数据库瞬时连接错误。

禁止重试：

- 参数校验失败、权限失败、鉴权配置缺失。
- 业务拒绝、库存不足、政策不允许。
- 用户交互中断和 Agent 取消信号。
- 熔断器拒绝。
- 非幂等写操作结果未知。
- Python 编程错误，例如 `TypeError`、`AttributeError`。

如响应包含 `Retry-After`，优先遵循服务端建议，但不能超过最大退避和本次请求剩余截止时间。

### 4.4 超时语义

- HTTP、MCP、数据库和子进程命令优先使用客户端原生超时，以便真正取消底层 I/O。
- 同步 Python 函数的线程级 timeout 只能停止等待，不能安全杀死线程，因此不能作为硬隔离保证。
- Agent 级硬超时由 Worker 进程边界兜底：Worker 无法在截止时间内响应时，Supervisor 终止并重建对应进程。
- 每次重试前检查 Agent 剩余截止时间；不足以完成下一次尝试时直接结束。

## 5. 幂等与写操作安全

### 5.1 幂等键

写调用统一携带：

```text
idempotency_key = session_id + agent_name + tool_call_id + normalized_arguments_hash
```

新增幂等执行记录，至少保存：幂等键、用户、工具名、参数摘要、状态、业务结果引用、创建时间和过期时间。数据库设置唯一约束，避免并发重复提交。

### 5.2 写操作规则

- 本地更新类工具必须使用数据库事务和唯一约束实现幂等。
- 外部服务原生支持幂等键时透传同一个键。
- 外部服务不支持幂等时，超时后先按外部订单号、用户和业务参数进行结果核验；只有确认未执行才允许补偿性重试。
- 无法确认结果的预订、出票、提交审批操作返回 `UNKNOWN_OUTCOME`，交给用户或后续恢复流程确认，绝不自动重复下单。
- 取消操作只有在服务端提供幂等取消语义，或本地能够确认当前状态时才允许重试。

## 6. 进程级故障隔离

### 6.1 执行协议

新增 `SubAgentExecutor` 抽象，输入输出只使用可序列化 DTO：

- 请求：`request_id`、`agent_name`、`user_id`、`session_id`、`deadline`、消息和必要业务上下文。
- 响应：`final`、`trace`、`pending_interaction`、状态变更摘要和结构化错误。
- 事件：沿用现有 `thinking`、`tool_call`、`tool_done`、`agent_done` 等 SSE 事件结构，并新增韧性事件。

MasterAgent 的四个子 Agent 工具改为调用 executor，不直接调用全局 Agent 单例。

### 6.2 Worker 模型

- 使用 `multiprocessing` 的 `spawn` 模式兼容 Windows。
- 每个业务域至少一个常驻 Worker 进程，进程内独立初始化模型、Agent、MCP 客户端和工具集合。
- 主进程 Supervisor 负责启动、健康检查、请求投递、结果收集、超时终止和拉起。
- 每个 Worker 同一时间默认只处理一个 Agent 请求，避免 Agent 全局单例及上下文变量发生串扰；吞吐量通过每域 Worker 数配置扩展。
- Worker 意外退出只失败当前请求；其他域 Worker 和 API/Master 继续工作。
- Worker 重启采用指数退避并设置单位时间最大重启次数，防止崩溃循环。
- 主进程关闭时执行优雅排空；超过关闭宽限期后再终止 Worker。

### 6.3 状态与事件

- 会话、长期记忆、订单和规划结果继续存放在数据库/Redis，不依赖 Worker 进程内存。
- Worker 事件通过独立 IPC 队列回传主进程，再进入现有 SSE 通道。
- 工具参数和日志先脱敏再跨进程传输，API Key 不进入事件载荷。
- SQLite 开发环境启用 WAL 和 `busy_timeout`；生产隔离模式推荐 MySQL/PostgreSQL 与 Redis，避免多进程 SQLite 写竞争。

### 6.4 运行模式

| 模式 | 用途 | 行为 |
|---|---|---|
| `local` | 单元测试、调试、无多进程环境 | 当前进程内执行，但仍启用统一工具策略 |
| `process` | 默认生产模式 | 子 Agent 在域级 Worker 中执行并受 Supervisor 管理 |

不在 Worker 可用性故障时自动降级到本地执行，以免故障期间绕过隔离并重复执行写操作。只有显式配置的开发模式允许本地执行。

## 7. 配置项规划

在 `backend/config/settings.py` 增加：

```text
tool_policy_enabled
tool_default_timeout_seconds
tool_default_max_attempts
tool_retry_initial_backoff_seconds
tool_retry_backoff_multiplier
tool_retry_max_backoff_seconds
tool_retry_jitter_ratio
tool_policy_strict_registration

subagent_execution_mode                 # local/process
subagent_worker_count_manage
subagent_worker_count_plan
subagent_worker_count_info
subagent_worker_count_booking
subagent_deadline_*_seconds
subagent_worker_start_timeout_seconds
subagent_worker_shutdown_grace_seconds
subagent_worker_max_restarts_per_window
```

现有熔断配置继续复用，并补充半开并发数和分布式探测锁 TTL。配置文件和 README 同步提供开发、测试、生产示例。

## 8. 代码改造清单

### 8.1 新增模块

```text
backend/runtime/
  tool_policy.py          # 副作用等级、策略模型和注册表
  tool_errors.py          # 超时、重试耗尽、未知结果等结构化异常
  resilient_tool.py       # 同步/异步统一包装与执行顺序
  retry.py                # 失败分类、指数退避、jitter、deadline
  idempotency.py          # 幂等键和执行记录服务
  agent_executor.py       # SubAgentExecutor 协议及本地实现
  process_executor.py     # 多进程实现、IPC 和请求关联
  worker.py               # Worker 入口与 Agent 注册
  supervisor.py           # 健康检查、终止、拉起和关闭
  dto.py                  # 跨进程请求、响应、事件 DTO
```

### 8.2 修改模块

- `backend/agents/base.py`：统一安装 `ResilientToolHook`，透传 deadline/cancel 上下文。
- `backend/agents/master.py`：四个子 Agent 工具改走 `SubAgentExecutor`。
- `backend/hooks/context_hooks.py`：明确 Hook 顺序，进度 Hook 增加 attempt/timeout/retry 字段。
- `backend/services/circuit_breaker.py`：增加 async 路径、逻辑调用级失败统计和半开单探测。
- `backend/infrastructure/stores.py`：完善 Redis 原子状态与探测锁。
- `backend/core/request_context.py`：增加 request、tool call、deadline、idempotency 上下文。
- `backend/services/runtime_events.py`：接收并转发 Worker IPC 事件。
- `backend/api/main.py`：应用 lifespan 中启动和关闭 Supervisor，替换已弃用的 `on_event`。
- `backend/config/settings.py`：新增策略及 Worker 配置。
- `backend/infrastructure/models.py`、`repositories.py`、数据库 schema：增加幂等执行记录。
- `backend/tools/*.py`：登记副作用等级；外部 I/O 统一接收剩余 deadline；写工具接入幂等或核验逻辑。
- `README_PYTHON.md`、`.env.example`、`开发进展.md`：补充运行模式和运维说明。

## 9. 实施阶段

### 阶段一：策略基础与可观测性

1. 建立工具策略模型、注册表和启动校验。
2. 实现错误分类、deadline、指数退避和 jitter。
3. 实现同步/异步 `ResilientToolHook`，接入 `BaseSubAgent`。
4. 将熔断器从 `live.py` 的局部调用提升到统一包装层。
5. 增加 retry/timeout/circuit SSE 与结构化日志。

验收：所有工具经过统一入口；只读工具可按策略重试；未知工具不重试；现有测试不回归。

### 阶段二：写工具幂等与恢复

1. 增加幂等执行记录表、Repository 和 Service。
2. 为本地幂等写工具添加事务与唯一约束。
3. 对预订、出票、取消、审批类工具增加结果核验和 `UNKNOWN_OUTCOME`。
4. 确保重试过程中复用相同幂等键。

验收：并发重复调用只产生一次业务副作用；未知结果不会触发盲目重试；能够查询和恢复未决调用。

### 阶段三：子 Agent 执行协议

1. 抽取 `SubAgentExecutor` 和 DTO。
2. 先以 `LocalSubAgentExecutor` 替换 Master 对子 Agent 的直接依赖。
3. 固化跨边界错误、HITL 暂停、会话恢复和事件格式。

验收：local 模式行为及 SSE 与改造前一致，Master 不再直接持有四个子 Agent 实例。

### 阶段四：进程隔离与 Supervisor

1. 实现四域 Worker、IPC 请求响应和事件回传。
2. 实现硬超时终止、崩溃检测、重启退避和优雅关闭。
3. 处理 Windows `spawn`、MCP/模型客户端子进程初始化及资源清理。
4. 增加 SQLite 开发保护和生产数据库检查提示。

验收：人为让 Plan Worker 死循环或退出时，Info/Booking/Manage 和 API 仍可用；超时 Worker 被终止重建；当前请求收到明确错误且不会重复写入。

### 阶段五：回归、压测与灰度

1. 全量回归测试和真实模型/MCP 集成测试。
2. 注入超时、429、连接重置、Worker crash、Redis 失联等故障。
3. 先启用统一策略，后灰度启用 process 模式。
4. 观察成功率、P95/P99、重试放大量、熔断率、未知结果数和 Worker 重启率。

## 10. 测试计划

### 10.1 单元测试

- 策略注册、默认策略和 Agent/工具级覆盖。
- 可重试与不可重试异常分类。
- 退避序列、最大退避、jitter 边界和 deadline 截断。
- 同步/异步工具超时。
- 取消信号和 HITL 信号不会被重试。
- 熔断关闭、打开、半开、恢复及 Redis 降级。
- 幂等键稳定性、并发唯一性和结果复用。

### 10.2 集成测试

- HTTP/MCP 前两次失败、第三次成功。
- 429 + `Retry-After`。
- Shell 超时后子进程清理。
- 非幂等写调用发生超时后返回 `UNKNOWN_OUTCOME`。
- Worker 正常请求、事件回传、进程退出、死锁超时和自动拉起。
- Worker 重启后的 Session/HITL 恢复。
- Redis 不可用时的降级行为。

### 10.3 契约与回归测试

- 现有回归测试全部通过（当前共 67 项）。
- SSE 前端字段向后兼容。
- deterministic fallback 和无 API Key 模式仍可运行。
- Master 的四个子 Agent 注册关系不变。
- 测试工具清单与策略注册表完全一致，无漏配。

## 11. 验收标准

- 任一注册工具都能查询到明确的副作用等级和执行策略。
- 只读工具在瞬时网络故障下按配置重试，等待时间符合指数退避范围。
- 非幂等写操作不会被自动重复执行。
- 单次工具超时不会无限占用 Agent；Agent 总截止时间始终生效。
- 连续失败达到阈值后熔断，半开只放行一个探测请求。
- 任一业务 Worker 崩溃、卡死或内存异常退出时，API、Master 和其他业务域继续服务。
- 故障 Worker 可按退避策略自动恢复，且不会导致当前写操作重复提交。
- 会话、长期记忆、计划结果及 HITL 状态在 Worker 重启后可恢复。
- 日志和 SSE 能关联 `request_id`、`agent_name`、`tool_name`、`attempt` 和错误类型，且不泄露密钥或敏感参数。
- 全量自动化测试通过，并完成至少一轮故障注入验证。

## 12. 风险与控制

| 风险 | 控制措施 |
|---|---|
| 重试放大第三方服务压力 | jitter、总 deadline、最大尝试次数、熔断和 `Retry-After` |
| 写操作重复执行 | 工具分级、幂等键、唯一约束、结果核验、`UNKNOWN_OUTCOME` |
| 同步函数 timeout 后仍在后台运行 | 客户端原生 timeout；Agent 级 Worker 硬终止兜底 |
| 多进程下 SQLite 锁竞争 | WAL + busy timeout；生产使用 MySQL/PostgreSQL |
| Worker 频繁崩溃循环 | 重启退避、时间窗限次、健康状态告警 |
| 跨进程事件乱序 | request/event 序列号，主进程按请求关联并保持单流顺序 |
| Redis 故障导致熔断状态分裂 | 明确降级告警；生产健康检查；幂等最终由数据库唯一约束保证 |
| 改造 Hook 顺序导致结果或事件变化 | 固化 Hook 顺序，增加工具结果和 SSE 契约测试 |

## 13. 交付物

- 统一工具策略、重试、退避、超时及熔断实现。
- 幂等记录及非幂等写保护。
- 本地与多进程两种 `SubAgentExecutor`。
- 四域 Worker、Supervisor 和 IPC 事件桥接。
- 配置模板、架构说明、运行及故障排查文档。
- 单元、集成、契约、故障注入测试及验收结果。

## 14. 推荐实施边界

本轮以“单应用内多进程”作为故障隔离终态，不同步引入 Docker 编排、消息队列或四套独立部署服务。这样可以真正隔离崩溃和卡死，同时控制改造范围。执行协议必须保持传输无关，未来需要独立服务时，可将 IPC executor 替换为 HTTP/gRPC executor，而无需改动 Master 的路由逻辑。

## 15. 实施记录（2026-09-01）

- 阶段一已完成：`ResilientToolHook` 已接入 `BaseSubAgent` 的 LangGraph 工具装配点，支持工具超时、可重试错误分类、指数退避、jitter、逻辑调用级熔断以及 `tool_retry`/`tool_timeout`/`tool_circuit_open` 进度事件。
- 阶段二已完成基础能力：新增 `tool_execution_record` 幂等记录表和 Repository；幂等写操作复用成功结果，重复进行中的请求被拒绝；非幂等写操作超时会转换为 `ToolUnknownOutcome`，不会盲目重试。
- 阶段三已完成：Master、续跑、HITL 恢复和调试 Agent 路径均通过 `SubAgentExecutor`；Master/API 不再启动时构造业务域 Agent，`local` 模式保持单进程兼容行为。
- 阶段四已完成首版：`process` 模式使用 Windows 兼容的 `spawn`，四个业务域按 Worker 隔离，支持 IPC 事件回传、硬截止时间、Worker 终止、崩溃重启、启动失败限次、半开单探测和域级并发锁；SQLite 开发模式启用 WAL/busy timeout，API lifespan/CLI 会负责关闭 Worker。
- 阶段五已完成基础回归：新增韧性、Worker、半开熔断和事件脱敏测试，当前全量测试为 67 项通过；process 模式已通过 CLI 端到端验证，并补充了 workflow 懒加载以避免进程 Worker 启动时的循环依赖。生产上线前仍建议在目标环境执行真实 MCP/模型故障注入和多实例压测。

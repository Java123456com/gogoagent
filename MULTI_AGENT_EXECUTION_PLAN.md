# GoGo Agent Python 多智能体架构执行计划

## 1. 目标与结论

本文以 `gogo-agent_java_version/src/main` 的当前源码为事实基线，并参考根目录
`项目实战3（gogo-agent）` 配套设计文档，为 Python 版补齐并固化以下运行架构：

- 4 个 ReAct Agent：`MasterAgent`、`ItineraryManageAgent`、`InfoAgent`、`BookingAgent`。
- 1 个 Plan-and-Execute Agent：`ItineraryPlanAgent`。
- 2 个普通单次 LLM Agent：`QueryRewritingAgent`、`IntentRecognitionAgent`。
- 2 个轻量 LLM 服务：`ConversationTitleService`、`QuestionRecommendationService`。
- 3 档模型名称、4 种调用配置：`qwen3.6-flash`、`glm-5.1`、
  `qwen3.7-max`、开启 thinking 的 `qwen3.7-max`。

Python 版已完成本文的核心代码改造：角色注册表、模型 Profile、真实 thinking 参数、显式
Plan-and-Execute 子图、轻量服务接入和工具权限边界均已落地。剩余工作集中在生产运营化：
真实模型 smoke test、指标/告警、生产环境 process 模式默认值与发布演练。

## 2. 事实基线与设计取舍

### 2.1 Java 当前源码的真实架构

Java 当前运行链路为：

```text
用户请求
  -> L1 规则 / L2 向量快速意图识别
  -> 命中：跳过问题改写
  -> 未命中：QueryRewritingAgent -> IntentRecognitionAgent(L1/L2/L3)
  -> MasterAgent
  -> Manage / Plan / Info / Booking
  -> 最终回复 -> QuestionRecommendationService

意图识别完成后：ConversationTitleService 异步生成标题
```

关键事实：

1. `MasterAgent` 始终负责最终调度。Java 曾实现“高置信单意图直跳子 Agent”，但已回滚。
2. Master 只注册 4 个子 Agent：Manage、Plan、Info、Booking。
3. 当前 `ItineraryPlanAgent` 直接挂载 `ItineraryReviewTools`，不再把
   `ItineraryReviewAgent` 注册为子 Agent。
4. `ReimbursementAgent.build()` 当前返回 `null`，不是可运行能力。
5. Java 的模型分级是按角色静态绑定，不是每次请求先由分类器动态选择模型。
6. Java 的 Plan Agent 本质是“强思考 ReAct + PlanNotebook + 规划/审核工具”，不是经典的
   Planner LLM 生成计划、Executor LLM 逐项执行的两个完全独立 Agent。

源码与配套文档冲突时，以当前源码为准。例如 `076_✅各个Agent配置对比一览.md` 中仍写着
Plan `maxIters=15`、审核为子 Agent；当前源码已经改为 `maxIters=30`、直接使用审核工具。

### 2.2 Python 目标口径

为了让“4 + 1 + 2 + 2”既准确又容易验收，采用运行角色口径，而不是按类名或文件数计数：

| 类型 | 数量 | 运行角色 | 实现形态 |
| --- | ---: | --- | --- |
| ReAct | 4 | Master、Manage、Info、Booking | LangGraph `create_react_agent` |
| Plan-and-Execute | 1 | Plan | 显式规划状态机，必要步骤可调用 ReAct 执行器 |
| 普通 Agent | 2 | Rewrite、Intent | 单次结构化 LLM 调用，无工具循环、无 ReAct memory |
| 轻量服务 | 2 | Title、Recommendation | 单次 fast model 调用，不计入 Agent |

`ItineraryReviewAgent` 仅保留为兼容/历史实现，不导出为 Master 可调度 Agent，也不计入运行架构；
审核能力由 Plan 中的 `review_tools` 提供。`ReimbursementAgent` 保留明确的
`NOT_IMPLEMENTED` 契约，不注册到生产路由。

## 3. Python 当前状态与缺口

| 能力 | 当前状态 | 主要缺口 |
| --- | --- | --- |
| 4 个 ReAct 角色 | 已完成 | `backend/agents/registry.py` 提供可机器校验的注册表 |
| Plan-and-Execute | 已完成高级闭环 | 显式图覆盖校验、候选搜索、规划、审核、结构化整改、最多两轮重规划、无进展保护、阶段恢复和渲染；真实供应商生产演练待补 |
| 2 个普通 Agent | 基本完成 | 单次调用和 L1/L2/L3 短路已具备；可继续增强 provider structured output |
| 2 个轻量服务 | 已完成 | Title 异步接入意图节点；Recommendation 使用 JSON、最多 4 条 |
| 模型分级 | 已完成 | `extra_body` 实际传递 `enable_thinking` 与 `thinking_budget` |
| 子 Agent 隔离 | 已有基础 | local/process 均支持，生产环境默认值与发布演练待定 |
| 上下文隔离 | 已有基础 | 已按 `sessionId:agentName` 保存并压缩；指标待补 |
| 故障治理 | 已有基础 | 已有超时、重试、熔断、幂等和 Worker 重启；端到端演练待补 |
| 架构说明 | 已完成 | Python README 和注册表采用统一计数口径 |

## 4. 目标架构

### 4.1 调度拓扑

```text
                          +------------------------+
                          | ConversationTitle      |  qwen3.6-flash
                          | 后台、限时、失败静默    |
                          +-----------^------------+
                                      |
Request -> L1/L2 -> Rewrite? -> Intent(L3?) -> Master(ReAct, qwen3.7-max)
                                                |
                    +---------------------------+---------------------------+
                    |                           |                           |
             Manage(ReAct)               Plan(Plan&Execute)           Info(ReAct)
             qwen3.7-max                 qwen3.7-max+thinking          glm-5.1
                    |                           |                           |
                    |                    plan -> gather ->                  |
                    |                    generate -> review                 |
                    |                    -> repair -> render                |
                    |                           |                           |
                    +---------------------------+---------------------------+
                                                |
                                         Booking(ReAct)
                                         qwen3.7-max+thinking
                                                |
                                         final response
                                                |
                                  QuestionRecommendation
                                      qwen3.6-flash
```

Booking 与 Plan 必须保持独立：Plan 只做可反复试错的读、搜索、计算和审核；Booking 才能持有
下单/取消等有副作用工具，并执行“审批通过”强门禁。该边界同时解决职责混乱、上下文污染和
最小权限问题。

### 4.2 模型策略

不新增一次额外的“复杂度分类 LLM 调用”。复杂度由任务角色和风险预先定义，保持与 Java 一致：

| Profile | 模型 | Thinking | 使用方 | 原因 |
| --- | --- | --- | --- | --- |
| `FAST` | `qwen3.6-flash` | 关闭 | Title、Recommendation、轻量抽取 | 短文本、无工具、低风险 |
| `STABLE` | `glm-5.1` | 关闭 | Rewrite、Intent L3、Info、审核、记忆压缩 | 结构化/检索/校验，稳定与成本优先 |
| `STRONG` | `qwen3.7-max` | 关闭 | Master、Manage | 路由与业务状态决策 |
| `STRONG_THINKING` | `qwen3.7-max` | 开启，预算默认 2048 | Plan、Booking | 多步骤工具编排和高风险执行 |

如果将来需要请求级动态升降档，只允许在同一角色内做受控升级，例如 Info 从 `STABLE` 升到
`STRONG`；不得让动态选模改变 Agent 工具权限或绕过 Booking 的审批门禁。

## 5. 分阶段实施计划

### 阶段 0：建立架构契约和基线测试

目标：先把“什么算完成”写成代码契约，防止后续继续出现 9 Agent、12 角色等口径漂移。

改造：

- 新增 `backend/agents/registry.py`，声明运行角色、类型、模型 Profile、最大迭代、工具权限和
  是否允许 Master 调度。
- 将 `full_agent_catalog.py` 改成从注册表生成展示信息，不再手工维护第二份事实。
- 明确生产可调度白名单只有 Manage、Plan、Info、Booking。
- Review 标注 `legacy_internal_only`；Reimbursement 标注 `not_implemented`。
- 新增 `tests/test_agent_architecture_contract.py`。

验收：

- 测试精确断言 4 个 ReAct、1 个 Plan-and-Execute、2 个普通 Agent、2 个轻量服务。
- 测试精确断言 Master 只有 4 个子 Agent 工具。
- Review/Reimbursement 无法通过生产路由执行。

### 阶段 1：模型 Profile 与真实 thinking 参数

目标：把“模型名字不同”升级成“调用行为确实不同”。

改造文件：

- `backend/config/settings.py`
- `backend/infrastructure/llm.py`
- 新增 `backend/infrastructure/model_profiles.py`
- `.env.example`

实施内容：

1. 用 `ModelProfile` 集中定义 model name、temperature、thinking 开关、thinking budget、超时和
   最大重试。
2. DashScope OpenAI-compatible 调用中为 `STRONG_THINKING` 传入 provider 支持的 thinking
   参数；参数名需通过当前 SDK 的实际请求测试确认，禁止只改函数名或注释。
3. 保留 4 个语义清晰的工厂：`fast_model`、`stable_model`、`strong_model`、
   `strong_model_with_thinking`，内部统一走 Profile。
4. 在 trace/日志记录 Agent、Profile、model name、thinking enabled；不得记录密钥或完整敏感
   prompt。
5. 模型调用失败时沿用确定性 fallback，但输出中增加明确的降级原因和指标。

验收：

- mock 请求断言 Plan/Booking 带 thinking 参数，Master/Manage 不带。
- 角色到 Profile 的映射与上表完全一致。
- 未配置 Key 时不发网络请求，现有 deterministic 测试继续通过。

### 阶段 2：将 Plan 改成可验证的 Plan-and-Execute 子图

目标：让规划阶段、状态、重试和恢复显式化，同时保留 Java 的工具边界。

新增/改造文件：

- 新增 `backend/workflow/itinerary_plan_graph.py`
- 新增 `backend/core/plan_state.py`
- 改造 `backend/agents/itinerary_plan.py`
- 改造 `backend/services/plan_notebook.py`
- 复用 `backend/tools/planner.py`、`backend/tools/review.py`、`backend/tools/plan_html.py`

建议子图：

```text
START
  -> validate_input
  -> create_plan
  -> load_constraints
  -> gather_candidates
  -> generate_proposals
  -> review_proposals
  -> [pass/warning] render_plan -> END
  -> [fail && repair_count < 2] repair_plan -> review_proposals
  -> [fail && repair_count >= 2] return_review_failure -> END
```

节点职责：

- `validate_input`：校验出发地、目的地、往返日期；缺字段时生成 HITL 表单并挂起。
- `create_plan`：创建 PlanNotebook，写入阶段任务和初始状态。
- `load_constraints`：并行加载审批单、政策、用户偏好和长期记忆，只读。
- `gather_candidates`：并行查询交通、酒店、天气和必要的签证信息；外部结果先压缩再入上下文。
- `generate_proposals`：调用确定性 `plan_itinerary` 组合、评分并生成代表方案。
- `review_proposals`：调用 `review_tools` 做客观校验、主观评估和仲裁。
- `repair_plan`：只针对审核问题重新筛选/调整，最多两轮，防止无限 Reflexion。
- `render_plan`：生成最终文本、结构化 travel data 和 HTML，不执行预订。

设计约束：

- Plan 子图不得注册任何下单工具。
- 每个节点进入、成功、失败都更新 PlanNotebook，并通过现有 SSE 推送 `plan_update`。
- 每个节点输出结构化字段，禁止依靠上一节点自然语言重新解析全部状态。
- 节点级状态写入 `sessionId:ItineraryPlanAgent`，恢复时从最后未完成节点继续。
- 候选原始大结果进入对象/候选存储，LLM 上下文只保留摘要和引用 ID。
- `review_tools` 是内部能力，不再调用独立 `ItineraryReviewAgent`。

验收：

- 正常规划严格经过 create/gather/generate/review/render。
- 审核失败能修复并重审，最多两次。
- 任一外部查询失败只降级对应数据源，不拖垮 Master 或其他 Agent。
- 中断后可从未完成步骤恢复，已完成的有副作用/高成本步骤不重复执行。
- 规划输出不产生 booking record。

### 阶段 3：收紧 4 个 ReAct Agent 的职责与工具权限

目标：让职责边界成为代码约束，而不只是 prompt 约定。

改造：

- `MasterAgent`：仅保留 `ask_user`、长期记忆工具和 4 个子 Agent 工具；不挂业务写工具。
- `ItineraryManageAgent`：持有差旅单读写、审批、冲突检测、预订只读、用户信息工具；不持有外部
  下单工具。
- `InfoAgent`：只持有政策、三个 RAG、天气/签证/资讯等只读工具。
- `BookingAgent`：持有审批单只读、候选方案只读、预订写入/取消和必要 API Key/Skill；所有写操作
  必须检查审批状态、用户确认和幂等键。
- 为工具增加 `READ_ONLY`、`REVERSIBLE_WRITE`、`IRREVERSIBLE_WRITE` 权限标签，并在 Agent 注册时
  校验允许矩阵。

验收：

- 架构测试扫描每个 Agent 的工具集合并与权限矩阵比对。
- Plan/Info 无法导入或调用预订写工具。
- Booking 在审批未通过、缺少明确确认或重复幂等键时拒绝执行。

### 阶段 4：补齐两个普通 Agent 的单次调用契约

目标：保证 Rewrite/Intent 轻量、无状态、可解析、可降级。

改造：

- 使用 Pydantic schema 约束 Rewrite 与 Intent 输出，优先采用 structured output；不再以宽松
  `json.loads` 作为唯一保障。
- Rewrite 只在 L1/L2 未命中时执行，并读取最近有限条历史。
- Intent 保持 L1 规则 -> L2 向量 -> L3 `STABLE` 的三级短路。
- 为单次调用增加独立超时；不创建 ReAct graph、不注册工具、不保存 ReAct memory。
- 所有请求最终仍进入 Master，保持 Java 已回滚直跳后的当前语义。

验收：

- L1/L2 命中时 Rewrite 和 L3 调用次数均为 0。
- L1/L2 未命中时调用顺序固定为 Rewrite -> Intent L3 -> Master。
- 非法 JSON、超时、模型不可用均能进入确定性 fallback。

### 阶段 5：真正接入两个轻量 LLM 服务

目标：不仅存在类，还要进入正确的业务时机，并且不阻塞主回复。

ConversationTitle：

- 在首次意图识别完成后提交后台任务。
- 只在会话仍为默认标题时更新，限制 24 字，默认超时 5 秒。
- 失败仅记录指标，不影响聊天主链路。

QuestionRecommendation：

- 在最终助手消息落库后执行，通过 SSE 发送 `suggestions`。
- 输出改为 `{"questions": [...]}` 结构化格式，最多 4 条。
- 注入 continuation 关键词分组；遇到澄清/选项问题时优先给具体选项，不机械生成“确认”。
- 设置短超时并允许空结果，不能延迟 `done` 太久。

改造文件：

- `backend/services/llm_services.py`
- `backend/workflow/pipeline.py`
- `backend/services/chat_stream.py`
- `backend/services/chat_service.py`

验收：

- 新会话标题能够异步更新；用户手工标题不会被覆盖。
- Title 超时不影响主回答。
- Recommendation 最多返回 4 条，解析失败时返回空列表。

### 阶段 6：上下文和故障域隔离收口

目标：针对上下文爆炸和故障扩散建立端到端保证。

改造：

- 生产环境默认 `subagent_execution_mode=process`；开发/测试保留 local。
- Worker 只覆盖 4 个 Master 子 Agent，其中 Plan Worker 运行 Plan-and-Execute 子图；Review 不作为
  独立 Worker。
- 保持 `sessionId:agentName` 会话命名空间，新增 user/session/agent 三重校验。
- 对外部工具统一执行 deadline、有限重试、指数退避、熔断和幂等；业务错误不重试。
- Master 调子 Agent 时只传递必要业务摘要和引用 ID，不复制完整候选搜索结果。
- 保持上下文压缩阈值和最近消息保护，并记录 token before/after、offload 数量和压缩原因。
- 子 Agent 崩溃返回结构化错误给 Master；不得终止其他 Worker 或清空整个会话。

验收：

- 强制杀死 Info Worker 后，Supervisor 能重启，Manage/Plan/Booking 仍可执行。
- 模拟天气服务连续失败后只熔断天气工具，政策 RAG 和订单查询仍可用。
- 两个用户使用相同 session 文本时不能互读消息、候选、偏好或预订记录。
- 60 条以上消息或达到 token 阈值时触发压缩，最近 20 条保持完整，卸载内容可回捞。

### 阶段 7：文档、观测与发布

改造：

- 更新 `README_PYTHON.md`，统一使用“4 ReAct + 1 Plan-and-Execute + 2 普通 Agent + 2 服务”口径。
- 将历史迁移文档标记为设计记录，不再作为当前运行状态说明。
- `/health` 增加模型 Profile、执行模式、Worker 健康状态；不暴露 API Key。
- 指标至少包括每个 Agent 的调用数、耗时、模型 token、工具失败/重试/熔断、上下文压缩、
  Plan 修复次数和 Worker 重启次数。
- 先在 deterministic 模式跑全量测试，再用真实模型做受控 smoke test，最后逐步开启 process 模式。

发布门槛：

1. `pytest` 全量通过，新增架构契约、模型参数、Plan 子图、服务接入和隔离测试。
2. Ruff 无新增错误。
3. 真实 DashScope smoke test 覆盖 Rewrite/Intent、Master 路由、Plan thinking、Booking 门禁、
   Title 和 Recommendation。
4. 对比基线记录端到端延迟、token、模型成本和失败率；没有基准数据时不宣称百分比收益。

## 6. 建议提交拆分

每个提交保持可测试、可回滚：

1. `test: define multi-agent architecture contract`
2. `refactor: centralize model profiles and enable thinking`
3. `feat: introduce itinerary plan-and-execute graph`
4. `refactor: enforce agent tool permission boundaries`
5. `refactor: harden rewrite and intent structured calls`
6. `feat: wire title and recommendation llm services`
7. `test: verify context and process fault isolation`
8. `docs: align python architecture and operations guide`

## 7. 完成定义

只有同时满足以下条件，才可以把该架构标记为“已实现”：

- 运行注册表能精确得到 4 + 1 + 2 + 2，且不是通过文档手工计数。
- Plan 有显式、可恢复、可观测的执行步骤和有界审核修复循环。
- `STRONG_THINKING` 的实际请求参数确实开启 thinking，而不是只有函数命名。
- Title 与 Recommendation 都接入真实业务链路，且失败不阻塞主回答。
- Agent 工具权限由代码校验，Plan 无下单权限，Booking 有审批/确认/幂等三重门禁。
- 每个 Agent 独立上下文；子 Agent 或单个工具故障不会扩散到其他 Agent。
- deterministic 全量测试和真实模型 smoke test 均通过。

## 8. 参考依据

Java 源码：

- `gogo-agent_java_version/src/main/java/com/gogo/travel/agent/config/ModelConfig.java`
- `gogo-agent_java_version/src/main/java/com/gogo/travel/agent/MasterAgent.java`
- `gogo-agent_java_version/src/main/java/com/gogo/travel/agent/ItineraryPlanAgent.java`
- `gogo-agent_java_version/src/main/java/com/gogo/travel/agent/BookingAgent.java`
- `gogo-agent_java_version/src/main/java/com/gogo/travel/agent/service/AgentPipelineService.java`
- `gogo-agent_java_version/src/main/java/com/gogo/travel/business/chat/service/ConversationTitleService.java`
- `gogo-agent_java_version/src/main/java/com/gogo/travel/agent/service/QuestionRecommendationService.java`

配套设计文档：

- `项目实战3（gogo-agent）/066_✅为什么单独搞一个BookingAgent？.md`
- `项目实战3（gogo-agent）/076_✅各个Agent配置对比一览.md`
- `项目实战3（gogo-agent）/077_✅工具并行执行&失败重试.md`
- `项目实战3（gogo-agent）/089_✅上下文工程——记忆自动压缩.md`
- `项目实战3（gogo-agent）/104_✅集群架构下的Agent打断与恢复.md`

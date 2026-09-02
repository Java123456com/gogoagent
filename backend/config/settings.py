"""运行时配置（对应 Java 的 application.yml / city-tier.yml / .env）。

所有外部依赖（模型、数据库、第三方 API Key、MCP、熔断、意图向量阈值）都收敛到
这一处，其余模块只读 ``get_settings()``，避免散落魔法值。默认值保证「无任何 API Key
也能跑通确定性路径」（L1 规则、政策、订单、规划评分等）。
"""
from __future__ import annotations

import json
from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---------------- LLM（DashScope 兼容 OpenAI 协议） ----------------
    # 是否真正调用大模型；False 时走确定性 fallback，用于本地无 Key 演示与单测。
    gogo_use_llm: bool = False
    openai_api_key: str | None = None
    openai_base_url: str | None = None  # 例如 https://dashscope.aliyuncs.com/compatible-mode/v1
    # 原 Java 的 agentscope.dashscope.api-key。可与 OPENAI_API_KEY 同时配置，
    # 百炼 provider 优先使用该值，模型 provider 仍可使用 OpenAI-compatible 配置。
    dashscope_api_key: str | None = None
    dashscope_cache_control: bool = True
    dashscope_embedding_enabled: bool = True
    dashscope_embedding_model: str = "text-embedding-v4"
    dashscope_embedding_dimensions: int = 1024
    # 三种模型，对应 Java 的 strongModel / stableModel / strongModelWithThinking
    fast_model: str = "qwen3.6-flash"
    strong_model: str = "qwen3.7-max"
    stable_model: str = "glm-5.1"
    strong_model_with_thinking: str = "qwen3.7-max"
    strong_model_thinking_budget: int = 2048

    # ---------------- 多目标行程规划 ----------------
    # 原始笛卡尔积超过阈值后，按价格/时间/偏好/政策等多路排序保留多样化候选，
    # 将实际枚举量压到原空间的 10% 以内，同时设置绝对上限防止组合爆炸。
    planner_pruning_trigger: int = 500
    planner_target_keep_ratio: float = 0.10
    planner_max_combinations: int = 1000
    # policy_score 每下降 1 分，综合分额外扣除此系数；政策仍是软约束，不直接剔除。
    planner_policy_penalty_weight: float = 0.30

    # ---------------- 数据库 ----------------
    # 默认 SQLite（零依赖演示）；生产可切换 MySQL（mysql+pymysql://...）
    # Keep the documented GOGO_DATABASE_URL name while accepting the plain
    # DATABASE_URL convention used by common deployment platforms.
    database_url: str = Field(
        default="sqlite:///./gogo_travel.db",
        validation_alias=AliasChoices("GOGO_DATABASE_URL", "DATABASE_URL"),
    )
    redis_url: str | None = None
    memory_cache_ttl_seconds: int = 1800
    api_key_cache_ttl_seconds: int = 604800

    # ---------------- 多实例/集群协调 ----------------
    # cluster_mode 开启后 Redis 是必需依赖：用于跨节点中断广播与请求代际围栏。
    # 默认关闭，保留单机/无 Redis 的本地开发体验。
    cluster_mode: bool = False
    app_instance_id: str | None = None
    interrupt_broadcast_channel: str = "agent:interrupt"
    interrupt_broadcast_poll_seconds: float = 0.25
    session_fence_ttl_seconds: int = 86400

    # ---------------- 百炼知识库/长期记忆 ----------------
    # 默认关闭远程 provider，拿到密钥后显式开启，避免开发环境误发外部请求。
    bailian_memory_enabled: bool = False
    bailian_knowledge_enabled: bool = False
    bailian_access_key_id: str | None = None
    bailian_access_key_secret: str | None = None
    bailian_workspace_id: str | None = None
    bailian_index_id: str | None = None
    bailian_memory_library_id: str | None = None
    bailian_project_id: str | None = None
    bailian_profile_schema: str | None = None
    # 与 AgentScope 1.0.12 默认值一致；可按地域/网关覆盖。
    bailian_memory_endpoint: str = "https://dashscope.aliyuncs.com"
    bailian_knowledge_endpoint: str = "bailian.cn-beijing.aliyuncs.com"
    external_request_timeout_seconds: float = 20.0

    # ---------------- 意图识别 ----------------
    intent_vector_threshold: float = 0.75
    intent_vector_margin: float = 0.05

    # ---------------- 差旅政策城市分级（city-tier.yml） ----------------
    tier1_cities: list[str] = ["北京", "上海", "广州", "深圳"]
    new_tier1_cities: list[str] = [
        "成都", "杭州", "重庆", "武汉", "西安", "苏州", "天津", "南京", "长沙",
        "郑州", "东莞", "青岛", "沈阳", "宁波", "昆明", "厦门", "合肥", "佛山",
        "无锡", "哈尔滨",
    ]
    tier2_cities: list[str] = [
        "济南", "福州", "大连", "贵阳", "太原", "南昌", "南宁", "石家庄", "长春",
        "呼和浩特", "兰州", "乌鲁木齐", "海口", "银川", "西宁", "拉萨",
    ]
    # 高频城市对最短衔接时长（分钟），方向无关；用于行程冲突检测
    city_pair_minutes: dict[str, int] = {
        "北京-上海": 300, "北京-广州": 360, "北京-深圳": 360, "北京-杭州": 300,
        "北京-南京": 270, "北京-天津": 90, "北京-成都": 360, "北京-西安": 330,
        "北京-武汉": 300, "北京-郑州": 240, "上海-广州": 330, "上海-深圳": 330,
        "上海-杭州": 120, "上海-南京": 130, "上海-苏州": 90, "上海-成都": 390,
        "上海-武汉": 300, "广州-深圳": 90, "广州-长沙": 210, "成都-重庆": 120,
    }

    # ---------------- 工具熔断（ToolCircuitBreakerHook） ----------------
    circuit_breaker_enabled: bool = True
    circuit_breaker_monitored_tools: list[str] = ["query_weather", "query_destination_news"]
    circuit_breaker_excluded_tools: list[str] = []
    circuit_breaker_failure_threshold: int = 3
    circuit_breaker_initial_cooldown_seconds: int = 60
    circuit_breaker_backoff_multiplier: float = 2.0
    circuit_breaker_max_cooldown_seconds: int = 600
    circuit_breaker_redis_key_prefix: str = "tool:cb:"
    circuit_breaker_state_ttl_seconds: int = 86400

    # ---------------- 统一工具执行策略 ----------------
    # 所有 Agent 工具均经过 ResilientToolHook；未登记工具采取保守策略。
    tool_policy_enabled: bool = True
    tool_default_timeout_seconds: float = 60.0
    tool_default_max_attempts: int = 3
    tool_retry_initial_backoff_seconds: float = 3.0
    tool_retry_backoff_multiplier: float = 2.0
    tool_retry_max_backoff_seconds: float = 15.0
    tool_retry_jitter_ratio: float = 0.20
    tool_policy_strict_registration: bool = False

    # ---------------- 子 Agent 执行隔离 ----------------
    # local 保持单进程调试路径；process 由 Supervisor 管理域级 Worker。
    subagent_execution_mode: str = "local"
    subagent_worker_count_manage: int = 1
    subagent_worker_count_plan: int = 1
    subagent_worker_count_info: int = 1
    subagent_worker_count_booking: int = 1
    subagent_worker_start_timeout_seconds: float = 20.0
    subagent_worker_shutdown_grace_seconds: float = 5.0
    subagent_worker_max_restarts_per_window: int = 5
    subagent_worker_restart_window_seconds: int = 60
    subagent_deadline_manage_seconds: float = 120.0
    subagent_deadline_plan_seconds: float = 600.0
    subagent_deadline_info_seconds: float = 120.0
    subagent_deadline_booking_seconds: float = 300.0

    # ---------------- 第三方集成（未配置时优雅降级） ----------------
    weather_mcp_endpoint: str | None = None
    weather_mcp_request_timeout_seconds: float = 30.0
    weather_mcp_enabled_tools: list[str] = [
        "城市天气实况", "城市15日预报", "城市天气预警", "城市空气实况",
        "城市24小时预报", "城市40日预报",
    ]
    orizn_visa_api_key: str | None = None
    orizn_mcp_command: str = "npx"
    orizn_mcp_args: list[str] = ["--no-install", "orizn-visa-mcp"]
    orizn_mcp_initialization_timeout_seconds: float = 20.0
    orizn_mcp_request_timeout_seconds: float = 30.0
    orizn_mcp_enabled_tools: list[str] = ["quick_visa_check", "check_visa_requirement"]
    mcp_persistent_sessions: bool = True
    news_api_key: str | None = None
    api_key_encrypt_secret: str = "gogo-travel-default-secret"
    travel_search_providers: list[str] = [
        "tuniu-cli", "flyai", "flight-manager", "rolling-go-hotel",
    ]
    rgh_workspace: str = "./.runtime/rgh-users"
    rgh_token_ttl_seconds: int = 2592000
    rgh_login_watch_timeout_seconds: int = 300
    rgh_workspace_retention_seconds: int = 604800
    rgh_login_output_retention_seconds: int = 3600
    minio_endpoint: str | None = None
    minio_access_key: str | None = None
    minio_secret_key: str | None = None
    minio_bucket: str = "gogo-travel"

    # ---------------- 鉴权 ----------------
    token_timeout_seconds: int = 2592000  # 30 天

    @field_validator(
        "weather_mcp_enabled_tools",
        "orizn_mcp_enabled_tools",
        "orizn_mcp_args",
        "circuit_breaker_monitored_tools",
        "circuit_breaker_excluded_tools",
        "travel_search_providers",
        mode="before",
    )
    @classmethod
    def parse_list_env(cls, value):
        """Accept both Pydantic JSON arrays and Java-style comma lists."""
        if not isinstance(value, str):
            return value
        try:
            decoded = json.loads(value)
            if isinstance(decoded, list):
                return decoded
        except json.JSONDecodeError:
            pass
        return [item.strip() for item in value.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()

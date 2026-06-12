import tomllib
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = BASE_DIR / "app" / "config"


def load_toml(filename: str) -> dict:
    """加载 TOML 配置文件，失败时带文件名和行号"""
    filepath = CONFIG_DIR / filename
    if not filepath.exists():
        raise FileNotFoundError(f"配置文件不存在: {filepath}")
    try:
        with open(filepath, "rb") as f:
            data = tomllib.load(f)
        if data is None:
            raise ValueError(f"配置文件为空: {filepath}")
        return data
    except tomllib.TOMLDecodeError as e:
        lineno = getattr(e, "lineno", None)
        line_info = f" (第 {lineno} 行)" if lineno else ""
        raise ValueError(f"TOML 格式错误 [{filename}{line_info}]: {e}")


class Settings(BaseSettings):
    """应用配置 — 敏感值从 .env 文件/环境变量读取"""

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "广告方向决策子智能体"
    app_version: str = "2.6.0"
    debug: bool = False

    host: str = "0.0.0.0"
    port: int = 8000

    data_source: str = "db"
    use_langgraph: bool = False  # USE_LANGGRAPH=true 时走 LangGraph bridge（默认关闭）
    # mcp 数据源：none / http(sse, Streamable HTTP JSON-RPC) / rest(旧 REST /tools/{name})
    mcp_transport: str = "none"
    # DATA_SOURCE=mcp 时，从 Doris 解析 parent_seller_sku / shop_account / 父 ASIN 供 MCP 工具入参
    mcp_resolve_sku_via_db: bool = True
    # MCP 工具失败或空表时回落 Doris
    mcp_fallback_to_db: bool = True
    # 可选：MCP 上下文专用 DB 地址（如本机隧道 127.0.0.1）；留空则用 db_host
    mcp_db_host: str = ""
    mcp_server_name: str = "user-starrocks-data-server"
    mcp_gateway_url: str = ""
    mcp_gateway_token: str = ""
    mcp_gateway_header_name: str = "Authorization"
    mcp_timeout: float = 1200.0
    mcp_retries: int = 1
    mcp_max_concurrency: int = 115
    mcp_max_connections: int = 120  # MCP httpx 连接池上限，须 ≥ mcp_max_concurrency（留余量防池排队成新瓶颈）
    # ad_keyword_report 匹配类型：逗号分隔 EXACT,BROAD,PHRASE；留空/all 则不传 match_type（全类型）
    mcp_keyword_match_types: str = ""
    mcp_default_shop_account: str = ""
    mcp_default_site_code: str = "Amazon_US"
    mcp_default_parent_seller_sku: str = ""
    mcp_shadow_enabled: bool = False
    # 用户主动刷新时跳过 MCP，直接 Doris（避免 MCP+DB 双跑导致超时）
    mcp_refresh_via_db: bool = True
    # MCP 任一工具失败时，按场景 meta_filter 全量回落 Doris
    mcp_failover_full_db: bool = True
    # Doris 回落整包超时（秒）
    db_failover_timeout: float = 300.0
    # Doris 并行查询单任务超时（秒）
    db_fetch_timeout: float = 180.0
    db_flow_keyword_timeout: float = 90.0
    db_flow_keyword_prefetch_limit: int = 150
    db_flow_keyword_expand_limit: int = 100
    db_use_legacy_flow_sql: bool = False
    db_child_asin_cap: int = 200               # 子 ASIN 截断上限（0 == 不限）
    # 单 MCP 工具超时（秒），超时后按维度回落 Doris
    mcp_tool_timeout: float = 1200.0
    mcp_bootstrap_timeout: float = 180.0
    mcp_context_timeout: float = 30.0
    # 运行时 Skill（playbook.yaml 驱动数据拉取）
    skills_enabled: bool = True
    skills_dir: str = ""
    # 场景化 meta_filter（用于减少不必要 MCP 工具调用）
    meta_filter_tactics: list[str] = [
        "META_AD_PRODUCT", "META_KW_AD", "META_KW_COMPETITOR_RANK", "META_TREND", "META_COMPETITOR",
    ]
    meta_filter_diagnosis: list[str] = [
        "META_AD_PRODUCT", "META_AD_PLACEMENT", "META_KW_AD",
        "META_KW_COMPETITOR_RANK", "META_FLOW_KEYWORD", "META_TREND", "META_COMPETITOR",
    ]
    meta_filter_execution: list[str] = [
        "META_AD_PRODUCT", "META_AD_PLACEMENT", "META_KW_AD",
        "META_KW_COMPETITOR_RANK", "META_FLOW_KEYWORD", "META_TREND",
    ]
    meta_filter_p3: list[str] = [
        "META_AD_PRODUCT", "META_KW_AD", "META_FLOW_KEYWORD", "META_TREND", "META_COMPETITOR",
    ]

    # 长期状态：json | mysql
    state_backend: str = "json"
    state_db_host: str = "127.0.0.1"
    state_db_port: int = 3306
    state_db_user: str = "ad_agent"
    state_db_pass: str = "ad_agent_pass"
    state_db_database: str = "ad_agent_state"

    # 短期 ASIN 数据缓存 Redis
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_enabled: bool = True
    redis_short_ttl: int = 7200
    redis_partial_ttl: int = 900
    redis_lru_ttl: int = 30

    # Campaign 分析
    campaign_discovery_timeout: float = 90.0
    campaign_mcp_tool_timeout: float = 300.0
    campaign_db_fallback_timeout: float = 60.0
    campaign_rank_timeout: float = 45.0          # 自然排名旁路拉取墙钟上限（从 task 启动算）
    campaign_prefilter_enabled: bool = True
    # Campaign LLM 分析
    # 每流(精准/广泛各一)并发上限。单 ASIN 上限 = exact + broad = 2×该值。
    campaign_llm_concurrency: int = 50
    # [DEPRECATED] campaign 不再使用进程级全局闸——全局 LLM 并发已统一收口到
    # client 层 llm_global_concurrency（见 LLM 配置段）。保留仅兼容旧 .env，无实际作用。
    campaign_global_llm_concurrency: int = 24
    campaign_llm_temperature: float = 0.3
    campaign_batch_size: int = 6
    # 纵深防御: 任务级总超时 (默认 900s=15min,防 Semaphore 饥饿永久挂死)
    campaign_total_timeout: int = 900
    # 信号量获取超时 (等 Semaphore 槽位的最长时间,防饿死在 sem 门前)
    campaign_sem_acquire_timeout: int = 120
    # 策略总览(执行总纲)：明细前的宏观方向 AI 文本 + 定性 preamble 注入分批
    # 在主链上，必须 fail-open；Windows 本机有 asyncio 取消缺陷风险，建议仅服务器(Linux)开启
    campaign_overview_enabled: bool = True   # 恢复(2026-06-08)：禁用根因(Win asyncio 取消挂死)已由 main.py SelectorEventLoopPolicy 根治；_run_overview 带 timeout_override=55 + fail-open
    # Campaign 组合(Portfolio)分类与预算汇总(2026-06-02)
    # 4 类 AI 自造逻辑分类: 主推/广泛自动/测试新增/淘汰；非亚马逊后台 Portfolio。
    # 关闭后 ai_portfolio_class 留空、result.budget_summary=None，行为退化到改前。
    campaign_portfolio_enabled: bool = True
    # daily_budget 兜底乘数: 当 override + asin_data.daily_budget 均不可用时
    # 用 (ad_data.spend / days) × multiplier 作兜底目标；与会议拍板的 1.15 一致
    campaign_budget_fallback_multiplier: float = 1.15
    # 3 组合预算约束占比 (2026-06-02 更新):
    #   总预算约束 = 策略上下文 daily_budget
    #             = 主推约束 + 测试约束 + 广泛约束 (3 组之和 == 总约束)
    #   淘汰组完全不在约束概念里 (按 KB 21 §6 每活动 $1,与运营策略预算无关)
    # 这 3 个 share 加起来必须 = 100
    campaign_portfolio_share_main: int = 60   # 主推
    campaign_portfolio_share_test: int = 20   # 测试/新增
    campaign_portfolio_share_broad: int = 20  # 广泛/自动

    # Campaign 新增活动分析 (KB 16, 独立并行分析线)
    campaign_new_enabled: bool = True         # 总开关 (关闭退化到无新增建议)
    campaign_new_batch_size: int = 10         # 每批送 LLM 的候选词数
    campaign_new_max_count: int = 20          # 单次分析最大候选词数 (排序后截断 Top-N)

    # CSV适配器配置
    csv_filename: str = "asin_test_data.xlsx"
    csv_key_column: str = "asin"
    request_timeout: float = 5.0

    # 数据库配置（Doris/StarRocks）— 从环境变量读取，此处仅占位，无 .env 不工作
    db_host: str = ""
    db_port: int = 0
    db_user: str = ""
    db_pass: str = ""
    db_database: str = ""

    # 数据源目录
    data_source_dir: str = "data/source"
    # 持久化目录
    persistence_dir: str = "config"

    # LLM 配置 — DeepSeek API — 从环境变量读取
    deepseek_api_key: str = ""
    deepseek_api_keys: str = ""  # 多 Key 逗号分隔，优先于 deepseek_api_key
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"        # 快档(默认)：其余调用点用，非思考
    # LLM 分级：重要节点(总览/汇总)用强档 = flash + 思考模式；其余沿用 deepseek_model(flash)非思考
    llm_model_strong: str = "deepseek-v4-flash"
    llm_strong_reasoning_effort: str = "high"        # 思考强度：high / max
    llm_timeout: int = 75
    # 服务级 LLM 总并发上限（全服务，所有非流式 chat() 调用共用）。
    # 实际每 worker 信号量 = llm_global_concurrency // num_workers（见 client._global_llm_sem）。
    # 需按 Key 数 × 单 Key/账号 RPM 调；50 key/3 账号场景下定为 240。
    llm_global_concurrency: int = 420
    # uvicorn worker 进程数（从 NUM_WORKERS 环境变量读）。必须与启动命令 --workers 一致，
    # 否则全局并发会被错误切分。用于把 llm_global_concurrency 静态切给各 worker。
    num_workers: int = 1

    # ERP 测试库（Campaign 分析完成后可选自动 write_full）
    erp_auto_write: bool = False
    erp_host: str = "192.168.2.51"
    erp_port: int = 3306
    erp_user: str = "erp_agentadvert"
    erp_password: str = "erp_agentadvert#weilai123"
    erp_database: str = "erp_agentadvert"
    erp_write_timeout: int = 30

    # 原始配置数据（启动时加载）
    _raw_tags: dict | None = None
    _raw_thresholds: dict | None = None
    _raw_layer_options: dict | None = None

    def _ensure_loaded(self):
        if self._raw_tags is None or self._raw_thresholds is None:
            self.reload_config()

    def reload_config(self):
        """重新加载 TOML 配置（开发/运维调整后调用）"""
        self._raw_tags = load_toml("tags.toml")
        self._raw_thresholds = load_toml("thresholds.toml")
        try:
            self._raw_layer_options = load_toml("layer_options.toml")
        except FileNotFoundError:
            self._raw_layer_options = None

    @property
    def llm_config(self) -> dict:
        # 多 Key：DEEPSEEK_API_KEYS 逗号分隔优先，兜底 DEEPSEEK_API_KEY
        keys = []
        if self.deepseek_api_keys:
            keys = [k.strip() for k in self.deepseek_api_keys.split(",") if k.strip()]
        elif self.deepseek_api_key:
            keys = [self.deepseek_api_key]
        state_path = str(BASE_DIR / "config" / "key_pool_state.local.json")
        return {
            "api_key": self.deepseek_api_key,
            "api_keys": keys,
            "base_url": self.deepseek_base_url,
            "model": self.deepseek_model,
            "timeout": self.llm_timeout,
            "state_path": state_path,
        }

    @property
    def tags_config(self) -> dict:
        self._ensure_loaded()
        return self._raw_tags

    @property
    def thresholds_config(self) -> dict:
        self._ensure_loaded()
        return self._raw_thresholds

    @property
    def layer_options_config(self) -> dict | None:
        self._ensure_loaded()
        return self._raw_layer_options


settings = Settings()

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
    mcp_max_concurrency: int = 8
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
    campaign_prefilter_enabled: bool = True
    # Campaign LLM 分析
    # 每流(精准/广泛各一)并发上限。单 ASIN 上限 = exact + broad = 2×该值。
    campaign_llm_concurrency: int = 10
    # 进程级 LLM 总并发上限（跨 ASIN/流共享）。须 ≥ 单 ASIN 上限(2×campaign_llm_concurrency=20)
    # 才不限速单 ASIN；24 给点余量并把多 ASIN 并行从 ~60 收到 24。可调。
    campaign_global_llm_concurrency: int = 24
    campaign_llm_temperature: float = 0.3
    campaign_batch_size: int = 6
    # 策略总览(执行总纲)：明细前的宏观方向 AI 文本 + 定性 preamble 注入分批
    # 在主链上，必须 fail-open；Windows 本机有 asyncio 取消缺陷风险，建议仅服务器(Linux)开启
    campaign_overview_enabled: bool = True

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
    deepseek_model: str = "deepseek-v4-pro"
    llm_timeout: int = 75

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

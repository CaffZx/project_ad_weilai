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

    data_source: str = "mcp"
    use_langgraph: bool = False  # USE_LANGGRAPH=true 时走 LangGraph bridge（默认关闭）
    # mcp 数据源：none / http(sse, Streamable HTTP JSON-RPC) / rest(旧 REST /tools/{name})
    mcp_transport: str = "none"
    # MCP 上下文解析：优先 MCP parent_listing_detail，不回落 DB
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
    # 单 MCP 工具超时（秒）
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
    meta_filter_dashboard_light: list[str] = [
        "META_AD_PRODUCT", "META_TREND",
    ]

    # 长期状态：json | mysql
    state_backend: str = "mysql"
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
    campaign_st_fetch_timeout: float = 180.0    # search_term 预取总超时（所有广泛活动共享）
    # 用 MCP 工具 ad_campaign_product_keyword_list 发现活动 + 关键词
    mcp_discover_campaigns: bool = True
    # 用 MCP 工具 ad_campaign_basic_info_v2 (campaign_id_list) 替代 V1 (campaign_name_list)。
    # V2 稳定后删除此开关及 V1 代码。
    campaign_basic_info_v2: bool = True
    # 用 MCP 工具 parent_listing_detail 替代 mcp_db_context._LOOKUP_SQL（父ASIN→站点/sku/店铺）。
    mcp_resolve_context: bool = True
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

    # 预算回算 LLM agent (KB23): 把 3 组推荐值的产出方式从规则引擎换成 LLM；
    # 失败/超时/校验不过 → 回落 campaign_budget_summary.build_summary (规则兜底)。
    campaign_budget_agent_enabled: bool = True   # 默认开;关闭则纯走规则引擎兜底
    campaign_budget_agent_timeout: int = 240     # 强档(pro+思考)慢,仿 synthesis 至少 240s
    # KB23 parent_allowed_net_increase (父 ASIN 本轮允许净增多少): 无数据源,占位 0,运维可调。
    campaign_parent_allowed_net_increase: float = 0.0
    # ★ ad_portfolio_list MCP 接入（2026-07-09）：拉取真实组合预算替换虚构 60/20/20。
    # 失败回退 settings 的 share_* 兜底值。关掉此开关即纯走旧路径。
    campaign_portfolio_fetch_enabled: bool = True

    # Campaign 新增活动分析 (KB 16, 独立并行分析线)
    campaign_new_enabled: bool = True         # 总开关 (关闭退化到无新增建议)
    campaign_new_history_enabled: bool = False  # AZ history enrichment 开关（关闭则仅用 own 自然位，不查历史趋势）
    campaign_new_batch_size: int = 10         # 每批送 LLM 的候选词数
    campaign_new_max_count: int = 40          # 单次分析最大候选词数 (排序后截断 Top-N；2026-06-18 20→40)
    campaign_new_max_creates: int = 20        # 输出硬截断 (双轮交集后 Top-N，EXACT 优先)
    #   ⚠ 与 KB03 §7「每日最大新词数=15」冲突，暂用 20 待 KB/运营定夺
    # ── 竞品词源 (Step3，reverse-only，默认关；live 验证返回结构后再开) ──
    campaign_new_competitor_enabled: bool = False     # 竞品词源总开关 (默认关)
    campaign_new_competitor_max: int = 3              # 取前 N 个竞品 ASIN 反查 (硬扇出上限)
    campaign_new_competitor_kw_per: int = 100         # 每竞品 reverse 取词数 (page_size)
    campaign_new_competitor_timeout: float = 60.0     # 竞品 MCP 总超时 (fail-open)
    # ── 多源配额 (Step4，仅 competitor 开启时分桶；注释待 KB 最终核) ──
    campaign_new_quota_competitor: int = 20   # 竞品桶
    campaign_new_quota_ranking: int = 15      # 自然位机会桶 (H10：10→15 防回归，事实相关不该砍)
    campaign_new_quota_flow: int = 5          # 流量词库桶 (最低价值通用词，让额)

    # 淘汰活动复评（重启）— KB 21 §7，确定性规则引擎，无 LLM
    campaign_restart_enabled: bool = True             # 总开关
    # §0.1 精准组合升降级：开关开启后精准规则生成 target_group_type
    exact_transition_enabled: bool = True
    campaign_restart_review_days: int = 14            # 入池 ≥N 天才复评 (KB §7)
    campaign_restart_budget: float = 3.0              # 恢复后日预算 (KB §7 情况一/二)
    campaign_restart_bid_floor: float = 0.20          # 情况一 Bid 维持值 / 情况二下限
    campaign_restart_bid_cap: float = 0.5             # 情况二 min(0.5, 均CPC) 上限 (KB §7)
    campaign_restart_high_spend_7d: float = 15.0      # 情况二「淘汰前花费较高」阈值 (借 §1，运营已确认，可调)
    campaign_restart_orders_window_max_days: int = 30 # 在池订单窗口上限 = min(入池天数, 此值)
    campaign_restart_max_candidates: int = 50         # 复评候选封顶 (按入池天数降序取，防大池一次拉爆 MCP)
    campaign_restart_fetch_concurrency: int = 16      # 复评拉在池订单/30dCPC 的并发上限

    # CSV适配器配置
    csv_filename: str = "asin_test_data.xlsx"
    csv_key_column: str = "asin"
    request_timeout: float = 5.0


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
    # 凭据从 .env 注入，此处仅占位；勿把真实密码写成默认值（与 db_* 一致的留空约定）。
    erp_auto_write: bool = False
    erp_host: str = ""
    erp_port: int = 3306
    erp_user: str = ""
    erp_password: str = ""
    erp_database: str = ""
    erp_write_timeout: int = 30
    # ERP 走 TLS：服务器同内网可不开；本机/VPN 经 caching_sha2_password 必须 TLS 才能握手。
    erp_use_tls: bool = False

    # 广告调整 MCP（whp-advert-agent，真实执行广告调整）— Part 6
    # 内网 endpoint 从 .env 注入（ADVERT_MCP_URL），勿硬编码 IP；留空则 client 初始化即报错。
    advert_mcp_url: str = ""
    advert_mcp_token: str = ""                 # Bearer token（sk-…），生产经 .env 注入，勿硬编码
    advert_mcp_timeout: float = 120.0          # 单次工具调用超时（秒）
    advert_mcp_enabled: bool = False           # 总开关：关则 /campaign/execute 直接拒绝
    advert_exec_dry_run: bool = True           # 空跑：构造 payload + 落 advert_record(DRY_RUN)，不真调 MCP
    campaign_sanity_enabled: bool = True                 # Sanity check（分析后校验）
    campaign_synthesis_enabled: bool = False             # 汇总合成 LLM
    campaign_negative_keyword_exec_enabled: bool = True   # 否词执行开关

    # ── 核心词判定（离线，7 天一次）──
    core_keyword_enabled: bool = True              # 主流程是否读取核心词标签。fail-soft：表空/异常返回空 set，不影响主流程
    core_keyword_analyze_enabled: bool = True      # 离线 endpoint 是否允许执行核心词分析
    core_keyword_llm_timeout: float = 120.0        # semantic_core LLM 超时（秒）
    core_keyword_mcp_timeout: float = 420.0        # MCP 拉数总超时（秒）

    # ── Azlisting/ERP 商品信息 MCP（独立于 StarRocks 数据 MCP）──
    azlisting_mcp_url: str = ""
    azlisting_mcp_token: str = ""
    azlisting_mcp_header_name: str = "Authorization"
    azlisting_mcp_timeout: float = 60.0
    azlisting_mcp_max_connections: int = 20
    azlisting_mcp_max_in_flight: int = 5

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
